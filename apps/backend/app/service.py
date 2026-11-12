"""Предметные операции D06. Права, receipt и CAS проверяются при фиксации записи."""
from datetime import timezone
import hashlib
import json
import logging
from threading import BoundedSemaphore
import uuid
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .contracts import validate_definition
from .errors import ApiError, error_value
from .normalization import fingerprint
from .storage import process_pdf

log = logging.getLogger(__name__)
# Один предел для всех экземпляров Service в процессе API или worker.
PDF_PREPARATION_SLOTS = BoundedSemaphore(2)


def ident(prefix):
    return prefix + '_' + uuid.uuid4().hex


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def row(c, query, **params):
    result = c.execute(text(query), params).mappings().first()
    return dict(result) if result else None


class Service:
    def __init__(self, engine, storage):
        self.engine, self.storage = engine, storage

    def scoped(self, c, actor, project, table='projects', object_id=None, lock=False):
        # table поступает только из внутренних констант handlers.
        query = f'SELECT * FROM aist.{table} WHERE owner_id=:actor AND '
        query += 'id=:project' if table == 'projects' else 'project_id=:project AND id=:object_id'
        if table == 'conversations':
            query += ' AND hidden_at IS NULL'
        value = row(c, query + (' FOR UPDATE' if lock else ''), actor=actor, project=project, object_id=object_id)
        if value is None:
            raise ApiError('resource_not_found')
        return value

    def check_scope(self, actor, project, source=None):
        with self.engine.connect() as c:
            self.scoped(c, actor, project)
            if source:
                self.scoped(c, actor, project, 'sources', source)

    def check_version_scope(self, actor, project, version):
        with self.engine.connect() as c:
            self.scoped(c,actor,project)
            self.scoped(c,actor,project,'source_versions',version)

    def project_view(self, value):
        return validate_definition('Project', dict(id=value['id'], name=value['name'],
            created_by=value['owner_id'], created_at=timestamp(value['created_at'])))

    def source_view(self, value):
        return validate_definition('Source', {k:timestamp(value[k]) if k=='created_at' else value[k]
            for k in ('id','project_id','title','latest_version_id','revision','created_at')})

    def conversation_view(self, value):
        return validate_definition('Conversation', {k:timestamp(value[k]) if k=='created_at' else value[k]
            for k in ('id','project_id','title','revision','created_at')})

    def inspect(self, c, value):
        if value.get('media_type') == 'application/json':
            from .moex import snapshot_data
            data, problem = snapshot_data(c, value)
        else:
            data, problem = self.storage.read(value)
        self.record_file_state(c, value, problem)
        return data, problem

    def record_file_state(self, c, value, problem):
        file_errors = {'export_missing', 'export_corrupt'}
        if problem:
            state, error = 'failed', error_value(problem)
        elif (value.get('media_type') != 'application/json'
              and (value.get('error') or {}).get('code') in file_errors):
            # Only a file with the original size and SHA can restore this exact PDF version.
            state, error = 'ready', None
        else:
            return
        if (state, error) == (value['processing_state'], value['error']):
            return
        if problem != 'temporarily_unavailable':
            c.execute(text("""UPDATE aist.source_versions SET processing_state=:state, error=CAST(:error AS jsonb)
                WHERE id=:id AND project_id=:project AND owner_id=:owner AND sha256=:sha
                AND processing_state=:previous AND error IS NOT DISTINCT FROM CAST(:previous_error AS jsonb)"""),
                dict(id=value['id'], project=value['project_id'], owner=value['owner_id'], sha=value['sha256'],
                     state=state, error=json.dumps(error) if error is not None else None,
                     previous=value['processing_state'],
                     previous_error=json.dumps(value['error']) if value['error'] is not None else None))
        value.update(processing_state=state, error=error)

    def version_view(self, c, value):
        self.inspect(c, value)
        return self.version_metadata(c, value)

    def version_metadata(self, c, value):
        result = {k:timestamp(value[k]) if k=='created_at' else value[k] for k in (
            'id','source_id','project_id','revision','sha256','byte_size','original_filename',
            'processing_state','processor_version','page_count','error','created_at')}
        from .search import material
        result['corpus'] = (material(c,value['id']) or {}).get('corpus','factual')
        result['media_type'] = value.get('media_type', 'application/pdf')
        return validate_definition('SourceVersion', result)

    def receipt_view(self, c, receipt):
        if receipt['operation'] == 'createProject':
            return self.project_view(self.scoped(c, receipt['actor_id'], receipt['project_id']))
        if receipt['operation'] == 'createConversation':
            return self.conversation_view(self.scoped(c, receipt['actor_id'], receipt['project_id'],
                'conversations', receipt['conversation_id']))
        return self.upload_receipt_view(c, receipt)[0]

    def upload_receipt_view(self, c, receipt):
        actor, project = receipt['actor_id'], receipt['project_id']
        source = self.scoped(c, actor, project, 'sources', receipt['source_id'])
        version = self.scoped(c, actor, project, 'source_versions', receipt['version_id'])
        result = validate_definition('UploadReceipt', {
            'source': self.source_view(source), 'version': self.version_metadata(c, version)})
        return result, version

    def replay(self, receipt):
        if receipt['version_id'] is None:
            with self.engine.connect() as c:
                return 200, self.receipt_view(c, receipt)
        with self.engine.connect() as c:
            result, version = self.upload_receipt_view(c, receipt)
        _, problem = self.storage.read(version)
        if problem or (version.get('error') or {}).get('code') in {'export_missing', 'export_corrupt'}:
            with self.engine.begin() as c:
                self.record_file_state(c, version, problem)
            result['version'].update(processing_state=version['processing_state'], error=version['error'])
        return 200, result

    def receipt(self, c, scope):
        return row(c, '''SELECT * FROM aist.receipts WHERE actor_id=:actor AND project_scope=:project_scope
            AND operation=:operation AND target=:target AND key=:key''', **scope)

    def write_receipt(self, c, scope, sha, project, source=None, version=None, conversation=None, *, proposal=None):
        c.execute(text('''INSERT INTO aist.receipts(actor_id,project_scope,operation,target,key,fingerprint,
            project_id,source_id,version_id,conversation_id,proposal_id)
            VALUES (:actor,:project_scope,:operation,:target,:key,:sha,:project,:source,:version,:conversation,:proposal)'''),
            {**scope,'sha':sha,'project':project,'source':source,'version':version,
             'conversation':conversation,'proposal':proposal})

    def matching_receipt(self, c, scope, sha):
        old = self.receipt(c, scope)
        if old and old['fingerprint'] != sha:
            raise ApiError('idempotency_conflict')
        return old

    def write_scope(self, c, actor, project, source, lock=False):
        if project:
            self.scoped(c, actor, project, lock=lock)
        if source:
            return self.scoped(c, actor, project, 'sources', source, lock=lock)
        return None

    def upload_revision(self, c, existing, expected):
        if existing and row(c, 'SELECT source_id FROM aist.moex_sources WHERE source_id=:id', id=existing['id']):
            raise ApiError('unsupported_source')
        current = existing['revision'] if existing else 0
        if expected != current:
            raise ApiError('revision_conflict', expected, current)
        return current

    def prepare_upload(self, data, file_sha, filename):
        if not PDF_PREPARATION_SLOTS.acquire(blocking=False):
            raise ApiError('temporarily_unavailable')
        try:
            parsed = process_pdf(data)
            version_id = ident('sv')
            texts = parsed['pages']
            layouts = parsed.get('layouts', [None] * len(texts))
            diagrams = parsed.get('diagrams', [[]] * len(texts))
            pages = [
                dict(version=version_id, sha=file_sha, page=i, text=value,
                     layout=layout, diagrams=json.dumps(regions))
                for i, (value, layout, regions) in enumerate(zip(texts, layouts, diagrams, strict=True), 1)
            ]
            version = dict(id=version_id, sha=file_sha, size=len(data), filename=filename,
                           storage=version_id + '.pdf', processor=parsed['processor_version'], count=len(pages))
            published = (*self.storage.publish(version_id, data), data)
            return version, pages, published
        finally:
            PDF_PREPARATION_SLOTS.release()

    def save_upload(self, c, scope, sha, body, current, version, pages):
        actor, project, source = scope['actor'], scope['project_scope'], scope['target']
        if not source:
            source = ident('src')
            c.execute(text('''INSERT INTO aist.sources(id,project_id,owner_id,title,latest_version_id,revision)
                VALUES (:id,:project,:actor,:title,:version,1)'''),
                dict(id=source, project=project, actor=actor, title=body['title'], version=version['id']))
        c.execute(text('''INSERT INTO aist.source_versions(id,source_id,project_id,owner_id,revision,sha256,
            byte_size,original_filename,storage_name,processor_version,page_count,processing_state)
            VALUES (:id,:source,:project,:actor,:revision,:sha,:size,:filename,:storage,:processor,:count,'ready')'''),
            dict(version, source=source, project=project, actor=actor, revision=current + 1))
        c.execute(text('''INSERT INTO aist.source_pages(version_id,sha256,page,text,layout_text,diagram_regions)
            VALUES (:version,:sha,:page,:text,:layout,CAST(:diagrams AS jsonb))'''), pages)
        if scope['operation'] == 'uploadSourceVersion':
            from .search import inherit_sample
            inherit_sample(c, version['id'])
            updated = c.execute(text('''UPDATE aist.sources SET latest_version_id=:version, revision=:revision
                WHERE id=:source AND project_id=:project AND owner_id=:actor AND revision=:current'''),
                dict(version=version['id'], revision=current + 1, source=source, project=project,
                     actor=actor, current=current))
            if updated.rowcount != 1:
                raise ApiError('temporarily_unavailable')
        self.write_receipt(c, scope, sha, project, source, version['id'])

    def write(self, actor, operation, key, body, project=None, source=None, data=None, filename=None):
        scope = dict(actor=actor, project_scope=project or '', operation=operation, target=source or '', key=key)
        file_sha = hashlib.sha256(data).hexdigest() if data is not None else None
        sha = fingerprint(body if data is None else {'metadata': body, 'file_sha256': file_sha, 'byte_size': len(data)})
        lock_id = int.from_bytes(hashlib.sha256(fingerprint(scope).encode()).digest()[:8], 'big', signed=True)
        published = None
        if operation in ('uploadSource', 'uploadSourceVersion'):
            with self.engine.connect() as c:
                existing = self.write_scope(c, actor, project, source)
                old = self.matching_receipt(c, scope, sha)
                if not old:
                    self.upload_revision(c, existing, body['expected_revision'])
            if old:
                return self.replay(old)
            version, pages, published = self.prepare_upload(data, file_sha, filename)

        committing = False
        try:
            with self.engine.connect() as c, c.begin() as transaction:
                c.execute(text("SET LOCAL statement_timeout='30000'"))
                existing = self.write_scope(c, actor, project, source, lock=True)
                c.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': lock_id})
                old = self.matching_receipt(c, scope, sha)
                if not old:
                    if operation == 'createProject':
                        project = ident('prj')
                        c.execute(text('INSERT INTO aist.projects(id,owner_id,name) VALUES (:id,:actor,:name)'),
                            dict(id=project, actor=actor, name=body['name']))
                        self.write_receipt(c, scope, sha, project)
                    elif operation == 'createConversation':
                        conversation = ident('conv')
                        c.execute(text("""INSERT INTO aist.conversations(id,project_id,owner_id,title,folder_project_id)
                            SELECT :id,id,:actor,:title,CASE WHEN personal THEN NULL ELSE id END
                            FROM aist.projects WHERE id=:project AND owner_id=:actor"""),
                            dict(id=conversation, project=project, actor=actor, title=body['title']))
                        self.write_receipt(c, scope, sha, project, conversation=conversation)
                    else:
                        current = self.upload_revision(c, existing, body['expected_revision'])
                        self.save_upload(c, scope, sha, body, current, version, pages)
                    result = self.receipt_view(c, self.receipt(c, scope))
                    if published:
                        self.storage.verify_candidate(*published[:2])
                    committing = True
                    try:
                        transaction.commit()
                    except BaseException:
                        c.invalidate()
                        raise
                    return 201, result
                transaction.rollback()
        except BaseException:
            if not committing:
                raise
            return self.recover_write(scope, sha, lock_id, published)
        finally:
            if not committing:
                self.cleanup(published)
        return self.replay(old)

    def recover_write(self, scope, sha, lock_id, published):
        # Ошибка commit оставляет исход открытым до сверки на другом соединении.
        try:
            with self.engine.connect() as fresh:
                fresh.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': lock_id})
                old = self.receipt(fresh, scope)
                kept = published and row(fresh,
                    'SELECT id FROM aist.source_versions WHERE storage_name=:name', name=published[0].name)
            if published and not kept:
                self.cleanup(published)
            if old and old['fingerprint'] == sha:
                return self.replay(old)
        except Exception:
            raise ApiError('outcome_unknown') from None
        if kept:
            raise ApiError('outcome_unknown')
        if old:
            raise ApiError('idempotency_conflict')
        raise ApiError('temporarily_unavailable')

    def cleanup(self, published):
        if published and not self.storage.remove_owned(*published):
            log.error('Owned upload cleanup failed; reconciliation required')

    def get(self, actor, project, table='projects', object_id=None):
        with self.engine.begin() as c:
            self.scoped(c,actor,project)
            value = self.scoped(c,actor,project,table,object_id)
            if table=='source_versions':
                return self.version_view(c,value)
            return {'projects':self.project_view,'sources':self.source_view,
                    'conversations':self.conversation_view}[table](value)

    def listing(self, actor, table, query, project=None):
        with self.engine.connect() as c:
            if project:
                self.scoped(c,actor,project)
            scope = 'owner_id=:actor' + (' AND project_id=:project' if project else '')
            if table == 'projects':
                scope += ' AND NOT personal'
            elif table == 'conversations':
                scope += ' AND hidden_at IS NULL'
            params = dict(actor=actor,project=project,limit=query.get('limit',50)+1)
            if 'after_id' in query:
                cursor = row(c,f'SELECT created_at,id FROM aist.{table} WHERE {scope} AND id=:cursor',
                    **params,cursor=query['after_id'])
                if cursor is None:
                    raise ApiError('cursor_invalid')
                scope += ' AND (created_at,id)>(:created_at,:cursor)'
                params.update(created_at=cursor['created_at'],cursor=cursor['id'])
            rows = list(c.execute(text(f'SELECT * FROM aist.{table} WHERE {scope} ORDER BY created_at,id LIMIT :limit'),params).mappings())
        more = len(rows)>params['limit']-1
        rows = rows[:params['limit']-1]
        view = {'projects':self.project_view,'sources':self.source_view,'conversations':self.conversation_view}[table]
        return validate_definition({'projects':'ProjectPage','sources':'SourcePage','conversations':'ConversationPage'}[table],
            {'items':[view(v) for v in rows],'next_after_id':rows[-1]['id'] if more else None})

    def page_or_file(self, actor, project, version_id, page=None):
        problem = None
        with self.engine.begin() as c:
            self.scoped(c,actor,project)
            version = self.scoped(c,actor,project,'source_versions',version_id)
            data, problem = self.inspect(c,version)
            if page is not None:
                if problem or version['processing_state']!='ready':
                    problem = 'source_not_ready'
                else:
                    content = row(c,'SELECT text,diagram_regions FROM aist.source_pages WHERE version_id=:id AND page=:page',id=version_id,page=page)
                    if not content:
                        problem = 'resource_not_found'
                    else:
                        return validate_definition('PageContent', {'source':dict(source_id=version['source_id'],
                            source_version_id=version_id,sha256=version['sha256']), 'page':page,'text':content['text'],'evidence':[],
                            **({'diagram_regions':content['diagram_regions']} if content['diagram_regions'] else {})})
        # Ошибка не откатывает диагностический failed статус.
        if problem:
            raise ApiError(problem)
        return data, version

    def reconcile_files(self):
        # Перезапуск проверяет ready версии, но не запускает парсер и не удаляет orphan-файлы.
        try:
            with self.engine.begin() as c:
                if not c.execute(text("SELECT to_regclass('aist.source_versions')")).scalar():
                    return
                for value in c.execute(text("""SELECT * FROM aist.source_versions WHERE processing_state='ready' OR
                        error->>'code' IN ('export_missing','export_corrupt')""")).mappings().all():
                    self.inspect(c,dict(value))
        except SQLAlchemyError:
            pass  # readiness сообщает о недоступной БД или непримененной миграции.
