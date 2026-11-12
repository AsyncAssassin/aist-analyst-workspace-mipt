"""Fault injection at the HTTP and worker boundaries, without a model endpoint."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import main, worker as worker_module
from app.auth import token_hash
from app.config import Settings
from app.contracts import validate_definition
from app.errors import ApiError
from app.service import Service, ident, row
from app.worker import Worker
from test_d15_postgres import start_run, upload, workspace


postgres = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
TOKEN = 'aist_' + 'T' * 43
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Cookie': 'session=marker-cookie',
           'X-Request-ID': 'marker-client-request-id'}
MARKERS = (TOKEN, 'marker-cookie', 'marker-client-request-id', 'marker-path', 'marker-query', 'marker-body',
           'marker-exception', 'marker-cause', 'marker-note', 'marker-statement', 'marker-param', 'marker-local')


def fail_database():
    secret_local = 'marker-local'
    try:
        raise ValueError('marker-cause', secret_local)
    except ValueError as cause:
        error = OperationalError('SELECT marker-statement', {'private': 'marker-param'},
                                 RuntimeError('marker-exception' * 20000))
        error.add_note('marker-note')
        raise error from cause


@pytest.fixture
def journal(caplog):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(message)s args=%(args)r exc_info=%(exc_info)r'))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream, caplog
    finally:
        root.removeHandler(handler)


def driver_command(code, *args):
    tests = str(Path(__file__).resolve().parent)
    return [sys.executable, '-c', f'import sys\nsys.path.insert(0, {tests!r})\n' + code, *args]


def assert_safe(value):
    assert all(marker not in value for marker in MARKERS)


def assert_api_error(response, code, status=503):
    assert response.status_code == status
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    body = validate_definition('ErrorResponse', response.json())
    assert body['error']['code'] == code
    assert_safe(response.text)
    return body['request_id']


@pytest.fixture
def authenticated(workspace):
    with workspace.engine.begin() as connection:
        connection.execute(text('UPDATE aist.sessions SET token_hash=:hash WHERE id=:id'),
                           {'hash': token_hash(TOKEN), 'id': workspace.session})
    return workspace


@postgres
def test_api_error_paths_and_concurrent_request_ids(authenticated, monkeypatch, journal):
    original_get = Service.get
    original_write = Service.write
    barrier = threading.Barrier(2)
    expected = ApiError('outcome_unknown')

    def get(service, actor, project_id):
        if project_id == 'marker-path':
            barrier.wait(timeout=4)
            fail_database()
        return original_get(service, actor, project_id)

    def write(service, actor, operation, key, payload, *args, **kwargs):
        if payload.get('name') == 'marker-body':
            barrier.wait(timeout=4)
            raise expected
        return original_write(service, actor, operation, key, payload, *args, **kwargs)

    monkeypatch.setattr(Service, 'get', get)
    monkeypatch.setattr(Service, 'write', write)
    with TestClient(main.create_app(authenticated.settings)) as client:
        assert_api_error(client.get('/v1/projects'), 'unauthenticated', 401)
        assert_api_error(client.get('/v1/projects/prj_missing', headers=HEADERS), 'resource_not_found', 404)
        assert client.get('/v1/projects', headers=HEADERS).status_code == 200
        headers = {**HEADERS, 'Idempotency-Key': ident('key')}
        assert client.post('/v1/projects', headers=headers, json={'name': 'First project'}).status_code == 201
        assert_api_error(client.post('/v1/projects', headers=headers, json={'name': 'Another project'}),
                         'idempotency_conflict', 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            get_response = pool.submit(client.get, '/v1/projects/marker-path?x=marker-query', headers=HEADERS)
            post_response = pool.submit(client.post, '/v1/projects', headers=headers, json={'name': 'marker-body'})
            unexpected_id = assert_api_error(get_response.result(timeout=8), 'temporarily_unavailable')
            expected_id = assert_api_error(post_response.result(timeout=8), 'outcome_unknown')
        assert unexpected_id != expected_id == expected.body['request_id']

    stream, caplog = journal
    lines = [record for record in caplog.records if record.getMessage().startswith('api_failure ')]
    assert len(lines) == 2
    unexpected = next(record for record in lines if unexpected_id in record.getMessage())
    deliberate = next(record for record in lines if expected_id in record.getMessage())
    assert 'stage=handler operation=/v1/projects/{project_id}' in unexpected.getMessage()
    assert 'status=503 code=temporarily_unavailable exception=OperationalError' in unexpected.getMessage()
    assert 'fail_database:' in unexpected.getMessage()
    assert 'stage=handler operation=/v1/projects' in deliberate.getMessage()
    assert 'status=503 code=outcome_unknown' in deliberate.getMessage()
    assert 'stack=' not in deliberate.getMessage()
    for record in lines:
        assert not record.args and record.exc_info is None and record.stack_info is None
        assert_safe(repr(vars(record)))
    assert_safe(stream.getvalue())
    assert len(stream.getvalue()) < 4500


@pytest.mark.parametrize('expected', [False, True])
def test_authentication_failure_is_logged_before_routing(monkeypatch, journal, expected):
    api_error = ApiError('temporarily_unavailable')

    def authenticate(*args):
        if expected:
            raise api_error
        fail_database()

    monkeypatch.setattr(main, 'active_session', authenticate)
    with TestClient(main.create_app()) as client:
        response = client.get('/v1/projects/marker-path?marker-query=1', headers=HEADERS)
    request_id = assert_api_error(response, 'temporarily_unavailable')
    stream, _ = journal
    output = stream.getvalue()
    assert output.count('api_failure ') == 1
    assert f'stage=auth operation=active_session request_id={request_id}' in output
    if expected:
        assert request_id == api_error.body['request_id']
        assert 'stack=' not in output
    else:
        assert 'exception=OperationalError' in output and 'authenticate:' in output
    assert_safe(output)


@pytest.mark.parametrize('stream_error', [False, True])
def test_failure_in_logging_handler_preserves_api_response(monkeypatch, capsys, stream_error):
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError('synthetic log sink failure')

    class BrokenStream(io.StringIO):
        def write(self, value):
            raise OSError('synthetic log sink failure')

    handler = logging.StreamHandler(BrokenStream()) if stream_error else BrokenHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    monkeypatch.setattr(logging, 'raiseExceptions', True)
    monkeypatch.setattr(main, 'active_session', lambda *args: fail_database())
    try:
        with TestClient(main.create_app()) as client:
            assert_api_error(client.get('/v1/projects', headers=HEADERS), 'temporarily_unavailable')
    finally:
        root.removeHandler(handler)
    stderr = capsys.readouterr().err
    if stream_error:
        assert 'Logging error' in stderr
    assert_safe(stderr)


@postgres
def test_uvicorn_tcp_uses_standard_process_logging(authenticated, tmp_path):
    driver = """
