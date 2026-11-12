"""Смысловые подмены новых форм: событие, предмет количества, период и показатель."""
from copy import deepcopy

import pytest

from app.tools import paragraph_remarks
from app.visual_observations import row_agrees
from test_domain import fact


def statement(quote, *, subject='Вектор', period=None):
    saved = fact()
    saved.update(amount=None, subject=subject, nature='source_statement', raw_text=quote,
                 period=period or dict(kind='unknown', start=None, end=None, label='Не указан'))
    saved['evidence'][0]['quote'] = quote
    return saved


def problems(text, facts):
    findings = []
    remarks = paragraph_remarks(dict(text=text), facts, [], {}, findings, None)
    return {code for code, _ in remarks} | {f['code'] for f in findings}


FOUNDING = 'Вектор - российская фармацевтическая компания, основанная в 2012 году в Казани.'
FOUNDING_CLAIM = 'Вектор: российская фармацевтическая компания, основанная в 2012 году в Казани.'
COUNTS = ('Продуктовый Портфель\n311\nрегистрационных\nудостоверений\n31\nЛП, МИ, БАД\n'
          'на стадии регистрации\n48\nЛП, БАД\nна стадии разработки')
COUNT_CLAIM = ('Продуктовый портфель: 311 регистрационных удостоверений, 31 ЛП, МИ и БАД на стадии регистрации, '
               '48 ЛП и БАД на стадии разработки.')
STAFF = 'Операционные Показатели\n1642\nколичество\nсотрудников\n72\nгородов\nприсутствия\n14\nстран\nэкспорта'
STAFF_CLAIM = 'Операционные показатели: 1642 сотрудника, 72 города присутствия, 14 стран экспорта.'
SHOPS = 'За 2024 Вектор открыла 37 новых магазинов, развивает e-commerce.'
SHOPS_CLAIM = 'За 2024 год Вектор открыла 37 новых магазинов и развивает e-commerce.'
PLAN = ('Строительство нового завода Bio Plant может стать драйвером роста '
        '(планируемая дата промышленного запуска: 2-3 кв. 2028 г.).')
PLAN_PERIOD = dict(kind='interval', label='II-III кварталы 2028 (план)', start='2028-04-01', end='2028-09-30')


@pytest.mark.parametrize('quote,claim', [(FOUNDING, FOUNDING_CLAIM), (COUNTS, COUNT_CLAIM),
    (STAFF, STAFF_CLAIM), (SHOPS, SHOPS_CLAIM)])
def test_new_forms_with_other_values_and_company(quote, claim):
    assert not problems(claim, [statement(quote)])


@pytest.mark.parametrize('claim', [FOUNDING_CLAIM.replace('2012', '2013'),
    FOUNDING_CLAIM.replace('компания, основанная', 'завод, запущенный'),
    FOUNDING_CLAIM.replace('Вектор:', 'Другой:')])
def test_founding_date_belongs_to_company_event(claim):
    assert problems(claim, [statement(FOUNDING)])


def test_same_year_of_another_event_is_not_founding():
    assert problems(FOUNDING_CLAIM, [statement('Вектор - компания, приобретенная в 2012 году.')])


@pytest.mark.parametrize('before,after', [('31 ЛП','48 ЛП'), ('48 ЛП','31 ЛП'),
    ('311 регистрационных','311 тыс. регистрационных'), ('регистрации','разработки')])
def test_portfolio_stages_and_scale_are_not_interchangeable(before, after):
    assert problems(COUNT_CLAIM.replace(before,after), [statement(COUNTS)])


@pytest.mark.parametrize('before,after', [('1642 сотрудника','1642 магазина'),
    ('14 стран','14 городов'), ('72 города','14 города')])
def test_operating_objects_are_not_interchangeable(before, after):
    assert problems(STAFF_CLAIM.replace(before,after), [statement(STAFF)])


@pytest.mark.parametrize('claim', [COUNT_CLAIM.replace('311 регистрационных', 'до 311 регистрационных'),
    COUNT_CLAIM.replace('Продуктовый портфель:', 'Продуктовый портфель конкурента:'),
    COUNT_CLAIM.replace('Продуктовый портфель:', 'План продуктового портфеля:')])
def test_portfolio_allowance_does_not_change_scope_or_modality(claim):
    assert problems(claim, [statement(COUNTS)])


