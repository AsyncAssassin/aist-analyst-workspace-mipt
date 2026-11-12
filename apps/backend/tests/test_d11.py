"""Детерминированные границы ручных чисел и происхождения."""
from copy import deepcopy
import pytest
from decimal import Decimal
from app.tools import check_units,check_periods,number_tokens,resolve_basis
from app.versions import editorial_payload
from app.errors import ApiError
from types import SimpleNamespace
import asyncio
import json
from starlette.requests import Request
from app.api import json_body
from unittest.mock import patch

MONEY={'value':'4586','unit':'money','scale':'1000000','currency':'RUB'}
PERCENT={'value':'56.85','unit':'percent','scale':'1','currency':None}

@pytest.mark.parametrize('text',['Выручка 4 586 млн RUB.','Выручка +4586 млн руб.','Выручка 4\u00a0586 млн рублей.'])
def test_money_spelling_keeps_value_and_unit(text):check_units(text,[MONEY])

@pytest.mark.parametrize('text',['Выручка -4586 млн RUB.','Выручка 4586 млн USD.','Выручка 4586 руб.','Выручка 4586%.','Рост 56,85 млн RUB.','Рост -56,85%.'])
def test_unit_sign_scale_substitution_rejected(text):
    with pytest.raises(ApiError):check_units(text,[MONEY,PERCENT])


def note(origin='human_editorial',text='Редакторская оговорка.'):
    return {'result_type':'analytical_note','schema_version':'1.0.0','content':{'title':'Записка','audience':'Команда','sections':[{'section_id':'section_one','title':'Вывод','blocks':[{'block_id':'block_one','kind':'paragraph','claim':{'text':text,'origin':origin,'fact_refs':[],'calculation_ids':[]}}]}]}}


@pytest.mark.parametrize('with_length',[False,True])
def test_editor_body_accepts_full_note_and_bounds_stream_before_parsing(with_length):
    payload=note(text='Содержательный абзац. '*150)
    block=payload['content']['sections'][0]['blocks'][0]
    payload['content']['sections'][0]['blocks']=[dict(block,block_id=f'block_{i}') for i in range(6)]
    value=dict(expected_revision=1,parent_version_id='av_parent',payload=payload)
    data=json.dumps(value,ensure_ascii=False).encode()
    assert len(data)>16384
    def request(body):
        headers=[(b'content-type',b'application/json')]
        if with_length:headers.append((b'content-length',str(len(body)).encode()))
        parts=iter([body[:10000],body[10000:]])
        async def receive():
            part=next(parts)
            return dict(type='http.request',body=part,more_body=len(part)==10000)
        return Request(dict(type='http',headers=headers),receive)
    assert asyncio.run(json_body(request(data),'SaveVersion'))==value
    for body,definition in [(data,'CreateProject'),(b' '*1048577,'SaveVersion')]:
        with pytest.raises(ApiError) as error:
            asyncio.run(json_body(request(body),definition))
        assert error.value.body['error']['code']=='too_large'


def test_editor_marks_only_changed_paragraphs_and_keeps_ids():
    original=note('inference');parent={'payload':original}
    changed=deepcopy(original);changed['content']['sections'][0]['blocks'][0]['claim']['text']='Ручная правка.'
    value=editorial_payload(changed,parent)
    assert value['content']['sections'][0]['blocks'][0]['claim']['origin']=='human_editorial'
    assert original['content']['sections'][0]['blocks'][0]['claim']['origin']=='inference'
    assert editorial_payload(original,parent)==original
    assert value['content']['sections'][0]['blocks'][0]['block_id']=='block_one'


@pytest.mark.parametrize('text',['Выручка 999 млн RUB.','Результат FY2021.'])
def test_manual_unfounded_number_has_blocking_finding(text):
    findings=[]
    assert resolve_basis(None,None,{},note(text=text),human=True,findings=findings)=={'facts':[],'calculations':[]}
    assert findings[0]['severity']=='error' and findings[0]['block_id']=='block_one'
    assert not resolve_basis(None,None,{},note(),human=True)['facts']


