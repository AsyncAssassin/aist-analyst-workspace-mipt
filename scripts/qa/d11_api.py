"""D11: настоящий HTTP/PG, отдельный локальный исполнитель, provider POST запрещен."""
import json,uuid,subprocess,sys,socket,time
from pathlib import Path
from copy import deepcopy
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import httpx
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute,json_value,row
from app.service import Service,ident
from app.storage import Storage
from app.executor import OpenClawExecutor
from app.openclaw_runtime import execution_message
from app.normalization import fingerprint
from app.errors import ApiError

out=Path('/evidence');state=json.loads((out/'private/state.json').read_text());p=state['project'];a=state['artifact'];actor=state['actor'];session=state['session']
settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.json',lease_seconds=600)
engine=make_engine(settings);service=Service(engine,Storage(settings.storage_root));runs=Runs(service,settings)
base=f'/v1/projects/{p}';versions=f'{base}/artifacts/{a}/versions';records=[]

def api(method,path,body=None,key=None,status=200,token=None):
    r=httpx.request(method,'http://web:8080/api'+path,json=body,headers={'Authorization':'Bearer '+(token or session['token']),'Idempotency-Key':key or uuid.uuid4().hex},timeout=30)
    assert r.status_code==status,(path,r.status_code,r.text[:400])
    return r.json()

