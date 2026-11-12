"""D15 regressions against an isolated PostgreSQL; every provider transport is synthetic.

Run with AIST_TEST_POSTGRES=true and an aist_d15_r1_test database name. The database user needs CREATEDB.
Every workspace gets a fresh migrated database, also used by new Settings instances and child processes.
"""
from datetime import datetime, timedelta
from dataclasses import replace
from decimal import Decimal
import json
import os
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import Settings
from app.accounting import Accounting
from app.database import ROOT, make_engine
from app.embeddings import CONFIG, INDEX_ID, PRICING, Embeddings, digest, input_text
from app.errors import ApiError
from app.facts import pin_of, save_fact
from app.runs import Runs
from app.search import Qdrant, index_material, register, ui_search
from app.service import Service, ident, row
from app.storage import Storage
from app.tools import invoke
from pdf_fixtures import pdf_bytes

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def isolated_database(settings, request):
    """Global worker operations need an empty database, not only a new project in the shared test database."""
    admin = make_engine(replace(settings, db_name='postgres'))
    request.addfinalizer(admin.dispose)
    name = 'aist_d15_r1_test_' + ident('workspace')

    def drop_database():
        with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))

    # CREATE may commit before the client raises; cleanup must already be registered.
    request.addfinalizer(drop_database)
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))

    # Keep defaults and subprocesses in this database for the whole test, including after monkeypatch.undo().
    environment = pytest.MonkeyPatch()
    request.addfinalizer(environment.undo)
    environment.setenv('AIST_DB_NAME', name)
    environment.setenv('AIST_STORAGE_ROOT', settings.storage_root)
    environment.setenv('AIST_EXPORT_ROOT', settings.export_root)
    environment.setenv('AIST_SNAPSHOT_PATH', settings.snapshot_path)
    command.upgrade(Config(str(ROOT / 'infra/alembic.ini')), 'head')
    return replace(settings, db_name=name)


