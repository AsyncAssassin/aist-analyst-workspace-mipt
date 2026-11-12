"""Матрица ASO: порядок, пропуски, происхождение и вычисленные ячейки."""
from copy import deepcopy

import pytest

from app import note_standard as standard
from test_note_standard import sample


def fixture():
    version = sample()
    block = version['payload']['content']['sections'][0]['blocks'][1]
    block['block_id'] = 'block_financial'
    for index, row in enumerate(block['rows']):
        row['row_id'] = f'row_{index}'
    facts = {(f['source']['source_version_id'], f['fact_id']): f for f in version['basis']['facts']}
    return block, facts


def add(block, facts, metric, value, year=2023, unit='percent', nature='reported_actual'):
    fact = deepcopy(next(iter(facts.values())))
    fact['fact_id'] = 'fact_' + str(len(facts))
    fact['metric'], fact['nature'] = metric, nature
    fact['amount'] = dict(value=value, unit=unit, currency='RUB' if unit == 'money' else None,
                          scale='1000000' if unit == 'money' else '1')
    fact['period'] = dict(kind='interval', label=f'1Q{str(year)[2:]}',
                          start=f'{year}-01-01', end=f'{year}-03-31')
    ref = dict(source_version_id='sv_sample', fact_id=fact['fact_id'])
    row = dict(row_id='row_' + fact['fact_id'], label=metric, period_label=fact['period']['label'],
               amount=deepcopy(fact['amount']), fact_refs=[ref], calculation_ids=[])
    block['rows'].append(row)
    facts['sv_sample', fact['fact_id']] = fact
    return row, fact


def test_sparse_mixed_units_sorted_with_exact_cell_origins():
    block, facts = fixture()
    margin, _ = add(block, facts, 'Маржа EBITDA', '-2.5')
    block['rows'].reverse()
    before = deepcopy((block, facts))
    findings = []
    table = standard.financial_projection(block, facts, findings=findings)
    assert table['headers'] == ['Показатель', '1Q22 (факт)', '1Q23 (факт)']
    assert table['unit'] == '' and findings == []
    assert table['values'][0] == ['Маржа EBITDA, %', '-', '-2,5']
    assert table['values'][1] == ['Выручка, млн руб.', '475,00', '542']
    assert table['origins'][0] == [None, None, margin]
    assert table['origins'][1][1]['row_id'] == 'row_0'
    assert (block, facts) == before


def test_actual_and_forecast_same_year_are_separate():
    block, facts = fixture()
    row, _ = add(block, facts, 'Выручка', '800', unit='money', nature='forecast')
    table = standard.financial_projection(block, facts)
    column = table['headers'].index('1Q23 (прогноз)')
    assert table['origins'][0][column] == row
    assert '1Q23 (факт)' in table['headers']


def test_same_bounds_alias_does_not_create_a_missing_cell():
    block, facts = fixture()
    row, fact = add(block, facts, 'Прибыль', '13', unit='money')
    row['period_label'] = fact['period']['label'] = 'Первый квартал 2023 года'
    table = standard.financial_projection(block, facts)
    assert len(table['headers']) == 3
    assert table['origins'][1][-1] == row


def test_conflicting_values_preserve_both_rows_and_sources():
    block, facts = fixture()
    row, _ = add(block, facts, 'Выручка', '800', unit='money')
    findings = []
    table = standard.financial_projection(block, facts, findings=findings)
    assert not table['pivoted'] and len(table['values']) == 3
    assert [c[-1]['row_id'] for c in table['origins']] == ['row_0', 'row_1', row['row_id']]
    assert [f['code'] for f in findings] == ['conflicting_financial_cell']


def test_identical_repeated_rows_are_preserved_without_a_conflict():
    block, facts = fixture()
    row = deepcopy(block['rows'][-1])
    row['row_id'] = 'row_repeated'
    block['rows'].append(row)
    findings = []
    table = standard.financial_projection(block, facts, findings=findings)
    assert not table['pivoted'] and len(table['values']) == 3
    assert table['origins'][-1][-1] == row
    assert findings == []
    conflicting, _ = add(block, facts, 'Выручка', '800', unit='money')
    table = standard.financial_projection(block, facts, findings=findings)
    assert len(table['values']) == 4 and table['origins'][-1][-1] == conflicting
    assert [f['code'] for f in findings] == ['conflicting_financial_cell']


