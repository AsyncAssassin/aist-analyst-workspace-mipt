"""FLOW-01.04 and 04-R1 through PDFs the worker parses and the normal ingest: a passage joining the name of one card
with the lines of another is excluded locally with its reason, in one range or cut into several, on the vertical and
on the two-column layout; the cards cited as printed, a title with its paragraph beside a margin marker and the
verified number of the batch are saved; the exact repeat returns the same result; the gap of the excluded passage
blocks the acceptance of a draft until the card is cited as printed; a verified card printed in an excluded passage
keeps its own fact. Positioned synthetic PDF pages retain the text-order and geometry constraints. No model, no paid request."""
from copy import deepcopy
import os
from pathlib import Path

import pytest

from app import tables
from app.errors import ApiError
from app.exports import Exports
from app.facts import pin_of
from app.extraction import APART, VERSION, ingest as ingest_payload, source_pages
from app.runs import json_value
from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import accept, export_request
from test_flow01_02_postgres import HEADER, checkpoint, coverage_findings, human_edit, note, summary
from test_flow01_03_postgres import LEFT, PITCH, ingest, uploaded, uploaded_pdf
from test_flow01_03_r1_postgres import evidence_dir
from test_flow01_03_r2_postgres import saved_of
from test_flow01_04 import COMPANY, HEADING, NORTH, ORDERS, RANKS, SOUTH, SOURCE, YEAR_2024, range_of
from test_perf02_r1_postgres import accounted_response, fact_rows, upload
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

ROW = 18   # points between the rows of a card
PROSE = ['О компании', 'Компания выпускает препараты', 'и поставляет их в аптеки.']
MARKER = 'Раздел 2'
PROFIT = ['Прибыль', '200 млн руб.', 'за 2022 год']


def passage_places(lines, layout, numbers):
    """Where the saved layout prints each of these original lines: every run of cells spelling the line whole, as
    (row, start, end) in layout characters, one per printing; [] for a line printed nowhere whole."""
    rows = tables.layout_rows(layout)
    runs = tables.spellings(rows)
    places = {}
    for number in numbers:
        line = lines[number - 1]
        found = runs.get(tables.squeeze(line), []) if line.strip() else []
        places[number] = [(r, rows[r][first][0], rows[r][last][1]) for r, first, last in found]
    return places


def site_page():
    """The two site cards of test_flow01_04 as positioned text objects in the text order of the archived PDF (the name
    after the lines of its card): the name at the top left of its card and the capacity at the right, the year and the
    area on the next row, the three captions on the row under them; the south card five rows under the north one, the
    source line at the bottom. Consecutive objects of the stream never share a row, as in the archived PDF."""
    left, middle, right = 55, 200, 380
    items = [(HEADING[0], LEFT, 560), (HEADING[1], LEFT, 542)]
    for lines, top in ((NORTH, 500), (SOUTH, 500 - 5 * ROW)):
        capacity, launch, year, area_caption, area, capacity_caption, name = lines
        items += [(capacity, right, top), (launch, left, top - 2 * ROW), (year, left, top - ROW), (area_caption, middle, top - 2 * ROW),
                  (area, middle, top - ROW), (capacity_caption, right, top - 2 * ROW), (name, left, top)]
    items.append((SOURCE, LEFT, 60))
    assert [text for text, _, _ in items] == ORDERS['the name after its lines']
    return items


def side_page():
    """The same two cards in the same text order printed side by side: the north card in the left column, the south
    card in the right one, the lines of each card one under the other (the review's two-column layout)."""
    items = [(HEADING[0], LEFT, 560), (HEADING[1], LEFT, 542)]
    for lines, left in ((NORTH, 55), (SOUTH, 380)):
        capacity, launch, year, area_caption, area, capacity_caption, name = lines
        items += [(capacity, left, 500 - 3 * ROW), (launch, left, 500 - 4 * ROW), (year, left, 500 - ROW), (area_caption, left, 500 - 5 * ROW),
                  (area, left, 500 - 2 * ROW), (capacity_caption, left, 500 - 6 * ROW), (name, left, 500)]
    items.append((SOURCE, LEFT, 60))
    assert [text for text, _, _ in items] == ORDERS['the name after its lines']
    return items


