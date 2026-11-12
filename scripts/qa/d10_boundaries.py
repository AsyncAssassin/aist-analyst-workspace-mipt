"""D10 synthetic PostgreSQL + HTTP tests. Real Novita is never addressed."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from app.accounting import Accounting
from app.config import Settings
from app.database import make_engine
from app.errors import ApiError
from app.extraction import source_pages, ingest
from app.openclaw_runtime import Boundary, PrivateHTTPServer
from app.runs import Runs, execute, row
from app.service import Service
from app.storage import Storage
from app.transport import Transport, ReaderConnection
from app.normalization import fingerprint

OUT=Path('/evidence');OUT.mkdir(exist_ok=True)
settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.json',lease_seconds=600,transport_seconds=3)
engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings)
user=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D10 synthetic QA']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',user['user_id']]))
records=[]

def api(method,path,body=None,status=200,**kwargs):
    res=httpx.request(method,os.getenv('AIST_D10_QA_API_BASE','http://web:8080/api')+path,json=body,headers={'Authorization':'Bearer '+session['token'],'Idempotency-Key':uuid.uuid4().hex},timeout=30,**kwargs)
    assert res.status_code==status,(res.status_code,res.text[:200]);return res.json()

def pdf():
    w=PdfWriter();page=w.add_blank_page(595,842)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):w._add_object(font)})})
    lines=['Synthetic Company','Revenue, million RUB','IFRS','123','9M20','140','9M21']
    stream=DecodedStreamObject();stream.set_data(('BT /F1 14 Tf 72 760 Td '+ ' 0 -24 Td '.join('('+x+') Tj' for x in lines)+' ET').encode())
    page[NameObject('/Contents')]=w._add_object(stream);out=io.BytesIO();w.write(out);return out.getvalue()

def setup():
    p=api('POST','/v1/projects',{'name':'D10 synthetic'},201)['id']
    upload=api('POST',f'/v1/projects/{p}/sources',status=201,files=[('metadata',(None,json.dumps({'title':'Synthetic','expected_revision':0}),'application/json')),('file',('synthetic.pdf',pdf(),'application/pdf'))])
    conv=api('POST',f'/v1/projects/{p}/conversations',{'title':'Synthetic'},201)['id']
    body=dict(expected_revision=0,text='Synthetic independent task',source_version_ids=[upload['version']['id']],skill_version_id='skill_analytical-note-v1',model_profile_version_id='model_novita-mimo-v1',requested_result_type='analytical_note',parameters={'audience':None,'sections':[]},base_version_id=None,expected_artifact_revision=0,requested_export=None)
    key=uuid.uuid4().hex
    _,submission=runs.write(user['user_id'],session['session_id'],p,conv,'submitMessage',key,body)
    r=runs.claim('qa_d10_'+uuid.uuid4().hex);assert r['id']==submission['run']['id']
    return r,(p,conv,key,body)

def check(name,**data):records.append({'case':name,'passed':True,**data});print(name+' passed',flush=True)
def assert_error(fn,code=None):
    try:fn()
    except ApiError as e:
        if code:assert e.code==code,(e.code,code)
        return
    raise AssertionError('Expected rejection')

VALID={'id':'synthetic-only','model':'xiaomimimo/mimo-v2.6-pro','created':1,'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"facts":[]}'}}], 'usage':{'prompt_tokens':101,'completion_tokens':17,'prompt_tokens_details':{'cached_tokens':0},'completion_tokens_details':{'reasoning_tokens':3}}}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_POST(self):
        self.server.posts+=1;self.rfile.read(int(self.headers['Content-Length']));self.server.sent.set()
        if self.server.mode=='late':self.server.release.wait(8)
        if self.server.mode=='unknown':
            self.send_response(200);self.send_header('Content-Length','1000');self.end_headers();self.wfile.write(b'{');self.close_connection=True;return
        body=json.dumps(VALID if self.server.mode!='error' else {'error':{'message':'synthetic provider failure'}}).encode()
        self.send_response(500 if self.server.mode=='error' else 200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

def transport_case(mode):
    r,replay=setup();server=PrivateHTTPServer(('127.0.0.1',0),Handler);server.mode=mode;server.posts=0;server.sent=threading.Event();server.release=threading.Event()
    thread=threading.Thread(target=server.serve_forever);thread.start()
    worker=SimpleNamespace(runs=runs,persistence=threading.RLock(),closed=threading.Event(),draining=threading.Event())
    transport=Transport(runs,worker.draining,worker.closed,worker.persistence,{},threading.Lock())
    request={'messages':[{'role':'user','content':'Synthetic '+mode}]};audit=OUT/r['id'];audit.mkdir()
    try:
        with patch('app.novita.credential',return_value='synthetic-token'),patch('app.transport.ReaderHTTPSConnection',side_effect=lambda *a,**kw:ReaderConnection('127.0.0.1',server.server_port,timeout=3)):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(transport.perform,r,{'purpose':'extraction'},request=request,audit=audit)
                assert server.sent.wait(8)
                if mode=='late':
                    runs.write(user['user_id'],session['session_id'],r['project_id'],r['id'],'cancelRun',uuid.uuid4().hex,{'expected_revision':r['revision']})
                    server.release.set()
                if mode=='unknown':assert_error(future.result,'outcome_unknown')
                else:future.result()
            if mode=='unknown':assert_error(lambda:transport.perform(r,{'purpose':'extraction'},request=request,audit=audit),'outcome_unknown')
        with engine.connect() as c:
            state=row(c,'SELECT * FROM aist.runs WHERE id=:r',r=r['id'])
            calls=[v[0] for v in execute(c,'SELECT data FROM aist.model_calls WHERE run_id=:r',r=r['id'])]
        assert server.posts==1 and len(calls)==1
        call=calls[0]
        if mode=='late':
            assert state['state']=='cancelled' and call['late_response'] and call['delivery']=='suppressed'
            assert state['result_version_id'] is None
            assert_error(lambda:runs.tool(r['id'],r['epoch'],'call_late','aist_read_source',{'source_version_id':r['input']['source_versions'][0]['source_version_id'],'page':1},call['id']),'run_terminal')
        elif mode=='error':assert state['state']=='failed' and call['provider_outcome']=='error'
        else:
            assert state['state']=='recovery_required' and call['transport_state']=='unknown'
            p,conv,key,body=replay
            _,same=runs.write(user['user_id'],session['session_id'],p,conv,'submitMessage',key,body)
            assert same['run']['id']==r['id'] and same['run']['state']=='recovery_required'
        check('transport_'+mode,run_id=r['id'],posts=server.posts,call=call)
    finally:server.release.set();server.shutdown();server.server_close();thread.join()

r,replay=setup()
with engine.begin() as c:
    assert execute(c,'SELECT count(*) FROM aist.facts WHERE project_id=:p',p=r['project_id']).scalar_one()==0
    pages=source_pages(runs,c,r)
items=[]
for value,year in [('123',2020),('140',2021)]:
    items.append(dict(metric='Revenue',subject='Synthetic Company',reporting_basis='IFRS',nature='reported_actual',period={'kind':'interval','label':'9M'+str(year)[2:],'start':f'{year}-01-01','end':f'{year}-09-30'},amount={'value':value,'unit':'money','currency':'RUB'},value_text=value,evidence=[{'page':1,'line_start':1,'line_end':7,'area':'Synthetic series'}],basis={'page':1,'metric_label':'Revenue','unit_label':'million RUB','reporting_label':'IFRS'}))
ledger=Accounting(runs);extraction_call=ledger.reserve(r['id'],r['epoch'],'extraction')
ledger.observe(r['id'],r['epoch'],extraction_call['id'],200,VALID)
facts=ingest(runs,r,pages,{'facts':items},extraction_call['id'])
refs=[{'source_version_id':f['source']['source_version_id'],'fact_id':f['fact_id']} for f in facts]
calc=runs.tool(r['id'],r['epoch'],'call_calc','aist_calculate',{'formula_id':'growth_percent.v1','inputs':[{'name':n,'fact':ref} for n,ref in zip(('previous','current'),refs)],'rounding':{'places':2,'mode':'HALF_UP'}})['result']
assert calc['exact']=={'numerator':'1700','denominator':'123'}
rows=[dict(row_id='row_'+str(i),label='Revenue',period_label=f['period']['label'],amount=f['amount'],fact_refs=[ref],calculation_ids=[]) for i,(f,ref) in enumerate(zip(facts,refs))]
rows.append(dict(row_id='row_growth',label='Growth',period_label='Comparable periods',amount=calc['rounded'],fact_refs=[],calculation_ids=[calc['calculation_id']]))
payload=dict(result_type='analytical_note',schema_version='1.0.0',content=dict(title='Synthetic historical draft',audience='QA',sections=[dict(section_id='section_one',title='Evidence',blocks=[dict(block_id='block_one',kind='paragraph',claim=dict(text='Revenue in comparable periods.',origin='source_fact',fact_refs=refs,calculation_ids=[])),dict(block_id='block_table',kind='table',caption='Revenue and growth',rows=rows)])]))
def args(p):return dict(idempotency_key=uuid.uuid4().hex,expected_revision=0,artifact_id=None,parent_version_id=None,payload=p)
for name,change in [('wrong_table_number',lambda p:p['content']['sections'][0]['blocks'][1]['rows'][0]['amount'].update(value='124')),('unreferenced_number',lambda p:p['content']['sections'][0]['blocks'][0]['claim'].update(text='Revenue 999.')),('false_human_editorial',lambda p:p['content']['sections'][0]['blocks'][0]['claim'].update(origin='human_editorial'))]:
    bad=deepcopy(payload);change(bad);reply=runs.tool(r['id'],r['epoch'],'call_'+name,'aist_save_artifact',args(bad));assert not reply['ok'];check(name)
saved=runs.tool(r['id'],r['epoch'],'call_save','aist_save_artifact',args(payload));assert saved['ok'],saved
view=saved['result'];vid=view['version']['id']
assert runs.version(user['user_id'],r['project_id'],vid)==view
assert len(view['version']['basis']['facts'])==2 and view['review']['acceptance'] is None
assert [c['status'] for c in view['checks']]==['passed','not_checked']
p,conv,key,body=replay
_,same=runs.write(user['user_id'],session['session_id'],p,conv,'submitMessage',key,body)
assert same['run']['result_version_id']==vid
check('new_ingest_note_table_and_paragraph_basis_replay',run_id=r['id'],version_id=vid,view=view)

r,_=setup()
with engine.connect() as c:v=runs.service.scoped(c,user['user_id'],r['project_id'],'source_versions',r['input']['source_versions'][0]['source_version_id'])
path=runs.service.storage.path(v['storage_name']);backup=path.with_suffix('.qa-held');path.rename(backup)
try:
    with engine.begin() as c:assert_error(lambda:source_pages(runs,c,r),'source_not_ready')
finally:backup.rename(path)
runs.stop(r['id'],r['epoch'],'source_not_ready');check('unavailable_source_before_extraction')
for mode in ('error','late','unknown'):transport_case(mode)
(OUT/'d10-boundaries.json').write_text(json.dumps({'passed':True,'external_model_posts':0,'records':records},ensure_ascii=False,indent=2)+'\n')
engine.dispose()
