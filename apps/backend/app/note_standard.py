"""Записка AIST v2: оформление сохраненного содержания без модельного вызова."""
from functools import lru_cache
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.run import Run

from . import note_renderer as legacy
from .domain import known
from .visual_observations import annotation, NOTICE
from .review_candidates import annotation as candidate_annotation

TEMPLATE = 'aist-note.v2'
BUILD = 'notepath06r2.1-docx1.2.0-lo25.2.3u8-ls2.2.0'
RENDERERS = {fmt: 'aist-' + fmt + '.' + BUILD for fmt in ('docx', 'pdf')}
FONT = 'Liberation Sans'
NATURE = {'reported_actual': 'факт', 'forecast': 'прогноз', 'source_statement': 'данные источника'}


def number(amount):
    integer, dot, fraction = amount['value'].partition('.')
    return re.sub(r'(?<=\d)(?=(\d{3})+$)', ' ', integer) + (',' + fraction if dot else '')


def financial_table(block, facts, *, findings=None, edited_values=(), calculations=None, observations=None):
    """Совместимость для проверки версии и старых потребителей одной таблицы."""
    table = financial_projection(block, facts, calculations=calculations, observations=observations,
                                 findings=findings, edited_values=edited_values)
    return table['unit'], (table['headers'], table['values'])


