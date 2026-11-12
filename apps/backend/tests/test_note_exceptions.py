from copy import deepcopy

import pytest

from app import tables
from app.tools import paragraph_remarks, row_remarks
from test_domain import fact


def issues(quote, claim, *, primary=None, other_pin=False):
    saved = fact()
    saved.update(amount=None, subject='Вектор', nature='source_statement', raw_text=primary or quote)
    saved['evidence'][0]['quote'] = quote
    if other_pin:
        saved['evidence'][0]['source'] = dict(saved['source'], source_version_id='sv_other')
    findings = []
    remarks = paragraph_remarks(dict(text=claim), [saved], [], {}, findings, None)
    return {code for code, _ in remarks} | {f['code'] for f in findings}


DEALS = 'За 12 лет мы закрыли 28 крупных контрактов.'
DEAL_CLAIM = 'За 12 лет компания закрыла 28 крупных контрактов.'
GROWTH = 'От доли рынка 3% сегодня мы можем вырасти в 4 и в 8 раз.'
GROWTH_CLAIM = 'По оценке компании, от текущей доли рынка компания может вырасти в 4 и в 8 раз.'
RANK = 'С 2018 года мы занимали 5 раз первое место по сервису на сайтах отзывов.'
RANK_CLAIM = 'С 2018 года Вектор 5 раз занимал первое место по сервису на сайтах отзывов.'


@pytest.mark.parametrize('quote,claim', [(DEALS, DEAL_CLAIM), (GROWTH, GROWTH_CLAIM), (RANK, RANK_CLAIM)])
def test_same_source_event_in_another_company(quote, claim):
    assert not issues(quote, claim)


@pytest.mark.parametrize('quote,claim', [
    (DEALS, DEAL_CLAIM.replace('12 лет', '1 год')),
    (DEALS, DEAL_CLAIM.replace('28', '12')),
    (DEALS, DEAL_CLAIM.replace('компания', 'другая компания')),
    (DEALS, DEAL_CLAIM.replace('закрыла', 'не закрыла')),
    (DEALS, DEAL_CLAIM.replace('контрактов', 'сделок')),
    (DEALS, DEAL_CLAIM.replace('контрактов.', 'контрактов и несколько десятков небольших.')),
    (DEALS, DEAL_CLAIM + ' Доход составил 28 млн руб.'),
    (GROWTH, GROWTH_CLAIM.replace('может вырасти', 'выросла')),
    (GROWTH, GROWTH_CLAIM.replace('раз', 'процентов')),
    (GROWTH, GROWTH_CLAIM.replace('4 и в 8', '8 и в 4')),
    (GROWTH, GROWTH_CLAIM.replace('доли рынка', 'прибыли')),
    (GROWTH[:-1]+'\nесли закроется сделка.', GROWTH_CLAIM),
    (RANK, RANK_CLAIM.replace('С 2018 года', 'За 2018 год')),
    (RANK, RANK_CLAIM.replace('5 раз', '4 раза')),
    (RANK.replace('отзывов.', 'отзывов reviews.ru.'), RANK_CLAIM.replace('отзывов.', 'отзывов other.ru.')),
])
def test_change_of_meaning_still_needs_review(quote, claim):
    assert issues(quote, claim)


def test_incidental_evidence_or_another_source_does_not_verify_the_event():
    assert issues(DEALS, DEAL_CLAIM, primary='Обзор рынка.')
    assert issues(DEALS, DEAL_CLAIM, other_pin=True)


def test_metric_period_and_amount_must_come_from_the_same_fact():
    saved = fact()
    row = dict(label='Выручка, млн руб.', calculation_ids=[], period_label=saved['period']['label'], amount=saved['amount'])
    assert not row_remarks(None, None, None, row, [saved], [])
    other = deepcopy(saved)
    other.update(metric='Капитал', amount=dict(saved['amount'],value='90'))
    assert row_remarks(None, None, None, dict(row,label='Капитал'), [saved,other], [])[0][0]=='row_label_mismatch'


@pytest.mark.parametrize('difference', [dict(subject='Дочерняя компания'),dict(reporting_basis='РСБУ'),dict(nature='forecast')])
def test_equal_values_do_not_resolve_different_scopes(difference):
    saved = fact()
    row = dict(label=saved['metric'], calculation_ids=[], period_label=saved['period']['label'], amount=saved['amount'])
    assert row_remarks(None, None, None, row, [saved,dict(saved,**difference)], [])[0][0]=='row_basis_mismatch'


def test_eighteen_months_are_not_a_year_or_an_instant():
    saved = fact()
    saved.update(metric='Чистая прибыль', period=dict(kind='interval', label='18 мес. по 30.06.23',
                 start='2022-01-01', end='2023-06-30'))
    row = dict(label=saved['metric'], calculation_ids=[], period_label=saved['period']['label'], amount=saved['amount'])
    assert not row_remarks(None, None, None, row, [saved], [])
    for label in ('2022 год', 'На 30.06.2023'):
        assert row_remarks(None, None, None, dict(row,period_label=label), [saved], [])[0][0]=='row_basis_mismatch'


@pytest.mark.parametrize('first',['55','1,6%','(55)'])
def test_shifted_row_is_not_an_available_or_resolvable_year_column(first):
    header='₽ млрд'.ljust(40)+'2020'.ljust(20)+'2021'
    row='Показатель'.ljust(18)+first.ljust(22)+'80'.ljust(20)+'116'
    original=['₽ млрд 2020 2021', 'Показатель '+first+' 80 116']
    layout=header+'\n'+row
    assert not any(t['rows'] for t in tables.page_tables(original, layout))
    loc=dict(row_line=2,year_line=1,column=1,period_line_start=1,period_line_end=1)
    with pytest.raises(tables.Unsupported) as exc:
        tables.resolve(original,layout,loc)
    assert exc.value.code=='ambiguous_link'


def test_rating_in_pdf_does_not_confirm_current_rating():
    quote = 'Кредитный рейтинг АА, стабильный.'
    for claim in ('Компания имеет кредитный рейтинг АА.', 'Действующий рейтинг: АА.',
                  'На дату презентации компания имеет рейтинг АА и сегодня.'):
        assert 'rating_date_unspecified' in issues(quote, claim)
    for claim in ('На дату презентации компания имеет рейтинг АА.', 'В презентации указан рейтинг АА.',
                  'Ранее действовал рейтинг АА.', 'Если компания имеет рейтинг АА, условия меняются.'):
        assert 'rating_date_unspecified' not in issues(quote, claim)
