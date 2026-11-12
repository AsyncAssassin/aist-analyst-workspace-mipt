"""FLOW-01.04 and 04-R1: a qualitative passage claims its label, the first cited line, and the lines cited in one run
of text with it, however the ranges are cut; the claimed lines must be printed in the label's block on the saved
layout, not merely follow it in the text order or share one row or one neighboring line with it. Lines printed in a
block with a heading of its own, in the label's column or row, are the lines of another card and the passage is
excluded locally; a title over a paragraph with a marker beside it, a chart spread over its image, the cards cited
as printed, prose and auxiliary references keep their facts. Synthetic pages with other names and numbers than any
archive, plus a hand-authored synthetic chart; no model, no database."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app import source_cards, tables
from app.errors import ApiError
from app.extraction import APART, VERSION, candidate, extraction_request, passage_claim
from test_d14 import PIN
from test_flow01_03 import placed

COMPANY = 'Synthetic Pharma'
HEADING = ['Synthetic Pharma', 'Производственные площадки']
SOURCE = 'Источник: данные Synthetic Pharma'
NOTE = 'Примечание: мощности указаны по данным компании'
# Two site cards printed one under the other the way a presentation prints them: the name at the top left, the year
# and the area on the next row, their captions under them, the capacity at the right; the second site is planned.
# The PDF lists the lines of each card before its name, as the archived presentation does.
NORTH = ['90 млн уп.', 'Запуск производства', '2010 г.', 'Площадь производства', '12 тыс. кв.м.', 'Мощность в год', 'Северная площадка']
SOUTH = ['4 млн уп.', 'Запуск продаж', '2026 г.', 'Площадь производства', '30 тыс. кв.м.', 'Плановая мощность в год', 'Южная площадка']
LAYOUT = '\n'.join([
    'Synthetic Pharma', 'Производственные площадки', '',
    placed((4, 'Северная площадка'), (48, '90 млн уп.')),
    placed((4, '2010 г.'), (26, '12 тыс. кв.м.'), (48, 'Мощность в год')),
    placed((4, 'Запуск производства'), (26, 'Площадь производства')),
    '', '',
    placed((4, 'Южная площадка'), (48, '4 млн уп.')),
    placed((4, '2026 г.'), (26, '30 тыс. кв.м.'), (48, 'Плановая мощность в год')),
    placed((4, 'Запуск продаж'), (26, 'Площадь производства')),
    '', SOURCE])
# The same two cards printed side by side in two columns, one line under the other (the review's second layout).
SIDE_LAYOUT = '\n'.join([
    'Synthetic Pharma', '',
    placed((0, NORTH[-1]), (70, SOUTH[-1])), placed((0, NORTH[2]), (70, SOUTH[2])), placed((0, NORTH[4]), (70, SOUTH[4])),
    placed((0, NORTH[0]), (70, SOUTH[0])), placed((0, NORTH[1]), (70, SOUTH[1])), placed((0, NORTH[3]), (70, SOUTH[3])),
    placed((0, NORTH[5]), (70, SOUTH[5]))])
# The same printed page in three orders of the PDF objects: the cards as the archived presentation lists them, the
# south card first, and the north name first with the south card right after it.
ORDERS = {'the name after its lines': HEADING + NORTH + SOUTH + [SOURCE],
          'the south card first': HEADING + SOUTH + NORTH + [SOURCE],
          'the north name before the south card': HEADING + [NORTH[-1]] + SOUTH + NORTH[:-1] + [SOURCE]}
PAGES = {name: {1: '\n'.join(lines), 2: NOTE} for name, lines in ORDERS.items()}
LAYOUTS = {1: LAYOUT, 2: NOTE}
# A heading, its rank and the caption beside the rank, as a positioning slide prints them: the caption's first line
# over the rank, the rank on the caption's second row. One rank is printed twice on the page: once with its own
# caption and once with a caption listed far later in the text.
RANKS = ['№3', '№7', 'По доле рынка', 'в сегменте дженериков', 'в 2024 г.', '№3', 'По числу сотрудников', 'в отрасли',
         'Прочие сведения', 'Еще одна строка', 'По объему экспорта']
RANK_LAYOUT = '\n'.join([
    'Synthetic Pharma', '',
    placed((16, 'По числу сотрудников')), placed((6, '№3'), (16, 'в отрасли')), '', '',
    placed((6, '№3'), (16, 'По объему экспорта')), '', '',
    placed((16, 'По доле рынка')), placed((6, '№7'), (16, 'в сегменте дженериков')), placed((16, 'в 2024 г.')), '',
    placed((16, 'Прочие сведения')), placed((16, 'Еще одна строка'))])
RANK_PAGES = {1: '\n'.join(['Synthetic Pharma', *RANKS])}
YEAR_2024 = dict(kind='interval', label='2024 г.', start='2024-01-01', end='2024-12-31')
# A title, a paragraph two rows under it and a section marker in the right margin beside the title (the review's
# F03 page); the same page without the marker.
PROSE = {1: 'Synthetic Pharma\nО компании\nКомпания выпускает препараты\nи поставляет их в аптеки.\nРаздел 2'}
PROSE_LAYOUT = '\n'.join(['Synthetic Pharma', '', '', '', placed((0, 'О компании'), (70, 'Раздел 2')), '', '',
                          'Компания выпускает препараты', 'и поставляет их в аптеки.'])
PROSE_LAYOUT_NO_MARKER = PROSE_LAYOUT.replace(placed((0, 'О компании'), (70, 'Раздел 2')), 'О компании')
CHART = json.loads(Path(__file__).with_name('fixtures').joinpath('flow01', 'synthetic-chart.json').read_text())


def range_of(text, lines):
    """The numbers of the one run of original lines equal to these, in this order."""
    page = text.splitlines()
    starts = [i + 1 for i in range(len(page) - len(lines) + 1) if page[i:i + len(lines)] == list(lines)]
    assert len(starts) == 1, (lines, starts)
    return starts[0], starts[0] + len(lines) - 1


def passage(text, lines, *extra, period=None, subject=COMPANY, page=1, metric=None):
    start, end = range_of(text, lines)
    return dict(metric=metric, subject=subject, reporting_basis='Не указано', nature='source_statement',
                period=period or dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None,
                basis=None, evidence=[dict(page=page, line_start=start, line_end=end, area='Карточка'), *extra])


def refused(item, pages, layouts=LAYOUTS, diagrams=None):
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, item, layouts, diagrams=diagrams)
    return getattr(error.value, 'skip_code', None), error.value.body['error']['message']


def admitted(item, pages, layouts=LAYOUTS, diagrams=None):
    before = deepcopy(item)
    fact, _ = candidate(PIN, pages, item, layouts, diagrams=diagrams)
    assert item == before
    assert fact['nature'] == 'source_statement' and fact['amount'] is None and fact['extraction_version'] == VERSION
    return fact


def content(fact):
    """What a saved passage states, apart from the technical IDs and the line numbers of one text order."""
    return (fact['metric'], fact['raw_text'], fact['period'], tuple(fact['limitations']),
            tuple(sorted(e['quote'] for e in fact['evidence'] if not e['locator']['area'].startswith('Исходный атрибут'))))


def grounds(fact):
    """The lines a fact rests on, whichever ranges cite them: the quotes of its evidence as one set of lines."""
    return sorted({line for e in fact['evidence'] if not e['locator']['area'].startswith('Исходный атрибут') for line in e['quote'].splitlines()})


def split(item, at):
    """The same candidate with its first range cut into the lines before at and the lines from at on."""
    first, *rest = item['evidence']
    assert first['line_start'] < at <= first['line_end']
    return dict(item, evidence=[dict(first, line_end=at - 1), dict(first, line_start=at), *rest])


def test_the_name_of_one_card_is_not_bound_to_the_lines_of_the_other_whatever_the_text_order():
    """Check 3: the name of the north site cited with the lines of the south site is excluded, with or without the
    south name, in every order of the PDF objects; each card cited as printed keeps its fact with the same content,
    the planned site with its plan words. The decision follows the printed places, not the text order."""
    verdicts = {}

    def contiguous(text, lines):
        page = text.splitlines()
        return any(page[i:i + len(lines)] == lines for i in range(len(page)))
    for order, text in PAGES.items():
        for name, lines in (('north', NORTH), ('south', SOUTH)):
            foreign = SOUTH if name == 'north' else NORTH
            for glued in ([lines[-1], *foreign[:-1]], [lines[-1], *foreign]):
                if contiguous(text[1], glued):
                    assert refused(passage(text[1], glued), text) == ('ambiguous_link', APART), (order, name, glued)
                    verdicts[(order, name, 'transfer')] = 'excluded'
            own = lines if contiguous(text[1], lines) else lines[:-1]
            fact = admitted(passage(text[1], own), text)
            assert fact['metric'] == lines[0] and fact['raw_text'] == '\n'.join(own)
            verdicts[(order, name, 'own')] = content(fact)
    # The transfer is excluded in every order it can be cited in; the north name before the south card is the order of
    # the archived defect, the name after its lines the order of the archived PDF.
    assert {key for key, value in verdicts.items() if value == 'excluded'} == {
        ('the name after its lines', 'north', 'transfer'), ('the south card first', 'south', 'transfer'),
        ('the north name before the south card', 'north', 'transfer'), ('the north name before the south card', 'south', 'transfer')}
    # The cards keep the same content under the two orders citing them whole; the plan words of the south site stay.
    assert verdicts[('the name after its lines', 'north', 'own')] == verdicts[('the south card first', 'north', 'own')]
    assert verdicts[('the name after its lines', 'south', 'own')] == verdicts[('the south card first', 'south', 'own')]
    south = verdicts[('the name after its lines', 'south', 'own')][1]
    assert all(word in south for word in ('Запуск продаж', '2026 г.', 'Плановая мощность в год', 'Южная площадка'))


def test_auxiliary_references_neither_rescue_a_transfer_nor_spoil_a_card():
    """An issuer identity, the source line of the page and a footnote on another page are separate references: added
    in any order, they change neither the exclusion of the transfer nor the content of the cards."""
    text = PAGES['the name after its lines']
    issuer = dict(page=1, line_start=1, line_end=1, area='Эмитент')
    source = dict(page=1, line_start=len(ORDERS['the name after its lines']), line_end=len(ORDERS['the name after its lines']), area='Источник')
    note = dict(page=2, line_start=1, line_end=1, area='Примечание')
    transfer = [NORTH[-1], *SOUTH[:-1]]
    for extras in ((issuer, source, note), (note, source, issuer), (source,), (note,)):
        assert refused(passage(text[1], transfer, *extras), text) == ('ambiguous_link', APART)
        fact = admitted(passage(text[1], NORTH, *extras), text)
        assert fact['raw_text'] == '\n'.join(NORTH)
        quotes = [e['quote'] for e in fact['evidence']]
        assert all(any(text[e['page']].splitlines()[e['line_start'] - 1] == quote for quote in quotes) for e in extras)
    plain = admitted(passage(text[1], NORTH), text)
    assert content(plain)[:4] == content(admitted(passage(text[1], NORTH, note, issuer), text))[:4]


def test_the_same_transfer_cut_into_ranges_or_behind_an_identity_reference_is_excluded():
    """R1-F01: the review's forms of the archived transfer, the same lines cut into the name and the foreign lines,
    and the issuer identity cited first with the explicit metric, are excluded like the one range; the lines cited
    in one run of text are the claim whichever ranges cite them. The north card under the same transformations, with
    an identity reference on the page and a footnote on another page, keeps the same grounds and content."""
    text = PAGES['the name after its lines']
    issuer = dict(page=1, line_start=1, line_end=1, area='Эмитент')
    note = dict(page=2, line_start=1, line_end=1, area='Примечание')
    transfer = passage(text[1], [NORTH[-1], *SOUTH[:-1]])
    name_line = transfer['evidence'][0]['line_start']
    forms = {'one range': transfer, 'two ranges': split(transfer, name_line + 1),
             'identity first': dict(transfer, metric=NORTH[-1], evidence=[issuer, *transfer['evidence']]),
             'identity first, two ranges': dict(split(transfer, name_line + 1), metric=NORTH[-1], evidence=[issuer, *split(transfer, name_line + 1)['evidence']]),
             'three ranges': dict(transfer, evidence=[dict(transfer['evidence'][0], line_end=name_line), dict(transfer['evidence'][0], line_start=name_line + 1, line_end=name_line + 2),
                                                     dict(transfer['evidence'][0], line_start=name_line + 3)])}
    for form, item in forms.items():
        assert refused(item, text) == ('ambiguous_link', APART), form
        page, label, claim = passage_claim(text, NORTH[-1], item['evidence'])
        assert (page, label, claim) == (1, name_line, list(range(name_line, name_line + 7))), form
    card = passage(text[1], NORTH)
    first = card['evidence'][0]['line_start']
    kept = {'one range': card, 'two ranges': split(card, first + 3), 'identity first': dict(card, metric=NORTH[0], evidence=[issuer, *card['evidence']]),
            'identity first, two ranges, note': dict(split(card, first + 3), metric=NORTH[0], evidence=[issuer, *split(card, first + 3)['evidence'], note])}
    facts = {form: admitted(item, text) for form, item in kept.items()}
    assert all(fact['metric'] == NORTH[0] and fact['period'] == card['period'] for fact in facts.values())
    assert all(set(NORTH) <= set(grounds(fact)) for fact in facts.values())
    assert grounds(facts['one range']) == grounds(facts['two ranges'])
    assert grounds(facts['identity first']) == sorted(set(NORTH) | {COMPANY})
    assert grounds(facts['identity first, two ranges, note']) == sorted(set(NORTH) | {COMPANY, NOTE})
    # A reference cited apart in the text is no claim: the claim stays the run around the label.
    assert passage_claim(text, NORTH[0], kept['identity first, two ranges, note']['evidence']) == (1, first, list(range(first, first + 7)))
    # A separate later range is checked as well: a valid issuer reference cannot hide a mixed passage.
    later = dict(transfer, metric=COMPANY, evidence=[issuer, *transfer['evidence']])
    assert refused(later, text) == ('ambiguous_link', APART)


@pytest.mark.parametrize('form', ['uncited line', 'identity first', 'blank line'])
def test_disjoint_foreign_properties_still_belong_to_the_other_card(form):
    """04-R2: the three exact negative inputs of the R1 review, through candidate."""
    pages = PAGES['the name after its lines']
    wrong = passage(pages[1], [NORTH[-1], *SOUTH[:-1]])
    title = dict(wrong['evidence'][0], line_end=wrong['evidence'][0]['line_start'])
    foreign = dict(wrong['evidence'][0], line_start=title['line_start'] + 2)
    item = dict(wrong, evidence=[title, foreign])
    if form == 'identity first':
        issuer = dict(page=1, line_start=1, line_end=1, area='Эмитент')
        item = dict(item, metric=NORTH[-1], evidence=[issuer, title, foreign])
    if form == 'blank line':
        lines = pages[1].splitlines()
        lines.insert(title['line_end'], '')
        pages = {**pages, 1: '\n'.join(lines)}
        item['evidence'] = [title, dict(foreign, line_end=foreign['line_end'] + 1)]
    before = deepcopy(item)
    assert refused(item, pages) == ('ambiguous_link', APART)
    assert item == before
    # Model-supplied area labels cannot turn properties into auxiliary references.
    disguised = dict(item, evidence=[dict(e, area='Примечание') for e in item['evidence']])
    assert refused(disguised, pages) == ('ambiguous_link', APART)


def test_disjoint_own_year_keeps_its_grounds_with_auxiliary_references_in_any_order():
    pages = PAGES['the name after its lines']
    title = passage(pages[1], [NORTH[-1]])['evidence'][0]
    year = passage(pages[1], [NORTH[2]])['evidence'][0]
    issuer = dict(page=1, line_start=1, line_end=1, area='Карточка')
    source_line = len(pages[1].splitlines())
    source = dict(page=1, line_start=source_line, line_end=source_line, area='Карточка')
    note = dict(page=2, line_start=1, line_end=1, area='Карточка')
    plain = dict(passage(pages[1], [NORTH[-1]]), evidence=[title, year])
    forms = [plain, dict(plain, evidence=[title, year, issuer, source, note]),
             dict(plain, metric=NORTH[-1], evidence=[issuer, title, year, source, note]),
             dict(plain, metric=NORTH[-1], evidence=[note, source, year, title, issuer])]
    for index, item in enumerate(forms):
        fact = admitted(item, pages)
        assert fact['metric'] == NORTH[-1] and fact['period'] == plain['period']
        expected = {NORTH[-1], NORTH[2]} | ({COMPANY, SOURCE, NOTE} if index else set())
        assert grounds(fact) == sorted(expected)
    # A separate submission keeps the literal foreign quotation under its own label.
    separate = admitted(passage(pages[1], SOUTH[1:-1]), pages)
    assert grounds(separate) == sorted(set(SOUTH[1:-1]))


def test_one_own_line_or_one_horizontal_does_not_attribute_the_other_properties():
    """R1-F02: the north name with its own year and the lines of the south card is excluded on the vertical layout;
    on the two-column layout the name with the foreign column is excluded while the north card cited as printed
    passes. The decision rests on where each claimed line is printed, not on one neighboring line."""
    lines = ['Synthetic Pharma', NORTH[-1], NORTH[2], *SOUTH[:-1]]
    with_year = {1: '\n'.join(lines)}
    assert refused(passage(with_year[1], lines[1:]), with_year) == ('ambiguous_link', APART)
    own_region = {1: [dict(label_lines=[2], content_lines=[3])]}
    assert refused(passage(with_year[1], lines[1:]), with_year, diagrams=own_region) == ('ambiguous_link', APART)
    assert admitted(passage(with_year[1], lines[1:3]), with_year)['raw_text'] == '\n'.join(lines[1:3])
    text = PAGES['the name after its lines']
    side = {1: SIDE_LAYOUT, 2: NOTE}
    assert refused(passage(text[1], [NORTH[-1], *SOUTH[:-1]]), text, side) == ('ambiguous_link', APART)
    assert refused(passage(text[1], [NORTH[-1], *SOUTH]), text, side) == ('ambiguous_link', APART)
    north = admitted(passage(text[1], NORTH), text, side)
    south = admitted(passage(text[1], SOUTH), text, side)
    assert north['raw_text'] == '\n'.join(NORTH) and south['raw_text'] == '\n'.join(SOUTH)
    assert content(north) == content(admitted(passage(text[1], NORTH), text)) and content(south) == content(admitted(passage(text[1], SOUTH), text))


def test_a_marker_beside_a_title_or_the_spread_of_one_chart_proves_no_mixing():
    """R1-F03: the title and its paragraph pass with the section marker in the margin as without it; the generic share
    chart of the synthetic fixture, cited as one qualitative passage with an unknown period and the issuer
    identity of another page, keeps its words, its forecast mark and its sources without any diagram region. Neither
    page shows a block with a heading of its own holding the claimed lines."""
    marked = admitted(passage(PROSE[1], PROSE[1].splitlines()[1:4]), PROSE, {1: PROSE_LAYOUT})
    plain = admitted(passage(PROSE[1], PROSE[1].splitlines()[1:4]), PROSE, {1: PROSE_LAYOUT_NO_MARKER})
    assert content(marked) == content(plain) and marked['raw_text'] == 'О компании\nКомпания выпускает препараты\nи поставляет их в аптеки.'
    pages = {int(n): text for n, text in CHART['pages'].items()}
    layouts = {int(n): text for n, text in CHART['layouts'].items()}
    diagrams = {int(n): regions for n, regions in CHART['diagrams'].items()}
    assert diagrams[10] == [] and pages[10].splitlines()[2] == '27% 63%'
    pin = dict(PIN, sha256=CHART['sha256'])
    chart = dict(metric=None, subject='Синтетическая Фарма', reporting_basis='Не указано', nature='source_statement',
                 period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None, basis=None,
                 evidence=[dict(page=10, line_start=3, line_end=15, area='Диаграмма учебного сегмента'), dict(page=2, line_start=3, line_end=4, area='Эмитент')])
    fact, _ = candidate(pin, pages, chart, layouts, diagrams=diagrams)
    assert fact['metric'] == '27% 63%' and fact['amount'] is None and fact['period']['kind'] == 'unknown'
    assert fact['raw_text'] == '\n'.join(pages[10].splitlines()[2:15])
    assert all(word in fact['raw_text'] for word in ('2032П', 'Объём рынка, трлн руб.', 'Источники: данные Компании'))
    assert candidate(pin, pages, chart, None)[0]['raw_text'] == fact['raw_text']


def test_rank_cards_with_a_shared_rank_bind_each_caption_to_the_rank_printed_beside_it():
    """The rank cited with the caption of the other rank is excluded, so is the passage repeating the rank around the
    other caption: the caption stands in a block with the other rank, printed in the label's column. The rank cited
    with its own caption keeps its fact and its period; two ranks alone, each without its caption, show no block
    with a heading of its own and stay an unestablished passage."""
    assert refused(passage(RANK_PAGES[1], RANKS[:5], period=YEAR_2024), RANK_PAGES, {1: RANK_LAYOUT}) == ('ambiguous_link', APART)
    assert refused(passage(RANK_PAGES[1], RANKS[:8], period=YEAR_2024), RANK_PAGES, {1: RANK_LAYOUT}) == ('ambiguous_link', APART)
    assert admitted(passage(RANK_PAGES[1], RANKS[:2]), RANK_PAGES, {1: RANK_LAYOUT})['raw_text'] == '№3\n№7'
    seven = admitted(passage(RANK_PAGES[1], RANKS[1:5], period=YEAR_2024), RANK_PAGES, {1: RANK_LAYOUT})
    assert seven['metric'] == '№7' and seven['period'] == YEAR_2024 and seven['raw_text'] == '\n'.join(RANKS[1:5])
    three = admitted(passage(RANK_PAGES[1], RANKS[5:8]), RANK_PAGES, {1: RANK_LAYOUT})
    assert three['metric'] == '№3' and three['raw_text'] == '\n'.join(RANKS[5:8])
    # The caption cited by itself names no rank and is a passage of its own.
    assert admitted(passage(RANK_PAGES[1], RANKS[2:5], period=YEAR_2024), RANK_PAGES, {1: RANK_LAYOUT})['metric'] == 'По доле рынка'


def test_prose_and_lines_the_layout_cannot_place_are_not_excluded():
    """A paragraph, a paragraph with a number, a single line, a label the layout wraps, a page without a layout and a
    passage whose other lines are printed twice give no new refusal: there is no block of another heading to show."""
    text = {1: 'Synthetic Pharma\nКомпания производит дженерики для российского рынка\nи поставляет их через дистрибьюторов.\nДоля розничных продаж составила 80% в 2024 г.'}
    layout = {1: 'Synthetic Pharma\nКомпания производит дженерики для российского рынка\nи поставляет их через дистрибьюторов.\nДоля розничных продаж составила 80% в 2024 г.'}
    assert admitted(passage(text[1], text[1].splitlines()[1:3]), text, layout)['raw_text'].startswith('Компания производит')
    assert admitted(passage(text[1], text[1].splitlines()[1:4]), text, layout)['metric'] == 'Компания производит дженерики для российского рынка'
    assert admitted(passage(text[1], text[1].splitlines()[3:4]), text, layout)['metric'] == 'Доля розничных продаж составила 80% в 2024 г.'
    order = PAGES['the name after its lines']
    transfer = passage(order[1], [NORTH[-1], *SOUTH[:-1]])
    # The same transfer with the label wrapped onto two rows of the layout, printed nowhere whole: not checkable.
    wrapped = {1: LAYOUT.replace(placed((4, 'Северная площадка'), (48, '90 млн уп.')), placed((4, 'Северная'), (48, '90 млн уп.')) + '\n' + placed((4, 'площадка')))}
    assert admitted(transfer, order, wrapped)['metric'] == 'Северная площадка'
    assert admitted(transfer, order, None)['metric'] == 'Северная площадка'
    assert admitted(transfer, order, {1: ''})['metric'] == 'Северная площадка'
    # The other line of the passage printed twice, once in the rank's own caption: nothing of another heading.
    twice = passage(RANK_PAGES[1], ['№7', 'По доле рынка'])
    doubled = {1: RANK_LAYOUT + '\n' + placed((16, 'По доле рынка'))}
    assert admitted(twice, RANK_PAGES, doubled)['metric'] == '№7'


def test_a_diagram_region_binds_a_label_the_grid_prints_apart():
    """A chart title and the values of its sectors stand apart on the grid: without regions the passage is kept as
    unestablished, the region of the chart image holding them all binds it, and the values inside the region of
    another chart under that chart's own uncited heading exclude it; a region of the values with no heading of its
    own establishes nothing."""
    text = {1: 'Synthetic Pharma\nСтруктура выручки\nДженерики 60%\nОригинальные препараты 40%\nСтруктура затрат'}
    layout = {1: '\n'.join(['Synthetic Pharma', 'Структура выручки', '', '', '', placed((30, 'Структура затрат')), placed((30, 'Дженерики 60%')), placed((30, 'Оригинальные препараты 40%'))])}
    chart = passage(text[1], text[1].splitlines()[1:4])
    assert admitted(chart, text, layout)['raw_text'] == '\n'.join(text[1].splitlines()[1:4])
    regions = {1: [dict(method='pdf-mask-components.v1', label_lines=[2], content_lines=[3, 4], bounds=[0, 0, 100, 100])]}
    assert admitted(chart, text, layout, diagrams=regions)['raw_text'] == '\n'.join(text[1].splitlines()[1:4])
    other = {1: [dict(method='pdf-mask-components.v1', label_lines=[2], content_lines=[], bounds=[0, 0, 100, 100]),
                 dict(method='pdf-mask-components.v1', label_lines=[5], content_lines=[3, 4], bounds=[0, 0, 100, 100])]}
    assert refused(chart, text, layout, diagrams=other) == ('ambiguous_link', APART)
    unnamed = {1: [dict(method='pdf-mask-components.v1', label_lines=[2], content_lines=[], bounds=[0, 0, 100, 100]),
                   dict(method='pdf-mask-components.v1', label_lines=[], content_lines=[3, 4], bounds=[0, 0, 100, 100])]}
    assert admitted(chart, text, layout, diagrams=unnamed)['metric'] == 'Структура выручки'


def test_label_bound_tells_bound_apart_and_unchecked_from_printed_places():
    """The helper on the synthetic pages: the claim of the archived order, the cards as printed, the transfer and the
    forms of the review; ties between cells; the claim around a label from cut ranges."""
    lines = ORDERS['the name after its lines']
    north, south = range_of(PAGES['the name after its lines'][1], NORTH), range_of(PAGES['the name after its lines'][1], SOUTH)
    assert source_cards.label_bound(lines, LAYOUT, north[0], range(*north)) is True
    assert source_cards.label_bound(lines, LAYOUT, south[0], range(south[0], south[1] + 1)) is True
    assert source_cards.label_bound(lines, LAYOUT, north[1], range(north[1], south[1])) is False         # the transfer
    assert source_cards.label_bound(lines, LAYOUT, north[1], range(north[1], south[1] + 1)) is False     # ... with the south name
    assert source_cards.label_bound(lines, SIDE_LAYOUT, north[1], range(north[1], south[1])) is False    # two columns
    assert source_cards.label_bound(lines, SIDE_LAYOUT, north[0], range(*north)) is True
    assert source_cards.label_bound(lines, LAYOUT, north[1], [north[1]]) is None                        # the label alone
    assert source_cards.label_bound(lines, '', north[1], range(north[1], south[1])) is None
    prose = PROSE[1].splitlines()
    assert source_cards.label_bound(prose, PROSE_LAYOUT, 2, [2, 3, 4]) is None                          # a title, a marker, a paragraph apart
    assert source_cards.label_bound(prose, PROSE_LAYOUT_NO_MARKER, 2, [2, 3, 4]) is None
    headed = PROSE_LAYOUT.replace('\n\n\nКомпания', '\n\nДеятельность\nКомпания')
    assert source_cards.label_bound(prose + ['Деятельность'], headed, 2, [2, 3, 4]) is False            # ... under an uncited heading aligned with the title
    assert source_cards.label_bound(prose, PROSE_LAYOUT.replace('О компании', 'О'), 2, [2, 3, 4]) is None   # the label printed nowhere whole
    rows = tables.layout_rows(LAYOUT)
    name, capacity, year, area = (3, 0), (3, 1), (4, 0), (4, 1)
    assert source_cards.tied(rows, name, capacity) and source_cards.tied(rows, name, year) and source_cards.tied(rows, year, area)
    assert not source_cards.tied(rows, name, area) and not source_cards.tied(rows, name, (5, 1))        # another column, one row under
    assert source_cards.tied(rows, name, (5, 0)) and not source_cards.tied(rows, name, (8, 0))         # one row apart in one column; further apart
    assert source_cards.tied(rows, (3, 0), (3, 1)) and not source_cards.tied(rows, (4, 0), (4, 2))     # neighbors on a row; a cell between
    text = PAGES['the name after its lines']
    assert passage_claim(text, NORTH[-1], [dict(page=1, line_start=north[1], line_end=north[1]), dict(page=1, line_start=north[1] + 1, line_end=south[1])]) == (1, north[1], list(range(north[1], south[1] + 1)))
    assert passage_claim(text, NORTH[-1], [dict(page=1, line_start=1, line_end=1), dict(page=1, line_start=north[1], line_end=south[1])]) == (1, north[1], list(range(north[1], south[1] + 1)))
    assert passage_claim(text, NOTE, [dict(page=2, line_start=1, line_end=1), dict(page=1, line_start=north[1], line_end=south[1])]) == (2, 1, [1])


def test_the_extractor_is_told_to_cite_each_card_with_its_own_lines():
    request = extraction_request(PAGES['the name after its lines'], 'Производственные площадки', LAYOUTS)
    instruction = request['messages'][0]['content']
    assert 'never join a label with the lines of another block' in instruction
    assert 'such a range is a reference and does not' in instruction
    assert 'excludes a passage whose cited lines are printed in a block' in instruction
    sent = json.loads(request['messages'][1]['content'])
    assert [line['text'] for line in sent['pages'][0]['lines']] == ORDERS['the name after its lines']
    assert 'table_layout' not in sent['pages'][0] and 'diagram_regions' not in sent['pages'][0]
