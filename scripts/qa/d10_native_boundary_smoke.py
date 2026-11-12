"""Installed OpenClaw replay/tool-schema probe with a local fake model, zero Novita."""
from pathlib import Path
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from app.config import Settings
from app.openclaw_runtime import prepare, environment, Gateway, CLI, PrivateHTTPServer, PrivateUnixServer, ProviderHandler, ToolHandler

root=Path('/data/d10/native-boundary-smoke-v2');root.mkdir(mode=0o700)
settings=SimpleNamespace(gateway_port=29105,provider_port=29106)
for port in (29105,29106):
    with socket.socket() as s:s.bind(('127.0.0.1',port))
token=secrets.token_hex(32);prepare(settings,root,token);env=environment(root,token)
args={'artifact_id':None,'parent_version_id':None,'expected_revision':0,'idempotency_key':'synthetic_save_01',
      'payload':{'result_type':'analytical_note','schema_version':'1.0.0','content':{
          'title':'Synthetic','audience':'QA','sections':[{'section_id':'section_one','title':'Test','blocks':[
              {'block_id':'block_one','kind':'paragraph','claim':{'text':'Synthetic','origin':'source_fact',
               'fact_refs':[{'source_version_id':'sv_fake','fact_id':'fact_fake'}],'calculation_ids':[]}}]}]}}}
original={'id':'call_original_with_underscores','type':'function','function':{'name':'aist_save_artifact','arguments':json.dumps(args)}}
class Boundary:
    def __init__(self):self.token=token;self.requests=[];self.tools=[]
    def model(self,body):
        self.requests.append(body)
        if len(self.requests)==1:
            advertised=next(t['function']['parameters'] for t in body['tools'] if t['function']['name']=='aist_save_artifact')
            assert advertised['properties']['payload']['type']=='object'
            message={'role':'assistant','content':None,'reasoning_content':'Original synthetic reasoning','tool_calls':[original]}
            finish='tool_calls'
        else:
            history=next(m for m in body['messages'] if m['role']=='assistant' and m.get('tool_calls'))
            replayed=history['tool_calls'][0]
            assert replayed['id']==original['id'] and replayed['function']['name']==original['function']['name']
            assert json.loads(replayed['function']['arguments'])==args
            assert history['reasoning_content']=='Original synthetic reasoning'
            assert self.tools and self.tools[0]['tool_call_id']==original['id']
            assert self.tools[0]['args']==args
            message={'role':'assistant','content':'Synthetic probe finished.'};finish='stop'
        return {'id':'synthetic_'+str(len(self.requests)),'created':1,'model':body['model'],
                'choices':[{'index':0,'message':message,'finish_reason':finish}],
                'usage':{'prompt_tokens':1,'completion_tokens':1}}
    def tool(self,value):self.tools.append(value);return {'ok':True,'tool_call_id':value['tool_call_id'],'result':{'saved':True}}
b=Boundary();http=PrivateHTTPServer(('127.0.0.1',29106),ProviderHandler);http.boundary=b
unix=PrivateUnixServer(str(root/'tools.sock'),ToolHandler);unix.boundary=b;os.chmod(root/'tools.sock',0o600)
threads=[threading.Thread(target=s.serve_forever) for s in (http,unix)]
for t in threads:t.start()
log=(root/'gateway-process.log').open('w');proc=subprocess.Popen([CLI,'gateway','run'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
try:
    end=time.monotonic()+150
    while True:
        try:
            with socket.create_connection(('127.0.0.1',29105),timeout=.2):break
        except OSError:
            assert time.monotonic()<end and proc.poll() is None;time.sleep(.2)
    gateway=Gateway(settings,root,env)
    gateway.request('agent',{'sessionKey':'agent:main:explicit:d10-native-smoke','message':'Synthetic tool integration probe.','idempotencyKey':'d10-native-smoke'})
    end=time.monotonic()+120
    while len(b.requests)<2:
        assert time.monotonic()<end;time.sleep(.2)
    time.sleep(2)
    assert len(b.requests)==2 and len(b.tools)==1
    history=next(m for m in b.requests[1]['messages'] if m['role']=='assistant' and m.get('tool_calls'))
    replayed=history['tool_calls'][0]
    assert replayed['id']==original['id'] and json.loads(replayed['function']['arguments'])==args
    assert history['reasoning_content']=='Original synthetic reasoning'
    assert b.tools[0]['tool_call_id']==original['id'] and b.tools[0]['args']==args
    (root/'exchange.json').write_text(json.dumps({'requests':b.requests,'tools':b.tools},ensure_ascii=False,indent=2)+'\n')
    (root/'summary.json').write_text(json.dumps({'passed':True,'external_model_posts':0,'local_model_requests':2,
        'tool_calls':1,'original_tool_ids_and_reasoning':True,'typed_null_and_object_received':True},indent=2)+'\n')
    print('Installed OpenClaw native tool/replay probe passed; external POSTs 0')
finally:
    (root/'exchange.json').write_text(json.dumps({'requests':b.requests,'tools':b.tools},ensure_ascii=False,indent=2)+'\n')
    os.killpg(proc.pid,signal.SIGTERM)
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
    log.close()
    for s in (http,unix):s.shutdown();s.server_close()
    for t in threads:t.join()
