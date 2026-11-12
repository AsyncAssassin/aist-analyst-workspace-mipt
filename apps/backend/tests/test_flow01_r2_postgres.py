"""FLOW-01.01-R2: the final body of a logical send and the deadline of its TLS handshake and lock waits.

The R1 review probes moved into regressions: a request composed again for the completion mode right before
its first POST is repeated byte for byte by a granted retry and checked against the input limit; a stalled
TLS handshake and a busy Run row end with the deadline of the attempt. A shifted Run clock and loopback
servers only; no model is involved."""
from dataclasses import replace
from decimal import Decimal
import json
import os
from socketserver import BaseRequestHandler, ThreadingTCPServer
import threading
import time

import pytest
from sqlalchemy import text

from app import collection, transport
from app.errors import ApiError
from app.live_profile import PROFILE
from app.openclaw_runtime import Boundary, service_context
from app.runs import json_value
from test_d15_postgres import workspace
from test_flow01_postgres import (agent, ask, closed_events, counts, current, executor, extraction, hints, invoke,
                                  note_run, plain_source, qualitative_item, request, shift_clock, step)
from test_flow01_r1_postgres import reset_clock, run_state
from test_perf02_r3_postgres import calls, record_pauses, refusal, stub, transport_events

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def delayed_connection(monkeypatch, seconds):
    """The first connection to the stub moves the Run clock: the preparation and the connection took this long."""
    original = transport.ReaderConnection.connect
    shifted = []

    def connect(connection):
        original(connection)
        if not shifted:
            shifted.append(seconds)
            shift_clock(monkeypatch, seconds)

    monkeypatch.setattr(transport.ReaderConnection, 'connect', connect)


def post(worker, run, directory, turn=1):
    return worker.transport.perform(run, {'purpose': 'agent'}, request=request(turn), audit=directory / 'calls')


