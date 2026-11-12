"""Version-bound ASO layout. Both the reader and the file writer consume this view."""
import re
from collections import Counter, defaultdict
from datetime import datetime
from .product_date import moscow_date
from .facts import LEGACY_EXCERPT_PREFIX

from . import note_standard as standard
from .review_candidates import pages as candidate_pages


# Решение пользователя 7 ноября: дата шапки по Москве, одинаковая на экране и в обоих файлах.
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря')
# Пояснение к выделенной ячейке нужно экрану; файл содержит только саму записку.
EDITED_VALUES_NOTE = 'Выделенное значение изменено редактором и расходится с источником.'


def display_text(text):
    """Типографика представления без изменения источников, URL и смещений выделения."""
    return re.sub(r'https?://\S+|[\u2013\u2014]',
                  lambda match: '-' if len(match[0]) == 1 else match[0], text, flags=re.I)


def version_date(created_at):
    """Дата создания версии словами в форме скилла ASO: «7 ноября 2026 г.»."""
    moment = moscow_date(datetime.fromisoformat(created_at.replace('Z', '+00:00')))
    return f'{moment.day} {MONTHS[moment.month - 1]} {moment.year} г.'


def period_header(header, column):
    """Календарный год таблицы ASO обозначается 2025Ф (факт) или 2027П (прогноз), другие периоды сохраняют подпись."""
    kind, start, end, nature = column
    year = start[:4]
    mark = {'reported_actual': 'Ф', 'forecast': 'П'}.get(nature)
    if mark and kind == 'interval' and start == year + '-01-01' and end == year + '-12-31':
        return year + mark
    return header


def table_value(text, item):
    """Отрицательное значение таблицы в скобках, отрицательная маржа EBITDA как «н.п.».

    Это правило отображения скилла ASO: сумма, расчет и ссылка ячейки не меняются."""
    if not item or not text.startswith('-'):
        return text
    if item['amount']['unit'] == 'percent' and standard.same_row_metric(item, 'EBITDA margin'):
        return 'н.п.'
    return '(' + text[1:] + ')'


# «x» в подписи коэффициента называет ту же единицу, что «коэф.»: «Net debt/EBITDA, x».
RATIO_WORDS = r'(?:коэф\.?|x|х)'


def mentions_unit(text, unit):
    """Подпись уже называет единицу, в том числе перед уточнением: «Валовая прибыль, млрд руб. (IAS 17)»."""
    scale, _, measure = unit.rpartition(' ')
    words = RATIO_WORDS if measure == 'коэф.' else standard.MEASURE_WORDS.get(measure, re.escape(measure))
    if scale:
        words = standard.SCALE_WORDS.get(scale, re.escape(scale)) + r'\s*' + words
    return re.search(r'(?<!\w)' + words + r'(?!\w)', text, re.I) is not None


def without_repeated_unit(label, item):
    """Строгая матрица дописывает единицу строки в конец подписи; повтор убирается, если подпись уже ее называет."""
    unit = standard.shared_unit(dict(rows=[item]))
    suffix = ', ' + unit if unit else ''
    if suffix and label.endswith(suffix) and mentions_unit(label[:-len(suffix)], unit):
        return label[:-len(suffix)]
    return label


