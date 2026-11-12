"""Local demo admission, conservative ledger, immutable deadlines and ordinary PDF upload."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from sqlalchemy.exc import TimeoutError as PoolTimeout

import pytest
from test_perf02_r4_postgres import model_save_args

from app import collection, extraction, openclaw_runtime as runtime, runs as run_clock, transport
from app.accounting import Accounting, money_available
from app.errors import ApiError
from app.facts import pin_of
from app.live_profile import FABLE_FULL_ID, FABLE_VISUAL_ID
from app.runs import Runs, execute, json_value
from app.service import ident
from app.visual_observations import available as visual_observations
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace, publish_fixture_worker
from test_demo_live01_b_postgres import installed
from test_draft01_postgres import http
from test_fable_postgres import native, reply as cached_reply, extracted as cached_extracted, audit, GOOD, QUAL, MIXED, FABLE
from test_flow01_postgres import executor, current
from test_flow01_05_postgres import ask
from test_perf02_r1_postgres import upload, cyrillic_pdf, fact_rows
from test_perf02_r4_postgres import paragraph, ref, table_row
from test_note_path06_r1_postgres import inspect_images

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
POLICY = Path('/workspace/executor/openclaw/policy.demo-live01.json')


def standard_usage(response):
    response['usage'].update(cache_read_input_tokens=0, cache_creation_input_tokens=0,
                            cache_creation=dict(ephemeral_5m_input_tokens=0,ephemeral_1h_input_tokens=0))
    return response


def reply(*args,**kwargs):
    return standard_usage(cached_reply(*args,**kwargs))


def extracted(*args,**kwargs):
    return standard_usage(cached_extracted(*args,**kwargs))


def configure(w, tmp_path, *, owners=None, visual=False):
    policy = json.loads(POLICY.read_text())
    policy['demo_access'] = dict(enabled=True, owner_ids=owners or [w.actor])
    path = tmp_path / 'demo-policy.json'
    path.write_text(json_value(policy))
    w.settings = replace(w.settings, model_profile_id=FABLE_VISUAL_ID if visual else FABLE_FULL_ID,
        embeddings_enabled=False, ready_facts_only=False, policy_path=str(path), transport_seconds=600, lease_seconds=1000)
    w.runs = Runs(w.service, w.settings)
    return policy


def change_access(w, **patch):
    path = Path(w.settings.policy_path)
    policy = json.loads(path.read_text())
    policy['demo_access'].update(patch)
    replacement = path.with_suffix('.next')
    replacement.write_text(json_value(policy))
    replacement.replace(path)


def request_note(w, source, *, key=None, claim=True):
    conv = conversation(w)
    request = body(sources=[source['id']], question='Подготовь справку по финансовым показателям, бизнесу, стратегии и рискам.')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note',
                   model_profile_version_id=w.settings.model_profile_id)
    result = send(w, conv, request, key)
    return conv, request, result, w.runs.claim('demo-c') if claim else None


def content(w, source, run=None):
    facts = [f['data'] for f in fact_rows(w)]
    business = next(f for f in facts if not f['amount'])
    financial = [f for f in facts if f['amount']]
    note = dict(title='Synthetic: информационная справка', audience='Аналитик', sections=[
        dict(section_id='sec_business', title='Бизнес', blocks=[paragraph('block_business',
            'Компания поставляет оборудование клиентам.', [ref(source, business['fact_id'])])]),
        dict(section_id='sec_finance', title='Финансы', blocks=[dict(block_id='block_revenue', kind='table',
            caption='Выручка', rows=[table_row('row_revenue', 'Выручка', f['period']['label'], f['amount']['value'],
                                            [ref(source, f['fact_id'])]) for f in financial])]),
    ])
    if run:
        for item in visual_observations(current(w, run)).values():
            note['sections'][0]['blocks'].append(dict(block_id='block_visual', kind='paragraph', claim=dict(
                text=item['text'], origin='inference', fact_refs=[], calculation_ids=[],
                visual_observation_ids=[item['observation_id']])))
    return note


def test_http_upload_empty_project_two_topics_installed_runtime(workspace, native, tmp_path, monkeypatch):
    w = workspace
    configure(w, tmp_path, visual=True)
    with http(w) as client:
        response = client.post(f'/v1/projects/{w.project}/sources', headers={'Idempotency-Key': ident('key')},
            data={'metadata': json_value(dict(title='Обычная загрузка', expected_revision=0))},
            files={'file': ('synthetic.pdf', cyrillic_pdf(MIXED), 'application/pdf')})
        assert response.status_code == 201, response.text
        source = response.json()['version']
        assert source['processing_state'] == 'ready'
        with w.engine.connect() as c:
            assert execute(c, 'SELECT count(*) FROM aist.facts').scalar_one() == 0
            assert execute(c, 'SELECT count(*) FROM aist.knowledge_materials').scalar_one() == 0
            assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
        conv, request, _, run = request_note(w, source)
        first = reply(args={'question':collection.NOTE_TOPICS[0]}, identifier='toolu_finance_c')
        first['content'].append(dict(type='tool_use', id='toolu_business_c', name='aist_get_facts',
            input={'question':collection.NOTE_TOPICS[1]}))
        for call in list(first['content'][1:]):
            first['content'].append(dict(call, id=call['id']+'_repeat'))
        qualitative = extracted([QUAL])
        qualitative['content'] = [dict(type='text', text=json_value(dict(facts=[QUAL], visual_observations=[
            dict(page=1, area='Схема бизнеса', text='Партнер предоставляет место для оборудования.', numeric=None)])))]
        def compose_with_received_labels():
            from app.model_context import note_reference_labels, translate_references
            material = current(w, run)
            labels = note_reference_labels(pin_of(source), [row['data'] for row in fact_rows(w)], collection.results(material))
            prompt = json_value(native.requests[2]['messages'])
            for identifier, label in labels.items():
                assert label in prompt and (not identifier.startswith('fact_') or identifier not in prompt)
            labelled = translate_references(content(w, source, run), labels)
            return reply('aist_save_artifact', model_save_args(labelled), identifier='toolu_save_c')
        native.queue = [dict(body=extracted([GOOD])), dict(body=qualitative), dict(body=first),
                       dict(body=compose_with_received_labels)]
        # Simulate a busy DB pool on the first status read after the single accepted agent dispatch.
        connect = w.engine.connect
        attach = w.runs.attach_executor
        poll_thread = []
        interruptions = []
        def record_dispatch(*args):
            attach(*args)
            poll_thread.append(threading.get_ident())
        def busy_once(*args, **kwargs):
            if threading.get_ident() in poll_thread and not interruptions:
                interruptions.append(True)
                raise PoolTimeout("busy checkout")
            return connect(*args, **kwargs)
        monkeypatch.setattr(w.runs, 'attach_executor', record_dispatch)
        monkeypatch.setattr(w.engine, 'connect', busy_once)
        state = installed(w, run, native, 'ordinary-upload-two-topics-v3')
        assert len(poll_thread) == 1 and interruptions == [True]
        assert state['state'] == 'completed', state
        assert len(native.requests) == 4
        journal = [json.loads(line) for line in (Path('/result/ordinary-upload-two-topics-v3/tools.jsonl')).read_text().splitlines()]
        collected = [entry['reply'] for entry in journal if entry['request']['name']=='aist_get_facts']
        assert len(collected)==4 and all(item['ok'] for item in collected)
        assert [item['result'] for item in collected[:2]] == [item['result'] for item in collected[2:]]
        calls, usage = audit(w, run)
        assert [c['purpose'] for c in calls] == ['extraction', 'extraction', 'agent', 'agent']
        assert all(u['reserved_usd'] == '15' for u in usage)
        assert all(r['max_tokens'] == 64000 and 'cache_control' not in json_value(r) and 'service_tier' not in r for r in native.requests)
        with w.engine.connect() as c:
            pages = extraction.source_pages(w.runs, c, run)
        images = [inspect_images(request, pin_of(source), pages) for request in native.requests[:2]]
        assert images[0] == images[1] and images[0]
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        assert saved['review']['acceptance'] is None
        assert all(item['verification_status']=='unchecked' for item in saved['version']['basis']['visual_observations'])
        assert 'unchecked_visual_observation' in {f['code'] for check in saved['checks'] for f in check['findings']}
        (Path('/result')/'ordinary-upload-version-v3.json').write_text(json_value(saved))
        replacement = client.post(f"/v1/projects/{w.project}/sources/{source['source_id']}/versions",
            headers={'Idempotency-Key':ident('key')}, data={'metadata':json_value(dict(title="Обычная загрузка",expected_revision=1))},
            files={'file':('replacement.pdf',cyrillic_pdf(['Новый документ']), 'application/pdf')})
        assert replacement.status_code == 201, replacement.text
        assert w.runs.get(w.actor,w.project,run['id'])['input']['source_versions'] == run['input']['source_versions']


def test_two_topics_cache_and_third_refusal(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'themes', MIXED)
    _, _, _, run = request_note(w, source)
    native.queue = [dict(body=extracted([GOOD])), dict(body=extracted([QUAL]))]
    worker = executor(w)
    for question in ('Финансы и периоды', 'Бизнес, рынок, стратегия и риски'):
        result = ask(w, worker, current(w,run), source, question, tmp_path)
        assert result['ok'], result
        again = ask(w, worker, current(w,run), source, question, tmp_path/'restarted')
        assert again['result'] == result['result']
    third = ask(w,worker,current(w,run),source,'Еще один вопрос',tmp_path)
    assert not third['ok'] and len(native.requests) == 2


@pytest.mark.parametrize('remaining,delay,allowed',[(900,0,True),(610,0,True),(421,0,True),(420,0,False),(410,0,False),(450,31,False)])
@pytest.mark.parametrize('stage',['tls','reserve','render'])
def test_note_window_keeps_completion_reserve(workspace,native,monkeypatch,tmp_path,remaining,delay,allowed,stage):
    w=workspace
    configure(w,tmp_path,visual=stage=='render')
    source=upload(w,'window',MIXED)
    _,_,_,run=request_note(w,source)
    offset=[0]
    monotonic=time.monotonic
    duration = run['input']['budget']['max_duration_seconds']
    extraction_seconds = 600 if stage == 'render' else 300
    monkeypatch.setattr(run_clock,'utcnow',lambda:run['created_at']+timedelta(seconds=duration-remaining+offset[0]))
    assert collection.send_seconds(run,600,'extraction') == max(0,min(extraction_seconds,remaining-360))
    worker=executor(w)
    if stage=='tls':
        original=transport.ReaderConnection.connect
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs);offset[0]+=delay;return value
        monkeypatch.setattr(transport.ReaderConnection,'connect',delayed)
    elif stage=='reserve':
        original=worker.transport.ledger.reserve
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs);offset[0]+=delay;return value
        monkeypatch.setattr(worker.transport.ledger,'reserve',delayed)
    else:
        from app import pdf_images
        original=pdf_images.render
        def delayed(*args,**kwargs):
            value=original(*args,**kwargs);offset[0]+=delay;return value
        monkeypatch.setattr(pdf_images,'render',delayed)
    monkeypatch.setattr(transport.time,'monotonic',lambda:monotonic()+offset[0])
    native.queue=[dict(body=extracted([QUAL]))]
    result=ask(w,worker,current(w,run),source,'Бизнес',tmp_path)
    assert result['ok'] == allowed, result
    assert len(native.requests) == int(allowed)


def test_atomic_slot_receipt_owners_wait_and_recovery(workspace,tmp_path,monkeypatch):
    w=workspace
    other=SimpleNamespace(**vars(w));other.actor=ident('usr');other.session=ident('ses')
    with w.engine.begin() as c:
        execute(c,'INSERT INTO aist.users(id,label) VALUES (:a,:a)',a=other.actor)
        execute(c,"INSERT INTO aist.sessions(id,user_id,token_hash,expires_at) VALUES (:s,:a,:s,clock_timestamp()+interval '1 hour')",s=other.session,a=other.actor)
    other.project=w.service.write(other.actor,'createProject',ident('key'),{'name':'Другой владелец'})[1]['id']
    configure(w,tmp_path,owners=[w.actor,other.actor]);other.runs=Runs(w.service,w.settings)
    source=upload(w,'first',MIXED);other_source=upload(other,'second',MIXED)
    barrier=threading.Barrier(2)
    def submit(pair):
        actor,document=pair
        barrier.wait()
        try:return actor,request_note(actor,document,claim=False)
        except ApiError as error:return actor,error
    with ThreadPoolExecutor(2) as pool:
        outcomes=list(pool.map(submit,[(w,source),(other,other_source)]))
    assert sum(not isinstance(result,ApiError) for _,result in outcomes)==1
    winner,result=next((actor,result) for actor,result in outcomes if not isinstance(result,ApiError))
    loser=next(actor for actor,result in outcomes if isinstance(result,ApiError))
    assert loser.runs.capabilities(loser.actor)['model_execution']['state']=='busy'
    assert winner.actor not in json_value(loser.runs.capabilities(loser.actor)['model_execution'])
    run=winner.runs.claim('winner')
    with w.engine.begin() as c:
        r=winner.runs.locked(c,run['id'])
        winner.runs.transition(c,r,'waiting_for_user',waiting=dict(id=ident('waiting'),prompt='Уточните задачу',created_at=run_clock.now()))
    with monkeypatch.context() as clock:
        clock.setattr(run_clock,'utcnow',lambda:run['created_at']+timedelta(seconds=run['input']['budget']['max_duration_seconds']+1))
        winner.runs.recover()
    assert current(w,run)['state']=='budget_exceeded'
    assert loser.runs.capabilities(loser.actor)['model_execution']['state']=='available'
    conv,request,result,_=request_note(w,source,key='same-operation',claim=False)
    duplicate=send(w,conv,request,'same-operation')
    assert duplicate[0]==200 and duplicate[1]['run']['id']==result[1]['run']['id']
    run=w.runs.claim('unknown-worker')
    ledger=Accounting(w.runs);call=ledger.reserve(run['id'],run['epoch'])
    w.runs.recover(owned='unknown-worker')
    assert current(w,run)['state']=='recovery_required'
    w.runs=Runs(w.service,w.settings);w.runs.recover()
    assert current(w,run)['state']=='failed'
    with w.engine.connect() as c:
        assert execute(c,'SELECT exposure FROM aist.model_calls WHERE id=:id',id=call['id']).scalar_one()==15
    assert w.runs.claim('restart') is None


@pytest.mark.parametrize('revocation',['owner','disabled','expired','live'])
def test_revocation_at_physical_post(workspace,native,monkeypatch,tmp_path,revocation):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'revocation',MIXED)
    _,_,_,run=request_note(w,source)
    connect=transport.ReaderConnection.connect
    def revoke(connection):
        connect(connection)
        if revocation=='owner':change_access(w,owner_ids=[])
        elif revocation=='live':change_access(w,enabled=False)
        else:
            with w.engine.begin() as c:
                column='disabled_at' if revocation=='disabled' else 'access_expires_at'
                execute(c,f'UPDATE aist.users SET {column}=clock_timestamp() WHERE id=:a',a=w.actor)
    monkeypatch.setattr(transport.ReaderConnection,'connect',revoke)
    with pytest.raises(ApiError):
        runtime.Boundary(executor(w),run,tmp_path,'fixture').model(dict(model=FABLE['model'],messages=[dict(role='user',content='Вопрос')]))
    assert not native.requests
    calls,usage=audit(w,run)
    assert calls[0]['transport_state']=='not_sent'
    with w.engine.connect() as c:
        assert execute(c,'SELECT exposure FROM aist.model_calls WHERE id=:id',id=calls[0]['id']).scalar_one()==0


@pytest.mark.parametrize(('authorized', 'additional_quotes'), [('50', 0), ('75', 2), ('100', 3)])
def test_money_reserve_restart_and_policy_version_do_not_reset(workspace,tmp_path,authorized,additional_quotes):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'money',MIXED)
    _,_,_,run=request_note(w,source)
    ledger=Accounting(w.runs)
    first=ledger.reserve(run['id'],run['epoch'])
    second=ledger.reserve(run['id'],run['epoch'])
    with w.engine.begin() as c:
        assert collection.shortfall(c,current(w,run))=='reserve_money'
        assert money_available(c,current(w,run),1)
        assert not money_available(c,current(w,run),2)
    with pytest.raises(ApiError):ledger.reserve(run['id'],run['epoch'],'extraction')
    third=ledger.reserve(run['id'],run['epoch'])
    for call in (first,second,third):
        ledger.mark_unknown(run['id'],call['id'],'synthetic unknown')
    w.runs.stop(run['id'],run['epoch'],'outcome_unknown')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    policy=json.loads(Path(w.settings.policy_path).read_text())
    policy['run']['policy_version']='demo-live01.v2'
    policy['total_authorized_usd']=authorized
    Path(w.settings.policy_path).write_text(json_value(policy))
    w.runs=Runs(w.service,w.settings)
    w.project=w.service.write(w.actor,'createProject',ident('key'),{'name':'После рестарта'})[1]['id']
    source=upload(w,'new-project',MIXED)
    publish_fixture_worker(w)
    if not additional_quotes:
        assert w.runs.capabilities(w.actor)['model_execution']['state']=='budget_exceeded'
        with pytest.raises(ApiError):request_note(w,source,claim=False)
    else:
        assert w.runs.capabilities(w.actor)['model_execution']['state']=='available'
        _,_,_,next_run=request_note(w,source)
        next_ledger=Accounting(w.runs)
        assert next_run['configuration']['policy']['total_authorized_usd'] == authorized
        for _ in range(additional_quotes):
            next_ledger.reserve(next_run['id'],next_run['epoch'])
        with pytest.raises(ApiError):next_ledger.reserve(next_run['id'],next_run['epoch'])
    with w.engine.connect() as c:
        assert execute(c,'SELECT sum(exposure) FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalar_one()==45
        assert execute(c,'SELECT count(*) FROM aist.runs').scalar_one()==(1 if authorized=='50' else 2)
        assert execute(c,'SELECT sum(exposure) FROM aist.model_calls').scalar_one() == 45 + additional_quotes * 15
        empty_project = dict(project_id='prj_unused', input={'budget':policy['run']}, configuration={'policy':policy})
        assert not money_available(c, empty_project, 1)


@pytest.mark.parametrize('failure',['429','timeout','unknown'])
def test_native_no_replay_after_failure(workspace,native,tmp_path,failure):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'failure',MIXED)
    _,_,_,run=request_note(w,source)
    if failure=='429':native.queue=[dict(status=429,body={'type':'error','error':{'type':'rate_limit_error'}})]
    elif failure=='timeout':
        w.runs.settings=replace(w.settings,transport_seconds=.2)
        native.queue=[dict(body=extracted([QUAL]),delay=.5)]
    else:native.queue=[dict(close=True)]
    boundary=runtime.Boundary(executor(w),run,tmp_path,'fixture')
    for _ in range(2):
        with pytest.raises(ApiError):boundary.model(dict(model=FABLE['model'],messages=[dict(role='user',content='Вопрос')]))
    w.runs.recover(owned=run['worker_id']);w.runs.recover()
    assert w.runs.claim('restarted') is None
    assert len(native.requests)==len(audit(w,run)[0])==1


def test_http_capabilities_and_owner_admission(workspace,tmp_path):
    w=workspace
    configure(w,tmp_path)
    conv=conversation(w)
    request=body();request['model_profile_version_id']=FABLE_FULL_ID
    publish_fixture_worker(w)
    with http(w) as client:
        assert client.get('/v1/capabilities').json()['model_execution']['state']=='available'
        change_access(w,owner_ids=[])
        capability=client.get('/v1/capabilities').json()['model_execution']
        assert capability['state']=='owner_denied' and set(capability)=={'state','message'}
        response=client.post(f"/v1/projects/{w.project}/conversations/{conv['id']}/messages",
            headers={'Idempotency-Key':'owner-denied'},json=request)
        assert response.status_code>=400 and response.json()['error']['code']=='tool_unavailable'
        assert client.get(f'/v1/projects/{w.project}/sources').status_code==200
        change_access(w,enabled=False)
        assert client.get('/v1/capabilities').json()['model_execution']['state']=='disabled'
        policy=json.loads(Path(w.settings.policy_path).read_text())
        policy['total_authorized_usd']='invalid'
        Path(w.settings.policy_path).write_text(json_value(policy))
        assert client.get('/v1/capabilities').json()['model_execution']['state']=='disabled'
        with w.engine.connect() as c:assert execute(c,'SELECT count(*) FROM aist.runs').scalar_one()==0


def test_reserved_extraction_counts_current_quote_once(workspace,native,tmp_path):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'reserve-once',MIXED)
    _,_,_,run=request_note(w,source)
    ledger=Accounting(w.runs)
    call=ledger.reserve(run['id'],run['epoch'])
    ledger.observe(run['id'],run['epoch'],call['id'],200,extracted([]),native=True)
    from test_billing_reconciliation_postgres import evidence
    ledger.invoice(run['id'], call['id'], evidence(ledger, run, call, '20'), apply=True)
    native.queue=[dict(body=extracted([QUAL]))]
    result=ask(w,executor(w),current(w,run),source,'Бизнес',tmp_path)
    assert result['ok'] and len(native.requests)==1
    with w.engine.begin() as c:
        assert money_available(c,current(w,run),1)
        assert not money_available(c,current(w,run),2)


def test_final_image_body_limit_refuses_without_truncation_or_replay(workspace,native,tmp_path,monkeypatch):
    from app import pdf_images,live_profile
    w=workspace
    configure(w,tmp_path,visual=True)
    source=upload(w,'images',MIXED,following=[(['Вторая страница документа'],None)])
    _,_,_,run=request_note(w,source)
    rendered=[]
    original=pdf_images.attach
    def attach(request,pin,images,parameters):
        rendered.append(images)
        return original(request,pin,images,parameters)
    monkeypatch.setattr(pdf_images,'attach',attach)
    monkeypatch.setitem(live_profile.FABLE_VISUAL,'extraction_input_bytes',1000)
    worker=executor(w)
    first=ask(w,worker,run,source,'Все сведения о компании',tmp_path)
    assert not first['ok'] and first['error']['code']=='budget_exceeded'
    assert 'Тело не обрезано' in first['error']['message']
    assert len(rendered)==1 and len(rendered[0])==2
    again=ask(w,worker,current(w,run),source,'Все сведения о компании',tmp_path/'restart')
    assert not again['ok'] and len(rendered)==1 and not native.requests
    assert not audit(w,run)[0]


def test_unpriced_cache_retains_conservative_reservation(workspace,tmp_path):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'unknown-cache',MIXED)
    _,_,_,run=request_note(w,source)
    ledger=Accounting(w.runs)
    call=ledger.reserve(run['id'],run['epoch'])
    ledger.observe(run['id'],run['epoch'],call['id'],200,cached_extracted([]),native=True)
    with w.engine.connect() as c:
        assert execute(c,'SELECT exposure FROM aist.model_calls WHERE id=:id',id=call['id']).scalar_one()==15
    assert audit(w,run)[1][-1]['estimated_usd']['value'] is None


def test_demo_standard_messages_reject_paid_tools(workspace,tmp_path):
    w=workspace
    configure(w,tmp_path)
    source=upload(w,'standard',MIXED)
    _,_,_,run=request_note(w,source)
    outgoing=transport.model_request(run,'agent',dict(messages=[],speed='fast',service_tier='priority',cache_control={'type':'ephemeral'}))
    assert 'speed' not in outgoing and 'service_tier' not in outgoing and 'cache_control' not in outgoing
    assert outgoing['max_tokens']==64000 and outgoing['output_config']=={'effort':'xhigh'}
    for forbidden in ({'tools':[{'type':'web_search_20250305','name':'web_search'}]}, {'mcp_servers':[{'url':'https://example.invalid'}]}):
        with pytest.raises(ApiError):transport.model_request(run,'agent',dict(messages=[],**forbidden))
    assert not audit(w,run)[0]


def test_metered_demo_preserves_unknown_and_counts_calls_above_all_old_caps(workspace, tmp_path):
    w = workspace
    policy = configure(w, tmp_path)
    source = upload(w, 'metered', MIXED)
    _, _, _, old = request_note(w, source)
    ledger = Accounting(w.runs)
    pending = ledger.reserve(old['id'], old['epoch'])
    ledger.mark_unknown(old['id'], pending['id'], 'synthetic unknown')
    w.runs.stop(old['id'], old['epoch'], 'outcome_unknown')
    w.runs.release_finished(old['id'], old['epoch'], old['worker_id'])

    policy.update(total_authorized_usd=None, project_limit=None)
    policy['run'].update(limit=None, policy_version=run_clock.METERED_DEMO_VERSION)
    path = Path(w.settings.policy_path)
    path.write_text(json_value(policy))
    w.runs = Runs(w.service, w.settings)
    _, _, _, new = request_note(w, source)
    assert new['input']['budget']['limit'] is None
    assert old['input']['budget']['limit'] == '50'
    assert w.runs.capabilities(w.actor)['model_execution']['state'] == 'busy'
    assert w.runs.capabilities('usr_other')['model_execution']['state'] == 'owner_denied'

    ledger = Accounting(w.runs)
    for _ in range(new['input']['budget']['max_model_calls']):
        ledger.reserve(new['id'], new['epoch'])
    with pytest.raises(ApiError) as limit:
        ledger.reserve(new['id'], new['epoch'])
    assert limit.value.body['error']['code'] == 'budget_exceeded'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT sum(exposure) FROM aist.model_calls').scalar_one() > 100
        assert execute(c, "SELECT exposure FROM aist.model_calls WHERE id=:id AND data->>'transport_state'='unknown'",
                       id=pending['id']).scalar_one() == 15
        assert money_available(c, current(w, new), 3)
    policy['project_limit'] = '50'
    with pytest.raises(ValueError):
        run_clock.validate_demo_policy(policy)


@pytest.mark.parametrize('has_financials', [True, False])
def test_interrupted_second_extraction_uses_only_committed_first(workspace, native, tmp_path, has_financials):
    from test_fable_postgres import THEMATIC_FACTS, THEMATIC_LINES
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'issuer', THEMATIC_LINES)
    _, _, _, run = request_note(w, source)
    worker = executor(w)
    first = THEMATIC_FACTS[0] if has_financials else [QUAL]
    native.queue = [dict(body=extracted(first)), dict(body=extracted([QUAL]), incomplete=True)]
    args = dict(source_version_id=source['id'], question='Первая тема')
    result = runtime.obtain_facts(worker, run, args, tmp_path, None)
    assert len(result['facts']) == len(first)
    for attempt in range(2):
        with pytest.raises(ApiError):
            runtime.obtain_facts(worker, run, dict(args, question='Вторая тема'), tmp_path, None)
        w.runs.recover()
    saved = current(w, run)
    assert saved['state'] == 'failed' and saved['error']['code'] == 'outcome_unknown'
    assert saved['result_version_id'] is None
    assert len(native.requests) == 2
    with w.engine.connect() as c:
        messages = list(execute(c, """SELECT text,artifact_version_id FROM aist.messages
            WHERE run_id=:r AND role='assistant'""", r=run['id']).mappings())
        assert len(messages) == 1 and messages[0]['artifact_version_id'] is None
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        assert execute(c, """SELECT exposure FROM aist.model_calls WHERE run_id=:r AND data->>'transport_state'='unknown'""",
                       r=run['id']).scalar_one() == 15
    assert 'Записка не сформирована' in messages[0]['text']


@pytest.mark.parametrize(('elapsed', 'remaining_window', 'fits'), [
    (243, 169, False), (179, 189, False), (179, 224, False), (180, 265, True), (2, 85, True),
    (400, 350, False), (400, 465, True),
])
def test_second_extraction_window_uses_first_duration(workspace, native, tmp_path, monkeypatch,
                                                      elapsed, remaining_window, fits):
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'issuer', MIXED)
    _, _, _, run = request_note(w, source)
    native.queue = [dict(body=extracted([GOOD]))]
    ask(w, executor(w), run, source, 'Финансовые показатели', tmp_path)
    with w.engine.begin() as c:
        observed = execute(c, "SELECT data FROM aist.model_calls WHERE run_id=:r", r=run['id']).scalar_one()
        observed['sent_at'] = run['created_at'].isoformat()
        observed['observed_at'] = (run['created_at'] + timedelta(seconds=elapsed)).isoformat()
        execute(c, 'UPDATE aist.model_calls SET data=CAST(:data AS jsonb) WHERE id=:id', data=json_value(observed), id=observed['id'])
        now = run['created_at'] + timedelta(seconds=run['input']['budget']['max_duration_seconds']-360-remaining_window)
        monkeypatch.setattr(run_clock, 'utcnow', lambda: now)
        assert collection.shortfall(c, current(w, run)) == (None if fits else 'previous_latency')
    if fits:
        native.queue = [dict(body=extracted([QUAL]))]
    result = ask(w, executor(w), current(w, run), source, 'Бизнес, рынок и риски', tmp_path)
    assert result['ok'] == fits
    assert len(native.requests) == (2 if fits else 1)
    if not fits:
        current_run = current(w, run)
        assert current_run['state'] == 'running' and collection.state(current_run)['reason'] == 'previous_latency'
        # A known gap cannot be published as a complete note, even with a usable row.
        from test_perf02_r4_postgres import save_args
        content = dict(title='Synthetic: справка', audience='Аналитик', sections=[
            dict(section_id='sec_finance', title='Финансы', blocks=[dict(block_id='block_table', kind='table',
                caption='Выручка', rows=[table_row('row_rev','Выручка',GOOD['period']['label'],GOOD['amount']['value'],
                                                    [ref(source, observed_fact['data']['fact_id'])])
                                          for observed_fact in fact_rows(w)])])])
        saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
        assert not saved['ok'] and 'не завершён' in saved['error']['message']

def test_empty_material_business_theme_stops_before_composing(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'issuer', MIXED)
    _, _, _, run = request_note(w, source)
    native.queue = [dict(body=extracted([GOOD])), dict(body=extracted([]))]
    assert runtime.execution_facts(executor(w), run, 'Подготовь справку о компании.', tmp_path) == (None, None)
    saved = current(w, run)
    assert saved['state'] == 'failed' and not saved['result_version_id']
    assert len(native.requests) == 2
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        assert 'Не удалось завершить сбор' in execute(c, "SELECT text FROM aist.messages WHERE role='assistant'").scalar_one()


def test_composer_must_use_both_material_themes(workspace, native, tmp_path):
    from test_perf02_r4_postgres import save_args
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'issuer', MIXED)
    _, _, _, run = request_note(w, source)
    native.queue = [dict(body=extracted([GOOD])), dict(body=extracted([QUAL]))]
    facts, _ = runtime.execution_facts(executor(w), run, 'Подготовь справку.', tmp_path)
    assert len(facts) == 2
    note = content(w, source)
    note['sections'] = note['sections'][:1]
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note))
    assert not reply['ok'] and 'пропущена обработанная тема' in reply['error']['message']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0


def test_reference_labels_only_resolve_exact_ids_in_reference_fields():
    from app.model_context import translate_references
    mapping = {'f_1':'fact_first', 's_1':'sv_first', 'v_1':'visual_first'}
    claim = dict(text='f_1 и s_1 записаны в исходном тексте', fact_refs=[
        dict(fact_id='f_1', source_version_id='s_1'), dict(fact_id='f_10', source_version_id='sv_foreign')],
        visual_observation_ids=['v_1', 'v_100'])
    value = translate_references(claim, mapping)
    assert value['text'] == claim['text'] and value['fact_refs'] == [
        dict(fact_id='fact_first', source_version_id='sv_first'), dict(fact_id='f_10', source_version_id='sv_foreign')]
    assert value['visual_observation_ids'] == ['visual_first','v_100'] and claim['fact_refs'][0]['fact_id'] == 'f_1'
