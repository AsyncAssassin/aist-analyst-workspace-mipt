"""Tools resolve only the source pins and authority of the durable Run."""
from .runs import execute,json_value,now
from .service import ident,row
from .normalization import checked,fingerprint,visible_text
from .source_events import source_event_numbers, company_event_numbers, portfolio_numbers, primary_statements, market_comparison_years
from .errors import ApiError
from .contracts import validate_definition
from .contracts import SCHEMA
from .facts import resolve_fact, search_evidence, pinned_version, source_excerpt_text, numeric_table_passage, LEGACY_EXCERPT_PREFIX, MISSING_FACT_ADVICE
from .domain import growth, refuse
from decimal import Decimal
from calendar import monthrange
from datetime import date
import re

DOTTED_DATE = (r'(?<![\w.])(?P<day>\d{1,2})\.\s*(?P<month>\d{1,2})'
               r'(?:\.\s*|\s+)(?P<year>(?:19|20)\d{2})'
               r'(?:\s*(?:года|г\.))?(?!\w|\.\d)')


SIGNS = '+-\u2212'
DASHES = '\u2013\u2014'
# Разрядная группа начинается с 1-3 цифр. Год из четырех цифр и перенос
# строки не соединяют соседние значения, даже в исходной PDF-цитате.
NUMERAL = re.compile(r'(?<!\w)(?:\d{1,3}(?:[ \u00a0\u202f]\d{3}(?!\d))+|\d+)(?:[.,]\d+)?')


def quoted_sign(value, start):
    """Знак числа в цитате PDF: извлечение текста может отделить его пробелом.

    Дефис вплотную после буквы, как в «топ-15», знаком не является, как и в тексте
    записки: иначе пересказ цитаты не совпал бы с ее числом. None означает
    неоднозначный знак: двойной знак или знак между числами."""
    prefix = value[:start].rstrip()
    if not prefix.endswith(tuple(SIGNS)) or prefix[-2:-1].isalpha():
        return ''
    before = prefix[:-1].rstrip()
    if before.endswith(tuple(SIGNS)) or (before and before[-1].isdigit()):
        return None
    return '+' if prefix[-1] == '+' else '-'


def written_sign(value, start):
    """Знак числа в тексте записки: только символ вплотную перед числом.

    Тире между числами обозначает диапазон, дефис или тире с пробелами разделяет
    части фразы, дефис внутри слова знаком не является. Тире вплотную перед
    числом читается как минус, чтобы не пропустить потерю знака. None означает
    неоднозначный знак: дефис между числами или двойной знак."""
    # Пробел после нескольких знаков не превращает их в одиночный разделитель.
    prefix = value[:start].rstrip()
    if prefix and prefix[-1] in SIGNS + DASHES:
        before = prefix[:-1].rstrip()
        if before and before[-1] in SIGNS + DASHES:
            return None
    mark = value[start - 1:start] if start else ''
    if not mark or mark not in SIGNS + DASHES:
        return ''
    left = value[start - 2:start - 1] if start > 1 else ''
    if left.isdigit() or left == '%':
        return '' if mark in DASHES else None
    if left and left in SIGNS + DASHES:
        return None
    if left.isalpha():
        return ''
    return '+' if mark == '+' else '-'


def numerals(value, sign_of):
    """Каждое число текста со знаком по правилу sign_of. Части дат знака не получают."""
    # ISO date separators are not unary signs; keep the existing date numerals.
    value=re.sub(r'(?<![\w+\-\u2212])\d{4}-\d{2}-\d{2}(?!\w)',
                 lambda m:m[0].replace('-', ' / '),value)
    value=re.sub(DOTTED_DATE,lambda m:' / '.join(m.group('day','month','year')),value)
    for match in NUMERAL.finditer(value):
        token = re.sub(r'[ \u00a0\u202f]', '', match[0]).replace(',', '.')
        yield value, match, sign_of(value, match.start()), token


def number_tokens(value, *, source_quote=False):
    if source_quote:
        value = source_list_text(value)
    numbers=set()
    for text, match, sign, token in numerals(value, quoted_sign if source_quote else written_sign):
        if sign is None:
            # Неоднозначный фрагмент исходной цитаты не дает числового
            # основания, но не запрещает ее качественный пересказ.
            if source_quote:continue
            fragment=text[max(0,match.start()-18):match.end()]
            refuse('Неоднозначный знак числа в абзаце: «'+fragment+'». '
                   'Диапазон обозначай коротким тире без пробелов, минус ставь вплотную к числу, '
                   'не соединяй числа и периоды дефисом.')
        numbers.add(Decimal(sign+token))
    return numbers


NUMBERS_ADVICE = ('Числа без основания: нужен подходящий числовой факт или расчет, '
                  'иначе передай смысл без этих чисел.')
UNITS_ADVICE = ('Сумма или процент должны точно повторять число, знак, единицу, '
                'масштаб и валюту основания.')
PERIOD_ADVICE = ('Квартал не подтверждает годовой показатель. Уточни формулировку по '
                 'сохраненному основанию; не заменяй дату события годовым интервалом.')
NUMERIC_FORM_ADVICE = ('Год пишите со словом «год» в нужном падеже, квартал в формате 1Q24. '
                       'Сумма требует единицы и масштаба. Все значения и периоды должны '
                       'иметь сохраненные основания.')
# Общие пояснения повторяются в отдельных замечаниях и приводятся в сводке один раз.
ADVICE = (NUMBERS_ADVICE, UNITS_ADVICE, PERIOD_ADVICE, NUMERIC_FORM_ADVICE, MISSING_FACT_ADVICE)
MESSAGE_LIMIT = SCHEMA['$defs']['Error']['properties']['message']['maxLength']
FINDING_LIMIT = SCHEMA['$defs']['Finding']['properties']['message']['maxLength']

# Поля шаблона ASO из скилла: неизвестный параметр сделки и стандартные следующие шаги.
# Шаблонное происхождение допускается только для этих формулировок слово в слово.
DEAL_TERMS = ('Сумма сделки', 'Оценка / Доля', 'Структура сделки')
TEMPLATE_TEXTS = frozenset([*(term + ': [TBD]' for term in DEAL_TERMS),
                            'Внутреннее обсуждение инвестиционной возможности',
                            'Запрос дополнительной информации, проведение детального анализа инвестиции'])
TEMPLATE_ADVICE = ('Шаблонное происхождение допускается только для строк «Сумма сделки: [TBD]», '
                   '«Оценка / Доля: [TBD]», «Структура сделки: [TBD]» и двух стандартных следующих шагов '
                   'слово в слово. Сведения о компании требуют оснований.')
# Свободный текст запроса и ручной правки в разделах сделки проверяет человек.
REQUEST_SECTIONS = ('описание возможности', 'описание сделки')
REQUEST_ADVICE = ('Сведения из текста запроса допускаются только в разделах «Описание возможности» '
                  'и «Описание Сделки», без ссылок на факты и расчеты.')
TEAM_REQUEST = ('Команда не указана. Чтобы добавить ее в шапку записки, напишите в чате '
                '«измени команду на И. Иванов, П. Петров» или укажите ее в редакторе.')


def excerpt(text, limit=40):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def located_claims(payload):
    """Каждое утверждение с местом, узнаваемым автору: раздел, блок, строка и служебный ID.

    Четвертый элемент называет место внутри блока: строку таблицы или утверждение
    ответа; у абзаца его нет, место абзаца определяет сам блок."""
    if payload['result_type'] == 'evidence_answer':
        for index, claim in enumerate(payload['content']['claims'], 1):
            location = f'Утверждение {index} «{excerpt(claim["text"])}»'
            yield location, claim, None, location
        return
    for si, section in enumerate(payload['content']['sections'], 1):
        for bi, block in enumerate(section['blocks'], 1):
            place = f'Раздел {si} «{excerpt(section["title"])}», блок {bi}'
            if block['kind'] == 'paragraph':
                yield f'{place}, абзац «{excerpt(block["claim"]["text"])}» ({block["block_id"]})', block['claim'], block, None
                continue
            table = f'{place}, таблица «{excerpt(block["caption"])}»'
            for ri, row in enumerate(block['rows'], 1):
                within = f'строка {ri} «{excerpt(row["label"])}» ({row["row_id"]})'
                yield f'{table}, {within}', row, block, within


def block_locations(payload):
    """Место каждого блока для замечаний проверок, у которых есть только block_id."""
    locations = {}
    for si, section in enumerate(payload['content'].get('sections', []), 1):
        for bi, block in enumerate(section['blocks'], 1):
            kind = (f'абзац «{excerpt(block["claim"]["text"])}»' if block['kind'] == 'paragraph'
                    else f'таблица «{excerpt(block["caption"])}»')
            locations[block['block_id']] = f'Раздел {si} «{excerpt(section["title"])}», блок {bi}, {kind} ({block["block_id"]})'
    return locations


def content_finding(code, block, within, message):
    """Содержательное несовпадение сохраняется у версии блокирующим замечанием.

    Место внутри блока входит в текст, блок хранится отдельно; длина ограничена контрактом."""
    text = (within + ': ' if within else '') + message
    if len(text) > FINDING_LIMIT:
        text = text[:FINDING_LIMIT - 1].rstrip() + '…'
    return dict(code=code, severity='error', block_id=block['block_id'] if block else None, message=text)


def feedback(errors, findings=(), locations=None):
    """Все замечания одной попытки в одном сообщении в пределах контракта."""
    lines = [location + ': ' + message for location, message in errors]
    for finding in findings:
        place = (locations or {}).get(finding['block_id']) or (
            'Блок ' + finding['block_id'] if finding['block_id'] else '')
        lines.append((place + ': ' if place else '') + finding['message'])
    shared = []
    compact = []
    for line in lines:
        for sentence in ADVICE:
            if sentence in line:
                line = line.replace(' ' + sentence, '').replace(sentence, '').rstrip()
                if sentence not in shared:
                    shared.append(sentence)
        compact.append(line)
    text = '\n'.join([*compact, *shared])
    if len(text) > MESSAGE_LIMIT:
        notice = '\n… список обрезан: исправь перечисленное и сохрани снова.'
        text = text[:MESSAGE_LIMIT - len(notice)].rsplit('\n', 1)[0] + notice
    return text


def amount_text(amount):
    scale = {'1': '', '1000': 'тыс. ', '1000000': 'млн ', '1000000000': 'млрд ', '1000000000000': 'трлн '}[amount['scale']]
    unit = amount['currency'] or {'percent': '%', 'count': 'ед.', 'ratio': 'коэф.'}[amount['unit']]
    return amount['value'] + ' ' + scale + unit


