"""Однократный сбор News Run и перенос первоисточников в неизменяемую записку."""

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from http.client import HTTPException
from jsonschema import ValidationError

from .http_io import internal_json
from .contracts import validate_definition
from .domain import refuse
from .normalization import checked, fingerprint
from .news_identity import company_sector
from .product_date import moscow_date, run_as_of
from .runs import execute, json_value

# Явная идентификация на обложке, в реквизитах или в представлении эмитента.
# Упоминание клиента в середине страницы и имя файла для этого недостаточны.
ENTITY = re.compile(
    r'(?<!\w)(?:МКПАО|МКАО|ПАО|АО|ООО|МФК|Публичное акционерное общество|Лизинговая компания)'
    r'\s+[«"](?:ЛК\s+[«"])?(?P<name>[А-Яа-яЁёA-Za-z0-9][А-Яа-яЁёA-Za-z0-9 .-]{1,90})[»"]',
    re.I,
)
ISSUER_LABEL = re.compile(r'(?:подготовлена?|эмитент|наименование компании) ?[:\-]? ?', re.I)
ENTITY_DESCRIPTION = re.compile(r' ?[-–—] ')
LLC_SUFFIX = re.compile(r'\bООО$', re.I)
KNOWN = {
    'henderson': ('HENDERSON', '5047265516'),
    'займер': ('Займер', '5406836941'),
    'совкомбанк': ('Совкомбанк', '4401116480'),
    'европлан': ('Европлан', '9705101614'),
    'промомед': ('ПРОМОМЕД', '9702034818'),
}


def cover_brand(page, filename):
    """A filename is only a hint; the cover must identify its own subject."""
    text = ' '.join(page.split())
    if re.search(r'обзор конкурентов|сравнение (?:компаний|конкурентов)|сравнительный обзор', text[:180], re.I):
        return set()
    text = re.split(r'\b(?:основные конкуренты|сравнение с конкурентами|обзор конкурентов)\b', text, maxsplit=1, flags=re.I)[0]
    proper = r'[А-ЯЁA-Z][А-Яа-яЁёA-Za-z0-9-]{1,40}(?: [А-Яа-яЁёA-Za-z0-9-]{2,30}){0,3}?'
    heading = re.match(
        rf'(?:(?i:о компании)\s+)?({proper})[!®]?\s+(?:(?i:инвестиционный тизер|investment teaser)|[—–-]\s+'
        r'(?:это )?(?:быстрорастущая |российская |крупнейшая |ведущая |федеральная )?'
        r'(?:сеть|компания|группа|производитель))', text)
    brands = {heading[1].strip()} if heading else set()
    self_definition = re.compile(
        r'(?<!\w)([А-ЯЁA-Z][А-Яа-яЁёA-Za-z0-9-]{2,40})\s*[-–—]\s*это\s+'
        r'(?:[а-яё-]+\s+){0,5}(?:компания|группа|сеть|производитель)\b')
    for match in self_definition.finditer(text):
        before = text[max(0, match.start() - 60):match.start()]
        if not re.search(r'(?:конкурент|партн[её]р|клиент|например)\w*[^.!?]{0,30}$', before, re.I):
            brands.add(match[1])
    actions = r'(открыл[аи]?|привлекает|развивает|производит|управляет|специализируется)\b'
    statements = {}
    for match in re.finditer(r'(?<!\w)([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9-]{2,40})\s+' + actions, text):
        before = text[max(0, match.start() - 60):match.start()]
        if re.search(r'(?:конкурент|партн[её]р|клиент|например)\w*[^.!?]{0,30}$', before, re.I):
            continue
        statements.setdefault(match[1], set()).add(match[2].casefold())
    profile = re.search(r'описание компании|обзор бизнеса|бизнес[ -]модель|о компании|федеральная сеть', text, re.I)
    stem = re.split(r'[_·]|\s+[-–—]\s+', Path(filename).stem)[0].strip(' !').casefold()
    # An imprecise filename can corroborate a spelling in the PDF, never supply it.
    normalized_stem = re.sub(r'([a-z])\1', r'\1', stem)
    lines = [line.strip(' !®') for line in page.splitlines() if line.strip()]
    if (lines and lines[0].casefold() == stem and re.fullmatch(r'[A-Za-zА-Яа-яЁё0-9 -]{2,65}', lines[0])
            and re.search(r'презентация (?:для )?инвестор|инвестиционный тизер|финансовые результаты|'
                          r'investor presentation|financial results', text[:250], re.I)):
        brands.add(lines[0])
    for subject, verbs in statements.items():
        if len(verbs) >= 2 and (profile or subject.casefold() in (stem, normalized_stem)):
            brands.add(subject)
    generic = {'компания', 'группа', 'она', 'они', 'банк', 'сеть', 'эмитент', 'бизнес', 'ритейлер', 'предприятие'}
    return {brand for brand in brands if brand.casefold() not in generic}


