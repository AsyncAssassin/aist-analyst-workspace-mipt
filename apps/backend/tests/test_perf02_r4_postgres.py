"""Complete save feedback, label and unit normalization, editor drafts and release of a small note."""
from copy import deepcopy
from dataclasses import replace
import io
import os

import pytest
from pypdf import PdfReader

from app.errors import ApiError
from app.exports import Exports
from app.facts import pin_of, save_fact
from app.note_renderer import renderer_versions
from app.note_standard import content_plan
from app.runs import execute
from app.service import ident
from app.versions import write as write_version
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_perf02_r1_postgres import upload

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

LINES = ['Синтетика: данные компании на 31.03.2023',
         'ROA, % 11,6% 1Q23',
         'ROE, % 27,3% 1Q23',
         'Собственный капитал 3 658 млн руб. 1 кв. 2023г.',
         'Собственный капитал 3 216 млн руб. 2022 г.',
         'Общий размер привлеченных средств 4 392 млн руб. на 31.03.2023',
         'Облигации 943 млн руб. на 31.03.2023',
         'Прокатные фирмы сдают приборы для замера шума',
         'Активные займы: более 18 тыс. шт. На контрольном рынке около 7 тыс. шт.']
MONEY = dict(unit='money', scale='1000000', currency='RUB')
Q1_2023 = dict(kind='interval', label='1Q23', start='2023-01-01', end='2023-03-31')
AT_2023 = dict(kind='instant', label='на 31.03.2023', start='2023-03-31', end='2023-03-31')
UNKNOWN = dict(kind='unknown', label='Не указано', start=None, end=None)
SPECS = [
    ('fact_roa', 'ROA, %', 2, dict(value='11.6', unit='percent', scale='1', currency=None), Q1_2023),
    ('fact_roe', 'ROE, %', 3, dict(value='27.3', unit='percent', scale='1', currency=None), Q1_2023),
    ('fact_capital1q23', 'Собственный капитал', 4, dict(MONEY, value='3658'), dict(kind='instant', label='1 кв. 2023г.', start='2023-03-31', end='2023-03-31')),
    ('fact_capital2022', 'Собственный капитал', 5, dict(MONEY, value='3216'), dict(kind='instant', label='2022 г.', start='2022-12-31', end='2022-12-31')),
    ('fact_funding', 'Общий размер привлеченных средств', 6, dict(MONEY, value='4392'), AT_2023),
    ('fact_bonds', 'Облигации', 7, dict(MONEY, value='943'), AT_2023),
    ('fact_clients', 'Клиенты', 8, None, UNKNOWN),
    ('fact_market', 'Активные займы', 9, None, UNKNOWN),
]


def note_source(w, *, following=()):
    """A Cyrillic PDF with saved percent, balance and qualitative facts located on its page."""
    source = upload(w, 'perf02r4', LINES, following=following)
    pin = pin_of(source)
    saved = {}
    with w.engine.begin() as c:
        for fact_id, metric, line, amount, period in SPECS:
            fact = dict(fact_id=fact_id, source=pin, extraction_version='perf02-r4-test.v1', metric=metric, subject='Синтетика',
                        reporting_basis='Данные компании', nature='reported_actual' if amount else 'source_statement',
                        period=period, raw_text=LINES[line - 1], amount=amount, limitations=['Сводка тестовой компании.'],
                        evidence=[dict(evidence_id='ev_' + fact_id, source=pin, quote=LINES[line - 1],
                                       locator=dict(pdf_page=1, area='Строка ' + str(line), bbox_pt=None, coordinate_system='pdf_points_top_left'))])
            saved[fact_id] = save_fact(w.service, c, w.actor, w.project, pin, fact)
    return source, saved


def ref(source, fact_id):
    return dict(source_version_id=source['id'], fact_id=fact_id)


def paragraph(block_id, text, refs):
    return dict(block_id=block_id, kind='paragraph', claim=dict(text=text, origin='source_fact', fact_refs=refs, calculation_ids=[]))


