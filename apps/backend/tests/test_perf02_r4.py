"""Metric abbreviations, complete save feedback, year-end balances and shared table units."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.contracts import SCHEMA, validate_definition
from app.errors import ApiError
from app.note_standard import financial_table, shared_unit, shared_unit_once
from app.tools import (MESSAGE_LIMIT, adopt_period_label, block_locations, check_periods, check_units,
                       feedback, number_tokens, resolve_basis)
from app.versions import review

VERSION = 'sv_r4'
PIN = dict(source_id='src_r4', source_version_id=VERSION, sha256='0' * 64)
RUN = dict(id='run_r4', project_id='prj_r4', owner_id='usr_r4',
           input=dict(source_versions=[PIN], skill_version_id='skill_analytical-note-v3'))
PERCENT = dict(value='11.6', unit='percent', scale='1', currency=None)
MONEY = dict(value='3658', unit='money', scale='1000000', currency='RUB')
YEAR_END = dict(kind='instant', start='2022-12-31', end='2022-12-31', label='2022')
QUARTER_END = dict(kind='instant', start='2023-03-31', end='2023-03-31', label='1 кв. 2023г.')


def saved_fact(fact_id, metric, amount, period, *, quote='цитата', nature='reported_actual'):
    return dict(fact_id=fact_id, source=PIN, extraction_version='pdf-lines.v7', metric=metric, subject='Компания',
                reporting_basis='Данные презентации', nature=nature, period=period, amount=amount, raw_text=quote,
                evidence=[dict(evidence_id='ev_' + fact_id, source=PIN, quote=quote,
                               locator=dict(pdf_page=1, area='Карточка', bbox_pt=None, coordinate_system='pdf_points_top_left'))],
                limitations=[])


FACTS = {
    'fact_roa': saved_fact('fact_roa', 'ROA, %', PERCENT, dict(kind='interval', start='2023-01-01', end='2023-03-31', label='1Q23')),
    'fact_roe': saved_fact('fact_roe', 'ROE, %', dict(PERCENT, value='27.3'), dict(kind='interval', start='2023-01-01', end='2023-03-31', label='1Q23')),
    'fact_capital_2022': saved_fact('fact_capital_2022', 'Собственный капитал', dict(MONEY, value='3216'), YEAR_END),
    'fact_capital_1q23': saved_fact('fact_capital_1q23', 'Собственный капитал', MONEY, QUARTER_END),
    'fact_funding': saved_fact('fact_funding', 'Привлеченные средства', dict(MONEY, value='4392'),
                               dict(kind='instant', start='2023-03-31', end='2023-03-31', label='на 31.03.2023')),
    'fact_quality': saved_fact('fact_quality', 'Клиенты', None, dict(kind='unknown', start=None, end=None, label='Не указано'),
                               quote='Прокатные фирмы сдают приборы для замера шума', nature='source_statement'),
}


def ref(fact_id):
    return dict(source_version_id=VERSION, fact_id=fact_id)


def lookup(service, c, r, reference):
    fact = FACTS.get(reference['fact_id'])
    if fact is None:
        error = ApiError('resource_not_found')
        error.body['error']['message'] = 'Факт ' + reference['fact_id'] + ' отсутствует в разрешенной версии ' + VERSION + '.'
        error.missing_basis = True
        raise error
    return {'data': fact}


def paragraph(block_id, text, *refs):
    return dict(block_id=block_id, kind='paragraph', claim=dict(text=text, origin='source_fact', fact_refs=[ref(x) for x in refs], calculation_ids=[]))


def row(row_id, label, period_label, amount, *refs):
    return dict(row_id=row_id, label=label, period_label=period_label, amount=deepcopy(amount), fact_refs=[ref(x) for x in refs], calculation_ids=[])


def note(sections):
    return dict(result_type='analytical_note', schema_version='1.0.0',
                content=dict(title='Компания: информационная справка', audience='Аналитик', sections=sections))


def reviewed(payload, **kwargs):
    with patch('app.tools.resolve_fact', side_effect=lookup), patch('app.tools.row', return_value=None):
        return review(SimpleNamespace(service=None), None, RUN, payload, **kwargs)


@pytest.mark.parametrize('text', ['ROA 11,6%', 'ROE 27,3%, ROA 11,6%', 'NPS 11,6%', 'ROA: 11,6 %', 'ROA 11,6 процента'])
def test_metric_abbreviation_before_a_percent_is_not_a_currency(text):
    remaining = check_units(text, [PERCENT, dict(PERCENT, value='27.3')])
    assert not number_tokens(remaining)


@pytest.mark.parametrize('text,amounts', [
    ('ROA 11,6%', []),
    ('ROA 11,6%', [MONEY]),
    ('USD 11,6', [dict(MONEY, value='11.6', scale='1')]),
    ('11,6 млн руб.', [PERCENT]),
    ('11,6 тыс. руб.', [dict(MONEY, value='11.6')]),
    ('-11,6%', [PERCENT]),
    ('11,7%', [PERCENT]),
    ('EUR 3658 млн', [MONEY]),
    ('₽ 3658 тыс.', [MONEY]),
])
def test_percent_and_money_substitutions_stay_rejected(text, amounts):
    with pytest.raises(ApiError) as refusal:
        check_units(text, amounts)
    message = refusal.value.body['error']['message']
    assert '«' + ' '.join(text.split()) + '»' in message or 'не совпадают' in message


def test_units_message_names_every_mismatched_fragment():
    with pytest.raises(ApiError) as refusal:
        check_units('Платеж до 17 тыс. руб., или около 15% от дохода; ROA 11,6%.', [PERCENT])
    message = refusal.value.body['error']['message']
    assert '«17 тыс. руб.»' in message and '«15%»' in message and '11,6' not in message.split('не совпадают')[0]


def test_year_end_wording_matches_the_balance_date_only():
    assert not number_tokens(check_periods('На конец 2022 года собственный капитал', [YEAR_END]))
    assert not number_tokens(check_periods('по состоянию на конец 2022 г. капитал', [YEAR_END]))
    for text, periods in [('За 2022 год собственный капитал', [YEAR_END]),
                          ('На конец 2021 года собственный капитал', [YEAR_END]),
                          ('На конец 2022 года капитал', [QUARTER_END])]:
        with pytest.raises(ApiError) as refusal:
            check_periods(text, periods)
        assert '2022' in refusal.value.body['error']['message'] or '2021' in refusal.value.body['error']['message']


def test_period_check_reports_every_mismatch_of_a_paragraph_at_once():
    with pytest.raises(ApiError) as refusal:
        check_periods('Рост за 2025 год к 2022 году и на 30.06.2024 относительно 2Q24.', [QUARTER_END])
    message = refusal.value.body['error']['message']
    for fragment in ('2025-01-01 по 2025-12-31', '2022-01-01 по 2022-12-31', '«30.06.2024»', '2024-04-01 по 2024-06-30'):
        assert fragment in message
    assert message.count('Квартал не подтверждает годовой показатель') == 1


@pytest.mark.parametrize('label,expected', [
    ('на 31.03.2023', '1 кв. 2023г.'), ('31.03.2023', '1 кв. 2023г.'), ('по состоянию на 31.03.2023 г.', '1 кв. 2023г.'),
    ('на 31.12.2022', 'на 31.12.2022'), ('на 30.03.2023', 'на 30.03.2023'), ('1Q23', '1Q23'), ('1 кв. 2023г.', '1 кв. 2023г.'),
])
def test_row_label_adopts_the_fact_label_only_for_the_same_balance_date(label, expected):
    table_row = row('row_capital', 'Собственный капитал', label, MONEY, 'fact_capital_1q23')
    adopt_period_label(table_row, [FACTS['fact_capital_1q23']])
    assert table_row['period_label'] == expected


@pytest.mark.parametrize('change', ['amount', 'scale', 'two_facts', 'calculation', 'interval'])
def test_row_label_is_not_rewritten_for_a_different_sum_basis_or_flow(change):
    table_row = row('row_capital', 'Собственный капитал', 'на 31.03.2023', MONEY, 'fact_capital_1q23')
    facts = [FACTS['fact_capital_1q23']]
    if change == 'amount':
        table_row['amount']['value'] = '2478'
    elif change == 'scale':
        table_row['amount']['scale'] = '1000'
    elif change == 'two_facts':
        facts = facts + [FACTS['fact_funding']]
    elif change == 'calculation':
        table_row['calculation_ids'] = ['calc_growth']
    else:
        facts = [dict(FACTS['fact_capital_1q23'], period=dict(kind='interval', start='2023-01-01', end='2023-03-31', label='1 кв. 2023г.'))]
    adopt_period_label(table_row, facts)
    assert table_row['period_label'] == 'на 31.03.2023'


def test_saved_r3_shape_is_accepted_after_normalization():
    payload = note([dict(section_id='sec_fin', title='Финансирование', blocks=[
        paragraph('block_roa', 'По аудированной отчетности за 1Q23 ROE составил 27,3%, ROA 11,6%.', 'fact_roe', 'fact_roa'),
        paragraph('block_capital', 'На 31.03.2023 собственный капитал составил 3658 млн руб. (на конец 2022 года 3216 млн руб.).',
                  'fact_capital_1q23', 'fact_capital_2022'),
        dict(block_id='block_table', kind='table', caption='Структура финансирования на 31.03.2023, млн руб.', rows=[
            row('row_capital', 'Собственный капитал, млн руб.', 'на 31.03.2023', MONEY, 'fact_capital_1q23'),
            row('row_funding', 'Привлеченные средства (млн руб.)', 'на 31.03.2023', dict(MONEY, value='4392'), 'fact_funding')])])])
    before = deepcopy(payload)
    normalized, basis, findings, errors = reviewed(payload)
    assert errors == [] and findings == []
    assert payload == before, 'the tool arguments keep their fingerprint'
    table = normalized['content']['sections'][0]['blocks'][2]
    assert table['caption'] == 'Структура финансирования на 31.03.2023'
    assert [r['label'] for r in table['rows']] == ['Собственный капитал', 'Привлеченные средства']
    assert [r['period_label'] for r in table['rows']] == ['1 кв. 2023г.', 'на 31.03.2023']
    assert {f['fact_id'] for f in basis['facts']} == {'fact_roe', 'fact_roa', 'fact_capital_1q23', 'fact_capital_2022', 'fact_funding'}


def test_one_attempt_collects_every_independent_remark_with_recognizable_places():
    FACTS['fact_market'] = saved_fact('fact_market', 'Рынок', None, dict(kind='unknown', start=None, end=None, label='Не указано'),
                                      quote='Активные займы: более 18 тыс. шт. На контрольном рынке около 7 тыс. шт.', nature='source_statement')
    payload = note([
        dict(section_id='sec_one', title='Продукт и клиенты', blocks=[
            paragraph('block_quality', 'Прокатные фирмы сдают приборы для замера шума; 30% предпринимателей, NPS 81%.', 'fact_quality'),
            paragraph('block_missing', 'Выручка за 2022 год составила 3157 млн руб.', 'fact_absent'),
        ]),
        dict(section_id='sec_two', title='Стратегия и финансирование', blocks=[
            paragraph('block_capital', 'Собственный капитал 3658 млн руб. увеличился на 16,2% к 2022 году; погашение в мае 2023 года.',
                      'fact_capital_1q23', 'fact_capital_2022'),
            dict(block_id='block_table', kind='table', caption='Структура', rows=[
                row('row_wrong', 'Собственный капитал', 'на 31.03.2023', dict(MONEY, value='2478'), 'fact_capital_1q23'),
                row('row_absent', 'Облигации', 'на 31.03.2023', dict(MONEY, value='943'), 'fact_absent')]),
            paragraph('block_form', 'Активные займы: более 18 тыс. шт. и около 7 тыс. шт.', 'fact_market'),
        ]),
    ])
    normalized, basis, findings, errors = reviewed(payload)
    quality = 'Раздел 1 «Продукт и клиенты», блок 1, абзац «Прокатные фирмы сдают приборы для замера …» (block_quality)'
    missing = 'Раздел 1 «Продукт и клиенты», блок 2, абзац «Выручка за 2022 год составила 3157 млн р…» (block_missing)'
    capital = 'Раздел 2 «Стратегия и финансирование», блок 1, абзац «Собственный капитал 3658 млн руб. увелич…» (block_capital)'
    table = 'Раздел 2 «Стратегия и финансирование», блок 2, таблица «Структура» (block_table)'
    absent = 'Раздел 2 «Стратегия и финансирование», блок 2, таблица «Структура», строка 2 «Облигации» (row_absent)'
    # DRAFT-01: only the missing basis refuses; every content mismatch is a blocking finding with its place.
    assert [location for location, _ in errors] == [missing, missing, absent, absent]
    refusals = [message for _, message in errors]
    assert refusals[0].startswith('Факт fact_absent отсутствует') and refusals[1] == 'Числовая проверка этого места не выполнена: основание отсутствует.'
    assert refusals[2] == refusals[0] and refusals[3] == refusals[1]
    assert [(f['code'], f['block_id']) for f in findings] == [('row_basis_mismatch', 'block_table')]
    message = findings[0]['message']
    assert message == ('строка 1 «Собственный капитал» (row_wrong): Сумма 2478 млн RUB и период «на 31.03.2023» '
                       'не совпадают с основанием. Основание: 3658 млн RUB, период «1 кв. 2023г.».')
    assert findings[0]['severity'] == 'error'
    text = feedback(errors, findings, block_locations(normalized))
    assert text.split('\n') == [location + ': ' + message for location, message in errors] + [table + ': ' + message]
    assert len(text) <= MESSAGE_LIMIT
    validate_definition('Error', dict(code='validation_failed', message=text, retryable=False, run_id=None, call_id=None,
                                      tool_call_id=None, expected_revision=None, current_revision=None))


def test_qualitative_quote_percent_is_a_finding_and_authorization_refusals_are_unchanged():
    payload = note([dict(section_id='sec_one', title='Клиенты', blocks=[
        paragraph('block_quality', 'Доля повторных клиентов 40%.', 'fact_quality')])])
    _, _, findings, errors = reviewed(payload)
    assert errors == findings == []
    with patch('app.tools.resolve_fact', side_effect=lookup), patch('app.tools.row', return_value=None):
        assert resolve_basis(SimpleNamespace(service=None), None, RUN, payload)['facts'] == [FACTS['fact_quality']]
    # Короткий ответ сохраняет прежнюю проверку того же числового утверждения.
    answer = dict(result_type='evidence_answer', schema_version='1.0.0', content=dict(
        claims=[payload['content']['sections'][0]['blocks'][0]['claim']], missing_information=[]))
    with patch('app.tools.resolve_fact', side_effect=lookup), pytest.raises(ApiError):
        resolve_basis(SimpleNamespace(service=None), None, RUN, answer)

    def foreign(service, c, r, reference):
        raise ApiError('resource_not_found')
    with patch('app.tools.resolve_fact', side_effect=foreign), pytest.raises(ApiError) as refusal:
        resolve_basis(SimpleNamespace(service=None), None, RUN, payload)
    assert refusal.value.code == 'resource_not_found'


def test_feedback_stays_within_the_contract_limit_without_losing_order():
    errors = [(f'Раздел 1 «Длинный», блок {i}, абзац «Текст» (block_{i})', 'Числа 1 не имеют сохраненного основания.') for i in range(1, 400)]
    text = feedback(errors)
    assert len(text) <= MESSAGE_LIMIT and text.endswith('сохрани снова.')
    assert text.startswith('Раздел 1 «Длинный», блок 1, абзац «Текст» (block_1): Числа 1')
    assert MESSAGE_LIMIT == SCHEMA['$defs']['Error']['properties']['message']['maxLength'] == 20000


def homogeneous_block():
    return dict(block_id='block_table', kind='table', caption='Финансовые показатели, млн руб.', rows=[
        row('row_1', 'Выручка, млн руб.', '2022', dict(MONEY, value='3157'), 'fact_a'),
        row('row_2', 'Чистая прибыль (млн руб.)', '2022', dict(MONEY, value='403'), 'fact_b'),
        row('row_3', 'Портфель на 31.12.2022 в млн руб.', '2022', dict(MONEY, value='4423'), 'fact_c'),
        row('row_4', 'Продажи млн рублей', '2022', dict(MONEY, value='813'), 'fact_d')])


def test_shared_unit_repeat_is_removed_but_meaningful_captions_stay():
    block = homogeneous_block()
    assert shared_unit(block) == 'млн руб.'
    shared_unit_once(block)
    assert block['caption'] == 'Финансовые показатели'
    assert [r['label'] for r in block['rows']] == ['Выручка', 'Чистая прибыль', 'Портфель на 31.12.2022', 'Продажи']
    kept = homogeneous_block()
    kept['caption'] = 'Показатели, млн руб., по МСФО'
    kept['rows'][0]['label'] = 'Выручка, тыс. руб.'
    kept['rows'][1]['label'] = 'млн руб.'
    shared_unit_once(kept)
    assert kept['caption'] == 'Показатели, млн руб., по МСФО'
    assert [r['label'] for r in kept['rows']][:2] == ['Выручка, тыс. руб.', 'млн руб.']


def test_mixed_units_and_colliding_labels_are_not_declared_homogeneous():
    mixed = homogeneous_block()
    mixed['rows'][1]['amount'] = PERCENT
    assert shared_unit(mixed) is None
    original = deepcopy(mixed)
    shared_unit_once(mixed)
    assert mixed == original
    colliding = homogeneous_block()
    colliding['rows'][1]['label'] = 'Выручка'
    original = deepcopy(colliding)
    shared_unit_once(colliding)
    assert colliding == original


def test_one_date_table_uses_canonical_bounds_and_allows_missing_cells():
    facts = {(VERSION, 'fact_capital_1q23'): FACTS['fact_capital_1q23'], (VERSION, 'fact_funding'): FACTS['fact_funding']}
    block = dict(block_id='block_table', kind='table', caption='Структура финансирования', rows=[
        row('row_capital', 'Собственный капитал', '1 кв. 2023г.', MONEY, 'fact_capital_1q23'),
        row('row_funding', 'Привлеченные средства', 'на 31.03.2023', dict(MONEY, value='4392'), 'fact_funding')])
    findings = []
    unit, (headers, rows) = financial_table(block, facts, findings=findings)
    assert unit == 'млн руб.' and len(headers) == 2 and len(rows) == 2 and findings == []
    incomplete = deepcopy(block)
    incomplete['rows'].append(row('row_more', 'Собственный капитал', '2022', dict(MONEY, value='3216'), 'fact_capital_2022'))
    facts[(VERSION, 'fact_capital_2022')] = FACTS['fact_capital_2022']
    financial_table(incomplete, facts, findings=findings)
    assert findings == []
