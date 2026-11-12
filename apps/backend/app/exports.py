"""Выпуск сохраненной версии, без модели и без скрытой повторной сборки."""
import errno
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
from subprocess import TimeoutExpired
from sqlalchemy.exc import SQLAlchemyError
from .errors import ApiError, error_value
from .normalization import checked, fingerprint
from .service import ident, row
from .runs import execute, json_value, now
from .versions import view
from .note_renderer import VALIDATOR, render, renderer_versions

log=logging.getLogger(__name__)
MEDIA={'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','pdf':'application/pdf'}
MAX_EXPORT=64*1024*1024


class ExportFiles:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.root.resolve()!=self.root.absolute(): raise ValueError('Export root must not contain symlinks')

    def path(self, e, stage=False):
        if not re.fullmatch(r'exp_[a-f0-9]{32}',e['id']) or e['format'] not in MEDIA: raise ValueError('Invalid export path')
        return self.root/('.'+e['id'] if stage else e['id']+'.'+e['format'])

    def stage(self,e):
        path=self.path(e,True);path.mkdir(mode=0o700)
        return path

    def read(self,e,*,stage=False):
        path = self.path(e)
        if stage:
            directory = self.path(e, True)
            if directory.is_symlink():
                return None, 'export_corrupt'
            path = directory / ('note.' + e['format'])
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): return None,'export_corrupt'
                data=f.read(MAX_EXPORT+1)
        except FileNotFoundError:return None,'export_missing'
        except OSError as error:
            return None, 'export_corrupt' if error.errno == errno.ELOOP else 'temporarily_unavailable'
        if len(data)!=e['file']['byte_size'] or hashlib.sha256(data).hexdigest()!=e['file']['sha256']:return None,'export_corrupt'
        return data,None

    def publish(self,e):
        source=self.path(e,True)/('note.'+e['format'])
        if source.is_symlink() or source.parent.is_symlink():raise ValueError('Invalid staged file')
        with source.open('rb') as f:os.fsync(f.fileno())
        # Hard link provides atomic publication without replacing any existing file.
        os.link(source,self.path(e),follow_symlinks=False)
        directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)

    def inventory(self,e):
        directory=self.path(e,True)
        if directory.is_symlink():raise OSError('Unsafe stage')
        result=[]
        for name in ('note.docx','note.pdf'):
            p=directory/name
            if p.exists():
                s=p.lstat()
                if not stat.S_ISREG(s.st_mode):raise OSError('Unsafe staged file')
                result.append((p,s,hashlib.sha256(p.read_bytes()).hexdigest()))
        return result

    def cleanup(self,e,inventory):
        # R2 principle: caller has confirmed failed; only this export's unpublished bytes.
        for p,original,sha in inventory:
            if p.parent!=self.path(e,True) or p.parent.is_symlink() or p.name not in ('note.docx','note.pdf'):raise OSError('Foreign cleanup path')
            current=p.lstat()
            if not stat.S_ISREG(current.st_mode) or (original.st_dev,original.st_ino)!=(current.st_dev,current.st_ino):raise OSError('Changed cleanup file')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise OSError('Changed cleanup bytes')
            p.unlink()


