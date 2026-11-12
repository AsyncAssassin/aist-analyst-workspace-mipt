"""Bounded basis collection of a first v3 note generation (FLOW-01).

The agent spends a few ordinary model calls and nested extractions on the basis; the remaining
calls and time stay reserved for the completion steps. The transition to completion is stored
in the Run checkpoint and enforced at the server boundary, not only by the skill text."""
from .accounting import attempt_counts, extraction_counts
from .errors import ApiError, error_value
from .live_profile import COLLECTION
from .runs import execute, json_value, now, row, age_seconds

BOUNDED_SKILL = 'skill_analytical-note-v3'
# Stable questions allow exact reuse of the two receipts. The user's task is also sent
# to each extraction; no issuer, page number or example is embedded in these questions.
NOTE_TOPICS = (
    'Финансовые результаты и операционные показатели компании. Собери главные фактические '
    'значения, единицы и масштаб: выручка, прибыль, EBITDA или их отраслевые аналоги, '
    'долг и долговая нагрузка, ликвидность и оборотный капитал. При наличии данных сначала '
    'охвати эти показатели за два последних сопоставимых периода, затем сохрани весь компактно раскрытый '
    'сопоставимый ряд существенных показателей. '
    'Для каждой существенной строки таблицы сначала закончи пару сопоставимых периодов, включая '
    'рентабельность, коэффициенты и изменение оборотного капитала. Описание бизнеса, магазинов, '
    'клиентов и рынка оставь отдельному проходу; здесь нужны только финансовые данные и их оговорки. '
    'Каждый показатель и период передай отдельным числовым наблюдением, включая отрицательные '
    'значения и коэффициенты. Сохрани опубликованные CAGR, маржу и изменение оборотного капитала '
    'с их интервалом и атрибуцией, без самостоятельного пересчета. '
    'Отдельно укажи существенные финансовые цели и прогнозы менеджмента.',
    'Бизнес и положение компании: основные продукты и клиенты, рынок и сравнение с конкурентами, '
    'стратегия и риски. Извлеки раскрытые условия рассматриваемой сделки или привлечения капитала '
    'с их статусом (намерение, объявлена, завершена), структуру собственности с долями и датой '
    'и дивидендную политику, если они есть. '
    'Для существенных инвестиционных проектов извлеки отдельные числовые основания суммы инвестиций '
    'и капитальных затрат с валютой, масштабом, названием проекта и статусом (план, прогноз, факт). '
    'Сохрани срок запуска отдельно от периода инвестиций: дату запуска нельзя подставлять как период суммы. '
    'Если период суммы не раскрыт, укажи неизвестный период. Не оставляй сумму только внутри качественной цитаты. '
    'Сохрани страницы и даты.',
)
NOTE_COMBINED_TOPIC = (
    'Финансовые результаты и бизнес компании. Собери существенные финансовые показатели за два последних '
    'сопоставимых периода: результаты, долг, рентабельность, коэффициенты, ликвидность и оборотный капитал. '
    'Сохрани компактно раскрытые сопоставимые ряды существенных показателей, отдельные значения, '
    'знак, единицы и периоды; различай факты и прогнозы. '
    + NOTE_TOPICS[1]
)


REASONS = {
    'extractions': 'использованы все разрешенные извлечения',
    'idle': 'два извлечения подряд не дали нового материала',
    'previous_latency': 'с учётом времени первого извлечения оставшегося окна недостаточно для второго',
    'reserve_calls': 'оставшиеся обращения к модели сохранены для расчета, составления и сохранения',
    'reserve_time': 'оставшееся время сохранено для составления и сохранения',
    'overload': 'модельный сервис перегружен, повторная отправка извлечения не разрешена',
    'reserve_money': 'доступная сумма сохранена для завершающего ответа',
}


