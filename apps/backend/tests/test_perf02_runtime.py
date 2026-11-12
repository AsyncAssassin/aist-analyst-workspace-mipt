"""Installed OpenClaw, real AIST validation and loopback model, capped at four calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace

import pytest

from app import openclaw_runtime as runtime, transport
from app.extraction import candidate
from app.facts import save_fact, pin_of
from app.live_profile import MODEL
from app.model_save import save_operation
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import upload_pdf, candidate as extraction_item
from test_d15_postgres import workspace

OWNERSHIP_REVIEW = dict(status='not_established',
                        reason='Синтетические материалы этого сценария не раскрывают владельцев.', excluded_sources=[])

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_OPENCLAW')!='true',reason='Requires isolated PostgreSQL and installed OpenClaw')


@pytest.mark.parametrize('mode', ['save', 'schema-repeat', 'reference-correction', 'draft', 'overload-retry'])
def test_installed_save_and_rejection_cycle(workspace,monkeypatch,tmp_path,mode):
    w = workspace
    source = upload_pdf(w)
    pin = pin_of(source)
    with w.engine.begin() as c:
        pages = {row['page']: row['text'] for row in execute(
            c, 'SELECT page,text FROM aist.source_pages WHERE version_id=:v', v=source['id']).mappings()}
        fact, basis = candidate(pin, pages, extraction_item())
        save_fact(w.service, c, w.actor, w.project, pin, fact, calculation_basis=basis)
    ref=dict(source_version_id=source['id'],fact_id=fact['fact_id'])
    correct=dict(content=dict(title='Компания: информационная справка',audience='Аналитики',sections=[
        dict(title='Финансовые показатели',blocks=[dict(kind='table',caption='Выручка',rows=[dict(
            label=fact['metric'],period_label=fact['period']['label'],amount=fact['amount'],fact_refs=[ref],calculation_ids=[])])])]))
    correct['ownership_review'] = OWNERSHIP_REVIEW
    request=body(sources=[source['id']],question='Подготовь справку по сохраненным финансовым фактам')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    w.runs.policy=deepcopy(w.runs.policy)
    w.runs.policy["run"]["max_model_calls"]=4
    send(w,conversation(w),request)
    run=w.runs.claim('perf02_runtime')
    received=[]
    out=Path(os.getenv('AIST_PERF02_EVIDENCE',str(tmp_path)))/mode
    out.mkdir(parents=True,exist_ok=True)
    class Stub(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(data)
            (out/f'{len(received)}-request.json').write_text(json_value(data))
            if mode=='overload-retry' and len(received)==1:
                # One complete overload refusal; the identical retry then saves the note.
                raw=json_value(dict(type='server_overload',message='server overload, please try again later trace_id: synthetic')).encode()
                self.send_response(429);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            args=deepcopy(correct)
            if mode=='schema-repeat':
                args['content'].pop('audience')
                args['content']['sections'][0]['section_id']='sec_changed_'+str(len(received))
            elif mode=='reference-correction' and len(received)==1:
                # A reference to a fact absent from the allowed version is still refused; the corrected send saves.
                args['content']['sections'][0]['blocks'][0]['rows'][0]['fact_refs']=[dict(ref,fact_id='fact_missing')]
            elif mode=='draft' and len(received)==1:
                # DRAFT-01: a wrong amount with a valid reference is saved as an unaccepted draft on the first attempt.
                args['content']['sections'][0]['blocks'][0]['rows'][0]['amount']['value']='999'
            message=dict(role='assistant',content=None,tool_calls=[dict(id=ident('call'),type='function',function=dict(name='aist_save_artifact',arguments=json_value(args)))])
            result=dict(id=ident('stub'),created=int(time.time()),model=MODEL,
                choices=[dict(index=0,message=message,finish_reason='tool_calls')],usage=dict(prompt_tokens=100,completion_tokens=50))
            raw=json_value(result).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    stub=ThreadingHTTPServer(('127.0.0.1',0),Stub)
    thread=threading.Thread(target=stub.serve_forever);thread.start()
    monkeypatch.setattr('app.novita.credential',lambda path:'nonpaid-local-credential')
    monkeypatch.setattr(transport,'ReaderHTTPSConnection',lambda *a,**kw:transport.ReaderConnection('127.0.0.1',stub.server_port,timeout=20))
    with TemporaryDirectory(prefix='p02-') as root:
        with socket.socket() as a,socket.socket() as b:
            a.bind(('127.0.0.1',0));b.bind(('127.0.0.1',0))
            settings=replace(w.settings,openclaw_root=root,transport_seconds=20,lease_seconds=300,
                gateway_port=a.getsockname()[1],provider_port=b.getsockname()[1],ready_facts_only=True,embeddings_enabled=False)
        w.runs.settings=settings
        worker=SimpleNamespace(settings=settings,runs=w.runs,draining=threading.Event(),closed=threading.Event(),persistence=threading.RLock())
        worker.transport=transport.Transport(w.runs,worker.draining,worker.closed,worker.persistence,{},threading.Lock())
        timer=threading.Timer(120,worker.draining.set);timer.start()
        try:
            runtime.run(worker,run)
        finally:
            timer.cancel();stub.shutdown();stub.server_close();thread.join()
            directory=Path(root)/run['id']
            for name in ('tools.jsonl','gateway-process.log'):
                if (directory/name).exists():(out/name).write_bytes((directory/name).read_bytes())
    state=w.runs.get(w.actor,w.project,run['id'])
    with w.engine.connect() as c:
        versions=list(execute(c,'SELECT data FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalars())
        calls=list(execute(c,'SELECT data FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalars())
    assert len(received)==(1 if mode in ('save','draft') else 2)
    assert len(calls)==len(received) and all(call['purpose']=='agent' for call in calls)
    assert all(data['thinking']=={'type':'disabled'} and data['enable_thinking'] is False for data in received)
    assert fact['fact_id'] in json_value(received[0])
    assert all(marker not in json_value(received) for marker in ('characters truncated','middle content omitted'))
    if mode=='schema-repeat':
        assert state['state']=='failed' and not versions
        feedback=next(m['content'] for m in received[1]['messages'] if m['role']=='tool')
        assert 'Received arguments:' not in feedback
        assert 'audience' in feedback
        assert not (out/'tools.jsonl').exists(), 'Failure must occur inside OpenClaw before AIST'
    else:
        assert state['state']=='completed' and len(versions)==1
        saved=w.runs.version(w.actor,w.project,state['result_version_id'])
        if mode=='draft':
            # The draft keeps the model text and its blocking remarks; the Run is complete without a second POST.
            assert saved['checks'][0]['status']=='failed' and saved['review']['acceptance'] is None
            assert all(f['severity']=='error' for f in saved['checks'][0]['findings']) and saved['checks'][0]['findings']
            assert [f['code'] for f in saved['checks'][0]['findings']]==['row_basis_mismatch']
            assert 'Сумма 999 млн RUB' in saved['checks'][0]['findings'][0]['message']
            sent=deepcopy(correct);sent['content']['sections'][0]['blocks'][0]['rows'][0]['amount']['value']='999'
            assert len((out/'tools.jsonl').read_text().splitlines())==1,'the save tool ran once'
        else:
            assert saved['checks'][0]['status']=='passed'
            sent=correct
        # A successful operation replay with another executor call ID is read-only.
        args=save_operation(run,sent)
        again=w.runs.tool(run['id'],run['epoch'],ident('repeat'),'aist_save_artifact',args)
        assert again['ok'] and again['result']['version']['id']==state['result_version_id']
        if mode=='reference-correction':
            assert 'validation_failed' in json_value(received[1]) and 'fact_missing' in json_value(received[1])
    waited=None
    if mode=='overload-retry':
        refused,retry=sorted(calls,key=lambda call:call['attempted_at'])
        assert refused['http_status']==429 and refused['provider_outcome']=='error' and retry['parent_model_call_id']==refused['id']
        assert received[0]==received[1], 'the retry repeats the same body'
        moment=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
        waited=(moment(retry['sent_at'])-moment(refused['observed_at'])).total_seconds()
        assert 10<=waited<=14, 'the first pause is 10 seconds plus up to 20% jitter'
        assert len((out/'tools.jsonl').read_text().splitlines())==1, 'the save tool ran once'
    (out/'result.json').write_text(json_value(dict(mode=mode,state=state['state'],stub_posts=len(received),paid_posts=0,versions=len(versions),retry_wait_seconds=waited)))


def test_installed_draft_with_remarks_completes_without_a_second_post(workspace,monkeypatch,tmp_path):
    """DRAFT-01: the flawed small note is saved as a draft on the first attempt: one POST, one version, remarks stored.

    A replay of the same operation and a read through a fresh connection return the same version without any
    provider call. The stub answer is test data, not evidence of a live model."""
    from app.database import make_engine
    from app.runs import Runs
    from app.service import Service
    from app.storage import Storage
    from test_perf02_r4_postgres import note_source, small_note
    w=workspace
    source,facts=note_source(w)
    request=body(sources=[source['id']],question='Подготовь справку по сохраненным фактам')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    w.runs.policy=deepcopy(w.runs.policy)
    w.runs.policy['run']['max_model_calls']=4
    send(w,conversation(w),request)
    run=w.runs.claim('draft01_runtime')
    received=[]
    out=Path(os.getenv('AIST_PERF02_EVIDENCE',str(tmp_path)))/'draft-with-remarks'
    out.mkdir(parents=True,exist_ok=True)
    def without_ids(content):
        for section in content['sections']:
            section.pop('section_id')
            for block in section['blocks']:
                block.pop('block_id')
                for row in block.get('rows',[]):row.pop('row_id')
        return content
    class Stub(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(data)
            (out/f'{len(received)}-request.json').write_text(json_value(data))
            content=without_ids(small_note(source))
            message=dict(role='assistant',content=None,tool_calls=[dict(id=ident('call'),type='function',function=dict(name='aist_save_artifact',arguments=json_value(dict(content=content, ownership_review=OWNERSHIP_REVIEW))))])
            result=dict(id=ident('stub'),created=int(time.time()),model=MODEL,
                choices=[dict(index=0,message=message,finish_reason='tool_calls')],usage=dict(prompt_tokens=100,completion_tokens=50))
            raw=json_value(result).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    stub=ThreadingHTTPServer(('127.0.0.1',0),Stub)
    thread=threading.Thread(target=stub.serve_forever);thread.start()
    monkeypatch.setattr('app.novita.credential',lambda path:'nonpaid-local-credential')
    monkeypatch.setattr(transport,'ReaderHTTPSConnection',lambda *a,**kw:transport.ReaderConnection('127.0.0.1',stub.server_port,timeout=20))
    with TemporaryDirectory(prefix='p02-') as root:
        with socket.socket() as a,socket.socket() as b:
            a.bind(('127.0.0.1',0));b.bind(('127.0.0.1',0))
            settings=replace(w.settings,openclaw_root=root,transport_seconds=20,lease_seconds=300,
                gateway_port=a.getsockname()[1],provider_port=b.getsockname()[1],ready_facts_only=True,embeddings_enabled=False)
        w.runs.settings=settings
        worker=SimpleNamespace(settings=settings,runs=w.runs,draining=threading.Event(),closed=threading.Event(),persistence=threading.RLock())
        worker.transport=transport.Transport(w.runs,worker.draining,worker.closed,worker.persistence,{},threading.Lock())
        timer=threading.Timer(120,worker.draining.set);timer.start()
        try:
            runtime.run(worker,run)
        finally:
            timer.cancel();stub.shutdown();stub.server_close();thread.join()
            directory=Path(root)/run['id']
            for name in ('tools.jsonl','gateway-process.log'):
                if (directory/name).exists():(out/name).write_bytes((directory/name).read_bytes())
    state=w.runs.get(w.actor,w.project,run['id'])
    with w.engine.connect() as c:
        versions=list(execute(c,'SELECT data FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalars())
        calls=list(execute(c,'SELECT data FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalars())
    assert len(received)==1 and len(calls)==1 and calls[0]['purpose']=='agent'
    assert state['state']=='completed' and len(versions)==1 and versions[0]['id']==state['result_version_id']
    journal=[json.loads(line) for line in (out/'tools.jsonl').read_text().splitlines()]
    assert [entry['reply']['ok'] for entry in journal]==[True]
    view=w.runs.version(w.actor,w.project,state['result_version_id'])
    check=view['checks'][0]
    assert check['status']=='failed' and view['review']['acceptance'] is None
    assert check['profile_version'] == 'note-technical.v29'
    assert view['checks'][1]['status'] == 'not_checked'
    assert view['version']['basis']['ownership_review'] == OWNERSHIP_REVIEW
    sections = view['version']['payload']['content']['sections']
    table = next(block for block in sections[0]['blocks'] if block.get('caption') ==
                 'Структура финансирования на 31.03.2023')
    finding, = check['findings']
    assert (finding['code'], finding['block_id'], finding['severity']) == ('row_basis_mismatch', table['block_id'], 'error')
    assert finding['message'].startswith('строка 3 «Облигации» (row_') and 'Сумма 942 млн RUB' in finding['message']
    assert [block['claim'] for block in sections[1]['blocks']] == [
        block['claim'] for block in small_note(source)['sections'][1]['blocks']]
    assert all(marker not in json_value(received) for marker in ('characters truncated','middle content omitted','Received arguments:'))
    # Replay with another executor call ID and a read through a fresh connection: no second version, no provider call.
    args=save_operation(run,dict(content=without_ids(small_note(source)), ownership_review=OWNERSHIP_REVIEW))
    again=w.runs.tool(run['id'],run['epoch'],ident('repeat'),'aist_save_artifact',args)
    assert again['ok'] and again['result']['version']['id']==state['result_version_id']
    fresh=Runs(Service(make_engine(w.settings),Storage(w.settings.storage_root)),w.settings)
    try:
        assert fresh.version(w.actor,w.project,state['result_version_id'])==view
    finally:
        fresh.engine.dispose()
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalar()==1
        assert execute(c,'SELECT count(*) FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalar()==1
    usage = w.runs.listing(w.actor, w.project, 'usage_entries', {}, run['id'])['items']
    entries = {entry['evidence_kind']: entry for entry in usage}
    assert len(usage) == 2 and entries.keys() == {'reservation', 'provider_response'}
    assert entries['provider_response']['id'] == calls[0]['usage_entry_id']
    assert entries['provider_response']['supersedes_id'] == entries['reservation']['id']
    assert len(received)==1
    (out/'result.json').write_text(json_value(dict(mode='draft',state=state['state'],stub_posts=len(received),paid_posts=0,
        versions=len(versions),version=versions[0]['id'],check=check['status'],findings=[(f['code'],f['block_id']) for f in check['findings']])))