def table_row(row_id, label, period_label, value, refs):
    return dict(row_id=row_id, label=label, period_label=period_label, amount=dict(MONEY, value=value), fact_refs=refs, calculation_ids=[])


def small_note(source, *, corrected=False, broken=False):
    """ROA, a year-end balance, a pivoting capital table and a one-date structure table.

    The flawed variant keeps a wrong bond amount and unsupported prose numbers. Under v29
    only the table mismatch blocks acceptance; prose grammar is not checked. broken=True
    also references a fact absent from the allowed version, which still refuses the save."""
    sections = [dict(section_id='sec_fin', title='Финансовые показатели', blocks=[
        paragraph('block_roa', 'По аудированной отчетности за 1Q23 ROE составил 27,3%, ROA 11,6%.', [ref(source, 'fact_roe'), ref(source, 'fact_roa')]),
        paragraph('block_capital', 'Собственный капитал на 31.03.2023 составил 3658 млн руб., на конец 2022 года 3216 млн руб.',
                  [ref(source, 'fact_capital1q23'), ref(source, 'fact_capital2022')]),
        dict(block_id='block_history', kind='table', caption='Динамика собственного капитала, млн руб.', rows=[
            table_row('row_2022', 'Собственный капитал, млн руб.', 'на 31.12.2022', '3216', [ref(source, 'fact_capital2022')]),
            table_row('row_1q23', 'Собственный капитал, млн руб.', 'на 31.03.2023', '3658', [ref(source, 'fact_capital1q23')])]),
        dict(block_id='block_structure', kind='table', caption='Структура финансирования на 31.03.2023, млн руб.', rows=[
            table_row('row_capital', 'Собственный капитал (млн руб.)', 'на 31.03.2023', '3658', [ref(source, 'fact_capital1q23')]),
            table_row('row_funding', 'Привлеченные средства', 'на 31.03.2023', '4392', [ref(source, 'fact_funding')]),
            table_row('row_bonds', 'Облигации', 'на 31.03.2023', '943' if corrected else '942', [ref(source, 'fact_bonds')])]),
    ]), dict(section_id='sec_market', title='Рынок и клиенты', blocks=[
        paragraph('block_market', 'Компания раскрывает оценку портфеля и ориентир по контрольному рынку.' if corrected else
                  'Активные займы: более 18 тыс. шт. и около 7 тыс. шт.', [ref(source, 'fact_market')]),
        paragraph('block_clients', 'Прокатные фирмы сдают приборы для замера шума.' if corrected else
                  'Прокатные фирмы сдают приборы для замера шума; 30% предпринимателей.', [ref(source, 'fact_clients')]),
    ])]
    if broken:
        sections[0]['blocks'].append(paragraph('block_absent', 'Выручка за 2022 год составила 3157 млн руб.', [ref(source, 'fact_absent')]))
    return dict(title='Синтетика: информационная справка', audience='Аналитик', sections=sections)


def model_save_args(content):
    """Команда модели для синтетических источников без сведений о владельцах."""
    return dict(content=content, ownership_review=dict(status='not_established',
        reason='Синтетические материалы этого сценария не раскрывают владельцев.', excluded_sources=[]))


def save_args(content, ownership_review=None):
    return dict(idempotency_key=ident('key'), expected_revision=0, artifact_id=None, parent_version_id=None,
                payload=dict(result_type='analytical_note', schema_version='1.0.0', content=content),
                ownership_review=ownership_review or dict(status='not_established',
                    reason='Синтетические материалы этого сценария не раскрывают владельцев.', excluded_sources=[]))


def start_note_run(w, source):
    request = body(sources=[source['id']], question='Подготовь справку по сохраненным фактам')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
    send(w, conversation(w), request)
    return w.runs.claim('perf02_r4')


