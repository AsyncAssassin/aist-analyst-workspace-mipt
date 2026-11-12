"""Отклоненный числовой ввод: сохранность, атомарность и доставка без нового POST."""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from app import collection, extraction, extraction_requests, pdf_images, openclaw_runtime as runtime
from app.accounting import Accounting
from app.errors import ApiError
from app.facts import pin_of
from app.live_profile import FABLE_VISUAL_ID, profile_for
from app.model_context import model_view
from app.normalization import checked
from app.runs import execute, json_value
from app.search import register
from app.service import ident
from test_d15_postgres import workspace
from test_fable_postgres import start_fable
from test_flow01_02_postgres import GOOD, QUAL, TRLN, checkpoint, ingest, note
from test_flow01_02_r1_postgres import follow_up, startup
from test_flow01_postgres import current, counts
from test_note_path06_r2_postgres import response
from test_perf02_r1_postgres import upload, accounted_response, fact_rows
from test_perf02_r4_postgres import start_note_run, save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def mixed(w, *, visual=False):
    source = upload(w, 'Числовой ввод Meridian', [
        'Meridian', 'Консолидированная отчетность по МСФО', 'Бизнес компании',
        'Компания поставляет оборудование клиентам.', 'Выручка, млн руб.', '137', '3M28',
        'Объем рынка', '3,7 трлн руб.'], following=[(['Meridian', 'Сведения об эмитенте'], None)])
    if visual:
        _, _, run = start_fable(w, full=True, source=source, profile_id=FABLE_VISUAL_ID)
    else:
        run = start_note_run(w, source)
    good, qual, rejected = deepcopy((GOOD, QUAL, TRLN))
    for item in (good, qual, rejected):
        item['subject'] = 'Meridian'
    good.update(period=dict(kind='interval', label='3M28', start='2028-01-01', end='2028-03-31'),
                amount=dict(value='137', unit='money', currency='RUB'), value_text='137')
    rejected.update(period=dict(kind='unknown', label='Не указано', start=None, end=None),
                    amount=dict(value='3.7', unit='money', currency='RUB'), value_text='3,7')
    rejected['evidence'].append(dict(page=2, line_start=1, line_end=1, area='Эмитент'))
    with w.engine.connect() as c:
        pages = extraction.source_pages(w.runs, c, run)
        layouts = extraction.source_pages(w.runs, c, run, layout=True)
    return source, run, pages, layouts, good, qual, rejected


def receipt(w, run, source, payload, question='Все данные'):
    """Полный профиль Fable и квитанция изображений, учет имитатора без транспорта."""
    from app.extraction_context import select_pages
    with w.engine.connect() as c:
        pages = extraction.source_pages(w.runs, c, run)
        layouts = extraction.source_pages(w.runs, c, run, layout=True)
        diagrams = extraction.source_pages(w.runs, c, run, diagrams=True)
        task = execute(c, 'SELECT text FROM aist.messages WHERE id=:m', m=run['message_id']).scalar_one()
        stored_source = w.service.scoped(c, w.actor, w.project, 'source_versions', source['id'])
    selected, coverage = select_pages(pages, question, layouts)
    coverage = dict(coverage, qualification=coverage['qualification'] + ' ' + pdf_images.QUALIFICATION)
    pin = pin_of(source)
    parameters = profile_for(run['input'])['page_images']
    request = extraction.extraction_request(selected, task, layouts, question=question, diagrams=diagrams,
        aso_note=run['input']['skill_version_id'] == 'skill_analytical-note-v3',
        conversation_context=run['configuration'].get('conversation_context', {}),
        clarification_dialogue=(run['checkpoint'] or {}).get('dialogue', []))
    data, problem = w.service.storage.read(stored_source)
    assert problem is None
    images = pdf_images.render(data, list(selected), parameters, 900)
    request = pdf_images.attach(request, pin, images, parameters)
    meta = extraction_requests.metadata(run, pin, question, coverage, request, source['processor_version'],
        visual_input=dict(source=pin, pages=list(selected), render=parameters), images=images)
    ledger = Accounting(w.runs)
    call = ledger.reserve(run['id'], run['epoch'], 'extraction', extraction=meta)
    assert ledger.observe(run['id'], run['epoch'], call['id'], 200, response(payload), native=True)
    return call['id'], meta, pages, layouts, diagrams


def no_transport(w):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        pytest.fail('Повтор или чтение кэша попытались отправить запрос')
    return SimpleNamespace(runs=w.runs, transport=SimpleNamespace(perform=forbidden)), attempts


