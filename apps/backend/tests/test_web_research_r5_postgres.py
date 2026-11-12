"""Повторы операций, разные вопросы к одному источнику и новости из кеша."""
from copy import deepcopy
import os

import pytest

from app import note_external
from app.errors import ApiError
from app.runs import execute, json_value
from test_d15_postgres import workspace
from test_web_research_postgres import initialized, local_parallel, network_state, reader, QUERY, URL
from test_aso_quality02_phase3_postgres import deal_note
from test_draft01_r1_postgres import save

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL')


def fetch(w, run, key, query, url=URL):
    found = w.runs.tool(run['id'], run['epoch'], key+'-search', 'aist_web_search', query)['result']
    args = dict(search_id=found['search_id'], url=url)
    return w.runs.tool(run['id'], run['epoch'], key, 'aist_web_fetch', args), args


@pytest.mark.parametrize('alias', ['same-args', 'same-resource', 'new-objective'])
def test_cached_executor_identity_cannot_change_arguments(workspace, monkeypatch, alias):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    first, first_args = fetch(w, run, 'first', QUERY)
    if alias == 'same-args':
        second_args = first_args
        second = w.runs.tool(run['id'], run['epoch'], 'second', 'aist_web_fetch', second_args)
    else:
        query = dict(QUERY, search_target='Другой объект',
                     objective='Денежные потоки' if alias == 'new-objective' else QUERY['objective'])
        second, second_args = fetch(w, run, 'second', query, URL+'#next')
    assert w.runs.tool(run['id'], run['epoch'], 'second', 'aist_web_fetch', second_args) == second
    changed = dict(first_args, url=URL+'#changed') if alias == 'same-args' else first_args
    with pytest.raises(ApiError, match='idempotency_conflict'):
        w.runs.tool(run['id'], run['epoch'], 'second', 'aist_web_fetch', changed)
    if alias != 'new-objective':
        assert first['result'] == second['result']
    assert len(calls) == {'same-args': 2, 'same-resource': 3, 'new-objective': 4}[alias]


@pytest.mark.parametrize('unavailable', [False, True])
def test_news_reuse_retains_outcome_without_reattributing_source(workspace, monkeypatch, unavailable):
    w = workspace
    source, run = initialized(w)
    calls = []
    base = reader(calls)
    def response(name, *args):
        value = base(name, *args)
        if name == 'aist_web_fetch':
            value.update(page_sha256=None, response_sha256='a'*64, retrieval_kind='provider_extract',
                         provider='parallel', provider_acquired_at=None)
        return dict(unavailable=True, bytes_read=20) if unavailable and name == 'aist_web_fetch' else value
    monkeypatch.setattr(note_external, 'web_request', response)
    first, _ = fetch(w, run, 'business', dict(QUERY, topics=['business']))
    original = deepcopy(first['result'])
    second, args = fetch(w, run, 'news', dict(QUERY, topics=['news']), URL+'#news')
    assert second['result'] == original
    for key in ['news', 'news-alias', 'news-alias']:
        assert w.runs.tool(run['id'], run['epoch'], key, 'aist_web_fetch', args)['result'] == original
    saved = save(w, run, deal_note(source))
    context = saved['version']['basis']['external_context']
    news = saved['version']['basis']['news_run']
    assert len(calls) == 3
    # Контракт фазы 6 r2: повтор чтения документа не заменяет явный отбор news_events.
    assert news['status'] == ('unavailable' if unavailable else 'empty')
    assert context['news_search']['failed'] == int(unavailable)
    assert context['news_search']['succeeded'] == (1 if unavailable else 2)
    if not unavailable:
        assert context['items'][0] == original['evidence']
        assert context['items'][0]['topics'] == ['business']
        assert context['topics']['news'] == 'collected'
        assert news['items'] == []


