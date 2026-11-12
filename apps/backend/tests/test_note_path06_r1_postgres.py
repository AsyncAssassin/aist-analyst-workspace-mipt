"""Изображения и денежная фраза через закрепленный PDF и локальный Messages-приемник."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from app import runs as run_clock
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading

import pytest
from test_perf02_r4_postgres import model_save_args

from app import extraction, extraction_requests, pdf_images, openclaw_runtime as runtime
from app.accounting import Accounting
from app.errors import ApiError
from app.facts import pin_of
from app.live_profile import FABLE_VISUAL_ID, FABLE_VISUAL, input_limit
from app.normalization import fingerprint
from app.runs import execute, json_value
from app.service import ident
from test_d15_postgres import workspace
from test_fable_postgres import native, start_fable, extracted, reply, audit
from test_flow01_postgres import current, executor
from test_flow01_02_postgres import QUAL, TRLN
from test_perf02_r1_postgres import upload, accounted_response, fact_rows
from test_perf02_r4_postgres import paragraph, save_args
from test_note_path06_r1 import prose

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')
LINES=['Север: выручка выросла до 7,25 млн руб. в 2021 году, доля рынка 12%.',
       'Компания поставляет оборудование клиентам.']


def start(w, *, following=()):
    source=upload(w,'Нейтральный PDF',LINES,following=following)
    _,_,run=start_fable(w,full=True,source=source,profile_id=FABLE_VISUAL_ID)
    return source,run


def own_items():
    return [prose(LINES[:1]), dict(metric=None,subject='Север',reporting_basis='Не указано',nature='source_statement',
        period=dict(kind='unknown',label='Не указано',start=None,end=None),amount=None,value_text=None,basis=None,
        evidence=[dict(page=1,line_start=2,line_end=2,area='Бизнес')])]


def ask(w,run,source,directory,question='Все данные'):
    return runtime.obtain_facts(executor(w),run,dict(source_version_id=source['id'],question=question),directory,None)


def inspect_images(request, pin, pages):
    blocks=request['messages'][-1]['content']
    text=json.loads(blocks[0]['text'])
    assert {p['page']:'\n'.join(line['text'] for line in p['lines']) for p in text['pages']}==pages
    metadata=[]
    for index,page in enumerate(pages):
        mapping=json.loads(blocks[1+index*2]['text']); image=blocks[2+index*2]
        data=base64.b64decode(image['source']['data'],validate=True)
        assert data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9')
        assert image['type']=='image' and image['source']['media_type']=='image/jpeg'
        assert mapping==dict(source=pin,page=page,image_sha256=sha256(data).hexdigest(),render=FABLE_VISUAL['page_images'])
        metadata.append(dict(page=page,sha256=mapping['image_sha256'],byte_size=len(data)))
    assert len(blocks)==1+2*len(pages)
    return metadata


def test_receiver_images_unchanged_text_and_reconnected_result_without_renderer(workspace,native,tmp_path,monkeypatch):
    w=workspace; source,run=start(w,following=[(['Вторая страница','Подпись другой страницы'],None)])
    native.queue=[dict(body=extracted(own_items()))]
    render=pdf_images.render
    def unlocked_render(*args,**kwargs):
        with w.engine.begin() as c:execute(c,'SELECT id FROM aist.runs WHERE id=:r FOR UPDATE NOWAIT',r=run['id'])
        return render(*args,**kwargs)
    monkeypatch.setattr(pdf_images,'render',unlocked_render)
    result=ask(w,run,source,tmp_path)
    assert result['status']=='complete' and len(result['facts'])==2
    with w.engine.connect() as c:pages=extraction.source_pages(w.runs,c,run)
    images=inspect_images(native.requests[0],pin_of(source),pages)
    operation,=extraction_requests.operations(current(w,run)).values()
    assert operation['images']==images and operation['request_sha256']==fingerprint(native.requests[0])
    assert operation['body_sha256']==sha256(json_value(native.requests[0]).encode()).hexdigest()
    assert 'только на изображении' in result['coverage']['qualification']
    before=fact_rows(w)
    monkeypatch.setattr(pdf_images,'render',lambda *a,**kw: pytest.fail('Повторное чтение не требует рендера'))
    assert ask(w,current(w,run),source,tmp_path/'reconnect',question='  ВСЕ   ДАННЫЕ  ')==result
    assert fact_rows(w)==before and len(native.requests)==1
    assert all('data:image' not in json_value(f) for f in result['facts'])
    Path('/result/visual-receiver.json').write_text(json_value(dict(images=images,result=result,operation=operation)))


def test_visual_extraction_finishes_after_scaled_old_window(workspace, native, tmp_path, monkeypatch):
    from app import collection
    w = workspace
    # Pin the scaled admission floor when creating the synthetic Run; inputs stay immutable.
    bounded_budget = collection.bounded_budget
    def scaled_budget(*args):
        return dict(bounded_budget(*args), min_extraction_seconds=0.06)
    monkeypatch.setattr(collection, 'bounded_budget', scaled_budget)
    source, run = start(w)
    assert run['configuration']['collection']['extraction_seconds'] == 600
    worker = executor(w)
    # Physical HTTP response crosses the old 300-second window, scaled to 0.3.
    monkeypatch.setattr(worker.transport, 'send_seconds', lambda current, purpose='agent': 0.6)
    native.queue = [dict(body=extracted(own_items()), delay=0.4)]
    result = runtime.obtain_facts(worker, run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    assert result['status'] == 'complete' and len(native.requests) == 1
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == 1 and calls[0]['transport_state'] == 'observed'
    timings = json.loads((tmp_path / 'calls' / (calls[0]['id'] + '-timings.json')).read_text())
    stages = timings['stages_seconds']
    assert 0.3 < stages['first_event'] < 0.6
    assert stages['message_stop'] is not None and stages['read_complete'] is not None
    assert usage[0]['reservation_state'] == 'released'


@pytest.mark.parametrize('failure',['render','oversize','time_before','time_after','unknown','wrong_sha','old_run','cancel_after','foreign_source'])
def test_preparation_failures_never_send_or_fall_back(workspace,native,tmp_path,monkeypatch,failure):
    w=workspace; source,run=start(w)
    limits = run['configuration']['collection']
    closed_at = run['created_at'] + timedelta(seconds=run['input']['budget']['max_duration_seconds']
        - limits['completion_seconds'] - limits['min_extraction_seconds'] + 1)
    def rendering(*args,**kwargs):
        if failure=='render':raise extraction_requests.failure('Тест: рендер недоступен, POST не отправлен.')
        if failure=='cancel_after':
            from test_d15_postgres import invalidate
            invalidate(w,run,'cancel')
        if failure=='time_after':
            monkeypatch.setattr(run_clock,'utcnow',lambda:closed_at)
        return [dict(page=1,sha256='b'*64,byte_size=7*1024*1024,data='A'*(12*1024*1024 if failure=='oversize' else 100))]
    if failure in ('render','oversize','time_after','cancel_after'):monkeypatch.setattr(pdf_images,'render',rendering)
    if failure=='time_before':
        monkeypatch.setattr(run_clock,'utcnow',lambda:closed_at)
    if failure=='old_run':
        monkeypatch.setattr(extraction,'VERSION','future-extraction-test')
    if failure=='unknown':Accounting(w.runs).reserve(run['id'],run['epoch'],'extraction')
    if failure=='foreign_source':source=upload(w,'Чужой pin',['Другой источник'])
    with pytest.raises(ApiError) as refused:
        if failure=='wrong_sha':
            runtime.extract_context(executor(w),run,dict(pin_of(source),sha256='a'*64),'Все данные','Все данные',tmp_path,None)
        else:ask(w,current(w,run),source,tmp_path)
    assert native.requests==[]
    if failure not in ('wrong_sha','unknown','old_run','cancel_after','foreign_source'):
        assert any(entry.get('limitation') for entry in extraction.recorded_extractions(current(w,run)))
    if failure=='unknown':assert refused.value.code=='outcome_unknown'


def test_transport_and_physical_boundary_share_eight_mib(workspace,native,tmp_path):
    w=workspace; source,run=start(w); worker=executor(w)
    over='x'*(input_limit(run['input'],'extraction')+1)
    with pytest.raises(ApiError) as refused:
        worker.transport.perform(run,dict(purpose='extraction'),request=dict(messages=[dict(role='user',content=over)]),audit=tmp_path)
    assert refused.value.code=='budget_exceeded' and audit(w,run)[0]==[] and native.requests==[]
    ledger=Accounting(w.runs); call=ledger.reserve(run['id'],run['epoch'],'extraction')
    class Never:
        def request(self,*args,**kwargs):pytest.fail('Превышение на физической границе отправлено')
    assert ledger.send(run['id'],run['epoch'],call['id'],Never(),'/v1/messages',over.encode()) is None
    assert native.requests==[]


def note(facts,*,wrong=False):
    money=next(f for f in facts if f['amount'])
    qual=next(f for f in facts if f['amount'] is None)
    refs=lambda f:[dict(source_version_id=f['source']['source_version_id'],fact_id=f['fact_id'])]
    return dict(title='Нейтральная записка',audience='Аналитик',sections=[dict(section_id='sec_result',title='Результаты',blocks=[
        paragraph('block_money',('Выручка составила 8,25 млн руб. в 2020 году.' if wrong else
            'Выручка составила 7,25 млн руб. в 2021 году.'),refs(money)),
        paragraph('block_basis','Стандарт отчетности не указан; сопоставимость для расчета роста не установлена.',refs(money)),
        paragraph('block_business','Компания поставляет оборудование клиентам.',refs(qual)),
        dict(block_id='block_visual',kind='paragraph',claim=dict(text='Сведения только на изображениях не имеют проверяемого числового основания.',
             origin='inference',fact_refs=[],calculation_ids=[]))])])


def test_installed_openclaw_visual_extraction_to_draft_and_negative_checks(workspace,native,monkeypatch):
    w=workspace; source,run=start(w)
    native.queue=[dict(body=reply(args=dict(question='Все данные'))),dict(body=extracted(own_items())),
                  dict(body=lambda:reply('aist_save_artifact',model_save_args(note([r['data'] for r in fact_rows(w)])),identifier='toolu_visual_save'))]
    with TemporaryDirectory(prefix='visual-runtime-',dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1',0));provider.bind(('127.0.0.1',0))
        w.runs.settings=replace(w.settings,openclaw_root=root,gateway_port=gateway.getsockname()[1],provider_port=provider.getsockname()[1])
        gateway.close();provider.close();worker=executor(w)
        timer=threading.Timer(100,worker.draining.set);timer.start()
        try:runtime.run(worker,run)
        finally:timer.cancel()
        state=w.runs.get(w.actor,w.project,run['id'])
        directory=Path(root)/run['id'];target=Path('/result/runtime-visual');target.mkdir(exist_ok=True)
        for name in ('tools.jsonl','gateway-process.log','failure.json'):
            if (directory/name).exists():(target/name).write_bytes((directory/name).read_bytes())
        assert state['state']=='completed',state
        saved=w.runs.version(w.actor,w.project,state['result_version_id'])
        assert next(c for c in saved['checks'] if c['kind']=='semantic')['status']=='not_checked'
        assert saved['review']['acceptance'] is None
        assert not saved['version']['basis']['calculations']
        money_block=saved['version']['payload']['content']['sections'][0]['blocks'][0]['block_id']
        assert not any(f.get('block_id')==money_block for f in saved['checks'][0]['findings'])
        assert w.runs.version(w.actor,w.project,state['result_version_id'])==saved and len(native.requests)==3
        with w.engine.connect() as c:pages=extraction.source_pages(w.runs,c,run)
        inspect_images(native.requests[1],pin_of(source),pages)
        assert all(block.get('type')!='image' for m in native.requests[2]['messages'] if isinstance(m['content'],list) for block in m['content'])
        (target/'result.json').write_text(json_value(dict(run=state,saved=saved,requests=native.requests,calls=audit(w,run))))
    # Use the established editor/save checks on a new draft, without another model request.
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    _,_,next_run=start_fable(w,full=True,source=source,profile_id=FABLE_VISUAL_ID)
    facts = [r['data'] for r in fact_rows(w)]
    wrong = note(facts, wrong=True)
    money = next(f for f in facts if f['amount'])
    wrong['sections'][0]['blocks'].append(dict(block_id='block_wrong_row', kind='table', caption='Выручка', rows=[
        dict(row_id='row_wrong', label=money['metric'], period_label='2020 год',
             amount=dict(money['amount'], value='8.25'),
             fact_refs=[dict(source_version_id=source['id'], fact_id=money['fact_id'])], calculation_ids=[])]))
    edited=w.runs.tool(next_run['id'],next_run['epoch'],ident('tool'),'aist_save_artifact',save_args(wrong))
    assert edited['ok'],edited
    negative=edited['result']
    codes={f['code'] for f in negative['checks'][0]['findings'] if f.get('block_id')=='block_wrong_row'}
    assert 'row_basis_mismatch' in codes
    assert negative['checks'][0]['status']=='failed'
    from test_draft01_postgres import accept
    with pytest.raises(ApiError) as blocked:
        accept(w,negative)
    assert blocked.value.code=='validation_failed'
    (target/'negative.json').write_text(json_value(negative))
    assert len(native.requests)==3


def test_mixed_ingest_keeps_neighbors_strict_origin_and_calculation_boundary(workspace):
    from test_flow01_03_postgres import positioned_pdf,table_page,GROUPS,ROWS,COMPANY,PASSAGE,cell,line_of
    from test_perf02_r4_postgres import start_note_run
    w=workspace
    own=[f'{COMPANY}: выручка выросла до 7,25 млн руб. в 2021 году.',
         f'{COMPANY}: выручка составила 6,5 млн руб. в 2020 году.','Объем рынка 2,2 трлн руб.']
    data=positioned_pdf([table_page(GROUPS,ROWS,'rows_first',passage=PASSAGE),[(line,40,500-i*35) for i,line in enumerate(own)]])
    source=w.service.write(w.actor,'uploadSource',ident('key'),dict(title='Смешанный пакет',expected_revision=0),w.project,None,data,'mixed.pdf')[1]['version']
    run=start_note_run(w,source)
    with w.engine.connect() as c:
        pages=extraction.source_pages(w.runs,c,run);layouts=extraction.source_pages(w.runs,c,run,layout=True)
    money=prose(own[:1],subject=COMPANY)
    money['evidence'][0]['page']=2;money['basis']['page']=2
    previous=prose(own[1:2],subject=COMPANY,value='6.5',raw='6,5',year=2020)
    previous['evidence'][0].update(page=2,line_start=2,line_end=2);previous['basis']['page']=2
    line=line_of(pages,1,PASSAGE)
    qual=dict(QUAL,subject=COMPANY,evidence=[dict(page=1,line_start=line,line_end=line,area='Бизнес')])
    table=cell(pages,layouts,1,'Выручка от продаж',2)
    old_table=cell(pages,layouts,1,'Выручка от продаж',1)
    unsupported=deepcopy(TRLN);unsupported.update(subject=COMPANY,evidence=[dict(page=2,line_start=3,line_end=3,area='Рынок')])
    unsupported['basis'].update(page=2,reporting_page=1)
    body=dict(facts=[money,qual,table,unsupported,previous,old_table]);original=deepcopy(body)
    call=accounted_response(w,run,body)
    result=extraction.ingest(w.runs,run,pages,body,call,pin_of(source),layouts,question='Смешанный пакет')
    assert result['status']=='partial' and len(result['facts'])==5 and result['skipped'][0]['code']=='ambiguous_link'
    assert body==original
    before=fact_rows(w)
    assert extraction.ingest(w.runs,run,pages,body,call,pin_of(source),layouts,question='Смешанный пакет')==result
    assert fact_rows(w)==before
    for wrong in ('line','source','sha','quote'):
        bad=deepcopy(body);pin=pin_of(source)
        if wrong=='line':bad['facts'][0]['evidence'][0]['line_end']=999
        if wrong=='source':pin['source_id']='src_foreign'
        if wrong=='sha':pin['sha256']='f'*64
        if wrong=='quote':bad['facts'][0]['evidence'][0]['quote']='Поддельный текст'
        with pytest.raises(ApiError):extraction.ingest(w.runs,run,pages,bad,call,pin,layouts,question='Подделка')
        assert fact_rows(w)==before
    def calculate(metric):
        pair=sorted([f for f in result['facts'] if f['metric']==metric],key=lambda f:f['period']['start'])
        args=dict(formula_id='growth_percent.v1',rounding=dict(places=2,mode='HALF_UP'),inputs=[
            dict(name=name,fact=dict(source_version_id=source['id'],fact_id=f['fact_id'])) for name,f in zip(('previous','current'),pair)])
        return w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_calculate',args)
    unknown=calculate('выручка');known=calculate('Выручка от продаж')
    assert not unknown['ok'] and known['ok']
    assert known['result']['rounded']['value']=='15.09'
    # Removing table cannot convert its disconnected cells into prose or waive a wrong column.
    bad=deepcopy(table);bad['period']=deepcopy(old_table['period']);bad.pop('table')
    with pytest.raises(ApiError):extraction.candidate(pin_of(source),pages,bad,layouts)
    Path('/result/mixed-ingest.json').write_text(json_value(dict(outcome=result,unknown_basis_growth=unknown,verified_growth=known)))


def test_old_text_cache_and_unknown_visual_send_never_become_reusable(workspace,native,tmp_path):
    w=workspace;source,run=start(w)
    # Same current question/profile, but a text-only record: even this cannot substitute images.
    entry=dict(source=pin_of(source),version=extraction.VERSION,question='Все данные',call_id='call_old',fact_refs=[],status='empty',skipped=[],
        request_key=extraction.request_key(pin_of(source),'Все данные'),question_key=extraction.request_key(pin_of(source),'Все данные'),
        model_profile_version_id=FABLE_VISUAL_ID)
    with w.engine.begin() as c:extraction.record_result(c,w.runs.locked(c,run['id']),entry)
    native.queue=[dict(body=extracted(own_items()),incomplete=True)]
    for folder in (tmp_path,tmp_path/'reconnected'):
        with pytest.raises(ApiError):ask(w,current(w,run),source,folder)
    assert len(native.requests)==1 and fact_rows(w)==[]


def test_visual_receipt_and_completed_cache_read_without_renderer(workspace,native,tmp_path,monkeypatch):
    w=workspace;source,run=start(w);native.queue=[dict(body=extracted(own_items()))]
    with monkeypatch.context() as receipt:
        def interrupted(*args,**kwargs):raise ApiError('outcome_unknown')
        receipt.setattr(runtime,'ingest',interrupted)
        with pytest.raises(ApiError):ask(w,run,source,tmp_path)
    monkeypatch.setattr(pdf_images,'render',lambda *a,**kw:pytest.fail('Сохраненный ответ не требует рендера'))
    result=ask(w,current(w,run),source,tmp_path/'receipt')
    assert result['status']=='complete' and len(native.requests)==1
    # A new Run with the same pinned preparation reads completed_requests, not the former Run's operation.
    w.runs.stop(run['id'],run['epoch'],'validation_failed')
    _,_,second=start_fable(w,full=True,source=source,profile_id=FABLE_VISUAL_ID)
    repeated=ask(w,second,source,tmp_path/'completed')
    assert repeated==result and len(native.requests)==1
    assert not extraction_requests.operations(current(w,second))
    with w.engine.begin() as c:
        checkpoint=current(w,second)['checkpoint']
        checkpoint['extractions'][0]['visual_request']['images'][0]['sha256']='0'*64
        execute(c,'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r',r=second['id'],v=json_value(checkpoint))
    with pytest.raises(ApiError) as refused:ask(w,current(w,second),source,tmp_path/'corrupt-identity')
    assert refused.value.code=='outcome_unknown' and len(native.requests)==1


@pytest.mark.parametrize('with_text', [False, True])
def test_scans_are_sent_as_original_pages_without_creating_text_facts(workspace, native, tmp_path, with_text):
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    from pdf_fixtures import pdf_bytes
    w = workspace
    writer = PdfWriter()
    if with_text:
        writer.add_page(PdfReader(BytesIO(pdf_bytes(pages=1))).pages[0])
    writer.add_page(PdfReader(BytesIO(pdf_bytes(pages=1, scanned=True))).pages[0])
    raw = BytesIO()
    writer.write(raw)
    source = w.service.write(w.actor, 'uploadSource', ident('key'), dict(title='Слайды',expected_revision=0),
                             w.project, None, raw.getvalue(), 'slides.pdf')[1]['version']
    page = 2 if with_text else 1
    page_view = w.service.page_or_file(w.actor, w.project, source['id'], page)
    assert page_view['page'] == page and page_view['text'] == '' and page_view['evidence'] == []
    _, _, run = start_fable(w, full=True, source=source, profile_id=FABLE_VISUAL_ID)
    answer = reply(stop='end_turn')
    answer['content'] = [dict(type='text',text=json_value(dict(facts=[],visual_observations=[
        dict(page=page, area='Изображение', text='На слайде виден цветной квадрат.', numeric=None)])))]
    native.queue = [dict(body=answer)]
    result = ask(w, run, source, tmp_path, 'Какие данные на слайде?')
    with w.engine.connect() as c:
        pages = extraction.source_pages(w.runs, c, run)
    inspect_images(native.requests[0], pin_of(source), pages)
    visual = result['visual_observations']
    assert not result['facts'] and len(visual) == 1
    assert visual[0]['page'] == page and visual[0]['verification_status'] == 'unchecked'
    from app import aso, aso_renderer
    claim = dict(text=visual[0]['text'], origin='inference', fact_refs=[], calculation_ids=[],
                 visual_observation_ids=[visual[0]['observation_id']])
    content = dict(title='Записка по изображению', audience='Аналитик', sections=[dict(
        section_id='sec_view',title='Описание',blocks=[dict(kind='paragraph',block_id='p_view',claim=claim)])])
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert saved['ok'], saved
    draft = saved['result']
    layout = aso.presentation(draft['version'], draft['checks'])
    value = layout['sections'][0]['blocks'][0]['rows'][0][0]
    assert value['links'] == [dict(source=source['id'],page=page)]
    assert any(f['code'] == 'unchecked_visual_observation' for c in draft['checks'] for f in c['findings'])
    aso_renderer.render(draft['version'], dict(theme='standard',release_mode='draft'),draft['checks'],tmp_path)
    assert (tmp_path/'note.docx').is_file() and (tmp_path/'note.pdf').is_file()
