"""Готовые поисковые инструменты, честное происхождение и бюджет исследования."""
import json
import time
from unittest.mock import Mock
from fastapi.testclient import TestClient
from jsonschema import ValidationError, validate
import pytest
from app import external_sources, news_sources, openclaw_runtime
from app.contracts import validate_definition
from app.errors import ApiError

QUERY = dict(objective='Найти сведения о новой компании', queries=['Новая Компания рынок отчет'],
             search_target='Новая Компания', search_role='company', topics=['business'])
RECEIPT = dict(bytes_read=345, sha256='a'*64)


def test_general_search_passes_only_explicit_query_and_discards_private_urls(monkeypatch):
    response = dict(result=dict(structuredContent=dict(results=[
        dict(url='https://new-company.example/about', title='Компания', excerpts=['Search snippet']),
        dict(url='http://127.0.0.1/private', title='private', excerpts=[])])))
    request = Mock(return_value=(response, RECEIPT))
    monkeypatch.setattr(external_sources, 'internal_json', request)
    result = external_sources.search(QUERY, time.monotonic()+5, 'a'*32)
    assert result == dict(provider='parallel-free', bytes_read=345, results=[
        dict(url='https://new-company.example/about', title='Компания', snippet='Search snippet')])
    arguments = request.call_args.kwargs['body']['params']['arguments']
    assert arguments == dict(objective=QUERY['objective'], search_queries=QUERY['queries'], session_id='a'*32)
    assert request.call_args.args[0] == 'https://search.parallel.ai/mcp'
    assert set(request.call_args.kwargs['headers']) == {'Accept'}


@pytest.mark.parametrize('response', [dict(error={}), dict(result=dict(isError=True)), dict(result=dict(structuredContent={}))])
def test_bad_search_response_is_unavailable(monkeypatch, response):
    monkeypatch.setattr(external_sources, 'internal_json', lambda *a, **k: (response, RECEIPT))
    with pytest.raises(ValueError):
        external_sources.search(QUERY, time.monotonic()+5)


@pytest.mark.parametrize('published,expected', [('2026-01-01','2026-01-01'), ('2030-01-01',None), ('bad',None)])
def test_extracted_text_has_provider_receipt_not_original_hash(monkeypatch, published, expected):
    text = 'Компания производит оборудование и поставляет продукцию промышленным клиентам. '*200
    payload = dict(results=[dict(url='https://company.example/report.pdf', title='Отчет', excerpts=[text],
        publish_date=published, acquired_at='2026-11-01T00:00:00Z')])
    request = Mock(return_value=(dict(result=dict(structuredContent=payload)), RECEIPT))
    monkeypatch.setattr(external_sources, 'internal_json', request)
    result = external_sources.read_page('https://company.example/report.pdf', '2026-11-08', time.monotonic()+5, 160000)
    assert result['page_sha256'] is None and result['response_sha256'] == RECEIPT['sha256']
    assert result['retrieval_kind'] == 'provider_extract' and result['truncated']
    assert result['quote'] == text.strip() and result['published'] == expected
    assert result['provider_acquired_at'] == '2026-11-01T00:00:00Z'
    result.pop('bytes_read')
    result.update(evidence_id='ext_test', source_kind='publication', search_target='Компания', search_role='company', topics=['business'])
    validate_definition('ExternalEvidence', result)
    result['page_sha256'] = 'b'*64
    with pytest.raises(ValidationError):
        validate_definition('ExternalEvidence', result)


def test_missing_extraction_is_not_original_text(monkeypatch):
    monkeypatch.setattr(external_sources, 'provider_result', lambda *args: (dict(results=[]), RECEIPT))
    assert external_sources.read_page('https://company.example/', '2026-11-08', time.monotonic()+5, 160000) == dict(unavailable=True, bytes_read=345)


