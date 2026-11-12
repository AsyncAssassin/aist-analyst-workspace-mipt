"""Поиск навыком: резерв до сети, собственные источники, версия и ограничения Run."""
from copy import deepcopy
from datetime import date
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

from app import external_sources, news_sources, note_external
from app.contracts import validate_definition
from app.errors import ApiError
from app.normalization import fingerprint
from app.runs import execute, json_value, now
from app.service import ident
from test_d15_postgres import workspace
from test_aso_quality02_phase3_postgres import start, deal_note, accept
from test_perf02_r4_postgres import note_source
from test_draft01_r1_postgres import save

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL')
QUERY = dict(objective='Описание бизнеса и рынка Орбиты', queries=['Орбита компания рынок'],
             search_target='Орбита', search_role='company', topics=['business', 'market'])
URL = 'https://company.example/business'
TEXT = 'Орбита производит оборудование и поставляет его клиентам на российском рынке. Материал описывает бизнес компании.'


def initialized(w, *, research_version='agent.v1'):
    if research_version == 'agent.v2':
        w.runs.policy['run']['max_model_calls'] = 16
    source, _ = note_source(w)
    run = start(w, source)
    note_external.prepare_all(w.runs, run)
    # Эти проверки сохраняют контракт прежнего публичного этапа. Два контекста проверяются отдельно.
    if research_version != run['external_context']['research_version']:
        run['external_context']['research_version'] = research_version
        with w.engine.begin() as c:
            execute(c, 'UPDATE aist.runs SET external_context=CAST(:value AS jsonb) WHERE id=:r',
                    value=json_value(run['external_context']), r=run['id'])
    return source, run


def reader(calls):
    def request(name, args, as_of, seconds, max_bytes, session_id=None, objective=None):
        calls.append((name, deepcopy(args)))
        assert 0 < seconds <= 45 and 0 < max_bytes <= 160_000
        if name == 'aist_web_search':
            return dict(results=[dict(url=URL, title='Описание компании', snippet='Поисковый фрагмент, не основание')], bytes_read=100)
        return dict(url=URL, title='Описание компании', published='2026-01-05', retrieved_at='2026-11-08T00:00:00+00:00',
                    page_sha256='a'*64, quote=TEXT, publisher='company.example', bytes_read=200, truncated=False)
    return request


@pytest.fixture
def local_parallel(monkeypatch):
    """Настоящие HTTP-вызовы backend -> News -> локальный MCP."""
    state = SimpleNamespace(news=[], rpc=[], responses=[], search=[dict(url=URL, excerpts=['сниппет'])],
                            fetch=[dict(url=URL, excerpts=[TEXT], publish_date='2025-01-05',
                                        acquired_at='2026-11-01T00:00:00Z')])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, data):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            route = urlsplit(self.path)
            fields = dict(parse_qsl(route.query, keep_blank_values=True))
            state.news.append((route.path, dict(fields)))
            fields['expires'] = float(fields['expires'])
            if route.path == '/web-search':
                result = news_sources.web_search(**fields)
            else:
                assert route.path == '/web-fetch'
                fields['as_of'] = date.fromisoformat(fields['as_of'])
                fields['max_bytes'] = int(fields['max_bytes'])
                result = news_sources.web_fetch(**fields)
            self.send_json(json.dumps(result).encode())

        def do_POST(self):
            assert self.path == '/mcp'
            assert 'Authorization' not in self.headers and 'Cookie' not in self.headers
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.rpc.append(request)
            name = request['params']['name']
            records = {'web_search': state.search, 'web_fetch': state.fetch}[name]
            result = dict(error=dict(code=-32603)) if records is None else dict(
                result=dict(structuredContent=dict(results=records)))
            data = json.dumps(dict(result, jsonrpc='2.0', id=request['id']), ensure_ascii=False).encode()
            state.responses.append(data)
            self.send_json(data)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        origin = 'http://127.0.0.1:' + str(server.server_port)
        monkeypatch.setenv('AIST_NEWS_ORIGIN', origin)
        monkeypatch.setattr(external_sources, 'SEARCH_ORIGIN', origin + '/mcp')
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()


