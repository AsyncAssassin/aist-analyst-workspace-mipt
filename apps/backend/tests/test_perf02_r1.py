"""Local period and forecast labels of cards; one caption over a series of dated amounts."""
from copy import deepcopy

import pytest

from app.errors import ApiError
from app.extraction import candidate
from app.source_cards import numeric_cards
from test_d10 import PIN

HEADER = 'Источник: данные Synthetic на 31.12.2024'


def qualitative(line_end, page=1):
    return dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None, basis=None,
                evidence=[dict(page=page, line_start=1, line_end=line_end, area='Карточки')])


def plain_page(second_line):
    text = '\n'.join([HEADER, second_line, 'Выручка', '100 млн ₽'])
    return {1: text}, {1: text}


def numeric_claim(period, metric='Выручка', value='100', value_text='100', unit='млн ₽'):
    return dict(metric=metric, subject='Synthetic', reporting_basis=HEADER, nature='reported_actual', period=period,
                amount=dict(value=value, unit='money', currency='RUB'), value_text=value_text,
                evidence=[dict(page=1, line_start=1, line_end=4, area='Карточка')],
                basis=dict(page=1, metric_label=metric, unit_label=unit, reporting_label=HEADER))


PAGE_DATE = dict(kind='instant', label='на 31.12.2024', start='2024-12-31', end='2024-12-31')
YEAR_2023 = dict(kind='interval', label='за 2023 год', start='2023-01-01', end='2023-12-31')


def test_forecast_label_blocks_numeric_card_and_page_date():
    pages, layouts = plain_page('Прогноз на 2025 год')
    assert numeric_cards(pages, layouts, qualitative(4)) == []
    for period in (PAGE_DATE, dict(kind='interval', label='2025', start='2025-01-01', end='2025-12-31')):
        with pytest.raises(ApiError) as refused:
            candidate(PIN, pages, numeric_claim(period), layouts)
        assert refused.value.code == 'validation_failed'


def test_annual_flow_label_sets_interval_not_page_date():
    pages, layouts = plain_page('за 2023 год')
    cards = numeric_cards(pages, layouts, qualitative(4))
    assert [(c['metric'], c['amount']['value'], c['period']) for c in cards] == [('Выручка', '100', YEAR_2023)]
    fact, basis = candidate(PIN, pages, cards[0], layouts)
    assert fact['nature'] == 'reported_actual' and fact['period'] == YEAR_2023 and fact['amount']['scale'] == '1000000'
    assert 'Выручка' in basis['definition']
    for wrong in (PAGE_DATE, dict(YEAR_2023, start='2023-04-01'), dict(YEAR_2023, kind='instant', start='2023-12-31')):
        with pytest.raises(ApiError):
            candidate(PIN, pages, numeric_claim(wrong), layouts)
    assert candidate(PIN, pages, numeric_claim(YEAR_2023), layouts)[0]['period'] == YEAR_2023


def test_local_date_below_amount_is_a_balance_date():
    text = '\n'.join([HEADER, 'Портфель', '500 млн ₽', '(на 30.06.2024)'])
    cards = numeric_cards({1: text}, {1: text}, qualitative(4))
    assert [(c['metric'], c['amount']['value'], c['period']['kind'], c['period']['end']) for c in cards] == [('Портфель', '500', 'instant', '2024-06-30')]
    assert candidate(PIN, {1: text}, cards[0], {1: text})[0]['period']['start'] == '2024-06-30'


@pytest.mark.parametrize('markers', [
    ['за 2023 год', '(на 31.12.2024)'],
    ['за 2023 год', 'за 2022 год'],
    ['(на 31.12.2023)', '(на 30.06.2024)'],
])
def test_conflicting_local_markers_give_no_card(markers):
    text = '\n'.join([HEADER, 'Выручка', *markers, '100 млн ₽'])
    assert numeric_cards({1: text}, {1: text}, qualitative(5)) == []


def test_bare_year_is_ambiguous_for_a_flow_metric():
    text = '\n'.join([HEADER, 'Выручка', '2023 г.', '100 млн ₽'])
    assert numeric_cards({1: text}, {1: text}, qualitative(4)) == []
    text = '\n'.join([HEADER, 'Выручка', '100 млн ₽', '2023 г.', '80 млн ₽', '2022 г.'])
    assert numeric_cards({1: text}, {1: text}, qualitative(6)) == []


def test_neighboring_cards_keep_their_own_periods():
    text = '\n'.join([HEADER, 'Выручка', 'за 2023 год', '100 млн ₽', 'Активы', '(на 30.06.2024)', '250 млн ₽', 'Прибыль', '30 млн ₽'])
    layout = '\n'.join(['Выручка            Активы              Прибыль',
                        'за 2023 год        (на 30.06.2024)',
                        '100 млн ₽          250 млн ₽           30 млн ₽'])
    cards = numeric_cards({1: text}, {1: layout}, qualitative(9))
    assert {(c['metric'], c['amount']['value'], c['period']['kind'], c['period']['start'], c['period']['end']) for c in cards} == {
        ('Выручка', '100', 'interval', '2023-01-01', '2023-12-31'),
        ('Активы', '250', 'instant', '2024-06-30', '2024-06-30'),
        ('Прибыль', '30', 'instant', '2024-12-31', '2024-12-31')}
    for card in cards:
        assert candidate(PIN, {1: text}, card, {1: layout})[0]['period'] == card['period']
    moved = {1: layout.replace('(на 30.06.2024)', '               ').replace('Прибыль', '  (на 30.06.2024)')}
    assert all(c['period']['end'] != '2024-06-30' for c in numeric_cards({1: text}, moved, qualitative(9)))


