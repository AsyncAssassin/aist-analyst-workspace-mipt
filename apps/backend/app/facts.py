"""Immutable source-bound facts and lexical evidence, shared by QA and extraction.

Ingest is a trusted server operation, deliberately absent from model tool arguments.
Calculation eligibility is explicit; locating text alone does not establish meaning.
"""
import json
import re
from sqlalchemy import text
from .domain import normalized_fact, locate_quote, refuse
from .errors import ApiError
from .normalization import fingerprint, checked
from .service import row


MISSING_FACT_ADVICE = ('Скопируй точный fact_id из результата успешного aist_get_facts. '
                       'Не повторяй ту же отсутствующую ссылку.')


def pin_of(version):
    return dict(source_id=version['source_id'], source_version_id=version['id'], sha256=version['sha256'])


def save_fact(service, c, actor, project, pin, candidate, *, calculation_basis=None, review=None):
    version = service.scoped(c, actor, project, 'source_versions', pin['source_version_id'])
    if pin_of(version) != pin:
        refuse('Версия источника или SHA не совпадают.')
    _, problem = service.inspect(c, version)
    if problem or version['processing_state'] != 'ready':
        refuse('Версия источника недоступна.', 'source_not_ready')
    from .search import require_factual
    require_factual(c, version['id'])
    fact = normalized_fact(candidate, pin)
    basis = calculation_basis or {}
    if set(basis) - {'definition', 'compatibility'} or any(not isinstance(v, str) or len(v)>1000 for v in basis.values()):
        refuse('Некорректная база сопоставимости.')
    # Reviewed composite labels are explicitly distinguished from contiguous quotes.
    if review is not None and (set(review) != {'method','reference'} or review['method'] != 'reviewed_locator' or not isinstance(review['reference'], str) or not review['reference'].strip()):
        refuse('Не указано происхождение сверки основания.')
    locations = []
    for evidence in fact['evidence']:
        locator = evidence['locator']
        if version.get('media_type') == 'application/json':
            if 'json_pointer' not in locator:
                refuse('JSON требует локатор раздела, а не страницу PDF.')
            page = row(c, 'SELECT text FROM aist.source_sections WHERE version_id=:v AND section=:section AND sha256=:sha',
                       v=version['id'], section=locator['json_pointer'], sha=pin['sha256'])
        else:
            if 'pdf_page' not in locator:
                refuse('PDF требует локатор страницы.')
            page = row(c, 'SELECT text FROM aist.source_pages WHERE version_id=:v AND page=:p AND sha256=:sha',
                       v=version['id'], p=locator['pdf_page'], sha=pin['sha256'])
        if not page:
            refuse('Страница основания отсутствует в этой версии.')
        located = locate_quote(page['text'], evidence['quote'])
        if not located and review is None:
            refuse('Цитата не найдена на указанной странице; требуется явная сверка основания.')
        locations.append({'evidence_id':evidence['evidence_id'], 'location': 'whitespace_exact' if located else 'reviewed_locator'})
    provenance = {'locations':locations, 'review':review, 'semantic_status':'not_checked'}
    digest = fingerprint({'data':fact,'calculation_basis':basis,'provenance':provenance})
    # Serializes two importers for this immutable version without locking unrelated projects.
    c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key':version['id']})
    require_factual(c, version['id'])
    old = row(c, 'SELECT * FROM aist.facts WHERE source_version_id=:v AND fact_id=:f', v=version['id'], f=fact['fact_id'])
    if old:
        if old['content_sha256'] != digest:
            refuse('Факт этой версии уже сохранен с другим содержанием.', 'idempotency_conflict')
        return old['data']
    c.execute(text('''INSERT INTO aist.facts(source_version_id,fact_id,source_id,sha256,project_id,owner_id,data,calculation_basis,provenance,content_sha256)
        VALUES (:v,:f,:s,:sha,:p,:a,CAST(:d AS jsonb),CAST(:b AS jsonb),CAST(:proof AS jsonb),:digest)'''),
        dict(v=version['id'],f=fact['fact_id'],s=version['source_id'],sha=pin['sha256'],p=project,a=actor,
             d=json.dumps(fact),b=json.dumps(basis),proof=json.dumps(provenance),digest=digest))
    return fact