def financial_projection(block, facts, *, calculations=None, observations=None,
                         findings=None, edited_values=()):
    """Матрица вместе с исходной строкой каждой ячейки, без изменения оснований.

    Непроверенные наблюдения допускаются только отдельным рядом одного изображения.
    Их происхождение и статус остаются у ячейки, поворот не является проверкой.
    """
    rows = block['rows']
    detailed = dict(unit=None, headers=['Показатель', 'Период', 'Значение'],
                    values=[[r['label'], r['period_label'], legacy.amount(r['amount'])] for r in rows],
                    origins=[[None, None, r] for r in rows], notes=[], pivoted=False)
    calculations = calculations or {}
    observations = observations or {}
    selected = []
    for row in rows:
        refs, calc_ids, visual_ids = row['fact_refs'], row['calculation_ids'], row.get('visual_observation_ids', [])
        if row.get('review_candidate_ids'):
            return detailed
        if len(refs) == 1 and not calc_ids and not visual_ids:
            fact = facts.get((refs[0]['source_version_id'], refs[0]['fact_id']))
            if not fact or not same_row_metric(row, fact['metric']):
                return detailed
            scope = (fact['source']['source_version_id'], fact['subject'], fact['reporting_basis'])
            if not known(scope[1]) or not known(scope[2]):
                return detailed
        elif len(visual_ids) == 1 and not refs and not calc_ids:
            observation = observations.get(visual_ids[0])
            if not observation or not observation['numeric']:
                return detailed
            fact = observation['numeric']
            if not known(fact['subject']) or not same_row_metric(row, fact['metric']):
                return detailed
            scope = (observation['source']['source_version_id'], fact['subject'],
                     ('visual', observation['page']))
        elif len(calc_ids) == 1 and not refs and not visual_ids:
            calculation = calculations.get(calc_ids[0])
            if not calculation or calculation['formula_id'] != 'growth_percent.v1':
                return detailed
            inputs = [facts.get((i['fact']['source_version_id'], i['fact']['fact_id']))
                      for i in calculation['inputs']]
            if len(inputs) != 2 or not all(inputs):
                return detailed
            if row['period_label'] != ' к '.join(f['period']['label'] for f in reversed(inputs)):
                return detailed
            previous, current = inputs
            if any(previous[k] != current[k] for k in ('subject', 'reporting_basis', 'metric', 'nature')):
                return detailed
            if not same_growth_metric(row, current['metric']):
                return detailed
            scope = (current['source']['source_version_id'], current['subject'], current['reporting_basis'])
            if previous['source']['source_version_id'] != scope[0] or not all(known(x) for x in scope):
                return detailed
            fact = dict(amount=calculation['rounded'], metric=calculation['formula_id'] + ':' + current['metric'],
                        nature=current['nature'], period=dict(kind='comparison', label=row['period_label'],
                        start=previous['period']['start'], end=current['period']['end']))
        else:
            return detailed
        amount = fact['amount']
        same_value = amount == row['amount']
        if not same_value and row.get('row_id') in edited_values and amount:
            same_value = all(amount[k] == row['amount'][k] for k in ('unit', 'currency', 'scale'))
        period = fact['period']
        if (not same_value or period['label'] != row['period_label'] or period['kind'] == 'unknown'
                or not period['start'] or not period['end'] or period['start'] > period['end']
                or fact['nature'] not in ('reported_actual', 'forecast')
                or period['kind'] == 'instant' and period['start'] != period['end']):
            return detailed
        selected.append((fact, scope))
    if not selected or any(scope != selected[0][1] for _, scope in selected):
        return detailed

    # Подпись не объединяет разные метрики, единицы или виды финансового периода.
    meanings, labels = {}, {}
    columns, cells = {}, {}
    repeated = False
    for row, (fact, scope) in zip(rows, selected):
        period, amount = fact['period'], row['amount']
        meaning = (fact['metric'], period['kind'], amount['unit'], amount['scale'], amount['currency'])
        if row['label'] in meanings and meanings[row['label']] != meaning:
            return detailed
        meanings[row['label']] = meaning
        key = (meaning, scope)
        if key in labels and labels[key] != row['label']:
            return detailed
        labels.setdefault(key, row['label'])
        column = (period['kind'], period['start'], period['end'], fact['nature'])
        columns.setdefault(column, period['label'])
        if (key, column) in cells:
            repeated = True
            if findings is not None and cells[key, column]['amount'] != row['amount']:
                findings.append(dict(code='conflicting_financial_cell', severity='error', block_id=block.get('block_id'),
                    row_id=row.get('row_id'), message='У показателя разные значения за один период. '
                    'Обе строки и источники сохранены. Уточните, какой ряд использовать.'))
            continue
        cells[key, column] = row
    if repeated:
        return detailed

    # Остаток можно поставить рядом с потоком только при единственном периоде с этой конечной датой.
    aliases = {}
    for column in columns:
        if column[0] == 'instant':
            matching = [c for c in columns if c[0] == 'interval' and c[2:] == column[2:]]
            if len(matching) == 1:
                aliases[column] = matching[0]
    ordered = sorted({aliases.get(c, c) for c in columns}, key=lambda c: (c[2], c[1], c[0], c[3]))
    remapped = {(key, aliases.get(column, column)): row for (key, column), row in cells.items()}
    # Ключи периода сохраняют различие между годом, кварталом и сравнением двух лет.
    headers = ['Показатель']
    used_labels = {}
    for column in ordered:
        label = columns[column] + ' (' + NATURE[column[3]] + ')'
        used_labels.setdefault(label, []).append(column)
    for column in ordered:
        label = columns[column] + ' (' + NATURE[column[3]] + ')'
        if len(used_labels[label]) > 1:
            label += ' ' + column[1] + ' - ' + column[2]
        headers.append(label)
    unit = shared_unit(block) or ''
    has_balances = any(f['period']['kind'] == 'instant' for f, _ in selected)
    values, origins = [], []
    for key, label in labels.items():
        meaning, _ = key
        if has_balances and meaning[1] in ('instant', 'interval'):
            label += ' (на конец периода)' if meaning[1] == 'instant' else ' (за период)'
        if not unit:
            first_row = next(row for (row_key, _), row in cells.items() if row_key == key)
            row_unit = shared_unit(dict(rows=[first_row]))
            if row_unit and not shared_unit_pattern(row_unit).match(label):
                label += ', ' + row_unit
        items = [remapped.get((key, column)) for column in ordered]
        values.append([label, *(number(item['amount']) if item else '-' for item in items)])
        origins.append([None, *items])
    scope = selected[0][1]
    notes = [scope[1] + (', ' + scope[2] if isinstance(scope[2], str) else '')]
    return dict(unit=unit, headers=headers, values=values, origins=origins, notes=notes, pivoted=True,
                columns=ordered)


def financial_pages(table, columns=4):
    """Ограничить ширину, повторяя названия строк, без потери исходного row_id."""
    width = len(table['headers']) - 1
    if not table['pivoted'] or width <= columns:
        return [table]
    return [dict(table, headers=[table['headers'][0], *table['headers'][start:start + columns]],
                 values=[[r[0], *r[start:start + columns]] for r in table['values']],
                 origins=[[r[0], *r[start:start + columns]] for r in table['origins']])
            for start in range(1, width + 1, columns)]


