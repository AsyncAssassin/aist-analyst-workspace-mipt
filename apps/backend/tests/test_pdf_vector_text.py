"""Синтетические векторные подписи: без клиентского PDF и внешних шрифтов."""
from io import BytesIO
import math

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, FloatObject
import pytest

from app.pdf_worker import extract, PROCESSOR_VERSION
from pdf_fixtures import pdf_bytes


# Несимметричные глифы. B содержит квадратичную кривую и внутренний контур.
OUTLINES = {
    'A': [[(40, 0), (150, 700), (300, 700), (550, 0), (400, 0), (230, 500), (180, 0)]],
    'B': [[(50, 0), (50, 700), (450, 700), ('q', (700, 350), (450, 0))],
          [(200, 150), (350, 150), (350, 350), (200, 350)]],
    'C': [[(70, 0), (70, 680), (520, 680), (520, 540), (220, 540), (220, 160), (500, 160), (500, 0)]],
    '5': [[(30, 0), (470, 0), (470, 480), (170, 480), (170, 600), (500, 600),
           (500, 740), (30, 740), (30, 350), (320, 350), (320, 160), (30, 160)]],
}


def font_bytes(characters, mac_codes=False):
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(['.notdef', *characters.values()])
    builder.setupCharacterMap({ord(char): name for char, name in characters.items()})
    glyphs = {}
    for name in ['.notdef', *characters.values()]:
        pen = TTGlyphPen(None)
        for contour in OUTLINES.get(name, []):
            pen.moveTo(contour[0])
            for point in contour[1:]:
                if point[0] == 'q':
                    pen.qCurveTo(*point[1:])
                else:
                    pen.lineTo(point)
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (650, 0) for name in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'Synthetic', 'styleName': 'Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    if mac_codes:
        cmap = newTable('cmap')
        cmap.tableVersion = 0
        table = CmapSubtable.newSubtable(0)
        table.platformID, table.platEncID, table.language = 1, 0, 0
        table.cmap = {33 + i: name for i, name in enumerate(characters.values())}
        cmap.tables = [table]
        builder.font['cmap'] = cmap
    result = BytesIO()
    builder.save(result)
    return result.getvalue()


def vector_pdf(text='AB C5', *, pages=1, angle=0, curved=False, font_maps=None,
               mac_codes=False, damage=None, hidden=False, clipped=False, malformed_font=False):
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes(pages=pages))))
    page = writer.pages[-1]
    font_maps = font_maps if font_maps is not None else [{c: c for c in 'ABC5'}]
    for i, characters in enumerate(font_maps):
        program = DecodedStreamObject()
        program.set_data(b'broken font' if malformed_font else font_bytes(characters, mac_codes))
        descriptor = DictionaryObject({NameObject('/FontFile2'): writer._add_object(program)})
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/TrueType'),
            NameObject('/FontDescriptor'): descriptor})
        if mac_codes:
            mapping = DecodedStreamObject()
            pairs = '\n'.join(f'<{33 + n:02x}> <{ord(c):04x}>' for n, c in enumerate(characters))
            mapping.set_data(f'{len(characters)} beginbfchar\n{pairs}\nendbfchar'.encode())
            font[NameObject('/ToUnicode')] = writer._add_object(mapping)
        page['/Resources']['/Font'][NameObject(f'/Outline{i}')] = writer._add_object(font)
    commands = ['q']
    if hidden:
        page['/Resources'][NameObject('/ExtGState')] = DictionaryObject({
            NameObject('/Invisible'): DictionaryObject({NameObject('/ca'): FloatObject(0)})})
        commands.append('/Invisible gs')
    if clipped:
        commands.append('0 0 1 1 re W n')
    # Внешняя матрица плюс поворот каждого глифа, как в криволинейной подписи.
    radians = math.radians(angle)
    commands.append(f'{math.cos(radians):.8f} {math.sin(radians):.8f} {-math.sin(radians):.8f} {math.cos(radians):.8f} 220 300 cm')
    x, y = 0.0, 0.0
    letter_number = 0
    for char in text:
        if char == ' ':
            x += 6
            continue
        tilt = math.radians(letter_number * 4 if curved else 0)
        commands.append(f'q {0.02*math.cos(tilt):.8f} {0.02*math.sin(tilt):.8f} {-0.02*math.sin(tilt):.8f} {0.02*math.cos(tilt):.8f} {x:.8f} {y:.8f} cm')
        for contour in reversed(OUTLINES[char]):
            start = current = contour[0]
            commands.append(f'{start[0]} {start[1]} m')
            for point in contour[1:]:
                if point[0] == 'q':
                    control, end = point[1:]
                    first = tuple((a + 2*b) / 3 for a, b in zip(current, control))
                    second = tuple((a + 2*b) / 3 for a, b in zip(end, control))
                    commands.append(' '.join(map(str, (*first, *second, *end))) + ' c')
                    current = end
                else:
                    point = (point[0] + 80, point[1]) if damage == letter_number else point
                    commands.append(f'{point[0]} {point[1]} l')
                    current = point
            commands.append('h')
        commands.append('f* Q')
        x += 13 * math.cos(tilt)
        y += 13 * math.sin(tilt)
        letter_number += 1
    commands.append('Q')
    content = DecodedStreamObject()
    content.set_data(page.get_contents().get_data() + b'\n' + '\n'.join(commands).encode())
    page[NameObject('/Contents')] = writer._add_object(content)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.mark.parametrize('angle,pages,curved', [(0, 1, False), (65, 3, True), (-90, 2, False), (180, 1, True)])
