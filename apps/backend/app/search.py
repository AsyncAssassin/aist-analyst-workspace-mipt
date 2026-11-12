"""Page-grounded retrieval. PostgreSQL authorizes and verifies every returned chunk."""
import json
from contextlib import nullcontext
import math
import uuid
import urllib.request
import urllib.error
from sqlalchemy import text
from .config import Settings
from .errors import ApiError
from .service import row
from .normalization import checked, fingerprint
from .embeddings import CONFIG, INDEX_ID, Embeddings, chunks, input_text, digest


def material(connection, version_id):
    saved = row(connection, 'SELECT * FROM aist.knowledge_materials WHERE version_id=:id', id=version_id)
    if saved and saved['corpus'] == 'samples':
        return saved
    # Older D15 uploads may already have unclassified descendants. Resolve their
    # restriction as well as copying it for new uploads; exact quotes do not undo it.
    restricted = row(connection, 'SELECT id FROM aist.sample_source_versions WHERE id=:id', id=version_id)
    if restricted:
        return dict(version_id=version_id, corpus='samples', company=None, period=None,
                    provenance='Ограничение исходного образца')
    return saved


def inherit_sample(connection, version_id):
    metadata = material(connection, version_id)
    if metadata and metadata['corpus'] == 'samples':
        connection.execute(text("""INSERT INTO aist.knowledge_materials
            (version_id,corpus,company,period,provenance)
            VALUES (:version_id,:corpus,:company,:period,:provenance)"""), metadata)


def require_factual(connection, version_id):
    metadata = material(connection, version_id)
    if metadata and metadata['corpus'] != 'factual':
        raise ApiError('sample_not_evidence')


def register(service, actor, project, version_id, corpus, company, period, provenance):
    if corpus not in ('factual', 'samples') or not provenance or len(provenance) > 2000:
        raise ApiError('validation_error')
    if any(value is not None and (not value.strip() or len(value) > 300) for value in (company, period)):
        raise ApiError('validation_error')
    with service.engine.begin() as connection:
        service.scoped(connection, actor, project, 'source_versions', version_id)
        connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), dict(key=version_id))
        if corpus == 'samples' and row(connection, 'SELECT fact_id FROM aist.facts WHERE source_version_id=:id LIMIT 1', id=version_id):
            raise ApiError('sample_not_evidence')
        metadata = dict(version_id=version_id, corpus=corpus, company=company, period=period, provenance=provenance)
        existing = material(connection, version_id)
        if existing:
            if existing != metadata:
                raise ApiError('idempotency_conflict')
            return metadata
        connection.execute(text("""INSERT INTO aist.knowledge_materials(version_id,corpus,company,period,provenance)
            VALUES (:version_id,:corpus,:company,:period,:provenance)"""), metadata)
        return metadata


