"""Границы r4: один ресурс, честный статус новостей и чтение сохраненных частей."""
import os

import pytest

from app import note_external
from app.contracts import validate_definition
from app.errors import ApiError
from app.runs import execute, json_value
from app.service import ident
from test_d15_postgres import workspace
from test_web_research_postgres import initialized, reader, QUERY, URL, TEXT
from test_aso_quality02_phase3_postgres import deal_note
from test_draft01_r1_postgres import save

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL')


@pytest.mark.parametrize('url', [URL+'#financials', URL+'#', 'https://COMPANY.EXAMPLE/business',
                               'https://company.example:443/business'])
def test_equivalent_url_keeps_first_attribution_and_arguments(workspace, monkeypatch, url):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    first = w.runs.tool(run['id'], run['epoch'], 'search-first', 'aist_web_search', QUERY)['result']
    original = w.runs.tool(run['id'], run['epoch'], 'fetch-first', 'aist_web_fetch', dict(search_id=first['search_id'], url=URL))
    second = w.runs.tool(run['id'], run['epoch'], 'search-other', 'aist_web_search', dict(QUERY, search_target='Другая компания'))['result']
    args = dict(search_id=second['search_id'], url=url)
    reused = w.runs.tool(run['id'], run['epoch'], 'fetch-other', 'aist_web_fetch', args)
    assert reused['result'] == original['result'] and len(calls) == 3
    assert w.runs.tool(run['id'], run['epoch'], 'fetch-other', 'aist_web_fetch', args) == reused
    with pytest.raises(ApiError) as failure:
        w.runs.tool(run['id'], run['epoch'], 'fetch-other', 'aist_web_fetch', dict(args, url=URL+'/changed'))
    assert failure.value.code == 'idempotency_conflict'
    with w.engine.connect() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert len(context['items']) == 1 and context['requests'] == 3
    assert context['items'][0]['search_target'] == QUERY['search_target']


@pytest.mark.parametrize('steps,expected', [
    (['unavailable'], 'unavailable'),
    (['empty'], 'empty'),
    (['other'], 'not_searched'),
    (['unavailable', 'other'], 'unavailable'),
    (['unavailable', 'found'], 'partial'),
    (['found', 'unavailable'], 'partial'),
    (['found'], 'found'),
    (['empty', 'unavailable'], 'unavailable'),
])
def test_news_status_reaches_saved_version_without_other_topic_overwrite(workspace, monkeypatch, steps, expected):
    w = workspace
    source, run = initialized(w)
    success = reader([])
    events = []
    for index, step in enumerate(steps):
        def response(*args):
            if step == 'unavailable':
                raise OSError('Поставщик недоступен')
            if step == 'empty':
                return dict(results=[], bytes_read=100)
            result = success(*args)
            if args[0] == 'aist_web_fetch':
                result.update(page_sha256=None, response_sha256='a'*64,
                              retrieval_kind='provider_extract', provider='parallel', provider_acquired_at=None)
            return result
        monkeypatch.setattr(note_external, 'web_request', response)
        query = dict(QUERY, objective=f'Проверка {index}', topics=['market'] if step == 'other' else ['news'])
        found = w.runs.tool(run['id'], run['epoch'], f'search-{index}', 'aist_web_search', query)['result']
        if step == 'found':
            fetched = w.runs.tool(run['id'], run['epoch'], f'fetch-{index}', 'aist_web_fetch',
                                  dict(search_id=found['search_id'], url=URL))
            assert fetched['ok'], fetched
            evidence = fetched['result']['evidence']
            events.append(dict(external_evidence_id=evidence['evidence_id'], description=TEXT,
                               published=None, event_date=None, date_quote=''))
    with w.engine.connect() as c:
        before = execute(c, 'SELECT news_run FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert before['items'] == []  # Reading a document alone does not select a news event.
    saved = save(w, run, deal_note(source), news_events=events or None)
    news = saved['version']['basis']['news_run']
    validate_definition('NewsRun', news)
    assert news['status'] == expected and len(news['items']) == len(events)


def test_retained_text_can_be_read_after_private_transition_without_network(workspace, monkeypatch):
    w = workspace
    source, run = initialized(w)
    text = TEXT * 250 + '\nЗавершающее примечание: структура выручки.'
    calls = []
    base = reader(calls)
    def response(name, *args):
        result = base(name, *args)
        if name == 'aist_web_fetch':
            result.update(quote=text, truncated=True)
        return result
    monkeypatch.setattr(note_external, 'web_request', response)
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    fetched = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL))['result']
    assert fetched['evidence']['quote'] == text[:12000] and fetched['text_length'] == len(text)
    w.runs.tool(run['id'], run['epoch'], 'private', 'aist_read_task', {}, read_task=lambda r: 'Закрытая задача')
    monkeypatch.setattr(note_external, 'web_request', lambda *a: pytest.fail('Не нужна новая сеть'))
    received = fetched['evidence']['quote']
    offset = fetched['next_offset']
    while offset is not None:
        part = w.runs.tool(run['id'], run['epoch'], f'part-{offset}', 'aist_read_web',
                           dict(evidence_id=fetched['evidence']['evidence_id'], offset=offset))['result']
        validate_definition('ReadWebResult', part)
        received += part['text']
        offset = part['next_offset']
    assert received == text and len(calls) == 2
    saved = save(w, run, deal_note(source))
    assert saved['version']['basis']['external_context']['items'][0]['quote'] == text


