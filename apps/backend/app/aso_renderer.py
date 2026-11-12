"""Одноколоночная записка AIST Modern с редактируемыми таблицами и повторяемой шапкой."""
import hashlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from . import note_renderer
from .aso import presentation

TEMPLATE = 'aist-aso.v1'
RENDERERS = {fmt: 'aist-aso-' + fmt + '.27-modern-lo25.2.3' for fmt in ('docx', 'pdf')}
BODY_WIDTH = 17.4
ASSETS = Path(__file__).resolve().parents[1] / 'assets/aso'
# Оценка используется только для читаемых продолжений, не для сокращения содержания.
ROW_PT, LINE_PT, CHARS_PER_CM, PART_PT = 7, 12, 4.6, 650
FONTS = (
    ('Inter', 'Regular', 'Inter-Regular.ttf'), ('Inter', 'Bold', 'Inter-Bold.ttf'),
    ('Inter', 'Italic', 'Inter-Italic.ttf'), ('Manrope', 'Regular', 'manrope-regular.ttf'),
    ('Manrope', 'Bold', 'manrope-bold.ttf'),
)


def embed_fonts(doc):
    """Полные OFL-шрифты для редакторов с поддержкой встроенных шрифтов DOCX."""
    from lxml import etree

    font_table = doc.part.part_related_by(RT.FONT_TABLE)
    root = parse_xml(font_table.blob)
    for family, style, filename in FONTS:
        font = next((node for node in root if node.get(qn('w:name')) == family), None)
        if font is None:
            font = OxmlElement('w:font')
            font.set(qn('w:name'), family)
            root.append(font)
        key = uuid5(NAMESPACE_URL, 'aist-modern/' + filename)
        data = bytearray((ASSETS / filename).read_bytes())
        mask = key.bytes[::-1]
        for index in range(32):
            data[index] ^= mask[index % 16]
        part = Part(PackURI('/word/fonts/' + filename + '.odttf'),
                    'application/vnd.openxmlformats-officedocument.obfuscatedFont', bytes(data), doc.part.package)
        link = OxmlElement('w:embed' + style)
        link.set(qn('r:id'), font_table.relate_to(part, RT.FONT))
        link.set(qn('w:fontKey'), '{' + str(key).upper() + '}')
        font.append(link)
    font_table._blob = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    doc.settings.element.append(OxmlElement('w:embedTrueTypeFonts'))


def write_text(paragraph, cell):
    if 'styled_runs' not in cell:
        paragraph.add_run(cell['text'])
        return
    for piece in cell['styled_runs']:
        run = paragraph.add_run(piece['text'])
        run.bold, run.italic = piece['bold'], piece['italic']


def add_link(paragraph, url):
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True))
    run = paragraph.add_run('ссылка')
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor.from_string('4A5568')
    run.font.underline = True
    link.append(run._r)
    paragraph._p.append(link)


def column_widths(count):
    label = 6.4
    return [label] + [(BODY_WIDTH - label) / (count - 1)] * (count - 1)


