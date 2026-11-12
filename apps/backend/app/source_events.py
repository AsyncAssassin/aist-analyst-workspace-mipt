"""Числа в качественных заявлениях эмитента. Сверяем событие целиком с одной цитатой."""
import re


def source_event_numbers(text, facts):
    if len(facts) != 1:
        return text
    fact = facts[0]
    if not fact.get('source') or not fact.get('subject') or fact.get('amount') is not None or fact.get('nature') != 'source_statement':
        return text
    # Используем только основной фрагмент этого факта, а не подписи или идентификацию эмитента на другой странице.
    quote = ' '.join(fact.get('raw_text', '').split())
    if not quote or not any(quote == ' '.join(e['quote'].split()) and e['locator'].get('pdf_page')
                            and e.get('source') == fact['source']
                            for e in fact['evidence']):
        return text
    subject = re.escape(fact['subject'])
    bank = '|банк' if 'банк' in fact['subject'].casefold() else ''
    issuer = rf'(?:{subject}|компания|эмитент{bank})'
    mask = set()

    def compare(source_pattern, claim_pattern, numbers):
        # Группы содержат и числа, и значимые признаки (например «крупных»).
        saved = [m.groupdict() for m in re.finditer(source_pattern, quote, re.I)]
        for m in re.finditer(claim_pattern, text, re.I):
            if m.groupdict() in saved:
                for name in numbers:
                    mask.update(i for i in range(*m.span(name)) if text[i].isdigit())

    # Начало предложения или пояснение после двоеточия. Не пропускаем отрицание, другой субъект или иной период.
    start = r'(?:^|(?<=[.!?:;]))\s*'
    count = r'(?P<count>\d+)\s+(?P<size>крупных\s+)?(?P<object>сделок|контрактов|проектов)'
    years = r'за\s+(?P<years>\d+)\s+лет\s+'
    deal_end = r'(?P<small>\s+и\s+несколько\s+десятков\s+небольших)?(?=\s*(?:[.;!?]|$))'
    compare(start + years + r'мы\s+(?:успешно\s+)?закрыли\s+' + count + deal_end,
            start + years + issuer + r'\s+закрыл[аи]?\s+' + count + deal_end, ('count', 'years'))

    # Текст о росте доли рынка должен сохранить и предмет оценки, и модальность, и весь список кратностей.
    factors = r'(?P<list>в\s+\d+(?:,\s*в\s+\d+)*(?:\s+и\s+(?:даже\s+)?в\s+\d+)?\s+раз)'
    market = r'от\s+(?:текущей\s+)?(?:небольшой\s+)?доли\s+рынка'
    source_growth = (start + market + r'(?:\s+[\d.,]+%)?(?:\s+сегодня)?\s+мы\s+можем\s+вырасти'
                     r'(?:\s+ещ[её])?\s+' + factors + r'(?=\s*(?:[.!?;]|$)|[ \t]*\n(?!\s*(?:если|при|в случае|только|при условии|за|до)\b))')
    # Фраза атрибуции может содержать дату источника. Она проходит обычную проверку дат отдельно.
    attribution = (rf'по\s+оценке\s+(?:{subject}(?:а|и|я)?|эмитента|компании),\s*'
                   r'(?:основанной\s+на\s+[^;!?\n]{1,150}?\s+на\s+\d{2}\.\d{2}\.\d{4},\s*)?')
    claim_growth = (start + attribution + market + r'\s+' + issuer + r'\s+может\s+вырасти'
                    r'(?:\s+ещ[её])?\s+' + factors + r'(?=\s*(?:[.!?;]|$))')
    for m in re.finditer(claim_growth, text, re.I):
        if any(' '.join(m['list'].lower().split()) == ' '.join(g['list'].lower().split())
               for g in re.finditer(source_growth, fact['raw_text'], re.I)):
            mask.update(i for i in range(*m.span('list')) if text[i].isdigit())

    since = r'с\s+(?P<year>(?:19|20)\d{2})\s+года\s+'
    times = r'(?P<times>\d+)\s+раз\s+'
    # Сохраняем названия площадок, если они указаны. Перенос строки в PDF иногда склеивает домен со словом.
    domains = r'(?P<sites>[a-z][a-z0-9-]*\.[a-z]{2,}(?:\s+и\s+[a-z][a-z0-9-]*\.[a-z]{2,})*)'
    place = (r'первое\s+место\s+по\s+сервису\s+на\s+сайтах(?:[-\s]+отзовиках|\s+отзывов)\s*'
             + '(?:' + domains + ')?' + r'(?=\s*[,;.!?]|$)')
    compare(start + since + r'мы\s+занимали\s+' + times + place,
            start + since + issuer + r'\s+' + times + r'занимал[аи]?\s+' + place, ('year', 'times'))
    clients = r'(?P<clients>\d+)\s+миллионов\s+'
    compare(start + clients + r'наших\s+розничных\s+клиентов\b',
            start + r'розничная\s+база\s+насчитывает\s+' + clients + r'клиентов\b', ('clients',))
    return ''.join(' ' if i in mask else ch for i, ch in enumerate(text)) if mask else text


