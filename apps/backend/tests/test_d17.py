"""Explicit quarter columns retain source, unit and period checks."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.errors import ApiError
from app.extraction import candidate
from app.contracts import validate_definition
from app.domain import growth
from app.tools import check_periods, resolve_basis
from test_d11 import note
from test_d14 import PIN
from test_domain import BASIS, calculation, fact

LAYOUT = {1: 'Synthetic Company\nАудированная отчетность\n'
             'Год                         2023            1Q24           1Q25    Рост,%\n'
             'Выручка, млн  ₽             4 321            987          1 123     +14%'}
PAGES = {1: 'Synthetic Company\nАудированная отчетность\n'
            'Год 2023 1Q24 1Q25Рост,%\nВыручка, млн ₽ 4 321 987 1 123 +14%'}
ITEM = dict(metric='Выручка, млн ₽', subject='Synthetic Company',
            reporting_basis='Аудированная отчетность', nature='reported_actual',
            period=dict(kind='interval', label='1Q25', start='2025-01-01', end='2025-03-31'),
            amount=dict(value='1123', unit='money', currency='RUB'), value_text='1 123',
            evidence=[dict(page=1, line_start=1, line_end=4, area='Синтетические данные')],
            basis=dict(page=1, metric_label='Выручка, млн ₽', unit_label='млн ₽',
                       reporting_label='Аудированная отчетность'),
            table=dict(page=1, row_line=4, year_line=3, period_line_start=3,
                       period_line_end=3, column=3))


def test_quarter_cell_preserves_original_amount_period_and_input():
    before = deepcopy(ITEM)
    fact, basis = candidate(PIN, PAGES, ITEM, LAYOUT)
    assert fact['amount'] == dict(value='1123', unit='money', currency='RUB', scale='1000000')
    assert fact['period'] == ITEM['period']
    assert PIN['sha256'] in basis['definition']
    assert ITEM == before


@pytest.mark.parametrize('column,label,closing,value,raw', [
    (1, '2023', '2023-12-31', '4321', '4 321'),
    (3, '1Q25', '2025-03-31', '1123', '1 123'),
])
def test_balance_uses_selected_column_closing_date(column, label, closing, value, raw):
    pages = {1: PAGES[1].replace('Выручка', 'Остаток')}
    layouts = {1: LAYOUT[1].replace('Выручка', 'Остаток')}
    item = deepcopy(ITEM)
    item['metric'] = item['basis']['metric_label'] = 'Остаток, млн ₽'
    item['period'] = dict(kind='instant', label=label, start=closing, end=closing)
    item['table']['column'] = column
    item['amount']['value'] = value
    item['value_text'] = raw
    original = deepcopy(item)
    saved, _ = candidate(PIN, pages, item, layouts)
    assert saved['period'] == item['period'] and saved['raw_text'] == raw
    assert saved['amount'] == {**item['amount'], 'scale': '1000000'}
    assert all(e['source'] == PIN for e in saved['evidence'])
    assert item == original
    for changes in ({'start': '2025-03-30', 'end': '2025-03-30'},
                    {'start': '2025-01-01'}, {'label': '1Q24'},
                    {'start': '2025-12-31', 'end': '2025-12-31'}):
        wrong = deepcopy(item)
        wrong['period'].update(changes)
        with pytest.raises(ApiError):
            candidate(PIN, pages, wrong, layouts)
    item['table']['column'] = 2
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)


def test_first_table_column_after_separate_currency_symbol():
    item = deepcopy(ITEM)
    item['table']['column'] = 1
    item['period'] = dict(kind='interval', label='2023', start='2023-01-01', end='2023-12-31')
    item['amount']['value'] = '4321'
    item['value_text'] = '4 321'
    before = deepcopy(item)

    fact, _ = candidate(PIN, PAGES, item, LAYOUT)

    assert fact['amount'] == dict(value='4321', unit='money', currency='RUB', scale='1000000')
    assert fact['period'] == item['period']
    assert item == before
    item['table']['column'] = 2
    with pytest.raises(ApiError):
        candidate(PIN, PAGES, item, LAYOUT)


def test_metric_can_omit_unit_when_complete_row_heading_is_verified():
    item = deepcopy(ITEM)
    item['metric'] = 'Выручка'
    fact, _ = candidate(PIN, PAGES, item, LAYOUT)
    assert fact['metric'] == 'Выручка'


def test_short_metric_and_separate_verified_unit_match_complete_table_label():
    item = deepcopy(ITEM)
    item['metric'] = item['basis']['metric_label'] = 'Выручка'
    fact, _ = candidate(PIN, PAGES, item, LAYOUT)
    assert fact['amount']['value'] == '1123'
    item['basis']['unit_label'] = 'тыс ₽'
    with pytest.raises(ApiError):
        candidate(PIN, PAGES, item, LAYOUT)


def test_percent_table_label_can_include_its_percent_unit():
    pages = {1:PAGES[1].replace('Выручка, млн ₽ 4 321 987 1 123 +14%', 'Маржа, % 43,2% 9,8% 11,2% +1,4%')}
    layouts = {1:LAYOUT[1].replace('Выручка, млн  ₽             4 321            987          1 123     +14%',
                                 'Маржа, %                   43,2%           9,8%         11,2%     +1,4%')}
    item = deepcopy(ITEM)
    item.update(metric='Маржа', basis=None, value_text='11,2%',
                amount=dict(value='11.2', unit='percent', currency=None))
    fact, _ = candidate(PIN, pages, item, layouts)
    assert fact['amount']['value'] == '11.2'
    item['metric'] = 'Другая маржа'
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)


@pytest.mark.parametrize('mutation', [
    lambda i: i['table'].update(column=2),
    lambda i: i['period'].update(label='2025', end='2025-12-31'),
    lambda i: i['period'].update(label='1Q24'),
    lambda i: i['period'].update(start='2025-02-01'),
    lambda i: i['period'].update(end='2025-03-30'),
    lambda i: i.update(value_text='123', amount=dict(value='123', unit='money', currency='RUB')),
    lambda i: i['amount'].update(currency='USD'),
    lambda i: i['basis'].update(unit_label='тыс ₽'),
    lambda i: i.update(metric='Чистая прибыль'),
])
def test_quarter_table_rejects_wrong_column_period_unit_or_metric(mutation):
    item = deepcopy(ITEM)
    mutation(item)
    with pytest.raises(ApiError):
        candidate(PIN, PAGES, item, LAYOUT)


def test_literal_forecast_label_remains_a_qualification_not_an_annual_interval():
    unknown=dict(kind='unknown', label='2025 году в целом', start=None, end=None)
    periods=[ITEM['period'], unknown]
    quote='Заявленные темпы роста по 2025 году в целом.'
    assert '2025' not in check_periods(quote, periods, source_quotes=[quote])
    with pytest.raises(ApiError):
        check_periods('Выручка за 2025 год.', periods, source_quotes=[quote])


@pytest.mark.parametrize('quote', ['', 'Заявленные темпы роста по 2024 году в целом.'])
def test_unknown_label_requires_literal_source_evidence(quote):
    period=dict(kind='unknown', label='2025 году в целом', start=None, end=None)
    with pytest.raises(ApiError):
        check_periods('Заявленные темпы роста по 2025 году в целом.', [period], source_quotes=[quote])


def test_quarter_in_qualitative_quote_does_not_establish_other_periods():
    period=dict(kind='unknown',label='2025 году в целом',start=None,end=None)
    quote='Фактические показатели 1Q25 подтверждают заявленные темпы роста по 2025 году в целом.'
    remaining=check_periods(quote,[period],source_quotes=[quote])
    assert '1Q25' not in remaining and '2025' not in remaining
    for unsupported in ('Показатели 2Q25.', 'Показатели 1Q24.', 'Выручка за 2025 год.'):
        with pytest.raises(ApiError):
            check_periods(unsupported,[period],source_quotes=[quote])


@pytest.fixture
def period_facts(monkeypatch):
    previous = fact('475', 2022, 'fact_previous')
    current = fact('542', 2023, 'fact_current')
    previous['period']['label'] = '1Q22'
    current['period']['label'] = '1Q23'
    qualification = fact(fid='fact_qualification')
    qualification.update(amount=None, nature='source_statement',
                         period=dict(kind='unknown', label='2023 году в целом', start=None, end=None))
    qualification['evidence'][0]['quote'] = (
        'Фактические показатели 1Q23 подтверждают заявленные темпы роста по 2023 году в целом.')
    facts = {f['fact_id']: f for f in (previous, current, qualification)}

    def resolve(service, connection, run, ref):
        saved = facts[ref['fact_id']]
        assert ref['source_version_id'] == saved['source']['source_version_id']
        return {'data': saved}

    monkeypatch.setattr('app.tools.resolve_fact', resolve)
    return facts


def period_payload(text, fact_ids, result_type, human=False):
    payload = note('human_editorial' if human else 'source_fact', text)
    claim = payload['content']['sections'][0]['blocks'][0]['claim']
    claim['fact_refs'] = [dict(source_version_id='sv_one', fact_id=fid) for fid in fact_ids]
    if result_type == 'evidence_answer':
        payload.update(result_type=result_type, content=dict(claims=[claim], missing_information=[]))
    return validate_definition('ResultPayload', payload)


@pytest.mark.parametrize('result_type,human', [
    ('analytical_note', False), ('analytical_note', True), ('evidence_answer', False),
])
@pytest.mark.parametrize('text,fact_id', [
    ('Выручка в 2023 году в целом составила 542 млн рублей.', 'fact_current'),
    ('Выручка за 1Q23 составила 475 млн рублей.', 'fact_previous'),
])
def test_answer_rejects_wrong_prose_period_while_note_keeps_unchecked_text(period_facts, result_type, human, text, fact_id):
    payload = period_payload(text, [fact_id, 'fact_qualification'], result_type, human)
    before = deepcopy(payload)
    if result_type == 'evidence_answer':
        with pytest.raises(ApiError, match='validation_failed'):
            resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human)
    else:
        findings = []
        basis = resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human, findings=findings)
        assert findings == []
        assert {f['fact_id'] for f in basis['facts']} == {fact_id, 'fact_qualification'}
    assert payload == before


@pytest.mark.parametrize('period', [
    dict(kind='interval', label='2023 год', start='2023-01-01', end='2023-12-31'),
    dict(kind='instant', label='31 декабря 2023 года', start='2023-12-31', end='2023-12-31'),
])
def test_qualitative_dates_are_not_financial_periods(period_facts, period):
    period_facts['fact_qualification']['period'] = period
    text = 'Выручка на 2023-12-31 составила 542 млн рублей.'
    payload = period_payload(text, ['fact_current', 'fact_qualification'], 'evidence_answer')
    with pytest.raises(ApiError, match='validation_failed'):
        resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload)


def test_event_month_in_qualification_cannot_change_financial_period(period_facts):
    qualification = period_facts['fact_qualification']
    qualification['evidence'][0]['quote'] = 'Выпуск погашен в мае 2023 года.'
    payload = period_payload('Выручка в мае 2023 года составила 542 млн рублей.',
                             ['fact_current', 'fact_qualification'], 'evidence_answer')
    with pytest.raises(ApiError):
        resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload)


@pytest.mark.parametrize('result_type', ['analytical_note', 'evidence_answer'])
@pytest.mark.parametrize('text,fact_ids', [
    ('Выручка за 1Q23 составила 542 млн рублей.', ['fact_current', 'fact_qualification']),
    ('Выручка за 1Q22 составила 475 млн рублей.', ['fact_previous', 'fact_qualification']),
    ('Фактические показатели 1Q23 подтверждают заявленные темпы роста по 2023 году в целом.',
     ['fact_qualification']),
])
def test_financial_periods_and_separate_qualification_remain_valid(period_facts, result_type, text, fact_ids):
    payload = period_payload(text, fact_ids, result_type)
    findings = []
    basis = resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, findings=findings)
    assert not findings
    assert {f['fact_id'] for f in basis['facts']} == set(fact_ids)


@pytest.mark.parametrize('text,valid', [
    ('Рост в 2023 году в целом составил 14,11%.', False),
    ('Рост за 1Q23 к 1Q22 составил 14,11%.', True),
])
def test_calculation_periods_cannot_be_extended_by_qualification(period_facts, monkeypatch, text, valid):
    args, _ = calculation()
    records = {}
    for item in args['inputs']:
        fact_id = 'fact_' + item['name']
        item['fact']['fact_id'] = fact_id
        records[item['name']] = dict(data=period_facts[fact_id], calculation_basis=BASIS)
    saved_calculation = growth('calc_period', args, records)
    monkeypatch.setattr('app.tools.row', lambda *args, **kwargs: {'reply': {'result': saved_calculation}})
    payload = period_payload(text, ['fact_qualification'], 'evidence_answer')
    payload['content']['claims'][0]['calculation_ids'] = ['calc_period']
    run = dict(id='run_period', project_id='project_period', owner_id='user_period')
    if valid:
        findings = []
        basis = resolve_basis(SimpleNamespace(service=None), None, run, payload, findings=findings)
        assert not findings
        assert basis['calculations'] == [saved_calculation]
    else:
        with pytest.raises(ApiError, match='validation_failed'):
            resolve_basis(SimpleNamespace(service=None), None, run, payload)


@pytest.mark.parametrize('result_type,human', [
    ('analytical_note', False), ('analytical_note', True), ('evidence_answer', False),
])
@pytest.mark.parametrize('label', ['2023', 'Не указано'])
def test_literal_qualification_with_short_period_label(period_facts, result_type, human, label):
    qualification = period_facts['fact_qualification']
    qualification['period']['label'] = label
    qualification['evidence'][0]['quote'] = (
        'Фактические показатели\n1Q23 подтверждают\nзаявленные темпы роста\nпо 2023 году в целом')
    text = ('Утверждение «Фактические показатели 1Q23 подтверждают заявленные темпы роста '
            'по 2023 году в целом» приведено с дословной подписью периода ' + label +
            ' и не превращено в установленный годовой интервал или годовой показатель.')
    payload = period_payload(text, ['fact_qualification'], result_type, human)
    before = deepcopy(payload)
    findings = []
    basis = resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human, findings=findings)
    assert not findings
    assert basis['facts'] == [qualification]
    assert payload == before


@pytest.mark.parametrize('text', [
    'Утверждение «Фактические показатели 1Q23 подтверждают темпы роста по 2023 году в целом».',
    'Утверждение «Фактические показатели 1Q23 подтверждают заявленные темпы роста по 2024 году в целом».',
    'Утверждение «Фактические показатели 2Q23 подтверждают заявленные темпы роста по 2023 году в целом».',
    'Утверждение «2023 году в целом».',
    'Подпись периода 2023 год.',
    'Подпись периода 2023-12-31.',
])
def test_short_label_does_not_authorize_changed_quote_or_annual_period(period_facts, text):
    period_facts['fact_qualification']['period']['label'] = '2023'
    payload = period_payload(text, ['fact_qualification'], 'evidence_answer')
    with pytest.raises(ApiError, match='validation_failed'):
        resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload)


@pytest.mark.parametrize('fact_ids,extra', [
    (['fact_qualification'], ' Выручка за 2023 год.'),
    (['fact_qualification', 'fact_current'], ' Выручка в 2023 году в целом составила 542 млн рублей.'),
    (['fact_qualification', 'fact_previous'], ' Выручка за 1Q23 составила 475 млн рублей.'),
])
@pytest.mark.parametrize('result_type,human', [
    ('analytical_note', False), ('analytical_note', True), ('evidence_answer', False),
])
def test_literal_quote_keeps_answer_strict_and_note_prose_unchecked(period_facts, fact_ids, extra, result_type, human):
    qualification = period_facts['fact_qualification']
    qualification['period']['label'] = '2023'
    text = 'Цитата «' + qualification['evidence'][0]['quote'] + '».' + extra
    payload = period_payload(text, fact_ids, result_type, human)
    before = deepcopy(payload)
    if result_type == 'evidence_answer':
        with pytest.raises(ApiError, match='validation_failed'):
            resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human)
    else:
        findings = []
        basis = resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human, findings=findings)
        assert findings == []
        assert {f['fact_id'] for f in basis['facts']} == set(fact_ids)
    assert payload == before


def test_numeric_qualification_label_is_not_an_amount(period_facts):
    period_facts['fact_qualification']['period']['label'] = '2023'
    payload = period_payload('Выручка составила 2023 млн рублей.', ['fact_qualification'], 'evidence_answer')
    with pytest.raises(ApiError, match='validation_failed'):
        resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload)


@pytest.mark.parametrize('result_type,human', [
    ('analytical_note', False), ('analytical_note', True), ('evidence_answer', False),
])
@pytest.mark.parametrize('text', [
    'Подпись периода 2023 млн рублей.',
    'В источнике указана подпись периода 2023%.',
])
def test_numeric_period_label_is_strict_in_answer_but_unchecked_in_note_prose(period_facts, result_type, human, text):
    period_facts['fact_qualification']['period']['label'] = '2023'
    payload = period_payload(text, ['fact_qualification'], result_type, human)
    before = deepcopy(payload)
    if result_type == 'evidence_answer':
        with pytest.raises(ApiError, match='validation_failed'):
            resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human)
    else:
        findings = []
        basis = resolve_basis(SimpleNamespace(service=None), None, dict(checkpoint={}), payload, human=human, findings=findings)
        assert findings == [] and basis['facts'] == [period_facts['fact_qualification']]
    assert payload == before
