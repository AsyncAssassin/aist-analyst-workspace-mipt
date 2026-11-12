"""Explicit retry and fail-closed model admission on an isolated PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from subprocess import TimeoutExpired, run
import sys

import pytest
from sqlalchemy.exc import DBAPIError

from app import exports as export_module, worker_availability
from app.errors import ApiError
from app.exports import Exports
from app.runs import Runs, execute
from app.service import ident
from test_chat01_postgres import body, conversation
from test_d15_postgres import workspace
from test_draft01_postgres import accept, export_request, http, model_draft
from test_openai_responses_postgres import configure_astra


pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def test_explicit_export_retry_keeps_identity_and_receipts(workspace, monkeypatch):
    w = workspace
    _, _, _, view = model_draft(w)
    version = view['version']
    with pytest.raises(ApiError, match='validation_failed'):
        accept(w, view, content_sha256='0' * 64)
    _, accepted = accept(w, view)
    approval = accepted['acceptance']['id']
    exports = Exports(w.runs)
    render = export_module.render
    renders = []
    failures = [TimeoutExpired('synthetic converter', 1)]

    def render_once(*args):
        renders.append(args[0]['id'])
        if failures:
            (args[3] / 'note.docx').write_bytes(b'unchecked partial document')
            raise failures.pop()
        return render(*args)

    monkeypatch.setattr(export_module, 'render', render_once)
    scope = (w.actor, w.session, w.project, version['artifact_id'], version['id'])
    key = ident('key')
    request = export_request('docx', 'accepted', accepted['revision'])
    status, first = exports.request(*scope, key, request)
    assert status == 202
    eid = first['id']
    exports.process_one()
    failed = exports.get(w.actor, w.project, eid)
    assert failed['state'] == 'failed' and failed['file'] is None
    assert failed['error']['code'] == 'temporarily_unavailable'
    assert exports.request(*scope, key, request) == (200, failed)
    assert exports.request(*scope, ident('key'), request) == (200, failed)
    with pytest.raises(ApiError, match='idempotency_conflict'):
        exports.request(*scope, key, export_request('pdf', 'draft'))
    assert exports.reconcile(eid) == failed
    exports.process_one()
    assert renders == [version['id']]

    args = [sys.executable, '-m', 'app.cli', 'retry-export', '--export-id', eid,
            '--failed-revision', str(failed['revision'])]

    def retry_cli():
        return run(args, capture_output=True, text=True, timeout=15)

    with ThreadPoolExecutor(2) as callers:
        attempts = list(callers.map(lambda _: retry_cli(), range(2)))
    acknowledged = [json.loads(result.stdout) for result in attempts if result.returncode == 0]
    assert acknowledged and all(item['id'] == eid and item['state'] == 'queued' for item in acknowledged)
    for result in attempts:
        if result.returncode:
            assert 'сверки' in result.stderr
    with ThreadPoolExecutor(2) as workers:
        list(workers.map(lambda _: exports.process_one(), range(2)))
    data, ready = exports.get(w.actor, w.project, eid, download=True)
    assert ready['state'] == 'ready' and data.startswith(b'PK')
    assert ready['file']['sha256'] == sha256(data).hexdigest()
    assert ready['identity_sha256'] == first['identity_sha256']
    assert ready['acceptance_id'] == approval
    assert w.runs.version(w.actor, w.project, version['id'])['review']['acceptance']['id'] == approval
    assert json.loads(retry_cli().stdout) == ready
    assert exports.request(*scope, key, request) == (200, ready)
    assert exports.get(w.actor, w.project, eid, download=True) == (data, ready)
    with w.engine.connect() as connection:
        saved = execute(connection, 'SELECT render_count, retry_history FROM aist.exports WHERE id=:id', id=eid).one()
        assert saved.render_count == 2
        assert [item['revision'] for item in saved.retry_history] == [failed['revision']]
        assert execute(connection, 'SELECT count(*) FROM aist.exports WHERE version_id=:v', v=version['id']).scalar_one() == 1
    assert renders == [version['id']] * 2

    with pytest.raises(ApiError, match='resource_not_found'):
        exports.request(w.actor, w.session, w.project, version['artifact_id'], ident('av'), key, request)
    other = w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Other project'})[1]['id']
    for actor, project in [(ident('usr'), w.project), (w.actor, other)]:
        with pytest.raises(ApiError, match='resource_not_found'):
            exports.get(actor, project, eid, download=True)
        with pytest.raises(ApiError, match='resource_not_found'):
            exports.request(actor, w.session, project, version['artifact_id'], version['id'], ident('key'), request)
    with pytest.raises(DBAPIError, match='identity is immutable'):
        with w.engine.begin() as connection:
            execute(connection, """UPDATE aist.exports
                SET data=jsonb_set(data,'{content_sha256}',to_jsonb(CAST(:sha AS text))) WHERE id=:id""", sha='a'*64, id=eid)
    with pytest.raises(DBAPIError, match='descriptor is immutable'):
        with w.engine.begin() as connection:
            execute(connection, """UPDATE aist.exports
                SET data=jsonb_set(data,'{file,sha256}',to_jsonb(CAST(:sha AS text))) WHERE id=:id""", sha='a'*64, id=eid)


def test_retry_does_not_repeat_failure_or_adopt_unknown_bytes(workspace, monkeypatch):
    w = workspace
    _, _, _, view = model_draft(w)
    version = view['version']
    exports = Exports(w.runs)
    _, item = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'],
                              ident('key'), export_request('docx', 'draft'))
    attempts = []

    def failed(*args):
        attempts.append(args[0]['id'])
        raise TimeoutExpired('synthetic converter', 1)

    monkeypatch.setattr(export_module, 'render', failed)
    exports.process_one()
    first = exports.get(w.actor, w.project, item['id'])
    path = exports.files.path(item)
    path.write_bytes(b'unknown file')
    with pytest.raises(ApiError, match='outcome_unknown'):
        exports.retry(item['id'], first['revision'])
    assert path.read_bytes() == b'unknown file'
    path.unlink()

    exports.retry(item['id'], first['revision'])
    exports.process_one()
    second = exports.get(w.actor, w.project, item['id'])
    assert second['state'] == 'failed' and second['revision'] > first['revision']
    assert exports.retry(item['id'], first['revision']) == second
    exports.process_one()
    assert attempts == [version['id']] * 2
    with pytest.raises(ApiError, match='revision_conflict'):
        exports.retry(item['id'], item['revision'])

    def invalid(*args):
        raise ValueError('Invalid document')

    exports.retry(item['id'], second['revision'])
    monkeypatch.setattr(export_module, 'render', invalid)
    exports.process_one()
    last = exports.get(w.actor, w.project, item['id'])
    assert last['error']['code'] == 'validation_failed'
    with pytest.raises(ApiError, match='export_not_ready'):
        exports.retry(item['id'], last['revision'])
    with pytest.raises(DBAPIError, match='Invalid export transition'):
        with w.engine.begin() as connection:
            execute(connection, """UPDATE aist.exports
                SET data=jsonb_set(data,'{state}','"queued"'::jsonb) WHERE id=:id""", id=item['id'])


def test_worker_availability_gates_new_work_but_not_receipts(workspace, tmp_path, monkeypatch):
    w = workspace
    foreign_worker = Runs(w.service, w.settings)
    configure_astra(w, tmp_path)
    worker_availability.publish(foreign_worker, 'worker_foreign_profile')
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    first, second = conversation(w), conversation(w)
    command = dict(body(), model_profile_version_id=w.settings.model_profile_id)

    with http(w) as client:
        def state():
            return client.get('/v1/capabilities').json()['model_execution']['state']

        def post(chat, key, payload=command):
            return client.post(f"/v1/projects/{w.project}/conversations/{chat['id']}/messages",
                               json=payload, headers={'Idempotency-Key': key})

        key = ident('key')
        assert client.get('/readyz').status_code == 200
        assert state() == 'temporarily_unavailable'
        result = post(first, key)
        assert (result.status_code, result.json()['error']['code']) == (503, 'temporarily_unavailable')
        with w.engine.connect() as connection:
            for table in ('runs', 'messages', 'run_receipts', 'model_calls', 'usage_entries'):
                assert not execute(connection, f'SELECT 1 FROM aist.{table} LIMIT 1').first()
        worker_availability.publish(w.runs, 'worker_current')
        assert state() == 'available'
        accepted = post(first, key)
        assert accepted.status_code == 202
        with w.engine.begin() as connection:
            execute(connection, "UPDATE aist.worker_availability SET expires_at=clock_timestamp()-interval '1 second'")
        assert state() == 'temporarily_unavailable'
        assert post(first, key).json() == accepted.json()
        assert post(first, key, dict(command, text='Другое тело')).json()['error']['code'] == 'idempotency_conflict'
        assert post(second, ident('key')).status_code == 503
        with w.engine.connect() as connection:
            assert execute(connection, 'SELECT count(*) FROM aist.run_receipts').scalar_one() == 1
