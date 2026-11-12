"""F01/F02 через настоящий HTTP и общий инструмент, без модельного транспорта."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import httpx
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute
from app.service import Service,ident
from app.storage import Storage

out=Path('/evidence');s=json.loads((out/'private/state.json').read_text())
settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.json',lease_seconds=600)
engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings)
client=httpx.Client(base_url='http://web:8080/api',headers={'Authorization':'Bearer '+s['session']['token']},timeout=30)
base=f'/v1/projects/{s["project"]}';path=base+'/artifacts/'+s['artifact'];v1=client.get(base+'/artifact-versions/'+s['version']).json();records=[]
basis = v1['version']['basis']
calculation, = [calc for calc in basis['calculations'] if calc['formula_id'] == 'growth_percent.v1']
previous, = [item['fact'] for item in calculation['inputs'] if item['name'] == 'previous']
fact, = [item for item in basis['facts'] if item['fact_id'] == previous['fact_id']
         and item['source']['source_version_id'] == previous['source_version_id']]
revenue = fact['amount']['value']
wrong_revenue = str(Decimal(revenue) + 1)
grouped_revenue = format(int(revenue), ',').replace(',', ' ')
growth = calculation['rounded']['value'].replace('.', ',')
bad=[f'Выручка {revenue} млн $.',f'Выручка {revenue} млн €.',f'Выручка {revenue} тыс. ₽.',
 f'Выручка {revenue} миллиардов рублей.',f'Выручка {revenue} миллионов долларов.',f'Выручка {revenue} млн USD.',
 f'Выручка ${revenue} млн.',f'Выручка {revenue} млн GBP.',f'Выручка −{revenue} млн RUB.',f'Выручка {wrong_revenue} млн RUB.',
 f'Выручка {revenue} млн RUB за 1M’21.',f'Выручка {revenue} млн RUB за 9Q’21.',
 f'Выручка {revenue} млн RUB за 2021 год.',f'Выручка {revenue} млн RUB за три месяца 2021 года.',
 f'Выручка {revenue} млн RUB с 2021-01-01 по 2021-12-31.']
good=[f'Выручка {revenue} млн RUB.',f'Выручка {revenue} млн ₽.',f'Выручка {revenue} миллионов рублей.',
 f'Выручка ₽{revenue} млн.',f'Выручка +{grouped_revenue} млн. RUB за 9M’21.',
 f"Выручка {revenue} млн RUB за 9M'21.",f'Выручка {revenue} млн RUB за 9 месяцев 2021 года.',
 f'Выручка {revenue} млн RUB за девять месяцев 2021 года.',
 f'Выручка {revenue} млн RUB с 2021-01-01 по 2021-09-30.', f'Рост {growth}% за 9M’22 к 9M’21.']
unknown=[f'Выручка {revenue} туманов.',f'Выручка {revenue} млн RUB за 9Z’21.',f'Выручка {revenue}.', f'Выручка {revenue} млн RUB за FY2021.']

def counts():
 with engine.connect() as c:return {t:execute(c,f'SELECT count(*) FROM aist.{t}').scalar_one() for t in ('artifact_versions','version_acceptances','facts','model_calls')}
def head():return client.get(path).json()['revision']
def payload(text):
 p=deepcopy(v1['version']['payload']);claim=p['content']['sections'][1]['blocks'][0]['claim'];claim['text']=text
 if text.startswith('Рост'):
  claim['origin']='calculation';claim['fact_refs']=[];claim['calculation_ids']=[v1['version']['basis']['calculations'][0]['calculation_id']]
 return p
def http_save(text):
 return client.post(path+'/versions',json=dict(expected_revision=head(),parent_version_id=s['version'],payload=payload(text)),headers={'Idempotency-Key':ident('key')})
def record(kind,text,reply):
 records.append(dict(kind=kind,text=text,result=reply));print(kind,text,flush=True)
def validate_saved(value,status,origin):
 assert value['version']['basis']==v1['version']['basis']
 assert value['checks'][0]['status']==status and value['checks'][1]['status']=='not_checked'
 assert value['version']['provenance']['origin']==origin and value['review']['acceptance'] is None
 assert value['checks'][0]['profile_version']=='d11-r1-references-numbers.v1'
 if status=='failed':assert value['checks'][0]['findings'][0]['code']=='unsupported_numeric_form'
start=counts();assert start['model_calls']==0
with patch('app.transport.Transport.perform',side_effect=AssertionError('Модельный транспорт запрещен')):
 for text in bad:
  before=counts();r=http_save(text);assert r.status_code==422,(text,r.text);assert counts()==before
  record('http_rejected',text,r.json())
 for text in good+unknown:
  r=http_save(text);assert r.status_code==201,(text,r.text);v=r.json();validate_saved(v,'failed' if text in unknown else 'passed','human_edit')
  if text in unknown:
   rejected=client.post(path+'/versions/'+v['version']['id']+'/accept',json=dict(expected_revision=0,content_sha256=v['version']['content_sha256'],acknowledge_unchecked_semantics=True),headers={'Idempotency-Key':ident('key')})
   assert rejected.status_code==422
  record('http_unchecked' if text in unknown else 'http_passed',text,v)
 def model_run():
  conv=client.get(base+'/conversations/'+s['conversation']).json()
  body=dict(expected_revision=conv['revision'],text='Локальная регрессия проверки текста D11-R1',source_version_ids=[s['source']],skill_version_id='skill_analytical-note-v1',model_profile_version_id='model_novita-mimo-v1',requested_result_type='analytical_note',parameters={'audience':None,'sections':[]},base_version_id=s['version'],expected_artifact_revision=head(),requested_export=None)
  _,submission=runs.write(s['actor'],s['session']['session_id'],s['project'],s['conversation'],'submitMessage',ident('key'),body)
  run=runs.claim('d11_r1_controlled');assert run['id']==submission['run']['id'];return run
 def tool_save(run,text):
  args=dict(idempotency_key=ident('key'),expected_revision=run['input']['expected_artifact_revision'],artifact_id=s['artifact'],parent_version_id=s['version'],payload=payload(text))
  return runs.tool(run['id'],run['epoch'],ident('call'),'aist_save_artifact',args)
 run=model_run()
 for text in bad:
  before=counts();reply=tool_save(run,text);assert not reply['ok'] and reply['error']['code']=='validation_failed';assert counts()==before
  record('tool_rejected',text,reply)
 reply=tool_save(run,good[-1]);assert reply['ok'];validate_saved(reply['result'],'passed','model_edit');record('tool_passed',good[-1],reply)
 run=model_run();reply=tool_save(run,unknown[0]);assert reply['ok'];validate_saved(reply['result'],'failed','model_edit');record('tool_unchecked',unknown[0],reply)
assert client.get(base+'/artifact-versions/'+s['version']).json()==v1
finish=counts();assert finish['model_calls']==0 and finish['facts']==start['facts']
(out/'evidence/r1-numeric.json').write_text(json.dumps(dict(passed=True,cases=records,before=start,after=finish,original_v1_unchanged=True,provider_posts=0),ensure_ascii=False,indent=2))
print('F01/F02 passed: HTTP, tool, unknown findings, original basis, provider POST 0')