def primary_statements(facts):
    """Основные цитаты одной компании и версии PDF, без соседних справочных цитат."""
    subjects = {f.get('subject') for f in facts}
    pins = {tuple(sorted(f.get('source', {}).items())) for f in facts}
    if len(subjects) != 1 or not next(iter(subjects), None) or len(pins) != 1:
        return []
    return [f for f in facts if f.get('amount') is None and f.get('nature') == 'source_statement'
            and f.get('raw_text') and any(e.get('source') == f.get('source')
                and e['locator'].get('pdf_page')
                and ' '.join(e['quote'].split()) == ' '.join(f['raw_text'].split())
                for e in f['evidence'])]


def company_event_numbers(text, facts):
    """Год основания и открытие новых объектов, привязанные к самому событию."""
    mask = set()
    for fact in primary_statements(facts):
        quote = ' '.join(fact['raw_text'].split())
        subject = re.escape(fact['subject'])
        start = r'(?:^|(?<=[.!?;]))\s*'
        year = r'(?P<year>(?:19|20)\d{2})'
        founding = (start + subject + r'\s*(?:[:–—-]|это)?\s*(?:это\s+)?'
                    r'(?:[а-яё]+\s+){0,3}компания,?\s+основанная\s+в\s+' + year + r'\s+году\b')
        shops = (start + r'за\s+' + year + r'(?:\s+год)?\s+' + subject +
                 r'\s+открыла\s+(?P<count>\d+)\s+новых\s+(?P<object>магазинов|офисов|филиалов)\b')
        for pattern in (founding, shops):
            saved = [{k: v.casefold() for k, v in m.groupdict().items()}
                     for m in re.finditer(pattern, quote, re.I)]
            for match in re.finditer(pattern, text, re.I):
                if {k: v.casefold() for k, v in match.groupdict().items()} in saved:
                    for name in ('year', 'count'):
                        if name in match.groupdict():
                            mask.update(range(*match.span(name)))
    return ''.join(' ' if i in mask else ch for i, ch in enumerate(text))


# Эти количественные подписи сохраняют объект и стадию, а не только цифру.
PORTFOLIO_QUANTITY = re.compile(
    r'(?<![\w.,+−-])(?P<value>\d+(?:[.,]\d+)?)\s*'
    r'(?P<scale>тыс\.?|млн\.?|млрд\.?)?\s*'
    r'(?P<object>регистрационных\s+удостоверений|'
    r'ЛП,\s*МИ,?\s*(?:и\s+)?БАД(?:ов)?\s+на\s+стадии\s+регистрации|'
    r'ЛП,?\s*(?:и\s+)?БАД(?:ов)?\s+на\s+стадии\s+разработки|'
    r'(?:количество\s+)?сотрудник(?:а|ов)?|город(?:а|ов)?\s+присутствия|стран\s+экспорта)(?!\w)', re.I)


def portfolio_numbers(text, facts):
    """Прямые пары число/объект в основном фрагменте, в том числе с переносами PDF."""
    def key(match):
        label = ' '.join(match['object'].casefold().split())
        label = re.sub(r'[,]|\bи\b', ' ', label)
        label = re.sub(r'^(?:количество\s+)?сотрудник(?:а|ов)?$', 'сотрудники', label)
        label = re.sub(r'^город(?:а|ов)?\s+присутствия$', 'города присутствия', label)
        label = re.sub(r'\bбадов\b', 'бад', label)
        return (match['value'].replace(',', '.'), (match['scale'] or '').casefold().rstrip('.'),
                ' '.join(label.split()))
    source = set()
    for fact in primary_statements(facts):
        # Плановые или ограниченные сверху/снизу количества не подтверждают точный фактический перечень.
        if re.search(r'\b(?:план\w*|прогноз\w*|целев\w*|ожида\w*|не\s+менее|не\s+более|'
                     r'более|менее|около|до\s*\d)', fact['raw_text'], re.I):
            continue
        for match in PORTFOLIO_QUANTITY.finditer(fact['raw_text']):
            line_start = fact['raw_text'].rfind('\n', 0, match.start()) + 1
            # Подпись под отдельным числом. «До N» и «план N» не равны фактическому N.
            if not fact['raw_text'][line_start:match.start()].strip():
                source.add(key(match))
    mask = set()
    start = 0
    for sentence in re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z])', text):
        offset = text.index(sentence, start)
        start = offset + len(sentence)
        heading = re.match(r'(?:Продуктовый портфель|Операционные показатели):\s*', sentence, re.I)
        if not heading:
            continue
        matches = list(PORTFOLIO_QUANTITY.finditer(sentence, heading.end()))
        end = heading.end()
        for match in matches:
            if sentence[end:match.start()].strip() not in ('', ','):
                break
            end = match.end()
        else:
            if sentence[end:].strip() in ('', '.'):
                for match in matches:
                    if key(match) in source:
                        mask.update(offset + i for i in range(*match.span()) if sentence[i].isdigit())
    return ''.join(' ' if i in mask else ch for i, ch in enumerate(text))


