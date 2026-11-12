"""Дата качественной цитаты и годы ряда допускаются только в своем вхождении."""
import pytest

from test_domain import fact
from test_note_path05_r4 import qualitative, remarks, series, change


@pytest.mark.parametrize('month,quote,year', [
    ('апрелем', 'Апрель', 2024),
    ('сентябрем', 'Сентябрь', 2018),
    ('январём', 'Январь', 2016),
    ('мартом', 'Март', 2015),
])
def test_document_month_forms_have_the_same_explicit_date(month, quote, year):
    assert not remarks(f'Документ датирован {month} {year} года.',
                       [qualitative([f'{quote} {year}'])])


@pytest.mark.parametrize('text,quotes', [
    ('Документ датирован октябрем 2018 года.', ['Сентябрь 2018']),
    ('Документ датирован сентябрем 2019 года.', ['Сентябрь 2018']),
    ('Документ датирован сентябрем 2018 года.', ['Дата не указана']),
    ('Документ датирован сентябрем 2018 года.', ['Сентябрь', '2018']),
    ('Документ датирован сентябрем 2018 года.', []),
    ('Документ датирован сентябрем 2018 года. Итоги за 2018 год.', ['Сентябрь 2018']),
])
def test_document_month_needs_its_own_quote_and_does_not_confirm_an_annual_period(text, quotes):
    assert 'period_mismatch' in remarks(text, [qualitative(quotes)])


def test_document_month_does_not_confirm_financial_amounts_or_calculations():
    date_fact = qualitative(['Сентябрь 2018'])
    assert 'unit_mismatch' in remarks('Документ датирован сентябрем 2018 года. Доход 90 млн руб.', [date_fact])
    numeric = fact('80', 2017)
    assert 'period_mismatch' in remarks('Документ датирован сентябрем 2018 года. Доход за 2018 год 80 млн руб.', [date_fact, numeric])
    calculation, inputs = change()
    assert 'period_mismatch' in remarks('Изменение за сентябрь 2018 года: -20%.', [date_fact], [calculation], inputs)


@pytest.mark.parametrize('separator', ['-', '\u2013'])
def test_qualitative_series_in_dynamics_uses_all_its_years(separator):
    text = f'Объем отгрузок показан в динамике за 2017{separator}2019 годы.'
    assert not remarks(text, [series()])
    assert not remarks(text, [series([f'Динамика отгрузок 2017{separator}2019'])])


@pytest.mark.parametrize('text', [
    'Объем отгрузок показан в динамике за 2017-2020 годы.',
    'Объем отгрузок показан в динамике за 2017-2019 годы. Компания основана в 2019 году.',
    'Объем отгрузок показан в динамике за 2017-2019 годы. Доход 80 млн руб.',
    'Объем отгрузок показан в динамике за 2017-2019 годы, доход 80 млн руб.',
    'Объем отгрузок показан в динамике за 2017-2019 годы, рост 12%.',
    'Объем отгрузок показан в динамике за 2017-2019 долларов.',
    'Объем отгрузок показан в динамике за 2017-2019 миллионов рублей.',
])
def test_series_form_does_not_extend_years_or_allow_other_claims(text):
    assert remarks(text, [series()])


def test_series_without_middle_year_or_with_financial_basis_keeps_the_error():
    text = 'Объем отгрузок показан в динамике за 2017-2019 годы.'
    assert remarks(text, [series(['Первая отметка 2017', 'Последняя отметка 2019'])])
    assert not remarks(text, [series(['Подтвержденный ряд 2017-2019'])])
    assert remarks(text, [series(), fact('80', 2019)])
    calculation, inputs = change()
    assert remarks(text, [series()], [calculation], inputs)