def row_source(row, facts, calculations, visuals):
    """Единственное основание строки как (область, метрика, период, характер данных) или None.

    Область та же, что у строгой матрицы: версия источника, субъект и база учета, у изображения его страница."""
    refs, calc_ids, visual_ids = row['fact_refs'], row['calculation_ids'], row.get('visual_observation_ids', [])
    if len(refs) == 1 and not calc_ids and not visual_ids:
        fact = facts.get((refs[0]['source_version_id'], refs[0]['fact_id']))
        if fact:
            scope = (fact['source']['source_version_id'], fact['subject'], fact['reporting_basis'])
            return scope, fact['metric'], fact['period'], fact['nature']
    elif len(visual_ids) == 1 and not refs and not calc_ids:
        observation = visuals.get(visual_ids[0])
        numeric = observation['numeric'] if observation else None
        if numeric:
            scope = (observation['source']['source_version_id'], numeric['subject'], ('visual', observation['page']))
            return scope, numeric['metric'], numeric['period'], numeric['nature']
    elif len(calc_ids) == 1 and not refs and not visual_ids:
        # Рост одной метрики одного основания между двумя периодами, как в строгой матрице.
        calculation = calculations.get(calc_ids[0])
        if calculation and calculation['formula_id'] == 'growth_percent.v1' and len(calculation['inputs']) == 2:
            previous, current = (facts.get((i['fact']['source_version_id'], i['fact']['fact_id']))
                                 for i in calculation['inputs'])
            if (previous and current and previous['source']['source_version_id'] == current['source']['source_version_id']
                    and all(previous[k] == current[k] for k in ('subject', 'reporting_basis', 'metric', 'nature'))):
                scope = (current['source']['source_version_id'], current['subject'], current['reporting_basis'])
                period = dict(kind='comparison', label=current['period']['label'] + ' к ' + previous['period']['label'],
                              start=previous['period']['start'], end=current['period']['end'])
                return scope, calculation['formula_id'] + ':' + current['metric'], period, current['nature']
    return None


def stated_period(period, nature, label):
    """Колонка по датам основания, если основание называет тот же период, что строка; иначе None."""
    if (period['label'] != label or period['kind'] == 'unknown' or not period['start'] or not period['end']
            or period['start'] > period['end'] or nature not in ('reported_actual', 'forecast')
            or period['kind'] == 'instant' and period['start'] != period['end']):
        return None
    return period['kind'], period['start'], period['end'], nature


def period_matrix(block, facts, calculations, visuals):
    """Периоды по колонкам для таблицы, которую строгая матрица оставляет подробной (решение пользователя 8 ноября).

    Ячейка показывает сумму и период своей строки. Даты и характер данных берутся из основания, только если оно
    называет тот же период, поэтому строка не попадает в чужую колонку. Строки объединяются по подписи, единице,
    области и метрике основания: одной подписи недостаточно. Второе значение той же ячейки уходит в отдельную
    строку с той же подписью, и каждая ячейка сохраняет свою исходную строку.
    """
    titles, entries, keyed = {}, [], defaultdict(list)
    for index, row in enumerate(block['rows']):
        amount = row['amount']
        measure = (row['label'], amount['unit'], amount['scale'], amount['currency'])
        source = row_source(row, facts, calculations, visuals)
        column = kind = None
        if source:
            scope, metric, period, nature = source
            column = stated_period(period, nature, row['period_label'])
            kind = column[0] if column else None
            key = (*measure, scope, metric, kind)
            if column:
                titles.setdefault(column, period['label'] + ' (' + standard.NATURE[nature] + ')')
        else:
            # Строка без единственного основания не объединяется с другими по одной подписи.
            key = (*measure, index)
        if not column:
            # Период без подтвержденных дат остается подписью строки, такие колонки идут после датированных.
            column = ('literal', row['period_label'], '', None)
            titles.setdefault(column, row['period_label'])
        entry = next((e for e in keyed[key] if column not in e['cells']), None)
        if entry is None:
            entry = dict(label=row['label'], kind=kind, first=row, cells={})
            keyed[key].append(entry)
            entries.append(entry)
        entry['cells'][column] = row

    dated = [c for c in titles if c[0] != 'literal']
    # Остаток на дату встает в колонку единственного периода с той же конечной датой, как в строгой матрице.
    aliases = {}
    for column in dated:
        matching = [c for c in dated if c[0] == 'interval' and c[2:] == column[2:]]
        if column[0] == 'instant' and len(matching) == 1:
            aliases[column] = matching[0]
    ordered = sorted({aliases.get(c, c) for c in dated}, key=lambda c: (c[2], c[1], c[0], c[3]))
    ordered += [c for c in titles if c[0] == 'literal']
    repeated = Counter(titles[c] for c in ordered)
    headers = ['Показатель']
    for column in ordered:
        title = titles[column]
        # Одинаковые подписи разных периодов различаются датами, как в строгой матрице.
        if repeated[title] > 1 and column[0] != 'literal':
            title += ' ' + column[1] + ' - ' + column[2]
        headers.append(title)

    unit = standard.shared_unit(block) or ''
    has_balances = any(entry['kind'] == 'instant' for entry in entries)
    values, origins = [], []
    for entry in entries:
        label = entry['label']
        if has_balances and entry['kind'] in ('instant', 'interval'):
            label += ' (на конец периода)' if entry['kind'] == 'instant' else ' (за период)'
        row_unit = standard.shared_unit(dict(rows=[entry['first']]))
        if not unit and row_unit and not mentions_unit(label, row_unit):
            label += ', ' + row_unit
        cells = {aliases.get(column, column): row for column, row in entry['cells'].items()}
        items = [cells.get(column) for column in ordered]
        values.append([label, *(standard.number(item['amount']) if item else '-' for item in items)])
        origins.append([None, *items])
    return dict(unit=unit, headers=headers, values=values, origins=origins, columns=ordered)


