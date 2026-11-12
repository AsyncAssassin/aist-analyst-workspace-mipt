"""Границы выбора навыка, продолжения и короткого ответа без провайдера."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from jsonschema import Draft202012Validator
from app.contracts import PACKAGE
from app.errors import ApiError
from app.extraction import candidate
from app.live_profile import delivered, verify_pins
from app.model_context import model_view
from app.openclaw_runtime import execution_message, execution_facts, prepare, tool_schema
from app.tools import resolve_basis
from test_d10 import PIN, PAGES, item


def registry():
    return delivered(json.loads((PACKAGE/'registry.json').read_text()))


def test_skill_pin_cannot_be_reused_for_other_bundle_or_result_type():
    reg=registry()
    skills=[s for s in reg['skills'] if s['status']=='enabled']
    assert {s['id'] for s in skills}=={'skill_analytical-note-v1','skill_analytical-note-v2','skill_analytical-note-v3','skill_source-answer-v1','skill_source-answer-v2','skill_source-answer-v3'}
    for skill in skills:
        config=dict(skill_sha256=skill['bundle_sha256'],model_configuration_sha256=reg['model_profiles'][0]['configuration_sha256'])
        request=dict(skill_version_id=skill['id'],requested_result_type=skill['result_type'])
        verify_pins(config,request)
        for other in skills:
            if other['id']!=skill['id']:
                with pytest.raises(ApiError):verify_pins(config,dict(skill_version_id=other['id'],requested_result_type=other['result_type']))
        with pytest.raises(ApiError):verify_pins(config,request|{'requested_result_type':'meeting_summary'})


def test_selected_parent_and_parameters_reach_executor_without_latest_lookup():
    fact, _ = candidate(PIN, PAGES, item())
    facts=[dict(fact, fact_id='fact_selected')]
    base={'id':'av_selected','revision':2,'basis':{'facts':facts},'payload':{'content':{'title':'Chosen'}}}
    remark=dict(code='unit_mismatch',severity='error',block_id='block_wrong',message='Сумма или процент «999 млн руб.» не совпадают с основанием.')
    checks=[dict(id='check_p',kind='programmatic',status='failed',findings=[remark],method='deterministic'),
            dict(id='check_s',kind='semantic',status='not_checked',findings=[],method='not_performed')]
    runs=SimpleNamespace(version=Mock(return_value={'version':base,'checks':checks,'review':{'acceptance':None}}))
    run={'owner_id':'usr_test','project_id':'prj_test','revision':1,'input':dict(
        skill_version_id='skill_analytical-note-v2',requested_result_type='analytical_note',
        budget={'max_model_calls':6},
        parameters={'audience':'Руководитель','sections':['Главное','Показатели','Ограничения']},
        source_versions=[],base_version_id='av_selected',target_artifact_id='artifact_test',expected_artifact_revision=7)}
    body=json.loads(execution_message(runs,run,'Сократи записку',facts).split('\n\n')[-1])
    assert body['base_version']==model_view(base) and body['expected_artifact_revision']==7
    assert body['facts']==[]
    assert body['parameters']==run['input']['parameters']
    # The remarks of the selected draft travel with it, without the server IDs of the checks.
    assert body['base_version_checks']==[dict(kind='programmatic',status='failed',findings=[remark]),
                                         dict(kind='semantic',status='not_checked',findings=[])]
    assert 'base_version_checks' in body['instruction']
    runs.version.assert_called_once_with('usr_test','prj_test','av_selected')


@pytest.mark.parametrize('kind,skill',[('analytical_note','skill_analytical-note-v2'),('evidence_answer','skill_source-answer-v1')])
def test_native_runtime_gets_only_selected_skill_and_matching_save_schema(tmp_path,kind,skill):
    prepare(SimpleNamespace(provider_port=30314,gateway_port=30313,transport_seconds=360),tmp_path,'synthetic-token',skill,kind)
    config=json.loads((tmp_path/'state/openclaw.json').read_text())
    assert config['agents']['defaults']['thinkingDefault']=='off'
    schemas=json.loads((tmp_path/'plugin/schemas.json').read_text())
    content=schemas['aist_save_artifact']['properties']['content']
    assert set(schemas['aist_save_artifact']['properties']) == (
        {'content'} if kind == 'evidence_answer' else {'content', 'questions', 'news_events', 'ownership_review'})
    assert schemas['aist_save_artifact']['required'] == ['content']
    assert len(list((tmp_path/'workspace/skills').glob('*/SKILL.md')))==1
    if kind=='evidence_answer':
        validator=Draft202012Validator(content)
        validator.validate(dict(claims=[],missing_information=['Нет основания.']))
        assert list(validator.iter_errors(dict(title='Неподходящий тип',sections=[])))
    else:
        assert 'sections' in content['properties']


def test_evidence_answer_reuses_scoped_facts_without_extracting_again(tmp_path):
    run=dict(id='run_test',epoch=1,owner_id='usr_test',project_id='prj_test',input=dict(
        base_version_id=None,requested_result_type='evidence_answer',source_versions=[dict(source_version_id='sv_test',sha256='a'*64)]))
    connection=Mock();context=Mock();context.__enter__=Mock(return_value=connection);context.__exit__=Mock(return_value=False)
    runs=Mock();runs.engine.begin.return_value=context;runs.locked.return_value=run
    worker=SimpleNamespace(runs=runs,transport=Mock())
    with patch('app.openclaw_runtime.source_pages',side_effect=AssertionError('Full PDF read')),patch('app.facts.pinned_version') as pin,patch('app.openclaw_runtime.execute') as execute:
        execute.return_value.scalars.return_value=[{'fact_id':'fact_known','source':{'source_version_id':'sv_test'}}]
        assert execution_facts(worker,run,'Что означает показатель?',tmp_path)==([{'fact_id':'fact_known','source':{'source_version_id':'sv_test'}}],None)
        pin.assert_called_once_with(runs.service,connection,run,'sv_test')
        assert execute.call_args.kwargs==dict(v='sv_test',p='prj_test',a='usr_test',sha='a'*64)
    worker.transport.perform.assert_not_called()


@pytest.mark.parametrize('label', ['9M22', "9M'22", '9M’22', '9M2022'])
def test_answer_keeps_strict_numeric_and_origin_checks(label):
    fact=dict(fact_id='fact_margin',source={'source_version_id':'sv_test'},metric='Margin',
        period={'label':'9M’22','start':'2022-01-01','end':'2022-09-30','kind':'interval'},
        amount={'value':'58','scale':'1','unit':'percent','currency':None},evidence=[])
    claim=dict(text=f'Рентабельность 58% за {label}.',origin='source_fact',fact_refs=[{'source_version_id':'sv_test','fact_id':'fact_margin'}],calculation_ids=[])
    payload=dict(result_type='evidence_answer',schema_version='1.0.0',content={'claims':[claim],'missing_information':[]})
    runs=SimpleNamespace(service=None)
    with patch('app.tools.resolve_fact',return_value={'data':fact}):
        findings=[]
        assert resolve_basis(runs,None,dict(checkpoint={}),payload,findings=findings)['facts']==[fact]
        assert not findings
        for changes in [dict(text='Рентабельность 59%.'),dict(text='Рентабельность 58 млн RUB.'),dict(text='Рентабельность 58% за 2022 год.'),dict(origin='human_editorial')]:
            bad=deepcopy(payload);bad['content']['claims'][0].update(changes)
            with pytest.raises(ApiError):resolve_basis(runs,None,dict(checkpoint={}),bad)
