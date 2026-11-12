"""Non-D02 inputs use the same ingest, tools and saved basis, without review overrides."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import httpx
from app.config import Settings
from app.database import make_engine
from app.service import Service
from app.storage import Storage
from app.runs import Runs
from app.facts import save_fact,pin_of
from app.normalization import fingerprint
sys.path.insert(0,'/workspace/apps/backend/tests')
from pdf_fixtures import pdf_bytes
from test_domain import fact
settings=Settings(lease_seconds=600);engine=make_engine(settings);service=Service(engine,Storage(settings.storage_root));runs=Runs(service,settings)
user=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D09 generic ingest QA']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',user['user_id']]))

def api(method,path,body=None,status=200,**kwargs):
    r=httpx.request(method,'http://web:8080/api'+path,json=body,headers={'Authorization':'Bearer '+session['token'],'Idempotency-Key':uuid.uuid4().hex},timeout=40,**kwargs)
    assert r.status_code==status,(r.status_code,r.text[:300]);return r.json()

p=api('POST','/v1/projects',{'name':'D09 generic ingest'},201)['id']
s=api('POST',f'/v1/projects/{p}/sources',status=201,files=[('metadata',(None,json.dumps({'title':'Synthetic arbitrary inputs','expected_revision':0}),'application/json')),('file',('generic.pdf',pdf_bytes('Revenue RUB million 1Q20=101 1Q21=102'),'application/pdf'))])
with engine.begin() as c:
    version=service.scoped(c,user['user_id'],p,'source_versions',s['version']['id']);pin=pin_of(version)
    for i,year in [(1,2020),(2,2021)]:
        f=fact(str(100+i),year,'fact_generic_'+str(i));f['source']=pin
        f['period']['label']='1Q'+str(year);f['evidence'][0]['source']=pin;f['evidence'][0]['quote']='Value '+str(100+i)+'.';f['evidence'][0]['locator']['pdf_page']=i
        save_fact(service,c,user['user_id'],p,pin,f,calculation_basis={'definition':'Synthetic revenue','compatibility':'Same synthetic reporting series'})
conv=api('POST',f'/v1/projects/{p}/conversations',{'title':'Generic path'},201)['id']
body=dict(expected_revision=0,text='Generic fixture ingest QA',source_version_ids=[pin['source_version_id']],skill_version_id='skill_qa-d07-v1',model_profile_version_id='model_qa-d07-v1',requested_result_type='evidence_answer',parameters={'audience':None,'sections':[]},base_version_id=None,expected_artifact_revision=0,requested_export=None)
submission=api('POST',f'/v1/projects/{p}/conversations/{conv}/messages',body,202);r=runs.claim('qa_generic');assert r['id']==submission['run']['id']
inputs=[{'name':n,'fact':{'source_version_id':pin['source_version_id'],'fact_id':'fact_generic_'+str(i)}} for n,i in [('previous',1),('current',2)]]
a={'formula_id':'growth_percent.v1','inputs':inputs,'rounding':{'places':2,'mode':'HALF_UP'}}
result=runs.tool(r['id'],r['epoch'],'qa_calc','aist_calculate',a);assert result['ok'],result
calc=result['result'];assert calc['exact']=={'numerator':'100','denominator':'101'} and calc['rounded']['value']=='0.99'
payload={'result_type':'evidence_answer','schema_version':'1.0.0','content':{'claims':[{'text':'Synthetic calculation from ordinary ingestion.','origin':'calculation','fact_refs':[],'calculation_ids':[calc['calculation_id']]}],'missing_information':['Synthetic QA input, no model.']}}
saved=runs.tool(r['id'],r['epoch'],'qa_save','aist_save_artifact',dict(idempotency_key='generic_save',expected_revision=0,artifact_id=None,parent_version_id=None,payload=payload));assert saved['ok'],saved
view=saved['result'];assert len(view['version']['basis']['facts'])==2
assert api('GET',f"/v1/projects/{p}/artifact-versions/{view['version']['id']}")==view
Path('/evidence/generic-ingest.json').write_text(json.dumps(dict(passed=True,review_override=False,calculation=calc,view=view,basis_sha256=fingerprint(view['version']['basis'])),ensure_ascii=False,indent=2))
print('Generic ingest, exact 100/101, server-resolved transitive basis: passed')
engine.dispose()
