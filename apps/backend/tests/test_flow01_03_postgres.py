"""FLOW-01.03 through the standard path: grouped period headers appear above the table rows and can be extracted
after them. Cells and local refusals are checked on the saved layout from synthetic positioned PDFs. No model, no paid request."""
from copy import deepcopy
import io
import json
import os

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app import tables
from app.errors import ApiError
from app.extraction import APART, VERSION, completed_requests, ingest as ingest_payload, source_pages
from app.facts import pin_of
from app.service import ident
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_flow01_02_postgres import checkpoint, coverage_findings, extract, note, summary
from test_perf02_r1_postgres import accounted_response, fact_rows, glyph_name
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

COMPANY = 'Synthetic Chemicals'
TITLE = 'Synthetic Chemicals: отчет о прибыли'
SOURCE = 'Источник: отчетность Synthetic Chemicals по МСФО'
PASSAGE = 'Компания производит химические реагенты для промышленности.'
CAPTION_12, CAPTION_6 = 'За 12 месяцев, закончившихся', 'За 6 месяцев'
GROUPS = [(CAPTION_12, ['31 декабря 2022', '31 декабря 2023']), (CAPTION_6, ['30 июня 2024'])]
ROWS = [('Выручка от продаж', ['8 120', '9 345', '5 010']), ('Себестоимость', ['(4 100)', '(4 870)', '(2 600)']),
        ('Чистая прибыль', ['1 230', '1 480', '1 480'])]
PERIODS = {1: ('За 12 месяцев, закончившихся 31 декабря 2022', '2022-01-01', '2022-12-31'),
           2: ('За 12 месяцев, закончившихся 31 декабря 2023', '2023-01-01', '2023-12-31'),
           3: ('За 6 месяцев 30 июня 2024', '2024-01-01', '2024-06-30')}
PITCH = 3.8   # points per layout character of pypdf's grid at 12 pt
LEFT = 40


def positioned_pdf(pages):
    """A landscape PDF whose text objects stand at given points; each page is a list of (text, x, y) in stream order."""
    chars = sorted({char for items in pages for text, _, _ in items for char in text if ord(char) > 127})
    codes = {char: 128 + index for index, char in enumerate(chars)}
    writer = PdfWriter()
    differences = ArrayObject([NumberObject(128), *(NameObject('/' + glyph_name(char)) for char in chars)])
    encoding = DictionaryObject({NameObject('/Type'): NameObject('/Encoding'), NameObject('/BaseEncoding'): NameObject('/WinAnsiEncoding'),
                                 NameObject('/Differences'): differences})
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                              NameObject('/BaseFont'): NameObject('/Helvetica'), NameObject('/Encoding'): writer._add_object(encoding)}))

    def encoded(line):
        out = bytearray()
        for char in line:
            byte = codes.get(char, ord(char))
            if byte in (0x28, 0x29, 0x5c):
                out += b'\\'
            out.append(byte)
        return bytes(out)
    for items in pages:
        page = writer.add_blank_page(width=842, height=595)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b' '.join(f'BT /F1 12 Tf 1 0 0 1 {x} {y} Tm ('.encode() + encoded(text) + b') Tj ET' for text, x, y in items))
        page[NameObject('/Contents')] = writer._add_object(stream)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def table_page(groups, rows, order, *, passage=None, caption_shift=0, number='7'):
    """The text objects of an Ozon-class statement page: a title, the rows, the page number, the unit, the group
    captions centered over their columns and the column headers, the source line. rows_first emits the rows
    before the header, as the Ozon PDF does; headers_first emits the header first. caption_shift moves the last
    caption off its group."""
    headers = [header for _, header_cells in groups for header in header_cells]
    centers = [90 + 32 * index for index in range(len(headers))]

    def centered(text, column_center, y):
        return text, round(LEFT + (column_center - len(text) / 2) * PITCH, 1), y
    header_items, index = [('млн руб.', LEFT, 505)], 0
    for position, (caption, header_cells) in enumerate(groups):
        span = (centers[index] - len(header_cells[0]) / 2, centers[index + len(header_cells) - 1] + len(header_cells[-1]) / 2)
        shift = caption_shift if position == len(groups) - 1 else 0
        header_items.append(centered(caption, (span[0] + span[1]) / 2 + shift, 520))
        header_items.extend(centered(header, centers[index + k], 505) for k, header in enumerate(header_cells))
        index += len(header_cells)
    row_items = []
    for n, (label, cells) in enumerate(rows):
        y = 480 - 18 * n
        row_items.append((label, LEFT, y))
        row_items.extend(centered(cell, centers[k], y) for k, cell in enumerate(cells) if cell)
    title, source, page_number = [(TITLE, LEFT, 560)], [(SOURCE, LEFT, 60)], [(number, 800, 30)]
    statement = [(passage, LEFT, 100)] if passage else []
    if order == 'rows_first':
        return title + row_items + page_number + header_items + statement + source
    return title + header_items + row_items + statement + source + page_number


