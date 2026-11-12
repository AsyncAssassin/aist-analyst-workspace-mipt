from datetime import date

import pytest
from lxml import html

from app import news_sources as news
from app.note_news import identify, news_text
from app.news_identity import article_identity, company_sector

TODAY = date(2026, 11, 4)


def test_identification_requires_issuer_and_rejects_another_tax_number():
    assert identify(['Презентация', 'МФК «Займер» - крупнейшая компания']) == ('Займер', '')
    assert identify(['ООО «Вектор». ИНН: 5406836941']) == ('Вектор', '5406836941')
    assert identify(['ООО «Займер»']) is None
    assert identify(['ООО МФК «Займер»']) is None
    assert identify(['Обзор конкурентов: ПАО «Совкомбанк»']) is None
    assert identify(['ПАО «Займер». ИНН: 4401116480']) is None
    assert identify(['ПАО «Займер». Эмитент: ПАО «Совкомбанк»']) is None


def test_primary_article_date_identity_duplicates_and_fallback_window():
    dates = ['03.11.2026', '03.11.2026', '01.09.2026', '31.10.2025', '01.10.2025', '02.11.2024', '05.11.2026']
    titles = [
        'Займер приобрёл компанию',
        'Займер приобрёл компанию',
        'Другая компания выплатила дивиденды',
        'Займер разместил облигации',
        'Займер привлёк финансирование',
        'Займер выпускает облигации',
        'Займер выплатил дивиденды',
    ]
    listing = ''.join(
        f'<a href="/news/company-news/post-{i}"><span class="_title_test">{title}</span><span class="_date_test">{stamp}</span></a>'
        for i, (title, stamp) in enumerate(zip(titles, dates))
    )

    def read(url):
        if url.endswith('company-news'):
            return html.fromstring(listing), '0' * 64
        idx = int(url.split('-')[-1])
        assert idx not in (5, 6)
        article = f'<h1>{titles[idx]}</h1><p class="_date_test">{dates[idx]}</p><time itemprop="startDate" datetime="2026-10-31"/>'
        return html.fromstring(article), '1' * 64

    result = news.collect('Займер', '', TODAY, read)
    assert result['status'] == 'found'
    assert [i['quote'] for i in result['items']] == [titles[i] for i in (0, 3, 4)]
    assert '31.10.2026' in news_text(result['items'][0])

    def wrong_date(url):
        if url.endswith('company-news'):
            return read(url)
        return (
            html.fromstring('<h1>Займер приобрёл компанию</h1><p class="_date_test">01.01.2024</p>'),
            '9' * 64,
        )

    assert news.collect('Займер', '', TODAY, wrong_date)['status'] == 'unavailable'


def test_directory_identifies_an_unlisted_company_by_name_and_matching_inn():
    def read(url, cookie=None):
        if url.endswith('/companies/'):
            if cookie:
                return (
                    html.fromstring(
                        '<div class="database-search-object-list"><a href="/database/companies/123">Вектор</a></div>'
                    ),
                    '0' * 64,
                )
            return html.fromstring('''<span onclick="setDatabaseObjectsHash('abc');">В</span>'''), '0' * 64
        assert url.endswith('/123/')
        return html.fromstring('<div><div>ИНН</div><div>5406836941</div></div>'), '1' * 64

    assert news.registered_card('Вектор', '5406836941', read) == (news.EXPERT, '/database/companies/123/')
    assert news.registered_card('Вектор', '4401116480', read) is None


def test_empty_readable_listing_is_different_from_unavailable():
    def offline(*_):
        raise TimeoutError()

    def old(*_):
        return html.fromstring('<a href="/releases/2020/test">03.11.2020</a>'), 'e' * 64

    assert news.collect('Совкомбанк', '', TODAY, offline)['status'] == 'unavailable'
    assert news.collect('Совкомбанк', '', TODAY, old)['status'] == 'empty'
    assert news.collect('Неизвестная', '', TODAY, offline)['status'] == 'unavailable'