def bounded_budget(policy_run, skill_id, result_type, base_version_id, model_profile_id=None):
    """Collection limits of a new Run, never wider than the server policy; None for other Runs."""
    from .live_profile import FABLE_ID, FULL_EXTRACTION_IDS, FABLE_VISUAL_ID, OPENAI_ID
    dialogue = (skill_id == 'skill_source-answer-v3' and result_type == 'evidence_answer'
                and base_version_id is None and model_profile_id in (FABLE_ID, *FULL_EXTRACTION_IDS))
    if not dialogue and (skill_id != BOUNDED_SKILL or result_type != 'analytical_note' or base_version_id is not None):
        return None
    # Публичное исследование занимает несколько продолжений. Денежные пределы
    # и более строгая серверная политика сохраняются.
    bounds = (dict(COLLECTION, max_model_calls=10, max_extractions=0, send_seconds=600,
                   max_duration_seconds=1200, completion_seconds=840)
              if model_profile_id == FABLE_ID else COLLECTION)
    if model_profile_id in FULL_EXTRACTION_IDS:
        bounds = dict(COLLECTION, max_duration_seconds=1800, send_seconds=600, extraction_seconds=300,
                      completion_seconds=360, min_extraction_seconds=60, max_extractions=2, max_tool_calls=40)
        if model_profile_id in (FABLE_VISUAL_ID, OPENAI_ID) and not dialogue:
            bounds['extraction_seconds'] = 600
    if dialogue:
        bounds = dict(kind='dialogue', max_model_calls=4, max_extractions=0 if model_profile_id == FABLE_ID else 1,
                      completion_calls=2, idle_extractions=1, max_duration_seconds=300, send_seconds=180,
                      extraction_seconds=120, completion_seconds=90, min_extraction_seconds=30, max_tool_calls=12)
    calls = min(policy_run['max_model_calls'], bounds['max_model_calls'])
    duration = min(policy_run['max_duration_seconds'], bounds['max_duration_seconds'])
    return dict(bounds, max_model_calls=calls, max_duration_seconds=duration,
                completion_calls=min(bounds['completion_calls'], calls))


def limits(run):
    return (run.get('configuration') or {}).get('collection')


def is_dialogue(run):
    return (limits(run) or {}).get('kind') == 'dialogue'


def state(run):
    return (run.get('checkpoint') or {}).get('collection') or {'closed': False, 'idle': 0}


def collecting(run):
    """A bounded Run before its transition to completion."""
    return limits(run) is not None and not state(run)['closed']


def send_seconds(run, transport_seconds, purpose='agent'):
    """One physical send of a bounded Run ends within the transport limit, the send limit and the remaining Run time."""
    bounds = limits(run)
    if bounds is None:
        return transport_seconds
    remaining = run['input']['budget']['max_duration_seconds'] - age_seconds(run)
    limit = bounds.get('extraction_seconds', bounds['send_seconds']) if purpose == 'extraction' else bounds['send_seconds']
    if 'min_extraction_seconds' in bounds and purpose == 'extraction':
        remaining -= bounds['completion_seconds']
    return max(0.0, min(transport_seconds, limit, remaining))


def retry_reserve(run):
    """Seconds that must remain after a granted overload pause: the next send, plus the completion reserve while collecting."""
    bounds = limits(run)
    if bounds is None:
        return 0
    return bounds['send_seconds'] + (0 if state(run)['closed'] else bounds['completion_seconds'])


