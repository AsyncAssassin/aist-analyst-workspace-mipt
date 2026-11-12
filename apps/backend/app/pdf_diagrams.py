"""Связи текста в отдельных областях изображений PDF, без OCR и догадок по близости."""
from array import array
from collections import defaultdict
from itertools import chain
import math
import re

from pypdf.generic import ContentStream
from pypdf._text_extraction._layout_mode._fixed_width_page import (
    TextStateManager, recurse_to_target_op,
)


def text_with_image_placements(page):
    resources = page['/Resources']
    objects = resources['/XObject'] if '/XObject' in resources else {}
    if any(obj.get_object().get('/Subtype') == '/Form' for obj in objects.values()):
        return page.extract_text() or '', []
    placements, stack = [], []
    opacity, clipped, text_mode = 1, False, 0
    visible_text = True

    def visit(op, args, matrix, text_matrix):
        nonlocal opacity, clipped, text_mode, visible_text
        if op == b'q':
            stack.append((opacity, clipped, text_mode))
        elif op == b'Q' and stack:
            opacity, clipped, text_mode = stack.pop()
        elif op in (b'W', b'W*'):
            clipped = True
        elif op == b'gs':
            states = resources['/ExtGState'] if '/ExtGState' in resources else {}
            state = states.get(args[0])
            if state is not None:
                opacity = float(state.get_object().get('/ca', opacity))
        elif op == b'Tr':
            text_mode = int(args[0])
        elif op in (b'Tj', b'TJ', b"'", b'"'):
            # Layout не учитывает видимость текста. Для таких страниц безопасно
            # сохранить строки без геометрических утверждений о суммах.
            if clipped or opacity != 1 or text_mode != 0:
                visible_text = False
        elif op == b'Do' and not clipped and opacity == 1:
            obj = objects.get(args[0])
            if obj is not None:
                image = obj.get_object()
                if image.get('/Subtype') == '/Image' and '/SMask' in image:
                    placements.append((image, tuple(matrix)))

    text = page.extract_text(visitor_operand_before=visit) or ''
    return text, placements if visible_text else []


def mask_components(image, matrix):
    """Дискретизация только непрозрачной части явной восьмибитной маски PDF."""
    if '/SMask' not in image:
        return None
    mask = image['/SMask']
    width, height = int(mask.get('/Width', 0)), int(mask.get('/Height', 0))
    if (mask.get('/ColorSpace') != '/DeviceGray' or mask.get('/BitsPerComponent') != 8
            or width < 1 or height < 1 or width * height > 4 * 1024 * 1024):
        return None
    decode = list(mask.get('/Decode', [0, 1]))
    if decode not in ([0, 1], [1, 0]):
        return None
    pixels = mask.get_data()
    if len(pixels) != width * height:
        return None
    cols, rows = min(width, 256), min(height, 256)
    cells = array('i', [0]) * (cols * rows)
    for y in range(rows):
        for x in range(cols):
            # Вся ячейка должна быть непрозрачной. Выбор одного пикселя мог бы
            # пропустить тонкий зазор и ошибочно соединить соседние сектора.
            left, right = x * width // cols, math.ceil((x + 1) * width / cols)
            top, bottom = y * height // rows, math.ceil((y + 1) * height / rows)
            opaque = all(
                min(pixels[row * width + left:row * width + right]) >= 250 if decode == [0, 1]
                else max(pixels[row * width + left:row * width + right]) <= 5
                for row in range(top, bottom))
            cells[y * cols + x] = -1 if opaque else 0
    components = []
    for start, value in enumerate(cells):
        if value != -1:
            continue
        number = len(components) + 1
        pending, component = [start], []
        cells[start] = number
        while pending:
            pos = pending.pop()
            component.append(pos)
            x, y = pos % cols, pos // cols
            neighbors = []
            if x: neighbors.append(pos - 1)
            if x + 1 < cols: neighbors.append(pos + 1)
            if y: neighbors.append(pos - cols)
            if y + 1 < rows: neighbors.append(pos + cols)
            for neighbor in neighbors:
                if cells[neighbor] == -1:
                    cells[neighbor] = number
                    pending.append(neighbor)
        components.append(component)
        if len(components) > 64:
            return None
    # Сплошной фон или свечение не должны объединять разные сектора.
    if not 2 <= len(components) <= 64:
        return None
    if any(len(c) < max(8, cols * rows * .002) for c in components):
        return None
    a, b, c, d, e, f = map(float, matrix)
    if not all(math.isfinite(v) for v in (a, b, c, d, e, f)) or abs(a*d - b*c) < 1e-8:
        return None
    return {'matrix': (a, b, c, d, e, f), 'cols': cols, 'rows': rows,
            'cells': cells, 'components': components}


