"""Чтение датированных публикаций. Этот процесс получает только публичное имя или идентификатор."""
from contextvars import ContextVar

import json
import re
import time
from threading import Lock
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from functools import lru_cache
from http.client import HTTPException
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI
from lxml import etree, html
from .news_identity import SECTORS, article_identity, subject_pattern

ZAYMER = 'https://ir.zaymer.ru'
EXPERT = 'https://raexpert.ru'
EUROPLAN = 'https://europlan.ru'
HENDERSON = 'https://ir.henderson.ru'
PROMOMED = 'https://promomed.pro'
SOLO = 'https://solopharm.com'
INTERFAX = 'https://www.interfax.ru'
RETAIL = 'https://www.retail.ru'
PUBLISHERS = {
    SOLO: 'Solopharm',
    RETAIL: 'Retail.ru',
    INTERFAX: 'Интерфакс',
    ZAYMER: 'Займер, для инвесторов',
    EUROPLAN: 'Европлан',
    HENDERSON: 'HENDERSON, для инвесторов',
    EXPERT: 'Эксперт РА',
    PROMOMED: 'ПРОМОМЕД',
}
ISSUERS = {
    'Займер': (ZAYMER, '/news/company-news'),
    'Совкомбанк': (EXPERT, '/database/companies/sovkombank/'),
    'Европлан': (EUROPLAN, '/company/news'),
    'HENDERSON': (HENDERSON, '/sitemap-feeds.xml'),
    'ПРОМОМЕД': (PROMOMED, '/novosti/'),
}
MONTHS = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
NETWORK_ERRORS = (ValueError, OSError, HTTPException, etree.ParserError, etree.XMLSyntaxError)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def fetch(url, deadline, cookie=None, *, budget=None):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    search_field = {INTERFAX: 'phrase', RETAIL: 'q'}.get('https://' + parts.netloc)
    allowed_search = (parts.path == '/search/' and len(query) == 1 and query[0][0] == search_field
                      and re.fullmatch(r'[А-Яа-яЁёA-Za-z0-9][А-Яа-яЁёA-Za-z0-9 .-]{1,90}', query[0][1]))
    if (
        parts.scheme != 'https'
        or 'https://' + parts.netloc not in PUBLISHERS
        or (parts.query and not allowed_search and url != PROMOMED + '/novosti/?page=2')
        or parts.fragment
    ):
        raise ValueError('untrusted URL')
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; AIST-News/1.0)', 'Accept': 'text/html'}
    if cookie:
        headers['Cookie'] = 'database_companies_index=' + cookie
    from .public_web import read
    xml = bool(parts.netloc == 'ir.henderson.ru' and re.fullmatch(r'/sitemap-feed(?:s|-\d+)\.xml', parts.path))
    allowance = 1_500_000
    if budget is not None:
        with budget['lock']:
            allowance = min(allowance, budget['remaining'])
            budget['remaining'] -= allowance
        if allowance <= 0:
            raise ValueError('news byte budget')
    size = 0
    try:
        doc, sha, _, size = read(url, deadline, headers=headers, xml=xml, redirects=0, max_bytes=allowance)
        return doc, sha
    except NETWORK_ERRORS as error:
        size = getattr(error, 'bytes_read', 0)
        raise
    finally:
        if budget is not None:
            with budget['lock']:
                budget['remaining'] += allowance - size


def clean(value):
    return ' '.join(value.split())


def first(node, path):
    return clean(node.xpath('string((' + path + ')[1])'))


def day(value):
    match = re.search(r'\b(\d{1,2})\.(\d{2})\.(20\d{2})\b', value)
    if match:
        return date(int(match[3]), int(match[2]), int(match[1]))
    match = re.search(r'\b(\d{1,2}) (\w+) (20\d{2})\b', value.casefold())
    if match and match[2] in MONTHS:
        return date(int(match[3]), MONTHS.index(match[2]) + 1, int(match[1]))
    return None


def years_before(today, years):
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 февраля
        return today.replace(year=today.year - years, day=28)


def company_name(value):
    return clean(re.sub(r'[«»"“”]', ' ', value)).casefold()


