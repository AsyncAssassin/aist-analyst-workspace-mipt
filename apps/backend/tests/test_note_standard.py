"""Регрессии буквального содержания и консервативного поворота финансовых строк."""
from copy import deepcopy
import json
import zipfile

import pytest
from app import note_renderer as legacy
from app import note_standard as standard

INTERNAL_NOTICES = [
    'Автоматически проверены ссылки, исходное написание числа и подпись периода.',
    'Полная смысловая и визуальная проверка не проведена.',
    'Исторический PDF; точность входов не повышается.',
]

def sample():
    facts, rows = [], []
    for i, year in enumerate((2022, 2023), 1):
        amount = dict(value='475.00' if year == 2022 else '542', unit='money', currency='RUB', scale='1000000')
        ref = dict(source_version_id='sv_sample', fact_id='f'+str(i))
        facts.append(dict(source=dict(source_version_id='sv_sample'), fact_id=ref['fact_id'], metric='Выручка',
            subject='Спектромер', reporting_basis='Данные презентации', nature='reported_actual',
            period=dict(kind='interval', label='1Q'+str(year)[2:], start=f'{year}-01-01', end=f'{year}-03-31'),
            amount=amount, raw_text='Сохраненная исходная строка с символом ₽ внутри текста', evidence=[dict(locator=dict(pdf_page=11), quote='Сохраненная цитата')],
            limitations=['Существенная оговорка о периметре.', *INTERNAL_NOTICES]))
        rows.append(dict(label='Выручка', period_label=facts[-1]['period']['label'], amount=amount, fact_refs=[ref], calculation_ids=[]))
    return dict(payload=dict(content=dict(title='Явный ручной заголовок ₽', sections=[dict(title='Показатели', blocks=[
        dict(kind='paragraph', claim=dict(text='Ручной текст & < > [7].\n- Пункт списка.\n- Второй пункт.', fact_refs=[], calculation_ids=[])),
        dict(kind='table', caption='Сохраненная подпись таблицы', rows=rows)])])), basis=dict(facts=facts, calculations=[]))


def table(version):
    return next(data for kind, data in standard.content_plan(version, dict(release_mode='draft'), []) if kind == 'table')


@pytest.mark.parametrize('label,unit,expected', [
    ('LFL продажи, %', 'percent', 'Изменение сопоставимых продаж (LFL), %'),
    ('LFL продажи', 'money', 'LFL продажи'),
    ('LFL sales adjusted', 'percent', 'LFL sales adjusted'),
    ('LFL traffic', 'percent', 'Изменение сопоставимого трафика (LFL)'),
])
def test_lfl_labels_keep_measure_and_do_not_generalize_to_other_metrics(label, unit, expected):
    row = dict(row_id='row_lfl', label=label, amount=dict(value='-10.4', unit=unit, scale='1', currency='RUB' if unit == 'money' else None))
    original = deepcopy(row)
    standard.translate_row_labels(dict(rows=[row]))
    assert row == dict(original, label=expected)
    assert standard.same_row_metric(row, label)


def test_pivot_preserves_exact_values_units_periods_cell_references_and_input():
    value=sample(); before=deepcopy(value)
    headers, rows=table(value)
    assert headers == ['Показатель', '1Q22 (факт)', '1Q23 (факт)']
    assert rows == [['Выручка', '475,00', '542']]
    assert ('caption', 'млн руб.') in standard.content_plan(value, dict(release_mode='draft'), [])
    assert value == before


def test_shared_unit_occurs_once_without_rewriting_saved_captions_or_labels():
    value = sample()
    block = value['payload']['content']['sections'][0]['blocks'][1]
    plan = standard.content_plan(value, dict(release_mode='accepted'), [])
    text = '\n'.join(standard.expected_paragraphs(plan))
    assert text.count('млн руб.') == 1
    assert 'Единица:' not in text and 'RUB' not in text
    assert ('caption', block['caption']) in plan
    block['caption'] = 'Авторская подпись, млн руб.'
    for row in block['rows']:
        row['label'] = 'Авторская выручка, млн руб.'
    original = deepcopy(value)
    plan = standard.content_plan(value, dict(release_mode='accepted'), [])
    assert ('caption', block['caption']) in plan
    assert table(value)[1][0][0] == 'Авторская выручка, млн руб.'
    assert value == original