def test_new_question_keeps_both_extractions_and_exact_question_reuses(workspace, monkeypatch):
    w = workspace
    source, run = initialized(w)
    calls = []
    base = reader(calls)
    objectives = []
    def response(name, args, *rest):
        value = base(name, args, *rest)
        if name == 'aist_web_fetch':
            objectives.append(rest[-1])
            value['quote'] += ' Ответ на вопрос: ' + rest[-1]
        return value
    monkeypatch.setattr(note_external, 'web_request', response)
    first_question = 'Контекст исследования. ' * 10 + 'Бизнес'
    second_question = 'Контекст исследования. ' * 10 + 'Денежные потоки'
    first, _ = fetch(w, run, 'first', dict(QUERY, objective=first_question))
    second, _ = fetch(w, run, 'second', dict(QUERY, objective=second_question), URL+'#cash')
    repeated, _ = fetch(w, run, 'third', dict(QUERY, objective=second_question, queries=['Дополнительная формулировка']))
    assert objectives == [first_question, second_question]
    assert repeated['result'] == second['result']
    assert first['result']['evidence']['evidence_id'] != second['result']['evidence']['evidence_id']
    saved = save(w, run, deal_note(source))
    assert saved['version']['basis']['external_context']['items'] == [first['result']['evidence'], second['result']['evidence']]
    assert len(calls) == 5


def test_old_unlabelled_read_is_not_mistaken_for_new_question(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    first, args = fetch(w, run, 'old', QUERY)
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.web_requests SET objective=NULL WHERE run_id=:r', r=run['id'])
    assert w.runs.tool(run['id'], run['epoch'], 'alias', 'aist_web_fetch', args)['result'] == first['result']
    fetch(w, run, 'new', dict(QUERY, objective='Новый вопрос'))
    assert len(calls) == 4


def test_known_budget_refusal_can_finish_research_but_unknown_still_blocks(workspace):
    w = workspace
    _, run = initialized(w, research_version='agent.v2')
    def refused(*args):
        raise ApiError('budget_exceeded')
    result = w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                         dict(company='Орбита', objective='Публичный контекст'), research=refused)
    assert result['result']['status'] == 'budget'
    with w.engine.connect() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert 'research_pending' not in context


def test_later_research_can_use_earlier_search_of_same_run(workspace, local_parallel):
    w = workspace
    _, run = initialized(w, research_version='agent.v2')
    found = None

    def discover(current, task, task_id):
        nonlocal found
        found = w.runs.tool(run['id'], run['epoch'], 'discover', 'aist_web_search', QUERY, research_id=task_id)['result']
        return dict(status='empty', summary='Первый проход сохранил выдачу.')

    def refine(current, task, task_id):
        before = network_state(w, run)
        with pytest.raises(ApiError, match='resource_not_found'):
            w.runs.tool(run['id'], run['epoch'], 'rejected', 'aist_web_fetch',
                        dict(search_id=found['search_id'], url=URL+'?changed=1'), research_id=task_id)
        assert network_state(w, run) == before and len(local_parallel.rpc) == 1
        reply = w.runs.tool(run['id'], run['epoch'], 'accepted', 'aist_web_fetch',
                            dict(search_id=found['search_id'], url=URL), research_id=task_id)
        assert reply['result']['status'] == 'collected'
        return dict(status='collected', summary='Прочитан источник из первого поиска.')

    for index, callback in enumerate([discover, refine]):
        result = w.runs.tool(run['id'], run['epoch'], f'research-{index}', 'aist_research',
                             dict(company='Орбита', objective=f'Исследование {index}'), research=callback)
        context, requests = network_state(w, run)
        assert result['ok'] and 'research_pending' not in context
        assert len(requests) == index + 1 and all(item['state'] == 'completed' for item in requests)
    assert len(local_parallel.rpc) == 2
    assert local_parallel.rpc[0]['params']['arguments']['session_id'] == local_parallel.rpc[1]['params']['arguments']['session_id']
    assert local_parallel.rpc[1]['params']['arguments']['objective'] == QUERY['objective']


