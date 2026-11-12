"""Real PDF parsing and atomic ingest, with an isolated database for every test."""
from copy import deepcopy
import os
from pathlib import Path

import pytest

from app import charts
from app.errors import ApiError
from app.extraction import candidate, ingest, source_pages
from app.runs import json_value
from app.service import ident
from test_d15_postgres import workspace
from test_flow01_02_postgres import checkpoint
from test_flow01_03_postgres import COMPANY, GROUPS, ROWS, PERIODS, positioned_pdf, structure_of, table_page
from test_flow01_06_r2 import claim, separate_claim
from test_perf02_r1_postgres import accounted_response, fact_rows
from test_perf02_r4_postgres import start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def chart_objects(multiple=False, *, reverse=False, shift=0, vertical=0):
    objects = []
    for n, metric in enumerate(['Платежи', 'Расходы'] if multiple else ['Поступления']):
        left = 45 + n * 410
        objects.append((metric + (', тыс. руб.' if n else ', млн руб.'), left, 505))
        for i, (value, label) in enumerate(zip(['-21,7', '38,9', '42,6'] if n == 0 else ['15,4', '28,3', '35,1'],
                                              ['2016', '3M17', '9M18'])):
            objects.append((value, left + 100 * i, 460 - i * 30))
            objects.append((label, left + 100 * i, 340))
    if reverse:
        objects.reverse()
    return [(text, x + shift, y + vertical) for text, x, y in objects]


def upload(w, multiple=False, reverse=False, shift=0, vertical=0, mixed=False):
    objects = [chart_objects(multiple, reverse=reverse, shift=shift, vertical=vertical),
               [('Vector Industries', 40, 550), ('МСФО', 40, 520)]]
    if mixed:
        table = [(text.replace(COMPANY, 'Vector Industries'), x, y) for text, x, y in table_page(GROUPS, ROWS, 'rows_first')]
        objects.append(table)
        objects.append([('Компания продает продукцию через дилеров.', 40, 550)])
    version = w.service.write(w.actor, 'uploadSource', ident('key'), dict(title='Independent charts', expected_revision=0),
                              w.project, None, positioned_pdf(objects), 'charts.pdf')[1]['version']
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    return run, pages, layouts


@pytest.mark.parametrize('multiple', [False, True])
@pytest.mark.parametrize('reverse,shift,vertical', [(False, 0, 0), (True, 0, 0), (True, 19, -24)])
def test_pdf_geometry_uses_its_own_source_lines(workspace, multiple, reverse, shift, vertical):
    w = workspace
    run, pages, layouts = upload(w, multiple, reverse, shift, vertical)
    pin = run['input']['source_versions'][0]
    structure = charts.context(pages[1].splitlines(), layouts[1])
    assert len(structure) == (2 if multiple else 1), (pages[1], layouts[1], structure)
    items = [claim(pages, layouts, chart['metric'], index) for chart in structure for index in range(3)]
    payload = dict(facts=items)
    original = deepcopy(payload)
    call = accounted_response(w, run, payload)
    result = ingest(w.runs, run, pages, payload, call, pin, layouts)
    assert result['status'] == 'complete' and len(result['facts']) == len(items)
    assert payload == original
    for fact, item in zip(result['facts'], items):
        assert fact['amount']['value'] == item['amount']['value']
        assert fact['period'] == item['period']
        assert all(e['quote'] in pages[e['locator']['pdf_page']] for e in fact['evidence'])
    if os.getenv('AIST_FLOW01_EVIDENCE'):
        name = f'pdf-{multiple}-{reverse}-{shift}-{vertical}.json'
        Path(os.environ['AIST_FLOW01_EVIDENCE'], name).write_text(json_value(dict(pages=pages, layouts=layouts, result=result,
                                                                              database=w.settings.db_name)))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