import socket
import sys

import uvicorn

from app.main import create_app
from app.service import Service
from test_failure_diagnostics import fail_database

def get(*args):
    fail_database()

Service.get = get
with socket.socket(fileno=int(sys.argv[1])) as listener:
    config = uvicorn.Config(create_app(), access_log=False, timeout_graceful_shutdown=5)
    uvicorn.Server(config).run(sockets=[listener])
"""
    output = tmp_path / 'uvicorn.log'
    with socket.socket() as listener, output.open('w') as log:
        listener.bind(('127.0.0.1', 0))
        host, port = listener.getsockname()
        fd = listener.fileno()
        process = subprocess.Popen(driver_command(driver, str(fd)), pass_fds=(fd,),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://{host}:{port}', trust_env=False, timeout=4) as client:
                deadline = time.monotonic() + 12
                while True:
                    try:
                        assert client.get('/healthz').status_code == 200
                        break
                    except httpx.ConnectError:
                        assert process.poll() is None and time.monotonic() < deadline
                        time.sleep(.05)
                response = client.get('/v1/projects/marker-path?x=marker-query', headers=HEADERS)
                request_id = assert_api_error(response, 'temporarily_unavailable')
        finally:
            process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise
    journal = output.read_text()
    assert journal.count('api_failure ') == 1
    assert f'request_id={request_id} status=503 code=temporarily_unavailable exception=OperationalError' in journal
    assert 'operation=/v1/projects/{project_id}' in journal and 'fail_database:' in journal
    assert_safe(journal)
    print(journal, end='')


@postgres
@pytest.mark.parametrize('secondary', [None, 'stop', 'recover', 'artifact'])
def test_worker_process_logs_saved_run(workspace, tmp_path, secondary):
    run = start_run(workspace, upload(workspace)['version'])
    driver = """
import sys
from dataclasses import replace
from app.config import Settings
from app.errors import ApiError
from app.service import row
from app.worker import Worker
from app import note_external
from test_failure_diagnostics import fail_database

run_id, policy_path, root, secondary = sys.argv[1:]
worker = Worker(replace(Settings(), executor_mode='openclaw', live_models_enabled=True,
                        policy_path=policy_path, openclaw_root=root))
try:
    with worker.engine.connect() as connection:
        run = row(connection, 'SELECT * FROM aist.runs WHERE id=:id', id=run_id)
    worker.id = run['worker_id']

    def prepare(*args):
        if secondary in ('recover', 'artifact'):
            raise ApiError('outcome_unknown')
        fail_database()

    note_external.prepare_all = prepare
    if secondary == 'stop':
        worker.runs.stop = lambda *args: fail_database()
    if secondary == 'recover':
        worker.runs.recover = lambda **kwargs: fail_database()
    worker.job(run)
finally:
    worker.engine.dispose()
