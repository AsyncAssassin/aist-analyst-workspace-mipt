"""FLOW-01.02-R1: the installed OpenClaw gateway receives a prior partial extraction in its first request and saves
the note at once, without any further extraction or cache read; the saved draft carries the blocking coverage
remark and cannot be accepted or released (review F01).

One integration check on the loopback model stub, the installed OpenClaw and the series database. No paid request."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace

import pytest
from test_perf02_r4_postgres import model_save_args

from app import openclaw_runtime as runtime, transport
from app.errors import ApiError
from app.live_profile import MODEL
from app.runs import execute, json_value
from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import accept
from test_flow01_02_postgres import MIXED, QUAL, TABLE, checkpoint, context, coverage_findings, human_edit, ingest, note
from test_flow01_02_r1_postgres import TASK, blocked, follow_up

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW') != 'true', reason='Requires isolated PostgreSQL and installed OpenClaw')


def delivered_extractions(request):
    """The completed_extractions of an agent request, parsed from the compact JSON of its task message."""
    content = [m for m in request['messages'] if m['role'] == 'user'][-1]['content']
    start = content.index('"completed_extractions":') + len('"completed_extractions":')
    value, _ = json.JSONDecoder().raw_decode(content, start)
    return value


def test_the_partial_extraction_delivered_at_the_start_binds_the_saved_note(workspace, monkeypatch, tmp_path):
    w = workspace
    source, first, pages, layouts = context(w, MIXED)
    partial, call_id = ingest(w, first, pages, layouts, [QUAL, TABLE], TASK)
    assert partial['status'] == 'partial' and [s['code'] for s in partial['skipped']] == ['unsupported_layout']
    qualitative = partial['facts'][0]
    w.runs.stop(first['id'], first['epoch'], 'validation_failed')
    run = follow_up(w, first, source, TASK)
    assert run['input']['base_version_id'] is None and checkpoint(w, run) is None
    received = []
    out = Path(os.getenv('AIST_FLOW01_EVIDENCE', str(tmp_path))) / 'startup-context'
    out.mkdir(parents=True, exist_ok=True)

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(data)
            (out / f'{len(received)}-request.json').write_text(json_value(data))
            # The agent saves the note by the delivered qualitative fact at once: no extraction, no cache read.
            tool = dict(id=ident('call'), type='function', function=dict(name='aist_save_artifact',
                        arguments=json_value(model_save_args(note(qualitative)))))
            payload = dict(id=ident('stub'), created=int(time.time()), model=MODEL,
                           choices=[dict(index=0, message=dict(role='assistant', content=None, tool_calls=[tool]), finish_reason='tool_calls')],
                           usage=dict(prompt_tokens=100, completion_tokens=50))
            raw = json_value(payload).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    stub = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    thread = threading.Thread(target=stub.serve_forever); thread.start()
    monkeypatch.setattr('app.novita.credential', lambda path: 'nonpaid-local-credential')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw: transport.ReaderConnection('127.0.0.1', stub.server_port, timeout=20))
    with TemporaryDirectory(prefix='f02r1-') as root:
        with socket.socket() as a, socket.socket() as b:
            a.bind(('127.0.0.1', 0)); b.bind(('127.0.0.1', 0))
            settings = replace(w.settings, openclaw_root=root, transport_seconds=20, lease_seconds=300,
                               gateway_port=a.getsockname()[1], provider_port=b.getsockname()[1], embeddings_enabled=False)
        w.runs.settings = settings
        worker = SimpleNamespace(settings=settings, runs=w.runs, draining=threading.Event(), closed=threading.Event(), persistence=threading.RLock())
        worker.transport = transport.Transport(w.runs, worker.draining, worker.closed, worker.persistence, {}, threading.Lock())
        timer = threading.Timer(120, worker.draining.set); timer.start()
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel(); stub.shutdown(); stub.server_close(); thread.join()
            directory = Path(root) / run['id']
            for name in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / name).exists(): (out / name).write_bytes((directory / name).read_bytes())
    state = w.runs.get(w.actor, w.project, run['id'])
    with w.engine.connect() as c:
        versions = list(execute(c, 'SELECT id FROM aist.artifact_versions WHERE project_id=:p ORDER BY revision', p=w.project).scalars())
    journal = [json.loads(line) for line in (out / 'tools.jsonl').read_text().splitlines()] if (out / 'tools.jsonl').exists() else []
    delivered = delivered_extractions(received[0]) if received else []
    recorded = (checkpoint(w, run) or {}).get('extractions', [])
    view = w.runs.version(w.actor, w.project, versions[0]) if versions else None
    gaps = coverage_findings(view) if view else []
    acceptance = None
    if view:
        try:
            status, review = accept(w, view, acknowledge_unchecked_semantics=False)
            acceptance = bool(status == 200 and review['acceptance'])
        except ApiError as error:
            acceptance = error.code
    measured = dict(state=state['state'], error=state['error'], stub_posts=len(received), paid_posts=0, versions=len(versions),
                    delivered=[(e['question'], e['status'], [s['code'] for s in e.get('skipped', [])]) for e in delivered],
                    recorded_before_save=[(e['call_id'], e['status']) for e in recorded],
                    tool_calls=[(e['request']['name'], e['reply']['ok']) for e in journal],
                    programmatic_status=view and view['checks'][0]['status'], coverage_gaps=[g['message'] for g in gaps],
                    acceptance=acceptance, first_run=first['id'], current_run=run['id'])
    (out / 'measurements.json').write_text(json.dumps(measured, ensure_ascii=False, indent=2))
    assert state['state'] == 'completed' and len(versions) == 1 and len(received) == 1, measured
    # The first request carried exactly the bound result, recorded before the agent could save.
    assert len(delivered) == len(recorded) == 1 and recorded[0]['call_id'] == call_id and recorded[0]['status'] == 'partial'
    for key in ('source', 'question', 'status', 'fact_refs', 'skipped'):
        assert delivered[0][key] == recorded[0][key]
    assert 'call_id' not in delivered[0]
    assert [(e['request']['name'], e['reply']['ok']) for e in journal] == [('aist_save_artifact', True)]
    assert view['checks'][0]['status'] == 'passed' and len(gaps) == 1, measured
    assert 'Часть запроса «' + TASK + '» не подтверждена: 1 из 1' in gaps[0]['message'] and 'unsupported_layout' in gaps[0]['message']
    assert acceptance == 'validation_failed'
    blocked(w, view)
    # A reload through the API and an unrelated manual edit keep the remark.
    assert coverage_findings(w.runs.version(w.actor, w.project, view['version']['id'])) == gaps
    edited = human_edit(w, view, 'Компания поставляет оборудование своим клиентам.')
    assert coverage_findings(edited) == gaps and edited['version']['provenance']['run_id'] is None
    blocked(w, edited)
    assert all(marker not in json_value(received) for marker in ('characters truncated', 'middle content omitted'))
