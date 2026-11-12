"""PDF uploads through the real API, connection pool, receipts and file store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
import secrets
import socket
from threading import Event, Lock, Thread
import time

import httpx
import pytest
from sqlalchemy import text
import uvicorn

from app.auth import token_hash
from app.errors import ApiError
from app.main import create_app
from app.service import Service, ident
from pdf_fixtures import pdf_bytes
from test_d15_postgres import workspace

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
PDF = pdf_bytes('First synthetic version', pages=1)
METADATA = dict(title='Synthetic PDF', expected_revision=0)
KEY = 'pdf-upload-regression'


@pytest.fixture
def api(workspace):
    w = workspace
    token = 'aist_' + secrets.token_urlsafe(32)
    with w.engine.begin() as c:
        c.execute(text('UPDATE aist.sessions SET token_hash=:hash WHERE id=:id'),
                  dict(hash=token_hash(token), id=w.session))
    app = create_app(replace(w.settings, executor_mode='disabled', live_models_enabled=False, embeddings_enabled=False))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', timeout_graceful_shutdown=5))
        thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + token}, timeout=20) as client:
                yield client, app.state.engine
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def upload_path(project, source=None):
    path = f'/v1/projects/{project}/sources'
    return f'{path}/{source}/versions' if source else path


def upload_http(client, project, key=KEY, data=PDF, metadata=METADATA, source=None, filename='original.pdf'):
    return client.post(upload_path(project, source), headers={'Idempotency-Key': key},
                       data={'metadata': json.dumps(metadata)}, files={'file': (filename, data, 'application/pdf')})


def upload(w, key=KEY, data=PDF, metadata=METADATA, source=None, project=None, actor=None, service=None):
    return (service or w.service).write(actor or w.actor, 'uploadSourceVersion' if source else 'uploadSource',
                                        key, metadata, project or w.project, source, data, 'original.pdf')


def other_project(w):
    return w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Other project'})[1]['id']


@contextmanager
def blocked_parser(monkeypatch, expected=2):
    entered, release = Event(), Event()
    from app.service import process_pdf
    calls = []
    lock = Lock()

    def parse(data):
        with lock:
            calls.append(data)
            if len(calls) == expected:
                entered.set()
        assert release.wait(25), 'Parser barrier timed out'
        return process_pdf(data)

    monkeypatch.setattr('app.service.process_pdf', parse)
    try:
        yield entered, release, calls
    finally:
        release.set()


def counts(w):
    with w.engine.connect() as c:
        return tuple(c.execute(text("""
            SELECT (SELECT count(*) FROM aist.receipts WHERE operation LIKE 'uploadSource%'),
                   (SELECT count(*) FROM aist.sources),
                   (SELECT count(*) FROM aist.source_versions),
                   (SELECT count(*) FROM aist.source_pages)""")).one())


@pytest.mark.parametrize('same_project', [False, True], ids=['different-projects', 'same-project'])
def test_delayed_uploads_leave_database_available(workspace, api, monkeypatch, same_project):
    w = workspace
    client, engine = api
    other = other_project(w)
    assert (engine.pool.size(), engine.pool._max_overflow, engine.pool.timeout()) == (2, 0, 3)
    with ThreadPoolExecutor(max_workers=2) as requests, blocked_parser(monkeypatch) as (entered, release, calls):
        uploads = [requests.submit(upload_http, client, project, f'{KEY}-{i}')
                   for i, project in enumerate((w.project, w.project if same_project else other))]
        assert entered.wait(10), f'Only {len(calls)} uploads reached preparation'
        checked_out = engine.pool.checkedout()
        with w.engine.connect() as c:
            open_transactions = c.execute(text("""
                SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid() AND xact_start IS NOT NULL
            """)).scalar_one()
        ready = client.get('/readyz')
        reading = client.get(f'/v1/projects/{other}')
        writing = client.post(f'/v1/projects/{w.project}/conversations',
                              headers={'Idempotency-Key': 'during-preparation'}, json={'title': 'Short write'})
        assert not release.is_set()
        assert (ready.status_code, reading.status_code, writing.status_code) == (200, 200, 201)
        assert (checked_out, open_transactions) == (0, 0)
        release.set()
        assert [result.result().status_code for result in uploads] == [201, 201]
    assert counts(w) == (2, 2, 2, 2)


def test_saturated_preparation_rejects_excess_but_allows_replay_and_other_requests(workspace, api, monkeypatch):
    w = workspace
    client, engine = api
    saved = upload_http(client, w.project)
    assert saved.status_code == 201, saved.text
    with ThreadPoolExecutor(max_workers=8) as requests, blocked_parser(monkeypatch) as (entered, release, calls):
        active = [requests.submit(upload_http, client, w.project, f'active-{i}') for i in range(2)]
        assert entered.wait(10)
        replay = upload_http(client, w.project, filename='renamed.pdf')
        assert replay.status_code == 200 and replay.json() == saved.json()
        excess = [requests.submit(upload_http, client, w.project, f'excess-{i}') for i in range(6)]
        for future in excess:
            response = future.result(timeout=4)
            assert response.status_code == 503 and response.json()['error']['code'] == 'temporarily_unavailable'
        another_service = Service(w.engine, w.service.storage)
        with pytest.raises(ApiError, match='temporarily_unavailable'):
            upload(w, 'direct-excess', service=another_service)
        assert client.get('/readyz').status_code == 200
        assert client.get('/v1/projects').status_code == 200
        assert len(calls) == 2 and engine.pool.checkedout() == 0
        release.set()
        assert all(f.result().status_code == 201 for f in active)
    assert upload_http(client, w.project, key='excess-0').status_code == 201
    assert counts(w) == (4, 4, 4, 4)


def test_concurrent_replay_discards_only_its_own_candidate(workspace, api, monkeypatch):
    w = workspace
    client, _ = api
    with ThreadPoolExecutor(max_workers=2) as requests, blocked_parser(monkeypatch) as (entered, release, calls):
        pending = [requests.submit(upload_http, client, w.project) for _ in range(2)]
        assert entered.wait(10)
        release.set()
        first, second = [future.result() for future in pending]
    assert sorted((first.status_code, second.status_code)) == [200, 201], (first.text, second.text)
    assert first.json() == second.json()
    version = first.json()['version']
    assert len(calls) == 2 and counts(w) == (1, 1, 1, 1)
    assert [p.name for p in w.service.storage.root.iterdir()] == [version['id'] + '.pdf']
    assert w.service.page_or_file(w.actor, w.project, version['id'])[0] == PDF
    assert upload_http(client, w.project).json() == first.json()
    assert len(calls) == 2
    for options in ({'data': pdf_bytes('Different bytes', pages=1)}, {'metadata': dict(METADATA, title='New title')}):
        response = upload_http(client, w.project, **options)
        assert response.status_code == 409 and response.json()['error']['code'] == 'idempotency_conflict'
    assert len(calls) == 2 and counts(w) == (1, 1, 1, 1)


def test_key_scope_and_version_history_with_real_pdf_worker(workspace, api):
    from app.search import register

    w = workspace
    client, _ = api
    first = upload_http(client, w.project)
    assert first.status_code == 201, first.text
    original = first.json()
    other = other_project(w)
    assert upload_http(client, other).status_code == 201
    second_source = upload_http(client, w.project, key='other-source')
    assert second_source.status_code == 201
    initial_version = original['version']['id']
    register(w.service, w.actor, w.project, initial_version, 'samples', None, None, 'Synthetic formatting sample')
    replacement = pdf_bytes('Second synthetic version', pages=2)
    metadata = dict(METADATA, expected_revision=1)
    versions = []
    for source in (original['source']['id'], second_source.json()['source']['id']):
        response = upload_http(client, w.project, data=replacement, metadata=metadata, source=source)
        assert response.status_code == 201, response.text
        assert response.json()['source']['revision'] == 2
        versions.append(response.json()['version'])
        replay = upload_http(client, w.project, data=replacement, metadata=metadata, source=source)
        assert replay.status_code == 200 and replay.json() == response.json()

    assert [version['corpus'] for version in versions] == ['samples', 'factual']
    replay = upload_http(client, w.project)
    assert replay.status_code == 200
    assert replay.json()['source']['latest_version_id'] == versions[0]['id']
    assert replay.json()['version']['id'] == initial_version
    for version_id, data, expected_text, page_count in (
            (initial_version, PDF, 'First synthetic version', 1),
            (versions[0]['id'], replacement, 'Second synthetic version', 2)):
        path = f'/v1/projects/{w.project}/source-versions/{version_id}'
        metadata = client.get(path).json()
        digest = sha256(data).hexdigest()
        assert metadata['sha256'] == digest and metadata['page_count'] == page_count
        assert metadata['byte_size'] == len(data) and metadata['processing_state'] == 'ready'
        assert metadata['original_filename'] == 'original.pdf'
        page = client.get(path + '/page', params={'page': 1}).json()
        assert expected_text in page['text']
        assert page['source'] == dict(source_id=original['source']['id'], source_version_id=version_id, sha256=digest)
        response = client.get(path + '/file')
        assert response.status_code == 200 and response.content == data
        assert response.headers['etag'] == f'"sha256:{digest}"'
    assert counts(w) == (5, 3, 5, 7)


def test_version_cas_rechecked_after_preparation(workspace, api, monkeypatch):
    w = workspace
    client, _ = api
    _, initial = upload(w)
    source = initial['source']['id']
    metadata = dict(METADATA, expected_revision=1)
    variants = [pdf_bytes(f'Candidate {i}', pages=1) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as requests, blocked_parser(monkeypatch) as (entered, release, calls):
        pending = [requests.submit(upload_http, client, w.project, f'upload-cas-{i}', data, metadata, source)
                   for i, data in enumerate(variants)]
        assert entered.wait(10)
        release.set()
        results = [f.result() for f in pending]
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    winner, loser = (0, 1) if results[0].status_code == 201 else (1, 0)
    error = results[loser].json()['error']
    assert (error['code'], error['expected_revision'], error['current_revision']) == ('revision_conflict', 1, 2)
    new = results[winner].json()
    assert counts(w) == (2, 1, 2, 2)
    assert w.service.get(w.actor, w.project, 'sources', source) == new['source']
    expected_files = {initial['version']['id'] + '.pdf': PDF, new['version']['id'] + '.pdf': variants[winner]}
    assert {p.name: p.read_bytes() for p in w.service.storage.root.iterdir()} == expected_files
    assert len(calls) == 2


def test_scope_checks_before_and_after_preparation(workspace, monkeypatch):
    w = workspace
    _, initial = upload(w)
    source = initial['source']['id']
    other = other_project(w)
    foreign, changing_project = ident('usr'), ident('prj')
    with w.engine.begin() as c:
        c.execute(text("INSERT INTO aist.users(id,label) VALUES (:id,'Other owner')"), dict(id=foreign))
        c.execute(text("INSERT INTO aist.projects(id,owner_id,name) VALUES (:id,:actor,'Transfer during parse')"),
                  dict(id=changing_project, actor=w.actor))
    with ThreadPoolExecutor(max_workers=1) as requests, blocked_parser(monkeypatch, 1) as (entered, release, calls):
        for options in (dict(actor=foreign), dict(source=source, project=other), dict(source=ident('src'))):
            with pytest.raises(ApiError, match='resource_not_found'):
                upload(w, key='wrong-scope', **options)
        assert not calls
        pending = requests.submit(upload, w, project=changing_project)
        assert entered.wait(10)
        with w.engine.begin() as c:
            c.execute(text('UPDATE aist.projects SET owner_id=:actor WHERE id=:id'),
                      dict(actor=foreign, id=changing_project))
        release.set()
        with pytest.raises(ApiError, match='resource_not_found'):
            pending.result()
    assert counts(w) == (1, 1, 1, 1)
    assert [p.name for p in w.service.storage.root.iterdir()] == [initial['version']['id'] + '.pdf']


def test_disconnected_http_client_keeps_slot_until_parser_finishes(workspace, api, monkeypatch):
    w = workspace
    client, _ = api
    url = client.base_url
    request = client.build_request('POST', upload_path(w.project), headers={'Idempotency-Key': KEY},
        data={'metadata': json.dumps(METADATA)}, files={'file': ('original.pdf', PDF, 'application/pdf')})
    headers = b''.join(name + b': ' + value + b'\r\n' for name, value in request.headers.raw)
    wire = b'POST ' + request.url.raw_path + b' HTTP/1.1\r\n' + headers + b'\r\n' + request.read()
    with ThreadPoolExecutor(max_workers=1) as requests, blocked_parser(monkeypatch) as (entered, release, calls):
        with socket.create_connection((url.host, url.port), timeout=5) as connection:
            connection.sendall(wire)
            active = requests.submit(upload_http, client, w.project, 'connected')
            assert entered.wait(10)
        response = upload_http(client, w.project, key='after-disconnect')
        assert response.status_code == 503 and response.json()['error']['code'] == 'temporarily_unavailable'
        assert len(calls) == 2
        release.set()
        assert active.result().status_code == 201
        deadline = time.monotonic() + 10
        while counts(w) != (2, 2, 2, 2) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert counts(w) == (2, 2, 2, 2)
        replay = upload_http(client, w.project)
        assert replay.status_code == 200, replay.text
        assert len(calls) == 2
        version = replay.json()['version']
        assert w.service.page_or_file(w.actor, w.project, version['id'])[0] == PDF
    assert upload_http(client, w.project, key='after-disconnect').status_code == 201


@pytest.mark.parametrize('failure,code', [
    ('invalid', 'unsupported_source'), ('pages', 'too_large'), ('bytes', 'too_large'),
    ('timeout', 'too_large'), ('parser-error', 'temporarily_unavailable'),
])
def test_parser_failures_return_connection_and_preparation_slot(workspace, monkeypatch, failure, code):
    import subprocess

    from app.storage import MAX_BYTES
    from app.pdf_worker import MAX_PAGES

    w = workspace
    assert w.engine.pool.checkedout() == 0
    payload = {
        'invalid': lambda: b'Not a PDF',
        'pages': lambda: pdf_bytes(pages=MAX_PAGES + 1),
        'bytes': lambda: b'x' * (MAX_BYTES + 1),
    }
    with monkeypatch.context() as patch:
        if failure in payload:
            data = payload[failure]()
        else:
            def parser_failure(args, **kwargs):
                assert args[-1] == 'app.pdf_worker' and w.engine.pool.checkedout() == 0
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(args, kwargs['seconds'])
                raise OSError('Synthetic parser failure')

            patch.setattr('app.storage.document_command', parser_failure)
            data = PDF
        for _ in range(2):
            with pytest.raises(ApiError, match=code):
                upload(w, data=data)
    assert counts(w) == (0, 0, 0, 0)
    assert w.engine.pool.checkedout() == 0 and not list(w.service.storage.root.iterdir())
    assert upload(w)[0] == 201


@pytest.mark.parametrize('failure', ['partial-write', 'file-fsync', 'link', 'directory-fsync'])
def test_failed_publication_runs_without_database_and_cleans_owned_files(workspace, monkeypatch, failure):
    import stat

    w = workspace
    store = w.service.storage
    foreign = store.root / 'foreign.pdf'
    foreign.write_bytes(b'Foreign content')
    fdopen, link, fsync = os.fdopen, os.link, os.fsync

    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            assert w.engine.pool.checkedout() == 0
            self.stream.write(data[:3])
            self.stream.flush()
            raise OSError('Synthetic partial-write')

    def open_file(fd, mode, *args, **kwargs):
        stream = fdopen(fd, mode, *args, **kwargs)
        return PartialWrite(stream) if failure == 'partial-write' and mode == 'wb' else stream

    def sync_file(fd):
        assert w.engine.pool.checkedout() == 0
        if failure == ('directory-fsync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-fsync'):
            raise OSError(f'Synthetic {failure}')
        fsync(fd)

    def link_file(*args, **kwargs):
        assert w.engine.pool.checkedout() == 0
        if failure == 'link':
            raise OSError('Synthetic link')
        return link(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr('app.storage.os.fdopen', open_file)
        patch.setattr('app.storage.os.fsync', sync_file)
        patch.setattr('app.storage.os.link', link_file)
        for _ in range(2):
            with pytest.raises(OSError, match=f'Synthetic {failure}'):
                upload(w)
    assert list(store.root.iterdir()) == [foreign] and foreign.read_bytes() == b'Foreign content'
    assert counts(w) == (0, 0, 0, 0) and w.engine.pool.checkedout() == 0
    assert upload(w)[0] == 201


@pytest.mark.parametrize('replacement', ['different-inode', 'symlink', 'same-inode'])
def test_final_transaction_rejects_replaced_candidate(workspace, monkeypatch, replacement):
    w = workspace
    store = w.service.storage
    foreign = store.root / 'foreign.pdf'
    foreign.write_bytes(b'Foreign content')
    prepare = w.service.prepare_upload
    replaced = []

    def replace_candidate(*args):
        prepared = prepare(*args)
        path = prepared[2][0]
        replaced.append(path)
        if replacement == 'different-inode':
            foreign.replace(path)
        elif replacement == 'symlink':
            path.unlink()
            path.symlink_to(foreign)
        else:
            # Same inode and size. Integrity must still fail.
            path.write_bytes(b'X' * len(PDF))
        return prepared

    with monkeypatch.context() as patch:
        patch.setattr(w.service, 'prepare_upload', replace_candidate)
        with pytest.raises(OSError, match='Prepared PDF changed'):
            upload(w)
    path, = replaced
    if replacement == 'symlink':
        assert path.is_symlink() and path.readlink() == foreign and foreign.read_bytes() == b'Foreign content'
    else:
        assert path.read_bytes() == (b'Foreign content' if replacement == 'different-inode' else b'X' * len(PDF))
    assert counts(w) == (0, 0, 0, 0) and w.engine.pool.checkedout() == 0
    _, result = upload(w)
    assert result['version']['id'] + '.pdf' != path.name and path.exists()


@pytest.mark.parametrize('failure', ['checkout', 'begin', 'before-commit'])
def test_failure_before_commit_discards_candidate_and_returns_connection(workspace, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    w = workspace
    prepare, write_receipt = w.service.prepare_upload, w.service.write_receipt

    def fail(*args):
        raise OSError(f'Synthetic {failure}')

    def prepare_then_fail(*args):
        prepared = prepare(*args)
        if failure == 'checkout':
            patch.setattr(w.engine, 'connect', fail)
        elif failure == 'begin':
            patch.setattr(Connection, 'begin', fail)
        return prepared

    def write_then_fail(*args):
        write_receipt(*args)
        fail()

    with monkeypatch.context() as patch:
        patch.setattr(w.service, 'prepare_upload', prepare_then_fail)
        if failure == 'before-commit':
            patch.setattr(w.service, 'write_receipt', write_then_fail)
        with pytest.raises(OSError, match=f'Synthetic {failure}'):
            upload(w)
    assert w.engine.pool.checkedout() == 0 and counts(w) == (0, 0, 0, 0)
    assert not list(w.service.storage.root.iterdir())
    assert upload(w)[0] == 201


@pytest.mark.parametrize('committed,recovery_available', [
    (False, True), (True, True), (True, False), (False, False),
], ids=['rollback-confirmed', 'commit-confirmed', 'commit-unknown', 'rollback-unknown'])
def test_commit_recovery_uses_receipt_and_preserves_unresolved_file(
        workspace, monkeypatch, committed, recovery_available):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from app.service import process_pdf

    w = workspace
    commit = w.engine.dialect.do_commit
    connect = w.engine.connect
    parses, pids = [], []
    attempted = False

    def parse(data):
        parses.append(data)
        return process_pdf(data)

    def lose_confirmation(connection):
        nonlocal attempted
        attempted = True
        if committed:
            commit(connection)
        raise OSError('Synthetic commit connection loss')

    def check_recovery_connection():
        if attempted and not recovery_available:
            raise OperationalError(None, None, OSError('Synthetic database outage'))
        return connect()

    def checkout(connection, *_):
        pids.append(connection.get_backend_pid())

    monkeypatch.setattr('app.service.process_pdf', parse)
    event.listen(w.engine, 'checkout', checkout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(w.engine.dialect, 'do_commit', lose_confirmation)
            patch.setattr(w.engine, 'connect', check_recovery_connection)
            if committed and recovery_available:
                status, original = upload(w)
                assert status == 200
            else:
                code = 'temporarily_unavailable' if recovery_available else 'outcome_unknown'
                with pytest.raises(ApiError, match=code):
                    upload(w)
        if recovery_available:
            assert len(pids) >= 3 and pids[1] != pids[2]
    finally:
        event.remove(w.engine, 'checkout', checkout)

    assert parses == [PDF] and w.engine.pool.checkedout() == 0
    candidates = list(w.service.storage.root.iterdir())
    assert len(candidates) == int(committed or not recovery_available)
    assert counts(w) == ((1, 1, 1, 1) if committed else (0, 0, 0, 0))
    status, replay = upload(w)
    assert status == (200 if committed else 201)
    assert len(parses) == (1 if committed else 2)
    assert counts(w) == (1, 1, 1, 1)
    version = replay['version']['id']
    assert w.service.page_or_file(w.actor, w.project, version)[0] == PDF
    if committed:
        assert candidates[0].name == version + '.pdf'
        if recovery_available:
            assert replay == original
    elif not recovery_available:
        assert candidates[0].name != version + '.pdf' and candidates[0].read_bytes() == PDF
    assert len(list(w.service.storage.root.iterdir())) == (2 if not committed and not recovery_available else 1)


def test_replay_checks_existing_file_without_reparsing_or_holding_database(workspace, monkeypatch):
    w = workspace
    _, saved = upload(w)
    version_id = saved['version']['id']
    store = w.service.storage
    path = store.path(version_id + '.pdf')
    path.write_bytes(b'Corrupt upload')
    read = store.read

    def check_file(version):
        assert w.engine.pool.checkedout() == 0
        return read(version)

    def no_parser(data):
        pytest.fail('Replay must not parse the PDF')

    monkeypatch.setattr(store, 'read', check_file)
    monkeypatch.setattr('app.service.process_pdf', no_parser)
    status, replay = upload(w)
    assert status == 200 and replay['version']['id'] == version_id
    assert replay['version']['processing_state'] == 'failed'
    assert replay['version']['error']['code'] == 'export_corrupt'
    with w.engine.connect() as c:
        assert w.service.scoped(c, w.actor, w.project, 'source_versions', version_id)['processing_state'] == 'failed'
    assert counts(w) == (1, 1, 1, 1) and path.read_bytes() == b'Corrupt upload'