def registered_card(name, inn, read):
    # Сайт получает только запрос на каталог и его публичную букву. Название и ИНН сверяются у нас.
    if not inn or not re.fullmatch(r'\d{10}', inn):
        return None
    directory = EXPERT + '/database/companies/'
    doc, _ = read(directory)
    letter = name[0].upper()
    if letter != 'А':
        token = next(
            (
                el.get('onclick', '')
                for el in doc.xpath('//span[@onclick]')
                if clean(el.text_content()) == letter
            ),
            '',
        )
        match = re.fullmatch(r"setDatabaseObjectsHash\('([\w=+/-]{1,150})'\);?", token)
        if not match:
            return None
        doc, _ = read(directory, match[1])
    paths = {
        el.get('href')
        for el in doc.xpath('//div[contains(@class,"database-search-object-list")]/a[@href]')
        if company_name(el.text_content()) == company_name(name)
        and re.fullmatch(r'/database/companies/[a-zA-Z0-9_-]+/?', el.get('href'))
    }
    if len(paths) != 1:
        return None
    path = paths.pop().rstrip('/') + '/'
    card, _ = read(EXPERT + path)
    actual = first(card, '//*[normalize-space(text())="ИНН"]/following-sibling::*[1]')
    return (EXPERT, path) if actual == inn else None


def superseded_dividend(headline, published, newer):
    # В одной ленте рекомендацию можно опустить, когда уже есть решение акционеров за тот же квартал.
    if not re.search(r'рекоменд', headline, re.I) or not re.search(r'дивиденд', headline, re.I):
        return False
    period = r'\b(IV|III|II|I|[1-4])\s+квартал(?:а)?\b(?:\s+(20\d{2}))?'
    previous = re.search(period, headline, re.I)
    if not previous:
        return False

    def year_for(match, stamp):
        if match[2]:
            return int(match[2])
        quarter = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}.get(match[1].upper())
        month = (quarter or int(match[1])) * 3
        end = date(stamp.year, month, monthrange(stamp.year, month)[1])
        return stamp.year if 0 <= (stamp - end).days <= 120 else None

    year = year_for(previous, published)
    if not year:
        return False
    for item in newer:
        title = item['quote']
        current = re.search(period, title, re.I)
        if (
            current
            and current[1].upper() == previous[1].upper()
            and year_for(current, date.fromisoformat(item['published'])) == year
            and re.search(r'акционер\w*.*утверд', title, re.I)
            and re.search(r'дивиденд', title, re.I)
            and 0 <= (date.fromisoformat(item['published']) - published).days <= 60
        ):
            return True
    return False


def topic(headline):
    if re.search(
        r'(?:telegram|телеграм|youtube)[ -]?канал|партн[её]р.{0,30}(?:турнир|забег|фестивал)|рассмотрит|обсудит|эфир|вебинар|день инвестора|купонн.{0,4} доход|запуске услуги|профинансировал приобретение|передал.{0,40}в лизинг|некредитный рейтинг|спецпредложени|приглаш|награду|отраслевая премия|региональный этап программы|запус[кт]\w*.{0,35}програм\w*.{0,60}для (?:медицинских работников|школьников|волонт[её]ров)',
        headline,
        re.I,
    ):
        return None
    for key, pattern in (
        (
            'ownership',
            r'присоедин|аквизиц|приобр[её]л[аи]? (?:компани|дол|банк)|закрыл[аи]? сделку|слияни|поглощ|смен[аи]л[аи]? собственника',
        ),
        (
            'performance',
            r'финансов.{0,15}результат|операцион.{0,16}(?:результат|показател)|выручк|чистая прибыль|чистой прибыли|новый бизнес вырос',
        ),
        ('capital', r'размест|закрыл.{0,12}книгу заявок|привл[её]к|облигац|выкуп'),
        ('dividend', r'дивиденд'),
        ('rating', r'кредитн.{0,12}рейтинг|рейтинг.{0,12}кредит'),
        ('registration', r'сертификат GMP|регистрационн.{0,8}удостоверени|зарегистрировала?.{0,55}препарат'),
        ('legal', r'суд[а-я ]{0,24}поддержал|отстаивает права|подал[аи]? иск'),
        ('business', r'расшир.{0,30}(?:выпуск|мощност|производств)|преодолел[аи]? рубеж.{0,30}магазин|обновил[аи]?.{0,30}станци|открыти[ея] магазин|темпы открытия|купил[аи]? сервис|расшир|запус[кт]|запуст|покуп|заключ|приобр'),
    ):
        if re.search(pattern, headline, re.I):
            return key
    return None


