"""Printed single-series charts on the saved text grid, with original fragment references.

The same geometry supplies request metadata and verifies explicit/derived observations. Text order,
equal values and model area labels never assign a column. Unresolved charts keep their competing
values so an older, weaker path cannot override a geometric contradiction.
"""
from calendar import monthrange
from datetime import date
from itertools import combinations
import re

from . import tables
from .source_cards import FORECAST, NUMBER, STOCK_METRICS, forecast_above

UNIT = r'(?:млн|тыс|млрд|трлн|триллион(?:а|ов)?)\.?\s*(?:руб\.?|₽|RUB)'
HEADING = re.compile(rf'(?P<metric>.+?),\s*(?P<unit>{UNIT}|%)$', re.I)
PERIOD = re.compile(r"(?:(?P<months>3|6|9|12)[mм]['’]?(?P<myear>\d{4}|\d{2})|"
                    r"(?P<quarter>[1-4])Q(?P<qyear>\d{4}|\d{2})|(?P<year>(?:19|20)\d{2}))", re.I)
VALUE = re.compile(rf'{NUMBER}%?')
MAX_CHARTS = 4
# A debt balance is measured at the closing date, like the existing equity cards.
# These literal financial captions never turn debt repayment/expense flows into balances.
DEBT_BALANCE = re.compile(r'(?:(?:чистый|валовой|совокупный)\s+)?долг', re.I)


def normalized(text):
    return tables.squeeze(text).casefold().replace('’', "'").replace('м', 'm')


def period(text, metric):
    """The existing year, quarter and cumulative month forms; forecasts stay distinguishable."""
    label = text.strip()
    forecast = bool(FORECAST.search(label))
    plain = re.sub(r'\s*\(?прогноз\)?\s*', '', label, flags=re.I)
    match = PERIOD.fullmatch(normalized(plain))
    if not match:
        return None
    year_text = match['year'] or match['myear'] or match['qyear']
    year = int(year_text) + (2000 if len(year_text) == 2 else 0)
    if not 1 <= year <= 9999:
        return None
    month = int(match['months']) if match['months'] else int(match['quarter']) * 3 if match['quarter'] else 12
    start_month = month - 2 if match['quarter'] else 1
    end = date(year, month, monthrange(year, month)[1]).isoformat()
    stock = any(name in normalized(metric) for name in STOCK_METRICS) or bool(DEBT_BALANCE.fullmatch(metric.strip()))
    return dict(kind='instant' if stock else 'interval', label=label,
                start=end if stock else date(year, start_month, 1).isoformat(), end=end, forecast=forecast)


def fragments(original, rows):
    """Original lines that print each complete grid cell, including cells of a joined heading.

    Identically printed lines retain every possible reference. Their text cannot choose a place;
    the chart/period geometry must do that. We never assign repeated lines by PDF stream order.
    """
    runs = tables.spellings(rows)
    references = {}
    for line, text in enumerate(original, 1):
        for row, first, last in runs.get(tables.squeeze(text), []):
            for index in range(first, last + 1):
                references.setdefault((row, index), []).append(line)
    # PDF stream lines can join values printed at different heights. Match complete number
    # fragments, not substrings of another number, without assigning them by text order.
    for row, cells in enumerate(rows):
        for index, cell in enumerate(cells):
            if (row, index) in references or not VALUE.fullmatch(cell[2]):
                continue
            spelling = r'\s*'.join(re.escape(char) for char in tables.squeeze(cell[2]))
            token = re.compile(r'(?<![\w.,])' + spelling + r'(?![\w.,])')
            found = [line for line, text in enumerate(original, 1) if token.search(text)]
            if found:
                references[row, index] = found
    return {place: dict(text=rows[place[0]][place[1]][2], lines=sorted(set(lines)),
                        grid=[place[0], *rows[place[0]][place[1]][:2]])
            for place, lines in references.items()}


