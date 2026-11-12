"""Source-bound normalization of numeric cards and resolved sectors, and the printed place of a cited passage."""
from datetime import date
from calendar import monthrange
from itertools import product
import re

from . import tables

SPACES = r'[   ]'
# A comma or point between digits belongs to the same printed token.
NUMBER_START = r'(?<!\d)(?<!\d[.,])'
NUMBER_END = r'(?!\d|[.,]\d)'
NUMBER = rf'{NUMBER_START}[+\-−]?(?:\d{{1,3}}(?:{SPACES}\d{{3}})+|\d+)(?:[.,]\d+)?{NUMBER_END}'
MONEY = re.compile(rf'(?P<value>{NUMBER})\s*(?P<unit>(?:млн|тыс|млрд|трлн|триллион(?:а|ов)?)\.?\s*(?:₽|руб\.?)(?:\s*/\s*мес\.)?)', re.I)
PERCENT = re.compile(rf'(?P<value>{NUMBER})\s*(?P<unit>%)')
# A signed growth percentage printed beside an amount is not an observation of its own.
MONEY_WITH_GROWTH = re.compile(MONEY.pattern + rf'(?:\s*[+\-−]\s*{NUMBER}\s*%)?', re.I)
DATE = re.compile(r'(?<!\d)(\d{2})\.(\d{2})\.((?:19|20)\d{2})(?!\d)')
YEAR = r'(?:19|20)\d{2}'
PERIOD_LABEL = re.compile(rf'(?P<flow>за\s*)?(?:(?P<quarter>[1-4])\s*кв(?:артал)?\.?\s*)?(?P<year>{YEAR})\s*(?:г\.?|год[а-я]*)?', re.I)
FORECAST = re.compile(r'прогноз|ожидаем|план', re.I)
# References of a qualitative passage do not name a competing subject.
PASSAGE_REFERENCE = re.compile(r'^(?:Источники?|Примечани[ея])\s*:', re.I)
# Bare year or quarter labels name a closing date only for an explicitly named stock metric.
STOCK_METRICS = ('собственныйкапитал',)


def card_date(lines):
    matches = [match for line in lines for match in DATE.finditer(line)]
    dates = set()
    for match in matches:
        try:
            dates.add(date(int(match[3]), int(match[2]), int(match[1])).isoformat())
        except ValueError:
            return None
    return next(iter(dates)) if len(dates) == 1 else None


def source_period(line):
    when=card_date([line])
    if when:
        return dict(kind='instant',label='на '+date.fromisoformat(when).strftime('%d.%m.%Y'),start=when,end=when)
    annual=re.search(r'за\s*((?:19|20)\d{2})\s*год',line,re.I)
    if annual:
        year=annual[1]
        return dict(kind='interval',label='за '+year+' год',start=year+'-01-01',end=year+'-12-31')
    return None


def date_line(line):
    return bool(re.fullmatch(r'\(?на\s*\d{2}\.\d{2}\.\d{4}\)?', line, re.I))


def period_label(line):
    """A card's own period line: an annual flow, a year, a quarter or a forecast label."""
    text = line.strip()
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1].strip()
    if FORECAST.search(text) and re.search(YEAR, text):
        return dict(text=text, forecast=True, flow=False, quarter=None, year=None)
    match = PERIOD_LABEL.fullmatch(text)
    if not match:
        return None
    return dict(text=text, forecast=False, flow=bool(match['flow']), quarter=match['quarter'], year=match['year'])


def marker_line(line):
    return date_line(line) or period_label(line) is not None


def label_line(line):
    """A caption line: no digits, units, footnotes or page attribution."""
    return bool(line) and not re.search(r'\d|[₽%]', line) and not re.fullmatch(r'(?:млн|млрд|тыс|трлн|триллион(?:а|ов)?)\.?', line, re.I) \
        and not line.startswith(('Источник:', '*'))


def instant(when):
    return dict(kind='instant', label='на '+date.fromisoformat(when).strftime('%d.%m.%Y'), start=when, end=when)


def label_period(line, metric):
    """Resolve a period label for the metric; None when the label alone cannot fix the period kind."""
    parsed = period_label(line)
    if parsed is None or parsed['forecast']:
        return None
    year = int(parsed['year'])
    # A quarter label names that calendar quarter: its last month closes the period.
    month = int(parsed['quarter'])*3 if parsed['quarter'] else 12
    end = date(year, month, monthrange(year, month)[1]).isoformat()
    if parsed['flow']:
        start = date(year, month-2 if parsed['quarter'] else 1, 1).isoformat()
        return dict(kind='interval', label=parsed['text'], start=start, end=end)
    if not any(stock in re.sub(r'\s+', '', metric).casefold() for stock in STOCK_METRICS):
        return None
    return dict(kind='instant', label=parsed['text'], start=end, end=end)