def finding_quote(finding, text):
    # Old quantity checks name the offending literal explicitly. Never search for a bare table number.
    quote = finding.get('quote')
    if not quote and finding['code'] in ('quantity_mismatch', 'period_mismatch'):
        match = re.match(r'(?:Количество или срок|Месяц события) «([^»]+)»', finding['message'])
        quote = match.group(1) if match else None
    return quote if quote and text.count(quote) == 1 else None


def styled_runs(cell, marks):
    text = cell['text']
    highlights = [(text.index(q), text.index(q) + len(q)) for q in cell['quotes']]
    bounds = sorted({0, len(text), *(n for pair in highlights for n in pair),
                     *(m[k] for m in marks for k in ('start', 'end'))})
    runs = []
    index = 0
    for left, right in zip(bounds, bounds[1:]):
        while index < len(marks) and marks[index]['end'] <= left:
            index += 1
        style = marks[index]['style'] if index < len(marks) and marks[index]['start'] <= left else None
        highlighted = cell['marked'] and (not highlights or any(a <= left < b for a,b in highlights))
        runs.append(dict(text=text[left:right], bold=style in ('bold','both'),
                         italic=style in ('italic','both'), highlight=highlighted))
    return runs


# Match only a final, standalone editorial sentence. Keep source quotations,
# analyst edits and qualifications about the business or comparability of figures.
_GAP_END = re.compile(
    r'(?:по (?:доступным|сохран[её]нным|имеющимся) основаниям (?:подтвердить (?:нельзя|невозможно)|(?:нельзя|невозможно) подтвердить)'
    r'|(?:нельзя|невозможно) подтвердить по (?:доступным|сохран[её]нным|имеющимся) основаниям'
    r'|в (?:доступных|сохран[её]нных|имеющихся) основаниях не (?:указаны|указана|указан|указано))'
    r'[.!]?$', re.I)
_GAP_TOPIC = re.compile(
    r'(?:текущ\w+ |точн\w+ |общ\w+ |актуальн\w+ |промежуточн\w+ )?'
    r'(?:структур\w*|дол[яию]|дат\w*|параметр\w*|объ[её]м\w*|темп\w*|состав|показател\w*|'
    r'данные|сведения|результат\w*|размер\w*|стоимость|периодичность)\b', re.I)


def reading_paragraph(claim, marks=()):
    """Remove service wording without rewriting quotes, financial values or editorial text."""
    text = claim['text']
    origin = claim.get('origin')
    if origin not in ('source_candidate', 'source_fact', 'inference'):
        return text, marks

    # The reader already marks the candidate; keep the qualification "предварительно".
    cuts = []
    if origin == 'source_candidate':
        cuts.extend(m.span() for m in re.finditer(
            r'(?<=предварительно) и требу(?:ет|ют) сверки с источником(?=[.!]|$)', text, re.I))
    elif not (claim.get('calculation_ids') or claim.get('visual_observation_ids') or claim.get('review_candidate_ids')):
        end = _GAP_END.search(text)
        if end:
            start = max((m.end() for m in re.finditer(r'(?<=[.!?])\s+(?=[А-ЯЁ])', text[:end.start()])), default=0)
            topic = text[start:end.start()].strip()
            # A date, value or contrast can be meaningful even in a sentence about missing evidence.
            if ((start or not claim.get('fact_refs')) and _GAP_TOPIC.match(topic) and not re.search(
                    r'[\d:;!?«»]|\b(?:но|однако|хотя|поскольку|если|тогда как|при этом|в связи|не)\b', topic, re.I)):
                cuts.append((len(text[:start].rstrip()), len(text)))
        # A standalone clause about retrieval may follow a sourced risk in the same sentence.
        cuts.extend(m.span() for m in re.finditer(
            r';\s*(?:условия|сроки|даты) [^;.!?\d,:«»]{1,60} по доступным основаниям не ясны(?=[.!]|$)', text, re.I))
    for start, stop in sorted(cuts, reverse=True):
        if start >= len(text):
            continue
        stop = min(stop, len(text))
        text = text[:start] + text[stop:]
        if marks:
            def offset(pos):
                return pos - min(max(pos - start, 0), stop - start)
            marks = [dict(mark, start=offset(mark['start']), end=offset(mark['end']))
                     for mark in marks if offset(mark['start']) < offset(mark['end'])]
    return text, marks