def test_portfolio_source_bound_is_not_an_exact_number():
    assert problems(COUNT_CLAIM, [statement(COUNTS.replace('311', 'До 311'))])
    assert problems(COUNT_CLAIM, [statement('План продуктового портфеля\n' + COUNTS)])
    assert problems(COUNT_CLAIM, [statement(COUNTS.replace('311', 'Не менее\n311'))])


@pytest.mark.parametrize('text', [SHOPS_CLAIM.replace('2024','2023'), SHOPS_CLAIM.replace('магазинов','сотрудников'),
    'Вектор имеет 37 магазинов всего.'])
def test_openings_are_not_total_stores_or_another_period(text):
    assert problems(text,[statement(SHOPS)])


@pytest.mark.parametrize('claim,quote',[(FOUNDING_CLAIM,FOUNDING),(COUNT_CLAIM,COUNTS),(SHOPS_CLAIM,SHOPS)])
def test_incidental_quote_and_wrong_pin_do_not_grant_new_allowance(claim,quote):
    saved = statement(quote)
    saved['raw_text']='Обзор деятельности.'
    assert problems(claim,[saved])
    saved=statement(quote)
    saved['evidence'][0]['source']=dict(saved['source'],sha256='b'*64)
    assert problems(claim,[saved])


def test_quarter_range_and_forecast_are_preserved():
    assert not problems(PLAN,[statement(PLAN,period=PLAN_PERIOD)])


@pytest.mark.parametrize('prefix', ['Завод Bio Plant планируется к промышленному запуску только в ',
                                    'Завод Bio Plant с планируемым промышленным запуском в '])
def test_explicit_plan_in_primary_quote_survives_legacy_unknown_period(prefix):
    assert not problems(prefix + '2-3 кв. 2028 г.', [statement(PLAN)])
    assert problems(prefix + '2-3 кв. 2027 г.', [statement(PLAN)])
    assert problems(prefix.replace('планируется', 'подготовлен').replace('планируемым', 'состоявшимся')
                    + '2-3 кв. 2028 г.', [statement(PLAN)])


@pytest.mark.parametrize('before,after',[('2-3','2'),('2028','2027'),('2-3','3-2'),
    ('планируемая дата промышленного запуска','завод запущен'), ('может стать','обязательно станет')])
def test_plan_cannot_be_narrowed_or_claimed_completed(before,after):
    assert problems(PLAN.replace(before,after),[statement(PLAN,period=PLAN_PERIOD)])


def test_financial_period_does_not_borrow_qualitative_range():
    numeric=fact()
    assert problems('Выручка 100 млн руб. за 2-3 кв. 2028 г.',[numeric,statement(PLAN,period=PLAN_PERIOD)])


def test_semicolon_does_not_separate_forecast_from_its_date():
    claim = ('Строительство нового завода Bio Plant, по оценке авторов, может стать драйвером роста; '
             'планируемая дата промышленного запуска: 2-3 кв. 2028 г.')
    assert not problems(claim, [statement(PLAN, period=PLAN_PERIOD)])
    assert 'event_mismatch' in problems(claim.replace('может стать', 'обязательно станет'),
                                       [statement(PLAN, period=PLAN_PERIOD)])


def test_qualitative_period_without_its_primary_statement_is_insufficient():
    saved = statement(PLAN, period=PLAN_PERIOD)
    saved['raw_text'] = 'Описание другого события.'
    assert 'event_mismatch' in problems(PLAN, [saved])
    saved = statement(PLAN, period=PLAN_PERIOD)
    saved['evidence'][0]['source'] = dict(saved['source'], sha256='b'*64)
    assert 'event_mismatch' in problems(PLAN, [saved])


CAPACITY_QUOTE = ('Производственные мощности\nВектор\nЗавод А\nВектор\nЗавод Б\n'
                  '125 млн\nупаковок\nв год\n2,4 млрд\nтаблеток\nв год')
CAPACITY_LAYOUT = ('Производственные мощности\n'
                   'Вектор              Вектор\n'
                   'Завод А             Завод Б\n\n'
                   '125 млн             2,4 млрд\n'
                   'упаковок            таблеток\n'
                   'в год               в год')
CAPACITY_CLAIM = ('Производственные мощности: Вектор Завод А: 125 млн упаковок в год; '
                  'Вектор Завод Б: 2,4 млрд таблеток в год.')