def card_period(markers, metric, fallback):
    """The card's own date or label decides; conflicting local markers give nothing.

    The page attribution date applies only to a card without any local marker."""
    dates = [line for line in markers if date_line(line)]
    labels = {period_label(line)['text'] for line in markers if not date_line(line)}
    if (dates and labels) or len(labels) > 1:
        return None
    if dates:
        when = card_date(dates)
        return instant(when) if when else None
    if labels:
        return label_period(labels.pop(), metric)
    return fallback


def occurrences(rows, phrase):
    pattern = r'\s*'.join(re.escape(char) for char in re.sub(r'\s+', '', phrase))
    return [(i, match.start(), match.end()) for i, row in enumerate(rows)
            for match in re.finditer(pattern, row, re.I)]


def monthly_card(layout, printed, labels):
    """A monthly rate, its label and calendar period in one text-layout column."""
    rows=layout.splitlines()
    pattern=r'\s*'.join(re.escape(char) for char in re.sub(r'\s+','',printed))
    positions=[(i,m.start(),m.end()) for i,row in enumerate(rows) for m in re.finditer(pattern,row,re.I)]
    if len(positions)!=1:return None
    row,left,_=positions[0]
    if row+2>=len(rows) or forecast_above(rows,left,row):return None
    matched_labels=set()
    for label in labels:
        if not label or re.search(r'\d|[₽%]|прогноз|ожидаем|план',label,re.I):continue
        pattern=r'\s*'.join(re.escape(char) for char in re.sub(r'\s+','',label))
        if any(abs(match.start()-left)<=3 for match in re.finditer(pattern,rows[row+1],re.I)):
            matched_labels.add(label)
    periods=[match[0] for match in re.finditer(r'(?<!\w)(?:[1-4]\s*кв\.?\s*(?:19|20)\d{2}|(?:19|20)\d{2})(?!\d)',rows[row+2])
             if abs(match.start()-left)<=3]
    if len(matched_labels)!=1 or len(periods)!=1:return None
    label=matched_labels.pop()
    period_text=periods[0]
    quarter=re.fullmatch(r'([1-4])\s*кв\.?\s*((?:19|20)\d{2})',period_text)
    annual=re.fullmatch(r'(?:19|20)\d{2}',period_text)
    if quarter:
        year=int(quarter[2]);month=int(quarter[1])*3
        period=dict(kind='interval',label=period_text,start=date(year,month-2,1).isoformat(),end=date(year,month,monthrange(year,month)[1]).isoformat())
    elif annual:
        period=dict(kind='interval',label=period_text,start=period_text+'-01-01',end=period_text+'-12-31')
    else:return None
    return label,period


def aligned_card(layout, metric_lines, amount_line):
    """The top row and column of a card whose caption lines align with its amount; None when ambiguous.

    Plain extraction order alone cannot associate two neighboring cards."""
    rows = layout.splitlines()
    amounts = occurrences(rows, amount_line)
    if len(amounts) != 1:
        return None
    y, x, _ = amounts[0]
    candidates = []
    for phrase in metric_lines:
        matches = [(row, column) for row, column, _ in occurrences(rows, phrase)
                   if abs(column-x) <= 3 and abs(row-y) <= 6]
        if not matches:
            return None
        candidates.append(matches)
    sequences = [positions for positions in product(*candidates)
                 if all(0 < b[0]-a[0] <= 2 for a,b in zip(positions,positions[1:]))
                 and (all(row < y for row,_ in positions) or all(row > y for row,_ in positions))]
    if len(sequences) != 1:
        return None
    # Another amount in this column between caption and value makes it ambiguous.
    # The card's own period line carries its year, so placed rows are not foreign numbers.
    used = {y, *(row for row, _ in sequences[0])}
    lo, hi = min(used), max(used)
    if any(i not in used and abs(match.start()-x) <= 3
           for i in range(lo, hi+1) for match in re.finditer(NUMBER,rows[i])):
        return None
    return lo, x


def card_stack(lines, number, direction):
    """Contiguous caption, date and period lines on one side of an amount, in text order."""
    labels, markers = [], []
    for offset in range(1, 5):
        prior = number + direction*offset
        if not 1 <= prior <= len(lines):
            break
        line = lines[prior-1].strip()
        if marker_line(line):
            markers.append(prior)
        elif label_line(line):
            labels.append(prior)
        else:
            break
    return sorted(labels), sorted(markers)