def market_comparison_years(text, facts, remarks):
    """База сравнения рынка и возврат доли к прежнему уровню имеют разные годы."""
    primary = primary_statements(facts)
    mask = set()
    year = r'(?P<year>(?:19|20)\d{2})\s*г\.'
    baseline = r'(?P<baseline>(?:19|20)\d{2})\s*г\.'
    market = r'(?P<market>[А-Яа-яЁё\s-]{5,100}рынка\s+РФ)'
    source_market = (r'В\s+' + year + r'\s+' + market + r'\s+остался\s+на\s+натуральных\s+значениях\s*'
                     r'\(млн\s+упак\.\)\s*' + baseline)
    claim_market = (r'В\s+' + year + r'\s+' + market + r'\s+в\s+натуральном\s+выражении\s*'
                    r'\(млн\s+упаковок\)\s+остался\s+на\s+уровне\s+' + baseline)
    def market_key(match):
        return (match['year'], match['baseline'], ' '.join(match['market'].casefold().split()))

    quotes = [' '.join(f['raw_text'].split()) for f in primary]
    source_comparisons = {market_key(m) for quote in quotes for m in re.finditer(source_market, quote, re.I)}
    for match in re.finditer(claim_market, text, re.I):
        if market_key(match) in source_comparisons:
            mask.update(range(*match.span('year')))
            mask.update(range(*match.span('baseline')))
        elif source_comparisons:
            remarks.append(('period_mismatch', 'Год и база сравнения рынка не совпадают с основным фрагментом основания.'))

    # Не разрешаем год из соседней компании: основная цитата должна назвать долю самого субъекта.
    claim_share = (r'доля\s+компании\s+по\s+упаковкам\s+в\s+коммерческом\s+сегменте\s+'
                   r'опустилась\s+фактически\s+до\s+уровня\s+' + baseline)
    for fact, quote in zip(primary, quotes):
        subject = re.escape(fact['subject']) + r'а?'
        source_share = (r'доля\s+рынка\s+' + subject + r'\s+по\s+упаковкам\s+снизилась\s+с\s+'
                        r'\d+(?:[.,]\d+)?%\s+до\s+\d+(?:[.,]\d+)?%\s+г/г,\s+фактически\s+до\s+уровня\s+' + baseline)
        years = {m['baseline'] for m in re.finditer(source_share, quote, re.I)}
        for match in re.finditer(claim_share, text, re.I):
            if match['baseline'] in years:
                mask.update(range(*match.span('baseline')))
            elif years:
                remarks.append(('period_mismatch', 'Год сравнения доли компании не совпадает с основанием.'))

    top = r'топ[-–—](?P<count>\d+)\s+производителей\s+по\s+объ[её]му\s+выпуска\s+в\s+упаковках'
    groups = {m['count'] for quote in quotes for m in re.finditer(top, quote, re.I)}
    for match in re.finditer(top, text, re.I):
        if match['count'] in groups:
            mask.update(range(*match.span('count')))

    # Лидер назван отдельно от анализируемой компании. Одинаковая дата не переносит его результат.
    leader = r'лидер(?:у|а)?\s+рынка\s*[,–—:-]\s*(?P<company>[^,;.]+),'
    source_leaders = {' '.join(m['company'].casefold().split())
                      for quote in quotes for m in re.finditer(leader, quote, re.I)}
    for match in re.finditer(leader, text, re.I):
        if source_leaders and ' '.join(match['company'].casefold().split()) not in source_leaders:
            remarks.append(('subject_mismatch', 'В основании результат лидера рынка относится к другой компании.'))
    return ''.join(' ' if i in mask else ch for i, ch in enumerate(text))