def test_provider_news_accepts_new_domain_with_complete_provenance():
    item = dict(quote='Компания открыла новый магазин.', url='https://company.example/news/new-store',
        publisher='company.example', published='2026-01-01', page_sha256=None,
        retrieval_kind='provider_extract', provider='parallel', provider_acquired_at=None,
        response_sha256='a'*64)
    validate_definition('NewsItem', item)
    without_receipt = {key: value for key, value in item.items() if key != 'response_sha256'}
    with pytest.raises(ValidationError):
        validate_definition('NewsItem', without_receipt)
    legacy = {key: item[key] for key in ('quote', 'url', 'publisher', 'published')}
    legacy['page_sha256'] = 'b'*64
    with pytest.raises(ValidationError):
        validate_definition('NewsItem', legacy)
    legacy['url'] = 'https://ir.zaymer.ru/news/company-news/post-123'
    validate_definition('NewsItem', legacy)


def test_http_routes_preserve_expiry_and_public_session(monkeypatch):
    seen = []
    def search(query, deadline, session_id):
        seen.append((query, deadline, session_id))
        return dict(results=[], provider='parallel-free', bytes_read=0)
    monkeypatch.setattr(external_sources, 'search', search)
    with TestClient(news_sources.app) as client:
        result = client.get('/web-search', params=dict(arguments=json.dumps(QUERY), expires=time.time()-1, session_id='a'*32))
    assert result.status_code == 200
    assert seen[0][0] == QUERY and seen[0][1] <= time.monotonic() and seen[0][2] == 'a'*32


def test_openclaw_schemas_expose_search_fetch_and_private_transition():
    for name, arguments in [('aist_web_search', QUERY), ('aist_web_fetch', dict(search_id='tool_result', url='https://company.example/about')), ('aist_read_task', {})]:
        validate(arguments, openclaw_runtime.tool_schema(name))
        validate_definition(openclaw_runtime.TOOLS[name], arguments)


def test_public_stage_omits_private_task_sources_and_history():
    from app.note_external import public_research_message
    run = dict(external_context=dict(research_version='agent.v1', company='Новая Компания', as_of='2026-11-08',
        identity=dict(company='Новая Компания', inn='', sector='', domain='')),
        input=dict(model_profile_version_id='model_anthropic-fable-xhigh-v1', budget=dict(max_model_calls=10),
        parameters={'secret': 'private input'}, source_versions=['private source']),
        configuration=dict(conversation_context={'private': 'private conversation'}), message_id='private task')
    message = public_research_message(run, 0)
    assert 'Новая Компания' in message and 'private' not in message
    with pytest.raises(ApiError) as error:
        public_research_message(run, 7)
    assert error.value.code == 'budget_exceeded'
    run['external_context']['research_closed'] = True
    assert public_research_message(run, 0) is None


@pytest.mark.parametrize('profile,calls,seconds', [('model_anthropic-fable-xhigh-v1', 10, 1200), ('model_anthropic-fable-xhigh-v2', 16, 1800), ('model_novita-mimo-v1', 16, 1800)])
def test_research_budget_supports_discovery_refinement_and_note(profile, calls, seconds):
    from app.collection import bounded_budget
    policy = dict(max_model_calls=16, max_duration_seconds=1800)
    budget = bounded_budget(policy, 'skill_analytical-note-v3', 'analytical_note', None, profile)
    assert budget['max_model_calls'] == calls and budget['max_duration_seconds'] == seconds
    assert budget['completion_calls'] == 3
    tight = bounded_budget(dict(max_model_calls=2, max_duration_seconds=100), 'skill_analytical-note-v3', 'analytical_note', None, profile)
    assert tight['max_model_calls'] == 2 and tight['max_duration_seconds'] == 100


def test_known_http_refusal_counts_received_bytes(monkeypatch):
    def unavailable(*args):
        error = ValueError('HTTP 403')
        error.bytes_read = 0
        raise error
    monkeypatch.setattr(external_sources, 'read_page', unavailable)
    with TestClient(news_sources.app) as client:
        response = client.get('/web-fetch', params=dict(url='https://company.example/', as_of='2026-11-08', expires=time.time()+10))
    assert response.json() == dict(unavailable=True, bytes_read=0)
