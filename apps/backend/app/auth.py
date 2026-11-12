"""Парольный вход и серверные сессии. Открытые секреты в БД не сохраняются."""
from collections import deque
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid

from sqlalchemy import text
from .errors import ApiError

TOKEN = re.compile(r'^Bearer (aist_[A-Za-z0-9_-]{43})$', re.IGNORECASE)
LOGIN = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
ITERATIONS = 600000
# Неизвестный логин проходит ту же дорогую проверку, что существующий.
DUMMY_HASH = 'pbkdf2_sha256$600000$' + '00' * 16 + '$' + '00' * 32


def token_hash(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def normalize_login(login):
    if not isinstance(login, str) or len(login) > 64:
        raise ApiError('validation_error')
    normalized = login.strip().lower()
    if not LOGIN.fullmatch(normalized):
        raise ApiError('validation_error')
    return normalized


def hash_password(password):
    if not isinstance(password, str) or not 20 <= len(password) <= 256:
        raise ApiError('validation_error')
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${derived.hex()}'


def verify_password(password, stored):
    algorithm, iterations, salt, expected = stored.split('$')
    if algorithm != 'pbkdf2_sha256' or int(iterations) != ITERATIONS:
        return False
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(derived, bytes.fromhex(expected))


class LoginLimits:
    """Один процесс: 60 попыток/мин, 8/5 мин на логин, две KDF сразу.

    Общий лимит действует и за proxy, без доверия к X-Forwarded-For.
    Память ограничена общим числом попыток за окно, перезапуск сбрасывает окна.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = deque()
        self.verifications = threading.BoundedSemaphore(2)

    def check(self, login):
        now = time.monotonic()
        with self.lock:
            while self.attempts and self.attempts[0][0] <= now - 300:
                self.attempts.popleft()
            recent = sum(at > now - 60 for at, _ in self.attempts)
            matching = sum(name == login for _, name in self.attempts)
            if recent >= 60 or matching >= 8:
                raise ApiError('login_rate_limited')
            self.attempts.append((now, login))


def issue_session(connection, user_id, ttl_seconds=86400):
    user = connection.execute(text('''SELECT *, clock_timestamp() AS now FROM aist.users
        WHERE id=:id FOR UPDATE'''), {'id': user_id}).mappings().one_or_none()
    if (user is None or user['disabled_at'] is not None or
            (user['access_expires_at'] is not None and user['access_expires_at'] <= user['now'])):
        raise ApiError('invalid_credentials')
    token = 'aist_' + secrets.token_urlsafe(32)
    session = 'ses_' + uuid.uuid4().hex
    expires = connection.execute(text('''INSERT INTO aist.sessions(id,user_id,token_hash,expires_at)
        VALUES (:id,:user,:hash,LEAST(clock_timestamp()+make_interval(secs=>:ttl),:expires))
        RETURNING expires_at'''), {'id': session, 'user': user_id, 'hash': token_hash(token),
                                   'ttl': ttl_seconds, 'expires': user['access_expires_at']}).scalar_one()
    return {'session_id': session, 'token': token, 'expires_at': expires.isoformat().replace('+00:00', 'Z')}


def login(engine, limits, payload):
    name = normalize_login(payload['login'])
    password = payload['password']
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise ApiError('validation_error')
    limits.check(name)
    if not limits.verifications.acquire(blocking=False):
        raise ApiError('login_rate_limited')
    try:
        with engine.connect() as connection:
            user = connection.execute(text('SELECT id,password_hash FROM aist.users WHERE login=:login'),
                                      {'login': name}).mappings().one_or_none()
        stored = user['password_hash'] if user else DUMMY_HASH
        valid = verify_password(password, stored)
        if not valid or user is None:
            raise ApiError('invalid_credentials')
        with engine.begin() as connection:
            # Та же блокировка используется отключением и сменой пароля.
            # Проверка вне транзакции не позволяет выдать сессию после отзыва.
            current = connection.execute(text('SELECT password_hash FROM aist.users WHERE id=:id FOR UPDATE'),
                                         {'id': user['id']}).scalar_one()
            if not hmac.compare_digest(current, stored):
                raise ApiError('invalid_credentials')
            return issue_session(connection, user['id'])
    finally:
        limits.verifications.release()


def create_account(connection, login_name, password, expires_at, label=None):
    name = normalize_login(login_name)
    password_hash = hash_password(password)
    user = 'usr_' + uuid.uuid4().hex
    connection.execute(text('''INSERT INTO aist.users(id,label,login,password_hash,access_expires_at)
        VALUES (:id,:label,:login,:hash,:expires)'''),
        {'id': user, 'label': label or name, 'login': name, 'hash': password_hash, 'expires': expires_at})
    return user


def change_account(connection, login_name, password=None):
    name = normalize_login(login_name)
    user = connection.execute(text('SELECT id FROM aist.users WHERE login=:login FOR UPDATE'),
                              {'login': name}).scalar_one_or_none()
    if user is None:
        raise ApiError('resource_not_found')
    if password is None:
        connection.execute(text('UPDATE aist.users SET disabled_at=clock_timestamp() WHERE id=:id'), {'id': user})
    else:
        connection.execute(text('UPDATE aist.users SET password_hash=:hash WHERE id=:id'),
                           {'id': user, 'hash': hash_password(password)})
    connection.execute(text('UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE user_id=:id AND revoked_at IS NULL'),
                       {'id': user})


def active_session(engine, authorization):
    match = TOKEN.fullmatch(authorization or '')
    if not match:
        raise ApiError('unauthenticated')
    with engine.connect() as connection:
        session = connection.execute(text('''SELECT s.id,s.user_id FROM aist.sessions s
          JOIN aist.users u ON u.id=s.user_id
          WHERE s.token_hash=:hash AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
          AND u.disabled_at IS NULL AND (u.access_expires_at IS NULL OR u.access_expires_at>clock_timestamp())'''),
          {'hash': token_hash(match[1])}).one_or_none()
    if session is None:
        raise ApiError('unauthenticated')
    return session


def authenticate(engine, authorization):
    return active_session(engine, authorization).user_id


def session_id(engine, authorization):
    return active_session(engine, authorization).id


def logout(engine, session):
    with engine.begin() as connection:
        connection.execute(text('UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:id'), {'id': session})
    return {'revoked': True}