SCALE_WORDS = {'млн': r'(?:млн\.?|миллион(?:ов|а)?)', 'тыс.': r'(?:тыс\.?|тысяч(?:а|и)?)',
               'млрд': r'(?:млрд\.?|миллиард(?:ов|а)?)',
               'трлн': r'(?:трлн\.?|триллион(?:ов|а)?)'}
MEASURE_WORDS = {'руб.': r'(?:руб\.?|рублей|₽|RUB)', 'USD': r'(?:USD|долл\.?|долларов|\$)',
                 'EUR': r'(?:EUR|евро|€)', '%': r'(?:%|процент(?:ов|а)?)',
                 'коэф.': r'(?:коэф\.?|x|×|х|раз(?:а|ов)?)'}


def shared_unit_pattern(unit):
    """Точный повтор общей единицы в конце подписи: после разделителя, пробела или в скобках."""
    scale, _, measure = unit.rpartition(' ')
    words = (SCALE_WORDS.get(scale, re.escape(scale)) + r'\s*' if scale else '') + MEASURE_WORDS.get(measure, re.escape(measure))
    return re.compile(r'^(?P<kept>.*?\S)\s*(?:[,;:]\s*|\(\s*|\s)(?:в\s+)?' + words + r'\s*\)?\s*$', re.I)


def shared_unit(block):
    """Общая единица всех строк таблицы в написании стандартного оформления, иначе None."""
    amounts = {(row['amount']['unit'], row['amount']['scale'], row['amount']['currency']) for row in block['rows']}
    if len(amounts) != 1:
        return None
    unit, scale, currency = amounts.pop()
    display = dict(value='1', unit=unit, scale=scale, currency='руб.' if currency == 'RUB' else currency)
    return legacy.amount(display)[1:].strip()


# Exact translations only. Adjusted, gross and segment metrics are different observations.
METRIC_TRANSLATIONS = {
    'money': {'revenue': 'Выручка', 'net debt': 'Чистый долг',
              'owc': 'Операционный оборотный капитал (OWC)',
              'change in owc': 'Изменение оборотного капитала (OWC)',
              'operating working capital': 'Операционный оборотный капитал (OWC)'},
    'percent': {'ebitda margin': 'Рентабельность по EBITDA', 'маржа ebitda': 'Рентабельность по EBITDA',
                'lfl продажи': 'Изменение сопоставимых продаж (LFL)',
                'lfl sales': 'Изменение сопоставимых продаж (LFL)',
                'lfl трафик': 'Изменение сопоставимого трафика (LFL)',
                'lfl traffic': 'Изменение сопоставимого трафика (LFL)',
                'lfl средний чек': 'Изменение сопоставимого среднего чека (LFL)'},
    'ratio': {'net debt/ebitda': 'Чистый долг/EBITDA', 'чистый долг/ebitda': 'Чистый долг/EBITDA'},
}


def translated_metric(text, unit):
    key = ' '.join(text.split()).casefold()
    if unit == 'ratio':
        # Пробелы вокруг разделителя не меняют только уже известный точный alias.
        key = re.sub(r'\s*/\s*', '/', key)
    return METRIC_TRANSLATIONS.get(unit, {}).get(key, text)


def translate_row_labels(block):
    """Localize exact standard labels of a new model table, keeping unit suffixes and IDs."""
    for row in block['rows']:
        text = row['label']
        match = shared_unit_pattern(shared_unit(dict(rows=[row]))).match(text)
        label = match['kept'] if match else text
        translated = translated_metric(label, row['amount']['unit'])
        row['label'] = translated + text[len(label):]


def same_row_metric(row, metric):
    """Сравнение подписи строки с источником, без повтора единицы измерения."""
    pattern = shared_unit_pattern(shared_unit(dict(rows=[row])))

    def label(text):
        match = pattern.match(text)
        stripped = match['kept'] if match else text
        return ' '.join(translated_metric(stripped, row['amount']['unit']).split()).casefold()

    left, right = label(row['label']), label(metric)
    if left == right:
        return True
    # Только явные синонимы маржи. Сумма EBITDA и adjusted EBITDA сюда не входят.
    ebitda_margin = {'ebitda margin', 'маржа ebitda', 'рентабельность по ebitda'}
    if row['amount']['unit'] == 'percent':
        return left in ebitda_margin and right in ebitda_margin
    funding = {'привлеченные средства', 'общий размер привлеченных средств'}
    return (row['amount']['unit'] == 'money'
            and left.replace('ё', 'е') in funding and right.replace('ё', 'е') in funding)