ROW_DATE_LABEL = (r'(?:на\s+|по\s+состоянию\s+на\s+)?(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.'
                  r'(?P<year>(?:19|20)\d{2})(?:\s*(?:г\.|года))?')


def adopt_period_label(row, facts):
    """Подпись остатка «на 31.03.2023» принимает точную подпись факта, если это та же дата.

    Только единственный числовой факт без расчета, равная сумма с теми же единицами и
    остаток на дату: чужая дата, поток за период или другая сумма остаются отказом."""
    if len(facts) != 1 or row['calculation_ids']:
        return
    fact = facts[0]
    period = fact['period']
    if period['kind'] != 'instant' or fact['amount'] != row['amount'] or row['period_label'] == period['label']:
        return
    match = re.fullmatch(ROW_DATE_LABEL, row['period_label'].strip(), re.I)
    if not match:
        return
    try:
        named = date(int(match['year']), int(match['month']), int(match['day'])).isoformat()
    except ValueError:
        return
    if named == period['end']:
        row['period_label'] = period['label']


def resolve_basis(runs, c, r, payload, parent=None, human=False, findings=None, errors=None, parent_checks=None):
    """Основания каждого утверждения и все независимые замечания за одну попытку.

    errors получает пары (место, замечание), отклоняющие сохранение: отсутствующий
    факт или расчет и подделка редакторского происхождения. Содержательные
    несовпадения финансовой строки и утверждение без ссылок становятся ошибками
    с узнаваемым местом. Непроверенные основания записки остаются предупреждениями.
    Без списка errors блокирующие замечания завершают проверку одним отказом."""
    from . import visual_observations as visual, review_candidates
    facts, calculations, observations = {}, {}, {}
    candidates = {}
    allowed_candidates = review_candidates.available(r, parent)
    inherited_candidates = review_candidates.inherited_links(parent)
    allowed_visual = visual.available(r, parent)
    inherited_visual = visual.inherited_links(parent)
    findings = findings if findings is not None else []
    standalone = errors is None
    collected = [] if standalone else errors
    content_findings = []
    inherited = {x['calculation_id']:x for x in parent['basis']['calculations']} if parent else {}
    previous = {b['block_id']:b for sec in parent['payload']['content'].get('sections',[]) for b in sec['blocks']} if parent else {}
    seen = set()
    for sec in payload['content'].get('sections',[]):
        for block in sec['blocks']:
            if block['block_id'] in inherited_candidates and block['kind'] != 'paragraph':
                refuse('Абзац с непроверенным кандидатом нельзя заменить таблицей с тем же идентификатором.')
        for obj in [sec, *sec['blocks'], *[row for b in sec['blocks'] if b['kind']=='table' for row in b['rows']]]:
            oid = obj.get('section_id') or obj.get('block_id') or obj.get('row_id')
            if oid in seen: refuse('Идентификаторы разделов, блоков и строк должны быть уникальны.')
            seen.add(oid)
    previous_rows = {item['row_id']: item for block in previous.values() for item in block.get('rows', [])}
    flagged_labels = {f.get('row_id') for check in parent_checks or [] for f in check['findings']
                      if f['code'] == 'row_label_mismatch'}
    section_titles = {block['block_id']: sec['title'] for sec in payload['content'].get('sections', [])
                      for block in sec['blocks']}
    # Снимки сообщений запроса: родительские для сохраненных абзацев, текущего Run для новых.
    requests = {item['message_id']: item['text'] for item in (parent or {}).get('basis', {}).get('request_messages', [])}
    used_requests = {}

    def resolve_request(claim, block, remarks):
        """Тексты сообщений запроса утверждения. Ссылку на сообщение своего Run ставит AIST."""
        if (not block or block['kind'] != 'paragraph'
                or section_titles[block['block_id']].strip().casefold() not in REQUEST_SECTIONS):
            remarks.append(REQUEST_ADVICE)
            return
        own = None if human else r.get('message_id')
        if own and own not in requests:
            requests[own] = row(c, 'SELECT text FROM aist.messages WHERE id=:m AND project_id=:p AND owner_id=:a',
                                m=own, p=r['project_id'], a=r['owner_id'])['text']
        ids = claim.get('request_message_ids') or ([own] if own else [])
        if not ids or any(mid not in requests for mid in ids):
            remarks.append('Сообщение запроса недоступно для этой версии. Укажите значение в строке '
                           'параметра сделки вручную или оставьте [TBD].')
            return
        claim['request_message_ids'] = ids
        used_requests.update((mid, requests[mid]) for mid in ids)
    def resolved_fact(ref, remarks, *, excerpt=False):
        """Факт по ссылке; пробел в разрешенной версии становится замечанием места.

        Чужой источник, несовпадение SHA и образцы сохраняют обычный отказ."""
        try:
            record = resolve_fact(runs.service, c, r, ref)
            fact = record['data']
            if excerpt and not any(loc['evidence_id'] == fact['evidence'][0]['evidence_id']
                                   and loc['location'] == 'whitespace_exact' for loc in record['provenance'].get('locations', [])):
                refuse('Цитата должна быть найдена в тексте указанной страницы PDF.')
            return fact
        except ApiError as error:
            if not getattr(error, 'missing_basis', False): raise
            remarks.append(error.body['error']['message'])
            return None

    from .note_news import block_id as news_block_id, matches_news_text, selected_snapshot
    saved_news={news_block_id(item):item for item in (selected_snapshot(r, parent) or {}).get('items',[])}
    from .note_external import resolve as resolve_external
    for location, claim, block, within in located_claims(payload):
        if resolve_external(claim, block, payload, r, parent, human):
            continue
        if (block and block['block_id'] in saved_news) or claim.get('origin')=='public_news':
            item=saved_news.get(block['block_id']) if block else None
            if (not item or block['kind']!='paragraph' or claim['origin']!='public_news'
                    or any(claim.get(k) for k in ('fact_refs','calculation_ids','review_candidate_ids','visual_observation_ids'))):
                refuse('Публикацию News Run можно взять только из этой родительской версии.')
            if not matches_news_text(claim['text'], item):
                findings.append(content_finding('news_text_changed',block,within,
                    'Текст публикации изменён относительно сохранённого первоисточника. Сверьте формулировку.'))
            continue
        remarks = []
        content = []
        claim_facts = []
        claim_calculations = []
        element_id = claim.get('row_id') or (block['block_id'] if block else None)
        candidate_ids = list(dict.fromkeys([*claim.get('review_candidate_ids', []),
                                           *inherited_candidates.get(element_id, [])]))
        if candidate_ids or claim.get('origin') == 'source_candidate':
            if payload['result_type'] != 'analytical_note' or not block or block['kind'] != 'paragraph':
                refuse('Кандидат разрешен только в абзаце аналитической записки.')
            if not candidate_ids:
                refuse('Абзац кандидата требует хотя бы одну разрешенную ссылку.')
            if claim.get('origin') != 'source_candidate' and element_id not in inherited_candidates:
                refuse('Ссылка на кандидат требует происхождения source_candidate.')
            for cid in candidate_ids:
                if cid not in allowed_candidates:
                    refuse('Кандидат отсутствует среди разрешенных результатов Run и родителя.', 'resource_not_found')
                candidates[cid] = review_candidates.authorize(runs, c, r, allowed_candidates[cid])
            claim['review_candidate_ids'] = candidate_ids
            claim['origin'] = 'source_candidate'
            content.append(('unchecked_source_candidate', 'Требует сверки с источником. '
                            'Сведения кандидата не допущены в факты. Правильность текста проверяет человек перед принятием.'))
        visual_ids = list(dict.fromkeys([*claim.get('visual_observation_ids', []), *inherited_visual.get(element_id, [])]))
        if visual_ids and payload['result_type'] != 'analytical_note':
            refuse('Визуальные наблюдения разрешены только в аналитической записке.')
        claim_visual = []
        for oid in visual_ids:
            if oid not in allowed_visual:
                refuse('Визуальное наблюдение отсутствует среди разрешенных результатов Run и родителя.', 'resource_not_found')
            observation = visual.authorize(runs, c, r, allowed_visual[oid])
            observations[oid] = observation
            claim_visual.append(observation)
        if visual_ids:
            claim['visual_observation_ids'] = visual_ids
            content.append(('unchecked_visual_observation', 'Визуальное извлечение, не проверено. '
                            'Сверка с изображением не выполнена. Правильность расшифровки проверяет человек перед принятием.'))
            if 'amount' in claim and not any(visual.row_agrees(claim, item) for item in claim_visual):
                content.append(('visual_row_mismatch', 'Показатель, сумма или период строки не совпадают с сохраненным визуальным наблюдением.'))
        missing = False
        for ref in claim['fact_refs']:
            fact = resolved_fact(ref, remarks, excerpt=claim.get('origin') == 'source_excerpt')
            if fact is None:
                missing = True
                continue
            facts[(ref['source_version_id'],ref['fact_id'])] = fact
            claim_facts.append(fact)
        for cid in claim['calculation_ids']:
            saved = row(c, """SELECT reply FROM aist.tool_calls WHERE run_id=:r AND project_id=:p AND owner_id=:a
                AND data->>'name'='aist_calculate' AND data->>'state'='completed'
                AND reply->'result'->>'calculation_id'=:id""", r=r['id'],p=r['project_id'],a=r['owner_id'],id=cid)
            if saved is None and cid not in inherited:
                remarks.append(f'Расчет {cid} отсутствует среди разрешенных сохраненных оснований.')
                missing = True
                continue
            calc = checked('Calculation', saved['reply']['result'] if saved else inherited[cid])
            for item in calc['inputs']:
                ref = item['fact']
                fact = resolved_fact(ref, remarks)
                if fact is None:
                    missing = True
                    continue
                facts[(ref['source_version_id'],ref['fact_id'])] = fact
            if missing:
                continue
            calculations[cid] = calc
            claim_calculations.append(calc)
        retained=bool(block and block==previous.get(block['block_id']))
        if (block and block['kind'] == 'paragraph' and not retained and not human
                and claim.get('origin') == 'source_fact' and claim_facts
                and not (claim_calculations or visual_ids or candidate_ids)
                and all(f['amount'] is None and numeric_table_passage(f['raw_text']) for f in claim_facts)):
            refuse('Пересказ финансовой таблицы требует отдельных числовых оснований с единицей '
                   'и периодом. Используй fact.amount или visual_observation_ids вместо общей цитаты.')
        editorial=claim.get('origin')=='human_editorial' and (human or retained)
        if editorial and not (claim['fact_refs'] or claim['calculation_ids'] or visual_ids or candidate_ids):
            # Свободные условия сделки проверяет человек. За пределами этих разделов
            # ручное число без основания сохраняет прежнюю блокировку.
            if (re.search(r'\d', claim['text'])
                    and section_titles[block['block_id']].strip().casefold() not in REQUEST_SECTIONS):
                findings.append(dict(code='unsupported_editorial_number',severity='error',block_id=block['block_id'],message='Числовое утверждение редактора не имеет сохраненного основания. Оно не считается проверенными данными.'))
            continue
        if claim.get('origin') == 'template':
            if not block or block['kind'] != 'paragraph' or claim['text'] not in TEMPLATE_TEXTS:
                remarks.append(TEMPLATE_ADVICE)
            collected.extend((location, remark) for remark in remarks)
            continue
        if claim.get('origin') == 'user_request':
            resolve_request(claim, block, remarks)
        elif not (claim['fact_refs'] or claim['calculation_ids'] or visual_ids or candidate_ids):
            # Схема допускает вывод без ссылок; такое утверждение сохраняется с замечанием.
            content.append(('unreferenced_claim', 'Утверждение должно иметь сохраненные основания.'))
        if claim.get('origin')=='human_editorial' and not editorial:
            remarks.append('Генерация не создает человеческую редактуру или принятие.')
        if claim.get('origin') == 'source_excerpt':
            if (not block or block['kind'] != 'paragraph' or len(claim_facts) != 1
                    or claim_calculations or candidate_ids or visual_ids or missing):
                refuse('Для цитаты нужен один доступный текстовый факт из выбранного PDF.')
            old_claim = previous.get(block['block_id'], {}).get('claim')
            quoted = source_excerpt_text(claim_facts[0], retained=claim == old_claim)
            if quoted is None and numeric_table_passage(claim_facts[0]['raw_text']):
                refuse('Финансовую таблицу нельзя сохранять сплошной цитатой. Используй отдельные '
                       'числовые основания с единицей и периодом: fact.amount или visual_observation_ids.')
            if quoted is None or claim['text'] not in ('Цитата из PDF', quoted, LEGACY_EXCERPT_PREFIX + quoted):
                refuse('Цитата должна полностью совпадать с основным фрагментом источника.')
            # Сохраняем координаты ручного форматирования старой цитаты при редактировании версии.
            if claim['text'] != LEGACY_EXCERPT_PREFIX + quoted:
                claim['text'] = quoted
            content.append(('unchecked_source_excerpt', 'Текст взят дословно из PDF. Значение и его смысл требуют сверки с источником.'))
        elif missing:
            remarks.append('Числовая проверка этого места не выполнена: основание отсутствует.')
        elif claim['fact_refs'] or claim['calculation_ids']:
            if 'amount' in claim:
                content.extend(row_remarks(runs, c, r, claim, claim_facts, claim_calculations))
                prior = previous_rows.get(claim['row_id'])
                label_changed = prior and (prior['label'] != claim['label'] or claim['row_id'] in flagged_labels)
                if label_changed and not any(code == 'row_label_mismatch' for code, _ in content):
                    from .note_standard import same_row_metric
                    if not any(same_row_metric(claim, f['metric']) for f in claim_facts):
                        content.append(('row_label_mismatch', 'Название показателя отличается от исходного. '
                                        'Сверьте название строки с её основанием.'))
            elif payload['result_type'] == 'evidence_answer':
                durations = printed_durations(c, claim['text'], claim_facts)
                content.extend(paragraph_remarks(claim, claim_facts, claim_calculations, facts, findings, block,
                                                 printed_quantities=durations, claim_visual=claim_visual,
                                                 capacity_pages=capacity_source_pages(c, claim['text'], claim_facts)))
        collected.extend((location, remark) for remark in remarks)
        for code, message in content:
            finding = content_finding(code, block, within, message)
            if payload['result_type'] == 'analytical_note' and code in {
                    'unchecked_visual_observation', 'unchecked_source_candidate', 'unchecked_source_excerpt'}:
                finding['severity'] = 'warning'
            if 'row_id' in claim:
                finding['row_id'] = claim['row_id']
            elif code == 'quantity_mismatch':
                match = re.search(r'Количество или срок «([^»]+)»', message)
                if match and claim['text'].count(match.group(1)) == 1:
                    finding['quote'] = match.group(1)
            findings.append(finding)
            content_findings.append(finding)
    blocking = [finding for finding in content_findings if finding['severity'] == 'error']
    if standalone and (collected or blocking):
        refuse(feedback(collected, blocking, block_locations(payload)))
    basis = {'facts':[facts[k] for k in sorted(facts)],'calculations':[calculations[k] for k in sorted(calculations)]}
    if used_requests:
        basis['request_messages'] = [dict(message_id=mid, text=used_requests[mid]) for mid in sorted(used_requests)]
    if observations:
        basis['visual_observations'] = [observations[key] for key in sorted(observations)]
    if candidates:
        basis['review_candidates'] = [candidates[key] for key in sorted(candidates)]
    return basis