def test_retained_text_is_scoped_and_offset_is_bounded(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    monkeypatch.setattr(note_external, 'web_request', reader([]))
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    item = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL))['result']['evidence']
    _, other = initialized(w)
    with pytest.raises(ApiError) as missing:
        w.runs.tool(other['id'], other['epoch'], 'read', 'aist_read_web', dict(evidence_id=item['evidence_id'], offset=0))
    assert missing.value.code == 'resource_not_found'
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'outside', 'aist_read_web', dict(evidence_id=item['evidence_id'], offset=len(TEXT)+1))


def test_analyst_reads_private_task_then_delegates_and_refines(workspace, monkeypatch):
    w = workspace
    source, run = initialized(w, research_version='agent.v2')
    seen = []
    monkeypatch.setattr(note_external, 'web_request', reader(seen))
    private = w.runs.tool(run['id'], run['epoch'], 'private', 'aist_read_source',
                          dict(source_version_id=source['id'], page=1))
    assert private['ok']
    tasks = []
    def research(current, task, task_id):
        tasks.append(task)
        query = dict(QUERY, objective=task['objective'])
        result = w.runs.tool(run['id'], run['epoch'], task_id+'-search', 'aist_web_search', query, research_id=task_id)['result']
        w.runs.tool(run['id'], run['epoch'], task_id+'-fetch', 'aist_web_fetch',
                    dict(search_id=result['search_id'], url=URL+'#'+str(len(tasks))), research_id=task_id)
        return dict(status='collected', summary='Сохранен материал по вопросу.')
    first_task = dict(company='Орбита', objective='Рынок оборудования в России за 2025 год')
    first = w.runs.tool(run['id'], run['epoch'], 'research-first', 'aist_research', first_task, research=research)
    validate_definition('ResearchReply', first)
    assert first['result']['sources'][0]['url'] == URL
    reading, = first['result']['readings']
    assert reading == w.runs.tool(run['id'], run['epoch'], 'compare-reading', 'aist_read_web',
                                dict(evidence_id=reading['evidence_id'], offset=0))['result']
    assert reading['text'] == TEXT
    reused = w.runs.tool(run['id'], run['epoch'], 'research-repeat', 'aist_research', first_task, research=research)
    assert reused['result'] == first['result'] and len(tasks) == 1
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'direct-web', 'aist_web_search', QUERY)
    second_task = dict(company='Орбита', objective='Уточни сегмент промышленного оборудования и выручку конкурентов')
    refined = w.runs.tool(run['id'], run['epoch'], 'research-refine', 'aist_research', second_task, research=research)
    assert len(refined['result']['readings']) == 1
    assert refined['result']['readings'][0]['evidence_id'] != reading['evidence_id']
    assert tasks == [first_task, second_task]
    assert len(seen) == 4  # Два вопроса к одному URL создают два независимых извлечения.
    saved = save(w, run, deal_note(source))
    context = saved['version']['basis']['external_context']
    assert context['research_closed'] and 'research_pending' not in context


def test_uncertain_research_blocks_restart_and_save(workspace):
    w = workspace
    source, run = initialized(w, research_version='agent.v2')
    def lost(*args):
        raise RuntimeError('Потерян worker после резервирования')
    task = dict(company='Орбита', objective='Изучить рынок')
    with pytest.raises(RuntimeError):
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research', task, research=lost)
    with pytest.raises(ApiError) as failure:
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research', task,
                    research=lambda *a: pytest.fail('Повтор исследования запрещен'))
    assert failure.value.code == 'outcome_unknown'
    with pytest.raises(ApiError):
        save(w, run, deal_note(source))


def test_cancelled_research_cannot_publish_a_late_summary(workspace):
    w = workspace
    _, run = initialized(w, research_version='agent.v2')
    def cancelled(*args):
        current = w.runs.get(w.actor, w.project, run['id'])
        w.runs.write(w.actor, w.session, w.project, run['id'], 'cancelRun', ident('key'),
                     dict(expected_revision=current['revision']))
        return dict(status='empty', summary='Поздний ответ')
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                     dict(company='Орбита', objective='Рынок'), research=cancelled)
    with w.engine.connect() as c:
        assert execute(c, "SELECT count(*) FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_research'", r=run['id']).scalar_one() == 0