def network_state(w, run):
    with w.engine.connect() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        requests = execute(c, 'SELECT id,state,url,objective,arguments_sha256 FROM aist.web_requests WHERE run_id=:r ORDER BY id',
                           r=run['id']).mappings()
        requests = [dict(item) for item in requests]
    return context, requests


def test_fetch_requires_url_from_selected_search_before_transport(workspace, local_parallel):
    w = workspace
    source, run = initialized(w)
    parallel = local_parallel
    allowed = URL + '?a=1&a=2&x=%2F+'
    article_link = 'https://company.example/linked'
    excerpt = TEXT + ' ' + article_link
    parallel.search = [dict(url=allowed, excerpts=[article_link])]
    parallel.fetch = [None, dict(parallel.fetch[0], url='https://COMPANY.EXAMPLE:443/business?a=1&a=2&x=%2F+#f',
                                 excerpts=[excerpt])]
    query = dict(QUERY, objective='Синтетический маркер Objective A21', queries=['Synthetic Query A21'])
    def tool(key, name, args, current=run):
        return w.runs.tool(current['id'], current['epoch'], key, name, args)
    found = tool('search', 'aist_web_search', query)['result']
    baseline = network_state(w, run)
    candidates = ['https://other.example/business', URL + '/changed?a=1&a=2&x=%2F+',
                  URL + '?a=2&a=1&x=%2F+', URL + '?a=1&x=%2F+', URL + '?a=1&a=2&x=/+',
                  URL + '?a=1&a=2&x=%2F%20', article_link]
    for index, candidate in enumerate(candidates):
        with pytest.raises(ApiError, match='resource_not_found'):
            tool(f'rejected-{index}', 'aist_web_fetch', dict(search_id=found['search_id'], url=candidate))
    assert len(parallel.news) == len(parallel.rpc) == 1 and network_state(w, run) == baseline

    args = dict(search_id=found['search_id'], url='https://COMPANY.EXAMPLE:443/business?a=1&a=2&x=%2F+#input')
    fetch = tool('fetch', 'aist_web_fetch', args)
    evidence = fetch['result']['evidence']
    session = fingerprint({'public_research': run['id']})
    assert [entry['params'] for entry in parallel.rpc] == [
        dict(name='web_search', arguments=dict(objective=query['objective'], search_queries=query['queries'], session_id=session)),
        dict(name='web_fetch', arguments=dict(urls=[allowed], full_content=False, allow_live_fetch=True,
                                              objective=query['objective'], session_id=session))]
    assert all(set(rpc) == {'jsonrpc', 'id', 'method', 'params'} and rpc['method'] == 'tools/call' for rpc in parallel.rpc)
    assert parallel.news[-1][1]['url'] == allowed
    assert evidence['url'] == allowed and evidence['quote'] == excerpt and evidence['published'] == '2025-01-05'
    assert evidence['provider_acquired_at'] == '2026-11-01T00:00:00Z'
    assert evidence['retrieval_kind'] == 'provider_extract' and evidence['provider'] == 'parallel'
    assert evidence['page_sha256'] is None and evidence['response_sha256'] == hashlib.sha256(parallel.responses[-1]).hexdigest()
    context, requests = network_state(w, run)
    fetch_request = next(item for item in requests if item['url'])
    assert fetch_request['arguments_sha256'] == fingerprint({'name': 'aist_web_fetch', 'args': args})
    assert fetch_request['url'] == allowed and fetch_request['objective'] == query['objective']
    assert context['requests'] == 2 and context['bytes_read'] == sum(map(len, parallel.responses))
    assert tool('read-retained', 'aist_read_web', dict(evidence_id=evidence['evidence_id'], offset=0))['result']['text'] == excerpt
    assert tool('fetch', 'aist_web_fetch', args) == fetch
    with pytest.raises(ApiError, match='idempotency_conflict'):
        tool('fetch', 'aist_web_fetch', dict(args, url=allowed))

    parallel.search = [dict(url=article_link)]
    other = tool('other-search', 'aist_web_search', dict(query, queries=['Другая выдача']))['result']
    parallel.search = []
    empty = tool('empty-search', 'aist_web_search', dict(query, queries=['Пустая выдача']))['result']
    parallel.search = None
    failed = tool('failed-search', 'aist_web_search', dict(query, queries=['Ошибка выдачи']))['result']
    assert empty['status'] == 'empty' and failed['status'] == 'unavailable'
    baseline = network_state(w, run)
    counts = len(parallel.news), len(parallel.rpc)
    for index, identifier in enumerate([other['search_id'], empty['search_id'], failed['search_id'],
                                        fetch_request['id'], 'tool_missing']):
        with pytest.raises(ApiError, match='resource_not_found'):
            tool(f'wrong-search-{index}', 'aist_web_fetch', dict(search_id=identifier, url=allowed))
    with pytest.raises(ApiError, match='resource_not_found'):
        tool('linked', 'aist_web_fetch', dict(search_id=found['search_id'], url=article_link))
    _, foreign = initialized(w)
    with pytest.raises(ApiError, match='resource_not_found'):
        tool('foreign', 'aist_web_fetch', args, foreign)
    assert network_state(w, run) == baseline and (len(parallel.news), len(parallel.rpc)) == counts
    saved = save(w, run, deal_note(source))
    assert saved['version']['basis']['external_context']['items'] == [evidence]


