"""Whole-page context from the existing lexical matcher and saved PDF text.

This deliberately narrow selector does not infer arbitrary PDF relations. An uncovered query term,
an ambiguous continuation or a broken explicit page link retains the full first context.
"""
import math
import re
from itertools import islice

from .facts import lexical_page_matches
from .tables import page_tables

VERSION = 'pdf-context.v5'
QUALIFICATION = ('Охват перечисляет переданные страницы, а не полноту сведений. Частичный или пустой ответ '
                 'не доказывает отсутствие сведений вне охвата или отсутствие пропущенных кандидатов внутри него.')
STOP_WORDS = set('и или за на по в из для о об the and of in for за все компании компания показатели показатель'.split())
SUPPORT = re.compile(r'мсфо|рсбу|ifrs|gaap|консолидированн|примечани|сноск|оговорк|ограничени|disclaimer|footnote|'
                     r'продолжени[ея]|continued|окончание таблицы|источник:|источники:|source:', re.I)
# Reject a letter continuing the page label, but keep an immediately following page number.
PAGE_LINK = re.compile(r'(?:см\.?\s*(?:также\s*)?(?:страниц[аыуе]?|стр\.?|с\.)|see\s+page)(?![^\W\d_])\s*'
                       r'(\d*)(?:[ \t]*-[ \t]*(\d*))?', re.I)
CONTINUATION = re.compile(r'продолжени[ея]|continued|окончание таблицы', re.I)
FOOTNOTE = re.compile(r'^\s*(?:\(\d+\)|\d+[).]|\*+)\s+\S', re.M)


# Headings guide the two extractions. A match does not grant a fact or establish a gap.
ASO_HEADINGS = {
    'Описание возможности': r'инвестиционная возможность|investment opportunity',
    'Описание Компании': r'бизнес|компани[яию]|продукт|стратеги|финансовые результаты|отч[её]т о|business overview|financial results',
    'Рынок и конкуренты': r'рын(?:ок|к[аеу])\b|конкурент|market overview|market share|competit',
    'Акционеры': r'акционер[ыао]|структура (?:капитала|собственности)|sharehold|ownership',
    'Инвест. тезис': r'инвестиционн(?:ой привлекательности|ый тезис)|факторы привлекательности|investment (?:thesis|highlights)',
    'Описание Сделки': r'условия (?:сделки|размещения)|параметры (?:сделки|предложения)|transaction (?:terms|overview)|offering terms',
    'Потенциальные риски': r'риски|факторы риска|risk factors',
    'Следующие шаги': r'следующие шаги|next steps',
    'News Run': r'существенные события|ключевые события|recent developments|news run',
    'Терминология': r'глоссарий|терминологи|glossary|definitions',
    'Приложения': r'приложени[ея]|appendi[xc]',
}


# Standalone subheadings inside a slide or a multi-column teaser. Body sentences
# and isolated numbers do not establish that a section exists.
CAPTION_TOPICS = {
    caption: topic for topic, captions in {
        'Описание Компании': ('обзор бизнеса', 'описание компании', 'финансовые показатели', 'business overview', 'financial highlights'),
        'Рынок и конкуренты': ('обзор рынка', 'рыночные показатели', 'рынок и конкуренты', 'конкурентная среда', 'market overview', 'competitive landscape'),
        'Акционеры': ('акционеры', 'структура собственности', 'shareholders', 'ownership structure'),
        'Инвест. тезис': ('инвестиционная привлекательность', 'инвестиционный тезис', 'investment highlights', 'investment thesis'),
        'Описание Сделки': ('условия сделки', 'параметры размещения', 'transaction terms', 'offering terms'),
        'Потенциальные риски': ('риски', 'факторы риска', 'risk factors', 'key risks'),
        'Терминология': ('глоссарий', 'терминология', 'glossary', 'definitions'),
    }.items() for caption in captions
}


def outline_pages(rows):
    """Keep the title and topic-bearing pages across the document within the existing 40-page index."""
    first_pages = {}
    representatives = {}
    for row in rows:
        lines = (' '.join(line.split()) for line in row['lead'].splitlines()
                 if re.search(r'[^\W\d_]{2}', line))
        # PDF headings often wrap; a single line misses the end of the heading.
        heading = ' '.join(islice(lines, 3))[:180]
        if not heading:
            if not row['lead'].strip() and len(first_pages) < 40:
                first_pages[row['page']] = dict(page=row['page'], text_layer=False)
            continue
        entry = dict(page=row['page'], lead=heading[:100])
        topics = {name for name, pattern in ASO_HEADINGS.items() if re.search(pattern, heading, re.I)}
        # Layout preserves column gutters, including a heading opposite a long paragraph.
        for cell in re.split(r'\s{2,}|[\n\t]', row['lead']):
            topic = CAPTION_TOPICS.get(cell.strip().rstrip(':').casefold())
            if topic:
                topics.add(topic)
        if topics:
            entry['possible_sections'] = [name for name in ASO_HEADINGS if name in topics]
        if len(first_pages) < 40:
            first_pages[row['page']] = entry
        for topic in topics:
            representatives.setdefault(topic, entry)
    selected = {entry['page']: entry for entry in representatives.values()}
    for page, entry in first_pages.items():
        if len(selected) == 40:
            break
        selected.setdefault(page, entry)
    return [selected[page] for page in sorted(selected)]


