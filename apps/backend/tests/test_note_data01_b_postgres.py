"""Предварительный абзац с отклоненным вводом: ссылки, наследование и строгие отказы."""
from copy import deepcopy
from dataclasses import replace
import os

import pytest

from app import extraction, collection
from app.errors import ApiError
from app.normalization import checked
from app.review_candidates import available, pages
from app.runs import execute
from app.search import register
from app.service import ident
from app.versions import persist, review
from test_d15_postgres import workspace
from test_draft01_postgres import http, export_request
from test_flow01_02_r1_postgres import follow_up, startup
from test_flow01_postgres import current
from test_note_data01_a_postgres import mixed, receipt
from test_note_path06_r2_postgres import codes, edit, observation
from test_perf02_r1_postgres import accounted_response, fact_rows
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def prepared(w, *, visual=False, only_candidate=False, extra_candidate=False):
    source, run, source_pages, layouts, good, qual, rejected = mixed(w, visual=visual)
    payload = dict(facts=[rejected] if only_candidate else [good, rejected, qual])
    if extra_candidate:
        payload['facts'].append(dict(rejected, evidence=list(reversed(rejected['evidence']))))
    if visual:
        payload['visual_observations'] = [observation('41.8', 2029, forecast=True)]
        call, meta, source_pages, layouts, diagrams = receipt(w, run, source, payload)
        result = extraction.ingest(w.runs, run, source_pages, payload, call, layouts=layouts,
                                   diagrams=diagrams, operation=meta, question='Все данные')
    else:
        call = accounted_response(w, run, payload)
        result = extraction.ingest(w.runs, run, source_pages, payload, call, layouts=layouts, question='Все данные')
    return source, current(w, run), result


def content(result, *, text='Предварительно объем рынка составляет 3,7 трлн руб.', mixed_basis=False):
    claim = dict(text=text, origin='source_candidate', fact_refs=[], calculation_ids=[],
                 review_candidate_ids=[result['skipped'][0]['review_candidate']['candidate_id']])
    if mixed_basis:
        fact = next(f for f in result['facts'] if f['amount'])
        claim['fact_refs'] = [dict(source_version_id=fact['source']['source_version_id'], fact_id=fact['fact_id'])]
        claim['visual_observation_ids'] = [item['observation_id'] for item in result['visual_observations']]
    return dict(title='Предварительная справка Meridian', audience='Аналитик', sections=[
        dict(section_id='sec_candidate', title='Показатели', blocks=[dict(block_id='block_candidate', kind='paragraph', claim=claim)])])


def save(w, run, value):
    args = save_args(value)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert saved['ok'], saved
    return saved['result'], args


def version_count(w):
    with w.engine.connect() as c:
        return execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one()


def test_server_snapshot_repeat_reload_and_source_usage(workspace):
    w = workspace
    source, run, result = prepared(w)
    before = deepcopy(result)
    facts = fact_rows(w)
    progress = collection.state(run)
    saved, args = save(w, run, content(result))
    snapshot = result['skipped'][0]['review_candidate']
    assert saved['version']['basis']['review_candidates'] == [
        {key: result['skipped'][0][key] for key in ('review_candidate', 'code', 'reason', 'message')}]
    assert saved['version']['basis']['facts'] == saved['version']['basis']['calculations'] == []
    assert {'unchecked_source_candidate', 'coverage_gap'} <= codes(saved)
    assert 'unused_selected_source' not in codes(saved)
    assert pages(snapshot) == [1, 2] and snapshot['status'] == 'not_admitted'
    assert checked('VersionView', saved) == saved and saved['review']['acceptance'] is None
    assert w.runs.version(w.actor, w.project, saved['version']['id']) == saved
    repeated = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert repeated['ok'] and repeated['result'] == saved and version_count(w) == 1
    assert fact_rows(w) == facts and result == before and collection.state(current(w, run)) == progress


@pytest.mark.parametrize('case', ['unknown', 'other_run', 'owner', 'project', 'sha', 'source',
                                 'snapshot_sha', 'snapshot_source', 'snapshot_version', 'sample'])
def test_authority_refuses_without_partial_version(workspace, case):
    w = workspace
    source, run, result = prepared(w, only_candidate=case == 'sample')
    payload = save_args(content(result))['payload']
    context = deepcopy(run)
    if case == 'unknown':
        payload['content']['sections'][0]['blocks'][0]['claim']['review_candidate_ids'] = ['candidate_absent']
    elif case == 'other_run':
        context = start_note_run(w, source)
    elif case == 'owner':
        context['owner_id'] = 'usr_foreign'
    elif case == 'project':
        context['project_id'] = 'prj_foreign'
    elif case == 'sha':
        context['input']['source_versions'][0]['sha256'] = 'b' * 64
    elif case == 'source':
        context['input']['source_versions'][0]['source_id'] = 'src_foreign'
    elif case.startswith('snapshot_'):
        # Защита также проверяет pin самого снимка, а не только текущего Run.
        candidate = next(iter(available(context).values()))['review_candidate']
        key, value = {'snapshot_sha': ('sha256', 'c' * 64), 'snapshot_source': ('source_id', 'src_foreign'),
                      'snapshot_version': ('source_version_id', 'sv_foreign')}[case]
        candidate['source'][key] = value
    else:
        register(w.service, w.actor, w.project, source['id'], 'samples', None, None, 'Образец')
    with pytest.raises(ApiError):
        with w.engine.begin() as c:
            persist(w.runs, c, context, payload)
    assert version_count(w) == 0