def uploaded(w, title, pages):
    return uploaded_pdf(w, title, positioned_pdf(pages))


def uploaded_pdf(w, title, data):
    version = w.service.write(w.actor, 'uploadSource', ident('key'), dict(title=title, expected_revision=0), w.project, None,
                              data, title + '.pdf')[1]['version']
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        text = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    return version, run, text, layouts


def line_of(text, page, needle):
    """The number of the one original line equal to needle on this page."""
    numbers = [n for n, line in enumerate(text[page].splitlines(), 1) if line.strip() == needle]
    assert len(numbers) == 1, (needle, numbers)
    return numbers[0]


def structure_of(text, layouts, page):
    """The table structure the extractor receives for this page, as the server computes it."""
    structure = tables.page_tables(text[page].splitlines(), layouts[page])
    assert len(structure) == 1, structure
    return structure[0]


def cell(text, layouts, page, metric, column, *, period=None, value_text=None, value=None, locator=None):
    """A table candidate built from the structure the extractor receives, with the actual line numbers of this PDF."""
    table = structure_of(text, layouts, page)
    row_line = line_of(text, page, next(line for line in text[page].splitlines() if line.startswith(metric + ' ')).strip())
    entry = table['columns'][column - 1]
    cells = dict((row['line'], row['cells']) for row in table['rows'])[row_line]
    value_text = value_text or printed_cell(text, page, row_line, cells, column)
    label, start, end = PERIODS[column]
    header_lines = [entry['year_line'], *entry['period_lines']]
    return dict(metric=metric, subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                period=period or dict(kind='interval', label=label, start=start, end=end),
                amount=dict(value=value or value_text.replace(' ', ''), unit='money', currency='RUB'), value_text=value_text,
                evidence=[dict(page=page, line_start=row_line, line_end=row_line, area='Строка таблицы'),
                          dict(page=page, line_start=min(header_lines), line_end=max(header_lines), area='Подпись периода и заголовок столбцов'),
                          dict(page=page, line_start=line_of(text, page, SOURCE), line_end=line_of(text, page, SOURCE), area='Источник')],
                basis=dict(page=page, metric_label=metric, unit_label='млн руб.', reporting_label='МСФО'),
                table=locator or dict(page=page, row_line=row_line, year_line=entry['year_line'], period_line_start=min(entry['period_lines']),
                                      period_line_end=max(entry['period_lines']), column=column))


def printed_cell(text, page, row_line, cells, column):
    """The amount printed in this column of the row: the structure maps the row's amount cells to columns."""
    numbers = [token for token in text[page].splitlines()[row_line - 1].split(' ') if token and (token[0].isdigit() or token[0] in '(-')]
    # Amounts of two tokens ("9 345") are joined: the row lists its cells in print order after the label.
    joined, current = [], ''
    for token in numbers:
        if current and token.isdigit() and len(token) == 3:
            current += ' ' + token
        elif current and token.endswith(')') and len(token) == 4:
            current += ' ' + token
        else:
            if current:
                joined.append(current)
            current = token
    joined.append(current)
    return joined[cells.index(column)]


def passage(text, page):
    line = line_of(text, page, PASSAGE)
    return dict(metric=None, subject=COMPANY, reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None, basis=None,
                evidence=[dict(page=page, line_start=line, line_end=line, area='Бизнес')])


def ingest(w, run, text, layouts, items, question, call_id=None):
    payload = dict(facts=deepcopy(items))
    original = deepcopy(payload)
    call_id = call_id or accounted_response(w, run, payload)
    outcome = ingest_payload(w.runs, run, text, payload, call_id, layouts=layouts, question=question)
    assert payload == original
    return outcome, call_id


