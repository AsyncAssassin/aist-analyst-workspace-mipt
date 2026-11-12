"""Сверка расходов использует реальный ledger и только локальные HTTP-ответы."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from sqlalchemy.exc import OperationalError

from dataclasses import replace

from app import openclaw_runtime as runtime, transport
from app.accounting import Accounting, money_available
from app.billing_evidence import ENDPOINTS
from app.database import make_engine
from app.errors import ApiError
from app.live_profile import OPENAI_ID, profile_for
from app.received_response import recover_observed
from app.runs import execute, json_value, now
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_flow01_postgres import current, executor
from test_openai_responses import reply
from test_openai_responses_postgres import configure_astra, installed_run, provider
from test_received_response_postgres import REQUEST, interrupt_observation, ledger_state, workspace_root

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
REFUSAL = dict(error=dict(type='invalid_request_error', code='fixture_refusal', message='Synthetic refusal'))
HEADERS = {'Content-Type': 'application/json'}


def rule(policy, profile_id, status, error):
    profile = profile_for({'model_profile_version_id': profile_id})
    protocol = profile['transport']
    return dict(rule_id='fixture-refusal', version='v1', source='https://billing.example.test/fixture',
                provider=profile['provider'], profile_id=profile_id, protocol=protocol, endpoint=''.join(ENDPOINTS[protocol]),
                budget_scope=policy['budget_scope'], pricing_version=policy['pricing_version'],
                http_status=status, error_type=error['type'], error_code=error.get('code'))


def astra(w, directory, *, approved=False, status=401, changes=None):
    configure_astra(w, directory)
    if approved:
        candidate = rule(w.runs.policy, OPENAI_ID, status, REFUSAL['error'])
        w.runs.policy['unbilled_errors'] = [dict(candidate, **(changes or {}))]
        Path(w.settings.policy_path).write_text(json_value(w.runs.policy))
    return submit(w)


def submit(w):
    request = body(question='Синтетическая проверка учета')
    request['model_profile_version_id'] = OPENAI_ID
    send(w, conversation(w), request)
    return w.runs.claim('billing-fixture')


def post(w, run, root):
    boundary = runtime.Boundary(executor(w), run, root / run['id'], 'synthetic-capability')
    return boundary.model(REQUEST)


def rejected(w, run, root):
    with pytest.raises(ApiError):
        post(w, run, root)
    # The synchronous fixture owns the job in place of Worker.tick.
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    return ledger_state(w, run)


def refusal(status=401, payload=REFUSAL, **options):
    return dict(status=status, body=payload, headers=HEADERS, **options)


def assert_reservation(run, call, usage, released):
    policy = run['configuration']['policy']
    assert str(call['exposure']) == ('0' if released else policy['quote_usd'])
    assert usage[-1]['reservation_state'] == ('released' if released else 'held')
    assert usage[-1]['billed_usd']['value'] is None
    assert usage[-1]['estimated_usd']['value'] == ('0' if released else None)
    assert call['usage_entry_id'] == usage[-1]['id']
    assert [value['supersedes_id'] for value in usage] == [None] + [value['id'] for value in usage[:-1]]


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500])
@pytest.mark.parametrize('shape', ['exact', 'other', 'partial', 'proxy', 'invalid', 'missing', 'default'])
def test_status_requires_exact_rule_and_complete_error(workspace, provider, tmp_path, status, shape):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path, approved=shape != 'default', status=status)
    entry = refusal(status)
    if shape == 'other':
        entry['body'] = dict(error=dict(REFUSAL['error'], type='different_error'))
    elif shape == 'partial':
        entry['body'] = dict(REFUSAL, usage=dict(input_tokens=2, output_tokens=5), output=[])
    elif shape == 'proxy':
        entry.update(raw=b'<html>Upstream unavailable</html>', headers={'Content-Type': 'text/html'})
    elif shape == 'invalid':
        entry['raw'] = b'{"error":'
    elif shape == 'missing':
        entry['missing'] = 9
    provider.queue = [entry]
    call, usage, events = rejected(w, run, root)
    assert_reservation(run, call, usage, shape == 'exact')
    assert len(provider.requests) == 1
    if shape == 'exact':
        assert usage[-1]['evidence_kind'] == 'reconciliation'
        assert usage[-1]['settlement']['http_status'] == status
        assert call['data']['provider_outcome'] == 'error'
    assert current(w, run)['error']['code'] == ('outcome_unknown' if shape == 'missing' else 'provider_error')


@pytest.mark.parametrize('defect', ['unframed', 'duplicate', 'endpoint', 'pricing', 'scope', 'code', 'invalid_rules'])
def test_ambiguous_evidence_stays_held(workspace, provider, tmp_path, defect):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    changes = {'endpoint': {'endpoint': 'api.example.test/v1/responses'},
               'pricing': {'pricing_version': 'another-tariff'},
               'scope': {'budget_scope': 'anthropic-authorized-total'},
               'code': {'error_code': 'other_error'},
               'invalid_rules': {'version': ''}}.get(defect)
    run = astra(w, tmp_path, approved=True, changes=changes)
    entry = refusal(unframed=defect == 'unframed')
    if defect == 'duplicate':
        entry['raw'] = b'{"error":null,' + json_value(REFUSAL).encode()[1:]
    provider.queue = [entry]
    call, usage, _ = rejected(w, run, root)
    assert_reservation(run, call, usage, False)
    assert len(provider.requests) == 1


def test_changes_to_current_policy_do_not_release_an_old_call(workspace, provider, tmp_path):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path)
    w.runs.policy['unbilled_errors'] = [rule(w.runs.policy, OPENAI_ID, 401, REFUSAL['error'])]
    Path(w.settings.policy_path).write_text(json_value(w.runs.policy))
    provider.queue = [refusal()]
    call, usage, _ = rejected(w, run, root)
    assert_reservation(run, call, usage, False)


@pytest.mark.parametrize('approved', [False, True])
def test_scope_admission_after_three_refusals(workspace, provider, tmp_path, approved):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    provider.queue = [refusal()] * 3
    for _ in range(3):
        run = astra(w, tmp_path, approved=approved)
        call, usage, _ = rejected(w, run, root)
        assert_reservation(run, call, usage, approved)
    second = ident('usr')
    w.runs.policy['demo_access']['owner_ids'].append(second)
    Path(w.settings.policy_path).write_text(json_value(w.runs.policy))
    with w.engine.begin() as c:
        count, total = execute(c, 'SELECT count(*),sum(exposure) FROM aist.model_calls').one()
        assert count == len(provider.requests) == 3
        assert total == (0 if approved else 72)
    assert w.runs.capabilities(second)['model_execution']['state'] == ('available' if approved else 'budget_exceeded')
    print(dict(posts=count, exposure_usd=str(total), second_owner=w.runs.capabilities(second)['model_execution']['state']))


def evidence(ledger, run, call, amount, **changes):
    inspected = ledger.billing_call(run['id'], call['id'])
    excluded = {'transport_state', 'can_reconcile', 'current_usage_id', 'exposure_usd', 'reservation_state',
                'reserved_usd', 'estimated_usd', 'billed_usd', 'reason', 'required_evidence', 'provider_outcome', 'http_status'}
    value = {key: item for key, item in inspected.items() if key not in excluded}
    value.update(operation_id=ident('billing'), account_scope='fixture-account', source_id='fixture-statement',
                 line_id=ident('line'), source_sha256=sha256(b'synthetic itemized statement').hexdigest(),
                 subject='fixture-operator', confirmed_at=now(), expected_usage_id=inspected['current_usage_id'],
                 currency='USD', amount_usd=amount)
    return dict(value, **changes)


@pytest.mark.parametrize('state', ['unknown', 'observed'])
@pytest.mark.parametrize('amount', ['0', '85.000000000001'])
def test_invoice_is_final_without_rewriting_transport(workspace, provider, tmp_path, state, amount):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path)
    provider.queue = [dict(close=True)] if state == 'unknown' else [refusal()]
    call, before, events = rejected(w, run, root)
    ledger = Accounting(w.runs)
    proof = evidence(ledger, run, call, amount)
    view_before = current(w, run)
    assert ledger.invoice(run['id'], call['id'], proof)['status'] == 'valid'
    assert ledger_state(w, run)[1] == before
    recorded = ledger.invoice(run['id'], call['id'], proof, apply=True)
    repeated = ledger.invoice(run['id'], call['id'], proof, apply=True)
    assert recorded['status'] == 'recorded' and repeated == dict(recorded, status='already_recorded')
    saved, after, same_events = ledger_state(w, run)
    assert same_events == events and saved['data'] == dict(call['data'], usage_entry_id=recorded['usage_entry_id'])
    assert after[:-1] == before and after[-1]['settlement'] == proof
    assert after[-1]['billed_usd']['value'] == amount and after[-1]['reservation_state'] == 'released'
    assert after[-1]['tokens'] == before[-1]['tokens']
    assert str(saved['exposure']) == amount
    view_after = current(w, run)
    assert all(view_before[key] == view_after[key] for key in ('state', 'error', 'epoch', 'worker_id', 'revision'))
    if state == 'unknown':
        with w.engine.begin() as c, pytest.raises(ApiError) as error:
            ledger.require_settled(c, view_after)
        assert error.value.code == 'outcome_unknown'
    with w.engine.begin() as c:
        assert money_available(c, view_after, 1) == (amount == '0')
    page = w.runs.listing(w.actor, w.project, 'usage_entries', {'limit': 50}, run['id'])
    assert 'source_sha256' not in json_value(page)
    assert len(provider.requests) == 1


def test_cli_preview_apply_repeat_conflict_and_unavailable(workspace, provider, tmp_path):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path)
    provider.queue = [refusal()]
    call, before, _ = rejected(w, run, root)
    proof = evidence(Accounting(w.runs), run, call, '0')
    path = tmp_path / 'evidence.json'
    path.write_text(json_value(proof))

    def cli(*args, run_id=run['id'], call_id=call['id'], **overrides):
        env = dict(os.environ, AIST_LIVE_MODELS_ENABLED='false', AIST_EXECUTOR_MODE='disabled',
                   AIST_POLICY_PATH='/not-used/policy.json', AIST_OPENCLAW_ROOT=str(root))
        return subprocess.run([sys.executable, '-m', 'app.cli', 'billing-call', '--run', run_id,
                               '--call', call_id, *args], env=dict(env, **overrides),
                              capture_output=True, text=True, timeout=15)

    def result(completed):
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    flags = ('--evidence', str(path))
    unused_storage = tmp_path / 'unused-storage'
    admin = make_engine(replace(w.settings, db_name='postgres'))
    try:
        name = w.settings.db_name
        with admin.begin() as c:
            execute(c, f'ALTER DATABASE "{name}" SET default_transaction_read_only TO on')
        assert result(cli(AIST_STORAGE_ROOT=str(unused_storage)))['exposure_usd'] == run['configuration']['policy']['quote_usd']
        assert result(cli(*flags, AIST_STORAGE_ROOT=str(unused_storage)))['status'] == 'valid'
        denied_write = cli(*flags, '--apply')
        assert not unused_storage.exists()
    finally:
        with admin.begin() as c:
            execute(c, f'ALTER DATABASE "{name}" RESET default_transaction_read_only')
        admin.dispose()
    assert not list(root.glob('.received/*'))
    assert ledger_state(w, run)[1] == before
    for rejected_command in (denied_write, cli(AIST_DB_PORT='1'),
                             cli(call_id=ident('call')), cli(run_id=ident('run')), cli('--apply')):
        assert rejected_command.returncode != 0
        assert not rejected_command.stdout
        assert all(secret not in rejected_command.stderr for secret in ('Traceback', 'SELECT', 'fixture-only-key', 'Synthetic refusal'))
    first = result(cli(*flags, '--apply'))
    assert first['status'] == 'recorded'
    assert result(cli(*flags, '--apply')) == dict(first, status='already_recorded')
    path.write_text(json_value(dict(proof, amount_usd='1')))
    conflict = cli(*flags, '--apply')
    assert conflict.returncode == 1 and 'Этот ключ уже использован' in conflict.stderr
    assert len(ledger_state(w, run)[1]) == len(before) + 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize('changes', [
    {'amount_usd': -1}, {'amount_usd': 0.1}, {'amount_usd': 'NaN'}, {'amount_usd': 'Infinity'},
    {'amount_usd': '-1'}, {'amount_usd': '0.0000000000001'}, {'amount_usd': '1e2'}, {'amount_usd': '0\n'},
    {'currency': 'EUR'}, {'provider_request_id': 'other'}, {'owner_id': 'usr_other'},
    {'project_id': 'prj_other'}, {'source_id': ''}, {'budget_scope': 'anthropic-authorized-total'},
    {'confirmed_at': '2000-01-01T00:00:00Z'}, {'per_call_attribution': False},
])
def test_invalid_evidence_is_read_only(workspace, provider, tmp_path, changes):
    w = workspace
    run = astra(w, tmp_path)
    provider.queue = [refusal()]
    call, before, _ = rejected(w, run, tmp_path)
    ledger = Accounting(w.runs)
    proof = evidence(ledger, run, call, '0', **changes)
    with pytest.raises(ApiError) as error:
        ledger.invoice(run['id'], call['id'], proof, apply=True)
    assert error.value.code == 'validation_error'
    assert ledger_state(w, run)[1] == before


def test_correction_requires_current_predecessor_and_a_new_line(workspace, provider, tmp_path):
    w = workspace
    provider.queue = [refusal(), refusal()]
    run = astra(w, tmp_path)
    call, before, _ = rejected(w, run, tmp_path)
    ledger = Accounting(w.runs)
    first = evidence(ledger, run, call, '0')
    ledger.invoice(run['id'], call['id'], first, apply=True)
    stale = evidence(ledger, run, call, '2', expected_usage_id=first['expected_usage_id'])
    with pytest.raises(ApiError) as error:
        ledger.invoice(run['id'], call['id'], stale, apply=True)
    assert error.value.code == 'revision_conflict'
    second = dict(stale, expected_usage_id=ledger_state(w, run)[0]['usage_entry_id'])
    ledger.invoice(run['id'], call['id'], second, apply=True)
    assert ledger.invoice(run['id'], call['id'], first, apply=True)['status'] == 'already_recorded'

    other = astra(w, tmp_path)
    other_call, _, _ = rejected(w, other, tmp_path)
    copied = evidence(ledger, other, other_call, '0', line_id=first['line_id'])
    with pytest.raises(ApiError) as error:
        ledger.invoice(other['id'], other_call['id'], copied, apply=True)
    assert error.value.code == 'idempotency_conflict'
    assert ledger_state(w, other)[0]['exposure'] == 24
    saved, after, _ = ledger_state(w, run)
    assert after[:-2] == before
    assert after[-1]['supersedes_id'] == after[-2]['id'] and saved['exposure'] == 2
    assert len(provider.requests) == 2


def test_grouped_bill_does_not_release_or_duplicate_per_call_cost(workspace, provider, tmp_path):
    w = workspace
    run = astra(w, tmp_path)
    provider.queue = [refusal()]
    call, before, _ = rejected(w, run, tmp_path)
    ledger = Accounting(w.runs)
    ledger.aggregate(dict(id=ident('bill'), provider='openai', model=REQUEST['model'],
                          period_start='2026-01-01T00:00:00Z', period_end='2026-01-01T01:00:00Z',
                          scope='provider_account_hour', coverage='complete', amount_usd='100', request_count=10,
                          per_call_attribution=False, observed_at=now()))
    assert ledger_state(w, run)[1] == before
    assert ledger_state(w, run)[0]['exposure'] == 24
    with w.engine.begin() as c:
        assert money_available(c, current(w, run), 1)
    ledger.invoice(run['id'], call['id'], evidence(ledger, run, call, '2'), apply=True)
    assert ledger_state(w, run)[0]['exposure'] == 2


@pytest.mark.parametrize('actually_committed', [False, True])
def test_invoice_unknown_commit(workspace, provider, tmp_path, monkeypatch, actually_committed):
    w = workspace
    run = astra(w, tmp_path)
    provider.queue = [refusal()]
    call, before, _ = rejected(w, run, tmp_path)
    ledger = Accounting(w.runs)
    proof = evidence(ledger, run, call, '0')
    original = w.engine.dialect.do_commit

    class ConnectionLost(Exception):
        pgcode = '08006'

    def lose_ack(dbapi):
        monkeypatch.setattr(w.engine.dialect, 'do_commit', original)
        (original if actually_committed else w.engine.dialect.do_rollback)(dbapi)
        raise OperationalError(None, None, ConnectionLost(), connection_invalidated=True)

    monkeypatch.setattr(w.engine.dialect, 'do_commit', lose_ack)
    if actually_committed:
        assert ledger.invoice(run['id'], call['id'], proof, apply=True)['status'] == 'already_recorded'
    else:
        with pytest.raises(OperationalError):
            ledger.invoice(run['id'], call['id'], proof, apply=True)
        assert ledger_state(w, run)[1] == before
        assert ledger.invoice(run['id'], call['id'], proof, apply=True)['status'] == 'recorded'
    assert ledger.invoice(run['id'], call['id'], proof, apply=True)['status'] == 'already_recorded'
    assert len(ledger_state(w, run)[1]) == len(before) + 1


def test_concurrent_invoice_reuses_operation(workspace, provider, tmp_path):
    w = workspace
    run = astra(w, tmp_path)
    provider.queue = [refusal()]
    call, before, _ = rejected(w, run, tmp_path)
    ledger = Accounting(w.runs)
    proof = evidence(ledger, run, call, '1')
    barrier = threading.Barrier(2)

    def apply():
        barrier.wait(timeout=5)
        return ledger.invoice(run['id'], call['id'], proof, apply=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(apply) for _ in range(2)]
        results = [result.result(timeout=5) for result in results]
    assert {result['status'] for result in results} == {'recorded', 'already_recorded'}
    assert len({result['usage_entry_id'] for result in results}) == 1
    assert len(ledger_state(w, run)[1]) == len(before) + 1
    with w.engine.connect() as c:
        counts = execute(c, "SELECT count(*) FROM aist.run_events WHERE run_id=:r AND data->>'type'='usage_recorded'", r=run['id'])
        assert counts.scalar_one() == len(before) + 1


def test_new_post_checks_exposure_after_reservation(workspace, provider, tmp_path, monkeypatch):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    old = astra(w, tmp_path)
    provider.queue = [refusal()]
    old_call, _, _ = rejected(w, old, root)
    ledger = Accounting(w.runs)
    proof = evidence(ledger, old, old_call, '85')
    new = submit(w)
    worker = executor(w)
    reserve = worker.transport.ledger.reserve

    def reserve_then_invoice(*args, **kwargs):
        call = reserve(*args, **kwargs)
        assert ledger.invoice(old['id'], old_call['id'], proof, apply=True)['status'] == 'recorded'
        return call

    monkeypatch.setattr(worker.transport.ledger, 'reserve', reserve_then_invoice)
    boundary = runtime.Boundary(worker, new, root / new['id'], 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(REQUEST)
    new_call, _, _ = ledger_state(w, new)
    assert len(provider.requests) == 1
    assert new_call['data']['transport_state'] == 'not_sent' and new_call['exposure'] == 0
    assert current(w, new)['state'] == 'budget_exceeded'


@pytest.mark.parametrize('mutation', ['valid', 'old_version', 'owner', 'response', 'symlink', 'fifo', 'partial_only'])
def test_restart_reads_only_verified_refusal(workspace, provider, tmp_path, monkeypatch, mutation):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path, approved=True)
    provider.queue = [refusal()]
    worker = executor(w)
    interrupt_observation(monkeypatch, worker)
    boundary = runtime.Boundary(worker, run, root / run['id'], 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(REQUEST)
    call, before, _ = ledger_state(w, run)
    proof = root / '.received' / run['id'] / (call['id'] + '.json')
    manifest = json.loads(proof.read_bytes())
    response = root / manifest['audit'] / (call['id'] + '-response.json')
    if mutation == 'old_version':
        proof.write_text(json_value(dict(manifest, version=1)))
    elif mutation == 'owner':
        manifest['binding']['owner_id'] = ident('usr')
        proof.write_text(json_value(manifest))
    elif mutation == 'response':
        response.write_bytes(response.read_bytes()[:-1])
    elif mutation in ('symlink', 'fifo'):
        moved = response.with_suffix('.moved')
        response.rename(moved)
        if mutation == 'symlink':
            response.symlink_to(moved)
        else:
            os.mkfifo(response)
    elif mutation == 'partial_only':
        proof.unlink()
    w.runs.stop(run['id'], run['epoch'], 'outcome_unknown')
    result = subprocess.run([sys.executable, '-m', 'app.cli', 'recover-observed', '--run', run['id']],
                            env=dict(os.environ, AIST_OPENCLAW_ROOT=str(root)), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['calls'][0]['status'] == ('recovered' if mutation == 'valid' else 'insufficient_evidence')
    after, usage, _ = ledger_state(w, run)
    if mutation == 'valid':
        assert_reservation(run, after, usage, True)
        assert after['data']['delivery'] == 'suppressed'
    else:
        assert before == usage
    assert current(w, run)['state'] == 'failed'
    assert len(provider.requests) == 1


@pytest.mark.parametrize('refused', [False, True])
def test_late_recovery_preserves_billed(workspace, provider, tmp_path, monkeypatch, refused):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path, approved=refused)
    provider.queue = [refusal() if refused else dict(body=reply())]
    worker = executor(w)
    interrupt_observation(monkeypatch, worker)
    boundary = runtime.Boundary(worker, run, root / run['id'], 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(REQUEST)
    call, _, _ = ledger_state(w, run)
    assert call['data']['transport_state'] == 'unknown'
    ledger = Accounting(w.runs)
    charge = evidence(ledger, run, call, '0' if not refused else '3')
    # Race both operations. The file is read before the recovery takes the Run lock.
    barrier = threading.Barrier(2)

    def apply():
        barrier.wait(timeout=5)
        return ledger.invoice(run['id'], call['id'], charge, apply=True)

    def recover():
        barrier.wait(timeout=5)
        return recover_observed(w.runs, run['id'])

    with ThreadPoolExecutor(max_workers=2) as pool:
        invoicing, recovering = pool.submit(apply), pool.submit(recover)
        assert recovering.result(timeout=10)['calls'][0]['status'] == 'recovered'
        try:
            assert invoicing.result(timeout=10)['status'] == 'recorded'
        except ApiError as error:
            assert error.code in ('revision_conflict', 'validation_error')
            charge = evidence(ledger, run, call, charge['amount_usd'])
            assert ledger.invoice(run['id'], call['id'], charge, apply=True)['status'] == 'recorded'
    saved, usage, _ = ledger_state(w, run)
    assert [entry['supersedes_id'] for entry in usage] == [None] + [entry['id'] for entry in usage[:-1]]
    assert usage[-1]['billed_usd']['value'] == charge['amount_usd']
    assert usage[-1]['reservation_state'] == 'released'
    assert str(saved['exposure']) == charge['amount_usd'] and saved['data']['delivery'] == 'suppressed'
    assert len(provider.requests) == 1


@pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW') != 'true', reason='Requires installed OpenClaw')
def test_installed_executor_refusal_uses_the_pinned_rule(workspace, provider, tmp_path):
    w = workspace
    run = astra(w, tmp_path, approved=True)
    provider.queue = [refusal()]
    state = installed_run(w, run, Path('/result/a13-installed-refusal'))
    call, usage, _ = ledger_state(w, run)
    assert state['state'] == 'failed' and state['error']['code'] == 'provider_error'
    assert_reservation(run, call, usage, True)
    assert len(provider.requests) == 1
    print(dict(posts=len(provider.requests), calls=1, exposure_usd=str(call['exposure']), reason=state['error']['code']))


@pytest.mark.parametrize('failure', ['not_sent', 'timeout', 'missing_sse', 'missing_usage'])
def test_local_and_uncertain_outcomes(workspace, provider, tmp_path, monkeypatch, failure):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path, approved=True)
    if failure == 'not_sent':
        def cannot_connect(self):
            raise ConnectionRefusedError()
        monkeypatch.setattr(transport.ReaderConnection, 'connect', cannot_connect)
    elif failure == 'timeout':
        w.runs.settings = replace(w.runs.settings, transport_seconds=.15)
        provider.queue = [dict(refusal(), delay=.5)]
    elif failure == 'missing_sse':
        provider.queue = [dict(body=reply(), incomplete=True)]
    else:
        incomplete = reply()
        del incomplete['usage']['output_tokens']
        provider.queue = [dict(body=incomplete)]
    call, usage, _ = rejected(w, run, root)
    if failure == 'not_sent':
        assert call['data']['transport_state'] == 'not_sent'
        assert_reservation(run, call, usage, True)
        Accounting(w.runs).mark_unknown(run['id'], call['id'], 'Repeated pre-send failure', not_sent=True)
        assert ledger_state(w, run)[1] == usage
        assert provider.requests == []
    else:
        assert_reservation(run, call, usage, False)
        assert len(provider.requests) == 1


@pytest.mark.parametrize('protocol', ['anthropic', 'novita'])
def test_refusal_rules_use_the_provider_envelope(workspace, provider, tmp_path, monkeypatch, protocol):
    from app import anthropic_messages, novita
    from app.live_profile import FABLE_ID
    from test_fable_postgres import start_fable
    from test_perf02_r3_postgres import start as start_novita

    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    error = {'type': 'authentication_error', 'message': 'Synthetic refusal'}
    if protocol == 'anthropic':
        monkeypatch.setattr(anthropic_messages, 'credential', lambda _: 'fixture-only-key')
        pricing = dict(w.runs.policy, budget_scope='anthropic-authorized-total',
                       pricing_version='anthropic-standard-2026-10-23.v1')
        w.runs.policy['unbilled_errors'] = [rule(pricing, FABLE_ID, 401, error)]
        _, _, run = start_fable(w)
        entry = refusal(payload=dict(type='error', error=error, request_id='req_fixture_native'))
        entry['headers'] = dict(HEADERS, **{'request-id': 'req_fixture_native'})
    else:
        monkeypatch.setattr(novita, 'credential', lambda _: 'fixture-only-key')
        profile_id = w.settings.model_profile_id
        w.runs.policy['unbilled_errors'] = [rule(w.runs.policy, profile_id, 401, error)]
        run = start_novita(w)
        entry = refusal(payload=error)
    profile = profile_for(run['input'])
    provider.queue = [entry]
    boundary = runtime.Boundary(executor(w), run, root / run['id'], 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(dict(model=profile['model'], messages=[dict(role='user', content='Синтетический запрос')]))
    call, usage, _ = ledger_state(w, run)
    assert_reservation(run, call, usage, True)
    assert provider.paths == [ENDPOINTS[profile['transport']][1]]


def test_scope_lock_orders_two_projects_and_admission(workspace, provider, tmp_path, monkeypatch):
    from types import SimpleNamespace

    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    configure_astra(w, tmp_path)
    other = SimpleNamespace(**vars(w))
    other.actor, other.session = ident('usr'), ident('ses')
    with w.engine.begin() as c:
        execute(c, 'INSERT INTO aist.users(id,label) VALUES (:id,:label)', id=other.actor, label='Other fixture owner')
        execute(c, """INSERT INTO aist.sessions(id,user_id,token_hash,expires_at)
            VALUES (:id,:actor,:hash,clock_timestamp()+interval '1 hour')""",
            id=other.session, actor=other.actor, hash=ident('fixture'))
    _, project = w.service.write(other.actor, 'createProject', ident('key'), {'name': 'Other fixture project'})
    other.project = project['id']
    w.runs.policy['demo_access']['owner_ids'].append(other.actor)
    Path(w.settings.policy_path).write_text(json_value(w.runs.policy))
    provider.queue = [refusal()] * 3
    run = submit(w)
    call, _, _ = rejected(w, run, root)
    second = submit(other)
    second_call, _, _ = rejected(other, second, root)
    third = submit(w)
    rejected(w, third, root)
    ledger = Accounting(w.runs)
    first = evidence(ledger, run, call, '0')
    reused = evidence(ledger, second, second_call, '0', line_id=first['line_id'])
    entered, release = threading.Event(), threading.Event()
    save_call = ledger.save_call

    def before_commit(*args):
        save_call(*args)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(ledger, 'save_call', before_commit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        settling = pool.submit(ledger.invoice, run['id'], call['id'], first, apply=True)
        try:
            assert entered.wait(5)
            competing = pool.submit(ledger.invoice, second['id'], second_call['id'], reused, apply=True)
            admission = pool.submit(w.runs.capabilities, other.actor)
            with pytest.raises(TimeoutError):
                admission.result(timeout=.1)
        finally:
            release.set()
        assert settling.result(timeout=5)['status'] == 'recorded'
        assert admission.result(timeout=5)['model_execution']['state'] == 'available'
        with pytest.raises(ApiError) as error:
            competing.result(timeout=5)
        assert error.value.code == 'idempotency_conflict'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT sum(exposure) FROM aist.model_calls').scalar_one() == 48
    assert len(provider.requests) == 3


def test_active_sent_call_cannot_be_reconciled(workspace, provider, tmp_path):
    w = workspace
    root = workspace_root(w, tmp_path / 'execution')
    run = astra(w, tmp_path)
    received, proceed = threading.Event(), threading.Event()

    def pending():
        received.set()
        assert proceed.wait(5)
        return REFUSAL

    provider.queue = [refusal(payload=pending)]
    ledger = Accounting(w.runs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        model = pool.submit(rejected, w, run, root)
        try:
            assert received.wait(5)
            call, before, _ = ledger_state(w, run)
            assert call['data']['transport_state'] == 'sent'
            assert not ledger.billing_call(run['id'], call['id'])['can_reconcile']
            with pytest.raises(ApiError) as error:
                ledger.invoice(run['id'], call['id'], evidence(ledger, run, call, '0'), apply=True)
            assert error.value.code == 'outcome_unknown'
            assert ledger_state(w, run)[1] == before
        finally:
            proceed.set()
        call, usage, _ = model.result(timeout=10)
    assert_reservation(run, call, usage, False)
    assert len(provider.requests) == 1