def test_model_cannot_invent_human_origin_but_can_keep_saved_paragraph():
    payload=note();parent={'payload':deepcopy(payload),'basis':{'calculations':[]}}
    with pytest.raises(ApiError):resolve_basis(None,None,{},payload)
    assert resolve_basis(None,None,{},payload,parent=parent)=={'facts':[],'calculations':[]}
    payload['content']['sections'][0]['blocks'][0]['claim']['text']='Новая модельная правка.'
    with pytest.raises(ApiError):resolve_basis(None,None,{},payload,parent=parent)


def test_duplicate_stable_ids_rejected():
    payload=note();payload['content']['sections'][0]['blocks']*=2
    with pytest.raises(ApiError):resolve_basis(None,None,{},payload,human=True)


@pytest.mark.parametrize('field', ['title', 'audience', 'section'])
def test_numeric_heading_is_not_semantically_verified_and_old_findings_are_unchanged(field):
    payload = note()
    parent = dict(payload=deepcopy(payload), basis={'calculations': []})
    target = payload['content']['sections'][0] if field == 'section' else payload['content']
    target['title' if field == 'section' else field] = 'Выручка 999 млн RUB'
    legacy = dict(code='unreferenced_editorial_heading', severity='error', block_id=None,
                  message='Прежняя числовая проверка заголовка.')
    checks = [dict(kind='programmatic', status='failed', findings=[legacy])]
    original = deepcopy(checks)
    findings = []
    resolve_basis(None, None, {}, payload, parent=parent, human=True, findings=findings, parent_checks=checks)
    assert findings == []
    assert checks == original


@pytest.mark.parametrize('text', [
    '4586 млн $', '4586 млн €', '4586 тыс. ₽', '4586 миллиардов рублей',
    '4586 миллионов долларов', '$4586 млн', '€ 4586 млн', '4586 млн USD',
    '4586 тыс. руб.', '−4586 миллионов рублей',
])
def test_r1_wrong_money_notation_rejected(text):
    with pytest.raises(ApiError): check_units(text,[MONEY])


def test_r1_unsupported_currency_code_stays_unverified():
    # Only supported designations are currencies; the amount remains for the unrecognized-form check.
    assert number_tokens(check_units('4586 млн GBP',[MONEY])) == {Decimal('4586')}


@pytest.mark.parametrize('text', ['4586 млн ₽', '4586 миллионов рублей', '₽4586 млн', '4\u202f586 млн. RUB'])
def test_r1_correct_money_notation_consumed(text):
    assert not number_tokens(check_units(text,[MONEY]))


PERIOD={'kind':'interval','label':'9M’21','start':'2021-01-01','end':'2021-09-30'}

@pytest.mark.parametrize('text', ['1M’21','9Q’21','2021 год','за 2021 годом',
    '1 месяц 2021 года','три месяца 2021 года','2021-01-01 по 2021-12-31','2021-12-31'])
def test_r1_changed_explicit_interval_rejected(text):
    with pytest.raises(ApiError): check_periods(text,[PERIOD])


@pytest.mark.parametrize('text', ["9M’21", "9M'21", '9M2021', '9 месяцев 2021 года',
    'девять месяцев 2021 года', '2021-01-01 по 2021-09-30'])
def test_r1_correct_explicit_interval_consumed(text):
    assert not number_tokens(check_periods(text,[PERIOD]))


def test_r1_annual_and_quarter_periods_require_matching_intervals():
    annual=dict(PERIOD,end='2021-12-31')
    quarter=dict(PERIOD,start='2021-07-01')
    assert not number_tokens(check_periods('2021 год',[annual]))
    assert not number_tokens(check_periods('3Q’21',[quarter]))
    with pytest.raises(ApiError):check_periods('3Q’21',[PERIOD])


def test_r1_unrecognized_numeric_notation_remains_unchecked():
    assert number_tokens(check_units('Выручка 4586 туманов.',[MONEY]))
    assert number_tokens(check_periods('9Z’21',[PERIOD]))