def capacity_problems(text, layout=CAPACITY_LAYOUT, quote=CAPACITY_QUOTE):
    findings = []
    remarks = paragraph_remarks(dict(text=text), [statement(quote)], [], {}, findings, None,
        capacity_pages=[dict(layout=layout, quote=quote)])
    return {code for code, _ in remarks} | {f['code'] for f in findings}


def test_capacity_is_bound_to_its_pdf_column():
    assert not capacity_problems(CAPACITY_CLAIM)


@pytest.mark.parametrize('before,after', [('Завод А:', 'Завод Б:'), ('таблеток', 'упаковок'),
    ('в год', 'в месяц'), ('2,4 млрд', '2,4 млн'), ('Производственные мощности:', 'План мощностей:')])
def test_capacity_does_not_allow_swapped_plant_unit_or_time(before, after):
    assert capacity_problems(CAPACITY_CLAIM.replace(before, after))


def test_capacity_requires_layout_and_full_citation():
    assert capacity_problems(CAPACITY_CLAIM, layout='')
    assert capacity_problems(CAPACITY_CLAIM, quote=CAPACITY_QUOTE.replace('Завод Б', ''))
    assert capacity_problems(CAPACITY_CLAIM, layout=CAPACITY_LAYOUT.replace('125 млн', 'До 125 млн'))


MARKET = ('В 2029 г. коммерческий сегмент потребительского рынка РФ остался на натуральных значениях '
          '(млн упак.) 2028 г. Таким образом доля рынка Вектора по упаковкам снизилась с 4.2% до 3.8% г/г, '
          'фактически до уровня 2022 г. При этом лидеру рынка - Альфа, удалось увеличить продажи на 9%.')
MARKET_CLAIM = ('В 2029 г. коммерческий сегмент потребительского рынка РФ в натуральном выражении '
                '(млн упаковок) остался на уровне 2028 г. Доля компании по упаковкам в коммерческом сегменте '
                'опустилась фактически до уровня 2022 г. Лидер рынка, Альфа, увеличил продажи.')
MARKET_PERIOD = dict(kind='interval', label='2029', start='2029-01-01', end='2029-12-31')


def test_market_comparison_keeps_its_baseline_and_subject():
    assert not problems(MARKET_CLAIM, [statement(MARKET, period=MARKET_PERIOD)])


@pytest.mark.parametrize('before,after', [('В 2029 г.', 'В 2028 г.'), ('уровне 2028 г.', 'уровне 2029 г.'),
    ('уровня 2022 г.', 'уровня 2028 г.'), ('по упаковкам', 'в денежном выражении'),
    ('Лидер рынка, Альфа,', 'Лидер рынка, Вектор,'), ('Доля компании', 'Доля компании 9%')])
def test_market_cannot_borrow_competitor_baseline_or_unit(before, after):
    assert problems(MARKET_CLAIM.replace(before, after), [statement(MARKET, period=MARKET_PERIOD)])


def test_market_dates_do_not_come_from_incidental_or_foreign_statements():
    saved = statement(MARKET, period=MARKET_PERIOD)
    saved['raw_text'] = 'Общий обзор рынка.'
    assert problems(MARKET_CLAIM, [saved])
    assert problems(MARKET_CLAIM, [statement(MARKET.replace('доля рынка Вектора', 'доля рынка Беты'), period=MARKET_PERIOD)])


def test_margin_alias_keeps_value_unit_and_period():
    row=dict(label='Рентабельность по EBITDA, %',period_label='2024',amount=dict(value='19.5',unit='percent',scale='1',currency=None))
    observation=dict(numeric=dict(metric='EBITDA margin',period=dict(label='2024'),amount=deepcopy(row['amount'])),
                     verification_status='unchecked')
    assert row_agrees(row,observation)
    for label in ('EBITDA','Чистая маржа','Скорректированная EBITDA','Adjusted EBITDA margin'):
        assert not row_agrees(dict(row,label=label),observation)
    assert not row_agrees(dict(row,period_label='2025'),observation)
    for amount in (dict(row['amount'],value='195'),dict(row['amount'],unit='money',currency='RUB')):
        assert not row_agrees(dict(row,amount=amount),observation)
    assert observation['verification_status']=='unchecked'
