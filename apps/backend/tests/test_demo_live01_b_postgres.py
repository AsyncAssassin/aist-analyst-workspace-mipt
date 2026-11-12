"""Read-only note dialogue through the installed executor and the internal Messages simulator."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time

import pytest

from app import collection, openclaw_runtime as runtime, runs as run_clock, transport
from app.errors import ApiError
from app.normalization import fingerprint, checked
from app.runs import execute, json_value
from app.service import ident
from app.versions import write as write_version
from test_chat01_postgres import body, conversation, send, reply as save_answer
from test_d15_postgres import workspace, publish_fixture_worker
from test_fable_postgres import native, start_fable, reply, extracted, audit, FABLE, FABLE_FULL_ID, GOOD, QUAL, MIXED
from test_flow01_postgres import executor, current
from test_perf02_r1_postgres import upload, fact_rows
from test_perf02_r4_postgres import note_source, small_note, save_args, ref

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def seed_note(w):
    source, _ = note_source(w)
    _, _, run = start_fable(w, full=True, source=source)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(small_note(source)))
    assert saved['ok'], saved
    first = saved['result']
    payload = deepcopy(first['version']['payload'])
    payload['content']['title'] = 'Ручная версия синтетической справки'
    _, manual = write_version(w.runs, w.actor, w.session, w.project, first['version']['artifact_id'], None,
        'saveVersion', ident('key'), dict(expected_revision=1, parent_version_id=first['version']['id'], payload=payload))
    return source, first, manual


def ask(w, source, context=None, *, conv=None, revision=0, question='Какой собственный капитал?', key=None):
    conv = conv or conversation(w)
    request = body(revision, [source['id']], question)
    request['model_profile_version_id'] = FABLE_FULL_ID
    if context:
        request['context_version_id'] = context['version']['id']
    response = send(w, conv, request, key)
    return conv, request, response, w.runs.claim('demo-live-b')


def answer(source, *, previous=False):
    return dict(claims=[dict(text=('Собственный капитал на конец 2022 года составил 3216 млн руб.' if previous else
                                  'Собственный капитал на 31.03.2023 составил 3658 млн руб.'),
        origin='source_fact', fact_refs=[ref(source, 'fact_capital2022' if previous else 'fact_capital1q23')],
        calculation_ids=[])], missing_information=[])


def installed(w, run, native, label):
    """Same runtime and durable tools as production; only the HTTPS socket points at the local fixture."""
    native.last_agent = None
    with TemporaryDirectory(prefix='demo-live-b-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0)); provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close(); provider.close()
        if (run.get('checkpoint') or {}).get('turn'):
            (Path(root) / run['id']).mkdir()
        worker = executor(w)
        timer = threading.Timer(100, worker.draining.set)
        timer.start()
        try:
            runtime.run(worker, run)
            w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
        finally:
            timer.cancel()
            destination = Path('/result') / label
            destination.mkdir(exist_ok=True)
            directory = runtime.execution_directory(Path(root), run)
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
    state = w.runs.get(w.actor, w.project, run['id'])
    calls, usage = audit(w, run)
    (destination / 'result.json').write_text(json_value(dict(state=state, calls=calls, usage=usage)))
    assert not native.prefix_failures
    return state


def test_installed_dialogue_old_manual_followup_and_legacy_receipts(workspace, native):
    w = workspace
    source, old, manual = seed_note(w)
    key = ident('key')
    conv, request, _, run = ask(w, source, old, key=key)
    assert run['configuration']['note_context'] == old
    assert run['input']['base_version_id'] is None and run['input']['target_artifact_id'] is None
    assert run['input']['expected_artifact_revision'] == 0
    assert {k: run['input']['budget'][k] for k in ('max_duration_seconds', 'max_model_calls', 'max_tool_calls')} == dict(
        max_duration_seconds=300, max_model_calls=4, max_tool_calls=12)
    assert send(w, conv, request, key)[1]['run']['id'] == run['id']
    with pytest.raises(ApiError) as conflict:
        send(w, conv, request | {'context_version_id': manual['version']['id']}, key)
    assert conflict.value.code == 'idempotency_conflict'
    native.queue = [dict(body=reply('aist_save_artifact', {'content': answer(source)}))]
    first = installed(w, run, native, 'old-note-answer')
    assert first['state'] == 'completed', first
    view = w.runs.version(w.actor, w.project, first['result_version_id'])
    assert view['version']['payload']['result_type'] == 'evidence_answer'
    assert view['version']['parent_version_id'] is None and view['version']['artifact_id'] != old['version']['artifact_id']
    assert view['checks'][0]['status'] == 'passed', view['checks']
    assert old['version']['id'] in json_value(native.requests[0])
    conv, _, _, followup = ask(w, source, manual, conv=conv, revision=2, question='А годом раньше?')
    assert followup['configuration']['note_context'] == manual
    history = followup['configuration']['conversation_context']['messages']
    assert len(history) == 2 and all(m['context_version_id'] == old['version']['id'] for m in history)
    native.queue = [dict(body=reply('aist_save_artifact', {'content': answer(source, previous=True)}))]
    later = installed(w, followup, native, 'manual-followup')
    assert later['state'] == 'completed', later
    assert 'А годом раньше?' in json_value(native.requests[-1])
    assert w.runs.version(w.actor, w.project, old['version']['id']) == old
    assert w.runs.version(w.actor, w.project, manual['version']['id']) == manual
    assert len(native.requests) == 2 and all(call['purpose'] == 'agent' for call in audit(w, run)[0] + audit(w, followup)[0])
    assert len(w.runs.listing(w.actor, w.project, 'messages', {}, conv['id'])['items']) == 4
    # An old body stays byte-for-byte meaningful to its old fingerprint and receipt after completion.
    legacy_key = ident('key')
    old_conv, legacy, _, legacy_run = ask(w, source, key=legacy_key)
    assert 'context_version_id' not in legacy_run['input'] and 'note_context' not in legacy_run['configuration']
    assert save_answer(w, legacy_run)['ok']
    assert send(w, old_conv, legacy, legacy_key)[1]['run']['id'] == legacy_run['id']
    with w.engine.connect() as c:
        receipt = execute(c, 'SELECT fingerprint FROM aist.run_receipts WHERE key=:key', key=legacy_key).scalar_one()
        assert receipt == fingerprint(legacy)
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0


def test_context_access_type_and_excluded_history(workspace):
    w = workspace
    source, old, manual = seed_note(w)
    conv, request, _, run = ask(w, source, manual)
    assert save_answer(w, run, 'Сведения в записке требуют сверки с источником.')['ok']
    state = w.runs.get(w.actor, w.project, run['id'])
    other_source = upload(w, 'other-source', MIXED)
    cases = [dict(context_version_id='av_missing'), dict(source_version_ids=[other_source['id']]),
             dict(context_version_id=state['result_version_id']), dict(base_version_id=old['version']['id'], expected_artifact_revision=2)]
    for patch in cases:
        with pytest.raises(ApiError):
            send(w, conversation(w), request | patch)
    _, other_project = w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Other project'})
    with w.engine.connect() as c:
        for actor, project in [('usr_foreign', w.project), (w.actor, other_project['id'])]:
            from app.conversation import note_context
            with pytest.raises(ApiError) as denied:
                note_context(w.service, c, actor, project, request, run['input']['source_versions'])
            assert denied.value.code == 'resource_not_found'
    _, _, _, next_run = ask(w, other_source, conv=conv, revision=2, question='Объясни предыдущую записку')
    frozen = next_run['configuration']['conversation_context']
    assert len(frozen['messages']) == 2 and all(m['excluded_sources'] for m in frozen['messages'])
    assert manual['version']['id'] not in json_value(frozen)
    assert 'Сведения в записке' not in json_value(frozen)
    assert all(not m['artifact_version_id'] for m in frozen['messages'])
    with pytest.raises(ApiError):
        w.runs.tool(next_run['id'], next_run['epoch'], ident('tool'), 'aist_read_source', dict(source_version_id=source['id'], page=1))


def test_installed_one_clarification_same_run_and_deadline(workspace, native, monkeypatch):
    w = workspace
    source, old, _ = seed_note(w)
    _, _, _, run = ask(w, source, old, question='А какой капитал?')
    prompt = 'За какой период нужен собственный капитал?'
    native.queue = [dict(body=reply('aist_request_input', dict(idempotency_key=ident('key'), expected_revision=run['revision'], prompt=prompt)))]
    waiting = installed(w, run, native, 'clarification')
    assert waiting['state'] == 'waiting_for_user', waiting
    original = current(w, run)['created_at']
    reply_body = dict(expected_revision=waiting['revision'], waiting_input_id=waiting['waiting_input']['id'], text='На конец прошлого года')
    key = ident('key')
    publish_fixture_worker(w)
    w.runs.write(w.actor, w.session, w.project, run['id'], 'resumeRun', key, reply_body)
    resumed = w.runs.claim('demo-live-b')
    assert resumed['id'] == run['id'] and resumed['created_at'] == original
    assert resumed['configuration'] == run['configuration']
    refused = w.runs.tool(resumed['id'], resumed['epoch'], ident('tool'), 'aist_request_input', dict(
        idempotency_key=ident('key'), expected_revision=resumed['revision'], prompt='Еще один вопрос?'))
    assert not refused['ok'] and 'Уточнение уже задано' in refused['error']['message']
    native.queue = [dict(body=reply('aist_save_artifact', {'content': answer(source, previous=True)}, identifier='toolu_after_clarification'))]
    finished = installed(w, resumed, native, 'clarification-answer')
    assert finished['state'] == 'completed', finished
    assert prompt in json_value(native.requests[-1]) and reply_body['text'] in json_value(native.requests[-1])
    assert w.runs.write(w.actor, w.session, w.project, run['id'], 'resumeRun', key, reply_body)[0] == 200
    _, _, _, expired = ask(w, source, old)
    assert w.runs.tool(expired['id'], expired['epoch'], ident('tool'), 'aist_request_input', dict(
        idempotency_key=ident('key'), expected_revision=expired['revision'], prompt=prompt))['ok']
    wait = w.runs.get(w.actor, w.project, expired['id'])
    monkeypatch.setattr(run_clock, 'utcnow', lambda: expired['created_at'] + timedelta(seconds=301))
    with pytest.raises(ApiError) as late:
        w.runs.write(w.actor, w.session, w.project, expired['id'], 'resumeRun', ident('key'), dict(
            expected_revision=wait['revision'], waiting_input_id=wait['waiting_input']['id'], text='Ответ'))
    assert late.value.code == 'budget_exceeded' and len(native.requests) == 2


def test_installed_limitation_does_not_promote_unverified_number(workspace, native):
    w = workspace
    source, old, _ = seed_note(w)
    _, _, _, run = ask(w, source, old, question='Подтверди непроверенное число из текста записки')
    bad = dict(claims=[], missing_information=['Непроверенное число 942 млн рублей.'])
    invalid_claim = dict(text='Непроверенное значение', origin='source_candidate', fact_refs=[], calculation_ids=[], review_candidate_ids=['candidate_fixture'])
    with pytest.raises(ApiError):
        checked('AnswerContent', dict(claims=[invalid_claim], missing_information=[]))
    native.queue = [dict(body=reply('aist_save_artifact', {'content': bad}, identifier='toolu_bad_number')),
                    dict(body=reply('aist_save_artifact', {'content': dict(claims=[], missing_information=[
                        'Это значение не подтверждено сохраненными фактами. Сверьте выбранную записку с источником.'])}, identifier='toolu_limitation'))]
    state = installed(w, run, native, 'unverified-limitation')
    assert state['state'] == 'completed', state
    saved = w.runs.version(w.actor, w.project, state['result_version_id'])
    assert saved['version']['basis']['facts'] == []
    assert '942' not in json_value(saved['version']['payload']['content'])
    assert saved['version']['provenance']['source_versions'] == old['version']['provenance']['source_versions']
    assert w.runs.version(w.actor, w.project, old['version']['id']) == old
    assert all(c['purpose'] == 'agent' for c in audit(w, run)[0])


def test_dialogue_one_extraction_exact_replay_and_reserved_calls(workspace, native, tmp_path):
    w = workspace
    source, _, _ = seed_note(w)
    other = upload(w, 'new-indicator', MIXED)
    _, _, _, run = ask(w, other, question='Какая выручка за квартал?')
    native.queue = [dict(body=extracted([GOOD]))]
    args = dict(source_version_id=other['id'], question='Выручка за 3M23')
    worker = executor(w)
    result = runtime.obtain_facts(worker, run, args, tmp_path, None)
    assert result['status'] == 'complete' and result['facts']
    assert runtime.obtain_facts(worker, current(w, run), args, tmp_path / 'replay', None) == result
    with pytest.raises(ApiError):
        runtime.obtain_facts(worker, current(w, run), args | {'question': 'Другой показатель'}, tmp_path, None)
    assert len(native.requests) == 1
    _, _, _, reserved = ask(w, source)
    for _ in range(2):
        call = worker.transport.ledger.reserve(reserved['id'], reserved['epoch'])
        worker.transport.ledger.observe(reserved['id'], reserved['epoch'], call['id'], 200, extracted([]), native=True)
    with pytest.raises(ApiError):
        runtime.obtain_facts(worker, reserved, dict(source_version_id=source['id'], question='Неизвестный показатель'), tmp_path, None)
    assert collection.state(current(w, reserved))['reason'] == 'reserve_calls'
    assert len(native.requests) == 1


def test_installed_new_indicator_extracts_once_and_saves_answer(workspace, native):
    w = workspace
    seed_note(w)
    source = upload(w, 'new-financial-indicator', MIXED)
    _, _, _, run = ask(w, source, question='Какая выручка за 3M23?')
    def final_answer():
        fact = next(f['data'] for f in fact_rows(w) if f['data']['source']['source_version_id'] == source['id'])
        claim = dict(text='Выручка за 3M23 составила 100 млн руб.', origin='source_fact',
                     fact_refs=[ref(source, fact['fact_id'])], calculation_ids=[])
        return reply('aist_save_artifact', {'content':dict(claims=[claim], missing_information=[])}, identifier='toolu_extracted_answer')
    native.queue = [dict(body=reply('aist_get_facts', {'question':'Выручка за 3M23'})),
                    dict(body=extracted([GOOD])), dict(body=final_answer)]
    state = installed(w, run, native, 'new-indicator-answer')
    assert state['state'] == 'completed', state
    saved = w.runs.version(w.actor, w.project, state['result_version_id'])
    assert saved['version']['payload']['result_type'] == 'evidence_answer'
    assert saved['checks'][0]['status'] == 'passed'
    calls, _ = audit(w, run)
    assert [call['purpose'] for call in calls] == ['agent', 'extraction', 'agent']
    assert len(native.requests) == 3


@pytest.mark.parametrize('remaining,delay,allowed', [(300, 0, True), (150, 0, True), (121, 0, True), (120, 0, False), (119, 0, False), (150, 31, False)])
@pytest.mark.parametrize('stage', ['tls', 'reserve'])
def test_dialogue_extraction_window_includes_tls_and_database(workspace, native, monkeypatch, tmp_path, remaining, delay, allowed, stage):
    w = workspace
    seed_note(w)
    source = upload(w, 'window', MIXED)
    _, _, _, run = ask(w, source)
    offset = [0]
    monotonic = time.monotonic
    monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=300 - remaining + offset[0]))
    assert collection.send_seconds(run, 600) == min(180, remaining)
    assert collection.send_seconds(run, 600, 'extraction') == min(120, remaining - 90)
    worker = executor(w)
    if delay and stage == 'tls':
        connect = transport.ReaderConnection.connect
        def delayed(connection):
            connect(connection)
            offset[0] += delay
        monkeypatch.setattr(transport.ReaderConnection, 'connect', delayed)
    if delay and stage == 'reserve':
        reserve = worker.transport.ledger.reserve
        def delayed_reserve(*args, **kwargs):
            result = reserve(*args, **kwargs)
            offset[0] += delay
            return result
        monkeypatch.setattr(worker.transport.ledger, 'reserve', delayed_reserve)
    if delay:
        monkeypatch.setattr(transport.time, 'monotonic', lambda: monotonic() + offset[0])
    native.queue = [dict(body=extracted([QUAL]))]
    args = dict(source_version_id=source['id'], question='Бизнес')
    if allowed:
        assert runtime.obtain_facts(worker, run, args, tmp_path, None)['status'] == 'complete'
        assert len(native.requests) == 1
    else:
        with pytest.raises(ApiError):
            runtime.obtain_facts(worker, run, args, tmp_path, None)
        assert not native.requests


def test_candidate_and_visual_context_remains_unverified(workspace):
    from test_note_data01_b_postgres import prepared, content, save
    w = workspace
    source, candidate_run, result = prepared(w, visual=True)
    view, _ = save(w, candidate_run, content(result, mixed_basis=True))
    seed_note(w)  # Enable the native profile on this isolated fixture without any model send.
    _, _, _, run = ask(w, source, view, question='Что означает непроверенное значение?')
    assert run['configuration']['note_context'] == view
    assert view['version']['basis']['review_candidates'] and view['version']['basis']['visual_observations']
    for field, value in [('review_candidate_ids', [result['skipped'][0]['review_candidate']['candidate_id']]),
                         ('visual_observation_ids', [result['visual_observations'][0]['observation_id']])]:
        claim = dict(text='Показатель', origin='source_fact', fact_refs=[], calculation_ids=[])
        with pytest.raises(ApiError):
            checked('AnswerContent', dict(claims=[claim | {field: value}], missing_information=[]))
    assert not save_answer(w, run, 'Непроверенное значение: 3,7 трлн рублей.')['ok']
    assert save_answer(w, run, 'Значение требует сверки с источником. Подтвержденного факта для ответа нет.')['ok']
    assert w.runs.version(w.actor, w.project, view['version']['id']) == view


def test_native_dialogue_capabilities_and_hard_counts(workspace):
    w = workspace
    source, old, _ = seed_note(w)
    assert {s['id'] for s in w.runs.registry['skills'] if s['status'] == 'enabled'} == {
        'skill_analytical-note-v3', 'skill_source-answer-v3'}
    _, _, _, run = ask(w, source, old)
    schema = runtime.tool_schema('aist_get_facts', source_versions=run['input']['source_versions'])
    assert 'question' in schema['properties'] and 'extract' not in schema['properties']
    worker = executor(w)
    for _ in range(4):
        call = worker.transport.ledger.reserve(run['id'], run['epoch'])
        worker.transport.ledger.observe(run['id'], run['epoch'], call['id'], 200, extracted([]), native=True)
    with pytest.raises(ApiError) as exhausted:
        worker.transport.ledger.reserve(run['id'], run['epoch'])
    assert exhausted.value.code == 'budget_exceeded'
    for _ in range(12):
        assert w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', {'refs': [ref(source, 'fact_capital2022')]})['ok']
    with pytest.raises(ApiError) as tools_exhausted:
        w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', {'refs': []})
    assert tools_exhausted.value.code == 'budget_exceeded'


@pytest.mark.parametrize('failure', ['close', '429'])
def test_dialogue_unknown_send_never_repeats(workspace, native, tmp_path, failure):
    w = workspace
    source, old, _ = seed_note(w)
    key = ident('key')
    conv, request, _, run = ask(w, source, old, key=key)
    native.queue = [dict(close=True)] if failure == 'close' else [dict(status=429, body={'type':'error','error':{'type':'rate_limit_error'}})]
    boundary = runtime.Boundary(executor(w), run, tmp_path, 'fixture')
    for _ in range(2):
        with pytest.raises(ApiError):
            boundary.model(dict(model=FABLE['model'], messages=[dict(role='user', content='Вопрос')]))
    w.runs.recover(owned=run['worker_id'])
    restored = runtime.Boundary(executor(w), current(w, run), tmp_path / 'restarted', 'fixture')
    with pytest.raises(ApiError):
        restored.model(dict(model=FABLE['model'], messages=[dict(role='user', content='Вопрос')]))
    assert w.runs.claim('restarted-worker') is None
    assert send(w, conv, request, key)[1]['run']['id'] == run['id']
    assert len(native.requests) == len(audit(w, run)[0]) == 1
    assert w.runs.version(w.actor, w.project, old['version']['id']) == old