def single_card(lines, layout, selected, number, match, header_period):
    """A card whose caption and own date or period stand on one side of the amount."""
    rows = layout.splitlines()
    for direction in (-1, 1):
        labels, markers = card_stack(lines, number, direction)
        if not labels:
            continue
        opposite = number - direction
        if 1 <= opposite <= len(lines) and marker_line(lines[opposite-1].strip()):
            # A period printed on the other side belongs to a caption series or is ambiguous.
            continue
        if any(min(labels) < marker < max(labels) for marker in markers):
            continue
        cited = sorted(labels + markers)
        if not set(cited).issubset(selected):
            continue
        placed = aligned_card(layout, [lines[n-1].strip() for n in cited], lines[number-1])
        if placed is None:
            continue
        label_lines = [lines[n-1].strip() for n in labels]
        marker_lines = [lines[n-1].strip() for n in markers]
        if any(FORECAST.search(line) for line in label_lines + marker_lines) or forecast_above(rows, placed[1], placed[0]):
            continue
        period = card_period(marker_lines, ' '.join(label_lines), header_period)
        if period is None:
            continue
        return match, label_lines, [(min(number, *cited), max(number, *cited), 'Числовой показатель источника')], period, (number, number)
    return None


def clear_between(rows, left, right, top, bottom):
    """No other text inside the column strictly between two rows of the series."""
    return all(not rows[i][max(0, left-3):right+3].strip() for i in range(top+1, bottom))


def forecast_line(line):
    parsed = period_label(line)
    return bool(parsed and parsed['forecast'])


def column_text(row, left):
    """The layout cell occupying this column of a row; '' when the column is empty there."""
    for cell in re.finditer(r'\S+(?:\s\S+)*', row):
        if cell.start() <= left+3 and cell.end() > left-3:
            return cell[0]
    return ''


def forecast_above(rows, left, top):
    """A forecast heading over this column of the full page layout.

    The scan does not depend on the lines the model cited: it climbs the contiguous
    caption block above the card, across the short gap allowed before a caption,
    and stops at any other text or a wider gap. A heading over another column
    never qualifies this one."""
    gap = 0
    for i in range(top-1, -1, -1):
        text = column_text(rows[i], left)
        if not text:
            gap += 1
            if gap > 2:
                return False
            continue
        if FORECAST.search(text):
            return True
        if gap or not label_line(text):
            return False
    return False


def column_order(pairs):
    """Column-major order under the membership tolerance: a one-space shift must not reorder a column."""
    ordered = []
    anchor = None
    for pair in sorted(pairs, key=lambda pair: (pair[1], pair[0])):
        if anchor is None or pair[1]-anchor > 3:
            anchor = pair[1]
        ordered.append((anchor, pair[0], pair))
    return [pair for _, _, pair in sorted(ordered, key=lambda entry: entry[:2])]


def caption_above(lines, rows, column_rows, left, right, row):
    """Contiguous caption rows directly above the first amount of a series.

    A forecast label printed over the caption qualifies the whole series, so it
    joins the heading even across the short gap allowed before the caption."""
    heading, bottom = [], None
    expected = row - 1
    while True:
        found = max((r for r in column_rows if r <= expected), default=None)
        if found is None:
            break
        line = lines[column_rows[found]-1].strip()
        forecast = forecast_line(line)
        if expected - found > (2 if not heading or forecast else 0):
            break
        if not (label_line(line) or forecast):
            break
        heading.insert(0, column_rows[found])
        bottom = bottom if bottom is not None else found
        expected = found - 1
    if not heading or not clear_between(rows, left, right, bottom, row):
        return None
    return heading