def row_remarks(runs, c, r, claim, claim_facts, claim_calculations):
    """Сумма и подпись строки против основания; несовпадение возвращается парой (код, замечание)."""
    adopt_period_label(claim, claim_facts)
    from .note_standard import same_row_metric, same_growth_metric
    matched_facts = [f for f in claim_facts if claim['amount']==f['amount'] and claim['period_label']==f['period']['label']]
    matched_calculations = []
    for calc in claim_calculations:
        inputs = [resolve_fact(runs.service, c, r, i['fact'])['data'] for i in calc['inputs']]
        if claim['amount'] == calc['rounded'] and claim['period_label'] == ' к '.join(
                fact['period']['label'] for fact in reversed(inputs)):
            matched_calculations.append((calc, inputs))
    if matched_calculations:
        if any(calc['formula_id'] == 'growth_percent.v1' and same_growth_metric(claim, inputs[-1]['metric'])
               for calc, inputs in matched_calculations):
            return []
        return [('row_label_mismatch', 'Название расчётной строки не соответствует показателю входных данных.')]
    named = [f for f in matched_facts if same_row_metric(claim, f['metric'])]
    scopes = {(f.get('subject'), f.get('reporting_basis'), f.get('nature')) for f in named}
    if len(scopes) > 1:
        return [('row_basis_mismatch', 'Одинаковое значение найдено у разных субъектов, в разных видах отчётности '
                 'или одновременно как факт и прогноз. Уточните основание строки.')]
    if named:
        return []
    if matched_facts:
        return [('row_label_mismatch', 'Значение и период совпадают, но в основании указан другой показатель. Сверьте название строки.')]
    remark = (f"Сумма {amount_text(claim['amount'])} и период «{claim['period_label']}» "
              'не совпадают с основанием.')
    # Ожидаемая подпись называется только по однозначно указанному основанию.
    if len(claim_facts) == 1 and not claim_calculations:
        fact = claim_facts[0]
        if fact['amount'] is None:
            remark += ' Качественное основание не содержит числовой суммы.'
        else:
            remark += f" Основание: {amount_text(fact['amount'])}, период «{fact['period']['label']}»."
    return [('row_basis_mismatch', remark)]


def content_check(remarks, code, check, *args, **kwargs):
    """Результат проверки содержания или ее замечание под своим кодом; чужие отказы не перехватываются."""
    try:
        return check(*args, **kwargs)
    except ApiError as error:
        if error.code != 'validation_failed':
            raise
        remarks.append((code, error.body['error']['message']))
        return None


def printed_durations(c, text, facts):
    """Read only the already resolved source pins when a duration needs its PDF layout."""
    if not re.search(r'\d\s*(?:дн(?:ей|я)|сут(?:ок|ки)|минут)', text, re.I):
        return {}
    from .source_cards import quoted_durations
    pages = {}
    result = {}
    for fact in facts:
        if fact['amount'] is not None:
            continue
        pin = fact['source']
        for evidence in fact['evidence']:
            page = evidence['locator'].get('pdf_page')
            if page is None:
                continue
            key = (pin['source_version_id'], pin['sha256'], page)
            if key not in pages:
                pages[key] = row(c, 'SELECT text,layout_text FROM aist.source_pages WHERE version_id=:v AND sha256=:sha AND page=:p',
                                 v=key[0], sha=key[1], p=page)
            if pages[key]:
                source = pages[key]
                durations = quoted_durations(source['text'], source['layout_text'], evidence['quote'])
                if durations:
                    result[evidence['quote']] = durations
    return result


def capacity_source_pages(c, text, facts):
    """Геометрия только основного фрагмента выбранного, уже разрешённого источника."""
    if not re.match(r'Производственные мощности:', text, re.I):
        return []
    pages = {}
    result = []
    for fact in primary_statements(facts):
        pin = fact['source']
        for evidence in fact['evidence']:
            if ' '.join(evidence['quote'].split()) != ' '.join(fact['raw_text'].split()):
                continue
            page = evidence['locator'].get('pdf_page')
            if page is None or evidence['source'] != pin:
                continue
            key = (pin['source_version_id'], pin['sha256'], page)
            if key not in pages:
                pages[key] = row(c, 'SELECT text,layout_text FROM aist.source_pages WHERE version_id=:v AND sha256=:sha AND page=:p',
                                 v=key[0], sha=key[1], p=page)
            source = pages[key]
            if (source and source['layout_text']
                    and ' '.join(evidence['quote'].split()) in ' '.join(source['text'].split())):
                result.append(dict(quote=evidence['quote'], layout=source['layout_text']))
    return result


def unsupported_data_years(text):
    """Маскируем только год в поддержанной оговорке, не соседние утверждения."""
    pattern = (r'(?<!\w)(?:данные|(?:промежуточн(?:ые|ых)\s+)?результат(?:ы|ах|ов))\s+'
               r'(?:за\s+)?(?P<year>(?:19|20)\d{2})\s+год(?:а)?\s+'
               r'нельзя\s+подтвердить\s+по\s+доступным\s+основаниям(?!\w)')

    def omit_year(match):
        prefix = re.split(r'[.!?;]', text[:match.start()])[-1]
        if re.search(r'\b(?:не|ни|нельзя)\b', prefix, re.I):
            return match[0]
        start, end = match.start('year') - match.start(), match.end('year') - match.start()
        return match[0][:start] + ' ' * (end - start) + match[0][end:]

    return re.sub(pattern, omit_year, text, flags=re.I)