def identify(pages, filename=''):
    found = set()
    issuers = set()
    needs_inn = False
    for index, page in enumerate(pages):
        text = ' '.join(page.split())
        if re.search(r'\bПАО ЭЙЧ ЭФ ДЖИ\s*\([«"]Компания[»"]\)', text, re.I):
            found.add('HENDERSON')
        prospectus = re.search(
            r'акций\s*(?:\([«"]Акции[»"]\)\s*)?Публичного акционерного общества\s*'
            r'«([^»]{2,90})»\s*\(ОГРН:\s*\d{13};\s*ИНН:\s*\d{10}\b', text, re.I)
        if prospectus:
            name = prospectus[1].strip()
            found.add(KNOWN.get(name.casefold(), (name, ''))[0])
        issuer_starts = {label.end() for label in ISSUER_LABEL.finditer(text)}
        matches = list(ENTITY.finditer(text))
        # У некоторых PDF заголовок «О нас» следует после чисел из инфографики.
        mentions = (Counter(match['name'].casefold() for match in matches)
                    if re.search(r'\bо нас\b|\bкто мы\b', text, re.I) else {})
        for match in matches:
            start = match.start()
            before_end = start - int(start > 0 and text[start - 1] == ' ')
            issuer_label = start in issuer_starts
            self_description = (mentions.get(match['name'].casefold(), 0) > 1
                                and ENTITY_DESCRIPTION.match(text, match.end()))
            if before_end > 4 and not issuer_label and not self_description:
                continue
            name = ' '.join(match['name'].split())
            if name.casefold() in KNOWN and (
                match[0].upper().startswith(('ООО ', 'АО '))
                or LLC_SUFFIX.search(text, max(0, before_end - 3), before_end)
            ):
                needs_inn = True
            if index > 0 and before_end <= 4 and name.casefold() not in KNOWN and not issuer_label:
                continue
            normalized = KNOWN.get(name.casefold(), (name, ''))[0]
            found.add(normalized)
            if issuer_label:
                issuers.add(normalized)
    # Don't transfer news from a brand to an unrelated legal entity on the same cover.
    brands = cover_brand(pages[0][:3500], filename) if pages else set()
    for page in pages[1:3]:
        if re.match(r'\s*(?:о компании|о нас|профиль компании|about us|company profile)\b', page, re.I):
            brands.update(cover_brand(page[:3500], filename))
    found.update(KNOWN.get(b.casefold(), (b, ''))[0] for b in brands)
    if len(found) != 1:
        return None
    name = found.pop()
    inns = set(re.findall(r'\bИНН\s*+[:№]?\s*+(\d{10})(?!\d)', ' '.join(pages), re.I))
    if len(inns) > 1:
        return None
    inn = next(iter(inns), '')
    if inn and sum(int(n) * w for n, w in zip(inn, [2, 4, 10, 3, 5, 9, 4, 6, 8])) % 11 % 10 != int(inn[-1]):
        return None
    if needs_inn and not inn:
        return None
    if name.casefold() in KNOWN and inn and inn != KNOWN[name.casefold()][1]:
        return None
    return (name, inn) if name.casefold() in KNOWN or inn or name in brands or name in issuers else None


def snapshot(status, date, company=''):
    return dict(
        status=status,
        company=company,
        as_of=date,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        items=[],
    )


def lookup(company, inn, as_of, sector=''):
    # Внешний процесс не имеет доступа к PDF, БД или ключу модели.
    url = os.getenv('AIST_NEWS_ORIGIN', 'http://news:8000').rstrip('/')
    query = urlencode(dict(name=company, inn=inn, as_of=as_of, sector=sector, expires=time.time() + 24))
    batch = internal_json(url + '/news?' + query, time.monotonic() + 24, 32_000)
    validate_definition('NewsRun', batch)
    if batch['company'] != company or batch['as_of'] != as_of:
        raise ValueError('news response scope')
    for item in batch['items']:
        url = urlsplit(item['url'])
        if (
            url.scheme != 'https'
            or url.netloc not in ('ir.zaymer.ru', 'raexpert.ru', 'europlan.ru', 'ir.henderson.ru', 'promomed.pro',
                                  'solopharm.com', 'www.interfax.ru', 'www.retail.ru')
            or item['published'] > as_of
        ):
            raise ValueError('news publication source')
    return batch


