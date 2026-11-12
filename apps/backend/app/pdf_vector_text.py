"""Восстановление залитых контуров букв по шрифтам самого PDF, без OCR.

Принимаются только совпадения всех линий и контрольных точек кривых после
поворота, переноса и равномерного масштабирования. Неоднозначное слово
пропускается целиком. Словарей, внешних шрифтов и смысловых догадок нет.
"""
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
import math

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont, TTLibError
from pypdf._cmap import get_encoding


@dataclass
class Glyph:
    font_id: int
    text: str
    contours: tuple
    advance: float
    em: int


@dataclass
class Match:
    glyph: Glyph
    scale: complex
    origin: complex


class OutlinePen(BasePen):
    """BasePen раскрывает составные глифы и квадратичные сплайны TrueType."""

    def __init__(self, glyph_set):
        super().__init__(glyph_set)
        self.contours = []
        self.segments = []
        self.start = self.point = 0j

    def _moveTo(self, point):
        self.start = self.point = complex(*point)

    def _lineTo(self, point):
        end = complex(*point)
        self.segments.append((self.point, end))
        self.point = end

    def _curveToOne(self, first, second, end):
        self.segments.append((self.point, complex(*first), complex(*second), complex(*end)))
        self.point = complex(*end)

    def _qCurveToOne(self, control, end):
        control, end = complex(*control), complex(*end)
        self.segments.append((self.point, (self.point + 2 * control) / 3,
                              (end + 2 * control) / 3, end))
        self.point = end

    def _closePath(self):
        if abs(self.point - self.start) > 1e-8:
            self.segments.append((self.point, self.start))
        if self.segments:
            self.contours.append(tuple(self.segments))
        self.segments = []


def shape_key(contours):
    return tuple(sorted((sum(len(s) == 2 for s in c), sum(len(s) == 4 for s in c))
                        for c in contours))


def font_glyphs(font, font_id):
    # Ограниченная область: простые TrueType с однозначной таблицей кодов.
    # Остальные виды шрифтов продолжает обрабатывать обычный pypdf.
    if font.get('/Subtype') != '/TrueType':
        return []
    descriptor = font['/FontDescriptor'] if '/FontDescriptor' in font else None
    if not descriptor or '/FontFile2' not in descriptor:
        return []
    data = descriptor['/FontFile2'].get_data()
    if len(data) > 2 * 1024 * 1024:
        return []
    with TTFont(BytesIO(data)) as embedded:
        encoding, unicode_map = get_encoding(font)
        # В PDF без /Encoding коды символьного TrueType берутся из Mac cmap.
        # Для Unicode cmap используем собственные Unicode-коды шрифта.
        cmap = embedded.getBestCmap()
        if cmap is not None:
            characters = {code: chr(code) for code in cmap}
        else:
            tables = [t for t in embedded['cmap'].tables if (t.platformID, t.platEncID) == (1, 0)]
            if len(tables) != 1 or font.get('/Encoding') not in (None, '/MacRomanEncoding'):
                return []
            cmap = tables[0].cmap
            characters = {code: unicode_map.get(chr(code),
                          encoding.get(code) if isinstance(encoding, dict) else None) for code in cmap}
        if len(cmap) > 2048:
            return []
        glyph_set = embedded.getGlyphSet()
        result = []
        for code, name in cmap.items():
            text = characters[code]
            if not text or len(text) != 1 or not text.isalnum():
                continue
            pen = OutlinePen(glyph_set)
            glyph_set[name].draw(pen)
            contours = tuple(pen.contours)
            if contours and sum(map(len, contours)) <= 256:
                result.append(Glyph(font_id, text, contours, glyph_set[name].width,
                                    embedded['head'].unitsPerEm))
        return result


def contour_matches(path, glyph, scale, origin, tolerance):
    if len(path) != len(glyph):
        return False
    for offset in range(len(glyph)):
        shifted = glyph[offset:] + glyph[:offset]
        if all(len(a) == len(b) and all(abs(x - (y * scale + origin)) <= tolerance
               for x, y in zip(a, b)) for a, b in zip(path, shifted)):
            return True
    return False


def match_outline(contours, glyph):
    path = max(contours, key=len)
    for contour in glyph.contours:
        if len(contour) != len(path):
            continue
        for offset in range(len(contour)):
            shifted = contour[offset:] + contour[:offset]
            if any(len(a) != len(b) for a, b in zip(path, shifted)):
                continue
            source = [p for segment in shifted for p in segment]
            target = [p for segment in path for p in segment]
            farthest = max(range(len(source)), key=lambda i: abs(source[i] - source[0]))
            vector = source[farthest] - source[0]
            if abs(vector) < 1e-8:
                continue
            scale = (target[farthest] - target[0]) / vector
            if abs(scale) < 1e-8:
                continue
            origin = target[0] - source[0] * scale
            # Допуск только на округление координат экспортёром PDF.
            tolerance = abs(scale) * glyph.em * 0.0001
            if any(abs(x - (y * scale + origin)) > tolerance for x, y in zip(target, source)):
                continue
            remaining = list(glyph.contours)
            for part in contours:
                found = next((i for i, candidate in enumerate(remaining)
                              if contour_matches(part, candidate, scale, origin, tolerance)), None)
                if found is None:
                    break
                remaining.pop(found)
            else:
                return Match(glyph, scale, origin)
    return None


