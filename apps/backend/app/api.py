from urllib.parse import quote
import base64
from threading import BoundedSemaphore
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser, MultiPartException
from .errors import ApiError
from .normalization import checked, parse_json, decode_json
from .storage import MAX_BYTES, safe_filename

router = APIRouter()


def service(request):
    return request.app.state.service


def key(request):
    values = request.headers.getlist('idempotency-key')
    if len(values)!=1:
        raise ApiError('validation_error')
    checked('WriteHeaders', {'Idempotency-Key':values[0]})
    return values[0]


def query(request, definition='ListQuery'):
    result = {}
    for name,value in request.query_params.multi_items():
        if name in result:
            raise ApiError('validation_error')
        if name in ('limit','page','after_seq'):
            if not value.isascii() or not value.isdigit() or len(value)>16:
                raise ApiError('validation_error')
            value = int(value)
        result[name] = value
    return checked(definition,result)


async def body_bytes(request, maximum):
    length = request.headers.get('content-length')
    if length:
        if not length.isdigit():
            raise ApiError('validation_error')
        if int(length)>maximum:
            raise ApiError('too_large')
    body = bytearray()
    async for part in request.stream():
        if len(body)+len(part)>maximum:
            raise ApiError('too_large')
        body.extend(part)
    return bytes(body)


async def json_body(request, definition):
    if request.headers.get('content-type','').split(';')[0].lower()!='application/json':
        raise ApiError('validation_error')
    maximum = 1048576 if definition == 'SaveVersion' else 16384
    if definition in ('LoginRequest', 'LogoutRequest'):
        maximum = 4096
    if definition == 'LoginRequest':
        return checked(definition, decode_json(await body_bytes(request, maximum), normalize_strings=False))
    return parse_json(await body_bytes(request,maximum),definition)


async def upload_body(request):
    if request.headers.get('content-type','').split(';')[0].lower()!='multipart/form-data':
        raise ApiError('validation_error')
    bounded = await body_bytes(request,MAX_BYTES+65536)
    async def stream():
        yield bounded
    form = None
    parser = MultiPartParser(request.headers,stream(),max_files=1,max_fields=1,max_part_size=16384)
    try:
        form = await parser.parse()
        items = form.multi_items()
        if len(items)!=2 or {k for k,v in items}!={'metadata','file'}:
            raise ApiError('validation_error')
        metadata, file = form['metadata'],form['file']
        if not isinstance(metadata,str) or not isinstance(file,UploadFile):
            raise ApiError('validation_error')
        payload = parse_json(metadata,'UploadMetadata')
        if file.content_type!='application/pdf':
            raise ApiError('unsupported_source')
        data = await file.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:
            raise ApiError('too_large')
        if not data:
            raise ApiError('unsupported_source')
        return payload,data,safe_filename(file.filename)
    except (MultiPartException, ValueError, UnicodeError):
        raise ApiError('validation_error') from None
    finally:
        if form is not None:
            await form.close()


@router.post('/v1/projects')
async def create_project(request: Request):
    idem = key(request)
    payload = await json_body(request,'CreateProject')
    status, value = await run_in_threadpool(service(request).write,request.state.actor,'createProject',idem,payload)
    return JSONResponse(value,status_code=status)


@router.get('/v1/projects')
def list_projects(request: Request):
    return service(request).listing(request.state.actor,'projects',query(request))


@router.get('/v1/projects/{project_id}')
def get_project(project_id: str, request: Request):
    return service(request).get(request.state.actor,project_id)


async def upload_source(project_id: str, request: Request, source_id=None):
    await run_in_threadpool(service(request).check_scope,request.state.actor,project_id,source_id)
    if request.query_params:
        raise ApiError('validation_error')
    idem = key(request)
    metadata,data,filename = await upload_body(request)
    status,value = await run_in_threadpool(service(request).write, request.state.actor,
        'uploadSourceVersion' if source_id else 'uploadSource',idem,metadata,
        project_id,source_id,data,filename)
    return JSONResponse(value,status_code=status)


@router.post('/v1/projects/{project_id}/sources')
async def new_source(project_id: str, request: Request):
    return await upload_source(project_id,request)


@router.post('/v1/projects/{project_id}/sources/{source_id}/versions')
async def new_version(project_id: str, source_id: str, request: Request):
    return await upload_source(project_id,request,source_id)


@router.get('/v1/projects/{project_id}/sources')
def list_sources(project_id: str, request: Request):
    service(request).check_scope(request.state.actor,project_id)
    return service(request).listing(request.state.actor,'sources',query(request),project_id)


@router.get('/v1/projects/{project_id}/sources/{source_id}')
def get_source(project_id: str, source_id: str, request: Request):
    return service(request).get(request.state.actor,project_id,'sources',source_id)


