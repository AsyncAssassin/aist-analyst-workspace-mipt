"""Целевые D12 HTTP/DB границы на отдельной БД; worker остановлен для fault injection."""
import concurrent.futures
from copy import deepcopy
import hashlib,json,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import httpx
from app.config import Settings
from app.database import make_engine
from app.service import Service,ident,row
from app.storage import Storage
from app.runs import Runs,execute,json_value
from app.exports import Exports
from app.errors import ApiError
from app.note_renderer import RENDERERS, render as real_render

s=json.loads(Path('/evidence/private/state.json').read_text());browser=json.loads(Path('/evidence/evidence/browser/result.json').read_text())
settings=Settings();engine=make_engine(settings);service=Service(engine,Storage(settings.storage_root));runs=Runs(service,settings);exports=Exports(runs)
client=httpx.Client(base_url='http://backend:8000',headers={'Authorization':'Bearer '+s['session']['token']},timeout=30)
p=f"/v1/projects/{s['project']}";checks=[]
def record(name,**data):checks.append(dict(name=name,**data));print(name);Path('/evidence/evidence/api-result.json').write_text(json.dumps(dict(passed=False,checks=checks),ensure_ascii=False,indent=2))
def post(path,body,key=None,status=202):
 r=client.post(path,json=body,headers={'Idempotency-Key':key or ident('key')});assert r.status_code==status,(r.status_code,r.text);return r.json()
def url(v):return p+f"/artifacts/{s['artifact']}/versions/{v}/exports"
def request(v,fmt='docx',theme='standard',mode='draft',revision=0):
 return dict(expected_revision=revision,format=fmt,template_version='aist-note.v1',renderer_version=RENDERERS[fmt],settings=dict(locale='ru-RU',paper='A4',theme=theme,release_mode=mode))
def get(e):return client.get(p+'/exports/'+e['id']).json()
def saved_version(title):
 head=client.get(p+'/artifacts/'+s['artifact']).json();v=client.get(p+'/artifact-versions/'+head['latest_version_id']).json()['version'];payload=deepcopy(v['payload']);payload['content']['audience']=title
 return post(p+'/artifacts/'+s['artifact']+'/versions',dict(expected_revision=head['revision'],parent_version_id=v['id'],payload=payload),status=201)['version']['id']
def process():exports.process_one()
def count(e):
 with engine.connect() as c:return row(c,'SELECT render_count FROM aist.exports WHERE id=:id',id=e['id'])['render_count']

v2=browser['v2'];v3=browser['v3'];body=request(v2,theme='compact',mode='accepted',revision=1);key=ident('key')
e=post(url(v2),body,key);same=post(url(v2),body,key,status=200);assert e['id']==same['id'];same=post(url(v2),body,status=200);assert e['id']==same['id'];record('same_key_and_new_key_reuse_identity',export=e['id'])
bad=deepcopy(body);bad['settings']['theme']='standard';post(url(v2),bad,key,status=409);record('changed_payload_same_key_conflicts')
stale=deepcopy(body);stale['expected_revision']=0;post(url(v2),stale,status=409);record('new_key_requires_current_review_revision')
concurrent_body=request(v2,'pdf','compact','accepted',1)
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
 results=list(pool.map(lambda _:client.post(url(v2),json=concurrent_body,headers={'Idempotency-Key':ident('key')}),range(6)))
assert sorted(r.status_code for r in results)==[200,200,200,200,200,202];assert len({r.json()['id'] for r in results})==1;record('concurrent_identity_has_one_export',statuses=[r.status_code for r in results])
process();process();assert get(e)['state']=='ready';before=count(e)
for i in range(3):
 r=client.get(p+'/exports/'+e['id']+'/file');assert r.status_code==200;assert hashlib.sha256(r.content).hexdigest()==get(e)['file']['sha256'];assert r.headers['etag']=='"sha256:'+get(e)['file']['sha256']+'"'
assert count(e)==before==1;record('repeated_download_saved_bytes_renderer_count_one')
post(url(v3),request(v3,mode='accepted'),status=409);record('unaccepted_version_cannot_release_accepted')
for field,value,code in [('format','xlsx','unsupported_format'),('template_version','other.v1','incompatible_template'),('renderer_version','other.v1','incompatible_template')]:
 b=request(v2,revision=1);b[field]=value;r=post(url(v2),b,status=422);assert r['error']['code']==code
record('unsupported_combinations_rejected')
other=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D12 foreign owner']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',other['user_id']]))
foreign=httpx.Client(base_url='http://backend:8000',headers={'Authorization':'Bearer '+session['token']},timeout=30)
assert foreign.get(p+'/exports/'+e['id']).status_code==404;assert foreign.get(p+'/exports/'+e['id']+'/file').status_code==404
assert foreign.post(url(v2),json=body,headers={'Idempotency-Key':ident('key')}).status_code==404
_,otherproject=service.write(s['actor'],'createProject',ident('key'),{'name':'D12 другой проект'})
assert client.get(f"/v1/projects/{otherproject['id']}/exports/{e['id']}/file").status_code==404
record('foreign_owner_and_project_denied')
selected=exports.selected
for kind in ['sha','acceptance']:
 def tampered(*args):
  v=selected(*args)
  if kind=='sha':v['version']['content_sha256']='0'*64
  else:v['review']['acceptance']['artifact_version_id']=v3
  return v
 with patch.object(exports,'selected',side_effect=tampered):
  try:exports.request(s['actor'],s['session']['session_id'],s['project'],s['artifact'],v2,ident('key'),body)
  except ApiError as error:assert error.code==('validation_failed' if kind=='sha' else 'acceptance_required')
  else:raise AssertionError('Invalid pin accepted')
