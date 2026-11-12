"""Sequential fact needs through real transport, accounting, tools and verified ingest."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject

from app import transport
from app.errors import ApiError
from app.extraction import completed_requests
from app.openclaw_runtime import Boundary, execution_facts, execution_message
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, reply, send
from test_d15_postgres import workspace

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')

LINES=['Sample Company','Business','Supplies equipment.','Revenue, million RUB','IFRS',
       '100','3M23','80','3M22','EBITDA, million RUB','20','3M23']


def upload_pdf(w):
    writer=PdfWriter();page=writer.add_blank_page(width=595,height=842)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 16 TL 72 760 Td '+' T* '.join('('+line+') Tj' for line in LINES)+' ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    data=io.BytesIO();writer.write(data)
    return w.service.write(w.actor,'uploadSource',ident('key'),dict(title='Synthetic report',expected_revision=0),
                           w.project,None,data.getvalue(),'report.pdf')[1]['version']


def candidate(metric='Revenue',year=2023):
    qualitative=metric=='Business'
    start,end=(1,3) if qualitative else (6,7) if metric=='Revenue' and year==2023 else (8,9) if metric=='Revenue' else (11,12)
    return dict(metric=metric,subject='Sample Company',reporting_basis='IFRS',
        nature='source_statement' if qualitative else 'reported_actual',
        period=dict(kind='unknown',label='Не указан',start=None,end=None) if qualitative else
            dict(kind='interval',label=f'3M{year%100}',start=f'{year}-01-01',end=f'{year}-03-31'),
        amount=None if qualitative else dict(value=LINES[start-1],unit='money',currency='RUB'),
        value_text=None if qualitative else LINES[start-1],
        evidence=[dict(page=1,line_start=start,line_end=end,area='Synthetic report')],
        basis=None if qualitative else dict(page=1,metric_label=metric,unit_label='million RUB',reporting_label='IFRS'))


@pytest.fixture
def provider(workspace,tmp_path,monkeypatch):
    state=SimpleNamespace(sent=[],facts=[],args=None,before_reply=None,status=200,broken=False,response=None)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            outgoing=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.sent.append(outgoing)
            if outgoing.get('response_format'):
                if state.before_reply:state.before_reply()
                if state.broken:
                    self.connection.close();return
                message=dict(role='assistant',content=json_value({'facts':state.facts}))
            else:
                message=dict(role='assistant',content=None,tool_calls=[dict(id=ident('executor'),type='function',
                    function=dict(name='aist_get_facts',arguments=json_value(state.args)))])
            payload=state.response
            if payload is None:
                payload=dict(id=ident('synthetic'),choices=[dict(message=message,finish_reason='stop' if outgoing.get('response_format') else 'tool_calls')],
                    usage=dict(prompt_tokens=100,completion_tokens=50))
            data=json_value(payload).encode()
            self.send_response(state.status);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    monkeypatch.setattr('app.novita.credential',lambda path:'local-credential-not-in-response')
    monkeypatch.setattr(transport,'ReaderHTTPSConnection',lambda *a,**kw:transport.ReaderConnection('127.0.0.1',server.server_port,timeout=3))
    worker=SimpleNamespace(runs=workspace.runs,closed=threading.Event(),draining=threading.Event(),persistence=threading.RLock())
    worker.transport=transport.Transport(workspace.runs,worker.draining,worker.closed,worker.persistence,{},threading.Lock())
    state.worker=worker;state.root=tmp_path
    yield state
    server.shutdown();server.server_close();thread.join()


def start(w,conv,version,question):
    with w.engine.connect() as c:
        revision=w.service.scoped(c,w.actor,w.project,'conversations',conv['id'])['revision']
    send(w,conv,body(revision=revision,sources=[version['id']],question=question))
    return w.runs.claim('chat01_r1')


def ask(w,p,run,args):
    p.args=args
    directory=p.root/(run['id']+'-'+str((run['checkpoint'] or {}).get('turn',0)))
    directory.mkdir(exist_ok=True)
    facts,_=execution_facts(p.worker,run,'Unused by loading facts',directory)
    with w.engine.connect() as c:
        task=execute(c,'SELECT text FROM aist.messages WHERE id=:id',id=run['message_id']).scalar_one()
        prior=[item for pin in run['input']['source_versions'] for item in completed_requests(w.runs,c,run,pin)]
    boundary=Boundary(p.worker,run,directory,'synthetic')
    prompt=execution_message(w.runs,run,task,facts,extractions=prior)
    payload=boundary.model(dict(model='xiaomimimo/mimo-v2.6-pro',messages=[dict(role='user',content=prompt)]))
    call=payload['choices'][0]['message']['tool_calls'][0]
    value=dict(session=run['executor_session_id'],tool_call_id=call['id'],name='aist_get_facts',args=args)
    p.last_boundary=boundary;p.last_tool=value
    return boundary.tool(value),boundary,value


def extract(version,question):
    return dict(refs=[],extract=dict(source_version_id=version['id'],question=question))


def saved_facts(w):
    with w.engine.connect() as c:
        return [dict(f) for f in execute(c,'SELECT * FROM aist.facts WHERE project_id=:p ORDER BY fact_id',p=w.project).mappings()]


def counts(w):
    with w.engine.connect() as c:
        return dict(execute(c,"SELECT data->>'purpose',count(*) FROM aist.model_calls WHERE project_id=:p GROUP BY data->>'purpose'",p=w.project).all())


def finish(w,run,facts,text):
    refs=[dict(source_version_id=f['source']['source_version_id'],fact_id=f['fact_id']) for f in facts]
    result=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',dict(idempotency_key=ident('key'),
        expected_revision=0,artifact_id=None,parent_version_id=None,payload=dict(result_type='evidence_answer',schema_version='1.0.0',
        content=dict(claims=[dict(text=text,origin='source_fact',fact_refs=refs,calculation_ids=[])],missing_information=[]))))
    assert result['ok'],result


def test_metric_period_transitions_and_ordinary_continuation(workspace,provider):
    w=workspace;p=provider;version=upload_pdf(w);conv=conversation(w)
    cases=[('Чем занимается компания?','Business',2023,'Supplies equipment.'),
           ('Какая выручка за первый квартал 2023 года?','Revenue',2023,'Выручка за 3M23: 100 млн рублей.'),
           ('А годом раньше?','Revenue',2022,'Выручка за 3M22: 80 млн рублей.'),
           ('Какова EBITDA за первый квартал 2023 года?','EBITDA',2023,'EBITDA за 3M23: 20 млн рублей.')]
    for index,(task,metric,year,answer) in enumerate(cases,1):
        run=start(w,conv,version,task);before=saved_facts(w)
        p.facts=[candidate(metric,year)]
        result,_,_=ask(w,p,run,extract(version,f'{metric} Sample Company {year if metric!="Business" else "description"} first quarter'))
        assert result['ok'],result
        fact=result['result']['facts'][0]
        assert fact['metric']==metric and fact['source']==run['input']['source_versions'][0]
        if metric!='Business':assert fact['period']['start']==f'{year}-01-01'
        assert counts(w)==dict(agent=index,extraction=index)
        assert len(saved_facts(w))==index
        assert all(f in saved_facts(w) for f in before)
        sent=json.loads(p.sent[-1]['messages'][1]['content'])
        assert sent['task']==task and sent['conversation_context']==run['configuration']['conversation_context']
        if task=='А годом раньше?':
            assert any('2023' in m['text'] for m in sent['conversation_context']['messages'])
            assert any('100 млн' in m['text'] for m in sent['conversation_context']['messages'])
            assert '2022' in sent['question']
        finish(w,run,result['result']['facts'],answer)
    before=deepcopy(saved_facts(w));before_counts=counts(w)
    run=start(w,conv,version,'Объясни последний ответ короче')
    fact=next(f['data'] for f in before if f['data']['metric']=='EBITDA')
    result,_,_=ask(w,p,run,dict(refs=[dict(source_version_id=version['id'],fact_id=fact['fact_id'])]))
    finish(w,run,result['result']['facts'],'EBITDA за 3M23: 20 млн рублей.')
    assert saved_facts(w)==before
    assert counts(w)==dict(agent=before_counts['agent']+1,extraction=before_counts['extraction'])
    with w.engine.connect() as c:
        calls=list(execute(c,"SELECT data FROM aist.model_calls WHERE project_id=:p AND data->>'purpose'='extraction'",p=w.project).scalars())
        assert all(call['provider_outcome']=='success' and call['delivery']=='delivered' and call['parent_model_call_id'] for call in calls)


def test_clarification_is_sent_to_extraction_and_no_guess_is_extracted_before_it(workspace,provider):
    w=workspace;p=provider;version=upload_pdf(w);conv=conversation(w)
    run=start(w,conv,version,'Какая выручка?')
    assert execution_facts(p.worker,run,'Какая выручка?',p.root)==([],None)
    assert counts(w)=={} and p.sent==[]
    assert w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_request_input',dict(idempotency_key=ident('key'),
        expected_revision=run['revision'],prompt='За какой период нужна выручка?'))['ok']
    waiting=w.runs.get(w.actor,w.project,run['id'])
    w.runs.write(w.actor,w.session,w.project,run['id'],'resumeRun',ident('key'),dict(expected_revision=waiting['revision'],
        waiting_input_id=waiting['waiting_input']['id'],text='За первый квартал 2023 года'))
    resumed=w.runs.claim('chat01_r1');p.facts=[candidate()]
    result,boundary,value=ask(w,p,resumed,extract(version,'Выручка Sample Company за первый квартал 2023 года'))
    assert result['ok'],result
    sent=json.loads(p.sent[-1]['messages'][1]['content'])
    assert sent['task']=='Какая выручка?'
    assert [m['text'] for m in sent['clarification_dialogue']]==['За какой период нужна выручка?','За первый квартал 2023 года']
    assert counts(w)==dict(agent=1,extraction=1)
    assert boundary.tool(value)==result and counts(w)==dict(agent=1,extraction=1)
    finish(w,resumed,result['result']['facts'],'Выручка за 3M23: 100 млн рублей.')


def test_successful_empty_result_survives_next_run_but_new_need_and_pin_can_extract(workspace,provider):
    w=workspace;p=provider;version=upload_pdf(w);conv=conversation(w)
    question='Какая чистая прибыль за первый квартал 2023 года?'
    for index in range(2):
        run=start(w,conv,version,question)
        result,_,_=ask(w,p,run,extract(version,question))
        assert result['ok'] and result['result']['facts']==[]
        assert reply(w,run,'Нужные сведения не найдены.')['ok']
        assert counts(w)==dict(agent=index+1,extraction=1)
        if index==1:
            sent=json.loads(p.sent[-1]['messages'][-1]['content'].rsplit('\n\n',1)[-1])
            assert sent['completed_extractions'][0]['fact_refs']==[]
            assert sent['completed_extractions'][0]['question']==question
    other=conversation(w);run=start(w,other,version,question)
    result,_,_=ask(w,p,run,extract(version,question))
    assert result['ok'] and counts(w)['extraction']==1
    sent=json.loads(p.sent[-1]['messages'][-1]['content'].rsplit('\n\n',1)[-1])
    assert sent['completed_extractions']==[] and sent['conversation_context']['messages']==[]
    assert reply(w,run,'Нужные сведения не найдены.')['ok']
    run=start(w,conv,version,'А за первый квартал 2022 года?')
    result,_,_=ask(w,p,run,extract(version,'Чистая прибыль за первый квартал 2022 года'))
    assert result['ok'] and counts(w)['extraction']==2
    assert reply(w,run,'Нужные сведения не найдены.')['ok']
    new=upload_pdf(w);run=start(w,conv,new,question)
    result,_,_=ask(w,p,run,extract(new,question))
    assert result['ok'] and counts(w)['extraction']==3
    assert reply(w,run,'Нужные сведения не найдены.')['ok']
    assert saved_facts(w)==[]


@pytest.mark.parametrize('failure',['invalid_fact','provider_error','unknown','cancel'])
def test_failed_or_suppressed_extraction_never_becomes_completed_empty(workspace,provider,failure):
    w=workspace;p=provider;version=upload_pdf(w);conv=conversation(w)
    run=start(w,conv,version,'Выручка за первый квартал 2023 года?')
    p.facts=[candidate()]
    if failure=='invalid_fact':p.facts[0]['amount']['value']='999'
    if failure=='provider_error':p.status=500
    if failure=='unknown':p.broken=True
    if failure=='cancel':
        p.before_reply=lambda:w.runs.write(w.actor,w.session,w.project,run['id'],'cancelRun',ident('key'),dict(expected_revision=run['revision']))
    # Reserve/issue the tool before applying a provider transport error to extraction.
    status=p.status;p.status=200
    if status!=200:p.before_reply=lambda:setattr(p,'status',status)
    try:
        result,boundary,value=ask(w,p,run,extract(version,'Revenue 3M23'))
        if failure=='invalid_fact':
            # FLOW-01.02: an excluded candidate is a completed extraction without usable facts, with its reason.
            assert result['ok'] and result['result']['status']=='empty' and result['result']['skipped'][0]['code']=='number_mismatch'
        else:
            assert not result['ok']
        assert boundary.tool(value)==result
    except ApiError as error:
        assert error.code in ('outcome_unknown','run_terminal','stale_epoch')
        with pytest.raises(ApiError):p.last_boundary.tool(p.last_tool)
    with w.engine.connect() as c:
        completed=completed_requests(w.runs,c,run,run['input']['source_versions'][0])
    assert [item['status'] for item in completed]==(['empty'] if failure=='invalid_fact' else [])
    assert saved_facts(w)==[] and counts(w)['extraction']==1
    assert len([s for s in p.sent if s.get('response_format')])==1
    latest=w.runs.get(w.actor,w.project,run['id'])
    if latest['state'] not in ('cancelled','failed','completed'):
        w.runs.write(w.actor,w.session,w.project,run['id'],'cancelRun',ident('key'),dict(expected_revision=latest['revision']))


def test_extraction_does_not_expand_source_authority(workspace,provider):
    w=workspace;p=provider;version=upload_pdf(w);excluded=upload_pdf(w);conv=conversation(w)
    run=start(w,conv,version,'Какая выручка?')
    with pytest.raises(ApiError) as denied:
        ask(w,p,run,extract(excluded,'Revenue 3M23'))
    assert denied.value.code=='resource_not_found'
    assert counts(w)==dict(agent=1) and saved_facts(w)==[]
    assert reply(w,run,'Требуется уточнение.')['ok']