class Qdrant:
    def __init__(self, settings): self.url=settings.qdrant_url.rstrip('/')

    def request(self, method, path, payload=None, allow_missing=False):
        data=json.dumps(payload).encode() if payload is not None else None
        req=urllib.request.Request(self.url+path,data=data,method=method,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                raw=response.read(1000001)
                if len(raw)>1000000: raise ValueError()
                value=json.loads(raw)
                if value.get('status')!='ok': raise ValueError()
                return value['result']
        except urllib.error.HTTPError as error:
            if error.code==404 and allow_missing:return None
            raise ApiError('search_index_unavailable') from None
        except (OSError,ValueError,KeyError,TypeError):
            raise ApiError('search_index_unavailable') from None

    def collection(self, corpus):
        if corpus not in ('factual','samples'): raise ApiError('validation_error')
        return '/collections/aist_'+INDEX_ID+'_'+corpus

    def ensure(self, corpus):
        path=self.collection(corpus)
        current=self.request('GET',path,allow_missing=True)
        if current is None:
            self.request('PUT',path,dict(vectors=dict(size=CONFIG['dimension'],distance='Cosine')))
        elif current.get('config',{}).get('params',{}).get('vectors')!={'size':CONFIG['dimension'],'distance':'Cosine'}:
            # Qdrant may add optional fields to vector configuration.
            v=current.get('config',{}).get('params',{}).get('vectors',{})
            if v.get('size')!=CONFIG['dimension'] or v.get('distance')!='Cosine':raise ApiError('search_index_unavailable')
        for field in ('owner_id','project_id','version_id','company','period'):
            if current is None or field not in current.get('payload_schema',{}):
                self.request('PUT',path+'/index?wait=true',dict(field_name=field,field_schema='keyword'))


def index_material(service, settings, actor, project, version_id, *, authorize_embedding=None):
    embeddings = Embeddings(service.engine, settings)
    qdrant = Qdrant(settings)
    with service.engine.begin() as connection:
        version = service.scoped(connection, actor, project, 'source_versions', version_id)
        _, problem = service.inspect(connection, version)
        if problem or version['processing_state'] != 'ready':
            raise ApiError('source_not_ready')
        metadata = material(connection, version_id)
        if not metadata:
            raise ApiError('search_not_indexed')
        embeddings.configuration(connection)
        pages = connection.execute(text('SELECT page,NULL AS section,text FROM aist.source_pages WHERE version_id=:id ORDER BY page'),
                                   dict(id=version_id)).mappings().all()
        if version.get('media_type') == 'application/json':
            pages = connection.execute(text('SELECT NULL AS page,section,text FROM aist.source_sections WHERE version_id=:id ORDER BY section'),
                                       dict(id=version_id)).mappings().all()
        fragments = []
        for page in pages:
            for start, end, quote in chunks(page['text']):
                normalized_text = input_text(quote)
                input_hash = digest(normalized_text)
                location = page['page'] if page['page'] is not None else page['section']
                identity = ':'.join(map(str, [INDEX_ID, version_id, version['sha256'], location, start, end, input_hash]))
                fragment = dict(index_id=INDEX_ID, id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                                version_id=version_id, page=page['page'], section=page['section'], char_start=start, char_end=end,
                                input_sha256=input_hash)
                connection.execute(text("""INSERT INTO aist.knowledge_chunks
                    (index_id,id,version_id,page,section,char_start,char_end,input_sha256)
                    VALUES (:index_id,:id,:version_id,:page,:section,:char_start,:char_end,:input_sha256)
                    ON CONFLICT DO NOTHING"""), fragment)
                fragments.append(dict(fragment, input_text=normalized_text))
    # Commit embeddings before Qdrant so an interrupted upsert reuses paid vectors.
    qdrant.ensure(metadata['corpus'])
    for offset in range(0, len(fragments), CONFIG['max_batch']):
        batch = fragments[offset:offset + CONFIG['max_batch']]
        vectors = embeddings.vectors(actor, [fragment['input_text'] for fragment in batch], 'document', authorize=authorize_embedding)
        points = [dict(id=fragment['id'], vector=vector, payload=dict(
            owner_id=actor, project_id=project, version_id=version_id, company=metadata['company'],
            period=metadata['period'], corpus=metadata['corpus'], index_id=INDEX_ID))
            for fragment, vector in zip(batch, vectors)]
        qdrant.request('PUT', qdrant.collection(metadata['corpus']) + '/points?wait=true', dict(points=points))
        with service.engine.begin() as connection:
            connection.execute(text("""UPDATE aist.knowledge_chunks SET indexed=true
                WHERE index_id=:index_id AND id=ANY(CAST(:ids AS uuid[]))"""),
                dict(index_id=INDEX_ID, ids=[fragment['id'] for fragment in batch]))
    return dict(version_id=version_id, chunks=len(fragments), index_id=INDEX_ID, corpus=metadata['corpus'])


def authorized_versions(service, connection, run, args):
    from .facts import pinned_version
    service.scoped(connection, run['owner_id'], run['project_id'])
    versions = {}
    for version_id in args['source_version_ids']:
        pinned_version(service, connection, run, version_id)
        version = service.scoped(connection, run['owner_id'], run['project_id'], 'source_versions', version_id)
        if version['processing_state'] != 'ready':
            raise ApiError('source_not_ready')
        metadata = material(connection, version_id) or dict(
            corpus='factual', company=None, period=None, provenance='Сохраненный оригинал')
        if args.get('corpus', 'factual') != metadata['corpus']:
            continue
        if any(args.get(field) is not None and args[field] != metadata[field] for field in ('company', 'period')):
            continue
        version.update(metadata)
        versions[version_id] = version
    return versions


def search_evidence(service, connection, run, args, settings=None, *, authorize_embedding=None):
    from .facts import lexical_search_evidence, pin_of
    checked('SearchEvidenceArgs', args)
    settings = settings or Settings()
    # Lexical tools reuse the caller transaction. Semantic tools and UI pass None:
    # their short authorization/read transactions finish before external I/O.
    with (nullcontext(connection) if connection is not None else service.engine.begin()) as database:
        versions = authorized_versions(service, database, run, args)
        method = args.get('method', 'lexical.v1')
        corpus = args.get('corpus', 'factual')
        result = dict(items=[], method=method, searched_source_version_ids=list(versions), hits=[], corpus=corpus,
                      index_id=INDEX_ID if method == 'semantic.qwen3.v1' else None,
                      qualification='Совпадение поиска не является проверенным фактом. Отсутствие ответа нельзя установить только по оценке близости.')
        if not versions:
            return checked('SearchEvidenceResult', result)
        if method == 'lexical.v1':
            legacy = lexical_search_evidence(service, database, run, dict(
                query=args['query'], limit=args['limit'], source_version_ids=list(versions)))
            result['items'] = legacy['items']
            for evidence in result['items']:
                version = versions[evidence['source']['source_version_id']]
                result['hits'].append(dict(evidence=evidence, title=version['original_filename'],
                    revision=version['revision'], corpus=corpus, char_start=None, char_end=None, score=None,
                    company=version['company'], period=version['period']))
            return checked('SearchEvidenceResult', result)
        counts = database.execute(text("""SELECT version_id,count(*) AS n,bool_and(indexed) AS ready
            FROM aist.knowledge_chunks WHERE index_id=:index_id AND version_id=ANY(:versions)
            GROUP BY version_id"""), dict(index_id=INDEX_ID, versions=list(versions))).mappings().all()
        if len(counts) != len(versions) or not all(count['ready'] for count in counts):
            raise ApiError('search_not_indexed')

    conditions = [dict(key='owner_id', match=dict(value=run['owner_id'])),
                  dict(key='project_id', match=dict(value=run['project_id'])),
                  dict(key='version_id', match=dict(any=list(versions))),
                  dict(key='corpus', match=dict(value=corpus)),
                  dict(key='index_id', match=dict(value=INDEX_ID))]
    for field in ('company', 'period'):
        if args.get(field) is not None:
            conditions.append(dict(key=field, match=dict(value=args[field])))
    scope_filter = dict(must=conditions)
    qdrant = Qdrant(settings)
    collection = qdrant.collection(corpus)
    count = qdrant.request('POST', collection + '/points/count', dict(filter=scope_filter, exact=True))
    if count.get('count') != sum(count['n'] for count in counts):
        raise ApiError('search_index_unavailable')
    vector = Embeddings(service.engine, settings).vectors(
        run['owner_id'], [input_text(args['query'], query=True)], 'query', authorize=authorize_embedding)[0]
    found = qdrant.request('POST', collection + '/points/query', dict(
        query=vector, filter=scope_filter, limit=args['limit'], with_payload=False, params=dict(exact=True)))
    points = found.get('points')
    if not isinstance(points, list) or len(points) > args['limit']:
        raise ApiError('search_index_unavailable')

    with (nullcontext(connection) if connection is not None else service.engine.begin()) as database:
        if set(authorized_versions(service, database, run, args)) != set(versions):
            raise ApiError('resource_not_found')
        for point in points:
            saved = row(database, """SELECT chunks.*,COALESCE(pages.text,sections.text) AS text FROM aist.knowledge_chunks chunks
                LEFT JOIN aist.source_pages pages ON pages.version_id=chunks.version_id AND pages.page=chunks.page
                LEFT JOIN aist.source_sections sections ON sections.version_id=chunks.version_id AND sections.section=chunks.section
                WHERE chunks.index_id=:index_id AND chunks.id=:id AND chunks.version_id=ANY(:versions)""",
                index_id=INDEX_ID, id=point['id'], versions=list(versions))
            score = point.get('score')
            if not saved or not isinstance(score, (float, int)) or not math.isfinite(score):
                raise ApiError('search_index_unavailable')
            version = versions[saved['version_id']]
            start, end = saved['char_start'], saved['char_end']
            quote = saved['text'][start:end]
            if digest(input_text(quote)) != saved['input_sha256']:
                raise ApiError('search_index_unavailable')
            if saved['section']:
                locator = dict(json_pointer=saved['section'], area='Фрагмент сохраненного JSON MOEX ISS')
            else:
                locator = dict(pdf_page=saved['page'], area='Фрагмент сохраненного текста страницы',
                               bbox_pt=None, coordinate_system='pdf_points_top_left')
            evidence = dict(evidence_id='ev_' + fingerprint(dict(index=INDEX_ID, id=str(saved['id'])))[:32],
                            source=pin_of(version), locator=locator, quote=quote)
            result['items'].append(evidence)
            result['hits'].append(dict(evidence=evidence, title=version['original_filename'], revision=version['revision'],
                corpus=corpus, char_start=start, char_end=end, score=score, company=version['company'], period=version['period']))
    return checked('SearchEvidenceResult', result)


def ui_search(service, settings, actor, project, args, *, authorize_embedding=None):
    from .facts import pin_of
    checked('SearchEvidenceArgs', args)
    with service.engine.begin() as connection:
        service.scoped(connection, actor, project)
        pins = [pin_of(service.scoped(connection, actor, project, 'source_versions', version_id))
                for version_id in args['source_version_ids']]
        run = dict(owner_id=actor, project_id=project, input=dict(source_versions=pins))
        if args.get('method', 'lexical.v1') == 'lexical.v1':
            return search_evidence(service, connection, run, args, settings)
    return search_evidence(service, None, run, args, settings, authorize_embedding=authorize_embedding)
