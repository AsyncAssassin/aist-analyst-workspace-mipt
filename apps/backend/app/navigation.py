"""Owner-scoped folders. PDF, versions and runs keep their original project."""
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from .api import json_body, key, query, service
from .errors import ApiError
from .normalization import checked, fingerprint
from .service import ident, row, timestamp

router = APIRouter(prefix='/v1/workspace')


def enabled(actor):
    return actor in os.getenv('AIST_CHAT_UI_OWNERS', '').split(',')


def require_navigation(request):
    actor = request.state.actor
    if not enabled(actor):
        raise ApiError('resource_not_found')
    return actor


def visible_folder(c, actor, project):
    folder = row(c, 'SELECT id,name,pinned FROM aist.projects WHERE id=:id AND owner_id=:actor AND NOT personal',
                 id=project, actor=actor)
    if folder is None:
        raise ApiError('resource_not_found')
    return folder


def chat_view(chat):
    return {k: chat[k] for k in ('id', 'title', 'project_id', 'folder_project_id')} | {
        'last_sent_at': timestamp(chat['last_sent_at'])}


def chat_views(c, actor, chats):
    """Present the company in place of an automatic first-message title throughout navigation."""
    items = [chat_view(chat) for chat in chats]
    if not items:
        return items
    by_id = {}
    for chat, item in zip(chats, items):
        if chat.get('title_override') is not None:
            item['title'] = chat['title_override']
        else:
            by_id[item['id']] = item
    if not by_id:
        return items
    records = c.execute(text("""
        SELECT m.conversation_id, m.text, s.original_filename,
               r.news_run->>'company' AS company,
               v.data#>>'{payload,content,title}' AS note_title
          FROM aist.messages m
          JOIN aist.runs r ON r.id=m.run_id AND r.owner_id=m.owner_id
          LEFT JOIN aist.artifact_versions v ON v.id=r.result_version_id AND v.owner_id=m.owner_id
          LEFT JOIN aist.source_versions s ON s.id=r.input#>>'{source_versions,0,source_version_id}'
                   AND s.owner_id=m.owner_id AND jsonb_array_length(r.input->'source_versions')=1
         WHERE m.owner_id=:actor AND m.conversation_id=ANY(:ids) AND m.sequence=1 AND m.role='user'
           AND r.input->>'requested_result_type'='analytical_note'
    """), {'actor': actor, 'ids': list(by_id)}).mappings()
    for record in records:
        item = by_id[record['conversation_id']]
        filename = record['original_filename'] or ''
        first_line = record['text'].split('\n', 1)[0][:300]
        file_only = record['text'] == 'Подготовь аналитическую записку.' and item['title'] == filename[:300]
        if item['title'] != first_line and not file_only:
            continue
        company = record['company']
        if not company and record['note_title']:
            subject, separator, _ = record['note_title'].partition(':')
            if separator:
                company = subject.strip()
        if company:
            item['title'] = ('Записка: ' + company)[:300]
        elif filename:
            item['title'] = ('Записка по файлу ' + filename)[:300]
        else:
            item['title'] = 'Аналитическая записка'
    return items


@router.get('')
def identity(request: Request):
    actor = request.state.actor
    with service(request).engine.connect() as c:
        personal = row(c, 'SELECT id FROM aist.projects WHERE owner_id=:actor AND personal', actor=actor)
    return checked('WorkspaceIdentity', dict(owner_id=actor, enabled=enabled(actor),
                                             personal_id=personal['id'] if personal else None))