def read_tool(w, worker, run, source, directory, question='Все данные'):
    args = dict(refs=[], extract=dict(source_version_id=source['id'], question=question))
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', args,
        obtain_facts=lambda current: runtime.obtain_facts(worker, current, args['extract'], directory, None))
    assert reply['ok'], reply
    return reply['result']


def test_mixed_snapshot_preserves_input_repeat_reload_and_identity(workspace):
    w = workspace
    source, run, pages, layouts, good, qual, rejected = mixed(w)
    invalid_qual = dict(qual, period=dict(kind='instant', label='2028 год', start='2028-01-01', end='2028-12-31'))
    payload = dict(facts=[good, rejected, invalid_qual, qual])
    original = deepcopy(payload)
    call = accounted_response(w, run, payload)
    result = extraction.ingest(w.runs, run, pages, payload, call, layouts=layouts, question='Числа и бизнес')
    assert checked('GetFactsResult', result) == result and payload == original
    assert result['status'] == 'partial' and len(result['facts']) == 2
    numeric, qualitative = result['skipped']
    candidate = numeric['review_candidate']
    assert candidate['input'] == rejected and 'table' not in candidate['input']
    assert candidate['input']['period']['kind'] == 'unknown'
    assert candidate['status'] == 'not_admitted' and candidate['source'] == pin_of(source)
    assert candidate['call_id'] == call and candidate['operation_key'] is None
    assert candidate['candidate_index'] == 2 and candidate['extraction_version'] == extraction.VERSION
    assert 'scale' not in candidate['input']['amount'] and 'fact_id' not in candidate['input']
    assert 'review_candidate' not in qualitative
    assert model_view(result)['skipped'] == result['skipped']
    before = checkpoint(w, run)
    w.engine.dispose()
    assert extraction.ingest(w.runs, run, pages, payload, call, layouts=layouts, question='Числа и бизнес') == result
    assert checkpoint(w, run) == before and payload == original
    # Индекс, ответ, исходные поля и порядок допустимых ссылок входят в идентичность.
    variants = [[rejected, good], [good, dict(rejected, table=None)],
                [good, dict(rejected, evidence=list(reversed(rejected['evidence'])))]]
    ids = {candidate['candidate_id']}
    for items in variants:
        outcome, _ = ingest(w, run, pages, layouts, items, 'Вариант')
        saved = outcome['skipped'][0]['review_candidate']
        assert saved['input'] == next(i for i in items if i['metric'] == rejected['metric'])
        assert saved['candidate_id'] not in ids
        ids.add(saved['candidate_id'])
    assert len(fact_rows(w)) == 2
    ref = dict(source_version_id=source['id'], fact_id=candidate['candidate_id'])
    for tool, args in [
        ('aist_get_facts', dict(refs=[ref])),
        ('aist_calculate', dict(formula_id='growth_percent.v1', rounding=dict(mode='HALF_UP', places=2),
                               inputs=[dict(name=name, fact=ref) for name in ('previous', 'current')]))]:
        assert not w.runs.tool(run['id'], run['epoch'], ident('tool'), tool, args)['ok']
    content = note(next(f for f in result['facts'] if f['amount'] is None))
    content['sections'][0]['blocks'][0]['claim']['review_candidate_ids'] = [candidate['candidate_id']]
    assert not w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))['ok']
    claim = content['sections'][0]['blocks'][0]['claim']
    claim.pop('review_candidate_ids')
    claim['fact_refs'] = [ref]
    assert not w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))['ok']


