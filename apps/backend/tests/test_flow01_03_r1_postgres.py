"""Period and unit validation through PDF parsing and ingestion.

A locator naming the year line only does not turn a nine-month column into a calendar year. The unit of
a cell is the one printed in its own table's heading or row; two tables of one page keep their own units. A
caption line printed twice on the page leaves its column unverifiable without aborting the request for the rest of
the page. Synthetic positioned PDFs, accounted responses without transport and the loopback provider; no model, no
paid request."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app import tables
from app.extraction import extraction_request
from app.runs import json_value
from app.service import ident
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_flow01_02_postgres import coverage_findings, extract, note, summary
from test_flow01_02_r1_postgres import blocked
from test_flow01_03_postgres import COMPANY, LEFT, PASSAGE, PITCH, SOURCE, ingest, line_of, passage, table_page, uploaded
from test_perf02_r1_postgres import fact_rows
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

CAPTION = 'За 9 месяцев, закончившихся 30 сентября'
PARTS = ['За 9 месяцев,', 'закончившихся 30 сентября']
# An assets table printed under the revenue table: a bare-year header, its own unit in the heading directly above.
ASSETS = [('Активы, млн руб.', LEFT, 330), ('2025', LEFT + 88 * PITCH, 310), ('2024', LEFT + 120 * PITCH, 310),
          ('Активы', LEFT, 290), ('71', LEFT + 89 * PITCH, 290), ('68', LEFT + 121 * PITCH, 290)]
UNCITED = 'Над выбранным столбцом стоит подпись периода, которую locator не цитирует.'
UNPLACED = 'Подпись периода над выбранным столбцом не имеет однозначной исходной строки.'
FOREIGN_UNIT = 'Единица не напечатана в заголовке выбранной таблицы или в строке показателя.'


def revenue_table(*, split_caption=False):
    """The nine-month revenue table in thousands of roubles with the header printed first and a passage below; the
    caption printed whole, or in two lines each centered over the two columns."""
    items = table_page([(CAPTION, ['2022', '2021'])], [('Выручка от продаж', ['1 403', '1 004'])], 'headers_first', passage=PASSAGE)
    items = [('тыс. руб.' if text == 'млн руб.' else text, x, y) for text, x, y in items]
    if split_caption:
        caption_x = next(x for text, x, _ in items if text == CAPTION)
        center = caption_x + len(CAPTION) * PITCH / 2
        items = [entry for entry in items if entry[0] != CAPTION]
        items += [(PARTS[0], round(center - len(PARTS[0]) * PITCH / 2, 1), 538),
                  (PARTS[1], round(center - len(PARTS[1]) * PITCH / 2, 1), 520)]
    return items


def evidence_dir():
    return Path(os.environ.get('AIST_FLOW01_EVIDENCE', '/tmp'))


def revenue_cell(text, layouts, caption_lines):
    """The 1 403 of the revenue table: its row, the header line of column 1 and the cited caption lines."""
    column = tables.page_tables(text[1].splitlines(), layouts[1])[0]['columns'][0]
    row = line_of(text, 1, 'Выручка от продаж 1 403 1 004')
    source = line_of(text, 1, SOURCE)
    header = [column['year_line'], *caption_lines]
    return dict(metric='Выручка от продаж', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                amount=dict(value='1403', unit='money', currency='RUB'), value_text='1 403',
                period=dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30'),
                evidence=[dict(page=1, line_start=row, line_end=row, area='Строка'),
                          dict(page=1, line_start=min(header), line_end=max(header), area='Период'),
                          dict(page=1, line_start=source, line_end=source, area='Источник')],
                basis=dict(page=1, metric_label='Выручка от продаж', unit_label='тыс. руб.', reporting_label='МСФО'),
                table=dict(page=1, row_line=row, year_line=column['year_line'],
                           period_line_start=min(caption_lines), period_line_end=max(caption_lines), column=1))


def assets_cell(text, layouts):
    """The 71 of the assets table: a bare-year column, a balance at its closing date, the unit of its own heading."""
    column = tables.page_tables(text[1].splitlines(), layouts[1])[1]['columns'][0]
    assert column['period_lines'] == []
    row, year, title = line_of(text, 1, 'Активы 71 68'), line_of(text, 1, '2025 2024'), line_of(text, 1, 'Активы, млн руб.')
    source = line_of(text, 1, SOURCE)
    return dict(metric='Активы', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                amount=dict(value='71', unit='money', currency='RUB'), value_text='71',
                period=dict(kind='instant', label='2025', start='2025-12-31', end='2025-12-31'),
                evidence=[dict(page=1, line_start=min(title, year, row), line_end=max(title, year, row), area='Активы'),
                          dict(page=1, line_start=source, line_end=source, area='Источник')],
                basis=dict(page=1, metric_label='Активы', unit_label='млн руб.', reporting_label='МСФО'),
                table=dict(page=1, row_line=row, year_line=year, period_line_start=year, period_line_end=year, column=1))


def test_false_period_and_scale_never_become_saved_facts(workspace, provider, tmp_path):
    """Period and unit checks through the saved PDF: the nine-month revenue in thousands and the assets in millions keep their own
    units; the year-line-only locator with the calendar year and the unit of the other table are excluded with their
    reasons; the passage is saved; the repeat of the call and another Run's cache keep the diagnostics; the draft
    carries the unclosed gap and is neither accepted nor released, while the gap of the wrong scale is closed by the
    saved fact of the same observation."""
    w, p = workspace, provider
    version, run, text, layouts = uploaded(w, 'R1 period and units', [revenue_table() + ASSETS])
    good = revenue_cell(text, layouts, [line_of(text, 1, CAPTION)])
    false_period = deepcopy(good)
    year_line = good['table']['year_line']
    false_period['table'].update(period_line_start=year_line, period_line_end=year_line)
    false_period['period'].update(label='2022', end='2022-12-31')
    false_scale = deepcopy(good)
    false_scale['basis']['unit_label'] = 'млн руб.'
    items = [good, false_period, false_scale, passage(text, 1), assets_cell(text, layouts)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'R1: период и единицы')
    status, facts, skipped = summary(outcome)
    assert status == 'partial'
    assert facts == sorted([('Активы', '71'), ('Выручка от продаж', '1403'), (PASSAGE, None)], key=lambda pair: (pair[0], pair[1] or ''))
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(2, 'ambiguous_link', UNCITED), (3, 'unit_mismatch', FOREIGN_UNIT)]
    by_value = {f['amount']['value']: f for f in outcome['facts'] if f['amount']}
    assert by_value['1403']['amount']['scale'] == '1000' and by_value['1403']['period']['end'] == '2022-09-30'
    assert by_value['71']['amount']['scale'] == '1000000' and by_value['71']['period'] == dict(kind='instant', label='2025', start='2025-12-31', end='2025-12-31')
    rows = fact_rows(w)
    assert len(rows) == 3
    evidence_dir().joinpath('r1-period-unit-counterexamples.json').write_text(json_value(dict(
        source=version['id'], text=text, layouts=layouts, outcome=outcome, saved=[row['data'] for row in rows])))
    again, _ = ingest(w, run, text, layouts, items, 'R1: период и единицы', call_id)
    assert again == outcome and fact_rows(w) == rows
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    # The same batch through the tool path on the loopback provider: the replay and another Run's cache keep the exclusions.
    question = 'Выручка, активы и бизнес'
    first = start_note_run(w, version)
    reply, tool_id = extract(w, p, first, question, items, tmp_path)
    assert reply['ok'] and summary(reply['result']) == (status, facts, skipped) and len(p.sent) == 1
    assert extract(w, p, first, question, [], tmp_path, tool_id)[0] == reply and len(p.sent) == 1
    second = start_note_run(w, version)
    cached, _ = extract(w, p, second, question, [], tmp_path)
    assert cached['result'] == reply['result'] and len(p.sent) == 1 and len(fact_rows(w)) == 3
    qualitative = next(f for f in cached['result']['facts'] if not f['amount'])
    saved = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative, PASSAGE)))
    assert saved['ok'], saved
    gaps = coverage_findings(saved['result'])
    # The wrong scale named the observation the saved 1 403 states, so its gap is closed; the calendar year 2022 has no basis.
    assert len(gaps) == 1 and '1 из 2' in gaps[0]['message'] and '(ambiguous_link)' in gaps[0]['message']
    assert saved['result']['checks'][0]['status'] == 'passed' and saved['result']['review']['acceptance'] is None
    blocked(w, saved['result'])
    assert coverage_findings(w.runs.version(w.actor, w.project, saved['result']['version']['id'])) == gaps


def test_a_repeated_caption_line_on_a_saved_pdf_does_not_abort_the_request(workspace):
    """The two-line caption printed once verifies the cell. With its first line printed again elsewhere on the
    page the column is unverifiable: the request for the page is still built with the structure of both tables, no
    cell of that column is admitted, the year line alone included, and the assets cell and the passage of the same
    page are saved."""
    w = workspace
    version, run, text, layouts = uploaded(w, 'R1 one caption', [revenue_table(split_caption=True) + ASSETS])
    parts = sorted(line_of(text, 1, part) for part in PARTS)
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [parts, parts] and [c['period_lines'] for c in structure[1]['columns']] == [[], []]
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    outcome, _ = ingest(w, run, text, layouts, [revenue_cell(text, layouts, parts), assets_cell(text, layouts), passage(text, 1)], 'R1: подпись в две строки')
    assert outcome['status'] == 'complete' and outcome['skipped'] == [] and len(outcome['facts']) == 3
    revenue = next(f for f in outcome['facts'] if f['amount'] and f['amount']['value'] == '1403')
    assert revenue['period']['end'] == '2022-09-30' and revenue['amount']['scale'] == '1000'
    assert PARTS[0] + '\n' + PARTS[1] in '\n'.join(e['quote'] for e in revenue['evidence'])
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    version, run, text, layouts = uploaded(w, 'R1 repeated caption', [revenue_table(split_caption=True) + ASSETS + [(PARTS[0], LEFT, 200)]])
    evidence_dir().joinpath('r1-repeated-caption.json').write_text(json_value(dict(source=version['id'], text=text, layouts=layouts)))
    copies = [n for n, line in enumerate(text[1].splitlines(), 1) if line.strip() == PARTS[0]]
    assert len(copies) == 2
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and len(structure[0]['rows']) == 1
    assert [c['period_lines'] for c in structure[1]['columns']] == [[], []] and len(structure[1]['rows']) == 1
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    second_part = line_of(text, 1, PARTS[1])
    whole = revenue_cell(text, layouts, [copies[0], second_part])
    placed_only = revenue_cell(text, layouts, [second_part])
    year_only = revenue_cell(text, layouts, [whole['table']['year_line']])
    year_only['period'].update(label='2022', end='2022-12-31')
    outcome, _ = ingest(w, run, text, layouts, [whole, placed_only, year_only, assets_cell(text, layouts), passage(text, 1)], 'R1: повторенная подпись')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == sorted([('Активы', '71'), (PASSAGE, None)], key=lambda pair: (pair[0], pair[1] or ''))
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(n, 'ambiguous_link', UNPLACED) for n in (1, 2, 3)]
    # The facts of this PDF: the assets cell and the passage only; the 1 403 saved from the first PDF is another source.
    saved = [row['data'] for row in fact_rows(w) if row['data']['source']['source_version_id'] == version['id']]
    assert sorted(f['amount']['value'] if f['amount'] else f['metric'] for f in saved) == sorted(['71', PASSAGE])
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
