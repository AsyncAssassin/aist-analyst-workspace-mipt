"""Границы цитаты, количества и имени на нейтральных данных, без модели."""
from copy import deepcopy

import pytest

from app.errors import ApiError
from app.extraction import candidate, locate_anchor
from app.tools import paragraph_remarks
from app.source_cards import label_bound, quoted_durations
from test_d14 import PIN
from test_flow01_03 import placed


def passage(lines, ranges):
    return dict(metric=None, subject='Сигма', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None),
                amount=None, value_text=None, basis=None,
                evidence=[dict(page=1, line_start=start, line_end=end, area='Исходное утверждение')
                          for start, end in ranges])


def test_duration_keeps_its_bound_and_attribute_lookup_does_not_import_previous_objects():
    lines = ['Компания Сигма', 'Заем', '45', 'дней', 'Срок до']
    pages = {1: '\n'.join(lines)}
    layouts = {1: '\n'.join(['Компания Сигма', '', 'Заем', '', 'Срок до', '45', 'дней'])}
    incomplete = passage(lines, [(2, 4)])
    with pytest.raises(ApiError) as refused:
        candidate(PIN, pages, incomplete, layouts)
    assert refused.value.skip_code == 'ambiguous_link'
    complete = passage(lines, [(2, 5)])
    original = deepcopy(complete)
    fact, _ = candidate(PIN, pages, complete, layouts)
    assert any('Срок до' in e['quote'] for e in fact['evidence']) and complete == original
    # Missing geometry is not a demonstrated conflict or a new blanket refusal.
    assert candidate(PIN, pages, incomplete, {})[0]['metric'] == 'Заем'
    # Принадлежность одной карточке не заменяет присутствие ограничения в цитате.
    regions = [dict(label_lines=[2], content_lines=[3, 4, 5])]
    assert label_bound(lines, layouts[1], 2, [2, 3, 4], regions) is False
    assert label_bound(lines, layouts[1], 2, [2, 3, 4, 5], regions) is True
    assert locate_anchor({1:'45\nдней\nНазвание карточки'}, 'Название карточки', {1}, trim=True)['line_start'] == 3


def test_year_of_a_neighboring_caption_cannot_date_a_rating_but_its_own_year_can():
    lines = ['Компания Сигма', 'AA', 'стабильный', '2021', 'рейтинг', 'повышен', 'Прибыль']
    pages = {1:'\n'.join(lines)}
    layout = '\n'.join(['Компания Сигма', '', placed((8,'2021')), placed((8,'Прибыль')),
                        '', '', '', '', placed((8,'AA')), placed((8,'стабильный')),
                        placed((8,'рейтинг')), placed((8,'повышен'))])
    item = passage(lines, [(2,6)])
    with pytest.raises(ApiError) as refused:
        candidate(PIN, pages, item, {1:layout})
    assert refused.value.skip_code == 'ambiguous_link'
    # The same year printed with the rating is a positive control, not an inferred event date.
    own_layout = layout.replace(placed((8,'2021')), '') + '\n' + placed((8,'2021'))
    assert candidate(PIN, pages, item, {1:own_layout})[0]['amount'] is None
    assert candidate(PIN, pages, item, {})[0]['amount'] is None


def remarks(text, quotes):
    fact = dict(fact_id='fact_literal', amount=None, nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None),
                evidence=[dict(quote=q, locator=dict(pdf_page=1)) for q in quotes])
    findings=[]
    result=paragraph_remarks(dict(text=text), [fact], [], {}, findings, dict(block_id='block_literal'))
    return {code for code, _ in result} | {f['code'] for f in findings}


@pytest.mark.parametrize('text,quote', [
    ('Более 17,3 млн человек зарегистрировано.', '17,3+\nмлн чел.\nзарегистрировано'),
    ('Более 8,6 млн человек зарегистрировано.', '8,6+ млн чел. зарегистрировано'),
    ('Выдача от 2 минут.', 'От 2х минут с момента обращения'),
    ('Ожидание от 7 минут.', 'От 7х минут с момента обращения'),
    ('Срок до 45 дней.', 'Срок до\n45\nдней'),
    ('По исследованию Б1 рынок растет.', 'Источник: Исследование Б1, публичные данные.'),
    ('По отчету Р7 рынок растет.', 'Отчет Р7, данные компании.'),
])
def test_confirmed_nonfinancial_spelling(text, quote):
    assert not remarks(text, [quote])


@pytest.mark.parametrize('text,quote', [
    ('Более 8,7 млн человек зарегистрировано.', '8,6+ млн чел. зарегистрировано'),
    ('Более 8,6 тыс. человек зарегистрировано.', '8,6+ млн чел. зарегистрировано'),
    ('До 8,6 млн человек зарегистрировано.', '8,6+ млн чел. зарегистрировано'),
    ('8,6 млн человек зарегистрировано.', '8,6+ млн чел. зарегистрировано'),
    ('Выдача до 7 минут.', 'От 7х минут с момента обращения'),
    ('Выдача 7 минут.', 'От 7х минут с момента обращения'),
    ('Выдача от 8 минут.', 'От 7х минут с момента обращения'),
    ('Срок 45 дней.', 'Срок до\n45\nдней'),
    ('Срок от 45 дней.', 'Срок до\n45\nдней'),
])
def test_quantity_change_keeps_a_finding(text, quote):
    assert 'quantity_mismatch' in remarks(text, [quote])