class Exports:
    def __init__(self,runs):
        self.runs,self.service,self.engine=runs,runs.service,runs.engine
        self.files=ExportFiles(runs.settings.export_root)

    def selected(self,c,actor,project,artifact,version):
        self.service.scoped(c,actor,project,'artifacts',artifact)
        saved=self.service.scoped(c,actor,project,'artifact_versions',version)
        if saved['artifact_id']!=artifact:raise ApiError('resource_not_found')
        return view(c,saved)

    def acceptance(self, selected):
        review, version = selected['review'], selected['version']
        accepted = review['acceptance']
        if (not accepted or accepted['artifact_version_id'] != version['id']
                or accepted['content_sha256'] != version['content_sha256']
                or accepted['validation_ids'] != review['validation_ids']):
            raise ApiError('acceptance_required')
        if any(x['status'] == 'failed' or any(f['severity'] == 'error' for f in x['findings'])
               for x in selected['checks']):
            raise ApiError('validation_failed')
        if (any(x['kind'] == 'semantic' and x['status'] == 'not_checked' for x in selected['checks'])
                and not accepted['acknowledge_unchecked_semantics']):
            raise ApiError('acceptance_required')
        return accepted['id']

    def save(self,c,e,**changes):
        e.update(changes,revision=e['revision']+1,updated_at=now())
        checked('Export',e)
        execute(c,'UPDATE aist.exports SET data=CAST(:d AS jsonb) WHERE id=:id',id=e['id'],d=json_value(e))

    def inspect(self,c,e):
        data=None
        if e['state']=='ready':
            data,problem=self.files.read(e)
            if problem == 'temporarily_unavailable':
                raise ApiError(problem)
            if problem:
                self.save(c,e,state='missing' if problem=='export_missing' else 'corrupt',error=error_value(problem))
        return data

    def request(self,actor,session,project,artifact,version,key,body):
        checked('CreateExport',body)
        scope=dict(actor=actor,project_scope=project,operation='requestExport',target=version,key=key)
        sha=fingerprint(body);committing=False;c=self.engine.connect();tx=c.begin()
        try:
            self.service.scoped(c,actor,project,lock=True)
            self.runs.session_alive(c,dict(session_id=session,owner_id=actor))
            selected=self.selected(c,actor,project,artifact,version)
            old=self.service.receipt(c,scope)
            if old:
                if old['fingerprint']!=sha:raise ApiError('idempotency_conflict')
                e=self.service.scoped(c,actor,project,'exports',old['export_id'],lock=True)['data']
                self.inspect(c,e);committing=True;tx.commit()
                if e['state']=='recovery_required':raise ApiError('outcome_unknown')
                return 200,e
            review=selected['review'];v=selected['version']
            if body['expected_revision']!=review['revision']:raise ApiError('revision_conflict',body['expected_revision'],review['revision'])
            if v['payload']['result_type']!='analytical_note':raise ApiError('unsupported_result_type')
            if body['format'] not in MEDIA:raise ApiError('unsupported_format')
            if body['renderer_version'] != renderer_versions(body['template_version']).get(body['format']):raise ApiError('incompatible_template')
            if fingerprint({k:v[k] for k in ('payload','basis')})!=v['content_sha256']:raise ApiError('validation_failed')
            acceptance=None
            if body['settings']['release_mode']=='accepted':
                acceptance=self.acceptance(selected)
            identity=dict(project_id=project,artifact_version_id=version,content_sha256=v['content_sha256'],acceptance_id=acceptance,
                          **{k:body[k] for k in ('format','template_version','renderer_version','settings')})
            digest=fingerprint(identity)
            existing=row(c,'SELECT data FROM aist.exports WHERE project_id=:p AND identity_sha256=:sha FOR UPDATE',p=project,sha=digest)
            if existing:
                e=existing['data'];status=200;self.inspect(c,e)
            else:
                e=checked('Export',dict(**identity,id=ident('exp'),identity_sha256=digest,state='queued',revision=0,file=None,
                    release_check=dict(status='pending',validator_version=VALIDATOR,checked_at=None,issues=[]),error=None,created_at=now(),updated_at=now()))
                execute(c,'''INSERT INTO aist.exports(id,project_id,owner_id,version_id,identity_sha256,data)
                  VALUES (:id,:p,:a,:v,:sha,CAST(:d AS jsonb))''',id=e['id'],p=project,a=actor,v=version,sha=digest,d=json_value(e));status=202
            execute(c,'''INSERT INTO aist.receipts(actor_id,project_scope,operation,target,key,fingerprint,project_id,export_id)
                VALUES (:actor,:project_scope,:operation,:target,:key,:sha,:project_scope,:e)''',**scope,sha=sha,e=e['id'])
            committing=True;tx.commit()
            if e['state']=='recovery_required':raise ApiError('outcome_unknown')
            return status,e
        except ApiError:raise
        except BaseException:
            if not committing:raise
            # Receipt lookup on the next exact POST resolves an unknown commit; never create a second identity.
            raise ApiError('outcome_unknown') from None
        finally:
            if tx.is_active:tx.rollback()
            c.close()

    def get(self,actor,project,export,download=False):
        with self.engine.begin() as c:
            self.service.scoped(c,actor,project)
            e=self.service.scoped(c,actor,project,'exports',export,lock=True)['data']
            data=self.inspect(c,e)
        if download:
            problem={'missing':'export_missing','corrupt':'export_corrupt','recovery_required':'outcome_unknown'}.get(e['state'])
            if problem:raise ApiError(problem)
            if e['state']!='ready':raise ApiError('export_not_ready')
            return data,e
        return checked('Export',e)

    def listing(self,actor,project,artifact,version,q):
        with self.engine.begin() as c:
            self.service.scoped(c,actor,project);self.selected(c,actor,project,artifact,version)
            limit=q.get('limit',50)
            params=dict(p=project,v=version,lim=limit+1);extra=''
            if q.get('after_id'):
                cursor=row(c,'''SELECT id,CAST(data->>'created_at' AS timestamptz) AS created_at
                    FROM aist.exports WHERE id=:id AND version_id=:v AND project_id=:p''',
                    id=q['after_id'],v=version,p=project)
                if not cursor:raise ApiError('cursor_invalid')
                extra=" AND (CAST(data->>'created_at' AS timestamptz),id)>(:created_at,:after)"
                params.update(created_at=cursor['created_at'],after=cursor['id'])
            items=[x[0] for x in execute(c,"SELECT data FROM aist.exports WHERE project_id=:p AND version_id=:v"+extra+
                " ORDER BY CAST(data->>'created_at' AS timestamptz),id LIMIT :lim FOR UPDATE",**params)]
            for e in items[:limit]:self.inspect(c,e)
        return checked('ExportPage',dict(items=items[:limit],next_after_id=items[limit-1]['id'] if len(items)>limit else None))

    def lock(self,c,eid):
        lock=int.from_bytes(hashlib.sha256(eid.encode()).digest()[:8],'big',signed=True)
        return lock,bool(execute(c,'SELECT pg_try_advisory_lock(:id)',id=lock).scalar())

    def process_one(self):
        # Same worker, one bounded rendering job. Session lock survives commits and fences competing workers.
        with self.engine.connect() as c:
            pending=[r[0] for r in execute(c,"SELECT id FROM aist.exports WHERE data->>'state' IN ('queued','rendering') ORDER BY data->>'created_at'")];c.commit()
            for eid in pending:
                lock,owned=self.lock(c,eid);c.commit()
                if not owned:continue
                try:
                    e=row(c,'SELECT data FROM aist.exports WHERE id=:id',id=eid)['data'];c.commit()
                    if e['state']=='rendering':
                        self.save(c,e,state='recovery_required',error=error_value('outcome_unknown'));c.commit();return
                    if e['state']!='queued':continue
                    self.save(c,e,state='rendering');execute(c,'UPDATE aist.exports SET render_count=render_count+1 WHERE id=:id',id=eid);c.commit()
                    self.produce(c,e);return
                finally:
                    if c.in_transaction():c.rollback()
                    try:execute(c,'SELECT pg_advisory_unlock(:id)',id=lock);c.commit()
                    except SQLAlchemyError:pass

    def produce(self,c,e):
        prepared=False
        directory=None
        pid=execute(c,'SELECT pg_backend_pid()').scalar();c.commit()
        try:
            if e['renderer_version'] != renderer_versions(e['template_version']).get(e['format']):raise ValueError('Pinned renderer is unavailable')
            version, checks = self.render_input(c, e)
            c.commit()
            directory=self.files.stage(e)
            render(version,e['settings'],checks,directory,e['template_version'])
            data=(directory/('note.'+e['format'])).read_bytes()
            if not 0<len(data)<=MAX_EXPORT:raise ValueError('Export size invalid')
            descriptor=dict(sha256=hashlib.sha256(data).hexdigest(),byte_size=len(data),media_type=MEDIA[e['format']],filename=f"aist-note-v{version['revision']}-"+e['id']+'.'+e['format'])
            # Save the checked candidate before publication. Any uncertain boundary keeps bytes for reconciliation.
            prepared=True
            if execute(c,'SELECT pg_backend_pid()').scalar()!=pid:raise RuntimeError('Worker connection changed')
            self.save(c,e,state='recovery_required',file=descriptor,release_check=dict(status='passed',validator_version=VALIDATOR,checked_at=now(),issues=[]),error=error_value('outcome_unknown'));c.commit()
            self.files.publish(e)
            _,problem=self.files.read(e)
            if problem:raise ValueError('Published bytes mismatch')
            self.save(c,e,state='ready',error=None);c.commit()
        except Exception as error:
            if c.in_transaction():c.rollback()
            if prepared:
                log.warning('Export publication needs reconciliation: %s',e['id']);return
            # Confirm failure on a fresh durable read before removing any own unpublished file.
            try:
                if execute(c,'SELECT pg_backend_pid()').scalar()!=pid:c.rollback();return
                e=row(c,'SELECT data FROM aist.exports WHERE id=:id FOR UPDATE',id=e['id'])['data']
                if e['state']!='rendering':c.rollback();return
                failure = error_value('temporarily_unavailable' if isinstance(error, TimeoutExpired) else 'validation_failed')
                if isinstance(error, TimeoutExpired):
                    failure['message'] = 'Рендер не завершился вовремя. После устранения причины оператор может повторить выпуск.'
                self.save(c,e,state='failed',error=failure,release_check=dict(status='failed',validator_version=VALIDATOR,checked_at=now(),issues=['Renderer: '+type(error).__name__]))
                c.commit()
            except Exception:
                log.warning('Export failure outcome unknown: %s',e['id']);return
            if directory is None:
                return
            try:
                self.files.cleanup(e,self.files.inventory(e));cleanup={'state':'removed'}
            except Exception as cleanup_error:cleanup={'state':'error','type':type(cleanup_error).__name__}
            execute(c,'UPDATE aist.exports SET cleanup=CAST(:d AS jsonb) WHERE id=:id',id=e['id'],d=json_value(cleanup));c.commit()

    def render_input(self, c, export):
        saved = row(c, """SELECT * FROM aist.artifact_versions
            WHERE id=:id AND project_id=:project
              AND owner_id=(SELECT owner_id FROM aist.exports WHERE id=:export)""",
                    id=export['artifact_version_id'], project=export['project_id'], export=export['id'])
        if saved is None:
            raise ApiError('resource_not_found')
        selected = view(c, saved)
        version = saved['data']
        content = {key: version[key] for key in ('payload', 'basis')}
        if fingerprint(content) != export['content_sha256'] or version['content_sha256'] != export['content_sha256']:
            raise ApiError('validation_failed')
        if export['settings']['release_mode'] == 'accepted' and self.acceptance(selected) != export['acceptance_id']:
            raise ApiError('acceptance_required')
        return version, selected['checks']

    def retry(self, eid, failed_revision):
        # The failure revision is the operator's idempotency token. An acknowledgement
        # can be lost even if a worker has already taken or finished the new attempt.
        with self.engine.connect() as c:
            lock, owned = self.lock(c, eid)
            c.commit()
            if not owned:
                raise ApiError('outcome_unknown')
            try:
                found = row(c, 'SELECT data, retry_history FROM aist.exports WHERE id=:id FOR UPDATE', id=eid)
                if not found:
                    raise ApiError('resource_not_found')
                e = found['data']
                if any(retry['revision'] == failed_revision for retry in found['retry_history']):
                    return e
                if e['revision'] != failed_revision:
                    raise ApiError('revision_conflict', failed_revision, e['revision'])
                if (e['state'] != 'failed' or e['file'] is not None
                        or (e['error'] or {}).get('code') not in ('temporarily_unavailable', 'validation_failed')
                        or e['release_check']['issues'] != ['Renderer: TimeoutExpired']):
                    raise ApiError('export_not_ready')
                self.render_input(c, e)
                if e['renderer_version'] != renderer_versions(e['template_version']).get(e['format']):
                    raise ApiError('incompatible_template')
                # Never discard unknown bytes. Only the renderer's cleanup may
                # remove its own unchecked files; an empty stage can be recreated.
                try:
                    self.files.path(e).lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise ApiError('outcome_unknown')
                try:
                    self.files.path(e, True).rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise ApiError('outcome_unknown') from None
                self.save(c, e, state='queued', error=None, release_check=dict(
                    status='pending', validator_version=VALIDATOR, checked_at=None, issues=[]))
                c.commit()
                return e
            finally:
                if c.in_transaction():
                    c.rollback()
                execute(c, 'SELECT pg_advisory_unlock(:id)', id=lock)
                c.commit()

    def reconcile(self,eid):
        # Recover the checked bytes under the same advisory lock as rendering.
        with self.engine.connect() as c:
            lock,owned=self.lock(c,eid);c.commit()
            if not owned:raise ApiError('outcome_unknown')
            try:
                found=row(c,'SELECT * FROM aist.exports WHERE id=:id FOR UPDATE',id=eid)
                if not found:raise ApiError('resource_not_found')
                e=found['data']
                if e['state']=='recovery_required' and e['file'] and e['release_check']['status']=='passed':
                    saved = self.service.scoped(c, found['owner_id'], found['project_id'],
                                                'artifact_versions', found['version_id'])
                    content = {key: saved['data'][key] for key in ('payload', 'basis')}
                    if fingerprint(content) != e['content_sha256']:
                        raise ApiError('validation_failed')
                    _, problem = self.files.read(e)
                    if problem == 'export_missing':
                        _, staged_problem = self.files.read(e, stage=True)
                        if staged_problem is None:
                            self.files.publish(e)
                            _, problem = self.files.read(e)
                    if problem is None:
                        self.save(c,e,state='ready',error=None)
                c.commit();return e
            finally:
                if c.in_transaction():c.rollback()
                execute(c,'SELECT pg_advisory_unlock(:id)',id=lock);c.commit()