def select_news(items, as_of):
    ordered = sorted(items, key=lambda item: item['published'], reverse=True)
    fresh = [item for item in ordered if item['published'] >= years_before(as_of, 1).isoformat()]
    pool = fresh if len(fresh) >= 3 else ordered
    # Сначала последние события разных типов, затем второе событие по теме.
    result = []
    counts = {}
    for limit in (1, 2):
        for item in pool:
            key = topic(item['quote'])
            if item not in result and counts.get(key, 0) < limit and len(result) < 5:
                result.append(item)
                counts[key] = counts.get(key, 0) + 1
    return sorted(result, key=lambda item: item['published'], reverse=True)


def corporate_items(name, host, index, as_of, read):
    doc, _ = read(host + index)
    candidates = {}
    incomplete = False
    readable = False
    if host == EUROPLAN:
        listings = [doc]
        try:
            listings.append(read(host + index + '/tag/itogi-raboty')[0])
        except NETWORK_ERRORS:
            incomplete = True
        for listing in listings:
            for link in listing.xpath('//a[@class="ep-smititle"]'):
                path = link.get('href', '')
                stamp = day(first(link, 'following-sibling::*[@class="ep-smitime"][1]'))
                if re.fullmatch(r'/company/news/\d+', path) and stamp:
                    readable = True
                    if years_before(as_of, 2) <= stamp <= as_of and topic(clean(link.text_content())):
                        candidates[path] = stamp
    elif host == PROMOMED:
        listings = [doc]
        if index + '?page=2' in doc.xpath('//a/@href'):
            try:
                listings.append(read(host + index + '?page=2')[0])
            except NETWORK_ERRORS:
                incomplete = True
        for listing in listings:
            for link in listing.xpath('//a[@class="card-article__link"]'):
                path = link.get('href', '')
                stamp = day(first(link, './/time/@datetime'))
                if re.fullmatch(r'/novosti/[a-z0-9-]+/', path) and stamp:
                    readable = True
                    title = first(link, './/*[contains(@class,"title")]')
                    if years_before(as_of, 2) <= stamp <= as_of and topic(title):
                        candidates[path] = stamp
    else:
        paths = doc.xpath('//*[local-name()="loc"]/text()')
        feeds = [url for url in paths if re.fullmatch(re.escape(host) + r'/sitemap-feed-\d+\.xml', url)]
        if len(feeds) != 1:
            return [], True
        feed, _ = read(feeds[0])
        for entry in feed.xpath('//*[local-name()="url"]'):
            url = first(entry, '*[local-name()="loc"]')
            stamp = first(entry, '*[local-name()="lastmod"]')[:10]
            if re.fullmatch(re.escape(host) + r'/press-releases/tpost/[a-z0-9-]+', url) and re.fullmatch(
                r'20\d\d-\d\d-\d\d', stamp
            ):
                readable = True
                # lastmod нужен только для порядка чтения. Дату публикации берём из самой статьи.
                if not re.search(
                    r'kollektsiy|otkritie|obnovlennii-salon|nagradu|novii-salon|otkril.*salon', url
                ):
                    candidates[url[len(host) :]] = date.fromisoformat(stamp)

    def article(candidate):
        path, listed = candidate
        try:
            page, digest = read(host + path)
            title = first(page, '//h1')
            if host in (EUROPLAN, PROMOMED):
                date_path = '//*[contains(@class,"ep-article")]//time' if host == EUROPLAN else '//*[@class="grid__aside-date"]'
                stamp = day(first(page, date_path))
                if stamp != listed:
                    return None, True
            else:
                raw = first(page, '//meta[@itemprop="datePublished"]/@content')[:10]
                stamp = date.fromisoformat(raw)
            if not stamp or not years_before(as_of, 2) <= stamp <= as_of:
                return None, False
            subject = (
                re.escape(name) if host != HENDERSON else r'(?:HENDERSON|ХЭНДЕРСОН(?: ФЭШН ГРУПП)?|ЭЙЧ ЭФ ДЖИ)'
            )
            if (
                not topic(title)
                or len(title) > 350
                or not re.search(r'(?<!\w)' + subject + r'(?:[аеу]|ом)?(?!\w)', title, re.I)
            ):
                return None, False
            item = dict(
                quote=title,
                published=stamp.isoformat(),
                url=host + path,
                publisher=PUBLISHERS[host],
                page_sha256=digest,
            )
            return item, False
        except NETWORK_ERRORS:
            return None, True

    items = []
    candidates = sorted(candidates.items(), key=lambda p: p[1], reverse=True)[:9]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item, failed in pool.map(article, candidates):
            incomplete |= failed
            if item and not any(
                i['quote'] == item['quote'] and i['published'] == item['published'] for i in items
            ):
                items.append(item)
    return select_news(items, as_of), incomplete or not readable


