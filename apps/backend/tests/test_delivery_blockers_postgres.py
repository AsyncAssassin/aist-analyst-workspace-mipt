"""Targeted lifecycle and file checks using isolated PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from threading import Event

import pytest

from app import note_external
from app.errors import ApiError
from app.exports import Exports
from app.runs import Runs, execute
from app.service import ident
from app.worker import Worker
from test_aso_quality02_phase3_postgres import start
from test_chat01_postgres import body, conversation, reply, send
from test_d15_postgres import workspace
from test_draft01_postgres import export_request, http, model_draft
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('action', ['cancel', 'expired'])
def test_external_identity_does_not_hold_run_lock(workspace, monkeypatch, action):
    w = workspace
    source, _ = note_source(w)
    run = start(w, source)
    identifying, proceed = Event(), Event()

    def paused_identity(materials):
        assert materials and materials[0][0]
        identifying.set()
        assert proceed.wait(7)
        return None

    monkeypatch.setattr(note_external, 'identity', paused_identity)
    with ThreadPoolExecutor(2) as workers:
        pending = workers.submit(note_external.prepare_all, w.runs, run)
        try:
            assert identifying.wait(3)
            renewed = workers.submit(w.runs.heartbeat, run['id'], run['epoch'], run['worker_id'])
            assert renewed.result(timeout=2) == run['id']
            if action == 'cancel':
                cancelled = workers.submit(w.runs.write, w.actor, w.session, w.project, run['id'],
                                           'cancelRun', ident('key'), {'expected_revision': run['revision']})
                assert cancelled.result(timeout=2)[1]['state'] == 'cancelled'
            else:
                with w.engine.begin() as c:
                    execute(c, "UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id",
                            id=run['id'])
                assert w.runs.heartbeat(run['id'], run['epoch'], run['worker_id']) is None
                w.runs.recover()
        finally:
            proceed.set()
        with pytest.raises(ApiError) as fenced:
            pending.result(timeout=3)
        assert fenced.value.code == ('run_terminal' if action == 'cancel' else 'outcome_unknown')
    with w.engine.connect() as c:
        assert execute(c, 'SELECT external_context FROM aist.runs WHERE id=:id', id=run['id']).scalar_one() is None


def test_recover_exception_keeps_owned_heartbeat_and_other_worker_steps(workspace, monkeypatch):
    w = workspace
    send(w, conversation(w), body())
    worker = Worker(w.settings)
    worker.engine.dispose()
    worker.engine, worker.runs = w.engine, w.runs
    run = w.runs.claim(worker.id)
    steps = []

    def fail_recover():
        steps.append('recover')
        raise RuntimeError('synthetic recovery failure')

    worker.jobs[run['id']] = (type('ActiveThread', (), {'is_alive': lambda self: True})(), run)
    monkeypatch.setattr(w.runs, 'recover', fail_recover)
    monkeypatch.setattr(worker, 'start_export', lambda: steps.append('export'))
    monkeypatch.setattr(worker, 'start_run', lambda: steps.append('claim'))
    assert worker.tick()
    assert steps == ['recover', 'export', 'claim']
    with w.engine.connect() as c:
        lease = execute(c, 'SELECT lease_until FROM aist.runs WHERE id=:id', id=run['id']).scalar_one()
    assert lease > run['lease_until']


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled', 'budget_exceeded'])
def test_terminal_stream_drains_every_page_and_then_closes(workspace, terminal):
    w = workspace
    conv = conversation(w)
    send(w, conv, body())
    run = w.runs.claim('stream_test')
    with w.engine.begin() as c:
        current = w.runs.locked(c, run['id'])
        for _ in range(7):
            w.runs.event(c, current, 'recovery_recorded', {'reason': 'Synthetic stream event'})
    if terminal == 'completed':
        assert reply(w, run, 'Финальный синтетический ответ.')['ok']
    elif terminal == 'cancelled':
        w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', ident('key'),
                     {'expected_revision': run['revision']})
    else:
        w.runs.stop(run['id'], run['epoch'], 'budget_exceeded' if terminal == 'budget_exceeded' else 'tool_failed')
    _, settled = w.runs.event_batch(w.actor, w.project, run['id'], {})
    assert not settled
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    path = f"/v1/projects/{w.project}/runs/{run['id']}/events/stream?limit=2"
    with http(w) as client:
        response = client.get(path, headers={'Last-Event-ID': f"{run['id']}:2"})
        assert response.status_code == 200
        ids = [int(line.rsplit(':', 1)[1]) for line in response.text.splitlines() if line.startswith('id: ')]
        last = w.runs.get(w.actor, w.project, run['id'])['last_event_seq']
        assert ids == list(range(3, last + 1))
        assert response.text.endswith(': run-finished\n\n')
        if terminal == 'completed':
            messages = client.get(f"/v1/projects/{w.project}/conversations/{conv['id']}/messages").json()['items']
            assert messages[-1]['text'] == 'Финальный синтетический ответ.'
        with w.engine.begin() as c:
            execute(c, "UPDATE aist.sessions SET expires_at=clock_timestamp() WHERE id=:s", s=w.session)
        assert client.get(path).status_code == 401


def test_pdf_storage_revalidates_exact_original_file_after_return(workspace):
    w = workspace
    source, _ = note_source(w)
    storage = w.service.storage
    original = storage.path(source['id'] + '.pdf')
    saved = original.read_bytes()
    displaced = storage.root.with_name('displaced')
    storage.root.rename(displaced)
    try:
        with pytest.raises(ApiError) as unavailable:
            w.service.page_or_file(w.actor, w.project, source['id'])
        assert unavailable.value.code == 'temporarily_unavailable'
        w.service.reconcile_files()
        with w.engine.connect() as c:
            assert execute(c, 'SELECT processing_state FROM aist.source_versions WHERE id=:id',
                           id=source['id']).scalar_one() == 'ready'
        storage.root.mkdir()
        w.service.reconcile_files()
        with pytest.raises(ApiError) as missing:
            w.service.page_or_file(w.actor, w.project, source['id'])
        assert missing.value.code == 'export_missing'
        original.write_bytes(b'unknown bytes')
        with pytest.raises(ApiError) as corrupt:
            w.service.page_or_file(w.actor, w.project, source['id'])
        assert corrupt.value.code == 'export_corrupt'
        original.write_bytes(saved)
        w.service.reconcile_files()
        data, version = w.service.page_or_file(w.actor, w.project, source['id'])
        assert data == saved and version['sha256'] == source['sha256'] and version['processing_state'] == 'ready'
    finally:
        if original.exists():
            original.unlink()
        if storage.root.exists():
            storage.root.rmdir()
        displaced.rename(storage.root)


@pytest.mark.parametrize('after_publish', [False, True])
def test_checked_export_recovers_without_rendering_again(workspace, monkeypatch, after_publish):
    w = workspace
    _, _, _, view = model_draft(w)
    exports = Exports(w.runs)
    version = view['version']
    key = ident('key')
    args = (w.actor, w.session, w.project, version['artifact_id'], version['id'], key, export_request('pdf', 'draft'))
    status, entry = exports.request(*args)
    assert status == 202
    publish = exports.files.publish

    def interrupted(value):
        if after_publish:
            publish(value)
        raise OSError('synthetic publication boundary failure')

    monkeypatch.setattr(exports.files, 'publish', interrupted)
    exports.process_one()
    eid = entry['id']
    assert exports.get(w.actor, w.project, eid)['state'] == 'recovery_required'
    with pytest.raises(ApiError) as uncertain:
        exports.request(*args)
    assert uncertain.value.code == 'outcome_unknown'
    monkeypatch.setattr(exports.files, 'publish', publish)
    with ThreadPoolExecutor(2) as workers:
        pending = [workers.submit(exports.reconcile, eid) for _ in range(2)]
        for future in pending:
            try:
                assert future.result(timeout=6)['state'] == 'ready'
            except ApiError as busy:
                assert busy.code == 'outcome_unknown'
    data, ready = exports.get(w.actor, w.project, eid, download=True)
    assert ready['state'] == 'ready' and data.startswith(b'%PDF-')
    assert exports.request(*args) == (200, ready)
    assert exports.get(w.actor, w.project, eid, download=True) == (data, ready)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT render_count FROM aist.exports WHERE id=:id', id=eid).scalar_one() == 1


def test_d07_running_chat_and_planned_skill_have_distinct_refusals(workspace):
    w = workspace
    source, _ = note_source(w)
    w.settings = replace(w.settings, executor_mode='qa-scripted', qa_url='http://simulator:8000', live_models_enabled=False)
    w.runs = Runs(w.service, w.settings)
    request = body(sources=[source['id']])
    request.update(skill_version_id='skill_qa-d07-v1', model_profile_version_id='model_qa-d07-v1')
    with http(w) as client:
        project = f'/v1/projects/{w.project}'
        active, empty = conversation(w), conversation(w)

        def submit(chat, payload):
            return client.post(f"{project}/conversations/{chat['id']}/messages", json=payload,
                               headers={'Idempotency-Key': ident('key')})

        created = submit(active, request)
        assert created.status_code == 202
        run = w.runs.claim('d07_scope')
        assert run['id'] == created.json()['run']['id'] and run['state'] == 'running'
        blocked = submit(active, dict(request, expected_revision=1, skill_version_id='skill_source-answer-v1'))
        assert (blocked.status_code, blocked.json()['error']['code']) == (409, 'revision_conflict')
        planned = submit(empty, dict(request, skill_version_id='skill_source-answer-v1'))
        assert (planned.status_code, planned.json()['error']['code']) == (409, 'tool_unavailable')
        future = submit(empty, dict(request, requested_result_type='meeting_summary'))
        assert (future.status_code, future.json()['error']['code']) == (422, 'unsupported_result_type')
        calls = client.get(f"{project}/runs/{run['id']}/model-calls").json()
        assert calls['items'] == []
        assert client.get(f"{project}/runs/{run['id']}/usage").json()['items'] == []
        assert client.get(f"{project}/conversations/{empty['id']}/messages").json()['items'] == []