@pytest.fixture
def workspace(tmp_path, monkeypatch, request):
    settings = Settings(storage_root=str(tmp_path / 'sources'), export_root=str(tmp_path / 'exports'),
                        snapshot_path=str(tmp_path / 'worker-snapshot.json'), executor_mode='openclaw',
                        live_models_enabled=True, embeddings_enabled=True, lease_seconds=120,
                        policy_path=str(ROOT / 'executor/openclaw/policy.d15.json'))
    assert settings.db_name.startswith('aist_d15_r1_test'), 'Use a dedicated regression database'
    settings = isolated_database(settings, request)
    # Undo test overrides before restoring the environment that preceded this workspace.
    request.addfinalizer(monkeypatch.undo)
    engine = make_engine(settings)
    request.addfinalizer(engine.dispose)
    service = Service(engine, Storage(settings.storage_root))
    runs = Runs(service, settings)
    request.node.user_properties.append(('database', settings.db_name))
    actor, session = ident('usr'), ident('ses')
    with engine.begin() as connection:
        connection.execute(text('INSERT INTO aist.users(id,label) VALUES (:id,:label)'), dict(id=actor, label='D15 regression'))
        connection.execute(text("""INSERT INTO aist.sessions(id,user_id,token_hash,expires_at)
            VALUES (:id,:actor,:hash,clock_timestamp()+interval '1 hour')"""),
            dict(id=session, actor=actor, hash=ident('synthetic')))
        # A pre-fix index row must stay compatible without replacing collections/cache.
        connection.execute(text('''INSERT INTO aist.embedding_indexes(id,configuration)
            VALUES (:id,CAST(:configuration AS jsonb)) ON CONFLICT DO NOTHING'''),
            dict(id=INDEX_ID, configuration=json.dumps({**CONFIG, **PRICING})))
    _, project = service.write(actor, 'createProject', ident('key'), {'name': 'D15 regression'})
    return SimpleNamespace(engine=engine, settings=settings, service=service, runs=runs,
                           actor=actor, session=session, project=project['id'], monkeypatch=monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    transport = SimpleNamespace(sent=[], before_dispatch=None, waiting_response=None, status=200, usage={'prompt_tokens': 42})

    class Connection:
        def __init__(self, *args, **kwargs):
            if transport.before_dispatch:
                transport.before_dispatch()

        def request(self, method, path, body, headers):
            self.inputs = json.loads(body)['input']
            transport.sent.append(self.inputs)

        def getresponse(self):
            if transport.waiting_response:
                transport.waiting_response()
            payload = dict(model=CONFIG['model'], usage=transport.usage,
                           data=[dict(index=index, embedding=[1.0] + [0.0] * 4095)
                                 for index in range(len(self.inputs))])
            return SimpleNamespace(status=transport.status, read=lambda limit: json.dumps(payload).encode())

        def close(self):
            pass

    monkeypatch.setattr('app.novita.credential', lambda path: 'synthetic-local-token')
    monkeypatch.setattr('app.embeddings.http.client.HTTPSConnection', Connection)
    return transport


@pytest.fixture
def qdrant(monkeypatch):
    index = SimpleNamespace(points={}, before_count=None, filters=[])

    def request(client, method, path, payload=None, allow_missing=False):
        collection = path.split('/')[2]
        points = index.points.setdefault(collection, {})
        if method == 'GET':
            return None
        if method == 'PUT':
            if '/points' in path:
                points.update({point['id']: point for point in payload['points']})
            return {}
        if path.endswith('/points/count') and index.before_count:
            index.before_count()
        index.filters.append(payload['filter'])
        matched = list(points.values())
        for condition in payload['filter']['must']:
            key, match = condition['key'], condition['match']
            allowed = match['any'] if 'any' in match else [match['value']]
            matched = [point for point in matched if point['payload'].get(key) in allowed]
        if path.endswith('/points/count'):
            return {'count': len(matched)}
        assert path.endswith('/points/query')
        return {'points': [dict(id=point['id'], score=1.0) for point in matched[:payload['limit']]]}

    monkeypatch.setattr(Qdrant, 'request', request)
    return index


def upload(workspace, content='Revenue 2021 100 RUB. Revenue 2022 200 RUB', previous=None):
    body = dict(title='Regression material', expected_revision=previous['source']['revision'] if previous else 0)
    _, receipt = workspace.service.write(workspace.actor, 'uploadSourceVersion' if previous else 'uploadSource',
        ident('key'), body, workspace.project, previous['source']['id'] if previous else None,
        pdf_bytes(content, pages=1), 'regression.pdf')
    return receipt


def publish_fixture_worker(w):
    """The test dispatches synchronously in place of a Worker loop."""
    from app.worker_availability import publish
    publish(w.runs, 'worker_test_dispatch')


def start_run(workspace, version):
    publish_fixture_worker(workspace)
    _, conversation = workspace.service.write(workspace.actor, 'createConversation', ident('key'),
                                             {'title': 'Regression'}, workspace.project)
    body = dict(expected_revision=0, text='Synthetic question', source_version_ids=[version['id']],
                skill_version_id='skill_source-answer-v2', model_profile_version_id='model_novita-mimo-v1',
                requested_result_type='evidence_answer', parameters=dict(audience=None, sections=[]),
                base_version_id=None, expected_artifact_revision=0, requested_export=None)
    _, submission = workspace.runs.write(workspace.actor, workspace.session, workspace.project,
                                         conversation['id'], 'submitMessage', ident('key'), body)
    run = workspace.runs.claim('d15_regression')
    assert run['id'] == submission['run']['id']
    return run


def calls(workspace):
    with workspace.engine.connect() as connection:
        return list(connection.execute(text('SELECT * FROM aist.embedding_calls WHERE owner_id=:owner ORDER BY created_at'),
                                       dict(owner=workspace.actor)).mappings())


def search_args(version, query='Synthetic question', corpus='factual'):
    return dict(query=query, source_version_ids=[version['id']], limit=5, method='semantic.qwen3.v1', corpus=corpus)


def invalidate(workspace, run, change):
    if change == 'cancel':
        workspace.runs.write(workspace.actor, workspace.session, workspace.project, run['id'],
                             'cancelRun', ident('key'), dict(expected_revision=run['revision']))
        return
    with workspace.engine.begin() as connection:
        if change == 'revoke':
            connection.execute(text('UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:id'), dict(id=workspace.session))
        elif change == 'lease':
            connection.execute(text("UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"), dict(id=run['id']))
        elif change == 'epoch':
            connection.execute(text('UPDATE aist.runs SET epoch=epoch+1 WHERE id=:id'), dict(id=run['id']))
        else:
            assert change == 'deadline'
            class ExpiredClock:
                @staticmethod
                def now(timezone):
                    return datetime.now(timezone) + timedelta(days=1)
            workspace.monkeypatch.setattr('app.runs.datetime', ExpiredClock)


@pytest.mark.parametrize('replacement', ['Revenue 2021 100 RUB. Revenue 2022 200 RUB', 'Changed sample. Revenue 2022 900 RUB'])
def test_sample_versions_cannot_become_facts_or_calculation_inputs(workspace, provider, qdrant, replacement):
    first = upload(workspace)
    register(workspace.service, workspace.actor, workspace.project, first['version']['id'],
             'samples', 'TrainingCo', '2021', 'Synthetic sample')
    second = upload(workspace, replacement, first)
    third = upload(workspace, replacement, second)
    assert second['version']['corpus'] == third['version']['corpus'] == 'samples'
    if replacement.startswith('Revenue 2021'):
        assert first['version']['sha256'] == second['version']['sha256']
    version_id = second['version']['id']
    with workspace.engine.begin() as connection:
        version = workspace.service.scoped(connection, workspace.actor, workspace.project, 'source_versions', version_id)
        pin = pin_of(version)
        with pytest.raises(ApiError, match='sample_not_evidence'):
            save_fact(workspace.service, connection, workspace.actor, workspace.project, pin, {})
        run = dict(id='synthetic', owner_id=workspace.actor, project_id=workspace.project, input=dict(source_versions=[pin]))
        with pytest.raises(ApiError, match='sample_not_evidence'):
            invoke(workspace.runs, connection, run, 1, 'aist_calculate', dict(
                formula_id='growth_percent.v1', inputs=[dict(name=name, fact=dict(source_version_id=version_id, fact_id='fact_synthetic'))
                                                      for name in ('previous', 'current')], rounding=dict(places=2, mode='HALF_UP')))
        with pytest.raises(ApiError, match='sample_not_evidence'):
            workspace.runs.eligibility(connection, workspace.actor, workspace.project,
                                      dict(source_version_ids=[version_id], requested_result_type='analytical_note'))
    indexed = index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version_id)
    assert indexed['corpus'] == 'samples'
    assert ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, search_args(second['version']))['hits'] == []
    found = ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, search_args(second['version'], corpus='samples'))
    assert found['hits'] and all(hit['corpus'] == 'samples' for hit in found['hits'])
    assert all(hit['company'] is None and hit['period'] is None for hit in found['hits'])


