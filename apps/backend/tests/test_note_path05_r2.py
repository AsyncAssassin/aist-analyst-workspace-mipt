"""Подписи периодов нейтральной таблицы через штатные layout и candidate."""
from copy import deepcopy
import io

import pytest
from pypdf import PdfReader

from app import tables
from app.errors import ApiError
from app.extraction import candidate, extraction_request
from test_flow01_03_postgres import COMPANY, PASSAGE, SOURCE, line_of, positioned_pdf, table_page

PIN = dict(source_id='src_period', source_version_id='sv_period', sha256='a' * 64)


def document_items(label, caption=None):
    items = table_page([(caption or '', [label, '2025', '2024'])],
                       [('Выручка от продаж', ['7 321', '6 218', '5 111']),
                        ('Активы', ['12 333', '10 222', '9 111']),
                        ('Расходы', ['(3 104)', '(2 090)', '(1 120)'])], 'headers_first', passage=PASSAGE)
    return [item for item in items if item[0]]


def document(label, caption=None):
    page = PdfReader(io.BytesIO(positioned_pdf([document_items(label, caption)]))).pages[0]
    return {1: page.extract_text()}, {1: page.extract_text(extraction_mode='layout')}


def table_item(pages, label, kind, start, end, caption=None, metric='Выручка от продаж'):
    lines = pages[1].splitlines()
    header = next(n for n, line in enumerate(lines, 1) if label in line)
    row = next(n for n, line in enumerate(lines, 1) if line.startswith(metric + ' '))
    source = line_of(pages, 1, SOURCE)
    period_line = line_of(pages, 1, caption) if caption else header
    value, spelling = {'Выручка от продаж': ('7321', '7 321'), 'Активы': ('12333', '12 333'),
                       'Расходы': ('-3104', '(3 104)')}[metric]
    return dict(metric=metric, subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                amount=dict(value=value, unit='money', currency='RUB'), value_text=spelling,
                period=dict(kind=kind, label=(caption + ' ' if caption else '') + label, start=start, end=end),
                evidence=[dict(page=1, line_start=row, line_end=row, area='Строка'),
                          dict(page=1, line_start=min(header, period_line), line_end=max(header, period_line), area='Период'),
                          dict(page=1, line_start=source, line_end=source, area='Источник')],
                basis=dict(page=1, metric_label=metric, unit_label='млн руб.', reporting_label='МСФО'),
                table=dict(page=1, row_line=row, year_line=header, period_line_start=period_line,
                           period_line_end=period_line, column=1))


@pytest.mark.parametrize('label', ['2026', '2026 г.', '2026 г', '2026 год', '2026 года'])
@pytest.mark.parametrize('kind,metric', [('interval', 'Выручка от продаж'), ('instant', 'Активы')])
def test_year_suffix_keeps_flow_and_closing_balance(label, kind, metric):
    pages, layouts = document(label)
    item = table_item(pages, label, kind, '2026-01-01' if kind == 'interval' else '2026-12-31', '2026-12-31', metric=metric)
    original = deepcopy(item)
    fact, _ = candidate(PIN, pages, item, layouts)
    assert fact['period'] == item['period'] and fact['amount'] == {**item['amount'], 'scale': '1000000'}
    assert item == original and fact['source'] == PIN
    assert tables.page_tables(pages[1].splitlines(), layouts[1])[0]['columns'][0]['period_lines'] == []
    for dates in [('2025-01-01', '2025-12-31'), ('2026-01-01', '2026-03-31')]:
        wrong = deepcopy(item)
        wrong['period'].update(start=dates[0], end=dates[1])
        with pytest.raises(ApiError) as refused:
            candidate(PIN, pages, wrong, layouts)
        assert refused.value.skip_code == 'period_mismatch'


@pytest.mark.parametrize('month,name', [(1, 'января'), (2, 'февраля'), (3, 'марта'), (4, 'апреля'),
                                     (5, 'мая'), (6, 'июня'), (7, 'июля'), (8, 'августа'),
                                     (9, 'сентября'), (10, 'октября'), (11, 'ноября'), (12, 'декабря')])
def test_calendar_date_uses_every_month_and_requires_instant(month, name):
    label = f'на 15 {name} 2026 года'
    iso = f'2026-{month:02d}-15'
    pages, layouts = document(label)
    item = table_item(pages, label, 'instant', iso, iso, metric='Активы')
    fact, _ = candidate(PIN, pages, item, layouts)
    assert fact['period'] == item['period']
    for change in [dict(kind='interval', start='2026-01-01', end='2026-12-31'),
                   dict(start=f'2026-{month:02d}-14'), dict(start='2025-01-01', end='2025-01-01')]:
        wrong = deepcopy(item)
        wrong['period'].update(change)
        with pytest.raises(ApiError) as refused:
            candidate(PIN, pages, wrong, layouts)
        assert refused.value.skip_code == 'period_mismatch'