@pytest.mark.parametrize('legacy', [False, True], ids=['with_snapshot', 'old_result'])
def test_full_fable_same_operation_and_other_run_cache_without_post(workspace, monkeypatch, tmp_path, legacy):
    w = workspace
    source, run, _, _, good, qual, rejected = mixed(w, visual=True)
    payload = dict(facts=[good, qual, rejected])
    call, meta, pages, layouts, diagrams = receipt(w, run, source, payload)
    result = extraction.ingest(w.runs, run, pages, payload, call, pin_of(source), layouts,
                              diagrams=diagrams, operation=meta, question='Все данные')
    if legacy:
        # Готовый прежний результат допустим без нового необязательного поля.
        with w.engine.begin() as c:
            saved = current(w, run)['checkpoint']
            for entry in [*saved['extractions'], saved['extraction_operations'][meta['key']]['result']]:
                for skip in entry['skipped']:
                    skip.pop('review_candidate', None)
            execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r', r=run['id'], v=json_value(saved))
        for skip in result['skipped']:
            skip.pop('review_candidate', None)
    worker, attempts = no_transport(w)
    monkeypatch.setattr(pdf_images, 'render', lambda *a, **kw: pytest.fail('Кэш не требует рендера'))
    monkeypatch.setattr(runtime, 'ingest', lambda *a, **kw: pytest.fail('Готовый результат не проходит ingest повторно'))
    before = current(w, run)
    calls_before = counts(w, run)
    w.engine.dispose()
    assert read_tool(w, worker, run, source, tmp_path) == result
    assert read_tool(w, worker, run, source, tmp_path/'again') == result
    assert collection.state(current(w, run)) == collection.state(before)
    assert counts(w, run) == calls_before
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    _, _, second = start_fable(w, full=True, source=source, profile_id=FABLE_VISUAL_ID)
    assert extraction.recorded_extractions(second) == []
    assert read_tool(w, worker, second, source, tmp_path/'cache') == result
    assert read_tool(w, worker, second, source, tmp_path/'cache-again') == result
    latest = current(w, second)
    assert counts(w, second) == ((0, 0), (0, 0))
    assert collection.state(latest) == dict(closed=False, idle=0)
    assert not extraction_requests.operations(latest)
    assert len(extraction.recorded_extractions(latest)) == 1
    assert startup(w, second)[0]['skipped'] == result['skipped']
    assert model_view(result)['skipped'] == result['skipped'] and attempts == []
    if not legacy:
        snapshot = result['skipped'][0]['review_candidate']
        assert snapshot['call_id'] == call and snapshot['operation_key'] == meta['key']


def test_receipt_recovery_candidates_only_empty_and_collection_stop(workspace, monkeypatch, tmp_path):
    w = workspace
    source, run, _, _, _, _, rejected = mixed(w, visual=True)
    worker, attempts = no_transport(w)
    receipt(w, run, source, dict(facts=[rejected]))
    render = pdf_images.render
    monkeypatch.setattr(pdf_images, 'render', lambda *a, **kw: pytest.fail('Квитанция не требует рендера'))
    first = read_tool(w, worker, run, source, tmp_path)
    assert first['status'] == 'empty' and first['facts'] == [] and 'visual_observations' not in first
    assert first['skipped'][0]['review_candidate']['status'] == 'not_admitted'
    assert collection.state(current(w, run)) == dict(closed=False, idle=1)
    assert read_tool(w, worker, run, source, tmp_path/'repeat') == first
    assert collection.state(current(w, run)) == dict(closed=False, idle=1)
    # Вторая самостоятельная квитанция также не дает нового материала.
    monkeypatch.setattr(pdf_images, 'render', render)
    receipt(w, current(w, run), source, dict(facts=[rejected]), question='Оценка рынка')
    second = read_tool(w, worker, run, source, tmp_path/'second', question='Оценка рынка')
    assert second['status'] == 'empty'
    state = collection.state(current(w, run))
    assert state['closed'] and state['idle'] == 2 and state['reason'] == 'idle'
    assert attempts == [] and fact_rows(w) == []