def test_already_uploaded_descendant_of_sample_is_restricted(workspace):
    first = upload(workspace)
    second = upload(workspace, previous=first)
    register(workspace.service, workspace.actor, workspace.project, first['version']['id'], 'samples', None, None, 'Late classification')
    assert workspace.service.get(workspace.actor, workspace.project, 'source_versions', second['version']['id'])['corpus'] == 'samples'
    with pytest.raises(ApiError, match='idempotency_conflict'):
        register(workspace.service, workspace.actor, workspace.project, second['version']['id'], 'factual', None, None, 'Incorrect override')


@pytest.mark.parametrize('change,code', [('cancel', 'run_terminal'), ('revoke', 'unauthenticated'),
                                       ('lease', 'outcome_unknown'), ('epoch', 'outcome_unknown'), ('deadline', 'budget_exceeded')])
@pytest.mark.parametrize('stage', ['before_reserve', 'before_send'])
def test_invalidated_run_does_not_send_embeddings(workspace, provider, qdrant, change, code, stage):
    source = upload(workspace)
    version = source['version']
    register(workspace.service, workspace.actor, workspace.project, version['id'], 'factual', None, None, 'Synthetic source')
    index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id'])
    run = start_run(workspace, version)
    before = len(provider.sent)
    if stage == 'before_reserve':
        qdrant.before_count = lambda: invalidate(workspace, run, change)
    else:
        provider.before_dispatch = lambda: invalidate(workspace, run, change)
    with pytest.raises(ApiError, match=code):
        workspace.runs.tool(run['id'], run['epoch'], 'call_synthetic', 'aist_search_evidence', search_args(version))
    assert len(provider.sent) == before
    ledger = calls(workspace)
    if stage == 'before_reserve':
        assert len(ledger) == 1  # Only the document indexing call.
    else:
        assert len(ledger) == 2 and ledger[-1]['state'] == 'not_sent' and ledger[-1]['exposure'] == 0
        assert ledger[-1]['usage'] is None
    with workspace.engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM aist.tool_calls WHERE run_id=:id'), dict(id=run['id'])).scalar_one() == 0