def test_a_granted_retry_repeats_the_request_composed_for_the_completion_mode_byte_for_byte(workspace, stub, monkeypatch, tmp_path):
    """Probe 1 of the R1 review: at 220 s the connection takes 30 s on the Run clock, the collection closes at
    the send boundary and the first POST leaves with the completion context. After a known 429 the granted
    retry repeats exactly that body: one ordinary call and its retry, one claim, both bodies in the audit."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=180)
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w), max_duration_seconds=600)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    shift_clock(monkeypatch, 220)
    delayed_connection(monkeypatch, 30)
    pauses = record_pauses(monkeypatch)
    stub.queue = [refusal(), agent(content='Сохраненные сведения прочитаны.')]
    payload = boundary.model(request(1))
    assert payload['choices'][0]['message']['content'] == 'Сохраненные сведения прочитаны.'
    assert pauses == [10.0] and len(stub.received) == 2 and not stub.queue
    print(json.dumps(dict(case='retry_body_after_recompose', posts=len(stub.received), pauses=pauses, same_body=stub.received[0] == stub.received[1],
                          completion=['Режим завершения' in hint for hint in hints(stub)]), ensure_ascii=False))
    assert stub.received[0] == stub.received[1], 'the retry must repeat the composed body byte for byte'
    assert all('Режим завершения' in hint and 'Сбор оснований ограничен' not in hint for hint in hints(stub))
    state = collection.state(current(w, run))
    assert state['closed'] and state['reason'] == 'reserve_time'
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['reserve_time']
    assert counts(w, run) == ((1, 1), (0, 0)) and run_state(w, run)['state'] == 'running'
    refused, retry = calls(w, run)
    assert (refused['data']['http_status'], refused['data']['provider_outcome']) == (429, 'error')
    assert 'Повторная отправка 1 из 3 через 10.0 с' in refused['data']['error']['message']
    assert retry['parent_model_call_id'] == refused['data']['id'] and retry['data']['provider_outcome'] == 'success'
    assert transport_events(w, refused['data']['id']) == ['reserved', 'attempted', 'sent', 'observed'] == transport_events(w, retry['data']['id'])
    audit = tmp_path / 'calls'
    assert [(audit / (c['data']['id'] + '-request.json')).read_bytes() for c in (refused, retry)] == stub.received
    claim, = audit.glob('*.claim')
    assert claim.read_text().splitlines() == ['single provider submission',
                                              'retry granted after ' + refused['data']['id'] + ': HTTP 429 server_overload, pause 10.0 s']


def padded_request(w, run, size):
    """An agent request whose body with the collecting context of this Run takes exactly this many bytes,
    built as Transport.perform builds the outgoing request."""
    req = request(2)
    with w.engine.begin() as c:
        hint = service_context(w.runs, c, w.runs.locked(c, run['id']), 3)
    outgoing = dict(req, stream=False, max_tokens=PROFILE['max_tokens'], n=1, parallel_tool_calls=False,
                    thinking={'type': PROFILE['thinking']}, enable_thinking=PROFILE['thinking'] == 'enabled',
                    messages=[dict(role='system', content=hint), *req['messages']])
    req['messages'][0]['content'] += 'x' * (size - len(json_value(outgoing).encode()))
    return req


def test_a_request_composed_beyond_the_input_limit_is_withheld_with_its_size_and_a_fitting_one_is_sent(workspace, stub, monkeypatch, tmp_path):
    """Probe 4 of the R1 review: a body at the input limit grows past it when the completion context, naming the
    saved extraction, replaces the collecting one. Nothing is sent, nothing is cut and no older body returns:
    the reserved call is recorded as not sent with its size as the known cause and the Run ends with the same
    reason. A composed body that still fits leaves as the request of the same logical send."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=180)
    limit = PROFILE['input_bytes']
    for margin in (0, 4000):
        reset_clock(monkeypatch)
        run = note_run(w, plain_source(w), max_duration_seconds=600)
        directory = tmp_path / str(margin)
        boundary = Boundary(executor(w), run, directory, 'synthetic')
        tool, = step(boundary, stub, agent(ask('Бизнес и клиенты компании. ' * 30)), 1)
        stub.queue = [extraction([qualitative_item(2, 3)])]
        assert invoke(boundary, run, tool)['ok']
        shift_clock(monkeypatch, 220)
        req = padded_request(w, run, limit - margin)
        delayed_connection(monkeypatch, 30)
        received = len(stub.received)
        stub.queue = [agent(content='Готовлю сохранение по имеющимся сведениям.')]
        if margin == 0:
            with pytest.raises(ApiError) as ended:
                boundary.model(req)
            assert ended.value.code == 'run_terminal' and len(stub.received) == received and stub.queue, 'no POST beyond the limit'
            withheld = calls(w, run)[-1]
            data = withheld['data']
            assert (data['purpose'], data['transport_state'], data['provider_outcome'], data['delivery']) == ('agent', 'not_sent', 'not_sent', 'suppressed')
            composed = (directory / 'calls' / (data['id'] + '-request.json')).read_bytes()
            assert len(composed) > limit and 'Режим завершения' in composed.decode()
            assert data['error']['code'] == 'budget_exceeded'
            assert '(' + str(len(composed)) + ' байт при лимите ' + str(limit) + ' байт)' in data['error']['message']
            assert Decimal(withheld['exposure']) == 0 and transport_events(w, data['id']) == ['reserved', 'attempted', 'not_sent']
            state = run_state(w, run)
            assert state['state'] == 'budget_exceeded' and state['error'] == data['error']
            assert collection.state(current(w, run))['reason'] == 'reserve_time' and counts(w, run) == ((3, 0), (1, 0))
            claims = list((directory / 'calls').glob('*.claim'))
            assert len(claims) == 3 and all(claim.read_text() == 'single provider submission\n' for claim in claims)
            print(json.dumps(dict(case='oversized_completion_request', limit=limit, initial_size=limit, composed_size=len(composed),
                                  posts=len(stub.received) - received, run_state=state['state'], call_state=data['transport_state'])))
            with pytest.raises(ApiError) as terminal:
                boundary.model(request(3))
            assert terminal.value.code == 'run_terminal' and len(stub.received) == received
            stub.queue = []
        else:
            payload = boundary.model(req)
            assert payload['choices'][0]['message']['content'] == 'Готовлю сохранение по имеющимся сведениям.'
            assert len(stub.received) == received + 1 and not stub.queue
            sent = stub.received[-1]
            assert limit - margin < len(sent) <= limit and 'Режим завершения' in hints(stub)[-1]
            recorded = calls(w, run)[-1]['data']
            assert (recorded['transport_state'], recorded['provider_outcome']) == ('observed', 'success')
            assert (directory / 'calls' / (recorded['id'] + '-request.json')).read_bytes() == sent
            assert run_state(w, run)['state'] == 'running' and collection.state(current(w, run))['reason'] == 'reserve_time'
            assert counts(w, run) == ((3, 0), (1, 0))
            print(json.dumps(dict(case='fitting_completion_request', limit=limit, initial_size=limit - margin, sent_size=len(sent))))


