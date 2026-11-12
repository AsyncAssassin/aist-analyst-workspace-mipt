"""Одна сохраненная версия, два шаблона и буквальные повторные скачивания."""
from copy import deepcopy
from dataclasses import replace
import json
import os

import pytest
from test_perf02_r4_postgres import model_save_args

from app.errors import ApiError
from app.exports import Exports
from app.note_renderer import renderer_versions
from app.openclaw_runtime import Boundary, execution_facts, execution_message, obtain_facts
from app.runs import execute
from app.service import ident
from app.versions import write as write_version
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import candidate, provider, upload_pdf
from test_d15_postgres import workspace

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')


def test_template_identity_legacy_release_integrity_and_actual_model_rules(workspace,provider,tmp_path,monkeypatch):
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    request=body(sources=[source['id']],question='Подготовь информационную справку')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    send(w,conv,request);run=w.runs.claim('template01_test')
    p.facts=[candidate()]
    directory=tmp_path/'model';directory.mkdir()
    facts,call=execution_facts(p.worker,run,request['text'],directory)
    assert facts == [] and call is None and p.sent == []
    p.args=dict(refs=[],extract=dict(source_version_id=source['id'],question='Выручка за 3M23'))
    boundary=Boundary(p.worker,run,directory,'synthetic')
    prompt=execution_message(w.runs,run,request['text'],facts)
    response=boundary.model(dict(model='xiaomimimo/mimo-v2.6-pro',messages=[dict(role='user',content=prompt)]))
    assert p.sent[-1]['messages'][-1]['content']==prompt
    context=json.loads(prompt.rsplit('\n\n',1)[1])
    assert context['execution_budget']==run['input']['budget']
    for text in ('Записка AIST','parameters.sections как каталог допустимых разделов','Не включай пустые разделы','не могут быть основаниями',
                 'разделы источников и расчетов, формулы, технические подтверждения и маркеры [1]',
                 'Пределы и отрицания сохраняй вместе с величиной и предметом утверждения.',
                 'Не переноси даты между самостоятельными утверждениями или карточками: дата источника'):
        assert text in prompt
    issued=response['choices'][0]['message']['tool_calls'][0]
    extracted=boundary.tool(dict(session=run['executor_session_id'],tool_call_id=issued['id'],
                                 name='aist_get_facts',args=p.args))
    assert extracted['ok'],extracted
    fact=extracted['result']['facts'][0];ref=dict(source_version_id=source['id'],fact_id=fact['fact_id'])
    payload=dict(result_type='analytical_note',schema_version='1.0.0',content=dict(title='Company: информационная справка',audience='Аналитик',sections=[
        dict(section_id='sec_main',title='Финансовые и операционные показатели',blocks=[dict(block_id='block_main',kind='table',caption='Выручка',rows=[
            dict(row_id='row_revenue',label=fact['metric'],period_label=fact['period']['label'],amount=fact['amount'],fact_refs=[ref],calculation_ids=[])])])]))
    reply=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',dict(idempotency_key=ident('key'),expected_revision=0,artifact_id=None,parent_version_id=None,payload=payload,ownership_review=model_save_args(payload['content'])['ownership_review']))
    assert reply['ok'],reply
    value=reply['result']['version'];saved=deepcopy(value)
    w.runs.settings=replace(w.settings,export_root=str(tmp_path/'exports'))
    exports=Exports(w.runs)
    files={};keys={}
    for template in ('aist-note.v1','aist-note.v2'):
        request=dict(expected_revision=0,format='pdf',template_version=template,renderer_version=renderer_versions(template)['pdf'],settings=dict(locale='ru-RU',paper='A4',theme='standard',release_mode='draft'))
        keys[template]=ident('key')
        status,entry=exports.request(w.actor,w.session,w.project,value['artifact_id'],value['id'],keys[template],request)
        assert status==202
        exports.process_one()
        data,ready=exports.get(w.actor,w.project,entry['id'],download=True)
        assert ready['state']=='ready'
        files[template]=(data,ready,request)
    assert files['aist-note.v1'][1]['identity_sha256']!=files['aist-note.v2'][1]['identity_sha256']
    assert w.runs.version(w.actor,w.project,value['id'])['version']==saved
    with w.engine.connect() as c:
        calls=execute(c,'SELECT count(*) FROM aist.model_calls WHERE project_id=:p',p=w.project).scalar()
    for template,(data,entry,request) in files.items():
        status,repeated=exports.request(w.actor,w.session,w.project,value['artifact_id'],value['id'],ident('key'),request)
        assert status==200 and repeated==entry
        assert Exports(w.runs).get(w.actor,w.project,entry['id'],download=True)[0]==data
    # Новый рендерер не меняет файл или receipt уже состоявшегося выпуска.
    from app import note_standard
    data,entry,request=files['aist-note.v2']
    with monkeypatch.context() as changed:
        changed.setitem(note_standard.RENDERERS,'pdf',request['renderer_version']+'.next')
        status,repeated=Exports(w.runs).request(w.actor,w.session,w.project,value['artifact_id'],value['id'],keys['aist-note.v2'],request)
        assert status==200 and repeated==entry
        assert Exports(w.runs).get(w.actor,w.project,entry['id'],download=True)[0]==data
        exports.process_one()
        assert w.runs.version(w.actor,w.project,value['id'])['version']==saved
    # Missing/corrupt files keep their identity and never enter the rendering queue again.
    for (data,entry,request),problem in zip(files.values(),('export_missing','export_corrupt')):
        path=exports.files.path(entry)
        if problem=='export_missing':path.unlink()
        else:path.write_bytes(b'corrupt')
        with pytest.raises(ApiError) as error:exports.get(w.actor,w.project,entry['id'],download=True)
        assert error.value.code==problem
        status,repeated=exports.request(w.actor,w.session,w.project,value['artifact_id'],value['id'],ident('key'),request)
        assert status==200 and repeated['state']==('missing' if problem=='export_missing' else 'corrupt')
    exports.process_one()
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.model_calls WHERE project_id=:p',p=w.project).scalar()==calls
        assert list(execute(c,'SELECT render_count FROM aist.exports WHERE project_id=:p',p=w.project).scalars())==[1,1]


