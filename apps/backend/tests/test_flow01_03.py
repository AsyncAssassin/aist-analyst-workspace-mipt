"""FLOW-01.03: a table cell is bound to its row, its column and the period caption centered over the column's group
on the saved text layout, whatever the order of the extracted text. Synthetic pages only; no model, no database."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app import tables
from app.errors import ApiError
from app.extraction import VERSION, candidate, extraction_request
from test_d14 import LAYOUT as NINE_MONTHS_LAYOUT, PAGES as NINE_MONTHS_PAGES, PIN, table_item as nine_months_item

COMPANY = 'Synthetic Chemicals'
SOURCE = 'Источник: отчетность Synthetic Chemicals по МСФО'
CAPTION_12 = 'За 12 месяцев, закончившихся'
CAPTION_6 = 'За 6 месяцев'
YEARS_12 = ['31 декабря 2022', '31 декабря 2023']
YEARS_6 = ['30 июня 2023', '30 июня 2024']
# Four period columns: a year and a half-year of the same year 2023 stand in different groups.
ROWS = [('Выручка от продаж', ['8 120', '9 345', '5 010', '4 980']),
        ('Себестоимость', ['(4 100)', '(4 870)', '(2 600)', '(2 450)']),
        ('Чистая прибыль', ['1 230', '1 480', '1 480', '905']),
        ('Дивиденды', ['-', '-', '300', '-'])]


def placed(*cells):
    """One layout row from (start column, text) cells."""
    row = ''
    for start, text in cells:
        row = row.ljust(start) + text
    return row


def centered(text, center):
    return round(center - len(text) / 2), text


def page(groups, rows, *, header_first=False):
    """An Ozon-class page: the plain text lists the rows before the header unless header_first; the layout places
    each group caption centered over its own columns, 30 characters apart."""
    headers = [header for _, header_cells in groups for header in header_cells]
    centers = [70 + 30 * i for i in range(len(headers))]
    caption_cells, index = [], 0
    for caption, header_cells in groups:
        first, last = centered(header_cells[0], centers[index]), centered(header_cells[-1], centers[index + len(header_cells) - 1])
        caption_cells.append(centered(caption, (first[0] + last[0] + len(last[1])) / 2))
        index += len(header_cells)
    layout = ['Отчет о совокупном доходе', '', placed(*caption_cells),
              placed((0, 'млн руб.'), *(centered(header, center) for header, center in zip(headers, centers))), '']
    layout += [placed((0, label), *(centered(cell, center) for cell, center in zip(cells, centers) if cell)) for label, cells in rows]
    layout += ['', SOURCE]
    header_lines = ['млн руб.']
    for caption, header_cells in groups:
        header_lines += [caption, ' '.join(header_cells)]
    row_lines = [' '.join([label, *cells]) for label, cells in rows]
    text = ['Отчет о совокупном доходе'] + (header_lines + row_lines if header_first else row_lines + ['7'] + header_lines) + [SOURCE]
    return {1: '\n'.join(text)}, {1: '\n'.join(layout)}


PAGES, LAYOUT = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], ROWS)
# Lines of PAGES: 1 title, 2-5 rows, 6 page number, 7 unit, 8 caption 12M, 9 its years, 10 caption 6M, 11 its years, 12 source.
ROW_LINE = {label: number for number, (label, _) in enumerate(ROWS, 2)}
GROUP_12 = dict(year_line=9, period_line_start=8, period_line_end=8)
GROUP_6 = dict(year_line=11, period_line_start=10, period_line_end=10)
PERIODS = {1: ('За 12 месяцев, закончившихся 31 декабря 2022', '2022-01-01', '2022-12-31', GROUP_12),
           2: ('За 12 месяцев, закончившихся 31 декабря 2023', '2023-01-01', '2023-12-31', GROUP_12),
           3: ('За 6 месяцев 30 июня 2023', '2023-01-01', '2023-06-30', GROUP_6),
           4: ('За 6 месяцев 30 июня 2024', '2024-01-01', '2024-06-30', GROUP_6)}


def item(metric, column, *, value_text=None, value=None, period=None, pages=PAGES, rows=ROWS, row_lines=ROW_LINE, periods=PERIODS):
    row_text = dict(rows)[metric]
    value_text = value_text or row_text[column - 1]
    label, start, end, group = periods[column]
    return dict(metric=metric, subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                period=period or dict(kind='interval', label=label, start=start, end=end),
                amount=dict(value=value or value_text.replace(' ', ''), unit='money', currency='RUB'), value_text=value_text,
                evidence=[dict(page=1, line_start=row_lines[metric], line_end=row_lines[metric], area='Строка таблицы'),
                          dict(page=1, line_start=7, line_end=12, area='Заголовок таблицы и источник')],
                basis=dict(page=1, metric_label=metric, unit_label='млн руб.', reporting_label='МСФО'),
                table=dict(page=1, row_line=row_lines[metric], column=column, **group))


def refused(data, pages=PAGES, layout=LAYOUT):
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, data, layout)
    return getattr(error.value, 'skip_code', None), error.value.body['error']['message']


def test_each_cell_is_bound_to_its_row_column_and_group_despite_the_header_printed_after_the_rows():
    for column, (label, start, end, _) in PERIODS.items():
        data = item('Выручка от продаж', column)
        before = deepcopy(data)
        fact, basis = candidate(PIN, PAGES, data, LAYOUT)
        assert data == before
        assert fact['amount'] == dict(value=ROWS[0][1][column - 1].replace(' ', ''), unit='money', currency='RUB', scale='1000000')
        assert fact['period'] == dict(kind='interval', label=label, start=start, end=end)
        assert fact['raw_text'] == ROWS[0][1][column - 1] and fact['extraction_version'] == VERSION == 'pdf-lines.v39'
        assert PIN['sha256'] in basis['definition']
        quotes = [e['quote'] for e in fact['evidence'] if e['locator']['area'] in ('Строка таблицы и выбранный годовой столбец', 'Общий период и годы столбцов')]
        # The row and the header of its own group are separate literal passages of the original text.
        caption, years = (CAPTION_12, YEARS_12) if column <= 2 else (CAPTION_6, YEARS_6)
        assert quotes == ['Выручка от продаж 8 120 9 345 5 010 4 980', caption + '\n' + ' '.join(years)]
        assert all(PAGES[1].count(quote) == 1 for quote in quotes)
    profit = candidate(PIN, PAGES, item('Чистая прибыль', 4), LAYOUT)[0]
    assert profit['amount']['value'] == '905' and profit['period']['end'] == '2024-06-30'


def test_the_year_and_the_half_year_of_the_same_year_are_different_periods():
    annual, half = PERIODS[2], PERIODS[3]
    swapped = [(2, dict(kind='interval', label=half[0], start=half[1], end=half[2])),       # the 12M column with the 6M period
               (3, dict(kind='interval', label=annual[0], start=annual[1], end=annual[2])),  # the 6M column with the 12M period
               (2, dict(kind='interval', label=annual[0], start=annual[1], end='2023-06-30')),
               (3, dict(kind='interval', label=half[0], start=half[1], end='2023-12-31')),
               (2, dict(kind='instant', label=annual[0], start=annual[2], end=annual[2])),
               (2, dict(kind='interval', label="6M'2023", start=half[1], end=half[2])),
               (3, dict(kind='interval', label="12M'2023", start=half[1], end=half[2]))]
    for column, period in swapped:
        code, message = refused(item('Выручка от продаж', column, period=period))
        assert code == 'period_mismatch', (column, period, message)
    # The same amount in both groups of 2023 does not transfer the period: the column decides.
    assert candidate(PIN, PAGES, item('Чистая прибыль', 2), LAYOUT)[0]['period']['end'] == '2023-12-31'
    assert candidate(PIN, PAGES, item('Чистая прибыль', 3), LAYOUT)[0]['period']['end'] == '2023-06-30'
    equal = item('Чистая прибыль', 2, period=dict(kind='interval', label=half[0], start=half[1], end=half[2]))
    assert refused(equal)[0] == 'period_mismatch'
    equal = item('Чистая прибыль', 3, period=dict(kind='interval', label=annual[0], start=annual[1], end=annual[2]))
    assert refused(equal)[0] == 'period_mismatch'


def test_swapped_groups_keep_their_links_and_an_equal_amount_does_not_choose_the_period():
    rows = [('Выручка от продаж', ['5 010', '4 980', '8 120', '9 345']), ('Чистая прибыль', ['1 480', '905', '1 230', '1 480'])]
    pages, layout = page([(CAPTION_6, YEARS_6), (CAPTION_12, YEARS_12)], rows)
    # Lines: 1 title, 2-3 rows, 4 page number, 5 unit, 6 caption 6M, 7 its years, 8 caption 12M, 9 its years, 10 source.
    row_lines = {'Выручка от продаж': 2, 'Чистая прибыль': 3}
    group_6, group_12 = dict(year_line=7, period_line_start=6, period_line_end=6), dict(year_line=9, period_line_start=8, period_line_end=8)
    periods = {1: (*PERIODS[3][:3], group_6), 2: (*PERIODS[4][:3], group_6), 3: (*PERIODS[1][:3], group_12), 4: (*PERIODS[2][:3], group_12)}
    for column in periods:
        data = item('Выручка от продаж', column, pages=pages, rows=rows, row_lines=row_lines, periods=periods)
        data['evidence'][1] = dict(page=1, line_start=5, line_end=10, area='Заголовок таблицы и источник')
        fact, _ = candidate(PIN, pages, data, layout)
        assert fact['amount']['value'] == rows[0][1][column - 1].replace(' ', '') and fact['period']['end'] == periods[column][2]
    # 1 480 is printed for both 6M 2023 (column 1) and 12M 2023 (column 4): each column keeps its own period.
    def profit(column):
        data = item('Чистая прибыль', column, pages=pages, rows=rows, row_lines=row_lines, periods=periods)
        data['evidence'][1] = dict(page=1, line_start=5, line_end=10, area='Заголовок таблицы и источник')
        return data
    for column, other in ((1, 4), (4, 1)):
        data = profit(column)
        assert candidate(PIN, pages, data, layout)[0]['period']['end'] == periods[column][2]
        label, start, end, _ = periods[other]
        data['period'] = dict(kind='interval', label=label, start=start, end=end)
        assert refused(data, pages, layout)[0] == 'period_mismatch'
        # The other group's caption and header with this column: the layout refuses the borrowed group.
        data = profit(column)
        data['table'].update({k: periods[other][3][k] for k in ('year_line', 'period_line_start', 'period_line_end')})
        assert refused(data, pages, layout)[0] == 'ambiguous_link'


@pytest.mark.parametrize('change,code', [
    (lambda i: i['table'].update(column=1), 'number_mismatch'),                                   # another column of the same group under this period claim
    (lambda i: i['table'].update(column=3), 'ambiguous_link'),                                    # a column the cited header line does not print
    (lambda i: i.update(value_text='9 34', amount=dict(value='934', unit='money', currency='RUB')), 'number_mismatch'),
    (lambda i: i.update(value_text='-9 345', amount=dict(value='-9345', unit='money', currency='RUB')), 'number_mismatch'),
    (lambda i: i['amount'].update(value='-9345'), 'number_mismatch'),
    (lambda i: (i.update(metric='Чистая прибыль'), i['basis'].update(metric_label='Чистая прибыль')), 'ambiguous_link'),  # another row
    (lambda i: i.update(metric='Чистая прибыль'), 'unit_mismatch'),                                # a metric apart from its own definition
    (lambda i: i['basis'].update(unit_label='тыс. руб.'), 'unit_mismatch'),
    (lambda i: i['amount'].update(currency='USD'), 'unit_mismatch'),
    (lambda i: i['table'].update(**GROUP_6), 'ambiguous_link'),                                    # the half-year group with a 12M column
    (lambda i: i['table'].update(year_line=11), 'ambiguous_link'),                                 # the header line of the other group
    (lambda i: i['table'].update(period_line_start=8, period_line_end=11), 'ambiguous_link'),      # both captions and both header lines
    (lambda i: i['table'].update(period_line_start=7, period_line_end=9), 'ambiguous_link'),       # the unit line as a caption
    (lambda i: i['table'].update(period_line_start=10, period_line_end=10), 'ambiguous_link'),     # the other caption alone
    (lambda i: i['table'].update(period_line_start=9, period_line_end=9), 'ambiguous_link'),       # the year line only: the caption over the column is not cited
    (lambda i: i['table'].update(row_line=12), 'number_mismatch'),                                 # the source line prints no amount in the column
])
def test_a_changed_column_value_sign_metric_unit_or_group_is_refused_with_its_reason(change, code):
    data = item('Выручка от продаж', 2)
    change(data)
    assert refused(data)[0] == code


def test_a_dash_or_an_empty_cell_is_not_a_number():
    for column in (1, 2, 4):
        zero = item('Дивиденды', column, value_text='0', value='0')
        assert refused(zero)[0] == 'number_mismatch'
        dash = item('Дивиденды', column, value_text='-', value='0')
        assert refused(dash)[0] == 'number_mismatch'
    assert candidate(PIN, PAGES, item('Дивиденды', 3), LAYOUT)[0]['amount']['value'] == '300'
    # A row whose cell is missing: the amount of the neighbor column is not taken.
    rows = [('Выручка от продаж', ['8 120', '', '5 010', '4 980'])]
    pages, layout = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], rows)
    data = item('Выручка от продаж', 2, value_text='5 010', pages=pages, rows=rows, row_lines={'Выручка от продаж': 2},
                periods={**PERIODS, 2: (*PERIODS[2][:3], dict(year_line=6, period_line_start=5, period_line_end=5))})
    data['evidence'][1] = dict(page=1, line_start=4, line_end=9, area='Заголовок')
    assert refused(data, pages, layout)[0] == 'number_mismatch'


def test_a_header_printed_below_the_rows_or_cited_from_another_table_is_a_limitation():
    pages, layout = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], ROWS)
    rows = layout[1].splitlines()
    # The same text with the header rows moved under the data rows: unsupported, not a different observation.
    below = {1: '\n'.join([rows[0], rows[1], *rows[4:-2], rows[2], rows[3], *rows[-2:]])}
    assert refused(item('Выручка от продаж', 2), pages, below) == ('unsupported_layout', 'Заголовок периодов расположен не над строкой показателя.')
    # A second table on the page: the header of the first table does not reach the rows of the second.
    second = ['', placed((0, 'Баланс')), placed((0, 'млн руб.'), centered('31 декабря 2023', 70), centered('30 июня 2024', 100)),
              placed((0, 'Активы'), centered('30 100', 70), centered('31 900', 100))]
    two = {1: layout[1] + '\n' + '\n'.join(second)}
    text = {1: pages[1] + '\nБаланс\nмлн руб.\n31 декабря 2023 30 июня 2024\nАктивы 30 100 31 900'}
    data = item('Активы', 1, value_text='30 100', pages=text, rows=[('Активы', ['30 100', '31 900'])], row_lines={'Активы': 16},
                periods={1: (PERIODS[2][0], PERIODS[2][1], PERIODS[2][2], dict(year_line=9, period_line_start=8, period_line_end=8))})
    data['evidence'] = [dict(page=1, line_start=16, line_end=16, area='Активы'), dict(page=1, line_start=7, line_end=12, area='Заголовок')]
    assert refused(data, text, two)[0] == 'unsupported_layout'


def test_a_caption_standing_off_its_group_leaves_the_header_unassigned():
    pages, layout = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], ROWS)
    rows = layout[1].splitlines()
    caption_row = rows[2]
    shifted = caption_row.replace(' ' * 9 + CAPTION_6, CAPTION_6 + ' ' * 9) if ' ' * 9 + CAPTION_6 in caption_row else None
    assert shifted and shifted != caption_row
    off = {1: '\n'.join([*rows[:2], shifted, *rows[3:]])}
    structure = tables.page_tables(pages[1].splitlines(), off[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [None] * 4 and structure[0]['rows']
    for column in (2, 3):
        assert refused(item('Выручка от продаж', column), pages, off) == ('ambiguous_link', 'Подписи над столбцами не распределены по группам однозначно.')


def test_an_explicitly_marked_forecast_column_is_not_an_actual_result():
    pages = {1: 'Synthetic Chemicals\nМСФО\nПоказатель 2023 2024 2025П\nВыручка, млн руб. 9 345 10 200 11 000'}
    layout = {1: 'Synthetic Chemicals\nМСФО\n' + placed((0, 'Показатель'), (30, '2023'), (50, '2024'), (70, '2025П')) + '\n'
                 + placed((0, 'Выручка, млн руб.'), (29, '9 345'), (48, '10 200'), (68, '11 000'))}
    def explicit(column, label, start, end):
        return dict(metric='Выручка, млн руб.', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                    period=dict(kind='interval', label=label, start=start, end=end),
                    amount=dict(value=['9345', '10200', '11000'][column - 1], unit='money', currency='RUB'), value_text=['9 345', '10 200', '11 000'][column - 1],
                    evidence=[dict(page=1, line_start=1, line_end=4, area='Таблица')],
                    basis=dict(page=1, metric_label='Выручка, млн руб.', unit_label='млн руб.', reporting_label='МСФО'),
                    table=dict(page=1, row_line=4, year_line=3, period_line_start=3, period_line_end=3, column=column))
    assert candidate(PIN, pages, explicit(2, '2024', '2024-01-01', '2024-12-31'), layout)[0]['amount']['value'] == '10200'
    assert refused(explicit(3, '2025П', '2025-01-01', '2025-12-31'), pages, layout)[0] == 'unsupported_layout'
    assert refused(explicit(3, '2025', '2025-01-01', '2025-12-31'), pages, layout)[0] == 'unsupported_layout'
    structure = tables.page_tables(pages[1].splitlines(), layout[1])
    assert structure == [dict(columns=[dict(column=1, label='2023', year_line=3, period_lines=[]), dict(column=2, label='2024', year_line=3, period_lines=[]),
                                       dict(column=3, label='2025П', year_line=3, period_lines=None)], rows=[dict(line=4, cells=[1, 2, 3])])]


def test_the_structure_given_to_the_extractor_names_exactly_the_links_the_server_verifies():
    structure = tables.page_tables(PAGES[1].splitlines(), LAYOUT[1])
    assert structure == [dict(columns=[
        dict(column=1, label='31 декабря 2022', year_line=9, period_lines=[8]), dict(column=2, label='31 декабря 2023', year_line=9, period_lines=[8]),
        dict(column=3, label='30 июня 2023', year_line=11, period_lines=[10]), dict(column=4, label='30 июня 2024', year_line=11, period_lines=[10])],
        rows=[dict(line=2, cells=[1, 2, 3, 4]), dict(line=3, cells=[1, 2, 3, 4]), dict(line=4, cells=[1, 2, 3, 4]), dict(line=5, cells=[1, 2, 3, 4])])]
    # Round trip: every cell the structure names verifies with the locator copied from it; the same cell under a
    # neighbor column of the same group does not.
    for row in structure[0]['rows']:
        label, cells = ROWS[row['line'] - 2]
        for column_number, cell in zip(row['cells'], cells):
            entry = structure[0]['columns'][column_number - 1]
            locator = dict(page=1, row_line=row['line'], year_line=entry['year_line'], period_line_start=min(entry['period_lines']),
                           period_line_end=max(entry['period_lines']), column=column_number)
            data = dict(item(label, column_number, value_text=cell, value=cell.replace(' ', '').replace('(', '-').replace(')', '')), table=locator)
            if cell == '-':
                assert refused(dict(data, amount=dict(value='0', unit='money', currency='RUB')))[0] == 'number_mismatch'
            else:
                fact = candidate(PIN, PAGES, data, LAYOUT)[0]
                assert fact['amount']['value'] == data['amount']['value'] and fact['raw_text'] == cell
    # The request carries the structure beside the original lines only when the layout is supplied; cells are not repeated.
    request = extraction_request(PAGES, 'Выручка и прибыль', LAYOUT)
    sent = json.loads(request['messages'][1]['content'])
    assert sent['pages'][0]['table_layout'] == structure
    assert [line['text'] for line in sent['pages'][0]['lines']] == PAGES[1].splitlines()
    assert 'table_layout' not in json.loads(extraction_request(PAGES, 'Выручка и прибыль')['messages'][1]['content'])['pages'][0]
    assert 'table_layout' in request['messages'][0]['content'] and 'period_lines' in request['messages'][0]['content']


def test_a_wrapped_label_row_has_no_place_and_is_a_limitation_not_a_guess():
    rows = [('Чистые доходы по ожидаемым кредитным убыткам по торговой и прочей дебиторской', ['(31)', '(1 417)', '(486)', '15']), ('Чистая прибыль', ['1 230', '1 480', '1 480', '905'])]
    pages, layout = page([(CAPTION_12, YEARS_12), (CAPTION_6, YEARS_6)], rows)
    # The plain text wraps the long label: the amounts follow the second part of the label on their own line.
    text = pages[1].replace(rows[0][0] + ' (31) (1 417) (486) 15', 'Чистые доходы по ожидаемым кредитным убыткам по торговой и прочей\nдебиторской (31) (1 417) (486) 15')
    pages = {1: text}
    assert tables.page_tables(pages[1].splitlines(), layout[1])[0]['rows'] == [dict(line=4, cells=[1, 2, 3, 4])]
    # Lines: 1 title, 2-3 the wrapped row, 4 profit, 5 page number, 6 unit, 7 caption 12M, 8 years, 9 caption 6M, 10 years, 11 source.
    data = item('дебиторской', 4, value_text='15', pages=pages, rows=[('дебиторской', ['(31)', '(1 417)', '(486)', '15'])], row_lines={'дебиторской': 3},
                periods={**PERIODS, 4: (*PERIODS[4][:3], dict(year_line=10, period_line_start=9, period_line_end=9))})
    data['evidence'][1] = dict(page=1, line_start=6, line_end=11, area='Заголовок')
    assert refused(data, pages, layout) == ('ambiguous_link', 'Текстовая строка не имеет однозначного положения в PDF.')


# The nine-month revenue table of test_d14 (thousands of roubles, one caption over two year columns) and, under
# it, an assets table with a bare-year header and its own unit.
ASSETS_TEXT = '\nАктивы, млн рублей\n2024 2023\nАктивы 71 68'
ASSETS_LAYOUT = '\n\nАктивы, млн рублей\n                              2024                  2023\nАктивы                          71                    68'
# The same revenue table with its caption printed in two lines, each centered over the two columns.
TWO_LINE_TEXT = 'Synthetic Company\nМСФО\nВыручка, в тысячах рублей\nЗа 9 месяцев,\nзакончившихся 30 сентября\n2022 2021\nВыручка 1 403 1 004'
TWO_LINE_LAYOUT = ('Synthetic Company\nМСФО\nВыручка, в тысячах рублей\n                                    За 9 месяцев,\n'
                   '                              закончившихся 30 сентября\n                              2022                  2021\n'
                   'Выручка                       1 403                 1 004')
UNCITED = 'Над выбранным столбцом стоит подпись периода, которую locator не цитирует.'
UNPLACED = 'Подпись периода над выбранным столбцом не имеет однозначной исходной строки.'
FOREIGN_UNIT = 'Единица не напечатана в заголовке выбранной таблицы или в строке показателя.'


def assets_item():
    """The 71 of the assets table: a bare-year column, a balance at its closing date, the unit of its own heading."""
    return dict(metric='Активы', subject='Synthetic Company', reporting_basis='МСФО', nature='reported_actual',
                period=dict(kind='instant', label='2024', start='2024-12-31', end='2024-12-31'),
                amount=dict(value='71', unit='money', currency='RUB'), value_text='71',
                evidence=[dict(page=1, line_start=1, line_end=9, area='Две таблицы')],
                basis=dict(page=1, metric_label='Активы', unit_label='млн рублей', reporting_label='МСФО'),
                table=dict(page=1, row_line=9, year_line=8, period_line_start=8, period_line_end=8, column=1))


def two_line_item(start, end):
    """The 1 403 of the two-line caption table, citing the caption lines start..end."""
    data = nine_months_item()
    data['evidence'] = [dict(page=1, line_start=1, line_end=7, area='Синтетическая таблица')]
    data['table'].update(row_line=7, year_line=6, period_line_start=start, period_line_end=end)
    return data


def test_the_caption_over_a_column_is_read_from_the_layout_and_cannot_be_omitted_for_a_calendar_year():
    """The nine-month column keeps its period; a locator naming the year line only, with the calendar year or
    with the right dates, is excluded for the uncited caption and never admitted as a full year."""
    good, _ = candidate(PIN, NINE_MONTHS_PAGES, nine_months_item(), NINE_MONTHS_LAYOUT)
    assert good['period'] == dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    structure = tables.page_tables(NINE_MONTHS_PAGES[1].splitlines(), NINE_MONTHS_LAYOUT[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [[4], [4]]
    year = nine_months_item()
    year['table'].update(period_line_start=5, period_line_end=5)
    year['period'].update(label='2022', end='2022-12-31')
    assert refused(year, NINE_MONTHS_PAGES, NINE_MONTHS_LAYOUT) == ('ambiguous_link', UNCITED)
    dates = nine_months_item()
    dates['table'].update(period_line_start=5, period_line_end=5)
    assert refused(dates, NINE_MONTHS_PAGES, NINE_MONTHS_LAYOUT) == ('ambiguous_link', UNCITED)


def test_a_heading_over_a_bare_year_header_is_neither_a_caption_nor_a_confirmed_absence_of_one():
    """A narrow heading beside the columns leaves a bare-year header explicit: the calendar year is admitted with the
    unit of that heading. A heading standing over the columns off their center is no group caption and no confirmed
    absence of one: the structure names the columns unverifiable and the server assumes no calendar year."""
    def table(heading):
        pages = {1: 'Synthetic Company\nМСФО\n' + heading + '\n2023 2022\nВыручка 9 345 8 120'}
        layout = {1: 'Synthetic Company\nМСФО\n' + placed((0, heading)) + '\n' + placed((30, '2023'), (50, '2022')) + '\n'
                     + placed((0, 'Выручка'), (29, '9 345'), (48, '8 120'))}
        return pages, layout

    def revenue():
        return dict(metric='Выручка', subject='Synthetic Company', reporting_basis='МСФО', nature='reported_actual',
                    period=dict(kind='interval', label='2023', start='2023-01-01', end='2023-12-31'),
                    amount=dict(value='9345', unit='money', currency='RUB'), value_text='9 345',
                    evidence=[dict(page=1, line_start=1, line_end=5, area='Таблица')],
                    basis=dict(page=1, metric_label='Выручка', unit_label='млн руб.', reporting_label='МСФО'),
                    table=dict(page=1, row_line=5, year_line=4, period_line_start=4, period_line_end=4, column=1))
    pages, layout = table('Показатели, млн руб.')
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[], []]
    fact, _ = candidate(PIN, pages, revenue(), layout)
    assert fact['period']['end'] == '2023-12-31' and fact['amount']['scale'] == '1000000'
    pages, layout = table('Показатели за девять месяцев, млн руб.')
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [None, None]
    assert refused(revenue(), pages, layout) == ('ambiguous_link', 'Подписи над столбцами не распределены по группам однозначно.')


def test_a_unit_from_another_table_does_not_change_the_scale():
    """The revenue table in thousands and the assets table in millions on one page: each cell keeps the unit
    of its own table, the unit of the other table excludes the candidate, and so does a unit printed on the page
    apart from the table, with a blank row between."""
    pages = {1: NINE_MONTHS_PAGES[1] + ASSETS_TEXT}
    layout = {1: NINE_MONTHS_LAYOUT[1] + ASSETS_LAYOUT}
    good, _ = candidate(PIN, pages, nine_months_item(), layout)
    assert good['amount'] == dict(value='1403', unit='money', currency='RUB', scale='1000')
    borrowed = nine_months_item()
    borrowed['basis']['unit_label'] = 'млн рублей'
    assert refused(borrowed, pages, layout) == ('unit_mismatch', FOREIGN_UNIT)
    structure = tables.page_tables(pages[1].splitlines(), layout[1])
    assert [[c['period_lines'] for c in table['columns']] for table in structure] == [[[4], [4]], [[], []]]
    assets, _ = candidate(PIN, pages, assets_item(), layout)
    assert assets['amount'] == dict(value='71', unit='money', currency='RUB', scale='1000000') and assets['period']['end'] == '2024-12-31'
    thousands = assets_item()
    thousands['basis']['unit_label'] = 'в тысячах рублей'
    assert refused(thousands, pages, layout) == ('unit_mismatch', FOREIGN_UNIT)
    apart = {1: NINE_MONTHS_LAYOUT[1].replace('Выручка, в тысячах рублей\n', 'Выручка, в тысячах рублей\n\n')}
    assert refused(nine_months_item(), NINE_MONTHS_PAGES, apart) == ('unit_mismatch', FOREIGN_UNIT)


def test_a_repeated_caption_fragment_leaves_its_column_unverifiable_without_a_request_crash():
    """The two-line caption printed once verifies the cell. With its first line printed again elsewhere on the
    page that line has no unique placement: the structure names the column unverifiable, the request is still built,
    and the server admits no cell of that column, not through the year line alone either."""
    pages, layout = {1: TWO_LINE_TEXT}, {1: TWO_LINE_LAYOUT}
    structure = tables.page_tables(pages[1].splitlines(), layout[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [[4, 5], [4, 5]] and structure[0]['rows'] == [dict(line=7, cells=[1, 2])]
    fact, _ = candidate(PIN, pages, two_line_item(4, 5), layout)
    assert fact['period'] == dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    assert 'За 9 месяцев,\nзакончившихся 30 сентября\n2022 2021' in [e['quote'] for e in fact['evidence']]
    repeated = {1: TWO_LINE_TEXT + '\nЗа 9 месяцев,'}, {1: TWO_LINE_LAYOUT + '\n\nЗа 9 месяцев,'}
    structure = tables.page_tables(repeated[0][1].splitlines(), repeated[1][1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and structure[0]['rows'] == [dict(line=7, cells=[1, 2])]
    request = extraction_request(repeated[0], 'Выручка', repeated[1])
    assert json.loads(request['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    for start, end in ((4, 5), (5, 5), (6, 6)):
        data = two_line_item(start, end)
        if start == 6:
            data['period'].update(label='2022', end='2022-12-31')
        assert refused(data, *repeated) == ('ambiguous_link', UNPLACED)


# Caption alignment and row units have separate positive and negative controls.
BESIDE = 'Подпись периода в заголовке таблицы не связана со столбцами однозначно.'
ROW_UNIT = 'Заявленная единица противоречит единице, напечатанной в строке показателя.'
LEFT_CAPTION = 'За 9 месяцев, закончившихся 30 сентября'


def bare_year_page(*lines):
    """A page of Synthetic Company: the given (text, layout row) pairs after the company and the standard, then a
    header of two bare year columns and one revenue row. The text and the layout name the same lines."""
    text = ['Synthetic Company', 'МСФО', *(text for text, _ in lines)]
    layout = ['Synthetic Company', 'МСФО', *(row for _, row in lines)]
    return {1: '\n'.join(text)}, {1: '\n'.join(layout)}


def revenue_of(pages, *, start=None, end=None, period=None, unit_label='тыс. руб.', column=1):
    """The 1 403 of the last line, its header line the one before it, the cited caption lines start..end (the header
    line alone by default)."""
    lines = len(pages[1].splitlines())
    start, end = (lines - 1 if start is None else start), (lines - 1 if end is None else end)
    return dict(metric='Выручка', subject='Synthetic Company', reporting_basis='МСФО', nature='reported_actual',
                period=period or dict(kind='interval', label='2022', start='2022-01-01', end='2022-12-31'),
                amount=dict(value='1403', unit='money', currency='RUB'), value_text='1 403',
                evidence=[dict(page=1, line_start=1, line_end=lines, area='Таблица')],
                basis=dict(page=1, metric_label='Выручка', unit_label=unit_label, reporting_label='МСФО'),
                table=dict(page=1, row_line=lines, year_line=lines - 1, period_line_start=start, period_line_end=end, column=column))


def test_a_period_caption_beside_the_columns_leaves_the_header_unverifiable_never_a_bare_year():
    """A nine-month caption printed at the left margin, above the header or on it, does not overlap the year
    columns; the header is unverifiable (period_lines null) rather than a confirmed absence of a caption: neither the
    calendar year nor the nine months are admitted, with the caption cited, with the year line alone and across a blank
    layout row. The same caption centered over the columns verifies the nine months; a heading without a period beside
    the columns leaves the bare years explicit."""
    header = placed((0, 'тыс. руб.'), (50, '2022'), (70, '2021'))
    row = placed((0, 'Выручка'), (49, '1 403'), (68, '1 004'))
    above = bare_year_page((LEFT_CAPTION, LEFT_CAPTION), ('тыс. руб. 2022 2021', header), ('Выручка 1 403 1 004', row))
    on_header = bare_year_page(('тыс. руб.', 'тыс. руб.'), (LEFT_CAPTION + ' 2022 2021', placed((0, LEFT_CAPTION), (50, '2022'), (70, '2021'))),
                               ('Выручка 1 403 1 004', row))
    apart = bare_year_page((LEFT_CAPTION, LEFT_CAPTION + '\n'), ('тыс. руб. 2022 2021', header), ('Выручка 1 403 1 004', row))
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    for pages, layout in (above, on_header, apart):
        structure = tables.page_tables(pages[1].splitlines(), layout[1])
        assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and structure[0]['rows'] == [dict(line=5, cells=[1, 2])]
        assert json.loads(extraction_request(pages, 'Выручка', layout)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
        year_only = revenue_of(pages, start=4, end=4)
        assert refused(year_only, pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=4, end=4, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=3, end=4, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=4, end=4, column=2), pages, layout) == ('ambiguous_link', BESIDE)
    # Positive controls: the caption centered over both columns, and a heading without a period at the left margin.
    centered_caption = bare_year_page((LEFT_CAPTION, placed(centered(LEFT_CAPTION, 62))), ('тыс. руб. 2022 2021', header), ('Выручка 1 403 1 004', row))
    assert [c['period_lines'] for c in tables.page_tables(centered_caption[0][1].splitlines(), centered_caption[1][1])[0]['columns']] == [[3], [3]]
    fact, _ = candidate(PIN, centered_caption[0], revenue_of(centered_caption[0], start=3, end=3, period=nine_months), centered_caption[1])
    assert fact['period'] == nine_months and fact['amount']['scale'] == '1000'
    assert refused(revenue_of(centered_caption[0], start=4, end=4), *centered_caption) == ('ambiguous_link', UNCITED)
    for heading in ('Показатели', 'Synthetic Company', 'Отчет за период'):
        plain = bare_year_page((heading, heading), ('тыс. руб. 2022 2021', header), ('Выручка 1 403 1 004', row))
        assert [c['period_lines'] for c in tables.page_tables(plain[0][1].splitlines(), plain[1][1])[0]['columns']] == [[], []]
        fact, _ = candidate(PIN, plain[0], revenue_of(plain[0], start=4, end=4), plain[1])
        assert fact['period']['end'] == '2022-12-31' and fact['amount']['scale'] == '1000'


def unit_page(heading_unit, row_label):
    """A bare-year table under a heading unit at the left margin, with one revenue row whose label may name a unit."""
    return bare_year_page((heading_unit, heading_unit), ('2022 2021', placed((80, '2022'), (105, '2021'))),
                          (row_label + ' 1 403 1 004', placed((0, row_label), (79, '1 403'), (104, '1 004'))))


def row_revenue(pages, row_label, unit_label):
    data = revenue_of(pages, unit_label=unit_label)
    data['basis']['metric_label'] = row_label
    return data


def test_the_unit_named_in_the_row_label_sets_the_scale_of_the_row():
    """R1-F02: with the table heading in thousands and the row label in millions the millions are the unit of the row;
    the thousands of the heading are excluded for the row, not admitted as a second scale of the same cell. The same
    holds the other way round; the same scale spelled differently in the row and the heading is no conflict; a row
    without a unit takes the heading's; a row naming two scales is excluded as ambiguous."""
    pages, layout = unit_page('в тысячах рублей', 'Выручка, млн рублей')
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[], []]
    fact, _ = candidate(PIN, pages, row_revenue(pages, 'Выручка, млн рублей', 'млн рублей'), layout)
    assert fact['amount'] == dict(value='1403', unit='money', currency='RUB', scale='1000000')
    assert refused(row_revenue(pages, 'Выручка, млн рублей', 'в тысячах рублей'), pages, layout) == ('unit_mismatch', ROW_UNIT)
    pages, layout = unit_page('млн руб.', 'Выручка, тыс. руб.')
    fact, _ = candidate(PIN, pages, row_revenue(pages, 'Выручка, тыс. руб.', 'тыс. руб.'), layout)
    assert fact['amount']['scale'] == '1000'
    assert refused(row_revenue(pages, 'Выручка, тыс. руб.', 'млн руб.'), pages, layout) == ('unit_mismatch', ROW_UNIT)
    pages, layout = unit_page('млн руб.', 'Выручка, млн ₽')
    for unit_label in ('млн ₽', 'млн руб.'):
        fact, _ = candidate(PIN, pages, row_revenue(pages, 'Выручка, млн ₽', unit_label), layout)
        assert fact['amount']['scale'] == '1000000'
    pages, layout = unit_page('млн руб.', 'Выручка')
    fact, _ = candidate(PIN, pages, row_revenue(pages, 'Выручка', 'млн руб.'), layout)
    assert fact['amount']['scale'] == '1000000'
    # A row without a unit takes the heading's only: a unit printed on the page apart from the table does not apply.
    pages, layout = bare_year_page(('Данные в тыс. руб.', 'Данные в тыс. руб.\n'), ('млн руб.', 'млн руб.'), ('2022 2021', placed((80, '2022'), (105, '2021'))),
                                   ('Выручка 1 403 1 004', placed((0, 'Выручка'), (79, '1 403'), (104, '1 004'))))
    assert candidate(PIN, pages, row_revenue(pages, 'Выручка', 'млн руб.'), layout)[0]['amount']['scale'] == '1000000'
    assert refused(row_revenue(pages, 'Выручка', 'тыс. руб.'), pages, layout) == ('unit_mismatch', FOREIGN_UNIT)
    pages, layout = unit_page('млн руб.', 'Выручка, млн руб. (тыс. долл.)')
    assert refused(row_revenue(pages, 'Выручка, млн руб. (тыс. долл.)', 'млн руб.'), pages, layout) == ('unsupported_scale', 'Строка показателя называет несколько масштабов.')


