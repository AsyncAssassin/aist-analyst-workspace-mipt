"""Synthetic inputs exercise extraction trust, never the acceptance document."""
from copy import deepcopy
from decimal import Decimal
import json
import threading
import time
import urllib.error
import urllib.request
import pytest
from app.accounting import response_fields
from app.errors import ApiError
from app.extraction import candidate, extraction_request, extraction_schema
from app.openclaw_runtime import to_sse, PrivateHTTPServer, ProviderHandler, tool_schema, TOOLS
from app.live_profile import delivered
from app.contracts import PACKAGE, validate_definition

PIN={'source_id':'s_test','source_version_id':'sv_test','sha256':'f'*64}
PAGES={1:'Synthetic Company\nВыручка, млн руб.\nМСФО\n1 2 3\n9 M ’ 2 0\n1 4 0\n9 M ’ 2 1'}

def item():
    return {'metric':'Выручка','subject':'Synthetic Company','reporting_basis':'МСФО',
      'nature':'reported_actual','period':{'kind':'interval','label':'9M’20','start':'2020-01-01','end':'2020-09-30'},
      'amount':{'value':'123','unit':'money','currency':'RUB'},'value_text':'1 2 3',
      'evidence':[{'page':1,'line_start':1,'line_end':5,'area':'Synthetic chart'}],
      'basis':{'page':1,'metric_label':'Выручка','unit_label':'млн руб.','reporting_label':'МСФО'}}

def test_source_line_ingest_preserves_raw_and_basis():
    f,b=candidate(PIN,PAGES,item())
    assert f['raw_text']=='1 2 3' and f['amount']['value']=='123'
    assert f['evidence'][0]['quote']== '\n'.join(PAGES[1].splitlines()[:5])
    assert PIN['sha256'] in b['definition']
    assert f['extraction_version']=='pdf-lines.v39' and f['amount']['scale']=='1000000'

@pytest.mark.parametrize('mutation',[
    lambda i:i['amount'].update(value='124'),
    lambda i:i.update(value_text='2 3',amount={'value':'23','unit':'money','scale':'1000000','currency':'RUB'}),
    lambda i:i['evidence'][0].update(page=2),
    lambda i:i['evidence'][0].update(line_end=99),
    lambda i:i['period'].update(label='9M’21',start='2021-01-01',end='2021-09-30'),
    lambda i:i['period'].update(end='2020-12-31'),
    lambda i:i.update(metric='EBITDA'),
    lambda i:i.update(subject='Other Company'),
    lambda i:i.update(reporting_basis='РСБУ'),
    lambda i:i['amount'].update(scale='1'),
    lambda i:i['amount'].update(currency='USD'),
    lambda i:i['basis'].update(metric_label='Adjusted revenue'),
    lambda i:i.update(review={'method':'reviewed_locator','reference':'bypass'}),
])
def test_false_extraction_is_rejected(mutation):
    data=item();mutation(data)
    with pytest.raises(ApiError):candidate(PIN,PAGES,data)

def test_inline_number_reports_unsupported_layout_and_keeps_qualitative_evidence():
    pages={1:PAGES[1].replace('1 2 3\n','1 2 3 млн руб.\n')}
    data=item()
    with pytest.raises(ApiError) as unsupported:
        candidate(PIN,pages,data)
    assert unsupported.value.code == 'validation_failed'
    data['amount']['value']='124'
    with pytest.raises(ApiError) as wrong_value:
        candidate(PIN,pages,data)
    assert 'исходному написанию' in wrong_value.value.body['error']['message']
    data.update(amount=None,value_text=None,basis=None,nature='source_statement',
                period=dict(kind='unknown',label='Не указан',start=None,end=None))
    fact,basis=candidate(PIN,pages,data)
    assert fact['amount'] is None and basis=={}
    assert '1 2 3 млн руб.' in fact['evidence'][0]['quote']

def test_new_document_text_drives_prompt_and_no_oracle_is_loaded():
    request=extraction_request({3:'Unique synthetic source'},'A different request')
    data=json.loads(request['messages'][1]['content'])
    assert data['pages']==[{'page':3,'lines':[{'line':1,'text':'Unique synthetic source'}]}]
    assert data['task']=='A different request'
    assert request['response_format']['type']=='json_object'
    assert data['output_schema']==extraction_schema()
    from jsonschema import Draft202012Validator
    validator=Draft202012Validator(extraction_schema())
    validator.validate({'facts':[item()]})
    incomplete=item();del incomplete['basis']
    assert list(validator.iter_errors({'facts':[incomplete]}))

def completion():
    return {'id':'synthetic-completion','created':1,'model':'synthetic',
      'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
       'reasoning_content':'Original reasoning history','tool_calls':[{'id':'call_original','type':'function',
       'function':{'name':'aist_read_source','arguments':'{"source_version_id":"sv_test","page":3}'}}]}}],
      'usage':{'prompt_tokens':70,'completion_tokens':30,'prompt_tokens_details':{'cached_tokens':20},'completion_tokens_details':{'reasoning_tokens':12}}}

def test_live_usage_and_sse_keep_original_ids_and_reasoning():
    c=completion();identifier,tokens,valid=response_fields(c,live=True)
    assert valid and tokens['cached_input']['value']==20 and tokens['reasoning_output']['value']==12
    assert not response_fields(c)[2]
    chunks=[json.loads(line[6:]) for line in to_sse(c).splitlines() if line.startswith('data: {')]
    delta=chunks[0]['choices'][0]['delta']
    assert delta['reasoning_content']==c['choices'][0]['message']['reasoning_content']
    assert delta['tool_calls'][0]['id']=='call_original'
    assert chunks[-1]['usage']==c['usage']
    c['choices'][0]['finish_reason']='length';assert not response_fields(c,live=True)[2]