def prepare(runs, run, lookup_news=None):
    inp = run['input']
    if (
        inp['requested_result_type'] != 'analytical_note'
        or inp.get('base_version_id')
        or run.get('news_run') is not None
    ):
        return
    as_of = run_as_of(run)
    unavailable = snapshot('unavailable', as_of)
    with runs.engine.begin() as c:
        current = runs.locked(c, run['id'])
        runs.fence(c, current, run['epoch'])
        changed = execute(
            c,
            """UPDATE aist.runs SET news_run=CAST(:news AS jsonb)
            WHERE id=:id AND epoch=:epoch AND state='running' AND news_run IS NULL RETURNING id""",
            news=json_value(unavailable),
            id=run['id'],
            epoch=run['epoch'],
        ).first()
        if not changed:
            return
        materials = []
        for pin in inp['source_versions']:
            v = runs.service.scoped(
                c, run['owner_id'], run['project_id'], 'source_versions', pin['source_version_id']
            )
            if v['sha256'] != pin['sha256'] or v['media_type'] != 'application/pdf':
                materials.append(([], ''))
                continue
            pages = (
                execute(
                    c,
                    'SELECT text FROM aist.source_pages WHERE version_id=:v AND sha256=:s ORDER BY page',
                    v=v['id'],
                    s=pin['sha256'],
                )
                .scalars()
                .all()
            )
            materials.append((pages, v['original_filename']))
    # Версии источников неизменяемы; разбор текста не удерживает строку запуска.
    names = []
    sectors = set()
    for pages, filename in materials:
        identity = identify(pages, filename)
        names.append(identity)
        if identity:
            sectors.add(company_sector(pages, identity[0]))
    identities = set(names)
    if None in identities or len(identities) != 1:
        result = snapshot('unmatched', as_of)
    else:
        company, inn = identities.pop()
        try:
            sector = next(iter(sectors)) if len(sectors) == 1 else ''
            result = (lookup_news or lookup)(company, inn, as_of, sector)
        except (OSError, HTTPException, ValueError, ValidationError):
            result = snapshot('unavailable', as_of, company)
    with runs.engine.begin() as c:
        current = runs.locked(c, run['id'])
        runs.fence(c, current, run['epoch'])
        execute(
            c,
            """UPDATE aist.runs SET news_run=CAST(:news AS jsonb)
            WHERE id=:id AND epoch=:epoch AND state='running'""",
            news=json_value(result),
            id=run['id'],
            epoch=run['epoch'],
        )


def block_id(item):
    if 'date_quote' in item:
        return 'news_' + fingerprint(item)[:32]
    return 'news_' + fingerprint([item['url'], item['page_sha256'] or item.get('response_sha256')])[:32]


def legacy_news_text(item):
    text = item['quote']
    if 'date_quote' in item:
        if item['event_date'] and item['event_date'] != item['published']:
            text += ' (дата события: ' + date.fromisoformat(item['event_date']).strftime('%d.%m.%Y') + ')'
        published = date.fromisoformat(item['published']).strftime('%d.%m.%Y') if item['published'] else 'Дата публикации не установлена'
        return text + '\n' + published + ' · ' + item['publisher']
    if item.get('event_date') and item['event_date'] != item['published']:
        text += ' (дата события: ' + date.fromisoformat(item['event_date']).strftime('%d.%m.%Y') + ')'
    text += '\n' + date.fromisoformat(item['published']).strftime('%d.%m.%Y') + ' · ' + item['publisher']
    return text


def contains_date(text, value):
    if not value:
        return False
    day = date.fromisoformat(value)
    month = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
             'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')[day.month - 1]
    normalized = ' '.join(text.casefold().split())
    return any(form in normalized for form in (
        value, day.strftime('%d.%m.%Y'), f'{day.day} {month} {day.year}'))


def news_text(item):
    """Только событие и известная дата; URL и происхождение остаются структурированными."""
    text = item['quote']
    event_date = item.get('event_date')
    if event_date and event_date != item['published'] and not contains_date(text, event_date):
        text = date.fromisoformat(event_date).strftime('%d.%m.%Y') + ': ' + text
    if item['published'] and not contains_date(text, item['published']):
        text += '\n' + date.fromisoformat(item['published']).strftime('%d.%m.%Y')
    return text


def matches_news_text(text, item):
    # Точное совпадение старого серверного текста позволяет читать прежние версии.
    # Свободная редакторская формулировка не очищается по строковому шаблону.
    return text in (news_text(item), legacy_news_text(item))


def selected_snapshot(run, parent):
    if 'news_selection' in run:
        return run['news_selection']
    return (parent or {}).get('basis', {}).get('news_run')