@pytest.mark.parametrize('reply_urls', [
    [URL + '?changed=1'], [URL + '/changed'], ['https://other.example/business'],
    ['https://127.0.0.1/'], [' https://company.example/business'], [URL, URL + '#duplicate'],
])
def test_mismatched_provider_reply_is_accounted_without_evidence(workspace, local_parallel, reply_urls):
    w = workspace
    _, run = initialized(w)
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    local_parallel.fetch = [dict(url=url, excerpts=[TEXT]) for url in reply_urls]
    args = dict(search_id=found['search_id'], url=URL)
    fetched = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', args)
    assert fetched['result']['status'] == 'unavailable' and fetched['result']['evidence'] is None
    assert w.runs.tool(run['id'], run['epoch'], 'repeat', 'aist_web_fetch', args)['result'] == fetched['result']
    context, requests = network_state(w, run)
    assert not context['items'] and context['requests'] == 2
    assert context['bytes_read'] == sum(map(len, local_parallel.responses))
    assert len(requests) == len(local_parallel.rpc) == 2 and all(item['state'] == 'completed' for item in requests)


def test_historical_fetch_replay_ignores_legacy_invalid_url_without_granting_new_access(workspace, local_parallel):
    w = workspace
    _, run = initialized(w)
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    args = dict(search_id=found['search_id'], url=URL)
    original = w.runs.tool(run['id'], run['epoch'], 'original', 'aist_web_fetch', args)
    bad = 'https://64:ff9b::8.8.8.8/'
    bad_item = dict(found['results'][0], url=bad)

    def historical_search(c, current, executor_id, results):
        search_id = ident('tool')
        query = dict(QUERY, queries=[executor_id])
        result = dict(found, search_id=search_id, query=query, results=results)
        w.runs._record_tool(c, current, run['epoch'], executor_id, 'aist_web_search', query, None, now(),
                            result, None, reserved_id=search_id)
        return search_id

    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.web_requests SET url=:bad WHERE run_id=:r AND url IS NOT NULL', bad=bad, r=run['id'])
        current = w.runs.locked(c, run['id'])
        legacy_args = dict(args, search_id=historical_search(c, current, 'old-search', [bad_item]))
        legacy_reply = w.runs._record_tool(c, current, run['epoch'], 'historical', 'aist_web_fetch',
                                          legacy_args, None, now(), original['result'], None)
        mixed_id = historical_search(c, current, 'mixed-search', [bad_item, *found['results']])
    assert w.runs.tool(run['id'], run['epoch'], 'historical', 'aist_web_fetch', legacy_args) == legacy_reply
    assert w.runs.tool(run['id'], run['epoch'], 'alias', 'aist_web_fetch', legacy_args)['result'] == original['result']
    with pytest.raises(ApiError, match='resource_not_found'):
        w.runs.tool(run['id'], run['epoch'], 'new', 'aist_web_fetch', dict(legacy_args, url=URL+'#new'))
    assert len(local_parallel.rpc) == 2
    accepted = w.runs.tool(run['id'], run['epoch'], 'allowed', 'aist_web_fetch', dict(args, search_id=mixed_id))
    assert accepted['result']['status'] == 'collected' and len(local_parallel.rpc) == 3
    assert any(item['url'] == bad for item in network_state(w, run)[1])