def test_recovers_outline_words_at_arbitrary_positions_and_preserves_native_text(angle, pages, curved):
    data = vector_pdf(angle=angle, pages=pages, curved=curved)
    original = PdfReader(BytesIO(data))
    result = extract(data)
    assert result['processor_version'] == PROCESSOR_VERSION
    assert result['pages'][-1] == original.pages[-1].extract_text().rstrip('\n') + '\nAB C5'
    assert result['pages'][:-1] == [p.extract_text() for p in original.pages[:-1]]
    assert result['layouts'] == [p.extract_text(extraction_mode='layout') for p in original.pages]


def test_decodes_embedded_mac_cmap_through_pdf_unicode_mapping():
    result = extract(vector_pdf('ABC', font_maps=[{'Ж': 'A', 'Ю': 'B', 'Я': 'C'}], mac_codes=True))
    assert result['pages'][0].endswith('\nЖЮЯ')


def test_does_not_choose_between_homographs_but_uses_unambiguous_complete_word():
    fonts = [{'A': 'A', 'B': 'B'}, {'А': 'A'}]
    data = vector_pdf('AA', font_maps=fonts)
    assert extract(data)['pages'][0] == PdfReader(BytesIO(data)).pages[0].extract_text()
    assert extract(vector_pdf('AB', font_maps=fonts))['pages'][0].endswith('\nAB')


@pytest.mark.parametrize('damage', [0, 1, 2])
def test_unknown_glyph_does_not_produce_partial_word(damage):
    data = vector_pdf('ACA', damage=damage)
    assert extract(data)['pages'][0] == PdfReader(BytesIO(data)).pages[0].extract_text()


@pytest.mark.parametrize('options', [{'font_maps': []}, {'malformed_font': True}, {'hidden': True}, {'clipped': True}])
def test_unproved_or_invisible_paths_do_not_add_source_text(options):
    data = vector_pdf(**options)
    assert extract(data)['pages'][0] == PdfReader(BytesIO(data)).pages[0].extract_text()


def test_scanned_pages_have_no_invented_text():
    data = pdf_bytes()
    assert extract(data)['pages'] == [p.extract_text() for p in PdfReader(BytesIO(data)).pages]
    assert extract(pdf_bytes(scanned=True))['pages'] == ['', '']


def test_outlines_outside_visible_page_do_not_add_text():
    writer = PdfWriter(clone_from=PdfReader(BytesIO(vector_pdf('ABC'))))
    writer.pages[0].cropbox.upper_right = (100, 100)
    output = BytesIO()
    writer.write(output)
    data = output.getvalue()
    assert extract(data)['pages'][0] == PdfReader(BytesIO(data)).pages[0].extract_text()


def test_indirect_resources_and_font_descriptor_are_supported():
    writer = PdfWriter(clone_from=PdfReader(BytesIO(vector_pdf('ABC'))))
    page = writer.pages[0]
    resources = page['/Resources']
    fonts = resources['/Font']
    font = fonts['/Outline0']
    font[NameObject('/FontDescriptor')] = writer._add_object(font['/FontDescriptor'])
    resources[NameObject('/Font')] = writer._add_object(fonts)
    page[NameObject('/Resources')] = writer._add_object(resources)
    output = BytesIO()
    writer.write(output)
    assert extract(output.getvalue())['pages'][0].endswith('\nABC')


def test_empty_cover_keeps_page_numbers_but_blank_document_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    empty = BytesIO()
    writer.write(empty)
    assert extract(empty.getvalue()) == {'error': 'unsupported_source'}
    writer.add_page(PdfReader(BytesIO(pdf_bytes(pages=1))).pages[0])
    mixed = BytesIO()
    writer.write(mixed)
    pages = extract(mixed.getvalue())['pages']
    assert pages[0] == '' and 'Value 101.' in pages[1]
