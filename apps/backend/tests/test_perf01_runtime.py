"""Replay the saved large reply through installed OpenClaw and local HTTP only."""
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace

import pytest
from test_perf02_r4_postgres import model_save_args

from app import openclaw_runtime as runtime, transport
from app.config import Settings
from app.live_profile import MODEL
from app.model_context import model_view
from app.extraction import candidate
from test_d10 import PIN, PAGES, item
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import candidate as pdf_candidate, upload_pdf
from test_d15_postgres import workspace


pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW') != 'true', reason='Requires installed OpenClaw and isolated PostgreSQL')


def test_plugin_deadlines():
    result = subprocess.run([runtime.NODE, '--test', str(runtime.ROOT / 'executor/openclaw/plugin/index.test.mjs')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_large_reply_installed_runtime(workspace, monkeypatch, tmp_path):
    w = workspace
    history = os.getenv('AIST_RUNTIME_REPLAY')
    if history:
        replies = [json.loads(line)['reply'] for line in (Path(history)/'tools.jsonl').read_text().splitlines()
                   if json.loads(line)['reply'].get('ok') and 'facts' in json.loads(line)['reply'].get('result', {})]
    else:
        fact, _ = candidate(PIN, PAGES, item())
        fact['evidence'][0]['quote'] += '\n' + 'Synthetic shared source qualification. ' * 180
        facts = [dict(deepcopy(fact), fact_id='fact_synthetic_' + str(i)) for i in range(12)]
        replies = [dict(ok=True, tool_call_id='call_original', result=dict(facts=facts))]
    large = max(replies, key=lambda r: len(json_value(r)))
    all_facts = [f for reply in replies for f in reply['result']['facts']]
    out = Path(os.getenv('AIST_RUNTIME_EVIDENCE', str(tmp_path))) / 'runtime'
    out.mkdir(exist_ok=True)
    results = []
    for mode in ('before', 'after', 'initial-facts', 'initial-base'):
        source = upload_pdf(w)
        request = body(sources=[source['id']], question='Неплатное воспроизведение сохраненного контекста')
        request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
        send(w, conversation(w), request)
        run = w.runs.claim('perf01_runtime')
        directory = Path('/tmp') / ('p01-' + secrets.token_hex(4)); directory.mkdir()
        received = []
        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(data)
                (out/(mode+'-'+str(len(received))+'-request.json')).write_text(json_value(data))
                message = dict(role='assistant', content='Локальная проверка завершена.')
                if mode in ('before', 'after') and len(received) == 1:
                    message = dict(role='assistant', content=None, tool_calls=[dict(id='call_replay', type='function',
                        function=dict(name='aist_get_facts', arguments=json_value(dict(refs=[]))))])
                payload = dict(id=ident('stub'), created=int(time.time()), model=MODEL,
                    choices=[dict(index=0, message=message, finish_reason='tool_calls' if message.get('tool_calls') else 'stop')],
                    usage=dict(prompt_tokens=100, completion_tokens=10))
                raw = json_value(payload).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        stub = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        stub_thread = threading.Thread(target=stub.serve_forever); stub_thread.start()
        monkeypatch.setattr('app.novita.credential', lambda path: 'nonpaid-local-credential')
        monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw: transport.ReaderConnection('127.0.0.1', stub.server_port, timeout=3))
        worker = SimpleNamespace(runs=w.runs, draining=threading.Event(), closed=threading.Event(), persistence=threading.RLock())
        worker.transport = transport.Transport(w.runs, worker.draining, worker.closed, worker.persistence, {}, threading.Lock())
        token = secrets.token_hex(32)
        class ReplayBoundary:
            def __init__(self):
                self.token = token
                self.profile = runtime.PROFILE
                self.reference_labels = {}
            def model(self, value):
                return worker.transport.perform(run, {'purpose':'agent'}, request=value, audit=directory/'calls')[1]
            def tool(self, value):
                assert value['name'] == 'aist_get_facts' and value['args'] == {'refs': []}
                return dict(large, tool_call_id=value['tool_call_id'])
        class LegacyToolHandler(runtime.ToolHandler):
            def handle(self):
                value = json.loads(self.rfile.readline())
                self.wfile.write(json_value(self.server.boundary.tool(value)).encode()+b'\n')
        http = runtime.PrivateHTTPServer(('127.0.0.1', 0), runtime.ProviderHandler); http.boundary=ReplayBoundary()
        unix = runtime.PrivateUnixServer(str(directory/'tools.sock'), LegacyToolHandler if mode=='before' else runtime.ToolHandler)
        unix.boundary=http.boundary
        threads=[threading.Thread(target=s.serve_forever) for s in (http, unix)]
        for t in threads:t.start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); gateway_port=probe.getsockname()[1]
        settings=Settings(gateway_port=gateway_port, provider_port=http.server_port)
        runtime.prepare(settings, directory, token, 'skill_analytical-note-v3', 'analytical_note', new_artifact=True,
                        source_versions=run['input']['source_versions'])
        env=runtime.environment(directory,token)
        message='Получить готовые факты инструментом и завершить проверку.'
        if mode.startswith('initial'):
            state=deepcopy(run)
            state['input']['source_versions']=[all_facts[0]['source']]
            state['input']['base_version_id']='av_saved' if mode=='initial-base' else None
            base=dict(id='av_saved', payload=dict(content=dict(title='Сохраненная версия', sections=[])), basis=dict(facts=all_facts, calculations=[]))
            # A view carries the checks of the selected version; this saved base has none.
            versions=SimpleNamespace(version=lambda *args:dict(version=base,checks=[],review=dict(acceptance=None)))
            message=runtime.execution_message(versions,state,'Сократи по готовым фактам',all_facts)
        log=(directory/'gateway.log').open('w')
        process=subprocess.Popen([runtime.CLI,'gateway','run'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            until=time.monotonic()+150
            while True:
                assert process.poll() is None, (directory/'gateway.log').read_text()[-4000:]
                try:
                    with socket.create_connection(('127.0.0.1',gateway_port),timeout=.2): break
                except OSError:
                    assert time.monotonic()<until
                    time.sleep(.2)
            gateway=runtime.Gateway(settings,directory,env)
            gateway.request('agent',dict(sessionKey='agent:main:explicit:'+run['id'],message=message,idempotencyKey=run['id']))
            expected=2 if mode in ('before','after') else 1
            until=time.monotonic()+100
            while len(received)<expected:
                assert time.monotonic()<until, (directory/'gateway.log').read_text()[-4000:]
                time.sleep(.2)
            actual=received[-1]
            assert all(request['thinking'] == {'type': 'disabled'} and request['enable_thinking'] is False
                       for request in received)
            encoded=json_value(actual)
            omitted=any(marker in encoded for marker in ('middle content omitted','characters truncated'))
            entry=dict(mode=mode,http_bytes=len(encoded.encode()),http_characters=len(encoded),truncated=omitted,paid_posts=0,stub_posts=len(received))
            if mode in ('before','after'):
                tool=next(m for m in actual['messages'] if m['role']=='tool')
                entry['tool_characters']=len(tool['content'])
                if mode=='before': assert omitted
                else:
                    assert not omitted
                    parsed=json.loads(tool['content'])
                    assert parsed==dict(model_view(large),tool_call_id='call_replay')
            else:
                assert not omitted
                content=next(m['content'] for m in actual['messages'] if m['role']=='user')
                context=json.JSONDecoder().raw_decode(content[content.index('{"task":'):])[0]
                got=context['base_version']['basis']['facts'] if mode=='initial-base' else context['facts']
                assert len(got)==len(all_facts)
                assert {f['fact_id'] for f in got}=={f['fact_id'] for f in all_facts}
                entry['facts']=len(got)
            results.append(entry)
            (out/'measurements.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            log.close()
            for server in (http,unix,stub):server.shutdown();server.server_close()
            for t in threads+[stub_thread]:t.join()
    assert len(results)==4


@pytest.mark.parametrize('valid', [True, False], ids=['facts', 'refusal'])
def test_delayed_question_reaches_installed_agent(workspace, monkeypatch, tmp_path, valid):
    w = workspace
    source = upload_pdf(w)
    request = body(sources=[source['id']], question='Подготовь справку о выручке за 3M23')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
    send(w, conversation(w), request)
    run = w.runs.claim('delayed_question')
    received = []
    delay_seconds = 18
    extracted = pdf_candidate()
    if not valid:
        extracted['amount']['value'] = '999'
    out = Path(os.getenv('AIST_RUNTIME_EVIDENCE', str(tmp_path))) / ('question-' + ('facts' if valid else 'refusal'))
    out.mkdir(exist_ok=True)

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(data)
            (out / f'{len(received)}-request.json').write_text(json_value(data))
            if data.get('response_format'):
                time.sleep(delay_seconds)
                message = dict(role='assistant', content=json_value(dict(facts=[extracted])))
            else:
                tool_messages = [m for m in data['messages'] if m['role'] == 'tool']
                name = 'aist_get_facts'
                args = dict(question='Revenue 3M23')
                if tool_messages:
                    # Даже при прежнем tool deadline завершаем изолированный Run штатно.
                    name = 'aist_request_input'
                    args = dict(idempotency_key=ident('key'), expected_revision=run['revision'], prompt='Уточните период.')
                    if valid and tool_messages[-1]['content'].lstrip().startswith('{'):
                        reply = json.loads(tool_messages[-1]['content'])
                        if reply.get('ok') and reply['result'].get('facts'):
                            fact = reply['result']['facts'][0]
                            ref = dict(source_version_id=source['id'], fact_id=fact['fact_id'])
                            table_row = dict(row_id='row_revenue', label=fact['metric'], period_label=fact['period']['label'],
                                             amount=fact['amount'], fact_refs=[ref], calculation_ids=[])
                            block = dict(block_id='block_revenue', kind='table', caption='Выручка', rows=[table_row])
                            section = dict(section_id='sec_main', title='Финансовые показатели', blocks=[block])
                            payload = dict(result_type='analytical_note', schema_version='1.0.0', content=dict(
                                title='Компания: справка', audience='Аналитик', sections=[section]))
                            name = 'aist_save_artifact'
                            args = dict(idempotency_key=ident('key'), expected_revision=0, artifact_id=None,
                                        parent_version_id=None, payload=payload,
                                        ownership_review=model_save_args(payload['content'])['ownership_review'])
                message = dict(role='assistant', content=None, tool_calls=[dict(id=ident('call'), type='function',
                    function=dict(name=name, arguments=json_value(args)))])
            payload = dict(id=ident('stub'), created=int(time.time()), model=MODEL,
                choices=[dict(index=0, message=message, finish_reason='tool_calls' if message.get('tool_calls') else 'stop')],
                usage=dict(prompt_tokens=100, completion_tokens=50))
            raw = json_value(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    stub = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    stub_thread = threading.Thread(target=stub.serve_forever)
    stub_thread.start()
    monkeypatch.setattr('app.novita.credential', lambda path: 'nonpaid-local-credential')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw:
                        transport.ReaderConnection('127.0.0.1', stub.server_port, timeout=30))
    with TemporaryDirectory(prefix='qd-') as root:
        with socket.socket() as gateway_probe, socket.socket() as provider_probe:
            gateway_probe.bind(('127.0.0.1', 0))
            provider_probe.bind(('127.0.0.1', 0))
            settings = replace(w.settings, openclaw_root=root, transport_seconds=30, lease_seconds=300,
                               gateway_port=gateway_probe.getsockname()[1], provider_port=provider_probe.getsockname()[1])
        w.runs.settings = settings
        worker = SimpleNamespace(settings=settings, runs=w.runs, draining=threading.Event(), closed=threading.Event(),
                                 persistence=threading.RLock())
        worker.transport = transport.Transport(w.runs, worker.draining, worker.closed, worker.persistence, {}, threading.Lock())
        watchdog = threading.Timer(120, worker.draining.set)
        watchdog.start()
        try:
            runtime.run(worker, run)
        finally:
            watchdog.cancel()
            stub.shutdown()
            stub.server_close()
            stub_thread.join()
            directory = Path(root) / run['id']
            for name in ('tools.jsonl', 'gateway-process.log'):
                if (directory / name).exists():
                    (out / name).write_bytes((directory / name).read_bytes())

    agent_requests = [r for r in received if not r.get('response_format')]
    assert len(agent_requests) >= 2, received
    tool = next(m for m in agent_requests[1]['messages'] if m['role'] == 'tool')
    with w.engine.connect() as connection:
        facts = list(execute(connection, 'SELECT data FROM aist.facts WHERE project_id=:p', p=w.project).scalars())
        versions = list(execute(connection, "SELECT data->'basis' FROM aist.artifact_versions WHERE project_id=:p", p=w.project).scalars())
    state = w.runs.get(w.actor, w.project, run['id'])['state']
    (out / 'measurements.json').write_text(json.dumps(dict(delay_seconds=delay_seconds, paid_posts=0,
        stub_posts=len(received), saved_facts=len(facts), saved_versions=len(versions), run_state=state,
        agent_tool_content=tool['content']), ensure_ascii=False, indent=2))
    assert 'tool deadline' not in tool['content'], tool['content']
    reply = json.loads(tool['content'])
    assert len([r for r in received if r.get('response_format')]) == 1
    assert reply['ok'] is True
    assert reply['result']['status'] == ('complete' if valid else 'empty')
    if not valid:
        assert reply['result']['facts'] == [] and reply['result']['skipped'][0]['code'] == 'ambiguous_link'
    if valid:
        assert len(facts) == 1 and len(versions) == 1 and state == 'completed'
        assert reply['result']['facts'] == model_view(dict(facts=facts))['facts']
        assert versions[0]['facts'] == facts
    else:
        assert reply['result']['skipped'][0]['metric'] == 'Revenue'
        assert facts == [] and versions == [] and state == 'waiting_for_user'
