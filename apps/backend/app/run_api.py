import asyncio
import json
import time
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse,StreamingResponse
from starlette.concurrency import run_in_threadpool
from .api import key,query,json_body
from .auth import authenticate
from .errors import ApiError
from .contracts import validate_definition

router=APIRouter()


def runs(request):return request.app.state.runs


@router.get('/v1/capabilities')
def capabilities(request:Request):
    return validate_definition('Registry',runs(request).capabilities(request.state.actor))


@router.post('/v1/projects/{project_id}/conversations/{conversation_id}/messages')
async def submit(project_id:str,conversation_id:str,request:Request):
    await run_in_threadpool(request.app.state.service.check_scope,request.state.actor,project_id)
    payload=await json_body(request,'CreateMessageRun')
    if request.query_params:raise ApiError('validation_error')
    status,value=await run_in_threadpool(runs(request).write,request.state.actor,request.state.session_id,project_id,conversation_id,'submitMessage',key(request),payload)
    return JSONResponse(value,status_code=status)


@router.get('/v1/projects/{project_id}/conversations/{conversation_id}/messages')
def messages(project_id:str,conversation_id:str,request:Request):
    return runs(request).listing(request.state.actor,project_id,'messages',query(request),conversation_id)


@router.get('/v1/projects/{project_id}/runs')
def list_runs(project_id:str,request:Request):
    return runs(request).listing(request.state.actor,project_id,'runs',query(request,'RunListQuery'))


@router.get('/v1/projects/{project_id}/runs/{run_id}')
def get_run(project_id:str,run_id:str,request:Request):
    return runs(request).get(request.state.actor,project_id,run_id)


@router.post('/v1/projects/{project_id}/runs/{run_id}/cancel')
async def cancel(project_id:str,run_id:str,request:Request):
    payload=await json_body(request,'CancelRun')
    if request.query_params:raise ApiError('validation_error')
    status,value=await run_in_threadpool(runs(request).write,request.state.actor,request.state.session_id,project_id,run_id,'cancelRun',key(request),payload)
    from .executor import dispatch_cancel
    await run_in_threadpool(dispatch_cancel,runs(request),run_id)
    return JSONResponse(value,status_code=status)


@router.post('/v1/projects/{project_id}/runs/{run_id}/input')
async def resume(project_id:str,run_id:str,request:Request):
    payload=await json_body(request,'ResumeRun')
    if request.query_params:raise ApiError('validation_error')
    status,value=await run_in_threadpool(runs(request).write,request.state.actor,request.state.session_id,project_id,run_id,'resumeRun',key(request),payload)
    return JSONResponse(value,status_code=status)


@router.get('/v1/projects/{project_id}/runs/{run_id}/model-calls')
def calls(project_id:str,run_id:str,request:Request):
    return runs(request).listing(request.state.actor,project_id,'model_calls',query(request),run_id)


@router.get('/v1/projects/{project_id}/runs/{run_id}/tool-calls')
def tools(project_id:str,run_id:str,request:Request):
    return runs(request).listing(request.state.actor,project_id,'tool_calls',query(request),run_id)


@router.get('/v1/projects/{project_id}/runs/{run_id}/usage')
def usage(project_id:str,run_id:str,request:Request):
    return runs(request).listing(request.state.actor,project_id,'usage_entries',query(request),run_id)


@router.get('/v1/projects/{project_id}/artifact-versions/{version_id}')
def version(project_id:str,version_id:str,request:Request):
    return runs(request).version(request.state.actor,project_id,version_id)


@router.get('/v1/projects/{project_id}/artifact-versions/{version_id}/aso')
def aso_view(project_id: str, version_id: str, request: Request):
    from .aso import presentation
    value = runs(request).version(request.state.actor, project_id, version_id)
    if value['version']['payload']['result_type'] != 'analytical_note':
        raise ApiError('unsupported_result_type')
    return validate_definition('AsoView', presentation(value['version'], value['checks']))


@router.get('/v1/projects/{project_id}/runs/{run_id}/events')
def events(project_id:str,run_id:str,request:Request):
    return runs(request).events(request.state.actor,project_id,run_id,query(request,'EventQuery'))


@router.get('/v1/projects/{project_id}/runs/{run_id}/events/stream')
async def stream(project_id:str,run_id:str,request:Request):
    values=request.headers.getlist('last-event-id')
    if len(values)>1:raise ApiError('validation_error')
    q=query(request,'EventQuery');last=values[0] if values else None
    batch_reader = runs(request).event_batch
    first = await run_in_threadpool(batch_reader, request.state.actor, project_id, run_id, q, last)
    async def generate():
        batch, settled = first
        started = time.monotonic()
        ping = 0
        try:
            while time.monotonic()-started<300:
                for event in batch['events']:
                    yield f"id: {run_id}:{event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event,ensure_ascii=False,separators=(',',':'))}\n\n"
                after=batch['next_after_seq']
                if settled:
                    yield ': run-finished\n\n'
                    return
                if await request.is_disconnected():
                    return
                if not batch['has_more']:
                    if time.monotonic() - ping >= 2:
                        yield ': keepalive\n\n'
                        ping = time.monotonic()
                    await asyncio.sleep(.2)
                await run_in_threadpool(authenticate, request.app.state.engine, request.headers.get('authorization'))
                batch, settled = await run_in_threadpool(batch_reader, request.state.actor, project_id, run_id,
                                                         {'after_seq': after, 'limit': q.get('limit', 100)})
        except Exception:
            # After headers, close the stream; no second JSON response is appended.
            return
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})


