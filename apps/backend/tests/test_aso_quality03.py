"""Границы сбора ASO: количество кандидатов не подменяет полноту материала."""
from copy import deepcopy
import json

import pytest

from app import collection, extraction
from app.errors import ApiError
from app.normalization import fingerprint


@pytest.mark.parametrize('scale,label', [('1', ''), ('1000', 'тыс. '), ('1000000', 'млн '),
                                       ('1000000000', 'млрд '), ('1000000000000', 'трлн ')])
def test_amount_scales_keep_decimal_strings_and_display(scale, label):
    from app.contracts import serialize_definition, validate_definition
    from app.note_renderer import amount as rendered_amount
    from app.tools import amount_text, AMOUNT_SUFFIX, stated_amount
    from decimal import Decimal
    value = dict(value='2.200000000000000001', unit='money', scale=scale, currency='RUB')
    assert json.loads(serialize_definition('Amount', value)) == value
    assert amount_text(value) == value['value'] + ' ' + label + 'RUB'
    assert rendered_amount(value) == '2,200000000000000001 ' + label + 'RUB'
    match = AMOUNT_SUFFIX.fullmatch('2,200000000000000001 ' + label + 'руб.')
    assert match and stated_amount(match) == (Decimal(value['value']), scale, 'RUB', 'money')
    for invalid in (dict(value, scale='1000000000000000'), dict(value, value=2.2),
                    dict(value, unit='percent', scale='1000000000000', currency=None)):
        from jsonschema import ValidationError
        with pytest.raises(ValidationError):
            validate_definition('Amount', invalid)


def test_trillion_scale_words_keep_ambiguous_labels_ambiguous():
    from app.extraction import scales_named
    from app.tools import amount_scale
    for label in ('трлн руб.', 'триллиона рублей', 'триллионов RUB', 'trillion RUB'):
        assert scales_named(label) == {'1000000000000'}
    assert amount_scale('триллиона') == '1000000000000'
    assert scales_named('трлн / млрд руб.') == {'1000000000000', '1000000000'}


def receipt(source, question, *, status='complete', error=None, suffix=''):
    entry = dict(source=source, question=question, call_id=source['source_version_id'] + question + suffix,
                 fact_refs=[dict(source_version_id=source['source_version_id'], fact_id='fact_' + suffix)],
                 status=status, skipped=[])
    if error:
        entry['error'] = error
    return entry


def prepared_run():
    source = dict(source_version_id='sv_one', sha256='a' * 64)
    entries = [receipt(source, question, suffix=str(i)) for i, question in enumerate(collection.NOTE_TOPICS)]
    run = dict(input=dict(skill_version_id=collection.BOUNDED_SKILL, base_version_id=None,
                          source_versions=[source]), checkpoint=dict(extractions=entries,
        collection=dict(closed=False, prepared_topics=[dict(source=source, question=q) for q in collection.NOTE_TOPICS])))
    basis = dict(facts=[dict(source=source, fact_id='fact_' + str(i)) for i in range(2)])
    return run, basis


@pytest.mark.parametrize('damage', ['missing_theme', 'failed_theme', 'foreign_receipt', 'missing_source', 'omitted_theme'])
def test_prepared_material_cannot_be_silently_omitted(damage):
    run, basis = prepared_run()
    entries = run['checkpoint']['extractions']
    if damage == 'missing_theme':
        entries.pop()
    elif damage == 'failed_theme':
        entries[-1]['error'] = {'code': 'budget_exceeded'}
    elif damage == 'foreign_receipt':
        entries[-1]['source'] = dict(source_version_id='sv_other', sha256='b' * 64)
    elif damage == 'missing_source':
        run['input']['source_versions'].append(dict(source_version_id='sv_two', sha256='c' * 64))
    else:
        basis['facts'].pop()
    with pytest.raises(ApiError):
        collection.require_note_topics(run, basis)


def test_partial_receipt_and_absent_optional_theme_are_not_a_failed_collection():
    run, basis = prepared_run()
    run['checkpoint']['extractions'][0]['status'] = 'partial'
    collection.require_note_topics(run, basis)
    second = run['checkpoint']['extractions'][1]
    second.update(status='empty', fact_refs=[])
    basis['facts'].pop()
    collection.require_note_topics(run, basis)


def test_different_successes_cannot_cover_failed_question():
    run, basis = prepared_run()
    source = run['input']['source_versions'][0]
    failed = receipt(source, 'Условия финансирования', error={'code': 'provider_error'}, suffix='failed')
    run['checkpoint']['extractions'].append(failed)
    with pytest.raises(ApiError):
        collection.require_note_topics(run, basis)
    # A known successful receipt for this same source and question closes the transport gap.
    run['checkpoint']['extractions'].append(receipt(source, failed['question'], suffix='resolved'))
    collection.require_note_topics(run, basis)


def test_selected_parent_is_not_forced_through_new_collection():
    run, basis = prepared_run()
    run['input']['base_version_id'] = 'version_selected'
    run['checkpoint']['extractions'] = []
    collection.require_note_topics(run, basis)


