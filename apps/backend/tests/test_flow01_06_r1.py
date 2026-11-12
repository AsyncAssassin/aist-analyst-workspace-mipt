"""FLOW-01.06-R1: monetary parentheses and the literal main passage, without external transport."""
from copy import deepcopy

import pytest

from app.errors import ApiError
from app.extraction import APART, candidate, excluded, observation, passage_label, verify_table
from test_d14 import PIN
from test_d10 import PAGES as CHART_PAGES, item as chart_item
from test_flow01_03 import (CAPTION_6, CAPTION_12, LAYOUT, PAGES, ROWS, YEARS_6, YEARS_12,
                            item as table_item, page)
from test_flow01_04 import (COMPANY, LAYOUTS, NORTH, PAGES as CARD_PAGES, SOUTH,
                            passage)


def checked_item(item, pages, layouts=None):
    original = deepcopy(item)
    fact, basis = candidate(PIN, pages, item, layouts)
    assert item == original
    return fact, basis


def test_independent_negative_table_cell_preserves_spelling_and_scale():
    item = table_item('Себестоимость', 4, value='-2450')
    assert verify_table(PAGES, LAYOUT, item)
    fact, basis = checked_item(item, PAGES, LAYOUT)
    assert fact['amount'] == dict(value='-2450', unit='money', currency='RUB', scale='1000000')
    assert fact['raw_text'] == '(2 450)' and fact['period'] == item['period']
    assert any('(2 450)' in e['quote'] for e in fact['evidence']) and basis


@pytest.mark.parametrize('raw,value', [('4 980', '4980'), ('-4 980', '-4980'), ('−4 980', '-4980'),
                                      ('4 980,25', '4980.25'), ('-4 980.25', '-4980.25'),
                                      ('(4 980,25)', '-4980.25')])
def test_existing_numeric_forms_and_parentheses_use_decimal(raw, value):
    rows = deepcopy(ROWS)
    rows[0][1][3] = raw
    pages, layouts = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], rows)
    item = table_item('Выручка от продаж', 4, rows=rows, value=value)
    fact, _ = checked_item(item, pages, layouts)
    assert fact['amount']['value'] == value and fact['raw_text'] == raw


@pytest.mark.parametrize('change', [
    lambda i: i['amount'].update(value='2450'),
    lambda i: i['amount'].update(value='-2451'),
    lambda i: i['amount'].update(value='-2450000000'),
    lambda i: i['amount'].update(currency=None),
    lambda i: i['basis'].update(unit_label='тыс руб.'),
    lambda i: i['table'].update(column=3),
    lambda i: i['period'].update(end='2024-12-31'),
    lambda i: i['period'].update(label='За 6 месяцев 30 июня 2023', start='2023-01-01', end='2023-06-30'),
])
def test_negative_cell_still_refuses_false_amount_unit_column_or_period(change):
    item = table_item('Себестоимость', 4, value='-2450')
    change(item)
    original = deepcopy(item)
    with pytest.raises(ApiError):
        candidate(PIN, PAGES, item, LAYOUT)
    assert item == original


@pytest.mark.parametrize('raw', ['(2 450', '2 450)', '(-2 450)', '(+2 450)', '(−2 450)', '-(2 450)'])
def test_malformed_parentheses_never_confirm_a_number_even_if_printed(raw):
    rows = deepcopy(ROWS)
    rows[1][1][3] = raw
    pages, layouts = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], rows)
    item = table_item('Себестоимость', 4, rows=rows, value='-2450')
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)


@pytest.mark.parametrize('unit', ['money', 'percent'])
def test_parentheses_do_not_extend_admission_outside_a_verified_money_table(unit):
    item = chart_item()
    item.update(value_text='(1 2 3)', amount=dict(value='-123', unit=unit, currency='RUB' if unit=='money' else None))
    if unit == 'percent':
        item['basis'] = None
    pages = {1: CHART_PAGES[1].replace('1 2 3\n', '(1 2 3)\n')}
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, item)
    assert error.value.skip_code == 'number_mismatch'


IDENTITY = 'Данный отчет содержит сведения о «Северный завод» (далее «Общество»).'
MAIN = ['Производство оборудования', 'Выпускаются насосы для промышленных объектов.']
NOTE = 'Примечание: сведения не проверялись независимо.'
SOURCE = 'Источник: данные общества'
TEXT = {1: IDENTITY, 2: '\n'.join([*MAIN, SOURCE, NOTE])}
ISSUER = dict(page=1, line_start=1, line_end=1, area='Основной фрагмент')
TOPIC = dict(page=2, line_start=1, line_end=2, area='Идентификация эмитента')
REFERENCES = [ISSUER, dict(page=2, line_start=3, line_end=3, area='Основной фрагмент'),
              dict(page=2, line_start=4, line_end=4, area='Основной фрагмент')]