def series_cards(lines, layout, selected):
    """One caption over a column of amounts, each followed by its own period or date line."""
    rows = layout.splitlines()
    located = {}
    for number in sorted(selected):
        if not 1 <= number <= len(lines) or not lines[number-1].strip():
            continue
        positions = occurrences(rows, lines[number-1].strip())
        if len(positions) == 1:
            located[number] = positions[0]
    pairs = []
    for number, (row, left, right) in located.items():
        amount = MONEY_WITH_GROWTH.fullmatch(lines[number-1].strip())
        if not amount or '/' in amount['unit']:
            continue
        below = [other for other, (r, c, _) in located.items() if r == row+1 and abs(c-left) <= 3]
        if len(below) != 1 or not marker_line(lines[below[0]-1].strip()):
            continue
        pairs.append((row, left, right, number, below[0], amount))
    observations = []
    current = None
    # Column-major order keeps each caption with its own column of amounts.
    for row, left, right, number, marker, amount in column_order(pairs):
        column_rows = {r: other for other, (r, c, _) in located.items() if abs(c-left) <= 3}
        nearest = max((r for r in column_rows if r < row), default=None)
        if current and abs(left-current[2]) <= 3 and nearest == current[1] and clear_between(rows, left, right, nearest, row):
            heading = current[0]
        else:
            heading = caption_above(lines, rows, column_rows, left, right, row)
        if heading is None:
            current = None
            continue
        current = (heading, row + 1, left)
        caption = [lines[n-1].strip() for n in heading]
        label = lines[marker-1].strip()
        top = min(r for r, n in column_rows.items() if n in heading)
        if any(FORECAST.search(line) for line in caption + [label]) or forecast_above(rows, left, top):
            continue
        period = card_period([label], ' '.join(caption), None)
        if period is None:
            continue
        observations.append((amount, caption, [(number, number, 'Числовой показатель источника'),
                                               (marker, marker, 'Подпись периода показателя')], period, (number, number)))
    return observations


def _numeric_cards(pages, layouts, item, diagrams=None):
    """Enrich cited qualitative passages; never scan unrelated pages for digits.

    Each card is paired with the lines where its amount is printed."""
    cards = []
    cited = {}
    for locator in item['evidence']:
        cited.setdefault(locator['page'], set()).update(range(locator['line_start'], locator['line_end']+1))
    for page, selected in cited.items():
        lines = pages.get(page, '').splitlines()
        headers = [(i, line) for i, line in enumerate(lines, 1)
                   if re.match(r'^Источник\s*:', line, re.I) and source_period(line)]
        if len(headers) != 1:
            continue
        header_line, header = headers[0]
        header_period = source_period(header)
        layout = layouts.get(page, '')
        observations = []
        for number in sorted(selected):
            if not 1 <= number <= len(lines):
                continue
            match = MONEY.fullmatch(lines[number-1].strip()) or PERCENT.fullmatch(lines[number-1].strip())
            if not match:
                continue
            if '/' in match['unit']:
                monthly=monthly_card(layout,lines[number-1].strip(),
                    [lines[n-1].strip() for n in selected if 1<=n<=len(lines)])
                if monthly:
                    label,period=monthly
                    # Both original quotations must already be in the selected passage.
                    anchors=[n for n in selected if 1<=n<=len(lines)
                             and re.sub(r'\s+','',label).casefold() in re.sub(r'\s+','',lines[n-1]).casefold()]
                    dates=[n for n in selected if 1<=n<=len(lines)
                           and re.sub(r'[\s.]','',period['label']) in re.sub(r'[\s.]','',lines[n-1])]
                    if anchors and dates:
                        start=min(number,min(anchors),min(dates));end=max(number,min(anchors),min(dates))
                        observations.append((match,[label],[(start,end,'Числовой показатель источника')],period,(number,number)))
                continue
            observation = single_card(lines, layout, selected, number, match, header_period)
            if observation:
                observations.append(observation)
        observations.extend(series_cards(lines, layout, selected))
        # Only existing geometric metadata may bind a sector to its printed label.
        for region in (diagrams or {}).get(page, []):
            labels = region.get('label_lines', [])
            content = region.get('content_lines', [])
            if not labels or not content or not set(content).issubset(selected):
                continue
            if any(not 1 <= n <= len(lines) for n in labels+content):
                continue
            text = ' '.join(lines[n-1] for n in content)
            match = MONEY.fullmatch(text.strip())
            if match:
                observations.append((match, [lines[n-1] for n in labels],
                                     [(min(content), max(content), 'Числовой показатель источника')], header_period,
                                     (min(content), max(content))))
        for match, labels, ranges, period, (first, last) in observations:
            metric = ' '.join(labels)
            evidence = [*item['evidence'],
                        *(dict(page=page, line_start=start, line_end=end, area=area) for start, end, area in ranges),
                        dict(page=page, line_start=header_line, line_end=header_line, area='Дата и атрибуция страницы')]
            for label in labels:
                positions = [i for i, line in enumerate(lines, 1) if line == label]
                if len(positions) == 1:
                    evidence.append(dict(page=page, line_start=positions[0], line_end=positions[0], area='Подпись показателя'))
            card = dict(metric=metric, subject=item['subject'], reporting_basis=header,
                nature='reported_actual', period=period,
                amount=dict(value=re.sub(SPACES, '', match['value']).replace(',', '.').replace('−','-'), unit='percent' if match['unit']=='%' else 'money', currency=None if match['unit']=='%' else 'RUB'),
                value_text=match['value'], evidence=evidence,
                basis=None if match['unit']=='%' else dict(page=page, metric_label=metric, unit_label=match['unit'], reporting_label=header))
            cards.append((card, dict(page=page, line_start=first, line_end=last)))
    return cards


