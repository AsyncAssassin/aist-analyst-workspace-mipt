"""Непроверенные визуальные основания: квитанция, черновик, редактура и строгие границы."""
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import zipfile

import pytest
from test_perf02_r4_postgres import model_save_args
from pypdf import PdfReader

from app import extraction, extraction_requests, pdf_images, openclaw_runtime as runtime
from app.errors import ApiError
from app.exports import Exports
from app.facts import pin_of
from app.live_profile import FABLE_VISUAL_ID
from app.model_context import model_view
from app.runs import execute, json_value
from app.service import ident
from app.versions import write as write_version
from app.visual_observations import available
from test_d15_postgres import workspace
from test_draft01_postgres import http, export_request
from test_fable_postgres import native, start_fable, extracted, reply
from test_flow01_postgres import current, executor
from test_note_path06_r1_postgres import start, own_items, ask, inspect_images, note
from test_perf02_r1_postgres import fact_rows
from test_perf02_r4_postgres import save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def observation(value='28.4', year=2020, *, page=1, forecast=False):
    return dict(page=page, area='Столбцы диаграммы в нижней части страницы',
                text=f'Выручка: {value} млн руб., {year}, ' + ('прогноз' if forecast else 'историческое значение'),
                numeric=dict(metric='Выручка', subject='Север', amount=dict(value=value, unit='money', scale='1000000', currency='RUB'),
                             period=dict(kind='interval', label=str(year), start=f'{year}-01-01', end=f'{year}-12-31'),
                             nature='forecast' if forecast else 'reported_actual'))


def response(payload):
    value = extracted([])
    value['content'] = [dict(type='text', text=json_value(payload))]
    return value


def packet(*, only_visual=False):
    return dict(facts=[] if only_visual else own_items(), visual_observations=[observation(), observation('43.7', 2027, forecast=True),
        dict(page=1, area='Схема бизнеса справа', text='Партнер предоставляет место для оборудования.', numeric=None)])


def content(result, *, wrong=False):
    observations = result['visual_observations']
    blocks = [] if not result['facts'] else note(result['facts'], wrong=wrong)['sections'][0]['blocks'][:3]
    blocks.extend([
        dict(block_id='block_visual', kind='paragraph', claim=dict(text=observations[2]['text'], origin='inference', fact_refs=[],
             calculation_ids=[], visual_observation_ids=[observations[2]['observation_id']])),
        dict(block_id='block_table', kind='table', caption='Визуальные данные', rows=[dict(row_id=f'row_visual{i}', label=item['numeric']['metric'],
             period_label=item['numeric']['period']['label'], amount=item['numeric']['amount'], fact_refs=[], calculation_ids=[],
             visual_observation_ids=[item['observation_id']]) for i,item in enumerate(observations[:2])])])
    return dict(title='Тестовая записка', audience='Аналитик', sections=[dict(section_id='sec_result', title='Результаты', blocks=blocks)])


def save(w, run, result, *, wrong=False):
    args = save_args(content(result, wrong=wrong))
    value = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert value['ok'], value
    return value['result']


def codes(view, block=None):
    return {f['code'] for f in view['checks'][0]['findings'] if block is None or f['block_id']==block}


def edit(w, parent, payload):
    version = parent['version']
    return write_version(w.runs,w.actor,w.session,w.project,version['artifact_id'],None,'saveVersion',ident('key'),
        dict(expected_revision=version['revision'],parent_version_id=version['id'],payload=payload))[1]