def directional_percentages(text, calculations):
    """Словесное направление разрешает только свое вхождение модуля growth_percent.v1."""
    pattern = (r'(?<!\w)(?P<verb>снизил(?:ась|ись|ся)|вырос(?:ла|ли)?)\s+на\s+'
               r'(?P<percent>(?P<sign>[+\-−])?(?P<value>\d+(?:[.,]\d+)?)\s*%)')
    conflicts = []

    def verify(match):
        prefix = re.split(r'[.!?;,]', text[:match.start()])[-1]
        if re.search(r'\b(?:не|ни|нельзя)\b', prefix, re.I):
            return match[0]
        if re.match(r'\s*(?:п\.?\s*п\.?|процентн\w*\s+пункт)', text[match.end():], re.I):
            return match[0]
        magnitude = Decimal(match['value'].replace(',', '.'))
        direction = -1 if match['verb'].lower().startswith('сниз') else 1
        values = {Decimal(calc['rounded']['value']) for calc in calculations
                  if calc['formula_id'] == 'growth_percent.v1'
                  and calc['rounded']['unit'] == 'percent' and calc['rounded']['scale'] == '1'
                  and calc['rounded']['currency'] is None
                  and abs(Decimal(calc['rounded']['value'])) == magnitude}
        if values and (values != {direction * magnitude} or
                       match['sign'] and (match['sign'] == '+') != (direction > 0)):
            conflicts.append(match[0])
        if not match['sign'] and magnitude > 0 and values == {direction * magnitude}:
            start = match.start('percent') - match.start()
            return match[0][:start] + ' ' * (len(match[0]) - start)
        return match[0]

    return re.sub(pattern, verify, text, flags=re.I), conflicts


def qualitative_series_years(text, facts):
    """Годы собственного ряда не становятся периодами других утверждений."""
    year = r'(?:19|20)\d{2}'
    year_range = rf'(?:с\s+)?(?P<first>{year})\s*(?:по|до|[-\u2013\u2014])\s*(?P<last>{year})'
    years = rf'(?:{year_range}|{year}(?:\s*(?:,|и)\s*{year})+)'
    pattern = (rf'(?<!\w)(?:ряд(?:ы|а|ов)?\b[^\d.!?;]*?\s+за\s+|в\s+динамике\s+за\s+|в\s+каждом\s+году\s+)'
               rf'(?P<years>{years})(?![\w.,/\-]\d)')

    def verify(match):
        if match['first']:
            first, last = int(match['first']), int(match['last'])
            if first > last:
                return match[0]
            required = set(range(first, last + 1))
        else:
            required = {int(value) for value in re.findall(year, match['years'])}
        for fact in facts:
            period = fact['period']
            if fact['amount'] is not None or fact['nature'] != 'source_statement' or period['kind'] != 'interval':
                continue
            if not all(period['start'] <= f'{value}-01-01' and f'{value}-12-31' <= period['end'] for value in required):
                continue
            confirmed = set()
            for evidence in fact['evidence']:
                if 'pdf_page' not in evidence['locator']:
                    continue
                quote = evidence['quote']
                confirmed.update(int(value) for value in re.findall(rf'(?<![\w./-])({year})(?![\w./-])', quote))
                for interval in re.finditer(year_range, quote, re.I):
                    first, last = int(interval['first']), int(interval['last'])
                    if first <= last:
                        confirmed.update(range(first, last + 1))
            if required <= confirmed:
                start, end = match.start('years') - match.start(), match.end('years') - match.start()
                masked = re.sub(year, lambda item: ' ' * len(item[0]), match['years'])
                return match[0][:start] + masked + match[0][end:]
        return match[0]

    return re.sub(pattern, verify, text, flags=re.I)


# Общие формы количеств нужны и для цитат списка, и для связи срока плана с показателем.
QUANTITY_NUMBER = r'[+\-−]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?'
QUANTITY_BOUND = r'не\s+более|не\s+менее|более|свыше|от|до'
NETWORK_UNIT = r'(?:(?:зарядных|активных)\s+)?станций'
QUANTITY_UNIT = (rf'(?:лет|год(?:а)?|месяц(?:а|ев)?|мес\.|минут(?:а|ы)?|дн(?:ей|я)|сут(?:ок|ки)|'
                 rf'человек|чел\.?|скачиваний|активных\s+займов|заявок|клиентов|кредитор(?:а|ов)?|'
                 rf'выпуск(?:а|ов)?|городов|{NETWORK_UNIT}|(?:новых\s+)?пользователей)')
QUANTITY = re.compile(
    rf'(?<!\w)(?:(?:количество|число)\s+(?P<unit_before>{NETWORK_UNIT})\s+)?'
    rf'(?P<bound>{QUANTITY_BOUND})?\s*(?P<value>{QUANTITY_NUMBER})(?:[хx])?(?P<plus>\+)?\s*'
    rf'(?P<scale>тыс\.?|млн\.?)?\s*(?(unit_before)|(?P<unit>{QUANTITY_UNIT}))(?!\w)', re.I)
YEAR_WORD = r'(?P<year>(?:19|20)\d{2})\s+год(?:а|у|ы)?(?!\w)'


def source_list_text(text):
    """Снимаем дефис списка перед количеством только при явной структуре списка.

    Текстовый пункт подтверждает многострочный список; двойной знак без такой
    структуры остается неоднозначным. Денежные суммы и проценты не затрагиваются.
    """
    lines = text.splitlines(keepends=True)
    listed = sum(bool(re.match(r'^\s*-\s+', line)) for line in lines) >= 2
    text_item = any(re.match(r'^\s*-\s+[А-Яа-яA-Za-z]', line) for line in lines)
    if not (listed and text_item):
        return text
    def remove_marker(match):
        return ' ' * len(match[0]) if QUANTITY.match(text, match.end()) else match[0]
    return re.sub(r'^[ \t]*-[ \t]+(?=[+\-−]?\d)', remove_marker, text, flags=re.M)


def quantity_value(match):
    bound = ' '.join((match['bound'] or '').lower().split())
    bound = {'более': 'more', 'свыше': 'more', 'от': 'lower', 'не менее': 'lower',
             'до': 'upper', 'не более': 'upper'}.get(bound, 'exact')
    if match['plus']:
        bound = 'more' if bound == 'exact' else 'conflicting'
    value = Decimal(re.sub(r'[ \u00a0\u202f]', '', match['value']).replace(',', '.').replace('−', '-'))
    scale = (match['scale'] or '').lower().rstrip('.')
    unit = ' '.join((match['unit'] or match['unit_before']).lower().rstrip('.').split())
    if unit in ('чел', 'человек'):
        unit = 'человек'
    elif unit in ('год', 'года', 'лет'):
        unit = 'год'
    return bound, value, scale, unit


def statement_spans(text):
    """Границы предложений и пунктов, без разделения десятичной дроби или переноса строки."""
    start = 0
    for end in re.finditer(r';|[.!?](?=\s+[А-ЯЁA-Z]|\s*$)', text):
        yield start, end.end(), text[start:end.end()]
        start = end.end()
    if start < len(text):
        yield start, len(text), text[start:]


def quantity_role(text, match):
    """У новых видов количества нужна связь с показателем, помимо числа и единицы.

    Поддержаны установка, охват, размер рынка, план сети и рост клиентской базы.
    Нераспознанная связь не дает нового допуска.
    """
    unit = quantity_value(match)[3]
    if unit not in ('городов', 'станций', 'зарядных станций', 'активных станций',
                    'пользователей', 'новых пользователей', 'человек'):
        return 'legacy'
    start, _, clause = next(item for item in statement_spans(text) if item[0] <= match.start('value') < item[1])
    position = match.start('value') - start
    # Соседний показатель после запятой не наследует действие этого количества.
    left = max((m.end() for m in re.finditer(r',(?=\s)', clause[:position])), default=0)
    right = re.search(r',(?=\s)', clause[position:])
    clause = clause[left:position + right.start() if right else len(clause)]
    if unit == 'человек':
        roles = [label for label, pattern in (('clients', r'клиент\w*'), ('registered', r'зарегистр\w*'),
                 ('staff', r'сотрудник\w*|работник\w*')) if re.search(pattern, clause, re.I)]
        return roles[0] if len(roles) == 1 else 'legacy' if not roles else None
    if unit == 'городов':
        return 'coverage' if re.search(r'географ\w*\s+бизнес\w*|охват\w*|охватыва\w*|присутств\w*', clause, re.I) else None
    if unit.endswith('пользователей'):
        return 'client_growth' if re.search(r'ежемесячн\w*\s+рост|рост\w*\s+клиентск\w*\s+баз\w*', clause, re.I) else None
    if re.search(r'установлен\w*|установить', clause, re.I):
        return 'installed'
    if re.search(r'рын(?:ок|ка|ке)\b', clause, re.I) and re.search(r'может\s+вырасти', clause, re.I):
        return 'market'
    if re.search(r'(?:количество|число)\s+станций|(?:рост\w*|расширени\w*)\s+сети', clause, re.I):
        return 'network_plan'
    return None


def mask_year_words(text):
    # Маскируется вхождение вместе со словом «год», но не соседние числа.
    return re.sub(r'(?<!\w)(?:19|20)\d{2}\s+год(?:а|у|ы)?(?!\w)',
                  lambda match: ' ' * len(match[0]), text, flags=re.I)