def prose_page():
    """A title, its paragraph two rows under it and a section marker in the right margin beside the title."""
    return [(COMPANY, LEFT, 560), (PROSE[0], LEFT, 500), (PROSE[1], LEFT, 500 - 3 * ROW), (PROSE[2], LEFT, 500 - 4 * ROW), (MARKER, 380, 500)]


def profit_page():
    """A dated source line and one monetary card the layout verifies on its own."""
    return [(HEADER, LEFT, 560), (PROFIT[0], 320, 500), (PROFIT[1], 320, 500 - ROW), (PROFIT[2], 320, 500 - 2 * ROW)]


def passage(text, lines, *extra, page=1):
    start, end = range_of(text[page], lines)
    return dict(metric=None, subject=COMPANY, reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None, basis=None,
                evidence=[dict(page=page, line_start=start, line_end=end, area='Карточка'), *extra])


def record(name, **data):
    evidence_dir().joinpath(name).write_text(json_value(data))


def test_two_site_cards_through_the_normal_ingest_keep_each_card_and_exclude_the_transfer(workspace):
    """Check 4 and 5: the transfer, both cards and the page heading in one batch give partial with the two cards and
    the heading saved and the transfer excluded with its reason, recorded once; the exact repeat changes nothing. A
    draft citing the north card is saved with the blocking gap of the transfer, its acceptance and accepted release
    are refused, an unrelated edit keeps the remark, and the name cited with its own lines closes the gap."""
    w = workspace
    order = ORDERS['the name after its lines']
    version, run, text, layouts = uploaded(w, 'FLOW-01.04 two site cards', [site_page()])
    assert text[1].splitlines() == order
    # The geometry the rule reads: the north name stands one row from its year and apart from every south line.
    lines = text[1].splitlines()
    places = passage_places(lines, layouts[1], range(1, len(lines) + 1))
    north_name, north_year = lines.index(NORTH[-1]) + 1, lines.index(NORTH[2]) + 1
    south_rows = {places[lines.index(line) + 1][0][0] for line in (SOUTH[0], SOUTH[2], SOUTH[4], SOUTH[6])}
    assert len(places[north_name]) == 1 and abs(places[north_name][0][0] - places[north_year][0][0]) == 1
    assert all(row - places[north_name][0][0] >= 3 for row in south_rows)
    transfer = passage(text, [NORTH[-1], *SOUTH[:-1]])
    items = [transfer, passage(text, NORTH), passage(text, SOUTH), passage(text, HEADING)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'Производственные площадки')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == sorted([(NORTH[0], None), (SOUTH[0], None), (HEADING[0], None)], key=lambda pair: pair[0])
    assert skipped == [(NORTH[-1], 'ambiguous_link', 1, 'null')] and outcome['skipped'][0]['message'] == APART
    assert outcome['skipped'][0]['observation']['metric'] == NORTH[-1] and outcome['skipped'][0]['observation']['subject'] == COMPANY
    north = next(f for f in outcome['facts'] if f['metric'] == NORTH[0])
    south = next(f for f in outcome['facts'] if f['metric'] == SOUTH[0])
    assert north['raw_text'] == '\n'.join(NORTH) and south['raw_text'] == '\n'.join(SOUTH)
    assert all(word in south['raw_text'] for word in ('Запуск продаж', '2026 г.', 'Плановая мощность в год'))
    assert all(f['extraction_version'] == VERSION for f in outcome['facts'])
    saved = saved_of(w, version)
    assert sorted(f['metric'] for f in saved) == sorted([NORTH[0], SOUTH[0], HEADING[0]])
    diagnostics = [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id]
    assert len(diagnostics) == 1 and diagnostics[0]['status'] == 'partial' and diagnostics[0]['skipped'] == outcome['skipped']
    again, _ = ingest(w, run, text, layouts, items, 'Производственные площадки', call_id)
    assert again == outcome and saved_of(w, version) == saved
    assert [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id] == diagnostics
    # The draft: saved with the blocking gap, not acceptable, not releasable as accepted, the remark survives an edit.
    view = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(north, 'Северная площадка выпускает готовые лекарственные формы.')))
    assert view['ok'], view
    view = view['result']
    gaps = coverage_findings(view)
    assert view['checks'][0]['status'] == 'passed' and view['review']['acceptance'] is None and len(gaps) == 1
    assert '1 из 1' in gaps[0]['message'] and '(ambiguous_link)' in gaps[0]['message']
    with pytest.raises(ApiError) as blocked:
        accept(w, view, acknowledge_unchecked_semantics=False)
    assert blocked.value.code == 'validation_failed'
    with pytest.raises(ApiError) as release:
        Exports(w.runs).request(w.actor, w.session, w.project, view['version']['artifact_id'], view['version']['id'],
                                ident('key'), export_request('pdf', 'accepted'))
    assert release.value.code == 'acceptance_required'
    edited = human_edit(w, view, 'Северная площадка выпускает готовые лекарственные формы для рынка.')
    assert coverage_findings(edited) == gaps and edited['version']['provenance']['run_id'] is None
    # The north name cited with its own lines, as printed, states the observation of the gap and closes it; the
    # transfer cited once more does not.
    later = start_note_run(w, version)
    name_start, name_end = range_of(text[1], [NORTH[-1]])
    own_start, own_end = range_of(text[1], NORTH[:-1])
    cited = dict(passage(text, [NORTH[-1]]), evidence=[dict(page=1, line_start=name_start, line_end=name_end, area='Название площадки'),
                                                      dict(page=1, line_start=own_start, line_end=own_end, area='Строки площадки')])
    repeated, _ = ingest(w, later, text, layouts, [transfer], 'Северная площадка')
    assert repeated['status'] == 'empty' and [s['message'] for s in repeated['skipped']] == [APART]
    still = human_edit(w, edited, 'Северная площадка выпускает готовые лекарственные формы.')
    assert coverage_findings(still) == gaps
    closing, _ = ingest(w, later, text, layouts, [cited], 'Северная площадка как напечатано')
    assert closing['status'] == 'complete' and closing['facts'][0]['metric'] == NORTH[-1]
    assert [e['quote'] for e in closing['facts'][0]['evidence']][:2] == [NORTH[-1], '\n'.join(NORTH[:-1])]
    closed = human_edit(w, still, 'Северная площадка выпускает готовые лекарственные формы для аптек.')
    assert coverage_findings(closed) == [] and closed['checks'][0]['status'] == 'passed', closed['checks'][0]['findings']
    record('04-two-site-cards.json', source=version['id'], text=text, layouts=layouts, outcome=outcome, saved=saved,
           draft_findings=gaps, closing=closing, closed_findings=coverage_findings(closed))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.stop(later['id'], later['epoch'], 'validation_failed')


