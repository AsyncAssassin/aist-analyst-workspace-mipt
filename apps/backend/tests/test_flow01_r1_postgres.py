"""FLOW-01.01-R1: the completion reserve is checked again right before every physical POST, and one
absolute deadline bounds the whole HTTP attempt. The review probes moved into regressions.

A shifted Run clock models the preparation and the pause that took longer than planned; a loopback HTTP
server models trickled headers, a slow body and a slow connection. No model is involved."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from test_perf02_r4_postgres import model_save_args

from app import collection, openclaw_runtime, transport
from app.errors import ApiError
from app.openclaw_runtime import Boundary
from app.runs import json_value
from test_d15_postgres import workspace
from test_perf02_r3_postgres import calls, generation, refusal, stub
from test_perf02_r4_postgres import AT_2023, note_source, small_note
from test_flow01_postgres import (QUESTIONS, SHIFT, agent, ask, closed_events, counts, current, executor, extraction,
                                  hints, invoke, note_run, plain_source, qualitative_item, request, shift_clock, step,
                                  versions, wrong_item)

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def reset_clock(monkeypatch, seconds=0.0):
    """The Run clock this many seconds ahead of the real one."""
    shift_clock(monkeypatch, seconds - SHIFT[0])


def overrunning_pause(monkeypatch, seconds):
    """A granted pause whose actual wait is longer: the clock moves by this many seconds, the pause returns normally."""
    granted = []

    def pause(self, active_run, pause_seconds):
        granted.append(pause_seconds)
        shift_clock(monkeypatch, seconds)
        return True

    monkeypatch.setattr(transport.Transport, 'pause', pause)
    return granted


def connection_class(monkeypatch, *, clock_shift=0.0, sleep=0.0):
    """Connections to the stub whose connect() takes longer: on the Run clock (clock_shift) or in real time (sleep)."""
    factory = transport.ReaderHTTPSConnection

    class Delayed(transport.ReaderConnection):
        def connect(self):
            time.sleep(sleep)
            super().connect()
            shift_clock(monkeypatch, clock_shift)

    def build(*args, **kwargs):
        plain = factory(*args, **kwargs)
        return Delayed(plain.host, plain.port, timeout=plain.timeout)

    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', build)


def run_state(w, run):
    return w.runs.get(w.actor, w.project, run['id'])


def test_a_delayed_preparation_withholds_the_nested_post_and_completes_the_agent_context(workspace, stub, monkeypatch, tmp_path):
    """Probe 2 of the review: 220 s after the creation the reserve fits (220 + 180 + 180 = 580); a preparation
    of 30 s makes it 610. The nested POST is withheld at the send boundary, the collection closes and the
    agent gets the limitation with the Run still running. Before an agent request the same delay sends the
    completion context with the same single ordinary call."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=180)
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w), max_duration_seconds=600)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    assert 'Сбор оснований ограничен' in hints(stub)[-1]
    shift_clock(monkeypatch, 220)
    original = openclaw_runtime.extraction_request

    def delayed_request(*args, **kwargs):
        value = original(*args, **kwargs)
        shift_clock(monkeypatch, 30)
        return value

    monkeypatch.setattr(openclaw_runtime, 'extraction_request', delayed_request)
    stub.queue = [extraction([qualitative_item(2, 3)])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded'
    assert 'Сбор оснований закрыт' in reply['error']['message'] and 'Сохраненных результатов извлечений нет' in reply['error']['message']
    assert len(stub.received) == 1 and stub.queue, 'the nested extraction must not be sent'
    state = collection.state(current(w, run))
    assert state['closed'] and state['reason'] == 'reserve_time'
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['reserve_time']
    assert len(calls(w, run)) == 1 and calls(w, run)[0]['data']['purpose'] == 'agent'
    assert run_state(w, run)['state'] == 'running' and counts(w, run) == ((1, 0), (0, 0))
    monkeypatch.setattr(openclaw_runtime, 'extraction_request', original)
    # The next agent request is already composed for the completion mode; no POST was spent on the withheld extraction.
    stub.queue = []
    assert step(boundary, stub, agent(content='Составляю записку по имеющимся основаниям.'), 2) == []
    assert 'Режим завершения' in hints(stub)[-1] and len(stub.received) == 2 and counts(w, run) == ((2, 0), (0, 0))
    # An agent request of a fresh Run at 220 s whose connection takes 30 s on the Run clock: the collection closes
    # at the send boundary and the same request leaves with the completion context, as one ordinary call.
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w), max_duration_seconds=600)
    boundary = Boundary(executor(w), run, tmp_path / 'agent', 'synthetic')
    shift_clock(monkeypatch, 220)
    connection_class(monkeypatch, clock_shift=30)
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    assert 'Режим завершения' in hints(stub)[-1] and 'Сбор оснований ограничен' not in hints(stub)[-1]
    assert len(stub.received) == 3 and counts(w, run) == ((1, 0), (0, 0))
    state = collection.state(current(w, run))
    assert state['closed'] and state['reason'] == 'reserve_time' and run_state(w, run)['state'] == 'running'
    sent = calls(w, run)[-1]['data']
    assert sent['transport_state'] == 'observed' and sent['provider_outcome'] == 'success'
    audit = list((tmp_path / 'agent' / 'calls').glob('*-request.json'))
    assert len(audit) == 1 and 'Режим завершения' in audit[0].read_text() and len(list((tmp_path / 'agent' / 'calls').glob('*.claim'))) == 1
    # The extraction the agent asked for in that reply is refused without a POST; the saved outcomes stay available.
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded' and len(stub.received) == 3