def test_unknown_visual_response_does_not_retry(workspace, tmp_path):
    w = workspace
    source, run, _, _, _, _, rejected = mixed(w, visual=True)
    call, _, _, _, _ = receipt(w, run, source, dict(facts=[rejected]))
    with w.engine.begin() as c:
        saved = current(w, run)['checkpoint']
        for operation in saved['extraction_operations'].values():
            operation.pop('response'); operation.pop('response_call_id')
        execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r', r=run['id'], v=json_value(saved))
        execute(c, "UPDATE aist.model_calls SET data=jsonb_set(data,'{transport_state}','\"unknown\"') WHERE id=:id", id=call)
    worker, attempts = no_transport(w)
    with pytest.raises(ApiError) as refused:
        runtime.obtain_facts(worker, current(w, run), dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    assert refused.value.code == 'outcome_unknown' and attempts == []
    assert extraction.recorded_extractions(current(w, run)) == []


@pytest.mark.parametrize('defect', ['source_id', 'page', 'owner', 'project', 'foreign_call', 'operation_call', 'sample', 'forged_snapshot', 'forecast'])
def test_strict_rejection_cannot_leave_a_snapshot(workspace, defect):
    w = workspace
    source, run, pages, layouts, good, qual, rejected = mixed(w)
    payload = dict(facts=[rejected, qual, good])
    pin = pin_of(source)
    operation = None
    if defect == 'source_id':
        pin['source_id'] = 'src_other'
    elif defect == 'page':
        payload['facts'][-1]['evidence'][0]['page'] = 999
    elif defect == 'forged_snapshot':
        payload['facts'][0]['review_candidate'] = dict(candidate_id='candidate_forged', status='not_admitted')
    elif defect == 'forecast':
        payload['facts'][0]['nature'] = 'forecast'
    call = accounted_response(w, run, payload)
    if defect in ('owner', 'project'):
        field = 'owner_id' if defect == 'owner' else 'project_id'
        run = dict(run, **{field: 'usr_other' if defect == 'owner' else 'prj_other'})
    elif defect == 'foreign_call':
        other = start_note_run(w, source)
        call = accounted_response(w, other, payload)
    elif defect == 'operation_call':
        operation = dict(key='f'*64)
    elif defect == 'sample':
        register(w.service, w.actor, w.project, source['id'], 'samples', None, None, 'Образец для теста')
    before = checkpoint(w, run)
    with pytest.raises(ApiError):
        extraction.ingest(w.runs, run, pages, payload, call, pin, layouts, question='Строгая граница', operation=operation)
    assert checkpoint(w, run) == before and fact_rows(w) == []


def test_parent_diagnostics_do_not_disclose_snapshot_or_change_checkpoint(workspace):
    w = workspace
    source, run, pages, layouts, _, qual, rejected = mixed(w)
    result, _ = ingest(w, run, pages, layouts, [qual, rejected], 'Числа и бизнес')
    assert startup(w, run)[0]['skipped'] == result['skipped']
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(result['facts'][0])))
    assert saved['ok'], saved
    parent = saved['result']['version']
    before = checkpoint(w, run)
    child = follow_up(w, run, source, 'Уточни текст', base_version_id=parent['id'], expected_artifact_revision=parent['revision'])
    entries = startup(w, child)
    assert len(entries) == 1 and entries[0]['call_id'] == before['extractions'][0]['call_id']
    diagnostic = {k:v for k,v in result['skipped'][0].items() if k != 'review_candidate'}
    assert entries[0]['skipped'] == [diagnostic]
    assert checkpoint(w, child) is None and checkpoint(w, run) == before
    assert 'review_candidate' not in json_value(model_view(entries))
    from app.coverage import findings
    with w.engine.connect() as c:
        assert findings(c, child, parent) == saved['result']['checks'][0]['findings']
    w.runs.stop(child['id'], child['epoch'], 'validation_failed')
    unrelated = follow_up(w, child, source, 'Другой вопрос')
    assert startup(w, unrelated) == [] and checkpoint(w, unrelated) is None


@pytest.mark.parametrize('code', ['number_mismatch', 'unit_mismatch', 'period_mismatch', 'ambiguous_link'])
def test_contradiction_or_uncertainty_keeps_its_original_reason(workspace, code):
    w = workspace
    _, run, pages, layouts, good, _, _ = mixed(w)
    if code == 'number_mismatch':
        good['amount']['value'] = '138'
    elif code == 'unit_mismatch':
        good['basis'] = None
    elif code == 'period_mismatch':
        good['period']['end'] = '2028-06-30'
    else:
        good['metric'] = 'Неизвестный показатель'
    result, _ = ingest(w, run, pages, layouts, [good], 'Проверка исходного заявления')
    assert result['status'] == 'empty' and result['facts'] == []
    skipped, = result['skipped']
    assert skipped['code'] == code and skipped['reason'] == extraction.SKIP_REASONS[code]
    assert skipped['message'] and skipped['review_candidate']['input'] == good
    assert skipped['review_candidate']['status'] == 'not_admitted'


def test_duplicate_indices_and_missing_table_are_distinct_in_one_response(workspace):
    w = workspace
    _, run, pages, layouts, _, _, rejected = mixed(w)
    items = [rejected, deepcopy(rejected), dict(rejected, table=None)]
    result, call = ingest(w, run, pages, layouts, items, None)
    snapshots = [s['review_candidate'] for s in result['skipped']]
    assert len({s['candidate_id'] for s in snapshots}) == 3
    assert [s['candidate_index'] for s in snapshots] == [1, 2, 3]
    assert [s['input'] for s in snapshots] == items
    assert all(s['call_id'] == call and s['operation_key'] is None for s in snapshots)
    assert len(checkpoint(w, run)['initial_extractions']) == 1
    again, _ = ingest(w, run, pages, layouts, items, None, call)
    assert again == result and len(checkpoint(w, run)['initial_extractions']) == 1
