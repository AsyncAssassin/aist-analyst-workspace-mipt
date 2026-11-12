"""Точечный допуск словоформ, направления и годов не подтверждает соседние числа."""
from copy import deepcopy

import pytest

from app.domain import growth
from app.tools import paragraph_remarks
from test_domain import calculation, fact
from test_note_path05_r3 import remarks as quantity_remarks


def qualitative(quotes, period=None):
    return dict(fact_id='fact_series', amount=None, nature='source_statement',
                period=period or dict(kind='unknown', label='Не указано', start=None, end=None),
                evidence=[dict(quote=quote, locator=dict(pdf_page=1)) for quote in quotes])


def remarks(text, facts=(), calculations=(), inputs=None):
    before = deepcopy((facts, calculations, inputs))
    findings = []
    result = paragraph_remarks(dict(text=text), facts, calculations, inputs or {}, findings,
                               dict(block_id='block_checked'))
    assert (facts, calculations, inputs) == before
    return {code for code, _ in result} | {item['code'] for item in findings}


def change(current='80'):
    args, records = calculation('100', current)
    result = growth('calc_change', args, records)
    inputs = {(item['data']['source']['source_version_id'], item['data']['fact_id']): item['data']
              for item in records.values()}
    return result, inputs


def series(quotes=('Объем продаж: 2017 2018 2019',)):
    return qualitative(quotes, dict(kind='interval', label='с 2017 по 2019', start='2017-01-01', end='2019-12-31'))


def test_year_word_forms_keep_value_scale_and_bound():
    assert not quantity_remarks('Клиент в возрасте 33 лет.', ['Возраст клиента 33 года'])
    assert not quantity_remarks('Возраст от 42 лет.', ['Возраст от 42 года'])
    for text in ['Возраст от 43 лет.', 'Возраст от 42 месяцев.', 'Возраст 42 лет.', 'Возраст от 42 тыс. лет.']:
        assert 'quantity_mismatch' in quantity_remarks(text, ['Возраст от 42 года'])
    assert 'quantity_mismatch' in quantity_remarks('Возраст от 42 лет.', ['Возраст от 42', 'года'])


@pytest.mark.parametrize('text,current', [
    ('Выручка снизилась на 20%.', '80'),
    ('Доходы снизились на 20%.', '80'),
    ('Выручка выросла на 25%.', '125'),
    ('Изменение выручки -20%.', '80'),
])
def test_direction_and_ordinary_signed_value(text, current):
    result, inputs = change(current)
    assert not remarks(text, calculations=[result], inputs=inputs)


@pytest.mark.parametrize('text', [
    'Выручка выросла на 20%.',
    'Выручка снизилась на 21%.',
    'Выручка не снизилась на 20%.',
    'Выручка снизилась на +20%.',
    'Выручка снизилась на 20 процентных пунктов.',
    'Выручка снизилась на 20% п.п.',
    'Выручка снизилась на 20%. Рентабельность 20%.',
    'Выручка снизилась на 20%, рентабельность 20%.',
])
def test_direction_cannot_hide_another_percentage(text):
    result, inputs = change()
    assert remarks(text, calculations=[result], inputs=inputs)


def test_direction_needs_its_calculation_and_cannot_choose_an_opposite_result():
    result, inputs = change()
    assert {'unsupported_numbers', 'unit_mismatch'} <= remarks('Выручка снизилась на 20%.')
    negative = fact('-20')
    negative['amount'] = dict(value='-20', unit='percent', scale='1', currency=None)
    assert {'unsupported_numbers', 'unit_mismatch'} <= remarks('Выручка снизилась на 20%.', [negative])
    other = deepcopy(result)
    other['formula_id'] = 'other.v1'
    assert 'unit_mismatch' in remarks('Выручка снизилась на 20%.', calculations=[other], inputs=inputs)
    opposite, _ = change('120')
    assert 'unit_mismatch' in remarks('Выручка снизилась на 20%.', calculations=[opposite], inputs=inputs)
    assert 'unit_mismatch' in remarks('Выручка снизилась на 20%.', calculations=[result, opposite], inputs=inputs)
    positive, _ = change('125')
    assert not remarks('Выручка снизилась на 20%. Прибыль выросла на 25%.', calculations=[result, positive], inputs=inputs)
    assert 'unit_mismatch' in remarks('Выручка выросла на 20%. Прибыль снизилась на 25%.', calculations=[result, positive], inputs=inputs)