def test_a_pause_that_overran_withholds_the_nested_retry_and_the_draft_uses_the_completion_reserve(workspace, stub, monkeypatch, tmp_path):
    """Probe 1 of the review: a known 429 at 220 s grants a 10 s pause, the wait actually takes 30 s. The retry
    is withheld at the send boundary without a second charge of the ordinary or extraction quota; the
    collection closes, the Run keeps running and the agent saves one draft on the basis available before the Run."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=180)
    reset_clock(monkeypatch)
    source, saved = note_source(w)
    run = note_run(w, source, max_duration_seconds=600)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask('Облигации на 31.03.2023')), 1)
    shift_clock(monkeypatch, 220)
    granted = overrunning_pause(monkeypatch, 30)
    stub.queue = [refusal(), extraction([wrong_item('Синтетика', 7, 'Облигации', 'Данные компании', AT_2023)])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded' and 'Сбор оснований закрыт' in reply['error']['message']
    assert granted == [10.0] and len(stub.received) == 2 and stub.queue, 'the retry must not be sent'
    assert counts(w, run) == ((2, 1), (1, 1))
    refused, withheld = [c['data'] for c in calls(w, run)[-2:]]
    assert refused['http_status'] == 429 and 'Повторная отправка 1 из 3 через 10.0 с' in refused['error']['message']
    assert withheld['parent_model_call_id'] == refused['id'] and withheld['transport_state'] == 'not_sent'
    assert withheld['error']['code'] == 'budget_exceeded' and 'оставшееся время сохранено' in withheld['error']['message']
    state = collection.state(current(w, run))
    assert state['closed'] and state['reason'] == 'reserve_time' and run_state(w, run)['state'] == 'running'
    # 350 s and six calls remain: the completion mode saves the draft with remarks, nothing is extracted again.
    stub.queue = []
    tool, = step(boundary, stub, agent(('aist_save_artifact', model_save_args(small_note(source)))), 2)
    assert 'Режим завершения' in hints(stub)[-1]
    saved = invoke(boundary, run, tool)
    assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
    state = run_state(w, run)
    assert state['state'] == 'running' and versions(w) == [] and len(stub.received) == 3


def test_an_agent_retry_that_stopped_fitting_after_the_pause_keeps_the_provider_refusal(workspace, stub, monkeypatch, tmp_path):
    """The agent retry keeps its body and its refusal: when it no longer fits after the pause it is not sent,
    it is not declared a new ordinary call, and the Run ends with the provider refusal it was repeating."""
    w = workspace
    w.runs.settings = replace(w.runs.settings, transport_seconds=180)
    reset_clock(monkeypatch)
    run = note_run(w, plain_source(w), max_duration_seconds=600)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    shift_clock(monkeypatch, 220)
    granted = overrunning_pause(monkeypatch, 30)
    stub.queue = [refusal(), agent(ask(QUESTIONS[0]))]
    with pytest.raises(ApiError) as ended:
        boundary.model(request(1))
    assert ended.value.code == 'run_terminal' and granted == [10.0] and len(stub.received) == 1
    refused, withheld = calls(w, run)
    assert refused['data']['http_status'] == 429 and withheld['parent_model_call_id'] == refused['data']['id']
    assert withheld['data']['transport_state'] == 'not_sent' and withheld['data']['error']['code'] == 'budget_exceeded'
    state = run_state(w, run)
    assert state['state'] == 'failed' and state['error']['code'] == 'provider_error' and 'после паузы не выполнена' in state['error']['message']
    assert counts(w, run) == ((1, 1), (0, 0))
    with pytest.raises(ApiError) as terminal:
        boundary.model(request(2))
    assert terminal.value.code == 'run_terminal' and len(stub.received) == 1


@pytest.fixture
def slow_server(monkeypatch):
    """A loopback model endpoint that trickles headers or the body, delays the reply, or answers at once."""
    mode = SimpleNamespace(kind='fast', seconds=0.0, body=json_value(generation()).encode(), received=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            mode.received += 1
            length = b'Content-Length: ' + str(len(mode.body)).encode() + b'\r\n\r\n'
            try:
                if mode.kind == 'headers':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: '); self.wfile.flush()
                    for _ in range(10):
                        time.sleep(0.08); self.wfile.write(b'x'); self.wfile.flush()
                    self.wfile.write(b'\r\n' + length + mode.body)
                elif mode.kind == 'body':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n' + length); self.wfile.flush()
                    for index in range(len(mode.body)):
                        time.sleep(0.08); self.wfile.write(mode.body[index:index + 1]); self.wfile.flush()
                else:
                    time.sleep(mode.seconds)
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n' + length + mode.body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever); thread.start()
    monkeypatch.setattr('app.novita.credential', lambda path: 'local-credential-not-in-response')
    monkeypatch.setattr(transport, 'ReaderHTTPSConnection', lambda *a, **kw: transport.ReaderConnection('127.0.0.1', server.server_port, timeout=3))
    try:
        yield mode
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(worker, run, directory):
    return worker.transport.perform(run, {'purpose': 'agent'}, request=request(), audit=directory / 'calls')


def test_one_absolute_deadline_bounds_headers_body_and_connection(workspace, slow_server, monkeypatch, tmp_path):
    """Probe 3 of the review: with a 0.2 s send limit the trickled headers (one byte per 0.08 s) end in an unknown
    outcome within 0.6 s, measured without the stop of the server; a trickled body and a slow connection do not
    extend the deadline either; a completed attempt releases its timer so fast replies keep succeeding."""
    w = workspace
    mode = slow_server
    reset_clock(monkeypatch)
    w.runs.settings = replace(w.runs.settings, transport_seconds=0.2)
    worker = executor(w)
    source = plain_source(w)
    for kind in ('headers', 'body'):
        mode.kind = kind
        run = note_run(w, source)
        started = time.monotonic()
        with pytest.raises(ApiError) as unknown:
            post(worker, run, tmp_path / kind)
        elapsed = time.monotonic() - started
        assert unknown.value.code == 'outcome_unknown' and elapsed < 0.6, (kind, elapsed)
        recorded, = calls(w, run)
        assert recorded['data']['transport_state'] == 'unknown' and run_state(w, run)['state'] == 'recovery_required'
    # The connection itself is part of the attempt: with 1.5 s in total, a 0.3 s connection leaves room for a reply
    # after 0.6 s; a 1.1 s connection does not, although the reply alone would fit the limit.
    w.runs.settings = replace(w.runs.settings, transport_seconds=1.5)
    mode.kind, mode.seconds = 'delay', 0.6
    connection_class(monkeypatch, sleep=0.3)
    run = note_run(w, source)
    call, payload = post(worker, run, tmp_path / 'connected')
    assert payload['choices'] and calls(w, run)[0]['data']['provider_outcome'] == 'success'
    connection_class(monkeypatch, sleep=1.1)
    run = note_run(w, source)
    started = time.monotonic()
    with pytest.raises(ApiError) as unknown:
        post(worker, run, tmp_path / 'late-connection')
    assert unknown.value.code == 'outcome_unknown' and time.monotonic() - started < 2.2
    assert calls(w, run)[0]['data']['transport_state'] == 'unknown' and run_state(w, run)['state'] == 'recovery_required'
    # The deadline passed during the connection: the POST is forbidden and the call is recorded as not sent.
    w.runs.settings = replace(w.runs.settings, transport_seconds=0.2)
    mode.kind, mode.seconds = 'fast', 0.0
    received = mode.received
    connection_class(monkeypatch, sleep=0.4)
    run = note_run(w, source)
    with pytest.raises(ApiError):
        post(worker, run, tmp_path / 'expired')
    assert mode.received == received, 'no POST after the deadline'
    recorded, = calls(w, run)
    assert recorded['data']['transport_state'] == 'not_sent' and recorded['data']['provider_outcome'] == 'not_sent'
    state = run_state(w, run)
    assert state['state'] == 'failed' and state['error']['code'] == 'provider_error'
    # Fast replies: two attempts in a row succeed; the timer of a finished attempt touches nothing afterwards.
    connection_class(monkeypatch)
    run = note_run(w, source)
    first = post(worker, run, tmp_path / 'fast')
    second = worker.transport.perform(run, {'purpose': 'agent'}, first[0], request=request(2), audit=tmp_path / 'fast' / 'calls')
    assert first[1]['choices'] and second[1]['choices'] and mode.received == received + 2
    assert [c['data']['provider_outcome'] for c in calls(w, run)] == ['success', 'success'] and run_state(w, run)['state'] == 'running'
    watchdogs = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    for timer in watchdogs:
        timer.join(1)
    assert not [t for t in watchdogs if t.is_alive()], 'the watchdog of a finished attempt is released'