@router.get('/v1/projects/{project_id}/source-versions/{source_version_id}')
def get_version(project_id: str, source_version_id: str, request: Request):
    if request.query_params:
        svc = service(request)
        with svc.engine.connect() as c:
            version = svc.scoped(c, request.state.actor, project_id, 'source_versions', source_version_id)
            query(request, 'PdfMetadataQuery')
            return svc.version_metadata(c, version)
    return service(request).get(request.state.actor,project_id,'source_versions',source_version_id)


@router.get('/v1/projects/{project_id}/source-versions/{source_version_id}/page')
def read_page(project_id: str, source_version_id: str, request: Request):
    # Scope раньше query и любой загрузки данных версии.
    service(request).check_version_scope(request.state.actor,project_id,source_version_id)
    return service(request).page_or_file(request.state.actor,project_id,source_version_id,
        query(request,'SourcePageQuery')['page'])


_preview_slots = BoundedSemaphore(1)


@router.get('/v1/projects/{project_id}/source-versions/{source_version_id}/preview')
def preview_page(project_id: str, source_version_id: str, request: Request):
    from .pdf_images import render
    svc = service(request)
    actor = request.state.actor
    svc.check_version_scope(actor, project_id, source_version_id)
    page = query(request, 'SourcePageQuery')['page']
    if not _preview_slots.acquire(blocking=False):
        raise ApiError('temporarily_unavailable')
    try:
        data, version = svc.page_or_file(actor, project_id, source_version_id)
        if version.get('media_type') != 'application/pdf' or not 1 <= page <= version['page_count']:
            raise ApiError('resource_not_found')
        try:
            image = render(data, [page], dict(long_side=1800, quality=87), 10)[0]
        except ApiError:
            raise ApiError('temporarily_unavailable') from None
        return Response(base64.b64decode(image['data']), media_type='image/jpeg',
                        headers={'Cache-Control':'private, no-store'})
    finally:
        _preview_slots.release()


@router.get('/v1/projects/{project_id}/source-versions/{source_version_id}/file')
def download(project_id: str, source_version_id: str, request: Request):
    data,version = service(request).page_or_file(request.state.actor,project_id,source_version_id)
    name = quote(version['original_filename'],safe='')
    fallback = 'source.json' if version.get('media_type') == 'application/json' else 'source.pdf'
    return Response(data,media_type='application/octet-stream',headers={
        'Content-Disposition': f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{name}",
        'ETag': '"sha256:'+version['sha256']+'"'})


@router.post('/v1/projects/{project_id}/conversations')
async def create_conversation(project_id: str, request: Request):
    await run_in_threadpool(service(request).check_scope,request.state.actor,project_id)
    idem = key(request)
    payload = await json_body(request,'CreateConversation')
    status,value = await run_in_threadpool(service(request).write,request.state.actor,
        'createConversation',idem,payload,project_id)
    return JSONResponse(value,status_code=status)


@router.get('/v1/projects/{project_id}/conversations')
def list_conversations(project_id: str, request: Request):
    service(request).check_scope(request.state.actor,project_id)
    return service(request).listing(request.state.actor,'conversations',query(request),project_id)


@router.get('/v1/projects/{project_id}/conversations/{conversation_id}')
def get_conversation(project_id: str, conversation_id: str, request: Request):
    return service(request).get(request.state.actor,project_id,'conversations',conversation_id)


@router.post('/v1/projects/{project_id}/search')
async def search(project_id: str, request: Request):
    from .search import ui_search
    payload=await json_body(request,'SearchEvidenceArgs')
    runs = request.app.state.runs
    session = dict(session_id=request.state.session_id, owner_id=request.state.actor)
    def authorize_embedding(connection):
        runs.session_alive(connection, session)
    return await run_in_threadpool(ui_search, service(request), runs.settings, request.state.actor,
                                  project_id, payload, authorize_embedding=authorize_embedding)


@router.get('/v1/projects/{project_id}/moex/wush')
def moex_status(project_id: str, request: Request):
    from .moex import status
    return status(service(request), request.state.actor, project_id)


@router.post('/v1/projects/{project_id}/moex/wush')
async def moex_update(project_id: str, request: Request):
    from .moex import update
    await run_in_threadpool(service(request).check_scope, request.state.actor, project_id)
    if request.query_params:
        raise ApiError('validation_error')
    payload = await json_body(request, 'MoexRequest')
    runs = request.app.state.runs
    session = dict(session_id=request.state.session_id, owner_id=request.state.actor)
    return await run_in_threadpool(update, service(request), request.state.actor, project_id, key(request),
                                  payload['mode'], authorize=lambda connection: runs.session_alive(connection, session), settings=runs.settings)