@pytest.mark.parametrize('text', [
    'Приведен ряд за 2017, 2018 и 2019 годы.',
    'Ряд за 2017-2019 годы.',
    'Объем увеличивался в каждом году с 2017 по 2019 год.',
])
def test_qualitative_year_list_or_range_has_its_own_quotes(text):
    assert not remarks(text, [series()])
    assert not remarks(text, [series(['Ряд за 2017-2019 годы'])])


@pytest.mark.parametrize('text', [
    'Приведен ряд за 2017, 2018 и 2020 годы.',
    'Ряд за 2017-2020 годы.',
    'Ряд за 2017, 2018 и 2019 годы. Компания основана в 2019 году.',
    'Ряд за 2017, 2018 и 2019 годы, компания основана в 2019 году.',
    'Ряд за 2017, 2018 и 2019 годы. Выручка за 2019 год 80 млн руб.',
    'Ряд за 2017, 2018 и 2019%.',
    'Ряд за 2017 и 2019 долларов.',
    'Ряд за 2017 и 2019 миллионов рублей.',
    'Ряд за 2017 и 2019 EUR.',
])
def test_series_years_do_not_confirm_other_periods_or_units(text):
    assert remarks(text, [series()])


def test_endpoints_do_not_imply_a_continuous_series_or_extend_numeric_periods():
    assert 'period_mismatch' in remarks('Ряд за 2017 по 2019 год.', [series(['Первое значение 2017', 'Последнее значение 2019'])])
    assert not remarks('Ряд за 2017 и 2019 годы.', [series(['2017 2019'])])
    numeric = fact('80', 2019)
    assert 'period_mismatch' in remarks('Ряд за 2017, 2018 и 2019 годы. Выручка за 2019 год 80 млн руб.', [numeric, series()])
    assert 'unit_mismatch' in remarks('Ряд за 2017, 2018 и 2019 годы, 80 млн руб.', [series(['2017 2018 2019', '80 млн руб.'])])


def test_missing_data_year_is_only_a_local_exception():
    basis = [fact('80', 2023)]
    for text in ['Выводы о промежуточных результатах 2024 года нельзя подтвердить по доступным основаниям.',
                 'Данные за 2025 год нельзя подтвердить по доступным основаниям.']:
        assert not remarks(text, basis)
    for text in ['Результаты 2025 года можно подтвердить по доступным основаниям.',
                 'Результаты 2025 года нельзя не подтвердить по доступным основаниям.',
                 'Не результаты 2025 года нельзя подтвердить по доступным основаниям.',
                 'Данные за 2025 год нельзя подтвердить по доступным основаниям. Выручка за 2025 год 80 млн руб.',
                 'Данные за 2025 год нельзя подтвердить по доступным основаниям, выручка за 2025 год 80 млн руб.']:
        assert {'unsupported_numbers', 'period_mismatch'} <= remarks(text, basis)
    assert 'unit_mismatch' in remarks('Данные за 2025 год нельзя подтвердить по доступным основаниям, прибыль 999 млн руб.', basis)


def test_changed_v3_instruction_is_delivered_by_existing_runtime_path(tmp_path):
    from types import SimpleNamespace
    from app.live_profile import bundle_for
    from app.openclaw_runtime import prepare
    instruction = bundle_for('skill_analytical-note-v3', 'analytical_note').read_text()
    prepare(SimpleNamespace(provider_port=30314, gateway_port=30313, transport_seconds=600), tmp_path,
            'synthetic-local-token', 'skill_analytical-note-v3', 'analytical_note')
    delivered = list((tmp_path / 'workspace/skills').glob('*/SKILL.md'))
    assert len(delivered) == 1 and delivered[0].read_text() == instruction
    assert 'вступление к таблице' in instruction and 'собственные ссылки' in instruction
    assert 'снизились на' in instruction and 'growth_percent.v1' in instruction
