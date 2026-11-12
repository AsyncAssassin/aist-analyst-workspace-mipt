"""Общий год интервала не ослабляет проверку полного финансового периода."""
from copy import deepcopy

import pytest

from app.errors import ApiError
from app.tools import check_periods, number_tokens, paragraph_remarks
from test_domain import fact


ANNUAL = dict(kind='interval', start='2023-01-01', end='2023-12-31')
PHRASE = 'полный календарный год с 1 января по 31 декабря 2023 г.'


@pytest.mark.parametrize('suffix', ['', ' г.', ' год', ' года', ' году', ' годом'])
@pytest.mark.parametrize('text,period', [
    ('с 1 января по 31 декабря 2023', ANNUAL),
    ('С 29 ФЕВРАЛЯ ПО 30 АПРЕЛЯ 2024', dict(kind='interval', start='2024-02-29', end='2024-04-30')),
    ('с 15 мая по 17 июля 1999', dict(kind='interval', start='1999-05-15', end='1999-07-17')),
])
def test_shared_year_consumes_the_whole_supported_interval(text, period, suffix):
    assert not number_tokens(check_periods(text + suffix, [period]))


@pytest.mark.parametrize('text', [
    'с 2 января по 31 декабря 2023 г.',
    'с 1 января по 30 декабря 2023 г.',
    'с 1 января по 31 декабря 2022 г.',
    'с 29 февраля по 31 декабря 2023 г.',
    'с 1 января по 31 апреля 2023 г.',
    'с 0 января по 31 декабря 2023 г.',
    'с 31 декабря по 1 января 2023 г.',
])
def test_shared_year_rejects_changed_invalid_or_reversed_dates(text):
    with pytest.raises(ApiError):
        check_periods(text, [ANNUAL])


@pytest.mark.parametrize('periods', [
    [dict(kind='interval', start='2023-01-01', end='2023-06-30'),
     dict(kind='interval', start='2023-07-01', end='2023-12-31')],
    [dict(kind='instant', start='2023-01-01', end='2023-01-01'),
     dict(kind='instant', start='2023-12-31', end='2023-12-31')],
    [dict(kind='interval', start='2023-01-01', end='2023-03-31')],
])
def test_shared_year_requires_one_complete_interval(periods):
    with pytest.raises(ApiError):
        check_periods(PHRASE, periods)


def test_reversed_months_do_not_infer_the_previous_year():
    with pytest.raises(ApiError):
        check_periods('с 31 декабря по 1 января 2024 года',
                      [dict(kind='interval', start='2023-12-31', end='2024-01-01')])


@pytest.mark.parametrize('text', [
    'на 1 января 2023 года', 'на 31.12.2023',
    'с 1 января 2023 года по 31 декабря 2023 года',
    'с 01.01.2023 по 31.12.2023',
    '2023-01-01 по 2023-12-31', '2023 год', '12M23',
])
def test_existing_annual_forms_still_pass(text):
    assert not number_tokens(check_periods(text, [ANNUAL]))


@pytest.mark.parametrize('text', ['1Q23', '1 кв. 2023г.', '3 месяца 2023 года'])
def test_existing_quarter_forms_still_pass(text):
    assert not number_tokens(check_periods(text, [dict(ANNUAL, end='2023-03-31')]))


@pytest.mark.parametrize('text,expected', [
    (PHRASE + ': 100 млн руб.', set()),
    ('с 1 января по 31 декабря: 100 млн руб.', {'unsupported_numeric_form'}),
    (PHRASE + ': 100 млн руб. (777).', {'unsupported_numbers', 'unsupported_numeric_form'}),
    (PHRASE + ': 101 млн руб.', {'unsupported_numbers', 'unit_mismatch'}),
    (PHRASE + ': 100 тыс. руб.', {'unit_mismatch'}),
    (PHRASE + ': 100 млн руб. Сравнение с 2020 г.', {'unsupported_numbers', 'period_mismatch'}),
])
def test_paragraph_keeps_numeric_and_unit_checks(text, expected):
    saved = fact(year=2023)
    saved['period'] = deepcopy(ANNUAL)
    findings = []
    remarks = paragraph_remarks(dict(text=text), [saved], [], {}, findings, None)
    assert {code for code, _ in remarks} | {f['code'] for f in findings} == expected


def test_qualitative_quote_does_not_supply_a_financial_interval():
    numeric = fact(year=2023)
    qualitative = fact(fid='fact_quote')
    qualitative.update(amount=None, nature='source_statement', period=deepcopy(ANNUAL))
    qualitative['evidence'][0]['quote'] = PHRASE
    findings = []
    remarks = paragraph_remarks(dict(text=PHRASE + ': 100 млн руб.'),
                                [numeric, qualitative], [], {}, findings, None)
    assert 'period_mismatch' in {code for code, _ in remarks}
