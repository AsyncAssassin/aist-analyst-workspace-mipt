"""Установленный OpenClaw: публичный поиск, закрытая задача, сохранение в одном Run."""
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import shutil
from tempfile import TemporaryDirectory
import threading

import pytest

from app import note_external, openclaw_runtime as runtime
from app.live_profile import bundle_for
from app.normalization import fingerprint
from app.runs import execute, json_value
from test_d15_postgres import workspace
from test_fable_postgres import native, start_fable, reply, audit, extracted, GOOD, QUAL, note, fact_rows
from test_flow01_postgres import executor
from test_perf02_r4_postgres import save_args, small_note
from test_web_research_postgres import local_parallel, network_state, reader, QUERY, TEXT, URL

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL и локальный runtime')


@pytest.mark.parametrize('agent_version', ['agent.v1', 'agent.v2'])
def test_installed_openclaw_authorizes_fetch_and_saves_retained_evidence(workspace, native, monkeypatch, local_parallel, agent_version):
    w = workspace
    source, _, run = start_fable(w, research=True)
    monkeypatch.setattr(note_external, 'identity', lambda materials: dict(company='Орбита', inn='', sector='', domain=''))
    note_external.prepare_all(w.runs, run)
    run['external_context']['research_version'] = agent_version
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.runs SET external_context=CAST(:value AS jsonb) WHERE id=:r',
                value=json_value(run['external_context']), r=run['id'])
    text = ('Синтетическая публикация A21. ' + TEXT + '\n') * 120
    text = text.strip()
    local_parallel.fetch[0]['excerpts'] = [text]
    query = dict(QUERY, objective='Синтетическое задание исследования A21', queries=['Search marker A21'])
    requested = 'https://COMPANY.EXAMPLE:443/business#section'

    def fetch(url, identifier):
        with w.engine.connect() as c:
            search_id = execute(c, "SELECT id FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_web_search'",
                                r=run['id']).scalar_one()
        return reply('aist_web_fetch', dict(search_id=search_id, url=url), identifier=identifier)

    def read():
        context, _ = network_state(w, run)
        evidence, = context['items']
        return reply('aist_read_web', dict(evidence_id=evidence['evidence_id'], offset=12000), identifier='toolu_part')

    def save_note():
        context, _ = network_state(w, run)
        evidence, = context['items']
        content = small_note(source, corrected=True)
        content['sections'][-1]['blocks'].append(dict(kind='paragraph', block_id='external_runtime', claim=dict(
            text=TEXT, origin='external_context', external_evidence_ids=[evidence['evidence_id']], fact_refs=[], calculation_ids=[])))
        args = dict(content=content, ownership_review=dict(status='not_established',
                    reason='Синтетический источник не раскрывает владельцев.', excluded_sources=[]))
        return reply('aist_save_artifact', args, identifier='toolu_save')

    discovery = [dict(body=reply('aist_web_search', query, identifier='toolu_search')),
                 dict(body=lambda: fetch('https://outside.example/linked', 'toolu_denied')),
                 dict(body=lambda: fetch(requested, 'toolu_fetch'))]
    if agent_version == 'agent.v1':
        open_task = reply('aist_read_task', {}, identifier='toolu_task')
        open_task['content'][-1]['input'] = {}
        native.queue = [*discovery, dict(body=open_task), dict(body=read), dict(body=save_note)]
    else:
        research = reply('aist_research', dict(company='Орбита', objective=query['objective']), identifier='toolu_research')
        summary = reply(stop='end_turn', identifier='toolu_summary')
        summary['content'] = [dict(type='text', text='Сохранена синтетическая выдержка A21.')]
        native.queue = [dict(body=research), *discovery, dict(body=read), dict(body=summary), dict(body=save_note)]
        gateway_request = runtime.Gateway.request
        def request(gateway, method, params):
            # The local native fixture verifies a prefix within one session.
            if method == 'agent' and params.get('agentId') == 'researcher':
                native.last_agent = None
            return gateway_request(gateway, method, params)
        monkeypatch.setattr(runtime.Gateway, 'request', request)
    with TemporaryDirectory(prefix='a21-openclaw-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0))
        provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1],
                                 provider_port=provider.getsockname()[1])
        gateway.close()
        provider.close()
        worker = executor(w)
        runtime.run(worker, run)
        state = w.runs.get(w.actor, w.project, run['id'])
        directory = Path(root) / run['id']
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        public_journal = journal
        if agent_version == 'agent.v2':
            research_directory, = Path(root).glob('research_*')
            public_journal = [json.loads(line) for line in (research_directory / 'tools.jsonl').read_text().splitlines()]
        context, requests = network_state(w, run)
        counters = dict(model_posts=len(native.requests), prefix_errors=native.prefix_failures, news_gets=len(local_parallel.news),
                        mcp_posts=len(local_parallel.rpc), ledger_requests=len(requests), state=state['state'])
        destination = Path('/evidence')
        if destination.is_dir():
            (destination / f'a21-openclaw-{agent_version}.json').write_text(json_value(dict(counters=counters,
                rpc=local_parallel.rpc, news=local_parallel.news, journal=journal, public_journal=public_journal,
                requests=requests, context=context)))
        assert state['state'] == 'completed', (state, counters)
        assert not native.prefix_failures and not native.queue
        if agent_version == 'agent.v1':
            assert len(native.requests) == 6
            assert [item['request']['name'] for item in journal] == [
                'aist_web_search', 'aist_web_fetch', 'aist_read_task', 'aist_read_web', 'aist_save_artifact']
            model_history = json_value(native.requests[-1]['messages'])
        else:
            assert len(native.requests) == 7
            assert [item['request']['name'] for item in journal] == ['aist_research', 'aist_save_artifact']
            assert [item['request']['name'] for item in public_journal] == ['aist_web_search', 'aist_web_fetch', 'aist_read_web']
            assert journal[0]['reply']['result']['research_id'] == research_directory.name
            model_history = json_value(native.requests[-2]['messages'])
        assert 'research_pending' not in context
        assert 'resource_not_found' in model_history
        session = fingerprint({'public_research': run['id']})
        assert [rpc['params']['name'] for rpc in local_parallel.rpc] == ['web_search', 'web_fetch']
        assert all(rpc['params']['arguments']['session_id'] == session for rpc in local_parallel.rpc)
        assert local_parallel.rpc[-1]['params']['arguments']['urls'] == [URL]
        assert len(local_parallel.news) == len(requests) == 2
        found = public_journal[0]['reply']['result']
        assert public_journal[1]['request']['args'] == dict(search_id=found['search_id'], url=requested)
        reading = next(item for item in public_journal if item['request']['name'] == 'aist_read_web')
        assert reading['reply']['result']['text'] == text[12000:]
        evidence, = context['items']
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        assert saved['version']['basis']['external_context']['items'] == [evidence]
        assert evidence['quote'] == text
        call = next(entry for entry in requests if entry['url'])
        assert call['url'] == URL and call['arguments_sha256'] == fingerprint(
            {'name': 'aist_web_fetch', 'args': public_journal[1]['request']['args']})
        assert list((directory / 'calls').glob('*-request.json'))
        assert list((directory / 'calls').glob('*-response.sse'))