def corrected_version(w):
    """One attempt refused for its broken reference, listing every place, then the corrected note saved and normalized."""
    source, facts = note_source(w)
    run = start_note_run(w, source)
    refused = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(small_note(source, broken=True)))
    assert not refused['ok'] and refused['error']['code'] == 'validation_failed'
    message = refused['error']['message']
    for expected in ('(row_bonds): Сумма 942 млн RUB и период «на 31.03.2023» не совпадают с основанием. Основание: 943 млн RUB, период «на 31.03.2023».',
                     '(block_absent): Факт fact_absent отсутствует в разрешенной версии ' + source['id']):
        assert expected in message, expected
    for absent in ('block_roa', 'block_capital', 'row_capital', 'row_2022', 'row_1q23', 'row_funding', 'ROA',
                   'block_market', 'block_clients'):
        assert absent not in message, absent
    assert message.count('(row_bonds)') == 1 and message.count('Факт fact_absent отсутствует') == 1
    assert message.count('Скопируй точный fact_id') == 1
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 0
    args = save_args(small_note(source, corrected=True))
    sent = deepcopy(args)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert saved['ok'], saved
    assert args == sent, 'normalization never rewrites the tool arguments'
    version = saved['result']['version']
    blocks = {b['block_id']: b for s in version['payload']['content']['sections'] for b in s['blocks']}
    assert blocks['block_history']['caption'] == 'Динамика собственного капитала'
    assert [(r['label'], r['period_label']) for r in blocks['block_history']['rows']] == [
        ('Собственный капитал', '2022 г.'), ('Собственный капитал', '1 кв. 2023г.')]
    assert blocks['block_structure']['caption'] == 'Структура финансирования на 31.03.2023'
    assert [(r['label'], r['period_label'], r['amount']['value']) for r in blocks['block_structure']['rows']] == [
        ('Собственный капитал', '1 кв. 2023г.', '3658'), ('Привлеченные средства', 'на 31.03.2023', '4392'), ('Облигации', 'на 31.03.2023', '943')]
    assert saved['result']['checks'][0]['status'] == 'passed' and saved['result']['review']['acceptance'] is None
    assert {f['fact_id'] for f in version['basis']['facts']} == set(facts) - {'fact_absent'}
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'completed'
    # Replaying the same arguments returns the saved version without a second write.
    again = w.runs.tool(run['id'], run['epoch'], ident('repeat'), 'aist_save_artifact', args)
    assert again['ok'] and again['result']['version']['id'] == version['id']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 1
    return version


def test_one_attempt_lists_every_place_then_the_corrected_note_is_normalized_and_saved(workspace):
    corrected_version(workspace)


def test_release_shows_the_shared_unit_once_in_docx_and_pdf(workspace, tmp_path):
    w = workspace
    version = corrected_version(w)
    plan = content_plan(version, dict(release_mode='draft', theme='standard'), [])
    captions = [data for kind, data in plan if kind == 'caption']
    assert captions == ['Динамика собственного капитала', 'млн руб.', 'Синтетика, Данные компании',
                        'Структура финансирования на 31.03.2023', 'млн руб.', 'Синтетика, Данные компании']
    tables = [data for kind, data in plan if kind == 'table']
    assert tables[0] == (['Показатель', '2022 г. (факт)', '1 кв. 2023г. (факт)'], [['Собственный капитал (на конец периода)', '3 216', '3 658']])
    assert tables[1] == (['Показатель', '1 кв. 2023г. (факт)'], [
        ['Собственный капитал (на конец периода)', '3 658'],
        ['Привлеченные средства (на конец периода)', '4 392'],
        ['Облигации (на конец периода)', '943']])
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    texts = {}
    for fmt in ('docx', 'pdf'):
        request = dict(expected_revision=0, format=fmt, template_version='aist-note.v2', renderer_version=renderer_versions('aist-note.v2')[fmt],
                       settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft'))
        status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], ident('key'), request)
        assert status == 202
        exports.process_one()
        data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
        assert ready['state'] == 'ready'
        if fmt == 'pdf':
            texts[fmt] = ''.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
        else:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                texts[fmt] = archive.read('word/document.xml').decode()
    for fmt, text in texts.items():
        assert 'Динамика собственного капитала' in text and 'млн руб.' in text, fmt
        assert 'Динамика собственного капитала, млн руб.' not in text and 'Собственный капитал, млн руб.' not in text, fmt
        assert 'Структура финансирования на 31.03.2023, млн руб.' not in text, fmt
    # Каждая из двух таблиц показывает единицу один раз, еще два упоминания есть в абзаце.
    assert texts['docx'].count('млн руб.') == 4 and 'млн RUB' not in texts['docx']