@router.get('/v1/projects/{project_id}/artifacts/{artifact_id}')
def artifact(project_id:str,artifact_id:str,request:Request):
    from .versions import artifact_view
    return artifact_view(runs(request),request.state.actor,project_id,artifact_id)


@router.get('/v1/projects/{project_id}/artifacts/{artifact_id}/versions')
def versions(project_id:str,artifact_id:str,request:Request):
    from .versions import listing
    return listing(runs(request),request.state.actor,project_id,artifact_id,query(request))


@router.get('/v1/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}')
def nested_version(project_id:str,artifact_id:str,version_id:str,request:Request):
    value=runs(request).version(request.state.actor,project_id,version_id)
    if value['version']['artifact_id']!=artifact_id:raise ApiError('resource_not_found')
    return value


async def version_write(request,project,artifact,version=None):
    from .versions import write
    await run_in_threadpool(request.app.state.service.check_scope,request.state.actor,project)
    body=await json_body(request,'AcceptVersion' if version else 'SaveVersion')
    if request.query_params:raise ApiError('validation_error')
    status,value=await run_in_threadpool(write,runs(request),request.state.actor,request.state.session_id,
        project,artifact,version,'acceptVersion' if version else 'saveVersion',key(request),body)
    return JSONResponse(value,status_code=status)


@router.post('/v1/projects/{project_id}/artifacts/{artifact_id}/versions')
async def save_version(project_id:str,artifact_id:str,request:Request):
    return await version_write(request,project_id,artifact_id)


@router.post('/v1/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}/accept')
async def accept_version(project_id:str,artifact_id:str,version_id:str,request:Request):
    return await version_write(request,project_id,artifact_id,version_id)


@router.post('/v1/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}/exports')
async def request_export(project_id:str,artifact_id:str,version_id:str,request:Request):
    from .exports import Exports
    await run_in_threadpool(request.app.state.service.check_scope,request.state.actor,project_id)
    payload=await json_body(request,'CreateExport')
    if request.query_params:raise ApiError('validation_error')
    status,value=await run_in_threadpool(Exports(runs(request)).request,request.state.actor,request.state.session_id,project_id,artifact_id,version_id,key(request),payload)
    return JSONResponse(value,status_code=status)


@router.get('/v1/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}/exports')
def list_exports(project_id:str,artifact_id:str,version_id:str,request:Request):
    from .exports import Exports
    return Exports(runs(request)).listing(request.state.actor,project_id,artifact_id,version_id,query(request))


@router.get('/v1/projects/{project_id}/exports/{export_id}')
def get_export(project_id:str,export_id:str,request:Request):
    from .exports import Exports
    if request.query_params:raise ApiError('validation_error')
    return Exports(runs(request)).get(request.state.actor,project_id,export_id)


@router.get('/v1/projects/{project_id}/exports/{export_id}/file')
def download_export(project_id:str,export_id:str,request:Request):
    from .exports import Exports
    from fastapi.responses import Response
    from urllib.parse import quote
    if request.query_params:raise ApiError('validation_error')
    data,e=Exports(runs(request)).get(request.state.actor,project_id,export_id,download=True)
    return Response(data,media_type=e['file']['media_type'],headers={
        'Content-Disposition':"attachment; filename=\"aist-note."+e['format']+"\"; filename*=UTF-8''"+quote(e['file']['filename'],safe=''),
        'ETag':'"sha256:'+e['file']['sha256']+'"','X-Content-Type-Options':'nosniff'})


@router.get('/v1/projects/{project_id}/conversations/{conversation_id}/edit-proposals')
def proposals(project_id:str,conversation_id:str,request:Request):
    from .edit_proposals import listing
    return validate_definition('EditProposalPage',listing(runs(request),request.state.actor,project_id,conversation_id,query(request)))


@router.post('/v1/projects/{project_id}/conversations/{conversation_id}/edit-proposals')
async def propose(project_id:str,conversation_id:str,request:Request):
    from .edit_proposals import prepare
    if request.query_params: raise ApiError('validation_error')
    body=await json_body(request,'PrepareEdit')
    result=await run_in_threadpool(prepare,runs(request),request.state.actor,request.state.session_id,
                                   project_id,conversation_id,key(request),body)
    return validate_definition('EditPlan',result)


@router.post('/v1/projects/{project_id}/conversations/{conversation_id}/edit-proposals/{proposal_id}')
async def decide_proposal(project_id:str,conversation_id:str,proposal_id:str,request:Request):
    from .edit_proposals import decide
    if request.query_params: raise ApiError('validation_error')
    idem = key(request)
    body=await json_body(request,'EditDecision')
    return await run_in_threadpool(decide,runs(request),request.state.actor,request.state.session_id,
                                   project_id,conversation_id,proposal_id,idem,body['decision'])