def presentation(version, checks):
    from .note_news import block_id as news_block_id, news_text, matches_news_text
    basis = version['basis']
    publications = {news_block_id(item):item for item in basis.get('news_run',{}).get('items',[])}
    facts = {(f['source']['source_version_id'], f['fact_id']): f for f in basis['facts']}
    calculations = {c['calculation_id']: c for c in basis['calculations']}
    visuals = {v['observation_id']: v for v in basis.get('visual_observations', [])}
    candidates = {i['review_candidate']['candidate_id']: i['review_candidate'] for i in basis.get('review_candidates', [])}
    by_block = defaultdict(list)
    for check in checks:
        for finding in check['findings']:
            if finding['severity'] == 'error':
                by_block[finding['block_id']].append(finding)

    def links(item):
        locations = {}
        def add(source, page):
            if page:
                locations[(source, page)] = dict(source=source, page=page)
        refs = list(item['fact_refs'])
        for calc_id in item['calculation_ids']:
            refs.extend(i['fact'] for i in calculations[calc_id]['inputs'])
        for ref in refs:
            fact = facts[(ref['source_version_id'], ref['fact_id'])]
            if item.get('origin') == 'source_excerpt':
                add(ref['source_version_id'], fact['evidence'][0]['locator']['pdf_page'])
                continue
            raw = ' '.join(fact['raw_text'].split()).casefold()
            metric = ' '.join(fact['metric'].split()).casefold()
            direct = []
            for evidence in fact['evidence']:
                quote = ' '.join(evidence['quote'].split()).casefold()
                if raw and raw in quote and (not fact['amount'] or metric in quote):
                    direct.append(evidence)
            for evidence in direct or fact['evidence']:
                add(ref['source_version_id'], evidence['locator'].get('pdf_page'))
        for oid in item.get('visual_observation_ids', []):
            observation = visuals[oid]
            add(observation['source']['source_version_id'], observation['page'])
        for cid in item.get('review_candidate_ids', []):
            candidate = candidates[cid]
            for page in sorted(candidate_pages(candidate)):
                add(candidate['source']['source_version_id'], page)
        return list(locations.values())

    def cell(value, item=None, remarks=()):
        result = dict(text=display_text(value), row_id=item.get('row_id') if item else None,
                      links=links(item) if item else [], marked=bool(remarks), quotes=[])
        if item:
            quotes = dict.fromkeys(display_text(q) for f in remarks if (q := finding_quote(f, value)))
            result['quotes'] = [quote for quote in quotes if result['text'].count(quote) == 1]
        return result

    sections = []
    for section in version['payload']['content']['sections']:
        blocks = []
        for block in section['blocks']:
            findings = by_block[block['block_id']]
            out = dict(id=block['block_id'], kind=block['kind'], caption='', headers=[], rows=[], notes=[], marked=False)
            if block['kind'] == 'paragraph':
                claim = block['claim']
                if block['block_id'] in publications and claim.get('origin')=='public_news':
                    out['publication']=publications[block['block_id']]
                elif section['title'].strip().casefold() in ('news run','новости компании'):
                    shared=next((item for item in publications.values() if matches_news_text(claim['text'], item)),None)
                    if shared:
                        out['publication']=shared
                text, marks = reading_paragraph(claim, block.get('marks', []))
                publication = out.get('publication')
                if publication and not marks and matches_news_text(text, publication):
                    text = news_text(publication)
                if not text:
                    continue
                prefix_length = 0
                if claim.get('origin') == 'source_excerpt' and text.startswith(LEGACY_EXCERPT_PREFIX + '«') and text.endswith('»'):
                    prefix_length = len(LEGACY_EXCERPT_PREFIX)
                value = cell(text[prefix_length:], claim, findings)
                value['marked'] = bool(value['quotes'])
                out['rows'] = [[value]]
                if marks:
                    marks = [dict(m, start=max(0, m['start'] - prefix_length), end=m['end'] - prefix_length)
                             for m in marks if m['end'] > prefix_length]
                    value['styled_runs'] = styled_runs(value, marks)
                if block.get('list_level'):
                    out['list_level'] = block['list_level']
            else:
                out['caption'] = block['caption']
                edited_values = {f.get('row_id') for f in findings if f.get('row_id') and f['code'] == 'row_basis_mismatch'
                                 and version.get('provenance', {}).get('origin') == 'human_edit'}
                table = standard.financial_projection(block, facts, calculations=calculations,
                    observations=visuals, edited_values=edited_values)
                if table['pivoted']:
                    out['notes'].extend(table['notes'])
                    values = []
                    for (label, *cells), items in zip(table['values'], table['origins']):
                        first = next(item for item in items if item)
                        values.append([without_repeated_unit(label, first), *cells])
                else:
                    # Решение пользователя 8 ноября: периоды идут по колонкам во всех таблицах ASO.
                    # Пояснение строк подробного вида остается на экране, в файл оно не выводится.
                    _, _, details = standard.detailed_presentation(block, facts, basis.get('sources', []))
                    out['notes'].extend(value for _, value in details)
                    table = period_matrix(block, facts, calculations, visuals)
                    values = table['values']
                for row_values, row_items in zip(values, table['origins']):
                    first = next(item for item in row_items if item)
                    if first['amount']['unit'] == 'percent' and row_values[0].lstrip().casefold().startswith('lfl '):
                        label = dict(first, label=row_values[0])
                        standard.translate_row_labels(dict(rows=[label]))
                        row_values[0] = label['label']
                unit, origins = table['unit'], table['origins']
                headers = [table['headers'][0], *map(period_header, table['headers'][1:], table['columns'])]
                if unit and not mentions_unit(out['caption'], unit) and out['caption'] != unit:
                    out['caption'] += ', ' + unit
                if edited_values:
                    out['notes'].append(EDITED_VALUES_NOTE)
                out['headers'] = [display_text(text) for text in headers]
                for row_values, row_items in zip(values, origins):
                    out['rows'].append([cell(table_value(value, item), item,
                                             [f for f in findings if item and f.get('row_id') == item['row_id']])
                                        for value, item in zip(row_values, row_items)])
                for cells, items in zip(out['rows'], origins):
                    first = next(item for item in items if item)
                    relative = first['amount']['unit'] in ('ratio', 'percent')
                    source = row_source(first, facts, calculations, visuals)
                    metric = standard.translated_metric(source[1], first['amount']['unit']).casefold() if source else ''
                    key_metric = metric in ('выручка', 'ebitda', 'net profit', 'чистая прибыль')
                    for index, value in enumerate(cells):
                        if key_metric or relative and index == 0:
                            value['styled_runs'] = [dict(text=value['text'], bold=key_metric,
                                italic=relative and index == 0, highlight=value['marked'])]
                out['marked'] = any(not f.get('row_id') for f in findings)
            out['caption'] = display_text(out['caption'])
            out['notes'] = [display_text(text) for text in out['notes']]
            blocks.append(out)
        if blocks:
            sections.append(dict(title=display_text(section['title']), blocks=blocks))
    content = version['payload']['content']
    # Старые версии без поля команды выводятся без строки «Команда».
    header = dict(team=display_text(content['team'])) if 'team' in content else {}
    return dict(template='aist-aso.v1', version_id=version['id'], sha256=version['content_sha256'],
                title=display_text(content['title']), revision=version['revision'], date=version_date(version['created_at']),
                **header, sections=sections)