def observation_key(card):
    """A literal caption and confirmed period, without a model-supplied location label."""
    period = card['period']
    return (re.sub(r'\s+', '', card['metric']).casefold().replace('’', "'").replace('м', 'm'),
            period['kind'], period['start'], period['end'])


def amount_key(card):
    amount = card['amount']
    return amount['value'], amount['unit'], amount['currency']


def confirmed_period(period):
    return period['kind'], period['start'], period['end']


def named_period(label, metric):
    """The period a supported label names; None for an unsupported form or an impossible date.

    The supported forms are the markers cards carry: a date with or without 'на', its ISO
    spelling, and the year, quarter and annual-flow labels of period_label, where the
    caption decides whether a bare year or quarter closes a period."""
    text = label.strip()
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1].strip()
    if re.fullmatch(rf'(?:на\s*)?{DATE.pattern}', text, re.I):
        when = card_date([text])
        return instant(when) if when else None
    if re.fullmatch(rf'{YEAR}-\d{{2}}-\d{{2}}', text):
        try:
            return instant(date.fromisoformat(text).isoformat())
        except ValueError:
            return None
    return label_period(text, metric)


def label_agrees(label, card):
    """The model label is the card's own marker or names its confirmed period by a supported rule.

    A shared year or end month proves nothing: another calendar date, or an annual flow
    instead of a balance, contradicts the card. The card's caption, not the model's,
    decides whether a bare year or quarter closes a period."""
    own = card['period']['label']
    if ' '.join(label.split()) == ' '.join(own.split()):
        return True
    marker, given = period_label(own), period_label(label)
    if marker and given and not given['forecast'] and all(marker[key] == given[key] for key in ('flow', 'quarter', 'year')):
        # The card's own year or quarter marker with another suffix or spacing.
        return True
    period = named_period(label, card['metric'])
    return period is not None and confirmed_period(period) == confirmed_period(card['period'])


def inspect_cards(pages, layouts, item, diagrams=None):
    """Return cited cards and observations from their complete source pages.

    Every card comes with the lines where its amount is printed. A caption may
    legitimately repeat across periods or inside distinct full local headings. An
    amount's alignment with a shared caption alone does not distinguish two
    observations with the same caption and period.
    """
    cards = _numeric_cards(pages, layouts, item, diagrams)
    page_cards = []
    for page in sorted({e['page'] for e in item['evidence']}):
        full_page = dict(item, evidence=[
            dict(page=page, line_start=1, line_end=len(pages.get(page, '').splitlines()), area='Страница')])
        page_cards.extend(_numeric_cards(pages, layouts, full_page, diagrams))
    return cards, page_cards


def ambiguous_card(card, page, page_cards):
    """Explicit admission result: this page prints another amount under the card's caption and period.

    Validation errors are handled by extraction."""
    amounts = {amount_key(other) for other, printed in page_cards
               if printed['page'] == page and observation_key(other) == observation_key(card)}
    return len(amounts) > 1


def source_observations(number, cited, page_cards):
    """Full-page cards whose printed amount is this number on a line the candidate quotes.

    The verified number and its quoted place identify the observation. The caption the
    model supplies cannot: a shortened name must not escape the page's competitors, and
    a shared word must not merge different captions."""
    printed_number = re.sub(r'\s+', '', number)
    return [(card, printed) for card, printed in page_cards
            if re.sub(r'\s+', '', card['value_text']) == printed_number
            and any((printed['page'], line) in cited for line in range(printed['line_start'], printed['line_end']+1))]


def placed_cards(pages, layouts, item, diagrams=None):
    """Admissible cards of a cited passage, each with the lines where its amount is printed."""
    cards, page_cards = inspect_cards(pages, layouts, item, diagrams)
    return [(card, printed) for card, printed in cards if not ambiguous_card(card, printed['page'], page_cards)]


def numeric_cards(pages, layouts, item, diagrams=None):
    return [card for card, _ in placed_cards(pages, layouts, item, diagrams)]