@pytest.mark.parametrize("full", [False, True])
def test_installed_research_then_private_composition(workspace, native, monkeypatch, full):
    w = workspace
    source, facts, run = start_fable(w, research=True, full=full)
    monkeypatch.setattr(note_external, 'identity', lambda materials: dict(company='Орбита', inn='', sector='', domain=''))
    note_external.prepare_all(w.runs, run)
    # Совместимость уже начатого публичного этапа agent.v1.
    run['external_context']['research_version'] = 'agent.v1'
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.runs SET external_context=CAST(:value AS jsonb) WHERE id=:r',
                value=json_value(run['external_context']), r=run['id'])
    requests = []
    monkeypatch.setattr(note_external, 'web_request', reader(requests))

    def fetch():
        with w.engine.connect() as c:
            search_id = execute(c, "SELECT id FROM aist.tool_calls WHERE run_id=:r AND data->>'name'='aist_web_search'", r=run['id']).scalar_one()
        return reply('aist_web_fetch', dict(search_id=search_id, url=URL), identifier='toolu_read')

    def save_note():
        with w.engine.connect() as c:
            context = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        assert context['research_closed'] and len(context['items']) == 1
        content = note(next(item['data'] for item in fact_rows(w) if item['data']['amount'] is None)) if full else small_note(source)
        item = context['items'][0]
        content['sections'][-1]['blocks'].append(dict(kind='paragraph', block_id='external_runtime', claim=dict(
            text=item['quote'], origin='external_context', external_evidence_ids=[item['evidence_id']], fact_refs=[], calculation_ids=[])))
        ownership_review = save_args(content)['ownership_review']
        return reply('aist_save_artifact', dict(content=content, ownership_review=ownership_review),
                     identifier='toolu_save')

    open_task = reply('aist_read_task', {'refs': []}, identifier='toolu_task')
    open_task['content'][-1]['input'] = {}
    native.queue = [dict(body=reply('aist_web_search', QUERY, identifier='toolu_search')),
                    dict(body=fetch), dict(body=open_task)]
    if full:
        native.queue.extend([dict(body=reply(args={'question': 'Финансовые результаты и бизнес'}, identifier='toolu_extract')), dict(body=extracted([GOOD, QUAL]))])
    native.queue.append(dict(body=save_note))
    with TemporaryDirectory(prefix='research-runtime-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0)); provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close(); provider.close()
        worker = executor(w)
        timer = threading.Timer(110, worker.draining.set)
        timer.start()
        directory = Path(root) / run['id']
        destination = Path('/evidence') / (os.getenv('AIST_RESEARCH_EVIDENCE', 'runtime-research-current') + ('-full' if full else '-ready'))
        destination.mkdir(exist_ok=True)
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel()
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
            shutil.copytree(directory / 'calls', destination / 'calls', dirs_exist_ok=True)
            (destination / 'audit.json').write_text(json_value(audit(w, run)))
        state = w.runs.get(w.actor, w.project, run['id'])
        assert state['state'] == 'completed', state
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        assert [item['request']['name'] for item in journal] == ['aist_web_search', 'aist_web_fetch', 'aist_read_task'] + (['aist_get_facts'] if full else []) + ['aist_save_artifact']
        assert all(item['reply']['ok'] for item in journal)
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
    assert len(native.requests) == (6 if full else 4) and native.prefix_failures == [] and not native.queue
    assert len(requests) == 2
    # Первые три модельных запроса содержат только публичный этап, включая полученные страницы.
    for request in native.requests[:3]:
        text = json_value(request['messages'])
        assert 'Подготовь справку по сохраненным фактам' not in text
        assert all(fact not in text for fact in facts)
        assert source['id'] not in text
    final = json_value(native.requests[-1]['messages'])
    assert 'Подготовь справку по сохраненным фактам' in final
    assert all(fact in final for fact in facts)
    delivered_task = next(block for message in native.requests[-1]['messages'] if isinstance(message['content'], list)
        for block in message['content'] if block.get('type') == 'tool_result' and block.get('tool_use_id') == 'toolu_task')
    task_message = json.loads(delivered_task['content'])['result']['message']
    skill = bundle_for('skill_analytical-note-v3', 'analytical_note').read_text()
    assert skill in native.requests[0]['messages'][0]['content']
    assert skill not in task_message
    assert json.loads(task_message)['task'] == 'Подготовь справку по сохраненным фактам'
    assert saved['version']['basis']['external_context']['research_closed']
    assert saved['version']['basis']['ownership_review']['status'] == 'not_established'
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == (6 if full else 4)
    assert saved['review']['acceptance'] is None
    (destination / 'result.json').write_text(json_value(dict(state=state, saved=saved, calls=calls, usage=usage, public_requests=requests)))