def test_note_extraction_refusal_is_returned_to_openclaw_without_saving_or_retrying(workspace,provider,tmp_path):
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    request=body(sources=[source['id']],question='Подготовь справку о бизнесе и выручке')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    send(w,conv,request);run=w.runs.claim('template01_r1_test')
    directory=tmp_path/'calls';directory.mkdir()
    assert execution_facts(p.worker,run,request['text'],directory)==([],None)
    bad=candidate();bad['amount']['value']='999'
    p.facts=[bad]
    args=dict(refs=[],extract=dict(source_version_id=source['id'],question='Выручка за 3M23'))
    reply=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_get_facts',args,
                     obtain_facts=lambda current:obtain_facts(p.worker,current,args['extract'],directory,None))
    # FLOW-01.02: the number absent from the cited line excludes that candidate with its reason. The reply is a
    # completed extraction without usable facts rather than a refusal; nothing is saved and nothing is retried.
    assert reply['ok'] and reply['result']['status']=='empty' and reply['result']['facts']==[]
    assert [(s['candidate'],s['metric'],s['code']) for s in reply['result']['skipped']]==[(1,'Revenue','number_mismatch')]
    assert len(p.sent)==1
    with w.engine.connect() as c:
        assert execute(c,'SELECT state FROM aist.runs WHERE id=:r',r=run['id']).scalar()=='running'
        assert execute(c,'SELECT count(*) FROM aist.facts WHERE project_id=:p',p=w.project).scalar()==0
        assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalar()==0


