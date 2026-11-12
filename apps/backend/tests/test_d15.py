"""Retrieval boundaries and untrusted provider responses; no fake vectors in production."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app.embeddings import chunks, normalize, input_text, response_vectors, CONFIG, novita_request
from app.errors import ApiError
from app.search import authorized_versions, Qdrant, require_factual
from app.contracts import validate_definition


def test_chunks_retain_exact_offsets_cover_pages_and_bound_input():
    page=('Отчетность 2022\n  Прокат\tсамокатов зависит от погоды.\n'*97)+'конец'
    values=list(chunks(page));covered=set()
    for start,end,quote in values:
        assert quote==page[start:end] and 0<end-start<=800
        assert len(input_text(quote))<=1500
        covered.update(range(start,end))
    assert covered==set(range(len(page)))
    assert len(values)>1 and values[1][0]<=values[0][1]
    assert input_text(' вопрос\n  другой ',True).endswith('\nQuery: вопрос другой')
    assert not input_text('документ').startswith('Instruct:')
    assert normalize('Ａ  \tБ')=='A Б'


def response():
    # Deliberately synthetic transport data, used only for validation tests.
    return {'model':CONFIG['model'],'data':[{'index':1,'embedding':[2.0]+[0.0]*4095},{'index':0,'embedding':[0.0,3.0]+[0.0]*4094}]}


def test_provider_indices_are_reordered_and_full_vectors_normalized():
    v=response_vectors(response(),2)
    assert len(v[0])==4096 and v[0][1]==1 and v[1][0]==1


@pytest.mark.parametrize('mutation',[
    lambda x:x.update(model='different-model'),
    lambda x:x['data'][0].update(index=0),
    lambda x:x['data'][0].update(index=True),
    lambda x:x['data'][0].update(embedding=[1]*256),
    lambda x:x['data'][0].update(embedding=[0]*4096),
    lambda x:x['data'][0]['embedding'].__setitem__(1,float('nan')),
    lambda x:x['data'][0]['embedding'].__setitem__(1,float('inf')),
    lambda x:x['data'][0]['embedding'].__setitem__(1,True),
    lambda x:x['data'].pop()])
def test_invalid_responses_never_become_embeddings(mutation):
    value=response();mutation(value)
    with pytest.raises(ApiError,match='embedding_invalid_response'):response_vectors(value,2)


def test_sample_is_never_a_fact_or_calculation_basis():
    with patch('app.search.material',return_value={'corpus':'samples'}):
        with pytest.raises(ApiError) as e:require_factual(None,'sv_sample')
        assert e.value.code=='sample_not_evidence'


def test_same_authority_filter_for_ui_and_tools_before_ranking():
    run={'owner_id':'usr_a','project_id':'prj_a','input':{'source_versions':[{'source_id':'s_a','source_version_id':'sv_a','sha256':'a'*64}]}}
    v={'id':'sv_a','source_id':'s_a','sha256':'a'*64,'processing_state':'ready'}
    calls=[]
    def scoped(c,actor,project,table='projects',object_id=None):
        calls.append((actor,project,object_id))
        if actor!='usr_a' or project!='prj_a' or object_id not in (None,'sv_a'):raise ApiError('resource_not_found')
        return v.copy()
    service=SimpleNamespace(scoped=scoped)
    args={'source_version_ids':['sv_a'],'corpus':'factual','company':'Whoosh','period':'9M2022'}
    with patch('app.search.material',return_value={'corpus':'factual','company':'Whoosh','period':'9M2022'}):
        assert list(authorized_versions(service,None,run,args))==['sv_a']
        for change in ({'company':'CarMoney'},{'period':'2025'},{'corpus':'samples'}):
            assert authorized_versions(service,None,run,{**args,**change})=={}
        with pytest.raises(ApiError):authorized_versions(service,None,run,{**args,'source_version_ids':['sv_unselected']})
        with pytest.raises(ApiError):authorized_versions(service,None,{**run,'owner_id':'usr_b'},args)
        with pytest.raises(ApiError):authorized_versions(service,None,{**run,'project_id':'prj_b'},args)
    assert calls


def test_429_timeout_and_qdrant_errors_have_no_retry_or_fallback():
    with patch('app.novita.credential',return_value='private-test-token'),patch('app.embeddings.http.client.HTTPSConnection') as connection:
        connection.return_value.getresponse.return_value.status=429
        connection.return_value.getresponse.return_value.read.return_value=b'{}'
        with pytest.raises(ApiError) as e:novita_request(['q'],'unused')
        assert e.value.code=='embedding_rate_limited' and connection.return_value.request.call_count==1
        connection.return_value.getresponse.side_effect=TimeoutError()
        with pytest.raises(ApiError) as e:novita_request(['q'],'unused')
        assert e.value.code=='embedding_unavailable' and connection.return_value.request.call_count==2
    with patch('app.search.urllib.request.urlopen',side_effect=TimeoutError()) as url:
        with pytest.raises(ApiError) as e:Qdrant(SimpleNamespace(qdrant_url='http://localhost')).request('GET','/collections')
        assert e.value.code=='search_index_unavailable' and url.call_count==1


def test_search_contract_keeps_legacy_and_explicit_semantic_method():
    base={'query':'вопрос','source_version_ids':['sv_a'],'limit':5}
    validate_definition('SearchEvidenceArgs',base)
    validate_definition('SearchEvidenceArgs',{**base,'method':'semantic.qwen3.v1','corpus':'samples','company':None,'period':None})
    with pytest.raises(Exception):validate_definition('SearchEvidenceArgs',{**base,'method':'hash.v1'})

@pytest.mark.parametrize('cancel_during_search',[False,True])
def test_semantic_search_releases_run_lock_and_refences_before_saving(cancel_during_search):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from app.runs import Runs
    active={'locked':False,'cancelled':False}
    @contextmanager
    def begin():
        try:yield Mock()
        finally:active['locked']=False
    runs=object.__new__(Runs);runs.engine=SimpleNamespace(begin=begin)
    run=dict(id='run_probe',project_id='prj_probe',owner_id='usr_probe',input={'budget':{'max_tool_calls':24}})
    def locked(*args):active['locked']=True;return run
    def fence(*args,**kwargs):
        if active['cancelled']:raise ApiError('run_terminal')
    def retrieve(*args):
        assert not active['locked'],'Retrieval blocks heartbeat on the Run row'
        active['cancelled']=cancel_during_search
        return {'items':[],'hits':[]}
    runs.locked=locked;runs.fence=fence;runs.event=Mock()
    args={'method':'semantic.qwen3.v1','query':'вопрос'}
    with patch('app.runs.row',return_value=None),patch('app.runs.execute') as sql,patch('app.tools.invoke',side_effect=retrieve) as invoke:
        sql.return_value.scalar_one.return_value=0
        if cancel_during_search:
            with pytest.raises(ApiError,match='run_terminal'):runs._tool_once('run_probe',1,'tool_probe','aist_search_evidence',args)
            runs.event.assert_not_called()
        else:
            assert runs._tool_once('run_probe',1,'tool_probe','aist_search_evidence',args)['ok']
            runs.event.assert_called_once()
        invoke.assert_called_once()


def test_pricing_and_batch_size_do_not_change_published_index_identity():
    from app.embeddings import INDEX_ID, PRICING, index_id
    assert INDEX_ID == 'qwen3_677d88a30eee8bfc'
    legacy = {**CONFIG, **PRICING}
    changed_accounting = {**legacy, 'pricing_checked_on': '2026-10-16',
                          'price_usd_per_million_input': '0.10', 'max_batch': 8}
    assert index_id(legacy) == index_id(changed_accounting) == INDEX_ID


@pytest.mark.parametrize('field,value', [
    ('provider', 'different'), ('model', 'different'), ('base_url', 'https://different.invalid'),
    ('dimension', 2048), ('weight_revision', 'confirmed-new-revision'),
    ('text_processing', 'different.v3'), ('query_instruction', 'Different instruction'),
    ('normalization', 'none'), ('similarity', 'Dot'),
])
def test_changed_embedding_semantics_get_a_separate_index(field, value):
    from app.embeddings import INDEX_ID, index_id
    assert index_id({**CONFIG, field: value}) != INDEX_ID