def test_response_wait_allows_heartbeat_cancel_and_keeps_paid_cache(workspace, provider, qdrant):
    version = upload(workspace)['version']
    register(workspace.service, workspace.actor, workspace.project, version['id'], 'factual', None, None, 'Synthetic source')
    index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id'])
    run = start_run(workspace, version)

    def during_response():
        assert workspace.runs.heartbeat(run['id'], run['epoch'], 'd15_regression') == run['id']
        invalidate(workspace, run, 'cancel')

    provider.waiting_response = during_response
    with pytest.raises(ApiError, match='run_terminal'):
        workspace.runs.tool(run['id'], run['epoch'], 'call_synthetic', 'aist_search_evidence', search_args(version))
    provider.waiting_response = None
    before = len(provider.sent)
    assert ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, search_args(version))['hits']
    assert len(provider.sent) == before == 2
    assert calls(workspace)[-1]['state'] == 'observed'


def test_cache_and_index_survive_pricing_changes_and_reconnect(workspace, provider, qdrant, monkeypatch):
    version = upload(workspace)['version']
    register(workspace.service, workspace.actor, workspace.project, version['id'], 'factual', None, None, 'Synthetic source')
    first_index = index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id'])
    first_result = ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, search_args(version))
    original_calls = calls(workspace)
    monkeypatch.setitem(PRICING, 'pricing_checked_on', '2026-10-16')
    monkeypatch.setitem(PRICING, 'price_usd_per_million_input', '0.10')
    workspace.engine.dispose()
    assert index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id']) == first_index
    assert ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, search_args(version)) == first_result
    assert len(provider.sent) == len(original_calls) == len(calls(workspace)) == 2
    Embeddings(workspace.engine, workspace.settings).vectors(workspace.actor, [input_text('Another question', query=True)], 'query')
    ledger = calls(workspace)
    assert ledger[0]['pricing']['price_usd_per_million_input'] == '0.07'
    assert ledger[-1]['pricing']['price_usd_per_million_input'] == '0.10'
    assert ledger[-1]['estimated_usd'] == Decimal('0.0000042')
    with workspace.engine.connect() as connection:
        saved = row(connection, 'SELECT configuration FROM aist.embedding_indexes WHERE id=:id', id=INDEX_ID)['configuration']
        assert saved['pricing_checked_on'] == '2026-10-15'  # Existing index row is untouched.


def test_revoked_ui_session_is_checked_at_send(workspace, provider):
    def authorize(connection):
        workspace.runs.session_alive(connection, dict(session_id=workspace.session, owner_id=workspace.actor))
    provider.before_dispatch = lambda: invalidate(workspace, None, 'revoke')
    with pytest.raises(ApiError, match='unauthenticated'):
        Embeddings(workspace.engine, workspace.settings).vectors(workspace.actor, ['New question'], 'query', authorize=authorize)
    assert not provider.sent
    assert calls(workspace)[0]['state'] == 'not_sent'


