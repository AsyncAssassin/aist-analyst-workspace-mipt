"""Публичные признаки компании для сопоставления PDF и новостной статьи."""

import re


SECTORS = {
    'retail': r'ритейл\w*|ретейл\w*|розничн\w* торговл\w*|розничн\w* сет\w*|сет[ьи]\b.{0,100}\bмагазин\w*|гипермаркет\w*|супермаркет\w*',
    'pharma': r'фармацевт\w*|производител\w* лекарств\w*|производств\w* лекарств\w*',
    'bank': r'банк\w*|кредитн\w* организац\w*',
    'leasing': r'лизинг\w*|лизингов\w*',
}


def subject_pattern(name):
    # Название домена и более длинное имя не равны отдельному бренду.
    return (r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])'
            r'(?!\s++[А-ЯЁA-Z][А-Яа-яЁёA-Za-z0-9-]*(?:\s++[А-Яа-яЁёA-Za-z0-9-]+){0,3}[»"])')


def subject_sentences(text, name):
    """Контекст самого названия, без подвала сайта и соседних реквизитов."""
    return [sentence for sentence in re.split(r'(?<=[.!?;\n])\s+', text)
            if re.search(subject_pattern(name), sentence, re.I)]


def company_sector(pages, name):
    # Только самоописание на начальных страницах, не отрасли клиентов/конкурентов.
    matches = set()
    for page in pages[:3]:
        text = ' '.join(page.split())
        if re.search(r'обзор конкурентов|сравнение (?:компаний|конкурентов)', text[:180], re.I):
            continue
        for sentence in subject_sentences(text, name):
            if re.search(r'\b(?:клиент|конкурент|партнер|партнёр)\w*\b', sentence, re.I):
                continue
            matches.update(sector for sector, pattern in SECTORS.items()
                           if re.search(pattern, sentence, re.I))
    return next(iter(matches)) if len(matches) == 1 else ''


def subject_inns(name, body):
    """Явные ИНН именно названного субъекта, а не соседнего контрагента."""
    subject = re.compile(subject_pattern(name), re.I)
    # Разделители не содержат пробелов. Не перебираем разбиения пробельной серии.
    before_subject = re.compile(r'ИНН\s++компании\s++' + subject.pattern
                                + r'\s*+[»"]?\s*+[:№]?\s*+(\d{10})(?!\d)', re.I)
    after_subject = re.compile(r'\s*+[»"]?\s*+[,(:]?\s*+(?:зарегистрирован[ао]?\s++под\s++)?'
                               r'ИНН\s*+[:№]?\s*+(\d{10})(?!\d)', re.I)
    identifiers = set()
    for sentence in subject_sentences(body, name):
        before = before_subject.search(sentence)
        if before:
            identifiers.add(before[1])
        for match in subject.finditer(sentence):
            identifier = after_subject.match(sentence, match.end())
            if identifier:
                identifiers.add(identifier[1])
    return identifiers


def article_identity(name, inn, sector, body):
    """ИНН проверяется у названного субъекта, отрасль может подтвердить статью без реквизитов."""
    identifiers = subject_inns(name, body)
    sentences = subject_sentences(body, name)
    if inn and identifiers:
        return identifiers == {inn}
    if sector:
        return any(re.search(SECTORS[sector], sentence, re.I) for sentence in sentences)
    # Одного совпадения имени в заголовке недостаточно для неизвестной отрасли.
    return False
