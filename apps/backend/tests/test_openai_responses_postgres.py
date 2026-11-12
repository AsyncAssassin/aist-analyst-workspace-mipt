"""Astra uses the installed executor and real accounting against a local Responses server."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
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

from app import openai_responses as responses, openclaw_runtime as runtime, transport, worker_availability
from app.accounting import Accounting
from app.errors import ApiError
from app.live_profile import OPENAI_ID, delivered
from app.runs import execute, json_value
from app.worker import Worker
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import publish_fixture_worker, workspace
from test_flow01_postgres import executor
from test_openai_responses import policy, reply, cached_reply
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(queue=[], requests=[], paths=[], responses=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state.paths.append(self.path)
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.requests.append(request)
            entry = state.queue.pop(0)
            if entry.get('close'):
                self.connection.close()
                return
            value = entry['body']() if callable(entry['body']) else entry['body']
            state.responses.append(deepcopy(value))
            data = entry.get('raw')
            if data is None:
                data = (responses.to_sse(value) if entry.get('status', 200) == 200 else json_value(value)).encode()
            if entry.get('keepalive'):
                heartbeat = b'event: keepalive\ndata: {"type":"keepalive","sequence_number":17}\n\n'
                position = data.index(b'event: response.output_item.done')
                data = heartbeat + data[:position] + heartbeat + data[position:] + heartbeat
            if entry.get('incomplete'):
                data = data[:data.rfind(b'event: response.completed')]
            if entry.get('delay'):
                time.sleep(entry['delay'])
            self.send_response(entry.get('status', 200))
            for name, header_value in entry.get('headers', {}).items():
                self.send_header(name, header_value)
            self.send_header('x-request-id', 'req_astra_fixture')
            if entry.get('chunked'):
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                # HTTP chunks deliberately split SSE frames and UTF-8 characters.
                for offset in range(0, len(data), 31):
                    chunk = data[offset:offset + 31]
                    self.wfile.write(format(len(chunk), 'x').encode() + b'\r\n' + chunk + b'\r\n')
                self.wfile.write(b'0\r\n\r\n')
            else:
                if not entry.get('unframed'):
                    self.send_header('Content-Length', str(len(data) + entry.get('missing', 0)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(responses, 'credential', lambda path: 'fixture-only-key')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw: transport.ReaderConnection('127.0.0.1', server.server_port, timeout=3))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure_astra(w, tmp_path, *, cache=False):
    value = policy(cache=cache)
    value['demo_access'] = dict(enabled=True, owner_ids=[w.actor])
    path = tmp_path / 'astra-policy.json'
    path.write_text(json_value(value))
    w.settings = w.runs.settings = replace(w.settings, model_profile_id=OPENAI_ID, ready_facts_only=False,
        embeddings_enabled=False, transport_seconds=600, lease_seconds=1000, policy_path=str(path))
    w.runs.policy = value
    w.runs.registry = delivered(w.runs.registry, OPENAI_ID)


def start_astra(w, tmp_path, *, sources=(), note=False, cache=False):
    configure_astra(w, tmp_path, cache=cache)
    request = body(sources=list(sources), question='Подготовь справку по источникам' if note else 'Объясни возможности AIST')
    request['model_profile_version_id'] = OPENAI_ID
    if note:
        request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
    send(w, conversation(w), request)
    return w.runs.claim('astra-fixture')


def tool_reply(name, args, identifier):
    value = reply(tool=True)
    value['id'] = 'resp_' + identifier
    value['output'][0]['id'] = 'rs_' + identifier
    value['output'][1].update(id='fc_' + identifier, call_id='call_' + identifier,
                              name=name, arguments=json_value(args))
    return value


def audit(w, run):
    with w.engine.connect() as c:
        calls = list(execute(c, 'SELECT data FROM aist.model_calls WHERE run_id=:r ORDER BY created_at', r=run['id']).scalars())
        usage = list(execute(c, "SELECT data FROM aist.usage_entries WHERE run_id=:r AND data->>'evidence_kind'='provider_response' ORDER BY created_at", r=run['id']).scalars())
    return calls, usage


def test_compound_executor_id_keeps_validation_error_durable(workspace, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path)
    executor_id = 'call_response|fc_response'
    reply = w.runs.tool(run['id'], run['epoch'], executor_id, 'aist_save_artifact', {})
    assert not reply['ok'] and reply['error']['code'] == 'validation_error'
    assert reply['tool_call_id'] == executor_id
    with w.engine.connect() as c:
        saved = execute(c, 'SELECT id,data FROM aist.tool_calls WHERE run_id=:r', r=run['id']).mappings().one()
    assert reply['error']['tool_call_id'] == saved['id']
    assert saved['data']['executor_tool_call_id'] == executor_id
    assert saved['data']['error'] == reply['error']
    assert w.runs.tool(run['id'], run['epoch'], executor_id, 'aist_save_artifact', {}) == reply
    assert audit(w, run) == ([], [])


def installed_run(w, run, destination):
    with TemporaryDirectory(prefix='astra-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0))
        provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close()
        provider.close()
        worker = executor(w)
        worker.id = run['worker_id']
        timer = threading.Timer(run['input']['budget']['max_duration_seconds'] + 10, worker.draining.set)
        timer.start()
        try:
            # Use the production job boundary: a saved result may complete before
            # the waiting RPC returns, which legitimately closes the Run fence.
            Worker.job(worker, run)
        finally:
            timer.cancel()
            destination.mkdir(parents=True, exist_ok=True)
            directory = Path(root) / run['id']
            for name in ('tools.jsonl', 'gateway-process.log', 'failure.json', 'openclaw.log'):
                if (directory / name).exists():
                    (destination / name).write_bytes((directory / name).read_bytes())
            resources = {}
            for name in ('memory.peak', 'memory.events', 'memory.swap.current', 'memory.swap.max', 'cpu.stat'):
                path = Path('/sys/fs/cgroup') / name
                if path.exists():
                    resources[name] = path.read_text()
            remaining = []
            for _ in range(40):
                remaining = []
                for process in Path('/proc').glob('[0-9]*'):
                    try:
                        environment = (process / 'environ').read_bytes().split(b'\0')
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        continue
                    if any(item.startswith(b'OPENCLAW_STATE_DIR=' + root.encode() + b'/') for item in environment):
                        remaining.append(process.name)
                if not remaining:
                    break
                time.sleep(.05)
            resources['remaining_executor_processes'] = remaining
            (destination / 'resources.json').write_text(json_value(resources))
            assert not remaining, 'Owned executor processes survived gateway cleanup'
    return w.runs.get(w.actor, w.project, run['id'])


@pytest.mark.parametrize('cache', [False, True])
def test_installed_gateway_preserves_responses_history_and_saves(workspace, provider, tmp_path, cache):
    w = workspace
    source, facts = note_source(w)
    run = start_astra(w, tmp_path, sources=[source['id']], cache=cache)
    first = tool_reply('aist_get_facts', dict(refs=[dict(source_version_id=source['id'], fact_id=next(iter(facts)))]), 'read')
    first['output'][1]['arguments'] = json.dumps(json.loads(first['output'][1]['arguments']), indent=2)
    final = tool_reply('aist_save_artifact', dict(content=dict(claims=[], missing_information=['Добавьте источник для анализа.'])), 'save')
    if cache:
        first['usage'] = cached_reply(inputs=100, cached=0, short=80, long=0)['usage']
        final['usage'] = cached_reply(inputs=100, cached=80, short=0, long=0)['usage']
    provider.queue = [dict(body=first), dict(body=final)]
    destination = Path('/result/astra-source-answer' + ('-cache' if cache else ''))
    state = installed_run(w, run, destination)
    (destination / 'requests.json').write_text(json_value(provider.requests))
    assert state['state'] == 'completed', state
    assert len(provider.requests) == 2
    assert provider.paths == ['/v1/responses'] * 2
    for request in provider.requests:
        assert request['reasoning'] == dict(effort='xhigh', mode='standard')
        assert request['max_output_tokens'] == 64000
        assert request['prompt_cache_options'] == (dict(mode='explicit', ttl='30m') if cache else dict(mode='explicit'))
        assert request['store'] is False and request['background'] is False
        assert request['truncation'] == 'disabled'
    history = provider.requests[1]['input']
    for item in first['output']:
        # The installed stateless client omits provider item IDs and completion status.
        expected = {key: value for key, value in item.items() if key not in ('id', 'status')}
        assert any({key: value for key, value in entry.items() if key not in ('id', 'status')} == expected for entry in history)
    assert any(item.get('type') == 'function_call_output' and item['call_id'] == 'call_read' for item in history)
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == 2
    assert all(c['provider_request_id'] == 'req_astra_fixture' and c['provider_outcome'] == 'success' for c in calls)
    assert sum(Decimal(u['estimated_usd']['value']) for u in usage) == Decimal('0.00348' if cache else '0.004')
    if cache:
        assert provider.requests[0]['prompt_cache_key'] == provider.requests[1]['prompt_cache_key']
        assert provider.requests[0]['input'][:-1] == provider.requests[1]['input'][:len(provider.requests[0]['input'])-1]
        assert [u['tokens']['cache_write_30m_input']['value'] for u in usage] == [80, 0]
        assert [u['tokens']['cached_input']['value'] for u in usage] == [0, 80]
        assert all(u['pricing_version'] == 'openai-astra.standard.cache.v2' for u in usage)
    journal = [json.loads(line) for line in (destination / 'tools.jsonl').read_text().splitlines()]
    assert journal[0]['request']['tool_call_id'] == 'call_read|fc_read'


@pytest.mark.parametrize('failure', ['disconnect', 'truncated', 'cached', 'cache_breakdown', 'refusal', 'overload'])
def test_failed_or_unpriced_send_never_retries(workspace, provider, tmp_path, failure):
    w = workspace
    run = start_astra(w, tmp_path, cache=failure == 'cache_breakdown')
    value = reply()
    entry = dict(body=value)
    if failure == 'disconnect':
        entry['close'] = True
    elif failure == 'truncated':
        entry.update(incomplete=True, keepalive=True, chunked=True)
    elif failure == 'cached':
        value['usage']['input_tokens_details']['cached_tokens'] = 1
    elif failure == 'cache_breakdown':
        value['usage']['input_tokens_details']['cache_write_tokens'] = 20
    elif failure == 'refusal':
        value['output'][-1]['content'] = [dict(type='refusal', refusal='Cannot complete')]
    elif failure == 'overload':
        entry.update(status=429, body=dict(error=dict(type='server_overload', message='Synthetic refusal')))
    provider.queue = [entry]
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'local-capability')
    request = dict(model='gpt-6-astra', input=[dict(role='user', content='Справка')])
    with pytest.raises(ApiError):
        boundary.model(request)
    assert len(provider.requests) == 1
    with pytest.raises(ApiError):
        boundary.model(dict(request, input=[dict(role='user', content='Повтор после ошибки')]))
    assert len(provider.requests) == 1
    calls, usage = audit(w, run)
    assert len(calls) == 1
    if failure in ('disconnect', 'truncated', 'cached', 'cache_breakdown', 'overload'):
        with w.engine.connect() as c:
            assert execute(c, 'SELECT exposure FROM aist.model_calls WHERE run_id=:r', r=run['id']).scalar_one() == Decimal('24')


def test_unsettled_call_blocks_other_context_before_second_reservation(workspace, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path)
    ledger = Accounting(w.runs)
    first = ledger.reserve(run['id'], run['epoch'])
    with pytest.raises(ApiError) as error:
        ledger.reserve(run['id'], run['epoch'])
    assert error.value.code == 'outcome_unknown'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE run_id=:r', r=run['id']).scalar_one() == 1
    assert first['transport_state'] == 'attempted'


@pytest.mark.parametrize('cache', [False, True])
def test_cancelled_run_accounts_for_late_response_without_tools(workspace, provider, tmp_path, cache):
    from app.service import ident

    w = workspace
    run = start_astra(w, tmp_path, cache=cache)

    def cancel_before_response():
        w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', ident('key'),
                     dict(expected_revision=run['revision']))
        worker_availability.withdraw(w.runs, 'worker_test_dispatch')
        assert w.runs.capabilities(w.actor)['model_execution']['state'] == 'temporarily_unavailable'
        publish_fixture_worker(w)
        assert w.runs.capabilities(w.actor)['model_execution']['state'] == 'busy'
        value = reply(tool=True)
        if cache:
            value['usage'] = cached_reply(inputs=100, cached=80, short=0, long=0)['usage']
        return value

    provider.queue = [dict(body=cancel_before_response, keepalive=True, chunked=True)]
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'local-capability')
    with pytest.raises(ApiError):
        boundary.model(dict(model='gpt-6-astra', input=[dict(role='user', content='Справка')]))
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    assert w.runs.capabilities(w.actor)['model_execution']['state'] == 'available'
    calls, usage = audit(w, run)
    assert len(provider.requests) == len(calls) == len(usage) == 1
    assert calls[0]['late_response'] and calls[0]['delivery'] == 'suppressed'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.tool_calls WHERE run_id=:r', r=run['id']).scalar_one() == 0
        assert execute(c, 'SELECT exposure FROM aist.model_calls WHERE run_id=:r', r=run['id']).scalar_one() == Decimal('0.00128' if cache else '0.002')


def test_cache_policy_reload_does_not_reprice_or_enable_cache_for_existing_run(workspace, provider, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path)
    configure_astra(w, tmp_path, cache=True)
    provider.queue = [dict(body=reply())]
    runtime.Boundary(executor(w), run, tmp_path, 'local-capability').model(
        dict(model='gpt-6-astra', input=[dict(role='user', content='Справка')]))
    assert provider.requests[0]['prompt_cache_options'] == dict(mode='explicit')
    assert 'prompt_cache_key' not in provider.requests[0]
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == 1
    assert usage[0]['pricing_version'] == 'openai-astra.standard.no-cache.v1'
    assert usage[0]['estimated_usd']['value'] == '0.002'
    with w.engine.connect() as c:
        configuration = execute(c, 'SELECT configuration FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert configuration['policy']['pricing_version'] == 'openai-astra.standard.no-cache.v1'


def test_cache_does_not_bypass_body_limit_or_reserve_for_an_unsent_request(workspace, provider, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path, cache=True)
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'local-capability')
    with pytest.raises(ApiError) as failure:
        boundary.model(dict(model='gpt-6-astra', input=[dict(role='user', content='я' * 300000)]))
    assert failure.value.code == 'budget_exceeded'
    assert provider.requests == []
    assert audit(w, run) == ([], [])


@pytest.mark.parametrize('secret_echo', [False, True])
def test_large_response_and_secret_echo_audit_boundary(workspace, provider, tmp_path, secret_echo):
    w = workspace
    run = start_astra(w, tmp_path)
    value = reply()
    value['output'][0]['encrypted_content'] = 'fixture-only-key' if secret_echo else 'x' * (2 * 1024 * 1024)
    provider.queue = [dict(body=value)]
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'local-capability')
    request = dict(model='gpt-6-astra', input=[dict(role='user', content='Справка')])
    if secret_echo:
        with pytest.raises(ApiError):
            boundary.model(request)
        assert not any(b'fixture-only-key' in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
    else:
        result = boundary.model(request)
        assert result['output'][0]['encrypted_content'] == value['output'][0]['encrypted_content']
    assert len(provider.requests) == 1


def text_reply(text, identifier):
    value = reply()
    value['id'] = 'resp_' + identifier
    value['output'] = [value['output'][0], value['output'][-1]]
    value['output'][0]['id'] = 'rs_' + identifier
    value['output'][-1].update(id='msg_' + identifier)
    value['output'][-1]['content'][0]['text'] = text
    return value


def test_total_budget_holds_unknown_spend_across_runs(workspace, tmp_path):
    w = workspace
    for index in range(3):
        run = start_astra(w, tmp_path)
        ledger = Accounting(w.runs)
        call = ledger.reserve(run['id'], run['epoch'])
        ledger.mark_unknown(run['id'], call['id'], 'Synthetic unobserved outcome')
        w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
        w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    with pytest.raises(ApiError) as error:
        run = start_astra(w, tmp_path)
        Accounting(w.runs).reserve(run['id'], run['epoch'])
    assert error.value.code == 'budget_exceeded'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT sum(exposure) FROM aist.model_calls').scalar_one() == Decimal('72')
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 3


def test_refined_unknown_reservation_preserves_history_and_blocks_replay(workspace, tmp_path):
    w = workspace
    ledger = Accounting(w.runs)
    for _ in range(3):
        run = start_astra(w, tmp_path)
        call = ledger.reserve(run['id'], run['epoch'])
        ledger.mark_unknown(run['id'], call['id'], 'Synthetic lost connection')
        w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
        w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    with w.engine.begin() as c:
        saved_run = w.runs.locked(c, run['id'])
        saved_call = ledger.call(c, run['id'], call['id'])['data']
        previous_id = saved_call['usage_entry_id']
        usage = ledger.usage(c, saved_run, saved_call, kind='reconciliation', reserved='6')
        ledger.save_call(c, saved_call, usage)
        assert usage['supersedes_id'] == previous_id and usage['reservation_state'] == 'held'
        assert all(value['value'] is None for value in usage['tokens'].values())
        assert usage['estimated_usd']['value'] is None and usage['billed_usd']['value'] is None
        assert saved_call['transport_state'] == saved_call['provider_outcome'] == 'unknown'
        assert saved_run['state'] == 'failed'
        assert execute(c, "SELECT data->>'reserved_usd' FROM aist.usage_entries WHERE id=:id", id=previous_id).scalar_one() == '24'
        assert execute(c, 'SELECT sum(exposure) FROM aist.model_calls').scalar_one() == Decimal('54')
        with pytest.raises(ApiError) as error:
            ledger.require_settled(c, saved_run)
        assert error.value.code == 'outcome_unknown'
    next_run = start_astra(w, tmp_path)
    next_call = ledger.reserve(next_run['id'], next_run['epoch'])
    with w.engine.connect() as c:
        assert execute(c, 'SELECT sum(exposure) FROM aist.model_calls').scalar_one() == Decimal('78')
    ledger.mark_unknown(next_run['id'], next_call['id'], 'Synthetic lost connection')
    w.runs.stop(next_run['id'], next_run['epoch'], 'outcome_unknown')
    w.runs.release_finished(next_run['id'], next_run['epoch'], next_run['worker_id'])
    with pytest.raises(ApiError) as error:
        next_run = start_astra(w, tmp_path)
        ledger.reserve(next_run['id'], next_run['epoch'])
    assert error.value.code == 'budget_exceeded'


@pytest.mark.parametrize('options', [
    {'reserved': '0'}, {'reserved': '25'}, {'reserved': '6', 'released': True},
    {'reserved': '6', 'estimate': '0'}, {'reserved': '6', 'billed': '0'},
    {'reserved': '6', 'kind': 'provider_response'},
])
def test_refining_a_reservation_cannot_release_or_invent_usage(workspace, tmp_path, options):
    w = workspace
    run = start_astra(w, tmp_path)
    ledger = Accounting(w.runs)
    call = ledger.reserve(run['id'], run['epoch'])
    ledger.mark_unknown(run['id'], call['id'], 'Synthetic lost connection')
    with w.engine.begin() as c:
        saved_run = w.runs.locked(c, run['id'])
        saved_call = ledger.call(c, run['id'], call['id'])['data']
        with pytest.raises(ApiError):
            ledger.usage(c, saved_run, saved_call, **dict({'kind': 'reconciliation'}, **options))
        assert execute(c, 'SELECT exposure FROM aist.model_calls WHERE id=:id', id=call['id']).scalar_one() == Decimal('24')


@pytest.mark.parametrize('code', ['context_length_exceeded', 'invalid_encrypted_content'])
def test_installed_error_recovery_cannot_submit_a_second_paid_call(workspace, provider, tmp_path, code):
    w = workspace
    run = start_astra(w, tmp_path)
    provider.queue = [dict(status=400, body=dict(error=dict(type='invalid_request_error', code=code, message=code)))]
    state = installed_run(w, run, Path('/result') / ('astra-recovery-' + code))
    assert state['state'] in ('failed', 'budget_exceeded')
    assert len(provider.requests) == 1
    calls, _ = audit(w, run)
    assert len(calls) == 1


@pytest.mark.parametrize('cache', [False, True])
def test_installed_note_extracts_images_researches_saves_and_rewrites(workspace, provider, tmp_path, monkeypatch, cache):
    from app import edit_proposals as edits, note_external
    from app.service import ident
    from test_fable_postgres import THEMATIC_LINES
    from test_flow01_02_postgres import GOOD, QUAL, note
    from test_perf02_r1_postgres import upload, fact_rows
    from test_perf02_r4_postgres import table_row, ref
    from test_web_research_postgres import reader, QUERY, URL, TEXT

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    from datetime import datetime
    from app import runs as run_clock

    source = upload(w, 'astra-visual', THEMATIC_LINES)
    created = '2026-12-31T21:00:00+00:00' if cache else '2026-12-31T20:59:59+00:00'
    expected_day = '2027-01-01' if cache else '2026-12-31'
    # Set the database clock for creation only. The executor starts after Moscow midnight.
    with w.engine.begin() as c:
        execute(c, 'ALTER TABLE aist.runs ALTER COLUMN created_at SET DEFAULT CAST(:stamp AS timestamptz)', stamp=created)
    monkeypatch.setattr(run_clock, 'utcnow', lambda: datetime.fromisoformat('2026-12-31T21:00:01+00:00'))
    run = start_astra(w, tmp_path, sources=[source['id']], note=True, cache=cache)
    assert run['created_at'] == datetime.fromisoformat(created)
    note_external.prepare_all(w.runs, run)
    assert run['external_context']['as_of'] == expected_day
    public_calls, cutoff_dates = [], []
    fetch = reader(public_calls)

    def web_request(name, args, as_of, *limits):
        cutoff_dates.append(as_of)
        response = fetch(name, args, as_of, *limits)
        if name == 'aist_web_fetch':
            response.update(published=expected_day, quote=TEXT + ' Дата публикации: ' + expected_day + '.')
        return response

    monkeypatch.setattr(note_external, 'web_request', web_request)

    def fetch_reply():
        with w.engine.connect() as c:
            search = execute(c, "SELECT id FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_web_search'", r=run['id']).scalar_one()
        return tool_reply('aist_web_fetch', dict(search_id=search, url=URL), 'fetch')

    def save_reply():
        fact = next(row['data'] for row in fact_rows(w) if row['data']['amount'] is None)
        content = note(fact)
        financial = next(row['data'] for row in fact_rows(w) if row['data']['amount'])
        content['sections'].append(dict(section_id='sec_finance', title='Финансы', blocks=[dict(
            block_id='block_revenue', kind='table', caption='Выручка по МСФО, млн руб.', rows=[
                table_row('row_revenue', 'Выручка', financial['period']['label'], financial['amount']['value'],
                          [ref(source, financial['fact_id'])])])]))
        with w.engine.connect() as c:
            context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        content['sections'][0]['blocks'].append(dict(kind='paragraph', block_id='research_context', claim=dict(
            text=context['items'][0]['quote'], origin='external_context', external_evidence_ids=[context['items'][0]['evidence_id']], fact_refs=[], calculation_ids=[])))
        event = dict(external_evidence_id=context['items'][0]['evidence_id'], description='Компания описала поставки оборудования.',
                     published=expected_day, event_date=None, date_quote='Дата публикации: ' + expected_day + '.')
        value = tool_reply('aist_save_artifact', dict(content=content, news_events=[event], ownership_review=dict(status='not_established',
            reason='Синтетические материалы раскрывают бизнес и рынок, владельцы не указаны.', excluded_sources=[])), 'note_save')
        value['output'].insert(1, dict(type='message', id='msg_save_commentary', role='assistant',
            phase='commentary', status='completed', content=[dict(type='output_text', text='Сохраняю записку.', annotations=[])]))
        return value

    provider.queue = [
        dict(body=text_reply(json_value(dict(facts=[GOOD, QUAL])), 'financial')),
        dict(body=text_reply(json_value(dict(facts=[QUAL])), 'business')),
        dict(body=tool_reply('aist_research', dict(company='Орбита', objective='Описание бизнеса и рынка Орбиты'), 'research')),
        dict(body=tool_reply('aist_web_search', QUERY, 'search')),
        dict(body=fetch_reply),
        dict(body=text_reply('Найдено описание бизнеса. Источник сохранен.', 'research_done')),
        dict(body=save_reply),
    ]
    for entry in provider.queue:
        entry.update(keepalive=True, chunked=True)
    destination = Path('/result/astra-note-research-rewrite' + ('-cache' if cache else ''))
    state = installed_run(w, run, destination)
    (destination / 'requests.json').write_text(json_value(provider.requests))
    assert state['state'] == 'completed', state
    assert len(provider.requests) == 7 and len(public_calls) == 2
    saved = w.runs.version(w.actor, w.project, state['result_version_id'])
    version = saved['version']
    assert saved['review']['acceptance'] is None
    assert version['provenance']['origin'] == 'generation' and version['parent_version_id'] is None
    evidence, = version['basis']['external_context']['items']
    assert evidence['published'] == expected_day and cutoff_dates == [expected_day, expected_day]
    assert version['basis']['external_context']['as_of'] == expected_day
    news = version['basis']['news_run']
    assert news['as_of'] == expected_day and news['items'][0]['published'] == expected_day
    def prompt_text(request):
        return json.dumps(request['input'], ensure_ascii=False).replace(chr(92) + '"', '"')
    assert f'"note_as_of":"{expected_day}"' in prompt_text(provider.requests[2])
    assert f'"as_of":"{expected_day}"' in prompt_text(provider.requests[3])
    assert any(f['amount'] and f['amount']['value'] == '100' for f in version['basis']['facts'])
    assert 'Extract each disclosed investment' in json_value(provider.requests[1])
    assert 'numeric.nature=forecast' in json_value(provider.requests[1])
    save_tool = next(tool for tool in provider.requests[2]['tools'] if tool['name'] == 'aist_save_artifact')
    assert 'ownership_review' in save_tool['parameters']['required']
    for request in provider.requests[:2]:
        images = [block for item in request['input'] if isinstance(item.get('content'), list)
                  for block in item['content'] if block.get('type') == 'input_image']
        assert images and all(image['detail'] == 'original' for image in images)
        assert request['text']['format']['type'] == 'json_object'
    assert {tool['name'] for tool in provider.requests[3]['tools']} == {'aist_web_search', 'aist_web_fetch', 'aist_read_web'}
    # The researcher's real remaining quota must shrink after each paid step,
    # while the analyst's reserved calls remain unavailable to it.
    for index, remaining in ((3, 12), (4, 11), (5, 10)):
        context = provider.requests[index]['input'][-1 if cache else 0]
        assert context['role'] == 'developer'
        assert f'остается {remaining} общих обращений' in context['content']
        assert f'не более {remaining - 5} для исследования' in context['content']
    # Returning to the analyst includes the researcher's paid calls in its count.
    assert 'остается 9 общих обращений' in provider.requests[6]['input'][-1 if cache else 0]['content']
    if cache:
        analyst = provider.requests[2]['prompt_cache_key']
        researcher = provider.requests[3]['prompt_cache_key']
        assert analyst != researcher
        assert provider.requests[6]['prompt_cache_key'] == analyst
        assert all(request['prompt_cache_key'] == researcher for request in provider.requests[3:6])
        for first, following in ((2, 6), (3, 4), (3, 5)):
            prefix = provider.requests[first]['input'][:-1]
            assert provider.requests[following]['input'][:len(prefix)] == prefix
        assert all('prompt_cache_key' not in request for request in provider.requests[:2])
        assert all(len(json_value(request).encode()) <= 600000 for request in provider.requests[2:])
    assert 'aist_research' in {tool['name'] for tool in provider.requests[2]['tools']}
    calls, usage = audit(w, run)
    assert [call['purpose'] for call in calls[:2]] == ['extraction', 'extraction']
    assert len(calls) == len(usage) == 7
    assert all(call['transport_state'] == 'observed' and call['delivery'] == 'delivered' for call in calls)
    assert sum(Decimal(item['estimated_usd']['value']) for item in usage) == Decimal('0.014')
    from app.exports import Exports
    from app.note_renderer import renderer_versions
    exports = Exports(w.runs)
    for fmt in ('docx', 'pdf'):
        status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'],
            ident('key'), dict(expected_revision=saved['review']['revision'], format=fmt,
                template_version='aist-aso.v1', renderer_version=renderer_versions('aist-aso.v1')[fmt],
                settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft')))
        assert status == 202
        exports.process_one()
        data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
        assert ready['state'] == 'ready'
        assert ready['artifact_version_id'] == version['id'] and ready['content_sha256'] == version['content_sha256']
        assert data.startswith(b'PK' if fmt == 'docx' else b'%PDF')
        assert exports.get(w.actor, w.project, entry['id'], download=True)[0] == data
        (destination / ('note.' + fmt)).write_bytes(data)
    assert len(provider.requests) == 7 and audit(w, run) == (calls, usage)
    journal = [json.loads(line) for line in (destination / 'tools.jsonl').read_text().splitlines()]
    save = next(item for item in journal if item['request']['name'] == 'aist_save_artifact')
    from app.model_save import save_operation
    args = save_operation(run, save['request']['args'])
    with w.engine.connect() as connection:
        counts = {table: execute(connection, 'SELECT count(*) FROM aist.' + table).scalar_one()
                  for table in ('artifact_versions', 'messages', 'tool_calls', 'model_calls', 'usage_entries')}
    for executor_id in (save['request']['tool_call_id'], 'call_save_again|fc_save_again'):
        replay = w.runs.tool(run['id'], run['epoch'], executor_id, 'aist_save_artifact', args, save['model_call_id'])
        assert replay['ok'] and replay['result']['version']['id'] == version['id']
    with w.engine.connect() as connection:
        assert counts == {table: execute(connection, 'SELECT count(*) FROM aist.' + table).scalar_one() for table in counts}
        assert execute(connection, "SELECT count(*) FROM aist.run_events WHERE run_id=:r AND data->>'type'='recovery_recorded'", r=run['id']).scalar_one() == 0

    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    chat = state['conversation_id']
    scope = (w.runs, w.actor, w.session, w.project, chat)
    command = 'Сократи первый абзац'
    choice = edits.prepare(*scope, ident('key'), dict(version_id=version['id'], text=command))
    assert choice['kind'] == 'rewrite'
    conv = w.service.get(w.actor, w.project, 'conversations', chat)
    request = dict(body(revision=conv['revision'], question=command),
        model_profile_version_id=OPENAI_ID, edit_request={key: choice[key] for key in ('version_id', 'target')})
    send(w, conv, request)
    rewrite = w.runs.claim('astra-rewrite-fixture')
    provider.queue = [dict(body=text_reply('Компания поставляет оборудование.', 'rewrite'))]
    edits.run_rewrite(executor(w), rewrite, tmp_path / 'rewrite')
    assert 'prompt_cache_key' not in provider.requests[-1]
    proposal, = edits.listing(w.runs, w.actor, w.project, chat, {})['items']
    assert proposal['after'] == 'Компания поставляет оборудование.' and proposal['state'] == 'pending'
    applied = edits.decide(*scope, proposal['id'], ident('key'), 'apply')
    edited = w.runs.version(w.actor, w.project, applied['applied_version_id'])['version']
    assert edited['parent_version_id'] == version['id']
    assert edited['basis'] == version['basis']
    assert edited['basis']['ownership_review']['status'] == 'not_established'
    assert w.runs.version(w.actor, w.project, version['id'])['version']['basis'] == version['basis']
    assert w.runs.version(w.actor, w.project, version['id'])['version']['content_sha256'] == version['content_sha256']
    for request in provider.requests:
        assert request['reasoning'] == dict(effort='xhigh', mode='standard')
        assert request['max_output_tokens'] == 64000
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    (destination / 'result.json').write_text(json_value(dict(state=state, calls=calls, usage=usage,
        saved=saved, edited=edited, requests=provider.requests)))