def page_point(mask, pos):
    x = (pos % mask['cols'] + .5) / mask['cols']
    y = 1 - (pos // mask['cols'] + .5) / mask['rows']
    a, b, c, d, e, f = mask['matrix']
    return (a*x + c*y + e, b*x + d*y + f)


def component_at(mask, point):
    a, b, c, d, e, f = mask['matrix']
    x, y = point[0] - e, point[1] - f
    determinant = a*d - b*c
    u, v = (d*x - c*y) / determinant, (a*y - b*x) / determinant
    if not (0 <= u < 1 and 0 < v <= 1):
        return 0
    return mask['cells'][int((1-v) * mask['rows']) * mask['cols'] + int(u * mask['cols'])]


def connected_masks(placements):
    masks = []
    for image, matrix in placements:
        mask = mask_components(image, matrix)
        if mask is not None:
            masks.append(mask)
    if not masks or len(masks) > 16:
        return [], {}
    parents = {(i, j): (i, j) for i, m in enumerate(masks) for j in range(1, len(m['components']) + 1)}

    def root(key):
        while parents[key] != key:
            key = parents[key]
        return key

    for i, mask in enumerate(masks):
        for other_index in range(i):
            overlap = defaultdict(int)
            for number, positions in enumerate(mask['components'], 1):
                for pos in positions:
                    other = component_at(masks[other_index], page_point(mask, pos))
                    if other:
                        overlap[number, other] += 1
            # Требуется площадь пересечения, одного касания границ недостаточно.
            for (number, other), count in overlap.items():
                if count >= max(4, len(mask['components'][number-1]) * .01):
                    parents[root((i, number))] = root((other_index, other))
    roots = {key: root(key) for key in parents}
    # Наложение не должно склеивать разные сектора одного изображения.
    # В таком случае общая область неоднозначна для всех входящих в неё строк.
    members = defaultdict(list)
    for key, parent in roots.items():
        members[parent].append(key[0])
    ambiguous = {parent for parent, images in members.items() if len(images) != len(set(images))}
    roots = {key: None if parent in ambiguous else parent for key, parent in roots.items()}
    return masks, roots


def region_at(masks, roots, points):
    regions = set()
    for point in points:
        found = {roots[i, component] for i, mask in enumerate(masks)
                 if (component := component_at(mask, point)) > 0}
        if len(found) != 1:
            return None
        regions.update(found)
    return next(iter(regions)) if len(regions) == 1 else None


def native_lines(page, text):
    # Используем тот же разбор матриц/шрифтов, что и табличный layout pypdf,
    # до его сдвига всего текста к левому краю. Версия pypdf закреплена в lock.
    operations = ContentStream(page['/Contents'], page.pdf, 'bytes').operations
    groups, _ = recurse_to_target_op(iter(chain(operations, [([], b'Q')])),
        TextStateManager(), b'Q', page._layout_mode_fonts())
    runs = []
    crop_left, crop_bottom, crop_right, crop_top = map(float, page.cropbox)
    for group in groups:
        if not group['text'].strip() or group['flip_sort'] != 1:
            continue
        x, y, right, height = (float(group[k]) for k in ('tx', 'ty', 'displaced_tx', 'font_height'))
        if right <= x or height <= 0:
            continue
        if not (crop_left <= x <= right <= crop_right and crop_bottom <= y <= y + height <= crop_top):
            continue
        if (runs and abs(runs[-1]['y'] - y) <= .1 * height
                and -.1 * height <= x - runs[-1]['bounds'][2] <= .8 * height):
            runs[-1]['text'] += group['text']
            runs[-1]['bounds'][2] = right
            runs[-1]['bounds'][3] = max(runs[-1]['bounds'][3], y + height)
        else:
            runs.append({'text': group['text'], 'y': y, 'bounds': [x, y, right, y + height]})
    by_text, by_line = defaultdict(list), defaultdict(list)
    for run in runs:
        by_text[re.sub(r'\s+', '', run['text'])].append(run)
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            by_line[re.sub(r'\s+', '', line)].append(number)
    result = []
    for value, matches in by_text.items():
        # Повторы единиц сопоставляются в порядке исходного потока только при
        # полном совпадении числа вхождений; иначе положение строки неизвестно.
        if len(matches) == len(by_line[value]):
            result.extend({**run, 'line': number} for run, number in zip(matches, by_line[value]))
    return result


def diagram_regions(page, native_text, labels, placements):
    if not labels:
        return []
    masks, roots = connected_masks(placements)
    if not masks:
        return []
    regions = defaultdict(lambda: {'label_lines': [], 'content_lines': [], 'bounds': []})
    for label in labels:
        region = region_at(masks, roots, label['points'])
        if region is not None:
            regions[region]['label_lines'].append(label['line'])
            regions[region]['bounds'].append(label['bounds'])
    for run in native_lines(page, native_text):
        left, bottom, right, top = run['bounds']
        points = [(left + (right-left)*x/4, bottom + (top-bottom)*y/4) for x in range(5) for y in range(5)]
        region = region_at(masks, roots, points)
        if region in regions:
            regions[region]['content_lines'].append(run['line'])
            regions[region]['bounds'].append(run['bounds'])
    result = []
    for region in regions.values():
        if not region['label_lines'] or not region['content_lines']:
            continue
        bounds = region['bounds']
        result.append({'method': 'pdf-mask-components.v1',
            'label_lines': sorted(region['label_lines']), 'content_lines': sorted(region['content_lines']),
            'bounds': [round(min(b[0] for b in bounds), 3), round(min(b[1] for b in bounds), 3),
                       round(max(b[2] for b in bounds), 3), round(max(b[3] for b in bounds), 3)]})
    return result if len(result) <= 64 else []