def dated_items(name, as_of, host, index, read):
    if host in (EUROPLAN, HENDERSON, PROMOMED):
        return corporate_items(name, host, index, as_of, read)
    doc, _ = read(host + index)
    issuer_site = host == ZAYMER
    route = r'/news/company-news/post-\d+' if issuer_site else r'/releases/20\d{2}/[A-Za-z0-9_-]+'
    candidates = {}
    indexes = [doc]
    incomplete = False
    if issuer_site:
        # Первая страница IR содержит всего несколько публикаций. Читаем ещё две в том же бюджете.
        for n in (2, 3):
            path = index + '/' + str(n)
            if path not in doc.xpath('//a/@href'):
                break
            try:
                indexes.append(read(host + path)[0])
            except NETWORK_ERRORS:
                incomplete = True
                break
    readable = not issuer_site and bool(
        first(doc, '//*[normalize-space(text())="ИНН"]/following-sibling::*[1]')
    )
    for listing in indexes:
        for link in listing.xpath('//a[@href]'):
            path = link.get('href')
            if not re.fullmatch(route, path):
                continue
            if issuer_site:
                stamp = first(link, './/*[contains(@class,"_date_")]')
            else:
                label = clean(link.text_content())
                if re.fullmatch(r'\d{2}\.\d{2}\.20\d{2}', label):
                    stamp = label
                elif re.search(r'[«"„]' + re.escape(name) + r'[»"“]', label, re.I):
                    # Дата рэнкинга в заголовке не является датой его публикации.
                    stamp = first(link, '../*[contains(@class,"__time")]')
                else:
                    continue
            published = day(stamp)
            if published is None:
                continue
            readable = True
            label = first(link, './/*[contains(@class,"_title_")]') if issuer_site else ''
            if issuer_site and not topic(label):
                continue
            if years_before(as_of, 2) <= published <= as_of:
                candidates[path] = published
    items = []
    seen = set()
    for path, published in sorted(candidates.items(), key=lambda p: p[1], reverse=True)[:7]:
        if len(items) >= 3 and published < years_before(as_of, 1):
            break
        try:
            page, digest = read(host + path)
            headline = first(page, '//h1')
            stamp = first(
                page,
                (
                    '//*[contains(@class,"_date_")]'
                    if issuer_site
                    else '//*[contains(@class,"b-subheader__date")]'
                ),
            )
            if not 10 <= len(headline) <= 350 or day(stamp) != published:
                incomplete = True
                continue
            # Не присваиваем событие одноимённой компании, дочерней организации или клиенту.
            if issuer_site:
                subject = r'(?<!\w)' + re.escape(name) + r'(?:а|у|ом|е)?(?!\w)'
            else:
                # В рейтинговом релизе проверяем имя целиком. «Совкомбанк Лизинг» не равен «Совкомбанк».
                subject = r'[«"„]' + re.escape(name) + r'[»"“]'
            if not re.search(subject, headline, re.I):
                continue
            identity = (published, company_name(headline))
            if identity in seen or superseded_dividend(headline, published, items):
                continue
            seen.add(identity)
            items.append(
                dict(
                    quote=headline,
                    published=published.isoformat(),
                    url=host + path,
                    publisher='Займер, для инвесторов' if issuer_site else 'Эксперт РА',
                    page_sha256=digest,
                )
            )
            event_date = first(page, '//time[@itemprop="startDate"]/@datetime')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_date):
                try:
                    items[-1]['event_date'] = date.fromisoformat(event_date).isoformat()
                except ValueError:
                    pass
            if not issuer_site and re.search(r'ожидаемый кредитный рейтинг', headline, re.I):
                # При наличии окончательного рейтинга того же выпуска предварительный уже устарел.
                series = re.search(r'серии ([0-9А-ЯA-Z-]+)', headline, re.I)
                if series and any(
                    series[1] in i['quote'] and 'ожидаемый' not in i['quote'] for i in items[:-1]
                ):
                    items.pop()
        except NETWORK_ERRORS:
            incomplete = True
    return select_news(items, as_of), incomplete or not readable