def test_unrecognized_year_comparison_names_remaining_fragments_and_accepts_explicit_years(monkeypatch):
    from test_domain import fact
    facts = {}
    for year in (2023, 2024):
        saved = fact('4586', year=year, fid=f'fact_{year}')
        saved['period'].update(label=str(year), end=f'{year}-12-31')
        facts[saved['fact_id']] = saved
    monkeypatch.setattr('app.tools.resolve_fact', lambda *args: {'data':facts[args[-1]['fact_id']]})
    claim = dict(text='Показатели 2024 к 2023: выручка 4586 млн руб.', origin='source_fact', fact_refs=[], calculation_ids=[])
    payload = dict(result_type='evidence_answer', schema_version='1.0.0', content=dict(claims=[claim], missing_information=[]))
    claim['fact_refs'] = [dict(source_version_id='sv_one', fact_id=fid) for fid in facts]
    findings = []
    runs = SimpleNamespace(service=None)
    resolve_basis(runs, None, {}, payload, findings=findings)
    message = findings[0]['message']
    assert findings[0]['code'] == 'unsupported_numeric_form'
    assert 'Нераспознанные фрагменты' in message and '2024' in message and '2023' in message
    assert 'со словом «год»' in message
    claim['text'] = 'Показатели за 2024 год к 2023 году: выручка 4586 млн руб.'
    corrected = []
    resolve_basis(runs, None, {}, payload, findings=corrected)
    assert not corrected
    claim['text'] = 'Выручка за 2025 год: 4586 млн руб.'
    with pytest.raises(ApiError):
        resolve_basis(runs, None, {}, payload)


def test_dotted_dates_require_saved_boundaries_or_qualitative_source_dates():
    assert not number_tokens(check_periods('На 30.09.2021.',[PERIOD]))
    unknown=dict(kind='unknown',label='Не указан',start=None,end=None)
    quote='Сведения компании на 15.04.2020.'
    assert not number_tokens(check_periods('Данные на 15.04.2020.',[unknown],source_quotes=[quote]))
    for text in ('На 29.09.2021.','На 31.02.2021.','На 30.13.2021.'):
        with pytest.raises(ApiError):check_periods(text,[PERIOD])
    with pytest.raises(ApiError):check_periods('На 15.04.2020.',[unknown])
    # Дата оговорки не превращает источник в годовой финансовый ряд.
    with pytest.raises(ApiError):check_periods('За 2020 год.',[unknown],source_quotes=[quote])


@pytest.mark.parametrize('wording', ['На 17. 11 2024г.', 'На 17.11.2024.'])
def test_spaced_source_date_remains_a_calendar_date(wording):
    unknown = dict(kind='unknown', label='Не указан', start=None, end=None)
    quote = 'Оценка на 17. 11 2024г.'
    assert not number_tokens(check_periods(wording, [unknown], source_quotes=[quote]))
    for wrong in ('На 18.11.2024.', 'На 17.11.2023.', 'За 2024 год.'):
        with pytest.raises(ApiError):
            check_periods(wrong, [unknown], source_quotes=[quote])


def test_event_month_is_supported_only_by_qualitative_source_evidence():
    unknown = dict(kind='unknown', label='Не указан', start=None, end=None)
    quote = 'Выпуск погашен в мае 2024 года.'
    assert not number_tokens(check_periods(quote, [unknown], source_quotes=[quote]))
    assert not number_tokens(check_periods('Период события: май 2024 года.', [unknown], source_quotes=[quote]))
    for wrong in ('В июне 2024 года.', 'В мае 2023 года.', 'За 2024 год.'):
        with pytest.raises(ApiError):
            check_periods(wrong, [unknown], source_quotes=[quote])
    with pytest.raises(ApiError):
        check_periods(quote, [unknown])


@pytest.mark.parametrize('wording', ['Показатели за периоды 2021, 9M21.',
                                    'Показатели относятся к периодам 2021 и 9M21.',
                                    'Экономика займа (данные за 2021).'])
