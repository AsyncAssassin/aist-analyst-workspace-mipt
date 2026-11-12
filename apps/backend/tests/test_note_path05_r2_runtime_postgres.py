"""Два вопроса первой записки; ответы и заданные длительности только тестовые."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading

import pytest
from test_perf02_r4_postgres import model_save_args

from app import collection, openclaw_runtime as runtime, runs as run_clock
from app.live_profile import FABLE, bundle_for
from app.runs import execute, json_value
from test_d15_postgres import workspace
from test_draft01_postgres import http
from test_fable_postgres import THEMATIC_LINES, THEMATIC_FACTS, audit, extracted, native, reply, start_fable
from test_flow01_02_postgres import QUAL
from test_flow01_05_postgres import ask
from test_flow01_postgres import current, executor
from test_perf02_r1_postgres import upload, fact_rows
from test_perf02_r4_postgres import paragraph, ref, table_row

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')
QUESTIONS = ['Существенные финансы и операционные показатели с сопоставимыми периодами',
             'Бизнес, продукты, рынок, стратегия, дивиденды, финансирование и существенные риски или оговорки']
LINES = THEMATIC_LINES + ['Оговорка компании', 'Планы зависят от спроса и не гарантируют результат.']
QUALITATIVE = THEMATIC_FACTS[1] + THEMATIC_FACTS[2] + [
    dict(QUAL, evidence=[dict(page=1, line_start=13, line_end=14, area='Оговорка компании')])]


def test_installed_native_runtime_two_groups_reach_one_draft(workspace, native):
    w = workspace
    source, _, run = start_fable(w, full=True, source=upload(w, 'two-groups-test', LINES))
    first = reply(args={'question': QUESTIONS[0]}, identifier='toolu_finance')
    first['content'].append(dict(type='tool_use', id='toolu_qualitative', name='aist_get_facts', input={'question': QUESTIONS[1]}))

    def save_note():
        facts = [f['data'] for f in fact_rows(w)]
        assert len(facts) == 5
        financial = sorted((f for f in facts if f['amount']), key=lambda f: f['period']['start'])
        business = next(f for f in facts if 'поставляет' in f['raw_text'])
        strategy = next(f for f in facts if 'расширяет' in f['raw_text'])
        qualification = next(f for f in facts if 'не гарантируют' in f['raw_text'])
        content = dict(title='Тестовая информационная справка', audience='Аналитик', sections=[
            dict(section_id='sec_business', title='Бизнес', blocks=[paragraph('block_business',
                'Компания поставляет оборудование клиентам.', [ref(source, business['fact_id'])])]),
            dict(section_id='sec_finance', title='Финансы', blocks=[dict(block_id='block_revenue', kind='table',
                caption='Выручка по МСФО, млн руб.', rows=[table_row('row_' + f['period']['start'][:4], 'Выручка',
                    f['period']['label'], f['amount']['value'], [ref(source, f['fact_id'])]) for f in financial])]),
            dict(section_id='sec_strategy', title='Стратегия и оговорка', blocks=[
                paragraph('block_strategy', 'Компания расширяет сеть продаж.', [ref(source, strategy['fact_id'])]),
                paragraph('block_qualification', 'Планы зависят от спроса и не гарантируют результат.',
                          [ref(source, qualification['fact_id'])])]),
        ])
        return reply('aist_save_artifact', model_save_args(content), identifier='toolu_two_groups_save')

    native.queue = [dict(body=first), dict(body=extracted(THEMATIC_FACTS[0])),
                    dict(body=extracted(QUALITATIVE)), dict(body=save_note)]
    destination = Path('/result/runtime-two-groups')
    destination.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='fable-two-groups-', dir='/data/execution') as root, socket.socket() as gateway, socket.socket() as provider:
        gateway.bind(('127.0.0.1', 0))
        provider.bind(('127.0.0.1', 0))
        w.runs.settings = replace(w.settings, openclaw_root=root, gateway_port=gateway.getsockname()[1], provider_port=provider.getsockname()[1])
        gateway.close()
        provider.close()
        worker = executor(w)
        timer = threading.Timer(100, worker.draining.set)
        timer.start()
        directory = Path(root) / run['id']
        try:
            runtime.run(worker, run)
        finally:
            timer.cancel()
            for filename in ('tools.jsonl', 'gateway-process.log', 'failure.json'):
                if (directory / filename).exists():
                    (destination / filename).write_bytes((directory / filename).read_bytes())
            (destination / 'requests.json').write_text(json_value(native.requests))
        state = w.runs.get(w.actor, w.project, run['id'])
        assert state['state'] == 'completed', state
        journal = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()]
        assert [e['request']['name'] for e in journal] == ['aist_get_facts', 'aist_get_facts', 'aist_save_artifact']
        assert [e['request']['args']['question'] for e in journal[:2]] == QUESTIONS
        assert all(e['reply']['ok'] for e in journal)
        assert all(e['reply']['result']['coverage']['full_document'] for e in journal[:2])
        saved = w.runs.version(w.actor, w.project, state['result_version_id'])
        entry = journal[-1]
        args = runtime.save_operation(run, entry['request']['args'], None)
        assert w.runs.tool(run['id'], run['epoch'], entry['request']['tool_call_id'], 'aist_save_artifact', args, entry['model_call_id']) == entry['reply']
        again = w.runs.tool(run['id'], run['epoch'], 'toolu_save_again', 'aist_save_artifact', args)
        assert again['ok'] and again['result']['version']['id'] == state['result_version_id']
    assert len(native.requests) == 4 and not native.queue and native.prefix_failures == []
    assert native.paths == ['/v1/messages'] * 4
    composer = native.requests[3]
    delivered = '\n'.join(block['text'] for block in composer['system'])
    for message in composer['messages']:
        content = message['content']
        delivered += '\n' + (content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content))
    instruction = bundle_for('skill_analytical-note-v3', 'analytical_note').read_text()
    assert instruction in delivered
    assert 'запроси их двумя aist_get_facts' in instruction
    assert 'финансовый первым' in instruction
    assert 'Для стратегии и финансирования запроси' not in instruction
    results = [json.loads(b['content']) for message in composer['messages'] if isinstance(message['content'], list)
               for b in message['content'] if b.get('type') == 'tool_result']
    assert results == [runtime.model_view(e['reply']) for e in journal[:2]]
    facts = [f for result in results for f in result['result']['facts']]
    finance = [f for f in facts if f['amount']]
    assert {(f['amount']['value'], f['amount']['scale'], f['period']['start'], f['period']['end']) for f in finance} == {
        ('80', '1000000', '2022-01-01', '2022-03-31'), ('100', '1000000', '2023-01-01', '2023-03-31')}
    sections = saved['version']['payload']['content']['sections']
    rows = sections[1]['blocks'][0]['rows']
    assert {r['fact_id'] for row in rows for r in row['fact_refs']} == {f['fact_id'] for f in finance}
    for block, metric, phrase in zip(sections[2]['blocks'], ('Стратегия компании', 'Оговорка компании'),
                                     ('Компания расширяет сеть продаж.', 'Планы зависят от спроса и не гарантируют результат.')):
        assert phrase in json_value(composer)
        assert block['claim']['fact_refs'] == [ref(source, next(f['fact_id'] for f in facts if f['metric'] == metric))]
    assert saved['review']['acceptance'] is None and saved['checks'][0]['status'] == 'passed'
    w.settings = replace(w.settings, live_models_enabled=False, executor_mode='disabled')
    with http(w) as client:
        response = client.get(f'/v1/projects/{w.project}/artifact-versions/{state["result_version_id"]}')
        assert response.status_code == 200 and response.json() == saved
    calls, usage = audit(w, run)
    assert len(calls) == len(usage) == 4
    assert [c['purpose'] for c in calls] == ['agent', 'extraction', 'extraction', 'agent']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls').scalar_one() == 0
    (destination / 'result.json').write_text(json_value(dict(state=state, saved=saved, calls=calls, usage=usage,
        posts=4, external_posts=0, synthetic_responses=True, reload_http=response.status_code, database=w.settings.db_name)))


@pytest.mark.parametrize('financial_seconds,second_allowed', [(130, True), (511, False)])
def test_two_group_timing_and_exact_replay_before_completion(workspace, native, monkeypatch, tmp_path, financial_seconds, second_allowed):
    w = workspace
    source, _, run = start_fable(w, full=True, source=upload(w, 'two-groups-clock-test', LINES))
    worker = executor(w)
    elapsed = [20]
    monkeypatch.setattr(run_clock, 'utcnow', lambda: run['created_at'] + timedelta(seconds=elapsed[0]))
    native.queue = [dict(body=reply()), dict(body=extracted(THEMATIC_FACTS[0]))]
    runtime.Boundary(worker, run, tmp_path, 'fixture').model(dict(model=FABLE['model'], messages=[]))
    elapsed[0] += 10
    first = ask(w, worker, current(w, run), source, QUESTIONS[0], tmp_path)
    assert first['ok'] and first['result']['status'] == 'complete'
    elapsed[0] += financial_seconds
    if second_allowed:
        native.queue.append(dict(body=extracted(QUALITATIVE)))
    second = ask(w, worker, current(w, run), source, QUESTIONS[1], tmp_path)
    assert second['ok'] == second_allowed
    assert len(native.requests) == (3 if second_allowed else 2)
    if second_allowed:
        assert second['result']['status'] == 'complete'
        elapsed[0] += 142
    else:
        assert collection.state(current(w, run))['reason'] == 'reserve_time'
    before = deepcopy(fact_rows(w))
    checkpoint = deepcopy(current(w, run)['checkpoint'])
    for question, outcome in zip(QUESTIONS, (first, second)):
        if not outcome['ok']:
            continue  # Отказ до отправки не создал результата извлечения для повтора.
        again = ask(w, executor(w), current(w, run), source, question, tmp_path / 'repeat')
        assert again['ok'] and again['result'] == outcome['result']
    assert fact_rows(w) == before and current(w, run)['checkpoint'] == checkpoint
    assert len(native.requests) == (3 if second_allowed else 2)
    assert current(w, run)['state'] == 'running'
    expected_window = 598 if second_allowed else 359
    assert worker.transport.send_seconds(current(w, run)) == expected_window
    native.queue.append(dict(body=reply()))
    runtime.Boundary(worker, current(w, run), tmp_path, 'fixture').model(dict(model=FABLE['model'],
        messages=[dict(role='user', content='Составь черновик из полученных оснований.')]))
    assert len(native.requests) == (4 if second_allowed else 3) and not native.queue
    calls, _ = audit(w, run)
    assert all(c['provider_outcome'] == 'success' for c in calls)
    assert [c['purpose'] for c in calls] == ['agent', 'extraction'] + (['extraction'] if second_allowed else []) + ['agent']
    (Path('/result') / f'clock-financial-{financial_seconds}.json').write_text(json_value(dict(
        synthetic_durations=True, startup_seconds=20, first_agent_seconds=10, financial_seconds=financial_seconds,
        qualitative_seconds=142 if second_allowed else None, second_allowed=second_allowed, elapsed_seconds=elapsed[0],
        completion_window=expected_window, exact_repeat_new_posts=0, exact_repeat_new_facts=0,
        posts=len(native.requests), external_posts=0, calls=calls, collection=collection.state(current(w, run)))))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