def test_human_editor_keeps_a_draft_with_content_findings_but_not_with_broken_references(workspace):
    w = workspace
    version = corrected_version(w)
    payload = deepcopy(version['payload'])
    payload['content']['sections'][1]['blocks'].append(dict(block_id='block_editorial', kind='paragraph', claim=dict(
        text='Редактор: около 25 тыс. активных займов.', origin='human_editorial', fact_refs=[], calculation_ids=[])))
    status, draft = write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], None, 'saveVersion', ident('key'),
                                  dict(expected_revision=1, parent_version_id=version['id'], payload=payload))
    assert status == 201 and draft['checks'][0]['status'] == 'failed'
    assert [f['code'] for f in draft['checks'][0]['findings']] == ['unsupported_editorial_number']
    assert draft['version']['payload']['content']['sections'][0] == version['payload']['content']['sections'][0]
    assert draft['version']['basis'] == version['basis']
    with pytest.raises(ApiError) as blocked:
        write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], draft['version']['id'], 'acceptVersion', ident('key'),
                      dict(expected_revision=0, content_sha256=draft['version']['content_sha256'], acknowledge_unchecked_semantics=True))
    assert blocked.value.code == 'validation_failed'
    # DRAFT-01: a wrong table amount is a saved draft with a blocking finding, not a refusal.
    wrong = deepcopy(version['payload'])
    wrong['content']['sections'][0]['blocks'][3]['rows'][2]['amount']['value'] = '942'
    status, mismatch = write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], None, 'saveVersion', ident('key'),
                                     dict(expected_revision=2, parent_version_id=draft['version']['id'], payload=wrong))
    assert status == 201 and mismatch['checks'][0]['status'] == 'failed' and mismatch['review']['acceptance'] is None
    finding, = mismatch['checks'][0]['findings']
    assert (finding['code'], finding['block_id']) == ('row_basis_mismatch', 'block_structure')
    assert finding['message'].startswith('строка 3 «Облигации» (row_bonds): Сумма 942 млн RUB')
    # A reference to a fact absent from the allowed version keeps the refusal, with the content remark alongside.
    absent = deepcopy(wrong)
    absent['content']['sections'][0]['blocks'].append(dict(block_id='block_absent', kind='paragraph', claim=dict(
        text='Выручка за 2022 год составила 3157 млн руб.', origin='human_editorial', fact_refs=[ref(source_of(version), 'fact_absent')], calculation_ids=[])))
    with pytest.raises(ApiError) as refused:
        write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], None, 'saveVersion', ident('key'),
                      dict(expected_revision=3, parent_version_id=mismatch['version']['id'], payload=absent))
    assert refused.value.code == 'validation_failed'
    message = refused.value.body['error']['message']
    assert '(block_absent): Факт fact_absent отсутствует' in message and '(row_bonds): Сумма 942 млн RUB' in message
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 3


def source_of(version):
    return dict(id=version['provenance']['source_versions'][0]['source_version_id'])