def select_events(run, parent, events):
    """Сервер связывает описания с разрешенным текстом, но не проверяет смысл выбранной даты."""
    from .note_external import saved_context
    context = saved_context(run, parent) or {}
    available = {item['evidence_id']: item for item in context.get('items', [])}
    # A Run keeps its cutoff while waiting or resuming. A direct editorial save has no Run.
    as_of = run_as_of(run) if run.get('created_at') else moscow_date(datetime.now(timezone.utc)).isoformat()
    items = []
    if len(events) > 5:
        refuse('News Run допускает не более пяти событий.')
    for event in events:
        checked('NewsEvent', event)
        source = available.get(event['external_evidence_id'])
        if source is None:
            refuse('Событие требует разрешенное прочитанное внешнее основание.', 'resource_not_found')
        if event['date_quote'] not in source['quote']:
            refuse('Выдержка для даты отсутствует в сохраненном тексте основания.')
        if event['published'] and event['published'] > as_of:
            refuse('Дата публикации не может быть позднее даты составления.')
        item = dict(quote=event['description'], published=event['published'], event_date=event['event_date'],
                    date_quote=event['date_quote'], metadata_published=source['published'],
                    **{key: source[key] for key in ('url', 'publisher', 'page_sha256', 'retrieved_at') if key in source})
        item['external_evidence_id'] = source['evidence_id']
        item.update({key: source[key] for key in ('response_sha256', 'retrieval_kind', 'provider', 'provider_acquired_at') if key in source})
        items.append(checked('NewsItem', item))
    if len({block_id(item) for item in items}) != len(items):
        refuse('Повтор события News Run.')
    progress = context.get('news_search') or {}
    return checked('NewsRun', dict(status=('partial' if progress.get('failed') else 'found') if items else 'empty',
        company=context.get('company', ''), as_of=as_of,
        retrieved_at=context.get('retrieved_at', datetime.now(timezone.utc).isoformat()), items=items))


def replace_selection(payload, run, parent, events):
    """Явная новая подборка заменяет только блоки прежнего News Run в новой версии."""
    previous = {block_id(item) for item in (parent or {}).get('basis', {}).get('news_run', {}).get('items', [])}
    payload = deepcopy(payload)
    for section in payload['content']['sections']:
        section['blocks'] = [block for block in section['blocks'] if block['block_id'] not in previous]
    payload['content']['sections'] = [section for section in payload['content']['sections'] if section['blocks']]
    context = dict(run, news_selection=select_events(run, parent, events))
    include(payload, {}, context, parent)
    return payload, context


def include(payload, basis, run, parent):
    if payload['result_type'] != 'analytical_note':
        return
    inherited = parent['basis'].get('news_run') if parent else None
    refresh_empty = bool(run.get('add_legacy_news') and (run.get('news_run') or {}).get('items')
                         and inherited and not inherited['items'] and inherited['status'] in ('unmatched', 'unavailable'))
    if parent and inherited is None and not run.get('add_legacy_news') and 'news_selection' not in run:
        return
    if 'news_selection' in run:
        saved = run['news_selection']
    elif inherited is None or refresh_empty:
        saved = run.get('news_run')
    else:
        saved = inherited
    if saved is None:
        return
    basis['news_run'] = deepcopy(saved)
    # An edit keeps exactly the parent's selection, including intentional removals.
    # A legacy version can receive its first snapshot from a trusted server operation.
    if (inherited is not None and not refresh_empty and 'news_selection' not in run) or not saved['items']:
        return
    sections = payload['content']['sections']
    news = next(
        (s for s in sections if s['title'].strip().casefold() in ('news run', 'новости компании')), None
    )
    ids = {
        v
        for s in sections
        for v in [
            s['section_id'],
            *(b['block_id'] for b in s['blocks']),
            *(row['row_id'] for b in s['blocks'] for row in b.get('rows', [])),
        ]
    }
    external = []
    for item in saved['items']:
        # Совпадение заголовка без даты не доказывает тождества событий.
        if news and any(
            b['kind'] == 'paragraph' and (
                b['block_id'] == block_id(item) and b['claim'].get('origin') == 'public_news'
                or matches_news_text(b['claim']['text'], item)) for b in news['blocks']
        ):
            continue
        bid = block_id(item)
        if bid in ids:
            refuse('Повтор идентификатора публикации News Run.')
        if news is None:
            sid = 'section_news_' + fingerprint(saved)[:16]
            if sid in ids:
                refuse('Повтор идентификатора раздела News Run.')
            news = dict(section_id=sid, title='News Run', blocks=[])
            pos = next(
                (
                    i
                    for i, s in enumerate(sections)
                    if s['title'].casefold().strip() in ('приложения', 'терминология')
                ),
                len(sections),
            )
            sections.insert(pos, news)
        external.append(
            dict(
                kind='paragraph',
                block_id=bid,
                claim=dict(text=news_text(item), origin='public_news', fact_refs=[], calculation_ids=[]),
            )
        )
        ids.add(bid)
    if news:
        news['blocks'][:0] = external