def filled_paths(page):
    """Контуры страницы в координатах PDF. Формы и растры не распознаются."""
    paths, contours, segments, stack = [], [], [], []
    matrix = (1, 0, 0, 1, 0, 0)
    opacity, clipped = 1, False
    start = point = 0j
    valid = True
    resources = page['/Resources']
    content = page.get_contents()
    if content is None:
        return paths
    for args, op in content.operations:
        if op == b'q':
            stack.append((matrix, opacity, clipped))
        elif op == b'Q':
            if stack:
                matrix, opacity, clipped = stack.pop()
        elif op == b'cm':
            a, b, c, d, e, f = map(float, args)
            x, y, z, w, u, v = matrix
            matrix = (a*x + b*z, a*y + b*w, c*x + d*z, c*y + d*w, e*x + f*z + u, e*y + f*w + v)
        elif op == b'gs':
            states = resources['/ExtGState'] if '/ExtGState' in resources else {}
            state = states.get(args[0])
            if state is not None:
                opacity = float(state.get_object().get('/ca', opacity))
        elif op in (b'm', b'l', b'c', b'v', b'y'):
            a, b, c, d, e, f = matrix
            points = [complex(a*float(args[i]) + c*float(args[i+1]) + e,
                              b*float(args[i]) + d*float(args[i+1]) + f)
                      for i in range(0, len(args), 2)]
            if op == b'm':
                if segments:
                    if abs(point - start) > 1e-8:
                        segments.append((point, start))
                    contours.append(tuple(segments))
                segments = []
                start = point = points[0]
            else:
                if op == b'v':
                    points.insert(0, point)
                elif op == b'y':
                    points.append(points[-1])
                segments.append((point, *points))
                point = points[-1]
        elif op == b'h':
            if abs(point - start) > 1e-8:
                segments.append((point, start))
            point = start
        elif op in (b're', b'W', b'W*'):
            valid = False
            if op in (b'W', b'W*'):
                clipped = True
        elif op in (b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'n', b'S', b's'):
            if segments:
                if abs(point - start) > 1e-8:
                    segments.append((point, start))
                contours.append(tuple(segments))
            if valid and not clipped and opacity > 0 and op in (b'f', b'F', b'f*') and contours:
                if sum(map(len, contours)) <= 256:
                    paths.append(tuple(contours))
            contours, segments, valid = [], [], True
    return paths


def word_text(letters):
    # Одинаковые контуры могут означать латинскую или кириллическую букву.
    # Выбираем только единственное прочтение целого слова в одном шрифте PDF.
    fonts = set.intersection(*({m.glyph.font_id for m in matches} for matches in letters))
    readings = {}
    for font in fonts:
        text = []
        for matches in letters:
            characters = {m.glyph.text for m in matches if m.glyph.font_id == font}
            if len(characters) != 1:
                break
            text.append(characters.pop())
        else:
            value = ''.join(text)
            readings[value] = [next(m for m in matches if m.glyph.font_id == font)
                               for matches in letters]
    return next(iter(readings.items())) if len(readings) == 1 else None


def recover_labels(page, cache):
    try:
        paths = filled_paths(page)
    except (KeyError, ValueError, IndexError, TypeError):
        # Повреждённая графика не отменяет текст, уже извлечённый pypdf.
        return []
    if not paths:
        return []
    index = defaultdict(list)
    resources = page['/Resources']
    fonts = resources['/Font'] if '/Font' in resources else {}
    for reference in fonts.values():
        font = reference.get_object()
        font_id = id(font)
        if font_id not in cache:
            try:
                cache[font_id] = font_glyphs(font, font_id)
            except (TTLibError, KeyError, ValueError, IndexError, TypeError, AttributeError):
                cache[font_id] = []
        for glyph in cache[font_id]:
            index[shape_key(glyph.contours)].append(glyph)
    lines, words, letters = [], [], []
    previous = None
    previous_center = None
    previous_matched = False
    box = page.cropbox
    for path in [*paths, None]:
        matches = [] if path is None else [match for glyph in index.get(shape_key(path), ())
                   if (match := match_outline(path, glyph)) is not None]
        points = [] if path is None else [p for contour in path for segment in contour for p in segment]
        if any(not (float(box.left) <= p.real <= float(box.right)
                    and float(box.bottom) <= p.imag <= float(box.top)) for p in points):
            matches = []
        center = ((complex(min(p.real for p in points), min(p.imag for p in points))
                   + complex(max(p.real for p in points), max(p.imag for p in points))) / 2
                  if points else None)
        current = matches[0] if matches else None
        reference = previous or current
        boundary = 'line'
        if center is not None and previous_center is not None and reference:
            step = (center - previous_center) / reference.scale
            ratio = current.scale / reference.scale if current else 1 + 0j
            em = reference.glyph.em
            angle = abs(math.atan2(ratio.imag, ratio.real))
            if (0 < step.real < 2.5 * em and abs(step.imag) < 0.5 * em
                    and 0.95 < abs(ratio) < 1.05 and angle < math.pi / 6):
                boundary = None
                if current and previous and previous_matched:
                    advance = ((current.origin - previous.origin) / previous.scale).real
                    if advance - previous.glyph.advance > 0.18 * em:
                        boundary = 'word'
        if boundary and letters:
            words.append(word_text(letters))
            letters = []
        if boundary == 'line' and words:
            # Не склеиваем части подписи через неизвестное или неоднозначное слово.
            if all(words):
                glyphs = [m for _, matches in words for m in matches]
                points = [p * m.scale + m.origin for m in glyphs
                          for contour in m.glyph.contours for segment in contour for p in segment]
                lines.append({'text': ' '.join(word for word, _ in words),
                    'points': [[p.real, p.imag] for p in points],
                    'bounds': [min(p.real for p in points), min(p.imag for p in points),
                               max(p.real for p in points), max(p.imag for p in points)]})
            words = []
        if path is not None:
            letters.append(matches)
        previous = current or (previous if boundary != 'line' else None)
        previous_center = center
        previous_matched = bool(matches)
    return lines