def tied(rows, a, b):
    """Two layout cells printed together: next to each other on one row, or in one column (their extents overlap
    within TOLERANCE) on neighboring rows or one row apart, the blank row inside a block. Cells of one row with
    another cell between them, or of neighboring rows in different columns, are not tied: one horizontal does
    not make two columns one card."""
    (row_a, i), (row_b, j) = a, b
    if row_a == row_b:
        return abs(i - j) == 1
    return abs(row_a - row_b) <= 2 and tables.overlapping(rows[row_a][i], rows[row_b][j])


def block(rows, seeds, within):
    """The cells of within reachable from the seeds through tied cells."""
    found, queue = set(seeds), list(seeds)
    while queue:
        cell = queue.pop()
        for other in within - found:
            if tied(rows, cell, other):
                found.add(other)
                queue.append(other)
    return found


def passage_conflict(lines, rows, claim, printings):
    """Established local conflicts only: an omitted bound or a year in another captioned card.

    Unplaced text and ambiguous geometry without a competing statement remain unchecked.
    This inspects existing layout cells; it never invents or extends a source quotation.
    """
    covered = {cell for n in claim for printing in printings[n] for cell in printing}
    for number in claim:
        text = lines[number - 1].strip()
        places = printings[number]
        if not places or not re.fullmatch(NUMBER, text):
            continue
        # A duration's unit below its value identifies which of repeated numbers is relevant.
        durations = []
        for printing in places:
            value = next(iter(printing)) if len(printing) == 1 else None
            if value is None:
                continue
            r, i = value
            units = {cell for cell in covered if cell[0] == r + 1
                     and tables.overlapping(rows[r][i], rows[cell[0]][cell[1]])
                     and re.fullmatch(r'дн(?:ей|я)|сут(?:ок|ки)|минут(?:а|ы)?', rows[cell[0]][cell[1]][2], re.I)}
            if not units:
                continue
            bounds = {(row, col) for row in range(max(0, r - 2), r)
                      for col, heading in enumerate(rows[row])
                      if tables.overlapping(rows[r][i], heading)
                      and re.search(r'\b(?:до|от|не более|не менее)\s*$', heading[2], re.I)}
            durations.append(bool(bounds) and not bool(bounds & covered))
        if durations and all(durations):
            return True
        if not re.fullmatch(YEAR, text):
            continue
        foreign = []
        for printing in places:
            own = block(rows, printing, covered)
            has_statement = any(n != number and not re.fullmatch(YEAR, lines[n - 1].strip())
                                and any(p & own for p in printings[n]) for n in claim)
            # An uncited caption directly below the year establishes its other statement.
            caption = any((r + 1, j) not in covered and tables.overlapping(rows[r][i], cell)
                          and re.search(r'[A-Za-zА-Яа-я]', cell[2])
                          and not PASSAGE_REFERENCE.match(cell[2])
                          for r, i in printing if r + 1 < len(rows)
                          for j, cell in enumerate(rows[r + 1]))
            foreign.append(not has_statement and caption)
        if len(claim) > 1 and foreign and all(foreign):
            return True
    return False


def quoted_durations(text, layout, quote):
    """Bounds printed with durations inside one complete literal quote, never across quotations.

    PDF object order can put a caption after its value. The existing layout places the
    caption, value and unit; this returns their relation without constructing a new quote.
    """
    lines = text.splitlines()
    quoted = [line.strip() for line in quote.splitlines()]
    starts = [start for start in range(len(lines) - len(quoted) + 1)
              if [line.strip() for line in lines[start:start + len(quoted)]] == quoted]
    if len(starts) != 1:
        return []
    claim = range(starts[0], starts[0] + len(quoted))
    rows = tables.layout_rows(layout or '')
    runs = tables.spellings(rows)
    covered = {(r, i) for n in claim for r, first, last in runs.get(tables.squeeze(lines[n]), [])
               for i in range(first, last + 1)}
    observations = {}
    for r, i in covered:
        value = rows[r][i][2]
        if not re.fullmatch(NUMBER, value):
            continue
        for row, col in covered:
            unit = rows[row][col][2]
            if row != r + 1 or not tables.overlapping(rows[r][i], rows[row][col]):
                continue
            if not re.fullmatch(r'дн(?:ей|я)|сут(?:ок|ки)|минут(?:а|ы)?', unit, re.I):
                continue
            bounds = {match[1].lower() for head, cell in covered if 0 < r - head <= 2
                      and tables.overlapping(rows[r][i], rows[head][cell])
                      if (match := re.search(r'\b(не более|не менее|до|от)\s*$', rows[head][cell][2], re.I))}
            observations.setdefault((value, unit), []).append(bounds)
    return [(value, unit, next(iter(found[0]))) for (value, unit), found in observations.items()
            if found and len(found[0]) == 1 and all(bounds == found[0] for bounds in found)]


