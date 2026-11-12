"""A narrow verifier for a monetary observation in its own saved prose sentence.

No reconstruction from images or disconnected cells. Unsupported language keeps the ordinary
local extraction limitation; this rule neither discovers facts nor changes qualitative candidates.
"""
from decimal import Decimal
import re

from . import charts, tables
from .source_cards import DATE, MONEY, YEAR, label_bound, source_period

PREDICATE = re.compile(r'\b(?:составил[аио]?|достиг(?:ла|ло|ли)?|получил[аи]?|вырос(?:ла|ло|ли)?|'
                       r'увелич(?:ил[а-я]*|ив)|снизил[а-я]*|сократил[а-я]*)\b', re.I)
FLOW = re.compile(r'выручк|прибыл|убыт|оборот|доход|расход', re.I)
QUALIFIED = re.compile(r'\bне\b|прогноз|план|ожида|возмож|может|могла|составит|достигнет|вырастет|'
                       r'увеличится|будет|вероят|более|менее|около|порядка|свыше|почти|примерно|диапазон', re.I)
PERIOD = re.compile(rf'(?<![\w.\-−])(?:(?P<quarter>[1-4])\s*(?:кв(?:артал[а-я]*)?\.?|Q)\s*)?'
                    rf'(?P<year>{YEAR})(?![\d.])(?:\s*(?:году|года|год|г\.))?(?!\w)', re.I)


def squeeze(text):
    return re.sub(r'\s+', '', text).casefold()


def sentences(text):
    # Decimal points and monetary abbreviations are not sentence boundaries. Newlines are wraps.
    masked = re.sub(r'(?<=\d)\.(?=\d)', '\x01', text)
    masked = re.sub(r'\b(руб|млн|млрд|трлн|тыс|кв|г)\.(?!(?-i:\s+[А-ЯA-Z]))', lambda m: m[1]+'\x01', masked, flags=re.I)
    return [part.replace('\x01', '.').strip() for part in re.split(r'[.!?](?:\s+|$)', masked) if part.strip()]


