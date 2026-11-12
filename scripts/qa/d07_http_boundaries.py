"""D07-R1 HTTP regressions: real workers, withheld bodies and PostgreSQL assertions."""
import argparse
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal
import httpx
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from app.accounting import Accounting
from app.config import Settings
from app.contracts import validate_definition
from app.database import make_engine
from app.errors import ApiError
from app.runs import Runs
from app.service import Service
from app.storage import Storage
from app.transport import Transport

sys.path.insert(0, '/workspace/apps/backend/tests')
from pdf_fixtures import pdf_bytes

parser = argparse.ArgumentParser()
parser.add_argument('--output', default='/evidence')
args = parser.parse_args()
OUT = Path(args.output); OUT.mkdir(parents=True, exist_ok=True)
settings = Settings()
engine = make_engine(settings)
runs = Runs(Service(engine, Storage(settings.storage_root)), settings)
ledger = Accounting(runs)
observations = []
actor = json.loads(subprocess.check_output([sys.executable, '-m', 'app.cli', 'create-user', '--label', 'Synthetic D07-R1']))
session = json.loads(subprocess.check_output([sys.executable, '-m', 'app.cli', 'issue-session', '--user-id', actor['user_id']]))
headers = {'Authorization': 'Bearer ' + session['token']}
VALID = {'synthetic': True, 'id': 'qa_boundary', 'usage': {'prompt_tokens': 101, 'completion_tokens': 53, 'cached_input': 31, 'reasoning_output': 17}}
EXPECTED_COST = (Decimal(101)*Decimal(runs.policy['input_per_million']) + Decimal(53)*Decimal(runs.policy['output_per_million'])) / Decimal(1000000)


def api(method, path, expected=200, **kwargs):
    h = headers | ({'Idempotency-Key': uuid.uuid4().hex} if method == 'POST' else {})
    response = httpx.request(method, 'http://web:8080/api' + path, headers=h, timeout=20, **kwargs)
    assert response.status_code == expected, (path, response.status_code)
    return response.json()


def setup():
    project = api('POST', '/v1/projects', 201, json={'name': 'Synthetic D07-R1 boundary'})['id']
    source = api('POST', f'/v1/projects/{project}/sources', 201, files=[('metadata', (None, json.dumps({'title': 'Synthetic boundary', 'expected_revision': 0}), 'application/json')), ('file', ('boundary.pdf', pdf_bytes('boundary'), 'application/pdf'))])
    conversation = api('POST', f'/v1/projects/{project}/conversations', 201, json={'title': 'Boundary'})['id']
    body = {'expected_revision': 0, 'text': 'Synthetic HTTP boundary', 'source_version_ids': [source['version']['id']], 'skill_version_id': 'skill_qa-d07-v1', 'model_profile_version_id': 'model_qa-d07-v1', 'requested_result_type': 'evidence_answer', 'parameters': {'audience': None, 'sections': []}, 'base_version_id': None, 'expected_artifact_revision': 0, 'requested_export': None}
    run = api('POST', f'/v1/projects/{project}/conversations/{conversation}/messages', 202, json=body)['run']['id']
    return {'project': project, 'run': run}


def path(ctx, suffix=''):
    return f"/v1/projects/{ctx['project']}/runs/{ctx['run']}" + suffix


def state(ctx):
    with engine.connect() as c:
        r = dict(c.execute(text('SELECT state,epoch,revision,result_version_id,last_event_seq FROM aist.runs WHERE id=:r'), {'r': ctx['run']}).mappings().one())
        r['calls'] = list(c.execute(text('SELECT data FROM aist.model_calls WHERE run_id=:r ORDER BY id'), {'r': ctx['run']}).scalars())
        r['usage'] = list(c.execute(text('SELECT data FROM aist.usage_entries WHERE run_id=:r ORDER BY created_at,id'), {'r': ctx['run']}).scalars())
        r['exposure'] = str(c.execute(text('SELECT coalesce(sum(exposure),0) FROM aist.model_calls WHERE run_id=:r'), {'r': ctx['run']}).scalar_one())
        r['versions'] = c.execute(text("SELECT count(*) FROM aist.artifact_versions WHERE data->'provenance'->>'run_id'=:r"), {'r': ctx['run']}).scalar_one()
        r['assistant_messages'] = c.execute(text("SELECT count(*) FROM aist.messages WHERE run_id=:r AND role='assistant'"), {'r': ctx['run']}).scalar_one()
        return r