def test_proposal_is_not_a_payment_and_different_quarters_remain():
    approved = [
        dict(quote='Акционеры Займера утвердили дивиденды за II квартал 2026 года', published='2026-09-30')
    ]
    assert news.superseded_dividend(
        'СД рекомендовал дивиденды за II квартал 2026 года', date(2026, 9, 4), approved
    )
    assert news.superseded_dividend('СД рекомендовал дивиденды за II квартал', date(2026, 8, 25), approved)
    assert not news.superseded_dividend(
        'СД рекомендовал дивиденды за II квартал 2025 года', date(2026, 8, 25), approved
    )
    assert not news.superseded_dividend(
        'СД рекомендовал дивиденды за III квартал 2026 года', date(2026, 9, 4), approved
    )
    assert not news.superseded_dividend(
        'Акционеры отменили дивиденды за II квартал 2026 года', date(2026, 9, 4), approved
    )


def test_subsidiary_rating_does_not_become_parent_news():
    def read(url):
        if '/database/' in url:
            return html.fromstring('<a href="/releases/2026/nov01">01.11.2026</a>'), 'a' * 64
        return (
            html.fromstring(
                '<h1>Рейтинг ООО «Совкомбанк Лизинг» подтверждён</h1><p class="b-subheader__date">01.11.2026</p>'
            ),
            'b' * 64,
        )

    result = news.collect('Совкомбанк', '', TODAY, read)
    assert result['status'] == 'empty' and not result['items']


def test_rating_index_uses_publication_date_and_skips_market_roundups_before_fetch():
    def read(url):
        if '/database/' in url:
            return (
                html.fromstring(
                    '''<div><a href="/releases/2026/general">Рэнкинг банков на 01.10.2026</a>
                <span class="b-aside-content__time">03.11.2026</span></div>
                <div><a href="/releases/2026/bank">Подтверждён рейтинг ПАО «Совкомбанк» по состоянию на 01.10.2026</a>
                <span class="b-aside-content__time">02.11.2026</span></div>'''
                ),
                '0' * 64,
            )
        assert url.endswith('/bank')
        return (
            html.fromstring(
                '<h1>Подтверждён рейтинг ПАО «Совкомбанк»</h1><span class="b-subheader__date">02.11.2026</span>'
            ),
            '1' * 64,
        )

    batch = news.collect('Совкомбанк', '', TODAY, read)
    assert batch['status'] == 'found'
    assert [i['published'] for i in batch['items']] == ['2026-11-02']


def test_operating_results_make_the_list_before_repeated_dividends():
    def item(title, stamp):
        return dict(quote=title, published=stamp)

    items = [
        item('Займер рекомендовал дивиденды по итогам квартала', '2026-10-28'),
        item('Акционеры Займера утвердили дивиденды за второй квартал', '2026-09-30'),
        item('Акционеры Займера утвердили дивиденды за первый квартал', '2026-06-22'),
        item('Займер объявляет операционные результаты III квартала', '2026-10-15'),
        item('Займер приобрёл компанию', '2026-05-01'),
        item('Займер разместил облигации', '2026-06-01'),
    ]
    result = news.select_news(items, TODAY)
    assert items[2] not in result
    assert result == [items[0], items[3], items[1], items[5], items[4]]


def test_europlan_prefers_results_and_verifies_the_article_date():
    title = 'Европлан объявляет операционные результаты за 9 месяцев'
    listing = (
        f'<a class="ep-smititle" href="/company/news/1">{title}</a><div class="ep-smitime">20.10.2026</div>'
    )
    listing += '<a class="ep-smititle" href="/company/news/2">Европлан выплатил купонный доход</a><div class="ep-smitime">21.10.2026</div>'

    def read(url):
        if url.endswith('/1'):
            return (
                html.fromstring(f'<h1>{title}</h1><div class="ep-article"><time>20.10.2026</time></div>'),
                '1' * 64,
            )
        assert not url.endswith('/2')
        return html.fromstring(listing), '0' * 64

    batch = news.collect('Европлан', '', TODAY, read)
    assert batch['status'] == 'found'
    assert [(i['quote'], i['published']) for i in batch['items']] == [(title, '2026-10-20')]