def test_missing_usage_keeps_reservation_instead_of_zero_cost(workspace, provider):
    provider.usage = None
    Embeddings(workspace.engine, workspace.settings).vectors(workspace.actor, ['New question'], 'query')
    call = calls(workspace)[0]
    assert call['state'] == 'observed' and call['estimated_usd'] is None and call['exposure'] == Decimal('.01')


def test_pre_fix_cached_vector_is_reused_without_a_provider_call(workspace, provider):
    call_id = ident('emb')
    input_hash = digest('Legacy cached input')
    vector = [1.0] + [0.0] * 4095
    with workspace.engine.begin() as connection:
        connection.execute(text("""INSERT INTO aist.embedding_calls
            (id,index_id,owner_id,purpose,input_hashes,state,exposure)
            VALUES (:id,:index,:owner,'document',CAST(:hashes AS jsonb),'observed',.01)"""),
            dict(id=call_id, index='qwen3_677d88a30eee8bfc', owner=workspace.actor, hashes=json.dumps([input_hash])))
        connection.execute(text("""INSERT INTO aist.embedding_cache(index_id,owner_id,input_sha256,vector,call_id)
            VALUES (:index,:owner,:hash,CAST(:vector AS jsonb),:call)"""),
            dict(index='qwen3_677d88a30eee8bfc', owner=workspace.actor, hash=input_hash, vector=json.dumps(vector), call=call_id))
    assert Embeddings(workspace.engine, workspace.settings).vectors(workspace.actor, ['Legacy cached input'], 'document') == [vector]
    assert not provider.sent
    assert len(calls(workspace)) == 1


@pytest.mark.parametrize('failure', ['429', 'timeout'])
def test_sent_failure_is_not_retried_and_keeps_unknown_exposure(workspace, provider, failure):
    if failure == '429':
        provider.status = 429
        code = 'embedding_rate_limited'
    else:
        def timeout():
            raise TimeoutError()
        provider.waiting_response = timeout
        code = 'embedding_unavailable'
    with pytest.raises(ApiError, match=code):
        Embeddings(workspace.engine, workspace.settings).vectors(workspace.actor, ['New question'], 'query')
    assert len(provider.sent) == 1
    call = calls(workspace)[0]
    assert call['state'] == 'unknown' and call['estimated_usd'] is None and call['exposure'] == Decimal('.01')


def test_scope_rejections_precede_qdrant_and_provider(workspace, provider, qdrant):
    version = upload(workspace)['version']
    register(workspace.service, workspace.actor, workspace.project, version['id'], 'factual', 'ReviewCo', '2022', 'Synthetic source')
    index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id'])
    _, other_project = workspace.service.write(workspace.actor, 'createProject', ident('key'), {'name': 'Other project'})
    arguments = search_args(version) | dict(company='ReviewCo', period='2022')
    before = len(provider.sent)
    for actor, project in [(ident('usr'), workspace.project), (workspace.actor, other_project['id'])]:
        with pytest.raises(ApiError, match='resource_not_found'):
            ui_search(workspace.service, workspace.settings, actor, project, arguments)
    unselected = upload(workspace)['version']
    run = start_run(workspace, version)
    with workspace.engine.begin() as connection:
        with pytest.raises(ApiError, match='resource_not_found'):
            invoke(workspace.runs, connection, run, run['epoch'], 'aist_search_evidence', search_args(unselected))
    assert ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project,
                     arguments | dict(company='Different'))['hits'] == []
    assert len(provider.sent) == before and qdrant.filters == []
    assert ui_search(workspace.service, workspace.settings, workspace.actor, workspace.project, arguments)['hits']
    assert {condition['key'] for condition in qdrant.filters[-1]['must']} == {
        'owner_id', 'project_id', 'version_id', 'corpus', 'index_id', 'company', 'period'}