def observation(fact):
    """What a verified cell states, apart from its technical IDs and the line numbers of this text order: the
    attributes, the cited row, header and source and the quotes the table check adds. The anchors the server adds
    for the issuer, metric and unit quote the first original passage containing the phrase, which depends on the
    text order by construction and is not part of the observation."""
    quotes = sorted(e['quote'] for e in fact['evidence'] if not e['locator']['area'].startswith('Исходный атрибут'))
    return (fact['metric'], fact['amount']['value'], fact['amount']['scale'], fact['amount']['currency'], fact['period']['label'],
            fact['period']['start'], fact['period']['end'], fact['reporting_basis'], tuple(quotes), tuple(fact['limitations']))


def test_the_same_table_in_two_text_orders_gives_the_same_observations_from_the_actual_lines_of_each(workspace):
    """Check 3: the rows-first PDF (the Ozon order) and the header-first PDF have the same visible layout; the standard
    path yields different line numbers, the structure names each PDF's own lines, and the verified observations agree."""
    w = workspace
    seen = {}
    for order in ('rows_first', 'headers_first'):
        version, run, text, layouts = uploaded(w, 'flow03-' + order, [table_page(GROUPS, ROWS, order)])
        lines = text[1].splitlines()
        assert lines.index(CAPTION_12) > lines.index('Выручка от продаж 8 120 9 345 5 010') if order == 'rows_first' else \
            lines.index(CAPTION_12) < lines.index('Выручка от продаж 8 120 9 345 5 010')
        table = structure_of(text, layouts, 1)
        assert [(c['column'], c['label']) for c in table['columns']] == [(1, '31 декабря 2022'), (2, '31 декабря 2023'), (3, '30 июня 2024')]
        assert table['columns'][0]['period_lines'] == table['columns'][1]['period_lines'] == [line_of(text, 1, CAPTION_12)]
        assert table['columns'][2]['period_lines'] == [line_of(text, 1, CAPTION_6)] and table['columns'][2]['year_line'] == line_of(text, 1, '30 июня 2024')
        assert [row['cells'] for row in table['rows']] == [[1, 2, 3]] * 3
        items = [cell(text, layouts, 1, 'Выручка от продаж', column) for column in (1, 2, 3)] + [cell(text, layouts, 1, 'Чистая прибыль', 3)]
        outcome, _ = ingest(w, run, text, layouts, items, 'Выручка и прибыль ' + order)
        assert outcome['status'] == 'complete' and outcome['skipped'] == [], outcome['skipped']
        facts = {observation(f): f for f in outcome['facts']}
        assert {(o[0], o[1], o[6]) for o in facts} == {('Выручка от продаж', '8120', '2022-12-31'), ('Выручка от продаж', '9345', '2023-12-31'),
                                                       ('Выручка от продаж', '5010', '2024-06-30'), ('Чистая прибыль', '1480', '2024-06-30')}
        assert all(o[2] == '1000000' and o[3] == 'RUB' and f['extraction_version'] == VERSION and f['source'] == pin_of(version) for o, f in facts.items())
        seen[order] = facts
        w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    assert set(seen['rows_first']) == set(seen['headers_first'])
    assert {f['fact_id'] for f in seen['rows_first'].values()}.isdisjoint({f['fact_id'] for f in seen['headers_first'].values()})
    half_year = next(f for o, f in seen['rows_first'].items() if o[1] == '5010')
    assert half_year['period'] == dict(kind='interval', label='За 6 месяцев 30 июня 2024', start='2024-01-01', end='2024-06-30')
    assert 'Выручка от продаж 8 120 9 345 5 010' in [e['quote'] for e in half_year['evidence']]
    assert CAPTION_6 + '\n30 июня 2024' in [e['quote'] for e in half_year['evidence']]


