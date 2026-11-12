"""Разбор владельцев и сбор сумм проектов до составления, без внешних вызовов."""
from copy import deepcopy

import pytest

from app.collection import NOTE_TOPICS, NOTE_COMBINED_TOPIC, empty_topic_needs_evidence, require_note_topics
from app.errors import ApiError
from app.extraction import extraction_request
from app.model_save import failed_command_digest, save_operation
from app.note_external import ownership_required, review_ownership
from app.openclaw_runtime import tool_schema
from app.visual_observations import snapshots
from test_perf02 import run


def ownership(status='included', reason='', excluded=()):
    return dict(status=status, reason=reason, excluded_sources=list(excluded))


def subject():
    return dict(input=dict(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note',
                           parameters=dict(sections=[])),
                external_context=dict(items=[dict(evidence_id='ext_owner', topics=['owners'])]))


def note(*, owners=True):
    claim = dict(text='Участник ООО «Орбита»: Иван Петров, доля не раскрыта на 01.11.2026.',
                 origin='external_context', fact_refs=[], calculation_ids=[], external_evidence_ids=['ext_owner'])
    section = dict(section_id='section_owners', title='Акционеры' if owners else 'Бизнес',
                   blocks=[dict(block_id='block_owner', kind='paragraph', claim=claim)])
    return dict(result_type='analytical_note', schema_version='1.0.0',
                content=dict(title='Компания', audience='Аналитик', sections=[section]))


def test_known_owners_require_section_and_explicit_review():
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(owners=False), {}, subject(), None, None)
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(owners=False), {}, subject(), None, ownership())
    basis = {}
    review = ownership()
    review_ownership(note(), basis, subject(), None, review)
    assert basis['ownership_review'] == review
    review['status'] = 'not_established'
    assert basis['ownership_review']['status'] == 'included'


def test_material_cannot_be_ignored_or_replaced_by_generic_unknown_perimeter():
    review = ownership('not_established', 'Инвестиционный периметр неизвестен.')
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(owners=False), {}, subject(), None, review)
    current = subject()
    current['external_context']['items'].append(dict(evidence_id='ext_other', topics=['owners']))
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(), {}, current, None, ownership())
    exclusions = [dict(external_evidence_id='ext_other', reason='Вторая выдержка относится к другому юридическому лицу.')]
    basis = {}
    review_ownership(note(), basis, current, None, ownership(excluded=exclusions))
    assert basis['ownership_review']['excluded_sources'] == exclusions


@pytest.mark.parametrize('excluded', [
    [dict(external_evidence_id='ext_foreign', reason='Другой источник')],
    [dict(external_evidence_id='ext_owner', reason='Дубль')],
    [dict(external_evidence_id='ext_other', reason=' ')],
    [dict(external_evidence_id='ext_other', reason='Дубль')] * 2,
])
def test_exclusions_cannot_forge_or_conflict_with_used_sources(excluded):
    current = subject()
    current['external_context']['items'].append(dict(evidence_id='ext_other', topics=['owners']))
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(), {}, current, None, ownership(excluded=excluded))


def test_search_material_without_ownership_allows_justified_absence():
    review = ownership('not_established', 'Прочитанный материал называет директора, состав участников не раскрыт.',
        [dict(external_evidence_id='ext_owner', reason='Страница о директоре, не о владельцах.')])
    basis = {}
    review_ownership(note(owners=False), basis, subject(), None, review)
    assert basis['ownership_review'] == review


def test_pdf_ownership_and_no_materials_do_not_require_external_research():
    current = subject()
    current['external_context'] = None
    payload = note()
    claim = payload['content']['sections'][0]['blocks'][0]['claim']
    claim.update(origin='source_fact', fact_refs=[dict(source_version_id='sv_pdf', fact_id='fact_owners')])
    del claim['external_evidence_ids']
    basis = {}
    review_ownership(payload, basis, current, None, ownership())
    review_ownership(note(owners=False), {}, current, None,
                     ownership('not_established', 'В выбранных материалах владельцы не раскрыты.'))


def test_narrow_task_and_existing_versions_keep_their_scope():
    current = subject()
    current['input']['parameters']['sections'] = ['Финансовые показатели']
    assert not ownership_required(current)
    review_ownership(note(owners=False), {}, current, None, None)
    review_ownership(note(owners=False), {}, subject(), {'basis': {}}, None)
    current['input']['skill_version_id'] = 'skill_analytical-note-v2'
    review_ownership(note(owners=False), {}, current, None, None)


@pytest.mark.parametrize('change', ['audience', 'owners', 'owner_material', 'other_material', 'no_previous_review'])
def test_model_edit_retains_review_only_for_unchanged_ownership(change):
    current = subject()
    parent = dict(payload=note(), basis=dict(external_context=deepcopy(current['external_context']),
                                            ownership_review=ownership()))
    if change == 'no_previous_review':
        del parent['basis']['ownership_review']
    original = deepcopy(parent)
    payload = deepcopy(parent['payload'])
    payload['content']['audience'] = 'Кредитный комитет'
    basis = dict(external_context=deepcopy(parent['basis']['external_context']))
    if change == 'owners':
        payload['content']['sections'][0]['blocks'][0]['claim']['text'] = 'Состав участников изменился.'
    elif change == 'owner_material':
        basis['external_context']['items'].append(dict(evidence_id='ext_new_owner', topics=['owners']))
    elif change == 'other_material':
        basis['external_context']['items'].append(dict(evidence_id='ext_news', topics=['news']))
    current['editorial_external_context'] = basis['external_context']
    review_ownership(payload, basis, current, parent, None)
    if change in ('audience', 'other_material'):
        assert basis['ownership_review'] == parent['basis']['ownership_review']
        assert basis['ownership_review'] is not parent['basis']['ownership_review']
    else:
        assert 'ownership_review' not in basis
    assert parent == original


