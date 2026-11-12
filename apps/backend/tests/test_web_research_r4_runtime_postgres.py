"""Два контекста установленного OpenClaw, общий Run, только локальные модели-имитаторы."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
from tempfile import TemporaryDirectory
import threading
import time

import pytest
from test_perf02_r4_postgres import model_save_args
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app import anthropic_messages, note_external, openclaw_runtime as runtime, transport
from app.live_profile import FABLE_FULL_ID, FABLE, MODEL
from app.runs import execute, json_value
from test_d15_postgres import workspace
from test_fable_postgres import start_fable, audit, reply
from test_flow01_postgres import executor
from test_perf02_r4_postgres import note_source, small_note
from test_web_research_postgres import reader, QUERY, URL, TEXT

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW') != 'true', reason='Только установленный OpenClaw и собственная PostgreSQL')


@pytest.mark.parametrize('provider', ['fable', 'novita'])
@pytest.mark.parametrize('money_limited', [False, True])
def test_installed_two_contexts_refine_and_save_one_run(workspace, monkeypatch, provider, money_limited):
    w = workspace
    source, _ = note_source(w)
    profile = FABLE_FULL_ID if provider == 'fable' else 'model_novita-mimo-v1'
    if money_limited:
        import test_fable_postgres as fixtures
        original_send = fixtures.send
        def small_budget(*args, **kwargs):
            w.runs.policy['quote_usd'] = '0.005'
            w.runs.policy['run']['limit'] = '0.012'
            return original_send(*args, **kwargs)
        monkeypatch.setattr(fixtures, 'send', small_budget)
    _, _, run = start_fable(w, full=True, source=source, profile_id=profile, research=True)
    note_external.prepare_all(w.runs, run)
    requests, public_calls, prefix_errors = [], [], []
    counts = dict(analyst=0, researcher=0)
    histories = {}
    agent_invocations, failed_polls = [], []
    research_thread = threading.local()
    gateway_request = runtime.Gateway.request
    read_row = runtime.row

    def invoke(gateway, method, params):
        result = gateway_request(gateway, method, params)
        if method == 'agent':
            agent_invocations.append(params.get('agentId', 'main'))
            if params.get('agentId') == 'researcher' and not failed_polls:
                research_thread.poll_failure = True
        return result

    def poll(connection, statement, **params):
        if (getattr(research_thread, 'poll_failure', False)
                and statement == 'SELECT * FROM aist.runs WHERE id=:r'):
            research_thread.poll_failure = False
            failed_polls.append('researcher')
            raise PoolTimeout('synthetic checkout timeout in research poll')
        return read_row(connection, statement, **params)

    monkeypatch.setattr(runtime.Gateway, 'request', invoke)
    monkeypatch.setattr(runtime, 'row', poll)
    base = reader(public_calls)
    def public_request(name, *args):
        result = base(name, *args)
        if name == 'aist_web_fetch':
            result.update(quote=TEXT*150, truncated=True)
        return result
    monkeypatch.setattr(note_external, 'web_request', public_request)

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert not any(name.lower().startswith('x-aist-') for name in self.headers)
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            names = [tool.get('name') or tool.get('function', {}).get('name') for tool in request.get('tools', [])]
            role = 'analyst' if 'aist_research' in names else 'researcher'
            counts[role] += 1
            turn = counts[role]
            requests.append(dict(role=role, request=request))
            key = json_value(request['messages'][0])
            if provider == 'fable' and key in histories:
                previous, response = histories[key]
                prefix = previous['messages'] + [dict(role='assistant', content=response['content'])]
                if request.get('system') != previous.get('system') or request['messages'][:len(prefix)] != prefix:
                    prefix_errors.append(role)
            name, args, ending = None, None, None
            if role == 'researcher':
                if turn == 1:
                    name, args = 'aist_read_source', dict(source_version_id='sv_not_public', page=1)
                elif turn in (2, 5):
                    name, args = 'aist_web_search', dict(QUERY, objective='Рынок' if turn == 2 else 'Уточнение периода')
                elif turn in (3, 6):
                    with w.engine.connect() as c:
                        search_id = execute(c, "SELECT id FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_web_search' ORDER BY data->>'started_at' DESC LIMIT 1", r=run['id']).scalar_one()
                    name, args = 'aist_web_fetch', dict(search_id=search_id, url=URL)
                else:
                    ending = 'Найдено описание рынка. Источники сохранены; сопоставимость требует анализа.'
            elif turn in (1, 2):
                name, args = 'aist_research', dict(company='Орбита', objective=(
                    'Рынок оборудования России за 2025 год' if turn == 1 else 'Уточни период и сегмент оборудования'))
            else:
                with w.engine.connect() as c:
                    context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
                item = context['items'][0]
                if turn == 3:
                    name, args = 'aist_read_web', dict(evidence_id=item['evidence_id'], offset=12000)
                else:
                    content = small_note(source)
                    content['sections'][-1]['blocks'].append(dict(kind='paragraph', block_id='research_r4', claim=dict(
                        text=TEXT, origin='external_context', external_evidence_ids=[item['evidence_id']], fact_refs=[], calculation_ids=[])))
                    name, args = 'aist_save_artifact', model_save_args(content)
            identifier = f'tool_{role}_{turn}'
            if provider == 'fable':
                value = reply(name, args, identifier=identifier) if name else dict(
                    id=identifier, type='message', role='assistant', model=FABLE['model'],
                    content=[dict(type='text', text=ending)], stop_reason='end_turn', stop_sequence=None,
                    usage=dict(input_tokens=100, output_tokens=50))
                histories[key] = (deepcopy(request), deepcopy(value))
                raw = anthropic_messages.to_sse(value).encode()
            else:
                message = dict(role='assistant', content=ending)
                if name:
                    message['tool_calls'] = [dict(id=identifier, type='function', function=dict(name=name, arguments=json_value(args)))]
                value = dict(id=identifier, created=int(time.time()), model=MODEL,
                    choices=[dict(index=0, message=message, finish_reason='tool_calls' if name else 'stop')],
                    usage=dict(prompt_tokens=100, completion_tokens=50))
                raw = json_value(value).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    output = Path('/evidence') / (os.getenv('AIST_RESEARCH_EVIDENCE', 'two-contexts') + '-' + provider + ('-budget' if money_limited else ''))
    output.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr('app.novita.credential', lambda path: 'local-only')
    monkeypatch.setattr(anthropic_messages, 'credential', lambda path: 'local-only')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **k: transport.ReaderConnection('127.0.0.1', server.server_port, timeout=20))
    try:
        with TemporaryDirectory(prefix='two-contexts-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as model:
            gateway.bind(('127.0.0.1', 0)); model.bind(('127.0.0.1', 0))
            w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=model.getsockname()[1])
            gateway.close(); model.close()
            worker = executor(w)
            gateways = []
            original_popen = runtime.subprocess.Popen
            def launch(args, **kwargs):
                process = original_popen(args, **kwargs)
                if args[0] == runtime.CLI:
                    gateways.append(process.pid)
                return process
            monkeypatch.setattr(runtime.subprocess, 'Popen', launch)
            timer = threading.Timer(220, worker.draining.set)
            timer.start()
            try:
                runtime.run(worker, run)
            finally:
                timer.cancel()
                directory = Path(root)
                for path in directory.rglob('*'):
                    if path.is_file() and (path.name in ('tools.jsonl', 'gateway-process.log', 'failure.json', 'executor.json') or 'calls' in path.parts):
                        target = output / path.relative_to(directory)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
    finally:
        server.shutdown(); server.server_close(); thread.join()
        (output/'requests.json').write_text(json_value(requests))
        state = w.runs.get(w.actor, w.project, run['id'])
        calls, usage = audit(w, run)
        (output/'result.json').write_text(json_value(dict(state=state, calls=calls, usage=usage, public_calls=public_calls, prefix_errors=prefix_errors)))
    assert len(gateways) == 1
    assert failed_polls == ['researcher']
    assert agent_invocations.count('researcher') == (1 if money_limited else 2)
    if money_limited:
        with w.engine.connect() as c:
            context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        assert state['state'] == 'budget_exceeded', state
        assert not context.get('research_pending')
        assert all(call['transport_state'] == 'observed' for call in calls)
        # Novita's fixture costs 0.0035 USD per reply. Its third reservation
        # fits exactly at 0.012, while Fable's 0.0040 replies allow only two.
        expected_calls = 2 if provider == 'fable' else 3
        assert len(calls) == len(usage) == expected_calls
        assert counts == dict(analyst=1, researcher=expected_calls-1)
        spent = sum(Decimal(entry['estimated_usd']['value']) for entry in usage)
        limit = Decimal(run['input']['budget']['limit'])
        assert spent <= limit < spent + Decimal('0.005')
        return
    assert state['state'] == 'completed', state
    assert counts == dict(analyst=4, researcher=7)
    assert len(calls) == len(usage) == 11 and not prefix_errors
    expected_parents = [None, 0, 1, 2, 3, 0, 5, 6, 7, 5, 9]
    assert [call['parent_model_call_id'] for call in calls] == [
        calls[index]['id'] if index is not None else None for index in expected_parents]
    assert len(public_calls) == 4
    for entry in requests:
        names = {tool.get('name') or tool.get('function', {}).get('name') for tool in entry['request'].get('tools', [])}
        text = json_value(entry['request']['messages'])
        if entry['role'] == 'researcher':
            assert names == {'aist_web_search', 'aist_web_fetch', 'aist_read_web'}
            assert source['id'] not in text and 'Подготовь справку по сохраненным фактам' not in text
        else:
            assert not names.intersection({'aist_web_search', 'aist_web_fetch'})
            assert 'Подготовь справку по сохраненным фактам' in text
    saved = w.runs.version(w.actor, w.project, state['result_version_id'])
    assert saved['review']['acceptance'] is None
    assert len(saved['version']['basis']['external_context']['items'][0]['quote']) > 12000
    assert len(saved['version']['basis']['external_context']['items']) == 2
    assert 'research_pending' not in saved['version']['basis']['external_context']
    (output/'saved.json').write_text(json_value(saved))