def same_growth_metric(row, metric):
    """Подпись расчёта роста сохраняет метрику входов, без неявных синонимов."""
    label = row['label'].strip()
    match = shared_unit_pattern('%').match(label)
    if match:
        label = match['kept']
    match = re.fullmatch(r'(?:рост|прирост|изменение|темп прироста)\s*[:\s]\s*(.+)', label, re.I)
    if not match:
        return False
    metric = ' '.join(metric.split()).casefold()
    genitive = {'выручка': 'выручки', 'прибыль': 'прибыли', 'чистая прибыль': 'чистой прибыли',
                'валовая прибыль': 'валовой прибыли', 'операционная прибыль': 'операционной прибыли'}
    return ' '.join(match[1].split()).casefold() in {metric, genitive.get(metric, metric)}


def shared_unit_once(block):
    """Убирает точный повтор общей единицы из caption и label однородной таблицы.

    Содержательная подпись, другие числа и даты сохраняются. Смешанные единицы и
    подписи, которые без повтора совпали бы между строками, остаются как есть."""
    unit = shared_unit(block)
    if unit is None:
        return
    pattern = shared_unit_pattern(unit)

    def kept(text):
        match = pattern.match(text)
        return match['kept'] if match else text

    labels = [kept(row['label']) for row in block['rows']]
    before = {(row['label'], row['period_label']) for row in block['rows']}
    after = {(label, row['period_label']) for label, row in zip(labels, block['rows'])}
    if len(after) < len(before):
        return
    block['caption'] = kept(block['caption'])
    for row, label in zip(block['rows'], labels):
        row['label'] = label


def detailed_presentation(block, facts, sources):
    """Format literal rows independently of financial_table's validation and pivot rules."""
    if any(row.get('visual_observation_ids') for row in block['rows']):
        return None, (['Показатель', 'Период', 'Значение'], [
            [row['label'], row['period_label'], legacy.amount(row['amount'])] for row in block['rows']]), []
    selected = [[facts[(ref['source_version_id'], ref['fact_id'])] for ref in row['fact_refs']]
                for row in block['rows']]
    confirmed = all(group and not row['calculation_ids'] and
                    all(fact['amount'] == row['amount'] and fact['period']['label'] == row['period_label']
                        for fact in group) for row, group in zip(block['rows'], selected))
    unit = shared_unit(block) if confirmed else None
    table = (['Показатель', 'Период', 'Значение'], [
        [row['label'], row['period_label'], number(row['amount']) if unit else legacy.amount(row['amount'])]
        for row in block['rows']])
    scopes = [[(fact['source']['source_version_id'], fact['subject'], fact['reporting_basis'], fact['nature'])
               for fact in group] for group in selected]
    unique = {scope for group in scopes for scope in group}
    common = (len(unique) == 1 and all(scopes) and
              all(known(scope[1]) and known(scope[2]) for scope in unique))
    multiple_sources = len({scope[0] for scope in unique}) > 1
    documents = {source['source']['source_version_id']: source for source in sources}
    source_numbers = {source: index for index, source in enumerate(dict.fromkeys(scope[0] for group in scopes for scope in group), 1)}

    def description(scope):
        source, subject, basis, nature = scope
        text = subject + ', ' + basis + ', ' + NATURE[nature]
        if multiple_sources:
            document = documents.get(source)
            text += ', источник ' + str(source_numbers[source])
            if document:
                text += ', ' + document['original_filename'] + ', версия ' + str(document['revision'])
        return text

    if common:
        details = [('caption', description(next(iter(unique))) + '.')]
    else:
        details = []
        for row, group in zip(block['rows'], scopes):
            if group:
                text = row['label'] + ', ' + row['period_label'] + ': ' + '; '.join(dict.fromkeys(description(scope) for scope in group)) + '.'
                if ('text', text) not in details:
                    details.append(('text', text))
    return unit, table, details


