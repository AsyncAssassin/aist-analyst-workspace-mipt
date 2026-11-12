"""Сохраненный ответ и отказ веб-запроса проверяются на штатной схеме PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import OperationalError

from app import openclaw_runtime as runtime, transport
from app.accounting import Accounting
from app.database import make_engine
from app.errors import ApiError
from app.live_profile import FABLE, MODEL
from app.received_response import recover_observed
from app.service import ident
from app.runs import execute
from test_d15_postgres import workspace
from test_fable_postgres import native, reply as native_reply, start_fable
from test_flow01_postgres import executor, current
from test_openai_responses import cached_reply, reply
from test_openai_responses_postgres import installed_run, provider, start_astra, tool_reply
from test_web_research_postgres import initialized, reader, QUERY, URL

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
REQUEST = dict(model='gpt-6-astra', input=[dict(role='user', content='Synthetic request')])


@contextmanager
def project_blocker(workspace, *, seconds=None):
    engine = make_engine(workspace.settings)
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with engine.begin() as connection:
            execute(connection, 'SELECT id FROM aist.projects WHERE id=:id FOR UPDATE', id=workspace.project)
            acquired.set()
            release.wait(seconds if seconds is not None else 20)

    with ThreadPoolExecutor(max_workers=1) as threads:
        holding = None

        def lock():
            nonlocal holding
            holding = threads.submit(hold)
            assert acquired.wait(5)

        try:
            yield lock
        finally:
            release.set()
            if holding is not None:
                holding.result(timeout=5)
            engine.dispose()


def workspace_root(workspace, root):
    root.mkdir()
    workspace.runs.settings = workspace.settings = replace(workspace.settings, openclaw_root=str(root))
    return root


def ledger_state(workspace, run):
    with workspace.engine.connect() as connection:
        call = execute(connection, 'SELECT * FROM aist.model_calls WHERE run_id=:id', id=run['id']).mappings().one()
        usage = list(execute(connection, 'SELECT data FROM aist.usage_entries WHERE model_call_id=:id ORDER BY created_at',
                             id=call['id']).scalars())
        events = list(execute(connection, 'SELECT state FROM aist.model_transport_events WHERE call_id=:id ORDER BY seq',
                              id=call['id']).scalars())
    return call, usage, events


def assert_observed(workspace, run, *, delivered, posts, history=()):
    call, usage, events = ledger_state(workspace, run)
    response, = [entry for entry in usage if entry['evidence_kind'] == 'provider_response']
    assert call['data']['delivery'] == ('delivered' if delivered else 'suppressed')
    assert call['data']['late_response'] is not delivered
    assert call['usage_entry_id'] == response['id']
    assert call['exposure'] == type(call['exposure'])(response['estimated_usd']['value'])
    assert response['reservation_state'] == 'released'
    assert events.count('observed') == 1
    assert len(posts) == 1
    assert usage[:len(history)] == list(history)
    print(dict(posts=len(posts), delivery=call['data']['delivery'], events=events, exposure=str(call['exposure'])))
    return response


def test_waits_for_same_response_after_three_second_project_lock(workspace, provider, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    pause = worker.closed.wait
    attempts = []

    def verify_released(seconds):
        assert not worker.persistence._is_owned()
        assert w.engine.pool.checkedout() == 0
        attempts.append(seconds)
        return pause(seconds)

    worker.closed.wait = verify_released
    with project_blocker(w, seconds=3.45) as lock:
        def received():
            lock()
            return reply()

        provider.queue = [dict(body=received)]
        result = runtime.Boundary(worker, run, root / run['id'], 'synthetic').model(REQUEST)
    assert result == reply()
    assert attempts
    assert_observed(w, run, delivered=True, posts=provider.requests)


@pytest.mark.parametrize('acknowledged', [True, False])
@pytest.mark.parametrize('fresh_unavailable', [False, True])
def test_unknown_commit_reads_ledger_before_replaying_local_write(workspace, provider, tmp_path, monkeypatch,
                                                                 acknowledged, fresh_unavailable):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    injected = []

    class ConnectionLost(Exception):
        pgcode = '08006'

    def inject(engine, connection, data, usage):
        real_save(connection, data, usage)
        if data['transport_state'] == 'observed' and not injected:
            original = engine.dialect.do_commit

            def ambiguous(dbapi):
                monkeypatch.setattr(engine.dialect, 'do_commit', original)
                (original if acknowledged else engine.dialect.do_rollback)(dbapi)
                injected.append(acknowledged)
                if fresh_unavailable:
                    connect = engine.connect

                    def temporarily_disconnected():
                        monkeypatch.setattr(engine, 'connect', connect)
                        injected.append('new_session_unavailable')
                        raise OperationalError(None, None, ConnectionLost(), connection_invalidated=True)

                    monkeypatch.setattr(engine, 'connect', temporarily_disconnected)
                raise OperationalError(None, None, ConnectionLost(), connection_invalidated=True)

            monkeypatch.setattr(engine.dialect, 'do_commit', ambiguous)

    real_save = worker.transport.ledger.save_call
    monkeypatch.setattr(worker.transport.ledger, 'save_call', lambda *args: inject(w.engine, *args))
    provider.queue = [dict(body=reply())]
    payload = runtime.Boundary(worker, run, root / run['id'], 'synthetic').model(REQUEST)
    assert payload == reply()
    assert injected == ([acknowledged, 'new_session_unavailable'] if fresh_unavailable else [acknowledged])
    assert_observed(w, run, delivered=True, posts=provider.requests)


def interrupt_observation(monkeypatch, worker):
    def interrupt(*args, **kwargs):
        raise ApiError('outcome_unknown')
    monkeypatch.setattr(worker.transport.ledger, 'observe', interrupt)


@pytest.mark.parametrize('mutation', [
    'none', 'extra_partial', 'no_manifest', 'temporary_only', 'response', 'request', 'profile', 'owner', 'run',
    'call', 'symlink', 'fifo', 'oversize', 'incomplete', 'incomplete_after_publication',
    'invalid_usage', 'wrong_model', 'only_partial',
    401, 403, 404, 429, 500,
])
def test_recover_only_complete_scoped_responses(workspace, provider, tmp_path, monkeypatch, mutation):
    w = workspace
    run = start_astra(w, tmp_path, cache=True)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    interrupt_observation(monkeypatch, worker)
    payload = cached_reply(inputs=150, cached=30, short=50, long=20)
    if mutation == 'invalid_usage':
        payload.pop('usage')
    if mutation == 'wrong_model':
        payload['model'] = 'wrong'
    entry = dict(body=payload)
    if isinstance(mutation, int):
        entry = dict(status=mutation, body={'error': {'message': 'Synthetic refusal'}})
    if mutation in ('incomplete', 'only_partial'):
        entry['incomplete'] = True
    provider.queue = [entry]
    with pytest.raises(ApiError):
        runtime.Boundary(worker, run, root / run['id'], 'synthetic').model(REQUEST)
    call, before, _ = ledger_state(w, run)
    manifest = root / '.received' / run['id'] / (call['id'] + '.json')
    audit = root / run['id'] / 'calls'
    request_path = audit / (call['id'] + '-request.json')
    response_path = audit / (call['id'] + '-response.sse')
    if mutation == 'no_manifest':
        manifest.unlink()
    elif mutation == 'temporary_only':
        manifest.rename(manifest.with_name('.tmp-incomplete'))
    elif mutation in ('profile', 'owner', 'run', 'call'):
        document = json.loads(manifest.read_bytes())
        field = dict(profile='profile_id', owner='owner_id', run='run_id', call='call_id')[mutation]
        document['binding'][field] = 'different'
        manifest.write_text(json.dumps(document))
    elif mutation in ('response', 'request'):
        target = response_path if mutation == 'response' else request_path
        with target.open('ab') as stream:
            stream.write(b' ')
    elif mutation in ('symlink', 'fifo'):
        original = response_path.rename(response_path.with_suffix('.original'))
        if mutation == 'symlink':
            response_path.symlink_to(original)
        else:
            os.mkfifo(response_path)
    elif mutation == 'oversize':
        with response_path.open('wb') as stream:
            stream.truncate(100_000_000)
    elif mutation == 'incomplete_after_publication':
        raw = response_path.read_bytes()
        raw = raw[:raw.rindex(b'event: response.completed')]
        response_path.write_bytes(raw)
        document = json.loads(manifest.read_bytes())
        document['response_sha256'] = sha256(raw).hexdigest()
        manifest.write_text(json.dumps(document))
    elif mutation == 'only_partial':
        response_path.unlink()
    elif mutation == 'none':
        (audit / (call['id'] + '-partial.bin')).unlink()

    if mutation in ('none', 'extra_partial'):
        with ThreadPoolExecutor(max_workers=2) as threads:
            results = list(threads.map(lambda _: recover_observed(w.runs, run['id']), range(2)))
        assert {result['calls'][0]['status'] for result in results} == {'recovered', 'already_accounted'}
        response = assert_observed(w, run, delivered=False, posts=provider.requests, history=before)
        assert response['tokens']['cache_write_30m_input']['value'] == 50
        assert current(w, run)['state'] == 'recovery_required'
        if mutation == 'extra_partial':
            w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
            w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
            control = start_astra(w, tmp_path, cache=True)
            provider.queue = [dict(body=payload)]
            runtime.Boundary(executor(w), control, root / control['id'], 'synthetic').model(REQUEST)
            normal = assert_observed(w, control, delivered=True, posts=provider.requests[1:])
            assert normal['estimated_usd'] == response['estimated_usd']
            assert normal['tokens'] == response['tokens']
    else:
        assert recover_observed(w.runs, run['id'])['calls'] == [dict(call_id=call['id'], status='insufficient_evidence')]
        saved, history, _ = ledger_state(w, run)
        assert saved == call and history == before
        assert len(provider.requests) == 1


def test_active_call_is_left_to_its_boundary(workspace, provider, tmp_path, monkeypatch):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    ready, release = threading.Event(), threading.Event()
    publish = transport.publish

    def pause_after_publication(*args, **kwargs):
        publish(*args, **kwargs)
        ready.set()
        assert release.wait(5)

    monkeypatch.setattr(transport, 'publish', pause_after_publication)
    provider.queue = [dict(body=reply())]
    with ThreadPoolExecutor(max_workers=1) as threads:
        boundary = runtime.Boundary(worker, run, root / run['id'], 'synthetic')
        pending = threads.submit(boundary.model, REQUEST)
        try:
            assert ready.wait(5)
            result = recover_observed(w.runs, run['id'])
            assert result['calls'][0]['status'] == 'active'
        finally:
            release.set()
        assert pending.result() == reply()
    assert_observed(w, run, delivered=True, posts=provider.requests)



@pytest.mark.parametrize('change', ['lease', 'epoch', 'cancel', 'terminal', 'revoke', 'closed'])
def test_response_after_authority_loss_has_no_delivery(workspace, provider, tmp_path, monkeypatch, change):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    publish = transport.publish

    def lose_authority(*args):
        publish(*args)
        if change == 'closed':
            worker.closed.set()
            return
        if change == 'cancel':
            w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', ident('key'),
                         dict(expected_revision=run['revision']))
            return
        if change == 'terminal':
            w.runs.stop(run['id'], run['epoch'], 'tool_failed')
            return
        changes = dict(
            lease="UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id",
            epoch='UPDATE aist.runs SET epoch=epoch+1 WHERE id=:id',
            revoke='UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:id',
        )
        with w.engine.begin() as connection:
            execute(connection, changes[change], id=w.session if change == 'revoke' else run['id'])

    monkeypatch.setattr(transport, 'publish', lose_authority)
    provider.queue = [dict(body=reply(tool=True))]
    boundary = runtime.Boundary(worker, run, root / run['id'], 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(REQUEST)
    assert not boundary.issued
    if change == 'closed':
        w.runs.recover(owned=run['worker_id'])
        assert recover_observed(w.runs, run['id'])['calls'][0]['status'] == 'recovered'
    assert_observed(w, run, delivered=False, posts=provider.requests)
    assert current(w, run)['state'] == dict(lease='running', epoch='running', cancel='cancelled',
                                         terminal='failed', revoke='failed', closed='recovery_required')[change]
    with w.engine.connect() as connection:
        assert execute(connection, 'SELECT count(*) FROM aist.tool_calls WHERE run_id=:id', id=run['id']).scalar_one() == 0
        assert execute(connection, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:id',
                       id=w.project).scalar_one() == 0


def test_exhausted_wait_leaves_complete_evidence_and_reservation(workspace, provider, tmp_path, monkeypatch):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    monkeypatch.setattr(transport, 'OBSERVE_SECONDS', 1)
    with project_blocker(w) as lock:
        def received():
            lock()
            return reply()

        provider.queue = [dict(body=received)]
        with pytest.raises(ApiError):
            runtime.Boundary(worker, run, root / run['id'], 'synthetic').model(REQUEST)
        call, history, events = ledger_state(w, run)
        assert events[-1] == 'sent'
        assert history[-1]['reservation_state'] == 'held'
        assert list((root / '.received' / run['id']).glob('call_*.json'))
    with w.engine.begin() as connection:
        execute(connection, "UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id", id=run['id'])
    assert recover_observed(w.runs, run['id'])['calls'][0]['status'] == 'recovered'
    assert_observed(w, run, delivered=False, posts=provider.requests, history=history)


def worker_environment(w, root):
    return dict(os.environ, AIST_OPENCLAW_ROOT=str(root), AIST_POLICY_PATH=w.settings.policy_path,
                AIST_EXECUTOR_MODE='openclaw', AIST_LIVE_MODELS_ENABLED='true',
                AIST_MODEL_PROFILE_ID=w.settings.model_profile_id, AIST_SHUTDOWN_SECONDS='1')


@pytest.mark.parametrize('automatic', [True, False])
def test_new_process_recovers_after_killing_the_sender(workspace, provider, tmp_path, automatic):
    w = workspace
    run = start_astra(w, tmp_path)
    root = workspace_root(w, tmp_path / 'execution')
    env = worker_environment(w, root)
    provider.queue = [dict(body=reply())]
    port = transport.ReaderHTTPSConnection('local-fixture', 443).port
    child = """
