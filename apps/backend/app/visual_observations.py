"""Непроверенная расшифровка изображения, отдельно от фактов и расчетов."""
from copy import deepcopy

from .domain import refuse
from .facts import pinned_version
from .normalization import checked, fingerprint
from .search import require_factual

NOTICE = 'Записка содержит непроверенные сведения, прочитанные моделью по изображениям страниц.'


def snapshots(candidates, pin, pages, call_id, receipt):
    """Привязка только к изображениям сохраненного запроса, без подтверждения прочтения."""
    if not candidates:
        return []
    if not receipt or receipt.get('source') != pin or receipt.get('visual_input', {}).get('source') != pin:
        refuse('Визуальное наблюдение не связано с изображениями этого запроса.')
    images = {image['page']: image for image in receipt.get('images', [])}
    result = []
    for index, candidate in enumerate(candidates):
        page = candidate['page']
        if page not in pages or page not in images or page not in receipt['visual_input']['pages']:
            refuse('Страница визуального наблюдения не передавалась в этом запросе.')
        numeric = candidate['numeric']
        if numeric:
            period = numeric['period']
            if period['kind'] != 'unknown' and (period['start'] > period['end'] or
                    period['kind'] == 'instant' and period['start'] != period['end']):
                refuse('Период визуального наблюдения противоречив.')
        identity = dict(call_id=call_id, request_key=receipt['key'], index=index, candidate=candidate)
        result.append(checked('VisualObservation', dict(deepcopy(candidate),
            observation_id='visual_' + fingerprint(identity)[:40], source=pin,
            extraction_version=receipt['version'], extraction_call_id=call_id, request_key=receipt['key'],
            image_sha256=images[page]['sha256'], verification_status='unchecked')))
    return result


def authorize(runs, c, run, observation):
    checked('VisualObservation', observation)
    pin = observation['source']
    if pinned_version(runs.service, c, run, pin['source_version_id']) != pin:
        refuse('Источник визуального наблюдения не совпадает с закрепленной версией.')
    require_factual(c, pin['source_version_id'])
    return observation


def available(run, parent=None):
    from .extraction import recorded_extractions
    observations = [item for entry in recorded_extractions(run) for item in entry.get('visual_observations', [])]
    if parent:
        observations.extend(parent['basis'].get('visual_observations', []))
    return {item['observation_id']: item for item in observations}


def inherited_links(parent):
    """Удаление ссылки у прежнего элемента не снимает его непроверенное основание."""
    links = {}
    if parent:
        for section in parent['payload']['content'].get('sections', []):
            for block in section['blocks']:
                if block['kind'] == 'paragraph':
                    links[block['block_id']] = block['claim'].get('visual_observation_ids', [])
                else:
                    for row in block['rows']:
                        links[row['row_id']] = row.get('visual_observation_ids', [])
    return links


def material_keys(observations):
    return {fingerprint({key: item[key] for key in ('source', 'image_sha256', 'page', 'area', 'text', 'numeric')})
            for item in observations}


def annotation(claim, observations):
    pages = sorted({observations[oid]['page'] for oid in claim.get('visual_observation_ids', [])})
    if not pages:
        return ''
    natures = {observations[oid]['numeric']['nature'] for oid in claim.get('visual_observation_ids', [])
               if observations[oid]['numeric']}
    labels = [label for nature, label in (('reported_actual', 'историческое значение'), ('forecast', 'прогноз'))
              if nature in natures]
    text = 'По изображению, стр. ' + ', '.join(map(str, pages)) + ', не проверено'
    return text + ('; ' + ', '.join(labels) if labels else '')


def row_agrees(row, observation):
    """Сверка пересказа с типизированной расшифровкой, не с изображением."""
    numeric = observation['numeric']
    if not numeric or row['amount'] != numeric['amount'] or row['period_label'] != numeric['period']['label']:
        return False
    from .note_standard import same_row_metric
    return same_row_metric(row, numeric['metric'])