def test_legacy_raw_resource_key_reuses_the_same_page(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    original = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL))
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.web_requests SET url=:url WHERE run_id=:r AND url IS NOT NULL',
                url=URL+'#old-fragment', r=run['id'])
    reused = w.runs.tool(run['id'], run['epoch'], 'fetch-new', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL+'#new'))
    assert reused['result'] == original['result'] and len(calls) == 2


def test_research_reserves_its_own_tool_slot(workspace, monkeypatch):
    w = workspace
    w.runs.policy['run']['max_tool_calls'] = 2
    _, run = initialized(w, research_version='agent.v2')
    monkeypatch.setattr(note_external, 'web_request', reader([]))
    def research(current, task, task_id):
        w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY, research_id=task_id)
        with pytest.raises(ApiError) as failure:
            w.runs.tool(run['id'], run['epoch'], 'too-many', 'aist_web_search',
                         dict(QUERY, objective='Еще вопрос'), research_id=task_id)
        assert failure.value.code == 'budget_exceeded'
        return dict(status='budget', summary='Достигнут лимит инструментов.')
    result = w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                         dict(company='Орбита', objective='Рынок'), research=research)
    assert result['result']['status'] == 'budget'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.tool_calls WHERE run_id=:r', r=run['id']).scalar_one() == 2


def test_research_stops_at_composition_reserve_without_stopping_run(workspace, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import openclaw_runtime as runtime
    from test_fable_postgres import start_fable
    w = workspace
    _, _, run = start_fable(w, full=True, research=True)
    note_external.prepare_all(w.runs, run)
    boundary = runtime.Boundary(SimpleNamespace(runs=w.runs), run, tmp_path, 'local-test')
    boundary.research_id = 'research_test'
    boundary.public_research = True
    with w.engine.begin() as c:
        execute(c, "UPDATE aist.runs SET external_context=jsonb_set(external_context, '{research_pending}', CAST(:pending AS jsonb)) WHERE id=:r",
                pending=json_value(dict(id=boundary.research_id, arguments_sha256='a'*64)), r=run['id'])
    used = run['input']['budget']['max_model_calls'] - note_external.research_reserve(run)
    monkeypatch.setattr(runtime, 'attempt_counts', lambda *args: (used, 0))
    with pytest.raises(ApiError) as failure:
        boundary.model(dict(model=boundary.profile['model'], messages=[]))
    assert failure.value.code == 'budget_exceeded' and boundary.research_limit
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE run_id=:r', r=run['id']).scalar_one() == 0


def test_research_admission_preserves_reserve_and_does_not_invoke_callback(workspace, monkeypatch):
    from app import accounting
    w = workspace
    _, run = initialized(w, research_version='agent.v2')
    used = run['input']['budget']['max_model_calls'] - note_external.research_reserve(run) - 1
    monkeypatch.setattr(accounting, 'attempt_counts', lambda *args: (used, 0))
    with pytest.raises(ApiError) as failure:
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                    dict(company='Орбита', objective='Рынок'),
                    research=lambda *args: pytest.fail('Исследование не помещается в остаток бюджета'))
    assert failure.value.code == 'budget_exceeded'
    with w.engine.connect() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert 'research_pending' not in context


def test_unknown_company_can_be_clarified_without_disabling_research(workspace, monkeypatch):
    w = workspace
    monkeypatch.setattr(note_external, 'identity', lambda materials: None)
    _, run = initialized(w, research_version='agent.v2')
    question = dict(idempotency_key=ident('key'), expected_revision=run['revision'], prompt='Как называется компания?')
    assert w.runs.tool(run['id'], run['epoch'], 'question', 'aist_request_input', question)['ok']
    waiting = w.runs.get(w.actor, w.project, run['id'])
    w.runs.write(w.actor, w.session, w.project, run['id'], 'resumeRun', ident('key'),
                 dict(expected_revision=waiting['revision'], waiting_input_id=waiting['waiting_input']['id'], text='Орбита'))
    resumed = w.runs.claim('research-r4')
    note_external.prepare_all(w.runs, resumed)
    assert not resumed['external_context'].get('research_closed')
    tasks = []
    def research(current, task, task_id):
        tasks.append(task)
        return dict(status='empty', summary='Публикации не найдены.')
    w.runs.tool(resumed['id'], resumed['epoch'], 'research', 'aist_research',
                 dict(company='Орбита', objective='Публичные сведения о компании'), research=research)
    assert tasks[0]['company'] == 'Орбита'