def note_image_pages(pages, question, layouts=None):
    """Keep text coverage intact. Prioritise original slides for a broad thematic extraction."""
    if len(pages) <= 8 or not question:
        return list(pages)
    # Short stems match common inflections. Words present almost everywhere (issuer name,
    # footer, generic prompt terms) should not determine which image gets a slot.
    stems = {word[:6] for word in re.findall(r'[^\W\d_]{3,}', question.casefold()) if word not in STOP_WORDS}
    matched = lexical_page_matches(pages, stems)
    frequencies = {stem: sum(stem in hits for hits in matched.values()) for stem in stems}
    scores = {page: sum(math.log(len(pages) / frequencies[stem]) for stem in sorted(hits))
              for page, hits in matched.items()}
    ranked = sorted((p for p in pages if scores.get(p, 0) > 0), key=lambda p: (-scores[p], p))
    if not ranked:
        return list(pages)
    # No lexical score can identify the content of an image-only slide or one with
    # just a page number, footer or title. Keep these even beyond the normal image budget.
    required = {p for p, text in pages.items() if len(re.findall(r'[^\W\d_]{2,}', text)) < 12}
    from .collection import NOTE_TOPICS
    if layouts and question == NOTE_TOPICS[1]:
        # Whole-text ranking alone favours slides repeating the same words. Reserve
        # space for the first original slide of each applicable business section.
        representatives = {}
        outline = outline_pages([dict(page=p, lead=layouts.get(p) or text) for p, text in pages.items()])
        for slide in outline:
            if slide['page'] == min(pages) or re.match(r'ограничение ответственности|контакты', slide.get('lead', ''), re.I):
                continue
            for topic in slide.get('possible_sections', []):
                if topic not in ('Терминология', 'Приложения', 'News Run'):
                    lead = slide.get('lead', '').casefold()
                    explicit = any(label == topic and lead.startswith(caption) for caption, label in CAPTION_TOPICS.items())
                    candidate = (0 if explicit else 1, slide['page'])
                    representatives[topic] = min(candidate, representatives.get(topic, candidate))
        ranked = list(dict.fromkeys([*(page for _, page in representatives.values()), *ranked]))
    return sorted(required | set(ranked[:8]))


def select_pages(pages, question, layouts=None):
    tokens = list(dict.fromkeys(t for t in re.findall(r'\w+', question.casefold()) if len(t) > 2 and t not in STOP_WORDS))
    matches = lexical_page_matches(pages, tokens)
    reasons = {p: ['lexical: ' + ', '.join(tokens_found)] for p, tokens_found in matches.items()}
    for page, text in pages.items():
        if not text.strip():
            reasons[page] = ['На странице нет извлечённого текста. Содержимое доступно на изображении страницы.']
    missing = [t for t in tokens if not any(t in found for found in matches.values())]
    fallback = None
    unresolved = []
    if not matches or not tokens:
        fallback = 'Нет поисковых совпадений; отсутствие сведений не установлено.'
    elif missing:
        fallback = 'Не покрыты содержательные слова вопроса: ' + ', '.join(missing)
    # Supporting pages are kept even when the dependency is nonadjacent. Their text, units and original
    # line numbers travel intact. Table/card structures remain those of the canonical whole-page builder.
    for page, text in pages.items():
        if SUPPORT.search(text) or FOOTNOTE.search(text):
            reasons.setdefault(page, []).append('Основание отчетности, источник, оговорка, сноска или продолжение.')
        if CONTINUATION.search(text):
            if page - 1 in pages:
                reasons.setdefault(page - 1, []).append('Заголовок перед явным продолжением таблицы.')
            else:
                unresolved.append('Не разрешен заголовок продолжения таблицы.')
    if reasons and pages:
        reasons.setdefault(min(pages), []).append('Титульная страница для идентификации источника.')
    # Reuse the verified column structures of stage 03, without new geometric rules. Equal period
    # headers are a conservative reason to include the other table, not proof that its facts are comparable.
    headers = {page: {tuple(column['label'].casefold() for column in table['columns'])
                     for table in page_tables(text.splitlines(), (layouts or {}).get(page) or '')}
               for page, text in pages.items()}
    pending = list(reasons)
    visited = set()
    while pending:
        page = pending.pop()
        if page in visited:
            continue
        visited.add(page)
        for related, labels in headers.items():
            if related not in reasons and headers[page] & labels:
                reasons[related] = ['Таблица с теми же заголовками периодов, что на странице ' + str(page) + '.']
                pending.append(related)
        for link in PAGE_LINK.finditer(pages[page]):
            first, last = link.groups()
            start = int(first) if first else 0
            end = start
            if last is not None:
                end = int(last) if last else 0
            # Validate the whole range before adding any page, and never walk beyond the document.
            if not pages or not min(pages) <= start <= end <= max(pages) \
                    or any(target not in pages for target in range(start, end + 1)):
                unresolved.append('Не разрешена явная ссылка «' + link.group() + '».')
                continue
            for target in range(start, end + 1):
                if target not in reasons:
                    pending.append(target)
                reasons.setdefault(target, []).append('Явная ссылка со страницы ' + str(page) + '.')
    if unresolved:
        fallback = ' '.join(unresolved)
    if fallback:
        for page in pages:
            reasons.setdefault(page, []).append('Полный контекст: ' + fallback)
    selected = {page: pages[page] for page in sorted(reasons)}
    return selected, dict(context_version=VERSION, pages=list(selected), document_pages=sorted(pages),
                         full_document=set(selected) == set(pages), reasons=[dict(page=p, reasons=reasons[p]) for p in sorted(reasons)],
                         fallback=fallback, unresolved=unresolved, qualification=QUALIFICATION)
