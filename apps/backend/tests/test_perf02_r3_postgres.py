"""Overload backoff on the HTTP stub: identical retries with their own accounting, one shared quota per Run,
Retry-After, interruption of the pause, and no retry after timeout, uncertainty, restart or another refusal.
Also the forecast heading outside the citation through the normal extraction transport and ingest."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from app import accounting, transport
from app.accounting import attempt_counts, retry_pause, waited_seconds
from app.errors import ApiError
from app.live_profile import MODEL
from app.openclaw_runtime import Boundary
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_perf02_r1_postgres import HEADER, ingest, qualitative, saved_amounts, upload

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')

OVERLOAD={'type':'server_overload','message':'server overload, please try again later trace_id: synthetic'}


def generation(tool=None):
    message=dict(role='assistant',content='Локальная проверка завершена.')
    if tool:
        message=dict(role='assistant',content=None,tool_calls=[dict(id=ident('call'),type='function',
            function=dict(name=tool,arguments=json_value(dict(refs=[]))))])
    return dict(id=ident('stub'),choices=[dict(index=0,message=message,finish_reason='tool_calls' if tool else 'stop')],
                usage=dict(prompt_tokens=100,completion_tokens=10))


def refusal(**entry):
    return dict(status=429,body=OVERLOAD,**entry)


@pytest.fixture
def stub(workspace,monkeypatch):
    """A loopback model endpoint replaying a scripted queue; each entry answers one POST."""
    state=SimpleNamespace(queue=[],received=[],on_request=None)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            state.received.append(self.rfile.read(int(self.headers['Content-Length'])))
            entry=state.queue.pop(0) if state.queue else dict(status=200,body=generation())
            if state.on_request:state.on_request(len(state.received))
            if entry.get('behavior')=='close':
                self.connection.close();return
            if entry.get('behavior')=='delay':
                time.sleep(entry['seconds'])
            data=json_value(entry['body']).encode()
            try:
                self.send_response(entry['status'])
                for name,value in entry.get('headers',{}).items():self.send_header(name,value)
                self.send_header('Content-Length',str(len(data)+entry.get('missing',0)));self.end_headers()
                self.wfile.write(data);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):return
            if entry.get('missing'):self.connection.close()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    monkeypatch.setattr('app.novita.credential',lambda path:'local-credential-not-in-response')
    monkeypatch.setattr(transport,'ReaderHTTPSConnection',lambda *a,**kw:transport.ReaderConnection('127.0.0.1',server.server_port,timeout=3))
    monkeypatch.setattr(transport,'jitter',lambda:0.0)
    yield state
    server.shutdown();server.server_close();thread.join()


def executor(w,**settings):
    if settings:w.runs.settings=replace(w.settings,**settings)
    worker=SimpleNamespace(runs=w.runs,draining=threading.Event(),closed=threading.Event(),persistence=threading.RLock())
    worker.transport=transport.Transport(w.runs,worker.draining,worker.closed,worker.persistence,{},threading.Lock())
    return worker


def start(w,**budget):
    """A Run of this project's policy; refusals hold a small quote so the project limit stays far away."""
    if w.runs.policy['quote_usd']!='0.01' or budget:
        w.runs.policy=deepcopy(w.runs.policy);w.runs.policy['quote_usd']='0.01';w.runs.policy['run'].update(budget)
    send(w,conversation(w),body(question='Проверка повторной отправки после перегрузки'))
    return w.runs.claim('perf02_r3')


def request(turn=1):
    return dict(model=MODEL,messages=[dict(role='user',content=f'Шаг {turn}: проверка повторной отправки')])


def post(worker,run,directory,turn=1,parent=None):
    return worker.transport.perform(run,{'purpose':'agent'},parent,request=request(turn),audit=directory/'calls')


def record_pauses(monkeypatch):
    pauses=[]
    monkeypatch.setattr(transport.Transport,'pause',lambda self,r,seconds:pauses.append(seconds) or True)
    return pauses


def calls(w,run):
    with w.engine.connect() as c:
        return [dict(v) for v in execute(c,'SELECT data,exposure,parent_model_call_id FROM aist.model_calls WHERE run_id=:r ORDER BY created_at,id',r=run['id']).mappings()]


def transport_events(w,call):
    with w.engine.connect() as c:
        return list(execute(c,'SELECT state FROM aist.model_transport_events WHERE call_id=:call ORDER BY seq',call=call).scalars())


def counts(w,run):
    with w.engine.connect() as c:return attempt_counts(c,run['id'])


def state(w,run):
    return w.runs.get(w.actor,w.project,run['id'])