def test_quantity_parts_cannot_be_combined_across_quotes():
    assert 'quantity_mismatch' in remarks('Более 8,6 млн человек.', ['8,6+', 'млн чел.'])


def test_one_literal_quote_can_keep_the_bound_printed_before_its_out_of_order_value():
    text = '45\nдней\nСрок до'
    layout = 'Срок до\n45\nдней'
    quantities = quoted_durations(text, layout, text)
    assert quantities == [('45', 'дней', 'до')]
    from app.tools import check_source_quantities
    assert not check_source_quantities('до 45 дней', [text], printed={text: quantities}).strip()
    for value in ['45 дней', 'от 45 дней', 'до 46 дней']:
        with pytest.raises(ApiError):
            check_source_quantities(value, [text], printed={text: quantities})
    assert quoted_durations(text, layout, '45\nдней') == []
    assert quoted_durations(text, '', text) == []


def test_pivot_shares_units_without_changing_saved_period_labels():
    from app import note_standard as standard
    from test_note_standard import balances_and_flows
    value = balances_and_flows()
    block = value['payload']['content']['sections'][0]['blocks'][1]
    block['block_id'] = 'block_detailed'
    for fact, row in zip(value['basis']['facts'][2:], block['rows'][2:]):
        fact['period']['label'] = row['period_label'] = 'На ' + fact['period']['end']
    original = deepcopy(value)
    facts = {(f['source']['source_version_id'], f['fact_id']): f for f in value['basis']['facts']}
    findings=[]
    before=standard.financial_table(block,facts,findings=findings)
    assert before[0] == 'млн руб.' and findings == []
    plan=standard.content_plan(value,dict(release_mode='draft'),[])
    rendered=next(data for kind,data in plan if kind=='table')
    assert rendered[0] == ['Показатель', '1Q22 (факт)', '1Q23 (факт)']
    assert rendered[1][-1] == ['Остаток (на конец периода)', '475,00', '542']
    text='\n'.join(standard.expected_paragraphs(plan))
    assert text.count('млн руб.')==1 and 'RUB' not in text
    assert sum('Спектромер' in data for kind,data in plan if kind!='table')==1
    assert standard.financial_table(block,facts,findings=[]) == before and value==original
    for caption in ['Показатели, млн руб.', 'млн руб.']:
        block['caption'] = caption
        plan = standard.content_plan(value, dict(release_mode='draft'), [])
        assert '\n'.join(standard.expected_paragraphs(plan)).count('млн руб.') == 1
    block['rows'].pop()
    remarks=[]
    standard.financial_table(block,facts,findings=remarks)
    assert remarks == []
    standard.content_plan(value,dict(release_mode='draft'),[])
    after=[]
    standard.financial_table(block,facts,findings=after)
    assert after==remarks


def test_mixed_export_preserves_units_sources_basis_and_forecast_status():
    from app import note_standard as standard
    from test_note_standard import sample
    value=sample()
    block=value['payload']['content']['sections'][0]['blocks'][1]
    fact=value['basis']['facts'][1]
    fact.update(nature='forecast',reporting_basis='Прогноз компании')
    fact['source']['source_version_id']=block['rows'][1]['fact_refs'][0]['source_version_id']='sv_second'
    fact['amount']['currency']='USD'
    before=deepcopy(value)
    plan=standard.content_plan(value,dict(release_mode='draft'),[])
    text='\n'.join(standard.expected_paragraphs(plan))
    assert all(part in text for part in ['млн RUB','млн USD','факт','прогноз','Прогноз компании','источник 1','источник 2'])
    assert value==before


@pytest.mark.parametrize('text,quotes,expected', [
    ('Исследование Р8.', ['Исследование Р7'], 'unsupported_numeric_form'),
    ('Исследование Q3.', ['Исследование Q3'], 'unsupported_numeric_form'),
    ('Исследование RUB10.', ['Исследование RUB10'], 'unsupported_numeric_form'),
    ('Исследование Р7: 25% роста.', ['Исследование Р7, 25% роста.'], 'unit_mismatch'),
    ('Исследование Р7: 10 млн руб.', ['Исследование Р7, 10 млн руб.'], 'unit_mismatch'),
    ('Более 8,6 млн человек, доход 10 млн руб.', ['8,6+ млн чел., доход 10 млн руб.'], 'unit_mismatch'),
    ('Более 8,6 млн человек, еще 77.', ['8,6+ млн чел.'], 'unsupported_numbers'),
    ('Исследование Р7: еще 77.', ['Исследование Р7'], 'unsupported_numbers'),
    ('Рейтинг повышен в 2021 году.', ['AA, стабильный. Источник от 27.02.2024.'], 'period_mismatch'),
])
def test_other_numbers_and_periods_are_not_hidden(text, quotes, expected):
    assert expected in remarks(text, quotes)
