"""Целевая HTTP/DB-проверка D13, транспорт модели запрещен."""
import json,uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute,json_value
from app.service import Service,ident
from app.storage import Storage
from app.openclaw_runtime import execution_message,execution_facts,tool_schema
from app.live_profile import verify_pins
from app.normalization import fingerprint
from jsonschema import Draft202012Validator

root=Path('/evidence');state=json.loads(Path('/private/state.json').read_text());ui=json.loads((root/'evidence/preflight-ui/result.json').read_text())
p,a,actor=state['project'],state['artifact'],state['actor'];session=state['session']
settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.d13.json',lease_seconds=600)
engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings)
client=httpx.Client(base_url='http://backend:8000',headers={'Authorization':'Bearer '+session['token']},timeout=30)
base=f'/v1/projects/{p}';records=[]
def api(method,path,body=None,status=200,key=None):
 r=client.request(method,path,json=body,headers={'Idempotency-Key':key or uuid.uuid4().hex})
 assert r.status_code==status,(path,r.status_code,r.text[:500]);return r.json()
def record(name,**info):
 records.append(dict(case=name,passed=True,**info));print(name,flush=True)
def counts():
 with engine.connect() as c:return {t:execute(c,f'SELECT count(*) FROM aist.{t}').scalar_one() for t in ('artifact_versions','version_acceptances','exports','facts','model_calls')}
