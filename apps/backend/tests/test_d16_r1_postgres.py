"""D16-R1: real PostgreSQL pool, synthetic ISS, embedding and Qdrant transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError

from app import moex, search
from app.errors import ApiError
from app.service import Service, ident
from app.storage import Storage
from test_d15_postgres import workspace, provider, qdrant
from test_d16 import snapshot
from test_d16_postgres import iss, perform

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def test_slow_embedding_leaves_reads_and_replays_available(workspace, iss, provider, qdrant):
    w = workspace
    entered, release = threading.Event(), threading.Event()

    def waiting():
        entered.set()
        assert release.wait(8)

    provider.waiting_response = waiting
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(perform, w, settings=w.settings)
        assert entered.wait(5)
        try:
            w.service.check_scope(w.actor, w.project)
            state = moex.status(w.service, w.actor, w.project)
            assert state['version'] and state['last_error']
            raw, _ = w.service.page_or_file(w.actor, w.project, state['version']['id'])
            assert json.loads(raw)['normalized']['price']['value'] == '17.25'
            pending = perform(w, settings=w.settings)
            assert pending['version_id'] == state['version']['id'] and pending['search_error']
            assert perform(w, 'cached-during-index', settings=w.settings)['search_error']
            with pytest.raises(ApiError) as error:
                perform(w, mode='refresh', settings=w.settings)
            assert error.value.code == 'idempotency_conflict'
            assert len(iss) == len(provider.sent) == 1
        finally:
            release.set()
        result = future.result()
    assert result['search_error'] is None
    assert perform(w, settings=w.settings) == result
    assert moex.status(w.service, w.actor, w.project)['last_error'] is None
    assert len(iss) == len(provider.sent) == 1


def test_parallel_projects_index_and_replay_without_duplicate_calls(workspace, provider, qdrant, monkeypatch):
    w = workspace
    _, other = w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Parallel MOEX project'})
    projects = [w.project, other['id']]
    fetched, indexing = threading.Barrier(2), threading.Barrier(2)
    requests = []

    def fetch(authorize):
        authorize()
        requests.append('synthetic ISS')
        fetched.wait(5)
        return snapshot()

    original_index = search.index_material

    def index(*args, **kwargs):
        indexing.wait(5)
        return original_index(*args, **kwargs)

    monkeypatch.setattr(moex, 'fetch_snapshot', fetch)
    monkeypatch.setattr(search, 'index_material', index)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(moex.update, w.service, w.actor, project, 'd16-r1-parallel', 'get', settings=w.settings)
                   for project in projects]
        results = [future.result() for future in futures]
    assert all(result['version_id'] and result['error'] is None and result['search_error'] is None for result in results)
    assert results[0]['version_id'] != results[1]['version_id']
    assert len(requests) == 2 and len(provider.sent) == 1
    with w.engine.begin() as connection:
        for result in results:
            chunks = connection.execute(text('SELECT indexed FROM aist.knowledge_chunks WHERE version_id=:v'),
                                        dict(v=result['version_id'])).scalars().all()
            assert len(chunks) == 2 and all(chunks)
    for project, result in zip(projects, results):
        assert moex.update(w.service, w.actor, project, 'd16-r1-parallel', 'get', settings=w.settings) == result
        found = search.ui_search(w.service, w.settings, w.actor, project, dict(query='Whoosh',
            source_version_ids=[result['version_id']], limit=5, method='semantic.qwen3.v1'))
        assert len(found['items']) == 2
        assert all(item['source']['source_version_id'] == result['version_id'] for item in found['items'])
    assert len(requests) == 2 and len(provider.sent) == 2  # Shared document cache and one query cache.


@pytest.mark.parametrize('failure', ['embedding', 'qdrant', 'database'])
def test_index_error_is_saved_and_replayed_without_retry(workspace, iss, provider, qdrant, monkeypatch, failure):
    w = workspace
    original_index = search.index_material
    attempts = []

    def index(*args, **kwargs):
        attempts.append(failure)
        if failure == 'database':
            raise TimeoutError('synthetic driver detail must not reach the receipt')
        return original_index(*args, **kwargs)

    monkeypatch.setattr(search, 'index_material', index)
    if failure == 'embedding':
        provider.status = 503
    elif failure == 'qdrant':
        original_request = search.Qdrant.request

        def request(client, method, path, *args, **kwargs):
            if method == 'PUT' and '/points?' in path:
                raise ApiError('search_index_unavailable')
            return original_request(client, method, path, *args, **kwargs)

        monkeypatch.setattr(search.Qdrant, 'request', request)
    result = perform(w, settings=w.settings)
    assert result['version_id'] and result['error'] is None and result['search_error']
    assert 'synthetic driver detail' not in result['search_error']
    sent = len(provider.sent)
    state = moex.status(w.service, w.actor, w.project)
    assert state['last_success_at'] and state['last_error'] == result['search_error']
    raw, _ = w.service.page_or_file(w.actor, w.project, result['version_id'])
    assert json.loads(raw)['normalized']['price']['value'] == '17.25'
    with w.engine.begin() as connection:
        saved = connection.execute(text('SELECT response FROM aist.moex_checks WHERE project_id=:p'),
                                   dict(p=w.project)).scalar_one()
        assert saved == result
    # A fresh Service and fresh pooled connections must use the durable receipt.
    w.engine.dispose()
    restarted = Service(w.engine, Storage(w.settings.storage_root))
    assert moex.update(restarted, w.actor, w.project, 'd16-operation-first', 'get', settings=w.settings) == result
    assert perform(w, 'cached-after-failure', settings=w.settings)['search_error'] == result['search_error']
    assert attempts == [failure] and len(iss) == 1 and len(provider.sent) == sent


def test_explicit_refresh_repairs_qdrant_without_repaying_embeddings(workspace, iss, provider, qdrant, monkeypatch):
    w = workspace
    original_request = search.Qdrant.request

    def unavailable(client, method, path, *args, **kwargs):
        if method == 'PUT' and '/points?' in path:
            raise ApiError('search_index_unavailable')
        return original_request(client, method, path, *args, **kwargs)

    monkeypatch.setattr(search.Qdrant, 'request', unavailable)
    failed = perform(w, settings=w.settings)
    assert failed['search_error'] and len(provider.sent) == 1
    assert perform(w, settings=w.settings) == failed
    monkeypatch.setattr(search.Qdrant, 'request', original_request)
    recovered = perform(w, 'repair', 'refresh', settings=w.settings)
    assert recovered == dict(version_id=failed['version_id'], changed=False, error=None, search_error=None)
    assert moex.status(w.service, w.actor, w.project)['last_error'] is None
    assert perform(w, settings=w.settings) == failed  # Historical error receipt stays unchanged.
    assert perform(w, 'repair', 'refresh', settings=w.settings) == recovered
    assert perform(w, 'get-after-repair', settings=w.settings)['search_error'] is None
    assert len(iss) == 2 and len(provider.sent) == 1
    with w.engine.begin() as connection:
        assert connection.execute(text('SELECT count(*) FROM aist.source_versions WHERE project_id=:p'),
                                  dict(p=w.project)).scalar_one() == 1
        assert connection.execute(text('SELECT bool_and(indexed) FROM aist.knowledge_chunks WHERE version_id=:v'),
                                  dict(v=failed['version_id'])).scalar_one()


@pytest.mark.parametrize('interruption', ['indexing', 'receipt_write'])
def test_interruption_keeps_incomplete_receipt_and_never_retries(workspace, iss, provider, qdrant, monkeypatch, interruption):
    w = workspace
    original_index, original_begin = search.index_material, w.engine.begin
    indexed = False

    def index(*args, **kwargs):
        nonlocal indexed
        if interruption == 'indexing':
            raise RuntimeError('synthetic process interruption')
        result = original_index(*args, **kwargs)
        indexed = True
        return result

    @contextmanager
    def begin():
        if indexed:
            raise TimeoutError('synthetic receipt write failure')
        with original_begin() as connection:
            yield connection

    monkeypatch.setattr(search, 'index_material', index)
    monkeypatch.setattr(w.engine, 'begin', begin)
    with pytest.raises(RuntimeError if interruption == 'indexing' else TimeoutError):
        perform(w, settings=w.settings)
    monkeypatch.setattr(w.engine, 'begin', original_begin)
    sent = len(provider.sent)
    w.engine.dispose()
    state = moex.status(w.service, w.actor, w.project)
    pending = perform(w, settings=w.settings)
    assert pending['version_id'] == state['version']['id'] and pending['search_error'] == state['last_error']
    assert 'не завершена' in pending['search_error']
    assert len(iss) == 1 and len(provider.sent) == sent


def test_older_index_failure_does_not_overwrite_newer_refresh(workspace, monkeypatch):
    w = workspace
    entered, release = threading.Event(), threading.Event()
    attempts = []
    current = snapshot()
    monkeypatch.setattr(moex, 'fetch_snapshot', lambda authorize: current)

    def index(service, settings, actor, project, version_id, **kwargs):
        attempts.append(version_id)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(8)
            raise ApiError('search_index_unavailable')

    monkeypatch.setattr(search, 'index_material', index)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(perform, w, settings=w.settings)
        assert entered.wait(5)
        try:
            current = snapshot()
            current['requests']['history']['sha256'] = 'c' * 64
            current['normalized']['price']['value'] = '40.25'
            current['sections'] = moex.snapshot_sections(current['normalized'])
            newer = perform(w, 'newer', 'refresh', settings=w.settings)
        finally:
            release.set()
        older = future.result()
    assert older['search_error'] and newer['search_error'] is None
    assert older['version_id'] != newer['version_id']
    state = moex.status(w.service, w.actor, w.project)
    assert state['version']['id'] == newer['version_id'] and state['last_error'] is None
    assert state['normalized']['price']['value'] == '40.25'
    raw, _ = w.service.page_or_file(w.actor, w.project, older['version_id'])
    assert json.loads(raw)['normalized']['price']['value'] == '17.25'
    assert perform(w, settings=w.settings) == older
    assert perform(w, 'newer', 'refresh', settings=w.settings) == newer
    assert len(attempts) == 2