def test_sitemap_update_is_not_an_event_or_publication_date():
    from lxml import etree

    host = news.HENDERSON

    def read(url):
        if url.endswith('sitemap-feeds.xml'):
            return (
                etree.fromstring(
                    f'<sitemapindex><sitemap><loc>{host}/sitemap-feed-123.xml</loc></sitemap></sitemapindex>'
                ),
                '0' * 64,
            )
        if url.endswith('123.xml'):
            return (
                etree.fromstring(
                    f'<urlset><url><loc>{host}/press-releases/tpost/abc123-rezultati</loc><lastmod>2026-11-02T15:00:00Z</lastmod></url></urlset>'
                ),
                '0' * 64,
            )
        return (
            html.fromstring(
                '<h1>HENDERSON объявляет результаты по выручке</h1><meta itemprop="datePublished" content="2026-10-15T07:00:00Z">'
            ),
            '1' * 64,
        )

    batch = news.collect('HENDERSON', '', TODAY, read)
    assert batch['status'] == 'found' and batch['items'][0]['published'] == '2026-10-15'
    assert 'event_date' not in batch['items'][0]
    assert identify(['Презентация', 'ПАО ЭЙЧ ЭФ ДЖИ («Компания») и группы Компании']) == ('HENDERSON', '')
    assert identify(['Товары HENDERSON продаются в магазине']) is None


def test_prospectus_issuer_and_pharma_releases():
    text = 'Предоставляется в связи с размещением акций («Акции») Публичного акционерного общества «ПРОМОМЕД» (ОГРН: 1217700349459; ИНН: 9702034818).'
    assert identify(['Презентация для инвесторов', text]) == ('ПРОМОМЕД', '9702034818')
    assert identify(['Презентация для инвесторов', text.replace('9702034818', '5406836941')]) is None
    assert identify(['Конкурент: ПАО «ПРОМОМЕД». ИНН: 9702034818']) is None
    path = '/novosti/registraciya/'
    headline = 'ПРОМОМЕД получил регистрационное удостоверение на препарат'
    listing = html.fromstring(f'<a class="card-article__link" href="{path}"><time datetime="03.11.2026"></time><b class="title">{headline}</b></a>')
    def read(url):
        if url.endswith('/novosti/'):
            return listing.getroottree(), '1' * 64
        assert url == news.PROMOMED + path
        return html.fromstring(f'<div><h1>{headline}</h1><p class="grid__aside-date">03.11.2026</p></div>'), '2' * 64
    result = news.collect('ПРОМОМЕД', '9702034818', TODAY, read)
    assert [(i['url'], i['published']) for i in result['items']] == [(news.PROMOMED + path, '2026-11-03')]


def test_legacy_snapshot_is_added_only_explicitly_and_only_once():
    from app.note_news import include, snapshot
    from copy import deepcopy

    base = {'result_type': 'analytical_note', 'content': {'sections': []}}
    batch = snapshot('found', TODAY.isoformat(), 'ПРОМОМЕД')
    batch['items'] = [dict(quote='ПРОМОМЕД опубликовал результаты', publisher='ПРОМОМЕД',
                           url=news.PROMOMED+'/novosti/rezultaty/', published=TODAY.isoformat(), page_sha256='a'*64)]
    empty_parent = {'basis': {'news_run': snapshot('unmatched', TODAY.isoformat())}}
    retry_payload, retry_basis = deepcopy(base), {}
    include(retry_payload, retry_basis, {'news_run': batch, 'add_legacy_news': True}, empty_parent)
    assert retry_basis['news_run'] == batch and empty_parent['basis']['news_run']['items'] == []
    payload, basis = deepcopy(base), {}
    include(payload, basis, {'news_run': batch}, {'basis': {}})
    assert payload == base and basis == {}
    include(payload, basis, {'news_run': batch, 'add_legacy_news': True}, {'basis': {}})
    assert len(payload['content']['sections'][0]['blocks']) == 1 and basis['news_run'] == batch
    removed = deepcopy(base)
    include(removed, {}, {'news_run': batch, 'add_legacy_news': True}, {'basis': basis})
    assert removed == base


def test_public_training_campaign_is_not_an_issuer_business_event():
    assert news.topic('Минздрав и компания запускают программу «Здоровье» для медицинских работников') is None
    assert news.topic('Группа запускает программу выкупа акций') == 'capital'