record('incorrect_content_sha_and_acceptance_pin_denied')
# Missing/corrupt operate on extra exports, never the browser control files.
for format,problem,expected in [('docx','missing',410),('pdf','corrupt',409)]:
 target=e if format=='docx' else results[0].json();original=get(target);path=exports.files.path(original)
 backup=Path('/evidence/evidence')/('retained-'+target['id']+'.'+format);backup.write_bytes(path.read_bytes())
 if problem=='missing':path.unlink()
 else:path.write_bytes(b'intentional QA corruption')
 r=client.get(p+'/exports/'+target['id']+'/file');assert r.status_code==expected
 current=get(target);assert current['state']==problem and current['file']==original['file']
 retry=post(url(v2),body if format=='docx' else concurrent_body,status=200);assert retry['state']==problem and retry['id']==target['id'];assert count(target)==1
 record(problem+'_descriptor_preserved_no_rebuild',export=target['id'],http_status=expected,render_count=count(target))
# Renderer failure after both candidate files are written, durable failed, own cleanup only.
for fail_cleanup in [False,True]:
 version=saved_version('Контроль отказа удаления' if fail_cleanup else 'Контроль отказа рендера');target=post(url(version),request(version))
 def fail(*args):real_render(*args);raise RuntimeError('Injected renderer failure after candidate')
 with patch('app.exports.render',side_effect=fail):
  if fail_cleanup:
   with patch.object(exports.files,'cleanup',side_effect=OSError('Injected unlink failure')):process()
  else:process()
 current=get(target);assert current['state']=='failed';assert post(url(version),request(version),status=200)['id']==target['id'];assert count(target)==1
 with engine.connect() as c:cleanup=row(c,'SELECT cleanup FROM aist.exports WHERE id=:id',id=target['id'])['cleanup']
 assert cleanup['state']==('error' if fail_cleanup else 'removed');assert (exports.files.path(target,True)/'note.docx').exists()==fail_cleanup
 record('cleanup_error_separate' if fail_cleanup else 'renderer_failure_confirmed_cleanup',export=target['id'],cleanup=cleanup)
# Simulate lost commit acknowledgement at final ready, both before and after durable commit.
class CommitFault:
 def __init__(self,c,after):self.c=c;self.n=0;self.after=after
 def __getattr__(self,k):return getattr(self.c,k)
 def commit(self):
  self.n+=1
  # produce: pid commit, read commit, candidate commit, final ready commit.
  if self.n==4:
   if self.after:self.c.commit()
   raise RuntimeError('Injected lost commit acknowledgement')
  return self.c.commit()
for after in [False,True]:
 version=saved_version('Сверка подтвержденной фиксации' if after else 'Сверка неизвестного исхода');target=post(url(version),request(version,'pdf'))
 with engine.connect() as c:
  lock,owned=exports.lock(c,target['id']);assert owned;c.commit()
  exports.save(c,target,state='rendering');execute(c,'UPDATE aist.exports SET render_count=render_count+1 WHERE id=:id',id=target['id']);c.commit()
  exports.produce(CommitFault(c,after),target)
  execute(c,'SELECT pg_advisory_unlock(:id)',id=lock);c.commit()
 current=get(target);assert current['state']==('ready' if after else 'recovery_required')
 data=exports.files.path(target).read_bytes();digest=hashlib.sha256(data).hexdigest()
 if not after:
  assert client.get(p+'/exports/'+target['id']+'/file').status_code==503;post(url(version),request(version,'pdf'),status=503)
  fixed=exports.reconcile(target['id']);assert fixed['state']=='ready'
 assert hashlib.sha256(exports.files.path(target).read_bytes()).hexdigest()==digest;assert count(target)==1
 record('lost_commit_ack_after_commit' if after else 'unknown_commit_preserves_file_until_reconciliation',export=target['id'],sha256=digest)
# Worker interruption while rendering has no candidate descriptor: no automatic fail or retry.
version=saved_version('Прерванный выпуск');target=post(url(version),request(version))
with engine.begin() as c:exports.save(c,target,state='rendering')
process();assert get(target)['state']=='recovery_required';assert exports.reconcile(target['id'])['state']=='recovery_required';assert count(target)==0
record('interrupted_render_stays_recovery_required_without_candidate')
# Read-only downloads cannot invoke renderer, including missing/corrupt endpoints.
with patch('app.exports.render',side_effect=AssertionError('Hidden rendering')):
 for target in browser['exports']:
  data,current=exports.get(s['actor'],s['project'],target['id'],download=True);assert hashlib.sha256(data).hexdigest()==target['file']['sha256']
record('download_renderer_interception')
with engine.connect() as c:
 calls=execute(c,'SELECT count(*) FROM aist.model_calls').scalar();assert calls==0
 versions=execute(c,'SELECT count(*) FROM aist.artifact_versions').scalar()
assert all(get(x)['state']=='ready' for x in browser['exports'])
subprocess.check_call([sys.executable,'-m','app.cli','revoke-session','--session-id',session['session_id']],stdout=subprocess.DEVNULL)
Path('/evidence/evidence/api-result.json').write_text(json.dumps(dict(passed=True,checks=checks,model_calls=calls,provider_posts=0,versions=versions),ensure_ascii=False,indent=2))
print('D12 boundary checks passed; ModelCall=0, provider POST=0')