# Map familiar headings only when the user requested the ASO catalogue.
# Independent client instructions and human edits keep their own structure.
def organize_sections(content, requested):
    catalogue = {name.casefold(): name for name in requested}
    if 'описание компании' not in catalogue:
        return
    aliases = {
        'бизнес': 'описание компании', 'о компании': 'описание компании',
        'продуктовое предложение': 'описание компании', 'бизнес-модель': 'описание компании',
        'финансы': 'описание компании', 'финансовые и операционные показатели': 'описание компании',
        'стратегия и дивиденды': 'описание компании',
        'рынок': 'рынок и конкуренты', 'конкуренты': 'рынок и конкуренты',
        'конкурентная среда': 'рынок и конкуренты', 'структура собственности': 'акционеры',
        'инвестиционный тезис': 'инвест. тезис', 'инвестиционная привлекательность': 'инвест. тезис',
        'условия сделки': 'описание сделки', 'риски': 'потенциальные риски',
        'новости компании': 'news run', 'глоссарий': 'терминология',
        'приложение': 'приложения',
    }
    merged = {}
    ordered = []
    for section in content['sections']:
        name = section['title'].casefold().strip()
        target = catalogue.get(name) or catalogue.get(aliases.get(name, name))
        if target is None:
            ordered.append(section)
        elif target in merged:
            merged[target]['blocks'].extend(section['blocks'])
        else:
            section['title'] = target
            merged[target] = section
    content['sections'] = [merged[name] for name in requested if name in merged] + ordered