def qualitative(evidence, metric=None):
    return dict(metric=metric, subject='Северный завод', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None),
                amount=None, value_text=None, basis=None, evidence=evidence)


@pytest.mark.parametrize('evidence', [[TOPIC, *REFERENCES], [*REFERENCES, TOPIC],
                                      [REFERENCES[2], TOPIC, ISSUER, REFERENCES[1]]])
def test_main_passage_is_independent_of_auxiliary_order_and_area(evidence):
    item = qualitative(evidence)
    fact, _ = checked_item(item, TEXT)
    assert fact['metric'] == passage_label(TEXT, item) == observation(TEXT, item)['metric'] == MAIN[0]
    assert fact['raw_text'] == '\n'.join(MAIN)
    assert {IDENTITY, SOURCE, NOTE, '\n'.join(MAIN)} <= {e['quote'] for e in fact['evidence']}


@pytest.mark.parametrize('locator,expected', [(ISSUER, IDENTITY), (REFERENCES[2], NOTE), (REFERENCES[1], SOURCE)])
def test_identity_source_and_limitation_can_be_the_fact_itself(locator, expected):
    fact, _ = checked_item(qualitative([locator]), TEXT)
    assert fact['metric'] == expected and fact['raw_text'] == expected


def test_explicit_metric_keeps_its_passage_and_wrong_metric_is_not_repaired():
    item = qualitative([ISSUER, TOPIC], metric=MAIN[0])
    fact, _ = checked_item(item, TEXT)
    assert fact['metric'] == MAIN[0] and fact['raw_text'] == '\n'.join(MAIN)
    fact, _ = checked_item(qualitative([ISSUER, TOPIC], metric=IDENTITY), TEXT)
    assert fact['metric'] == IDENTITY and fact['raw_text'] == IDENTITY
    with pytest.raises(ApiError) as error:
        candidate(PIN, TEXT, qualitative([ISSUER, TOPIC], metric='Придуманный показатель'))
    assert error.value.skip_code == 'ambiguous_link'


@pytest.mark.parametrize('first', ['Северный завод расширяет ассортимент.',
                                 IDENTITY + ' Завод расширяет ассортимент.',
                                 SOURCE + '\nСеверный завод расширяет ассортимент.'])
def test_unproven_auxiliary_role_does_not_discard_substantive_text(first):
    pages = {**TEXT, 1: first}
    reference = dict(ISSUER, line_end=len(first.splitlines()))
    fact, _ = checked_item(qualitative([reference, TOPIC]), pages)
    assert fact['metric'] == first.splitlines()[0] and fact['raw_text'] == first


@pytest.mark.parametrize('identity_first', [True, False])
def test_disjoint_card_mixing_keeps_its_diagnostic_topic_with_an_identity_reference(identity_first):
    pages = CARD_PAGES['the name after its lines']
    wrong = passage(pages[1], [NORTH[-1], *SOUTH[:-1]])
    title = dict(wrong['evidence'][0], line_end=wrong['evidence'][0]['line_start'])
    foreign = dict(wrong['evidence'][0], line_start=title['line_start'] + 2)
    issuer = dict(page=1, line_start=1, line_end=1, area='Источник')
    wrong['evidence'] = [issuer, title, foreign] if identity_first else [title, foreign, issuer]
    original = deepcopy(wrong)
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, wrong, LAYOUTS)
    diagnostic = excluded(pages, 1, wrong, error.value)
    assert diagnostic['code'] == 'ambiguous_link' and diagnostic['message'] == APART
    assert diagnostic['metric'] == diagnostic['observation']['metric'] == NORTH[-1]
    assert wrong == original
    wrong['evidence'].append(dict(issuer, line_end=999))
    with pytest.raises(ApiError) as invalid:
        candidate(PIN, pages, wrong, LAYOUTS)
    assert not hasattr(invalid.value, 'skip_code')


def test_related_disjoint_ranges_keep_the_main_topic_without_explicit_metric():
    pages = CARD_PAGES['the name after its lines']
    title = passage(pages[1], [NORTH[-1]])['evidence'][0]
    year = passage(pages[1], [NORTH[2]])['evidence'][0]
    issuer = dict(page=1, line_start=1, line_end=1, area='Источник')
    for references in ([issuer, title, year], [title, year, issuer]):
        item = dict(passage(pages[1], [NORTH[-1]]), evidence=references)
        fact, _ = checked_item(item, pages, LAYOUTS)
        assert fact['metric'] == NORTH[-1] and fact['raw_text'] == NORTH[-1]
        assert {NORTH[-1], NORTH[2], COMPANY} <= {e['quote'] for e in fact['evidence']}