def capacity_numbers(text, pages):
    """Мощность площадки из её собственной колонки PDF, а не порядка строк извлечения."""
    heading = re.match(r'Производственные мощности:\s*', text, re.I)
    if not heading:
        return text
    item = re.compile(r'\s*(?P<plant>[^:;\n]+):\s*(?P<amount>\d+(?:[.,]\d+)?\s+(?:тыс|млн|млрд))'
                      r'\s+(?P<unit>упаковок|таблеток)\s+(?P<rate>в год|в месяц)\s*', re.I)
    mask = set()
    offset = heading.end()
    for part in text[heading.end():].rstrip('.').split(';'):
        match = item.fullmatch(part)
        if match:
            for page in pages:
                quote = page['quote']
                lines = [line.strip() for line in quote.splitlines() if line.strip()]
                if not any(re.fullmatch(r'Производственные мощности', line, re.I) for line in lines):
                    continue
                # Подпись может занимать несколько строк, но они должны принадлежать одной колонке.
                captions = [lines[i:j] for i in range(len(lines)) for j in range(i+1, min(i+4, len(lines)+1))
                            if tables.squeeze(''.join(lines[i:j])).casefold() == tables.squeeze(match['plant']).casefold()]
                amounts = [line for line in lines
                           if tables.squeeze(line).replace(',', '.') == tables.squeeze(match['amount']).replace(',', '.')]
                rows = page['layout'].splitlines()
                for amount in amounts:
                    placed = occurrences(rows, amount)
                    if len(placed) != 1:
                        continue
                    y, x, _ = placed[0]
                    if y+2 >= len(rows) or forecast_above(rows, x, y):
                        continue
                    if tables.squeeze(column_text(rows[y], x)) != tables.squeeze(amount):
                        continue
                    if (column_text(rows[y+1], x).casefold() != match['unit'].casefold()
                            or column_text(rows[y+2], x).casefold() != match['rate'].casefold()
                            or match['unit'].casefold() not in [line.casefold() for line in lines]
                            or match['rate'].casefold() not in [line.casefold() for line in lines]):
                        continue
                    if any(aligned_card(page['layout'], caption, amount) is not None for caption in captions):
                        mask.update(offset+i for i in range(*match.span('amount')) if part[i].isdigit())
        offset += len(part)+1
    return ''.join(' ' if i in mask else ch for i, ch in enumerate(text))