@pytest.mark.parametrize('only_visual', [False, True])
def test_visual_snapshot_replay_and_draft(workspace,native,tmp_path,monkeypatch,only_visual):
    w=workspace;source,run=start(w);payload=packet(only_visual=only_visual);original=deepcopy(payload)
    native.queue=[dict(body=response(payload))]
    result=ask(w,run,source,tmp_path)
    assert payload==original and result['status']=='complete'
    assert len(result['facts'])==(0 if only_visual else 2) and len(result['visual_observations'])==3
    with w.engine.connect() as c:pages=extraction.source_pages(w.runs,c,run)
    images=inspect_images(native.requests[0],pin_of(source),pages)
    operation,=extraction_requests.operations(current(w,run)).values()
    for item in result['visual_observations']:
        assert item['source']==pin_of(source) and item['verification_status']=='unchecked'
        assert item['image_sha256']==images[0]['sha256'] and item['extraction_call_id']==operation['response_call_id']
        assert 'compatibility' not in item and 'evidence' not in item
    assert model_view(result)['visual_observations']==result['visual_observations']
    monkeypatch.setattr(pdf_images,'render',lambda *a,**kw:pytest.fail('Кэш не требует рендера'))
    assert ask(w,current(w,run),source,tmp_path/'new-connection')==result and len(native.requests)==1
    draft=save(w,run,result)
    assert len(draft['version']['basis']['visual_observations'])==3
    assert codes(draft)=={'unchecked_visual_observation'}
    assert w.runs.version(w.actor,w.project,draft['version']['id'])==draft
    Path('/result/'+('visual-only' if only_visual else 'mixed')+'.json').write_text(json_value(dict(result=result,view=draft,posts=len(native.requests))))


@pytest.mark.parametrize('invalid', ['page','status','id','pin','sha','over_limit','period'])
def test_malformed_visual_response_is_strict(workspace,native,tmp_path,invalid):
    w=workspace;source,run=start(w);payload=packet()
    item=payload['visual_observations'][0]
    if invalid=='page':item['page']=2
    elif invalid=='status':item['verification_status']='verified'
    elif invalid=='id':item['observation_id']='visual_forged'
    elif invalid=='pin':item['source']=pin_of(source)
    elif invalid=='sha':item['image_sha256']='a'*64
    elif invalid=='over_limit':payload['visual_observations']=[deepcopy(item) for _ in range(65)]
    else:item['numeric']['period']['end']='2019-12-31'
    native.queue=[dict(body=response(payload))]
    with pytest.raises(ApiError):ask(w,run,source,tmp_path)
    assert fact_rows(w)==[] and not available(current(w,run))
    assert len(native.requests)==1


def test_unrelated_run_unknown_id_short_answer_and_calculation(workspace,native,tmp_path):
    w=workspace;source,run=start(w);native.queue=[dict(body=response(packet()))];result=ask(w,run,source,tmp_path)
    _,_,other=start_fable(w,full=True,source=source,profile_id=FABLE_VISUAL_ID)
    bad=w.runs.tool(other['id'],other['epoch'],ident('tool'),'aist_save_artifact',save_args(content(result)))
    assert not bad['ok'] and bad['error']['code']=='resource_not_found'
    unknown=deepcopy(result);unknown['visual_observations'][0]['observation_id']='visual_absent'
    bad=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',save_args(content(unknown)))
    assert not bad['ok'] and bad['error']['code']=='resource_not_found'
    visual_ref=dict(source_version_id=source['id'],fact_id=result['visual_observations'][0]['observation_id'])
    bad=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_calculate',dict(formula_id='growth_percent.v1',
        inputs=[dict(name='previous',fact=visual_ref),dict(name='current',fact=visual_ref)],rounding=dict(mode='HALF_UP',places=2)))
    assert not bad['ok']
    from app.versions import review
    with w.engine.begin() as c:
        with pytest.raises(ApiError):review(w.runs,c,current(w,run),dict(result_type='evidence_answer',schema_version='1.0.0',
            content=dict(claims=[content(result)['sections'][0]['blocks'][-2]['claim']],missing_information=[])))
    forged=deepcopy(content(result));forged['sections'][0]['blocks'][-2]['claim']['origin']='human_editorial'
    assert not w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',save_args(forged))['ok']
    assert len(native.requests)==1