def qualitative_plan_years(text, facts):
    """Сроки только подтвержденных качественных выражений, без новых финансовых периодов."""
    quotes = [e['quote'] for f in facts if f['amount'] is None and f['nature'] == 'source_statement'
              for e in f['evidence'] if 'pdf_page' in e['locator']]
    # Полное выражение о размере рынка сохраняет модальность и оба среза.
    for quote in quotes:
        for _, _, sentence in statement_spans(quote):
            if not re.search(r'рын(?:ок|ка)\b.*может\s+вырасти', sentence, re.I | re.S):
                continue
            try:
                check_units(sentence, [])
            except ApiError as error:
                if error.code != 'validation_failed':
                    raise
                continue
            quantities = list(QUANTITY.finditer(sentence))
            if not quantities or not all(quantity_role(sentence, q) == 'market' for q in quantities):
                continue
            pattern = r'\s+'.join(re.escape(word) for word in sentence.strip().split())
            text = re.sub(pattern, lambda match: mask_year_words(match[0]), text, flags=re.I)

    event = re.compile(r'(?<!\w)(?:начало\s+продаж|запуск)\s+'
                       rf'(?P<object>[а-яё]+(?:\s+[а-яё]+){{0,2}}?)\s+в\s+{YEAR_WORD}', re.I)
    future = r'план\w*|ожида\w*|позволит|фактор\w*\s+роста|новый\s+источник'
    conditions = (r'\bпривлечени\w*\s+финансировани\w*', r'\bсохранени\w*\s+лидирующ\w*\s+позиц\w*')

    def planned(sentence):
        contrary = (r'\b(?:состоял\w*|произош\w*|осуществлен\w*|осуществлён\w*)|'
                    r'\bне\s+(?:план\w*|ожида\w*|наход\w*|обусловлен\w*)')
        return re.search(future, sentence, re.I) and not re.search(contrary, sentence, re.I)

    def conditions_kept(sentence, required):
        for pattern in required:
            match = re.search(pattern, sentence, re.I)
            if not match or re.search(r'\b(?:без|не)\s+(?:\w+\s+){0,2}$', sentence[:match.start()], re.I):
                return False
        return True

    def attached_deadline(sentence, quantity, deadline):
        if deadline.end() <= quantity.start():
            return bool(re.fullmatch(r'\s+увеличить\s*', sentence[deadline.end():quantity.start()], re.I))
        if quantity.end() <= deadline.start():
            return not sentence[quantity.end():deadline.start()].strip()
        return False

    source_plans = []
    source_events = set()
    for quote in quotes:
        for _, _, sentence in statement_spans(quote):
            for match in event.finditer(sentence):
                if planned(sentence):
                    source_events.add((' '.join(match['object'].lower().split()), match['year']))
            deadline = re.search(rf'\bк\s+{YEAR_WORD}', sentence, re.I)
            if not deadline or not planned(sentence):
                continue
            for quantity in QUANTITY.finditer(sentence):
                if quantity_role(sentence, quantity) == 'network_plan' and attached_deadline(sentence, quantity, deadline):
                    required = [pattern for pattern in conditions if re.search(pattern, sentence, re.I)]
                    source_plans.append((deadline['year'], quantity_value(quantity), required))

    pieces = []
    for _, _, sentence in statement_spans(text):
        if planned(sentence):
            def event_year(match):
                key = (' '.join(match['object'].lower().split()), match['year'])
                return mask_year_words(match[0]) if key in source_events else match[0]
            sentence = event.sub(event_year, sentence)
            quantities = [q for q in QUANTITY.finditer(sentence) if quantity_role(sentence, q) == 'network_plan']
            def plan_year(match):
                for year, quantity, required in source_plans:
                    linked = any(quantity_value(q) == quantity and attached_deadline(sentence, q, match) for q in quantities)
                    if year == match['year'] and linked and conditions_kept(sentence, required):
                        return mask_year_words(match[0])
                return match[0]
            sentence = re.sub(rf'\bк\s+{YEAR_WORD}', plan_year, sentence, flags=re.I)
        pieces.append(sentence)
    return ''.join(pieces)


def visual_series_years(text, observations):
    """Годы описания визуального ряда не подтверждают сумму или период обычного факта."""
    if not observations:
        return text
    year = r'(?:19|20)\d{2}'
    years = rf'{year}(?:\s*(?:,|и)\s*{year})*'
    series = re.compile(rf'\bряд\s+(?P<metric>[^\d.!?;,]+?)\s+за\s+(?P<actual>{years})\s+год(?:ы|а|у)?'
                        rf'(?:\s+и\s+прогноз\s+(?:компании\s+)?на\s+(?P<forecast>{years})\s+год(?:ы|а|у)?)?(?!\w)', re.I)

    def supported(metric, values, nature):
        required = set(re.findall(year, values))
        found = set()
        for observation in observations:
            numeric = observation['numeric']
            if not numeric or observation['verification_status'] != 'unchecked' or numeric['nature'] != nature:
                continue
            label = re.sub(r'\s*\([^()]*\)\s*$', '', numeric['metric']).casefold().strip()
            forms = {label}
            if label.endswith('а'):
                forms.update((label[:-1] + 'ы', label[:-1] + 'и'))
            if metric.casefold().strip() not in forms:
                continue
            period = numeric['period']
            if period['kind'] == 'interval' and period['start'][4:] == '-01-01' and period['end'] == period['start'][:4] + '-12-31':
                found.add(period['start'][:4])
        return required <= found

    def verify(match):
        prefix = next(part[:match.start() - start] for start, end, part in statement_spans(text)
                      if start <= match.start() < end)
        if not re.search(r'диаграмм\w*|изображени\w*|визуальн\w*', prefix, re.I):
            return match[0]
        if not supported(match['metric'], match['actual'], 'reported_actual'):
            return match[0]
        if match['forecast'] and not supported(match['metric'], match['forecast'], 'forecast'):
            return match[0]
        return re.sub(year, lambda item: ' ' * len(item[0]), match[0])

    text = series.sub(verify, text)
    # Это явное отсутствие расчета, а не использование наблюдений как его входов.
    unavailable = re.compile(rf'\bтемп\s+роста\s+(?P<metric>[^\d.!?;,]+?)\s+'
                             rf'(?P<current>{year})\s+года\s+к\s+(?P<previous>{year})\s+году\s+'
                             r'нельзя\s+подтвердить\s+по\s+доступным\s+основаниям,\s+расчет\s+не\s+выполнялся(?!\w)', re.I)
    def no_calculation(match):
        values = match['current'] + ' ' + match['previous']
        return mask_year_words(match[0]) if supported(match['metric'], values, 'reported_actual') else match[0]
    return unavailable.sub(no_calculation, text)


def period_date_numbers(period):
    """Числа типизированных дат для предварительной проверки наличия основания.

    Подпись не является набором сумм или источником неизвестных дат. Собственный
    период утверждения отдельно проверяет check_periods, сумму проверяет check_units.
    """
    return {Decimal(part) for boundary in (period['start'], period['end']) if boundary
            for part in boundary.split('-')}


def rating_date_finding(text, claim_facts, block):
    """Презентация подтверждает рейтинг на дату документа, но не его действие сегодня."""
    if not claim_facts or not all(f.get('nature') == 'source_statement' and f.get('amount') is None and
            any(e['locator'].get('pdf_page') for e in f['evidence']) for f in claim_facts):
        return None
    if not any('рейтинг' in f.get('raw_text', '').casefold() for f in claim_facts):
        return None
    for _, _, sentence in statement_spans(text):
        current = re.search(r'\b(?:имеет\s+(?:кредитный\s+)?рейтинг|действующий\s+(?:кредитный\s+)?рейтинг)\b', sentence, re.I)
        if current is None or sentence.rstrip().endswith('?') or re.search(
                r'\b(?:если|при условии|гипотетически)\b|\bне\s*$', sentence[:current.start()], re.I):
            continue
        # Принимаем явную привязку к документу. Год в соседнем предложении не задаёт дату рейтинга.
        as_of = re.search(r'\bна\s+(?:дату|момент)\s+(?:публикации\s+|подготовки\s+)?(?:этой\s+)?'
                          r'(?:презентации|отч[её]та|документа)\b', sentence, re.I)
        if as_of and not re.search(r'\b(?:сегодня|сейчас|на текущий момент)\b', sentence, re.I):
            continue
        return dict(code='rating_date_unspecified', severity='error', block_id=block['block_id'] if block else None,
                    message='Укажите, что рейтинг приведён на дату документа. Эта презентация сама по себе '
                            'не подтверждает, что рейтинг действует сегодня.')


