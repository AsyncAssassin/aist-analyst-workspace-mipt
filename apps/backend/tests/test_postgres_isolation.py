"""Recovery must stay in the workspace database through new services, workers and processes."""
from dataclasses import replace
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from app.config import Settings
from app.database import make_engine
from app.runs import Runs
from app.worker import Worker
from test_d15_postgres import invalidate, isolated_database, start_run, upload, workspace

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def recover_with_worker(worker, expected_database):
    previous_signals = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        with worker.engine.connect() as connection:
            assert connection.execute(text('SELECT current_database()')).scalar_one() == expected_database
        # Exercise real startup and shutdown recovery without starting jobs or sending provider requests.
        worker.signal()
        assert worker.run() == 0
    finally:
        for number, handler in previous_signals.items():
            signal.signal(number, handler)
        worker.engine.dispose()


@pytest.mark.parametrize('entry', ['fixture', 'new_runs', 'class_method', 'worker_settings', 'worker_defaults', 'child_worker'])
def test_recovery_stays_in_its_database_after_monkeypatch_undo(request, monkeypatch, entry):
    original_database = Settings().db_name
    w = request.getfixturevalue('workspace')
    database = w.settings.db_name
    # Fail before any recovery if isolation regresses; never use the shared database as the destructive probe.
    assert database != original_database and database.startswith('aist_d15_r1_test_workspace_')
    monkeypatch.undo()
    assert Settings().database_url() == w.settings.database_url()
    with w.engine.connect() as connection:
        assert connection.execute(text('SELECT current_database()')).scalar_one() == database
        assert connection.execute(text('SELECT count(*) FROM aist.runs')).scalar_one() == 0
    run = start_run(w, upload(w)['version'])
    invalidate(w, run, 'lease')

    if entry == 'fixture':
        w.runs.recover()
    elif entry == 'new_runs':
        w.runs = Runs(w.service, w.settings)
        w.runs.recover(owned='another-worker')
    elif entry == 'class_method':
        Runs.recover(w.runs)
    elif entry == 'worker_settings':
        recover_with_worker(Worker(w.settings), database)
    elif entry == 'worker_defaults':
        recover_with_worker(Worker(), database)
    else:
        child = subprocess.run([sys.executable, __file__, database], check=True, capture_output=True, text=True, timeout=20)
        assert json.loads(child.stdout) == {'database': database, 'recovered': True}

    with w.engine.connect() as connection:
        saved = connection.execute(text('SELECT state,epoch,worker_id,lease_until FROM aist.runs WHERE id=:id'),
                                   {'id': run['id']}).one()
        assert tuple(saved) == ('recovery_required', run['epoch'] + 1, None, None)


@pytest.mark.parametrize('failure_event, created', [('before_cursor_execute', False), ('after_cursor_execute', True)])
def test_create_database_error_cleans_up_even_after_server_success(monkeypatch, failure_event, created):
    names = ('AIST_DB_NAME', 'AIST_STORAGE_ROOT', 'AIST_EXPORT_ROOT', 'AIST_SNAPSHOT_PATH')
    original = {name: os.environ.get(name) for name in names}
    settings = Settings()
    admin = make_engine(replace(settings, db_name='postgres'))
    finalizers, databases = [], []

    def fail_create(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('CREATE DATABASE '):
            database = statement.split('"')[1]
            assert database != settings.db_name and database.startswith('aist_d15_r1_test_workspace_')
            databases.append(database)
            raise RuntimeError('simulated CREATE DATABASE error')

    event.listen(admin, failure_event, fail_create)
    monkeypatch.setattr('test_d15_postgres.make_engine', lambda settings: admin)
    try:
        try:
            with pytest.raises(RuntimeError, match='simulated CREATE DATABASE error'):
                isolated_database(settings, SimpleNamespace(addfinalizer=finalizers.append))
            database, = databases
            with admin.connect() as connection:
                assert connection.execute(text('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)'),
                                          {'name': database}).scalar_one() is created
        finally:
            for finalize in reversed(finalizers):
                finalize()

        assert {name: os.environ.get(name) for name in names} == original
        with admin.connect() as connection:
            assert not connection.execute(text('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)'),
                                          {'name': database}).scalar_one()
    finally:
        # A failing regression must not leave its own database behind; assert cleanup before this fallback.
        try:
            for database in databases:
                with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                    connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
        finally:
            admin.dispose()


@pytest.mark.parametrize('failure', ['migration', 'test_body'])
def test_failed_workspace_drops_only_its_database_and_restores_environment(request, tmp_path, monkeypatch, failure):
    names = ('AIST_DB_NAME', 'AIST_STORAGE_ROOT', 'AIST_EXPORT_ROOT', 'AIST_SNAPSHOT_PATH')
    original = {name: os.environ.get(name) for name in names}
    finalizers, databases = [], []
    fixture_request = SimpleNamespace(addfinalizer=finalizers.append, node=request.node)

    if failure == 'migration':
        def fail_migration(*args):
            databases.append(Settings().db_name)
            raise RuntimeError('simulated migration failure')
        monkeypatch.setattr('test_d15_postgres.command.upgrade', fail_migration)

    with pytest.raises(RuntimeError, match='simulated'):
        try:
            w = workspace.__wrapped__(tmp_path, monkeypatch, fixture_request)
            databases.append(w.settings.db_name)
            # Test overrides must unwind before the fixture restores the enclosing environment.
            monkeypatch.setenv('AIST_DB_NAME', 'aist_d15_r1_test_unused')
            raise RuntimeError('simulated test failure')
        finally:
            for finalize in reversed(finalizers):
                finalize()

    assert {name: os.environ.get(name) for name in names} == original
    database, = databases
    assert database != original['AIST_DB_NAME']
    admin = make_engine(replace(Settings(), db_name='postgres'))
    try:
        with admin.connect() as connection:
            assert not connection.execute(text('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)'),
                                          {'name': database}).scalar_one()
    finally:
        admin.dispose()


if __name__ == '__main__':
    expected_database = sys.argv[1]
    assert Settings().db_name == expected_database
    recover_with_worker(Worker(), expected_database)
    print(json.dumps({'database': expected_database, 'recovered': True}))