def test_year_in_period_enumeration_requires_a_saved_annual_interval(wording):
    annual = dict(PERIOD, label='2021', end='2021-12-31')
    assert not number_tokens(check_periods(wording, [annual, PERIOD]))
    with pytest.raises(ApiError):
        check_periods(wording, [PERIOD])
    for amount in ('2021', '2021,3', '2021.3'):
        wording = 'За ' + amount + ' млн рублей.'
        assert number_tokens(check_periods(wording, [annual])) == number_tokens(amount)


def test_spaced_source_date_does_not_hide_a_number_or_change_its_value():
    assert number_tokens('На 17. 11 2024г.: 4586 млн руб.') == {17, 11, 2024, 4586}
    assert number_tokens('На 17. 11 2024г.: 4 586 млн руб.', source_quote=True) == {17, 11, 2024, 4586}
    for invalid in ('На 31. 02 2024г.', 'На 17. 13 2024г.'):
        with pytest.raises(ApiError):
            check_periods(invalid, [], source_quotes=[invalid])


def test_qualitative_source_date_survives_full_basis_validation():
    ref = dict(source_version_id='sv_test', fact_id='fact_event')
    quote = 'Оценка на 17. 11 2024г. Выпуск погашен в мае 2024 года.'
    saved = dict(source={'source_version_id':'sv_test'}, fact_id='fact_event', amount=None,
                 period=dict(kind='unknown', label='Не указан', start=None, end=None),
                 nature='source_statement', raw_text=quote,
                 evidence=[dict(quote=quote, locator={'pdf_page':1})])
    with patch('app.tools.resolve_fact', return_value={'data':saved}):
        payload = note('source_fact', quote)
        payload['content']['sections'][0]['blocks'][0]['claim']['fact_refs'] = [ref]
        findings = []
        resolve_basis(SimpleNamespace(service=None), None, {}, payload, findings=findings)
        assert not findings


@pytest.mark.parametrize('human,valid', [(False, False), (False, True), (True, False)])
def test_unrecognized_numbers_keep_an_unaccepted_draft_for_model_and_editor(monkeypatch, human, valid):
    """DRAFT-01: the unrecognized numeric form no longer refuses the model save; the draft keeps the finding."""
    from app.versions import persist
    from test_domain import fact
    saved = fact('4586', fid='fact_revenue')
    payload = note('source_fact', 'Выручка 4586 ' + ('млн руб.' if valid else 'туманов.'))
    claim = payload['content']['sections'][0]['blocks'][0]['claim']
    claim['fact_refs'] = [dict(source_version_id='sv_one', fact_id='fact_revenue')]
    old = dict(payload=note(), basis={'calculations':[]})
    monkeypatch.setattr('app.tools.resolve_fact', lambda *args: {'data':saved})
    monkeypatch.setattr('app.facts.source_documents', lambda *args: [])
    monkeypatch.setattr('app.versions.parent_version', lambda *args: ({'result_type':'analytical_note'}, old, []))
    writes = []
    monkeypatch.setattr('app.versions.execute', lambda *args, **kwargs: writes.append(args[1]))
    context = dict(id='run_unit', owner_id='usr_unit', project_id='prj_unit', input=dict(
        source_versions=[saved['source']], skill_version_id='skill_unit', model_profile_version_id='model_unit'))
    args = (SimpleNamespace(service=None), None, context, payload)
    result = persist(*args, artifact='artifact_unit' if human else None,
                     parent='av_parent' if human else None, expected=1 if human else 0, human=human)
    assert result['checks'][0]['status'] == 'passed'
    assert result['checks'][0]['profile_version'] == 'note-technical.v29'
    assert result['checks'][0]['findings'] == []
    assert result['checks'][1]['status'] == 'not_checked'
    assert result['review']['acceptance'] is None
    assert len(writes) == 2


