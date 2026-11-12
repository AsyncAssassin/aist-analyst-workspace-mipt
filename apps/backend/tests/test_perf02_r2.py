"""Quarterly flow labels keep their calendar quarter; a forecast heading qualifies the series under it."""
import pytest

from app.errors import ApiError
from app.extraction import candidate
from app.source_cards import numeric_cards
from test_d10 import PIN
from test_perf02_r1 import HEADER, PAGE_DATE, YEAR_2023, qualitative

QUARTERS = {1: ('01-01', '03-31'), 2: ('04-01', '06-30'), 3: ('07-01', '09-30'), 4: ('10-01', '12-31')}


def page(*lines):
    text = '\n'.join([HEADER, *lines])
    return {1: text}, {1: text}


def claim(period, line_end, metric='Выручка'):
    return dict(metric=metric, subject='Synthetic', reporting_basis=HEADER, nature='reported_actual', period=period,
                amount=dict(value='100', unit='money', currency='RUB'), value_text='100',
                evidence=[dict(page=1, line_start=1, line_end=line_end, area='Карточка')],
                basis=dict(page=1, metric_label=metric, unit_label='млн ₽', reporting_label=HEADER))


@pytest.mark.parametrize('label', ['за 1 кв. 2023 г.', 'за 2 кв. 2023 г.', 'за 3 квартал 2023 года', 'за 4 кв. 2023'])
def test_quarterly_flow_label_is_the_calendar_quarter(label):
    first, last = QUARTERS[int(label.split()[1])]
    expected = dict(kind='interval', label=label, start='2023-' + first, end='2023-' + last)
    pages, layouts = page(label, 'Выручка', '100 млн ₽')
    cards = numeric_cards(pages, layouts, qualitative(4))
    assert [(c['metric'], c['amount']['value'], c['period']) for c in cards] == [('Выручка', '100', expected)]
    fact, basis = candidate(PIN, pages, cards[0], layouts)
    assert fact['nature'] == 'reported_actual' and fact['period'] == expected and fact['amount']['scale'] == '1000000'
    assert 'Выручка' in basis['definition']
    assert candidate(PIN, pages, claim(expected, 4), layouts)[0]['period'] == expected
    for wrong in (dict(expected, start='2023-01-01', end='2023-12-31'), dict(expected, kind='instant', start=expected['end']), PAGE_DATE):
        with pytest.raises(ApiError):
            candidate(PIN, pages, claim(wrong, 4), layouts)


def test_annual_flow_and_quarter_balance_keep_their_kinds():
    pages, layouts = page('за 2023 год', 'Выручка', '100 млн ₽')
    assert [c['period'] for c in numeric_cards(pages, layouts, qualitative(4))] == [YEAR_2023]
    pages, layouts = page('Собственный капитал', '100 млн ₽', '1 кв. 2023 г.')
    cards = numeric_cards(pages, layouts, qualitative(4))
    assert [(c['period']['kind'], c['period']['label'], c['period']['start'], c['period']['end']) for c in cards] == [
        ('instant', '1 кв. 2023 г.', '2023-03-31', '2023-03-31')]
    assert candidate(PIN, pages, cards[0], layouts)[0]['period'] == cards[0]['period']
    with pytest.raises(ApiError):
        candidate(PIN, pages, claim(dict(kind='interval', label='1 кв. 2023 г.', start='2023-01-01', end='2023-03-31'), 4, 'Собственный капитал'), layouts)
    # The same quarter label alone does not make a flow metric's amount a balance.
    pages, layouts = page('Выручка', '100 млн ₽', '1 кв. 2023 г.')
    assert numeric_cards(pages, layouts, qualitative(4)) == []


FORECAST_2025 = dict(kind='instant', label='2025 г.', start='2025-12-31', end='2025-12-31')


@pytest.mark.parametrize('lines', [
    ['Прогноз на 2025 год', 'Собственный капитал', '100 млн ₽', '2025 г.'],
    ['Собственный капитал', 'Прогноз на 2025 год', '100 млн ₽', '2025 г.'],
    ['Прогноз на 2025 год', '', 'Собственный капитал', '100 млн ₽', '2025 г.'],
    ['План 2025', 'Собственный капитал', '100 млн ₽ +10,0%', '2025 г.', '90 млн ₽', '2024 г.'],
])
def test_forecast_heading_gives_no_numeric_fact_for_its_series(lines):
    pages, layouts = page(*lines)
    item = qualitative(len(lines) + 1)
    assert numeric_cards(pages, layouts, item) == []
    fact, basis = candidate(PIN, pages, item, layouts)
    assert fact['amount'] is None and fact['nature'] == 'source_statement' and basis == {}
    assert '100' in fact['evidence'][0]['quote']
    for period in (FORECAST_2025, PAGE_DATE):
        with pytest.raises(ApiError) as refused:
            candidate(PIN, pages, claim(period, len(lines) + 1, 'Собственный капитал'), layouts)
        assert refused.value.code == 'validation_failed'


def test_actual_series_beside_a_forecast_series():
    text = '\n'.join([HEADER, 'Собственный капитал', '100 млн ₽', '2024 г.', '80 млн ₽', '2023 г.',
                      'Прогноз на 2025 год', 'Выручка', '120 млн ₽', 'за 2025 год', 'Прогнозы не гарантируют результат'])
    layout = '\n'.join(['Собственный капитал          Прогноз на 2025 год',
                        '100 млн ₽                    Выручка',
                        '2024 г.                      120 млн ₽',
                        '80 млн ₽                     за 2025 год',
                        '2023 г.',
                        'Прогнозы не гарантируют результат'])
    cards = numeric_cards({1: text}, {1: layout}, qualitative(11))
    assert {(c['metric'], c['amount']['value'], c['period']['kind'], c['period']['end']) for c in cards} == {
        ('Собственный капитал', '100', 'instant', '2024-12-31'), ('Собственный капитал', '80', 'instant', '2023-12-31')}
    for card in cards:
        fact, _ = candidate(PIN, {1: text}, card, {1: layout})
        assert fact['period'] == card['period'] and fact['nature'] == 'reported_actual'
    # Without the forecast heading the right column is an ordinary annual flow series.
    plain = {1: layout.replace('Прогноз на 2025 год', ' ' * 19)}
    assert {(c['amount']['value'], c['period']['kind'], c['period']['start']) for c in numeric_cards({1: text}, plain, qualitative(11))} == {
        ('100', 'instant', '2024-12-31'), ('80', 'instant', '2023-12-31'), ('120', 'interval', '2025-01-01')}
