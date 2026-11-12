"""Real PostgreSQL history, receipts, waiting/input and source authority."""
import json
import os

import pytest
from sqlalchemy import text
from app.errors import ApiError
from app.service import ident, row
from app.openclaw_runtime import execution_message
from test_d15_postgres import workspace, upload, publish_fixture_worker

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')


def conversation(w):
    return w.service.write(w.actor,'createConversation',ident('key'),{'title':'CHAT-01'},w.project)[1]


def body(revision=0, sources=None, question='Объясни возможности AIST'):
    return dict(expected_revision=revision,text=question,source_version_ids=sources or [],skill_version_id='skill_source-answer-v3',
        model_profile_version_id='model_novita-mimo-v1',requested_result_type='evidence_answer',parameters=dict(audience=None,sections=[]),
        base_version_id=None,expected_artifact_revision=0,requested_export=None)


def send(w,conv,request,key=None):
    if w.runs.settings.executor_mode == 'openclaw':
        publish_fixture_worker(w)
    return w.runs.write(w.actor,w.session,w.project,conv['id'],'submitMessage',key or ident('key'),request)


def reply(w,r,text='Добавьте материал, я помогу его разобрать.'):
    return w.runs.tool(r['id'],r['epoch'],ident('tool'),'aist_save_artifact',dict(idempotency_key=ident('key'),
        expected_revision=0,artifact_id=None,parent_version_id=None,payload=dict(result_type='evidence_answer',schema_version='1.0.0',content=dict(claims=[],missing_information=[text]))))


def test_no_pdf_wait_resume_idempotency_and_frozen_history(workspace):
    w=workspace;conv=conversation(w);first=body(question='Помоги разобраться')
    key=ident('key');send(w,conv,first,key)
    run=w.runs.claim('chat01_test')
    prompt='Какую компанию и период нужно изучить?'
    result=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_request_input',dict(idempotency_key=ident('key'),expected_revision=run['revision'],prompt=prompt))
    assert result['ok']
    waiting=w.runs.get(w.actor,w.project,run['id'])
    answer=dict(expected_revision=waiting['revision'],waiting_input_id=waiting['waiting_input']['id'],text='Пока только возможности AIST')
    input_key=ident('key')
    _,resumed=w.runs.write(w.actor,w.session,w.project,run['id'],'resumeRun',input_key,answer)
    assert resumed['run']['id']==run['id']
    next_run=w.runs.claim('chat01_test')
    assert next_run['checkpoint']['turn']==1
    assert next_run['executor_session_id']!=run['executor_session_id']
    sent=json.loads(execution_message(w.runs,next_run,first['text'],[]).rsplit('\n\n',1)[-1])
    assert sent['task']==first['text']
    assert [m['text'] for m in sent['clarification_dialogue']]==[prompt,answer['text']]
    assert reply(w,next_run)['ok']
    saved=w.runs.get(w.actor,w.project,run['id'])
    assert saved['state']=='completed' and saved['result_version_id'] is None
    history=w.runs.listing(w.actor,w.project,'messages',{},conv['id'])['items']
    assert [m['role'] for m in history]==['user','assistant','user','assistant']
    assert history[1]['text']==prompt
    with w.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM aist.artifacts WHERE project_id=:p'),dict(p=w.project)).scalar()==0
        assert c.execute(text('SELECT count(*) FROM aist.exports WHERE project_id=:p'),dict(p=w.project)).scalar()==0
        frozen=row(c,'SELECT configuration FROM aist.runs WHERE id=:r',r=run['id'])['configuration']
    send(w,conv,body(revision=4,question='Что умеешь еще?'))
    later_run=w.runs.claim('chat01_test');assert reply(w,later_run)['ok']
    assert len(later_run['configuration']['conversation_context']['messages'])==4
    assert send(w,conv,first,key)[1]['run']['id']==run['id']
    assert w.runs.write(w.actor,w.session,w.project,run['id'],'resumeRun',input_key,answer)[0]==200
    with w.engine.connect() as c:
        assert row(c,'SELECT configuration FROM aist.runs WHERE id=:r',r=run['id'])['configuration']==frozen
        assert c.execute(text('SELECT count(*) FROM aist.model_calls WHERE project_id=:p'),dict(p=w.project)).scalar()==0
    other=conversation(w);send(w,other,body());other_run=w.runs.claim('chat01_test')
    assert other_run['configuration']['conversation_context']['messages']==[]
    assert reply(w,other_run)['ok']
    with pytest.raises(ApiError) as failure:w.runs.listing('usr_foreign',w.project,'messages',{},conv['id'])
    assert failure.value.code=='resource_not_found'


