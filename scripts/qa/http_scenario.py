"""Настоящий HTTP + PostgreSQL. Секретное состояние остается только в .runtime."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import httpx
from sqlalchemy import text

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'apps/backend/tests'))
from pdf_fixtures import pdf_bytes
from app.config import Settings
from app.contracts import validate_definition
from app.database import make_engine

parser=argparse.ArgumentParser()
parser.add_argument('phase',choices=['initial','isolation','restart'])
parser.add_argument('--origin',default='http://web:8080/api')
parser.add_argument('--direct',default='http://backend:8000')
parser.add_argument('--output',default='/evidence')
args=parser.parse_args()
out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
private=out/'private'; private.mkdir(mode=0o700,exist_ok=True)
engine=make_engine(Settings())
observations=[]


def admin(*args):
    result=subprocess.run([sys.executable,'-m','app.cli',*args],capture_output=True,check=True)
    return json.loads(result.stdout)


def counts():
    with engine.connect() as c:
        tables=['users','sessions','projects','sources','source_versions','source_pages','conversations','receipts']
        result={table:c.execute(text(f'SELECT count(*) FROM aist.{table}')).scalar_one() for table in tables}
    result['files']=len(list(Path('/data/sources').iterdir()))
    return result


def call(method,path,actor='a',status=200,definition=None,origin=None,headers=None,**kwargs):
    combined={'Authorization':'Bearer '+state[actor]['token']} if actor else {}
    combined.update(headers or {})
    response=httpx.request(method,(origin or args.origin)+path,headers=combined,timeout=35,**kwargs)
    assert response.status_code in (status if isinstance(status,tuple) else (status,)), (method,path,response.status_code,response.text[:1200])
    entry={'method':method,'path':path,'actor':actor,'status':response.status_code,
        'via':'direct' if origin==args.direct else 'web'}
    if response.status_code>=400:
        value=response.json();validate_definition('ErrorResponse',value)
        for secret in ('Traceback','psycopg','/data','local-only-example',state['a']['token'],state['b']['token']):
            assert secret not in response.text
        entry['body']=value
    elif definition:
        value=response.json();validate_definition(definition,value);entry['body']=value
    elif response.headers.get('content-type')=='application/octet-stream':
        entry['sha256']=hashlib.sha256(response.content).hexdigest()
        entry['byte_size']=len(response.content)
    observations.append(entry)
    return response


def create_project(actor,name,key):
    return call('POST','/v1/projects',actor,201,'Project',headers={'Idempotency-Key':key},json={'name':name}).json()


def upload(project,data,key,revision=0,source=None,status=201,actor='a',title='Synthetic source',filename='../../private/synthetic.pdf'):
    path=f'/v1/projects/{project}/sources'+(f'/{source}/versions' if source else '')
    return call('POST',path,actor,status,'UploadReceipt' if not isinstance(status,int) or status<400 else None,
        headers={'Idempotency-Key':key},files=[('metadata',(None,json.dumps({'title':title,'expected_revision':revision}),'application/json')),
        ('file',(filename,data,'application/pdf'))])


def assert_no_change(before):
    assert counts()==before,(before,counts())


def loss_after_commit(project,data,key):
    boundary='loss-'+uuid.uuid4().hex
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
          '{"title":"Lost response","expected_revision":0}\r\n'
          f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="loss.pdf"\r\n'
          'Content-Type: application/pdf\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
    # Сразу после отправки закрываем receive side: HTTP ответ не читается.
    c=socket.create_connection(('web',8080),timeout=3)
    c.sendall((f'POST /api/v1/projects/{project}/sources HTTP/1.1\r\nHost: web\r\n'
        f'Authorization: Bearer {state["a"]["token"]}\r\nIdempotency-Key: {key}\r\n'
        f'Content-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body)
    # Не читаем ответ. DB receipt подтверждает commit до закрытия socket.
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        with engine.connect() as db:
            receipt=db.execute(text('SELECT version_id FROM aist.receipts WHERE actor_id=:actor AND key=:key'),
                {'actor':state['a']['user_id'],'key':key}).scalar_one_or_none()
        if receipt:break
        time.sleep(.03)
    assert receipt, 'commit was not observed'
    c.close()
    observations.append({'case':'lost_http_response_after_commit','version_id':receipt,'response_read':False})
    return receipt


if args.phase=='initial':
    assert counts()=={'users':0,'sessions':0,'projects':0,'sources':0,'source_versions':0,'source_pages':0,'conversations':0,'receipts':0,'files':0}
    state={}
    for actor in ('a','b'):
        user=admin('create-user','--label','Synthetic '+actor)
        session=admin('issue-session','--user-id',user['user_id'],'--ttl-seconds','86400')
        state[actor]={**user,**session}
    p=create_project('a','Café\nProject','shared-key');q=create_project('b','Other owner','shared-key')
    r=create_project('a','Other own project','project-2')
    state.update(project=p['id'],other=q['id'],same_owner=r['id'])
    repeat=call('POST','/v1/projects',status=200,definition='Project',headers={'Idempotency-Key':'shared-key'},json={'name':'Cafe\u0301\r\nProject'}).json()
    assert repeat==p
    before=counts()
    call('POST','/v1/projects',status=409,headers={'Idempotency-Key':'shared-key'},json={'name':'Different'})
    for raw in ['{"name":"X","name":"Y"}','{"name":NaN}','{"name":"X","user_id":"usr_fake"}']:
        call('POST','/v1/projects',status=422,headers={'Idempotency-Key':'invalid-json','Content-Type':'application/json'},content=raw)
    assert_no_change(before)
    for origin in (args.origin,args.direct):
        call('GET','/v1/projects',actor=None,status=401,origin=origin,headers={'X-Owner-Id':state['a']['user_id']})
        call('GET','/v1/projects',actor=None,status=401,origin=origin,headers={'Authorization':'Bearer invalid'})
    expired=admin('issue-session','--user-id',state['a']['user_id'],'--ttl-seconds','1')
    revoked=admin('issue-session','--user-id',state['a']['user_id'])
    assert admin('revoke-session','--session-id',revoked['session_id'])['revoked']
    time.sleep(1.1)
    for session in (expired,revoked):
        call('GET','/v1/projects',actor=None,status=401,headers={'Authorization':'Bearer '+session['token']})
    first=call('GET','/v1/projects?limit=1',definition='ProjectPage').json()
    assert first['items']==[p] and first['next_after_id']==p['id']
    second=call('GET','/v1/projects?limit=1&after_id='+p['id'],definition='ProjectPage').json()
    assert second['items']==[r] and second['next_after_id'] is None
    assert call('GET','/v1/projects',actor='b',definition='ProjectPage').json()['items']==[q]
    call('GET','/v1/projects?after_id='+q['id'],status=409)
    call('GET','/v1/projects?limit=1&limit=2',status=422)
    call('GET','/v1/projects?limit=101',status=422)
    a,b=pdf_bytes(),pdf_bytes('beta')
    (out/'synthetic-alpha.pdf').write_bytes(a);(out/'synthetic-beta.pdf').write_bytes(b)
    receipt=upload(p['id'],a,'source-1').json();sid=receipt['source']['id'];vid=receipt['version']['id']
    state.update(source=sid,version=vid,sha=hashlib.sha256(a).hexdigest())
    assert receipt['version']['page_count']==2 and receipt['version']['sha256']==state['sha']
    assert receipt['version']['byte_size']==len(a) and receipt['version']['original_filename']=='synthetic.pdf'
    assert receipt['version']['processor_version']=='pypdf-6.19.0-text-v1'
    for origin in (args.origin,args.direct):
        assert call('GET',f'/v1/projects/{p["id"]}',definition='Project',origin=origin).json()==p
        assert call('GET',f'/v1/projects/{p["id"]}/sources/{sid}',definition='Source',origin=origin).json()==receipt['source']
        assert call('GET',f'/v1/projects/{p["id"]}/source-versions/{vid}',definition='SourceVersion',origin=origin).json()==receipt['version']
        for page in (1,2):
            content=call('GET',f'/v1/projects/{p["id"]}/source-versions/{vid}/page?page={page}',definition='PageContent',origin=origin).json()
            assert content['text'].strip()==f'Synthetic AIST alpha. Page {page}. Value {100+page}.'
            assert content['source']=={'source_id':sid,'source_version_id':vid,'sha256':state['sha']} and content['evidence']==[]
        downloaded=call('GET',f'/v1/projects/{p["id"]}/source-versions/{vid}/file',origin=origin)
        assert downloaded.content==a and downloaded.headers['etag']=='"sha256:'+state['sha']+'"'
        assert 'synthetic.pdf' in downloaded.headers['content-disposition']
    assert upload(p['id'],a,'source-1',status=200,filename='other-name.pdf').json()==receipt
    before=counts()
    upload(p['id'],b,'source-1',status=409)
    upload(p['id'],a,'stale-source',revision=1,status=409)
    call('GET',f'/v1/projects/{p["id"]}/source-versions/{vid}/page?page=3',status=404)
    assert_no_change(before)
    # Независимые multipart boundary одинакового одновременного POST.
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:upload(p['id'],a,'concurrent-upload',status=(200,201)),range(2)))
    assert sorted(x.status_code for x in responses)==[200,201]
    assert responses[0].json()==responses[1].json()
    after=counts()
    for name,delta in [('sources',1),('source_versions',1),('source_pages',2),('receipts',1),('files',1)]:
        assert after[name]-before[name]==delta,(name,before,after)
    new=upload(p['id'],b,'version-2',revision=1,source=sid).json()
    state.update(new_version=new['version']['id'],new_sha=hashlib.sha256(b).hexdigest())
    assert new['source']['latest_version_id']==state['new_version'] and new['source']['revision']==2
    assert call('GET',f'/v1/projects/{p["id"]}/source-versions/{vid}/file').content==a
    assert call('GET',f'/v1/projects/{p["id"]}/source-versions/{state["new_version"]}/file').content==b
    assert 'beta' in call('GET',f'/v1/projects/{p["id"]}/source-versions/{state["new_version"]}/page?page=1',definition='PageContent').json()['text']
    assert upload(p['id'],b,'version-2',revision=1,source=sid,status=200).json()==new
    before=counts();upload(p['id'],b,'new-stale',revision=1,source=sid,status=409);assert_no_change(before)
    conversation=call('POST',f'/v1/projects/{p["id"]}/conversations',status=201,definition='Conversation',headers={'Idempotency-Key':'conversation'},json={'title':'Conversation'}).json()
    state['conversation']=conversation['id'];assert conversation['revision']==0
    assert call('POST',f'/v1/projects/{p["id"]}/conversations',definition='Conversation',headers={'Idempotency-Key':'conversation'},json={'title':'Conversation'}).json()==conversation
    conv2=call('POST',f'/v1/projects/{p["id"]}/conversations',status=201,definition='Conversation',headers={'Idempotency-Key':'conversation-2'},json={'title':'Second'}).json()
    page=call('GET',f'/v1/projects/{p["id"]}/conversations?limit=1',definition='ConversationPage').json()
    assert page['items']==[conversation] and page['next_after_id']==conversation['id']
    assert call('GET',f'/v1/projects/{p["id"]}/conversations?after_id={conversation["id"]}',definition='ConversationPage').json()['items']==[conv2]
    source_page=call('GET',f'/v1/projects/{p["id"]}/sources?limit=1',definition='SourcePage').json()
    assert source_page['items'][0]['id']==sid and source_page['next_after_id']==sid
    assert len(call('GET',f'/v1/projects/{p["id"]}/sources?after_id={sid}',definition='SourcePage').json()['items'])==1
    before=counts()
    # Каждая foreign связь, включая собственный project с объектом другого собственного project.
    for actor,wrong_project in [('b',p['id']),('a',q['id']),('a',r['id'])]:
        suffixes=[f'/sources/{sid}',f'/source-versions/{vid}',f'/source-versions/{vid}/page?page=1',
            f'/source-versions/{vid}/file',f'/conversations/{conversation["id"]}']
        if wrong_project!=r['id']:
            suffixes+=['','/sources','/conversations']
        for suffix in suffixes:
            error=call('GET',f'/v1/projects/{wrong_project}'+suffix,actor=actor,status=404).json()['error']
            assert error['current_revision'] is None and error['message']=='Ресурс не найден.'
        upload(wrong_project,a,'version-2',revision=1,source=sid,status=404,actor=actor)
        if wrong_project!=r['id']:
            upload(wrong_project,a,'source-1',status=404,actor=actor)
            call('POST',f'/v1/projects/{wrong_project}/conversations',actor=actor,status=404,
                headers={'Idempotency-Key':'conversation'},json={'title':'Conversation'})
    call('GET',f'/v1/projects/{r["id"]}/sources?after_id={sid}',status=409)
    call('GET',f'/v1/projects/{r["id"]}/conversations?after_id={conversation["id"]}',status=409)
    assert call('GET',f'/v1/projects/{q["id"]}/sources',actor='b',definition='SourcePage').json()['items']==[]
    assert_no_change(before)
    # Отказы PDF не оставляют ни receipt, ни files, ни DB rows.
    negatives=[('encrypted',pdf_bytes(encrypted=True),415),('scanned',pdf_bytes(scanned=True),415),
        ('corrupt',b'%PDF-1.7\nbroken',415),('other',b'not a PDF',415),('pages',pdf_bytes(pages=41),413),
        ('bytes',a+b' '*(8*1024*1024),413)]
    for name,data,expected in negatives:
        before=counts();upload(p['id'],data,'unsupported-'+name,status=expected);assert_no_change(before)
    # Метаданные/SHA клиента отвергаются, лишние части тоже.
    before=counts()
    call('POST',f'/v1/projects/{p["id"]}/sources',status=422,headers={'Idempotency-Key':'fake-sha'},
        files={'metadata':(None,json.dumps({'title':'X','expected_revision':0,'sha256':'0'*64})), 'file':('x.pdf',a,'application/pdf')})
    call('POST',f'/v1/projects/{p["id"]}/sources',status=422,headers={'Idempotency-Key':'extra-file'},
        files=[('metadata',(None,'{"title":"X","expected_revision":0}')),('file',('a.pdf',a,'application/pdf')),('file',('b.pdf',b,'application/pdf'))])
    assert_no_change(before)
    state['lost_version']=loss_after_commit(p['id'],a,'lost-response')
    before=counts()
    repeated=upload(p['id'],a,'lost-response',status=200,title='Lost response').json()
    assert repeated['version']['id']==state['lost_version'];assert_no_change(before)
    # FK и immutable защита проверяются настоящим SQL, не формой JSON.
    for sql,params in [
        ('UPDATE aist.sources SET latest_version_id=:version WHERE id=:source',{'version':state['new_version'],'source':responses[0].json()['source']['id']}),
        ('UPDATE aist.source_versions SET sha256=:sha WHERE id=:id',{'sha':'0'*64,'id':vid}),
        ('UPDATE aist.source_pages SET text=\'changed\' WHERE version_id=:id',{'id':vid}),
        ('UPDATE aist.receipts SET actor_id=:actor WHERE project_id=:project',{'actor':state['b']['user_id'],'project':p['id']}),
        ('INSERT INTO aist.conversations(id,project_id,owner_id,title) VALUES (\'conv_forged\',:project,:owner,\'forged\')',{'project':p['id'],'owner':state['b']['user_id']})]:
        try:
            with engine.begin() as c:c.execute(text(sql),params)
        except Exception:
            observations.append({'case':'db_constraint_rejected','statement':sql.split(' ')[0:3]})
        else:raise AssertionError('DB invariant accepted mutation')
    state['counts']=counts()
    session_file=private/'state.json';session_file.write_text(json.dumps(state));session_file.chmod(0o600)
elif args.phase=='isolation':
    state=json.loads((private/'state.json').read_text())
    p,q=state['project'],state['other']
    bsource=upload(q,pdf_bytes(),'source-1',actor='b').json()
    bconversation=call('POST',f'/v1/projects/{q}/conversations',actor='b',status=201,definition='Conversation',
        headers={'Idempotency-Key':'conversation'},json={'title':'Conversation'}).json()
    assert bsource['version']['id']!=state['version']
    assert bconversation['id']!=state['conversation']
    assert upload(q,pdf_bytes(),'source-1',actor='b',status=200).json()==bsource
    before=counts()
    for suffix in [f'/sources/{bsource["source"]["id"]}',f'/source-versions/{bsource["version"]["id"]}',
                   f'/source-versions/{bsource["version"]["id"]}/page?page=1',f'/source-versions/{bsource["version"]["id"]}/file',
                   f'/conversations/{bconversation["id"]}']:
        call('GET',f'/v1/projects/{p}'+suffix,status=404)
    upload(p,pdf_bytes(),'version-2',source=bsource['source']['id'],revision=1,status=404)
    call('GET',f'/v1/projects/{p}/sources?after_id={bsource["source"]["id"]}',status=409)
    call('GET',f'/v1/projects/{p}/conversations?after_id={bconversation["id"]}',status=409)
    for path in ['/v1/projects',f'/v1/projects/{p}/sources',f'/v1/projects/{p}/conversations',
                 f'/v1/projects/{p}/source-versions/{state["version"]}/file',
                 f'/v1/projects/{p}/source-versions/{state["version"]}/page?page=1']:
        call('GET',path,actor=None,status=401)
    call('POST',f'/v1/projects/{p}/sources',actor=None,status=401,
        headers={'Idempotency-Key':'source-1','X-Owner-Id':state['a']['user_id']},content=b'not even multipart')
    call('POST',f'/v1/projects/{p}/conversations',status=409,
        headers={'Idempotency-Key':'conversation'},json={'title':'Different'})
    assert_no_change(before)
    # Две операции новой версии конкурируют за CAS, успешный receipt проверяется раньше CAS.
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:upload(p,pdf_bytes('gamma'),'version-concurrent',
            revision=2,source=state['source'],status=(200,201)),range(2)))
    assert sorted(x.status_code for x in responses)==[200,201]
    assert responses[0].json()==responses[1].json()
    after=counts()
    assert after['source_versions']==before['source_versions']+1 and after['files']==before['files']+1
    assert responses[0].json()['source']['revision']==3
    # CAS с разными ключами допускает ровно одну следующую версию.
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda number:upload(p,pdf_bytes('delta'),'cas-race-'+str(number),
            revision=3,source=state['source'],status=(201,409)),range(2)))
    assert sorted(x.status_code for x in responses)==[201,409]
    assert counts()['source_versions']==after['source_versions']+1
    assert counts()['files']==after['files']+1
    state['counts']=counts();(private/'state.json').write_text(json.dumps(state))
else:
    state=json.loads((private/'state.json').read_text())
    assert counts()==state['counts']
    p=state['project'];vid=state['version'];new=state['new_version']
    call('GET',f'/v1/projects/{p}',definition='Project')
    assert call('GET',f'/v1/projects/{p}/conversations/{state["conversation"]}',definition='Conversation').json()['revision']==0
    assert call('GET',f'/v1/projects/{p}/source-versions/{vid}',definition='SourceVersion').json()['sha256']==state['sha']
    assert hashlib.sha256(call('GET',f'/v1/projects/{p}/source-versions/{vid}/file').content).hexdigest()==state['sha']
    assert hashlib.sha256(call('GET',f'/v1/projects/{p}/source-versions/{new}/file').content).hexdigest()==state['new_sha']
    assert 'alpha' in call('GET',f'/v1/projects/{p}/source-versions/{vid}/page?page=2',definition='PageContent').json()['text']
    assert 'beta' in call('GET',f'/v1/projects/{p}/source-versions/{new}/page?page=2',definition='PageContent').json()['text']
    assert upload(p,pdf_bytes(),'source-1',status=200).json()['version']['id']==vid
    assert upload(p,pdf_bytes('beta'),'version-2',revision=1,source=state['source'],status=200).json()['version']['id']==new
    assert upload(p,pdf_bytes(),'lost-response',status=200,title='Lost response').json()['version']['id']==state['lost_version']
    assert counts()==state['counts']

summary={'phase':args.phase,'passed':True,'requests':observations,'counts':counts(),
    'live_model_calls':0,'assertions':'D06 subset of A01/A02/A03/A18; future Run/tool/artifact assertions not executed'}
(out/(args.phase+'.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'phase':args.phase,'passed':True,'http_requests':sum('method' in x for x in observations),'counts':counts()}))
engine.dispose()