def test_dependency_edits_and_http_release(workspace,native,tmp_path):
    w=workspace;source,run=start(w);native.queue=[dict(body=response(packet()))];result=ask(w,run,source,tmp_path)
    # A visual reference must not authorize an ordinary amount or period.
    original=content(result,wrong=True)
    original['sections'][0]['blocks'][0]['claim']['visual_observation_ids']=[result['visual_observations'][0]['observation_id']]
    saved=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',save_args(original))
    assert saved['ok'],saved
    view=saved['result']
    assert {'unchecked_visual_observation'}==codes(view,'block_money')
    payload=deepcopy(view['version']['payload'])
    blocks=payload['content']['sections'][0]['blocks']
    blocks[0]['claim'].pop('visual_observation_ids')
    blocks[-2]['claim'].pop('visual_observation_ids');blocks[-2]['claim']['text']='Партнер предоставляет площадку для оборудования.'
    blocks[-1]['rows'][0].pop('visual_observation_ids')
    retained=edit(w,view,payload)
    assert {'unchecked_visual_observation'}==codes(retained,'block_money')
    assert len(retained['version']['basis']['visual_observations'])==3
    assert all(b['claim'].get('visual_observation_ids') for b in retained['version']['payload']['content']['sections'][0]['blocks'] if b['block_id'] in ('block_money','block_visual'))
    payload=deepcopy(retained['version']['payload']);blocks=payload['content']['sections'][0]['blocks']
    blocks[0]['claim']['text']='Выручка составила 7,25 млн руб. в 2021 году.'
    blocks.remove(next(b for b in blocks if b['block_id']=='block_visual'))
    # One typed visual row is still deliberately inconsistent with its own snapshot.
    blocks[-1]['rows'][1]['amount']['value']='99'
    fixed=edit(w,retained,payload)
    assert codes(fixed,'block_money')=={'unchecked_visual_observation'}
    assert not codes(fixed,'block_visual') and 'visual_row_mismatch' in codes(fixed,'block_table')
    assert len(fixed['version']['basis']['visual_observations'])==2
    w.settings=replace(w.settings,model_profile_id='model_novita-mimo-v1',live_models_enabled=False,executor_mode='disabled',embeddings_enabled=False)
    client=http(w)
    client.__enter__()
    try:
        version=fixed['version'];base=f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
        assert client.get(f'/v1/projects/{w.project}/artifact-versions/{version["id"]}').json()==fixed
        for acknowledge in (False,True):
            denied=client.post(base+'/accept',json=dict(expected_revision=0,content_sha256=version['content_sha256'],acknowledge_unchecked_semantics=acknowledge),headers={'Idempotency-Key':ident('key')})
            assert denied.status_code==422,denied.text
        denied=client.post(base+'/exports',json=export_request('pdf','accepted'),headers={'Idempotency-Key':ident('key')})
        assert denied.status_code==409 and denied.json()['error']['code']=='acceptance_required',denied.text
    finally:client.__exit__(None,None,None)
    # Export the consistent edited draft, keeping row-level and paragraph annotations.
    payload=deepcopy(retained['version']['payload'])
    payload['content']['sections'][0]['blocks'][0]['claim']['text']='Выручка составила 7,25 млн руб. в 2021 году.'
    final=write_version(w.runs,w.actor,w.session,w.project,fixed['version']['artifact_id'],None,'saveVersion',ident('key'),
        dict(expected_revision=fixed['version']['revision'],parent_version_id=retained['version']['id'],payload=payload))[1]
    exports=Exports(w.runs);files={};target=Path('/result/files');target.mkdir(exist_ok=True)
    for fmt in ('docx','pdf'):
        version=final['version']
        _,entry=exports.request(w.actor,w.session,w.project,version['artifact_id'],version['id'],ident('key'),export_request(fmt,'draft'))
        exports.process_one();data,ready=exports.get(w.actor,w.project,entry['id'],download=True)
        assert ready['state']=='ready',ready
        assert exports.get(w.actor,w.project,entry['id'],download=True)[0]==data
        (target/f'visual-draft.{fmt}').write_bytes(data)
        if fmt=='pdf':text='\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(data)).pages)
        else:
            from lxml import etree
            with zipfile.ZipFile(io.BytesIO(data)) as archive:text='\n'.join(etree.fromstring(archive.read('word/document.xml')).xpath('//*[local-name()="t"]/text()'))
        assert text.count('Записка содержит непроверенные сведения')==1
        assert ' '.join(text.split()).count('По изображению, стр. 1, не проверено')==4
        assert 'не проверено; историческое значение' in ' '.join(text.split()) and 'не проверено; прогноз' in ' '.join(text.split())
        assert 'visual_' not in text and 'reported_actual' not in text and '28,4' in text
        files[fmt]=dict(bytes=len(data),text=text,export=ready)
    assert len(native.requests)==1
    Path('/result/dependency-http-export.json').write_text(json_value(dict(first=view,retained=retained,fixed=fixed,final=final,files=files,posts=1)))