import json, os, signal, sys
from pathlib import Path
from app import openclaw_runtime, openai_responses, transport
from app.runs import row
from app.worker import Worker

run_id, port = sys.argv[1:]
worker = Worker()
with worker.engine.connect() as connection:
    run = row(connection, 'SELECT * FROM aist.runs WHERE id=:id', id=run_id)
openai_responses.credential = lambda _: 'fixture-only-key'
transport.ReaderHTTPSConnection = lambda *args, **kwargs: transport.ReaderConnection('127.0.0.1', int(port), timeout=3)
publish = transport.publish
def stop(*args):
    publish(*args)
    print('published', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
transport.publish = stop
boundary = openclaw_runtime.Boundary(worker, run, Path(worker.settings.openclaw_root) / run_id, 'synthetic')
boundary.model(json.loads(os.environ['REQUEST']))
"""
    sender = subprocess.Popen([sys.executable, '-c', child, run['id'], str(port)],
                              env=dict(env, REQUEST=json.dumps(REQUEST)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        marker = root / '.received' / run['id']
        deadline = time.monotonic() + 12
        ready, _, _ = select.select([sender.stdout], [], [], deadline - time.monotonic())
        assert ready and sender.stdout.readline().strip() == 'published'
        assert list(marker.glob('call_*.json'))
    finally:
        sender.kill()
        sender.communicate(timeout=5)
    call, history, _ = ledger_state(w, run)
    assert call['data']['transport_state'] == 'sent'
    if not automatic:
        Accounting(w.runs).mark_unknown(run['id'], call['id'], 'synthetic interruption')
        _, history, _ = ledger_state(w, run)
    with w.engine.begin() as connection:
        execute(connection, "UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id", id=run['id'])
    command = [sys.executable, '-m', 'app.cli', 'recover-observed', '--run', run['id']]
    if automatic:
        restart = """
import threading
from app.worker import Worker
worker = Worker()
timer = threading.Timer(2, worker.signal)
timer.start()
try:
    raise SystemExit(worker.run())
finally:
    timer.cancel()
"""
        done = subprocess.run([sys.executable, '-c', restart], env=env, capture_output=True, text=True, timeout=15)
    else:
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert json.loads(done.stdout)['calls'][0]['status'] == 'recovered'
    assert done.returncode == 0, done.stdout + done.stderr
    again = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(again.stdout)['calls'][0]['status'] == 'already_accounted'
    assert_observed(w, run, delivered=False, posts=provider.requests, history=history)
    assert current(w, run)['state'] in ('recovery_required', 'failed')


def test_native_recovery_uses_actual_cache_usage(workspace, native, tmp_path, monkeypatch):
    w = workspace
    _, _, run = start_fable(w)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    native.queue = [dict(body=native_reply())]
    interrupt_observation(monkeypatch, worker)
    with pytest.raises(ApiError):
        runtime.Boundary(worker, run, root / run['id'], 'synthetic').model(
            dict(model=FABLE['model'], messages=[dict(role='user', content='Synthetic request')]))
    assert recover_observed(w.runs, run['id'])['calls'][0]['status'] == 'recovered'
    response = assert_observed(w, run, delivered=False, posts=native.requests)
    assert response['tokens']['cache_write_1h_input']['value'] == 10
    w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
    _, _, control = start_fable(w)
    native.queue = [dict(body=native_reply())]
    runtime.Boundary(executor(w), control, root / control['id'], 'synthetic').model(
        dict(model=FABLE['model'], messages=[dict(role='user', content='Synthetic request')]))
    normal = assert_observed(w, control, delivered=True, posts=native.requests[1:])
    assert normal['estimated_usd'] == response['estimated_usd']
    assert normal['tokens'] == response['tokens']


@pytest.mark.parametrize('truncated', [False, True])
def test_chat_completions_recovery_uses_exact_runtime_request(workspace, provider, tmp_path, monkeypatch, truncated):
    w = workspace
    _, run = initialized(w)
    root = workspace_root(w, tmp_path / 'execution')
    worker = executor(w)
    from app import novita
    monkeypatch.setattr(novita, 'credential', lambda _: 'fixture-only-key')
    payload = dict(id='fixture-chat', model=MODEL, choices=[dict(message=dict(content='Synthetic reply'), finish_reason='stop')],
                   usage=dict(prompt_tokens=123, completion_tokens=25))
    provider.queue = [dict(body=payload, raw=json.dumps(payload).encode())]
    interrupt_observation(monkeypatch, worker)
    with pytest.raises(ApiError):
        worker.transport.perform(run, {'purpose': 'agent'}, request=dict(messages=[dict(role='user', content='Synthetic')]),
                                 audit=root / run['id'] / 'calls', runtime_context='Synthetic context')
    if truncated:
        call, before, _ = ledger_state(w, run)
        manifest = root / '.received' / run['id'] / (call['id'] + '.json')
        response_path = root / run['id'] / 'calls' / (call['id'] + '-response.json')
        raw = response_path.read_bytes()[:-1]
        response_path.write_bytes(raw)
        document = json.loads(manifest.read_bytes())
        document['response_sha256'] = sha256(raw).hexdigest()
        manifest.write_text(json.dumps(document))
        assert recover_observed(w.runs, run['id'])['calls'][0]['status'] == 'insufficient_evidence'
        assert ledger_state(w, run)[1] == before
        assert len(provider.requests) == 1
        return
    assert recover_observed(w.runs, run['id'])['calls'][0]['status'] == 'recovered'
    response = assert_observed(w, run, delivered=False, posts=provider.requests)
    w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
    _, control = initialized(w)
    provider.queue = [dict(body=payload, raw=json.dumps(payload).encode())]
    executor(w).transport.perform(control, {'purpose': 'agent'},
                                  request=dict(messages=[dict(role='user', content='Synthetic')]),
                                  audit=root / control['id'] / 'calls', runtime_context='Synthetic context')
    normal = assert_observed(w, control, delivered=True, posts=provider.requests[1:])
    assert normal['estimated_usd'] == response['estimated_usd']
    assert normal['tokens'] == response['tokens']


def test_installed_executor_delivers_after_accounting_lock(workspace, provider, tmp_path):
    w = workspace
    run = start_astra(w, tmp_path)
    result = tool_reply('aist_save_artifact', dict(content=dict(claims=[], missing_information=['Синтетический пример.'])), 'save')
    with project_blocker(w, seconds=3.45) as lock:
        def received():
            lock()
            return result

        provider.queue = [dict(body=received)]
        state = installed_run(w, run, Path('/result/a12-installed-lock'))
    assert state['state'] == 'completed', state
    assert_observed(w, run, delivered=True, posts=provider.requests)



@pytest.fixture
def web_endpoint(monkeypatch):
    endpoint = SimpleNamespace(requests=[], before_reply=lambda value: value)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            name = 'aist_web_search' if route.path == '/web-search' else 'aist_web_fetch'
            args = json.loads(query['arguments'][0]) if name == 'aist_web_search' else dict(url=query['url'][0])
            value = reader(endpoint.requests)(name, args, '2026-11-12', 1, 160000)
            value = endpoint.before_reply(value)
            data = json.dumps(value).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        endpoint.origin = f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        monkeypatch.setenv('AIST_NEWS_ORIGIN', endpoint.origin)
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join()


def test_web_lock_retries_only_local_commit(workspace, web_endpoint):
    w = workspace
    _, run = initialized(w)
    with project_blocker(w, seconds=3.45) as lock:
        def request(value):
            lock()
            return value

        web_endpoint.before_reply = request
        saved = w.runs.tool(run['id'], run['epoch'], 'search-1', 'aist_web_search', QUERY)
    assert saved['ok']
    replay = w.runs.tool(run['id'], run['epoch'], 'search-2', 'aist_web_search', QUERY)
    assert replay['result'] == saved['result']
    context = current(w, run)['external_context']
    assert len(web_endpoint.requests) == context['requests'] == 1
    assert context['bytes_read'] == 100


@pytest.mark.parametrize('invalid', ['date', 'url', 'format'])
def test_web_invalid_result_is_durable_and_does_not_publish_evidence(workspace, web_endpoint, invalid):
    w = workspace
    _, run = initialized(w)
    def reject_fetch(payload):
        if 'published' not in payload:
            return payload
        if invalid == 'date':
            return dict(payload, published='2099-01-01')
        if invalid == 'url':
            prefix = 'https://пример.рф/'
            # The provider can send a URL that becomes oversized after IDNA.
            return dict(payload, url=prefix + 'a' * (2048 - len(prefix)))
        return dict(payload, quote=[])

    web_endpoint.before_reply = reject_fetch
    search = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    args = dict(search_id=search['result']['search_id'], url=URL)
    failed = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', args)
    assert failed['error']['code'] == 'validation_error'
    assert w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', args) == failed
    assert w.runs.tool(run['id'], run['epoch'], 'fetch-alias', 'aist_web_fetch', args)['error']['code'] == 'validation_error'
    independent = w.runs.tool(run['id'], run['epoch'], 'independent', 'aist_web_search', dict(QUERY, objective='Новый вопрос'))
    assert independent['ok']
    context = current(w, run)['external_context']
    assert context['items'] == []
    assert len(web_endpoint.requests) == context['requests'] == 3
    assert context['bytes_read'] == 400


def test_web_recovery_waits_for_owner_then_fences_late_response(workspace, web_endpoint):
    w = workspace
    _, run = initialized(w)
    called, release = threading.Event(), threading.Event()
    def paused(value):
        called.set()
        assert release.wait(5)
        return value

    web_endpoint.before_reply = paused
    with ThreadPoolExecutor(max_workers=3) as threads:
        late = threads.submit(w.runs.tool, run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
        try:
            assert called.wait(5)
            assert recover_observed(w.runs, run['id'])['web'][0]['status'] == 'active'
            with w.engine.begin() as connection:
                execute(connection, 'UPDATE aist.runs SET epoch=epoch+1 WHERE id=:id', id=run['id'])
            reconciled = list(threads.map(lambda _: recover_observed(w.runs, run['id']), range(2)))
            assert sorted(len(value['web']) for value in reconciled) == [0, 1]
        finally:
            release.set()
        with pytest.raises(ApiError):
            late.result()
    epoch = run['epoch'] + 1
    exact = w.runs.tool(run['id'], epoch, 'search', 'aist_web_search', QUERY)
    assert exact['error']['code'] == 'outcome_unknown'
    with pytest.raises(ApiError) as conflict:
        w.runs.tool(run['id'], epoch, 'search', 'aist_web_search', dict(QUERY, objective='Другой вопрос'))
    assert conflict.value.code == 'idempotency_conflict'
    alias = w.runs.tool(run['id'], epoch, 'alias', 'aist_web_search', QUERY)
    assert alias['error']['code'] == 'outcome_unknown'
    context = current(w, run)['external_context']
    assert context['requests'] == len(web_endpoint.requests) == 1
    assert not context['items'] and context['bytes_read'] == 0
    assert w.runs.tool(run['id'], epoch, 'fresh', 'aist_web_search', dict(QUERY, objective='Другой вопрос'))['ok']
    context = current(w, run)['external_context']
    assert context['requests'] == len(web_endpoint.requests) == 2
    assert context['bytes_read'] == 100


def test_web_worker_death_leaves_refusal_and_no_second_network_call(workspace, web_endpoint, tmp_path):
    w = workspace
    _, run = initialized(w)
    called, release = threading.Event(), threading.Event()

    def block(value):
        called.set()
        assert release.wait(10)
        return value

    web_endpoint.before_reply = block
    driver = """
import json, os
from app.worker import Worker
worker = Worker()
worker.runs.tool(os.environ['RUN'], int(os.environ['EPOCH']), 'lost-web', 'aist_web_search',
                 json.loads(os.environ['ARGS']))
"""
    root = workspace_root(w, tmp_path / 'execution')
    env = worker_environment(w, root)
    sender = subprocess.Popen([sys.executable, '-c', driver],
                              env=dict(env, RUN=run['id'], EPOCH=str(run['epoch']), ARGS=json.dumps(QUERY)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert called.wait(6)
        sender.kill()
        sender.communicate(timeout=5)
    finally:
        release.set()
        if sender.poll() is None:
            sender.kill()
            sender.communicate(timeout=5)
    with w.engine.begin() as connection:
        execute(connection, 'UPDATE aist.runs SET epoch=epoch+1 WHERE id=:id', id=run['id'])
    recovered = subprocess.run([sys.executable, '-m', 'app.cli', 'recover-observed', '--run', run['id']],
                               env=env, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(recovered.stdout)['web'][0]['status'] == 'insufficient_evidence'
    refused = w.runs.tool(run['id'], run['epoch'] + 1, 'lost-web', 'aist_web_search', QUERY)
    assert refused['error']['code'] == 'outcome_unknown'
    assert len(web_endpoint.requests) == current(w, run)['external_context']['requests'] == 1