# Wrapped captions retain their spatial relationship to the year columns.
WRAPPED = ['За 9', 'месяцев, закончившихся 30 сентября']


def wrapped_page(parts, center=None):
    """A revenue table with bare year columns: the unit at the left margin, then the caption printed in the
    given parts on consecutive rows, each at the left margin or centered over the two columns."""
    captions = [(part, placed((0, part) if center is None else centered(part, center))) for part in parts]
    return bare_year_page(('тыс. руб.', 'тыс. руб.'), *captions, ('2022 2021', placed((50, '2022'), (70, '2021'))),
                          ('Выручка 1 403 1 004', placed((0, 'Выручка'), (49, '1 403'), (68, '1 004'))))


def test_a_supported_caption_wrapped_beside_the_columns_is_still_a_caption_never_a_bare_year():
    """The nine-month caption wrapped onto two rows at the left margin, after 'За 9' or after 'За', leaves the
    header unverifiable like the caption printed whole: the structure names both columns null and the server admits
    neither the calendar year nor the nine months, with the year line alone or with both caption lines cited. The same
    two rows centered over the columns verify the nine months and refuse the year; a title wrapped onto two rows without
    a period leaves the bare years explicit; an assets table printed under the wrapped caption keeps its own fact."""
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    for parts in (WRAPPED, ['За', '9 месяцев, закончившихся 30 сентября'], [LEFT_CAPTION]):
        pages, layout = wrapped_page(parts)
        lines = len(pages[1].splitlines())
        first, last = 4, lines - 2   # the caption lines: after the company, the standard and the unit, before the header
        structure = tables.page_tables(pages[1].splitlines(), layout[1])
        assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and structure[0]['rows'] == [dict(line=lines, cells=[1, 2])]
        assert json.loads(extraction_request(pages, 'Выручка', layout)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
        assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=first, end=last), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=first, end=last, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, column=2), pages, layout) == ('ambiguous_link', BESIDE)
    # Positive control of the review: the same two rows centered over the group verify the nine months and nothing else.
    pages, layout = wrapped_page(WRAPPED, 62)
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[4, 5], [4, 5]]
    fact, _ = candidate(PIN, pages, revenue_of(pages, start=4, end=5, period=nine_months), layout)
    assert fact['period'] == nine_months and fact['amount']['scale'] == '1000'
    assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', UNCITED)
    assert refused(revenue_of(pages, start=4, end=5), pages, layout)[0] == 'period_mismatch'
    # A title wrapped onto two rows without a period assigns nothing and forbids nothing.
    pages, layout = wrapped_page(['Отчет о прибылях', 'и убытках'])
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[], []]
    fact, _ = candidate(PIN, pages, revenue_of(pages), layout)
    assert fact['period']['end'] == '2022-12-31' and fact['amount']['scale'] == '1000'
    # The assets table under the wrapped caption: its own bare years stay explicit, its rows never complete the caption.
    pages, layout = wrapped_page(WRAPPED)
    pages, layout = {1: pages[1] + ASSETS_TEXT}, {1: layout[1] + ASSETS_LAYOUT}
    structure = tables.page_tables(pages[1].splitlines(), layout[1])
    assert [[c['period_lines'] for c in table['columns']] for table in structure] == [[None, None], [[], []]]
    assets = assets_item()
    assets['evidence'] = [dict(page=1, line_start=1, line_end=10, area='Две таблицы')]
    assets['table'].update(row_line=10, year_line=9, period_line_start=9, period_line_end=9)
    fact, _ = candidate(PIN, pages, assets, layout)
    assert fact['amount'] == dict(value='71', unit='money', currency='RUB', scale='1000000') and fact['period']['end'] == '2024-12-31'
    revenue = revenue_of(wrapped_page(WRAPPED)[0])   # the 1 403 of the revenue table: lines 1..7 of the combined page
    assert refused(revenue, pages, layout) == ('ambiguous_link', BESIDE)