def relevant_subject(title, name):
    # Company must be named in the event itself. General market summaries often list it elsewhere.
    return bool(topic(title) and re.search(subject_pattern(name), title, re.I)
                and not re.search(r'\b(?:конкурент[а-я]*|похожа на|по примеру)\s+[«"]?' + re.escape(name),
                                  title, re.I))


def press_items(name, as_of, host, read, inn='', sector=''):
    """Discover articles by public company name, verify each date and headline on its own page."""
    candidates = {}
    earliest = years_before(as_of, 2)
    incomplete = False
    if host == SOLO:
        doc, _ = read(host + '/ru')
        links = doc.xpath('//a[@class="swiper-slide" and starts-with(@href,"/about/news/")]')
        for link in links:
            short = first(link, './/span')
            if not re.fullmatch(r'\d\d/\d\d/\d\d',short):
                continue
            stamp = datetime.strptime(short,'%d/%m/%y').date()
            title = first(link, './/p')
            path = link.get('href','')
            if re.fullmatch(r'/about/news/[a-z0-9-]+',path) and relevant_subject(title, 'Solopharm'):
                candidates[path] = stamp
        readable = bool(links)
    elif host == INTERFAX:
        doc, _ = read(host + '/search/?' + urlencode({'phrase':name}))
        readable = bool(doc.xpath('//*[contains(@class,"sPageResult__total")]'))
        for link in doc.xpath('//div[@class="sPageResult"]//a[@href]'):
            path = link.get('href','')
            if not re.fullmatch(r'/(?:business|russia|world|digital)/\d+',path) or not relevant_subject(clean(link.text_content()),name):
                continue
            stamp = first(link, 'parent::*//time/@datetime')[:10]
            if re.fullmatch(r'20\d\d-\d\d-\d\d',stamp):
                candidates[path] = date.fromisoformat(stamp)
    else:
        doc, _ = read(host + '/search/?' + urlencode({'q':name}))
        links = doc.xpath('//a[@href]')
        readable = bool(doc.xpath('//*[contains(@class,"search")]'))
        # The search also returns the named company's card, which contains dated releases.
        card_names = {company_name(name), company_name('Группа ' + name)}
        cards = {a.get('href') for a in links if re.fullmatch(r'/rbc/tradingnetworks/[a-z0-9-]+/',a.get('href',''))
                 and (company_name(a.get('title','')) in card_names
                      or company_name(a.text_content()) in card_names)}
        if len(cards) == 1:
            try:
                directory, _ = read(host + cards.pop())
                links += directory.xpath('//article//a[@href]')
            except NETWORK_ERRORS:
                incomplete = True
        for link in links:
            path = link.get('href','')
            if not re.fullmatch(r'/news/[a-z0-9-]+/',path) or not relevant_subject(clean(link.text_content()),name):
                continue
            dated = re.search(r'-(\d{1,2})-([a-z]+)-(20\d{2})-\d+/$',path)
            months = 'yanvarya fevralya marta aprelya maya iyunya iyulya avgusta sentyabrya oktyabrya noyabrya dekabrya'.split()
            if dated and dated[2] in months:
                candidates[path] = date(int(dated[3]),months.index(dated[2])+1,int(dated[1]))
    candidates = [(path,stamp) for path,stamp in candidates.items() if earliest <= stamp <= as_of]

    def article(candidate):
        path, listed = candidate
        try:
            page, digest = read(host + path)
            title = first(page, '//h1')
            if not relevant_subject(title, 'Solopharm' if host == SOLO else name) or len(title) > 350:
                return None, False
            if host == RETAIL:
                metadata = [json.loads(el.text) for el in page.xpath('//script[@type="application/ld+json"]') if el.text]
                record = next((x for x in metadata if isinstance(x,dict) and x.get('@type')=='NewsArticle'
                               and x.get('mainEntityOfPage') == host + path and clean(x.get('headline','')) == title), {})
                stamp = record.get('datePublished','')[:10]
            else:
                stamp = first(page, '//*[@itemprop="datePublished"]/@content')[:10]
            body_path = ('//*[contains(@class,"contain__description-material")]'
                         if host == RETAIL else '//*[@itemprop="articleBody"]')
            article_body = first(page, body_path)
            if not article_body:
                return None, True
            subject = 'Solopharm' if host == SOLO else name
            if not article_identity(subject, inn, sector, article_body):
                return None, False
            if stamp != listed.isoformat():
                return None, True
            return dict(quote=title,published=stamp,url=host+path,publisher=PUBLISHERS[host],page_sha256=digest), False
        except NETWORK_ERRORS:
            return None, True

    items = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item, failed in pool.map(article, sorted(candidates,key=lambda c:c[1],reverse=True)[:5]):
            incomplete |= failed
            if item:
                items.append(item)
    return items, incomplete or not readable


