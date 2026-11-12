"""Границы нескольких PDF и табличного извлечения. Только синтетические числа."""
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
import subprocess
from pypdf import PdfReader, PdfWriter
from app.domain import compare_observations, growth
from app.errors import ApiError
from app.extraction import candidate, extraction_request
from app.facts import pinned_version
from app.note_renderer import content_plan
from app.pdf_worker import extract, PROCESSOR_VERSION
from app.tools import resolve_basis
from pdf_fixtures import pdf_bytes
from test_domain import fact, calculation, BASIS
from test_exports import sample

PIN=dict(source_id='s_table',source_version_id='sv_table',sha256='b'*64)
LAYOUT={1:'Synthetic Company\nМСФО\nВыручка, в тысячах рублей\n                           За 9 месяцев, закончившихся 30 сентября\n                              2022                  2021\nВыручка                       1 403                 1 004'}
PAGES={1:'Synthetic Company\nМСФО\nВыручка, в тысячах рублей\nЗа 9 месяцев, закончившихся 30 сентября\n2022 2021\nВыручка 1 403 1 004'}


def table_item():
    return dict(metric='Выручка',subject='Synthetic Company',reporting_basis='МСФО',nature='reported_actual',
        period=dict(kind='interval',label="9M'2022",start='2022-01-01',end='2022-09-30'),
        amount=dict(value='1403',unit='money',currency='RUB'),value_text='1 403',
        evidence=[dict(page=1,line_start=1,line_end=6,area='Синтетическая таблица')],
        basis=dict(page=1,metric_label='Выручка',unit_label='в тысячах рублей',reporting_label='МСФО'),
        table=dict(page=1,row_line=6,year_line=5,period_line_start=4,period_line_end=4,column=1))


def test_table_cell_is_bound_to_metric_year_header_and_original_quote():
    f,b=candidate(PIN,PAGES,table_item(),LAYOUT)
    assert f['amount']==dict(value='1403',unit='money',currency='RUB',scale='1000')
    assert f['extraction_version']=='pdf-lines.v39' and f['raw_text']=='1 403'
    assert f['period']['end']=='2022-09-30' and PIN['sha256'] in b['definition']
    second=table_item();second.update(amount=dict(value='1004',unit='money',currency='RUB'),value_text='1 004')
    second['period'].update(label="9M'2021",start='2021-01-01',end='2021-09-30');second['table']['column']=2
    assert candidate(PIN,PAGES,second,LAYOUT)[0]['amount']['value']=='1004'


@pytest.mark.parametrize('mutation',[
    lambda i:i['table'].update(column=2),
    lambda i:i['table'].update(row_line=5),
    lambda i:i['period'].update(start='2021-01-01',end='2021-09-30',label="9M'2021"),
    lambda i:i['period'].update(end='2022-12-31'),
    lambda i:i['period'].update(kind='instant',start='2022-09-30'),
    lambda i:i.update(metric='Other metric'),
    lambda i:i.update(value_text='403',amount=dict(value='403',unit='money',currency='RUB')),
    lambda i:i['basis'].update(unit_label='млн рублей'),
    lambda i:i['amount'].update(currency='USD')])
def test_table_false_cell_period_unit_and_metric_rejected(mutation):
    i=table_item();mutation(i)
    with pytest.raises(ApiError):candidate(PIN,PAGES,i,LAYOUT)


def test_table_cannot_bypass_original_text_match_or_missing_layout():
    for pages,layout in [(PAGES,{}),({1:PAGES[1].replace('1 403','9 403')},LAYOUT)]:
        with pytest.raises(ApiError):candidate(PIN,pages,table_item(),layout)


def test_pdf_empty_password_is_supported_but_password_and_limits_still_enforced():
    writer=PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes())))
    writer.encrypt('',owner_password='synthetic-owner')
    buf=BytesIO();writer.write(buf)
    assert extract(buf.getvalue())['processor_version']==PROCESSOR_VERSION
    assert len(extract(buf.getvalue())['layouts'])==2
    assert extract(pdf_bytes(encrypted=True))=={'error':'unsupported_source'}
    assert extract(pdf_bytes(pages=41))=={'error':'too_large'}
    assert extract(b'%PDF-'+b' '*(16*1024*1024))=={'error':'too_large'}