def test_tool_receipt_and_cached_query_prevent_repeat_payment(workspace, provider, qdrant):
    version = upload(workspace)['version']
    register(workspace.service, workspace.actor, workspace.project, version['id'], 'factual', None, None, 'Synthetic source')
    index_material(workspace.service, workspace.settings, workspace.actor, workspace.project, version['id'])
    run = start_run(workspace, version)
    arguments = search_args(version)
    first = workspace.runs.tool(run['id'], run['epoch'], 'call_first', 'aist_search_evidence', arguments)
    assert first['ok'] and first['result']['hits']
    workspace.engine.dispose()
    assert workspace.runs.tool(run['id'], run['epoch'], 'call_first', 'aist_search_evidence', arguments) == first
    another = workspace.runs.tool(run['id'], run['epoch'], 'call_second', 'aist_search_evidence', arguments)
    assert another['result'] == first['result']
    with pytest.raises(ApiError, match='idempotency_conflict'):
        workspace.runs.tool(run['id'], run['epoch'], 'call_first', 'aist_search_evidence', arguments | dict(query='Changed question'))
    assert len(provider.sent) == len(calls(workspace)) == 2


@pytest.mark.parametrize('ceiling', ['13', None])
def test_optional_shared_model_ceiling_preserves_reservations_and_run_limits(workspace, tmp_path, ceiling):
    policy = json.loads((ROOT / 'executor/openclaw/policy.template01-r1.json').read_text())
    assert policy['total_authorized_usd'] is None
    policy.update(total_authorized_usd=ceiling, historical_exposure_usd='100')
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    workspace.settings = replace(workspace.settings, policy_path=str(path))
    workspace.runs = Runs(workspace.service, workspace.settings)
    version = upload(workspace)['version']
    run = start_run(workspace, version)
    accounting = Accounting(workspace.runs)
    assert run['input']['budget']['max_model_calls'] == 12
    assert run['input']['budget']['max_duration_seconds'] == 3600
    if ceiling is not None:
        with pytest.raises(ApiError, match='budget_exceeded'):
            accounting.reserve(run['id'], run['epoch'])
        # Reloading an unlimited policy must not rewrite the policy pinned to an old Run.
        policy['total_authorized_usd'] = None
        path.write_text(json.dumps(policy))
        with pytest.raises(ApiError, match='budget_exceeded'):
            Accounting(Runs(workspace.service, workspace.settings)).reserve(run['id'], run['epoch'])
        expected_calls = 0
    else:
        for _ in range(12):
            accounting.reserve(run['id'], run['epoch'])
        with pytest.raises(ApiError, match='budget_exceeded'):
            accounting.reserve(run['id'], run['epoch'])
        expected_calls = 12
    with workspace.engine.connect() as connection:
        count, exposure = connection.execute(text('''SELECT count(*),coalesce(sum(exposure),0)
            FROM aist.model_calls WHERE run_id=:run'''), dict(run=run['id'])).one()
        assert count == expected_calls and exposure == Decimal('.35') * expected_calls
        assert connection.execute(text('SELECT count(*) FROM aist.usage_entries WHERE run_id=:run'),
                                  dict(run=run['id'])).scalar_one() == expected_calls
    if ceiling is None:
        expired = start_run(workspace, version)
        invalidate(workspace, expired, 'deadline')
        with pytest.raises(ApiError, match='budget_exceeded'):
            accounting.reserve(expired['id'], expired['epoch'])


@pytest.mark.parametrize('ceiling', ['13', None])
def test_optional_shared_embedding_ceiling_preserves_accounting(workspace, tmp_path, ceiling):
    policy = json.loads((ROOT / 'executor/openclaw/policy.template01-r1.json').read_text())
    policy.update(total_authorized_usd=ceiling, historical_exposure_usd='100')
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    embeddings = Embeddings(workspace.engine, replace(workspace.settings, policy_path=str(path)))
    with workspace.engine.begin() as connection:
        if ceiling is not None:
            with pytest.raises(ApiError, match='budget_exceeded'):
                embeddings.reserve(connection, workspace.actor, [digest('Synthetic input')], 'query', PRICING)
        else:
            embeddings.reserve(connection, workspace.actor, [digest('Synthetic input')], 'query', PRICING)
    recorded = calls(workspace)
    if ceiling is not None:
        assert recorded == []
    else:
        assert len(recorded) == 1
        assert recorded[0]['state'] == 'attempted' and recorded[0]['exposure'] == Decimal('.01')