def test_draft_excerpt_materializes_immutable_pdf_text_and_refuses_substitution(workspace):
    from app.aso import presentation

    w = workspace
    source, facts = note_source(w)
    run = start_note_run(w, source)
    block = paragraph('block_quote', 'Цитата из PDF', [ref(source, 'fact_market')])
    block['claim']['origin'] = 'source_excerpt'
    content = dict(title='Справка', audience='Аналитик', sections=[
        dict(section_id='sec_market', title='Рынок и конкуренты', blocks=[block])])

    def attempt(value):
        return w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(value))

    for forged in ('В компании 20 тыс. активных займов.', 'У конкурентов около 19 тыс. шт.'):
        changed = deepcopy(content)
        changed['sections'][0]['blocks'][0]['claim']['text'] = forged
        assert not attempt(changed)['ok']
    changed = deepcopy(content)
    changed['sections'][0]['blocks'][0]['claim']['fact_refs'] = [ref(source, 'fact_roa')]
    assert not attempt(changed)['ok']
    args = save_args(content)
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert reply['ok'], reply
    view = reply['result']
    assert w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)['result'] == view
    saved = w.runs.version(w.actor, w.project, view['version']['id'])
    assert saved == view and saved['review']['acceptance'] is None
    assert saved['checks'][0]['status'] == 'passed'
    assert any(f['code'] == 'unchecked_source_excerpt' and f['severity'] == 'warning'
               for check in saved['checks'] for f in check['findings'])
    cell = presentation(saved['version'], saved['checks'])['sections'][0]['blocks'][0]['rows'][0][0]
    assert cell['text'] == '«' + facts['fact_market']['raw_text'] + '»'
    assert not cell['marked'] and cell['links'] == [dict(source=source['id'], page=1)]
    assert saved['version']['basis']['facts'][0]['amount'] is None
    assert args['payload']['content'] == content


def test_numeric_grid_refused_for_new_note_but_old_excerpt_survives_edit(workspace, monkeypatch):
    from app import tools
    from app.facts import source_excerpt_text

    w = workspace
    lines = ['Синтетика', '2024 2025', 'Выручка 120 150', 'Чистый долг 12 15']
    source = upload(w, 'financial-grid', lines)
    pin = pin_of(source)
    raw = '\n'.join(lines[1:])
    fact = dict(fact_id='fact_grid', source=pin, extraction_version='fixture.v1',
                metric=lines[1], subject='Синтетика', reporting_basis='Данные компании',
                nature='source_statement', period=UNKNOWN, raw_text=raw, amount=None, limitations=[],
                evidence=[dict(evidence_id='ev_grid', source=pin, quote=raw,
                    locator=dict(pdf_page=1, area='Таблица', bbox_pt=None, coordinate_system='pdf_points_top_left'))])
    with w.engine.begin() as c:
        save_fact(w.service, c, w.actor, w.project, pin, fact)
    run = start_note_run(w, source)
    block = paragraph('block_grid', 'Цитата из PDF', [ref(source, 'fact_grid')])
    block['claim']['origin'] = 'source_excerpt'
    content = dict(title='Справка', audience='Аналитик', sections=[
        dict(section_id='sec_business', title='Описание Компании', blocks=[block])])
    refused = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert not refused['ok'] and 'Финансовую таблицу нельзя' in refused['error']['message']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar() == 0
    paraphrase = deepcopy(content)
    paraphrase['sections'][0]['blocks'][0]['claim'].update(
        origin='source_fact', text='Все ключевые показатели приведены в млрд руб.')
    refused = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(paraphrase))
    assert not refused['ok'] and 'Пересказ финансовой таблицы требует' in refused['error']['message']
    # Seed a historical version through the former excerpt rule, then restore the new rule.
    with monkeypatch.context() as historical:
        historical.setattr(tools, 'source_excerpt_text', lambda f, **kw: source_excerpt_text(f, retained=True))
        saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert saved['ok'], saved
    old = saved['result']['version']
    immutable = deepcopy(old)
    payload = deepcopy(old['payload'])
    payload['content']['title'] = 'Уточнённый заголовок'
    args = dict(expected_revision=1, parent_version_id=old['id'], payload=payload)
    key = ident('key')
    edited = write_version(w.runs, w.actor, w.session, w.project, old['artifact_id'], None, 'saveVersion', key, args)
    repeated = write_version(w.runs, w.actor, w.session, w.project, old['artifact_id'], None, 'saveVersion', key, args)
    assert edited[0] == 201 and repeated[0] == 200
    assert repeated[1] == edited[1]
    assert edited[1]['version']['payload']['content']['sections'] == old['payload']['content']['sections']
    assert w.runs.version(w.actor, w.project, old['id'])['version'] == immutable
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar() == 2
