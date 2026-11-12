"""Thin executor boundary, plus an explicit scripted QA injection."""
import http.client
import json
from urllib.parse import urlsplit, urlencode
from .errors import ApiError
from .runs import execute,row,json_value


def qa_get(settings,path):
    url=urlsplit(settings.qa_url)
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','simulator') or url.username or url.password:
        raise ValueError('QA simulator must be explicitly local')
    c=http.client.HTTPConnection(url.hostname,url.port or 80,timeout=3)
    try:
        c.request('GET',path);r=c.getresponse();raw=r.read(200001)
        if r.status!=200 or len(raw)>200000:raise ApiError('temporarily_unavailable')
        return json.loads(raw)
    finally:c.close()


class ScriptedExecutor:
    """A fixed QA script, not a model reasoning loop and not OpenClaw."""
    def __init__(self,settings):self.settings=settings

    def start(self,r):
        return r['checkpoint'] or {'steps':qa_get(self.settings,'/plan?'+urlencode({'run_id':r['id']}))['steps'],'index':0,'safe_resume':True}

    def cancel(self,executor_run,executor_session):
        return qa_get(self.settings,'/abort?'+urlencode({'run_id':executor_run,'session_id':executor_session}))


class OpenClawExecutor:
    """RPC seam for the installed 2026.9.8 executor. No provider or tool loop here.

    The D10 worker supplies the installed Gateway client and durable correlation callbacks.
    The saved AIST version is the result; the Gateway's final prose does not publish it.
    """
    def __init__(self,gateway,attach):self.gateway,self.attach=gateway,attach

    def start(self,r,message,session_identity=None):
        session=r['executor_session_id']
        response=self.gateway.request('agent',dict(session_identity or {}, sessionKey=session,
                                                  message=message,idempotencyKey=r['id']))
        executor_run=response['runId']
        self.attach(r['id'],r['epoch'],executor_run,session)
        return {'executor_run_id':executor_run,'executor_session_id':session}

    def result(self,runs,actor,project,run):
        # The saved AIST result is authoritative; a CLI answer never publishes another version.
        value=runs.get(actor,project,run)
        return runs.version(actor,project,value['result_version_id']) if value['result_version_id'] else None

    def cancel(self,executor_run,executor_session,*,allow_finished=False):
        reply=self.gateway.request('sessions.abort',{'key':executor_session,'runId':executor_run})
        if allow_finished and reply.get('status') == 'no-active-run' and reply.get('abortedRunId') is None:
            current = self.gateway.request('sessions.describe', {'key': executor_session})
            if current.get('session', {}).get('hasActiveRun') is False:
                return reply
        if reply.get('abortedRunId')!=executor_run or reply.get('status')!='aborted':raise ApiError('outcome_unknown')
        return reply


def dispatch_cancel(runs,run):
    with runs.engine.connect() as c:
        r=row(c,'SELECT * FROM aist.runs WHERE id=:r',r=run)
    if not r or r['state']!='cancelled' or r['cancel_dispatched_at'] or not r['executor_run_id']:return
    if r['configuration']['executor']!='qa-scripted':return
    try:
        ScriptedExecutor(runs.settings).cancel(r['executor_run_id'],r['executor_session_id'])
        with runs.engine.begin() as c:
            execute(c,"UPDATE aist.runs SET cancel_dispatched_at=clock_timestamp() WHERE id=:r AND state='cancelled' AND executor_run_id=:e AND executor_session_id=:s",r=run,e=r['executor_run_id'],s=r['executor_session_id'])
    except Exception:
        # Durable cancelled fence already exists. Worker retries only the addressable abort.
        return


def qa_arguments(value, results):
    """Resolve explicit prior-step references in a fixed QA script, never model code."""
    if isinstance(value, dict):
        if set(value) == {'$qa_result', 'path'}:
            result = results[value['$qa_result']]
            for part in value['path']:
                result = result[part]
            return result
        return {k:qa_arguments(v,results) for k,v in value.items()}
    if isinstance(value, list):
        return [qa_arguments(v,results) for v in value]
    return value