def test_installed_openclaw_composes_visual_draft(workspace,native,tmp_path):
    w=workspace;source,run=start(w)
    commands=[]
    def composed():
        result=ask(w,current(w,run),source,tmp_path/'reconnected')
        command=model_save_args(content(result));commands.append(command)
        return reply('aist_save_artifact',command,identifier='toolu_visual_save')
    native.queue=[dict(body=reply(args=dict(question='Все данные'))),dict(body=response(packet())),dict(body=reply(args=dict(question='Все данные'),identifier='toolu_repeat')),dict(body=composed)]
    with TemporaryDirectory(prefix='visual-r2-runtime-',dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1',0));provider.bind(('127.0.0.1',0))
        w.runs.settings=replace(w.settings,openclaw_root=root,gateway_port=gateway.getsockname()[1],provider_port=provider.getsockname()[1])
        gateway.close();provider.close();worker=executor(w)
        timer=threading.Timer(100,worker.draining.set);timer.start()
        try:runtime.run(worker,run)
        finally:timer.cancel()
        state=w.runs.get(w.actor,w.project,run['id']);assert state['state']=='completed',state
        view=w.runs.version(w.actor,w.project,state['result_version_id'])
        assert codes(view)=={'unchecked_visual_observation'} and len(native.requests)==4
        with w.engine.connect() as c:
            assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalar()==1
            call=execute(c,"SELECT * FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_save_artifact'",r=run['id']).mappings().one()
            pages=extraction.source_pages(w.runs,c,run)
        assert w.runs.tool(run['id'],run['epoch'],call['executor_tool_call_id'],'aist_save_artifact',runtime.save_operation(run,commands[0]))['ok']
        inspect_images(native.requests[1],pin_of(source),pages)
        assert 'VisualObservationCandidate' in json_value(native.requests[1])
        assert 'visual_observations' in json_value(native.requests[2])
        assert len(native.requests)==4
        Path('/result/runtime.json').write_text(json_value(dict(state=state,view=view,posts=4,
            request_has_images=True,composer_has_visuals=True,versions=1,extraction_limit=current(w,run)['configuration']['collection']['max_extractions'])))


def test_visual_authority_before_save(workspace,native,tmp_path):
    from app.versions import review
    from app.search import register
    w=workspace;source,run=start(w);native.queue=[dict(body=response(packet(only_visual=True)))];result=ask(w,run,source,tmp_path)
    payload=save_args(content(result))['payload']
    for change in ('owner','project','sha','pin'):
        context=deepcopy(current(w,run))
        if change=='owner':context['owner_id']='usr_foreign'
        elif change=='project':context['project_id']='prj_foreign'
        elif change=='sha':context['input']['source_versions'][0]['sha256']='b'*64
        else:context['input']['source_versions'][0]['source_id']='src_foreign'
        with w.engine.begin() as c:
            with pytest.raises(ApiError):review(w.runs,c,context,payload)
    register(w.service,w.actor,w.project,source['id'],'samples',None,None,'Явно тестовый образец')
    with w.engine.begin() as c:
        with pytest.raises(ApiError) as refused:review(w.runs,c,current(w,run),payload)
    assert refused.value.code=='sample_not_evidence' and len(native.requests)==1


def test_existing_page_outside_transmitted_images(workspace,native,tmp_path,monkeypatch):
    from app import extraction_context
    w=workspace;source,run=start(w,following=[(['Вторая страница источника'],None)])
    select=extraction_context.select_pages
    def first_only(pages,*args):
        _,coverage=select(pages,*args)
        return {1:pages[1]},dict(coverage,pages=[1],full_document=False)
    monkeypatch.setattr(extraction_context,'select_pages',first_only)
    payload=packet(only_visual=True);payload['visual_observations'][0]['page']=2
    native.queue=[dict(body=response(payload))]
    with pytest.raises(ApiError):ask(w,run,source,tmp_path)
    assert len(native.requests)==1 and not available(current(w,run))


def test_visual_receipt_recovery_cache_and_collection_progress(workspace,native,tmp_path,monkeypatch):
    from app import collection
    w=workspace;source,run=start(w);native.queue=[dict(body=response(packet(only_visual=True)))]
    ingest=runtime.ingest
    def interrupted(*args,**kwargs):raise RuntimeError('После сохранения квитанции')
    monkeypatch.setattr(runtime,'ingest',interrupted)
    with pytest.raises(RuntimeError):ask(w,run,source,tmp_path)
    assert len(native.requests)==1 and not available(current(w,run))
    monkeypatch.setattr(runtime,'ingest',ingest)
    monkeypatch.setattr(pdf_images,'render',lambda *args,**kwargs:pytest.fail('Повторный рендер при восстановлении'))
    result=ask(w,current(w,run),source,tmp_path/'recover')
    assert collection.state(current(w,run))['idle']==0
    assert ask(w,current(w,run),source,tmp_path/'repeat')==result
    assert collection.state(current(w,run))['idle']==0 and len(native.requests)==1
    _,_,following=start_fable(w,full=True,source=source,profile_id=FABLE_VISUAL_ID)
    assert not available(following)
    cached=ask(w,following,source,tmp_path/'cache')
    assert cached==result and len(native.requests)==1
    draft=save(w,following,cached)
    assert codes(draft)=={'unchecked_visual_observation'}
    Path('/result/recovery-cache.json').write_text(json_value(dict(result=result,draft=draft,local_posts=1,
        recovery_rendered=False,cache_bound_to_following_run=True,collection=collection.state(current(w,run)))))


@pytest.mark.parametrize('change', ['metric','sign','scale','currency','period'])
def test_typed_visual_row_keeps_its_own_observation(change):
    from app.visual_observations import row_agrees
    item=observation();numeric=item['numeric']
    row=dict(label='Выручка, млн руб.',period_label=numeric['period']['label'],amount=deepcopy(numeric['amount']))
    assert row_agrees(row,item)
    if change=='metric':row['label']='Прибыль'
    elif change=='sign':row['amount']['value']='-28.4'
    elif change=='scale':row['amount']['scale']='1000'
    elif change=='currency':row['amount']['currency']='USD'
    else:row['period_label']='2022'
    assert not row_agrees(row,item)


def test_unused_visual_observations_do_not_block_ordinary_note(workspace,native,tmp_path):
    from test_draft01_postgres import accept
    w=workspace;source,run=start(w);native.queue=[dict(body=response(packet()))];result=ask(w,run,source,tmp_path)
    ordinary=note(result['facts']);ordinary['sections'][0]['blocks']=ordinary['sections'][0]['blocks'][:3]
    saved=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',save_args(ordinary))
    assert saved['ok'],saved
    view=saved['result'];assert codes(view)==set() and 'visual_observations' not in view['version']['basis']
    status,review=accept(w,view)
    assert status==200 and review['acceptance'] is not None and len(native.requests)==1