def test_graphics_heavy_page_preserves_text_without_inferred_regions(monkeypatch):
    from app import pdf_worker
    monkeypatch.setattr(pdf_worker, 'MAX_CONTENT_BYTES', 1)
    result = extract(pdf_bytes(pages=2))
    assert result['diagrams'] == [[], []]
    for i, (text, layout) in enumerate(zip(result['pages'], result['layouts']), 1):
        assert f'Value {100+i}.' in text and f'Value {100+i}.' in layout
    assert extract(pdf_bytes(scanned=True))['pages'] == ['', '']


def observations():
    a=fact('1403',2022,'same');b=deepcopy(a)
    b['source']=dict(source_id='s_other',source_version_id='sv_other',sha256='c'*64)
    a['amount']['scale']='1000';b['amount'].update(value='1',scale='1000000')
    return [dict(data=x,calculation_basis=deepcopy(BASIS)) for x in (a,b)]


def test_rounding_and_real_conflict_preserve_both_qualified_refs_and_inputs():
    a,b=observations();old=deepcopy([a,b])
    result=compare_observations(a,b)
    assert result['value_relation']=='rounding_consistent' and result['comparability']=='established'
    assert len({r['source_version_id'] for r in result['fact_refs']})==2
    assert {r['fact_id'] for r in result['fact_refs']}=={'same'} and [a,b]==old
    b['data']['amount']['value']='2'
    assert compare_observations(a,b)['value_relation']=='conflict'
    b['data']['amount'].update(value='1.403',scale='1000000')
    assert compare_observations(a,b)['value_relation']=='equal'
    b['calculation_basis']['compatibility']='Другой периметр'
    assert compare_observations(a,b)['comparability']=='not_established'


@pytest.mark.parametrize('mutation',[
    lambda f:f.update(subject='Other Company'),
    lambda f:f['period'].update(end='2022-12-31'),
    lambda f:f['amount'].update(currency='USD')])
def test_period_currency_perimeter_do_not_become_rounding_match(mutation):
    a,b=observations();mutation(b['data'])
    assert compare_observations(a,b)['value_relation']=='not_comparable'


def test_growth_never_mixes_documents_even_when_ids_or_metadata_match():
    args,records=calculation();records['current']['data']['source']=PIN
    args['inputs'][1]['fact']['source_version_id']=PIN['source_version_id']
    with pytest.raises(ApiError):growth('calc_synthetic',args,records)


def test_unselected_or_forged_source_is_refused_before_fact_lookup():
    service=SimpleNamespace(scoped=Mock())
    run=dict(input={'source_versions':[PIN]},owner_id='u',project_id='p')
    with pytest.raises(ApiError):pinned_version(service,None,run,'sv_foreign')
    service.scoped.assert_not_called()
    service.scoped.return_value=dict(id=PIN['source_version_id'],source_id=PIN['source_id'],sha256='0'*64)
    with pytest.raises(ApiError):pinned_version(service,None,run,PIN['source_version_id'])


def test_missing_fact_ref_identifies_the_requested_id_without_guessing_a_replacement():
    from app.facts import resolve_fact
    service = SimpleNamespace(scoped=Mock(return_value={**PIN, 'id':PIN['source_version_id']}))
    run = dict(input={'source_versions':[PIN]}, owner_id='u', project_id='p')
    ref = dict(source_version_id=PIN['source_version_id'], fact_id='fact_typo')
    with patch('app.search.require_factual'), patch('app.facts.row', return_value=None) as lookup:
        with pytest.raises(ApiError) as error:
            resolve_fact(service, None, run, ref)
    assert error.value.code == 'resource_not_found'
    message = error.value.body['error']['message']
    assert ref['fact_id'] in message and ref['source_version_id'] in message
    assert 'успешного aist_get_facts' in message
    assert lookup.call_count == 1
    assert lookup.call_args.kwargs == dict(v=PIN['source_version_id'], f='fact_typo', p='p', a='u', sha=PIN['sha256'])