def shortfall(c, run, *, reserved=False, attempt_seconds=None):
    """Why the collection must close before the next ordinary send, or None when the send still fits:
    after it the completion steps keep their calls and, after its longest wait, their seconds.

    Before the reservation the send is the one pending attempt. Right before the physical POST
    (reserved=True) the saved calls already count it exactly once, so a 429 retry is neither a new
    ordinary call nor a new extraction, and attempt_seconds are the seconds left to the fixed deadline
    of this attempt: the time already spent on the connection is part of it, not added again."""
    bounds = limits(run)
    budget = run['input']['budget']
    pending = 0 if reserved else 1
    extractions, _ = extraction_counts(c, run['id'])
    if extractions + pending > bounds['max_extractions']:
        return 'extractions'
    ordinary, _ = attempt_counts(c, run['id'])
    if ordinary + pending + bounds['completion_calls'] > budget['max_model_calls']:
        return 'reserve_calls'
    seconds = bounds.get('extraction_seconds', bounds['send_seconds']) if attempt_seconds is None else attempt_seconds
    remaining = budget['max_duration_seconds'] - age_seconds(run)
    if 'min_extraction_seconds' in bounds:
        seconds = min(seconds, remaining - bounds['completion_seconds'])
        if seconds < bounds['min_extraction_seconds']:
            return 'reserve_time'
        # With usable material already committed, prefer finishing over a second
        # long extraction that would consume the completion reserve mid-stream.
        if extractions and not is_dialogue(run) and any(
                item.get('fact_refs') or item.get('visual_observations') for item in results(run)):
            elapsed = execute(c, """SELECT max(extract(epoch from
                ((data->>'observed_at')::timestamptz - (data->>'sent_at')::timestamptz)))
                FROM aist.model_calls WHERE run_id=:r AND data->>'purpose'='extraction'
                AND data->>'provider_outcome'='success' AND data->>'delivery'='delivered'""",
                r=run['id']).scalar()
            if elapsed and elapsed > bounds['min_extraction_seconds']:
                needed = min(bounds['extraction_seconds'], float(elapsed) + bounds['min_extraction_seconds'])
                if remaining - bounds['completion_seconds'] < needed:
                    return 'previous_latency'
    if seconds + bounds['completion_seconds'] > remaining:
        return 'reserve_time'
    if 'demo_access' in run['configuration']['policy']:
        from .accounting import money_available
        if not money_available(c, run, pending + 1):
            return 'reserve_money'
    return None


def save_state(c, run, value):
    checkpoint = dict(run.get('checkpoint') or {}, collection=value)
    execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r', v=json_value(checkpoint), r=run['id'])
    run['checkpoint'] = checkpoint
    return value


def close(runs, c, run, reason):
    """Record the transition to completion once; a later cause keeps the first recorded reason."""
    current = state(run)
    if current['closed']:
        return current
    current = save_state(c, run, dict(current, closed=True, reason=reason, message=REASONS[reason], closed_at=now()))
    runs.event(c, run, 'collection_closed', {'reason': reason, 'message': current['message']})
    return current


def completed_extraction(runs, run, new_facts):
    """Count a finished extraction; two in a row without new usable facts close the collection."""
    with runs.engine.begin() as c:
        current = runs.locked(c, run['id'])
        if current['state'] != 'running' or not collecting(current):
            return
        idle = 0 if new_facts else state(current).get('idle', 0) + 1
        save_state(c, current, dict(state(current), idle=idle))
        if idle >= limits(current)['idle_extractions']:
            close(runs, c, current, 'idle')


def empty_topic_needs_evidence(question, pages):
    """Empty extraction is inconclusive for scans or slides that explicitly discuss this theme."""
    import re
    if any(not text.strip() for text in pages):
        return True
    finance = r'выручк|чистая прибыль|финансовые результаты|ebitda|revenue|financial results'
    business = (r'бизнес-модель|продукт|клиент|конкурент|стратеги|фактор.{0,4}риска|условия сделки|'
                r'инвестиц|капитальн.{0,5}затрат|запуск|business model|market share|investment|capex|commissioning')
    indicators = finance if question == NOTE_TOPICS[0] else business
    if question == NOTE_COMBINED_TOPIC:
        indicators = finance + '|' + business
    return any(re.search(indicators, text, re.I) for text in pages)


