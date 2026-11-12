"""Native Fable boundary: installed OpenClaw, isolated databases and an internal HTTP simulator."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace

import pytest
from test_perf02_r4_postgres import model_save_args

from app import anthropic_messages, collection, openclaw_runtime as runtime, runs as run_clock, transport
from app.accounting import response_fields
from app.errors import ApiError
from app.live_profile import FABLE, FABLE_ID, FABLE_FULL, FABLE_FULL_ID, bundle_for, delivered
from app.normalization import fingerprint
from app.runs import execute, json_value
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_flow01_postgres import executor
from test_flow01_postgres import current
from test_flow01_02_postgres import MIXED, GOOD, QUAL, TRLN, note
from test_flow01_05_postgres import ask
from test_perf02_r1_postgres import upload, fact_rows
from test_perf02_r4_postgres import note_source, small_note

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
THINKING = dict(type='thinking', thinking='Synthetic planning with signed native continuity.', signature='c3ludGhldGljLXNpZ25hdHVyZQ==')
THEMATIC_LINES = MIXED[:7] + ['Выручка, млн руб.', '80', '3M22', 'Стратегия компании', 'Компания расширяет сеть продаж.']
THEMATIC_QUESTIONS = ['Существенные финансовые результаты', 'Бизнес, продукты и клиенты', 'Стратегия и финансирование']
THEMATIC_FACTS = [
    [GOOD, dict(GOOD, amount=dict(value='80', unit='money', currency='RUB'), value_text='80',
                period=dict(kind='interval', label='3M22', start='2022-01-01', end='2022-03-31'),
                evidence=[dict(page=1, line_start=8, line_end=10, area='Выручка за прошлый квартал')])],
    [QUAL],
    [dict(QUAL, evidence=[dict(page=1, line_start=11, line_end=12, area='Стратегия')])],
]


def reply(name='aist_get_facts', args=None, *, identifier='toolu_fixture_01', stop='tool_use'):
    content = [deepcopy(THINKING), dict(type='tool_use', id=identifier, name=name, input=args or {'refs': []})]
    return dict(id='msg_fixture_' + identifier, type='message', role='assistant', model=FABLE['model'], content=content,
                stop_reason=stop, stop_sequence=None, usage=dict(input_tokens=100, output_tokens=50,
                cache_read_input_tokens=200, cache_creation_input_tokens=30,
                cache_creation=dict(ephemeral_5m_input_tokens=20, ephemeral_1h_input_tokens=10)))


@pytest.fixture
def native(workspace, monkeypatch):
    state = SimpleNamespace(queue=[], requests=[], paths=[], responses=[], prefix_failures=[], last_agent=None)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            state.paths.append(self.path)
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if request.get('tools') and state.last_agent and state.last_agent[1].get('stop_reason') == 'tool_use':
                previous, response = state.last_agent
                prefix = previous['messages'] + [dict(role='assistant', content=response['content'])]
                if (request.get('system') != previous.get('system') or request.get('tools') != previous.get('tools')
                        or request['messages'][:len(prefix)] != prefix):
                    state.prefix_failures.append('Native signed prefix changed')
            state.requests.append(request)
            entry = state.queue.pop(0)
            if state.prefix_failures:
                entry = dict(status=400, body={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': state.prefix_failures[-1]}})
            if entry.get('close'):
                self.connection.close()
                return
            value = entry['body']() if callable(entry['body']) else entry['body']
            if request.get('tools'):
                state.last_agent = (request, deepcopy(value))
            state.responses.append(deepcopy(value))
            data = (anthropic_messages.to_sse(value) if entry.get('status', 200) == 200 else json_value(value)).encode()
            if entry.get('incomplete'):
                data = data[:data.rfind(b'event: message_stop')]
            self.send_response(entry.get('status', 200))
            self.send_header('request-id', 'req_fixture_native')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if entry.get('delay'):
                time.sleep(entry['delay'])
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(anthropic_messages, 'credential', lambda path: 'fixture-only-key')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *args, **kw: transport.ReaderConnection('127.0.0.1', server.server_port, timeout=3))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def start_fable(w, *, full=False, following=(), source=None, profile_id=None, research=False):
    if source is None:
        source, facts = (upload(w, 'full-fable', MIXED, following=following), []) if full else note_source(w)
    else:
        assert full
        facts = []
    profile_id = profile_id or (FABLE_FULL_ID if full else FABLE_ID)
    w.settings = w.runs.settings = replace(w.settings, model_profile_id=profile_id, ready_facts_only=not full,
                                           embeddings_enabled=False, transport_seconds=600, lease_seconds=1000)
    w.runs.registry = delivered(w.runs.registry, profile_id)
    # The fixture started on the historical provider; its private policy now belongs only to Fable.
    policy = deepcopy(w.runs.policy)
    policy.update(budget_scope='anthropic-authorized-total', total_authorized_usd='200', historical_exposure_usd='12.294996',
                  quote_usd='10', project_limit='200', input_per_million='10', output_per_million='50',
                  cache_read_per_million='0.25', cache_write_5m_per_million='12.5', cache_write_1h_per_million='20',
                  pricing_version='anthropic-standard-2026-10-23.v1')
    policy['run'].update(policy_version='fable-fixture-full' if full else 'fable-fixture-r1', limit='200',
                         max_model_calls=16 if full and research else 8 if full else 10 if research else 3, max_tool_calls=24, max_duration_seconds=900)
    w.runs.policy = policy
    request = body(sources=[source['id']], question='Подготовь справку по сохраненным фактам')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note', model_profile_version_id=profile_id)
    send(w, conversation(w), request)
    return source, facts, w.runs.claim('fable-fixture')


def audit(w, run):
    with w.engine.connect() as c:
        calls = list(execute(c, 'SELECT data FROM aist.model_calls WHERE run_id=:r ORDER BY created_at', r=run['id']).scalars())
        usage = list(execute(c, "SELECT data FROM aist.usage_entries WHERE run_id=:r AND data->>'evidence_kind'='provider_response' ORDER BY created_at", r=run['id']).scalars())
    return calls, usage


@pytest.mark.parametrize('correction', [False, True])
def test_installed_native_runtime_retains_signed_history_and_saves_once(workspace, native, correction, monkeypatch):
    w = workspace
    source, facts, run = start_fable(w)
    from app import note_external
    from test_external_context import batch
    context, _ = batch()
    monkeypatch.setattr(note_external, 'identity', lambda materials: dict(company=context['company'], inn='', sector='retail', domain=''))
    from test_external_context_postgres import seed_context
    seed_context(w, run, context)
    context = deepcopy(run['external_context'])
    external = context['items'][0]
    calculation = dict(formula_id='growth_percent.v1', inputs=[dict(name=name, fact=dict(source_version_id=source['id'], fact_id=fact))
        for name, fact in [('previous', 'fact_capital2022'), ('current', 'fact_capital1q23')]], rounding=dict(places=2, mode='HALF_UP'))
    native.queue = [dict(body=reply('aist_calculate', calculation))]
    if correction:
        native.queue.append(dict(body=reply('aist_save_artifact', {'content': {'title': 'Incomplete fixture'}}, identifier='toolu_bad_save')))
    content = small_note(source)
    content['sections'][-1]['blocks'].append(dict(kind='paragraph', block_id='external_runtime', claim=dict(
        text=external['quote'], origin='external_context', external_evidence_ids=[external['evidence_id']],
        fact_refs=[], calculation_ids=[])))
    command = model_save_args(content)
    command['ownership_review']['reason'] = 'Источник упоминает основного акционера, но не называет его.'
    command['ownership_review']['excluded_sources'] = [dict(external_evidence_id=external['evidence_id'],
        reason='Имя или наименование акционера в выдержке не раскрыто.')]
    native.queue.append(dict(body=reply('aist_save_artifact', command, identifier='toolu_final_save')))
    with TemporaryDirectory(prefix='fable-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0)); provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close(); provider.close()
        worker = executor(w)
        timer = threading.Timer(100, worker.draining.set)
        timer.start()
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel()
            destination = Path('/result') / ('runtime-correction' if correction else 'runtime-normal')
            destination.mkdir(exist_ok=True)
            directory = Path(root) / run['id']
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
        state = w.runs.get(w.actor, w.project, run['id'])
        assert state['state'] == 'completed', state
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        assert saved['version']['basis']['external_context'] == context
        # Replay the same service operation after completion without an executor or provider.
        entry = journal[-1]
        args = runtime.save_operation(run, entry['request']['args'], None)
        replay = w.runs.tool(run['id'], run['epoch'], entry['request']['tool_call_id'], 'aist_save_artifact', args, entry['model_call_id'])
        assert replay == entry['reply']
    assert len(native.requests) == (3 if correction else 2)
    assert native.prefix_failures == []
    assert all(path == '/v1/messages' for path in native.paths)
    first = json_value(native.requests[0])
    assert external['quote'] in first and 'search_target' in first
    delivered_text = '\n'.join(b['text'] for b in native.requests[0]['system'])
    for message in native.requests[0]['messages']:
        content = message['content']
        delivered_text += '\n' + (content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content))
    assert bundle_for('skill_analytical-note-v3', 'analytical_note').read_text() in delivered_text
    assert all(fact in first for fact in facts)
    assert 'Новое извлечение' in first
    assert run['configuration']['model_profile'] == FABLE
    assert run['configuration']['model_configuration_sha256'] == fingerprint(FABLE)
    for request in native.requests:
        assert request['thinking'] == {'type': 'adaptive'} and request['output_config'] == {'effort': 'xhigh'}
        assert request['max_tokens'] == 64000 and 'enable_thinking' not in request
        assert 'cache_control' not in json_value(request)
    second_blocks = [block for message in native.requests[1]['messages'] if isinstance(message['content'], list) for block in message['content']]
    assert THINKING in second_blocks
    assert any(b.get('type') == 'tool_use' and b['id'] == 'toolu_fixture_01' and b['input'] == calculation for b in second_blocks)
    assert any(b.get('type') == 'tool_result' and b['tool_use_id'] == 'toolu_fixture_01' for b in second_blocks)
    assert journal[0]['request']['name'] == 'aist_calculate'
    # У балансовых фикстур нет определения и базы для расчета роста. Отказ передается следующей команде.
    assert journal[0]['reply']['ok'] is False
    assert journal[0]['reply']['error']['message'] == 'Не установлены определение показателя и общая база сопоставимости.'
    assert journal[0]['reply']['error']['message'] in json_value(native.requests[1])
    if correction:
        feedback = json_value(native.requests[2])
        assert 'toolu_bad_save' in feedback and 'sections' in feedback and runtime.FORMAT_CORRECTION in feedback
    assert saved['review']['acceptance'] is None and saved['checks'][0]['status'] == 'passed' and saved['checks'][1]['status'] == 'not_checked'
    calls, usage = audit(w, run)
    assert len(calls) == len(native.requests) == len(usage)
    assert all(c['provider_request_id'] == 'req_fixture_native' for c in calls)
    assert all(u['tokens']['reasoning_output']['value'] is None and Decimal(u['estimated_usd']['value']) == Decimal('0.004') for u in usage)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    (destination / 'result.json').write_text(json_value(dict(state=state, calls=calls, usage=usage, saved=saved, posts=len(native.requests))))


@pytest.mark.parametrize('changed', ['system', 'tools', 'user', 'thinking', 'tool_input'])
def test_native_mock_refuses_changed_signed_prefix(workspace, native, tmp_path, changed):
    w = workspace
    _, _, run = start_fable(w)
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'fixture')
    request = dict(model=FABLE['model'], system='Stable instruction', tools=[dict(name='aist_get_facts', input_schema={'type':'object'})],
                   messages=[dict(role='user', content='Original task')])
    native.queue = [dict(body=reply()), dict(body=reply(identifier='toolu_second'))]
    first = boundary.model(request)
    continuation = deepcopy(request)
    continuation['messages'].extend([dict(role='assistant', content=first['content']), dict(role='user',
        content=[dict(type='tool_result', tool_use_id='toolu_fixture_01', content='Fixture result')])])
    if changed == 'system':
        continuation['system'] += ' changed'
    elif changed == 'tools':
        continuation['tools'][0]['description'] = 'changed'
    elif changed == 'user':
        continuation['messages'][0]['content'] = 'changed'
    elif changed == 'thinking':
        continuation['messages'][1]['content'][0]['signature'] = 'changed'
    else:
        continuation['messages'][1]['content'][1]['input'] = {'refs': ['changed']}
    with pytest.raises(ApiError):
        boundary.model(continuation)
    assert native.prefix_failures == ['Native signed prefix changed'] and len(native.requests) == 2


@pytest.mark.parametrize('failure', ['429', '500', 'close', 'incomplete', 'max_tokens', 'empty'])
def test_native_failure_never_retries_or_executes_partial_tool(workspace, native, tmp_path, failure):
    w = workspace
    source, _, run = start_fable(w)
    entry = dict(body=reply('aist_save_artifact', model_save_args(small_note(source))))
    if failure in ('429', '500'):
        entry = dict(status=int(failure), body={'type': 'error', 'error': {'type': 'server_overload'}})
    elif failure in ('close', 'incomplete'):
        entry[failure] = True
    elif failure == 'max_tokens':
        entry['body']['stop_reason'] = 'max_tokens'
    else:
        entry['body']['content'] = []
        entry['body']['stop_reason'] = 'end_turn'
    native.queue = [entry]
    worker = executor(w)
    boundary = runtime.Boundary(worker, run, tmp_path, 'fixture')
    with pytest.raises(ApiError):
        boundary.model(dict(model=FABLE['model'], messages=[dict(role='user', content='Fixture')]))
    assert len(native.requests) == 1 and not boundary.issued
    with pytest.raises(ApiError):
        boundary.model(dict(model=FABLE['model'], messages=[dict(role='user', content='No second call')]))
    assert len(native.requests) == 1
    calls, usage = audit(w, run)
    assert len(calls) == 1
    assert calls[0]['transport_state'] == ('unknown' if failure in ('close', 'incomplete') else 'observed')
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.tool_calls').scalar_one() == 0


def test_native_deadlines_and_pre_network_guards(workspace, native, monkeypatch, tmp_path):
    w = workspace
    _, _, run = start_fable(w)
    worker = executor(w)
    clock = run_clock.utcnow
    for elapsed, expected in [(181, 600), (301, 599), (601, 299), (850, 50), (901, 0)]:
        monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=elapsed))
        assert collection.send_seconds(run, 600) == expected
    monkeypatch.setattr(run_clock, 'utcnow', clock)
    assert runtime.client_timeout(w.settings, 900, 600) == 915
    assert run['configuration']['collection']['completion_seconds'] == 840
    old = deepcopy(run)
    old['input']['budget']['max_duration_seconds'] = 600
    old['configuration']['collection'].update(send_seconds=300, completion_seconds=600, max_duration_seconds=600)
    monkeypatch.setattr(run_clock, 'utcnow', lambda: old['created_at'] + timedelta(seconds=400))
    assert collection.send_seconds(old, 600) == 200
    monkeypatch.setattr(run_clock, 'utcnow', clock)
    with pytest.raises(ApiError):
        worker.transport.perform(run, {'purpose': 'extraction'}, request={}, audit=tmp_path)
    from app.embeddings import Embeddings
    with pytest.raises(ApiError) as blocked:
        Embeddings(w.engine, w.settings).vectors(w.actor, ['Uncached fixture query'], 'query')
    assert blocked.value.code == 'embedding_unavailable'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    state = w.runs.get(w.actor, w.project, run['id'])
    w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', 'key_cancel_fable', {'expected_revision': state['revision']})
    with pytest.raises(ApiError):
        worker.transport.perform(run, {'purpose': 'agent'}, request=dict(messages=[]), audit=tmp_path)
    assert native.requests == []


def test_native_thinking_detail_is_observed_without_changing_output_charge():
    payload = reply()
    payload['usage']['output_tokens_details'] = {'thinking_tokens': 40}
    _, tokens, valid = response_fields(payload, live=True, native=True)
    assert valid and tokens['reasoning_output']['value'] == 40 and tokens['output']['value'] == 50
    payload['usage']['output_tokens_details']['thinking_tokens'] = 51
    _, tokens, valid = response_fields(payload, live=True, native=True)
    assert valid and tokens['reasoning_output']['value'] is None


def test_native_events_do_not_extend_absolute_send_deadline(workspace, monkeypatch, tmp_path):
    w = workspace
    _, _, run = start_fable(w)
    worker = executor(w)
    monkeypatch.setattr(worker.transport, 'send_seconds', lambda current, purpose='agent': 0.15)
    monkeypatch.setattr(anthropic_messages, 'credential', lambda path: 'fixture-key')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b'event: ping\ndata: {"type":"ping"}\n\n'); self.wfile.flush()
                    time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *args, **kw: transport.ReaderConnection('127.0.0.1', server.server_port, timeout=3))
    started = time.monotonic()
    try:
        with pytest.raises(ApiError):
            runtime.Boundary(worker, run, tmp_path, 'fixture').model({'model': FABLE['model'], 'messages': []})
        assert time.monotonic() - started < 0.45
        calls, _ = audit(w, run)
        assert len(calls) == 1 and calls[0]['transport_state'] == 'unknown'
        timings = json.loads((tmp_path / 'calls' / (calls[0]['id'] + '-timings.json')).read_text())
        stages = timings['stages_seconds']
        assert timings['events'] > 1 and timings['bytes'] > 0
        assert stages['first_event'] is not None and stages['message_stop'] is None
        assert stages['read_complete'] is None and stages['accounting_complete'] is not None
        assert stages['attempt_finished'] < 0.45
        assert not list((tmp_path / 'calls').glob('*-response.json'))
        with w.engine.connect() as c:
            usage = execute(c, 'SELECT data FROM aist.usage_entries WHERE id=:id', id=calls[0]['usage_entry_id']).scalar_one()
        assert usage['estimated_usd']['value'] is None and usage['reservation_state'] == 'held'
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_native_response_after_short_old_limit_records_stages(workspace, native, monkeypatch, tmp_path):
    w = workspace
    _, _, run = start_fable(w)
    worker = executor(w)
    # Scale the old/new limits to 0.05/0.35 seconds; the response arrives between them.
    monkeypatch.setattr(worker.transport, 'send_seconds', lambda current, purpose='agent': 0.35)
    native.queue = [dict(body=reply(), delay=0.10)]
    result = runtime.Boundary(worker, run, tmp_path, 'fixture').model({'model': FABLE['model'], 'messages': []})
    assert result['stop_reason'] == 'tool_use' and len(native.requests) == 1
    calls, _ = audit(w, run)
    timings = json.loads((tmp_path / 'calls' / (calls[0]['id'] + '-timings.json')).read_text())
    stages = timings['stages_seconds']
    assert 0.05 < stages['first_event'] < 0.35
    assert all(value is not None for value in stages.values())
    assert list(stages.values()) == sorted(stages.values())
    assert timings['bytes'] > 0 and timings['events'] > 0


def test_native_stage_audit_handles_split_events_without_content(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(anthropic_messages.time, 'monotonic', lambda: clock[0])
    path = tmp_path / 'timings.json'
    trace = anthropic_messages.StreamAudit(path, 10.0, 600)
    events = [dict(type='content_block_start', index=0, content_block=dict(type='thinking', thinking='', signature='private-signature')),
              dict(type='content_block_stop', index=0),
              dict(type='content_block_start', index=1, content_block=dict(type='tool_use', name='aist_save_artifact', input={})),
              dict(type='content_block_delta', index=1, delta=dict(type='input_json_delta', partial_json='')),
              dict(type='content_block_delta', index=1, delta=dict(type='input_json_delta', partial_json='{"private":"arguments"}')),
              dict(type='message_stop')]
    raw = b''.join(('data: ' + json.dumps(event) + '\r\n\r\n').encode() for event in events)
    for byte in raw:
        clock[0] += 0.001
        trace.feed(bytes([byte]))
    data = json.loads(path.read_text())
    assert data['events'] == len(events) and data['bytes'] == len(raw)
    stages = data['stages_seconds']
    assert 0 < stages['first_event'] < stages['thinking_closed'] < stages['tool_use_started'] < stages['arguments_started'] < stages['message_stop']
    assert stages['headers'] is None and stages['read_complete'] is None
    assert 'private' not in path.read_text() and 'signature' not in path.read_text()


def extracted(items):
    value = reply(stop='end_turn')
    text = json_value(dict(facts=deepcopy(items)))
    middle = len(text) // 2
    value['content'] = [deepcopy(THINKING), dict(type='text', text=text[:middle]), dict(type='text', text=text[middle:])]
    value['usage']['output_tokens_details'] = dict(thinking_tokens=40)
    return value


def test_full_native_extraction_preserves_body_ingest_and_saved_result(workspace, native, tmp_path):
    from app import extraction_requests
    from app.extraction import extraction_request, source_pages
    w = workspace
    source, _, run = start_fable(w, full=True)
    assert not fact_rows(w)
    assert run['configuration']['extraction_thinking'] == 'adaptive'
    assert run['input']['budget']['max_model_calls'] == 8 and run['input']['budget']['max_tool_calls'] == 24
    native.queue = [dict(body=extracted([QUAL, GOOD]))]
    args = dict(source_version_id=source['id'], question='Все данные')
    result = runtime.obtain_facts(executor(w), run, args, tmp_path, None)
    assert result['status'] == 'complete' and len(result['facts']) == 2
    assert next(f for f in result['facts'] if f['amount'])['amount']['value'] == '100'
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
        diagrams = source_pages(w.runs, c, run, diagrams=True)
        task = execute(c, 'SELECT text FROM aist.messages WHERE id=:m', m=run['message_id']).scalar_one()
    request = extraction_request(pages, task, layouts, question=args['question'], diagrams=diagrams,
                                aso_note=True,
                                conversation_context=run['configuration']['conversation_context'], clarification_dialogue=[])
    actual = native.requests[0]
    assert actual == transport.model_request(run, 'extraction', request)
    assert actual['system'] == [dict(type='text', text=m['content']) for m in request['messages'] if m['role'] == 'system']
    assert all(m['role'] != 'system' for m in actual['messages']) and actual['max_tokens'] == 64000
    before = fact_rows(w)
    assert runtime.obtain_facts(executor(w), current(w, run), args, tmp_path / 'replay', None) == result
    assert len(native.requests) == 1 and fact_rows(w) == before
    operation, = extraction_requests.operations(current(w, run)).values()
    assert operation['model_profile_version_id'] == FABLE_FULL_ID
    assert operation['model_configuration_sha256'] == fingerprint(FABLE_FULL)
    assert operation['request_sha256'] == fingerprint(actual)
    from hashlib import sha256
    assert operation['body_sha256'] == sha256(json_value(actual).encode()).hexdigest()
    calls, usage = audit(w, run)
    assert calls[0]['purpose'] == 'extraction' and calls[0]['model_profile_version_id'] == FABLE_FULL_ID
    assert usage[0]['tokens']['output']['value'] == 50 and usage[0]['tokens']['reasoning_output']['value'] == 40
    assert Decimal(usage[0]['estimated_usd']['value']) == Decimal('0.004')
    Path('/result/full-extraction.json').write_text(json_value(dict(request=actual, result=result, calls=calls, usage=usage)))


def test_installed_full_native_runtime_extracts_then_saves_once(workspace, native, monkeypatch):
    w = workspace
    source, _, run = start_fable(w, full=True)
    assert fact_rows(w) == []
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
    native.queue = [dict(body=reply(args={'question': 'Все данные'})), dict(body=extracted([QUAL, GOOD])),
                    dict(body=lambda: reply('aist_save_artifact', model_save_args(note(next(f['data'] for f in fact_rows(w) if f['data']['amount'] is None))),
                                           identifier='toolu_full_save'), delay=0.10)]
    with TemporaryDirectory(prefix='fable-full-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0)); provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close(); provider.close()
        worker = executor(w)
        # The agent response crosses a scaled old 300-second limit (0.05), while its new
        # deadline (0.35) and the Run deadline remain in force and collection is still open.
        monkeypatch.setattr(worker.transport, 'send_seconds', lambda active, purpose='agent': 0.35 if purpose == 'agent' else 300)
        timer = threading.Timer(100, worker.draining.set); timer.start()
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel()
            directory = Path(root) / run['id']
            destination = Path('/result/runtime-full'); destination.mkdir(exist_ok=True)
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
        state = w.runs.get(w.actor, w.project, run['id'])
        assert state['state'] == 'completed', state
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        assert [entry['request']['name'] for entry in journal] == ['aist_get_facts', 'aist_save_artifact']
        assert journal[0]['reply']['ok'] and len(journal[0]['reply']['result']['facts']) == 2
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        calls, usage = audit(w, run)
        assert [call['purpose'] for call in calls] == ['agent', 'extraction', 'agent']
        assert calls[1]['parent_model_call_id'] == calls[0]['id'] == calls[2]['parent_model_call_id']
        timing = json.loads((directory / 'calls' / (calls[-1]['id'] + '-timings.json')).read_text())
        assert 0.05 < timing['stages_seconds']['first_event'] < 0.35
        assert not collection.state(current(w, run))['closed']
    assert len(native.requests) == len(usage) == 3 and not native.prefix_failures
    assert all(path == '/v1/messages' for path in native.paths)
    assert all(call['model_profile_version_id'] == FABLE_FULL_ID for call in calls)
    assert all(u['tokens']['output']['value'] == 50 for u in usage)
    assert native.requests[0]['system'] == native.requests[2]['system'] and native.requests[0]['tools'] == native.requests[2]['tools']
    block = next(b for m in native.requests[2]['messages'] if isinstance(m['content'], list)
                 for b in m['content'] if b.get('type') == 'tool_result')
    assert json.loads(block['content']) == runtime.model_view(journal[0]['reply'])
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    (destination / 'result.json').write_text(json_value(dict(state=state, calls=calls, usage=usage, saved=saved, posts=3)))


@pytest.mark.parametrize('change', [None, 'cancel', 'epoch', 'deadline', 'revoke', 'terminal', 'sha', 'suppressed'])
def test_full_receipt_recovery_keeps_authority_and_does_not_send_again(workspace, native, tmp_path, monkeypatch, change):
    from app import extraction_requests
    from test_d15_postgres import invalidate
    w = workspace
    source, _, run = start_fable(w, full=True)
    worker = executor(w)
    native.queue = [dict(body=extracted([QUAL]))]
    args = dict(source_version_id=source['id'], question='Все данные')
    original = runtime.ingest
    def interrupted(*args, **kwargs):
        raise RuntimeError('interrupted after the durable receipt')
    monkeypatch.setattr(runtime, 'ingest', interrupted)
    with pytest.raises(RuntimeError):
        runtime.obtain_facts(worker, run, args, tmp_path, None)
    assert len(native.requests) == 1 and not fact_rows(w)
    monkeypatch.setattr(runtime, 'ingest', original)
    if change in ('cancel', 'epoch', 'deadline', 'revoke'):
        invalidate(w, current(w, run), change)
    elif change == 'terminal':
        w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    elif change == 'sha':
        w.service.storage.path(source['id'] + '.pdf').write_bytes(b'corrupted fixture PDF')
    elif change == 'suppressed':
        call = next(iter(extraction_requests.operations(current(w, run)).values()))['response_call_id']
        with w.engine.begin() as c:
            execute(c, "UPDATE aist.model_calls SET data=jsonb_set(data,'{delivery}','\"suppressed\"') WHERE id=:id", id=call)
    if change is None:
        result = runtime.obtain_facts(executor(w), run, args, tmp_path / 'recovered', None)
        assert result['status'] == 'complete' and len(fact_rows(w)) == 1
    else:
        with pytest.raises(ApiError):
            runtime.obtain_facts(executor(w), run, args, tmp_path / 'recovered', None)
        assert not fact_rows(w)
    assert len(native.requests) == 1


@pytest.mark.parametrize('failure', ['json', 'reference', 'tool_use'])
def test_full_known_extraction_refusal_replays_and_allows_another_subject(workspace, native, tmp_path, failure):
    w = workspace
    source, _, run = start_fable(w, full=True)
    value = extracted([dict(QUAL, evidence=[dict(page=999, line_start=1, line_end=1, area='Invalid')])])
    if failure == 'json':
        value['content'] = [deepcopy(THINKING), dict(type='text', text='Invalid JSON')]
    elif failure == 'tool_use':
        value = reply('aist_save_artifact', {'content': {'title': 'Not facts'}})
    native.queue = [dict(body=value)]
    args = dict(source_version_id=source['id'], question='Все данные')
    for directory in (tmp_path, tmp_path / 'replay'):
        with pytest.raises(ApiError) as error:
            runtime.obtain_facts(executor(w), run, args, directory, None)
        assert error.value.code == 'validation_failed'
    assert len(native.requests) == 1 and not fact_rows(w)
    native.queue = [dict(body=extracted([GOOD]))]
    other = runtime.obtain_facts(executor(w), run, dict(args, question='Финансовые результаты'), tmp_path / 'finance', None)
    assert other['status'] == 'complete' and other['coverage']['full_document']
    assert len(native.requests) == 2 and len(fact_rows(w)) == 1


@pytest.mark.parametrize('failure', ['incomplete', '429'])
def test_full_native_extraction_failure_stops_without_retry_or_facts(workspace, native, tmp_path, failure):
    w = workspace
    source, _, run = start_fable(w, full=True)
    entry = dict(body=extracted([QUAL]), incomplete=True) if failure == 'incomplete' else dict(
        status=429, body={'type': 'error', 'error': {'type': 'rate_limit_error'}})
    native.queue = [entry]
    args = dict(source_version_id=source['id'], question='Все данные')
    for directory in (tmp_path, tmp_path / 'retry'):
        with pytest.raises(ApiError):
            runtime.obtain_facts(executor(w), run, args, directory, None)
    assert len(native.requests) == 1 and not fact_rows(w)
    calls, _ = audit(w, run)
    assert len(calls) == 1 and calls[0]['transport_state'] == ('unknown' if failure == 'incomplete' else 'observed')


@pytest.mark.parametrize('remaining,delay,allowed,limit', [(660, 0, True, 600), (659, 0, True, 600), (660, 10, True, 600), (421, 0, True, 61)])
def test_full_extraction_admission_and_post_share_one_absolute_window(workspace, native, tmp_path, monkeypatch, remaining, delay, allowed, limit):
    w = workspace
    source, _, run = start_fable(w, full=True)
    w.runs.settings = replace(w.settings, transport_seconds=limit)
    offset = [0]
    monotonic = time.monotonic
    monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=900 - remaining + offset[0]))
    if delay:
        connect = transport.ReaderConnection.connect
        def delayed(connection):
            connect(connection)
            offset[0] += delay
        monkeypatch.setattr(transport.ReaderConnection, 'connect', delayed)
        monkeypatch.setattr(transport.time, 'monotonic', lambda: monotonic() + offset[0])
    native.queue = [dict(body=extracted([QUAL]))]
    args = dict(source_version_id=source['id'], question='Все данные')
    if allowed:
        result = runtime.obtain_facts(executor(w), run, args, tmp_path, None)
        assert result['status'] == 'complete' and len(native.requests) == 1
        assert not collection.state(current(w, run))['closed']
    else:
        with pytest.raises(ApiError) as error:
            runtime.obtain_facts(executor(w), run, args, tmp_path, None)
        assert error.value.code == 'budget_exceeded' and native.requests == []
        assert collection.state(current(w, run))['reason'] == 'reserve_time'


@pytest.mark.parametrize('remaining,used,expected', [(870, 0, 600), (500, 4, 500), (800, 5, 600)])
def test_full_composer_window_and_reserve_calls_are_independent(workspace, native, monkeypatch, tmp_path, remaining, used, expected):
    w = workspace
    source, _, run = start_fable(w, full=True)
    worker = executor(w)
    # Seed completed accounted calls only for the quota boundary, without physical requests.
    for _ in range(used):
        call = worker.transport.ledger.reserve(run['id'], run['epoch'])
        worker.transport.ledger.observe(run['id'], run['epoch'], call['id'], 200, extracted([]), native=True)
    monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=900 - remaining))
    assert worker.transport.send_seconds(run) == expected
    if used == 5:
        with pytest.raises(ApiError):
            runtime.obtain_facts(worker, run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
        assert not native.requests and collection.state(current(w, run))['reason'] == 'reserve_calls'
    native.queue = [dict(body=reply())]
    runtime.Boundary(worker, run, tmp_path, 'fixture').model(dict(model=FABLE['model'], messages=[]))
    assert len(native.requests) == 1 and current(w, run)['state'] == 'running'
    assert collection.state(current(w, run))['closed'] == (used != 0)
    calls, _ = audit(w, run)
    assert len(calls) == used + 1 and calls[-1]['provider_outcome'] == 'success'


def test_full_agent_rejects_unfinished_note_without_changing_signed_history(workspace, native, monkeypatch, tmp_path):
    w = workspace
    source, _, run = start_fable(w, full=True)
    worker = executor(w)
    boundary = runtime.Boundary(worker, run, tmp_path, 'fixture')
    request = dict(model=FABLE['model'], system='Stable', tools=[dict(name='aist_get_facts', input_schema={'type': 'object'})],
                   messages=[dict(role='user', content='Справка')])
    def limited_note():
        content = note(fact_rows(w)[0]['data'])
        content['sections'][0]['blocks'].append(dict(block_id='block_limitation', kind='paragraph', claim=dict(
            text='Дополнительные сведения не подтверждены: сбор оснований завершен.', origin='inference', fact_refs=[], calculation_ids=[])))
        return reply('aist_save_artifact', model_save_args(content), identifier='toolu_save_closed')
    native.queue = [dict(body=reply(args={'question': 'Все данные'})), dict(body=extracted([QUAL])),
                    dict(body=reply(args={'question': 'Новый вопрос'}, identifier='toolu_closed')),
                    dict(body=limited_note)]
    first = boundary.model(request)
    def invoke(value):
        tool = next(b for b in value['content'] if b['type'] == 'tool_use')
        return boundary.tool(dict(session=run['executor_session_id'], tool_call_id=tool['id'], name=tool['name'], args=tool['input']))
    facts = invoke(first)
    assert facts['ok']
    continuation = deepcopy(request)
    continuation['messages'] += [dict(role='assistant', content=first['content']), dict(role='user', content=[
        dict(type='tool_result', tool_use_id='toolu_fixture_01', content=json_value(facts))])]
    offset = [470]
    monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=offset[0]))
    connect = transport.ReaderConnection.connect
    def delayed(connection):
        connect(connection)
        offset[0] = 490
    monkeypatch.setattr(transport.ReaderConnection, 'connect', delayed)
    second = boundary.model(continuation)
    assert collection.state(current(w, run))['reason'] == 'reserve_time'
    refused = invoke(second)
    assert not refused['ok'] and 'Сбор оснований закрыт' in refused['error']['message']
    assert len(native.requests) == 3
    continuation['messages'] += [dict(role='assistant', content=second['content']), dict(role='user', content=[
        dict(type='tool_result', tool_use_id='toolu_closed', content=json_value(refused))])]
    final = boundary.model(continuation)
    save = invoke(final)
    assert not save['ok'] and save['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in save['error']['message']
    assert not current(w, run)['result_version_id']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
    assert not native.prefix_failures and len(native.requests) == 4
    calls, usage = audit(w, run)
    assert [c['purpose'] for c in calls] == ['agent', 'extraction', 'agent', 'agent']
    assert all(c['provider_outcome'] == 'success' and c['error'] is None for c in calls)
    Path('/result/reserve-closure.json').write_text(json_value(dict(requests=native.requests, calls=calls, usage=usage, refusal=refused)))


@pytest.mark.parametrize('change', ['cancel', 'terminal', 'collection'])
def test_full_send_boundary_rechecks_cancellation_and_collection(workspace, native, monkeypatch, tmp_path, change):
    from test_d15_postgres import invalidate
    w = workspace
    source, _, run = start_fable(w, full=True)
    connect = transport.ReaderConnection.connect
    def interrupted(connection):
        connect(connection)
        if change == 'cancel':
            invalidate(w, current(w, run), 'cancel')
        elif change == 'terminal':
            w.runs.stop(run['id'], run['epoch'], 'validation_failed')
        else:
            with w.engine.begin() as c:
                collection.close(w.runs, c, w.runs.locked(c, run['id']), 'reserve_calls')
    monkeypatch.setattr(transport.ReaderConnection, 'connect', interrupted)
    with pytest.raises(ApiError):
        runtime.obtain_facts(executor(w), run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    assert native.requests == [] and not fact_rows(w)
    calls, _ = audit(w, run)
    assert len(calls) == 1 and calls[0]['transport_state'] == 'not_sent'


def test_full_two_completed_empty_extractions_close_collection(workspace, native, tmp_path):
    w = workspace
    source, _, run = start_fable(w, full=True, following=[(['Склад', 'Логистика'], None)])
    native.queue = [dict(body=extracted([])), dict(body=extracted([]))]
    for question in ('Выручка', 'Бизнес'):
        result = runtime.obtain_facts(executor(w), run, dict(source_version_id=source['id'], question=question), tmp_path, None)
        assert result['status'] == 'empty' and result['coverage']['pages'] == [1]
    assert collection.state(current(w, run))['reason'] == 'idle'
    with pytest.raises(ApiError):
        runtime.obtain_facts(executor(w), run, dict(source_version_id=source['id'], question='Склад'), tmp_path, None)
    assert len(native.requests) == 2


def test_old_fable_pin_stays_ready_only_even_on_a_full_worker(workspace, native, tmp_path):
    w = workspace
    source, _, run = start_fable(w)
    original = deepcopy(run['configuration'])
    w.runs.settings = replace(w.settings, model_profile_id=FABLE_FULL_ID, ready_facts_only=False)
    worker = executor(w)
    with pytest.raises(ApiError) as error:
        worker.transport.perform(run, {'purpose': 'extraction'}, request={}, audit=tmp_path)
    assert error.value.code == 'tool_unavailable'
    with pytest.raises(ApiError):
        runtime.obtain_facts(worker, run, dict(source_version_id=source['id'], question='Новые факты'), tmp_path, None)
    assert current(w, run)['configuration'] == original
    assert original['model_configuration_sha256'] == fingerprint(FABLE)
    assert original['collection']['max_extractions'] == 0 and original['collection']['send_seconds'] == 600
    assert native.requests == []


@pytest.mark.parametrize('items,status', [([QUAL], 'complete'), ([QUAL, TRLN], 'partial'), ([], 'empty'),
    ([dict(QUAL, evidence=[dict(page=999, line_start=1, line_end=1, area='Invalid')])], 'refused')])
def test_full_known_outcomes_replay_with_new_tool_ids_even_after_collection_closes(workspace, native, tmp_path, items, status):
    w = workspace
    source, _, run = start_fable(w, full=True)
    native.queue = [dict(body=extracted(items))]
    first = ask(w, executor(w), run, source, 'Бизнес компании', tmp_path)
    assert first['ok'] == (status != 'refused')
    if first['ok']:
        assert first['result']['status'] == status and first['result']['coverage']['full_document']
    facts = fact_rows(w)
    calls, _ = audit(w, run)
    for closed in (False, True):
        if closed:
            with w.engine.begin() as c:
                collection.close(w.runs, c, w.runs.locked(c, run['id']), 'reserve_calls')
        checkpoint = current(w, run)['checkpoint']
        again = ask(w, executor(w), current(w, run), source, 'Бизнес компании', tmp_path / str(closed))
        assert again['tool_call_id'] != first['tool_call_id']
        assert again['ok'] == first['ok']
        if first['ok']:
            assert again['result'] == first['result']
        else:
            assert {k: v for k, v in again['error'].items() if k != 'tool_call_id'} == {
                k: v for k, v in first['error'].items() if k != 'tool_call_id'}
        assert current(w, run)['checkpoint'] == checkpoint
        assert fact_rows(w) == facts and audit(w, run)[0] == calls and len(native.requests) == 1
    Path('/result', 'repeat-' + status + '.json').write_text(json_value(dict(
        first=first, repeated=again, checkpoint=checkpoint, calls=calls, posts=1, database=w.settings.db_name)))


@pytest.mark.parametrize('state', ['reserved', 'attempted', 'sent', 'unknown'])
def test_full_unfinished_call_blocks_another_question_under_the_run_lock(workspace, native, tmp_path, state):
    w = workspace
    source, _, run = start_fable(w, full=True)
    worker = executor(w)
    call = worker.transport.ledger.reserve(run['id'], run['epoch'], 'extraction')
    with w.engine.begin() as c:
        execute(c, "UPDATE aist.model_calls SET data=jsonb_set(data,'{transport_state}',CAST(:state AS jsonb)) WHERE id=:id",
                state=json_value(state), id=call['id'])
    before = audit(w, run)[0]
    for question in THEMATIC_QUESTIONS:
        with pytest.raises(ApiError) as error:
            runtime.obtain_facts(executor(w), current(w, run), dict(source_version_id=source['id'], question=question), tmp_path, None)
        assert error.value.code == 'outcome_unknown'
    assert native.requests == [] and fact_rows(w) == [] and audit(w, run)[0] == before


def test_full_two_subjects_share_one_quota_and_replays_do_not_consume_it(workspace, native, tmp_path):
    w = workspace
    source, _, run = start_fable(w, full=True, source=upload(w, 'thematic-quota', THEMATIC_LINES))
    questions = [THEMATIC_QUESTIONS[0], 'Бизнес, рынок, стратегия и финансирование']
    native.queue = [dict(body=extracted(items)) for items in [THEMATIC_FACTS[0], THEMATIC_FACTS[1] + THEMATIC_FACTS[2]]]
    replies = []
    for question in questions:
        result = ask(w, executor(w), current(w, run), source, question, tmp_path)
        assert result['ok'] and result['result']['status'] == 'complete'
        assert result['result']['coverage']['full_document']
        replies.append(result)
    assert len(fact_rows(w)) == 4 and collection.state(current(w, run))['idle'] == 0
    fourth = ask(w, executor(w), current(w, run), source, 'Дополнительные операционные показатели', tmp_path)
    assert not fourth['ok'] and fourth['error']['code'] == 'budget_exceeded'
    assert collection.state(current(w, run))['reason'] == 'extractions'
    checkpoint = current(w, run)['checkpoint']
    for question, reply_value in zip(questions, replies):
        replay = ask(w, executor(w), current(w, run), source, question, tmp_path / 'reconstructed')
        assert replay['ok'] and replay['result'] == reply_value['result']
    assert current(w, run)['checkpoint'] == checkpoint and len(fact_rows(w)) == 4
    calls, usage = audit(w, run)
    assert len(native.requests) == len(calls) == 2
    Path('/result/thematic-quota.json').write_text(json_value(dict(
        replies=replies, fourth=fourth, checkpoint=checkpoint, calls=calls, usage=usage, posts=2)))


def test_full_replayed_empty_outcome_does_not_close_collection_early(workspace, native, tmp_path):
    w = workspace
    source, _, run = start_fable(w, full=True)
    native.queue = [dict(body=extracted([])), dict(body=extracted([]))]
    for _ in range(2):
        first = ask(w, executor(w), current(w, run), source, THEMATIC_QUESTIONS[0], tmp_path)
        assert first['ok'] and first['result']['status'] == 'empty'
        assert collection.state(current(w, run)) == dict(closed=False, idle=1)
    second = ask(w, executor(w), current(w, run), source, THEMATIC_QUESTIONS[1], tmp_path)
    assert second['ok'] and second['result']['status'] == 'empty'
    assert collection.state(current(w, run))['reason'] == 'idle'
    closed = current(w, run)['checkpoint']
    assert ask(w, executor(w), current(w, run), source, THEMATIC_QUESTIONS[0], tmp_path)['result'] == first['result']
    assert current(w, run)['checkpoint'] == closed
    assert not ask(w, executor(w), current(w, run), source, THEMATIC_QUESTIONS[2], tmp_path)['ok']
    assert len(native.requests) == 2


def test_installed_full_native_runtime_collects_two_subjects_and_saves_financial_bases(workspace, native):
    from test_draft01_postgres import http
    from test_perf02_r4_postgres import paragraph, ref, table_row
    w = workspace
    source, _, run = start_fable(w, full=True, source=upload(w, 'thematic-runtime', THEMATIC_LINES))
    questions = [THEMATIC_QUESTIONS[0], 'Бизнес, рынок, стратегия и финансирование']
    first = reply(args={'question': questions[0]}, identifier='toolu_finance')
    first['content'] += [dict(type='tool_use', id=identifier, name='aist_get_facts', input={'question': question})
                         for identifier, question in zip(('toolu_business',), questions[1:])]

    def save_note():
        facts = [f['data'] for f in fact_rows(w)]
        assert len(facts) == 4
        calls, _ = audit(w, run)
        assert len(calls) == 4 and all(c['transport_state'] == 'observed' for c in calls[:3])
        assert [c['purpose'] for c in calls[:3]] == ['agent', 'extraction', 'extraction']
        assert len(calls) + run['configuration']['collection']['completion_calls'] == 7
        business = next(f for f in facts if 'поставляет' in f['raw_text'])
        strategy = next(f for f in facts if 'расширяет' in f['raw_text'])
        financial = sorted((f for f in facts if f['amount']), key=lambda f: f['period']['start'])
        content = dict(title='Synthetic: информационная справка', audience='Аналитик', sections=[
            dict(section_id='sec_business', title='Бизнес', blocks=[paragraph('block_business',
                'Компания поставляет оборудование клиентам.', [ref(source, business['fact_id'])])]),
            dict(section_id='sec_finance', title='Финансы', blocks=[dict(block_id='block_revenue', kind='table',
                caption='Выручка по МСФО, млн руб.', rows=[table_row('row_' + f['period']['start'][:4], 'Выручка',
                    f['period']['label'], f['amount']['value'], [ref(source, f['fact_id'])]) for f in financial])]),
            dict(section_id='sec_strategy', title='Стратегия', blocks=[paragraph('block_strategy',
                'Компания расширяет сеть продаж.', [ref(source, strategy['fact_id'])])]),
        ])
        return reply('aist_save_artifact', model_save_args(content), identifier='toolu_thematic_save')

    native.queue = [dict(body=first), *[dict(body=extracted(items)) for items in [THEMATIC_FACTS[0], THEMATIC_FACTS[1] + THEMATIC_FACTS[2]]], dict(body=save_note)]
    with TemporaryDirectory(prefix='fable-thematic-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0)); provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close(); provider.close()
        worker = executor(w)
        timer = threading.Timer(100, worker.draining.set)
        timer.start()
        directory = Path(root) / run['id']
        destination = Path('/result/runtime-thematic')
        destination.mkdir(exist_ok=True)
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel()
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
        state = w.runs.get(w.actor, w.project, run['id'])
        assert state['state'] == 'completed', state
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        assert [entry['request']['name'] for entry in journal] == ['aist_get_facts'] * 2 + ['aist_save_artifact']
        assert [entry['request']['args']['question'] for entry in journal[:2]] == questions
        assert all(entry['reply']['ok'] for entry in journal)
        assert all(entry['reply']['result']['coverage']['full_document'] for entry in journal[:2])
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        entry = journal[-1]
        args = runtime.save_operation(run, entry['request']['args'], None)
        replay = w.runs.tool(run['id'], run['epoch'], entry['request']['tool_call_id'], 'aist_save_artifact', args, entry['model_call_id'])
        assert replay == entry['reply']
        new_id_replay = w.runs.tool(run['id'], run['epoch'], 'toolu_save_again', 'aist_save_artifact', args)
        assert new_id_replay['ok'] and new_id_replay['result']['version']['id'] == state['result_version_id']
        with pytest.raises(ApiError):
            runtime.obtain_facts(executor(w), run, dict(source_version_id=source['id'], question=THEMATIC_QUESTIONS[0]), directory, None)
    assert len(native.requests) == 4 and native.prefix_failures == [] and not native.queue
    assert all(path == '/v1/messages' for path in native.paths)
    composer = native.requests[3]
    delivered_text = '\n'.join(block['text'] for block in composer['system'])
    for message in composer['messages']:
        content = message['content']
        delivered_text += '\n' + (content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content))
    assert bundle_for('skill_analytical-note-v3', 'analytical_note').read_text() in delivered_text
    results = [json.loads(b['content']) for message in composer['messages'] if isinstance(message['content'], list)
               for b in message['content'] if b.get('type') == 'tool_result']
    assert results == [runtime.model_view(entry['reply']) for entry in journal[:2]]
    actual_facts = [f for result in results for f in result['result']['facts']]
    finance = [f for f in actual_facts if f['amount']]
    assert {(f['amount']['value'], f['amount']['scale'], f['period']['start'], f['period']['end']) for f in finance} == {
        ('80', '1000000', '2022-01-01', '2022-03-31'), ('100', '1000000', '2023-01-01', '2023-03-31')}
    saved_rows = saved['version']['payload']['content']['sections'][1]['blocks'][0]['rows']
    assert {ref_value['fact_id'] for row in saved_rows for ref_value in row['fact_refs']} == {f['fact_id'] for f in finance}
    assert saved['review']['acceptance'] is None and saved['checks'][0]['status'] == 'passed'
    # Reconnect the read API without a provider or the fixture's in-memory live budget policy.
    w.settings = replace(w.settings, live_models_enabled=False, executor_mode='disabled')
    with http(w) as client:
        response = client.get(f'/v1/projects/{w.project}/artifact-versions/{state["result_version_id"]}')
        assert response.status_code == 200 and response.json() == saved
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == 4
    assert [c['purpose'] for c in calls] == ['agent', 'extraction', 'extraction', 'agent']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    (destination / 'result.json').write_text(json_value(dict(state=state, saved=saved, calls=calls, usage=usage,
        posts=4, external_posts=0, reload_http=response.status_code, database=w.settings.db_name)))