def test_the_review_forms_in_one_batch_keep_the_sound_part_and_repeat_exactly(workspace):
    """R1 check 4: the transfer cut into the name and the foreign lines on the vertical page, the name with the foreign
    column on the two-column page, the north card as printed on that page, the title with its paragraph beside the
    margin marker and the profit card give partial: the two transfers are excluded with their reason, the card, the
    title, the profit passage and the verified profit are saved, the exact repeat adds neither facts nor diagnostics."""
    w = workspace
    version, run, text, layouts = uploaded(w, 'FLOW-01.04-R1 review forms', [site_page(), side_page(), prose_page(), profit_page()])
    assert text[1].splitlines() == text[2].splitlines() == ORDERS['the name after its lines']
    assert text[3].splitlines() == [COMPANY, *PROSE, MARKER] and text[4].splitlines() == [HEADER, *PROFIT]
    # The geometry the rule reads: on page 2 the two names share one row, each over its own column; on page 3 the
    # marker shares the title's row and the paragraph stands two rows under the title.
    lines = text[2].splitlines()
    places = passage_places(lines, layouts[2], range(1, len(lines) + 1))
    north_name, south_name, south_year = (lines.index(line) + 1 for line in (NORTH[-1], SOUTH[-1], SOUTH[2]))
    assert places[north_name][0][0] == places[south_name][0][0] and places[south_name][0][0] + 1 == places[south_year][0][0]
    assert places[south_year][0][1] > places[north_name][0][2]
    prose = passage_places(text[3].splitlines(), layouts[3], range(1, 6))
    assert prose[2][0][0] == prose[5][0][0] and prose[3][0][0] - prose[2][0][0] >= 3
    name_line = range_of(text[1], [NORTH[-1]])[0]
    foreign = range_of(text[1], SOUTH[:-1])
    cut = dict(passage(text, [NORTH[-1]]), evidence=[dict(page=1, line_start=name_line, line_end=name_line, area='Название'),
                                                   dict(page=1, line_start=foreign[0], line_end=foreign[1], area='Строки')])
    columns = passage(text, [NORTH[-1], *SOUTH[:-1]], page=2)
    items = [cut, columns, passage(text, NORTH, page=2), passage(text, PROSE, page=3), passage(text, PROFIT, page=4)]
    outcome, call_id = ingest(w, run, text, layouts, items, 'Формы ревью')
    status, facts, skipped = summary(outcome)
    assert status == 'partial'
    assert facts == sorted([(NORTH[0], None), (PROSE[0], None), (PROFIT[0], None), (PROFIT[0], '200')], key=lambda pair: (pair[0], pair[1] or ''))
    assert skipped == [(NORTH[-1], 'ambiguous_link', 1, 'null'), (NORTH[-1], 'ambiguous_link', 2, 'null')]
    assert [s['message'] for s in outcome['skipped']] == [APART, APART]
    north = next(f for f in outcome['facts'] if f['metric'] == NORTH[0])
    title = next(f for f in outcome['facts'] if f['metric'] == PROSE[0])
    profit = next(f for f in outcome['facts'] if f['amount'])
    assert north['raw_text'] == '\n'.join(NORTH) and title['raw_text'] == '\n'.join(PROSE)
    assert profit['amount'] == dict(value='200', unit='money', currency='RUB', scale='1000000') and profit['period']['end'] == '2022-12-31'
    assert all(f['extraction_version'] == VERSION for f in outcome['facts'])
    saved = saved_of(w, version)
    assert sorted(((f['metric'], (f['amount'] or {}).get('value')) for f in saved), key=lambda pair: (pair[0], pair[1] or '')) == facts
    diagnostics = [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id]
    assert len(diagnostics) == 1 and diagnostics[0]['status'] == 'partial' and diagnostics[0]['skipped'] == outcome['skipped']
    again, _ = ingest(w, run, text, layouts, items, 'Формы ревью', call_id)
    assert again == outcome and saved_of(w, version) == saved
    assert [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id] == diagnostics
    # A local link exclusion does not soften a later structural/source/provenance failure: nothing from the
    # attempted batch may be saved, including the otherwise valid prose and numeric card.
    for defect in ('schema', 'reference', 'sha', 'source', 'call'):
        payload = dict(facts=deepcopy(items))
        payload['facts'][2]['evidence'][0]['area'] = 'Атомарная проба'
        pin = pin_of(version)
        if defect == 'schema':
            payload['facts'][-1]['unexpected'] = True
        if defect == 'reference':
            payload['facts'][-1]['evidence'][0]['line_end'] = 999
        if defect == 'sha':
            pin['sha256'] = '0' * 64
        if defect == 'source':
            pin['source_id'] = ident('src')
        attempt = accounted_response(w, run, payload)
        before_checkpoint = checkpoint(w, run)
        with pytest.raises(ApiError):
            ingest_payload(w.runs, run, text, payload, ident('call') if defect == 'call' else attempt,
                           pin=pin, layouts=layouts, question='Атомарный отказ после исключения')
        assert saved_of(w, version) == saved and checkpoint(w, run) == before_checkpoint
    record('04-r1-review-forms.json', source=version['id'], text=text, layouts=layouts, outcome=outcome, saved=saved)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


