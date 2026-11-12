"""Unconfirmed parts of a note's extraction requests (FLOW-01.02).

A candidate ingest excluded with a classified reason leaves a gap of its request until an admitted fact of
the same source states the same observation. The gap follows the note through its lineage: the Run saving
the version and the Runs behind its parent versions, including results those Runs received at their start or
reused. A Run with another task inherits nothing, and no statement of the model closes a gap."""
from .domain import known
from .extraction import compact, completed_requests, record_result, recorded_extractions
from .runs import execute
from .service import row
from .source_cards import confirmed_period

CODE = 'coverage_gap'


def startup_entries(runs, c, run, task):
    """The extraction results the agent receives at the start of a Run, each bound to the Run before its first
    step: the Run's own results, the results behind its selected base version, and the completed request whose
    question is the task itself on a pinned source, which another Run completed. The other results of the
    conversation or of the PDF are not delivered: the limitations of a result follow its delivery, and the saved
    facts stay available on their own."""
    base = None
    if run['input']['base_version_id'] is not None:
        saved = row(c, 'SELECT data FROM aist.artifact_versions WHERE id=:v AND project_id=:p AND owner_id=:a',
                    v=run['input']['base_version_id'], p=run['project_id'], a=run['owner_id'])
        base = saved['data'] if saved else None
    entries = lineage_entries(c, run, base)
    delivered = {(entry.get('request_key'), entry['call_id']) for entry in entries}
    for pin in run['input']['source_versions']:
        for entry in completed_requests(runs, c, run, pin, task):
            if (entry['request_key'], entry['call_id']) in delivered:
                continue
            record_result(c, run, entry)
            entries.append(entry)
            delivered.add((entry['request_key'], entry['call_id']))
    # Полные результаты предков нужны для охвата. Составителю доступны только
    # визуальные основания и кандидаты выбранного родителя и текущих результатов.
    from .visual_observations import available
    from .review_candidates import available as available_candidates
    permitted = available(run, base)
    permitted_candidates = available_candidates(run, base)
    current_results = {(entry.get('request_key'), entry['call_id']) for entry in recorded_extractions(run)}
    result = []
    for entry in entries:
        view = dict(entry)
        if 'visual_observations' in entry:
            view['visual_observations'] = [item for item in entry['visual_observations']
                                          if item['observation_id'] in permitted]
        # История предков сохраняет диагностику охвата, но сама по себе не дает
        # доступа к исходным числовым кандидатам. Checkpoint остается неизменным.
        if (entry.get('request_key'), entry['call_id']) not in current_results and 'skipped' in entry:
            view['skipped'] = [{key: value for key, value in item.items()
                                if key != 'review_candidate' or value['candidate_id'] in permitted_candidates}
                               for item in entry['skipped']]
        result.append(view)
    return result


def lineage_entries(c, context, parent):
    """The recorded extraction results behind a version: the saving Run's own, then those of the Runs behind
    its parents, each result once."""
    entries = list(recorded_extractions(context)) if context.get('id') else []
    seen = {context['id']} if context.get('id') else set()
    version = parent
    while version is not None:
        # A version saved by a Run names it in its provenance; an edit names none and continues the chain.
        run_id = (version.get('provenance') or {}).get('run_id')
        if run_id and run_id not in seen:
            seen.add(run_id)
            prior = row(c, 'SELECT checkpoint FROM aist.runs WHERE id=:r AND project_id=:p AND owner_id=:a',
                        r=run_id, p=context['project_id'], a=context['owner_id'])
            if prior:
                entries.extend(recorded_extractions(prior))
        parent_id = version.get('parent_version_id')
        saved = None if parent_id is None else row(
            c, 'SELECT data FROM aist.artifact_versions WHERE id=:v AND project_id=:p AND owner_id=:a',
            v=parent_id, p=context['project_id'], a=context['owner_id'])
        version = saved['data'] if saved else None
    unique = {}
    for entry in entries:
        unique.setdefault((entry.get('request_key'), entry['call_id']), entry)
    return list(unique.values())


def closed(c, project, source, target):
    """An admitted fact of the same source states this observation: the literal metric and issuer, the confirmed
    period, the unit and currency, and the reporting basis where both state one. An equal amount, a page or a
    similar caption is not enough."""
    period = (target['period']['kind'], target['period']['start'], target['period']['end'])
    for fact in execute(c, 'SELECT data FROM aist.facts WHERE source_version_id=:v AND sha256=:sha AND project_id=:p',
                        v=source['source_version_id'], sha=source['sha256'], p=project).scalars():
        amount = fact['amount'] or {}
        if (compact(fact['metric']), compact(fact['subject'])) != (compact(target['metric']), compact(target['subject'])):
            continue
        if confirmed_period(fact['period']) != period or (amount.get('unit'), amount.get('currency')) != (target['unit'], target['currency']):
            continue
        if known(fact['reporting_basis']) and known(target['reporting_basis']) \
                and compact(fact['reporting_basis']) != compact(target['reporting_basis']):
            continue
        return True
    return False


def findings(c, context, parent):
    """Предупреждение о неполноте по каждому незакрытому запросу; детали остаются в Run."""
    remarks = []
    for entry in lineage_entries(c, context, parent):
        # record_failure describes an operation, not an admitted observation. Its refusal stays in the
        # history; actual context dependencies and excluded observations remain independent gaps,
        # including in older records. A complete answer to another question closes neither of them.
        unresolved = (entry.get('coverage') or {}).get('unresolved', [])
        if unresolved:
            message = 'Не разрешена зависимость контекста: ' + ' '.join(unresolved)
            remarks.append(dict(code=CODE, severity='warning', block_id=None, message=message[:1000]))
        unclosed = [item for item in entry.get('skipped', [])
                    if not closed(c, context['project_id'], entry['source'], item['observation'])]
        if not unclosed:
            continue
        codes = ', '.join(sorted({item['code'] for item in unclosed}))
        question = entry.get('question')
        part = 'запроса «' + ' '.join(question.split())[:160] + '»' if question else 'задания'
        message = ('Часть ' + part + ' не подтверждена: ' + str(len(unclosed)) + ' из ' + str(len(entry['skipped']))
                   + ' исключенных кандидатов извлечения не имеют сохраненного основания того же наблюдения (' + codes
                   + '). Исключение кандидата не доказывает отсутствие сведений в источнике; полнота материала и правильность его использования в записке автоматически не подтверждены.')
        remarks.append(dict(code=CODE, severity='warning', block_id=None, message=message[:1000]))
    return remarks
