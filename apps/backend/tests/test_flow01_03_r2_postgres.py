"""Caption alignment and row units through PDF parsing and ingestion.

A nine-month caption printed at the left margin, beside the year columns, leaves the header unverifiable: no
calendar-year fact and no nine-month fact, while the passage and the assets table printed under it keep their facts.
A unit named in the row label sets the scale of the row; the other unit of the table heading does not create a
second fact of the same cell, and the excluded copy leaves no gap once the correct fact of the same observation is
saved. The readable PDFs with an embedded Cyrillic font are synthetic fixtures in fixtures/flow01; the variants with a
neighbor table or reversed units come from the positioned-PDF generator. No model, no paid request."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app import tables
from app.extraction import extraction_request, source_pages
from app.runs import json_value
from app.service import ident
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_flow01_02_postgres import coverage_findings, extract, note, summary
from test_flow01_03 import BESIDE, ROW_UNIT
from test_flow01_03_postgres import COMPANY, LEFT, PASSAGE, PITCH, SOURCE, ingest, line_of, passage, uploaded
from test_flow01_03_r1_postgres import ASSETS, CAPTION, assets_cell, evidence_dir, revenue_cell, revenue_table
from test_perf02_r1_postgres import fact_rows
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'flow01'


def embedded(w, title, name):
    """A synthetic PDF fixture uploaded through the standard path; its saved text keeps the printed runs of
    spaces between the cells of a line."""
    version = w.service.write(w.actor, 'uploadSource', ident('key'), dict(title=title, expected_revision=0), w.project, None,
                              (FIXTURES / (name + '.pdf')).read_bytes(), name + '.pdf')[1]['version']
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        text = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    return version, run, text, layouts


def printed_line(text, page, needle):
    """The number of the one original line printing needle, whatever the runs of spaces between its cells."""
    numbers = [n for n, line in enumerate(text[page].splitlines(), 1) if ' '.join(line.split()) == ' '.join(needle.split())]
    assert len(numbers) == 1, (needle, numbers)
    return numbers[0]


def saved_of(w, version):
    """The facts saved from this PDF, apart from the facts of the other PDFs of the project."""
    return [row['data'] for row in fact_rows(w) if row['data']['source']['source_version_id'] == version['id']]


def record(name, version, text, layouts, outcome, saved):
    evidence_dir().joinpath(name).write_text(json_value(dict(
        source=version['id'], text=text, layouts=layouts, structure=tables.page_tables(text[1].splitlines(), layouts[1]),
        outcome=outcome, saved=saved)))


def test_a_caption_beside_the_columns_saves_neither_the_year_nor_the_nine_months_and_keeps_the_rest_of_the_page(workspace):
    """The caption stands left of the columns, the structure names both columns unverifiable,
    the calendar year and the nine months are excluded with the caption cited or with the year line alone, the passage
    is saved, the repeat of the call changes nothing. On the generator's page with the assets table under the same
    revenue table the assets cell keeps its fact: the ambiguity of one table does not reach the page."""
    w = workspace
    version, run, text, layouts = embedded(w, 'R2 left caption', 'embedded-left-caption')
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and len(structure[0]['rows']) == 1
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    caption, header, row = (printed_line(text, 1, s) for s in (CAPTION, 'тыс. руб. 2022 2021', 'Выручка от продаж 1 403 1 004'))
    source = printed_line(text, 1, SOURCE)
    assert structure[0]['columns'][0]['year_line'] == header and structure[0]['rows'][0]['line'] == row

    def revenue(period, first):
        return dict(metric='Выручка от продаж', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                    amount=dict(value='1403', unit='money', currency='RUB'), value_text='1 403', period=period,
                    evidence=[dict(page=1, line_start=first, line_end=row, area='Таблица'), dict(page=1, line_start=source, line_end=source, area='Источник')],
                    basis=dict(page=1, metric_label='Выручка от продаж', unit_label='тыс. руб.', reporting_label='МСФО'),
                    table=dict(page=1, row_line=row, year_line=header, period_line_start=first, period_line_end=header, column=1))
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    items = [revenue(dict(kind='interval', label='2022', start='2022-01-01', end='2022-12-31'), header),
             revenue(nine_months, caption), revenue(nine_months, header), passage(text, 1)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'R2: подпись слева')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [(PASSAGE, None)]
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(n, 'ambiguous_link', BESIDE) for n in (1, 2, 3)]
    assert all(s['observation']['metric'] == 'Выручка от продаж' for s in outcome['skipped'])
    saved = saved_of(w, version)
    assert [f['metric'] for f in saved] == [PASSAGE] and saved[0]['amount'] is None
    record('r2-left-caption.json', version, text, layouts, outcome, saved)
    again, _ = ingest(w, run, text, layouts, items, 'R2: подпись слева', call_id)
    assert again == outcome and saved_of(w, version) == saved
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    # The generator's page: the same nine-month caption moved to the left margin, the assets table under it.
    page = [(item, LEFT if item == CAPTION else x, y) for item, x, y in revenue_table()] + ASSETS
    version, run, text, layouts = uploaded(w, 'R2 left caption and assets', [page])
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [[c['period_lines'] for c in table['columns']] for table in structure] == [[None, None], [[], []]]
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    cited = revenue_cell(text, layouts, [line_of(text, 1, CAPTION)])
    year = deepcopy(cited)
    year_line = cited['table']['year_line']
    year['table'].update(period_line_start=year_line, period_line_end=year_line)
    year['period'].update(label='2022', end='2022-12-31')
    outcome, _ = ingest(w, run, text, layouts, [year, cited, assets_cell(text, layouts), passage(text, 1)], 'R2: подпись слева и активы')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == sorted([('Активы', '71'), (PASSAGE, None)], key=lambda pair: (pair[0], pair[1] or ''))
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(1, 'ambiguous_link', BESIDE), (2, 'ambiguous_link', BESIDE)]
    assets = next(f for f in outcome['facts'] if f['amount'])
    assert assets['amount'] == dict(value='71', unit='money', currency='RUB', scale='1000000')
    assert assets['period'] == dict(kind='instant', label='2025', start='2025-12-31', end='2025-12-31')
    saved = saved_of(w, version)
    assert sorted(f['amount']['value'] if f['amount'] else f['metric'] for f in saved) == sorted(['71', PASSAGE])
    record('r2-left-caption-assets.json', version, text, layouts, outcome, saved)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_the_unit_of_the_row_label_sets_its_scale_and_the_heading_unit_creates_no_second_fact(workspace, provider, tmp_path):
    """The heading in thousands, the row label in millions; the millions are saved once,
    the copy claiming the thousands of the heading is excluded, the passage is saved, the repeat changes nothing. The
    reversed page (millions in the heading, thousands in the row) behaves the same way. Through the tool path the
    exclusion survives the replay and another Run's cache, and the draft carries no gap for it: the saved millions
    state the observation the excluded copy named."""
    w, p = workspace, provider
    version, run, text, layouts = embedded(w, 'R2 row unit', 'embedded-row-unit')
    row_label = 'Выручка от продаж, млн руб.'
    header, row, source = (printed_line(text, 1, s) for s in ('2022 2021', row_label + ' 1 403 1 004', SOURCE))

    def revenue(unit_label):
        return dict(metric='Выручка от продаж', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                    amount=dict(value='1403', unit='money', currency='RUB'), value_text='1 403',
                    period=dict(kind='interval', label='2022', start='2022-01-01', end='2022-12-31'),
                    evidence=[dict(page=1, line_start=header, line_end=row, area='Таблица'), dict(page=1, line_start=source, line_end=source, area='Источник')],
                    basis=dict(page=1, metric_label=row_label, unit_label=unit_label, reporting_label='МСФО'),
                    table=dict(page=1, row_line=row, year_line=header, period_line_start=header, period_line_end=header, column=1))
    items = [revenue('млн руб.'), revenue('тыс. руб.'), passage(text, 1)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'R2: единица строки')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Выручка от продаж', '1403'), (PASSAGE, None)]
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(2, 'unit_mismatch', ROW_UNIT)]
    millions = next(f for f in outcome['facts'] if f['amount'])
    assert millions['amount'] == dict(value='1403', unit='money', currency='RUB', scale='1000000') and millions['period']['end'] == '2022-12-31'
    saved = saved_of(w, version)
    assert sorted((f['amount'] or {}).get('scale') or '' for f in saved) == ['', '1000000']
    record('r2-row-unit.json', version, text, layouts, outcome, saved)
    again, _ = ingest(w, run, text, layouts, items, 'R2: единица строки', call_id)
    assert again == outcome and saved_of(w, version) == saved
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    # The same batch through the tool path on the loopback provider: the replay and another Run's cache keep the exclusion.
    question = 'Выручка и бизнес'
    first = start_note_run(w, version)
    reply, tool_id = extract(w, p, first, question, items, tmp_path)
    assert reply['ok'] and summary(reply['result']) == (status, facts, skipped) and len(p.sent) == 1
    assert extract(w, p, first, question, [], tmp_path, tool_id)[0] == reply and len(p.sent) == 1
    second = start_note_run(w, version)
    cached, _ = extract(w, p, second, question, [], tmp_path)
    assert cached['result'] == reply['result'] and len(p.sent) == 1 and saved_of(w, version) == saved
    qualitative = next(f for f in cached['result']['facts'] if not f['amount'])
    drafted = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative, PASSAGE)))
    assert drafted['ok'], drafted
    # The excluded thousands named the observation the saved millions state: the draft has no coverage gap.
    assert coverage_findings(drafted['result']) == []
    assert coverage_findings(w.runs.version(w.actor, w.project, drafted['result']['version']['id'])) == []
    # The reversed page from the generator: millions in the heading, thousands in the row label.
    row_label = 'Выручка от продаж, тыс. руб.'
    page = [(COMPANY, LEFT, 560), ('млн руб.', LEFT, 520), ('2022', LEFT + 88 * PITCH, 505), ('2021', LEFT + 120 * PITCH, 505),
            (row_label, LEFT, 480), ('1 403', LEFT + 88 * PITCH, 480), ('1 004', LEFT + 120 * PITCH, 480), (PASSAGE, LEFT, 100), (SOURCE, LEFT, 60)]
    version, run, text, layouts = uploaded(w, 'R2 row unit reversed', [page])
    assert [c['period_lines'] for c in tables.page_tables(text[1].splitlines(), layouts[1])[0]['columns']] == [[], []]
    header, row, source = (line_of(text, 1, s) for s in ('2022 2021', row_label + ' 1 403 1 004', SOURCE))
    items = [revenue('тыс. руб.'), revenue('млн руб.'), passage(text, 1)]
    outcome, _ = ingest(w, run, text, layouts, items, 'R2: единица строки наоборот')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Выручка от продаж', '1403'), (PASSAGE, None)]
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(2, 'unit_mismatch', ROW_UNIT)]
    assert next(f for f in outcome['facts'] if f['amount'])['amount']['scale'] == '1000'
    saved = saved_of(w, version)
    assert sorted((f['amount'] or {}).get('scale') or '' for f in saved) == ['', '1000']
    record('r2-row-unit-reversed.json', version, text, layouts, outcome, saved)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
