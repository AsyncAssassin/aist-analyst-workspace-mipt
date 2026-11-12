"""Геометрия синтетических секторов, без клиентских названий и файлов."""
from copy import deepcopy
from io import BytesIO
import json

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, NumberObject

from app.errors import ApiError
from app.extraction import candidate, extraction_request
from app.pdf_worker import extract
from test_d14 import PIN
from test_pdf_vector_text import vector_pdf


LABELS = ('ABC', 'BCA', 'CAB', 'CBA')


def sector_pdf(*, count=3, pages=1, amounts=None, scale=1, shift=(0, 0), masks='split',
               multiline=False, extra_value=False, clipped=False, malformed=False, hidden_amounts=False,
               value_slots=None):
    """Две сегментированные маски перекрываются по площади внутри каждого сектора."""
    writer = PdfWriter(clone_from=PdfReader(BytesIO(vector_pdf(pages=pages))))
    page = writer.pages[-1]
    page.mediabox.upper_right = (950, 842)
    amounts = amounts or tuple(str(121 + 137 * i) for i in range(count))
    width, height = count * 140, 140
    objects = DictionaryObject()
    commands = ['BT /F1 12 Tf 50 760 Td (Synthetic issuer) Tj ET',
                f'q {scale} 0 0 {scale} {shift[0]} {shift[1]} cm']
    if clipped:
        commands.append('0 0 1 1 re W n')
    for layer in range(2):
        alpha = DecodedStreamObject()
        pixels = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                # PDF y направлен вверх, порядок пикселей изображения вниз.
                local_y = height - y - 1
                opaque = (10 <= x % 140 <= 125 and
                          (0 <= local_y <= 85 if layer == 0 else 75 <= local_y < 140))
                if masks == 'solid':
                    opaque = True
                elif masks == 'bridge' and layer == 1:
                    # Связывает два сектора нижнего изображения, сохраняя отдельный третий.
                    opaque = 75 <= local_y < 140 and (10 <= x <= 265 or 290 <= x <= 405)
                pixels[y * width + x] = 255 if opaque else 0
        alpha.set_data(b'broken' if malformed else bytes(pixels))
        alpha.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                      NameObject('/Width'): NumberObject(width), NameObject('/Height'): NumberObject(height),
                      NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
        image = DecodedStreamObject()
        image.set_data(bytes([160 + layer * 80]) * width * height)
        image.update({key: value for key, value in alpha.items() if key != '/Length'})
        image[NameObject('/SMask')] = writer._add_object(alpha)
        objects[NameObject(f'/Sector{layer}')] = writer._add_object(image)
        commands.append(f'q {width} 0 0 {height} 200 220 cm /Sector{layer} Do Q')
    page['/Resources'][NameObject('/XObject')] = objects
    if hidden_amounts:
        commands.append('3 Tr')
    for i, amount in enumerate(amounts):
        # Td меняет строку явно даже при общей вертикальной позиции сумм.
        slot = i if value_slots is None else value_slots[i]
        commands.append(f'BT /F1 12 Tf {230 + 140*slot} 260 Td ({amount}) Tj 0 -16 Td (million) Tj ET')
    commands.append('0 Tr')
    commands.append('BT /F1 12 Tf 50 180 Td (999 million outside chart) Tj ET')
    if extra_value:
        commands.append('BT /F1 12 Tf 230 282 Td (654) Tj ET')
    for i, label in enumerate(LABELS[:count]):
        data = PdfReader(BytesIO(vector_pdf(label))).pages[0].get_contents().get_data().split(b'\n', 1)[1]
        commands.append(f'q 1 0 0 1 {140*i} 0 cm\n' + data.decode() + '\nQ')
        if multiline and i == 0:
            commands.append('q 1 0 0 1 0 20 cm\n' + data.decode() + '\nQ')
    commands.append('Q')
    content = DecodedStreamObject()
    content.set_data('\n'.join(commands).encode())
    page[NameObject('/Contents')] = writer._add_object(content)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def linked_text(result, page=-1):
    lines = result['pages'][page].splitlines()
    return {tuple(lines[i-1] for i in region['label_lines']):
            tuple(lines[i-1] for i in region['content_lines']) for region in result['diagrams'][page]}


@pytest.mark.parametrize('count,pages,scale,shift', [(2, 1, 1, (0, 0)), (3, 4, .7, (25, 100)), (4, 2, 1.1, (-50, 20))])
def test_joins_overlapping_sector_layers_and_preserves_source(count, pages, scale, shift):
    data = sector_pdf(count=count, pages=pages, scale=scale, shift=shift)
    result = extract(data)
    original = PdfReader(BytesIO(data))
    assert linked_text(result) == {(label,): (str(121 + 137*i), 'million') for i, label in enumerate(LABELS[:count])}
    assert result['diagrams'][:-1] == [[] for _ in range(pages-1)]
    assert result['pages'][-1].startswith(original.pages[-1].extract_text())
    assert result['layouts'] == [p.extract_text(extraction_mode='layout') for p in original.pages]