def test_dotted_boundary_is_not_a_decimal_number_in_a_financial_claim():
    ref=dict(source_version_id='sv_test',fact_id='fact_test')
    fact=dict(source={'source_version_id':'sv_test'},fact_id='fact_test',amount=MONEY,
              period=PERIOD,nature='reported_actual',evidence=[])
    runs=SimpleNamespace(service=None)
    with patch('app.tools.resolve_fact',return_value={'data':fact}):
        for text in ('На 30.09.2021: 4586 млн RUB.', 'На 30.09.2021: 4586,01 млн RUB.', 'На 29.09.2021: 4586 млн RUB.'):
            payload=dict(result_type='evidence_answer',schema_version='1.0.0',content=dict(missing_information=[],claims=[
                dict(text=text,origin='source_fact',fact_refs=[ref],calculation_ids=[])]))
            if text=='На 30.09.2021: 4586 млн RUB.':
                findings=[]
                assert resolve_basis(runs,None,{},payload,findings=findings)['facts']==[fact]
                assert findings==[]
            else:
                with pytest.raises(ApiError):resolve_basis(runs,None,{},payload)


@pytest.mark.parametrize('case', ['new_incomplete', 'complete', 'retained', 'human', 'mixed'])
def test_money_matrix_is_checked_in_every_new_version(monkeypatch, case):
    from app.versions import persist
    from test_domain import fact
    facts, rows = {}, []
    for metric in ('Revenue', 'Profit'):
        for year in (2022, 2023):
            fid = f'fact_{metric.lower()}_{year}'
            saved = fact('100' if metric == 'Revenue' else '50', year=year, fid=fid)
            saved['metric'] = metric
            if case == 'mixed' and metric == 'Profit':
                saved['amount']['currency'] = 'USD'
            facts[fid] = saved
            rows.append(dict(row_id=f'row_{fid}', label=metric, period_label=saved['period']['label'],
                amount=deepcopy(saved['amount']), fact_refs=[dict(source_version_id='sv_one', fact_id=fid)], calculation_ids=[]))
    if case != 'complete':
        rows.pop()
    payload = note()
    payload['content']['sections'][0]['blocks'] = [dict(kind='table', block_id='table_main', caption='Показатели', rows=rows)]
    old = dict(payload=deepcopy(payload), basis={'calculations':[]})
    monkeypatch.setattr('app.tools.resolve_fact', lambda *args: {'data':facts[args[-1]['fact_id']]})
    monkeypatch.setattr('app.facts.source_documents', lambda *args: [])
    monkeypatch.setattr('app.versions.parent_version', lambda *args: ({'result_type':'analytical_note'}, old, []))
    writes = []
    monkeypatch.setattr('app.versions.execute', lambda *args, **kwargs: writes.append(args[1]))
    context = dict(id='run_unit', owner_id='usr_unit', project_id='prj_unit', input=dict(
        source_versions=[saved['source']], skill_version_id='skill_analytical-note-v3', model_profile_version_id='model_unit'))
    inherited = case in ('retained', 'human')
    result = persist(SimpleNamespace(service=None), None, context, payload,
        artifact='artifact_unit' if inherited else None, parent='av_parent' if inherited else None,
        expected=1 if inherited else 0, human=case == 'human')
    assert result['checks'][0]['status'] == 'passed'
    assert result['checks'][0]['findings'] == []
    assert result['review']['acceptance'] is None
    assert len(writes) == 2


def test_editing_request_claim_removes_only_its_stale_link():
    from app.contracts import validate_definition
    from app.versions import editorial_payload
    payload = note('user_request', 'Сумма сделки: 500 млн руб.')
    claim = payload['content']['sections'][0]['blocks'][0]['claim']
    claim['request_message_ids'] = ['msg_request']
    other = deepcopy(payload['content']['sections'][0]['blocks'][0])
    other['block_id'] = 'block_other'
    payload['content']['sections'][0]['blocks'].append(other)
    parent = dict(payload=deepcopy(payload), basis={})
    payload['content']['sections'][0]['blocks'][0]['claim']['text'] = 'Сумма сделки: 600 млн руб.'
    original = deepcopy(payload)
    changed = editorial_payload(payload, parent)
    edited, unchanged = changed['content']['sections'][0]['blocks']
    validate_definition('Claim', edited['claim'])
    assert edited['claim']['origin'] == 'human_editorial' and 'request_message_ids' not in edited['claim']
    assert unchanged == other and payload == original
    assert parent['payload']['content']['sections'][0]['blocks'][0]['claim']['text'] == 'Сумма сделки: 500 млн руб.'