def require_note_topics(run, basis):
    """Do not publish a new note from an unfinished extraction or omit a whole prepared theme."""
    from .domain import refuse
    if run['input'].get('skill_version_id') != BOUNDED_SKILL or run['input'].get('base_version_id'):
        return
    entries = results(run)
    prepared = state(run).get('prepared_topics')
    if prepared is not None:
        for expected in prepared:
            if not any(e.get('source') == expected['source'] and e.get('question') == expected['question']
                       and e.get('status') and not e.get('error') for e in entries):
                refuse('Сбор запрошенных материалов не завершён. Записка не сохранена.')
        used_sources = {f['source']['source_version_id'] for f in basis['facts']}
        used_sources.update(v['source']['source_version_id'] for v in basis.get('visual_observations', []))
        used_sources.update(v['review_candidate']['source']['source_version_id']
                            for v in basis.get('review_candidates', []))
        if any(pin['source_version_id'] not in used_sources for pin in run['input']['source_versions']):
            refuse('Не использован один из выбранных PDF. Записка не сохранена: включи его существенные '
                   'сведения или уточни в чате состав материалов.')
    # A budget refusal before any attempt is request history, not a failed extraction.
    # Required prepared themes have already been checked above; sent/unknown attempts still block.
    unstarted = {key for key, operation in (run.get('checkpoint') or {}).get('extraction_operations', {}).items()
                 if operation.get('error', {}).get('code') == 'budget_exceeded'
                 and operation.get('call_ids') == []} if prepared is not None else set()
    # A different successful question cannot close the receipt of a failed one.
    if any(e.get('error') and not any(other.get('source') == e.get('source')
            and other.get('question') == e.get('question') and other.get('status') and not other.get('error')
            for other in entries) and not (e.get('request_key') in unstarted
                and e['error']['code'] == 'budget_exceeded') for e in entries):
        refuse('Сбор запрошенных материалов не завершён. Записка не сохранена.')
    if prepared is None and (state(run).get('reason') == 'previous_latency'
            or any(e.get('error') for e in entries) and len({e['call_id'] for e in entries if e.get('status')}) < 2):
        if not all(any(e['question'] == q and 'status' in e and not e.get('error') for e in entries)
                   for q in NOTE_TOPICS):
            refuse('Сбор запрошенных материалов не завершён. Записка не сохранена.')
    used_facts = {(f['source']['source_version_id'], f['fact_id']) for f in basis['facts']}
    used_images = {v['observation_id'] for v in basis.get('visual_observations', [])}
    for item in entries:
        if item['question'] not in (*NOTE_TOPICS, NOTE_COMBINED_TOPIC) or item.get('error'):
            continue
        refs = {(ref['source_version_id'], ref['fact_id']) for ref in item['fact_refs']}
        images = {v['observation_id'] for v in item.get('visual_observations', [])}
        if (refs or images) and not (refs & used_facts or images & used_images):
            refuse('В записке пропущена обработанная тема «' + item['question'].split('.')[0]
                   + '». Используй относящиеся к ней основания и сохрани их ссылки.')


def known_facts(c, run, pin):
    return set(execute(c, 'SELECT fact_id FROM aist.facts WHERE source_version_id=:v AND project_id=:p',
                       v=pin['source_version_id'], p=run['project_id']).scalars())


def results(run):
    """The question results recorded for this Run, with their saved references: its own extractions and the results
    bound to it at its start or through the cache. A result of the conversation the Run never received is not named."""
    from .extraction import recorded_extractions
    return [item for item in recorded_extractions(run) if item.get('question') is not None]