def test_calculated_row_has_its_own_period_and_input_references():
    block, facts = fixture()
    amount = dict(value='14.11', unit='percent', scale='1', currency=None)
    calculation = dict(calculation_id='calc_growth', formula_id='growth_percent.v1', rounded=amount,
        inputs=[dict(fact=deepcopy(row['fact_refs'][0])) for row in block['rows']])
    row = dict(row_id='row_growth', label='Рост выручки', amount=amount,
        period_label='1Q23 к 1Q22', fact_refs=[], calculation_ids=['calc_growth'])
    block['rows'].append(row)
    table = standard.financial_projection(block, facts, calculations={'calc_growth': calculation})
    assert table['pivoted']
    column = table['headers'].index('1Q23 к 1Q22 (факт)')
    assert table['values'][1][column] == '14,11'
    assert table['origins'][1][column] == row
    row['label'] = 'Рост чистой прибыли'
    assert not standard.financial_projection(block, facts, calculations={'calc_growth': calculation})['pivoted']


def test_calculation_cannot_be_saved_under_another_metric(monkeypatch):
    from types import SimpleNamespace
    from app import tools
    block, facts = fixture()
    amount = dict(value='14.11', unit='percent', scale='1', currency=None)
    calculation = dict(calculation_id='calc_growth', formula_id='growth_percent.v1', rounded=amount,
        inputs=[dict(fact=deepcopy(row['fact_refs'][0])) for row in block['rows']])
    monkeypatch.setattr(tools, 'resolve_fact', lambda service, c, r, ref:
                        dict(data=facts[ref['source_version_id'], ref['fact_id']]))
    row = dict(label='Рост выручки, %', amount=amount, period_label='1Q23 к 1Q22')
    runs = SimpleNamespace(service=None)
    assert tools.row_remarks(runs, None, None, row, [], [calculation]) == []
    row['label'] = 'Рост чистой прибыли, %'
    assert tools.row_remarks(runs, None, None, row, [], [calculation])[0][0] == 'row_label_mismatch'


@pytest.mark.parametrize('change', ['sign', 'scale', 'year', 'value', 'subject', 'basis', 'metric', 'source'])
def test_mismatched_rows_never_get_an_invented_matrix_binding(change):
    block, facts = fixture()
    row = block['rows'][1]
    fact = facts['sv_sample', 'f2']
    row['amount'] = deepcopy(row['amount'])
    if change == 'sign': row['amount']['value'] = '-542'
    elif change == 'scale': row['amount']['scale'] = '1000'
    elif change == 'year': row['period_label'] = '1Q24'
    elif change == 'value': row['amount']['value'] = '543'
    elif change == 'subject': fact['subject'] = 'Другое юридическое лицо'
    elif change == 'basis': fact['reporting_basis'] = 'РСБУ'
    elif change == 'metric': row['label'] = 'Чистая прибыль'
    else: row['fact_refs'][0]['source_version_id'] = 'sv_other'
    table = standard.financial_projection(block, facts)
    assert not table['pivoted']
    assert table['origins'][-1][-1] == row


def test_eighteen_months_are_not_combined_with_annual_period():
    block, facts = fixture()
    row, fact = add(block, facts, 'Прибыль', '13', unit='money')
    row['period_label'] = fact['period']['label'] = '18 месяцев по 31.03.2023'
    fact['period']['start'] = '2021-10-01'
    table = standard.financial_projection(block, facts)
    assert len(table['headers']) == 4
    assert table['origins'][1][table['headers'].index(row['period_label'] + ' (факт)')] == row


def test_wide_tables_split_without_dropping_a_cell_or_its_id():
    block, facts = fixture()
    for year in range(2024, 2030):
        add(block, facts, 'Выручка', str(year), year=year, unit='money')
    table = standard.financial_projection(block, facts)
    pages = standard.financial_pages(table)
    assert len(pages) == 2 and all(len(p['headers']) <= 5 for p in pages)
    assert [cell['row_id'] for p in pages for cell in p['origins'][0][1:]] == [r['row_id'] for r in block['rows']]