def test_brand_identification_requires_document_evidence_and_unique_subject():
    assert identify(['Иридиум Инвестиционный тизер'], 'Отчет.pdf') == ('Иридиум', '')
    assert identify(['Орбита\nПрезентация для инвесторов'], 'Орбита_презентация инвесторам.pdf') == ('Орбита', '')
    assert identify(['Презентация', 'О компании Иридиум - российская компания'], 'Документ.pdf') == ('Иридиум', '')
    profile = 'Обзор бизнеса. За 2022 Virelia открыла 17 магазинов. Virelia развивает доставку.'
    assert identify(['Финансовые результаты. Иридиум – это российская промышленная компания.'], 'Отчет.pdf') == ('Иридиум', '')
    assert identify(['Описание компании. Компания развивает продажи. Компания производит товары.'], 'Компания.pdf') is None
    assert identify([profile], 'документ.pdf') == ('Virelia', '')
    assert identify([profile], 'Virellia.pdf') == ('Virelia', '')
    assert identify([profile + ' Altura открыла магазины. Altura развивает сеть.'], 'Virellia.pdf') is None
    assert identify(['Основные конкуренты. Virelia открыла магазины. Virelia развивает сеть.'], 'Virelia.pdf') is None
    assert identify(['Рынок розницы: в числе клиентов была Virelia.'], 'Virelia.pdf') is None


def test_publisher_search_uses_current_article_and_rejects_wrong_subject_or_date():
    path = '/business/12345'
    title = '«Яндекс» приобрёл сеть «Орбита»'
    stamp = '2026-10-14'
    def read(url):
        if url.startswith(news.INTERFAX + '/search/?phrase='):
            doc = f'<div class="sPageResult__total">Найдено 2</div><div class="sPageResult"><div><time datetime="{stamp}T10:15"></time><a href="{path}">{title}</a><a href="/business/98765">Сеть Вереск запустила сервис</a></div></div>'
        else:
            assert url == news.INTERFAX + path
            doc = (f'<h1>{title}</h1><meta itemprop="datePublished" content="2026-10-14T10:15">'
                   '<div itemprop="articleBody">Орбита развивает сеть магазинов.</div>')
        return html.fromstring(doc), 'a' * 64
    articles, partial = news.press_items('Орбита', TODAY, news.INTERFAX, read, sector='retail')
    assert not partial and [i['url'] for i in articles] == [news.INTERFAX + path]
    def wrong_date(url):
        doc, sha = read(url)
        for el in doc.xpath('//*[@itemprop="datePublished"]'):
            el.set('content', '2020-10-14T10:15')
        return doc, sha
    assert news.press_items('Орбита', TODAY, news.INTERFAX, wrong_date, sector='retail') == ([], True)
    assert news.topic('Маяк запустил Telegram-канал о реставрации зданий') is None


@pytest.mark.parametrize('group', [False, True])
def test_retail_discovers_exact_company_card_and_validates_article_metadata(group):
    profile = '/rbc/tradingnetworks/orbita/'
    path = '/news/orbita-preodolela-rubezh-v-100-magazinov-28-avgusta-2026-1234/'
    title = 'Орбита преодолела рубеж в 100 магазинов'
    card_title = 'Группа Орбита' if group else 'Орбита'
    def read(url):
        if url.startswith(news.RETAIL + '/search/?q='):
            body = f'<div class="search"><a href="{profile}" title="Перейти на страницу компании">{card_title}</a><a href="/rbc/tradingnetworks/orbita-plus/" title="Орбита Плюс">Орбита Плюс</a></div>'
        elif url == news.RETAIL + profile:
            body = f'<article><a href="{path}">{title}</a></article>'
        else:
            assert url == news.RETAIL + path
            body = f'<h1>{title}</h1><script type="application/ld+json">{{"@type":"NewsArticle","headline":"{title}","datePublished":"2026-08-28","mainEntityOfPage":"{url}"}}</script>'
            body += '<div class="contain__description-material"><p>Сеть Орбита открыла свой 100-й магазин.</p></div>'
        return html.fromstring(body), 'a' * 64
    result, partial = news.press_items('Орбита', TODAY, news.RETAIL, read, sector='retail')
    assert not partial and [i['quote'] for i in result] == [title]
    assert news.press_items('Орбита', TODAY, news.RETAIL, read, '5406836941')[0] == []
    result, partial = news.press_items('Орбита', TODAY, news.RETAIL, read, '5406836941', 'retail')
    assert not partial and [i['quote'] for i in result] == [title]