@pytest.mark.parametrize('case', ['missing_ids', 'empty_ids', 'wrong_origin', 'forged_snapshot', 'table', 'answer', 'fact', 'calculation'])
def test_candidate_cannot_bypass_other_contracts(workspace, case):
    w = workspace
    source, run, result = prepared(w)
    value = content(result)
    claim = value['sections'][0]['blocks'][0]['claim']
    ref = dict(source_version_id=source['id'], fact_id=claim['review_candidate_ids'][0])
    args = save_args(value)
    name = 'aist_save_artifact'
    if case == 'missing_ids':
        claim.pop('review_candidate_ids')
    elif case == 'empty_ids':
        claim['review_candidate_ids'] = []
    elif case == 'wrong_origin':
        claim['origin'] = 'inference'
    elif case == 'forged_snapshot':
        claim['review_candidates'] = [result['skipped'][0]['review_candidate']]
    elif case == 'table':
        value['sections'][0]['blocks'] = [dict(block_id='block_table', kind='table', caption='Показатели', rows=[
            dict(row_id='row_candidate', label='Рынок', amount=dict(value='3.7', unit='money', scale='1000000000000', currency='RUB'),
                 period_label='Не указано', fact_refs=[], calculation_ids=[], review_candidate_ids=claim['review_candidate_ids'])])]
    elif case == 'answer':
        args['payload'] = dict(result_type='evidence_answer', schema_version='1.0.0', content=dict(claims=[claim], missing_information=[]))
    elif case == 'fact':
        claim.pop('review_candidate_ids')
        claim.update(origin='source_fact', fact_refs=[ref])
    else:
        name = 'aist_calculate'
        args = dict(formula_id='growth_percent.v1', inputs=[dict(name=n, fact=ref) for n in ('previous', 'current')],
                    rounding=dict(mode='HALF_UP', places=2))
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), name, args)
    assert not reply['ok'], reply
    assert version_count(w) == 0