def content_plan(version, settings, checks):
    content, basis = version['payload']['content'], version['basis']
    facts = {(f['source']['source_version_id'], f['fact_id']): f for f in basis['facts']}
    observations = {item['observation_id']: item for item in basis.get('visual_observations', [])}
    plan = [('title', content['title'])]
    if settings['release_mode'] == 'draft':
        plan.append(('p', 'ЧЕРНОВИК'))
        # Черновик с замечаниями проверок обозначается одной строкой; сами замечания
        # остаются в сохраненной версии, перечень ошибок в документ не входит.
        if any(check['status'] == 'failed' for check in checks):
            plan.append(('p', 'Версия не принята: в сохраненной версии есть замечания проверок.'))
    if observations:
        plan.append(('p', NOTICE))
    for section in content['sections']:
        plan.append(('h', section['title']))
        for block in section['blocks']:
            if block['kind'] == 'paragraph':
                plan.append(('text', block['claim']['text']))
                if mark := annotation(block['claim'], observations):
                    plan.append(('p', mark))
                if mark := candidate_annotation(block['claim'], basis):
                    plan.append(('p', mark))
            else:
                plan.append(('caption', block['caption']))
                projection = financial_projection(block, facts,
                    calculations={c['calculation_id']: c for c in basis['calculations']}, observations=observations)
                unit = projection['unit']
                details = []
                if not projection['pivoted']:
                    unit, table, details = detailed_presentation(block, facts, basis.get('sources', []))
                    projection = dict(projection, headers=table[0], values=table[1])
                else:
                    details = [('caption', text) for text in projection['notes']]
                if unit and block['caption'].strip() != unit and not shared_unit_pattern(unit).match(block['caption']):
                    plan.append(('caption', unit))
                plan.extend(detail for detail in details if detail[0] == 'caption')
                for page in financial_pages(projection):
                    if page['pivoted']:
                        for cells, origins in zip(page['values'], page['origins']):
                            for index, item in enumerate(origins):
                                if item and (mark := annotation(item, observations)):
                                    cells[index] += '\n' + mark
                    else:
                        for row, cells in zip(block['rows'], page['values']):
                            if mark := annotation(row, observations):
                                cells[0] += '\n' + mark
                    plan.append(('table', (page['headers'], page['values'])))
                plan.extend(detail for detail in details if detail[0] == 'text')
    # Оговорки для читателя входят в сохраненные абзацы. Основания и служебные
    # ограничения остаются в AIST: скачивание не дописывает выбранную версию.
    return plan


def text_lines(text):
    # Markers remain literal text. There is no new block model or automatic renumbering.
    return text.split('\n')


def expected_paragraphs(plan):
    result = []
    for kind, data in plan:
        if kind == 'table':
            headers, rows = data
            result.extend(headers)
            for row in rows:
                result.extend(row)
        elif kind == 'text':
            result.extend(text_lines(data))
        else:
            result.append(data)
    return result


def format_table(table, widths):
    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    repeat = OxmlElement('w:tblHeader')
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for ri, row in enumerate(table.rows):
        # Normal financial rows fit a page; allow genuinely tall saved text to flow.
        if max(len(cell.text) for cell in row.cells) < 600:
            row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for ci, cell in enumerate(row.cells):
            cell.width = Cm(widths[ci])
            properties = cell._tc.get_or_add_tcPr()
            margins = OxmlElement('w:tcMar')
            borders = OxmlElement('w:tcBorders')
            for side in ('top', 'left', 'bottom', 'right'):
                margin = OxmlElement('w:' + side)
                margin.set(qn('w:w'), '45' if side in ('top', 'bottom') else '70')
                margin.set(qn('w:type'), 'dxa')
                margins.append(margin)
                border = OxmlElement('w:' + side)
                for attr, value in [('val', 'single'), ('sz', '4'), ('color', 'B4C6E7')]:
                    border.set(qn('w:' + attr), value)
                borders.append(border)
            properties.extend([margins, borders])
            if ri == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), '203864')
                properties.append(shade)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.keep_with_next = ri == 0
                paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT if ci == 0 else WD_ALIGN_PARAGRAPH.RIGHT
                for run in paragraph.runs:
                    run.bold = ri == 0
                    run.font.color.rgb = RGBColor.from_string('FFFFFF' if ri == 0 else '000000')