def test_delivered_registry_hashes_and_tool_schemas():
    registry=delivered(json.loads((PACKAGE/'registry.json').read_text()))
    validate_definition('Registry',registry)
    assert len([s for s in registry['skills'] if s['status']=='enabled'])==6
    assert set(TOOLS)=={'aist_read_source','aist_search_evidence','aist_get_facts','aist_calculate',
                        'aist_save_artifact','aist_request_input','aist_web_search','aist_web_fetch','aist_read_task','aist_read_web','aist_research'}
    for name in TOOLS:
        schema=tool_schema(name)
        assert schema['type']=='object' and '$ref' not in json.dumps(schema)
    save=tool_schema('aist_save_artifact')['properties']
    assert set(save)=={'content', 'questions', 'news_events', 'ownership_review'} and save['content']['type']=='object'
    assert tool_schema('aist_save_artifact')['required'] == ['content']
    from app.contracts import ROOT
    manifest=json.loads((ROOT/'executor/openclaw/provider/openclaw.plugin.json').read_text())
    assert manifest['providers']==['novita-aist','anthropic-aist','openai-aist']

def test_loopback_provider_has_no_tool_endpoint():
    class Boundary:
        token='synthetic'
        def model(self,body):raise AssertionError('must not dispatch')
    server=PrivateHTTPServer(('127.0.0.1',0),ProviderHandler);server.boundary=Boundary()
    thread=threading.Thread(target=server.serve_forever);thread.start()
    try:
        for path in ('/tool','/attach','/cancel','/finish'):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,data=b'{}',headers={'Authorization':'Bearer synthetic'}))
            assert e.value.code==423
    finally:server.shutdown();server.server_close();thread.join()

def test_server_close_waits_for_inflight_handler():
    from http.server import BaseHTTPRequestHandler
    entered=threading.Event();release=threading.Event();closed=threading.Event()
    class Slow(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            entered.set();release.wait(3);self.send_response(200);self.end_headers()
    server=PrivateHTTPServer(('127.0.0.1',0),Slow)
    serving=threading.Thread(target=server.serve_forever);serving.start()
    reader=threading.Thread(target=lambda:urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}').read());reader.start()
    assert entered.wait(2);server.shutdown()
    closer=threading.Thread(target=lambda:(server.server_close(),closed.set()));closer.start()
    assert not closed.wait(.1)
    release.set();closer.join(3);reader.join(3);serving.join(3)
    assert closed.is_set()

def test_forged_session_id_arguments_or_tool_id_never_reach_runs(tmp_path):
    from types import SimpleNamespace
    from app.openclaw_runtime import Boundary
    class RejectCalls:
        def tool(self,*a):raise AssertionError('Untrusted request reached durable tool')
    worker=SimpleNamespace(runs=RejectCalls())
    b=Boundary(worker,{'id':'run_test','executor_session_id':'session_owned','input':{}},tmp_path,'synthetic')
    b.issued['call_original']={'name':'aist_read_source','args':{'source_version_id':'sv_owned','page':1},'model':'call_model'}
    good={'session':'session_owned','tool_call_id':'call_original','name':'aist_read_source','args':{'source_version_id':'sv_owned','page':1}}
    for change in [{'session':'session_foreign'},{'tool_call_id':'call_forged'},{'args':{'source_version_id':'sv_foreign','page':1}},{'actor':'usr_forged'}]:
        with pytest.raises(ApiError):b.tool(good|change)


def test_scale_is_derived_only_from_literal_source_unit():
    for label,scale in [('млн руб.','1000000'),('тыс руб.','1000'),('млрд руб.','1000000000')]:
        data=item();data['basis']['unit_label']=label
        pages={1:PAGES[1].replace('млн руб.',label)}
        f,_=candidate(PIN,pages,data)
        assert f['amount']['value']=='123' and f['amount']['scale']==scale
    data=item();data['subject']='АО «Synthetic Company»'
    assert candidate(PIN,PAGES,data)[0]['subject']=='Synthetic Company'

def test_source_reporting_heading_can_be_shorter_than_full_source_phrase():
    data=item();data['reporting_basis']='МСФО отчетность Компании'
    pages={1:PAGES[1].replace('МСФО','МСФО отчетность Компании')}
    f,b=candidate(PIN,pages,data)
    assert f['reporting_basis']=='МСФО отчетность Компании' and 'МСФО' in b['compatibility']

def test_worker_requires_exact_delivered_skill_and_model_pins():
    from app.live_profile import verify_pins
    registry=delivered(json.loads((PACKAGE/'registry.json').read_text()))
    configuration={'skill_sha256':next(s['bundle_sha256'] for s in registry['skills'] if s['id']=='skill_analytical-note-v1'),
                   'model_configuration_sha256':registry['model_profiles'][0]['configuration_sha256']}
    run_input={'skill_version_id':'skill_analytical-note-v1','requested_result_type':'analytical_note'}
    verify_pins(configuration,run_input)
    for key in configuration:
        with pytest.raises(ApiError):verify_pins(configuration|{key:'0'*64},run_input)

def test_prose_numbers_do_not_merge_period_suffix_with_next_year():
    from app.tools import number_tokens
    assert number_tokens('9M’20 2020-01-01')=={Decimal(9),Decimal(20),Decimal(2020),Decimal(1)}
    assert number_tokens('1234 5678')=={Decimal(1234),Decimal(5678)}
    assert number_tokens('1 234 567,89')=={Decimal('1234567.89')}