@pytest.mark.parametrize('attempts,code,allowed', [([], 'budget_exceeded', True),
    (['call_sent'], 'budget_exceeded', False), ([], 'provider_error', False),
    (None, 'budget_exceeded', False)])
def test_unstarted_optional_request_does_not_poison_prepared_note(attempts, code, allowed):
    run, basis = prepared_run()
    source = run['input']['source_versions'][0]
    failed = receipt(source, 'Дополнительные условия проекта', error={'code': code}, suffix='optional')
    failed['request_key'] = 'optional_request'
    run['checkpoint']['extractions'].append(failed)
    run['checkpoint']['extraction_operations'] = {'optional_request': dict(call_ids=attempts, error={'code': code})}
    if allowed:
        collection.require_note_topics(run, basis)
        # An unfinished mandatory theme remains a blocker even with no paid attempt.
        run['checkpoint']['extractions'][0]['error'] = {'code': 'budget_exceeded'}
    with pytest.raises(ApiError):
        collection.require_note_topics(run, basis)


@pytest.mark.parametrize('label', ['Привлеченные средства', 'Привлечённые средства, млн руб.'])
def test_funding_label_preserves_metric_unit_value_and_period(label):
    from app.visual_observations import row_agrees
    row = dict(label=label, period_label='на 31.12.2025',
               amount=dict(value='123', unit='money', scale='1000000', currency='RUB'))
    observation = dict(numeric=dict(metric='Общий размер привлеченных средств',
        period=dict(label=row['period_label']), amount=deepcopy(row['amount'])), verification_status='unchecked')
    assert row_agrees(row, observation)
    for other in ('Собственные средства', 'Привлеченные средства клиентов', 'Краткосрочные привлеченные средства',
                  'Облигации', 'Денежные средства'):
        assert not row_agrees(dict(row, label=other), observation)
    assert not row_agrees(dict(row, period_label='2025 год'), observation)
    for amount in (dict(row['amount'], value='321'), dict(row['amount'], scale='1000'),
                   dict(row['amount'], unit='percent', scale='1', currency=None)):
        assert not row_agrees(dict(row, amount=amount), observation)
    assert observation['verification_status'] == 'unchecked'


@pytest.mark.parametrize('page,needs_evidence', [
    ('Выручка и финансовые результаты', True), ('Продукты и клиенты', True),
    ('', True), ('Контактные данные: адрес офиса', False),
])
def test_combined_topic_does_not_treat_a_material_empty_result_as_success(page, needs_evidence):
    assert collection.empty_topic_needs_evidence(collection.NOTE_COMBINED_TOPIC, [page]) is needs_evidence


@pytest.mark.parametrize('facts,visuals', [(65, 0), (0, 65), (65, 64), (64, 65)])
def test_schema_candidate_ceiling_is_enforced_before_writing(facts, visuals):
    fact = dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None,
                value_text=None, basis=None, evidence=[dict(page=1, line_start=1, line_end=1, area='Описание')])
    visual = dict(page=1, area='Схема', text='Схема производства.', numeric=None)
    with pytest.raises(ApiError) as raised:
        extraction.ingest(None, None, {}, dict(facts=[fact] * facts, visual_observations=[visual] * visuals), 'call')
    assert 'maxItems=64' in raised.value.body['error']['message']


@pytest.mark.parametrize('previous_version', ['pdf-lines.v35', 'pdf-lines.v38'])
def test_prompt_schema_and_existing_request_identity_change_together(monkeypatch, previous_version):
    request = extraction.extraction_request({1: 'Источник'}, 'Справка', question=collection.NOTE_TOPICS[0], aso_note=True)
    assert 'schema ceiling is 64 items in EACH list' in request['messages'][0]['content']
    assert 'CANDIDATE_LIMIT' not in json.dumps(request)
    schema = json.loads(request['messages'][1]['content'])['output_schema']
    assert schema == extraction.extraction_schema()
    assert schema['properties']['facts']['maxItems'] == schema['properties']['visual_observations']['maxItems'] == 64
    old_schema = deepcopy(schema)
    for name in ('facts', 'visual_observations'):
        old_schema['properties'][name]['maxItems'] = 18
    assert fingerprint(schema) != fingerprint(old_schema)
    pin = dict(source_version_id='sv_one', sha256='a' * 64)
    new_key = extraction.request_key(pin, collection.NOTE_TOPICS[0])
    monkeypatch.setattr(extraction, 'VERSION', previous_version)
    assert extraction.request_key(pin, collection.NOTE_TOPICS[0]) != new_key


def test_mixed_candidate_ceiling_and_published_rates_remain_source_observations():
    from jsonschema import Draft202012Validator
    fact = dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None,
                value_text=None, basis=None, evidence=[dict(page=1, line_start=1, line_end=1, area='Описание')])
    visual = dict(page=1, area='Схема', text='Схема производства.', numeric=None)
    Draft202012Validator(extraction.extraction_schema()).validate(dict(facts=[fact] * 64, visual_observations=[visual] * 64))
    instruction = extraction.extraction_request({1: 'Источник'}, 'Справка')['messages'][0]['content']
    assert 'published growth rates, CAGR' in instruction
    assert 'Do not calculate CAGR yourself' in instruction
    assert 'full comparable series when compactly disclosed' in instruction
    assert extraction.VERSION == 'pdf-lines.v39'


