"""Проверка срока и остановки настоящего локального дочернего процесса при старте gateway."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import sys
import time
import pytest
from app import openclaw_runtime as runtime, note_external
import os
from app.errors import ApiError
from app.runs import execute
from test_d15_postgres import workspace
from test_fable_postgres import start_fable
from test_flow01_postgres import executor

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL')

@pytest.mark.parametrize('stop', ['deadline','closed','cancel','epoch','draining'])
def test_gateway_start_is_bounded_by_run_and_stop(workspace,monkeypatch,stop):
    w=workspace
    _,_,run=start_fable(w,full=True,research=True)
    note_external.prepare_all(w.runs,run)
    with TemporaryDirectory(prefix='gw-',dir='/data/execution') as root:
        script=Path(root)/'slow-gateway'
        script.write_text('#!/usr/local/bin/python\nimport time\ntime.sleep(3)\n')
        script.chmod(0o700)
        original_popen=runtime.subprocess.Popen
        monkeypatch.setattr(runtime.subprocess,'Popen',lambda argv,**kw: original_popen([sys.executable,str(script)],**kw))
        w.runs.settings=replace(w.settings,openclaw_root=root)
        worker=executor(w)
        if stop=='deadline':
            anchor=time.monotonic()
            monkeypatch.setattr(runtime,'age_seconds',lambda current: current['input']['budget']['max_duration_seconds']-.7+time.monotonic()-anchor)
            callback=lambda: None
        elif stop=='closed': callback=worker.closed.set
        elif stop=='draining': callback=worker.draining.set
        elif stop=='epoch':
            def callback():
                with w.engine.begin() as c: execute(c,'UPDATE aist.runs SET epoch=epoch+1 WHERE id=:r',r=run['id'])
        else:
            def callback():
                with w.engine.begin() as c: execute(c,"UPDATE aist.runs SET state='cancelled' WHERE id=:r",r=run['id'])
        timer=threading.Timer(.25,callback);timer.start()
        started=time.monotonic();error=None
        try:
            with pytest.raises(ApiError) as failure:
                directory = Path(root) / 'gateway'
                directory.mkdir()
                boundary = runtime.Boundary(worker, run, directory, 'synthetic')
                with runtime.running_gateway(worker, boundary, worker.settings, directory,
                                             runtime.environment(directory, 'synthetic')):
                    pytest.fail('Медленный gateway не должен завершить запуск после остановки Run')
            error=failure.value.code
        finally:
            timer.cancel();timer.join()
        elapsed=time.monotonic()-started
        assert elapsed < 2, f'Gateway продолжал запуск после {stop}: {elapsed:.2f} с, {error}'
