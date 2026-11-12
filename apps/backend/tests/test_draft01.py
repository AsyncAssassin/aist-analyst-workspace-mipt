"""DRAFT-01: a structurally valid note with content remarks is saved as an unaccepted draft.

Content mismatches become blocking findings with a recognizable place; a broken reference,
a forged origin and a short answer with remarks keep the refusal. No model, no database."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.contracts import validate_definition
from app.errors import ApiError
from app.note_standard import content_plan
from app.tools import FINDING_LIMIT, block_locations, content_finding, feedback, resolve_basis
from app.versions import persist
from test_perf02_r4 import FACTS, MONEY, PIN, RUN, VERSION, lookup, note, paragraph, ref, reviewed, row


def answer(text, refs):
    return dict(result_type='evidence_answer', schema_version='1.0.0', content=dict(missing_information=[], claims=[
        dict(text=text, origin='source_fact', fact_refs=[ref(x) for x in refs], calculation_ids=[])]))


def table(block_id, *rows):
    return dict(block_id=block_id, kind='table', caption='Структура финансирования', rows=list(rows))


def test_content_remarks_are_blocking_findings_with_places_and_only_references_refuse():
    payload = note([dict(section_id='sec_one', title='Финансирование', blocks=[
        paragraph('block_currency', 'Собственный капитал на 31.03.2023 составил 3658 млн USD.', 'fact_capital_1q23'),
        table('block_table', row('row_period', 'Привлеченные средства', '2022', dict(MONEY, value='4392'), 'fact_funding')),
        dict(block_id='block_inference', kind='paragraph', claim=dict(text='Компания расширяет сеть партнеров.',
                                                                      origin='inference', fact_refs=[], calculation_ids=[])),
        paragraph('block_correct', 'На конец 2022 года собственный капитал составил 3216 млн руб.', 'fact_capital_2022'),
    ])])
    normalized, basis, findings, errors = reviewed(payload)
    assert errors == []
    assert [(f['code'], f['block_id'], f['severity']) for f in findings] == [
        ('row_basis_mismatch', 'block_table', 'error'),
        ('unreferenced_claim', 'block_inference', 'error')]
    assert findings[0]['message'] == ('строка 1 «Привлеченные средства» (row_period): Сумма 4392 млн RUB и период «2022» '
                                      'не совпадают с основанием. Основание: 4392 млн RUB, период «на 31.03.2023».')
    assert findings[1]['message'] == 'Утверждение должно иметь сохраненные основания.'
    for finding in findings:
        validate_definition('Finding', finding)
    # Numbers, dates and references of the draft are kept as written: nothing is removed to pass the check.
    assert normalized['content']['sections'][0]['blocks'][0]['claim']['text'] == payload['content']['sections'][0]['blocks'][0]['claim']['text']
    assert {f['fact_id'] for f in basis['facts']} == {'fact_capital_1q23', 'fact_funding', 'fact_capital_2022'}

    broken = note([dict(section_id='sec_one', title='Финансирование', blocks=[
        paragraph('block_absent', 'Выручка за 2022 год составила 3157 млн руб.', 'fact_absent')])])
    _, basis, findings, errors = reviewed(broken)
    assert findings == [] and basis['facts'] == []
    assert [location.rsplit('(', 1)[1] for location, _ in errors] == ['block_absent)', 'block_absent)']
    assert errors[0][1].startswith('Факт fact_absent отсутствует в разрешенной версии ' + VERSION)

    forged = note([dict(section_id='sec_one', title='Финансирование', blocks=[
        dict(block_id='block_forged', kind='paragraph', claim=dict(text='Редактор одобрил этот текст.',
                                                                   origin='human_editorial', fact_refs=[ref('fact_quality')], calculation_ids=[]))])])
    _, _, findings, errors = reviewed(forged)
    assert findings == [] and [message for _, message in errors] == ['Генерация не создает человеческую редактуру или принятие.']


@pytest.mark.parametrize('case', ['draft', 'clean', 'answer', 'missing', 'forged'])
def test_persist_saves_the_note_draft_but_refuses_answers_and_broken_references(monkeypatch, case):
    writes = []
    monkeypatch.setattr('app.tools.resolve_fact', lookup)
    monkeypatch.setattr('app.tools.row', lambda *args, **kwargs: None)
    monkeypatch.setattr('app.facts.source_documents', lambda *args: [])
    monkeypatch.setattr('app.versions.execute', lambda *args, **kwargs: writes.append(args[1]))
    context = dict(RUN, input=dict(RUN['input'], model_profile_version_id='model_unit'))
    text = {'draft': 'ROA за 1Q23 составил 11,7%.', 'clean': 'ROA за 1Q23 составил 11,6%.',
            'missing': 'Выручка за 2022 год составила 3157 млн руб.', 'forged': 'Редактор одобрил этот текст.'}
    if case == 'answer':
        payload = answer('ROA за 1Q23 составил 11,7%.', ['fact_roa'])
    else:
        payload = note([dict(section_id='sec_one', title='Показатели', blocks=[
            paragraph('block_one', text[case], 'fact_absent' if case == 'missing' else 'fact_roa')])])
        if case == 'forged':
            payload['content']['sections'][0]['blocks'][0]['claim']['origin'] = 'human_editorial'
    if case in ('draft', 'clean'):
        result = persist(SimpleNamespace(service=None), None, context, payload)
        check = result['checks'][0]
        assert check['status'] == 'passed' and check['profile_version'] == 'note-technical.v29'
        assert check['findings'] == []
        assert result['checks'][1]['status'] == 'not_checked'
        assert result['review']['acceptance'] is None and result['version']['provenance']['origin'] == 'generation'
        assert len(writes) == 2
        return
    with pytest.raises(ApiError) as refusal:
        persist(SimpleNamespace(service=None), None, context, payload)
    assert refusal.value.code == 'validation_failed' and not writes
    message = refusal.value.body['error']['message']
    expected = {'answer': 'Утверждение 1 «ROA за 1Q23 составил 11,7%.»: Числа 11.7 не имеют сохраненного основания.',
                'missing': '(block_one): Факт fact_absent отсутствует',
                'forged': '(block_one): Генерация не создает человеческую редактуру'}[case]
    assert expected in message, message


def test_finding_message_keeps_the_row_place_within_the_contract_limit():
    block = dict(block_id='block_table')
    within = 'строка 3 «Облигации» (row_bonds)'
    finding = content_finding('row_basis_mismatch', block, within, 'Сумма 942 млн RUB и период «на 31.03.2023» не совпадают с основанием.')
    assert finding == dict(code='row_basis_mismatch', severity='error', block_id='block_table',
                           message=within + ': Сумма 942 млн RUB и период «на 31.03.2023» не совпадают с основанием.')
    long = content_finding('unsupported_numbers', block, within, 'Числа ' + ', '.join(str(n) for n in range(400)) + ' не имеют основания.')
    assert len(long['message']) == FINDING_LIMIT and long['message'].endswith('…') and long['message'].startswith(within)
    validate_definition('Finding', long)
    assert content_finding('unit_mismatch', None, 'Утверждение 1 «Текст»', 'Сумма не совпадает.')['block_id'] is None


def test_feedback_names_answer_claims_table_rows_and_paragraph_excerpts():
    with patch('app.tools.resolve_fact', side_effect=lookup), patch('app.tools.row', return_value=None):
        with pytest.raises(ApiError) as refusal:
            resolve_basis(SimpleNamespace(service=None), None, RUN, answer('ROA за 1Q23 составил 11,7%.', ['fact_roa']))
    lines = refusal.value.body['error']['message'].split('\n')
    assert lines[0] == 'Утверждение 1 «ROA за 1Q23 составил 11,7%.»: Числа 11.7 не имеют сохраненного основания.'
    assert lines[1] == 'Утверждение 1 «ROA за 1Q23 составил 11,7%.»: Сумма или процент «11,7%» не совпадают с основанием.'
    payload = note([dict(section_id='sec_one', title='Финансирование', blocks=[
        paragraph('block_text', 'Собственный капитал на 31.03.2023 составил 2478 млн руб.', 'fact_capital_1q23'),
        table('block_table', row('row_wrong', 'Собственный капитал', 'на 31.03.2023', dict(MONEY, value='2478'), 'fact_capital_1q23'))])])
    _, _, findings, errors = reviewed(payload)
    lines = feedback(errors, findings, block_locations(payload)).split('\n')
    assert len(lines) == 1
    assert lines[0] == ('Раздел 1 «Финансирование», блок 2, таблица «Структура финансирования» (block_table): '
                        'строка 1 «Собственный капитал» (row_wrong): Сумма 2478 млн RUB и период «на 31.03.2023» '
                        'не совпадают с основанием. Основание: 3658 млн RUB, период «1 кв. 2023г.».')


def test_draft_release_marks_the_draft_and_its_remarks_without_changing_the_text():
    version = dict(payload=dict(content=dict(title='Записка', audience='Аналитик', sections=[dict(section_id='sec', title='Раздел', blocks=[
        paragraph('block_text', 'Собственный капитал на 31.03.2023 составил 2478 млн руб.', 'fact_capital_1q23')])])),
        basis=dict(facts=[FACTS['fact_capital_1q23']], calculations=[]))
    failed = [dict(kind='programmatic', status='failed'), dict(kind='semantic', status='not_checked')]
    passed = [dict(kind='programmatic', status='passed'), dict(kind='semantic', status='not_checked')]
    draft = content_plan(version, dict(release_mode='draft', theme='standard'), failed)
    assert draft[:3] == [('title', 'Записка'), ('p', 'ЧЕРНОВИК'), ('p', 'Версия не принята: в сохраненной версии есть замечания проверок.')]
    assert ('text', 'Собственный капитал на 31.03.2023 составил 2478 млн руб.') in draft
    assert content_plan(version, dict(release_mode='draft', theme='standard'), passed)[:3] == [
        ('title', 'Записка'), ('p', 'ЧЕРНОВИК'), ('h', 'Раздел')]
    assert content_plan(version, dict(release_mode='accepted', theme='standard'), passed)[:2] == [('title', 'Записка'), ('h', 'Раздел')]
    assert PIN['source_version_id'] == VERSION
