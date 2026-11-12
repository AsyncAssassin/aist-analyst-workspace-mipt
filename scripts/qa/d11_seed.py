"""Явный локальный QA-импорт сохраненного D10 Whoosh. Никакой генерации."""
import json,sys,subprocess,hashlib
from pathlib import Path
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute,json_value
from app.service import Service,ident
from app.storage import Storage,process_pdf
from app.facts import save_fact
from app.normalization import fingerprint

settings=Settings(executor_mode='openclaw',live_models_enabled=True,policy_path='/workspace/executor/openclaw/policy.json')
engine=make_engine(settings);service=Service(engine,Storage(settings.storage_root));runs=Runs(service,settings)
user=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D11 Whoosh QA snapshot']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',user['user_id']]))
actor=user['user_id']
_,project=service.write(actor,'createProject',ident('key'),{'name':'D11 QA: сохраненный Whoosh из D10'})
p=project['id'];_,conv=service.write(actor,'createConversation',ident('key'),{'title':'Редактор Whoosh, QA-снимок'},project=p)
snapshot=json.loads(Path('/inputs/version.json').read_text());v=snapshot['version'];v['project_id']=p
pin=v['provenance']['source_versions'][0];pdf=Path('/inputs/whoosh.pdf').read_bytes()
assert hashlib.sha256(pdf).hexdigest()==pin['sha256']
assert fingerprint({k:v[k] for k in ('payload','basis')})==v['content_sha256']
parsed=process_pdf(pdf);service.storage.publish(pin['source_version_id'],pdf)
with engine.begin() as c:
    execute(c,'''INSERT INTO aist.sources(id,project_id,owner_id,title,latest_version_id,revision)
        VALUES (:id,:p,:a,'Whoosh, QA-снимок D10',:v,1)''',id=pin['source_id'],p=p,a=actor,v=pin['source_version_id'])
    execute(c,'''INSERT INTO aist.source_versions(id,source_id,project_id,owner_id,revision,sha256,byte_size,original_filename,storage_name,processor_version,page_count,processing_state)
        VALUES (:v,:s,:p,:a,1,:sha,:size,'Whoosh_презентация инвесторам.pdf',:storage,:processor,:pages,'ready')''',
        v=pin['source_version_id'],s=pin['source_id'],p=p,a=actor,sha=pin['sha256'],size=len(pdf),storage=pin['source_version_id']+'.pdf',processor=parsed['processor_version'],pages=len(parsed['pages']))
    for i,text in enumerate(parsed['pages'],1):execute(c,'INSERT INTO aist.source_pages(version_id,sha256,page,text) VALUES (:v,:sha,:i,:text)',v=pin['source_version_id'],sha=pin['sha256'],i=i,text=text)
    for f in v['basis']['facts']:
        save_fact(service,c,actor,p,pin,f,calculation_basis={'definition':'Выручка','compatibility':'D10 Whoosh, одинаковые интервалы МСФО'} if f['metric']=='Выручка' else {})
    execute(c,'''INSERT INTO aist.artifacts(id,project_id,owner_id,result_type,latest_version_id,revision)
        VALUES (:id,:p,:a,'analytical_note',:v,1)''',id=v['artifact_id'],p=p,a=actor,v=v['id'])
    execute(c,'''INSERT INTO aist.artifact_versions(id,artifact_id,project_id,owner_id,revision,data,view)
        VALUES (:v,:id,:p,:a,1,CAST(:d AS jsonb),CAST(:view AS jsonb))''',v=v['id'],id=v['artifact_id'],p=p,a=actor,d=json_value(v),view=json_value(snapshot))
body=dict(expected_revision=0,text='QA-импорт сохраненной записки Whoosh из D10, без новой генерации.',source_version_ids=[pin['source_version_id']],skill_version_id='skill_analytical-note-v1',model_profile_version_id='model_novita-mimo-v1',requested_result_type='analytical_note',parameters={'audience':None,'sections':[]},base_version_id=None,expected_artifact_revision=0,requested_export=None)
_,submission=runs.write(actor,session['session_id'],p,conv['id'],'submitMessage',ident('key'),body)
r=runs.claim('d11_seed')
with engine.begin() as c:
    r=runs.locked(c,r['id']);runs.append_message(c,r,'assistant','QA-снимок Whoosh из сохраненной D10, не новая генерация.',v['id']);runs.transition(c,r,'completed',result=v['id']);runs.event(c,r,'result_saved',{'artifact_version_id':v['id']})
state=dict(project=p,conversation=conv['id'],artifact=v['artifact_id'],version=v['id'],source=pin['source_version_id'],actor=actor,session=session,run=r['id'])
Path('/evidence/private').mkdir(exist_ok=True,mode=0o700);out=Path('/evidence/private/state.json');out.write_text(json.dumps(state));out.chmod(0o600)
Path('/evidence/seed.json').write_text(json.dumps({k:state[k] for k in state if k!='session'}|dict(qa_snapshot=True,content_sha256=v['content_sha256'],source_sha256=pin['sha256'],provider_posts=0),ensure_ascii=False,indent=2))
print('D11 QA snapshot imported; provider POST 0')