def test_overload_refusal_is_retried_once_with_its_own_accounting(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w,max_model_calls=4);worker=executor(w)
    pauses=record_pauses(monkeypatch);monkeypatch.setattr(transport,'jitter',lambda:0.5)
    stub.queue=[refusal(),dict(status=200,body=generation('aist_get_facts'))]
    boundary=Boundary(worker,run,tmp_path,'synthetic')
    payload=boundary.model(request(1))
    assert payload['choices'][0]['message']['tool_calls'] and len(boundary.issued)==1
    assert pauses==[pytest.approx(11.0)] and len(stub.received)==2 and stub.received[0]==stub.received[1]
    refused,retry=calls(w,run)
    assert (refused['data']['http_status'],refused['data']['transport_state'],refused['data']['provider_outcome'],refused['data']['delivery'])==(429,'observed','error','delivered')
    assert refused['data']['error']['code']=='provider_error' and 'Повторная отправка 1 из 3 через 11.0 с' in refused['data']['error']['message']
    assert retry['parent_model_call_id']==refused['data']['id'] and retry['data']['provider_outcome']=='success' and retry['data']['purpose']=='agent'
    quote=Decimal(w.runs.policy['quote_usd'])
    assert Decimal(refused['exposure'])==quote and 0<Decimal(retry['exposure'])<quote
    assert transport_events(w,refused['data']['id'])==['reserved','attempted','sent','observed']==transport_events(w,retry['data']['id'])
    assert state(w,run)['state']=='running' and counts(w,run)==(1,1)
    claim=next((tmp_path/'calls').glob('*.claim')).read_text().splitlines()
    assert claim[0]=='single provider submission' and claim[1]=='retry granted after '+refused['data']['id']+': HTTP 429 server_overload, pause 11.0 s'
    assert len(list((tmp_path/'calls').glob('*-request.json')))==2==len(list((tmp_path/'calls').glob('*-response.json')))
    # The next ordinary step sees one used call, not two: the retry is not a free step either.
    boundary.model(request(2))
    hints=[json.loads(raw)['messages'][0]['content'] for raw in stub.received]
    assert [hint.split('после этого ответа: ')[1][0] for hint in hints]==['3','3','2']
    assert state(w,run)['state']=='running' and counts(w,run)==(2,1)


def test_three_retries_then_the_run_fails_without_a_fifth_send(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w);worker=executor(w);pauses=record_pauses(monkeypatch)
    stub.queue=[refusal()]*4+[dict(status=200,body=generation())]
    assert post(worker,run,tmp_path) is None
    assert pauses==[10,20,40] and len(stub.received)==4 and len(set(stub.received))==1
    recorded=calls(w,run)
    assert [c['data']['http_status'] for c in recorded]==[429]*4
    assert [c['parent_model_call_id'] for c in recorded]==[None]+[c['data']['id'] for c in recorded[:3]]
    assert 'Повторная отправка не разрешена' in recorded[-1]['data']['error']['message']
    assert all(Decimal(c['exposure'])==Decimal(w.runs.policy['quote_usd']) for c in recorded)
    final=state(w,run)
    assert final['state']=='failed' and final['error']['code']=='provider_error' and counts(w,run)==(1,3)


def test_retry_quota_is_shared_by_the_requests_of_one_run(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w,max_model_calls=3);worker=executor(w);record_pauses(monkeypatch)
    stub.queue=[refusal(),refusal(),dict(status=200,body=generation())]
    first=post(worker,run,tmp_path,1)
    stub.queue=[refusal(),dict(status=200,body=generation())]
    second=post(worker,run,tmp_path,2,parent=first[0])
    assert first[1]['choices'] and second[1]['choices'] and counts(w,run)==(2,3)
    stub.queue=[refusal(),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path,3,parent=second[0]) is None
    assert len(stub.received)==6 and state(w,run)['state']=='failed' and counts(w,run)==(3,3)