def verify(pages, layouts, item):
    """Return True only for a complete original phrase. Raise a classified content refusal otherwise."""
    def reject(code, message):
        raise tables.Unsupported(code, message)
    main = item['evidence'][0]
    lines = pages[main['page']].splitlines()
    start, end = main['line_start'], main['line_end']
    paragraph = '\n'.join(lines[start-1:end])
    b = item['basis']
    if not b or b['page'] != main['page'] or item['amount']['unit'] != 'money':
        reject('unit_mismatch', 'Текстовой сумме нужны собственные показатель и денежная единица.')
    if label_bound(lines, layouts.get(main['page'], ''), start, list(range(start, end+1))) is False:
        reject('ambiguous_link', 'Текстовая сумма ссылается на разные печатные блоки.')
    raw = squeeze(item['value_text'])
    found = [(phrase, match) for phrase in sentences(paragraph) for match in MONEY.finditer(phrase)
             if squeeze(match['value']) == raw]
    if len(found) != 1:
        reject('ambiguous_link', 'Нет единственной собственной денежной фразы с этим написанием.')
    phrase, amount = found[0]
    # One sentence may contain several independent assertions. A metric in the preceding
    # clause cannot borrow this amount merely because only one monetary number is printed.
    cuts = [match for match in re.finditer(r'(?<!\d),(?!\d)|;|\b(?:а|но|и)\b', phrase, re.I)]
    left = max([cut.end() for cut in cuts if cut.end() <= amount.start()] or [0])
    right = min([cut.start() for cut in cuts if cut.start() >= amount.end()] or [len(phrase)])
    clause = phrase[left:right]
    if (len(list(MONEY.finditer(phrase))) != 1 or len(list(PREDICATE.finditer(clause))) != 1
            or squeeze(item['metric']) not in squeeze(clause)
            or squeeze(b['metric_label']) not in squeeze(clause)):
        reject('ambiguous_link', 'Показатель и сумма не связаны одной однозначной текстовой фразой.')
    if QUALIFIED.search(phrase) or item['nature'] != 'reported_actual':
        reject('ambiguous_link', 'Прогноз, оговорка, граница или отрицание не подтверждают точное историческое значение.')
    if re.search(r'\bот\s*$', phrase[:amount.start()], re.I):
        reject('ambiguous_link', 'Нижняя граница не подтверждает точное значение.')
    if re.search(r'\bдо\s*$', phrase[:amount.start()], re.I) and not re.search(r'вырос|увелич|достиг|сниз|сократ', phrase, re.I):
        reject('ambiguous_link', 'Граница до суммы не является достигнутым значением.')
    if squeeze(amount['unit']) != squeeze(b['unit_label']) or item['amount']['currency'] != 'RUB':
        reject('unit_mismatch', 'Денежная единица должна принадлежать выбранной сумме в ее фразе.')
    number = Decimal(re.sub(r'\s+', '', amount['value']).replace(',', '.').replace('−', '-'))
    if number != Decimal(item['amount']['value']):
        reject('number_mismatch', 'Текстовое число не совпадает со значением кандидата.')
    subject = squeeze(item['subject'])
    name = r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in item['subject'].split()) + r'(?!\w)'
    predicate = PREDICATE.search(clause)
    if re.search(name, clause, re.I):
        prefix = clause[:predicate.start()]
        if not re.search(name, prefix, re.I):
            reject('ambiguous_link', 'Упоминание компании после сказуемого не устанавливает владельца суммы.')
        prefix = re.sub(name, '', prefix, flags=re.I)
        prefix = re.sub(r'\b(?:за|в)\s+' + PERIOD.pattern, '', prefix, flags=re.I)
        prefix = re.sub(r'\bна\s+' + DATE.pattern, '', prefix, flags=re.I)
        for label in (b['metric_label'], item['metric']):
            prefix = re.sub(re.escape(label), '', prefix, flags=re.I)
        prefix = re.sub(r'\bкомпани[яи]\b', '', prefix, flags=re.I)
        if re.search(r'\w', prefix):
            reject('ambiguous_link', 'Субъект и показатель перед сказуемым не связаны однозначно.')
    else:
        # A named topic followed by a bare metric/predicate shares that topic, but a new
        # subject such as "Юг получила" or "выручка Юга составила" does not.
        shared_topic = (squeeze(phrase).startswith(subject+':')
                        and squeeze(clause[:predicate.start()]) == squeeze(item['metric']))
        continuation = (subject not in squeeze(phrase) and squeeze(paragraph).startswith(subject)
                        and re.search(r'\bкомпания\b', phrase, re.I)
                        and not re.search(r'\b[А-ЯA-Z][а-яa-z]+', phrase.split(maxsplit=1)[-1])
                        and '\n\n' not in paragraph)
        if not (shared_topic or continuation):
            reject('ambiguous_link', 'Субъект суммы не установлен ее собственной фразой или абзацем.')
    if re.search(r'конкурент|другая компания|сообщает|по словам|по данным', phrase, re.I):
        reject('ambiguous_link', 'Принадлежность суммы компании неоднозначна.')
    quoted = re.findall(r'[«"]([^»"]+)[»"]', phrase)
    if any(squeeze(name) != subject for name in quoted):
        reject('ambiguous_link', 'Фраза содержит название другого субъекта.')
    # A literal reporting label is optional only here. If present, it belongs to this paragraph.
    report = b['reporting_label']
    if report is None:
        if item['reporting_basis'] != 'Не указано' or re.search(r'МСФО|РСБУ|IFRS|GAAP', paragraph, re.I):
            reject('unit_mismatch', 'Заявленная отчетная база не может быть заменена неизвестной.')
    elif (squeeze(report) not in squeeze(paragraph)
          or squeeze(item['reporting_basis']) != squeeze(report)):
        reject('unit_mismatch', 'Отчетная база не подтверждена собственной текстовой фразой.')
    dates = list(DATE.finditer(clause))
    if dates:
        expected = source_period(clause) if len(dates) == 1 and not FLOW.search(item['metric']) else None
    else:
        markers = list(PERIOD.finditer(clause))
        expected = None
        if len(markers) == 1:
            marker = markers[0]
            rest = clause[:marker.start()] + clause[marker.end():]
            if not re.search(r'квартал|полугод|месяц|\bна\s*$', rest, re.I):
                token = (marker['quarter']+'Q' if marker['quarter'] else '') + marker['year']
                expected = charts.period(token, item['metric'])
                if not FLOW.search(item['metric']):
                    expected = None
    if (not expected or any(item['period'][key] != expected[key] for key in ('kind', 'start', 'end'))
            or squeeze(item['period']['label']) not in squeeze(phrase)):
        reject('period_mismatch', 'Период суммы должен совпадать с ее собственной текстовой фразой.')
    return True