def prior_numeric_observations(runs, c, run, pin, question):
    """Give the business pass its financial checklist, frozen by the existing send operation.

    Only this Run's received financial receipt for the same pinned source contributes. The
    checklist never grants evidence or upgrades visual observations. Recovery reuses the
    original checklist, rather than changing the request hash after a later extraction.
    """
    if question is None or ' '.join(question.split()).casefold() != NOTE_TOPICS[1].casefold():
        return None
    from copy import deepcopy
    from .extraction import VERSION
    from .extraction_requests import operations
    from .facts import resolve_fact
    from .normalization import fingerprint
    entries = results(run)
    saved = [*operations(run).values(), *(entry.get('visual_request', {}) for entry in entries)]
    for item in saved:
        if (item.get('source') == pin and item.get('version') == VERSION
                and ' '.join((item.get('question') or '').split()).casefold() == NOTE_TOPICS[1].casefold()
                and 'prior_numeric_observations' in item):
            return deepcopy(item['prior_numeric_observations'])
    observations = {}
    for entry in entries:
        if entry.get('source') != pin or entry.get('question') != NOTE_TOPICS[0] or entry.get('error'):
            continue
        for ref in entry['fact_refs']:
            fact = resolve_fact(runs.service, c, run, ref)['data']
            if fact.get('amount') is None:
                continue
            item = {key: fact[key] for key in ('metric', 'subject', 'amount', 'period', 'nature')}
            item.update(pages=sorted({e['locator']['pdf_page'] for e in fact['evidence']}),
                        evidence_kind='saved_text')
            observations[fingerprint(item)] = item
        for visual in entry.get('visual_observations', []):
            if visual.get('numeric') is None:
                continue
            item = deepcopy(visual['numeric'])
            item.update(pages=[visual['page']], evidence_kind='visual_unchecked')
            observations[fingerprint(item)] = item
    return [observations[key] for key in sorted(observations)]


def results_text(items):
    if not items:
        return 'Сохраненных результатов извлечений нет.'
    lines = []
    for item in items:
        references = [ref['fact_id'] for ref in item['fact_refs']]
        references.extend(observation['observation_id'] + ' (визуальное, не проверено)'
                          for observation in item.get('visual_observations', []))
        line = '«' + (item.get('question') or 'Исходное задание') + '»: ' + (', '.join(references) or 'материал не получен')
        if item.get('limitation'):
            line += '; ограничение: ' + item['limitation']
        if item.get('skipped'):
            # An excluded candidate is neither a saved fact nor proof that the source lacks the data.
            codes = ', '.join(sorted({entry['code'] for entry in item['skipped']}))
            line += '; исключено кандидатов: ' + str(len(item['skipped'])) + ' (' + codes + ')'
        if item.get('coverage'):
            line += '; страницы: ' + ', '.join(map(str, item['coverage']['pages']))
        lines.append(line)
    return 'Имеющиеся результаты извлечений: ' + '; '.join(lines) + '.'


def limitation(run):
    """The reply to an extraction request after the transition: the limitation and the known results, no POST."""
    failure = ApiError('budget_exceeded')
    failure.body['error']['message'] = ('Сбор оснований закрыт: ' + state(run)['message'] + '. Новое извлечение не выполняется. '
                                        + results_text(results(run))
                                        + ' Используй сохраненные факты через aist_get_facts с refs, aist_calculate и aist_save_artifact. '
                                          'Сохраняй записку только после разбора существенных тем. Незавершённый сбор объясни в чате, '
                                          'не добавляя служебный абзац в документ. Числа без оснований не добавляй.')
    return failure


def withheld_error(current):
    """The error of a ModelCall whose POST the send boundary withheld because the collection closed first."""
    return dict(error_value('budget_exceeded'), message='POST не отправлен: сбор оснований закрыт (' + current['message'] + ').')


def withheld_refusal(runs, run):
    """After a nested POST withheld at the send boundary: the limitation and the known results while the Run
    goes on with its completion steps; None when the Run ended instead."""
    with runs.engine.connect() as c:
        current = row(c, 'SELECT * FROM aist.runs WHERE id=:r', r=run['id'])
        if current['state'] != 'running' or not state(current)['closed']:
            return None
        return limitation(current)