def test_active_conversation_rejects_new_submission_and_cancel_is_durable(workspace):
    w=workspace;conv=conversation(w);_,s=send(w,conv,body())
    with pytest.raises(ApiError):send(w,conv,body(revision=1))
    w.runs.write(w.actor,w.session,w.project,s['run']['id'],'cancelRun',ident('key'),dict(expected_revision=0))
    send(w,conv,body(revision=1));r=w.runs.claim('chat01_test');assert reply(w,r)['ok']


def test_no_source_reply_rejects_numeric_assertions(workspace):
    w=workspace;conv=conversation(w);send(w,conv,body());r=w.runs.claim('chat01_test')
    failed=reply(w,r,'Выручка составила 542 млн рублей.')
    assert not failed['ok']
    assert w.runs.get(w.actor,w.project,r['id'])['result_version_id'] is None
    assert reply(w,r)['ok']


def test_sources_remain_pinned_during_clarification(workspace):
    w=workspace;version=upload(w)['version'];conv=conversation(w)
    send(w,conv,body(sources=[version['id']],question='За какой период?'));r=w.runs.claim('chat01_test')
    wait=w.runs.tool(r['id'],r['epoch'],ident('tool'),'aist_request_input',dict(idempotency_key=ident('key'),expected_revision=r['revision'],prompt='Уточните период.'))
    assert wait['ok']
    other=upload(w,content='Other source')['version']
    current=w.runs.get(w.actor,w.project,r['id'])
    assert current['input']['source_versions'][0]['source_version_id']==version['id']
    w.runs.write(w.actor,w.session,w.project,r['id'],'resumeRun',ident('key'),dict(expected_revision=current['revision'],waiting_input_id=current['waiting_input']['id'],text='Первый квартал'))
    resumed=w.runs.claim('chat01_test')
    with pytest.raises(ApiError) as failure:w.runs.tool(r['id'],resumed['epoch'],ident('tool'),'aist_read_source',dict(source_version_id=other['id'],page=1))
    assert failure.value.code=='resource_not_found'
    w.runs.write(w.actor,w.session,w.project,r['id'],'cancelRun',ident('key'),dict(expected_revision=resumed['revision']))


def test_changed_source_masks_old_answer_and_denies_old_document(workspace):
    w=workspace;conv=conversation(w);old=upload(w)['version']
    send(w,conv,body(sources=[old['id']]));first=w.runs.claim('chat01_test')
    assert reply(w,first,'Предыдущий ответ по первому документу.')['ok']
    new=upload(w,content='Another permitted test document')['version']
    send(w,conv,body(revision=2,sources=[new['id']],question='А годом раньше?'))
    second=w.runs.claim('chat01_test')
    messages=second['configuration']['conversation_context']['messages']
    assert messages[-1]['excluded_sources'] and messages[-1]['artifact_version_id'] is None
    assert 'Предыдущий ответ' not in messages[-1]['text']
    history=w.runs.listing(w.actor,w.project,'messages',{},conv['id'])['items']
    assert history[1]['text']=='Предыдущий ответ по первому документу.'
    assert history[1]['artifact_version_id']
    with pytest.raises(ApiError) as error:
        w.runs.tool(second['id'],second['epoch'],ident('tool'),'aist_read_source',dict(source_version_id=old['id'],page=1))
    assert error.value.code=='resource_not_found'
    assert reply(w,second,'Недостающие сведения нужно уточнить.')['ok']