@pytest.mark.parametrize('pending', ['model', 'web'])
def test_budget_signal_never_clears_an_unresolved_send(workspace, pending):
    from app.accounting import Accounting
    w = workspace
    _, run = initialized(w, research_version='agent.v2')
    def refused(*args):
        if pending == 'model':
            Accounting(w.runs).reserve(run['id'], run['epoch'])
        else:
            with w.engine.begin() as c:
                execute(c, """INSERT INTO aist.web_requests
                    (id,run_id,epoch,executor_tool_call_id,arguments_sha256,state)
                    VALUES ('tool_pending',:r,:e,'pending',:sha,'reserved')""",
                    r=run['id'], e=run['epoch'], sha='a'*64)
        raise ApiError('budget_exceeded')
    with pytest.raises(ApiError) as failure:
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                    dict(company='Орбита', objective='Рынок'), research=refused)
    assert failure.value.code == 'outcome_unknown'
    with w.engine.connect() as c:
        original = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()['research_pending']
    with pytest.raises(ApiError) as next_failure:
        w.runs.tool(run['id'], run['epoch'], 'another-research', 'aist_research',
                    dict(company='Орбита', objective='Другое исследование'),
                    research=lambda *args: pytest.fail('Неопределенное исследование нельзя заменить следующим'))
    assert next_failure.value.code == 'outcome_unknown'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()['research_pending'] == original


def test_new_extraction_respects_network_budget_and_keeps_old_text(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    first, args = fetch(w, run, 'first', QUERY)
    next_search = w.runs.tool(run['id'], run['epoch'], 'new-search', 'aist_web_search', dict(QUERY, objective='Новая тема'))['result']
    with w.engine.begin() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        context['requests'] = 18
        execute(c, 'UPDATE aist.runs SET external_context=CAST(:value AS jsonb) WHERE id=:r',
                value=json_value(context), r=run['id'])
    with pytest.raises(ApiError) as failure:
        w.runs.tool(run['id'], run['epoch'], 'new-fetch', 'aist_web_fetch', dict(search_id=next_search['search_id'], url=URL))
    assert failure.value.code == 'budget_exceeded'
    assert w.runs.tool(run['id'], run['epoch'], 'old-alias', 'aist_web_fetch', args)['result'] == first['result']
    assert len(calls) == 3


@pytest.mark.parametrize('known_company', [False, True])
def test_v2_without_research_saves_not_searched_and_edit_preserves_it(workspace, monkeypatch, known_company):
    from test_draft01_postgres import http
    from test_draft01_r1_postgres import edit
    w = workspace
    subject = dict(company='Орбита', inn='', sector='', domain='') if known_company else None
    monkeypatch.setattr(note_external, 'identity', lambda materials: subject)
    monkeypatch.setattr(note_external, 'web_request', lambda *args: pytest.fail('Чтение и правка не запускают поиск'))
    source, run = initialized(w, research_version='agent.v2')
    initial = deepcopy(run['external_context'])
    # Повтор подготовки при продолжении Run не создает другое основание.
    note_external.prepare_all(w.runs, run)
    assert run['external_context'] == initial
    w.runs.tool(run['id'], run['epoch'], 'read-source', 'aist_read_source',
                dict(source_version_id=source['id'], page=1))
    saved = save(w, run, deal_note(source))
    basis = saved['version']['basis']
    assert basis['news_run'] == note_external.news_from_context(basis['external_context'])
    assert basis['news_run']['status'] == 'not_searched'
    assert basis['news_run']['company'] == ('Орбита' if known_company else '')
    assert basis['external_context']['news_search'] == dict(succeeded=0, failed=0)
    content = deepcopy(saved['version']['payload']['content'])
    content['team'] = 'Тестовый аналитик'
    with http(w) as client:
        child = edit(w, client, saved, content, 'human', [source])
    assert child['version']['basis']['news_run'] == basis['news_run']
    assert child['version']['basis']['external_context'] == basis['external_context']
    assert w.runs.version(w.actor, w.project, saved['version']['id']) == saved
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.web_requests WHERE run_id=:r', r=run['id']).scalar_one() == 0
