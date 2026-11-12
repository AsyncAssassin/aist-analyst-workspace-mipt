"""DRAFT-01-R1: сохранность замечаний через HTTP и инструменты без вызова модели."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import make_engine
from app.errors import ApiError
from app.exports import Exports
from app.facts import pin_of, save_fact
from app.runs import Runs, execute
from app.search import register
from app.service import Service, ident
from app.storage import Storage
from app.versions import persist
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_domain import calculation
from test_draft01_postgres import export_request, http
from test_perf02_r1_postgres import upload
from test_perf02_r4_postgres import note_source, paragraph, ref, save_args, table_row

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def write_evidence(name, result):
    if destination := os.getenv('AIST_R1_EVIDENCE'):
        Path(destination, name + '.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))


def incomplete_note(source):
    # Исходная красная проба F01, критерий полноты и данные не упрощены.
    return dict(title='Показатели компании', audience='Аналитик', sections=[dict(
        section_id='sec_fin', title='Финансирование', blocks=[dict(
            block_id='block_matrix', kind='table', caption='Финансовые показатели', rows=[
                table_row('row_capital_old', 'Собственный капитал', '2022 г.', '3216', [ref(source, 'fact_capital2022')]),
                table_row('row_capital_new', 'Собственный капитал', '1 кв. 2023г.', '3658', [ref(source, 'fact_capital1q23')]),
                table_row('row_funding', 'Привлеченные средства', 'на 31.03.2023', '4392', [ref(source, 'fact_funding')]),
            ])])])


def start(w, sources, parent=None, skill='v3'):
    request = body(sources=[source['id'] for source in sources], question='Подготовь редакцию по выбранным документам')
    request.update(skill_version_id='skill_analytical-note-' + skill, requested_result_type='analytical_note')
    if parent:
        request.update(base_version_id=parent['version']['id'], expected_artifact_revision=parent['version']['revision'])
    send(w, conversation(w), request)
    run = w.runs.claim('draft01_r1')
    assert run['project_id'] == w.project
    return run


def save(w, run, content, parent=None, *, ownership_review=None, news_events=None):
    args = save_args(content, ownership_review)
    if news_events is not None:
        args['news_events'] = news_events
    if parent:
        if ownership_review is None:
            args.pop('ownership_review')
        version = parent['version']
        args.update(expected_revision=version['revision'], artifact_id=version['artifact_id'], parent_version_id=version['id'])
    result = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert result['ok'], result
    view = result['result']
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'completed' and state['error'] is None
    assert state['result_version_id'] == view['version']['id']
    # Новый tool_call_id с прежним ключом не создает версию, сообщение или запрос модели.
    with w.engine.connect() as c:
        before = execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one()
    again = w.runs.tool(run['id'], run['epoch'], ident('repeat'), 'aist_save_artifact', args)
    assert again['ok'] and again['result'] == view
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == before
        assert execute(c, 'SELECT count(*) FROM aist.messages WHERE run_id=:r', r=run['id']).scalar_one() == 2
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE project_id=:p', p=w.project).scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls WHERE owner_id=:a', a=w.actor).scalar_one() == 0
    return view


def edit(w, client, parent, content, mode, sources):
    version = parent['version']
    if mode == 'human':
        url = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions'
        request = dict(expected_revision=version['revision'], parent_version_id=version['id'],
                       payload=dict(version['payload'], content=content))
        headers = {'Idempotency-Key': ident('key')}
        response = client.post(url, json=request, headers=headers)
        assert response.status_code == 201, response.text
        child = response.json()
        with w.engine.connect() as c:
            count_before = execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE artifact_id=:a', a=version['artifact_id']).scalar_one()
        repeated = client.post(url, json=request, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == child
        with w.engine.connect() as c:
            assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE artifact_id=:a', a=version['artifact_id']).scalar_one() == count_before
    else:
        run = start(w, sources, parent, mode.removeprefix('model_'))
        child = save(w, run, content, parent)
    assert child['review']['acceptance'] is None
    assert child['checks'][1]['status'] == 'not_checked'
    assert child['version']['parent_version_id'] == version['id']
    assert w.runs.version(w.actor, w.project, version['id']) == parent
    return child


def codes(view):
    return [finding['code'] for finding in view['checks'][0]['findings']]


def assert_blocked(w, client, view, *, unchecked_semantics=False):
    version = view['version']
    read_url = f'/v1/projects/{w.project}/artifact-versions/{version["id"]}'
    current = client.get(read_url)
    assert current.status_code == 200 and current.json() == view
    assert view['checks'][0]['status'] == ('passed' if unchecked_semantics else 'failed')
    assert view['checks'][1]['status'] == 'not_checked' and view['review']['acceptance'] is None
    url = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
    with w.engine.connect() as c:
        before = tuple(execute(c, '''SELECT
            (SELECT count(*) FROM aist.version_acceptances WHERE project_id=:p),
            (SELECT count(*) FROM aist.exports WHERE project_id=:p)''', p=w.project).one())
    acceptance = client.post(url + '/accept', json=dict(expected_revision=view['review']['revision'],
        content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=not unchecked_semantics), headers={'Idempotency-Key': ident('key')})
    assert acceptance.status_code == 422, acceptance.text
    assert acceptance.json()['error']['code'] == 'validation_failed'
    assert acceptance.json()['error']['message'] == ('Подтвердите принятие без смысловой проверки.'
        if unchecked_semantics else 'Ошибки проверок блокируют принятие этой версии.')
    export = client.post(url + '/exports', json=export_request('docx', 'accepted', view['review']['revision']),
                         headers={'Idempotency-Key': ident('key')})
    assert export.status_code == 409 and export.json()['error']['code'] == 'acceptance_required', export.text
    with w.engine.connect() as c:
        after = tuple(execute(c, '''SELECT
            (SELECT count(*) FROM aist.version_acceptances WHERE project_id=:p),
            (SELECT count(*) FROM aist.exports WHERE project_id=:p)''', p=w.project).one())
    assert before == after
    return dict(acceptance_status=acceptance.status_code, acceptance_body=acceptance.json(),
                export_status=export.status_code, export_body=export.json(), records_before=before, records_after=after)


def accept_and_export(w, client, view, tmp_path):
    assert view['checks'][0]['status'] == 'passed' and codes(view) == []
    assert view['review']['acceptance'] is None and view['checks'][1]['status'] == 'not_checked'
    version = view['version']
    url = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
    acceptance = client.post(url + '/accept', json=dict(expected_revision=view['review']['revision'],
        content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=True), headers={'Idempotency-Key': ident('key')})
    assert acceptance.status_code == 200, acceptance.text
    accepted = w.runs.version(w.actor, w.project, version['id'])
    assert accepted['review']['acceptance']['content_sha256'] == version['content_sha256']
    response = client.post(url + '/exports', json=export_request('docx', 'accepted', accepted['review']['revision']),
                           headers={'Idempotency-Key': ident('key')})
    assert response.status_code == 202, response.text
    # Очередь этой свежей БД не содержит посторонних незавершенных заданий.
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    exports.process_one()
    data, released = exports.get(w.actor, w.project, response.json()['id'], download=True)
    assert data and released['state'] == 'ready' and released['acceptance_id'] == accepted['review']['acceptance']['id']
    return accepted


def assert_reload(w, view):
    fresh = Runs(Service(make_engine(w.settings), Storage(w.settings.storage_root)), w.settings)
    try:
        assert fresh.version(w.actor, w.project, view['version']['id']) == view
    finally:
        fresh.engine.dispose()


@pytest.mark.parametrize('mode', ['human', 'model_v2', 'model_v3'])
def test_sparse_matrix_can_be_accepted_after_unrelated_edit(workspace, tmp_path, mode):
    w = workspace
    source, _ = note_source(w)
    original = save(w, start(w, [source]), incomplete_note(source))
    assert codes(original) == []
    with http(w) as client:
        original = accept_and_export(w, client, original, tmp_path)
        content = deepcopy(original['version']['payload']['content'])
        content['title'] = 'Показатели компании: рабочая редакция'
        child = edit(w, client, original, content, mode, [source])
        assert child['version']['payload']['content']['sections'] == original['version']['payload']['content']['sections']
        assert codes(child) == []
        accept_and_export(w, client, child, tmp_path)
        assert_reload(w, w.runs.version(w.actor, w.project, child['version']['id']))
        write_evidence('matrix-' + mode, dict(original=original, child=child))


def test_matrix_identity_cosmetics_and_independent_corrections(workspace, tmp_path):
    w = workspace
    source, _ = note_source(w)
    original = save(w, start(w, [source]), incomplete_note(source))
    with http(w) as client:
        content = deepcopy(original['version']['payload']['content'])
        content['title'] = 'Выручка 999 млн RUB'
        table = content['sections'][0]['blocks'][0]
        table['block_id'] = 'block_renamed'
        table['caption'] += ' '
        for index, row in enumerate(table['rows']):
            row['row_id'] = f'row_renamed_{index}'
        content['sections'].insert(0, dict(section_id='sec_intro', title='Введение', blocks=[
            paragraph('block_intro', 'Сведения о клиентах.', [ref(source, 'fact_clients')])]))
        changed = edit(w, client, original, content, 'model_v3', [source])
        assert codes(changed) == [] and changed['checks'][0]['status'] == 'passed'
        assert changed['version']['payload']['content']['title'] == 'Выручка 999 млн RUB'
        assert changed['checks'][1]['status'] == 'not_checked'
        content = deepcopy(changed['version']['payload']['content'])
        content['audience'] = 'Кредитный комитет'
        retained = edit(w, client, changed, content, 'human', [source])
        assert codes(retained) == codes(changed)
        assert retained['checks'][0]['status'] == 'passed'
        # Отдельная таблица меняет представление, но не значения и основания строк.
        content = deepcopy(retained['version']['payload']['content'])
        table = content['sections'][1]['blocks'][0]
        funding = table['rows'].pop()
        content['sections'][1]['blocks'].append(dict(block_id='block_funding', kind='table', caption='Привлеченные средства', rows=[funding]))
        matrix_fixed = edit(w, client, retained, content, 'model_v2', [source])
        assert codes(matrix_fixed) == [] and matrix_fixed['checks'][0]['status'] == 'passed'
        assert matrix_fixed['version']['basis'] == retained['version']['basis']
        content = deepcopy(matrix_fixed['version']['payload']['content'])
        content['title'] = 'Показатели компании'
        fixed = edit(w, client, matrix_fixed, content, 'model_v3', [source])
        accepted = accept_and_export(w, client, fixed, tmp_path)
        content = deepcopy(accepted['version']['payload']['content'])
        content['audience'] = 'Совет директоров'
        after_acceptance = edit(w, client, accepted, content, 'human', [source])
        assert codes(after_acceptance) == [] and after_acceptance['review']['acceptance'] is None
        assert w.runs.version(w.actor, w.project, original['version']['id']) == original
        assert_reload(w, after_acceptance)
        write_evidence('matrix-correction-chain', dict(original=original, renamed=changed, retained=retained,
            matrix_fixed=matrix_fixed, fixed=fixed, accepted=accepted, after_acceptance=after_acceptance))


def second_source(w, facts):
    selected = [facts['fact_clients'], facts['fact_capital1q23']]
    source = upload(w, 'selected_second', [fact['raw_text'] for fact in selected])
    pin = pin_of(source)
    with w.engine.begin() as c:
        for original in selected:
            fact = deepcopy(original)
            fact['source'] = pin
            for evidence in fact['evidence']:
                evidence['source'] = pin
            save_fact(w.service, c, w.actor, w.project, pin, fact)
    return source


@pytest.mark.parametrize('mode', ['human', 'model_v3'])
def test_review_missing_selected_pdf_is_a_draft_and_tracks_real_coverage(workspace, tmp_path, mode):
    w = workspace
    source, facts = note_source(w)
    other = second_source(w, facts)
    run = start(w, [source, other])
    # Список проекта изменился после закрепления Run; этот PDF не обязателен.
    upload(w, 'not_selected', ['Не выбранный для этой записки документ.'])
    content = incomplete_note(source)
    content['sections'][0]['blocks'][0]['rows'] = content['sections'][0]['blocks'][0]['rows'][1:2]
    content['title'] = 'Справка по perf02r4.pdf и selected_second.pdf'
    original = save(w, run, content)
    assert codes(original) == ['unused_selected_source']
    finding, = original['checks'][0]['findings']
    assert finding['block_id'] is None and finding['severity'] == 'error' and 'selected_second.pdf' in finding['message']
    # Сравнительный факт второго PDF добавлен в basis, но не подменяет основание содержания.
    assert {f['source']['source_version_id'] for f in original['version']['basis']['facts']} == {source['id'], other['id']}
    with http(w) as client:
        initial_denial = assert_blocked(w, client, original)
        content = deepcopy(original['version']['payload']['content'])
        content['title'] = 'Справка по выбранным документам'
        content['audience'] = 'Команда анализа'
        child = edit(w, client, original, content, mode, [source, other])
        assert codes(child) == ['unused_selected_source']
        assert_blocked(w, client, child)
        assert_reload(w, child)
        content = deepcopy(child['version']['payload']['content'])
        content['sections'][0]['blocks'].append(paragraph('block_second', 'Сведения о клиентах.', [ref(other, 'fact_clients')]))
        covered = edit(w, client, child, content, 'model_v3', [source, other])
        assert codes(covered) == []
        accepted = accept_and_export(w, client, covered, tmp_path)
        content = deepcopy(accepted['version']['payload']['content'])
        content['sections'][0]['blocks'].pop()
        removed = edit(w, client, accepted, content, mode, [source, other])
        assert codes(removed) == ['unused_selected_source']
        assert_blocked(w, client, removed)
        # Продолжение сохраняет закрепленный набор родителя, как требует действующий контракт.
        with pytest.raises(ApiError) as changed_selection:
            start(w, [source], removed)
        assert changed_selection.value.code == 'resource_not_found'
        separate = save(w, start(w, [source]), deepcopy(content))
        assert codes(separate) == []
        assert separate['version']['provenance']['source_versions'] == [pin_of(source)]
        assert w.runs.version(w.actor, w.project, original['version']['id']) == original
        write_evidence('coverage-' + mode, dict(original=original, initial_denial=initial_denial, child=child,
            covered=covered, accepted=accepted, removed=removed, separate=separate))


def test_calculation_inputs_close_coverage_without_rejecting_a_sparse_matrix(workspace):
    w = workspace
    source, _ = note_source(w)
    arguments, records = calculation()
    other = upload(w, 'calculation_second', [record['data']['raw_text'] for record in records.values()])
    pin = pin_of(other)
    with w.engine.begin() as c:
        for record in records.values():
            fact = record['data']
            fact['source'] = pin
            for evidence in fact['evidence']:
                evidence['source'] = pin
            save_fact(w.service, c, w.actor, w.project, pin, fact, calculation_basis=record['calculation_basis'])
        for item in arguments['inputs']:
            item['fact']['source_version_id'] = other['id']
    original = save(w, start(w, [source, other]), incomplete_note(source))
    assert set(codes(original)) == {'unused_selected_source'}
    with http(w) as client:
        assert_blocked(w, client, original)
        run = start(w, [source, other], original)
        calculated = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_calculate', arguments)
        assert calculated['ok'], calculated
        content = deepcopy(original['version']['payload']['content'])
        content['sections'][0]['blocks'].append(dict(block_id='block_calculation', kind='paragraph', claim=dict(
            text='Изменение выручки рассчитано по сохраненным данным.', origin='calculation', fact_refs=[],
            calculation_ids=[calculated['result']['calculation_id']])))
        covered = save(w, run, content, original)
        assert codes(covered) == []
        assert covered['checks'][0]['status'] == 'passed'
        content = deepcopy(covered['version']['payload']['content'])
        content['audience'] = 'Руководство'
        retained = edit(w, client, covered, content, 'human', [source, other])
        assert codes(retained) == []
        assert retained['checks'][0]['status'] == 'passed'
        write_evidence('coverage-calculation', dict(original=original, covered=covered, retained=retained))


def test_coverage_does_not_relax_source_sha_reference_or_origin_boundaries(workspace):
    w = workspace
    source, facts = note_source(w)
    selected = second_source(w, facts)
    unselected = upload(w, 'unselected', ['Документ того же проекта вне Run.'])
    _, project = w.service.write(w.actor, 'createProject', ident('key'), dict(name='Другой проект'))
    elsewhere = SimpleNamespace(**{**vars(w), 'project': project['id']})
    foreign = upload(elsewhere, 'foreign', ['Документ другого проекта.'])
    run = start(w, [source, selected])
    for forbidden in (foreign, unselected):
        content = incomplete_note(source)
        content['sections'][0]['blocks'][0]['rows'][0]['fact_refs'] = [ref(forbidden, 'fact_clients')]
        reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
        assert not reply['ok'] and reply['error']['code'] == 'resource_not_found'
    for kind in ('fact', 'calculation', 'origin', 'structure'):
        content = incomplete_note(source)
        claim = paragraph('block_invalid', 'Сведения о клиентах.', [ref(source, 'fact_clients')])
        if kind == 'fact':
            claim['claim']['fact_refs'] = [ref(source, 'fact_absent')]
        elif kind == 'calculation':
            claim['claim']['calculation_ids'] = ['calc_absent']
        elif kind == 'origin':
            claim['claim']['origin'] = 'human_editorial'
        else:
            claim['block_id'] = 'block_matrix'
        content['sections'][0]['blocks'].append(claim)
        reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
        assert not reply['ok'] and reply['error']['code'] == 'validation_failed', reply
    bad_input = dict(run['input'], source_versions=[pin_of(source), dict(pin_of(selected), sha256='0' * 64)])
    with w.engine.begin() as c, pytest.raises(ApiError) as mismatch:
        persist(w.runs, c, dict(run, input=bad_input), save_args(incomplete_note(source))['payload'])
    assert mismatch.value.code == 'resource_not_found'
    register(w.service, w.actor, w.project, unselected['id'], 'samples', None, None, 'Синтетический образец')
    sample_input = dict(run['input'], source_versions=[pin_of(source), pin_of(unselected)])
    content = incomplete_note(source)
    content['sections'][0]['blocks'].append(paragraph('block_sample', 'Пример оформления.', [ref(unselected, 'fact_sample')]))
    with w.engine.begin() as c, pytest.raises(ApiError) as sample:
        persist(w.runs, c, dict(run, input=sample_input), save_args(content)['payload'])
    assert sample.value.code == 'sample_not_evidence'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE project_id=:p', p=w.project).scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls WHERE owner_id=:a', a=w.actor).scalar_one() == 0
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
