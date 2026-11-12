"""Fault injection только в тестовом HTTP процессе, на собственной настоящей PG."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch
import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
import uvicorn
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'apps/backend/tests'))
from pdf_fixtures import pdf_bytes
from app.main import create_app
from app.config import Settings
from app.contracts import validate_definition
from app.database import make_engine

parser=argparse.ArgumentParser();parser.add_argument('--output',default='/evidence');parser.add_argument('--partial-only',action='store_true');args=parser.parse_args()
out=Path(args.output);state=json.loads((out/'private/state.json').read_text())
engine=make_engine(Settings());settings=Settings();root=Path(settings.storage_root)
observations=[]


def snapshot():
    with engine.connect() as c:
        counts={t:c.execute(text(f'SELECT count(*) FROM aist.{t}')).scalar_one()
            for t in ['projects','sources','source_versions','source_pages','conversations','receipts']}
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file() and not p.is_symlink()}
    return {'counts':counts,'files':files}


class TransactionProxy:
    def __init__(self, real, mode, owner=None):self.real,self.mode,self.owner=real,mode,owner
    def rollback(self):return self.real.rollback()
    def commit(self):
        if self.mode=='commit_before':raise SQLAlchemyError('simulated commit failure')
        self.real.commit()
        if self.mode=='commit_unknown':self.owner.uncertain=True
        if self.mode in ('commit_after','commit_unknown'):raise SQLAlchemyError('simulated lost acknowledgement')


class ConnectionProxy:
    def __init__(self, real, mode, owner=None):self.real,self.mode,self.owner=real,mode,owner
    def __getattr__(self,name):return getattr(self.real,name)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def begin(self):return TransactionProxy(self.real.begin(),self.mode,self.owner)
    def execute(self,statement,*args,**kwargs):
        if self.mode=='insert_failure' and 'INSERT INTO aist.source_versions' in str(statement):
            raise SQLAlchemyError('simulated DB insert failure')
        return self.real.execute(statement,*args,**kwargs)
    def close(self):return self.real.close()


class EngineProxy:
    def __init__(self,real,mode):self.real,self.mode,self.uncertain=real,mode,False
    def connect(self):
        if self.uncertain:raise SQLAlchemyError('simulated reconciliation outage')
        return ConnectionProxy(self.real.connect(),self.mode,self)
    @contextmanager
    def begin(self):
        c=self.connect()
        try:
            tx=c.real.begin()
            yield c
            tx.commit()
        finally:c.close()


app=create_app(settings)
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='critical'))
thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
deadline=time.monotonic()+10
while not server.started and time.monotonic()<deadline:time.sleep(.03)
assert server.started
origin=f'http://127.0.0.1:{port}'
headers={'Authorization':'Bearer '+state['a']['token']}
data=pdf_bytes('fault')
project=state['project']


def upload(key,status=201):
    response=httpx.post(origin+f'/v1/projects/{project}/sources',headers={**headers,'Idempotency-Key':key},
        files={'metadata':(None,'{"title":"Fault scenario","expected_revision":0}','application/json'),
            'file':('fault.pdf',data,'application/pdf')},timeout=30)
    assert response.status_code==status,(key,response.status_code,response.text)
    validate_definition('ErrorResponse' if status>=400 else 'UploadReceipt',response.json())
    assert 'simulated' not in response.text and state['a']['token'] not in response.text
    return response.json()


try:
    baseline=upload('fault-baseline',200 if args.partial_only else 201);baseline_path=root/(baseline['version']['id']+'.pdf')
    foreign=root/'foreign-sentinel.pdf';foreign.write_bytes(b'synthetic foreign sentinel')
    baseline_snapshot=snapshot()
    before=baseline_snapshot
    with patch('app.storage.os.fsync',side_effect=OSError('simulated disk failure')):
        value=upload('fault-disk',503)
    assert snapshot()==before and not list(root.glob('*.tmp'))
    observations.append({'case':'file_write_failure','response':value,'no_db_or_file_change':True})
    real_fdopen=os.fdopen
    class PartialWrite:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):self.stream.close()
        def write(self,content):
            self.stream.write(content[:len(content)//2]);self.stream.flush()
            raise OSError('simulated partial write failure')
    def fdopen(fd,mode,*args,**kwargs):
        stream=real_fdopen(fd,mode,*args,**kwargs)
        return PartialWrite(stream) if mode=='wb' else stream
    before=snapshot()
    with patch('app.storage.os.fdopen',side_effect=fdopen):value=upload('fault-partial-write',503)
    assert snapshot()==before and not list(root.glob('*.tmp'))
    observations.append({'case':'partial_temporary_write_failure','response':value,'no_db_or_file_change':True})
    if args.partial_only:
        foreign.unlink()
        result={'passed':True,'actual_http_fault_process':True,'real_postgresql':True,'observations':observations}
        (out/'fault-partial.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'passed':True,'partial_write_http':True}))
        raise SystemExit(0)
    for mode in ('insert_failure','commit_before','commit_after','commit_unknown'):
        before=snapshot();real=app.state.service.engine;app.state.service.engine=EngineProxy(real,mode)
        try:value=upload('fault-'+mode,200 if mode=='commit_after' else 503)
        finally:app.state.service.engine=real
        after=snapshot()
        if mode in ('insert_failure','commit_before'):
            assert after==before
        else:
            assert after['counts']['source_versions']==before['counts']['source_versions']+1
            assert len(after['files'])==len(before['files'])+1
            repeated=upload('fault-'+mode,200)
            assert repeated['version']['id'] in [p.name.removesuffix('.pdf') for p in root.glob('sv_*.pdf')]
            assert snapshot()==after
        assert baseline_path.read_bytes()==data and foreign.read_bytes()==b'synthetic foreign sentinel'
        observations.append({'case':mode,'response':value,'protected_files_unchanged':True,
            'before_counts':before['counts'],'after_counts':after['counts'],'file_delta':len(after['files'])-len(before['files'])})
    # Неопределенный исход без commit: файл сохраняется до ручной сверки, retry не удаляет его.
    class UnknownBefore(TransactionProxy):
        def commit(self):
            self.owner.uncertain=True
            raise SQLAlchemyError('simulated unknown before commit')
    class UnknownConnection(ConnectionProxy):
        def begin(self):return UnknownBefore(self.real.begin(),'unused',self.owner)
    class UnknownEngine(EngineProxy):
        def connect(self):
            if self.uncertain:raise SQLAlchemyError('simulated database outage')
            return UnknownConnection(self.real.connect(),'unused',self)
    before=snapshot();real=app.state.service.engine;app.state.service.engine=UnknownEngine(real,'unused')
    try:value=upload('fault-uncommitted-unknown',503)
    finally:app.state.service.engine=real
    after=snapshot();assert after['counts']==before['counts'] and len(after['files'])==len(before['files'])+1
    orphan=set(after['files'])-set(before['files']);assert len(orphan)==1
    upload('fault-uncommitted-unknown',201)
    assert all((root/name).exists() for name in orphan)
    observations.append({'case':'unknown_uncommitted_file_retained','response':value,'preserved_orphan_names':sorted(orphan)})
    # Объект готов только с реальным файлом. Missing/corrupt меняют статус на failed.
    missing=upload('fault-missing');mid=missing['version']['id'];mpath=root/(mid+'.pdf')
    mpath.unlink()
    response=httpx.get(origin+f'/v1/projects/{project}/source-versions/{mid}/file',headers=headers)
    assert response.status_code==410 and response.json()['error']['code']=='export_missing'
    response=httpx.get(origin+f'/v1/projects/{project}/source-versions/{mid}',headers=headers)
    assert response.json()['processing_state']=='failed'
    # Для сохранения тестового тома возвращаем исходные bytes; статус остается failed до явного восстановления.
    mpath.write_bytes(data)
    corrupt=upload('fault-corrupt');cid=corrupt['version']['id'];cpath=root/(cid+'.pdf');cpath.write_bytes(b'corrupt')
    response=httpx.get(origin+f'/v1/projects/{project}/source-versions/{cid}/file',headers=headers)
    assert response.status_code==409 and response.json()['error']['code']=='export_corrupt'
    cpath.write_bytes(data)
    response=httpx.get(origin+f'/v1/projects/{project}/source-versions/{cid}/page?page=1',headers=headers)
    assert response.status_code==409 and response.json()['error']['code']=='source_not_ready'
    # Restart integrity sweep не выдает ready для файла, удаленного между чтениями.
    startup=upload('fault-startup');sp=root/(startup['version']['id']+'.pdf');sp.unlink()
    app.state.service.reconcile_files()
    response=httpx.get(origin+f'/v1/projects/{project}/source-versions/{startup["version"]["id"]}',headers=headers)
    assert response.json()['processing_state']=='failed';sp.write_bytes(data)
    observations.append({'case':'missing_corrupt_startup_integrity','missing':mid,'corrupt':cid,'startup':startup['version']['id'],'passed':True})
    # Cleanup не удаляет symlink и файл с другим inode.
    storage=app.state.service.storage
    link=root/'synthetic-symlink';link.symlink_to(foreign)
    assert not storage.remove_owned(link,foreign.stat(),foreign.read_bytes())
    assert foreign.exists() and link.is_symlink();link.unlink()
    old_stat=baseline_path.stat()
    assert not storage.remove_owned(foreign,old_stat,data)
    assert foreign.read_bytes()==b'synthetic foreign sentinel';foreign.unlink()
    observations.append({'case':'cleanup_symlink_and_foreign_inode','passed':True})
    assert not list(root.glob('*.tmp'))
    result={'passed':True,'actual_http_fault_process':True,'real_postgresql':True,'observations':observations,'final_snapshot':snapshot()}
    (out/'faults.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'fault_cases':len(observations),'counts':snapshot()['counts']}))
finally:
    server.should_exit=True;thread.join(timeout=10);assert not thread.is_alive()
    engine.dispose()