LEGACY_EXCERPT_PREFIX = 'По тексту источника (требует сверки): '


def numeric_table_passage(raw):
    """Period headers and repeated numeric rows cannot be read as continuous prose."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    has_period_header = any(re.fullmatch(r'(?:\s*(?:19|20)\d{2}\s*){2,}', line) for line in lines)
    numeric_rows = sum(bool(re.search(r'[^\W\d_]{2,}', line))
                       and len(re.findall(r'(?<!\w)[−-]?\d+(?:[.,]\d+)?(?!\w)', line)) >= 2
                       for line in lines)
    return has_period_header and numeric_rows >= 2


def source_excerpt_text(fact, *, retained=False):
    """One entire primary PDF passage. Never turn a transcription into an Amount."""
    from .extraction import AMBIGUOUS_CARD
    raw = fact['raw_text']
    primary = fact['evidence'][0] if fact['evidence'] else None
    if (fact['amount'] is not None or fact['nature'] != 'source_statement'
            or AMBIGUOUS_CARD in fact['limitations'] or len(raw) >= 2000
            or not primary or primary['source'] != fact['source']
            or not primary['locator'].get('pdf_page') or primary['quote'] != raw):
        return None
    passage = ' '.join(raw.split())
    # Existing versions remain readable/editable; new compositions must use bound cells.
    if not retained and numeric_table_passage(raw):
        return None
    if not re.search(r'\d', passage) or len(re.findall(r'[^\W\d_]{3,}', passage)) < 2:
        return None
    # Flattening a row of bare values would lose geometric relationships in charts.
    if any(re.fullmatch(r'[\s\d.,%+()−-]+', line) and len(re.findall(r'\d+[.,]?\d*\s*%', line)) > 1
           for line in raw.splitlines()):
        return None
    return '«' + passage + '»'


def pinned_version(service, c, run, version_id):
    pin = next((p for p in run['input']['source_versions'] if p['source_version_id'] == version_id), None)
    if pin is None:
        refuse('Источник или версия не входят в разрешенные основания задачи.', 'resource_not_found')
    version = service.scoped(c, run['owner_id'], run['project_id'], 'source_versions', version_id)
    if pin_of(version) != pin:
        refuse('SHA закрепленной версии не совпадает.', 'resource_not_found')
    return pin


def resolve_fact(service, c, run, ref):
    checked('FactRef', ref)
    pin = pinned_version(service, c, run, ref['source_version_id'])
    from .search import require_factual
    require_factual(c, ref['source_version_id'])
    found = row(c, '''SELECT * FROM aist.facts WHERE source_version_id=:v AND fact_id=:f
        AND project_id=:p AND owner_id=:a AND sha256=:sha''',
        v=ref['source_version_id'], f=ref['fact_id'], p=run['project_id'], a=run['owner_id'], sha=pin['sha256'])
    if found is None:
        # Пробел основания в разрешенной версии, в отличие от чужого источника или SHA,
        # остается замечанием места при сохранении документа.
        error = ApiError('resource_not_found')
        error.body['error']['message'] = (f"Факт {ref['fact_id']} отсутствует в разрешенной версии "
                                          f"{ref['source_version_id']}. " + MISSING_FACT_ADVICE)
        error.missing_basis = True
        raise error
    return found


def source_documents(service, c, run):
    documents=[]
    for pin in run['input']['source_versions']:
        pinned_version(service,c,run,pin['source_version_id'])
        version=service.scoped(c,run['owner_id'],run['project_id'],'source_versions',pin['source_version_id'])
        documents.append(dict(source=pin,original_filename=version['original_filename'],revision=version['revision']))
    return documents


def comparison_basis(service, c, run, facts):
    """Сохраняем альтернативные наблюдения выбранных PDF, не выбирая победителя."""
    from .domain import compare_observations
    def key(f):
        p=f['period']
        return (re.sub(r'\s+','',f['metric']).casefold(),p['kind'],p['start'],p['end'])
    referenced={key(f) for f in facts if f['amount']}
    records={}
    for pin in run['input']['source_versions']:
        pinned_version(service,c,run,pin['source_version_id'])
        for saved in c.execute(text('''SELECT * FROM aist.facts WHERE source_version_id=:v
                AND project_id=:p AND owner_id=:a AND sha256=:sha'''),
                dict(v=pin['source_version_id'],p=run['project_id'],a=run['owner_id'],sha=pin['sha256'])).mappings():
            f=saved['data']
            if f['amount'] and key(f) in referenced:records[(pin['source_version_id'],f['fact_id'])]=dict(saved)
    comparisons=[]
    ordered=sorted(records)
    for i,left in enumerate(ordered):
        for right in ordered[i+1:]:
            a,b=records[left],records[right]
            if left[0]!=right[0] and key(a['data'])==key(b['data']):
                comparisons.append(compare_observations(a,b))
    needed={tuple(ref[k] for k in ('source_version_id','fact_id')) for x in comparisons for ref in x['fact_refs']}
    merged={(f['source']['source_version_id'],f['fact_id']):f for f in facts}
    merged.update({k:records[k]['data'] for k in needed})
    return [merged[k] for k in sorted(merged)],comparisons


def lexical_page_matches(pages, tokens):
    """All matching saved pages, without a top-N cutoff; the public search still applies its own limit."""
    return {page: found for page, content in pages.items()
            if (found := [token for token in tokens if token in content.casefold()])}


def lexical_search_evidence(service, c, run, args):
    checked('SearchEvidenceArgs', args)
    tokens = re.findall(r'\w+', args['query'].casefold())
    matches = {}
    for vid in args['source_version_ids']:
        pin = pinned_version(service, c, run, vid)
        facts = c.execute(text('SELECT data FROM aist.facts WHERE source_version_id=:v AND owner_id=:a AND project_id=:p'),
                          {'v':vid,'a':run['owner_id'],'p':run['project_id']}).scalars()
        for f in facts:
            for e in f['evidence']:
                hay = ' '.join([f['metric'],f['raw_text'],f['period']['label'],e['quote'],e['locator']['area']]).casefold()
                score = sum(t in hay for t in tokens)
                if score:
                    matches[(vid,e['evidence_id'])] = (score,e)
        pages = {p['page']: p['text'] for p in c.execute(text(
            'SELECT page,text FROM aist.source_pages WHERE version_id=:v ORDER BY page'), {'v':vid}).mappings()}
        for page, found in lexical_page_matches(pages, tokens).items():
            p = dict(page=page, text=pages[page])
            lower = p['text'].casefold()
            score = len(found)
            start = min(lower.index(t) for t in tokens if t in lower)
            quote = p['text'][max(0,start-150):start+700]
            eid = 'ev_' + fingerprint({'source':pin,'page':p['page'],'quote':quote})[:32]
            e = dict(evidence_id=eid,source=pin,locator=dict(pdf_page=p['page'],area='Совпадение в сохраненном тексте страницы',bbox_pt=None,coordinate_system='pdf_points_top_left'),quote=quote)
            matches[(vid,eid)] = (score,e)
    ordered = sorted(matches.values(), key=lambda match: (
        -match[0], match[1]['source']['source_version_id'],
        match[1]['locator'].get('pdf_page', 0), match[1]['locator'].get('json_pointer', ''),
        match[1]['evidence_id']))
    return checked('SearchEvidenceResult',dict(items=[e for _,e in ordered[:args['limit']]],method='lexical.v1',searched_source_version_ids=args['source_version_ids']))


def search_evidence(service, c, run, args, settings=None, *, authorize_embedding=None):
    from .search import search_evidence as search
    return search(service,c,run,args,settings,authorize_embedding=authorize_embedding)