@pytest.mark.parametrize('text', [
    'Выручка составила 137 млн руб. за 1Q28.',
    'Выручка составила 138 млн руб. за 1Q28.',
    'Выручка составила 137 млрд руб. за 1Q28.',
    'Выручка составила 137 млн руб. за 2028 год.',
])
def test_mixed_paragraph_keeps_unchecked_prose_and_source_warnings(workspace, text):
    w = workspace
    _, run, result = prepared(w, visual=True)
    value = content(result, text=text, mixed_basis=True)
    baseline = deepcopy(value)
    claim = baseline['sections'][0]['blocks'][0]['claim']
    claim.pop('review_candidate_ids')
    claim['origin'] = 'source_fact'
    with w.engine.begin() as c:
        _, _, findings, errors = review(w.runs, c, run, save_args(baseline)['payload'])
    assert not errors
    saved, _ = save(w, run, value)
    actual = saved['checks'][0]['findings']
    assert [f for f in actual if f['code'] not in ('unchecked_source_candidate', 'coverage_gap')] == findings
    assert codes(saved, 'block_candidate') == {'unchecked_source_candidate', 'unchecked_visual_observation'}
    assert saved['checks'][0]['status'] == 'passed' and all(f['severity'] == 'warning' for f in actual)
    assert saved['checks'][1]['status'] == 'not_checked' and saved['review']['acceptance'] is None
    assert saved['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['text'] == text


def test_edit_inheritance_parent_scope_and_explicit_human_acceptance(workspace):
    w = workspace
    source, run, result = prepared(w, extra_candidate=True)
    saved, _ = save(w, run, content(result))
    payload = deepcopy(saved['version']['payload'])
    claim = payload['content']['sections'][0]['blocks'][0]['claim']
    claim.update(text='Предварительные сведения требуют сверки.', origin='inference')
    claim.pop('review_candidate_ids')
    edited = edit(w, saved, payload)
    assert edited['version']['basis']['review_candidates'] == saved['version']['basis']['review_candidates']
    assert edited['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['origin'] == 'source_candidate'
    assert 'unchecked_source_candidate' in codes(edited)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    next_run = follow_up(w, run, source, 'Продолжи предварительный абзац', base_version_id=edited['version']['id'],
                         target_artifact_id=edited['version']['artifact_id'], expected_artifact_revision=edited['version']['revision'])
    entries = startup(w, next_run)
    ids = {skip['review_candidate']['candidate_id'] for entry in entries for skip in entry['skipped'] if 'review_candidate' in skip}
    cid = result['skipped'][0]['review_candidate']['candidate_id']
    assert ids == {cid} and not extraction.recorded_extractions(current(w, next_run))
    unselected = result['skipped'][1]['review_candidate']['candidate_id']
    assert unselected not in ids and unselected not in available(next_run, edited['version'])
    assert sum(len(entry['skipped']) for entry in entries) == 2
    payload = deepcopy(edited['version']['payload'])
    payload['content']['sections'][0]['blocks'] = [dict(block_id='block_new', kind='paragraph', claim=dict(
        text='Редакторский вывод.', origin='human_editorial', fact_refs=[], calculation_ids=[]))]
    removed = edit(w, edited, payload)
    assert 'review_candidates' not in removed['version']['basis']
    assert 'unchecked_source_candidate' not in codes(removed) and 'coverage_gap' in codes(removed)
    assert w.runs.version(w.actor, w.project, saved['version']['id']) == saved
    assert not available(next_run, removed['version'])
    w.runs.stop(next_run['id'], next_run['epoch'], 'validation_failed')
    distant = follow_up(w, next_run, source, 'Продолжи новую версию', base_version_id=removed['version']['id'],
                        target_artifact_id=removed['version']['artifact_id'], expected_artifact_revision=removed['version']['revision'])
    diagnostics = startup(w, distant)
    assert diagnostics and all('review_candidate' not in skip for entry in diagnostics for skip in entry['skipped'])
    with pytest.raises(ApiError):
        with w.engine.begin() as c:
            persist(w.runs, c, current(w, distant), saved['version']['payload'], removed['version']['artifact_id'],
                    removed['version']['id'], removed['version']['revision'])
    w.settings = replace(w.settings, executor_mode='disabled', live_models_enabled=False, embeddings_enabled=False)
    with http(w) as client:
        version = edited['version']
        base = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
        assert edited['checks'][0]['status'] == 'passed'
        assert all(f['severity'] == 'warning' for f in edited['checks'][0]['findings'])
        response = client.post(base + '/accept', json=dict(expected_revision=0, content_sha256=version['content_sha256'],
            acknowledge_unchecked_semantics=False), headers={'Idempotency-Key': ident('key')})
        assert response.status_code == 422, response.text
        for fmt in ('docx', 'pdf'):
            response = client.post(base + '/exports', json=export_request(fmt, 'accepted'), headers={'Idempotency-Key': ident('key')})
            assert response.status_code == 409 and response.json()['error']['code'] == 'acceptance_required'
        response = client.post(base + '/accept', json=dict(expected_revision=0, content_sha256=version['content_sha256'],
            acknowledge_unchecked_semantics=True), headers={'Idempotency-Key': ident('key')})
        assert response.status_code == 200, response.text
        accepted = w.runs.version(w.actor, w.project, version['id'])
        assert accepted['review']['acceptance']['content_sha256'] == version['content_sha256']
        assert accepted['checks'] == edited['checks']
        assert w.runs.version(w.actor, w.project, removed['version']['id']) == removed


def test_candidate_pages_include_all_saved_locators():
    candidate = dict(input=dict(evidence=[dict(page=5), dict(page=2), dict(page=5)],
                               basis=dict(page=3, reporting_page=7), table=dict(page=4)))
    before = deepcopy(candidate)
    assert pages(candidate) == [2, 3, 4, 5, 7] and candidate == before


def test_candidate_amount_equal_to_fact_still_warns_without_semantic_verification(workspace):
    w = workspace
    source, run, source_pages, layouts, good, _, _ = mixed(w)
    rejected = deepcopy(good)
    rejected['period']['end'] = '2028-06-30'
    packet = dict(facts=[good, rejected])
    call = accounted_response(w, run, packet)
    result = extraction.ingest(w.runs, run, source_pages, packet, call, layouts=layouts, question='Показатели')
    assert result['skipped'][0]['review_candidate']['input'] == rejected
    value = content(result, text='Выручка составила 137 млн руб. за 1Q28.')
    value['sections'][0]['blocks'][0]['claim']['fact_refs'] = [
        dict(source_version_id=source['id'], fact_id=result['facts'][0]['fact_id'])]
    saved, _ = save(w, run, value)
    assert codes(saved, 'block_candidate') == {'unchecked_source_candidate'}
    assert saved['checks'][0]['status'] == 'passed'
    assert saved['checks'][1]['status'] == 'not_checked'
    assert all(f['severity'] == 'warning' for f in saved['checks'][0]['findings'])
    assert saved['review']['acceptance'] is None