def label_bound(lines, layout, label, claim, regions=(), *, disjoint=False):
    """Whether the lines a qualitative passage claims with its label are printed in the label's block.

    label and claim are original line numbers: the label line and the lines cited in one run of text with it. The
    text order of a PDF may list the lines of a neighboring card right after the name of another, and one row of
    the page may hold two columns of cards: words that follow each other in the text are not therefore printed
    together, and one neighboring line does not vouch for the others. The label's block is every claimed cell
    reachable from a printing of the label through tied cells (tied), over the claimed cells only; a diagram region
    holding all claimed lines binds the passage as well.

    True when every claimed line printed whole has a printing in the label's block. False when the label, printed
    once, has its own column left uncited while a claimed cell on its row heads a column of claimed cells (two cards
    side by side, the other card taken whole), or when a claimed line is printed in a block with a heading of its own: a block outside the label's that holds a claimed cell aligned with
    the label, in its column (start within TOLERANCE) or on its row, beside cells of other columns, or that is
    headed by an uncited cell so aligned, printed on the row of a block cell or above it; the same when a diagram
    region holds a claimed line under label lines the passage leaves out. None when the layout establishes neither:
    the label is printed nowhere whole, no other claimed line is, or the unbound lines stand apart with no heading
    of their own (a title over a paragraph, a chart spread over its image). A claimed peer heading competes only
    when the label's own neighboring text was omitted. Source attributions, notes and fragments of unplaced cited
    lines do not establish a competing heading. For disjoint references, attribution needs an omitted property
    below the label in its own column; a margin marker is not such a property. An uncited competing heading must
    start its source column block, rather than continue an omitted paragraph. Position does not establish meaning."""
    # A page attribution or note is a reference, not a competing subject heading. Keep its automatic fact label;
    # its position alone cannot attribute the cited chart annotation to another subject.
    if PASSAGE_REFERENCE.match(lines[label - 1].strip()):
        return None
    claim = sorted({n for n in claim if not PASSAGE_REFERENCE.match(lines[n - 1].strip())} | {label})
    rows = tables.layout_rows(layout)
    runs = tables.spellings(rows)
    printings = {n: [{(r, i) for i in range(first, last + 1)} for r, first, last in runs.get(tables.squeeze(lines[n - 1]), [])]
                 if lines[n - 1].strip() else [] for n in claim}
    if passage_conflict(lines, rows, claim, printings):
        return False
    members = [set(region.get('label_lines', [])) | set(region.get('content_lines', [])) for region in regions]
    if any(set(claim) <= group for group in members):
        return True
    if any(label not in group and group & set(claim) and set(region.get('label_lines', [])) - set(claim)
           for region, group in zip(regions, members)):
        return False
    if not printings[label] or not any(printings[n] for n in claim if n != label):
        return None
    covered = {cell for n in claim for printing in printings[n] for cell in printing}
    label_cells = set().union(*printings[label])
    own = block(rows, label_cells, covered)
    unbound = [n for n in claim if n != label and printings[n] and not any(printing & own for printing in printings[n])]
    uncited = {(r, i) for r, cells in enumerate(rows) for i in range(len(cells))} - covered
    # A cell may belong to a cited line the layout cannot place whole, for example an amount with a raised
    # footnote. Such fragments are not evidence of an omitted heading.
    unplaced = [tables.squeeze(lines[n - 1]) for n in claim if not printings[n]]
    uncited_headings = {cell for cell in uncited
                       if not PASSAGE_REFERENCE.match(rows[cell[0]][cell[1]][2])
                       and not any(tables.squeeze(rows[cell[0]][cell[1]][2]) in line for line in unplaced)}

    def under(cell, pool):
        return any(other[0] == cell[0] + 1 and tables.overlapping(rows[cell[0]][cell[1]], rows[other[0]][other[1]]) for other in pool)
    if len(printings[label]) == 1:
        # Two cards side by side, their names on one row: the label printed once with its own column left uncited
        # while a claimed cell on its row heads a column of claimed cells names the other card.
        (row, first), (_, last) = min(label_cells), max(label_cells)
        if any(under(cell, uncited) for cell in label_cells) and any(cell[0] == row and not first <= cell[1] <= last and under(cell, own) for cell in own):
            return False
    if not unbound:
        return True
    label_rows = {r for r, _ in label_cells}
    label_starts = {rows[r][i][0] for r, i in label_cells if (r, i - 1) not in label_cells}

    def aligned(cell):
        r, i = cell
        return any(abs(rows[r][i][0] - start) <= tables.TOLERANCE for start in label_starts)

    def peer(cell):
        return cell[0] in label_rows or aligned(cell)
    if disjoint:
        # The full page distinguishes the continuation of a paragraph from a separate card's heading.
        # Reuse the existing cell ties, without filling gaps in the evidence or guessing from its first line.
        omitted_column = any(cell[0] > own_label[0] and tied(rows, cell, own_label)
                             for cell in uncited_headings for own_label in label_cells)
        if not omitted_column:
            return None
        uncited_headings = {cell for cell in uncited_headings
                           if not any(other[0] < cell[0] and tied(rows, cell, other)
                                      for other in covered | uncited)}
    remaining = covered - own
    while remaining:
        group = block(rows, {next(iter(remaining))}, remaining)
        remaining -= group
        if not any(printing & group for n in unbound for printing in printings[n]):
            continue
        # The block holds a claimed line aligned with the label beside the cells of other claimed lines in other
        # columns (a rank cited with the caption of the other rank), or is headed by an uncited cell so aligned (the
        # name of the card whose lines were taken). One claimed line printed twice side by side is no such block.
        heads = {n for n in claim if any(printing & group and any(peer(cell) for cell in printing) for printing in printings[n])}
        # A title over a complete paragraph may also cover several indicator cards further down. Their alignment
        # does not establish competing subjects unless the title's own neighboring text has been left out.
        omitted_own = any(tied(rows, cell, own_label) for cell in uncited_headings for own_label in label_cells)
        held = omitted_own and heads and any(not aligned(cell) for n in claim if n not in heads for printing in printings[n] for cell in printing & group)
        # An uncited heading does not choose between identical labels printed in different blocks. The claimed
        # peer-heading case above still detects competing ranks; an ambiguous label placement alone cannot.
        headed = len(printings[label]) == 1 and any(
            peer(cell) and any(tied(rows, cell, member) and cell[0] <= member[0] for member in group)
            for cell in uncited_headings)
        if held or headed:
            return False
    return None