def get(v):return api('GET',base+'/artifact-versions/'+v)
def head():return api('GET',base+'/artifacts/'+a)['revision']
path=ui['requests'][0]['path'];body=ui['requests'][0]['body'];selected=get(ui['chosen']);initial=counts()
assert initial['model_calls']==0 and initial['artifact_versions']==10
r=runs.claim('d13_local_controlled');assert r['id']==ui['noteSubmission']['run']['id']
verify_pins(r['configuration'],r['input'])
with patch('app.transport.Transport.perform',side_effect=AssertionError('Paid transport forbidden')):
 worker=SimpleNamespace(runs=runs,transport=SimpleNamespace(perform=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Extraction forbidden'))))
 facts,parent_call=execution_facts(worker,r,body['text'],Path('/tmp'))
 assert facts==selected['version']['basis']['facts'] and parent_call is None
 message=json.loads(execution_message(runs,r,body['text'],facts).split('\n\n')[-1]);assert message['base_version']==selected['version'] and message['parameters']==body['parameters']
 (root/'evidence/preflight-executor-input.json').write_text(json.dumps(message,ensure_ascii=False,indent=2))
 record('ui_selected_v2_and_parameters_reach_executor_head_is_10')
 args=dict(artifact_id=a,parent_version_id=selected['version']['id'],expected_revision=10,idempotency_key=ident('key'),payload=deepcopy(selected['version']['payload']))
 args['payload']['content']['audience']='Руководитель, локальная проверка'
 Draft202012Validator(tool_schema('aist_save_artifact','analytical_note')).validate(args)
 def tool(data,label):return runs.tool(r['id'],r['epoch'],'d13_'+label,'aist_save_artifact',data)
 table=next(b for s in args['payload']['content']['sections'] for b in s['blocks'] if b['kind']=='table')
 for label,field,value in [('number','value','3703'),('scale','scale','1'),('unit','currency','USD')]:
  bad=deepcopy(args);b=next(b for s in bad['payload']['content']['sections'] for b in s['blocks'] if b['kind']=='table');b['rows'][0]['amount'][field]=value
  reply=tool(bad,label);assert not reply['ok'] and counts()==initial
 bad=deepcopy(args);next(b for s in bad['payload']['content']['sections'] for b in s['blocks'] if b['kind']=='table')['rows'][0]['period_label']='9M’23'
 assert not tool(bad,'period')['ok']
 record('save_rejects_changed_numbers_scale_currency_period')
 reply=tool(args,'valid');assert reply['ok'],reply
 saved=reply['result'];new=saved['version'];assert new['parent_version_id']==selected['version']['id'] and new['revision']==11 and new['provenance']['origin']=='model_edit'
 assert new['basis']==selected['version']['basis'] and new['payload']['content']['sections']==selected['version']['payload']['content']['sections']
 assert saved['review']['acceptance'] is None and get(selected['version']['id'])==selected
 assert tool(args,'valid')==reply
 repeated=api('POST',path,body,key=ui['requests'][0]['key']);assert repeated['run']['result_version_id']==new['id']
 record('same_artifact_parent_origin_ids_basis_acceptance_and_idempotency',version=new['id'])
 def request(**updates):
  b=deepcopy(body);b.update(expected_revision=api('GET',path.removesuffix('/messages'))['revision'],expected_artifact_revision=head());b.update(updates);return b
 stale=request(expected_artifact_revision=10)
 assert api('POST',path,stale,status=409)['error']['code']=='revision_conflict'
 mismatch=request(base_version_id=None,expected_artifact_revision=0,requested_result_type='evidence_answer')
 assert api('POST',path,mismatch,status=409)['error']['code']=='tool_unavailable'
 record('stale_head_and_skill_result_mismatch_rejected')
 # Существующая версия в другом проекте, только локальные контрольные данные.
 _,foreign_project=runs.service.write(actor,'createProject',ident('key'),{'name':'D13 foreign-version QA'})
 clone=deepcopy(selected);aid=ident('artifact');vid=ident('av');clone['version'].update(id=vid,artifact_id=aid,project_id=foreign_project['id'],revision=1,parent_version_id=None)
 clone['review'].update(artifact_version_id=vid,acceptance=None,revision=0)
 for ch in clone['checks']:ch.update(id=ident('check'),artifact_version_id=vid)
 clone['review']['validation_ids']=[ch['id'] for ch in clone['checks']]
 with engine.begin() as c:
  execute(c,"INSERT INTO aist.artifacts(id,project_id,owner_id,result_type,latest_version_id,revision) VALUES (:id,:p,:a,'analytical_note',:v,1)",id=aid,p=foreign_project['id'],a=actor,v=vid)
  execute(c,'INSERT INTO aist.artifact_versions(id,artifact_id,project_id,owner_id,revision,data,view) VALUES (:v,:id,:p,:a,1,CAST(:d AS jsonb),CAST(:view AS jsonb))',v=vid,id=aid,p=foreign_project['id'],a=actor,d=json_value(clone['version']),view=json_value(clone))
 assert api('POST',path,request(base_version_id=vid),status=404)['error']['code']=='resource_not_found'
 record('actual_foreign_project_version_rejected')
 # Голова меняется после постановки Run, до сохранения исполнителем.
 queued=api('POST',path,request(),status=202);race=runs.claim('d13_local_controlled');assert race['id']==queued['run']['id']
 manual=dict(expected_revision=head(),parent_version_id=selected['version']['id'],payload=deepcopy(selected['version']['payload']))
 manual['payload']['content']['audience']='Локальная конкурентная правка'
 api('POST',base+'/artifacts/'+a+'/versions',manual,status=201)
 stale_args=dict(args,expected_revision=race['input']['expected_artifact_revision'])
 failure=runs.tool(race['id'],race['epoch'],'d13_stale_save','aist_save_artifact',stale_args);assert not failure['ok'] and failure['error']['code']=='revision_conflict'
 api('POST',base+'/runs/'+race['id']+'/cancel',{'expected_revision':runs.get(actor,p,race['id'])['revision']})
 record('concurrent_human_revision_blocks_model_save')
 # Отдельный короткий ответ: сохраненные факты, без полного чтения PDF и извлечения.
 answer_body=request(base_version_id=None,expected_artifact_revision=0,skill_version_id='skill_source-answer-v1',requested_result_type='evidence_answer',parameters={'audience':None,'sections':[]},text='Локальная проверка определения EBITDA')
 short=api('POST',path,answer_body,status=202);r=runs.claim('d13_local_controlled');assert r['id']==short['run']['id']
 with patch('app.openclaw_runtime.source_pages',side_effect=AssertionError('Full PDF read forbidden')):
  facts,call=execution_facts(worker,r,answer_body['text'],Path('/tmp'))
 assert call is None and len(facts)==5
 margin, = [f for f in facts if f['amount'] and f['amount']['unit'] == 'percent' and 'ebitda' in f['metric'].lower()]
 footnote, = [f for f in facts if not f['amount'] and 'кикшеринг' in f['raw_text'].lower()]
 margin_text = f"Рентабельность EBITDA {margin['amount']['value'].replace('.', ',')}%."
 definition = footnote['evidence'][0]['quote']
 refs=[{'source_version_id':f['source']['source_version_id'],'fact_id':f['fact_id']} for f in (margin,footnote)]
 answer=dict(result_type='evidence_answer',schema_version='1.0.0',content=dict(claims=[dict(text=f'{margin_text} {definition}',origin='source_fact',fact_refs=refs,calculation_ids=[])],missing_information=[]))
 aa=dict(artifact_id=None,parent_version_id=None,expected_revision=0,idempotency_key=ident('key'),payload=answer)
 Draft202012Validator(tool_schema('aist_save_artifact','evidence_answer')).validate(aa)
 short_saved=runs.tool(r['id'],r['epoch'],'d13_short_save','aist_save_artifact',aa);assert short_saved['ok'],short_saved
 assert short_saved['result']['version']['payload']['result_type']=='evidence_answer'
 final=counts();assert final['exports']==initial['exports'] and final['facts']==initial['facts'] and final['model_calls']==0
 assert get(selected['version']['id'])==selected
 record('short_answer_existing_facts_no_extraction_no_exports_no_provider',version=short_saved['result']['version']['id'])
(root/'evidence/preflight-db.json').write_text(json.dumps(dict(passed=True,synthetic=True,provider_posts=0,initial=initial,final=final,cases=records),ensure_ascii=False,indent=2))