def test_wide_exports_keep_every_period_value_without_review_highlight(tmp_path):
    from docx import Document
    from pypdf import PdfReader
    from app import aso_renderer
    block, facts = fixture()
    for year in range(2024, 2030):
        add(block, facts, 'Выручка', str(year), year=year, unit='money')
    version = dict(id='av_wide', content_sha256='0' * 64, revision=1, created_at='2026-11-07T00:00:00Z',
        basis=dict(facts=list(facts.values()), calculations=[]),
        payload=dict(content=dict(title='Проверка широкой таблицы', sections=[dict(
            title='Финансовые показатели', blocks=[block])])))
    finding = dict(code='row_basis_mismatch', severity='error', message='Сверить значение',
                   block_id=block['block_id'], row_id=block['rows'][-1]['row_id'])
    before = deepcopy(version)
    aso_renderer.render(version, dict(theme='standard', release_mode='draft'),
                        [dict(findings=[finding])], tmp_path)
    document = Document(tmp_path / 'note.docx')
    # После сведения сеток для Pages проверяем реальные ячейки, не число таблиц XML.
    cells = [c for table in document.tables for row in table.rows for c in row.cells]
    for row in block['rows']:
        assert any(c.text == standard.number(row['amount']) for c in cells)
        assert any(row['period_label'] + ' (факт)' in c.text for c in cells)
    marked_value = standard.number(block['rows'][-1]['amount'])
    # Замечание остается на экране, файл содержит только значение.
    assert any(c.text == marked_value for c in cells) and not document.element.xpath('.//w:highlight')
    text = '\n'.join(page.extract_text() for page in PdfReader(tmp_path / 'note.pdf').pages)
    assert all(row['period_label'] in text and standard.number(row['amount']) in text for row in block['rows'])
    assert version == before


def test_visual_money_percent_and_ratio_keep_unchecked_origin():
    block, facts = fixture()
    add(block, facts, 'Маржа EBITDA', '-2.5')
    add(block, facts, 'Чистый долг/EBITDA', '-0.2', unit='ratio')
    observations = {}
    for row in block['rows']:
        ref = row['fact_refs'].pop()
        fact = facts[ref['source_version_id'], ref['fact_id']]
        observation_id = 'vo_' + row['row_id']
        row['visual_observation_ids'] = [observation_id]
        observations[observation_id] = dict(observation_id=observation_id,
            numeric={k: deepcopy(fact[k]) for k in ('metric', 'subject', 'amount', 'period', 'nature')},
            source=deepcopy(fact['source']), page=2, verification_status='unchecked')
    before = deepcopy((block, observations))
    table = standard.financial_projection(block, {}, observations=observations)
    assert table['pivoted'] and table['values'][-1] == ['Чистый долг/EBITDA, коэф.', '-', '-0,2']
    assert table['origins'][-1][-1]['visual_observation_ids'] == ['vo_row_fact_3']
    assert (block, observations) == before
    # Другая страница не подтверждает совместимость двух изображений.
    observations['vo_row_fact_3']['page'] = 3
    assert not standard.financial_projection(block, {}, observations=observations)['pivoted']


def test_changed_input_marks_dependent_calculation_without_recalculating():
    from app.versions import edited_calculation_findings
    block, facts = fixture()
    calculation = dict(calculation_id='calc_growth', inputs=[dict(fact=deepcopy(r['fact_refs'][0])) for r in block['rows']])
    computed = dict(row_id='row_growth', amount=dict(value='14.11', unit='percent', scale='1', currency=None),
                    fact_refs=[], calculation_ids=['calc_growth'])
    block['rows'].append(computed)
    prose = dict(kind='paragraph', block_id='block_comment', claim=dict(calculation_ids=['calc_growth']))
    payload = dict(content=dict(sections=[dict(blocks=[block, prose])]))
    basis = dict(facts=list(facts.values()), calculations=[calculation])
    before = deepcopy(basis)
    assert edited_calculation_findings(payload, basis) == []
    block['rows'][1]['amount'] = dict(block['rows'][1]['amount'], value='800')
    findings = edited_calculation_findings(payload, basis)
    assert [(f['block_id'], f.get('row_id')) for f in findings] == [
        ('block_financial', 'row_growth'), ('block_comment', None)]
    assert all(f['code'] == 'calculation_input_edited' for f in findings)
    assert basis == before and computed['amount']['value'] == '14.11'