def check(name,**info):
    records.append(dict(case=name,passed=True,**info));print(name,flush=True)
    (out/'evidence/api-progress.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))

def head():return api('GET',f'{base}/artifacts/{a}')['revision']
def get(vid):return api('GET',f'{base}/artifact-versions/{vid}')
v1=get(state['version'])
def body(payload=None,parent=None):return dict(expected_revision=head(),parent_version_id=parent or state['version'],payload=deepcopy(payload or v1['version']['payload']))
def counts():
    with engine.connect() as c:return {t:execute(c,f'SELECT count(*) FROM aist.{t}').scalar_one() for t in ('artifact_versions','version_acceptances','model_calls','tool_calls','facts')}
start=counts()
assert start['model_calls']==0
# Конкурирующие запросы с одним expected_revision.
b1=body();b2=deepcopy(b1);b1['payload']['content']['audience']='Редактор один';b2['payload']['content']['audience']='Редактор два'
def raw(b):return httpx.post('http://web:8080/api'+versions,json=b,headers={'Authorization':'Bearer '+session['token'],'Idempotency-Key':uuid.uuid4().hex},timeout=30)
with ThreadPoolExecutor(2) as pool:responses=list(pool.map(raw,[b1,b2]))
assert sorted(r.status_code for r in responses)==[201,409]
check('concurrent_save_one_201_one_409')
# Потеря ответа после durable commit: отправляем запрос, не читаем ответ, закрываем socket.
b=body();key=uuid.uuid4().hex;raw_body=json.dumps(b,ensure_ascii=False).encode();sock=socket.create_connection(('backend',8000))
sock.sendall((f'POST {versions} HTTP/1.1\r\nHost: backend\r\nAuthorization: Bearer {session["token"]}\r\nIdempotency-Key: {key}\r\nContent-Type: application/json\r\nContent-Length: {len(raw_body)}\r\nConnection: close\r\n\r\n').encode()+raw_body)
for _ in range(100):
    with engine.connect() as c:receipt=service.receipt(c,dict(actor=actor,project_scope=p,operation='saveVersion',target=a,key=key))
    if receipt:break
    time.sleep(.05)
assert receipt;sock.close();before=counts();replay=api('POST',versions,b,key=key)
assert replay['version']['id']==receipt['artifact_version_id'] and counts()==before
changed=deepcopy(b);changed['payload']['content']['audience']='Другой payload'
assert api('POST',versions,changed,key=key,status=409)['error']['code']=='idempotency_conflict'
assert api('POST',versions,b,status=409)['error']['code']=='revision_conflict'
check('lost_response_exact_replay_and_key_conflict',version=replay['version']['id'])
# Проверка полных и вложенных чтений, пагинации и неверного cursor.
items=api('GET',versions+'?limit=2');assert len(items['items'])==2 and items['next_after_id']
nextpage=api('GET',versions+'?limit=2&after_id='+items['next_after_id']);assert nextpage['items'][0]['revision']==3
assert api('GET',versions+'/'+state['version'])==v1
api('GET',versions+'?after_id=av_absent',status=409)
check('list_get_lookup_pagination')
# Другой owner, project и parent той же учетной записи.
foreign=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D11 isolation QA']))
foreign_session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',foreign['user_id']]))
api('GET',base+'/artifact-versions/'+state['version'],token=foreign_session['token'],status=404)
api('POST',versions,body(),token=foreign_session['token'],status=404)
_,project2=service.write(actor,'createProject',ident('key'),{'name':'D11 QA foreign project'})
api('POST',f'/v1/projects/{project2["id"]}/artifacts/{a}/versions',body(),status=404)
# Контрольный второй Artifact и версия с ошибкой, только QA fixture.
clone=deepcopy(v1);clone_id=ident('artifact');clone_vid=ident('av');clone['version'].update(id=clone_vid,artifact_id=clone_id)
clone['review']['artifact_version_id']=clone_vid
for i,ch in enumerate(clone['checks']):ch.update(id=ident('check'),artifact_version_id=clone_vid)
clone['review']['validation_ids']=[x['id'] for x in clone['checks']]
clone['checks'][0].update(status='failed',findings=[dict(code='qa_injected_error',severity='error',block_id=None,message='Контрольный отказ принятия')])
with engine.begin() as c:
    execute(c,"INSERT INTO aist.artifacts(id,project_id,owner_id,result_type,latest_version_id,revision) VALUES (:a,:p,:o,'analytical_note',:v,1)",a=clone_id,p=p,o=actor,v=clone_vid)
    execute(c,'INSERT INTO aist.artifact_versions(id,artifact_id,project_id,owner_id,revision,data,view) VALUES (:v,:a,:p,:o,1,CAST(:d AS jsonb),CAST(:view AS jsonb))',v=clone_vid,a=clone_id,p=p,o=actor,d=json_value(clone['version']),view=json_value(clone))
api('POST',versions,body(parent=clone_vid),status=404)
api('GET',versions+'/'+clone_vid,status=404)
check('owner_project_parent_isolation')
# Подмена чисел, знаков, единиц, периода и provenance.
value_from_snapshot = v1['version']['payload']['content']['sections'][1]['blocks'][1]['rows'][0]['amount']['value']
different_value = str(Decimal(value_from_snapshot) + 1)
for name,field,value,index in [('number','value',different_value,0),('sign','value','-'+value_from_snapshot,0),('unit','currency','USD',0),('scale','scale','1',0)]:
    bad=body();bad['payload']['content']['sections'][1]['blocks'][1]['rows'][index]['amount'][field]=value
    before=counts();api('POST',versions,bad,status=422);assert counts()==before;check('reject_'+name)
for index in (0,2):
    bad=body();bad['payload']['content']['sections'][1]['blocks'][1]['rows'][index]['period_label']='9M’23'
    api('POST',versions,bad,status=422)
check('reject_fact_and_calculation_period')
for text in (f'Выручка -{value_from_snapshot} млн RUB.', f'Выручка {value_from_snapshot} млн USD.',
             f'Выручка {value_from_snapshot} руб.', f'Выручка {value_from_snapshot} млн RUB за 2023 год.'):
    bad=body();block=bad['payload']['content']['sections'][1]['blocks'][0];block['claim']['text']=text
    api('POST',versions,bad,status=422)
check('reject_prose_sign_unit_scale_period')
b=body();b['payload']['content']['sections'][0]['blocks'][0]['claim']['text']+=' Добавлено редактором.'
edit=api('POST',versions,b,status=201);assert edit['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['origin']=='human_editorial'
assert edit['version']['provenance']['actor_id']==actor
check('server_marks_changed_paragraph_human')
b=body();b['payload']['content']['sections'][0]['blocks'].append(dict(block_id='block_unconfirmed',kind='paragraph',claim=dict(text='Будущая выручка 999 млн RUB.',origin='human_editorial',fact_refs=[],calculation_ids=[])))
unchecked=api('POST',versions,b,status=201);assert unchecked['checks'][0]['status']=='failed';assert unchecked['checks'][0]['findings'][0]['block_id']=='block_unconfirmed'
check('unconfirmed_numbers_finding',version=unchecked['version']['id'])
# Принятие привязано к review конкретной версии; ошибочный SHA и errors не принимаются.
def accept_body(v,ack=True):return dict(expected_revision=v['review']['revision'],content_sha256=v['version']['content_sha256'],acknowledge_unchecked_semantics=ack)
def accept_path(v):return f'{base}/artifacts/{v["version"]["artifact_id"]}/versions/{v["version"]["id"]}/accept'
api('POST',accept_path(edit),dict(accept_body(edit),content_sha256='0'*64),status=422)
api('POST',accept_path(edit),accept_body(edit,False),status=422)
api('POST',accept_path(edit),dict(accept_body(edit),expected_revision=8),status=409)
api('POST',accept_path(unchecked),accept_body(unchecked),status=422)
api('POST',accept_path(clone),accept_body(clone),status=422)
key=uuid.uuid4().hex;accepted=api('POST',accept_path(edit),accept_body(edit),key=key);before=counts()
assert api('POST',accept_path(edit),accept_body(edit),key=key)==accepted
assert api('POST',accept_path(edit),accept_body(edit))==accepted and counts()==before
assert accepted['acceptance']['validation_ids']==edit['review']['validation_ids']
api('POST',accept_path(edit),dict(accept_body(edit),acknowledge_unchecked_semantics=False),key=key,status=409)
check('acceptance_sha_checks_ack_revision_exact_replay',acceptance=accepted)
# Исторический parent при актуальной голове разрешен.
historical=api('POST',versions,body(),status=201);assert historical['version']['parent_version_id']==state['version']
assert historical['review']['acceptance'] is None
check('historical_parent_current_head')
# Управляемый исполнитель через штатную границу OpenClawExecutor, без модели/нового цикла.
class ControlledGateway:
    def request(self,method,args):
        assert method=='agent';self.input=json.loads(args['message'].split('\n\n')[-1]);return {'runId':'controlled_'+uuid.uuid4().hex}

def model_run():
    revision=api('GET',base+'/conversations/'+state['conversation'])['revision']
    request=dict(expected_revision=revision,text='Локальная проверка продолжения выбранной исторической версии',source_version_ids=[state['source']],skill_version_id='skill_analytical-note-v1',model_profile_version_id='model_novita-mimo-v1',requested_result_type='analytical_note',parameters={'audience':'Комитет','sections':[]},base_version_id=state['version'],expected_artifact_revision=head(),requested_export=None)
    _,submission=runs.write(actor,session['session_id'],p,state['conversation'],'submitMessage',ident('key'),request)
    run=runs.claim('d11_controlled');assert run['id']==submission['run']['id']
    gateway=ControlledGateway();adapter=OpenClawExecutor(gateway,runs.attach_executor)
    adapter.start(run,execution_message(runs,run,request['text'],[]))
    assert gateway.input['base_version']==v1['version']
    assert gateway.input['expected_artifact_revision']==run['input']['expected_artifact_revision']
    payload=deepcopy(gateway.input['base_version']['payload']);payload['content']['audience']='Комитет, локальный управляемый исполнитель'
    args=dict(idempotency_key=ident('key'),expected_revision=run['input']['expected_artifact_revision'],artifact_id=a,parent_version_id=state['version'],payload=payload)
    return run,args,gateway
r,args,gateway=model_run()
with patch('app.transport.Transport.perform',side_effect=AssertionError('Provider POST запрещен')):
    reply=runs.tool(r['id'],r['epoch'],'d11_model_save','aist_save_artifact',args)
assert reply['ok'],reply
model=reply['result'];assert model['version']['provenance']['origin']=='model_edit';assert model['version']['parent_version_id']==state['version'];assert model['review']['acceptance'] is None
assert runs.tool(r['id'],r['epoch'],'d11_model_save','aist_save_artifact',args)==reply
(out/'evidence/model-input.json').write_text(json.dumps(gateway.input,ensure_ascii=False,indent=2))
(out/'evidence/model-version.json').write_text(json.dumps(model,ensure_ascii=False,indent=2))
check('selected_snapshot_reaches_executor_model_edit_saved',version=model['version']['id'])
r,args,_=model_run();human=api('POST',versions,body(),status=201)
reply=runs.tool(r['id'],r['epoch'],'d11_stale_save','aist_save_artifact',args);assert not reply['ok'] and reply['error']['code']=='revision_conflict'
assert head()==human['version']['revision'];assert runs.get(actor,p,r['id'])['result_version_id'] is None
try:runs.tool(r['id'],r['epoch'],'d11_accept_attempt','aist_accept_version',{})
except ApiError as e:assert e.code=='tool_unavailable'
else:raise AssertionError('Модель получила инструмент принятия')
runs.write(actor,session['session_id'],p,r['id'],'cancelRun',ident('key'),{'expected_revision':runs.get(actor,p,r['id'])['revision']})
check('human_head_change_conflicts_with_model')
assert get(state['version'])==v1
finish=counts();assert finish['model_calls']==0 and finish['facts']==start['facts']
check('source_snapshot_and_facts_preserved_provider_posts_zero',before=start,after=finish)
(out/'evidence/api-result.json').write_text(json.dumps(dict(passed=True,cases=records,provider_posts=0),ensure_ascii=False,indent=2))