SERIES_TEXT = '\n'.join([
    'Источник: данные Synthetic на 30.09.2024', 'Ставка 9,5%', 'средняя', 'ставка купона', 'по выпуску', '17,2%',
    'Общий объем', 'средств', '(на 30.09.2024)', '3 950млн ₽', '2 800 млн₽ +40,0%', '2 000 млн₽', 'Собственный',
    'капитал', '2023 г.', '2022г.', '3 150млн₽+12,5%', '2 кв. 2024г.'])
SERIES_LAYOUT = '\n'.join([
    'Ставка 9,5%                   Общий объем',
    'средняя                       средств',
    '                              (на 30.09.2024)',
    '                              3 950        млн ₽',
    '                              Собственный',
    '                              капитал',
    '',
    '17,2%                         3 150         млн  ₽  +12,5%',
    'ставка купона                 2 кв. 2024г.',
    'по выпуску',
    '',
    '                              2 800 млн        ₽    +40,0%',
    '                              2023 г.',
    '',
    '                              2 000 млн₽',
    '                              2022г.',
    '',
    'Источник: данные Synthetic на 30.09.2024'])


def test_caption_series_binds_each_amount_to_its_own_closing_date():
    pages, layouts = {1: SERIES_TEXT}, {1: SERIES_LAYOUT}
    cards = numeric_cards(pages, layouts, qualitative(18))
    observed = {(c['metric'], c['amount']['value'], c['period']['kind'], c['period']['label'], c['period']['end']) for c in cards}
    assert observed == {
        ('Общий объем средств', '3950', 'instant', 'на 30.09.2024', '2024-09-30'),
        ('Собственный капитал', '3150', 'instant', '2 кв. 2024г.', '2024-06-30'),
        ('Собственный капитал', '2800', 'instant', '2023 г.', '2023-12-31'),
        ('Собственный капитал', '2000', 'instant', '2022г.', '2022-12-31')}
    assert all(c['amount']['unit'] == 'money' for c in cards), 'growth percentages are not observations'
    for card in cards:
        fact, basis = candidate(PIN, pages, card, layouts, diagrams=None)
        assert fact['period'] == card['period'] and fact['nature'] == 'reported_actual' and fact['amount']['scale'] == '1000000'
        assert fact['raw_text'] == card['value_text']
    capital = next(c for c in cards if c['amount']['value'] == '2800')
    quotes = [e['quote'] for e in candidate(PIN, pages, capital, layouts)[0]['evidence']]
    assert '2 800 млн₽ +40,0%' in quotes and '2023 г.' in quotes and 'Собственный' in quotes and 'капитал' in quotes
    for wrong in (dict(kind='interval', label='2023 г.', start='2023-01-01', end='2023-12-31'),
                  dict(kind='instant', label='на 30.09.2024', start='2024-09-30', end='2024-09-30')):
        bad = deepcopy(capital); bad['period'] = wrong
        with pytest.raises(ApiError):
            candidate(PIN, pages, bad, layouts)
    bad = deepcopy(capital); bad['amount']['value'] = '40.0'; bad['value_text'] = '40,0'
    with pytest.raises(ApiError):
        candidate(PIN, pages, bad, layouts)
    # Without the cited period lines the series amounts stay qualitative.
    partial = qualitative(18); partial['evidence'] = [dict(page=1, line_start=1, line_end=14, area='Без подписей периодов')]
    assert {c['amount']['value'] for c in numeric_cards(pages, layouts, partial)} == {'3950'}
    assert numeric_cards(pages, {}, qualitative(18)) == []


def test_caption_series_needs_a_stock_metric_or_explicit_flow_labels():
    text = '\n'.join([HEADER, 'Выручка', '100 млн ₽ +25,0%', '2023 г.', '80 млн ₽', '2022 г.'])
    assert numeric_cards({1: text}, {1: text}, qualitative(6)) == []
    text = '\n'.join([HEADER, 'Выручка', '100 млн ₽ +25,0%', 'за 2023 год', '80 млн ₽', 'за 2022 год'])
    cards = numeric_cards({1: text}, {1: text}, qualitative(6))
    assert {(c['amount']['value'], c['period']['kind'], c['period']['start'], c['period']['end']) for c in cards} == {
        ('100', 'interval', '2023-01-01', '2023-12-31'), ('80', 'interval', '2022-01-01', '2022-12-31')}
    text = '\n'.join([HEADER, 'Прогноз капитала', '100 млн ₽', '2025 г.'])
    assert numeric_cards({1: text}, {1: text}, qualitative(4)) == []
