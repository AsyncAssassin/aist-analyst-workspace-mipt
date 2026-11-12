from alembic import context
from app.config import Settings
from app.database import make_engine

engine = make_engine(Settings())
try:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET lock_timeout = '3s'")
        connection.exec_driver_sql("SET statement_timeout = '60s'")
        connection.commit()
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
