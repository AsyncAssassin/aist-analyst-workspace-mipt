"""D14: настоящая локальная БД, синтетические PDF, без модели и исполнителя."""
import json,sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'/workspace/apps/backend/tests')
from pdf_fixtures import pdf_bytes
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute
from app.service import Service,ident
from app.storage import Storage
from app.facts import save_fact,pin_of,resolve_fact
from app.errors import ApiError
from app.tools import invoke
from app.openclaw_runtime import execution_facts
from types import SimpleNamespace
settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.d14.json',lease_seconds=600)
engine=make_engine(settings);service=Service(engine,Storage(settings.storage_root));runs=Runs(service,settings)
actor=ident('usr');session=ident('ses');records=[]
with engine.begin() as c:
 execute(c,'INSERT INTO aist.users(id,label) VALUES (:id,:label)',id=actor,label='D14 synthetic boundary QA')
 execute(c,"INSERT INTO aist.sessions(id,user_id,token_hash,expires_at) VALUES (:id,:a,:hash,clock_timestamp()+interval '1 hour')",id=session,a=actor,hash=ident('synthetic'))
_,project=service.write(actor,'createProject',ident('key'),{'name':'D14 local boundaries'});p=project['id']
_,conversation=service.write(actor,'createConversation',ident('key'),{'title':'D14 synthetic'},p)
sources=[]
for name in ('alpha','beta','unselected'):
 _,upload=service.write(actor,'uploadSource',ident('key'),dict(title=name,expected_revision=0),p,data=pdf_bytes(name),filename=name+'.pdf');sources.append(upload)
refs=[]
with engine.begin() as c:
 for upload in sources:
  v=service.scoped(c,actor,p,'source_versions',upload['version']['id']);pin=pin_of(v)
  f=dict(fact_id='fact_same',source=pin,extraction_version='d14-synthetic-qa.v1',metric='Synthetic AIST',subject='Synthetic AIST',reporting_basis='Не указано',nature='source_statement',period=dict(kind='unknown',label='Не указано',start=None,end=None),amount=None,raw_text='Synthetic AIST',evidence=[dict(evidence_id='ev_same',source=pin,locator=dict(pdf_page=1,area='Synthetic QA',bbox_pt=None,coordinate_system='pdf_points_top_left'),quote='Synthetic AIST')],limitations=['Синтетические QA-данные, не финансовые факты.'])
  save_fact(service,c,actor,p,pin,f);refs.append(dict(source_version_id=v['id'],fact_id=f['fact_id']))
body=dict(expected_revision=0,text='Synthetic D14 boundary',source_version_ids=[x['version']['id'] for x in sources[:2]],skill_version_id='skill_analytical-note-v3',model_profile_version_id='model_novita-mimo-v1',requested_result_type='analytical_note',parameters={'audience':None,'sections':[]},base_version_id=None,expected_artifact_revision=0,requested_export=None)
with engine.begin() as c:
 for count in (1,2,3):assert len(runs.eligibility(c,actor,p,body|{'source_version_ids':[x['version']['id'] for x in sources[:count]]})['pins'])==count
 for invalid in [body|{'skill_version_id':'skill_analytical-note-v2'},body|{'source_version_ids':[]},body|{'source_version_ids':[x['version']['id'] for x in sources]+[sources[0]['version']['id']]}]:
  try:runs.eligibility(c,actor,p,invalid)
  except ApiError as e:assert e.code=='source_limit'
  else:raise AssertionError('Invalid source count accepted')
records.append('one_two_three_supported_old_skill_and_invalid_counts_refused')
key=ident('key');status,submitted=runs.write(actor,session,p,conversation['id'],'submitMessage',key,body);assert status==202
r=runs.claim('d14_local_no_transport');assert r['id']==submitted['run']['id']
with patch('app.transport.Transport.perform',side_effect=AssertionError('Provider forbidden')):
 worker=SimpleNamespace(runs=runs,transport=None)
 facts,_=execution_facts(worker,r,'Synthetic',Path('/tmp'));assert len(facts)==2
 assert len({f['fact_id'] for f in facts})==1 and len({f['source']['source_version_id'] for f in facts})==2
 with engine.begin() as c:
  try:resolve_fact(service,c,r,refs[2])
  except ApiError as e:assert e.code=='resource_not_found'
  else:raise AssertionError('Unselected source accepted')
 records.append('same_fact_id_resolved_separately_and_real_unselected_fact_refused')
 payload=dict(result_type='analytical_note',schema_version='1.0.0',content=dict(title='Синтетическая записка',audience='QA',sections=[dict(section_id='section_qa',title='Проверка',blocks=[dict(block_id='block_qa',kind='paragraph',claim=dict(text='Синтетические основания из двух документов.',origin='source_fact',fact_refs=refs[:2],calculation_ids=[]))])]))
 args=dict(artifact_id=None,parent_version_id=None,expected_revision=0,idempotency_key=ident('key'),payload=payload)
 reply=runs.tool(r['id'],r['epoch'],'d14_save','aist_save_artifact',args);assert reply['ok'],reply
 saved=reply['result'];assert len(saved['version']['basis']['facts'])==2 and saved['review']['acceptance'] is None
 assert runs.tool(r['id'],r['epoch'],'d14_save','aist_save_artifact',args)==reply
 source=sources[0]['source'];old=sources[0]['version'];_,replacement=service.write(actor,'uploadSourceVersion',ident('key'),dict(title=source['title'],expected_revision=1),p,source['id'],pdf_bytes('changed'),'replacement.pdf')
 assert replacement['version']['sha256']!=old['sha256']
 repeated=runs.write(actor,session,p,conversation['id'],'submitMessage',key,body)[1]
 assert repeated['run']['result_version_id']==saved['version']['id']
 assert runs.version(actor,p,saved['version']['id'])==saved
 with engine.begin() as c:assert resolve_fact(service,c,r,refs[0])['data']['source']['sha256']==old['sha256']
 records.append('new_source_version_does_not_change_pinned_run_or_saved_basis_and_repeat')
 # Одноисточниковый короткий ответ, тот же механизм и отсутствие нового извлечения.
 short=body|dict(expected_revision=2,source_version_ids=[sources[1]['version']['id']],skill_version_id='skill_source-answer-v1',requested_result_type='evidence_answer')
 _,submitted=runs.write(actor,session,p,conversation['id'],'submitMessage',ident('key'),short);r=runs.claim('d14_local_short')
 with patch('app.openclaw_runtime.source_pages',side_effect=AssertionError('PDF re-extraction')):
  f,_=execution_facts(worker,r,'Synthetic',Path('/tmp'));assert len(f)==1
 answer=dict(result_type='evidence_answer',schema_version='1.0.0',content=dict(claims=[dict(text='Синтетический короткий ответ.',origin='source_fact',fact_refs=[refs[1]],calculation_ids=[])],missing_information=[]))
 reply=runs.tool(r['id'],r['epoch'],'d14_short','aist_save_artifact',args|{'payload':answer});assert reply['ok'],reply
 records.append('single_pdf_short_answer_saved_without_extraction_or_exports')
with engine.begin() as c:
 assert execute(c,'SELECT count(*) FROM aist.model_calls').scalar_one()==0
 assert execute(c,'SELECT count(*) FROM aist.exports').scalar_one()==0
 execute(c,'UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:id',id=session)
Path('/evidence/local-boundaries.json').write_text(json.dumps(dict(passed=True,cases=records,provider_posts=0),ensure_ascii=False,indent=2))
print(json.dumps(dict(passed=True,cases=records,provider_posts=0),ensure_ascii=False))