def until(fn, seconds=12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = fn()
        if value: return value
        time.sleep(.04)
    raise AssertionError('boundary setup deadline')


def assert_snapshot(name):
    snapshot = json.loads((OUT / (name + '-snapshot.json')).read_text())
    assert snapshot['saved'] is True
    with engine.connect() as c:
        actual_runs = [dict(v) for v in c.execute(text('SELECT id,state,epoch,last_event_seq FROM aist.runs ORDER BY id')).mappings()]
        actual_calls = list(c.execute(text('SELECT data FROM aist.model_calls ORDER BY id')).scalars())
    assert snapshot['runs'] == actual_runs
    assert snapshot['calls'] == actual_calls
    return {'saved': True, 'runs': len(actual_runs), 'calls': len(actual_calls), 'matches_database': True}


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = False
    def __init__(self, mode, payload=VALID, status=200, close=False):
        super().__init__(('127.0.0.1', 0), Handler)
        self.mode = mode; self.payload = payload; self.status = status; self.close = close
        self.posts = 0; self.condition = threading.Condition(); self.ready = 0
        self.cleanup = threading.Event(); self.finished = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever)
        self.thread.start()
    def wait_ready(self, count=1):
        with self.condition:
            assert self.condition.wait_for(lambda: self.ready >= count, timeout=12), 'HTTP request not served'
    def stop(self):
        self.cleanup.set(); self.shutdown(); self.server_close(); self.thread.join(timeout=3)
        assert not self.thread.is_alive()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def do_GET(self):
        body = json.dumps({'steps': [{'kind': 'model'}, {'kind': 'save'}]}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def ready(self):
        with self.server.condition:
            self.server.ready += 1; self.server.condition.notify_all()
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        with self.server.condition: self.server.posts += 1
        body = self.server.payload if isinstance(self.server.payload, bytes) else json.dumps(self.server.payload).encode()
        if self.server.mode == 'headers_held':
            self.ready(); self.server.cleanup.wait(); return
        self.send_response(self.server.status)
        self.send_header('Content-Length', str(len(body)))
        if self.server.close:
            self.send_header('Connection', 'close'); self.close_connection = True
        self.end_headers()
        if self.server.mode in ('held', 'before', 'after', 'late', 'truncated'):
            self.wfile.write(body[:1]); self.wfile.flush(); self.ready()
            if self.server.mode in ('held', 'late'): self.server.cleanup.wait()
            elif self.server.mode in ('before', 'after'): self.server.cleanup.wait(.35 if self.server.mode == 'before' else 2.2)
            else:
                self.close_connection = True; self.connection.shutdown(2); return
            try: self.wfile.write(body[1:]); self.wfile.flush()
            except OSError: pass
        else:
            self.wfile.write(body); self.wfile.flush(); self.ready()
        self.server.finished.set()


def worker(server, name):
    env = os.environ | {'AIST_QA_URL': f'http://127.0.0.1:{server.server_port}', 'AIST_LEASE_SECONDS': '4', 'AIST_SHUTDOWN_SECONDS': '1', 'AIST_TRANSPORT_SECONDS': '30', 'AIST_SNAPSHOT_PATH': str(OUT / (name + '-snapshot.json'))}
    log = (OUT / (name + '-worker.log')).open('w')
    return subprocess.Popen([sys.executable, '-m', 'app.worker'], env=env, stdout=log, stderr=log), log


def stop_worker(process, log, seconds=6):
    start = time.monotonic(); process.send_signal(signal.SIGTERM)
    process.wait(timeout=seconds)
    elapsed = time.monotonic() - start
    log.close(); assert process.returncode == 0, process.returncode
    return round(elapsed, 3)


def cleanup_worker(process, log):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        # Failure cleanup is separate from the measured successful lifecycle.
        process.wait(timeout=40)
    if log: log.close()


def record(name, **values):
    observations.append({'case': name, 'passed': True, **values})
    (OUT / 'http-boundaries.json').write_text(json.dumps({'passed': True, 'observations': observations, 'local_POSTs': sum(v.get('local_POSTs', 0) for v in observations), 'external_provider_POSTs': 0}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'case': name, 'passed': True}), flush=True)