def test_swapping_values_changes_links_without_a_label_or_number_dictionary():
    before = extract(sector_pdf())
    after = extract(sector_pdf(amounts=('395', '121', '258')))
    assert linked_text(before)[('ABC',)] == ('121', 'million')
    assert linked_text(after)[('ABC',)] == ('395', 'million')
    assert linked_text(after)[('BCA',)] == ('121', 'million')


def test_moving_values_with_identical_plain_text_changes_only_geometric_links():
    before = extract(sector_pdf())
    after = extract(sector_pdf(value_slots=(1, 2, 0)))
    assert before['pages'] == after['pages']
    assert linked_text(after) == {('ABC',): ('395', 'million'), ('BCA',): ('121', 'million'),
                                  ('CAB',): ('258', 'million')}
    def supplied(result):
        return extraction_request({1: result['pages'][0]}, 'Financing', diagrams={1: result['diagrams'][0]})
    assert supplied(before) != supplied(after)


def test_multiline_labels_and_multiple_values_remain_a_group_not_one_guessed_amount():
    result = extract(sector_pdf(multiline=True, extra_value=True))
    assert linked_text(result)[('ABC', 'ABC')] == ('121', 'million', '654')


@pytest.mark.parametrize('options', [{'masks': 'solid'}, {'clipped': True}, {'malformed': True}, {'hidden_amounts': True}])
def test_unsupported_or_unsegmented_images_do_not_create_links(options):
    result = extract(sector_pdf(**options))
    assert '121' in result['pages'][0]
    assert result['diagrams'] == [[]]


def test_overlap_bridging_different_sectors_is_ambiguous():
    result = extract(sector_pdf(masks='bridge'))
    assert linked_text(result) == {('CAB',): ('395', 'million')}


def test_extractor_gets_original_line_references_and_separate_geometry():
    result = extract(sector_pdf(pages=2))
    pages = dict(enumerate(result['pages'], 1))
    diagrams = dict(enumerate(result['diagrams'], 1))
    payload = extraction_request(pages, 'Financing', diagrams=diagrams)
    supplied = json.loads(payload['messages'][1]['content'])['pages']
    assert 'diagram_regions' not in supplied[0]
    assert supplied[1]['diagram_regions'] == result['diagrams'][1]
    assert [line['text'] for line in supplied[1]['lines']] == pages[2].splitlines()


@pytest.mark.parametrize('form', ['separate', 'issuer_first', 'joined'])
@pytest.mark.parametrize('foreign_values', [False, True])
def test_issuer_reference_does_not_merge_with_sector_values(form, foreign_values):
    result = extract(sector_pdf())
    pages = {1: result['pages'][0]}
    regions = result['diagrams'][0]
    region = regions[0]
    locators = [dict(page=1, line_start=line, line_end=line, area='Сектор диаграммы')
                for line in [*region['label_lines'], *region['content_lines'], 1]]
    if form == 'issuer_first':
        locators = [locators[-1], *locators[:-1]]
    elif form == 'joined':
        locators = [locators[0], dict(locators[-1], line_end=region['content_lines'][-1])]
    if foreign_values:
        locators.extend(dict(page=1, line_start=line, line_end=line, area='Примечание')
                        for line in regions[1]['content_lines'])
    item = dict(metric=None, subject='Synthetic issuer', reporting_basis='Не указано',
        nature='source_statement', period=dict(kind='unknown', label='Не указан', start=None, end=None),
        amount=None, value_text=None, basis=None, evidence=locators)
    original = deepcopy(item)
    if foreign_values:
        with pytest.raises(ApiError) as failure:
            candidate(PIN, pages, item, {1: result['layouts'][0]}, diagrams={1: regions})
        assert failure.value.skip_code == 'ambiguous_link'
    else:
        fact, _ = candidate(PIN, pages, item, {1: result['layouts'][0]}, diagrams={1: regions})
        assert fact['metric'] == 'ABC' and fact['amount'] is None and fact['source'] == PIN
        expected = ['\n'.join(pages[1].splitlines()[e['line_start']-1:e['line_end']]) for e in locators]
        assert [e['quote'] for e in fact['evidence'][:len(locators)]] == expected
    assert item == original


def test_downsampling_keeps_thin_transparent_gaps_between_sectors():
    from app.pdf_diagrams import mask_components
    width, height = 1024, 128
    alpha = DecodedStreamObject()
    alpha.set_data(bytes(0 if x in (341, 682) else 255 for y in range(height) for x in range(width)))
    alpha.update({NameObject('/Width'): NumberObject(width), NameObject('/Height'): NumberObject(height),
                  NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
    image = DictionaryObject({NameObject('/SMask'): alpha})
    mask = mask_components(image, (width, 0, 0, height, 0, 0))
    assert len(mask['components']) == 3
