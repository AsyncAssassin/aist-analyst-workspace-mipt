from pathlib import Path
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError, TimeoutError

ROOT = Path(__file__).resolve().parents[3]


def make_engine(settings):
    return create_engine(settings.database_url(), pool_pre_ping=True,
                         pool_size=2, max_overflow=0, pool_timeout=3,
                         connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"})


def migration_heads():
    return tuple(ScriptDirectory.from_config(Config(str(ROOT / "infra/alembic.ini"))).get_heads())


def probe_database(engine):
    expected = migration_heads()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            actual = tuple(MigrationContext.configure(connection).get_current_heads())
            foundation = connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'aist')"
            )).scalar_one()
    except SQLAlchemyError:
        # Не возвращаем текст драйвера, DSN, hostname или credentials.
        return {"database": "unavailable", "migrations": "unknown", "reason": "database_unavailable"}
    if set(actual) != set(expected) or not foundation:
        return {"database": "available", "migrations": "pending", "reason": "migration_required",
                "expected_revision": list(expected), "applied_revision": list(actual)}
    return {"database": "available", "migrations": "current", "reason": None,
            "expected_revision": list(expected), "applied_revision": list(actual)}


def temporarily_unavailable(error):
    if isinstance(error, TimeoutError):  # SQLAlchemy pool checkout timeout
        return True
    if not isinstance(error, DBAPIError):
        return False
    state = getattr(error.orig, 'pgcode', None)
    if state == '57014':
        return getattr(getattr(error.orig, 'diag', None), 'message_primary', None) == 'canceling statement due to statement timeout'
    if state is not None:
        return state in {
            '08000', '08001', '08003', '08006', '08007', '40001', '40P01', '55P03', '57P01', '57P02', '57P03', '53300',
        }
    if error.connection_invalidated:
        return True
    # libpq does not always provide SQLSTATE when a connection cannot be opened.
    if isinstance(error, OperationalError) and error.statement is None:
        details = str(error.orig).lower()
        return any(cause in details for cause in ('connection refused', 'timeout expired',
                                                   'server closed the connection unexpectedly'))
    return False