def build_docx(plan, theme, path):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.5)
    section.left_margin = section.right_margin = Cm(1.7)
    for name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Caption'):
        style = doc.styles[name]
        style.font.name = FONT
        style.font.size = Pt(10)
        style.font.bold = name in ('Title', 'Heading 1', 'Heading 2')
        style.font.italic = False
        style.font.color.rgb = RGBColor.from_string('4472C4' if name == 'Title' else '000000')
        style.paragraph_format.space_before = Pt(5 if name.startswith('Heading') else 0)
        style.paragraph_format.space_after = Pt(2 if theme == 'compact' else 3)
        style.paragraph_format.line_spacing = 1.0
        style.paragraph_format.widow_control = True
    for fonts in doc.styles.element.xpath('.//w:rFonts'):
        for attribute in list(fonts.attrib):
            del fonts.attrib[attribute]
        for family in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
            fonts.set(qn('w:' + family), FONT)
    for style in doc.styles:
        for border in style.element.xpath('.//w:pBdr'):
            border.getparent().remove(border)
    doc.core_properties.author = 'AIST'
    doc.core_properties.title = plan[0][1][:legacy.DOCX_METADATA_TITLE_LIMIT]
    for kind, data in plan:
        if kind == 'table':
            headers, rows = data
            table = doc.add_table(rows=1, cols=len(headers))
            for cell, title in zip(table.rows[0].cells, headers):
                cell.text = title
            for row in rows:
                for cell, text in zip(table.add_row().cells, row):
                    cell.text = text
            widths = [6.2] + [11.4 / (len(headers) - 1)] * (len(headers) - 1)
            if headers == ['Показатель', 'Период', 'Значение']:
                widths = [7.0, 4.6, 6.0]
            format_table(table, widths)
        elif kind == 'text':
            for line in text_lines(data):
                paragraph = doc.add_paragraph(line)
                if re.match(r'^\s*[-*•]\s+', line):
                    paragraph.paragraph_format.left_indent = Cm(.55)
                    paragraph.paragraph_format.first_line_indent = Cm(-.3)
        else:
            paragraph = doc.add_paragraph(data, {'title': 'Title', 'h': 'Heading 1', 'caption': 'Caption'}.get(kind, 'Normal'))
            if kind == 'caption':
                paragraph.paragraph_format.keep_with_next = True
    explicit_ruble_glyphs(doc)
    doc.save(path)


def explicit_ruble_glyphs(doc):
    # Liberation Sans has no ruble glyph. Explicit runs preserve both the glyph and
    # PDF text order; LibreOffice's implicit fallback paints it after the whole run.
    paragraphs = [*doc.paragraphs, *[p for t in doc.tables for row in t.rows for cell in row.cells for p in cell.paragraphs]]
    for paragraph in paragraphs:
        for run in list(paragraph.runs):
            if '₽' not in run.text:
                continue
            for part in re.split('(₽)', run.text):
                if not part:
                    continue
                element = deepcopy(run._r)
                replacement = Run(element, paragraph)
                replacement.text = part
                if part == '₽':
                    replacement.font.name = 'DejaVu Sans'
                run._r.addprevious(element)
            paragraph._p.remove(run._r)


@lru_cache(maxsize=1)
def verify_environment():
    legacy.verify_environment()
    if legacy.document_command(['/usr/bin/dpkg-query', '-W', '-f=${Version}', 'fonts-liberation'], seconds=5).decode() != '1:2.1.5-3':
        raise RuntimeError('Standard renderer font package mismatch')
    for name, expected in (
        ('Regular', 'bade59d822652f76e6941aa87b40a87c13d1cc70db98ededb5011127efafd1d3'),
        ('Bold', '1b5f2da6f4cadce4c05b9ecebe3a6fcd374eb95ae443605e799f4c3287978939'),
    ):
        path = Path('/usr/share/fonts/truetype/liberation') / ('LiberationSans-' + name + '.ttf')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Standard renderer font files mismatch')


def render(version, settings, checks, directory):
    verify_environment()
    plan = content_plan(version, settings, checks)
    docx = directory / 'note.docx'
    build_docx(plan, settings['theme'], docx)
    pdf = legacy.convert(docx, directory)
    return legacy.verify(docx, pdf, [], expected_paragraphs=expected_paragraphs(plan), font_name=('LiberationSans', 'DejaVuSans'))