def paragraph_remarks(claim, claim_facts, claim_calculations, facts, findings, block, *, printed_quantities=None, claim_visual=(), capacity_pages=()):
    """Независимые проверки абзаца: числа, единицы, периоды; остаток только после их успеха.

    Возвращает пары (код, замечание) содержательных несовпадений."""
    remarks = []
    rating = rating_date_finding(claim['text'], claim_facts, block)
    if rating:
        findings.append(rating)
    text = unsupported_data_years(claim['text'])
    text, direction_conflicts = directional_percentages(text, claim_calculations)
    unit_text = text
    text = visual_series_years(text, claim_visual)
    text = qualitative_plan_years(text, claim_facts)
    # Допуск года ряда не скрывает сумму или процент от существующей проверки единиц.
    numeric_facts = [f for f in claim_facts if f['amount'] is not None]
    financial_basis = bool(numeric_facts or claim_calculations)
    if not financial_basis:
        text = qualitative_series_years(text, claim_facts)
    allowed=set()
    for f in claim_facts:
        allowed |= period_date_numbers(f['period'])
        if f['amount']:allowed.add(Decimal(f['amount']['value']))
        else:
            # PDF qualifications retain source numerals (e.g. footnotes).
            # JSON numbers must come from normalized amounts/periods;
            # registry identifiers are not monetary or period evidence.
            quotes = [e['quote'] for e in f['evidence'] if 'pdf_page' in e['locator']]
            for quote in quotes:
                allowed |= number_tokens(quote,source_quote=True)
    for calc in claim_calculations:
        allowed|={Decimal(calc['rounded']['value']),Decimal(calc['exact']['numerator']),Decimal(calc['exact']['denominator'])}
        for item in calc['inputs']:
            period=facts[(item['fact']['source_version_id'],item['fact']['fact_id'])]['period']
            allowed |= period_date_numbers(period)
    if not financial_basis:
        # Дефис между двумя названными годами не является знаком суммы.
        # Обе границы должны быть напечатаны вместе в цитате привязанного факта.
        quotes = [e['quote'] for f in claim_facts for e in f['evidence'] if 'pdf_page' in e['locator']]
        text = cited_year_intervals(text, quotes)
    # Диапазон кварталов проверяется целиком до разбора знака отдельного числа.
    range_periods = [f['period'] for f in (numeric_facts if financial_basis else claim_facts)]
    for calc in claim_calculations:
        range_periods.extend(facts[(i['fact']['source_version_id'], i['fact']['fact_id'])]['period'] for i in calc['inputs'])
    text = quarter_ranges(text, range_periods, remarks, qualitative_facts=[] if financial_basis else claim_facts)
    numeric_position = len(remarks)
    # Даты оговорки не подтверждают период суммы или расчета.
    # Оговорки с другими периодами сохраняются отдельным абзацем.
    period_facts = numeric_facts if financial_basis else claim_facts
    periods = [f['period'] for f in period_facts]
    for calc in claim_calculations:
        periods.extend(facts[(i['fact']['source_version_id'],i['fact']['fact_id'])]['period'] for i in calc['inputs'])
    source_quotes = [] if financial_basis else [e['quote']
        for f in claim_facts if f['amount'] is None and f['nature']=='source_statement'
        for e in f['evidence'] if 'pdf_page' in e['locator']]
    amounts=[f['amount'] for f in claim_facts if f['amount']]+[x['rounded'] for x in claim_calculations]
    # Дословное цитирование оговорки не разрешает денежную сумму.
    # Единицы проверяются до маскирования годов ряда и дословных цитат.
    units_verified = content_check(remarks, 'unit_mismatch', check_units, unit_text, amounts) is not None
    if direction_conflicts:
        if units_verified:
            remarks.append(('unit_mismatch', 'Направление процентного изменения не совпадает с однозначным расчетом: '
                            + '; '.join(direction_conflicts)))
        units_verified = False
    if not financial_basis:
        text = source_event_numbers(text, claim_facts)
        text = company_event_numbers(text, claim_facts)
        text = portfolio_numbers(text, claim_facts)
        text = market_comparison_years(text, claim_facts, remarks)
        if capacity_pages:
            from .source_cards import capacity_numbers
            text = capacity_numbers(text, capacity_pages)
    remaining = content_check(remarks, 'period_mismatch', check_periods, text, periods, source_quotes=source_quotes)
    # Проверенный период уже разобран: короткий год в 9M’22 не является отдельной суммой.
    numeric_remarks = []
    tokens = content_check(numeric_remarks, 'ambiguous_sign', number_tokens, remaining if remaining is not None else text)
    unsupported = tokens - allowed if tokens is not None else set()
    if unsupported:
        values=', '.join(format(value,'f') for value in sorted(unsupported))[:300]
        numeric_remarks.append(('unsupported_numbers', 'Числа '+values+' не имеют сохраненного основания. '+NUMBERS_ADVICE))
    remarks[numeric_position:numeric_position] = numeric_remarks
    # Остаток текста оценивается только после успешных проверок единиц и периодов.
    if remaining is not None and units_verified:
        remaining = content_check(remarks, 'unit_mismatch', check_units, remaining, amounts)
        if remaining is not None:
            quotes = [e['quote'] for f in claim_facts if f['amount'] is None for e in f['evidence'] if 'pdf_page' in e['locator']]
            remaining = content_check(remarks, 'quantity_mismatch', check_source_quantities, remaining, quotes,
                                      printed=printed_quantities or {})
            if remaining is not None:
                remaining = source_names(remaining, quotes)
    else:
        remaining = None
    if remaining is not None and re.search(r'\d',remaining):
        unparsed = ', '.join(re.findall(r'\S*\d\S*', remaining))[:160]
        findings.append(dict(code='unsupported_numeric_form',severity='error',block_id=block['block_id'] if block else None,
            message=f'Числовая формулировка не распознана полностью. Нераспознанные фрагменты: {unparsed}. '+NUMERIC_FORM_ADVICE))
    return remarks


def check_source_quantities(text, quotes, *, printed=None):
    """Compare each complete nonfinancial quantity with one source statement, including its bound."""
    source = set()
    for quote in quotes:
        source_text = source_list_text(quote)
        statements = {(quantity_value(match), quantity_role(source_text, match)) for match in QUANTITY.finditer(source_text)}
        for value, unit, bound in (printed or {}).get(quote, []):
            statement = quantity_value(QUANTITY.fullmatch(bound + ' ' + value + ' ' + unit))
            # The source bound overrides the apparent fixed value caused by PDF object order.
            statements.discard((('exact', *statement[1:]), 'legacy'))
            statements.add((statement, 'legacy'))
        source.update(statements)
    unmatched=[]
    def verify(match):
        role = quantity_role(text, match)
        if role is None or (quantity_value(match), role) not in source:
            unmatched.append(' '.join(match[0].split()))
        return ' '*len(match[0])
    remaining=QUANTITY.sub(verify,text)
    if unmatched:
        listed=', '.join('«'+item+'»' for item in dict.fromkeys(unmatched))[:300]
        refuse(f'Количество или срок {listed} не совпадают с цитатой основания.')
    return remaining


def cited_year_intervals(text, quotes):
    interval = r'(?<![\w+−-])((?:19|20)\d{2})[ \t]*[-–—][ \t]*((?:19|20)\d{2})(?![\w-])'
    pairs = {(m[1], m[2]) for quote in quotes for m in re.finditer(interval, quote) if m[1] < m[2]}
    pattern = interval + r'(?=\s+год(?:ах|ы|ов|а)\b)'
    return re.sub(pattern, lambda m: ' ' * len(m[0]) if (m[1], m[2]) in pairs else m[0], text, flags=re.I)


def source_names(text, quotes):
    """Consume a literal alphanumeric name identified as such in its own quotation.

    Financial units and periods have already been checked; a name does not mask the rest of the paragraph.
    """
    token = r'[A-Za-zА-Яа-яЁё]+\d+[A-Za-zА-Яа-яЁё\d]*'
    named = re.compile(rf'\b(?:исследование|исследовател[ья]|агентство|компания|отч[её]т)\s+[«"]?({token})(?!\w)', re.I)
    names = {match[1].casefold() for quote in quotes for match in named.finditer(quote)
             if not re.fullmatch(r'(?:[QHM]\d+|(?:RUB|USD|EUR)\d+)', match[1], re.I)}
    # B2B и Ba1 являются обозначениями, когда такая же запись есть в цитате.
    for quote in quotes:
        names.update(m[0].casefold() for m in re.finditer(r'\bB2[BCG]\b', quote, re.I))
        names.update(m[1].casefold() for m in re.finditer(r"\bMoody[’']?s\s*:\s*([ABCa]{1,3}[123])\b", quote, re.I))
    return re.sub(rf'(?<!\w){token}(?!\w)',
                  lambda match: ' ' * len(match[0]) if match[0].casefold() in names else match[0], text)


def invoke(runs,c,r,epoch,name,args):
    if name=='aist_get_facts':
        checked('GetFactsArgs',args)
        if args.get('extract') is not None:raise ApiError('tool_unavailable')
        return checked('GetFactsResult',{'facts':[resolve_fact(runs.service,c,r,ref)['data'] for ref in args['refs']]})
    if name=='aist_search_evidence':
        def authorize_embedding(connection):
            current = runs.locked(connection, r['id'])
            runs.fence(connection, current, epoch)
            from .collection import limits, state
            if limits(current) is not None and state(current)['closed']:
                refuse('Сбор оснований закрыт: платный поиск по смыслу не выполняется. Используй сохраненные факты.', 'budget_exceeded')
        return search_evidence(runs.service, c, r, args, runs.settings, authorize_embedding=authorize_embedding)
    if name=='aist_calculate':
        checked('CalculateArgs',args)
        records={item['name']:resolve_fact(runs.service,c,r,item['fact']) for item in args['inputs']}
        return growth(ident('calc'),args,records)
    if name=='aist_read_source':
        checked('ReadSourceArgs',args)
        pinned_version(runs.service,c,r,args['source_version_id'])
        v=runs.service.scoped(c,r['owner_id'],r['project_id'],'source_versions',args['source_version_id'])
        _,problem=runs.service.inspect(c,v)
        if problem or v['processing_state']!='ready':raise ApiError('tool_failed')
        page=row(c,'SELECT text,diagram_regions FROM aist.source_pages WHERE version_id=:v AND page=:p',v=v['id'],p=args['page'])
        if not page:raise ApiError('tool_failed')
        return validate_definition('PageContent',dict(source={'source_id':v['source_id'],'source_version_id':v['id'],'sha256':v['sha256']},page=args['page'],text=page['text'],evidence=[],
            **({'diagram_regions':page['diagram_regions']} if page['diagram_regions'] else {})))
    if name=='aist_request_input':
        checked('RequestInputArgs',args)
        from .collection import is_dialogue
        if is_dialogue(r) and any(item['role'] == 'assistant' for item in (r.get('checkpoint') or {}).get('dialogue', [])):
            refuse('Уточнение уже задано. Сохрани ответ по доступным основаниям или объясни ограничение.')
        if args['expected_revision']!=r['revision']:raise ApiError('revision_conflict',args['expected_revision'],r['revision'])
        active=execute(c,"SELECT count(*) FROM aist.model_calls WHERE run_id=:r AND data->>'transport_state' IN ('reserved','attempted','sent','unknown')",r=r['id']).scalar_one()
        if active:raise ApiError('outcome_unknown')
        from .conversation import append_dialogue
        checkpoint=append_dialogue(r['checkpoint'],'assistant',args['prompt'])
        checkpoint['safe_resume']=True
        execute(c,'UPDATE aist.runs SET checkpoint=CAST(:cp AS jsonb) WHERE id=:r',
                cp=json_value(checkpoint),r=r['id'])
        runs.append_message(c,r,'assistant',args['prompt'])
        waiting={'id':ident('waiting'),'prompt':args['prompt'],'created_at':now()}
        runs.transition(c,r,'waiting_for_user',waiting=waiting)
        runs.event(c,r,'user_input_required',waiting)
        execute(c,'UPDATE aist.runs SET worker_id=NULL,lease_until=NULL WHERE id=:r',r=r['id'])
        return waiting
    if name!='aist_save_artifact':raise ApiError('tool_unavailable')
    checked('SaveArtifactArgs',args)
    if args['artifact_id']!=r['input']['target_artifact_id'] or args['parent_version_id']!=r['input']['base_version_id']:raise ApiError('resource_not_found')
    if args['expected_revision']!=r['input']['expected_artifact_revision']:raise ApiError('revision_conflict',args['expected_revision'],r['input']['expected_artifact_revision'])
    p=args['payload']
    if p['result_type']=='evidence_answer' and any(number_tokens(item) for item in p['content']['missing_information']):
        refuse('Числа ответа должны быть в утверждениях со ссылками, а не в пояснении недостающих сведений.')
    if p['result_type']!=r['input']['requested_result_type'] or p['result_type'] not in ('evidence_answer','analytical_note'):raise ApiError('unsupported_result_type')
    active=execute(c,"SELECT count(*) FROM aist.model_calls WHERE run_id=:r AND data->>'transport_state' IN ('reserved','attempted','sent','unknown')",r=r['id']).scalar_one()
    if active:raise ApiError('outcome_unknown')
    if not r['input']['source_versions']:
        content=p['content']
        reply='\n'.join(content['missing_information']).strip()
        if content['claims'] or not reply or len(reply)>20000 or number_tokens(reply):
            refuse('Без источников допустимы только объяснение работы AIST и просьба о материалах, без финансовых утверждений.')
        mid=runs.append_message(c,r,'assistant',reply)
        runs.transition(c,r,'completed')
        return runs.message_view(row(c,'SELECT * FROM aist.messages WHERE id=:id',id=mid))
    from .versions import persist, TBD
    view=persist(runs,c,r,p,args['artifact_id'],args['parent_version_id'],args['expected_revision'],
                 news_events=args.get('news_events'), ownership_review=args.get('ownership_review'))
    version=view['version']['id']
    summary=p['content']['title'] if p['result_type']=='analytical_note' else '\n'.join([*[x['text'] for x in p['content']['claims']],*p['content']['missing_information']])
    if p['result_type']=='analytical_note' and view['version']['payload']['content'].get('team')==TBD:
        # Как в скилле: без имен команды ответ один раз просит их указать.
        parent=row(c,"SELECT data->'payload'->'content'->>'team' AS team FROM aist.artifact_versions WHERE id=:v",
                   v=args['parent_version_id']) if args['parent_version_id'] else None
        if not parent or parent['team']!=TBD:
            summary+='\n'+TEAM_REQUEST
    if p['result_type'] == 'analytical_note' and args.get('questions'):
        summary += '\n\n' + '\n'.join(args['questions'])
    if p['result_type'] == 'analytical_note' and args.get('ownership_review', {}).get('status') == 'not_established':
        summary += '\n\nВладельцы: ' + args['ownership_review']['reason']
    runs.append_message(c,r,'assistant',summary[:20000],version)
    runs.transition(c,r,'completed',result=version)
    runs.event(c,r,'result_saved',{'artifact_version_id':version})
    return view