@pytest.mark.parametrize('original,unit,translated', [
    ('Revenue', 'money', 'Выручка'), ('Net debt', 'money', 'Чистый долг'),
    ('OWC', 'money', 'Операционный оборотный капитал (OWC)'),
    ('EBITDA margin', 'percent', 'Рентабельность по EBITDA'),
    ('Net debt/EBITDA', 'ratio', 'Чистый долг/EBITDA'),
])
def test_standard_translation_keeps_numeric_binding(original, unit, translated):
    from app.note_standard import translate_row_labels
    from app.visual_observations import row_agrees
    row = dict(row_id='row_original', label=original, period_label='2024 год',
               amount=dict(value='-20.8', unit=unit, scale='1000000000' if unit == 'money' else '1',
                           currency='RUB' if unit == 'money' else None), visual_observation_ids=['vo_original'])
    observation = dict(numeric=dict(metric=original, period=dict(label=row['period_label']),
                                    amount=deepcopy(row['amount'])), verification_status='unchecked')
    before = deepcopy(row)
    translate_row_labels(dict(rows=[row]))
    assert row == dict(before, label=translated)
    assert row_agrees(row, observation)
    assert not row_agrees(dict(row, label='Adjusted ' + original), observation)
    assert not row_agrees(dict(row, period_label='2023 год'), observation)
    for key, value in [('value', '20.8'), ('scale', '1000'), ('unit', 'count'), ('currency', 'USD')]:
        assert not row_agrees(dict(row, amount=dict(row['amount'], **{key: value})), observation)
    assert observation['verification_status'] == 'unchecked'


def test_translation_does_not_rewrite_adjusted_metrics_or_other_units():
    from app.note_standard import translate_row_labels
    rows = [dict(label=label, amount=dict(value='10', unit=unit, scale='1', currency=None))
            for label, unit in [('Adjusted EBITDA margin', 'percent'), ('Revenue per store', 'money'),
                                ('Gross debt', 'money'), ('Revenue', 'percent')]]
    before = deepcopy(rows)
    translate_row_labels(dict(rows=rows))
    assert rows == before


def test_extraction_effort_changes_only_visual_profile_and_preserves_all_material():
    from app import live_profile, transport
    request = {'messages': [dict(role='system', content='Full instruction'),
                           dict(role='user', content=[dict(type='text', text='All pages'),
                               dict(type='image', source=dict(type='base64', data='synthetic'))])]}
    before = deepcopy(request)
    for profile_id in (live_profile.FABLE_ID, live_profile.FABLE_FULL_ID, live_profile.FABLE_VISUAL_ID):
        run = dict(input=dict(model_profile_version_id=profile_id))
        for purpose in ('agent', 'extraction'):
            sent = transport.model_request(run, purpose, request)
            expected = 'high' if purpose == 'extraction' and profile_id == live_profile.FABLE_VISUAL_ID else 'xhigh'
            assert sent['output_config']['effort'] == expected
            assert sent['max_tokens'] == 64000 and sent['thinking'] == {'type': 'adaptive'}
            assert sent['messages'][-1] == request['messages'][-1]
    assert request == before
    assert fingerprint(live_profile.FABLE_VISUAL) != fingerprint(dict(live_profile.FABLE_VISUAL, extraction_effort='xhigh'))


@pytest.mark.parametrize('metric,label,value,unit,scale,currency', [
    ('OWC', 'Операционный оборотный капитал (OWC)', '-20.8', 'money', '1000000000', 'RUB'),
    ('Change in OWC', 'Изменение оборотного капитала (OWC)', '-3.4', 'money', '1000000000', 'RUB'),
    ('Net debt/EBITDA', 'Чистый долг/EBITDA', '0.3', 'ratio', '1', None),
    ('EBITDA margin', 'Рентабельность по EBITDA', '17', 'percent', '1', None),
])
def test_financial_rows_keep_individual_units_sign_and_period(metric, label, value, unit, scale, currency):
    from app.visual_observations import row_agrees
    from app.note_standard import shared_unit

    amount = dict(value=value, unit=unit, scale=scale, currency=currency)
    row = dict(label=label, period_label='2024', amount=amount)
    observation = dict(numeric=dict(metric=metric, period=dict(label='2024'), amount=deepcopy(amount)))
    assert row_agrees(row, observation)
    assert not row_agrees(dict(row, period_label='2023'), observation)
    for changed in (dict(amount, value='99'), dict(amount, value=value.lstrip('-')) if value.startswith('-') else dict(amount, value='-' + value),
                    dict(amount, scale='1000'), dict(amount, unit='count', currency=None, scale='1')):
        assert not row_agrees(dict(row, amount=changed), observation)
    money = dict(label='Чистый долг', period_label='2024', amount=dict(value='2.7', unit='money', scale='1000000000', currency='RUB'))
    if unit != 'money':
        assert shared_unit(dict(rows=[money, row])) is None
