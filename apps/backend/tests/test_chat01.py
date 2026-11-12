"""Conversation boundaries and first-answer extraction without a paid provider."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import pytest
from app.conversation import append_dialogue, snapshot, HISTORY_BYTES
from app.errors import ApiError
from app.openclaw_runtime import execution_facts, execution_message, execution_directory
from app.normalization import checked
from app.contracts import PACKAGE


def test_answer_defers_extraction_until_agent_resolves_the_question(tmp_path):
    run=dict(id='run_test',epoch=1,owner_id='usr_test',project_id='prj_test',input=dict(
        base_version_id=None,requested_result_type='evidence_answer',source_versions=[dict(source_version_id='sv_test',sha256='a'*64)]))
    from contextlib import nullcontext
    runs=Mock();runs.engine.begin.return_value=nullcontext(None);runs.locked.return_value=run
    worker=SimpleNamespace(runs=runs,transport=Mock())
    with patch('app.openclaw_runtime.execute') as sql, patch('app.facts.pinned_version'), \
         patch('app.openclaw_runtime.source_pages',side_effect=AssertionError('Premature full PDF read')):
        for saved in ([],[{'fact_id':'fact_description','source':{'source_version_id':'sv_test'}}]):
            sql.return_value.scalars.return_value=saved
            assert execution_facts(worker,run,'Какая выручка?',tmp_path)==(saved,None)
    worker.transport.perform.assert_not_called()


def test_snapshot_is_bounded_owned_and_masks_excluded_result_text():
    service=Mock();service.scoped.return_value={'data':{'provenance':{'source_versions':[{'source_version_id':'sv_old','sha256':'a'*64}]}}}
    rows=[dict(id=f'msg_{i}',sequence=i,role='assistant',text='Secret old number',artifact_version_id='av_old') for i in range(20,7,-1)]
    with patch('app.conversation.execute') as sql:
        sql.return_value.mappings.return_value=rows
        context=snapshot(service,None,'usr_owner','prj_owner','conv_owner',[])
        assert len(context['messages'])==12 and context['truncated']
        assert len(json.dumps(context['messages'],ensure_ascii=False).encode())<HISTORY_BYTES
        assert all(m['excluded_sources'] and not m['artifact_version_id'] for m in context['messages'])
        assert 'Secret old number' not in json.dumps(context)
        assert sql.call_args.kwargs['actor']=='usr_owner'
        assert sql.call_args.kwargs['project']=='prj_owner'
        assert sql.call_args.kwargs['conversation']=='conv_owner'
        service.scoped.assert_any_call(None,'usr_owner','prj_owner','conversations','conv_owner')


def test_dialogue_limit_refuses_without_mutation():
    old={'safe_resume':True,'dialogue':[{'role':'assistant','text':'Уточните период.'}]}
    with pytest.raises(ApiError):append_dialogue(old,'user','я'*24000)
    assert len(old['dialogue'])==1


def test_execution_prompt_uses_frozen_context_and_original_question_with_clarification():
    run=dict(id='run_test',revision=3,owner_id='usr_test',project_id='prj_test',
        configuration={'conversation_context':{'messages':[{'role':'assistant','text':'Previous','artifact_version_id':'av_saved'}]}},
        checkpoint={'turn':1,'dialogue':[{'role':'assistant','text':'Какой период?'},{'role':'user','text':'Первый квартал'}]},
        input=dict(skill_version_id='skill_source-answer-v3',requested_result_type='evidence_answer',source_versions=[],
                   budget={'max_model_calls':6},
                   parameters={'audience':None,'sections':[]},base_version_id=None,target_artifact_id=None,expected_artifact_revision=0))
    message=execution_message(Mock(),run,'Исходный вопрос',[])
    sent=json.loads(message.rsplit('\n\n',1)[-1])
    assert sent['task']=='Исходный вопрос'
    assert sent['clarification_dialogue']==run['checkpoint']['dialogue']
    assert sent['conversation_context']==run['configuration']['conversation_context']
    assert execution_directory(Path('/test'),run)==Path('/test/run_test/continuation-1')
    assert execution_directory(Path('/test'),run|{'checkpoint':None})==Path('/test/run_test')


def test_source_free_contract_does_not_permit_note_or_base():
    body=json.loads((PACKAGE/'examples/entities.json').read_text())['records']['question_completed']['value']['input']
    body.update(source_versions=[])
    checked('RunInput',body)
    with pytest.raises(ApiError):checked('RunInput',body|{'requested_result_type':'analytical_note'})
    with pytest.raises(ApiError):checked('RunInput',body|{'base_version_id':'av_foreign'})


@pytest.mark.parametrize('with_facts',[False,True])
def test_runtime_releases_old_gateway_after_immediate_clarification_resume(tmp_path, monkeypatch,with_facts):
    from app import openclaw_runtime as runtime
    from contextlib import nullcontext
    run=dict(id='run_race',epoch=1,revision=1,worker_id='worker_old',executor_session_id='session_old',
        message_id='msg_original',configuration={},created_at=datetime.now(timezone.utc),
        input=dict(source_versions=[],skill_version_id='skill_source-answer-v3',model_profile_version_id='model_novita-mimo-v1',
        requested_result_type='evidence_answer',base_version_id=None,budget={'max_duration_seconds':900}))
    # The waiting state was already consumed by /input before the old loop polled.
    current=run|{'state':'running','worker_id':None,'executor_session_id':'session_new'}
    runs=Mock();runs.engine.begin.return_value=nullcontext(None);runs.engine.connect.return_value=nullcontext(None)
    runs.locked.return_value=run
    worker=Mock(runs=runs);worker.settings=SimpleNamespace(openclaw_root=str(tmp_path),provider_port=0,gateway_port=0,
                                                         shutdown_seconds=5,transport_seconds=360)
    worker.closed.is_set.return_value=False;worker.draining.is_set.return_value=False
    for name in ('verify_pins','prepare'):
        monkeypatch.setattr(runtime,name,Mock())
    monkeypatch.setattr(runtime,'row',Mock(side_effect=[{'text':'Original question'},current]))
    facts=[dict(fact_id='fact_saved',source={'source_version_id':'sv_saved'},metric='Business',subject='Company',
                period={'kind':'unknown'},amount=None,evidence=[{'quote':'Supplies equipment.'}],
                reporting_basis='Company data',nature='source_statement')] if with_facts else []
    monkeypatch.setattr(runtime,'execution_facts',Mock(return_value=(facts,None)))
    monkeypatch.setattr(runtime,'execution_message',Mock(return_value='Frozen request'))
    monkeypatch.setattr('app.facts.source_documents',Mock(return_value=[]))
    monkeypatch.setattr(runtime,'PrivateHTTPServer',MagicMock())
    monkeypatch.setattr(runtime,'PrivateUnixServer',MagicMock())
    # This test isolates the old-session loop; PostgreSQL startup fences are tested separately.
    monkeypatch.setattr(runtime,'gateway_remaining',Mock(return_value=900))
    monkeypatch.setattr(runtime.os,'chmod',Mock())
    process=Mock(pid=99999);process.poll.return_value=None
    monkeypatch.setattr(runtime.subprocess,'Popen',Mock(return_value=process))
    monkeypatch.setattr(runtime.os,'killpg',Mock())
    monkeypatch.setattr(runtime.socket,'create_connection',Mock(return_value=nullcontext()))
    adapter=Mock();adapter.start.return_value={'executor_run_id':'exec_old'}
    monkeypatch.setattr(runtime,'OpenClawExecutor',Mock(return_value=adapter))
    monkeypatch.setattr(runtime.time,'sleep',Mock(side_effect=AssertionError('Old Gateway must stop before polling again')))
    runtime.run(worker,run)
    assert runtime.execution_message.call_args.args[3]==facts
    adapter.start.assert_called_once_with(run,'Frozen request',session_identity=None)
    assert runtime.os.killpg.call_args_list == [call(99999, runtime.signal.SIGTERM), call(99999, runtime.signal.SIGKILL)]


def test_snapshot_utf8_limit_does_not_trim_or_modify_full_messages():
    service=Mock()
    recent=dict(id='msg_new',sequence=2,role='user',text='я'*5000,artifact_version_id=None)
    old=dict(id='msg_old',sequence=1,role='assistant',text='я'*5000,artifact_version_id=None)
    with patch('app.conversation.execute') as sql:
        sql.return_value.mappings.return_value=[recent,old]
        value=snapshot(service,None,'usr_owner','prj_owner','conv_owner',[])
    assert value['messages']==[recent] and value['truncated']
    assert len(json.dumps(value['messages'],ensure_ascii=False).encode())<HISTORY_BYTES
    assert old['text']=='я'*5000