AMOUNT_SCALE = r'(?:тысяч(?:а|и|у)?|тыс\.?|миллион(?:а|ов)?|млн\.?|миллиард(?:а|ов)?|млрд\.?|триллион(?:а|ов)?|трлн\.?)(?!\w)'
# Только поддержанные обозначения валют: сокращение показателя перед числом,
# например ROA или NPS, валютой не является.
AMOUNT_CURRENCY = r'(?:RUB|USD|EUR|руб(?:лей|ля|ль)?\.?|долл(?:аров|ара|ар)?\.?|евро)(?!\w)|[₽$€]'
# Фрагмент включает возможный знак, значение знака определяет written_sign по всему тексту.
AMOUNT_NUMBER = r'[+\-\u2212\u2013\u2014]?\s*(?P<digits>(?:\d{1,3}(?:[ \u00a0\u202f]\d{3}(?!\d))+|\d+)(?:[.,]\d+)?)'
AMOUNT_SUFFIX = re.compile(rf'(?<!\w)(?P<value>{AMOUNT_NUMBER})\s*(?P<scale>{AMOUNT_SCALE})?\s*'
                           rf'(?P<unit>{AMOUNT_CURRENCY}|%|процент(?:а|ов)?(?!\w))', re.I)
AMOUNT_PREFIX = re.compile(rf'(?<!\w)(?P<unit>{AMOUNT_CURRENCY})\s*(?P<value>{AMOUNT_NUMBER})\s*'
                           rf'(?P<scale>{AMOUNT_SCALE})?', re.I)
CURRENCY_CODES = {'rub': 'RUB', '₽': 'RUB', 'usd': 'USD', '$': 'USD', 'eur': 'EUR', 'евро': 'EUR', '€': 'EUR'}


def amount_scale(text):
    text = (text or '').casefold()
    return ('1000' if text.startswith('тыс') else '1000000' if text.startswith(('млн', 'миллион'))
            else '1000000000' if text.startswith(('млрд', 'миллиард'))
            else '1000000000000' if text.startswith(('трлн', 'триллион')) else '1')


def stated_amount(m):
    """Значение, масштаб, валюта и вид суммы или процента найденного фрагмента.

    Неоднозначный знак здесь не отклоняется: его называет отдельная проверка number_tokens."""
    sign = written_sign(m.string, m.start('digits')) or ''
    value = Decimal(sign + re.sub(r'[ \u00a0\u202f]', '', m['digits']).replace(',', '.'))
    multiplier = amount_scale(m['scale'])
    unit_text = m['unit'].lower().rstrip('.')
    code = ('RUB' if unit_text.startswith('руб') else 'USD' if unit_text.startswith('долл') else
            None if unit_text == '%' or unit_text.startswith('процент') else CURRENCY_CODES[unit_text])
    return value, multiplier, code, 'money' if code else 'percent'


def stated_amounts(text, replace):
    # Префикс валюты обрабатывается первым, чтобы не потерять масштаб после суммы.
    return AMOUNT_SUFFIX.sub(replace, AMOUNT_PREFIX.sub(replace, text))


def check_units(text, amounts):
    # Возвращаем непроверенный остаток. Отсутствие совпадений не означает passed.
    mismatched = []

    def verify(m):
        value, multiplier, code, unit = stated_amount(m)
        if not any(Decimal(a['value'])==value and a['scale']==multiplier and a['currency']==code and a['unit']==unit for a in amounts):
            mismatched.append(' '.join(m[0].split()))
        return ' ' * len(m[0])

    remaining = stated_amounts(text, verify)
    if mismatched:
        listed = ', '.join('«' + item + '»' for item in dict.fromkeys(mismatched))[:300]
        refuse(f'Сумма или процент {listed} не совпадают с основанием. ' + UNITS_ADVICE)
    return remaining


def quarter_ranges(text, periods, remarks, *, qualitative_facts=()):
    """Финансовый интервал типизирован; срок плана также может быть явно задан основной цитатой."""
    pattern = (r'(?<![\w+−-])(?P<first>[1-4])\s*[-–—]\s*(?P<last>[1-4])\s*'
               r'(?:кв\.?|квартал(?:ы|а|ов)?)\s*(?P<year>(?:19|20)\d{2})'
               r'(?:\s*(?:г\.|год(?:а|у)?))?(?!\w)')
    def verify(match):
        first, last, year = (int(match[name]) for name in ('first', 'last', 'year'))
        start = date(year, first * 3 - 2, 1).isoformat()
        end = date(year, last * 3, monthrange(year, last * 3)[1]).isoformat()
        supported_range = any(p['kind'] == 'interval' and (p['start'], p['end']) == (start, end) for p in periods)
        supported_plan = False
        for fact in primary_statements(qualitative_facts):
            matches = [m for m in re.finditer(pattern, fact['raw_text'], re.I)
                       if (m['first'], m['last'], m['year']) == (match['first'], match['last'], match['year'])]
            if len(matches) != 1:
                continue
            # Контекст срока сохраняет плановый характер. Одинаковая дата не доказывает состоявшийся запуск.
            plan = r'планируемая\s+дата\s+(?P<event>[а-яё]+(?:\s+[а-яё]+){0,2})\s*:\s*$'
            source_match = matches[0]
            source_prefix = fact['raw_text'][:source_match.start()]
            source_plan = re.search(plan, source_prefix, re.I)
            if not source_plan:
                continue
            supported_plan = True
            period = fact['period']
            if period['kind'] == 'unknown' or (period['start'], period['end']) == (start, end):
                supported_range = True
            else:
                remarks.append(('period_mismatch', 'Период основания расходится с указанным в нём сроком плана.'))
            # Точка с запятой разделяет описание фактора и его срок, но не снимает модальность.
            begin = max((m.end() for m in re.finditer(r'[.!?](?=\s+[А-ЯЁA-Z])', text[:match.start()])), default=0)
            prefix = text[begin:match.start()]
            claim_plan = re.search(plan, prefix, re.I)
            source_event = ' '.join(source_plan['event'].casefold().split())
            same_plan = bool(claim_plan and ' '.join(claim_plan['event'].casefold().split()) == source_event)
            if source_event == 'промышленного запуска':
                same_plan |= bool(re.search(r'(?:планируется\s+к\s+промышленному\s+запуску\s+(?:только\s+)?в|'
                                            r'с\s+планируемым\s+промышленным\s+запуском\s+в)\s*$', prefix, re.I))
            if not same_plan:
                remarks.append(('event_mismatch', 'Источник указывает плановый срок, а не состоявшийся запуск.'))
            if re.search(r'может\s+стать', source_prefix, re.I) and re.search(r'драйвер\w*\s+роста', prefix, re.I):
                if not re.search(r'может\s+стать', prefix, re.I) or re.search(r'обязательно|гарантированно|не\s+может', prefix, re.I):
                    remarks.append(('event_mismatch', 'Возможный драйвер роста в источнике не подтверждает гарантированный результат.'))
        if qualitative_facts and not supported_plan:
            remarks.append(('event_mismatch', 'Срок события не связан с основным фрагментом основания.'))
        if first > last or not supported_range:
            remarks.append(('period_mismatch', 'Диапазон кварталов «' + match[0] + '» не совпадает с основанием.'))
        return ' ' * len(match[0])
    return re.sub(pattern, verify, text, flags=re.I)


