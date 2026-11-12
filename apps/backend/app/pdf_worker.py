"""Ограниченный синхронный subprocess парсера, без OCR и внешних обращений."""
import io
import json
import logging
import resource
import sys
import subprocess
import tempfile
from pathlib import Path
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from .pdf_vector_text import recover_labels
from .pdf_diagrams import diagram_regions, text_with_image_placements

MAX_BYTES = 16 * 1024 * 1024
MAX_PAGES = 40
PROCESSOR_VERSION = 'pypdf-6.19.0-text-v6'
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_PAGE_TEXT = 200_000


def has_visible_page(data):
    # Only a PDF without any text needs this check. Thumbnails contain no extracted facts.
    with tempfile.TemporaryDirectory(prefix='aist-pdf-ink-') as temp:
        prefix = str(Path(temp) / 'thumb')
        rendered = subprocess.run(
            ['pdftoppm', '-gray', '-scale-to', '128', '-', prefix], input=data,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if rendered.returncode:
            return False
        for image in Path(temp).glob('thumb-*.pgm'):
            pixels = image.read_bytes().split(b'\n', 3)[-1]
            if any(shade < 245 for shade in pixels):
                return True
    return False


def dense_page_text(data, number, mode):
    # В больших векторных иллюстрациях сотни тысяч команд рисования.
    # Poppler читает текстовый слой без построения Python-объекта на каждую команду.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.run(
            ['/usr/bin/pdftotext', '-f', str(number), '-l', str(number), mode, '-nopgbrk', '-enc', 'UTF-8', '-', '-'],
            input=data, stdout=output, stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        if output.tell() > 4 * MAX_PAGE_TEXT:
            return None
        if proc.returncode:
            raise ValueError('PDF page could not be read')
        output.seek(0)
        raw = output.read(4 * MAX_PAGE_TEXT + 1)
    if len(raw) > 4 * MAX_PAGE_TEXT:
        return None
    text = raw.decode('utf-8')
    return text if len(text) <= MAX_PAGE_TEXT else None


def extract(data):
    if len(data) > MAX_BYTES:
        return {'error': 'too_large'}
    if not data.startswith(b'%PDF-'):
        return {'error': 'unsupported_source'}
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted and not reader.decrypt(''):
        return {'error': 'unsupported_source'}
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        return {'error': 'too_large'}
    pages, layouts, diagrams = [], [], []
    font_cache = {}
    for number, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is None:
            pages.append('')
            layouts.append('')
            diagrams.append([])
            continue
        stream_size = len(content.get_data())
        if stream_size > MAX_CONTENT_BYTES:
            if stream_size > MAX_BYTES:
                return {'error': 'too_large'}
            del content
            try:
                value = dense_page_text(data, number, '-raw')
                layout = dense_page_text(data, number, '-layout')
            except subprocess.TimeoutExpired:
                return {'error': 'too_large'}
            if value is None or layout is None:
                return {'error': 'too_large'}
            if '\x00' in value:
                return {'error': 'unsupported_source'}
            pages.append(value if value.strip() else '')
            layouts.append(layout)
            diagrams.append([])
            continue
        try:
            value, placements = text_with_image_placements(page)
        except (ValueError, TypeError, KeyError, IndexError, NotImplementedError, PdfReadError):
            value, placements = page.extract_text() or '', []
        if '\x00' in value:
            return {'error': 'unsupported_source'}
        labels = recover_labels(page, font_cache)
        native_text = value
        if labels:
            # Исходный текст и его порядок сохраняются. Дополняем только тем,
            # что доказано контурами встроенных глифов. Геометрия хранится отдельно.
            value += '' if value.endswith('\n') else '\n'
            for number, label in enumerate(labels, len(value.splitlines()) + 1):
                label['line'] = number
            value += '\n'.join(label['text'] for label in labels)
        try:
            regions = diagram_regions(page, native_text, labels, placements)
        except (ValueError, TypeError, KeyError, IndexError, NotImplementedError, PdfReadError):
            # Неизвестная графика не должна лишать документ исходного текста.
            regions = []
        diagrams.append(regions if len(json.dumps(regions)) <= 100000 else [])
        if len(value) > 200000:
            return {'error': 'too_large'}
        pages.append(value if value.strip() else '')
        layout = page.extract_text(extraction_mode='layout') or ''
        if len(layout) > 200000:
            return {'error': 'too_large'}
        layouts.append(layout)
    if not any(pages) and not has_visible_page(data):
        return {'error': 'unsupported_source'}
    return {'pages': pages, 'layouts': layouts, 'diagrams': diagrams, 'processor_version': PROCESSOR_VERSION}


def main():
    logging.disable(logging.CRITICAL)
    # При исчерпании cgroup отказ должен затронуть парсер одного файла, а не HTTP-сервис.
    try:
        Path('/proc/self/oom_score_adj').write_text('1000')
    except OSError:
        pass
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * MAX_PAGE_TEXT + 1,) * 2)
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    try:
        result = extract(sys.stdin.buffer.read(MAX_BYTES + 1))
    except MemoryError:
        result = {'error': 'too_large'}
    except Exception:
        result = {'error': 'unsupported_source'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