@pytest.mark.parametrize('identity_position', [None, 'first', 'last'])
def test_disjoint_foreign_properties_in_a_mixed_batch_are_excluded_and_repeat_exactly(workspace, identity_position):
    """04-R2: the exact R1 ingest input beside a correct quotation and an independently verified number."""
    w = workspace
    version, run, text, layouts = uploaded(w, 'FLOW-01.04-R2 disjoint references', [site_page(), profit_page()])
    try:
        wrong = passage(text, [NORTH[-1], *SOUTH[:-1]])
        title = dict(wrong['evidence'][0], line_end=wrong['evidence'][0]['line_start'])
        foreign = dict(wrong['evidence'][0], line_start=title['line_start'] + 2)
        separated = dict(wrong, evidence=[title, foreign])
        if identity_position is not None:
            issuer = dict(page=1, line_start=1, line_end=1, area='Название')
            separated['evidence'].insert(0 if identity_position == 'first' else len(separated['evidence']), issuer)
        items = [separated, passage(text, NORTH), passage(text, PROFIT, page=2)]
        outcome, call_id = ingest(w, run, text, layouts, items, 'Разнесенные основания R2')
        status, facts, skipped = summary(outcome)
        assert status == 'partial'
        assert facts == [(NORTH[0], None), (PROFIT[0], None), (PROFIT[0], '200')]
        assert skipped == [(NORTH[-1], 'ambiguous_link', 1, 'null')]
        assert outcome['skipped'][0]['message'] == APART
        assert outcome['skipped'][0]['observation']['metric'] == NORTH[-1]
        assert all(f['extraction_version'] == VERSION for f in outcome['facts'])
        saved = saved_of(w, version)
        assert len(saved) == 3 and all(f['metric'] != NORTH[-1] for f in saved)
        north = next(f for f in saved if f['metric'] == NORTH[0])
        assert north['raw_text'] == '\n'.join(NORTH)
        assert not any(word in e['quote'] for e in north['evidence'] for word in (SOUTH[2], SOUTH[4]))
        profit = next(f for f in saved if f['amount'])
        assert profit['amount'] == dict(value='200', unit='money', currency='RUB', scale='1000000')
        assert profit['period'] == dict(kind='interval', label='за 2022 год', start='2022-01-01', end='2022-12-31')
        diagnostics = [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id]
        assert len(diagnostics) == 1 and diagnostics[0]['version'] == VERSION
        assert diagnostics[0]['status'] == 'partial' and diagnostics[0]['skipped'] == outcome['skipped']
        repeated, _ = ingest(w, run, text, layouts, items, 'Разнесенные основания R2', call_id)
        assert repeated == outcome and saved_of(w, version) == saved
        assert [e for e in checkpoint(w, run)['extractions'] if e['call_id'] == call_id] == diagnostics
        record(f'04-r2-disjoint-mixed-{identity_position}.json', source=version['id'], text=text, layouts=layouts, candidate=separated,
               outcome=outcome, saved=saved, diagnostics=diagnostics, repeat_equal=repeated == outcome)
    finally:
        w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_a_verified_card_printed_in_an_excluded_passage_keeps_its_own_fact(workspace):
    """The transition after the new exclusion: a passage whose label is printed apart from the cards cited with it,
    beside a line of its own the passage leaves out, is excluded, while the card whose caption and period the layout
    verifies on its own is saved, and the card whose literal caption cannot be anchored is excluded as before. The
    same cards without the stray label keep the passage."""
    w = workspace
    # The stray label stands far under the cards with a line of its own beside it that the passage leaves out.
    lines = [HEADER, 'Прочие сведения', 'Выручка сегмента', 'Прибыль', '100 млн руб.', 'оборудования', 'за 2023 год', '200 млн руб.',
             'за 2022 год', 'Пометка к прочим сведениям']
    positions = [(72, 800), (72, 560), (72, 760), (320, 740), (72, 720), (72, 740), (72, 700), (320, 720), (320, 700), (72, 542)]
    version = upload(w, 'flow04-' + ident('src')[-6:], lines, positions)
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)

    def cards(start):
        return dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                    period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None,
                    basis=None, evidence=[dict(page=1, line_start=start, line_end=9, area='Карточки')])
    outcome, call_id = ingest(w, run, pages, layouts, [cards(2)], 'Сегменты')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Прибыль', '200')]
    assert skipped == [('Выручка сегмента оборудования', 'ambiguous_link', 1, '{"page":1,"line_start":5,"line_end":5}'),
                       ('Прочие сведения', 'ambiguous_link', 1, 'null')]
    assert [s['message'] for s in outcome['skipped'] if s['derived'] is None] == [APART]
    profit = outcome['facts'][0]
    assert profit['amount'] == dict(value='200', unit='money', currency='RUB', scale='1000000') and profit['raw_text'] == '200'
    assert profit['period'] == dict(kind='interval', label='за 2022 год', start='2022-01-01', end='2022-12-31')
    assert len(fact_rows(w)) == 1
    again, _ = ingest(w, run, pages, layouts, [cards(2)], 'Сегменты', call_id)
    assert again == outcome and len(fact_rows(w)) == 1
    # The same cards cited from their own first line: the passage and the card are saved as in FLOW-01.02.
    outcome, _ = ingest(w, run, pages, layouts, [cards(3)], 'Сегменты как напечатано')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Выручка сегмента', None), ('Прибыль', '200')]
    assert skipped == [('Выручка сегмента оборудования', 'ambiguous_link', 1, '{"page":1,"line_start":5,"line_end":5}')]
    # The card derived from another cited range carries that range in its evidence, hence its own ID: the same
    # observation twice, beside the passage.
    assert sorted((r['data']['metric'], (r['data']['amount'] or {}).get('value')) for r in fact_rows(w)) == [
        ('Выручка сегмента', None), ('Прибыль', '200'), ('Прибыль', '200')]
    record('04-card-in-excluded-passage.json', source=version['id'], text=pages, layouts=layouts, outcome=outcome)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def rank_page():
    positions = [
        (60, 463), (60, 301), (140, 319), (140, 301), (140, 283),
        (60, 517), (140, 535), (140, 517), (140, 211), (140, 193), (140, 463),
    ]
    return [(COMPANY, LEFT, 571), *[(line, x, y) for line, (x, y) in zip(RANKS, positions)]]