def test_same_fact_id_is_resolved_and_rendered_by_pair():
    a,b=observations();facts=[x['data'] for x in (a,b)]
    refs=[dict(source_version_id=f['source']['source_version_id'],fact_id=f['fact_id']) for f in facts]
    payload=dict(result_type='evidence_answer',content=dict(claims=[dict(text='Исходные наблюдения.',origin='inference',fact_refs=refs,calculation_ids=[])]))
    with patch('app.tools.resolve_fact',side_effect=lambda service,c,r,ref:next(x for x in (a,b) if x['data']['source']['source_version_id']==ref['source_version_id'])):
        result=resolve_basis(SimpleNamespace(service=None),None,dict(checkpoint={}),payload)
    assert len(result['facts'])==2
    v=sample();v['basis']['facts']=facts
    v['basis']['sources']=[dict(source=f['source'],original_filename=name,revision=1) for f,name in zip(facts,['Первый.pdf','Второй.pdf'])]
    row=v['payload']['content']['sections'][0]['blocks'][1]['rows'][0];row['fact_refs']=refs
    plan=content_plan(v,dict(release_mode='draft'),[])
    assert next(value for kind,value in plan if kind=='table')[0][3]==['1','2']
    assert any('Первый.pdf' in str(x) for x in plan) and any('Второй.pdf' in str(x) for x in plan)
    assert [value[0] for kind,value in plan if kind=='basis']==['1','2']


def test_release_uses_supporting_quote_and_preserves_substantive_limits_without_review_labels():
    v=sample();f=v['basis']['facts'][0]
    f.update(amount=None,metric='Изменение периметра',raw_text='Заголовок компании',
        evidence=[dict(quote='Заголовок компании',locator=dict(pdf_page=1)),
                  dict(quote='Изменение\nпериметра раскрыто в примечании.',locator=dict(pdf_page=2))],
        limitations=['Исторический PDF. Автоматически проверены ссылки, исходное написание числа и подпись периода. Полная смысловая и визуальная проверка не проведена.',
                     'Сопоставимость не установлена.'])
    old=deepcopy(v)
    plan=content_plan(v,dict(release_mode='draft'),[dict(kind='semantic',status='not_checked')])
    assert ('p','Изменение\nпериметра раскрыто в примечании.') in plan
    assert ('p','Сопоставимость не установлена.') in plan and ('p','Исторический PDF.') in plan
    assert 'Автоматически проверены' not in str(plan) and 'Смысловая проверка' not in str(plan)
    assert ('p','Заголовок компании') not in plan and v==old


def test_multiple_execution_reuses_first_source_and_extracts_only_the_selected_new_pdf(tmp_path):
    from app.openclaw_runtime import execution_facts
    pins=[dict(source_version_id='sv_old',sha256='a'*64),dict(source_version_id='sv_new',sha256='b'*64)]
    run=dict(id='run_multi',epoch=1,project_id='prj_test',owner_id='usr_test',input=dict(base_version_id=None,requested_result_type='analytical_note',source_versions=pins))
    c=Mock();ctx=Mock();ctx.__enter__=Mock(return_value=c);ctx.__exit__=Mock(return_value=False)
    runs=Mock();runs.engine.begin.return_value=ctx;runs.locked.return_value=run
    worker=SimpleNamespace(runs=runs)
    old={'fact_id':'fact_old','source':pins[0]};new={'fact_id':'fact_new','source':pins[1]}
    with patch('app.facts.pinned_version'), patch('app.openclaw_runtime.execute') as execute, \
            patch('app.openclaw_runtime.extract_context', return_value=(dict(facts=[new]), 'call_new')) as extract:
        execute.return_value.scalars.side_effect=[[old],[]]
        assert execution_facts(worker,run,'Task',tmp_path)==([old,new],'call_new')
        extract.assert_called_once_with(worker, run, pins[1], 'Task', None, tmp_path, None)



@pytest.mark.parametrize('suffix',['',' года'])
def test_table_accepts_literal_composed_period_and_document_wide_reporting_heading(suffix):
    item=table_item();item['period']['label']='За 9 месяцев, закончившихся 30 сентября 2022'+suffix
    item['evidence']=[dict(page=1,line_start=3,line_end=6,area='Table')]
    pages={1:PAGES[1].replace('МСФО',''),2:'МСФО'}
    # Original row indices in this fixture remain unchanged after an empty line.
    item['basis']['reporting_page']=2
    f,_=candidate(PIN,pages,item,LAYOUT)
    assert f['period']['label']==item['period']['label']
    assert any(e['locator']['pdf_page']==2 and 'МСФО' in e['quote'] for e in f['evidence'])
    item['period']['label']='За 6 месяцев, закончившихся 30 июня 2022'
    with pytest.raises(ApiError):candidate(PIN,pages,item,LAYOUT)