# The parts of a caption beside the columns
# belong to one block by position (overlapping extents, whatever rows lie between), never by row adjacency alone or
# by the words of another block.
GAP_HEADER = ('2022 2021', placed((50, '2022'), (70, '2021')))
GAP_ROW = ('Выручка 1 403 1 004', placed((0, 'Выручка'), (49, '1 403'), (68, '1 004')))
# Annual control fact saved by an earlier extractor from the same layout,
# excluding technical IDs and the extraction version.
SEPARATE_BLOCKS_CONTROL = Path(__file__).resolve().parent / 'fixtures' / 'flow01' / 'separate-blocks-control.json'


def gapped_page(blank_rows, center=None):
    """The wrapped nine-month caption with blank layout rows between its parts, at the left margin or centered over the
    two year columns, over the bare-year revenue table with the unit in its header row; the plain text has no blank
    lines: 3 the first part, 4 the second, 5 the header, 6 the row."""
    first = placed((0, WRAPPED[0]) if center is None else centered(WRAPPED[0], center)) + '\n' * blank_rows
    second = placed((0, WRAPPED[1]) if center is None else centered(WRAPPED[1], center))
    header = ('тыс. руб. 2022 2021', placed((0, 'тыс. руб.'), (50, '2022'), (70, '2021')))
    return bare_year_page((WRAPPED[0], first), (WRAPPED[1], second), header, GAP_ROW)