# Only standalone publisher boilerplate. A risk, pricing term or real offer with
# conditions must survive even when it mentions a prospectus, liability or an IPO.
_PUBLISHER_NOTICE = re.compile(
    r'(?:настоящ(?:ий|ая) |эт(?:от|а) )?(?:документ|материал|презентация|информация)'
    r' (?:носит (?:исключительно )?информационный характер'
    r'|(?:не является|не представляет собой) (?:публичной офертой|публичную оферту|'
    r'инвестиционной рекомендацией|индивидуальной инвестиционной рекомендацией|предложением купить ценные бумаги)'
    r'|подготовлен[а]? (?:исключительно )?(?:в информационных целях|для ознакомления))'
    r'(?: и не является (?:публичной офертой|инвестиционной рекомендацией))?[.!]?', re.I)


_DOCUMENT_PURPOSE = re.compile(
    r'(?:настоящ(?:ая|ий) )?(?:презентация|документ|материал)'
    r'(?: для инвесторов)?(?: [а-яё]+ \d{4}(?: года| г\.?)?)?'
    r' подготовлен[а]? в связи с (?:планируемым |предстоящим )?'
    r'публичным (?:предложением|размещением) акций(?: (?:(?-i:[А-ЯЁA-Z]{2,8}) ){0,3}«[^»]+»)?[.]?', re.I)


def omit_editorial_boilerplate(content):
    from .domain import refuse
    sections = []
    removed = False
    for section in content['sections']:
        kept = []
        for block in section['blocks']:
            claim = block.get('claim', {})
            if block['kind'] == 'paragraph':
                prose, marks = reading_paragraph(claim, block.get('marks', []))
                if not prose:
                    removed = True
                    continue
                if prose != claim['text']:
                    claim['text'] = prose
                    if marks:
                        block['marks'] = marks
                    else:
                        block.pop('marks', None)
                    removed = True
            text = ' '.join(claim.get('text', '').split())
            purpose_only = (section['title'].casefold().strip() == 'описание возможности'
                            and _DOCUMENT_PURPOSE.fullmatch(text) and not claim.get('calculation_ids'))
            if (block['kind'] == 'paragraph' and not claim.get('review_candidate_ids')
                    and not claim.get('visual_observation_ids') and (_PUBLISHER_NOTICE.fullmatch(text) or purpose_only)):
                removed = True
            else:
                kept.append(block)
        if kept:
            sections.append(dict(section, blocks=kept))
    if removed:
        if not sections:
            refuse('В ответе нет содержательных фрагментов для записки. Используй сведения из источников.')
        content['sections'] = sections
    return removed