def test_swapped_groups_and_an_equal_amount_keep_each_column_with_its_own_period(workspace):
    """Check 2: the half-year group first, an amount equal in two periods: the links hold, a period claimed from the
    equal amount of the other column is refused, as are the year for the half-year column and the other group."""
    w = workspace
    groups = [(CAPTION_6, ['30 июня 2024']), (CAPTION_12, ['31 декабря 2022', '31 декабря 2023'])]
    rows = [('Выручка от продаж', ['5 010', '8 120', '9 345']), ('Чистая прибыль', ['1 480', '1 230', '1 480'])]
    periods = {1: PERIODS[3], 2: PERIODS[1], 3: PERIODS[2]}
    version, run, text, layouts = uploaded(w, 'flow03-swapped', [table_page(groups, rows, 'rows_first')])

    def swapped(metric, column, **changes):
        label, start, end = periods[column]
        changes.setdefault('period', dict(kind='interval', label=label, start=start, end=end))
        return cell(text, layouts, 1, metric, column, **changes)
    good = [swapped('Выручка от продаж', c) for c in (1, 2, 3)] + [swapped('Чистая прибыль', 1), swapped('Чистая прибыль', 3)]
    equal_half = swapped('Чистая прибыль', 3, period=dict(kind='interval', label=PERIODS[3][0], start=PERIODS[3][1], end=PERIODS[3][2]))
    equal_year = swapped('Чистая прибыль', 1, period=dict(kind='interval', label=PERIODS[2][0], start=PERIODS[2][1], end=PERIODS[2][2]))
    half_as_year = swapped('Выручка от продаж', 1, period=dict(kind='interval', label=PERIODS[3][0], start='2024-01-01', end='2024-12-31'))
    other_group = swapped('Выручка от продаж', 1)
    other_group['table'].update(year_line=good[1]['table']['year_line'], period_line_start=good[1]['table']['period_line_start'],
                                period_line_end=good[1]['table']['period_line_end'])
    outcome, _ = ingest(w, run, text, layouts, [*good, equal_half, equal_year, half_as_year, other_group], 'Подмена периода')
    status, facts, skipped = summary(outcome)
    assert status == 'partial'
    assert facts == [('Выручка от продаж', '5010'), ('Выручка от продаж', '8120'), ('Выручка от продаж', '9345'), ('Чистая прибыль', '1480'), ('Чистая прибыль', '1480')]
    assert [(s['candidate'], s['code']) for s in outcome['skipped']] == [(6, 'period_mismatch'), (7, 'period_mismatch'), (8, 'period_mismatch'), (9, 'ambiguous_link')]
    profits = sorted((f['period']['end'], f['fact_id']) for f in outcome['facts'] if f['metric'] == 'Чистая прибыль')
    assert [end for end, _ in profits] == ['2023-12-31', '2024-06-30'] and profits[0][1] != profits[1][1]
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_an_ambiguous_table_is_excluded_while_the_number_and_the_passage_of_the_batch_are_saved(workspace, provider, tmp_path):
    """Check 5: a page whose caption stands off its group and a candidate citing both groups are excluded with their
    reasons; the verified cell and the qualitative passage of the same batch are saved; the exclusion survives the cache
    and the draft, blocks its acceptance, and the unconfirmed number reaches no calculation."""
    w, p = workspace, provider
    pages = [table_page(GROUPS, ROWS, 'rows_first', passage=PASSAGE), table_page(GROUPS, ROWS, 'rows_first', caption_shift=9, number='8')]
    version, run, text, layouts = uploaded(w, 'flow03-ambiguous', pages)
    unassigned = tables.page_tables(text[2].splitlines(), layouts[2])
    assert [c['period_lines'] for c in unassigned[0]['columns']] == [None] * 3 and len(unassigned[0]['rows']) == 3
    good = cell(text, layouts, 1, 'Выручка от продаж', 2)
    both_groups = cell(text, layouts, 1, 'Выручка от продаж', 3)
    both_groups['table'].update(period_line_start=min(line_of(text, 1, CAPTION_12), line_of(text, 1, CAPTION_6)),
                                period_line_end=max(line_of(text, 1, '31 декабря 2022 31 декабря 2023'), line_of(text, 1, '30 июня 2024')))
    # The second page prints the same rows and header lines at the same line numbers, without the passage before the source.
    shifted = cell(text, layouts, 1, 'Чистая прибыль', 3)
    first, second = text[1].splitlines(), text[2].splitlines()
    assert first[:4] == second[:4] and first[5:10] == second[5:10] and (first[4], second[4]) == ('7', '8')
    shifted['evidence'] = [dict(e, page=2, **({'line_start': line_of(text, 2, SOURCE), 'line_end': line_of(text, 2, SOURCE)} if e['area'] == 'Источник' else {}))
                           for e in shifted['evidence']]
    shifted['basis']['page'] = 2
    shifted['table'] = dict(shifted['table'], page=2)
    question = 'Выручка, прибыль и бизнес'
    reply, tool_id = extract(w, p, run, question, [passage(text, 1), good, both_groups, shifted], tmp_path)
    assert reply['ok'], reply
    result = reply['result']
    status, facts, skipped = summary(result)
    assert status == 'partial' and facts == [('Выручка от продаж', '9345'), (PASSAGE, None)]
    assert [(s['candidate'], s['code']) for s in result['skipped']] == [(3, 'ambiguous_link'), (4, 'ambiguous_link')]
    assert 'не относится к группе' in result['skipped'][0]['message'] and 'не распределены по группам' in result['skipped'][1]['message']
    assert result['skipped'][1]['observation']['period']['end'] == '2024-06-30'
    # The request carried the structure of both pages: the second page names no citable caption for its groups.
    sent = json.loads(p.sent[-1]['messages'][1]['content'])
    assert sent['pages'][0]['table_layout'] == tables.page_tables(text[1].splitlines(), layouts[1])
    assert sent['pages'][1]['table_layout'] == tables.page_tables(text[2].splitlines(), layouts[2])
    rows = fact_rows(w)
    assert len(rows) == 2 and not any(row['data']['amount'] and row['data']['amount']['value'] == '5010' for row in rows)
    # The same tool operation replays, another Run reads the cache: the same facts, the same exclusions, no POST.
    assert extract(w, p, run, question, [], tmp_path, tool_id)[0] == reply and len(p.sent) == 1
    second = start_note_run(w, version)
    cached, _ = extract(w, p, second, question, [], tmp_path)
    assert cached['result'] == result and len(p.sent) == 1
    # The excluded numbers have no fact: a calculation can reference the verified cell only.
    numeric = next(f for f in result['facts'] if f['amount'])
    refused = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_calculate', dict(
        formula_id='growth_percent.v1', rounding=dict(places=2, mode='HALF_UP'), inputs=[
            dict(name='previous', fact=dict(source_version_id=version['id'], fact_id=numeric['fact_id'])),
            dict(name='current', fact=dict(source_version_id=version['id'], fact_id='fact_' + 'b' * 32))]))
    assert not refused['ok'] and refused['error']['code'] == 'resource_not_found'
    qualitative = next(f for f in result['facts'] if not f['amount'])
    saved = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative, PASSAGE)))
    assert saved['ok'], saved
    gaps = coverage_findings(saved['result'])
    assert len(gaps) == 1 and '2 из 2 исключенных кандидатов' in gaps[0]['message'] and 'ambiguous_link' in gaps[0]['message']
    assert saved['result']['review']['acceptance'] is None and saved['result']['checks'][0]['status'] == 'passed'
    reloaded = w.runs.version(w.actor, w.project, saved['result']['version']['id'])
    assert coverage_findings(reloaded) == gaps
    with w.engine.connect() as c:
        entries = completed_requests(w.runs, c, second, pin_of(version), question)
    assert len(entries) == 1 and [s['code'] for s in entries[0]['skipped']] == ['ambiguous_link', 'ambiguous_link']