"""
    root = tmp_path / 'execution'
    if secondary == 'artifact':
        root.touch()
    completed = subprocess.run(driver_command(driver, run['id'], workspace.settings.policy_path,
                                              str(root), secondary or ''),
                               capture_output=True, text=True, timeout=15)
    journal = completed.stdout + completed.stderr
    assert completed.returncode == 0, journal
    assert journal.count('stage=run_prepare ') == 1
    assert f"worker_id={run['worker_id']} run_id={run['id']} epoch={run['epoch']}" in journal
    if secondary:
        assert f'stage=run_{secondary} ' in journal
    if secondary in (None, 'stop'):
        assert 'exception=OperationalError' in journal and 'fail_database:' in journal
    else:
        assert 'code=outcome_unknown' in journal
    assert_safe(journal)
    with workspace.engine.connect() as connection:
        saved = row(connection, 'SELECT state, epoch FROM aist.runs WHERE id=:id', id=run['id'])
        model_posts = connection.execute(text('SELECT count(*) FROM aist.model_calls')).scalar_one()
    assert saved == {'state': 'failed' if secondary is None else 'running', 'epoch': run['epoch']}
    assert model_posts == 0
    print(journal, end='')


@postgres
@pytest.mark.parametrize('recovery_fails', [False, True])
def test_worker_loop_and_shutdown_keep_order_and_context(workspace, monkeypatch, journal, recovery_fails):
    worker = Worker(replace(workspace.settings, executor_mode='qa-scripted', live_models_enabled=False,
                            qa_url='http://127.0.0.1:1', shutdown_seconds=.25))
    original_recover = worker.runs.recover
    original_snapshot = worker.snapshot
    original_execute = worker_module.execute
    run = start_run(workspace, upload(workspace)['version'])
    calls = []
    job_stop = threading.Event()
    job = threading.Thread(target=job_stop.wait, args=(8,))
    watchdog = threading.Timer(6, worker.signal)
    worker.jobs[run['id']] = (job, run)
    recover_count = 0
    heartbeat_count = 0
    snapshots = []

    def recover(owned=None):
        nonlocal recover_count
        recover_count += 1
        if owned:
            calls.append('shutdown_recover')
            if recovery_fails:
                fail_database()
            return original_recover(owned)
        calls.append('recover')
        if recover_count == 3:
            fail_database()
        return original_recover()

    def heartbeat(*args):
        nonlocal heartbeat_count
        heartbeat_count += 1
        calls.append('heartbeat')
        if heartbeat_count in (1, 4):
            fail_database()

    def execute(connection, statement, **kwargs):
        if 'cancel_dispatched_at IS NULL' in statement:
            calls.append('cancel_lookup')
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [run['id']]))
        return original_execute(connection, statement, **kwargs)

    def cancel(*args):
        calls.append('cancel')
        if calls.count('cancel') == 1:
            fail_database()

    def claim(*args):
        calls.append('claim')
        if calls.count('claim') == 3:
            worker.signal()
        fail_database()

    def snapshot(saved, reason=None):
        calls.append('snapshot')
        snapshots.append(saved)
        if len(snapshots) == 1:
            fail_database()
        job_stop.set()
        if recovery_fails:
            fail_database()
        return original_snapshot(saved, reason)

    monkeypatch.setattr(worker.runs, 'recover', recover)
    monkeypatch.setattr(worker.runs, 'heartbeat', heartbeat)
    monkeypatch.setattr(worker.runs, 'claim', claim)
    monkeypatch.setattr(worker_module, 'execute', execute)
    monkeypatch.setattr(worker_module, 'dispatch_cancel', cancel)
    monkeypatch.setattr(worker.exports, 'process_one', fail_database)
    monkeypatch.setattr(worker, 'snapshot', snapshot)
    signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    job.start()
    watchdog.start()
    try:
        assert worker.run() == 2
        assert time.monotonic() < worker.final_deadline
    finally:
        watchdog.cancel()
        watchdog.join(timeout=2)
        for sig, handler in signals.items():
            signal.signal(sig, handler)
        job_stop.set()
        job.join(timeout=2)
        if worker.export_thread:
            worker.export_thread.join(timeout=2)
            assert not worker.export_thread.is_alive()
        worker.engine.dispose()
    assert not job.is_alive() and not watchdog.is_alive()
    assert calls[:16] == [
        'recover',
        'heartbeat', 'recover', 'cancel_lookup', 'cancel', 'claim',
        'heartbeat', 'recover', 'cancel_lookup', 'cancel', 'claim',
        'heartbeat', 'recover', 'cancel_lookup', 'cancel', 'claim',
    ]
    assert calls[-3:] == ['shutdown_recover', 'snapshot', 'snapshot']
    assert snapshots == [not recovery_fails, False]
    stream, _ = journal
    output = stream.getvalue()
    stages = ('heartbeat', 'recover', 'cancel', 'claim', 'export', 'shutdown_heartbeat', 'shutdown_snapshot')
    if recovery_fails:
        stages += ('shutdown_recover', 'shutdown_snapshot_fallback')
    for stage in stages:
        records = [line for line in output.splitlines() if f'stage={stage} ' in line]
        assert records
        for record in records:
            assert f'worker_id={worker.id}' in record and 'exception=OperationalError' in record
            if stage in ('heartbeat', 'cancel', 'shutdown_heartbeat'):
                assert f"run_id={run['id']}" in record
            else:
                assert 'run_id=' not in record
    assert_safe(output)
