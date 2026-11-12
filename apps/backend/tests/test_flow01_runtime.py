"""FLOW-01: the bounded collection through the installed OpenClaw gateway and the loopback model stub.

One integration check: two extraction questions in one reply, a third one later, the fourth refused by
AIST without a provider POST, then a refusal to save an unfinished fourth topic. No paid request."""
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
from app.live_profile import MODEL
from app.runs import execute, json_value
from app.service import ident
from test_d15_postgres import workspace
from test_flow01_postgres import CARDS, QUESTIONS, note_run, plain_source, qualitative_item, note_content

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW') != 'true', reason='Requires isolated PostgreSQL and installed OpenClaw')


def test_installed_agent_stops_extracting_and_refuses_unfinished_note(workspace, monkeypatch, tmp_path):
    w = workspace
    with w.engine.connect() as c:
        assert execute(c, 'SELECT current_database()').scalar_one() == w.settings.db_name
        assert w.settings.db_name.startswith('aist_d15_r1_test_workspace_')
        assert execute(c, 'SELECT count(*) FROM aist.runs').scalar_one() == 0
    source = plain_source(w)
    run = note_run(w, source)
    received = []
    out = Path(os.getenv('AIST_FLOW01_EVIDENCE', str(tmp_path))) / 'runtime'
    out.mkdir(parents=True, exist_ok=True)

    def tool_call(name, args):
        return dict(id=ident('call'), type='function', function=dict(name=name, arguments=json_value(args)))

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(data)
            (out / f'{len(received)}-request.json').write_text(json_value(data))
            if data.get('response_format'):
                number = len([r for r in received if r.get('response_format')])
                message = dict(role='assistant', content=json_value(dict(facts=[qualitative_item(*CARDS[QUESTIONS[number - 1]])])))
            else:
                turn = len([r for r in received if not r.get('response_format')])
                if turn == 1:
                    tools = [tool_call('aist_get_facts', dict(question=QUESTIONS[0])), tool_call('aist_get_facts', dict(question=QUESTIONS[1]))]
                elif turn == 2:
                    tools = [tool_call('aist_get_facts', dict(question=QUESTIONS[2]))]
                elif turn == 3:
                    tools = [tool_call('aist_get_facts', dict(question=QUESTIONS[3]))]
                else:
                    facts = [f for m in data['messages'] if m['role'] == 'tool' and m['content'].lstrip().startswith('{')
                             for f in (json.loads(m['content']).get('result') or {}).get('facts', [])]
                    first = dict(facts[0], source=dict(source_version_id=facts[0]['source_version_id']))
                    tools = [tool_call('aist_save_artifact', model_save_args(note_content(first)))]
                message = dict(role='assistant', content=None, tool_calls=tools)
            payload = dict(id=ident('stub'), created=int(time.time()), model=MODEL,
                           choices=[dict(index=0, message=message, finish_reason='stop' if data.get('response_format') else 'tool_calls')],
                           usage=dict(prompt_tokens=100, completion_tokens=50))
            raw = json_value(payload).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    stub = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    thread = threading.Thread(target=stub.serve_forever); thread.start()
    monkeypatch.setattr('app.novita.credential', lambda path: 'nonpaid-local-credential')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw: transport.ReaderConnection('127.0.0.1', stub.server_port, timeout=20))
    with TemporaryDirectory(prefix='f01-') as root:
        with socket.socket() as a, socket.socket() as b:
            a.bind(('127.0.0.1', 0)); b.bind(('127.0.0.1', 0))
            settings = replace(w.settings, openclaw_root=root, transport_seconds=20, lease_seconds=300,
                               gateway_port=a.getsockname()[1], provider_port=b.getsockname()[1], embeddings_enabled=False)
        w.runs.settings = settings
        worker = SimpleNamespace(settings=settings, runs=w.runs, draining=threading.Event(), closed=threading.Event(), persistence=threading.RLock())
        worker.transport = transport.Transport(w.runs, worker.draining, worker.closed, worker.persistence, {}, threading.Lock())
        timer = threading.Timer(240, worker.draining.set); timer.start()
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel(); stub.shutdown(); stub.server_close(); thread.join()
            directory = Path(root) / run['id']
            for name in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / name).exists(): (out / name).write_bytes((directory / name).read_bytes())
    state = w.runs.get(w.actor, w.project, run['id'])
    with w.engine.connect() as c:
        versions = list(execute(c, 'SELECT data FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalars())
        events = list(execute(c, 'SELECT data FROM aist.run_events WHERE run_id=:r ORDER BY seq', r=run['id']).scalars())
        calls = list(execute(c, 'SELECT data FROM aist.model_calls WHERE run_id=:r ORDER BY created_at,id', r=run['id']).scalars())
    agent_requests = [r for r in received if not r.get('response_format')]
    extractions = [r for r in received if r.get('response_format')]
    journal = [json.loads(line) for line in (out / 'tools.jsonl').read_text().splitlines()]
    (out / 'measurements.json').write_text(json.dumps(dict(database=w.settings.db_name, state=state['state'], error=state['error'], stub_posts=len(received),
        agent_posts=len(agent_requests), extraction_posts=len(extractions), paid_posts=0, versions=len(versions),
        tool_replies=[(e['request']['name'], e['reply']['ok'], (e['reply'].get('error') or {}).get('code')) for e in journal],
        closed=[e['data'] for e in events if e['type'] == 'collection_closed']), ensure_ascii=False, indent=2))
    # Запрошенная четвертая тема осталась без результата. Исчерпание квоты
    # не разрешает выдать ее за завершенный сбор и опубликовать записку.
    assert state['state'] == 'failed' and not versions, state
    assert state['error']['code'] == 'validation_failed'
    assert len(extractions) == 3 and len(agent_requests) == 5 and len(calls) == 8
    assert all(c['purpose'] == ('extraction' if r.get('response_format') else 'agent') for c, r in zip(calls, received))
    assert [e['data']['reason'] for e in events if e['type'] == 'collection_closed'] == ['extractions']
    hint = agent_requests[3]['messages'][0]['content']
    assert 'Режим завершения' in hint and QUESTIONS[2] in hint
    refused = [e for e in journal if e['request']['name'] == 'aist_get_facts' and not e['reply']['ok']]
    assert len(refused) == 1 and refused[0]['reply']['error']['code'] == 'budget_exceeded'
    assert refused[0]['request']['args'].get('question') == QUESTIONS[3] and 'Сбор оснований закрыт' in refused[0]['reply']['error']['message']
    assert [e['request']['name'] for e in journal] == ['aist_get_facts'] * 4 + ['aist_save_artifact'] * 2
    assert all(not e['reply']['ok'] and e['reply']['error']['code'] == 'validation_failed' for e in journal[-2:])
    assert all(marker not in json_value(received) for marker in ('characters truncated', 'middle content omitted'))