def complete_case(name, payload, status=200, close=False, success=False, cost=False, input_value=None):
    server = Server('complete', payload, status, close); process = log = None
    try:
        ctx = setup(); process, log = worker(server, name); server.wait_ready()
        until(lambda: state(ctx)['state'] in ('failed', 'completed', 'recovery_required'))
        elapsed = stop_worker(process, log)
        s = state(ctx); m = s['calls'][0]; u = next(v for v in s['usage'] if v['id'] == m['usage_entry_id'])
        assert server.posts == 1 and len(s['calls']) == 1 and len(s['usage']) == 2
        assert s['state'] == ('completed' if success else 'failed'), s
        assert m['transport_state'] == 'observed' and m['provider_outcome'] == ('success' if success else 'error')
        assert m['http_status'] == status and m['observed_at']
        validate_definition('ModelCall', m); validate_definition('UsageEntry', u)
        if name.startswith('id_'): assert m['provider_completion_id'] is None
        if cost:
            assert u['tokens']['input']['value'] == 101 and u['tokens']['output']['value'] == 53
            assert Decimal(u['estimated_usd']['value']) == EXPECTED_COST and Decimal(s['exposure']) == EXPECTED_COST
            assert u['reservation_state'] == 'released'
        else:
            assert u['estimated_usd']['value'] is None and u['estimated_usd']['reason']
            assert u['tokens']['input']['value'] == input_value
            assert u['reservation_state'] == 'held' and Decimal(s['exposure']) == Decimal(runs.policy['quote_usd'])
            for token in u['tokens'].values():
                if token['value'] is None: assert token['reason']
        assert s['versions'] == (1 if success else 0) and s['assistant_messages'] == (1 if success else 0)
        api_calls = api('GET', path(ctx, '/model-calls')); validate_definition('CallPage', api_calls)
        api_usage = api('GET', path(ctx, '/usage')); validate_definition('UsagePage', api_usage)
        before = state(ctx)
        replay = ledger.observe(ctx['run'], s['epoch'], m['id'], 200, VALID)
        assert replay is success and state(ctx) == before, 'observation replay mutated usage or outcome'
        snapshot = assert_snapshot(name)
        record(name, local_POSTs=server.posts, elapsed_seconds=elapsed, run_id=ctx['run'], actual=s, replay_no_mutation=True, snapshot=snapshot)
    finally:
        cleanup_worker(process, log); server.stop()


def shutdown_case(name, mode, close=False, count=1, cancelled=False):
    server = Server(mode, close=close); process = log = None
    try:
        contexts = [setup() for _ in range(count)]
        process, log = worker(server, name); server.wait_ready(count)
        if cancelled:
            ctx = contexts[0]; s = state(ctx)
            api('POST', path(ctx, '/cancel'), json={'expected_revision': s['revision']})
        started = time.monotonic(); elapsed = stop_worker(process, log)
        assert elapsed < 6 and elapsed < 30 and server.posts == count
        # Withheld servers have not been released for successful process exit.
        if mode in ('held', 'headers_held'): assert not server.cleanup.is_set() and not server.finished.is_set()
        for index, ctx in enumerate(contexts):
            s = state(ctx); m = s['calls'][0]; u = next(v for v in s['usage'] if v['id'] == m['usage_entry_id'])
            expected = 'cancelled' if cancelled and index == 0 else 'completed' if mode == 'before' else 'recovery_required'
            assert s['state'] == expected, s
            if mode != 'before':
                assert m['transport_state'] == 'unknown' and m['delivery'] == 'suppressed'
                assert u['reservation_state'] == 'held' and u['tokens']['input']['value'] is None
                assert u['estimated_usd']['value'] is None and Decimal(s['exposure']) == Decimal(runs.policy['quote_usd'])
                assert s['versions'] == 0 and s['assistant_messages'] == 0
        snapshot = assert_snapshot(name)
        if mode == 'after':
            assert server.finished.wait(4)
            assert_snapshot(name)  # Response after the fence cannot mutate the DB.
        before = server.posts
        process, log = worker(server, name + '-restart'); time.sleep(.7); stop_worker(process, log)
        assert server.posts == before
        assert_snapshot(name + '-restart')
        record(name, local_POSTs=server.posts, contexts=[state(ctx) for ctx in contexts], elapsed_seconds=elapsed, shutdown_seconds=1, transport_seconds=30, body_held_until_process_exit=mode in ('held', 'headers_held'), POST_after_restart=0, snapshot=snapshot)
    finally:
        cleanup_worker(process, log); server.stop()


