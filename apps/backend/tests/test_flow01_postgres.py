"""FLOW-01: the bounded basis collection of a first v3 note on the loopback model stub and a shifted clock.

The stub answers one POST per queued entry; the Boundary is driven directly, as the installed
OpenClaw agent would drive it. No model is involved and no document is invented."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import threading
from types import SimpleNamespace

import pytest
from test_perf02_r4_postgres import model_save_args

from app import collection, runs as run_clock, transport
from app.accounting import attempt_counts, extraction_counts
from app.errors import ApiError
from app.live_profile import COLLECTION, MODEL
from app.openclaw_runtime import Boundary, executor_ended
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_perf02_r1_postgres import HEADER, PAGE_DATE, upload
from test_perf02_r3_postgres import calls, generation, record_pauses, refusal, stub
from test_perf02_r4_postgres import AT_2023, note_source, small_note

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

LINES = [HEADER, 'Бизнес компании', 'Компания поставляет оборудование клиентам.', 'Рынок компании',
         'Доля рынка компании растет.', 'Стратегия компании', 'Компания расширяет сеть продаж.',
         'Облигации', '943 млн руб.', 'на 31.12.2024']
QUESTIONS = ['Бизнес и клиенты компании', 'Рынок компании', 'Стратегия компании', 'Продукт компании']
CARDS = {QUESTIONS[0]: (2, 3), QUESTIONS[1]: (4, 5), QUESTIONS[2]: (6, 7)}


def plain_source(w):
    # Keep a second unrelated page: subsequent narrow questions can refine page 1 within the collection quota.
    return upload(w, 'flow01', LINES, following=[(['Logistics', 'Warehouse operations'], None)])


def qualitative_item(start, end):
    return dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None,
                basis=None, evidence=[dict(page=1, line_start=start, line_end=end, area='Карточка')])


def wrong_item(subject='Synthetic', line=9, label='Облигации', reporting=HEADER, period=PAGE_DATE):
    """A numeric candidate whose amount is absent from the cited line: the deterministic check refuses it."""
    return dict(metric=label, subject=subject, reporting_basis=reporting, nature='reported_actual', period=period,
                amount=dict(value='999', unit='money', currency='RUB'), value_text='999',
                evidence=[dict(page=1, line_start=line, line_end=line, area='Строка')],
                basis=dict(page=1, metric_label=label, unit_label='млн руб.', reporting_label=reporting))


def extraction(items):
    message = dict(role='assistant', content=json_value({'facts': items}))
    return dict(status=200, body=dict(id=ident('stub'), choices=[dict(index=0, message=message, finish_reason='stop')],
                                      usage=dict(prompt_tokens=100, completion_tokens=10)))


def agent(*tools, content='Локальная проверка завершена.'):
    """One agent reply: tool calls (name, args) or a final text without tools."""
    if tools:
        message = dict(role='assistant', content=None, tool_calls=[dict(id=ident('call'), type='function',
                       function=dict(name=name, arguments=json_value(args))) for name, args in tools])
    else:
        message = dict(role='assistant', content=content)
    return dict(status=200, body=dict(id=ident('stub'), choices=[dict(index=0, message=message,
                finish_reason='tool_calls' if tools else 'stop')], usage=dict(prompt_tokens=100, completion_tokens=10)))


def ask(question):
    return ('aist_get_facts', dict(question=question))


def note_run(w, source, **budget):
    """A first generation of a v3 note; the project policy is set once, before its first Run."""
    if w.runs.policy['quote_usd'] != '0.01' or budget:
        w.runs.policy = deepcopy(w.runs.policy)
        w.runs.policy['quote_usd'] = '0.01'
        w.runs.policy['run'].update(budget)
    request = body(sources=[source['id']], question='Подготовь справку о компании')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
    send(w, conversation(w), request)
    return w.runs.claim('flow01')


def executor(w):
    worker = SimpleNamespace(settings=w.runs.settings, runs=w.runs, draining=threading.Event(), closed=threading.Event(),
                             persistence=threading.RLock())
    worker.transport = transport.Transport(w.runs, worker.draining, worker.closed, worker.persistence, {}, threading.Lock())
    return worker


def request(turn=1, tool_message=None):
    messages = [dict(role='user', content=f'Шаг {turn}: подготовка записки')]
    if tool_message is not None:
        tool_call_id, reply = tool_message
        messages += [dict(role='assistant', content=None, tool_calls=[dict(id=tool_call_id, type='function',
                     function=dict(name='aist_save_artifact', arguments='{}'))]),
                     dict(role='tool', tool_call_id=tool_call_id, content=json_value(reply))]
    return dict(model=MODEL, messages=messages)


def step(boundary, stub, entry, turn, tool_message=None):
    """One agent request answered by the queued stub entry; returns the tool calls of the reply."""
    stub.queue.append(entry)
    payload = boundary.model(request(turn, tool_message))
    return payload['choices'][0]['message'].get('tool_calls') or []


def invoke(boundary, run, tool, tool_call_id=None):
    value = dict(session=run['executor_session_id'], tool_call_id=tool_call_id or tool['id'], name=tool['function']['name'],
                 args=json.loads(tool['function']['arguments']))
    return boundary.tool(value)


def hints(stub):
    return [json.loads(raw)['messages'][0]['content'] for raw in stub.received]


def current(w, run):
    with w.engine.connect() as c:
        return dict(execute(c, 'SELECT * FROM aist.runs WHERE id=:r', r=run['id']).mappings().one())


def closed_events(w, run):
    with w.engine.connect() as c:
        return [e for e in execute(c, 'SELECT data FROM aist.run_events WHERE run_id=:r ORDER BY seq', r=run['id']).scalars()
                if e['type'] == 'collection_closed']


def counts(w, run):
    with w.engine.connect() as c:
        return attempt_counts(c, run['id']), extraction_counts(c, run['id'])


SHIFT = [0.0]


def shift_clock(monkeypatch, seconds):
    """The controlled clock: every later reading of the Run clock is this many seconds further on."""
    SHIFT[0] += seconds
    monkeypatch.setattr(run_clock, 'utcnow', lambda: datetime.now(timezone.utc) + timedelta(seconds=SHIFT[0]))


def versions(w):
    with w.engine.connect() as c:
        return list(execute(c, 'SELECT data FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalars())


def note_content(fact):
    ref = dict(source_version_id=fact['source']['source_version_id'], fact_id=fact['fact_id'])
    return dict(title='Synthetic: информационная справка', audience='Аналитик', sections=[dict(title='Бизнес', blocks=[
        dict(kind='paragraph', claim=dict(text='Компания поставляет оборудование клиентам.', origin='source_fact',
                                          fact_refs=[ref], calculation_ids=[]))])])


def test_bounded_budget_takes_the_stricter_limits_and_leaves_other_runs_alone():
    wide = collection.bounded_budget(dict(max_model_calls=12, max_duration_seconds=3600), 'skill_analytical-note-v3', 'analytical_note', None)
    assert (wide['max_model_calls'], wide['max_duration_seconds'], wide['completion_calls']) == (12, 1800, 3)
    narrow = collection.bounded_budget(dict(max_model_calls=2, max_duration_seconds=300), 'skill_analytical-note-v3', 'analytical_note', None)
    assert (narrow['max_model_calls'], narrow['max_duration_seconds'], narrow['completion_calls']) == (2, 300, 2)
    policy = dict(max_model_calls=12, max_duration_seconds=3600)
    assert collection.bounded_budget(policy, 'skill_analytical-note-v3', 'analytical_note', 'av_base') is None
    assert collection.bounded_budget(policy, 'skill_source-answer-v3', 'evidence_answer', None) is None
    assert collection.bounded_budget(policy, 'skill_analytical-note-v2', 'analytical_note', None) is None
    assert COLLECTION['max_extractions'] == 3 and COLLECTION['idle_extractions'] == 2


def test_new_note_run_carries_the_bounded_budget_and_an_answer_run_does_not(workspace):
    w = workspace
    run = note_run(w, plain_source(w))
    assert run['configuration']['collection'] == collection.bounded_budget(w.runs.policy['run'], 'skill_analytical-note-v3', 'analytical_note', None)
    assert (run['input']['budget']['max_model_calls'], run['input']['budget']['max_duration_seconds']) == (8, 1200)
    assert w.runs.policy['run']['max_duration_seconds'] == 1200
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    send(w, conversation(w), body(sources=[plain_source(w)['id']], question='Короткий вопрос'))
    answer = w.runs.claim('flow01')
    assert 'collection' not in answer['configuration'] and answer['input']['budget'] == w.runs.policy['run']


def test_two_known_refusals_then_the_draft_with_remarks_is_saved(workspace, stub, tmp_path):
    """Check 1: two refused extractions close the collection; the agent then saves a structurally valid
    draft with remarks on the basis saved before the Run. The counters come from the saved calls."""
    w = workspace
    source, saved = note_source(w, following=[(['Logistics', 'Warehouse operations'], None)])
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    for turn, question in enumerate(('Облигации на 31.03.2023', 'Общий размер'), 1):
        tool, = step(boundary, stub, agent(ask(question)), turn)
        assert 'Сбор оснований ограничен' in hints(stub)[-1]
        stub.queue = [extraction([wrong_item('Синтетика', 7, 'Облигации', 'Данные компании', AT_2023)])]
        reply = invoke(boundary, run, tool)
        # FLOW-01.02: the wrong number is excluded with its reason; a result without usable facts is no progress.
        assert reply['ok'] and reply['result']['status'] == 'empty' and reply['result']['skipped'][0]['metric'] == 'Облигации'
        assert collection.state(current(w, run))['idle'] == turn
    assert collection.state(current(w, run)) ['closed'] and collection.state(current(w, run))['reason'] == 'idle'
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['idle']
    assert counts(w, run) == ((4, 0), (2, 0)) and len(stub.received) == 4
    tool, = step(boundary, stub, agent(('aist_save_artifact', model_save_args(small_note(source)))), 3)
    assert 'Режим завершения' in hints(stub)[-1] and 'исключено кандидатов: 1 (number_mismatch)' in hints(stub)[-1]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is True
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'completed' and len(versions(w)) == 1
    view = w.runs.version(w.actor, w.project, state['result_version_id'])
    assert view['checks'][0]['status'] == 'failed' and view['review']['acceptance'] is None
    assert counts(w, run) == ((5, 0), (2, 0)) and len(stub.received) == 5


def test_without_a_basis_no_document_is_invented(workspace, stub, tmp_path):
    """Check 1 and 6: a final text or an invalid save command never becomes a completed Run."""
    w = workspace
    run = note_run(w, plain_source(w))
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    for turn in (1, 2):
        tool, = step(boundary, stub, agent(ask(QUESTIONS[turn])), turn)
        stub.queue = [extraction([wrong_item()])]
        assert invoke(boundary, run, tool)['result']['status'] == 'empty'
    assert collection.state(current(w, run))['closed']
    tool, = step(boundary, stub, agent(('aist_save_artifact', dict(content='просто текст без структуры'))), 3)
    assert boundary.issued[tool['id']]['format_error'] and 'content:' in boundary.issued[tool['id']]['format_error']
    with pytest.raises(ApiError) as refused:
        invoke(boundary, run, tool)
    assert refused.value.code == 'validation_failed' and versions(w) == []
    assert step(boundary, stub, agent(content='Оснований недостаточно, записку составить нельзя.'), 4) == []
    assert boundary.ending.startswith('Оснований недостаточно')
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    executor_ended(w.runs, run, boundary)
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'failed' and state['error']['code'] == 'tool_failed'
    assert 'без сохранения результата' in state['error']['message'] and 'Оснований недостаточно' in state['error']['message']
    assert versions(w) == [] and len(stub.received) == 6


def test_three_extractions_then_no_fourth_post_and_the_completion_steps_stay_available(workspace, stub, tmp_path):
    """Check 2: three extractions fit only when the agent asks two in one reply (eight calls, three reserved);
    the fourth gets the limitation and the known results without a POST, the completion steps stay available."""
    w = workspace
    source = plain_source(w)
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    facts = []
    first, second = step(boundary, stub, agent(ask(QUESTIONS[0]), ask(QUESTIONS[1])), 1)
    assert 'извлечений выполнено 0 из 3' in hints(stub)[-1]
    for tool, question in ((first, QUESTIONS[0]), (second, QUESTIONS[1])):
        stub.queue = [extraction([qualitative_item(*CARDS[question])])]
        reply = invoke(boundary, run, tool)
        assert reply['ok'] is True and reply['result']['facts'], (reply.get('error') or {}).get('message')
        facts += reply['result']['facts']
    tool, = step(boundary, stub, agent(ask(QUESTIONS[2])), 2)
    assert 'извлечений выполнено 2 из 3' in hints(stub)[-1]
    stub.queue = [extraction([qualitative_item(*CARDS[QUESTIONS[2]])])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is True and reply['result']['facts'], (reply.get('error') or {}).get('message')
    facts += reply['result']['facts']
    assert counts(w, run) == ((5, 0), (3, 0)) and not collection.state(current(w, run))['closed']
    tool, = step(boundary, stub, agent(ask(QUESTIONS[3])), 3)
    assert collection.state(current(w, run))['reason'] == 'extractions'
    hint = hints(stub)[-1]
    assert 'Режим завершения' in hint and all(q in hint for q in QUESTIONS[:3]) and facts[0]['fact_id'] in hint
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded'
    assert 'Сбор оснований закрыт' in reply['error']['message'] and QUESTIONS[2] in reply['error']['message']
    assert len(stub.received) == 6 and counts(w, run) == ((6, 0), (3, 0))
    refs = [dict(source_version_id=f['source']['source_version_id'], fact_id=f['fact_id']) for f in facts]
    read = w.runs.tool(run['id'], run['epoch'], ident('read'), 'aist_get_facts', dict(refs=refs))
    assert read['ok'] and [f['fact_id'] for f in read['result']['facts']] == [f['fact_id'] for f in facts]
    tool, = step(boundary, stub, agent(('aist_save_artifact', model_save_args(note_content(read['result']['facts'][0])))), 4)
    saved = invoke(boundary, run, tool)
    assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
    assert versions(w) == [] and len(stub.received) == 7
    boundary.ending = 'Четвертая запрошенная тема не получена.'
    executor_ended(w.runs, run, boundary)
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'failed'
    with pytest.raises(ApiError) as terminal:
        boundary.model(request(5))
    assert terminal.value.code == 'run_terminal' and len(stub.received) == 7


def test_two_calls_in_one_reply_concurrent_commands_and_rephrasing_do_not_bypass_the_limits(workspace, stub, tmp_path):
    """Check 3: every tool call of one reply is checked, concurrent commands serialize, a new wording or
    tool call ID after the transition gets the limitation, and saved outcomes are returned without a POST."""
    w = workspace
    source = plain_source(w)
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    first, wrong = step(boundary, stub, agent(ask(QUESTIONS[0]), ask('Облигации компании')), 1)
    stub.queue = [extraction([qualitative_item(2, 3)])]
    assert invoke(boundary, run, first)['ok']
    stub.queue = [extraction([wrong_item()])]
    assert invoke(boundary, run, wrong)['result']['status'] == 'empty' and collection.state(current(w, run))['idle'] == 1
    assert counts(w, run) == ((3, 0), (2, 0)) and len(stub.received) == 3
    # The next reply repeats the refused question under a new tool call ID and reads the saved result of the
    # first one: both come from the saved outcomes without a POST, and the refusal is not a new idle extraction.
    # Two new questions then arrive at once with one extraction left: exactly one is sent, the other is limited.
    wrong_again, cached, left, right = step(boundary, stub, agent(ask('Облигации компании'), ask(QUESTIONS[0]),
                                                                  ask(QUESTIONS[1]), ask(QUESTIONS[2])), 2)
    replay = invoke(boundary, run, wrong_again)
    assert replay['ok'] and replay['result']['status'] == 'empty' and replay['result']['skipped'][0]['code'] == 'number_mismatch'
    assert collection.state(current(w, run))['idle'] == 1 and counts(w, run) == ((4, 0), (2, 0)) and len(stub.received) == 4
    again = invoke(boundary, run, cached)
    assert again['ok'] and counts(w, run) == ((4, 0), (2, 0)) and len(stub.received) == 4
    stub.queue = [extraction([qualitative_item(4, 5)])]
    results = {}
    def run_tool(name, tool):
        results[name] = invoke(boundary, run, tool)
    threads = [threading.Thread(target=run_tool, args=(name, tool)) for name, tool in (('left', left), ('right', right))]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(stub.received) == 5 and counts(w, run) == ((5, 0), (3, 0))
    refused = [name for name, reply in results.items() if not reply['ok']]
    assert len(refused) == 1 and results[refused[0]]['error']['code'] == 'budget_exceeded'
    assert collection.state(current(w, run))['reason'] == 'extractions'
    # A rephrased question after the transition is refused without a POST, under any tool call ID.
    tool, = step(boundary, stub, agent(ask('Стратегия и планы компании')), 3)
    assert 'Режим завершения' in hints(stub)[-1]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and 'Сбор оснований закрыт' in reply['error']['message'] and len(stub.received) == 6


def test_preparatory_steps_keep_the_completion_reserve_and_reload_keeps_the_transition(workspace, stub, tmp_path):
    """Check 4: with 8 calls, the agent and extraction steps stop at five so three remain; a new Boundary
    after a reload sees the same counters and the closed collection; recovery allows no new send."""
    w = workspace
    source = plain_source(w)
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    stub.queue = [extraction([qualitative_item(2, 3)])]
    facts = invoke(boundary, run, tool)['result']['facts']
    refs = [dict(source_version_id=f['source']['source_version_id'], fact_id=f['fact_id']) for f in facts]
    tool, = step(boundary, stub, agent(('aist_get_facts', dict(refs=refs))), 2)
    assert invoke(boundary, run, tool)['ok']
    tool, = step(boundary, stub, agent(ask(QUESTIONS[1])), 3)
    stub.queue = [extraction([qualitative_item(4, 5)])]
    assert invoke(boundary, run, tool)['ok']
    assert counts(w, run) == ((5, 0), (2, 0)) and not collection.state(current(w, run))['closed']
    assert step(boundary, stub, agent(content='Составляю записку.'), 4) == []
    state = collection.state(current(w, run))
    assert state['closed'] and state['reason'] == 'reserve_calls' and 'Режим завершения' in hints(stub)[-1]
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['reserve_calls']
    # Reload: another Boundary and directory, same durable counters, transition and limits.
    reloaded = Boundary(executor(w), current(w, run), tmp_path / 'reloaded', 'synthetic')
    tool, = step(reloaded, stub, agent(ask(QUESTIONS[2])), 5)
    assert 'Режим завершения' in hints(stub)[-1]
    reply = invoke(reloaded, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded' and len(stub.received) == 7
    assert counts(w, run) == ((7, 0), (2, 0))
    # Recovery: an uncertain worker outcome leaves no right to send again; the old epoch is fenced.
    w.runs.recover(owned=run['worker_id'])
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'recovery_required'
    with pytest.raises(ApiError) as fenced:
        Boundary(executor(w), run, tmp_path / 'again', 'synthetic').model(request(6))
    assert fenced.value.code == 'outcome_unknown' and len(stub.received) == 7
    assert counts(w, run) == ((7, 0), (2, 0)) and collection.state(current(w, run))['closed']


def test_nested_overload_is_retried_within_the_rules_and_otherwise_closes_the_collection(workspace, stub, monkeypatch, tmp_path):
    """Check 5: a fitting Retry-After pause is taken; a too large one is not shortened, the known refusal
    closes the collection, the Run keeps its completion steps and a draft is saved."""
    w = workspace
    source, saved = note_source(w, following=[(['Logistics', 'Warehouse operations'], None)])
    run = note_run(w, source)
    pauses = record_pauses(monkeypatch)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask('Облигации на 31.03.2023')), 1)
    stub.queue = [refusal(headers={'Retry-After': '30'}), extraction([wrong_item('Синтетика', 7, 'Облигации', 'Данные компании', AT_2023)])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] and reply['result']['status'] == 'empty'
    assert pauses == [30.0] and len(stub.received) == 3 and counts(w, run) == ((2, 1), (1, 1))
    tool, = step(boundary, stub, agent(ask('Общий размер')), 2)
    stub.queue = [refusal(headers={'Retry-After': '200'})]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'provider_error'
    assert 'HTTP 429' in reply['error']['message'] and 'Сбор оснований закрыт' in reply['error']['message']
    assert pauses == [30.0] and len(stub.received) == 5 and counts(w, run) == ((4, 1), (2, 1))
    refused = calls(w, run)[-1]['data']
    assert refused['http_status'] == 429 and 'не разрешена' in refused['error']['message']
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    assert collection.state(current(w, run))['reason'] == 'overload'
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['overload']
    tool, = step(boundary, stub, agent(('aist_save_artifact', model_save_args(small_note(source)))), 3)
    assert 'Режим завершения' in hints(stub)[-1]
    saved = invoke(boundary, run, tool)
    assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running' and versions(w) == []


def test_agent_overload_timeout_and_late_replies_keep_their_terminal_reasons(workspace, stub, monkeypatch, tmp_path):
    SHIFT[0] = 0.0
    """Check 5: an agent refusal after the exhausted backoff fails the Run as a provider error; a timed-out
    extraction stays unknown; a reply after the deadline stop is accounted late and revives nothing."""
    w = workspace
    source = plain_source(w)
    run = note_run(w, source)
    pauses = record_pauses(monkeypatch)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    stub.queue = [refusal()] * 4
    with pytest.raises(ApiError) as ended:
        boundary.model(request(1))
    assert ended.value.code == 'run_terminal' and pauses == [10, 20, 40]
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'failed' and state['error']['code'] == 'provider_error' and counts(w, run) == ((1, 3), (0, 0))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    # A nested extraction that times out: the call stays unknown, the Run requires recovery, nothing is retried.
    run = note_run(w, source)
    worker = executor(w)
    worker.runs.settings = w.runs.settings.__class__(**{**w.runs.settings.__dict__, 'transport_seconds': 1})
    boundary = Boundary(worker, run, tmp_path / 'timeout', 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    stub.queue = [refusal(behavior='delay', seconds=3)]
    with pytest.raises(ApiError) as unknown:
        invoke(boundary, run, tool)
    assert unknown.value.code in ('outcome_unknown', 'run_terminal')
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'recovery_required' and calls(w, run)[-1]['data']['transport_state'] == 'unknown'
    assert pauses == [10, 20, 40] and len(stub.received) == 6
    # A reply that arrives after the deadline stop is accounted as late and does not revive the Run.
    w.runs.settings = worker.runs.settings.__class__(**{**w.runs.settings.__dict__, 'transport_seconds': 60})
    run = note_run(w, source)
    ledger = executor(w).transport.ledger
    call = ledger.reserve(run['id'], run['epoch'], 'extraction')
    shift_clock(monkeypatch, 601)
    w.runs.stop(run['id'], run['epoch'], 'budget_exceeded', message='Истек общий срок задачи (600 с): результат не сохранен.')
    delivered = ledger.observe(run['id'], run['epoch'], call['id'], 200, generation())
    state = w.runs.get(w.actor, w.project, run['id'])
    assert delivered is False and state['state'] == 'budget_exceeded' and 'Истек общий срок' in state['error']['message']
    observed = calls(w, run)[-1]['data']
    assert observed['late_response'] is True and observed['delivery'] == 'suppressed' and observed['provider_outcome'] == 'success'


def test_time_reserve_closes_the_collection_and_bounds_every_send(workspace, stub, monkeypatch, tmp_path):
    """The shifted clock: the collection closes when the longest send and the completion reserve no longer fit,
    and one physical send never outlasts the send limit or the remaining Run time."""
    SHIFT[0] = 0.0
    w = workspace
    run = note_run(w, plain_source(w), max_duration_seconds=600)
    assert collection.send_seconds(run, 1200) == pytest.approx(180, abs=2)
    with w.engine.connect() as c:
        assert collection.shortfall(c, run) is None
    shift_clock(monkeypatch, 300)
    with w.engine.connect() as c:
        assert collection.shortfall(c, run) == 'reserve_time'
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    assert collection.state(current(w, run))['reason'] == 'reserve_time' and 'Режим завершения' in hints(stub)[-1]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded' and len(stub.received) == 1
    shift_clock(monkeypatch, 200)
    assert collection.send_seconds(run, 1200) == pytest.approx(100, abs=2)
    shift_clock(monkeypatch, 101)
    with pytest.raises(ApiError) as expired:
        boundary.model(request(2))
    assert expired.value.code == 'budget_exceeded' and len(stub.received) == 1


def test_exhausted_steps_repeated_save_refusal_cancel_and_revocation_give_exact_outcomes(workspace, stub, tmp_path):
    """Check 6: a user limit below the reserve is not raised; the last step without a result names the quota;
    a save refused twice names its refusal; cancellation and a revoked session stop every send."""
    w = workspace
    source = plain_source(w)
    run = note_run(w, source, max_model_calls=4)
    assert run['configuration']['collection']['completion_calls'] == 3 and run['input']['budget']['max_model_calls'] == 4
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'budget_exceeded' and len(stub.received) == 1
    assert collection.state(current(w, run))['reason'] == 'reserve_calls'
    for turn in (2, 3, 4):
        assert step(boundary, stub, agent(content=f'Шаг {turn} без сохранения.'), turn) == []
    with pytest.raises(ApiError) as exhausted:
        boundary.model(request(5))
    state = w.runs.get(w.actor, w.project, run['id'])
    assert exhausted.value.code == 'budget_exceeded' and state['state'] == 'budget_exceeded'
    assert state['error']['message'] == 'Обращения к модели исчерпаны (4 из 4) без сохранения результата.' and len(stub.received) == 4
    # A save refused twice with the same content ends the Run with that refusal, never with a document.
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path / 'save', 'synthetic')
    broken = dict(small_note(source, broken=True), sections=[dict(title='Выручка', blocks=[dict(kind='paragraph', claim=dict(
        text='Выручка за 2022 год составила 3157 млн руб.', origin='source_fact',
        fact_refs=[dict(source_version_id=source['id'], fact_id='fact_absent')], calculation_ids=[]))])])
    tool, = step(boundary, stub, agent(('aist_save_artifact', dict(content=broken))), 1)
    reply = invoke(boundary, run, tool)
    assert reply['ok'] is False and reply['error']['code'] == 'validation_failed' and 'fact_absent' in reply['error']['message']
    tool2, = step(boundary, stub, agent(('aist_save_artifact', dict(content=broken))), 2, tool_message=(tool['id'], reply))
    assert invoke(boundary, run, tool2)['ok'] is False
    with pytest.raises(ApiError) as twice:
        boundary.model(request(3, tool_message=(tool2['id'], reply)))
    state = w.runs.get(w.actor, w.project, run['id'])
    assert twice.value.code == 'validation_failed' and state['state'] == 'failed' and state['error']['code'] == 'validation_failed'
    assert state['error']['message'].startswith('Сохранение отклонено повторно с тем же содержанием: ') and 'fact_absent' in state['error']['message']
    assert versions(w) == []
    # Cancellation and a revoked session are checked before every send and every nested extraction.
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path / 'cancel', 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', ident('key'), dict(expected_revision=run['revision']))
    with pytest.raises(ApiError) as cancelled:
        invoke(boundary, run, tool)
    assert cancelled.value.code == 'run_terminal'
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path / 'revoked', 'synthetic')
    tool, = step(boundary, stub, agent(ask(QUESTIONS[0])), 1)
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:s', s=w.session)
    with pytest.raises(ApiError) as revoked:
        invoke(boundary, run, tool)
    assert revoked.value.code == 'unauthenticated'
    received = len(stub.received)
    with pytest.raises(ApiError):
        boundary.model(request(2))
    assert len(stub.received) == received