def partitions(headings, markers):
    """One consecutive period run per heading, aligned at its left edge or centered over it."""
    if not 1 <= len(headings) <= MAX_CHARTS or not 2 * len(headings) <= len(markers) <= tables.MAX_COLUMNS * len(headings):
        return None
    found = []
    for cuts in combinations(range(1, len(markers)), len(headings) - 1):
        bounds = [0, *cuts, len(markers)]
        groups = [markers[a:b] for a, b in zip(bounds, bounds[1:])]
        if any(not 2 <= len(group) <= tables.MAX_COLUMNS for group in groups):
            continue
        # A repeated period within one run indicates neighboring axes or competing series.
        if any(len({(period(cell[2], '')['start'], period(cell[2], '')['end']) for _, cell in group}) != len(group)
               for group in groups):
            continue
        if all(abs(heading[1][0] - group[0][1][0]) <= tables.TOLERANCE
               or abs(tables.center(heading[1]) - (group[0][1][0] + group[-1][1][1]) / 2) <= tables.TOLERANCE
               for heading, group in zip(headings, groups)):
            found.append(groups)
    return found[0] if len(found) == 1 else None


def page_charts(original, layout):
    rows = tables.layout_rows(layout)
    refs = fragments(original, rows)
    charts = []
    for axis, cells in enumerate(rows):
        markers = [(i, cell) for i, cell in enumerate(cells) if period(cell[2], '')]
        if len(markers) < 2:
            continue
        # A nearer period axis separates vertically stacked charts.
        heading_row = None
        for r in range(axis - 1, -1, -1):
            if sum(period(cell[2], '') is not None for cell in rows[r]) >= 2:
                break
            headings = [(i, cell) for i, cell in enumerate(rows[r]) if HEADING.fullmatch(cell[2])]
            if headings:
                heading_row = r
                break
        if heading_row is None:
            continue
        groups = partitions(headings, markers)
        if groups is None:
            # The axes/headings exist but do not separate the series. Keep the printed competitors:
            # source-text adjacency is no escape from this unresolved geometric assignment.
            left = markers[0][1][0] - tables.TOLERANCE
            right = markers[-1][1][1] + tables.TOLERANCE
            values = [refs[r, i] for r in range(heading_row + 1, axis) for i, cell in enumerate(rows[r])
                      if left <= tables.center(cell) <= right and VALUE.fullmatch(cell[2]) and (r, i) in refs]
            if values:
                charts.append(dict(metric='', unit='', heading=None, headings=[refs.get((heading_row, i)) for i, _ in headings],
                                   values=values, periods=[], observations=[], ambiguous=True))
            continue
        for (heading_index, heading), columns in zip(headings, groups):
            caption = HEADING.fullmatch(heading[2])
            metric, unit = caption['metric'], caption['unit']
            column_bands = tables.bands(columns)
            left, right = column_bands[0][0], column_bands[-1][1]
            values = []
            aligned = [[] for _ in columns]
            forecast = bool(FORECAST.search(metric)) or forecast_above(layout.splitlines(), heading[0], heading_row)
            centers = [tables.center(cell) for _, cell in columns]
            ambiguous = any(b - a < tables.MIN_PITCH for a, b in zip(centers, centers[1:]))
            for r in range(heading_row + 1, axis):
                for index, cell in enumerate(rows[r]):
                    if not left <= tables.center(cell) < right:
                        continue
                    forecast |= bool(FORECAST.search(cell[2]))
                    if not VALUE.fullmatch(cell[2]):
                        continue
                    # Profitability annotations are not another monetary series.
                    if unit != '%' and cell[2].endswith('%'):
                        continue
                    value = refs.get((r, index), dict(text=cell[2], lines=[], grid=[r, *cell[:2]]))
                    values.append(value)
                    matches = [n for n, (_, marker) in enumerate(columns)
                               if abs(tables.center(cell) - tables.center(marker)) <= tables.TOLERANCE
                               and column_bands[n][0] <= cell[0] <= cell[1] <= column_bands[n][1]]
                    if len(matches) != 1 or not value['lines']:
                        ambiguous = True
                    else:
                        aligned[matches[0]].append(value)
            if not values:
                continue
            ambiguous |= any(len(values_in_column) != 1 for values_in_column in aligned)
            title = refs.get((heading_row, heading_index))
            labels = [refs.get((axis, index)) for index, _ in columns]
            ambiguous |= title is None or any(label is None for label in labels)
            observations = []
            if not ambiguous:
                for values_in_column, label in zip(aligned, labels):
                    when = period(label['text'], metric)
                    observations.append(dict(value=values_in_column[0], label=label,
                                             period={k: v for k, v in when.items() if k != 'forecast'},
                                             forecast=forecast or when['forecast']))
            charts.append(dict(metric=metric, unit=unit, heading=title, headings=[title], values=values,
                               periods=[period(cell[2], metric) for _, cell in columns],
                               observations=observations, ambiguous=ambiguous))
    # Apply the same uniqueness rule before exposing links to either caller. Even an unresolved
    # plot with this caption and period is a competitor; its failed amount alignment cannot hide it.
    for chart in charts:
        unique = []
        for observation in chart['observations']:
            competitors = [other for other in charts for when in other['periods']
                           if normalized(other['metric']) == normalized(chart['metric'])
                           and all(when[key] == observation['period'][key] for key in ('kind', 'start', 'end'))]
            if len(competitors) == 1:
                unique.append(observation)
        chart['observations'] = unique
    return charts