@pytest.mark.parametrize('label', ['29 февраля 2028', '29 февраля 2028 г.', 'на 29 февраля 2028 г', '29 февраля 2028 год'])
def test_leap_date_and_optional_spelling(label):
    pages, layouts = document(label)
    item = table_item(pages, label, 'instant', '2028-02-29', '2028-02-29', metric='Активы')
    assert candidate(PIN, pages, item, layouts)[0]['period'] == item['period']


@pytest.mark.parametrize('day,valid', [('15', True), ('32', False)])
def test_date_split_by_layout_spaces_is_not_reduced_to_its_year(day, valid):
    label = f'на {day} мая 2026 года'
    pages, layouts = document(label)
    layouts = {1: layouts[1].replace(label, f'на  {day} мая  2026  года')}
    column = tables.page_tables(pages[1].splitlines(), layouts[1])[0]['columns'][0]
    assert column['label'] == label and column['period_lines'] == ([] if valid else None)
    item = table_item(pages, label, 'instant', '2026-05-15', '2026-05-15', metric='Активы')
    if valid:
        assert candidate(PIN, pages, item, layouts)[0]['period'] == item['period']
        item['period'].update(kind='interval', start='2026-01-01', end='2026-12-31')
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)


@pytest.mark.parametrize('label', ['2026П', '2026 прогноз', '2026/2027', '2026 год 2027', '2Q26 прогноз',
                                 'на 15 мая 2026 прогноз', '29 февраля 2026', '31 апреля 2026', '32 января 2026'])
def test_unsupported_or_conflicting_whole_label_never_admits_a_year(label):
    pages, layouts = document(label)
    item = table_item(pages, label, 'interval', '2026-01-01', '2026-12-31')
    assert tables.column_period(label) is None
    structure = tables.page_tables(pages[1].splitlines(), layouts[1])
    for table in structure:
        for column in table['columns']:
            if column['label'] == label:
                assert column['period_lines'] is None
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)


@pytest.mark.parametrize('kind,start', [('interval', '2026-04-01'), ('instant', '2026-06-30')])
def test_quarter_stays_quarter(kind, start):
    pages, layouts = document('2Q26')
    item = table_item(pages, '2Q26', kind, start, '2026-06-30')
    assert candidate(PIN, pages, item, layouts)[0]['period'] == item['period']
    for dates in [('2026-01-01', '2026-12-31'), ('2026-01-01', '2026-03-31'), ('2025-04-01', '2025-06-30')]:
        wrong = deepcopy(item)
        wrong['period'].update(start=dates[0], end=dates[1])
        with pytest.raises(ApiError) as refused:
            candidate(PIN, pages, wrong, layouts)
        assert refused.value.skip_code == 'period_mismatch'


def test_group_caption_keeps_date_as_interval_end_and_rejects_balance():
    caption, label = 'За 6 месяцев, закончившихся', '30 июня 2026 год'
    pages, layouts = document(label, caption)
    item = table_item(pages, label, 'interval', '2026-01-01', '2026-06-30', caption)
    fact, _ = candidate(PIN, pages, item, layouts)
    assert fact['period'] == item['period']
    for change in [dict(kind='instant', start='2026-06-30'), dict(end='2026-12-31'),
                   dict(start='2026-04-01'), dict(start='2025-01-01', end='2025-06-30')]:
        wrong = deepcopy(item)
        wrong['period'].update(change)
        with pytest.raises(ApiError):
            candidate(PIN, pages, wrong, layouts)
    request = extraction_request(pages, 'Показатели', layouts)
    instruction = request['messages'][0]['content']
    assert 'separate calendar date' in instruction and 'only kind=instant' in instruction


@pytest.mark.parametrize('defect', ['number', 'sign', 'row', 'scale'])
def test_other_cell_guarantees_are_preserved(defect):
    pages, layouts = document('2026 г.')
    item = table_item(pages, '2026 г.', 'interval', '2026-01-01', '2026-12-31', metric='Расходы')
    assert candidate(PIN, pages, item, layouts)[0]['amount']['value'] == '-3104'
    if defect == 'number':
        item.update(value_text='2 090')
        item['amount']['value'] = '2090'
    elif defect == 'sign':
        item['amount']['value'] = '3104'
    elif defect == 'row':
        item['metric'] = item['basis']['metric_label'] = 'Активы'
    else:
        item['basis']['unit_label'] = 'тыс. руб.'
    with pytest.raises(ApiError):
        candidate(PIN, pages, item, layouts)
