"""Table geometry of one saved page.

The text layout pypdf saves beside the plain text places every printed cell of a page on a fixed-pitch grid;
two or more spaces separate the cells of a row. The layout is a projection of the saved PDF, never a
quotation: it binds a printed amount to the period column above it, the column to the caption centered
over its group and the table to the unit printed in its own heading, while the quotations stay the original
lines the model names. The tolerances absorb the rounding of the grid only; an alignment the grid cannot
resolve is reported as a limitation, never guessed.
"""
from calendar import monthrange
from datetime import date
from itertools import combinations
import re

CELL = re.compile(r'\S+(?:\s\S+)*')
# One year or one explicit quarter in a header cell; a cell naming several years (2019-2023) is no column.
PERIOD_TOKEN = re.compile(r'(?<![\w])(?:[1-4]Q(?:\d{4}|\d{2})|(?:19|20)\d{2})(?!\d)')
YEAR_SUFFIX = r'(?:\s*(?:г\.?|года?))?'
YEAR_LABEL = re.compile(r'((?:19|20)\d{2})' + YEAR_SUFFIX, re.I)
MONTHS = {name: number for number, name in enumerate((
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'), 1)}
DATE_LABEL = re.compile(r'(?:на\s+)?(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+((?:19|20)\d{2})' + YEAR_SUFFIX, re.I)
DATE_PREFIX = re.compile(r'(?:на\s+)?\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')', re.I)
# A period caption of the kind the server verifies, wherever it is printed: 'За 6 месяцев', 'За 9 месяцев,
# закончившихся 30 сентября'. extraction builds the full pattern with the closing date (HEADER_PERIOD) on it.
CAPTION_TOKEN = re.compile(r'За\s+(3|6|9|12)\s+месяц(?:а|ев)', re.I)
# The continuation of that caption ('месяцев, закончившихся 30 сентября') where a block of cells begins with it: the
# part the grid has not bound to its 'За 9', printed apart from it or above the heading window.
CAPTION_CONTINUATION = re.compile(r'месяц(?:а|ев)(?!\w)', re.I)
# A printed amount: digits with group spaces, a decimal part, a sign or parentheses, nothing spelled in letters.
AMOUNT = re.compile(r'\(?[+\-−]?\d[\d\s]*(?:[.,]\d+)?\)?')
TOLERANCE = 4       # layout characters: the rounding of the grid
MIN_PITCH = 8       # adjacent column centers closer than this cannot separate their amounts
MAX_COLUMNS = 12
CAPTION_ROWS = 4    # rows above a header row that may carry its group captions
LONGEST_LINE = 400  # an original line longer than this is prose, not a table row


class Unsupported(Exception):
    """A limitation of the layout for one candidate; the code is a classified reason of ingest."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def squeeze(text):
    return re.sub(r'\s+', '', text)


def center(cell):
    return (cell[0] + cell[1]) / 2


def layout_rows(layout):
    """The cells of every layout row as (start, end, text)."""
    return [[(m.start(), m.end(), m[0]) for m in CELL.finditer(row)] for row in layout.splitlines()]


def spellings(rows):
    """Every run of adjacent cells of a row, keyed by its text without spaces: the places where an original line of
    that spelling may be printed, one (row, first cell, last cell) per printing."""
    runs = {}
    for r, cells in enumerate(rows):
        for first in range(len(cells)):
            text = ''
            for last in range(first, len(cells)):
                text += squeeze(cells[last][2])
                if len(text) > LONGEST_LINE:
                    break
                runs.setdefault(text, []).append((r, first, last))
    return runs


def placements(original, rows):
    """Where each original line is printed: the one row and run of adjacent cells spelling exactly that line.

    A line printed nowhere (a rotated page number), split across rows (a wrapped label) or spelled by several
    runs (a repeated word) has no placement and cannot locate a table cell."""
    runs = spellings(rows)
    placed = {}
    for number, line in enumerate(original, 1):
        found = runs.get(squeeze(line), []) if line.strip() else []
        placed[number] = found[0] if len(found) == 1 else None
    return placed


def cell_lines(placed, rows):
    """The inverse of the placements: the original line printed at each layout cell (row, cell index), and the
    rows one original line prints whole, the only rows the extractor may name as table rows."""
    line_at, whole_row = {}, {}
    for number, place in placed.items():
        if place is None:
            continue
        r, first, last = place
        line_at.update({(r, i): number for i in range(first, last + 1)})
        if (first, last) == (0, len(rows[r]) - 1):
            whole_row[r] = number
    return line_at, whole_row


def header_columns(cells):
    """The period columns of a header row: 2 to 12 cells carrying one year or quarter each, clearly apart.

    A notes or growth column carries no period token and is never a column; None when the row is no header."""
    columns = []
    for i, cell in enumerate(cells):
        if len(PERIOD_TOKEN.findall(cell[2])) != 1:
            continue
        first, last = i, i
        # Двойной пробел PDF может разделить день с месяцем, год и его суффикс.
        # Сохраняем всю соседнюю подпись, даже если ее дата невозможна.
        if i and DATE_PREFIX.fullmatch(cells[i - 1][2]):
            first = i - 1
            if first and cells[first - 1][2].lower() == 'на':
                first -= 1
        if i + 1 < len(cells) and re.fullmatch(r'г\.?|года?', cells[i + 1][2], re.I):
            last = i + 1
        columns.append((first, (cells[first][0], cells[last][1], ' '.join(part[2] for part in cells[first:last + 1]))))
    if not 2 <= len(columns) <= MAX_COLUMNS:
        return None
    centers = [center(cell) for _, cell in columns]
    if any(b - a < MIN_PITCH for a, b in zip(centers, centers[1:])):
        return None
    return columns


def column_period(label):
    """Границы целой подписи столбца без группового интервала; у отдельной даты они равны.

    Геометрия сохраняет и неподдержанные столбцы, чтобы не сдвигать номера соседних.
    Прогнозные маркеры и противоречащие годы не отбрасываются ради распознавания."""
    label = ' '.join(label.split())
    day = DATE_LABEL.fullmatch(label)
    year = YEAR_LABEL.fullmatch(label)
    quarter = re.fullmatch(r'([1-4])Q(\d{4}|\d{2})', label)
    try:
        if day:
            value = date(int(day[3]), MONTHS[day[2].lower()], int(day[1]))
            return value, value
        if year:
            return date(int(year[1]), 1, 1), date(int(year[1]), 12, 31)
        if quarter:
            year_number = int(quarter[2])
            if year_number < 100:
                year_number += 2000
            month = int(quarter[1]) * 3
            return date(year_number, month - 2, 1), date(year_number, month, monthrange(year_number, month)[1])
    except ValueError:
        return None
    return None


def bands(columns):
    """The horizontal band of each column: halfway to its neighbors, half a pitch beyond the outer columns."""
    centers = [center(cell) for _, cell in columns]
    edges = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    left = centers[0] - (centers[1] - centers[0]) / 2
    right = centers[-1] + (centers[-1] - centers[-2]) / 2
    return list(zip([left, *edges], [*edges, right]))


def split_row(cells, column_bands):
    """The label cells of a table row and the band of each amount cell.

    A label ends before the first band. An amount cell belongs to the band holding its center when it lies
    inside that band; a cell outside every band or straddling one (a growth column, a wide note) has none."""
    labels, amounts = [], []
    for cell in cells:
        if cell[1] <= column_bands[0][0]:
            labels.append(cell)
            continue
        band = next((i for i, (left, right) in enumerate(column_bands) if left <= center(cell) < right), None)
        if band is not None and not (column_bands[band][0] <= cell[0] and cell[1] <= column_bands[band][1]):
            band = None
        amounts.append((cell, band))
    return labels, amounts


def detached_value(labels):
    # Число перед первой полосой столбцов может быть непривязанным значением ряда.
    # Не сдвигаем последующие ячейки влево и не выдаем его за часть названия строки.
    return any(AMOUNT.fullmatch(cell[2].removesuffix('%').rstrip()) for cell in labels)


def foreign_row(cells, column_bands):
    """A row of another table printed above this header: a period header of its own, or amounts standing under
    these columns. Neither a caption nor a unit of this table can stand beyond such a row."""
    return bool(header_columns(cells)) or any(band is not None and AMOUNT.fullmatch(cell[2])
                                               for cell, band in split_row(cells, column_bands)[1])


def centered_partition(cells, columns):
    """The one split of the columns into consecutive runs, one per caption cell, each cell centered over its run.

    None when no split fits the tolerance or several do: the captions then do not assign the columns."""
    count = len(columns)
    fits = []
    for cuts in combinations(range(1, count), len(cells) - 1):
        bounds = [0, *cuts, count]
        runs = [range(a, b) for a, b in zip(bounds, bounds[1:])]
        if all(abs(center(cell) - (columns[run[0]][1][0] + columns[run[-1]][1][1]) / 2) <= TOLERANCE
               for cell, run in zip(cells, runs)):
            fits.append(runs)
    return fits[0] if len(fits) == 1 else None


def caption_cells(rows, header_index, columns):
    """The caption cells over each column, from the rows directly above the header row.

    On a caption row the cells standing over the columns are each centered over their own consecutive run of
    columns, and the runs cover the header: a group caption over its group. Per column, the cells are (row index,
    cell index); every list is empty when nothing stands over the columns (the column labels name the periods).
    A further row that does not split ends the captions, and so does a row of a table printed above: its rows
    never caption this header.

    Unsupported (ambiguous_link) when the captions do not assign the columns: the nearest row over the columns
    admits no such split, or several; or a period caption of the supported kind stands beside the columns, in the
    header row, on a caption row or on a row between the header and its captions, printed whole, wrapped onto
    further rows of its own block, or begun by a block with its continuation while its 'За 9' stands apart
    (caption_beside). Printed left of the columns, such a caption applies to the table but to no column the grid
    can tell: the header is then unverifiable, never a bare year. A company name or a page title without a period
    beside the columns assigns nothing and forbids nothing, and neither do the words of separate blocks beside the
    columns; what stands above the assigned captions describes the page, not the columns."""
    column_bands = bands(columns)
    span = (columns[0][1][0] - TOLERANCE, columns[-1][1][1] + TOLERANCE)
    captions = [[] for _ in columns]
    beside = {header_index: [cell for i, cell in enumerate(rows[header_index]) if i not in {i for i, _ in columns}]}
    for r in range(header_index - 1, max(header_index - CAPTION_ROWS, 0) - 1, -1):
        if foreign_row(rows[r], column_bands):
            break
        over = [(i, cell) for i, cell in enumerate(rows[r]) if cell[1] > span[0] and cell[0] < span[1]]
        if not over:
            if not any(captions):
                beside[r] = rows[r]
            continue
        partition = centered_partition([cell for _, cell in over], columns)
        if partition is None:
            if any(captions):
                break
            raise Unsupported('ambiguous_link', 'Подписи над столбцами не распределены по группам однозначно.')
        beside[r] = [cell for i, cell in enumerate(rows[r]) if i not in {i for i, _ in over}]
        for (i, _), run in zip(over, partition):
            for column in run:
                captions[column].append((r, i))
    if caption_beside(beside):
        raise Unsupported('ambiguous_link', 'Подпись периода в заголовке таблицы не связана со столбцами однозначно.')
    return captions


def caption_beside(beside):
    """Whether a period caption of the supported kind stands among the cells beside the columns, keyed by layout row.

    The cells form blocks by position before a word of them is read: two cells belong to one block when they stand
    in one column of the page (overlapping), on one row or on different rows of the heading, whatever rows lie between
    them. 'За 9' over a blank row over 'месяцев, закончившихся 30 сентября' is one block, as is the caption printed
    whole or wrapped onto the next row. A block is read in print order, row by row and left to right, with every word
    kept. Cells printed apart, left and right of the columns or in separate margins, stay separate blocks whatever
    their rows: 'За 9' of one block and 'месяцев' of another never complete each other into the caption. The group
    captions over the columns and the rows of another table are never among these cells.

    A caption stands beside the columns when a block prints it whole, and when a block begins with its continuation:
    'месяцев, закончившихся 30 сентября' opening a block is a part of the supported caption whose 'За 9' the position
    has not bound to it (indented beyond the tolerance, or above the heading window). Which columns that caption
    describes is then unresolved, and the blocks not joining is no proof that the years stand alone. A part the
    block itself settles is no caption: 'За 9' continued by 'дней' or 'месяцев' preceded by 'Возраст' in its own
    block are the words of a note, and a 'За 9' no block continues completes no supported caption."""
    blocks = []
    for r, cell in sorted((r, cell) for r, cells in beside.items() for cell in cells):
        joined = [block for block in blocks if any(overlapping(cell, other) for _, other in block)]
        apart = [block for block in blocks if block not in joined]
        blocks = apart + [[*(entry for block in joined for entry in block), (r, cell)]]
    texts = [' '.join(cell[2] for _, cell in sorted(block)) for block in blocks]
    return any(CAPTION_TOKEN.search(text) or CAPTION_CONTINUATION.match(text) for text in texts)


def overlapping(a, b):
    """Whether two cells stand in one column of the page: their horizontal extents overlap within the grid tolerance."""
    return a[0] <= b[1] + TOLERANCE and b[0] <= a[1] + TOLERANCE


def caption_lines(groups, column, line_at):
    """The original lines printing the caption over this column: [] when nothing stands over it (the column label
    names the period itself); None when the captions do not assign the columns, or a caption cell is printed by no
    unique original line (a fragment repeated on the page): the column is then not verifiable, never a bare year."""
    if groups is None:
        return None
    lines = {line_at.get(place) for place in groups[column]}
    return None if None in lines else sorted(lines)


def heading_rows(rows, header_index, columns):
    """The rows of the table's own heading: the header row and the rows standing directly above it, up to a blank
    row or a row of a table printed above. A unit printed here, or in the label of the row, belongs to the table; a
    unit printed elsewhere on the page, over another table or apart from this one, does not."""
    column_bands = bands(columns)
    first = header_index
    while first > 0 and rows[first - 1] and not foreign_row(rows[first - 1], column_bands):
        first -= 1
    return rows[first:header_index + 1]


def page_tables(original, layout):
    """The tables of a page for the extractor: per header row its period columns, each with the original line
    printing its header cell and the original lines of its group caption, and per table row the column of every
    amount cell. Only the links the server verifies are given; the cells themselves are not repeated."""
    rows = layout_rows(layout)
    line_at, whole_row = cell_lines(placements(original, rows), rows)
    headers = [(r, header_columns(cells)) for r, cells in enumerate(rows)]
    headers = [(r, columns) for r, columns in headers if columns]
    tables = []
    for position, (r, columns) in enumerate(headers):
        try:
            groups = caption_cells(rows, r, columns)
        except Unsupported:
            groups = None   # the captions do not assign the columns: every column of this header is unverifiable
        entries = []
        for number, (i, cell) in enumerate(columns, 1):
            period_lines = caption_lines(groups, number - 1, line_at)
            year_line = line_at.get((r, i))
            if (year_line is None or squeeze(cell[2]) not in squeeze(original[year_line - 1])
                    or period_lines == [] and column_period(cell[2]) is None):
                period_lines = None
            entries.append(dict(column=number, label=cell[2], year_line=year_line, period_lines=period_lines))
        following = headers[position + 1][0] if position + 1 < len(headers) else len(rows)
        table_rows = []
        for data_row in range(r + 1, following):
            number = whole_row.get(data_row)
            if number is None:
                continue
            labels, amounts = split_row(rows[data_row], bands(columns))
            if labels and not detached_value(labels) and any(band is not None for _, band in amounts):
                table_rows.append(dict(line=number, cells=[None if band is None else band + 1 for _, band in amounts]))
        if table_rows:
            tables.append(dict(columns=entries, rows=table_rows))
    return tables


def resolve(original, layout, locator):
    """The cell a table locator names, located on the saved layout: the amount cells under the named column of
    the named row, the row's label, the column header, the caption of the column's group and the text of the
    table's own heading. Every position comes from the layout; the model only names the original lines.

    The caption over the column is read from the layout, whatever range the locator cites: a column under a
    group caption must cite that caption in full, a column with nothing over it names its period itself and
    cites its header line only (explicit), and captions the layout does not assign, prints beside the columns
    or prints by no unique original line, leave the column unverifiable. The year line may stand inside the
    cited caption range, as a header quoted whole."""
    rows = layout_rows(layout)
    placed = placements(original, rows)
    line_at, _ = cell_lines(placed, rows)
    if locator['period_line_start'] > locator['period_line_end']:
        raise Unsupported('unsupported_layout', 'Диапазон строк подписи периода пуст.')
    row_place, header_place = placed.get(locator['row_line']), placed.get(locator['year_line'])
    if row_place is None or header_place is None:
        raise Unsupported('ambiguous_link', 'Текстовая строка не имеет однозначного положения в PDF.')
    header_index, row_index = header_place[0], row_place[0]
    if row_index <= header_index:
        raise Unsupported('unsupported_layout', 'Заголовок периодов расположен не над строкой показателя.')
    columns = header_columns(rows[header_index])
    if columns is None or not 1 <= locator['column'] <= len(columns):
        raise Unsupported('unsupported_layout', 'Не установлены столбцы периода.')
    cell_index, column = columns[locator['column'] - 1]
    if not header_place[1] <= cell_index <= header_place[2]:
        raise Unsupported('ambiguous_link', 'Указанная строка заголовка не содержит выбранный столбец.')
    if squeeze(column[2]) not in squeeze(original[locator['year_line'] - 1]):
        raise Unsupported('ambiguous_link', 'Указанная строка заголовка не содержит подпись столбца целиком.')
    if row_place[1:] != (0, len(rows[row_index]) - 1):
        raise Unsupported('ambiguous_link', 'Строка показателя делит ряд расположения с другим текстом.')
    if any(header_columns(rows[r]) for r in range(header_index + 1, row_index)):
        raise Unsupported('unsupported_layout', 'Между заголовком и строкой показателя стоит другой заголовок периодов.')
    labels, amounts = split_row(rows[row_index], bands(columns))
    if detached_value(labels):
        raise Unsupported('ambiguous_link', 'Число строки находится перед первым столбцом. По расположению текста нельзя надёжно определить период.')
    groups = caption_cells(rows, header_index, columns)
    group = set(groups[locator['column'] - 1])
    cited_numbers = [n for n in range(locator['period_line_start'], locator['period_line_end'] + 1) if n != locator['year_line']]
    caption = None
    if not group:
        if cited_numbers:
            raise Unsupported('unsupported_layout', 'Над выбранным столбцом нет подписи периода.')
    else:
        if not group <= set(line_at):
            raise Unsupported('ambiguous_link', 'Подпись периода над выбранным столбцом не имеет однозначной исходной строки.')
        if not cited_numbers:
            raise Unsupported('ambiguous_link', 'Над выбранным столбцом стоит подпись периода, которую locator не цитирует.')
        cited = set()
        for number in cited_numbers:
            place = placed.get(number)
            if place is None:
                raise Unsupported('ambiguous_link', 'Текстовая строка не имеет однозначного положения в PDF.')
            cells = {(place[0], i) for i in range(place[1], place[2] + 1)}
            if not cells & group:
                raise Unsupported('ambiguous_link', 'Процитированная подпись периода не относится к группе выбранного столбца.')
            cited |= cells
        if not group <= cited:
            raise Unsupported('unsupported_layout', 'Подпись периода выбранного столбца процитирована не полностью.')
        caption = ' '.join(rows[r][i][2] for r, i in sorted(group))
    heading = ' '.join(cell[2] for row in heading_rows(rows, header_index, columns) for cell in row)
    return dict(cells=[cell[2] for cell, band in amounts if band == locator['column'] - 1],
                label=' '.join(cell[2] for cell in labels), column_label=column[2], caption=caption,
                explicit=caption is None, heading=heading)