def test_unrecognized_number_keeps_a_model_draft_that_the_editor_corrects(workspace,provider,tmp_path):
    """DRAFT-01: the structurally valid note with an unrecognized numeric form is saved as an unaccepted draft;
    the Run is finished and the correction is the editor's new version with its own checks."""
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    request=body(sources=[source['id']],question='Подготовь справку о выручке')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    send(w,conv,request);run=w.runs.claim('template01_r2_save_check')
    p.facts=[candidate()]
    args=dict(refs=[],extract=dict(source_version_id=source['id'],question='Выручка за 3M23'))
    extracted=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_get_facts',args,
        obtain_facts=lambda current:obtain_facts(p.worker,current,args['extract'],tmp_path,None))
    assert extracted['ok'],extracted
    fact=extracted['result']['facts'][0]
    claim=dict(text=f"Выручка {fact['amount']['value']} туманов.",origin='source_fact',
               fact_refs=[dict(source_version_id=source['id'],fact_id=fact['fact_id'])],calculation_ids=[])
    payload=dict(result_type='analytical_note',schema_version='1.0.0',content=dict(
        title='Компания: справка',audience='Аналитик',sections=[dict(section_id='sec_main',title='Выручка',
        blocks=[dict(block_id='block_revenue',kind='paragraph',claim=claim)])]))
    save=dict(idempotency_key=ident('key'),expected_revision=0,artifact_id=None,parent_version_id=None,payload=payload,ownership_review=model_save_args(payload['content'])['ownership_review'])
    reply=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',save)
    assert reply['ok'],reply
    check=reply['result']['checks'][0]
    assert check['status']=='passed' and check['findings']==[]
    assert reply['result']['checks'][1]['status']=='not_checked'
    assert reply['result']['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['text']==claim['text']
    assert reply['result']['review']['acceptance'] is None
    assert w.runs.get(w.actor,w.project,run['id'])['state']=='completed'
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalar()==1
        assert execute(c,'SELECT count(*) FROM aist.model_calls WHERE project_id=:p',p=w.project).scalar()==1
    version=reply['result']['version']
    corrected=deepcopy(payload)
    corrected['content']['sections'][0]['blocks'][0]['claim']['text']=f"Выручка {fact['amount']['value']} млн рублей."
    status,saved=write_version(w.runs,w.actor,w.session,w.project,version['artifact_id'],None,'saveVersion',ident('key'),
        dict(expected_revision=1,parent_version_id=version['id'],payload=corrected))
    assert status==201 and saved['checks'][0]['status']=='passed' and saved['checks'][0]['findings']==[]
    assert saved['review']['acceptance'] is None
    assert saved['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['origin']=='human_editorial'
    assert w.runs.get(w.actor,w.project,run['id'])['state']=='completed'
    assert len(p.sent)==1


def test_extraction_reports_locator_limit_before_persistence(workspace,provider,tmp_path):
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    request=body(sources=[source['id']],question='Подготовь справку')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    send(w,conv,request);run=w.runs.claim('template01_r1_locator')
    data=candidate('Business');data['evidence'][0]['area']='x'*501
    p.facts=[data]
    args=dict(refs=[],extract=dict(source_version_id=source['id'],question='Описание бизнеса'))
    reply=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_get_facts',args,
                     obtain_facts=lambda current:obtain_facts(p.worker,current,args['extract'],tmp_path,None))
    assert not reply['ok']
    assert 'facts.0.evidence.0.area' in reply['error']['message']
    assert 'maxLength=500' in reply['error']['message']
    assert len(p.sent)==1
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.facts WHERE project_id=:p',p=w.project).scalar()==0


def test_note_context_over_old_limit_is_sent_once_and_new_limit_refuses_before_reservation(workspace,provider,tmp_path):
    from app.live_profile import PROFILE
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    request=body(sources=[source['id']],question='Справка по нескольким группам оснований')
    request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
    send(w,conv,request);run=w.runs.claim('template01_r1_context')
    p.args={'refs':[]}
    boundary=Boundary(p.worker,run,tmp_path,'synthetic')
    prompt='Source-bound tool history. '*8000
    assert len(prompt.encode())>180000
    boundary.model(dict(model='xiaomimimo/mimo-v2.6-pro',messages=[dict(role='user',content=prompt)]))
    assert len(p.sent)==1 and p.sent[0]['messages'][-1]['content']==prompt
    assert p.sent[0]['max_tokens']==32000
    assert p.sent[0]['thinking']=={'type':'disabled'} and p.sent[0]['enable_thinking'] is False
    with pytest.raises(ApiError) as error:
        boundary.model(dict(model='xiaomimimo/mimo-v2.6-pro',messages=[dict(role='user',content='x'*PROFILE['input_bytes'])]))
    assert error.value.code=='budget_exceeded'
    assert len(p.sent)==1
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalar()==1


def test_remaining_budget_includes_nested_extraction_without_allowing_replay(workspace,provider,tmp_path):
    w,p=workspace,provider
    source=upload_pdf(w);conv=conversation(w)
    send(w,conv,body(sources=[source['id']]))
    run=w.runs.claim('template01_r1_remaining_budget')
    boundary=Boundary(p.worker,run,tmp_path,'synthetic')
    p.facts=[candidate()]
    p.args=dict(refs=[],extract=dict(source_version_id=source['id'],question='Revenue for 3M23'))
    request=dict(model='xiaomimimo/mimo-v2.6-pro',messages=[dict(role='user',content='Get the missing basis')])
    original=deepcopy(request)
    response=boundary.model(request)
    maximum=run['input']['budget']['max_model_calls']
    assert f'после этого ответа: {maximum-1}.' in p.sent[0]['messages'][0]['content']
    tool=response['choices'][0]['message']['tool_calls'][0]
    reply=boundary.tool(dict(session=run['executor_session_id'],tool_call_id=tool['id'],
                             name='aist_get_facts',args=p.args))
    assert reply['ok'],reply
    next_request=dict(request,messages=[dict(role='user',content='Use the returned facts')])
    boundary.model(next_request)
    assert f'после этого ответа: {maximum-3}.' in p.sent[-1]['messages'][0]['content']
    assert request==original
    # Остаток изменился, но прежний запрос не должен стать новой платной отправкой.
    with pytest.raises(ApiError) as error:
        boundary.model(dict(next_request,temperature=0.1))
    assert error.value.code=='outcome_unknown'
    assert len(p.sent)==3
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.model_calls WHERE run_id=:r',r=run['id']).scalar()==3


@pytest.mark.parametrize('source_line',['Supplies equipment.','Supplies equipment. Copyright 2018-2024'])
def test_numbers_without_basis_and_ambiguous_signs_are_saved_as_draft_findings(workspace,provider,tmp_path,monkeypatch,source_line):
    """DRAFT-01: numbers without basis and an ambiguous sign no longer refuse the note; each is a finding of its paragraph."""
    from test_chat01_r1_postgres import LINES
    monkeypatch.setattr('test_chat01_r1_postgres.LINES',LINES[:2]+[source_line]+LINES[3:])
    w,p=workspace,provider
    source=upload_pdf(w)
    def note_run():
        request=body(sources=[source['id']])
        request.update(skill_version_id='skill_analytical-note-v3',requested_result_type='analytical_note')
        send(w,conversation(w),request);return w.runs.claim('template01_r1_save_diagnostic')
    run=note_run()
    p.facts=[candidate('Business')]
    fact=obtain_facts(p.worker,run,dict(source_version_id=source['id'],question='Business description'),tmp_path,None)['facts'][0]
    claim=dict(text='Supplies 999 machines.',origin='source_fact',
               fact_refs=[dict(source_version_id=source['id'],fact_id=fact['fact_id'])],calculation_ids=[])
    payload=dict(result_type='analytical_note',schema_version='1.0.0',content=dict(title='Company',audience='Analyst',
        sections=[dict(section_id='sec_company',title='Business',blocks=[dict(block_id='block_company',kind='paragraph',claim=claim)])]))
    second=deepcopy(claim);second['text']='Supplies 888 machines.'
    payload['content']['sections'][0]['blocks'].append(dict(block_id='block_customers',kind='paragraph',claim=second))
    args=dict(idempotency_key=ident('key'),expected_revision=0,artifact_id=None,parent_version_id=None,payload=payload,ownership_review=model_save_args(payload['content'])['ownership_review'])
    saved=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',args)
    assert saved['ok'],saved
    findings=saved['result']['checks'][0]['findings']
    # v29 сохраняет текст, но не объявляет его смысл проверенным.
    assert findings == [] and saved['result']['checks'][1]['status'] == 'not_checked'
    blocks = saved['result']['version']['payload']['content']['sections'][0]['blocks']
    assert [block['claim']['text'] for block in blocks] == ['Supplies 999 machines.', 'Supplies 888 machines.']
    assert saved['result']['review']['acceptance'] is None and w.runs.get(w.actor,w.project,run['id'])['state']=='completed'
    with w.engine.connect() as c:
        assert execute(c,'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p',p=w.project).scalar()==1
    # An ambiguous sign is a finding of the same paragraph in its own Run; the text is saved as written.
    again=note_run()
    claim['text']='Supplies 2018-2024 machines.'
    args['idempotency_key']=ident('key')
    saved=w.runs.tool(again['id'],again['epoch'],ident('tool'),'aist_save_artifact',args)
    assert saved['ok'],saved
    findings=saved['result']['checks'][0]['findings']
    assert findings == [] and saved['result']['checks'][1]['status'] == 'not_checked'
    assert saved['result']['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['text']=='Supplies 2018-2024 machines.'
    # The corrected texts save without any finding in their own Run.
    clean=note_run()
    claim['text']='Supplies equipment.'
    second['text']='Supplies equipment to customers.'
    args['idempotency_key']=ident('key')
    saved=w.runs.tool(clean['id'],clean['epoch'],ident('tool'),'aist_save_artifact',args)
    assert saved['ok'],saved
    assert saved['result']['checks'][0]['status']=='passed' and saved['result']['checks'][0]['findings']==[]