@router.get('/projects')
def folders(request: Request):
    actor = require_navigation(request)
    search = query(request, 'FolderQuery')
    params = dict(actor=actor, search=search.get('search', ''), limit=search.get('limit', 50)+1)
    predicate = "owner_id=:actor AND NOT personal AND strpos(lower(name), lower(:search))>0"
    with service(request).engine.connect() as c:
        if cursor := search.get('after_id'):
            anchor = row(c, 'SELECT pinned, name FROM aist.projects WHERE '+predicate+' AND id=:id', **params,id=cursor)
            if anchor is None:
                raise ApiError('cursor_invalid')
            predicate += ' AND (NOT pinned,lower(name),id)>(NOT :pinned,lower(:name),:cursor)'
            params.update(anchor, cursor=cursor)
        items = c.execute(text("""
            SELECT p.id, p.name, p.pinned, (
                SELECT count(*) FROM aist.conversations c
                WHERE c.owner_id = :actor AND c.folder_project_id = p.id AND c.hidden_at IS NULL
            ) AS chat_count
            FROM aist.projects p WHERE """ + predicate +
            ' ORDER BY pinned DESC,lower(name),id LIMIT :limit'), params).mappings().all()
    limit = params['limit']-1
    return checked('FolderPage', dict(items=[dict(f) for f in items[:limit]],
                                      next_after_id=items[limit-1]['id'] if len(items)>limit else None))


@router.get('/projects/{project_id}/recent-pdfs')
def recent_pdfs(project_id: str, request: Request):
    actor = require_navigation(request)
    if request.query_params:
        raise ApiError('validation_error')
    svc = service(request)
    with svc.engine.connect() as c:
        svc.scoped(c, actor, project_id)
        # An earlier sample restricts all its later versions, including old unclassified uploads.
        versions = c.execute(text("""SELECT v.id, v.original_filename, v.revision, v.page_count
            FROM aist.source_versions v
            WHERE v.owner_id=:actor AND v.project_id=:project
              AND v.media_type='application/pdf' AND v.processing_state='ready'
              AND NOT EXISTS (SELECT 1 FROM aist.sample_source_versions sample WHERE sample.id=v.id)
            ORDER BY v.created_at DESC, v.id DESC LIMIT 20"""),
            dict(actor=actor, project=project_id)).mappings().all()
    return checked('RecentPdfPage', {'items': [dict(version) for version in versions]})


@router.get('/projects/{project_id}/materials')
def project_materials(project_id: str, request: Request):
    from .project_materials import options
    actor = require_navigation(request)
    opts = query(request, 'MaterialQuery')
    with service(request).engine.connect() as c:
        visible_folder(c, actor, project_id)
        return checked('MaterialOptions', options(c, actor, project_id, opts.get('search', '')))


@router.get('/chats')
def recent(request: Request):
    actor = require_navigation(request)
    opts = query(request, 'RecentChatQuery')
    limit = opts.get('limit', 50)
    params = dict(actor=actor, limit=limit+1)
    predicate = 'owner_id=:actor AND hidden_at IS NULL'
    if ('before_time' in opts) != ('before_id' in opts):
        raise ApiError('cursor_invalid')
    if 'before_time' in opts:
        try:
            before = datetime.fromisoformat(opts['before_time'].upper()).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            raise ApiError('validation_error') from None
        predicate += ' AND (last_sent_at,id)<(:before,:id)'
        params.update(before=before, id=opts['before_id'])
    with service(request).engine.connect() as c:
        if folder := opts.get('folder_id'):
            visible_folder(c, actor, folder)
            predicate += ' AND folder_project_id=:folder'
            params['folder'] = folder
        rows = c.execute(text('SELECT id,project_id,folder_project_id,title,title_override,last_sent_at FROM aist.conversations WHERE '+
                              predicate+' ORDER BY last_sent_at DESC,id DESC LIMIT :limit'), params).mappings().all()
        items = chat_views(c, actor, rows[:limit])
    cursor = dict(before_time=items[-1]['last_sent_at'], before_id=items[-1]['id']) if len(rows)>limit else None
    return checked('RecentChatPage', dict(items=items,next_cursor=cursor))