def test_a_stalled_tls_handshake_ends_with_the_deadline_of_the_attempt(workspace, monkeypatch, tmp_path):
    """Probe 2 of the R1 review: a loopback endpoint receives the real ClientHello and stalls for a second.
    With a 0.2 s limit the attempt returns within 0.6 s, measured without the stop of the endpoint; nothing
    was POSTed and the reserved call is recorded as not sent."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=0.2)
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w))
    worker = executor(w)
    handshakes = []

    class StalledTLS(BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            handshakes.append(self.request.recv(4096)[:1] == b'\x16')  # a TLS handshake record
            time.sleep(1.0)

    server = ThreadingTCPServer(('127.0.0.1', 0), StalledTLS)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    real_https = transport.ReaderHTTPSConnection
    monkeypatch.setattr('app.novita.credential', lambda path: 'local-credential-not-in-response')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection',
                        lambda *a, **kw: real_https('127.0.0.1', server.server_address[1], timeout=3))
    started = time.monotonic()
    try:
        with pytest.raises(ApiError) as failed:
            post(worker, run, tmp_path)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(json.dumps(dict(case='tls_handshake_deadline', limit=0.2, elapsed=round(elapsed, 3), handshakes=handshakes)))
    assert handshakes == [True], 'the endpoint saw the ClientHello'
    assert elapsed < 0.6, elapsed
    assert failed.value.code == 'outcome_unknown'
    recorded, = calls(w, run)
    assert (recorded['data']['transport_state'], recorded['data']['provider_outcome']) == ('not_sent', 'not_sent')
    state = run_state(w, run)
    assert state['state'] == 'failed' and state['error']['code'] == 'provider_error'


def test_a_busy_run_row_ends_the_attempt_with_its_deadline_and_leaves_the_record_to_recovery(workspace, stub, monkeypatch, tmp_path):
    """Probe 3 of the R1 review: another transaction holds the Run row for a second after the connection.
    With a 0.2 s limit the wait for the lock ends with the deadline and the attempt returns within 0.6 s;
    the record of the withheld POST waits for the busy row only briefly and stays with the existing recovery.
    Nothing is POSTed, before or after the release of the row."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=0.2)
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w))
    worker = executor(w)
    original = transport.ReaderConnection.connect
    locked, released = threading.Event(), threading.Event()
    errors = []

    def hold_lock():
        try:
            with w.engine.begin() as c:
                c.execute(text('SELECT id FROM aist.runs WHERE id=:id FOR UPDATE'), {'id': run['id']})
                locked.set()
                time.sleep(1.0)
        except Exception as error:
            errors.append(type(error).__name__)
            locked.set()
        released.set()

    holder = threading.Thread(target=hold_lock)

    def connect(connection):
        original(connection)
        holder.start()
        assert locked.wait(2) and not errors

    monkeypatch.setattr(transport.ReaderConnection, 'connect', connect)
    started = time.monotonic()
    with pytest.raises(ApiError) as failed:
        post(worker, run, tmp_path)
    elapsed = time.monotonic() - started
    print(json.dumps(dict(case='run_row_lock_deadline', limit=0.2, elapsed=round(elapsed, 3), posts=len(stub.received), errors=errors)))
    assert elapsed < 0.6, elapsed
    assert failed.value.code == 'outcome_unknown' and not stub.received and not errors
    # Returned while the row was still held: the known outcome could not be recorded yet, the reservation stays.
    recorded, = calls(w, run)
    assert recorded['data']['transport_state'] == 'attempted' and run_state(w, run)['state'] == 'running'
    holder.join(3)
    assert released.is_set() and not errors and not stub.received, 'no late POST after the release of the row'
    recorded, = calls(w, run)
    assert recorded['data']['transport_state'] == 'attempted' and Decimal(recorded['exposure']) == Decimal(w.runs.policy['quote_usd'])
    # The existing recovery reconciles the attempted call and the Run; the old epoch is fenced, nothing is sent again.
    w.runs.recover(owned=run['worker_id'])
    recorded, = calls(w, run)
    assert recorded['data']['transport_state'] == 'unknown' and run_state(w, run)['state'] == 'recovery_required'
    with pytest.raises(ApiError) as fenced:
        post(worker, run, tmp_path / 'again', 2)
    assert fenced.value.code == 'outcome_unknown' and not stub.received and len(calls(w, run)) == 1
