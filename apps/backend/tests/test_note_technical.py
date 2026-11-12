"""v29: строгие строки и происхождение, честный предел проверки свободной прозы."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.errors import ApiError
from app.note_standard import same_row_metric, translated_metric
from app.tools import resolve_basis
from app.versions import edited_calculation_findings
from test_perf02_r4 import FACTS, MONEY, RUN, lookup, note, paragraph, ref, row


def inspect(payload, parent=None, human=False):
    findings, errors = [], []
    with patch('app.tools.resolve_fact', side_effect=lookup), patch('app.tools.row', return_value=None):
        basis = resolve_basis(SimpleNamespace(service=None), None, RUN, payload, parent=parent,
                              human=human, findings=findings, errors=errors)
    return basis, findings, errors


@pytest.mark.parametrize('alias', ['Net Debt/EBITDA', 'Net Debt / EBITDA', 'Net Debt/ EBITDA',
                                 'Net Debt /EBITDA', 'Net Debt\t/\tEBITDA', 'Чистый долг / EBITDA'])
def test_exact_ratio_alias_only_ignores_separator_whitespace(alias):
    item = dict(label='Чистый долг/EBITDA', amount=dict(value='2.8', unit='ratio', scale='1', currency=None))
    assert same_row_metric(item, alias)
    for other in ('Gross Debt / EBITDA', 'Net Debt / Adjusted EBITDA', 'Чистый долг / Выручка'):
        assert not same_row_metric(item, other)
        assert translated_metric(other, 'ratio') == other
    assert translated_metric(alias, 'money') == alias


@pytest.mark.parametrize('suffix', [', x', ' (x)', ', ×', ', х', ', коэф.', ', раз', ' (раз)', ' раза', ', разов'])
def test_ratio_unit_suffix_keeps_metric_and_exact_visual_value(suffix):
    from app.visual_observations import row_agrees
    amount = dict(value='2.8', unit='ratio', scale='1', currency=None)
    item = dict(label='Чистый долг/EBITDA' + suffix, amount=amount, period_label='2023')
    observation = dict(numeric=dict(metric='Net Debt / EBITDA', amount=deepcopy(amount),
                                    period=dict(label='2023')))
    assert row_agrees(item, observation)
    for metric in ('Gross Debt / EBITDA', 'Net Debt / Adjusted EBITDA', 'Net Debt / Revenue'):
        assert not row_agrees(item, dict(numeric=dict(observation['numeric'], metric=metric)))
    for change in (dict(value='2.9'), dict(scale='1000'), dict(unit='money', currency='RUB')):
        assert not row_agrees(dict(item, amount=dict(amount, **change)), observation)
    assert not row_agrees(dict(item, period_label='2024'), observation)
    assert not same_row_metric(dict(item, amount=dict(amount, unit='percent')), 'Net Debt / EBITDA')


def test_ratio_unit_suffix_does_not_erase_the_metric_window():
    from app.visual_observations import row_agrees
    amount = dict(value='0.6', unit='ratio', scale='1', currency=None)
    item = dict(label='Чистый долг / EBITDA за 12М, раз', amount=amount, period_label='31 дек. 25')
    observation = dict(numeric=dict(metric='Чистый долг / EBITDA за 12М', amount=deepcopy(amount),
                                    period=dict(label='31 дек. 25')))
    assert row_agrees(item, observation)
    for metric in ('Чистый долг / EBITDA за 9М', 'Чистый долг / скорректированная EBITDA за 12М'):
        assert not row_agrees(item, dict(numeric=dict(observation['numeric'], metric=metric)))


def test_wrong_number_in_linked_prose_is_unchecked_but_short_answer_is_strict():
    block = paragraph('block_prose', 'ROA за 1Q23 составил 99,9%.', 'fact_roa')
    payload = note([dict(section_id='section_one', title='Приложение 1', blocks=[block])])
    basis, findings, errors = inspect(payload)
    assert basis['facts'] == [FACTS['fact_roa']] and findings == errors == []
    answer = dict(result_type='evidence_answer', schema_version='1.0.0',
                  content=dict(claims=[block['claim']], missing_information=[]))
    _, findings, errors = inspect(answer)
    assert errors == [] and {'unsupported_numbers', 'unit_mismatch'} <= {f['code'] for f in findings}
    assert all(f['severity'] == 'error' for f in findings)


@pytest.mark.parametrize('change', ['value', 'sign', 'scale', 'currency', 'unit', 'period', 'metric'])
def test_mixed_fact_visual_row_cannot_hide_an_exact_mismatch(change):
    fact = FACTS['fact_capital_1q23']
    item = row('row_mixed', fact['metric'], fact['period']['label'], MONEY, fact['fact_id'])
    item['visual_observation_ids'] = ['visual_one']
    observation = dict(observation_id='visual_one', numeric=dict(metric=fact['metric'], amount=deepcopy(MONEY), period=fact['period']))
    if change == 'value': item['amount']['value'] = '2480'
    elif change == 'sign': item['amount']['value'] = '-3658'
    elif change == 'scale': item['amount']['scale'] = '1000000000'
    elif change == 'currency': item['amount']['currency'] = 'USD'
    elif change == 'unit': item['amount'] = dict(value='3658', unit='percent', scale='1', currency=None)
    elif change == 'period': item['period_label'] = '1 кв. 2022г.'
    else: item['label'] = 'Привлеченные средства'
    payload = note([dict(section_id='section_one', title='Финансы', blocks=[
        dict(block_id='block_table', kind='table', caption='Финансы', rows=[item])])])
    with patch('app.visual_observations.available', return_value={'visual_one': observation}), \
            patch('app.visual_observations.authorize', return_value=observation):
        _, findings, errors = inspect(payload)
    assert errors == []
    assert any(f['code'] == 'unchecked_visual_observation' and f['severity'] == 'warning' for f in findings)
    assert any(f['code'] == 'visual_row_mismatch' and f['severity'] == 'error' and f['row_id'] == 'row_mixed' for f in findings)
    assert any(f['code'] in ('row_basis_mismatch', 'row_label_mismatch') and f['severity'] == 'error' for f in findings)


def test_editorial_without_basis_and_forged_origin_do_not_gain_provenance():
    payload = note([dict(section_id='section_one', title='Описание компании', blocks=[
        dict(block_id='block_editor', kind='paragraph', claim=dict(text='Выручка 999 млн руб.',
            origin='human_editorial', fact_refs=[], calculation_ids=[]))])])
    _, findings, errors = inspect(payload, human=True)
    assert errors == [] and findings[0]['code'] == 'unsupported_editorial_number'
    _, _, errors = inspect(payload)
    assert any('Генерация не создает' in message for _, message in errors)


def test_changed_calculation_input_remains_an_error():
    item = row('row_input', 'Собственный капитал', '2022', dict(MONEY, value='999'), 'fact_capital_2022')
    result = row('row_result', 'Рост', '2023 к 2022', MONEY)
    result['calculation_ids'] = ['calc_one']
    payload = note([dict(section_id='section_one', title='Финансы', blocks=[
        dict(block_id='block_table', kind='table', caption='Финансы', rows=[item, result])])])
    findings = edited_calculation_findings(payload, dict(facts=[FACTS['fact_capital_2022']],
        calculations=[dict(calculation_id='calc_one', inputs=[dict(fact=ref('fact_capital_2022'))])]))
    assert [(f['code'], f['row_id'], f['severity']) for f in findings] == [('calculation_input_edited', 'row_result', 'error')]