@pytest.mark.parametrize('defect', [None, 'reference', 'pin'])
def test_mixed_batch_and_strict_atomic_refusals(workspace, defect):
    w = workspace
    run, pages, layouts = upload(w, True, mixed=True)
    pin = run['input']['source_versions'][0]
    numeric = claim(pages, layouts, index=2)
    ambiguous = deepcopy(numeric)
    ambiguous['metric'] = 'Расходы'
    structure = structure_of(pages, layouts, 3)
    column = structure['columns'][0]
    row_line = next(r['line'] for r in structure['rows'] if pages[3].splitlines()[r['line'] - 1].startswith(ROWS[0][0]))
    first, last = min(column['period_lines']), max(column['period_lines'])
    table = dict(metric=ROWS[0][0], subject='Vector Industries', reporting_basis='МСФО', nature='reported_actual',
                 period=dict(kind='interval', label=PERIODS[1][0], start=PERIODS[1][1], end=PERIODS[1][2]),
                 amount=dict(value='8120', unit='money', currency='RUB'), value_text='8 120',
                 evidence=[dict(page=3, line_start=row_line, line_end=row_line, area='Строка таблицы'),
                           dict(page=3, line_start=first, line_end=max(last, column['year_line']), area='Период')],
                 basis=dict(page=3, metric_label=ROWS[0][0], unit_label='млн руб.', reporting_label='МСФО'),
                 table=dict(page=3, row_line=row_line, year_line=column['year_line'], period_line_start=first,
                            period_line_end=last, column=1))
    passage = dict(metric=None, subject='Vector Industries', reporting_basis='Не указано', nature='source_statement',
                   period=dict(kind='unknown', label='Не указан', start=None, end=None), amount=None, value_text=None, basis=None,
                   evidence=[dict(page=4, line_start=1, line_end=1, area='Описание бизнеса')])
    items = [table, numeric, passage, ambiguous]
    if defect == 'reference':
        bad = deepcopy(numeric)
        bad['evidence'][0]['line_end'] = 999
        items.append(bad)
    if defect == 'pin':
        pin = dict(pin, sha256='a' * 64)
    payload = dict(facts=items)
    original = deepcopy(payload)
    call = accounted_response(w, run, payload)
    before = checkpoint(w, run)
    if defect:
        with pytest.raises(ApiError) as error:
            ingest(w.runs, run, pages, payload, call, pin, layouts)
        assert not hasattr(error.value, 'skip_code')
        assert not fact_rows(w) and checkpoint(w, run) == before
    else:
        result = ingest(w.runs, run, pages, payload, call, pin, layouts)
        assert result['status'] == 'partial' and len(result['facts']) == 3
        assert [(s['candidate'], s['code']) for s in result['skipped']] == [(4, 'ambiguous_link')]
        saved, recorded = fact_rows(w), checkpoint(w, run)
        assert ingest(w.runs, run, pages, payload, call, pin, layouts) == result
        assert saved == fact_rows(w) and recorded == checkpoint(w, run)
    assert payload == original
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_qualitative_ingest_derives_only_the_cited_observation(workspace):
    w = workspace
    run, pages, layouts = upload(w)
    pin = run['input']['source_versions'][0]
    qualitative = claim(pages, layouts, index=1, qualitative=True)
    payload = dict(facts=[qualitative])
    call = accounted_response(w, run, payload)
    outcome = ingest(w.runs, run, pages, payload, call, pin, layouts)
    numeric = [f for f in outcome['facts'] if f['amount']]
    explicit, _ = candidate(pin, pages, claim(pages, layouts, index=1), layouts)
    assert len(numeric) == 1
    for key in ('metric', 'period', 'amount', 'subject', 'reporting_basis', 'limitations'):
        assert explicit[key] == numeric[0][key]
    assert any(f['amount'] is None for f in outcome['facts'])
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


@pytest.mark.parametrize('raw', ['37,6', '38,9'])
def test_equal_number_in_an_independent_pdf_block_survives_mixed_ingest(workspace, raw):
    w = workspace
    objects = [*chart_objects(), ('Доходы от услуг, млн руб.', 45, 250), (raw, 45, 220), ('6M19', 45, 190)]
    source = positioned_pdf([objects, [('Vector Industries', 40, 550), ('МСФО', 40, 520)]])
    version = w.service.write(w.actor, 'uploadSource', ident('key'), dict(title='Independent observation', expected_revision=0),
                              w.project, None, source, 'independent.pdf')[1]['version']
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    item = separate_claim(pages, raw, 'млн руб.')
    payload = dict(facts=[claim(pages, layouts), item])
    original = deepcopy(payload)
    call = accounted_response(w, run, payload)
    result = ingest(w.runs, run, pages, payload, call, run['input']['source_versions'][0], layouts)
    assert result['status'] == 'complete' and len(result['facts']) == 2
    fact = next(f for f in result['facts'] if f['metric'] == item['metric'])
    assert fact['amount']['value'] == item['amount']['value'] and fact['period'] == item['period']
    assert payload == original
    if os.getenv('AIST_FLOW01_EVIDENCE'):
        Path(os.environ['AIST_FLOW01_EVIDENCE'], 'independent-' + raw + '.json').write_text(
            json_value(dict(pages=pages, layouts=layouts, result=result, database=w.settings.db_name)))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