def test_search_fetch_save_and_repeat_share_own_snapshot(workspace, monkeypatch):
    w = workspace
    source, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    search = w.runs.tool(run['id'], run['epoch'], 'search-1', 'aist_web_search', QUERY)
    validate_definition('WebSearchReply', search)
    assert not run['external_context']['items']
    args = dict(search_id=search['result']['search_id'], url=URL)
    fetched = w.runs.tool(run['id'], run['epoch'], 'fetch-1', 'aist_web_fetch', args)
    validate_definition('WebFetchReply', fetched)
    assert w.runs.tool(run['id'], run['epoch'], 'fetch-1', 'aist_web_fetch', args) == fetched
    repeated = w.runs.tool(run['id'], run['epoch'], 'fetch-new-id', 'aist_web_fetch', args)
    assert repeated['result'] == fetched['result'] and len(calls) == 2
    item = fetched['result']['evidence']
    content = deal_note(source)
    content['sections'][1]['blocks'].append(dict(kind='paragraph', block_id='web_context', claim=dict(
        text=TEXT, origin='external_context', external_evidence_ids=[item['evidence_id']], fact_refs=[], calculation_ids=[])))
    saved = save(w, run, content)
    context = saved['version']['basis']['external_context']
    assert context['items'] == [item] and context['requests'] == 2 and context['bytes_read'] == 300
    assert context['research_version'] == 'agent.v1'
    accept(w, saved)
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'new-search', 'aist_web_search', QUERY)


def test_search_id_and_url_are_scoped_to_the_run(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)['result']
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'other-url', 'aist_web_fetch', dict(search_id=found['search_id'], url='https://127.0.0.1/'))
    _, another = initialized(w)
    with pytest.raises(ApiError):
        w.runs.tool(another['id'], another['epoch'], 'other-run', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL))
    assert len(calls) == 1