def context(original, layout):
    """Only supported links, without repeating full source lines or proposing synthetic quotations."""
    return [dict(metric=chart['metric'], unit=chart['unit'], heading=chart['heading'],
                 observations=chart['observations']) for chart in page_charts(original, layout) if chart['observations']]


def cited(fragment, selected):
    return fragment is not None and bool(set(fragment['lines']) & selected)


def separate_observation(original, layout, selected, item, chart):
    """A duplicate spelling can belong to a separately quoted source observation.

    Require its own printed metric/unit, value and period outside this plot. Neither the
    model's metric nor PDF stream order assigns a duplicate value to a position. This only
    removes a false chart claim; the existing numeric path still verifies the whole fact.
    """
    basis = item['basis']
    # A quoted chart caption still claims that chart, including ambiguous axes. Identical
    # captions cannot disambiguate duplicate source lines by their PDF stream order.
    if basis is None or any(heading is None or cited(heading, selected) for heading in chart['headings']):
        return False
    rows = tables.layout_rows(layout)
    refs = fragments(original, rows)
    plot = {tuple(value['grid']) for value in chart['values']}
    for (r, i), value in refs.items():
        if tuple(value['grid']) in plot or not cited(value, selected) or normalized(value['text']) != normalized(item['value_text']):
            continue
        heading_row = next((n for n in range(r - 1, -1, -1) if any(HEADING.fullmatch(cell[2]) for cell in rows[n])), None)
        if heading_row is None:
            continue
        for h, cell in enumerate(rows[heading_row]):
            caption = HEADING.fullmatch(cell[2])
            if (not caption or not cited(refs.get((heading_row, h)), selected)
                    or normalized(item['metric']) not in {normalized(caption['metric']), normalized(cell[2])}
                    or normalized(basis['metric_label']) not in {normalized(caption['metric']), normalized(cell[2])}
                    or normalized(caption['unit']) != normalized(basis['unit_label'])
                    or abs(cell[0] - rows[r][i][0]) > tables.TOLERANCE
                    or FORECAST.search(caption['metric'])):
                continue
            # A separate observation has one value followed by its own period. Other
            # intervening content ends this ownership proof and preserves ambiguity.
            following = next((n for n in range(r + 1, len(rows)) if rows[n]), None)
            if following is None or any(rows[n] for n in range(heading_row + 1, r)):
                continue
            labels = [(n, marker) for n, marker in enumerate(rows[following])
                      if normalized(marker[2]) == normalized(item['period']['label'])
                      and abs(tables.center(marker) - tables.center(rows[r][i])) <= tables.TOLERANCE
                      and cited(refs.get((following, n)), selected)]
            if len(labels) == 1:
                return True
    return False