def test_rank_caption_and_numeric_card_share_one_ingest(workspace):
    fixture = Path(__file__).with_name('fixtures') / 'flow01' / 'rank-caption-ingest.pdf'
    w = workspace
    version, run, pages, layouts = uploaded_pdf(w, 'FLOW-01.04 independent rank cards', fixture.read_bytes())
    assert pages[1].splitlines() == [text for text, _, _ in rank_page()]
    site_lines = pages[2].splitlines()
    profit_lines = pages[3].splitlines()
    assert [line.strip() for line in site_lines] == [text for text, _, _ in site_page()]
    assert [line.strip() for line in profit_lines] == [text for text, _, _ in profit_page()]
    south_start = len(HEADING) + len(NORTH)
    south = site_lines[south_start:south_start + len(SOUTH)]
    rank = dict(passage(pages, RANKS[1:5]), period=YEAR_2024)
    wrong_rank = dict(passage(pages, RANKS[:5]), period=YEAR_2024)
    wrong_site = passage(pages, [site_lines[south_start - 1], *south[:-1]], page=2)
    site = passage(pages, south, page=2)
    profit = passage(pages, profit_lines[1:], page=3)
    items = [wrong_rank, rank, profit, wrong_site, site]
    result, call_id = ingest(w, run, pages, layouts, items, 'Ранги, производственные карточки и прибыль')
    assert result['status'] == 'partial'
    assert [(entry['candidate'], entry['code'], entry['message']) for entry in result['skipped']] == [
        (1, 'ambiguous_link', APART), (4, 'ambiguous_link', APART)]
    by_text = {fact['raw_text']: fact for fact in result['facts']}
    assert by_text['\n'.join(RANKS[1:5])]['period'] == YEAR_2024
    assert by_text['\n'.join(south)]['amount'] is None
    value, = [fact for fact in result['facts'] if fact['amount']]
    assert value['amount'] == dict(value='200', unit='money', currency='RUB', scale='1000000')
    assert value['period']['end'] == '2022-12-31' and value['source'] == pin_of(version)
    saved = saved_of(w, version)
    before = checkpoint(w, run)
    assert ingest(w, run, pages, layouts, items, 'Ранги, производственные карточки и прибыль', call_id)[0] == result
    assert saved_of(w, version) == saved and checkpoint(w, run) == before
    record('rank-caption-ingest.json', outcome=result, pages=pages, layouts=layouts)
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