@pytest.mark.parametrize('difference', ['company', 'basis', 'currency', 'scale', 'unit', 'source', 'duplicate', 'label', 'amount', 'period', 'unknown', 'calculation', 'multiple'])
def test_ambiguous_tables_keep_every_literal_row(difference):
    value=sample(); fact=value['basis']['facts'][1]; rows=value['payload']['content']['sections'][0]['blocks'][1]['rows']; row=rows[1]
    if difference=='company':fact['subject']='Other company'
    elif difference=='basis':fact['reporting_basis']='Other basis'
    elif difference in ('currency','scale','unit'):
        fact['amount'][difference]={'currency':'USD','scale':'1000','unit':'count'}[difference]
    elif difference=='source':
        fact['source']['source_version_id']='sv_other';row['fact_refs'][0]['source_version_id']='sv_other'
    elif difference=='duplicate':rows.append(deepcopy(rows[0]))
    elif difference=='label':row['label']='Редакторский заголовок'
    elif difference=='amount':row['amount']={**row['amount'], 'value':'543'}
    elif difference=='period':row['period_label']='Редакторский период'
    elif difference=='unknown':fact['period']['kind']='unknown'
    elif difference=='calculation':
        # Direct helper test: a calculation row is never guessed to belong to a period column.
        row['calculation_ids']=['calc'];row['fact_refs']=[]
    elif difference=='multiple':row['fact_refs'].append(rows[0]['fact_refs'][0])
    index={(f['source']['source_version_id'],f['fact_id']):f for f in value['basis']['facts']}
    caption, (headers, data)=standard.financial_table(value['payload']['content']['sections'][0]['blocks'][1],index)
    assert caption is None and headers==['Показатель','Период','Значение']
    assert data==[[r['label'],r['period_label'],legacy.amount(r['amount'])] for r in rows]


def test_forecast_and_actual_have_separate_explicit_columns():
    value=sample();value['basis']['facts'][1]['nature']='forecast'
    assert table(value)[0][-1]=='1Q23 (прогноз)'


def balances_and_flows():
    value = sample()
    block = value['payload']['content']['sections'][0]['blocks'][1]
    for fact, row in zip(deepcopy(value['basis']['facts']), deepcopy(block['rows'])):
        fact['fact_id'] += '_balance'
        fact['metric'] = row['label'] = 'Остаток'
        fact['period'].update(kind='instant', start=fact['period']['end'])
        row['fact_refs'][0]['fact_id'] = fact['fact_id']
        value['basis']['facts'].append(fact)
        block['rows'].append(row)
    return value


def test_balance_and_flow_share_columns_without_losing_periods_or_references():
    value = balances_and_flows()
    original = deepcopy(value)
    headers, rows = table(value)
    assert headers == ['Показатель', '1Q22 (факт)', '1Q23 (факт)']
    assert rows == [['Выручка (за период)', '475,00', '542'],
                    ['Остаток (на конец периода)', '475,00', '542']]
    plan = standard.content_plan(value, dict(release_mode='accepted'), [])
    assert '\n'.join(standard.expected_paragraphs(plan)).count('млн руб.') == 1
    assert value == original


@pytest.mark.parametrize('difference', ['closing_date', 'interval_start', 'kind', 'definition', 'currency', 'source'])
def test_shared_label_does_not_merge_incompatible_balance_and_flow(difference):
    value = balances_and_flows()
    fact = value['basis']['facts'][-1]
    row = value['payload']['content']['sections'][0]['blocks'][1]['rows'][-1]
    if difference == 'closing_date':
        fact['period'].update(start='2023-03-30', end='2023-03-30')
    elif difference == 'interval_start':
        for f in value['basis']['facts'][2:]:
            f['period'].update(kind='interval', start=f['period']['end'][:4] + '-02-01')
    elif difference == 'kind':
        fact['period'].update(kind='interval', start='2023-01-01')
    elif difference == 'definition':
        fact['metric'] = 'Средний остаток'
    elif difference == 'currency':
        fact['amount']['currency'] = row['amount']['currency'] = 'USD'
    else:
        fact['source']['source_version_id'] = row['fact_refs'][0]['source_version_id'] = 'sv_other'
    if difference in ('closing_date', 'interval_start'):
        headers, rows = table(value)
        assert len(headers) == (4 if difference == 'closing_date' else 5)
        assert all(' - ' in h for h in headers[1:] if h.startswith('1Q23'))
        assert sum(cell == '542' for r in rows for cell in r) == 2
    else:
        assert table(value)[0] == ['Показатель', 'Период', 'Значение']
        assert len(table(value)[1]) == 4


def test_one_period_retains_shared_unit_and_every_value():
    value = balances_and_flows()
    block = value['payload']['content']['sections'][0]['blocks'][1]
    block['rows'] = block['rows'][::2]
    headers, rows = table(value)
    assert headers == ['Показатель', '1Q22 (факт)']
    assert len(rows) == 2 and all(row[1] == '475,00' for row in rows)
    assert ('caption', 'млн руб.') in standard.content_plan(value, dict(release_mode='accepted'), [])


