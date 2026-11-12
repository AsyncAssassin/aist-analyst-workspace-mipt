"""PostgreSQL worker, bounded shutdown and durable startup recovery."""
import json
import logging
import os
import sys
import signal
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from pathlib import Path
from sqlalchemy import event
from .config import Settings
from .database import make_engine,probe_database
from .service import Service,row
from .storage import Storage
from .runs import Runs,execute,json_value
from .executor import ScriptedExecutor,dispatch_cancel,qa_arguments
from .transport import Transport,interrupt_connection
from .errors import ApiError
from . import worker_availability
from .diagnostics import failure_message, write_failure


log = logging.getLogger(__name__)

SYNTHETIC_PAYLOAD={'result_type':'evidence_answer','schema_version':'1.0.0','content':{'claims':[],'missing_information':['Синтетический результат D07. Финансовая аналитика не выполнялась.']}}

# One finalization budget for persistence, snapshot and all readers together.
# A connect/pool checkout already started may take its existing 3-second limit.
FINALIZATION_SECONDS=5
READY_FILE = Path('/tmp/aist-worker-ready')


class Worker:
    def __init__(self,settings=None):
        self.settings=settings or Settings();self.engine=make_engine(self.settings)
        self.runs=Runs(Service(self.engine,Storage(self.settings.storage_root)),self.settings)
        from .exports import Exports
        self.exports=Exports(self.runs);self.export_thread=None
        self.id='worker_'+__import__('uuid').uuid4().hex
        self.draining=threading.Event();self.closed=threading.Event();self.persistence=threading.RLock()
        self.connection_lock=threading.Lock();self.connections={};self.jobs={};self.deadline=None
        self.final_deadline=None
        self.executor_failed = threading.Event()
        event.listen(self.engine,'before_cursor_execute',self.bound_statement)
        self.transport=Transport(self.runs,self.draining,self.closed,self.persistence,self.connections,self.connection_lock)

    def signal(self,*_):
        if not self.draining.is_set():
            self.deadline=time.monotonic()+self.settings.shutdown_seconds
            self.final_deadline=self.deadline+FINALIZATION_SECONDS;self.draining.set()

    def bound_statement(self,connection,cursor,statement,parameters,context,executemany):
        if self.final_deadline is None:return
        remaining=self.final_deadline-time.monotonic()
        if remaining<=0:raise ApiError('outcome_unknown')
        # SET LOCAL applies to this transaction only, including lock waits.
        cursor.execute('SET LOCAL statement_timeout = %s',(max(1,min(3000,int(remaining*1000))),))

    def job(self,r):
        context = dict(worker_id=self.id, run_id=r['id'], epoch=r['epoch'])
        stage = 'run_prepare'
        try:
            from .note_external import prepare_all
            prepare_all(self.runs,r)
            if self.settings.executor_mode=='openclaw':
                stage = 'run_openclaw'
                from .openclaw_runtime import run
                run(self,r)
                return
            stage = 'run_scripted'
            adapter=ScriptedExecutor(self.settings);cp=adapter.start(r)
            with self.persistence:
                if self.closed.is_set():return
                self.runs.checkpoint(r['id'],r['epoch'],cp)
            parent=None
            # Predetermined test actions only. OpenClaw owns the actual reasoning/tool loop.
            while cp['index']<len(cp['steps']) and not self.closed.is_set():
                step=cp['steps'][cp['index']];kind=step['kind']
                if kind=='model':
                    if self.draining.is_set():return
                    parent=self.transport.perform(r,step,parent)
                    if parent is None:return
                else:
                    with self.persistence:
                        if self.closed.is_set() or (self.draining.is_set() and kind!='save'):return
                        with self.engine.connect() as c:current=row(c,'SELECT * FROM aist.runs WHERE id=:r',r=r['id'])
                        eid='qa_'+r['id']+'_'+str(cp['index'])
                        if kind=='wait':
                            cp['index']+=1;cp['safe_resume']=True
                            self.runs.checkpoint(r['id'],r['epoch'],cp)
                            self.runs.tool(r['id'],r['epoch'],eid,'aist_request_input',{'idempotency_key':eid,'expected_revision':current['revision'],'prompt':step.get('prompt','Уточните синтетический пример.')},parent)
                            return
                        if kind=='tool':
                            reply=self.runs.tool(r['id'],r['epoch'],eid,'aist_read_source',{'source_version_id':r['input']['source_versions'][0]['source_version_id'],'page':step.get('page',1)},parent)
                        elif kind=='tool_call':
                            arguments=qa_arguments(step['args'],cp.get('results',{}))
                            reply=self.runs.tool(r['id'],r['epoch'],eid,step['name'],arguments,parent)
                            if reply['ok']:
                                cp.setdefault('results',{})[str(cp['index'])]=reply['result']
                                if step['name']=='aist_save_artifact':return
                        elif kind=='save':
                            reply=self.runs.tool(r['id'],r['epoch'],eid,'aist_save_artifact',{'idempotency_key':eid,'expected_revision':0,'artifact_id':None,'parent_version_id':None,'payload':SYNTHETIC_PAYLOAD},parent)
                        else:raise ApiError('validation_error')
                        if not reply['ok']:return
                        if kind=='save':return
                cp['index']+=1
                with self.persistence:
                    if self.closed.is_set():return
                    self.runs.checkpoint(r['id'],r['epoch'],cp)
        except ApiError as error:
            if isinstance(error, worker_availability.GatewayStartupError):
                self.executor_failed.set()
            code, body = error.code, error.body
            failure = failure_message('worker_failure', stage, code=code, **context) if error.status >= 500 else None
        except Exception as error:
            code, body = 'temporarily_unavailable', None
            failure = failure_message('worker_failure', stage, error, code=code, **context)
        else:
            return
        write_failure(log, failure)

        if body is not None and self.settings.executor_mode=='openclaw':
            try:
                p=Path(self.settings.openclaw_root)/r['id']/'failure.json'
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(json.dumps(body,ensure_ascii=False)+'\n')
            except Exception as error:
                failure = failure_message('worker_failure', 'run_artifact', error, code=code, **context)
            else:
                failure = None
            if failure:
                write_failure(log, failure)
                return

        with self.persistence:
            if self.closed.is_set():
                return
            stage = 'run_stop'
            try:
                if code=='outcome_unknown' and self.settings.executor_mode=='openclaw':
                    stage = 'run_recover'
                    self.runs.recover(owned=self.id)
                elif code not in ('outcome_unknown','run_terminal'):
                    self.runs.stop(r['id'],r['epoch'],code)
            except Exception as error:
                failure = failure_message('worker_failure', stage, error, code=code, **context)
            else:
                failure = None
        write_failure(log, failure)

    def export_job(self):
        try:
            self.exports.process_one()
        except Exception as error:
            failure = failure_message('worker_failure', 'export', error, worker_id=self.id)
        else:
            return
        write_failure(log, failure)

    def snapshot(self,saved,reason=None):
        result={'worker_id':self.id,'saved':saved,'synthetic':self.settings.executor_mode=='qa-scripted','runs':[],'calls':[]}
        if saved:
            with self.engine.begin() as c:
                execute(c,'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                result['runs']=[{'id':v['id'],'state':v['state'],'epoch':v['epoch'],'last_event_seq':v['last_event_seq']} for v in execute(c,'SELECT * FROM aist.runs ORDER BY id').mappings()]
                result['calls']=[v[0] for v in execute(c,'SELECT data FROM aist.model_calls ORDER BY id')]
        else:result['reason']=reason or 'database unavailable; startup reconciliation required'
        p=Path(self.settings.snapshot_path);p.parent.mkdir(parents=True,exist_ok=True)
        temp=p.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');temp.replace(p)
        return result

    def check_executor_files(self):
        from .openclaw_runtime import CLI

        if (not os.access(CLI, os.X_OK) or
                not os.access(self.settings.openclaw_root, os.W_OK | os.X_OK)):
            raise RuntimeError('OpenClaw executable or work directory unavailable')
        from .live_profile import profile_for

        protocol = profile_for({'model_profile_version_id': self.settings.model_profile_id})['transport']
        key = {'https_openai_responses': self.settings.openai_secret_path,
               'https_anthropic_messages': self.settings.anthropic_secret_path,
               'https_chat_completions': self.settings.novita_secret_path}[protocol]
        if not Path(key).is_file() or not os.access(key, os.R_OK):
            raise RuntimeError('Model credential file unavailable')

    def check_executor_start(self):
        from .openclaw_runtime import CLI, environment

        self.check_executor_files()
        with TemporaryDirectory(prefix='check-', dir=self.settings.openclaw_root) as directory:
            subprocess.run([CLI, '--version'], env=environment(Path(directory), ''), timeout=12,
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write_ready(self):
        if self.settings.executor_mode == 'openclaw' and self.settings.live_models_enabled:
            if self.draining.is_set() or self.executor_failed.is_set():
                return
            worker_availability.publish(self.runs, self.id)
        temporary = READY_FILE.with_suffix('.tmp')
        temporary.write_text(f'{os.getpid()} {time.monotonic()}\n')
        temporary.replace(READY_FILE)

    def step(self, stage, action, **context):
        try:
            action()
        except Exception as error:
            write_failure(log, failure_message('worker_failure', stage, error, worker_id=self.id, **context))
            return False
        return True

    def start_export(self):
        if self.export_thread is None or not self.export_thread.is_alive():
            self.export_thread = threading.Thread(target=self.export_job, name='export', daemon=True)
            self.export_thread.start()

    def dispatch_pending_cancels(self):
        if self.settings.executor_mode != 'qa-scripted':
            return
        with self.engine.connect() as c:
            pending = execute(c, '''SELECT id FROM aist.runs WHERE state='cancelled'
                AND worker_id=:worker AND lease_until>clock_timestamp()
                AND executor_run_id IS NOT NULL AND cancel_dispatched_at IS NULL''', worker=self.id).scalars().all()
        for rid in pending:
            self.step('cancel', lambda: dispatch_cancel(self.runs, rid), run_id=rid)

    def start_run(self):
        capacity = {'openclaw': 1, 'qa-scripted': 4}.get(self.settings.executor_mode, 0)
        if (self.executor_failed.is_set() or len(self.jobs) >= capacity
                or (self.settings.executor_mode == 'openclaw' and not self.settings.live_models_enabled)):
            return
        run = self.runs.claim(self.id)
        if run:
            rid = run['id']
            thread = threading.Thread(target=self.job, args=(run,), name=rid)
            thread.start()
            self.jobs[rid] = (thread, run)

    def tick(self):
        # Service owned jobs even if another run's recovery or export fails.
        for rid, (thread, run) in list(self.jobs.items()):
            epoch = run['epoch']
            if thread.is_alive():
                self.step('heartbeat', lambda: self.runs.heartbeat(rid, epoch, self.id), run_id=rid, epoch=epoch)
            elif self.step('run_release', lambda: self.runs.release_finished(rid, epoch, self.id),
                           run_id=rid, epoch=epoch):
                self.jobs.pop(rid)
        self.step('recover', self.runs.recover)
        self.step('export_start', self.start_export)
        self.step('cancel_lookup', self.dispatch_pending_cancels)
        return self.step('claim', self.start_run)

    def run(self):
        signal.signal(signal.SIGTERM,self.signal);signal.signal(signal.SIGINT,self.signal)
        stage = 'startup_probe'
        try:
            if probe_database(self.engine)['reason'] is not None:
                raise RuntimeError('worker database not ready')
            stage = 'startup_recover'
            self.runs.recover()
            from .received_response import recover_saved_runs
            for result in recover_saved_runs(self.runs):
                for call in result['calls']:
                    log.info(failure_message('response_recovery', 'startup_recover',
                                             run_id=result['run_id'], operation=call['call_id'], status=call['status']))
        except Exception as error:
            failure = failure_message('worker_failure', stage, error, worker_id=self.id)
        else:
            failure = None
        if failure:
            write_failure(log, failure)
            self.engine.dispose()
            return 1
        live_executor = self.settings.executor_mode == 'openclaw' and self.settings.live_models_enabled
        if live_executor and not self.step('startup_executor', self.check_executor_start):
            self.executor_failed.set()
        next_ready_update = 0
        withdrawn = False
        while not self.draining.is_set():
            update_ready = time.monotonic() >= next_ready_update
            if live_executor and update_ready and not self.executor_failed.is_set():
                if not self.step('executor_files', self.check_executor_files):
                    self.executor_failed.set()
            tick_ok = self.tick()
            if self.executor_failed.is_set():
                if not withdrawn:
                    READY_FILE.unlink(missing_ok=True)
                    withdrawn = self.step('readiness_withdraw', lambda: worker_availability.withdraw(self.runs, self.id))
            elif tick_ok and update_ready:
                self.step('readiness', self.write_ready)
                next_ready_update = time.monotonic() + 1
            self.draining.wait(.2)
        READY_FILE.unlink(missing_ok=True)
        if self.settings.executor_mode == 'openclaw' and not withdrawn:
            self.step('readiness_withdraw', lambda: worker_availability.withdraw(self.runs, self.id))
        while (any(t.is_alive() for t,r in self.jobs.values()) or (self.export_thread and self.export_thread.is_alive())) and time.monotonic()<self.deadline:
            try:
                for rid,(t,r) in self.jobs.items():
                    if time.monotonic()>=self.deadline:break
                    if t.is_alive():self.runs.heartbeat(rid,r['epoch'],self.id)
            except Exception as error:
                failure = failure_message('worker_failure', 'shutdown_heartbeat', error,
                                          worker_id=self.id, run_id=rid, epoch=r['epoch'])
            else:
                failure = None
            write_failure(log, failure)
            time.sleep(.05)
        # Serialize the fence and final snapshot with every own persistence action.
        saved=False
        self.closed.set()
        acquired=self.persistence.acquire(timeout=max(0,self.final_deadline-time.monotonic()))
        failures = []
        try:
            if acquired:
                try:
                    self.runs.recover(owned=self.id)
                    saved=True
                except Exception as error:
                    failures.append(failure_message('worker_failure', 'shutdown_recover', error, worker_id=self.id))
            try:
                if acquired:
                    self.snapshot(saved)
                else:
                    self.snapshot(False,'shutdown persistence deadline; startup reconciliation required')
            except Exception as error:
                failures.append(failure_message('worker_failure', 'shutdown_snapshot', error, worker_id=self.id))
                saved=False
                if acquired:
                    try:
                        self.snapshot(False)
                    except Exception as error:
                        failures.append(failure_message('worker_failure', 'shutdown_snapshot_fallback',
                                                        error, worker_id=self.id))
        finally:
            if acquired:self.persistence.release()
        for failure in failures:
            write_failure(log, failure)
        with self.connection_lock:
            for c in list(self.connections.values()):interrupt_connection(c)
        for t,r in self.jobs.values():t.join(timeout=max(0,self.final_deadline-time.monotonic()))
        self.engine.dispose()
        return 0 if saved else 2


def readiness(settings):
    try:
        pid, updated = READY_FILE.read_text().split()
        if not 0 <= time.monotonic() - float(updated) < 12:
            return False
        os.kill(int(pid), 0)
        if settings.executor_mode == 'openclaw':
            from .openclaw_runtime import CLI
            if not os.access(CLI, os.X_OK) or not os.access(settings.openclaw_root, os.W_OK | os.X_OK):
                return False
    except (OSError, ValueError):
        return False
    engine = make_engine(settings)
    try:
        return probe_database(engine)['reason'] is None
    finally:
        engine.dispose()


def main():
    if sys.argv[1:] == ['--ready']:
        raise SystemExit(0 if readiness(Settings()) else 1)
    raise SystemExit(Worker().run())


if __name__=='__main__':main()