def row_height(texts, widths):
    lines = max(sum(max(1, -(-len(part) // int(width * CHARS_PER_CM))) for part in text.split('\n'))
                for text, width in zip(texts, widths))
    return ROW_PT + LINE_PT * lines


def table_parts(headers, rows, widths):
    if not rows:
        return []
    heights = [row_height([cell['text'] for cell in row], widths) for row in rows]
    room = PART_PT - row_height(headers, widths)
    if room < min(heights):
        return [[row] for row in rows]
    target = sum(heights) / -(-sum(heights) // room)
    parts, used = [[]], 0
    for row, height in zip(rows, heights):
        if parts[-1] and (used >= target or used + height > room):
            parts.append([])
            used = 0
        parts[-1].append(row)
        used += height
    return parts


def cell_margins(cell, top=0, bottom=0, left=0, right=0):
    margins = OxmlElement('w:tcMar')
    for side, width in (('top', top), ('left', left), ('bottom', bottom), ('right', right)):
        edge = OxmlElement('w:' + side)
        edge.set(qn('w:w'), str(width))
        edge.set(qn('w:type'), 'dxa')
        margins.append(edge)
    cell._tc.get_or_add_tcPr().append(margins)


def add_table(doc, columns):
    # Word и LibreOffice могут слить соседние таблицы с разным числом колонок.
    if len(doc.element.body) >= 2 and doc.element.body[-2].tag == qn('w:tbl'):
        gap = doc.add_paragraph().paragraph_format
        gap.space_before = gap.space_after = Pt(0)
        gap.line_spacing = Pt(1)
    return doc.add_table(rows=1, cols=columns)


def add_financial_table(doc, headers, values, widths, caption):
    table = add_table(doc, len(headers))
    table.autofit = False
    indent = OxmlElement('w:tblInd')
    indent.set(qn('w:w'), '0')
    indent.set(qn('w:type'), 'dxa')
    table._tbl.tblPr.append(indent)
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    for cell, text in zip(table.rows[0].cells, headers):
        cell.text = text
    for values_row in values:
        row = table.add_row()
        for cell, value in zip(row.cells, values_row):
            write_text(cell.paragraphs[0], value)
    for index, row in enumerate(table.rows):
        row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for column, cell in enumerate(row.cells):
            cell.width = Cm(widths[column])
            cell_margins(cell, top=40, bottom=40, left=65, right=65)
            borders = OxmlElement('w:tcBorders')
            border = OxmlElement('w:bottom')
            for name, value in [('val', 'single'), ('sz', '4'), ('color', 'D8D9D5')]:
                border.set(qn('w:' + name), value)
            borders.append(border)
            cell._tc.get_or_add_tcPr().append(borders)
            if index == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), 'F0F1EE')
                cell._tc.get_or_add_tcPr().append(shade)
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT if column == 0 else WD_ALIGN_PARAGRAPH.RIGHT
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.keep_with_next = index == 0
            for run in paragraph.runs:
                run.font.size = Pt(10)
                if index == 0:
                    run.bold = True
    table.rows[0].height = Pt(row_height(headers, widths))
    table.rows[0].height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    # Подпись внутри повторяемой шапки сохраняется при импорте в Pages.
    caption_row = table.add_row()
    caption_cell = caption_row.cells[0].merge(caption_row.cells[-1])
    line = caption_cell.paragraphs[0]
    run = line.add_run(caption)
    run.bold = True
    run.font.size = Pt(10)
    line.paragraph_format.keep_with_next = True
    line.paragraph_format.space_after = Pt(2)
    cell_margins(caption_cell, top=90, bottom=60)
    caption_row.height = Pt(row_height([caption], [BODY_WIDTH]))
    caption_row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    caption_row._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    caption_row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
    table._tbl.tr_lst[0].addprevious(caption_row._tr)


def page_field(paragraph, name):
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), name)
    paragraph._p.append(field)