def test_saved_labels_do_not_merge_different_metrics():
    value = sample()
    rows = value['payload']['content']['sections'][0]['blocks'][1]['rows']
    for row in rows:
        row['label'] = 'Выручка компании'
    assert table(value)[1][0][0] == 'Выручка компании'
    value['basis']['facts'][1]['metric'] = 'Другой показатель'
    assert table(value)[0] == ['Показатель', 'Период', 'Значение']


@pytest.mark.parametrize('theme',['standard','compact'])
def test_real_standard_release_keeps_basis_internal_and_preserves_saved_content(tmp_path,theme):
    value=sample()
    value['basis']['calculations'] = [dict(calculation_id='calc1', formula_display='(current - previous) / previous * 100',
        inputs=[dict(name='current', fact=dict(source_version_id='sv_sample', fact_id='f2'))],
        limitations=['Существенная оговорка расчета.'])]
    value['payload']['content']['sections'][0]['blocks'].append(dict(kind='paragraph',claim=dict(
        text='Изменение выручки: 14,11%.',fact_refs=[],calculation_ids=['calc1'])))
    value['payload']['content']['sections'][0]['blocks'].append(dict(kind='paragraph',claim=dict(
        text='Существенная оговорка о периметре.',fact_refs=[],calculation_ids=[])))
    original=deepcopy(value);settings=dict(theme=theme,release_mode='draft')
    report=standard.render(value,settings,[],tmp_path)
    assert report['pages']==1 and report['links']==0 and value==original
    assert all('LiberationSans' in font or 'DejaVuSans' in font for font in report['fonts'])
    assert any('DejaVuSans' in font for font in report['fonts'])
    plan=standard.content_plan(value,settings,[])
    text='\n'.join(standard.expected_paragraphs(plan))
    assert text.count('Ручной текст & < > [7].')==1
    assert 'Изменение выручки: 14,11%.' in text
    assert 'Существенная оговорка расчета.' not in text
    assert text.count('Существенная оговорка о периметре.') == 1
    assert 'Источники и расчеты' not in text and 'current' not in text
    assert '[1]' not in text and '[2]' not in text
    assert not any(notice in text for notice in INTERNAL_NOTICES)
    assert 'Сохраненная цитата' not in text and 'с. 11' not in text
    with zipfile.ZipFile(tmp_path/'note.docx') as archive:
        xml=archive.read('word/document.xml').decode()
        assert 'tblHeader' in xml and 'cantSplit' in xml and '203864' in xml
        assert 'w:hyperlink' not in xml and 'w:bookmarkStart' not in xml
    bad=deepcopy(plan);bad[0]=('title','Подмененный заголовок')
    with pytest.raises(ValueError,match='DOCX content mismatch'):
        legacy.verify(tmp_path/'note.docx',tmp_path/'note.pdf',[],expected_paragraphs=standard.expected_paragraphs(bad),font_name=('LiberationSans','DejaVuSans'))


def test_release_does_not_append_basis_or_filter_saved_editorial_text():
    value = sample()
    saved = 'Исторический PDF. Формулировка сохранена вручную. Существенная оговорка расчета.'
    value['payload']['content']['sections'][0]['blocks'][0]['claim']['text'] = saved
    value['basis']['comparisons'] = [dict(message='Служебная сверка масштаба.')]
    original = deepcopy(value)
    text = '\n'.join(standard.expected_paragraphs(standard.content_plan(value, dict(release_mode='accepted'), [])))
    assert text.count(saved) == 1
    assert 'Существенная оговорка о периметре.' not in text
    assert 'Служебная сверка масштаба.' not in text
    assert 'ЧЕРНОВИК' not in text
    assert value == original
    draft = standard.content_plan(value, dict(release_mode='draft'), [dict(status='failed')])
    assert ('p', 'ЧЕРНОВИК') in draft
    assert not any('ошибки проверок' in text for kind, text in draft if kind != 'table')


def test_registration_retains_legacy_versions():
    from app.contracts import PACKAGE, validate_definition
    registry=legacy.registrations(json.loads((PACKAGE/'registry.json').read_text()))
    validate_definition('Registry', registry)
    for template, renderers in [('aist-note.v1',legacy.RENDERERS),('aist-note.v2',standard.RENDERERS)]:
        assert {r['format']:r['version'] for r in registry['renderers'] if r['templates']==[template]}==renderers
    assert legacy.renderer_versions('unknown')=={}