@pytest.mark.parametrize('error',[subprocess.TimeoutExpired('rpc',25),ApiError('outcome_unknown')])
def test_status_timeout_does_not_abort_or_redispatch_accepted_agent(error):
    from app.openclaw_runtime import session_status
    gateway=Mock();gateway.request.side_effect=error
    assert session_status(gateway,'session_synthetic') is None
    gateway.request.assert_called_once_with('sessions.describe',{'key':'session_synthetic'})
    gateway.request.side_effect=None;gateway.request.return_value={'session':{'hasActiveRun':False}}
    assert session_status(gateway,'session_synthetic') is False
    gateway.request.side_effect=ApiError('resource_not_found')
    with pytest.raises(ApiError):session_status(gateway,'session_synthetic')


def test_rpc_timeout_records_one_attempt_without_agent_redispatch(tmp_path):
    import json
    from app.openclaw_runtime import Gateway
    gateway=Gateway(SimpleNamespace(gateway_port=30413),tmp_path,{})
    with patch('app.openclaw_runtime.subprocess.run',side_effect=subprocess.TimeoutExpired('rpc',90)) as run:
        with pytest.raises(subprocess.TimeoutExpired):gateway.request('agent',{'idempotencyKey':'run_synthetic'})
    assert run.call_count==1 and run.call_args.kwargs['timeout']==90
    receipt=json.loads(next(tmp_path.glob('agent-*.json')).read_text())
    assert receipt=={'method':'agent','params':{'idempotencyKey':'run_synthetic'},'timeout_seconds':90}


def test_initial_and_editing_use_content_schema_with_optional_existing_ids():
    from app.openclaw_runtime import tool_schema
    initial=tool_schema('aist_save_artifact',new_artifact=True)
    assert initial==tool_schema('aist_save_artifact')
    assert set(initial['properties'])=={'content', 'questions', 'news_events', 'ownership_review'}
    assert initial['required'] == ['content']
    section=initial['properties']['content']['properties']['sections']['items']
    assert 'section_id' in section['properties'] and 'section_id' not in section['required']


@pytest.mark.parametrize('wording',['относительно девяти месяцев','к девяти месяцам','с девятью месяцами'])
def test_declined_period_and_qualitative_source_dates_keep_exact_boundaries(wording):
    from app.tools import check_periods,number_tokens
    period=dict(kind='interval',start='2021-01-01',end='2021-09-30')
    assert not number_tokens(check_periods(wording+' 2021 года',[period]))
    quotes=['Сравнительные данные за период 9 месяцев 2021 года. Консолидируется с 1 сентября 2022 года.']
    assert not number_tokens(check_periods('за девять месяцев 2021 года, с 1 сентября 2022 года',[],source_quotes=quotes))
    for wrong in ['за шесть месяцев 2021 года','2 сентября 2022 года','2021 год','1 сентября 2021 года']:
        with pytest.raises(ApiError):check_periods(wrong,[],source_quotes=quotes)
    with pytest.raises(ApiError):check_periods('1 сентября 2022 года',[])

def test_cited_labels_and_year_range_do_not_become_financial_amounts():
    from app.tools import cited_year_intervals, source_names
    quote = '2022 - 2023. Moody’s: Ba1, стабильный. Канал B2B.'
    text = 'В 2022-2023 годах сегмент B2B. Рейтинг Ba1. Сумма 17 млн руб.'
    cleaned = source_names(cited_year_intervals(text, [quote]), [quote])
    assert '2022' not in cleaned and 'B2B' not in cleaned and 'Ba1' not in cleaned
    assert '17 млн руб.' in cleaned
    wrong = 'В 2022-2024 годах сегмент B2C. Рейтинг Ba2. Изменение -17.'
    assert source_names(cited_year_intervals(wrong, [quote]), [quote]) == wrong