@router.get('/chats/{conversation_id}')
def open_chat(conversation_id: str, request: Request):
    actor = require_navigation(request)
    with service(request).engine.connect() as c:
        chat = row(c, 'SELECT * FROM aist.conversations WHERE owner_id=:actor AND id=:id AND hidden_at IS NULL',
                   actor=actor,id=conversation_id)
        if chat is None:
            raise ApiError('resource_not_found')
        latest = row(c, """SELECT a.latest_version_id FROM aist.runs r JOIN aist.artifact_versions v
            ON v.id=r.result_version_id JOIN aist.artifacts a ON a.id=v.artifact_id
            WHERE r.conversation_id=:id AND r.owner_id=:actor AND a.result_type='analytical_note'
            ORDER BY r.created_at DESC,r.id DESC LIMIT 1""", id=conversation_id,actor=actor)
        display_chat = chat_views(c, actor, [chat])[0]
    return checked('OpenedChat', dict(chat=display_chat, note_id=latest['latest_version_id'] if latest else None))


def change(service, actor, command, idem):
    digest = fingerprint(command)
    with service.engine.begin() as c:
        # Serializes brief navigation commands for this owner, including personal-project creation.
        c.execute(text('SELECT id FROM aist.users WHERE id=:id FOR UPDATE'), dict(id=actor))
        existing = row(c, 'SELECT fingerprint,result FROM aist.navigation_receipts WHERE owner_id=:a AND key=:k',
                       a=actor,k=idem)
        if existing:
            if existing['fingerprint'] != digest:
                raise ApiError('idempotency_conflict')
            return existing['result']
        action = command['action']
        if action == 'personal':
            project = row(c, 'SELECT id FROM aist.projects WHERE owner_id=:a AND personal', a=actor)
            if project is None:
                project = row(c, """INSERT INTO aist.projects(id,owner_id,name,personal)
                    VALUES (:id,:a,'Личное пространство',true) RETURNING id""", id=ident('prj'),a=actor)
            target = project['id']
        elif action == 'pin':
            target = command['project_id']
            visible_folder(c, actor, target)
            c.execute(text('UPDATE aist.projects SET pinned=:pin WHERE id=:id AND owner_id=:a'),
                      dict(id=target, a=actor, pin=command['pinned']))
        else:
            target = command['chat_id']
            chat = row(c, 'SELECT * FROM aist.conversations WHERE owner_id=:a AND id=:id AND hidden_at IS NULL FOR UPDATE',
                       a=actor, id=target)
            if chat is None:
                raise ApiError('resource_not_found')
            if action == 'move':
                folder = command['folder_id']
                if folder is not None:
                    visible_folder(c, actor, folder)
                if chat['folder_project_id'] not in (command['expected_folder_id'],folder):
                    raise ApiError('revision_conflict')
                c.execute(text('UPDATE aist.conversations SET folder_project_id=:folder WHERE id=:id'),
                          dict(folder=folder,id=target))
            elif action == 'rename':
                title = command['title'].strip()
                if not title:
                    raise ApiError('validation_error')
                if chat_views(c, actor, [chat])[0]['title'] != command['expected_title']:
                    raise ApiError('revision_conflict')
                c.execute(text('UPDATE aist.conversations SET title_override=:title WHERE id=:id'),
                          dict(title=title, id=target))
            else:
                active = row(c, """SELECT id FROM aist.runs WHERE conversation_id=:id
                    AND state IN ('queued','running','waiting_for_user','recovery_required') LIMIT 1""", id=target)
                if active:
                    raise ApiError('revision_conflict')
                c.execute(text('UPDATE aist.conversations SET hidden_at=clock_timestamp() WHERE id=:id'),dict(id=target))
        result = checked('NavigationResult', dict(id=target))
        c.execute(text("""INSERT INTO aist.navigation_receipts(owner_id,key,fingerprint,result)
                       VALUES (:a,:k,:sha,CAST(:result AS jsonb))"""),
                  dict(a=actor,k=idem,sha=digest,result=json.dumps(result)))
        return result


@router.post('/actions')
async def navigation_action(request: Request):
    actor = require_navigation(request)
    if request.query_params:
        raise ApiError('validation_error')
    command = await json_body(request, 'NavigationAction')
    return await run_in_threadpool(change, service(request), actor, command, key(request))