def resolve(pages, layouts, item):
    """Return the one cited chart observation, or None if no chart establishes a relation/contradiction."""
    selected = {}
    for locator in item['evidence']:
        selected.setdefault(locator['page'], set()).update(range(locator['line_start'], locator['line_end'] + 1))
    touched, matches = [], []
    for page, lines in selected.items():
        charts = page_charts(pages[page].splitlines(), layouts.get(page) or '')
        for chart in charts:
            values = [value for value in chart['values'] if cited(value, lines)
                      and normalized(value['text']).removesuffix('%') == normalized(item['value_text']).removesuffix('%')]
            if not values:
                continue
            if separate_observation(pages[page].splitlines(), layouts.get(page) or '', lines, item, chart):
                continue
            touched.append(chart)
            if chart['ambiguous'] or not cited(chart['heading'], lines):
                continue
            if normalized(item['metric']) not in {normalized(chart['metric']), normalized(chart['heading']['text'])}:
                continue
            for observation in chart['observations']:
                if observation['value'] not in values or not cited(observation['label'], lines):
                    continue
                matches.append((page, chart, observation))
    if not touched:
        return None
    valid = []
    for page, chart, observation in matches:
        when = period(item['period']['label'], chart['metric'])
        expected = observation['period']
        if when and not when['forecast'] and not observation['forecast'] and all(
                item['period'][key] == expected[key] == when[key] for key in ('kind', 'start', 'end')):
            valid.append((page, chart, observation))
    if len(valid) != 1:
        code = 'period_mismatch' if matches and not valid else 'ambiguous_link'
        raise tables.Unsupported(code, 'График не подтверждает единственную связь процитированного числа, ряда и периода либо содержит прогноз.')
    page, chart, observation = valid[0]
    basis = item['basis']
    if chart['unit'] == '%':
        agrees = item['amount']['unit'] == 'percent' and item['amount']['currency'] is None and basis is None
    else:
        agrees = (item['amount']['unit'] == 'money' and basis is not None and basis['page'] == page
                  and normalized(basis['unit_label']) == normalized(chart['unit'])
                  and normalized(basis['metric_label']) in {normalized(chart['metric']), normalized(chart['heading']['text'])})
    if not agrees:
        raise tables.Unsupported('unit_mismatch', 'Единица и определение не совпадают с собственной подписью ряда графика.')
    return observation


def derived(pages, layouts, item):
    """Only observations whose title, value and period the qualitative candidate actually quotes."""
    selected = {}
    for locator in item['evidence']:
        selected.setdefault(locator['page'], set()).update(range(locator['line_start'], locator['line_end'] + 1))
    for page, lines in selected.items():
        original = pages[page].splitlines()
        reporting = item['reporting_basis']
        if reporting == 'Не указано':
            sources = [original[n - 1] for n in sorted(lines) if re.match(r'^Источник\s*:', original[n - 1], re.I)]
            if len(sources) != 1:
                continue
            reporting = sources[0]
        reporting_pages = [p for p, numbers in selected.items()
                           if normalized(reporting) in normalized('\n'.join(pages[p].splitlines()[n - 1] for n in sorted(numbers)))]
        if not reporting_pages:
            continue
        for chart in page_charts(original, layouts.get(page) or ''):
            if not cited(chart['heading'], lines):
                continue
            for obs in chart['observations']:
                if not all(cited(obs[key], lines) for key in ('value', 'label')) or obs['forecast']:
                    continue
                raw = obs['value']['text']
                percent = chart['unit'] == '%'
                basis = None if percent else dict(page=page, metric_label=chart['metric'], unit_label=chart['unit'],
                                                  reporting_label=reporting, reporting_page=reporting_pages[0])
                card = dict(metric=chart['metric'], subject=item['subject'], reporting_basis=reporting,
                            nature='reported_actual', period=obs['period'], value_text=raw,
                            amount=dict(value=normalized(raw).removesuffix('%').replace(',', '.').replace('−', '-'),
                                        unit='percent' if percent else 'money', currency=None if percent else 'RUB'),
                            basis=basis, evidence=item['evidence'])
                number = min(set(obs['value']['lines']) & lines)
                yield card, dict(page=page, line_start=number, line_end=number)
