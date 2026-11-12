"""Краткоживущий сигнал допуска к модельным операциям в общей БД."""
from sqlalchemy import text

from .errors import ApiError
from .normalization import fingerprint


TTL_SECONDS = 12


class GatewayStartupError(ApiError):
    def __init__(self):
        super().__init__('temporarily_unavailable')


def compatibility(runs):
    # Политика допуска владельцев меняется без изменения профиля исполнителя.
    policy = {key: value for key, value in runs.policy.items() if key != 'demo_access'}
    return fingerprint(dict(protocol=1, executor=runs.settings.executor_mode,
                            profile=runs.settings.model_profile_id, registry=runs.registry, policy=policy))


def available(connection, runs):
    return connection.execute(text("""
        SELECT EXISTS (SELECT 1 FROM aist.worker_availability
            WHERE compatibility_sha256=:sha AND expires_at>clock_timestamp())
    """), {'sha': compatibility(runs)}).scalar_one()


def publish(runs, worker_id):
    with runs.engine.begin() as connection:
        connection.execute(text('DELETE FROM aist.worker_availability WHERE expires_at<=clock_timestamp()'))
        connection.execute(text("""
            INSERT INTO aist.worker_availability(worker_id, compatibility_sha256, expires_at)
            VALUES (:id,:sha,clock_timestamp()+:ttl*interval '1 second')
            ON CONFLICT (worker_id) DO UPDATE SET
              compatibility_sha256=excluded.compatibility_sha256, expires_at=excluded.expires_at
        """), {'id': worker_id, 'sha': compatibility(runs), 'ttl': TTL_SECONDS})


def withdraw(runs, worker_id):
    with runs.engine.begin() as connection:
        connection.execute(text('DELETE FROM aist.worker_availability WHERE worker_id=:id'), {'id': worker_id})
