"""Actual PostgreSQL, HTTP, proxy and separate-worker-process acceptance probes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import httpx
from sqlalchemy import event,text
from sqlalchemy.exc import OperationalError

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'apps/backend/tests'))
from pdf_fixtures import pdf_bytes
from app.config import Settings
from app.database import make_engine
from app.service import Service,row
from app.storage import Storage
from app.runs import Runs,execute,json_value,now
from app.accounting import Accounting,exposure
from app.transport import Transport
from app.worker import SYNTHETIC_PAYLOAD
from app.contracts import validate_definition
from app.errors import ApiError

parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['linked','shutdown','db-down','db-recovery','restart','deep','ack','additional','lifecycle'])
parser.add_argument('--output',default='/evidence');parser.add_argument('--start-case',default='all');args=parser.parse_args()
out=Path(args.output);out.mkdir(parents=True,exist_ok=True);private=out/'private';private.mkdir(mode=0o700,exist_ok=True)
settings=Settings(lease_seconds=4,shutdown_seconds=3);engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings);ledger=Accounting(runs)
observations=[];workers=[];state={}
if (private/'state.json').exists():state=json.loads((private/'state.json').read_text())


def record(case,**values):
    observations.append({'case':case,**values});print(json.dumps({'case':case,'passed':True}),flush=True)


def admin(*a):return json.loads(subprocess.check_output([sys.executable,'-m','app.cli',*a]))


def new_actor():
    v=admin('create-user','--label','Synthetic D07');v.update(admin('issue-session','--user-id',v['user_id']));return v


def call(method,path,body=None,key=None,status=200,actor='a',headers=None,definition=None,origin='http://web:8080/api',**kwargs):
    h={'Authorization':'Bearer '+state[actor]['token']} if actor else {}
    if key:h['Idempotency-Key']=key
    h.update(headers or {})
    res=httpx.request(method,origin+path,headers=h,json=body,timeout=40,**kwargs)
    assert res.status_code in ((status,) if isinstance(status,int) else status),(method,path,res.status_code,res.text[:1000])
    v=res.json()
    if res.status_code>=400:validate_definition('ErrorResponse',v)
    elif definition:validate_definition(definition,v)
    observations.append({'method':method,'path':path,'status':res.status_code,'body':v})
    return v


def setup():
    p=call('POST','/v1/projects',{'name':'D07 synthetic'},uuid.uuid4().hex,201,definition='Project')['id']
    metadata={'title':'Synthetic D07','expected_revision':0}
    s=call('POST',f'/v1/projects/{p}/sources',key=uuid.uuid4().hex,status=201,definition='UploadReceipt',files=[('metadata',(None,json.dumps(metadata),'application/json')),('file',('synthetic.pdf',pdf_bytes(),'application/pdf'))])
    conv=call('POST',f'/v1/projects/{p}/conversations',{'title':'D07 conversation'},uuid.uuid4().hex,201,definition='Conversation')['id']
    return {'project':p,'source':s,'conversation':conv}


def payload(ctx,revision=0):
    return {'expected_revision':revision,'text':'Синтетическая проверка D07','source_version_ids':[ctx['source']['version']['id']],
        'skill_version_id':'skill_qa-d07-v1','model_profile_version_id':'model_qa-d07-v1','requested_result_type':'evidence_answer',
        'parameters':{'audience':None,'sections':[]},'base_version_id':None,'expected_artifact_revision':0,'requested_export':None}


def submit(ctx,steps=None):
    body=payload(ctx);key=uuid.uuid4().hex
    v=call('POST',f"/v1/projects/{ctx['project']}/conversations/{ctx['conversation']}/messages",body,key,202,definition='Submission')
    ctx.update(run=v['run']['id'],body=body,key=key)
    if steps is not None:
        res=httpx.post('http://simulator:8090/control',json={'run_id':ctx['run'],'steps':steps});assert res.status_code==200
    return v


def path(ctx,suffix=''):return f"/v1/projects/{ctx['project']}/runs/{ctx['run']}"+suffix


def get(ctx):return call('GET',path(ctx),definition='Run')


def stats():return httpx.get('http://simulator:8090/stats').json()


def db_run(ctx):
    with engine.connect() as c:return row(c,'SELECT * FROM aist.runs WHERE id=:r',r=ctx['run'])


def until(fn,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.04)
    raise AssertionError('deadline exceeded')


def start_worker(name,shutdown=3,transport=60):
    env={**os.environ,'AIST_LEASE_SECONDS':'4','AIST_SHUTDOWN_SECONDS':str(shutdown),'AIST_TRANSPORT_SECONDS':str(transport),'AIST_SNAPSHOT_PATH':str(out/(name+'-snapshot.json'))}
    log=(out/(name+'-worker.log')).open('w');p=subprocess.Popen([sys.executable,'-m','app.worker'],env=env,stdout=log,stderr=log)
    workers.append((p,log,name));return p


def stop_worker(p,expected=0):
    start=time.monotonic();p.send_signal(signal.SIGTERM);p.wait(timeout=13)
    assert p.returncode==expected,(p.returncode,expected)
    return time.monotonic()-start


def manual(ctx):
    r=runs.claim('worker_manual_'+uuid.uuid4().hex);assert r and r['id']==ctx['run'],r and r['id'];return r


def assert_error(fn,code):
    try:fn()
    except ApiError as e:assert e.code==code,(e.code,code)
    else:raise AssertionError('expected '+code)


def transport():return Transport(runs,threading.Event(),threading.Event(),threading.RLock(),{},threading.Lock())


def cancel(ctx):
    v=get(ctx);return call('POST',path(ctx,'/cancel'),{'expected_revision':v['revision']},uuid.uuid4().hex,definition='Run')


def check_events(ctx):
    v=get(ctx);e=call('GET',path(ctx,'/events'),definition='EventPage')['events']
    assert [x['seq'] for x in e]==list(range(1,v['last_event_seq']+1))
    transitions=[x for x in e if x['type']=='state_changed'];assert transitions[-1]['data']['to']==v['state']
    return e


def linked():
    if args.start_case=='all':
        state['a']=new_actor();state['b']=new_actor()
        ctx=setup();body=payload(ctx);key=uuid.uuid4().hex;endpoint=f"/v1/projects/{ctx['project']}/conversations/{ctx['conversation']}/messages"
        with ThreadPoolExecutor(2) as pool:
            values=list(pool.map(lambda _:call('POST',endpoint,body,key,(200,202),definition='Submission'),range(2)))
        assert values[0]['run']['id']==values[1]['run']['id'];ctx.update(run=values[0]['run']['id'],body=body,key=key)
        with engine.connect() as c:
            assert execute(c,'SELECT count(*) FROM aist.messages WHERE conversation_id=:conv',conv=ctx['conversation']).scalar_one()==1
            assert execute(c,'SELECT count(*) FROM aist.runs WHERE conversation_id=:conv',conv=ctx['conversation']).scalar_one()==1
        conflict=call('POST',endpoint,{**body,'text':'different'},key,409);assert conflict['error']['code']=='idempotency_conflict'
        duplicate=call('POST',endpoint,{**body,'source_version_ids':body['source_version_ids']*2},uuid.uuid4().hex,422);assert duplicate['error']['code']=='validation_error'
        blocked=call('POST',endpoint,{**body,'expected_revision':1},uuid.uuid4().hex,409)
        assert blocked['error']['code']=='revision_conflict'
        empty=setup()
        empty_endpoint=f"/v1/projects/{empty['project']}/conversations/{empty['conversation']}/messages"
        empty_body=payload(empty)
        planned=call('POST',empty_endpoint,{**empty_body,'skill_version_id':'skill_source-answer-v1'},uuid.uuid4().hex,409)
        assert planned['error']['code']=='tool_unavailable'
        future=call('POST',empty_endpoint,{**empty_body,'requested_result_type':'meeting_summary'},uuid.uuid4().hex,422)
        assert future['error']['code']=='unsupported_result_type'
        with engine.connect() as c:
            assert execute(c,'SELECT count(*) FROM aist.runs WHERE conversation_id=:conv',conv=empty['conversation']).scalar_one()==0
        caps=call('GET','/v1/capabilities',definition='Registry');assert all(s['status']!='enabled' for s in caps['skills'])
        record('concurrent_submit_and_honest_registry',run_id=ctx['run'])
        # Lost response: observe commit in PG without reading the HTTP reply.
        lost=setup();b=payload(lost);k=uuid.uuid4().hex;raw=json.dumps(b).encode();sock=socket.create_connection(('web',8080))
        ep=f"/api/v1/projects/{lost['project']}/conversations/{lost['conversation']}/messages"
        sock.sendall((f'POST {ep} HTTP/1.1\r\nHost: web\r\nAuthorization: Bearer {state["a"]["token"]}\r\nIdempotency-Key: {k}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode()+raw)
        def saved():
            with engine.connect() as c:return row(c,'SELECT * FROM aist.run_receipts WHERE key=:k',k=k)
        receipt=until(saved);sock.close();lost.update(run=receipt['run_id'],body=b,key=k)
        again=call('POST',ep[4:],b,k,200,definition='Submission');assert again['run']['id']==lost['run']
        state['lost']=lost;record('lost_response_repeat',run_id=lost['run'],response_read=False)
        before=stats()['post_count'];httpx.post('http://simulator:8090/control',json={'run_id':ctx['run'],'steps':[{'kind':'model','delay':1},{'kind':'tool'},{'kind':'save'}]})
        w1=start_worker('linked-1');w2=start_worker('linked-2')
        until(lambda:db_run(ctx)['state']=='running')
        pin=get(ctx)['input']['source_versions'];source=ctx['source']['source']['id']
        s2=call('POST',f"/v1/projects/{ctx['project']}/sources/{source}/versions",key=uuid.uuid4().hex,status=201,definition='UploadReceipt',files=[('metadata',(None,json.dumps({'title':'New head','expected_revision':1}),'application/json')),('file',('new.pdf',pdf_bytes('new-head'),'application/pdf'))])
        until(lambda:db_run(ctx)['state']=='completed');until(lambda:db_run(lost)['state']=='completed')
        assert stats()['post_count']==before+2 and get(ctx)['input']['source_versions']==pin
        assert s2['version']['id']!=pin[0]['source_version_id']
        v=get(ctx);view=call('GET',f"/v1/projects/{ctx['project']}/artifact-versions/{v['result_version_id']}",definition='VersionView')
        assert view['version']['provenance']['source_versions']==pin
        msgs=call('GET',endpoint,definition='MessagePage')['items'];assert [m['sequence'] for m in msgs]==[1,2] and msgs[1]['artifact_version_id']==v['result_version_id']
        term=call('POST',path(ctx,'/cancel'),{'expected_revision':1},uuid.uuid4().hex,409);assert term['error']['code']=='run_terminal'
        for suffix in ('','/events','/events/stream','/model-calls','/tool-calls','/usage'):
            err=call('GET',path(ctx,suffix),actor='b',status=404);assert err['error']['current_revision'] is None
        call('GET',f"/v1/projects/{ctx['project']}/artifact-versions/{v['result_version_id']}",actor='b',status=404)
        call('POST',path(ctx,'/cancel'),{'expected_revision':v['revision']},uuid.uuid4().hex,404,actor='b')
        check_events(ctx);stop_worker(w1);stop_worker(w2)
        state['completed']=ctx;record('two_processes_pins_result_and_owner_fences',provider_POST_delta=2,result_version_id=v['result_version_id'])
    # Cancel one of two active executor sessions and retain late usage.
    one,two=setup(),setup();submit(one,[{'kind':'model','delay':2},{'kind':'save'}]);submit(two,[{'kind':'model','delay':1.2},{'kind':'save'}])
    before=stats()['post_count'];w=start_worker('cancel')
    until(lambda:stats()['post_count']==before+2);pair=db_run(one)
    assert cancel(one)['state']=='cancelled'
    until(lambda:db_run(two)['state']=='completed');until(lambda:call('GET',path(one,'/model-calls'),definition='CallPage')['items'][0]['transport_state']=='observed')
    calls=call('GET',path(one,'/model-calls'),definition='CallPage')['items'];assert len(calls)==1 and calls[0]['late_response'] and calls[0]['delivery']=='suppressed'
    assert get(one)['result_version_id'] is None
    with engine.connect() as c:assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE data->\'provenance\'->>\'run_id\'=:r',r=one['run']).scalar_one()==0
    aborts=stats()['aborts'];assert {'run_id':pair['executor_run_id'],'session_id':pair['executor_session_id']} in aborts
    assert not any(a['run_id']==db_run(two)['executor_run_id'] for a in aborts)
    seq=check_events(one)[-1]['seq'];late=call('GET',path(one,'/events'),definition='EventPage',params={'after_seq':seq-2})['events'];assert all(e['seq']>seq-2 for e in late)
    stop_worker(w);record('targeted_cancel_and_late_usage',provider_POST_delta=stats()['post_count']-before)
    # Durable prompt, same Run and idempotent input after process restart.
    wait=setup();submit(wait,[{'kind':'model'},{'kind':'wait'},{'kind':'tool'},{'kind':'save'}]);w=start_worker('waiting-first')
    v=until(lambda:(x:=db_run(wait))['state']=='waiting_for_user' and x);stop_worker(w)
    assert all(m['transport_state']=='observed' for m in call('GET',path(wait,'/model-calls'),definition='CallPage')['items'])
    input_body={'expected_revision':v['revision'],'waiting_input_id':v['waiting_input']['id'],'text':'Synthetic input'};key=uuid.uuid4().hex
    resumed=call('POST',path(wait,'/input'),input_body,key,202,definition='Submission');repeat=call('POST',path(wait,'/input'),input_body,key,200,definition='Submission')
    assert resumed['message']['id']==repeat['message']['id'] and resumed['run']['id']==wait['run']
    before=stats()['post_count'];w=start_worker('waiting-resume');until(lambda:db_run(wait)['state']=='completed');stop_worker(w)
    assert stats()['post_count']==before
    msgs=call('GET',f"/v1/projects/{wait['project']}/conversations/{wait['conversation']}/messages",definition='MessagePage')['items'];assert [m['sequence'] for m in msgs]==[1,2,3]
    record('waiting_input_repeat_restart_same_context',model_POST_after_resume=0)
    # SSE must remain alive beyond the previous absolute proxy deadline.
    long=setup();submit(long,[{'kind':'model','delay':23},{'kind':'save'}]);w=start_worker('sse')
    before=time.monotonic();frames=[];keepalive=0
    with httpx.stream('GET','http://web:8080/api'+path(long,'/events/stream'),headers={'Authorization':'Bearer '+state['a']['token']},timeout=35) as stream:
        assert stream.status_code==200
        for line in stream.iter_lines():
            if line.startswith(':'):keepalive+=1
            if line.startswith('data: '):
                e=json.loads(line[6:]);validate_definition('RunEvent',e);frames.append(e)
                if e['type']=='result_saved':break
    elapsed=time.monotonic()-before;assert elapsed>20 and keepalive>=8
    v=get(long);assert v['state']=='completed'
    last=frames[-3]['seq']
    with httpx.stream('GET','http://web:8080/api'+path(long,'/events/stream'),headers={'Authorization':'Bearer '+state['a']['token'],'Last-Event-ID':long['run']+':'+str(last)},params={'after_seq':last},timeout=5) as stream:
        reconnected=[]
        for line in stream.iter_lines():
            if line.startswith('data: '):
                reconnected.append(json.loads(line[6:]))
                if len(reconnected)==2:break
    assert all(e['seq']>last for e in reconnected)
    for params,head in [({'after_seq':v['last_event_seq']+1},{}),({'after_seq':last+1},{'Last-Event-ID':long['run']+':'+str(last)}),({}, {'Last-Event-ID':one['run']+':0'})]:
        err=call('GET',path(long,'/events/stream'),status=409,params=params,headers=head);assert err['error']['code']=='cursor_invalid'
    stop_worker(w);record('same_origin_SSE_over_20_seconds_reconnect',elapsed_seconds=elapsed,keepalives=keepalive)
    # Disconnect early cannot cancel a task.
    dis=setup();submit(dis,[{'kind':'model','delay':1},{'kind':'save'}]);w=start_worker('disconnect')
    with httpx.stream('GET','http://web:8080/api'+path(dis,'/events/stream'),headers={'Authorization':'Bearer '+state['a']['token']},timeout=5) as stream:
        next(stream.iter_lines())
    until(lambda:db_run(dis)['state']=='completed');stop_worker(w);record('SSE_disconnect_preserves_run')


def shutdown():
    for name,delay,expected in [('before-deadline',1.7,'completed'),('after-deadline',7,'recovery_required')]:
        ctx=setup();submit(ctx,[{'kind':'model','delay':delay},{'kind':'save'}]);count=stats()['post_count'];w=start_worker(name)
        until(lambda:stats()['post_count']==count+1);elapsed=stop_worker(w)
        assert elapsed>.9
        v=get(ctx);assert v['state']==expected,(name,v)
        snap=json.loads((out/(name+'-snapshot.json')).read_text());assert snap['saved']
        sr=next(r for r in snap['runs'] if r['id']==ctx['run']);assert sr['state']==v['state'] and sr['last_event_seq']==v['last_event_seq']
        if expected=='recovery_required':
            m=call('GET',path(ctx,'/model-calls'),definition='CallPage')['items'][0];assert m['transport_state']=='unknown'
            u=call('GET',path(ctx,'/usage'),definition='UsagePage')['items'][-1];assert u['reservation_state']=='held' and u['estimated_usd']['value'] is None and u['tokens']['input']['value'] is None
        count=stats()['post_count'];w=start_worker(name+'-restart');time.sleep(1);stop_worker(w);assert stats()['post_count']==count
        record('D03F-F02_'+name,elapsed_seconds=elapsed,run_id=ctx['run'],state=v['state'],POST_after_restart=0)
    # A cancelled Run remains terminal at an overdue shutdown.
    ctx=setup();submit(ctx,[{'kind':'model','delay':7},{'kind':'save'}]);count=stats()['post_count'];w=start_worker('cancelled-deadline');until(lambda:stats()['post_count']==count+1);cancel(ctx);stop_worker(w)
    assert get(ctx)['state']=='cancelled';record('shutdown_retains_cancelled_terminal')


def db_down():
    ctx=setup();submit(ctx,[{'kind':'model','delay':10},{'kind':'save'}]);before=stats()['post_count'];w=start_worker('db-down');until(lambda:stats()['post_count']==before+1)
    state['db_down']=ctx;save_state();(private/'db-down-ready').write_text('ready')
    until(lambda:(private/'db-stopped').exists(),timeout=40)
    elapsed=stop_worker(w,expected=2);snap=json.loads((out/'db-down-snapshot.json').read_text());assert snap['saved'] is False
    record('SIGTERM_database_unavailable',saved=False,elapsed_seconds=elapsed,run_id=ctx['run'])


def db_recovery():
    ctx=state['db_down'];before=stats()['post_count'];time.sleep(4.1);w=start_worker('db-restored')
    until(lambda:db_run(ctx)['state']=='recovery_required');stop_worker(w)
    assert stats()['post_count']==before
    calls=call('GET',path(ctx,'/model-calls'),definition='CallPage')['items'];assert calls[0]['transport_state']=='unknown'
    record('startup_after_database_outage',POST_after_restart=0,run_id=ctx['run'])


def restart():
    for key in ('completed','lost'):
        ctx=state[key];repeat=call('POST',f"/v1/projects/{ctx['project']}/conversations/{ctx['conversation']}/messages",ctx['body'],ctx['key'],200,definition='Submission');assert repeat['run']['id']==ctx['run'] and repeat['run']['state']=='completed'
    record('API_restart_durable_receipts')


def deep():
    # Real DB fencing, tool replies and structured failures.
    ctx=setup();submit(ctx);r=manual(ctx);before=stats()['post_count']
    a={'source_version_id':ctx['source']['version']['id'],'page':1};eid='call_replay_'+uuid.uuid4().hex
    reply=runs.tool(r['id'],r['epoch'],eid,'aist_read_source',a);repeat=runs.tool(r['id'],r['epoch'],eid,'aist_read_source',a);assert reply==repeat and reply['ok']
    assert_error(lambda:runs.tool(r['id'],r['epoch'],eid,'aist_read_source',{**a,'page':2}),'idempotency_conflict')
    alien=state['completed']['source']['version']['id']
    assert_error(lambda:runs.tool(r['id'],r['epoch'],'call_alien','aist_read_source',{'source_version_id':alien,'page':1}),'resource_not_found')
    with engine.begin() as c:execute(c,"UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:r",r=r['id'])
    assert_error(lambda:runs.tool(r['id'],r['epoch'],eid,'aist_read_source',a),'outcome_unknown')
    assert_error(lambda:runs.tool(r['id'],r['epoch'],'call_save','aist_save_artifact',{'idempotency_key':'save','expected_revision':0,'artifact_id':None,'parent_version_id':None,'payload':SYNTHETIC_PAYLOAD}),'outcome_unknown')
    runs.recover();assert get(ctx)['state']=='recovery_required' and stats()['post_count']==before
    record('expired_lease_blocks_save_and_cached_tool_reply')
    error=setup();submit(error);r=manual(error);eid='call_error_'+uuid.uuid4().hex
    v=runs.tool(r['id'],r['epoch'],eid,'aist_read_source',{'source_version_id':error['source']['version']['id'],'page':999})
    assert not v['ok'] and v['tool_call_id']==eid and v['error']['code']=='tool_failed' and get(error)['state']=='failed'
    record('structured_tool_failure_original_correlation')
    # Revocation prevents new work and saved replies, including a previously authorized Run.
    original=state['a'];state['a']=new_actor();rev=setup();submit(rev);r=manual(rev);state['a']=original
    admin('revoke-session','--session-id',r['session_id'])
    assert_error(lambda:runs.tool(r['id'],r['epoch'],'call_revoked','aist_read_source',{'source_version_id':rev['source']['version']['id'],'page':1}),'unauthenticated')
    runs.stop(r['id'],r['epoch'],'unauthenticated');record('session_revocation_closes_executor_authority')
    # Three held operations leave room for one of two concurrent reservations.
    ctx=setup();submit(ctx);r=manual(ctx)
    for _ in range(3):ledger.reserve(r['id'],r['epoch'])
    def reservation(_):
        try:return {'call':ledger.reserve(r['id'],r['epoch'])['id']}
        except ApiError as e:return {'error':e.code}
    with ThreadPoolExecutor(2) as pool:values=list(pool.map(reservation,range(2)))
    assert sum('call' in v for v in values)==1 and any(v.get('error')=='budget_exceeded' for v in values)
    with engine.connect() as c:
        total=execute(c,'SELECT sum(exposure) FROM aist.model_calls WHERE run_id=:r',r=r['id']).scalar_one();assert total==Decimal('1.20000004')
    cancel(ctx);record('concurrent_budget_reservations',accepted=1,refused=1,exposure=format(total,'f'))
    # Nested operation, Decimal pricing, supersedes and partial aggregate stay one ledger.
    ctx=setup();submit(ctx);r=manual(ctx);t=transport();cid=t.perform(r,{'kind':'model'})
    eid='call_nested_tool';runs.tool(r['id'],r['epoch'],eid,'aist_read_source',{'source_version_id':ctx['source']['version']['id'],'page':1},cid)
    with engine.connect() as c:tid=execute(c,'SELECT id FROM aist.tool_calls WHERE run_id=:r AND executor_tool_call_id=:e',r=r['id'],e=eid).scalar_one()
    nested=ledger.reserve(r['id'],r['epoch'],'extraction',parent_model=cid,parent_tool=tid)
    conn=http.client.HTTPConnection('simulator',8090,timeout=3);conn.connect();ledger.send(r['id'],r['epoch'],nested['id'],conn,'/completion',json_value({'run_id':r['id'],'call_id':nested['id'],'synthetic':True,'step':{'kind':'model'}}));res=conn.getresponse();ledger.observe(r['id'],r['epoch'],nested['id'],res.status,json.loads(res.read()));conn.close()
    with engine.connect() as c:estimated=execute(c,'SELECT exposure FROM aist.model_calls WHERE id=:id',id=cid).scalar_one()
    expected=(Decimal(101)*Decimal(settings and runs.policy['input_per_million'])+Decimal(53)*Decimal(runs.policy['output_per_million']))/Decimal(1000000);assert estimated==expected
    inspection = ledger.billing_call(r['id'], cid)
    fields = ('run_id', 'call_id', 'owner_id', 'project_id', 'profile_id', 'provider', 'protocol', 'budget_scope',
              'provider_request_id', 'provider_completion_id')
    proof = {key: inspection[key] for key in fields}
    proof.update(operation_id='qa_' + uuid.uuid4().hex, account_scope='qa-fixture',
                 source_id='d07-itemized-fixture', line_id=cid, subject='qa-scenario',
                 source_sha256=hashlib.sha256(b'D07 synthetic charge: USD 0.00010001').hexdigest(),
                 confirmed_at=now(), expected_usage_id=inspection['current_usage_id'], currency='USD',
                 amount_usd='0.00010001')
    invoice = ledger.invoice(r['id'], cid, proof, apply=True)
    assert invoice['status'] == 'recorded'
    assert ledger.invoice(r['id'], cid, proof, apply=True)['usage_entry_id'] == invoice['usage_entry_id']
    aggregate={'id':'bill_'+uuid.uuid4().hex,'provider':'qa','model':'qa-d07','period_start':'2026-10-08T00:00:00Z','period_end':'2026-10-08T01:00:00Z','scope':'provider_account_hour','coverage':'partial','amount_usd':'99.99999999','request_count':2,'per_call_attribution':False,'observed_at':'2026-10-08T01:01:00Z'};ledger.aggregate(aggregate)
    with engine.connect() as c:cost=execute(c,'SELECT sum(exposure) FROM aist.model_calls WHERE run_id=:r',r=r['id']).scalar_one();assert cost==Decimal('0.00010001')+expected
    entries=call('GET',path(ctx,'/usage'),definition='UsagePage')['items'];assert len(entries)==5
    cancel(ctx);record('nested_calls_Decimal_supersedes_aggregate',exposure=format(cost,'f'),estimated_usd=format(expected,'f'),usage_entries=len(entries))
    # A complete response with malformed payload is observed error, not unknown.
    ctx=setup();submit(ctx);r=manual(ctx);t=transport();assert t.perform(r,{'kind':'model','invalid':True}) is None
    assert get(ctx)['state']=='failed';m=call('GET',path(ctx,'/model-calls'),definition='CallPage')['items'][0];assert m['transport_state']=='observed' and m['provider_outcome']=='error'
    record('observed_invalid_response')
    # Real after-send disconnect must never POST again on restart.
    ctx=setup();submit(ctx,[{'kind':'model','disconnect':True},{'kind':'save'}]);before=stats()['post_count'];w=start_worker('transport-reset');until(lambda:db_run(ctx)['state']=='recovery_required');stop_worker(w)
    w=start_worker('transport-reset-restart');time.sleep(.7);stop_worker(w);assert stats()['post_count']==before+1
    record('after_send_disconnect_unknown_no_replay',POST_delta=1)
    # Connect refusal has evidence of no send, true zero usage and released reservation.
    ctx=setup();submit(ctx);r=manual(ctx)
    bad=Settings(executor_mode='qa-scripted',qa_url='http://127.0.0.1:1',policy_path=settings.policy_path,storage_root=settings.storage_root)
    local=Runs(Service(engine,Storage(settings.storage_root)),bad);t=Transport(local,threading.Event(),threading.Event(),threading.RLock(),{},threading.Lock())
    assert_error(lambda:t.perform(r,{'kind':'model'}),'outcome_unknown')
    m=call('GET',path(ctx,'/model-calls'),definition='CallPage')['items'][0];assert m['transport_state']=='not_sent'
    u=call('GET',path(ctx,'/usage'),definition='UsagePage')['items'][-1];assert u['estimated_usd']['value']=='0' and u['reservation_state']=='released'
    record('before_send_connection_refused_proven_zero')
    ack()


def ack():
    # Simulate loss of DB commit acknowledgement AFTER actual commit, using the driver hook.
    ctx=setup();body=payload(ctx);key=uuid.uuid4().hex;armed=[True]
    def lost_ack(connection):
        if armed[0]:
            armed[0]=False;connection.connection.dbapi_connection.commit();raise OperationalError('commit',{},Exception('ack lost'))
    event.listen(engine,'commit',lost_ack)
    try:
        status,v=runs.write(state['a']['user_id'],state['a']['session_id'],ctx['project'],ctx['conversation'],'submitMessage',key,body)
        assert status==200
    finally:event.remove(engine,'commit',lost_ack)
    ctx.update(run=v['run']['id']);r=manual(ctx);cid=transport().perform(r,{'kind':'model'})
    armed[0]=True;event.listen(engine,'commit',lost_ack)
    try:
        reply=runs.tool(r['id'],r['epoch'],'call_save_ack','aist_save_artifact',{'idempotency_key':'ack-save-key','expected_revision':0,'artifact_id':None,'parent_version_id':None,'payload':SYNTHETIC_PAYLOAD},cid)
        assert reply['ok'] and get(ctx)['state']=='completed'
    finally:event.remove(engine,'commit',lost_ack)
    record('commit_ack_reconciliation_submit_and_result')
    ctx=setup();submit(ctx);r=manual(ctx);before=stats()['post_count'];armed[0]=True
    event.listen(engine,'commit',lost_ack)
    try:assert_error(lambda:ledger.reserve(r['id'],r['epoch']),'outcome_unknown')
    finally:event.remove(engine,'commit',lost_ack)
    assert get(ctx)['state']=='recovery_required' and stats()['post_count']==before
    record('attempted_commit_ack_lost_never_sends_POST')



def additional():
    # Normal runtime cannot enable planned skills or the separate QA fixture IDs.
    ctx=setup();env={**os.environ,'AIST_EXECUTOR_MODE':'disabled','AIST_POLICY_PATH':'','AIST_QA_URL':''}
    log=(out/'normal-api.log').open('w');process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8103','--no-access-log'],env=env,stdout=log,stderr=log)
    def ready():
        try:return httpx.get('http://127.0.0.1:8103/readyz').status_code==200
        except httpx.HTTPError:return False
    try:
        until(ready)
        endpoint=f"/v1/projects/{ctx['project']}/conversations/{ctx['conversation']}/messages"
        for skill in ['skill_qa-d07-v1','skill_source-answer-v1']:
            refusal=call('POST',endpoint,{**payload(ctx),'skill_version_id':skill},uuid.uuid4().hex,409,origin='http://127.0.0.1:8103')
            assert refusal['error']['code']=='tool_unavailable'
        caps=call('GET','/v1/capabilities',origin='http://127.0.0.1:8103',definition='Registry');assert all(v['status']!='enabled' for v in caps['model_profiles'])
        with engine.connect() as c:assert execute(c,'SELECT count(*) FROM aist.messages WHERE conversation_id=:conv',conv=ctx['conversation']).scalar_one()==0
    finally:process.send_signal(signal.SIGTERM);process.wait(timeout=8);log.close()
    record('ordinary_runtime_refuses_QA_and_planned_skills')
    # Concurrent operations in DIFFERENT Runs consume the same project quota.
    first=setup();second=dict(first);third=dict(first)
    for current in (second,third):
        current['conversation']=call('POST',f"/v1/projects/{first['project']}/conversations",{'title':'Quota concurrency'},uuid.uuid4().hex,201,definition='Conversation')['id']
    for current in (first,second,third):submit(current)
    rs=[manual(c) for c in (first,second,third)]
    for r,n in zip(rs,(3,3,2)):
        for _ in range(n):ledger.reserve(r['id'],r['epoch'])
    def reserve(r):
        try:return {'call':ledger.reserve(r['id'],r['epoch'])['id']}
        except ApiError as e:return {'error':e.code}
    with ThreadPoolExecutor(2) as pool:values=list(pool.map(reserve,rs[:2]))
    assert sum('call' in v for v in values)==1 and any(v.get('error')=='budget_exceeded' for v in values)
    with engine.connect() as c:total=execute(c,'SELECT sum(exposure) FROM aist.model_calls WHERE project_id=:p',p=first['project']).scalar_one()
    assert total==Decimal('2.70000009')
    for current in (first,second,third):cancel(current)
    record('competing_Run_reservations_share_project_quota',exposure=format(total,'f'),accepted=1,refused=1)
    # Limit causes a durable budget_exceeded and no ninth model POST/result.
    ctx=setup();submit(ctx,[{'kind':'model'}]*9+[{'kind':'save'}]);before=stats()['post_count'];w=start_worker('budget-limit')
    until(lambda:db_run(ctx)['state']=='budget_exceeded');stop_worker(w);assert stats()['post_count']==before+8 and get(ctx)['result_version_id'] is None
    record('worker_budget_exceeded_no_extra_POST',actual_POSTs=8)
    # DB constraints reject mutation of immutable inputs, messages, receipts and cursors.
    ctx=state['completed'];r=db_run(ctx);rejected=[]
    cases=[("UPDATE aist.messages SET text='changed' WHERE run_id=:r",'append_only_message'),
        ("UPDATE aist.runs SET input=input || '{\"parameters\":{}}'::jsonb WHERE id=:r",'immutable_run_input'),
        ("UPDATE aist.runs SET last_event_seq=last_event_seq+1 WHERE id=:r",'event_snapshot_consistency'),
        ("UPDATE aist.conversations SET revision=revision+1 WHERE id=:conv",'conversation_revision_consistency'),
        ("UPDATE aist.run_receipts SET fingerprint=repeat('f',64) WHERE run_id=:r",'immutable_receipt')]
    for sql,label in cases:
        try:
            with engine.begin() as c:execute(c,sql,r=ctx['run'],conv=ctx['conversation'])
        except Exception:rejected.append(label)
        else:raise AssertionError(label)
    record('database_invariants',rejected=rejected)
    # Parent calls from another Run cannot become capabilities for a new model operation.
    ctx=setup();submit(ctx);r=manual(ctx)
    with engine.connect() as c:alien=execute(c,'SELECT id FROM aist.model_calls WHERE run_id=:r LIMIT 1',r=state['completed']['run']).scalar_one()
    assert_error(lambda:ledger.reserve(r['id'],r['epoch'],'extraction',parent_model=alien),'resource_not_found')
    cancel(ctx);record('foreign_parent_call_rejected')


def lifecycle():
    # Source readiness is checked again at claim, using the exact pinned version.
    ctx=setup();submit(ctx);before=stats()['post_count'];v=ctx['source']['version']['id'];original=Path(settings.storage_root)/(v+'.pdf');preserved=original.with_suffix('.qa-preserved')
    original.rename(preserved)
    try:
        assert runs.claim('worker_readiness') is None
        assert get(ctx)['state']=='failed' and stats()['post_count']==before
    finally:preserved.rename(original)
    record('source_readiness_rechecked_before_executor')
    # Revoked session denies HTTP history and queued executor admission.
    old=state['a'];state['a']=new_actor();ctx=setup();submit(ctx);admin('revoke-session','--session-id',state['a']['session_id'])
    call('GET',path(ctx),status=401);call('GET',f"/v1/projects/{ctx['project']}/conversations/{ctx['conversation']}/messages",status=401)
    assert runs.claim('worker_revoked') is None and db_run(ctx)['state']=='failed';state['a']=old
    record('revocation_denies_history_and_queued_execution')
    # A missing waiting context refuses input without a new message or paid loop.
    ctx=setup();submit(ctx);r=manual(ctx);runs.checkpoint(r['id'],r['epoch'],{'safe_resume':True,'steps':[],'index':0})
    reply=runs.tool(r['id'],r['epoch'],'call_waiting_lost','aist_request_input',{'idempotency_key':'waiting-key','expected_revision':r['revision'],'prompt':'Synthetic waiting prompt'})
    with engine.begin() as c:execute(c,'UPDATE aist.runs SET checkpoint=NULL WHERE id=:r',r=r['id'])
    v=get(ctx);refusal=call('POST',path(ctx,'/input'),{'expected_revision':v['revision'],'waiting_input_id':reply['result']['id'],'text':'Synthetic input'},uuid.uuid4().hex,503)
    assert refusal['error']['code']=='outcome_unknown'
    with engine.connect() as c:assert execute(c,'SELECT count(*) FROM aist.messages WHERE run_id=:r',r=r['id']).scalar_one()==1
    cancel(ctx);record('lost_waiting_context_no_new_message_or_POST')
    # A send-commit acknowledgement loss is reconciled before reading the same response.
    ctx=setup();submit(ctx);r=manual(ctx);m=ledger.reserve(r['id'],r['epoch']);conn=http.client.HTTPConnection('simulator',8090,timeout=3);conn.connect();armed=[True]
    def lost_ack(connection):
        if armed[0]:armed[0]=False;connection.connection.dbapi_connection.commit();raise OperationalError('commit',{},Exception('ack lost'))
    event.listen(engine,'commit',lost_ack)
    try:ledger.send(r['id'],r['epoch'],m['id'],conn,'/completion',json_value({'run_id':r['id'],'call_id':m['id'],'synthetic':True,'step':{'kind':'model'}}))
    finally:event.remove(engine,'commit',lost_ack)
    response=conn.getresponse();payload=json.loads(response.read());conn.close();armed[0]=True;event.listen(engine,'commit',lost_ack)
    try:assert ledger.observe(r['id'],r['epoch'],m['id'],response.status,payload)
    finally:event.remove(engine,'commit',lost_ack)
    save=runs.tool(r['id'],r['epoch'],'call_lifecycle_save','aist_save_artifact',{'idempotency_key':'lifecycle-save','expected_revision':0,'artifact_id':None,'parent_version_id':None,'payload':SYNTHETIC_PAYLOAD},m['id'])
    assert save['ok'] and get(ctx)['state']=='completed'
    record('send_and_observe_commit_ack_reconciled_without_retry')
    # Latest bounded-read transport, actual process, success and after-send reset.
    for mode,step,state_value in [('success',{'kind':'model','delay':.4},'completed'),('reset',{'kind':'model','disconnect':True},'recovery_required')]:
        ctx=setup();submit(ctx,[step,{'kind':'save'}]);before=stats()['post_count'];w=start_worker('final-'+mode)
        until(lambda:db_run(ctx)['state']==state_value);stop_worker(w);assert stats()['post_count']==before+1
        record('final_worker_transport_'+mode,actual_POSTs=1,state=state_value)

def save_state():
    (private/'state.json').write_text(json.dumps(state));os.chmod(private/'state.json',0o600)

try:
    globals()[args.phase.replace('-','_')]();save_state()
    result={'phase':args.phase,'passed':True,'synthetic_executor':True,'synthetic_provider':True,'product_PostgreSQL_worker_transport':True,'provider':stats(),'observations':observations}
    (out/(args.phase+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
except BaseException:
    save_state();(out/(args.phase+'-failed.json')).write_text(json.dumps({'passed':False,'observations':observations},ensure_ascii=False,indent=2)+'\n');raise
finally:
    for p,log,name in workers:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        log.close()
