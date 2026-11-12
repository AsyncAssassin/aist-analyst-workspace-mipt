"""Independent boundary probes using a real worker, PostgreSQL and local HTTP."""
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
import httpx
from sqlalchemy import text
from app.config import Settings
from app.database import make_engine

sys.path.insert(0,'/workspace/apps/backend/tests')
from pdf_fixtures import pdf_bytes

OUT=Path('/evidence')
engine=make_engine(Settings())
observations=[]
actor=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','Independent boundary probes']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',actor['user_id']]))
headers={'Authorization':'Bearer '+session['token']}
def api(method,path,expected=200,**kw):
    r=httpx.request(method,'http://web:8080/api'+path,headers=headers|({'Idempotency-Key':uuid.uuid4().hex} if method=='POST' else {}),timeout=20,**kw)
    assert r.status_code==expected,(path,r.status_code)
    return r.json()
def setup_run():
    p=api('POST','/v1/projects',201,json={'name':'Independent D07 probe'})['id']
    source=api('POST',f'/v1/projects/{p}/sources',201,files=[('metadata',(None,json.dumps({'title':'Synthetic boundary','expected_revision':0}),'application/json')),('file',('boundary.pdf',pdf_bytes('boundary'),'application/pdf'))])
    conversation=api('POST',f'/v1/projects/{p}/conversations',201,json={'title':'Boundary'})['id']
    b={'expected_revision':0,'text':'Synthetic boundary','source_version_ids':[source['version']['id']],
       'skill_version_id':'skill_qa-d07-v1','model_profile_version_id':'model_qa-d07-v1','requested_result_type':'evidence_answer',
       'parameters':{'audience':None,'sections':[]},'base_version_id':None,'expected_artifact_revision':0,'requested_export':None}
    return api('POST',f'/v1/projects/{p}/conversations/{conversation}/messages',202,json=b)['run']['id']
def state(rid):
    with engine.connect() as c:
        r=c.execute(text('SELECT state,result_version_id,last_event_seq FROM aist.runs WHERE id=:r'),{'r':rid}).mappings().one()
        calls=list(c.execute(text('SELECT data FROM aist.model_calls WHERE run_id=:r ORDER BY created_at'),{'r':rid}).scalars())
        return dict(r)|{'calls':calls}
def until(fn,seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        v=fn()
        if v:return v
        time.sleep(.05)
    raise AssertionError('probe setup timeout')

class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a):pass
    def do_GET(self):
        data=json.dumps({'steps':[{'kind':'model'},{'kind':'save'}]}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts+=1
        value={'synthetic':True,'id':{'invalid':'object'} if self.server.mode=='bad_id' else 'qa_boundary',
               'usage':{'prompt_tokens':101,'completion_tokens':53,'cached_input':31,'reasoning_output':17}}
        body=json.dumps(value).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
        if self.server.mode=='partial_close':
            self.send_header('Connection','close');self.close_connection=True
        self.end_headers()
        if self.server.mode=='partial_close':
            self.wfile.write(body[:1]);self.wfile.flush();self.server.ready.set();self.server.release.wait(25)
            try:self.wfile.write(body[1:]);self.wfile.flush()
            except OSError:pass
        else:
            self.wfile.write(body);self.wfile.flush();self.server.ready.set()

for mode in ('bad_id','partial_close'):
    server=Server(('127.0.0.1',0),Handler);server.mode=mode;server.posts=0;server.ready=threading.Event();server.release=threading.Event()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    process=None;log=None
    try:
        rid=setup_run()
        snapshot=OUT/('independent-'+mode+'-snapshot.json')
        env=os.environ|{'AIST_QA_URL':'http://127.0.0.1:'+str(server.server_port),'AIST_LEASE_SECONDS':'4','AIST_SHUTDOWN_SECONDS':'1','AIST_TRANSPORT_SECONDS':'30','AIST_SNAPSHOT_PATH':str(snapshot)}
        log=(OUT/('independent-'+mode+'-worker.log')).open('w')
        process=subprocess.Popen([sys.executable,'-m','app.worker'],env=env,stdout=log,stderr=log)
        assert server.ready.wait(10),'Local response was not served'
        if mode=='bad_id':
            after=until(lambda:(v:=state(rid))['state'] in ('failed','recovery_required','completed') and v)
            process.send_signal(signal.SIGTERM);process.wait(timeout=8)
            observations.append({'case':'fully_received_response_invalid_completion_id','expected':{'run':'failed','call':'observed','provider_outcome':'error'},'actual':after,'local_POSTs':server.posts,'finding':after['calls'][0]['transport_state']!='observed'})
        else:
            time.sleep(.25)
            started=time.monotonic();process.send_signal(signal.SIGTERM)
            try:process.wait(timeout=7);still_running=False
            except subprocess.TimeoutExpired:still_running=True
            after=state(rid)
            snap=json.loads(snapshot.read_text()) if snapshot.exists() else None
            observations.append({'case':'SIGTERM_during_partial_body_Connection_close','shutdown_seconds':1,'transport_seconds':30,
                'elapsed_seconds':round(time.monotonic()-started,3),'process_alive_after_7_seconds':still_running,
                'snapshot_saved':snap and snap['saved'],'actual':after,'local_POSTs':server.posts,'finding':still_running})
            server.release.set()
            process.wait(timeout=8)
            observations[-1]['exit_after_releasing_body']=process.returncode
            observations[-1]['POSTs_after_release']=server.posts
        (OUT/'independent-probes.json').write_text(json.dumps({'completed':True,'observations':observations,'external_provider_POSTs':0},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'case':observations[-1]['case'],'finding':observations[-1]['finding']},ensure_ascii=False),flush=True)
    finally:
        server.release.set()
        if process is not None and process.poll() is None:
            process.kill();process.wait(timeout=5)
        if log:log.close()
        server.shutdown();server.server_close();thread.join(timeout=3)
engine.dispose()