def overload_refusal(runs, run):
    """After a completely received 429 of a nested extraction without a granted retry: close the collection,
    keep the Run running and hand the known refusal to the agent. None when this is not that case."""
    with runs.engine.begin() as c:
        current = runs.locked(c, run['id'])
        last = row(c, """SELECT data FROM aist.model_calls WHERE run_id=:r AND data->>'purpose'='extraction'
                         ORDER BY created_at DESC, id DESC LIMIT 1""", r=run['id'])
        refused = bool(last) and last['data']['transport_state'] == 'observed' and last['data']['http_status'] == 429 \
            and last['data']['provider_outcome'] == 'error'
        if current['state'] != 'running' or not collecting(current) or not refused:
            return None
        close(runs, c, current, 'overload')
        message = last['data']['error']['message']
    failure = ApiError('provider_error')
    result_name = 'ответ' if is_dialogue(run) else 'записку'
    failure.body['error']['message'] = ('Извлечение не выполнено: ' + message
                                        + ' Сбор оснований закрыт: составь ' + result_name
                                        + ' только если основной материал уже разобран. Иначе сообщи о незавершённом сборе в чате без сохранения записки.')
    # The known refusal is recorded on the tool call; the Run keeps its completion steps.
    failure.run_continues = True
    return failure


def context(runs, c, run):
    """The service hint before the next agent request of a bounded Run: the limits while collecting,
    the completion mode after the transition. The reserve is checked and the collection closed here first."""
    bounds = limits(run)
    current = state(run)
    if not current['closed']:
        window = None
        if 'min_extraction_seconds' in bounds:
            window = send_seconds(run, runs.settings.transport_seconds, 'extraction')
        elif 'extraction_seconds' in bounds:
            window = min(runs.settings.transport_seconds, bounds['extraction_seconds'])
        reason = shortfall(c, run, attempt_seconds=window)
        if reason:
            current = close(runs, c, run, reason)
    if current['closed']:
        research = run.get('external_context') or {}
        research_hint = (
            'Закрытие сбора из PDF не закрывает отдельное публичное исследование: aist_research и aist_read_web '
            'остаются доступны в пределах собственных лимитов. Выполни необходимое исследование по заданию '
            'до сохранения записки. '
            if research.get('research_version') == 'agent.v2' and not research.get('research_closed') else '')
        return ('Режим завершения: сбор оснований закрыт (' + current['message'] + '). Новое извлечение (aist_get_facts с question) '
                'и платный поиск по смыслу недоступны: сервер отклонит их без отправки. Доступны чтение сохраненных фактов '
                '(aist_get_facts с refs), aist_calculate, aist_save_artifact и aist_request_input. ' + research_hint + results_text(results(run))
                + (' Сохрани ответ на вопрос по имеющимся основаниям.' if is_dialogue(run) else ' Сохрани записку, только если разобраны существенные темы запроса. При незавершённом сборе сообщи о пробеле в чате.')
                + ' Пробел в доступных основаниях не приписывай PDF. '
                'Пробелы сбора объясняй в чате, не в тексте записки. Числа без оснований не добавляй. '
                'Уточнение запрашивай только если без него черновик невозможен. '
                'Это служебное состояние исполнения, в текст результата его не включай.')
    extractions, _ = extraction_counts(c, run['id'])
    return ('Сбор оснований ограничен: извлечений выполнено ' + str(extractions) + ' из ' + str(bounds['max_extractions'])
            + ('; повторное извлечение недоступно; ' if is_dialogue(run) else '; два извлечения подряд без нового материала закрывают сбор; ') + str(bounds['completion_calls'])
            + ' обращения и ' + str(bounds['completion_seconds']) + ' с сохраняются для расчета, составления и одной коррекции '
            'сохранения. Счетчик обращений включает вложенные извлечения aist_get_facts. Чтение готовых фактов, расчет и '
            'сохранение не вызывают модель, но после ответа инструмента обычно нужен следующий модельный шаг. Если основания '
            'достаточны, заверши результат; не расходуй остаток на необязательные числа. Это служебное состояние исполнения, '
            'в текст результата его не включай.')
