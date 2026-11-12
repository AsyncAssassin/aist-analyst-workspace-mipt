"""Wrapped captions beside year columns through PDF parsing and ingestion.

The supported nine-month caption wrapped onto two lines at the left margin ('За 9' / 'месяцев, закончившихся
30 сентября') is a caption beside the columns like the one printed whole: the header is unverifiable, the calendar year
is excluded with the year line alone or with both caption lines cited, the passage of the batch is saved and the
diagnostics are recorded once. On the generator's page the assets table printed under the same caption keeps its fact.
The readable PDF with an embedded font is a synthetic fixture in fixtures/flow01. No model, no paid request."""
from copy import deepcopy
import json
import os

import pytest

from app import tables
from app.extraction import extraction_request
from test_d15_postgres import workspace
from test_flow01_02_postgres import checkpoint, summary
from test_flow01_03 import BESIDE, WRAPPED
from test_flow01_03_postgres import COMPANY, LEFT, PASSAGE, SOURCE, ingest, line_of, passage, uploaded
from test_flow01_03_r1_postgres import ASSETS, CAPTION, assets_cell, revenue_cell, revenue_table
from test_flow01_03_r2_postgres import embedded, printed_line, record, saved_of

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def test_a_wrapped_caption_beside_the_columns_saves_no_calendar_year_and_keeps_the_rest_of_the_page(workspace):
    """The caption occupies two lines at the left margin, the structure names both columns
    unverifiable, the calendar year is excluded with the year line alone and with both caption lines cited, so are the
    nine months, the passage is saved, the diagnostics are recorded once and the repeat of the call changes nothing. On
    the generator's page with the assets table under the same wrapped caption the assets cell keeps its fact."""
    w = workspace
    version, run, text, layouts = embedded(w, 'R3 wrapped caption', 'wrapped-left-caption')
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [c['period_lines'] for c in structure[0]['columns']] == [None, None] and len(structure[0]['rows']) == 1
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    first, last, header, row = (printed_line(text, 1, s) for s in (*WRAPPED, 'тыс. руб. 2022 2021', 'Выручка от продаж 1 403 1 004'))
    source = printed_line(text, 1, SOURCE)
    assert last == first + 1 and structure[0]['columns'][0]['year_line'] == header and structure[0]['rows'][0]['line'] == row

    def revenue(period, start, end):
        return dict(metric='Выручка от продаж', subject=COMPANY, reporting_basis='МСФО', nature='reported_actual',
                    amount=dict(value='1403', unit='money', currency='RUB'), value_text='1 403', period=period,
                    evidence=[dict(page=1, line_start=min(start, header), line_end=row, area='Таблица'), dict(page=1, line_start=source, line_end=source, area='Источник')],
                    basis=dict(page=1, metric_label='Выручка от продаж', unit_label='тыс. руб.', reporting_label='МСФО'),
                    table=dict(page=1, row_line=row, year_line=header, period_line_start=start, period_line_end=end, column=1))
    year = dict(kind='interval', label='2022', start='2022-01-01', end='2022-12-31')
    nine_months = dict(kind='interval', label="9M'2022", start='2022-01-01', end='2022-09-30')
    items = [revenue(year, header, header), revenue(year, first, last), revenue(nine_months, first, last), passage(text, 1)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'R3: перенос подписи')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [(PASSAGE, None)]
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(n, 'ambiguous_link', BESIDE) for n in (1, 2, 3)]
    assert all(s['observation']['metric'] == 'Выручка от продаж' for s in outcome['skipped'])
    saved = saved_of(w, version)
    assert [f['metric'] for f in saved] == [PASSAGE] and saved[0]['amount'] is None
    diagnostics = [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id]
    assert len(diagnostics) == 1 and diagnostics[0]['status'] == 'partial' and diagnostics[0]['skipped'] == outcome['skipped']
    record('r3-wrapped-caption.json', version, text, layouts, outcome, saved)
    again, _ = ingest(w, run, text, layouts, items, 'R3: перенос подписи', call_id)
    assert again == outcome and saved_of(w, version) == saved
    assert [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id] == diagnostics
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    # The generator's page: the caption wrapped at the left margin over the header, the assets table under the table.
    page = [entry for entry in revenue_table() if entry[0] != CAPTION] + [(WRAPPED[0], LEFT, 538), (WRAPPED[1], LEFT, 520)] + ASSETS
    version, run, text, layouts = uploaded(w, 'R3 wrapped caption and assets', [page])
    structure = tables.page_tables(text[1].splitlines(), layouts[1])
    assert [[c['period_lines'] for c in table['columns']] for table in structure] == [[None, None], [[], []]]
    assert json.loads(extraction_request(text, 'Выручка', layouts)['messages'][1]['content'])['pages'][0]['table_layout'] == structure
    parts = [line_of(text, 1, part) for part in WRAPPED]
    assert parts[1] == parts[0] + 1
    cited = revenue_cell(text, layouts, parts)
    annual = deepcopy(cited)
    year_line = cited['table']['year_line']
    annual['table'].update(period_line_start=year_line, period_line_end=year_line)
    annual['period'].update(label='2022', end='2022-12-31')
    outcome, _ = ingest(w, run, text, layouts, [annual, cited, assets_cell(text, layouts), passage(text, 1)], 'R3: перенос подписи и активы')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == sorted([('Активы', '71'), (PASSAGE, None)], key=lambda pair: (pair[0], pair[1] or ''))
    assert [(s['candidate'], s['code'], s['message']) for s in outcome['skipped']] == [(1, 'ambiguous_link', BESIDE), (2, 'ambiguous_link', BESIDE)]
    assets = next(f for f in outcome['facts'] if f['amount'])
    assert assets['amount'] == dict(value='71', unit='money', currency='RUB', scale='1000000')
    assert assets['period'] == dict(kind='instant', label='2025', start='2025-12-31', end='2025-12-31')
    saved = saved_of(w, version)
    assert sorted(f['amount']['value'] if f['amount'] else f['metric'] for f in saved) == sorted(['71', PASSAGE])
    record('r3-wrapped-caption-assets.json', version, text, layouts, outcome, saved)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
