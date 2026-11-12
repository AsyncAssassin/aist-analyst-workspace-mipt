"""Парольный вход: реальная PostgreSQL, изолированная БД, без провайдеров."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import secrets
import threading

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app import auth
from app.errors import ApiError
from app.main import create_app
from test_d15_postgres import workspace, upload

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.fixture
def accounts(workspace):
    w = workspace
    w.password = '  Decomposed-e\u0301\r\nPassword-123456  '
    w.expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    with w.engine.begin() as connection:
        w.user = auth.create_account(connection, ' Tester01 ', w.password, w.expiry)
        w.other = auth.create_account(connection, 'tester02', secrets.token_urlsafe(32), w.expiry)
    settings = replace(w.settings, executor_mode='disabled', live_models_enabled=False,
                       embeddings_enabled=False, policy_path='')
    with TestClient(create_app(settings)) as client:
        w.client = client
        yield w


def sign_in(w, password=None, name='tester01'):
    return w.client.post('/v1/auth/login', json={'login': name, 'password': w.password if password is None else password})


def bearer(response):
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_password_exact_session_deadline_and_server_logout(accounts):
    w = accounts
    first = sign_in(w, name=' Tester01 ')
    headers = bearer(first)
    assert datetime.fromisoformat(first.json()['expires_at'].replace('Z', '+00:00')) <= w.expiry
    assert w.client.get('/v1/projects', headers=headers).status_code == 200
    assert sign_in(w, w.password.strip()).status_code == 401
    assert sign_in(w, w.password.replace('e\u0301', '\u00e9')).status_code == 401
    assert sign_in(w, w.password.replace('\r\n', '\n')).status_code == 401
    with w.engine.connect() as connection:
        record = connection.execute(text('SELECT password_hash FROM aist.users WHERE id=:id'), {'id': w.user}).scalar_one()
        assert record.startswith('pbkdf2_sha256$600000$') and w.password not in record
        assert connection.execute(text('SELECT token_hash FROM aist.sessions WHERE id=:id'), {'id': first.json()['session_id']}).scalar_one() == auth.token_hash(first.json()['token'])
    assert w.client.post('/v1/auth/logout', json={}, headers=headers).json() == {'revoked': True}
    assert w.client.get('/v1/projects', headers=headers).status_code == 401


def test_failure_messages_and_operator_revocation(accounts):
    w = accounts
    headers = bearer(sign_in(w))
    failures = [sign_in(w, 'wrong'), sign_in(w, name='unknown')]
    with w.engine.begin() as connection:
        auth.change_account(connection, 'tester01', 'New-operator-password-123456')
    assert w.client.get('/v1/projects', headers=headers).status_code == 401
    failures.append(sign_in(w))
    headers = bearer(sign_in(w, 'New-operator-password-123456'))
    with w.engine.begin() as connection:
        auth.change_account(connection, 'tester01')
    failures.append(sign_in(w, 'New-operator-password-123456'))
    assert w.client.get('/v1/projects', headers=headers).status_code == 401
    with w.engine.begin() as connection:
        connection.execute(text('UPDATE aist.users SET disabled_at=NULL,access_expires_at=clock_timestamp()-interval \'1 second\' WHERE id=:id'), {'id': w.user})
    failures.append(sign_in(w, 'New-operator-password-123456'))
    assert all(r.status_code == 401 for r in failures)
    assert len({r.json()['error']['message'] for r in failures}) == 1
    with w.engine.begin() as connection:
        with pytest.raises(ApiError, match='invalid_credentials'):
            auth.issue_session(connection, w.user)


@pytest.mark.parametrize('change', ['disable', 'password'])
def test_revocation_wins_against_password_check_in_flight(accounts, monkeypatch, change):
    w = accounts
    checked, proceed = threading.Event(), threading.Event()
    original = auth.verify_password

    def paused(password, stored):
        result = original(password, stored)
        checked.set()
        assert proceed.wait(10)
        return result

    monkeypatch.setattr(auth, 'verify_password', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(auth.login, w.engine, auth.LoginLimits(), {'login': 'tester01', 'password': w.password})
        assert checked.wait(10)
        try:
            with w.engine.begin() as connection:
                auth.change_account(connection, 'tester01', None if change == 'disable' else 'replacement-password-123456')
        finally:
            proceed.set()
        with pytest.raises(ApiError) as error:
            pending.result(timeout=10)
        assert error.value.code == 'invalid_credentials'
    with w.engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM aist.sessions WHERE user_id=:id'), {'id': w.user}).scalar_one() == 0


def test_size_exact_route_and_bounded_expensive_checks(accounts, monkeypatch):
    w = accounts
    def unexpected(*args):
        raise AssertionError('KDF must not run')
    monkeypatch.setattr(auth, 'verify_password', unexpected)
    for payload in ({'login':'x'*65,'password':'x'}, {'login':'tester01','password':'x'*257}, {'login':'tester01','password':'x','extra':True}):
        assert w.client.post('/v1/auth/login', json=payload).status_code == 422
    assert w.client.post('/v1/auth/login', content=b' '*4097, headers={'Content-Type':'application/json'}).status_code == 413
    assert w.client.post('/v1/auth/login', content=iter([b' '*2048,b' '*2049]), headers={'Content-Type':'application/json'}).status_code == 413
    for method, path in [('GET','/v1/auth/login'),('POST','/v1/auth/login/'),('POST','/v1/auth/logout'),('POST','/v1/auth/login/anything')]:
        assert w.client.request(method, path).status_code == 401
    limits=w.client.app.state.login_limits
    for _ in range(8): limits.check('tester01')
    assert sign_in(w).status_code == 429
    assert w.client.post('/v1/auth/login', json={'login':'tester01','password':w.password}, headers={'X-Forwarded-For':'different-address'}).status_code == 429
    assert limits.verifications.acquire(False) and limits.verifications.acquire(False)
    try:
        assert sign_in(w, name='another').status_code == 429
    finally:
        limits.verifications.release();limits.verifications.release()
    global_limit=auth.LoginLimits()
    for i in range(60): global_limit.check(str(i))
    with pytest.raises(ApiError) as error: global_limit.check('new')
    assert error.value.code=='login_rate_limited'


def test_owner_isolation_legacy_bearer_and_disabled_generation(accounts):
    w = accounts
    source=upload(w)['version']
    with w.engine.begin() as connection:
        legacy=auth.issue_session(connection, w.actor)
    own={'Authorization':'Bearer '+legacy['token']}
    other=bearer(sign_in(w))
    for path in (f'/v1/projects/{w.project}', f'/v1/projects/{w.project}/source-versions/{source["id"]}/file'):
        assert w.client.get(path,headers=other).status_code==404
        assert w.client.get(path).status_code==401
    assert w.client.get(f'/v1/projects/{w.project}',headers=own).status_code==200
    _, conversation = w.service.write(w.actor, 'createConversation', 'test-conv', {'title':'No model'}, w.project)
    payload=dict(expected_revision=0,text='Не запускать модель',source_version_ids=[source['id']],
                 skill_version_id='skill_analytical-note-v3',model_profile_version_id='model_anthropic-fable-xhigh-v1',
                 requested_result_type='analytical_note',parameters={'audience':None,'sections':[]},base_version_id=None,
                 expected_artifact_revision=0,requested_export=None)
    response=w.client.post(f'/v1/projects/{w.project}/conversations/{conversation["id"]}/messages',json=payload,
                           headers={**own,'Idempotency-Key':'no-generation'})
    assert response.status_code==409 and response.json()['error']['code']=='tool_unavailable'
    with w.engine.connect() as connection:
        for table in ('runs','messages','model_calls','embedding_calls'):
            assert connection.execute(text(f'SELECT count(*) FROM aist.{table}')).scalar_one()==0