def test_issuer_self_description_supports_legal_forms_and_later_pages():
    pages = ['Презентация для инвесторов',
             'Сводка об ассортименте была подготовлена\nМКПАО\n«Лента» (далее Компания).',
             '«Группа Лента» проверяет выкладку в магазинах розничной сети.']
    assert identify(pages, 'документ.pdf') == ('Лента', '')
    assert company_sector(pages, 'Лента') == 'retail'
    assert identify(['Презентация'] * 4 + ['Эмитент: МКАО «Орбита».']) == ('Орбита', '')
    assert identify(['Сравнение конкурентов: МКПАО «Лента».']) is None
    assert identify(['Наш клиент МКПАО «Лента» расширяет сеть.']) is None
    assert identify(['Эмитент: МКПАО «Орбита». Эмитент: ПАО «Вектор».']) is None
    assert identify(['Презентация для инвесторов'], 'Лента.pdf') is None
    assert identify(['Технология МКПАО «Орбита» используется в нашем продукте.']) is None


@pytest.mark.parametrize('body,expected', [
    ('Ритейлер «Лента» увеличил выручку.', True),
    ('Издатель «Лента» увеличил выручку.', False),
    ('Супермаркеты в России растут. Издатель «Лента» увеличил выручку.', False),
    ('Ритейлер «Лента» (ИНН 5406836941) увеличил выручку.', True),
    ('Ритейлер «Лента» (ИНН 4401116480) увеличил выручку.', False),
    ('Ритейлер «Лента» увеличил выручку. Банк-кредитор: ООО «Вектор», ИНН 4401116480.', True),
    ('Ритейлер «Лента» заключил договор с ООО «Вектор», ИНН 4401116480.', True),
    ('Ритейлер «Лента» заключил договор с «Вектор», ИНН 4401116480.', True),
    ('Ритейлер Лента, ИНН: 4401116480, увеличил выручку.', False),
    ('ООО «Вектор» (ИНН 5406836941) получило кредит. Издатель «Лента» увеличил выручку.', False),
])
def test_press_identity_distinguishes_sector_and_subject_tax_id(body, expected):
    assert article_identity('Лента', '5406836941', 'retail', body) is expected


def test_company_context_does_not_come_from_a_competitor_or_domain():
    assert company_sector(['Конкурент Лента: крупнейшая розничная сеть.'], 'Лента') == ''
    assert not news.relevant_subject('Лента.ру увеличила выручку', 'Лента')
    assert not news.relevant_subject('Выручка конкурента «Лента» выросла', 'Лента')
    assert not news.relevant_subject('Супер-Лента увеличила выручку', 'Лента')
    assert not news.relevant_subject('«Лента Логистика» увеличила выручку', 'Лента')
    assert not article_identity('Лента', '', '', 'Издатель Лента увеличил выручку.')


def test_article_without_inn_is_accepted_only_with_matching_business_context():
    path = '/business/12345'
    title = 'Лента увеличила выручку'
    body = '<div itemprop="articleBody">Ритейлер «Лента» увеличил выручку.</div>'

    def read(url):
        if '/search/' in url:
            content = (f'<div class="sPageResult__total">Найдено 1</div><div class="sPageResult">'
                       f'<div><time datetime="2026-10-14"></time><a href="{path}">{title}</a></div></div>')
        else:
            content = f'<h1>{title}</h1><meta itemprop="datePublished" content="2026-10-14">' + body
        return html.fromstring(content), 'a' * 64

    items, partial = news.press_items('Лента', TODAY, news.INTERFAX, read, '5406836941', 'retail')
    assert not partial and [item['url'] for item in items] == [news.INTERFAX + path]
    assert news.press_items('Лента', TODAY, news.INTERFAX, read, '5406836941', 'pharma') == ([], False)


def test_official_solopharm_article_uses_site_brand_spelling():
    path = '/about/news/new-gmp'
    title = 'Solopharm получила сертификат GMP'

    def read(url):
        if url == news.SOLO + '/ru':
            content = f'<a class="swiper-slide" href="{path}"><span>28/10/26</span><p>{title}</p></a>'
        else:
            assert url == news.SOLO + path
            content = (f'<h1>{title}</h1><meta itemprop="datePublished" content="2026-10-28">'
                       '<div itemprop="articleBody">Фармацевтическая компания Solopharm получила сертификат GMP.</div>')
        return html.fromstring(content), 'b' * 64

    items, partial = news.press_items('Солофарм', TODAY, news.SOLO, read, sector='pharma')
    assert not partial and [item['quote'] for item in items] == [title]