def check_periods(text, periods, *, source_quotes=()):
    """Проверка явных интервалов по датам оснований, без смыслового анализа фраз.

    Все несовпадения абзаца собираются и возвращаются одним отказом."""
    # Полная дословная цитата оговорки не устанавливает финансовый период.
    # resolve_basis передает эти цитаты только без числовых фактов и расчетов.
    literal_quotes = {' '.join(quote.split()).casefold() for quote in source_quotes}
    problems = []

    def quoted_qualification(match):
        quoted = next(value for value in match.groups() if value is not None)
        if ' '.join(quoted.split()).casefold() in literal_quotes:
            return ' ' * len(match[0])
        return match[0]

    text = re.sub(r'«([^«»]+)»|“([^“”]+)”|"([^"]+)"', quoted_qualification, text)
    numeric_labels = []
    # Literal qualifications can name a forecast year without establishing dates.
    # Consume only the complete saved label, verified in a qualitative source quote;
    # do not turn it into an interval available to other financial statements.
    for period in periods:
        label=period.get('label','')
        if period['kind']!='unknown' or not re.search(r'\d',label):
            continue
        pattern=r'(?<!\w)'+r'\s+'.join(re.escape(word) for word in label.split())+r'(?!\w)'
        if any(re.search(pattern,quote,re.I) for quote in source_quotes):
            if re.search(r'[A-Za-zА-Яа-я]',label):
                text=re.sub(pattern,lambda match:' '*len(match[0]),text,flags=re.I)
            elif re.fullmatch(r'(?:19|20)\d{2}',label):
                numeric_labels.append(label)
    intervals = {(p['start'],p['end']) for p in periods if p['kind']=='interval'}
    boundaries = {d for p in periods for d in (p['start'],p['end']) if d}
    year = r'(?:19\d{2}|20\d{2}|\d{2})'
    separator = r"\s*['’ʼ`]?\s*"
    calendar_pattern=rf'(?<!\w)(?P<count>\d+)\s*(?P<kind>[MQH]){separator}(?P<year>{year})(?!\w)'
    quarter_pattern=r'(?<!\w)(?P<count>[1-4])\s*кв(?:артал)?\.?\s*(?P<year>(?:19|20)\d{2})(?:\s*г\.)?(?!\w)'

    def calendar_interval(match):
        year_number=int(match['year'])
        if year_number<100:
            year_number+=2000
        count=int(match['count'])
        kind=match['kind'].upper()
        if kind=='M' and 1<=count<=12:
            first,last=1,count
        elif kind=='Q' and 1<=count<=4:
            first,last=3*count-2,3*count
        elif kind=='H' and 1<=count<=2:
            first,last=6*count-5,6*count
        else:
            refuse('Обозначение периода не соответствует допустимому интервалу.')
        return (date(year_number,first,1).isoformat(),
                date(year_number,last,monthrange(year_number,last)[1]).isoformat())

    months = {'один':1,'одного':1,'одному':1,'одним':1,'одном':1,
              'три':3,'трех':3,'трёх':3,'трем':3,'трём':3,'тремя':3,
              'шесть':6,'шести':6,'шестью':6,'девять':9,'девяти':9,'девятью':9,
              'двенадцать':12,'двенадцати':12,'двенадцатью':12}
    duration_pattern=rf'(?<!\w)(?P<count>\d+|{"|".join(months)})\s+месяц(?:ами|ам|ах|ев|а|е|у|ы)?\s+(?P<year>(?:19|20)\d{{2}})(?:\s*(?:год(?:а|у|ом)?|г\.)(?!\w))?'
    month_names={'января':1,'февраля':2,'марта':3,'апреля':4,'мая':5,'июня':6,
                 'июля':7,'августа':8,'сентября':9,'октября':10,'ноября':11,'декабря':12}
    date_pattern=rf'(?<!\w)(?P<day>\d{{1,2}})\s+(?P<month>{"|".join(month_names)})\s+(?P<year>(?:19|20)\d{{2}})(?:\s*года)?(?!\w)'
    event_month_names = {**month_names, 'январе':1, 'феврале':2, 'марте':3, 'апреле':4,
                         'мае':5, 'июне':6, 'июле':7, 'августе':8, 'сентябре':9,
                         'октябре':10, 'ноябре':11, 'декабре':12,
                         'январь':1, 'февраль':2, 'март':3, 'апрель':4, 'май':5,
                         'июнь':6, 'июль':7, 'август':8, 'сентябрь':9, 'октябрь':10,
                         'ноябрь':11, 'декабрь':12,
                         'январем':1, 'январём':1, 'февралем':2, 'февралём':2, 'мартом':3,
                         'апрелем':4, 'маем':5, 'июнем':6, 'июлем':7, 'августом':8,
                         'сентябрем':9, 'сентябрём':9, 'октябрем':10, 'октябрём':10,
                         'ноябрем':11, 'ноябрём':11, 'декабрем':12, 'декабрём':12}
    event_month_pattern = (rf'(?<!\w)(?P<month>{"|".join(event_month_names)})\s+'
                           r'(?P<year>(?:19|20)\d{2})(?:\s*(?:года|г\.))?(?!\w)')
    event_months = set()
    def duration(m):
        return months.get(m['count'].lower()) or int(m['count'])
    def source_date(m):
        month=month_names.get(m['month'].lower())
        if month is None:month=int(m['month'])
        try:return date(int(m['year']),month,int(m['day'])).isoformat()
        except ValueError:refuse('Календарная дата недопустима.')
    # Качественная оговорка может содержать дату приобретения или период
    # сравнительных данных. Они разрешаются только из ее сохраненных цитат.
    for quote in source_quotes:
        for match in re.finditer(calendar_pattern,quote,re.I):
            intervals.add(calendar_interval(match))
        for match in re.finditer(quarter_pattern,quote,re.I):
            intervals.add(calendar_interval(dict(count=match['count'],kind='Q',year=match['year'])))
        for m in re.finditer(duration_pattern,quote,re.I):
            n,y=duration(m),int(m['year'])
            if 1<=n<=12:intervals.add((date(y,1,1).isoformat(),date(y,n,monthrange(y,n)[1]).isoformat()))
        for m in re.finditer(date_pattern,quote,re.I):boundaries.add(source_date(m))
        for m in re.finditer(DOTTED_DATE,quote):boundaries.add(source_date(m))
        for m in re.finditer(event_month_pattern,quote,re.I):
            event_months.add((event_month_names[m['month'].lower()], int(m['year'])))

    def check_interval(start,end):
        if (start,end) not in intervals:
            problems.append(f'Период {start} по {end} не совпадает с сохраненным основанием.')

    shared_year_pattern = (
        rf'(?<!\w)с\s+(?P<start_day>\d{{1,2}})\s+(?P<start_month>{"|".join(month_names)})\s+'
        rf'по\s+(?P<end_day>\d{{1,2}})\s+(?P<end_month>{"|".join(month_names)})\s+'
        r'(?P<year>(?:19|20)\d{2})(?:\s*(?:год(?:а|у|ом)?|г\.))?(?!\w)')

    def shared_year_interval(match):
        start = source_date(dict(day=match['start_day'], month=match['start_month'], year=match['year']))
        end = source_date(dict(day=match['end_day'], month=match['end_month'], year=match['year']))
        if start > end:
            problems.append('Начало календарного интервала позже конца.')
        else:
            check_interval(start, end)
        return ' ' * len(match[0])

    # Общий год относится к обеим границам. Проверяем пару до отдельных дат:
    # две границы разных фактов или instant не подтверждают целый интервал.
    text = re.sub(shared_year_pattern, shared_year_interval, text, flags=re.I)

    def calendar_period(m):
        check_interval(*calendar_interval(m))
        return ' ' * len(m[0])

    text = re.sub(calendar_pattern,calendar_period,text,flags=re.I)
    def quarter_period(match):
        check_interval(*calendar_interval(dict(count=match['count'],kind='Q',year=match['year'])))
        return ' '*len(match[0])
    text = re.sub(quarter_pattern,quarter_period,text,flags=re.I)
    # Цифровые и распространенные словесные обозначения месяцев с явным годом.
    def month_period(m):
        n = duration(m)
        y = int(m['year'])
        if not 1<=n<=12: refuse('Число месяцев периода недопустимо.')
        check_interval(date(y,1,1).isoformat(),date(y,n,monthrange(y,n)[1]).isoformat())
        return ' ' * len(m[0])
    text = re.sub(duration_pattern,month_period,text,flags=re.I)
    def calendar_date(m):
        if source_date(m) not in boundaries:
            problems.append(f'Дата «{" ".join(m[0].split())}» не совпадает с основанием.')
        return ' ' * len(m[0])
    text = re.sub(date_pattern,calendar_date,text,flags=re.I)
    text = re.sub(DOTTED_DATE,calendar_date,text)

    def event_month(m):
        key = (event_month_names[m['month'].lower()], int(m['year']))
        if key not in event_months:
            problems.append(f'Месяц события «{" ".join(m[0].split())}» не совпадает с качественным основанием.')
        return ' ' * len(m[0])

    # Месяц события из цитаты не создает финансовый интервал или годовое основание.
    text = re.sub(event_month_pattern,event_month,text,flags=re.I)

    def year_end(m):
        # «На конец 2022 года» называет остаток на 31.12.2022, не годовой оборот.
        if f"{m['year']}-12-31" not in boundaries:
            problems.append(f'Дата «{" ".join(m[0].split())}» (31.12.{m["year"]}) не совпадает с основанием.')
        return ' ' * len(m[0])
    text = re.sub(r'(?<!\w)на\s+конец\s+(?P<year>(?:19|20)\d{2})\s*(?:года|г\.)(?!\w)',year_end,text,flags=re.I)

    def annual(m):
        y = int(m['year']); check_interval(f'{y}-01-01',f'{y}-12-31')
        return ' ' * len(m[0])
    text = re.sub(r'(?<!\w)(?P<year>(?:19|20)\d{2})\s*(?:год(?:а|у|ом)?|г\.)(?!\w)',annual,text,flags=re.I)
    text = re.sub(r'(?<!\w)(?:за|период(?:ы|ах|ам|ов|е|у|ом)?)\s+(?P<year>(?:19|20)\d{2})'
                  r'(?=\s*(?:[,;.](?!\d)|и(?!\w)|\)|$))',annual,text,flags=re.I)

    def date_range(m):
        check_interval(m['start'],m['end'])
        return ' ' * len(m[0])
    iso = r'\d{4}-\d{2}-\d{2}'
    text = re.sub(rf'(?<!\w)(?P<start>{iso})\s*(?:по|до|\.\.|-)\s*(?P<end>{iso})(?!\w)',date_range,text,flags=re.I)
    def boundary(m):
        if m[0] not in boundaries:
            problems.append(f'Дата «{m[0]}» не совпадает с основанием.')
        return ' ' * len(m[0])
    text = re.sub(rf'(?<!\w){iso}(?!\w)',boundary,text)
    # У качественной оговорки нет финансовых оснований. Проверяем единицы до
    # удаления числовой подписи, иначе «подпись периода 2023%» скроет процент.
    if numeric_labels:
        try:
            check_units(text, [])
        except ApiError as error:
            if error.code!='validation_failed':raise
            problems.append(error.body['error']['message'])
    # Числовая подпись допускается только как явное название поля оговорки.
    # Годовые интервалы и даты выше уже проверены.
    for label in numeric_labels:
        pattern = r'(?<!\w)подпис(?:ь|и|ью)\s+периода\s+' + re.escape(label) + r'(?!\w)'
        text = re.sub(pattern, lambda match: ' ' * len(match[0]), text, flags=re.I)
    if problems:
        listed = ' '.join(dict.fromkeys(problems))[:600]
        advice = ' ' + PERIOD_ADVICE if any(problem.startswith('Период ') for problem in problems) else ''
        refuse(listed + advice)
    return text