@pytest.mark.parametrize('defect', ['row_line', 'year_line', 'period_line_start', 'pin'])
def test_a_nonexistent_reference_or_a_wrong_pin_in_a_mixed_batch_refuses_the_whole_batch(workspace, defect):
    """Check 4, review F02/F03: with the verifiable Ozon-class table beside them, a line the source does not have or
    a wrong SHA still refuses the whole new batch before any write."""
    w = workspace
    version, run, text, layouts = uploaded(w, 'flow03-strict-' + defect, [table_page(GROUPS, ROWS, 'rows_first', passage=PASSAGE)])
    good = cell(text, layouts, 1, 'Выручка от продаж', 2)
    items = [passage(text, 1), good]
    pin = pin_of(version)
    if defect == 'pin':
        pin = dict(pin, sha256='0' * 64)
    else:
        bad = cell(text, layouts, 1, 'Чистая прибыль', 3)
        bad['table'][defect] = 999
        items.append(bad)
    payload = dict(facts=deepcopy(items))
    original = deepcopy(payload)
    call_id = accounted_response(w, run, payload)
    before = checkpoint(w, run)
    with pytest.raises(ApiError) as refused:
        ingest_payload(w.runs, run, text, payload, call_id, pin=pin, layouts=layouts, question='Строгий отказ')
    assert refused.value.code == 'validation_failed'
    message = refused.value.body['error']['message']
    assert message == ('Версия источника или SHA не совпадают.' if defect == 'pin' else 'Факт 3 (Чистая прибыль): Строки таблицы отсутствуют в источнике.')
    assert fact_rows(w) == [] and payload == original and checkpoint(w, run) == before
    outcome, _ = ingest(w, run, text, layouts, [passage(text, 1), good], 'Пригодный пакет')
    assert outcome['status'] == 'complete' and len(outcome['facts']) == 2 and len(fact_rows(w)) == 2
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
