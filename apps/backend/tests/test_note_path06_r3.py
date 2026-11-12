"""Собственные количества и годы допускаются по фрагментам, не по общему набору чисел."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.errors import ApiError
from app.tools import number_tokens, paragraph_remarks, check_units
from test_domain import fact
from test_note_path05_r4 import qualitative


def remarks(text, facts=(), visual=(), unrelated=()):
    before = deepcopy((facts, visual, unrelated))
    findings = []
    all_facts = {(f.get('source', {}).get('source_version_id'), f['fact_id']): f
                 for f in [*facts, *unrelated]}
    result = paragraph_remarks(dict(text=text), facts, [], all_facts, findings,
                               dict(block_id='block_probe'), claim_visual=visual)
    assert (facts, visual, unrelated) == before
    return {code for code, _ in result} | {item['code'] for item in findings}


def annual_fact(value, year):
    result = fact(value, year)
    result['period'].update(label=str(year), end=f'{year}-12-31')
    return result


def network_list(markers=True):
    lines = ['География бизнеса охватывает более 74 городов.',
             'Установлено 18,6 тыс. зарядных станций.',
             '3,7 млн человек стали клиентами компании.',
             '+125 тыс. новых пользователей: ежемесячный рост клиентской базы.']
    return qualitative(['\n'.join(('- ' if markers else '') + line for line in lines)])


@pytest.mark.parametrize('markers', [False, True])
def test_quantities_with_and_without_list_markers(markers):
    text = ('География бизнеса охватывает более 74 городов. Установлено 18,6 тыс. зарядных станций. '
            'Клиентами компании стали 3,7 млн человек. '
            'Ежемесячный рост клиентской базы составляет +125 тыс. новых пользователей.')
    assert not remarks(text, [network_list(markers)])
    assert not remarks(text.replace(' ', '\n'), [network_list(markers)])


@pytest.mark.parametrize('text', [
    'География бизнеса охватывает более 75 городов.',
    'География бизнеса охватывает 74 городов.',
    'География бизнеса охватывает до 74 городов.',
    'Установлено 18,6 млн зарядных станций.',
    'Установлено 18,7 тыс. зарядных станций.',
    'Установлено -18,6 тыс. зарядных станций.',
    'Установлено 18,6 тыс. активных станций.',
    'Продано 18,6 тыс. зарядных станций.',
    'Сотрудниками компании стали 3,7 млн человек.',
    'В компании 3,7 млн человек.',
    'Установлено 18,6 тыс. зарядных станций, продано 18,6 тыс. зарядных станций.',
    'Ежемесячный рост клиентской базы составляет -125 тыс. новых пользователей.',
    'Ежемесячный рост клиентской базы составляет +125 млн новых пользователей.',
    'Ежемесячный рост клиентской базы составляет +125 тыс. пользователей.',
])
def test_quantity_value_unit_bound_and_indicator_are_not_interchangeable(text):
    assert remarks(text, [network_list()])


def test_only_own_quantity_references_count():
    text = 'Установлено 18,6 тыс. зарядных станций.'
    assert remarks(text, [], unrelated=[network_list()])
    assert remarks(text, [qualitative(['Установлено оборудование.'])], unrelated=[network_list()])


def test_real_sign_and_ambiguous_sign_are_preserved():
    # В тексте записки дефис с пробелом разделяет части фразы (решение 3А.4), в цитате PDF это знак.
    assert number_tokens('- 37') == {Decimal('37')}
    assert number_tokens('−37') == {Decimal('-37')}
    assert number_tokens('+37') == {Decimal('37')}
    assert number_tokens('- 37 млн руб.', source_quote=True) == {Decimal('-37')}
    assert number_tokens('- 37%', source_quote=True) == {Decimal('-37')}
    # Один обособленный дефис без подтвержденной структуры не считается маркером.
    assert number_tokens('- 37 тыс. новых пользователей.', source_quote=True) == {Decimal('-37')}
    assert number_tokens('Рост - +37', source_quote=True) == set()
    # Несколько знаков с пробелами неоднозначны и в записке, профиль v16.
    with pytest.raises(ApiError):
        number_tokens('Рост - +37')
    with pytest.raises(ApiError):
        number_tokens('Рост + -37')
    quote = '- География бизнеса\n- -37 тыс. новых пользователей: ежемесячный рост клиентской базы.'
    assert number_tokens(quote, source_quote=True) == {Decimal('-37')}
    assert not remarks('Ежемесячный рост клиентской базы: -37 тыс. новых пользователей.', [qualitative([quote])])
    assert remarks('Ежемесячный рост клиентской базы: +37 тыс. новых пользователей.', [qualitative([quote])])


@pytest.mark.parametrize('text', ['37 млн руб.', '(37 млн руб.)', '37%', '(37%)'])
def test_qualitative_quotes_do_not_become_money_or_percent_facts(text):
    assert 'unit_mismatch' in remarks(text, [qualitative([text])])
    amount = dict(value='37', scale='1000000' if 'руб' in text else '1',
                  currency='RUB' if 'руб' in text else None, unit='money' if 'руб' in text else 'percent')
    assert not any(c.isdigit() for c in check_units(text, [amount]))
    with pytest.raises(ApiError):
        check_units(text.replace('37', '-37'), [amount])


def plan():
    return qualitative(['Сохранение лидирующих позиций и привлечение финансирования позволит компании '
                        'к 2031 году увеличить количество станций до 217 тыс.',
                        'Ключевые факторы роста: начало продаж сервиса в 2028 году, новый источник выручки.'])


@pytest.mark.parametrize('text', [
    'При сохранении лидирующих позиций и привлечении финансирования компания планирует '
    'к 2031 году увеличить количество станций до 217 тыс.',
    'План роста сети до 217 тыс. станций к 2031 году обусловлен привлечением финансирования '
    'и сохранением лидирующих позиций.',
    'Начало продаж сервиса в 2028 году как новый источник выручки.',
    'Запуск сервиса в 2028 году находится на стадии планов.',
])
def test_own_plan_year_and_quantity(text):
    assert not remarks(text, [plan()])
    assert not remarks(text.replace(' ', '\n'), [plan()])


@pytest.mark.parametrize('text', [
    'Запуск сервиса в 2029 году находится на стадии планов.',
    'Запуск оборудования в 2028 году находится на стадии планов.',
    'Запуск сервиса в 2028 году состоялся.',
    'Начало продаж сервиса в 2028 году состоялось, это новый источник выручки.',
    'Запуск сервиса в 2028 году не находится на стадии планов.',
    'План роста сети до 217 тыс. станций к 2032 году обусловлен привлечением финансирования '
    'и сохранением лидирующих позиций.',
    'План роста сети до 218 тыс. станций к 2031 году обусловлен привлечением финансирования '
    'и сохранением лидирующих позиций.',
    'План роста сети до 217 млн станций к 2031 году обусловлен привлечением финансирования '
    'и сохранением лидирующих позиций.',
    'План роста сети до 217 тыс. станций к 2031 году.',
    'План роста сети до 217 тыс. станций к 2031 году не обусловлен привлечением финансирования '
    'и сохранением лидирующих позиций.',
    'План роста сети до 217 тыс. станций к 2031 году без привлечения финансирования '
    'при сохранении лидирующих позиций.',
    'Выручка в 2028 году составила 37 млн руб.',
    'Запуск сервиса в 2028 году находится на стадии планов. Выручка в 2028 году составила 37 млн руб.',
])
def test_plan_does_not_authorize_wrong_year_metric_or_financial_period(text):
    assert remarks(text, [plan(), annual_fact('37', 2019)])


def test_foreign_plan_and_same_year_in_another_statement():
    text = 'Запуск сервиса в 2028 году находится на стадии планов.'
    assert 'period_mismatch' in remarks(text, [], unrelated=[plan()])
    assert 'period_mismatch' in remarks(text + ' Прибыль за 2028 год выросла.', [plan()])
    assert 'period_mismatch' in remarks('Выручка в 2028 году: 37 млн руб.',
                                      [plan(), annual_fact('37', 2019)])


def test_market_expression_preserves_its_modality():
    quote = ('За три года рынок оборудования может вырасти: с 42 тыс. станций '
             'в 2027 году до 86 тыс. станций в 2030 году.')
    source = qualitative([quote])
    assert not remarks('По оценке компании, ' + quote.lower(), [source])
    for text in [quote.replace('может вырасти', 'вырос'), quote.replace('2030', '2031'),
                 quote.replace('86', '87'), quote + ' Прибыль за 2030 год выросла.']:
        assert remarks(text, [source])


def visual(value, year, forecast=False, quarterly=False):
    return dict(verification_status='unchecked', numeric=dict(metric='Выручка (млн руб.)',
                nature='forecast' if forecast else 'reported_actual',
                amount=dict(value=value, unit='money', scale='1000000', currency='RUB'),
                period=dict(kind='interval', label=str(year), start=f'{year}-01-01',
                            end=f'{year}-03-31' if quarterly else f'{year}-12-31')))


SERIES = 'Диаграмма содержит ряд выручки за 2017 и 2018 годы и прогноз компании на 2027 и 2028 годы.'
MONEY = 'Текстовое основание подтверждает выручку 37 млн руб. в 2018 году.'
OBSERVATIONS = [visual('12', 2017), visual('38', 2018), visual('82', 2027, True), visual('96', 2028, True)]


@pytest.mark.parametrize('reverse,wrapped', [(False, False), (True, False), (False, True)])
def test_mixed_paragraph_keeps_independent_sentences(reverse, wrapped):
    sentences = [SERIES, MONEY]
    if reverse:
        sentences.reverse()
    text = ' '.join(sentences)
    if wrapped:
        text = text.replace(' ', '\n')
    assert not remarks(text, [annual_fact('37', 2018)], OBSERVATIONS)
    unavailable = 'Темп роста выручки 2018 года к 2017 году нельзя подтвердить по доступным основаниям, расчет не выполнялся.'
    assert not remarks(text + ' ' + unavailable, [annual_fact('37', 2018)], OBSERVATIONS)


@pytest.mark.parametrize('changed', [
    MONEY.replace('37', '38'), MONEY.replace('37', '82'), MONEY.replace('руб.', 'USD'),
    MONEY.replace('млн', 'млрд'), MONEY.replace('2018', '2017'), MONEY.replace('2018', '2027'),
])
def test_visual_value_or_same_year_does_not_authorize_ordinary_finances(changed):
    assert remarks(SERIES + ' ' + changed, [annual_fact('37', 2018)], OBSERVATIONS)
    assert remarks(changed + '\n' + SERIES, [annual_fact('37', 2018)], OBSERVATIONS)


def test_visual_years_need_own_metric_nature_and_full_period():
    assert remarks(SERIES, [annual_fact('37', 2018)])
    assert remarks(SERIES.replace('выручки', 'прибыли'), [annual_fact('37', 2018)], OBSERVATIONS)
    assert remarks(SERIES.replace('2027 и 2028', '2027 и 2029'), [annual_fact('37', 2018)], OBSERVATIONS)
    assert remarks('Диаграмма содержит ряд выручки за 2027 и 2028 годы.', [annual_fact('37', 2018)], OBSERVATIONS)
    quarterly = deepcopy(OBSERVATIONS)
    quarterly[0] = visual('12', 2017, quarterly=True)
    assert remarks(SERIES, [annual_fact('37', 2018)], quarterly)
    ordinary = annual_fact('37', 2018)
    ordinary['period'].update(label='1Q18', end='2018-03-31')
    assert 'period_mismatch' in remarks(SERIES + ' ' + MONEY, [ordinary], OBSERVATIONS)
    assert remarks(SERIES + ' Темп роста выручки 2018 года к 2017 году составил 12%.', [annual_fact('37', 2018)], OBSERVATIONS)


def test_plan_deadline_does_not_mask_a_second_occurrence_for_finances():
    text = ('План роста сети до 217 тыс. станций к 2031 году обусловлен привлечением финансирования '
            'и сохранением лидирующих позиций, выручка к 2031 году составила 37 млн руб.')
    assert 'period_mismatch' in remarks(text, [plan(), annual_fact('37', 2019)])


def test_independent_plan_sentences_can_be_reordered():
    statements = ['Запуск сервиса в 2028 году находится на стадии планов.',
                  'План роста сети до 217 тыс. станций к 2031 году обусловлен привлечением финансирования '
                  'и сохранением лидирующих позиций.']
    for order in (statements, list(reversed(statements))):
        assert not remarks(' '.join(order), [plan()])


def test_wrapped_source_list_keeps_the_marker_separate_from_its_sign():
    basis = network_list()
    basis['evidence'][0]['quote'] = basis['evidence'][0]['quote'].replace('тыс. ', 'тыс.\n').replace('млн ', 'млн\n')
    text = ('Ежемесячный рост клиентской базы составляет +125 тыс. новых пользователей. '
            'Клиентами компании стали 3,7 млн человек.')
    assert not remarks(text, [basis])