def test_explicit_model_review_replaces_previous_review_and_is_still_validated():
    current = subject()
    parent = dict(payload=note(), basis=dict(external_context=deepcopy(current['external_context']),
                                            ownership_review=ownership()))
    original = deepcopy(parent)
    replacement = ownership(reason='Повторно сверено с сохраненными материалами.')
    basis = {}
    review_ownership(note(), basis, current, parent, replacement)
    assert basis['ownership_review'] == replacement
    assert basis['ownership_review'] is not replacement
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(), {}, current, parent, ownership('not_established', 'Владельцы неизвестны.'))
    assert parent == original


def test_claimed_inclusion_without_source_is_not_coverage():
    payload = note()
    payload['content']['sections'][0]['blocks'][0]['claim'].update(origin='inference', external_evidence_ids=[])
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(payload, {}, subject(), None, ownership())
    with pytest.raises(ApiError, match='validation_failed'):
        review_ownership(note(), {}, subject(), None, ownership('not_established', 'Данных нет'))


def test_optional_review_survives_save_normalization_and_affects_identity():
    current = run()
    current['input'].update(requested_result_type='analytical_note', expected_artifact_revision=0,
                            target_artifact_id=None, base_version_id=None)
    command = dict(content=note()['content'], ownership_review=ownership())
    saved = save_operation(current, command)
    assert saved['ownership_review'] == command['ownership_review']
    assert save_operation(current, saved) == saved
    bare = dict(content=command['content'])
    assert failed_command_digest(command) != failed_command_digest(bare)
    assert 'ownership_review' in tool_schema('aist_save_artifact')['properties']
    assert 'ownership_review' not in tool_schema('aist_save_artifact', 'evidence_answer')['properties']
    assert 'ownership_review' in tool_schema('aist_save_artifact', ownership_required=ownership_required(subject()))['required']


@pytest.mark.parametrize('question', [NOTE_TOPICS[1], NOTE_COMBINED_TOPIC])
def test_project_amount_is_requested_in_existing_prepared_pass(question):
    request = extraction_request({1: 'Проект: инвестиции 10.7 млрд руб. Планируемый запуск: 2-3 кв. 2027 г.'},
                                 'Подготовь записку', question=question, aso_note=True)
    assert 'инвестиционных проектов' in question
    assert 'неизвестный период' in question
    instruction = request['messages'][0]['content']
    assert 'Extract each disclosed investment' in instruction
    assert 'never infer the investment period from the launch date' in instruction
    assert 'numeric.nature=forecast' in instruction
    assert 'actual disclosed or unknown' in instruction
    assert 'Do not invent source bindings' in instruction


def test_old_business_receipt_does_not_complete_updated_prepared_topic():
    pin = dict(source_version_id='sv_pdf', source_id='src_pdf', sha256='a' * 64)
    old_question = NOTE_TOPICS[1].split('Для существенных инвестиционных проектов')[0].rstrip() + ' Сохрани страницы и даты.'
    entry = dict(question=old_question, source=pin, status='complete', fact_refs=[], call_id='call_old', request_key='old')
    current = dict(input=dict(skill_version_id='skill_analytical-note-v3', base_version_id=None, source_versions=[pin]),
        checkpoint=dict(initial_extractions=[entry], collection=dict(prepared_topics=[dict(source=pin, question=NOTE_TOPICS[1])])))
    with pytest.raises(ApiError, match='validation_failed'):
        require_note_topics(current, dict(facts=[]))


@pytest.mark.parametrize('text', ['Инвестиции в новый завод: 10.7 млрд руб.', 'Project capex: RUB 10.7 billion.'])
def test_empty_business_extraction_cannot_skip_project_only_page(text):
    assert empty_topic_needs_evidence(NOTE_TOPICS[1], [text])
    assert empty_topic_needs_evidence(NOTE_COMBINED_TOPIC, [text])


def test_planned_project_amount_preserves_unknown_period_and_unchecked_status():
    pin = dict(source_version_id='sv_pdf', source_id='src_pdf', sha256='a' * 64)
    numeric = dict(metric='Инвестиции в проект', subject='Проект Орбита', nature='forecast',
        amount=dict(value='10.7', unit='money', currency='RUB', scale='1000000000'),
        period=dict(kind='unknown', label='Период инвестиций не раскрыт', start=None, end=None))
    candidate = dict(page=1, area='Планы', text='Проект Орбита: инвестиции 10.7 млрд руб.; запуск в 2027 году.',
                     numeric=numeric)
    receipt = dict(source=pin, visual_input=dict(source=pin, pages=[1]),
                   images=[dict(page=1, sha256='b' * 64)], key='c' * 64, version='fixture.v1')
    observation, = snapshots([candidate], pin, {1: candidate['text']}, 'call_fixture', receipt)
    assert observation['numeric'] == numeric
    assert observation['verification_status'] == 'unchecked'
    assert observation['source'] == pin and observation['image_sha256'] == 'b' * 64
    with pytest.raises(ApiError):
        snapshots([dict(candidate, page=2)], pin, {1: candidate['text']}, 'call_fixture', receipt)
