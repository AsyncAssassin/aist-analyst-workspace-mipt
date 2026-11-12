"""Ссылки на отклоненный ввод из текущего Run и выбранной версии, без допуска в факты."""
from .domain import refuse
from .facts import pinned_version
from .normalization import checked
from .search import require_factual


def available(run, parent=None):
    from .extraction import recorded_extractions
    items = [
        {key: skip[key] for key in ('review_candidate', 'code', 'reason', 'message')}
        for entry in recorded_extractions(run) for skip in entry.get('skipped', [])
        if 'review_candidate' in skip
    ]
    if parent:
        items.extend(parent['basis'].get('review_candidates', []))
    return {item['review_candidate']['candidate_id']: item for item in items}


def authorize(runs, c, run, item):
    checked('ReviewCandidateBasis', item)
    pin = item['review_candidate']['source']
    if pinned_version(runs.service, c, run, pin['source_version_id']) != pin:
        refuse('Источник кандидата не совпадает с закрепленной версией.', 'resource_not_found')
    require_factual(c, pin['source_version_id'])
    return item


def inherited_links(parent):
    if not parent:
        return {}
    return {block['block_id']: block['claim']['review_candidate_ids']
            for section in parent['payload']['content'].get('sections', []) for block in section['blocks']
            if block['kind'] == 'paragraph' and block['claim'].get('review_candidate_ids')}


def pages(candidate):
    """Все указанные страницы исходного ввода; содержимое кандидата не нормализуется."""
    value = candidate['input']
    numbers = {link['page'] for link in value['evidence']}
    for part in ('basis', 'table'):
        if value.get(part):
            numbers.add(value[part]['page'])
    if value.get('basis') and 'reporting_page' in value['basis']:
        numbers.add(value['basis']['reporting_page'])
    return sorted(numbers)


def annotation(claim, basis):
    used = set(claim.get('review_candidate_ids', []))
    if not used:
        return ''
    documents = {item['source']['source_version_id']: item['original_filename']
                 for item in basis.get('sources', [])}
    source_pages = {}
    for item in basis.get('review_candidates', []):
        candidate = item['review_candidate']
        if candidate['candidate_id'] in used:
            version = candidate['source']['source_version_id']
            source_pages.setdefault(version, set()).update(pages(candidate))
    labels = [documents.get(version, 'Исходный документ') + ', стр. ' + ', '.join(map(str, sorted(numbers)))
              for version, numbers in source_pages.items()]
    return 'Требует сверки с источником. Предварительные сведения: ' + '; '.join(labels) + '.'