def late_case(terminal):
    name = 'late_response_after_' + terminal
    server = Server('late'); process = log = None
    try:
        ctx = setup(); process, log = worker(server, name); server.wait_ready(); s = state(ctx)
        if terminal == 'cancelled': api('POST', path(ctx, '/cancel'), json={'expected_revision': s['revision']})
        else: runs.stop(ctx['run'], s['epoch'], 'provider_error')
        server.cleanup.set()
        until(lambda: state(ctx)['calls'][0]['transport_state'] == 'observed')
        stop_worker(process, log); s = state(ctx); m = s['calls'][0]
        assert s['state'] == terminal and m['late_response'] and m['delivery'] == 'suppressed'
        assert Decimal(s['exposure']) == EXPECTED_COST and s['versions'] == 0 and s['assistant_messages'] == 0
        assert ledger.observe(ctx['run'], s['epoch'], m['id'], 200, VALID) is False
        assert state(ctx) == s and server.posts == 1
        record(name, local_POSTs=server.posts, actual=s, snapshot=assert_snapshot(name))
    finally:
        cleanup_worker(process, log); server.stop()


def unknown_case(mode, db_commit=False):
    name = 'observed_commit_unknown' if db_commit else 'incomplete_body_unknown'
    server = Server(mode); process = log = None
    try:
        ctx = setup()
        if db_commit:
            r = runs.claim('boundary_manual'); assert r['id'] == ctx['run']
            local_settings = Settings(qa_url=f'http://127.0.0.1:{server.server_port}')
            local_runs = Runs(Service(engine, Storage(settings.storage_root)), local_settings)
            transport = Transport(local_runs, threading.Event(), threading.Event(), threading.RLock(), {}, threading.Lock())
            commits = [0]
            def uncertain_commit(connection):
                commits[0] += 1
                if commits[0] == 3: raise OperationalError('commit', {}, Exception('observation acknowledgement unavailable'))
            event.listen(engine, 'commit', uncertain_commit)
            try:
                try: transport.perform(r, {'kind': 'model'})
                except ApiError as error: assert error.code == 'outcome_unknown'
                else: raise AssertionError('uncertain observation commit must not deliver')
            finally: event.remove(engine, 'commit', uncertain_commit)
        else:
            process, log = worker(server, name); server.wait_ready()
            until(lambda: state(ctx)['state'] == 'recovery_required'); stop_worker(process, log)
        s = state(ctx); m = s['calls'][0]; u = next(v for v in s['usage'] if v['id'] == m['usage_entry_id'])
        assert s['state'] == 'recovery_required' and m['transport_state'] == 'unknown' and server.posts == 1
        assert u['reservation_state'] == 'held' and u['estimated_usd']['value'] is None and s['versions'] == 0
        process, log = worker(server, name + '-restart'); time.sleep(.7); stop_worker(process, log)
        assert server.posts == 1
        record(name, local_POSTs=server.posts, actual=state(ctx), POST_after_restart=0, snapshot=assert_snapshot(name + '-restart'))
    finally:
        cleanup_worker(process, log); server.stop()


try:
    for name, completion in [('id_object', {'invalid': 'object'}), ('id_list', []), ('id_integer', 7), ('id_empty', ''), ('id_too_long', 'x'*201)]:
        complete_case(name, VALID | {'id': completion}, cost=True)
    for name, usage in [('usage_null', None), ('usage_list', []), ('usage_missing', None)]:
        payload = VALID | {'usage': usage}
        if name == 'usage_missing': del payload['usage']
        complete_case(name, payload)
    complete_case('usage_partial', VALID | {'usage': {'prompt_tokens': 101, 'completion_tokens': False}}, input_value=101)
    complete_case('damaged_json', b'{invalid json')
    complete_case('nonobject_json', [])
    complete_case('correct_keep_alive', VALID, success=True, cost=True)
    complete_case('correct_connection_close', VALID, close=True, success=True, cost=True)
    complete_case('provider_http_error', VALID | {'error': {'code': 'synthetic_error'}}, status=503, cost=True)
    complete_case('provider_json_error', VALID | {'error': {'code': 'synthetic_error'}}, cost=True)
    shutdown_case('partial_body_close', 'held', close=True)
    shutdown_case('partial_body_keep_alive', 'held')
    shutdown_case('headers_not_received', 'headers_held')
    shutdown_case('four_partial_readers_cancelled', 'held', close=True, count=4, cancelled=True)
    shutdown_case('response_before_deadline', 'before', close=True)
    shutdown_case('response_after_deadline', 'after', close=True)
    late_case('cancelled'); late_case('failed')
    unknown_case('truncated'); unknown_case('complete', db_commit=True)
    result = json.loads((OUT / 'http-boundaries.json').read_text())
    assert len(result['observations']) == 25
    result['completed'] = True
    (OUT / 'http-boundaries.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
except BaseException:
    (OUT / 'http-boundaries-failed.json').write_text(json.dumps({'passed': False, 'observations': observations}, ensure_ascii=False, indent=2) + '\n')
    raise
finally:
    engine.dispose()