def test_reservation_survives_uncertain_provider_and_is_not_repeated(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    def lost(*args):
        with w.engine.connect() as c:
            state = execute(c, 'SELECT state FROM aist.web_requests WHERE run_id=:r', r=run['id']).scalar_one()
            assert state == 'reserved'
        raise RuntimeError('worker lost before response')
    monkeypatch.setattr(note_external, 'web_request', lost)
    with pytest.raises(RuntimeError):
        w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    monkeypatch.setattr(note_external, 'web_request', lambda *args: pytest.fail('must not repeat I/O'))
    result = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    assert not result['ok'] and result['error']['code'] == 'outcome_unknown'
    with pytest.raises(ApiError) as error:
        w.runs.tool(run['id'], run['epoch'], 'different-search', 'aist_web_search', dict(QUERY, objective='Другая тема'))
    assert error.value.code == 'outcome_unknown'


def test_unavailable_search_is_not_no_results_and_keeps_draft_possible(workspace, monkeypatch):
    w = workspace
    source, run = initialized(w)
    def unavailable(*args):
        raise OSError('network unavailable')
    monkeypatch.setattr(note_external, 'web_request', unavailable)
    result = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    assert result['ok'] and result['result']['status'] == 'unavailable'
    saved = save(w, run, deal_note(source))
    assert not saved['version']['basis']['external_context']['items']


def test_stale_worker_does_not_publish_search_result(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    def lost_epoch(*args):
        with w.engine.begin() as c:
            execute(c, 'UPDATE aist.runs SET epoch=epoch+1 WHERE id=:r', r=run['id'])
        return reader([])(*args)
    monkeypatch.setattr(note_external, 'web_request', lost_epoch)
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.tool_calls WHERE run_id=:r', r=run['id']).scalar_one() == 0
        assert execute(c, 'SELECT state FROM aist.web_requests WHERE run_id=:r', r=run['id']).scalar_one() == 'reserved'


@pytest.mark.parametrize('change', ['duration', 'requests', 'bytes', 'tools'])
def test_budget_exhaustion_does_not_call_network(workspace, monkeypatch, change):
    from app.runs import json_value
    w = workspace
    if change == 'tools':
        w.runs.policy['run']['max_tool_calls'] = 1
    _, run = initialized(w)
    if change == 'tools':
        w.runs.tool(run['id'], run['epoch'], 'tool_existing', 'aist_get_facts', dict(refs=[]))
    with w.engine.begin() as c:
        if change == 'duration':
            monkeypatch.setattr(note_external, 'age_seconds', lambda run: 99999)
        elif change == 'tools':
            pass
        else:
            context = run['external_context']
            context['requests' if change == 'requests' else 'bytes_read'] = 18 if change == 'requests' else 8_000_000
            execute(c, 'UPDATE aist.runs SET external_context=CAST(:v AS jsonb) WHERE id=:r', r=run['id'], v=json_value(context))
    monkeypatch.setattr(note_external, 'web_request', lambda *args: pytest.fail('No budget for I/O'))
    with pytest.raises(ApiError) as error:
        w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    assert error.value.code == 'budget_exceeded'


@pytest.mark.parametrize('name,args', [('aist_read_source', {}), ('aist_read_task', {})])
def test_private_materials_close_web_before_delivery(workspace, monkeypatch, name, args):
    w = workspace
    source, run = initialized(w)
    if name == 'aist_read_source':
        args = dict(source_version_id=source['id'], page=1)
    result = w.runs.tool(run['id'], run['epoch'], 'tool_private', name, args, read_task=lambda run: 'Private task')
    assert result['ok']
    with w.engine.connect() as c:
        context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
    assert context['research_closed']
    monkeypatch.setattr(note_external, 'web_request', lambda *args: pytest.fail('Private data must not reach network'))
    with pytest.raises(ApiError) as error:
        w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    assert error.value.code == 'tool_unavailable'


def test_explicit_conflicting_inn_is_not_saved_as_company_evidence(workspace, monkeypatch):
    w = workspace
    monkeypatch.setattr(note_external, 'identity', lambda materials: dict(company='Орбита', inn='7701234567', sector='', domain=''))
    _, run = initialized(w)
    base = reader([])
    def wrong_company(name, *args):
        result = base(name, *args)
        if name == 'aist_web_fetch':
            result['quote'] = 'Орбита ИНН 7709999999. Компания производит оборудование и публикует результаты своей деятельности.'
        return result
    monkeypatch.setattr(note_external, 'web_request', wrong_company)
    found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', QUERY)
    result = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', dict(search_id=found['result']['search_id'], url=URL))
    assert result['result']['status'] == 'unavailable' and result['result']['evidence'] is None


def test_same_url_from_second_search_reuses_original_snapshot(workspace, monkeypatch):
    w = workspace
    _, run = initialized(w)
    calls = []
    monkeypatch.setattr(note_external, 'web_request', reader(calls))
    found = w.runs.tool(run['id'], run['epoch'], 'tool_search1', 'aist_web_search', QUERY)['result']
    fetched = w.runs.tool(run['id'], run['epoch'], 'tool_fetch1', 'aist_web_fetch', dict(search_id=found['search_id'], url=URL))
    another = w.runs.tool(run['id'], run['epoch'], 'tool_search2', 'aist_web_search', dict(QUERY, search_target='Другая цель'))['result']
    reused = w.runs.tool(run['id'], run['epoch'], 'tool_fetch2', 'aist_web_fetch', dict(search_id=another['search_id'], url=URL))
    assert reused['result'] == fetched['result']
    assert len(calls) == 3 and calls[-1][0] == 'aist_web_search'


@pytest.mark.parametrize('published,expected', [('2027-01-01','2027-01-01'), ('2027-01-02',None)])
def test_provider_fetch_uses_passed_cutoff_for_publication(published, expected, local_parallel):
    local_parallel.fetch[0]['publish_date'] = published
    evidence = note_external.web_request('aist_web_fetch', {'url': URL}, '2027-01-01', 5, 160000)
    assert evidence['published'] == expected
    assert evidence['provider_acquired_at'] == '2026-11-01T00:00:00Z'
    assert local_parallel.news[-1][1]['as_of'] == '2027-01-01'