def test_retries_do_not_extend_the_ordinary_quota_but_the_money_limit_sees_every_attempt(workspace,stub,monkeypatch,tmp_path):
    # One project keeps one policy: both limits are set before its first Run.
    w=workspace;quote=Decimal('0.01')
    run=start(w,max_model_calls=1,limit=str(quote*2+Decimal('0.001')));worker=executor(w);record_pauses(monkeypatch)
    stub.queue=[refusal(),dict(status=200,body=generation())]
    first=post(worker,run,tmp_path,1)
    assert first[1]['choices'] and counts(w,run)==(1,1)
    with pytest.raises(ApiError) as error:post(worker,run,tmp_path,2,parent=first[0])
    assert error.value.code=='budget_exceeded' and len(stub.received)==2
    run=start(w);worker=executor(w)
    stub.queue=[refusal(),refusal(),refusal(),dict(status=200,body=generation())]
    with pytest.raises(ApiError) as error:post(worker,run,tmp_path/'second')
    assert error.value.code=='budget_exceeded' and len(stub.received)==4
    with w.engine.connect() as c:
        assert execute(c,'SELECT sum(exposure) FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalar_one()==quote*2


def test_retry_after_lengthens_the_pause_within_the_wait_and_run_limits(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w);worker=executor(w);pauses=record_pauses(monkeypatch)
    stub.queue=[refusal(headers={'Retry-After':'30'}),refusal(headers={'Retry-After':'Mon, 01 Jan 2001 00:00:00 GMT'}),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path)
    assert pauses==[30.0,20.0] and counts(w,run)==(1,2)
    assert 'через 30.0 с' in calls(w,run)[0]['data']['error']['message']
    # Total waiting is capped at 180 seconds; the actual waits already recorded count against it.
    monkeypatch.setattr(accounting,'waited_seconds',lambda history:150.0)
    stub.queue=[refusal(headers={'Retry-After':'40'}),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path,2) is None
    assert len(stub.received)==4 and state(w,run)['state']=='failed' and counts(w,run)==(2,2)
    run=start(w);stub.queue=[refusal(headers={'Retry-After':'200'}),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path/'second') is None and len(stub.received)==5 and state(w,run)['state']=='failed'
    assert pauses==[30.0,20.0]


def test_a_pause_beyond_the_remaining_run_time_is_not_granted(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w,max_duration_seconds=15);worker=executor(w);pauses=record_pauses(monkeypatch)
    stub.queue=[refusal(headers={'Retry-After':'20'}),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path) is None and len(stub.received)==1 and pauses==[]
    final=state(w,run);assert final['state']=='failed' and final['error']['code']=='provider_error'
    assert 'не разрешена' in calls(w,run)[0]['data']['error']['message']


def test_waited_seconds_sums_the_recorded_gaps():
    history=[(dict(attempted_at='2026-10-19T12:00:40.500Z'),dict(observed_at='2026-10-19T12:00:30.000Z')),
             (dict(attempted_at='2026-10-19T12:02:00.000Z'),dict(observed_at='2026-10-19T12:01:30.250Z'))]
    assert waited_seconds(history)==pytest.approx(40.25)
    assert retry_pause(len(history),None,0)==40


@pytest.mark.parametrize('interruption',['cancel','worker-stop'])
def test_interruption_during_the_pause_forbids_the_next_send(workspace,stub,monkeypatch,tmp_path,interruption):
    w=workspace;run=start(w);worker=executor(w)
    def interrupt():
        if interruption=='cancel':
            w.runs.write(w.actor,w.session,w.project,run['id'],'cancelRun',ident('key'),dict(expected_revision=run['revision']))
        else:worker.draining.set()
    stub.on_request=lambda count:threading.Timer(0.3,interrupt).start() if count==1 else None
    stub.queue=[refusal(),dict(status=200,body=generation())]
    started=time.monotonic()
    assert post(worker,run,tmp_path) is None
    assert time.monotonic()-started<8 and len(stub.received)==1 and len(calls(w,run))==1
    assert state(w,run)['state']==('cancelled' if interruption=='cancel' else 'running')


@pytest.mark.parametrize('entry,outcome',[
    (refusal(behavior='delay',seconds=3),'unknown'),
    (refusal(behavior='close'),'unknown'),
    (refusal(missing=20),'unknown'),
    (dict(status=429,body={'type':'rate_limit_exceeded','message':'slow down'}),'failed'),
    (dict(status=429,body={'error':{'code':429,'message':'quota'}}),'failed'),
    (dict(status=401,body={'error':{'type':'authentication_error'}}),'failed'),
    (dict(status=402,body={'type':'insufficient_balance'}),'failed'),
    (dict(status=503,body=OVERLOAD),'failed'),
    (dict(status=429,body=dict(OVERLOAD,choices=[])),'failed'),
],ids=['timeout','connection-lost','partial-body','other-429','untyped-429','authorization','balance','503','429-with-generation'])
def test_no_retry_after_timeout_uncertainty_or_another_refusal(workspace,stub,monkeypatch,tmp_path,entry,outcome):
    w=workspace;run=start(w);worker=executor(w,transport_seconds=1)
    monkeypatch.setattr(transport.Transport,'pause',lambda self,r,seconds:pytest.fail('no pause expected'))
    stub.queue=[entry,dict(status=200,body=generation())]
    if outcome=='unknown':
        with pytest.raises(ApiError) as error:post(worker,run,tmp_path)
        assert error.value.code=='outcome_unknown'
    else:
        assert post(worker,run,tmp_path) is None
    assert len(stub.received)==1
    recorded,=calls(w,run)
    assert recorded['data']['transport_state']==('unknown' if outcome=='unknown' else 'observed')
    assert Decimal(recorded['exposure'])==Decimal(w.runs.policy['quote_usd'])
    assert state(w,run)['state']==('recovery_required' if outcome=='unknown' else 'failed')


def test_restart_grants_no_new_quota_and_no_retry_after_uncertainty(workspace,stub,monkeypatch,tmp_path):
    w=workspace;run=start(w);worker=executor(w)
    stub.on_request=lambda count:threading.Timer(0.3,worker.draining.set).start() if count==1 else None
    stub.queue=[refusal(),dict(status=200,body=generation())]
    assert post(worker,run,tmp_path) is None and len(stub.received)==1
    w.runs.recover(owned=run['worker_id'])
    assert state(w,run)['state']=='recovery_required'
    stub.on_request=None;stub.queue=[dict(status=200,body=generation())]
    # The restarted worker finds the durable claim of the same body; a fresh directory meets the fenced epoch. Nothing is sent again.
    again=executor(w)
    for directory in (tmp_path,tmp_path/'elsewhere'):
        with pytest.raises(ApiError) as error:post(again,run,directory)
        assert error.value.code=='outcome_unknown'
    assert len(stub.received)==1 and counts(w,run)==(1,0)
    recorded,=calls(w,run)
    assert recorded['data']['http_status']==429 and recorded['data']['transport_state']=='observed'


def test_lost_observe_acknowledgement_ends_the_run_instead_of_retrying(workspace,stub,monkeypatch,tmp_path):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    w=workspace;run=start(w);worker=executor(w)
    monkeypatch.setattr(transport.Transport,'pause',lambda self,r,seconds:pytest.fail('no pause after an uncertain commit'))
    commits=[0]
    def lost_ack(connection):
        # Reserve and send commit first; the third commit records the refusal and the granted retry.
        commits[0]+=1
        if commits[0]==3:
            connection.connection.dbapi_connection.commit()
            raise OperationalError('commit',{},Exception('ack lost'))
    stub.queue=[refusal(),dict(status=200,body=generation())]
    event.listen(w.engine,'commit',lost_ack)
    try:assert post(worker,run,tmp_path) is None
    finally:event.remove(w.engine,'commit',lost_ack)
    assert len(stub.received)==1
    recorded,=calls(w,run)
    assert recorded['data']['http_status']==429 and recorded['data']['transport_state']=='observed'
    final=state(w,run);assert final['state']=='failed' and final['error']['code']=='provider_error'


def test_ingest_keeps_a_forecast_qualitative_whatever_the_model_cites(workspace,provider,tmp_path):
    w,p=workspace,provider
    version=upload(w,'forecast-outside-quote',[HEADER,'Прогноз на 2025 год','Собственный капитал','100 млн руб.','2025 г.'])
    item=qualitative();item['evidence'][0].update(line_start=3,line_end=5)
    reply=ingest(w,p,version,tmp_path,'Собственный капитал',[item])
    assert reply['ok'] and [f['amount'] for f in reply['result']['facts']]==[None]
    assert '100 млн руб.' in reply['result']['facts'][0]['evidence'][0]['quote']
    assert saved_amounts(w,version)==[]
    gap=upload(w,'forecast-gap',[HEADER,'Прогноз',' ','Собственный капитал','100 млн руб.','2025 г.'])
    item=qualitative();item['evidence'][0].update(line_start=1,line_end=6)
    reply=ingest(w,p,gap,tmp_path,'Прогноз капитала',[item])
    assert reply['ok'] and [f['amount'] for f in reply['result']['facts']]==[None]
    assert saved_amounts(w,gap)==[]
    plain=upload(w,'series',[HEADER,'Собственный капитал','100 млн руб.','2025 г.'])
    item=qualitative();item['evidence'][0].update(line_start=2,line_end=4)
    reply=ingest(w,p,plain,tmp_path,'Собственный капитал без прогноза',[item])
    assert reply['ok'] and saved_amounts(w,plain)==[('Собственный капитал','100','instant','2025 г.','2025-12-31','2025-12-31','reported_actual')]
    assert len(p.sent)==3