def side_blocks_page(left, right, right_at=110):
    """A revenue table with bare year columns between two annotation blocks of three rows each: one at the
    left margin, the other at right_at, beyond the columns or in a second margin column. Each block is a list of the
    four rows it prints ('' for a row it leaves blank); neither captions the period of the revenue columns."""
    annotations = [(' '.join(part for part in (l, r) if part), placed((0, l), (right_at, r))) for l, r in zip(left, right)]
    return bare_year_page(('тыс. руб.', 'тыс. руб.'), *annotations, GAP_HEADER, GAP_ROW)


def stated(fact):
    """A fact apart from its technical IDs and the processing version."""
    content = {k: v for k, v in fact.items() if k not in ('fact_id', 'extraction_version')}
    content['evidence'] = [{k: v for k, v in e.items() if k != 'evidence_id'} for e in fact['evidence']]
    return content


def test_the_parts_of_a_caption_form_one_block_by_position_not_by_adjacent_rows_or_by_words_of_other_blocks():
    """A blank layout row between 'За 9' and 'месяцев, закончившихся 30 сентября', both inside the heading
    window, hides no caption: the header is unverifiable and the server admits neither the calendar year nor the nine
    months, with the year line alone or with both parts cited. The same parts centered over the columns with the same
    gap verify the nine months. 'За 9' of a left annotation block and 'месяцев' of a right one, printed on
    neighboring rows on either side of the year columns, form no caption: the columns stay bare years and the annual
    fact is saved with the content of the control; the blocks swapped, or printed apart in one margin, give the same."""
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    for blank_rows in (1, 2):
        pages, layout = gapped_page(blank_rows)
        structure = tables.page_tables(pages[1].splitlines(), layout[1])
        assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and structure[0]['rows'] == [dict(line=6, cells=[1, 2])]
        assert json.loads(extraction_request(pages, 'Выручка', layout)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
        assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=3, end=4), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, start=3, end=4, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE)
        assert refused(revenue_of(pages, column=2), pages, layout) == ('ambiguous_link', BESIDE)
    # Positive control: the same two parts centered over the group with the blank row between verify the nine months.
    pages, layout = gapped_page(1, 62)
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[3, 4], [3, 4]]
    fact, _ = candidate(PIN, pages, revenue_of(pages, start=3, end=4, period=nine_months), layout)
    assert fact['period'] == nine_months and fact['amount']['scale'] == '1000'
    assert 'За 9\nмесяцев, закончившихся 30 сентября\nтыс. руб. 2022 2021' in [e['quote'] for e in fact['evidence']]
    assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', UNCITED)
    assert refused(revenue_of(pages, start=3, end=4), pages, layout)[0] == 'period_mismatch'
    # The left block 'Срок оплаты / За 9 / дней', the right block 'Возраст / месяцев / 12'.
    pages, layout = side_blocks_page(['Срок оплаты', 'За 9', '', 'дней'], ['Возраст', '', 'месяцев', '12'])
    assert pages[1] == 'Synthetic Company\nМСФО\nтыс. руб.\nСрок оплаты Возраст\nЗа 9\nмесяцев\nдней 12\n2022 2021\nВыручка 1 403 1 004'
    structure = tables.page_tables(pages[1].splitlines(), layout[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [[], []] and structure[0]['rows'] == [dict(line=9, cells=[1, 2])]
    assert json.loads(extraction_request(pages, 'Выручка', layout)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    annual, _ = candidate(PIN, pages, revenue_of(pages), layout)
    control = json.loads(SEPARATE_BLOCKS_CONTROL.read_text())
    assert control['extraction_version'] == 'pdf-lines.v14' and annual['extraction_version'] == VERSION
    assert stated(annual) == stated(control)
    assert annual['amount'] == dict(value='1403', unit='money', currency='RUB', scale='1000') and annual['period']['end'] == '2022-12-31'
    # The same words with the blocks swapped, and both blocks in the left margin 20 characters apart: no caption either.
    quotes = sorted(e['quote'] for e in annual['evidence'] if e['locator']['area'] in ('Строка таблицы и выбранный годовой столбец', 'Общий период и годы столбцов'))
    assert quotes == ['2022 2021', 'Выручка 1 403 1 004']
    for pages, layout in (side_blocks_page(['Возраст', '', 'месяцев', '12'], ['Срок оплаты', 'За 9', '', 'дней']),
                          side_blocks_page(['Срок оплаты', 'За 9', '', 'дней'], ['Возраст', '', 'месяцев', '12'], right_at=20)):
        assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[], []]
        fact, _ = candidate(PIN, pages, revenue_of(pages), layout)
        assert (fact['amount'], fact['period'], fact['raw_text'], fact['limitations']) == (annual['amount'], annual['period'], annual['raw_text'], annual['limitations'])
        assert sorted(e['quote'] for e in fact['evidence'] if e['locator']['area'] in ('Строка таблицы и выбранный годовой столбец', 'Общий период и годы столбцов')) == quotes
    # The same words as parts of one block: 'За 9' over 'месяцев' in the left margin is a caption of the supported kind.
    pages, layout = side_blocks_page(['Срок оплаты', 'За 9', 'месяцев', 'дней'], ['Возраст', '', '', '12'])
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [None, None]
    assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', BESIDE)


# A part of the supported caption that its
# own block leaves unresolved is a caption whose belonging the position has not settled, never a confirmed bare year.
CONTINUATION = 'месяцев, закончившихся 30 сентября'


def indented_page(offset, continuation=CONTINUATION):
    """A synthetic page: 'За 9' at the left margin, the continuation on the next row indented by offset
    layout characters, two blank rows, the header row with the unit at the left and the revenue row. Lines: 3 the first
    part, 4 the continuation, 7 the header, 8 the row."""
    return bare_year_page(('За 9', 'За 9'), (continuation, placed((offset, continuation))), ('', ''), ('', ''),
                          ('тыс. руб. 2022 2021', placed((20, 'тыс. руб.'), (50, '2022'), (70, '2021'))), GAP_ROW)


def test_a_part_of_the_caption_its_block_leaves_unresolved_is_no_proof_of_a_bare_year():
    """The continuation 'месяцев, закончившихся 30 сентября' indented beyond the grid tolerance stands in a
    block of its own, apart from 'За 9', both inside the heading window. The parts not joined by position are no
    confirmed absence of a caption: the structure names both columns unverifiable and the server admits neither the
    calendar year nor the nine months, with the year line alone (the locator of the probe) or with both parts cited, each
    locator checked on its own. The continuation printed under the first part (the control of the probe) keeps the same
    refusal. The same geometry with a continuation of no supported caption ('дней после отчетной даты') leaves the bare
    years explicit: the indent forbids nothing by itself, the unresolved part of the supported form does."""
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    for offset in (12, 0):
        pages, layout = indented_page(offset)
        assert pages[1] == 'Synthetic Company\nМСФО\nЗа 9\n' + CONTINUATION + '\n\n\nтыс. руб. 2022 2021\nВыручка 1 403 1 004'
        structure = tables.page_tables(pages[1].splitlines(), layout[1])
        assert [c['period_lines'] for c in structure[0]['columns']] == [None, None], (offset, structure)
        assert structure[0]['rows'] == [dict(line=8, cells=[1, 2])]
        assert json.loads(extraction_request(pages, 'Выручка', layout)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
        assert refused(revenue_of(pages), pages, layout) == ('ambiguous_link', BESIDE), offset
        assert refused(revenue_of(pages, column=2), pages, layout) == ('ambiguous_link', BESIDE), offset
        assert refused(revenue_of(pages, start=3, end=4), pages, layout) == ('ambiguous_link', BESIDE), offset
        assert refused(revenue_of(pages, start=3, end=4, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE), offset
        assert refused(revenue_of(pages, start=3, end=7, period=nine_months), pages, layout) == ('ambiguous_link', BESIDE), offset
    # Positive control: 'За 9' over an indented continuation that completes no supported caption is a note beside the
    # columns, and the bare years stay explicit with the unit of the header row.
    pages, layout = indented_page(12, 'дней после отчетной даты')
    assert [c['period_lines'] for c in tables.page_tables(pages[1].splitlines(), layout[1])[0]['columns']] == [[], []]
    annual, _ = candidate(PIN, pages, revenue_of(pages), layout)
    assert annual['amount'] == dict(value='1403', unit='money', currency='RUB', scale='1000') and annual['period']['end'] == '2022-12-31'
    quotes = sorted(e['quote'] for e in annual['evidence'] if e['locator']['area'] in ('Строка таблицы и выбранный годовой столбец', 'Общий период и годы столбцов'))
    assert quotes == ['Выручка 1 403 1 004', 'тыс. руб. 2022 2021']