def render(version, settings, checks, directory):
    from . import note_standard

    note_renderer.verify_environment()
    for line in (ASSETS / 'SHA256SUMS').read_text().splitlines():
        digest, filename = line.split('  ', 1)
        if hashlib.sha256((ASSETS / filename).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Modern renderer asset mismatch')
        if filename.endswith('.ttf') and hashlib.sha256((Path('/usr/local/share/fonts/aist') / filename).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Modern installed font mismatch')
    view = presentation(version, checks)
    doc = Document()
    for level in doc.part.numbering_part.element.findall('.//' + qn('w:lvl')):
        number = level.find(qn('w:numFmt'))
        if number is not None and number.get(qn('w:val')) == 'bullet':
            style = level.find(qn('w:pStyle'))
            style_id = style.get(qn('w:val')) if style is not None else ''
            level.find(qn('w:lvlText')).set(qn('w:val'), {'ListBullet2': '○', 'ListBullet3': '▪'}.get(style_id, '•'))
            fonts = level.find('.//' + qn('w:rFonts'))
            if fonts is not None:
                for attr in ('ascii', 'hAnsi', 'cs', 'eastAsia'):
                    fonts.set(qn('w:' + attr), 'Inter')
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.7)
    section.left_margin = section.right_margin = Cm(1.8)
    normal = doc.styles['Normal']
    normal.font.name, normal.font.size = 'Inter', Pt(11)
    normal.font.color.rgb = RGBColor.from_string('0E1116')
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.05
    heading = doc.styles['Heading 1']
    heading.font.name, heading.font.size, heading.font.bold = 'Manrope', Pt(12), True
    heading.font.color.rgb = RGBColor.from_string('0E1116')
    fonts = heading.element.rPr.rFonts
    for attribute in list(fonts.attrib):
        if attribute.endswith('Theme'):
            del fonts.attrib[attribute]
    for attribute in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
        fonts.set(qn('w:' + attribute), 'Manrope')
    heading.paragraph_format.space_before = Pt(9)
    heading.paragraph_format.space_after = Pt(4)
    heading.paragraph_format.keep_with_next = True
    for depth, name in enumerate(('List Bullet', 'List Bullet 2', 'List Bullet 3')):
        style = doc.styles[name].paragraph_format
        style.left_indent, style.first_line_indent = Pt(10 + 14 * depth), Pt(-9)
    header = doc.add_table(rows=1, cols=2)
    header.autofit = False
    for column, cell, width in zip(header.columns, header.rows[0].cells, (12, 5.4)):
        column.width = cell.width = Cm(width)
        cell_margins(cell, bottom=110)
    brand = header.cell(0, 0).paragraphs[0]
    brand.add_run().add_picture(str(ASSETS / 'aist.png'), width=Cm(.8))
    brand.add_run('  aist.').font.name = 'Manrope'
    brand.runs[-1].bold, brand.runs[-1].font.size = True, Pt(17)
    date_line = header.cell(0, 1).paragraphs[0]
    date_line.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    date_line.add_run(view['date']).font.size = Pt(9)
    title = doc.add_paragraph()
    run = title.add_run(view['title'])
    run.font.name, run.font.size, run.bold = 'Manrope', Pt(22), True
    title.paragraph_format.keep_with_next = True
    expected = ['  aist.', view['date'], view['title']]
    if 'team' in view:
        team = doc.add_paragraph('Команда: ' + view['team'])
        team.runs[0].font.size = Pt(9)
        team.paragraph_format.keep_with_next = True
        expected.append(team.text)
    footer = section.footer.paragraphs[0]
    doc.styles['Footer'].font.name, doc.styles['Footer'].font.size = 'Inter', Pt(9)
    doc.styles['Footer'].paragraph_format.tab_stops.clear_all()
    footer.paragraph_format.tab_stops.add_tab_stop(Cm(BODY_WIDTH), WD_TAB_ALIGNMENT.RIGHT)
    footer.add_run(view['title'] + '\t').font.size = Pt(9)
    page_field(footer, 'PAGE')
    footer.add_run(' / ').font.size = Pt(9)
    page_field(footer, 'NUMPAGES')
    urls = []
    for group in view['sections']:
        if group['title'].casefold().strip() == 'приложения':
            doc.add_section(WD_SECTION_START.NEW_PAGE)
        doc.add_paragraph(group['title'], 'Heading 1')
        expected.append(group['title'])
        blocks = group['blocks']
        # Короткий раздел переносится целиком. Большой текст остается делимым.
        keep_section = (all(block['kind'] == 'paragraph' for block in blocks)
                        and sum(row_height([block['rows'][0][0]['text']], [BODY_WIDTH - 1])
                                for block in blocks) <= 120)
        for index, block in enumerate(blocks):
            if block['kind'] == 'paragraph':
                paragraph = doc.add_paragraph(style=('List Bullet', 'List Bullet 2', 'List Bullet 3')[block.get('list_level', 0)])
                value = block['rows'][0][0]
                paragraph.paragraph_format.keep_together = row_height([value['text']], [BODY_WIDTH - 1]) <= 120
                paragraph.paragraph_format.keep_with_next = keep_section and index < len(blocks) - 1
                write_text(paragraph, value)
                expected.append(value['text'])
                if block.get('publication'):
                    paragraph.add_run(' ')
                    add_link(paragraph, block['publication']['url'])
                    expected[-1] += ' ссылка'
                    urls.append(block['publication']['url'])
            else:
                pages = note_standard.financial_pages(dict(headers=block['headers'], values=block['rows'],
                    origins=block['rows'], pivoted=True), columns=5)
                for number, page in enumerate(pages):
                    headers = list(page['headers'])
                    widths = column_widths(len(headers))
                    # Полная подпись слишком высокого столбца выносится в обычный
                    # абзац, который может переноситься между страницами.
                    for column, (header, width) in enumerate(zip(headers, widths), 1):
                        if row_height([header], [width]) <= PART_PT // 2:
                            continue
                        label = f'Столбец {column}'
                        explanation = f'{label}. {header}'
                        doc.add_paragraph(explanation)
                        expected.append(explanation)
                        headers[column - 1] = label
                    for part, values in enumerate(table_parts(headers, page['values'], widths)):
                        caption = block['caption'] + (' (продолжение)' if number or part else '')
                        add_financial_table(doc, headers, values, widths, caption)
                        expected.extend([caption, *headers])
                        expected.extend(value['text'] for row in values for value in row)
    embed_fonts(doc)
    doc.core_properties.title = view['title'][:note_renderer.DOCX_METADATA_TITLE_LIMIT]
    doc.core_properties.author = 'AIST'
    doc.core_properties.content_status = 'Черновик' if settings['release_mode'] == 'draft' else 'Принятая версия'
    doc.core_properties.version = str(view['revision'])
    path = directory / 'note.docx'
    doc.save(path)
    pdf = note_renderer.convert(path, directory)
    return note_renderer.verify(path, pdf, [], expected_paragraphs=expected,
                                font_name=('Inter', 'Manrope'), external_urls=urls, page_footer=view['title'])