def collect(name, inn, as_of, read, sector=''):
    items = []
    try:
        source = ISSUERS.get(name) or registered_card(name, inn, read)
        if source:
            items, partial = dated_items(name, as_of, *source, read)
        elif name:
            hosts = [INTERFAX, RETAIL]
            if name.casefold() in ('солофарм', 'solopharm'):
                hosts.insert(0, SOLO)
            def search(host):
                try:
                    return press_items(name, as_of, host, read, inn, sector)
                except NETWORK_ERRORS:
                    return [], True
            partial = False
            with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
                for articles, unavailable in pool.map(search, hosts):
                    items.extend(articles)
                    partial |= unavailable
            # Prefer the issuer to press coverage of the same headline.
            unique = {}
            for item in items:
                unique.setdefault((item['published'],company_name(item['quote'])),item)
            items = select_news(list(unique.values()), as_of)
        else:
            return dict(status='unmatched', company=name, as_of=as_of.isoformat(),
                        retrieved_at=datetime.now(timezone.utc).isoformat(), items=[])
        status = ('partial' if items else 'unavailable') if partial else ('found' if items else 'empty')
    except NETWORK_ERRORS:
        status = 'unavailable'
    return dict(
        status=status,
        company=name,
        as_of=as_of.isoformat(),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        items=items,
    )


request_deadline = ContextVar('news_request_deadline', default=None)


@lru_cache(maxsize=64)
def hourly(name, inn, as_of, hour, sector=''):
    deadline = time.monotonic() + 20
    if request_deadline.get() is not None:
        deadline = min(deadline, request_deadline.get())
    budget = {'remaining': 4_000_000, 'lock': Lock()}

    @lru_cache(maxsize=14)
    def read(url, cookie=None):
        return fetch(url, deadline, cookie, budget=budget)

    return collect(name, inn, as_of, read, sector)


@app.get('/news')
def news(name: str, as_of: date, inn: str = '', sector: str = '', expires: float | None = None):
    if sector not in ('', *SECTORS) or (name not in ISSUERS and (
        not re.fullmatch(r'[А-Яа-яЁёA-Za-z0-9][А-Яа-яЁёA-Za-z0-9 .-]{1,90}', name)
        or (inn and not re.fullmatch(r'\d{10}', inn))
    )):
        return collect('', None, as_of, lambda *args: None)
    if expires is None:
        return hourly(name, inn, as_of, int(time.time() // 3600), sector)
    remaining = expires - time.time()
    if remaining <= 0:
        return dict(status='unavailable', company=name, as_of=as_of.isoformat(),
                    retrieved_at=datetime.now(timezone.utc).isoformat(), items=[])
    # The deadline belongs to this request, not to the hourly cache key.
    token = request_deadline.set(time.monotonic() + remaining)
    try:
        return hourly(name, inn, as_of, int(time.time() // 3600), sector)
    finally:
        request_deadline.reset(token)


@app.get('/web-search')
def web_search(arguments: str, expires: float, session_id: str | None = None):
    from .external_sources import search
    from .contracts import validate_definition
    query = validate_definition('WebSearchArgs', json.loads(arguments))
    from http.client import HTTPException
    try:
        return search(query, time.monotonic() + min(45, max(0, expires - time.time())), session_id)
    except (OSError, HTTPException, ValueError) as error:
        return dict(unavailable=True, bytes_read=getattr(error, 'bytes_read', 160_000))


@app.get('/web-fetch')
def web_fetch(url: str, as_of: date, expires: float, max_bytes: int = 160_000,
              session_id: str | None = None, objective: str | None = None):
    from .external_sources import read_page, MAX_RESPONSE_BYTES
    from http.client import HTTPException
    allowance = min(MAX_RESPONSE_BYTES, max(0, max_bytes))
    try:
        return read_page(url, as_of.isoformat(), time.monotonic() + min(45, max(0, expires - time.time())), allowance, session_id, objective)
    except (OSError, HTTPException, ValueError) as error:
        return dict(unavailable=True, bytes_read=getattr(error, 'bytes_read', allowance))
