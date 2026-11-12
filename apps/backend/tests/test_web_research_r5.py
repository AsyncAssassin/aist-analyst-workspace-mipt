"""Границы вопроса провайдеру, новостей и ожидания RPC."""
from copy import deepcopy
import os
import json
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import external_sources, note_external, openclaw_runtime as runtime
from app.errors import ApiError
from app.contracts import validate_definition


def test_prepared_astra_research_keeps_five_composition_calls_only_after_all_topics_finish():
    from app.live_profile import OPENAI_ID, FABLE_ID
    first = dict(source_version_id='sv_first', sha256='a'*64)
    second = dict(source_version_id='sv_second', sha256='b'*64)
    topics = [dict(source=pin, question='Финансы и бизнес') for pin in (first, second)]
    entries = [dict(deepcopy(topic), call_id='call_'+str(i), status='partial') for i, topic in enumerate(topics)]
    run = dict(input=dict(model_profile_version_id=OPENAI_ID),
               checkpoint=dict(collection=dict(prepared_topics=topics), initial_extractions=entries))
    original = deepcopy(run)
    assert note_external.research_reserve(run) == 5
    for change in ('missing', 'error', 'pending', 'wrong_sha', 'no_plan'):
        altered = deepcopy(run)
        if change == 'missing':altered['checkpoint']['initial_extractions'].pop()
        elif change == 'error':altered['checkpoint']['initial_extractions'][1]['error'] = dict(code='outcome_unknown')
        elif change == 'pending':altered['checkpoint']['initial_extractions'][1].pop('status')
        elif change == 'wrong_sha':altered['checkpoint']['initial_extractions'][1]['source']['sha256'] = 'c'*64
        else:altered['checkpoint']['collection']['prepared_topics'] = []
        assert note_external.research_reserve(altered) == 7
    assert note_external.research_reserve(dict(run, input=dict(model_profile_version_id=FABLE_ID))) == 3
    assert run == original


def test_research_readings_preserve_text_boundaries_and_omit_previous_materials():
    items = [dict(evidence_id='ext_previous', quote='Прежнее основание')]
    items += [dict(evidence_id='ext_source'+str(i), quote=('Материал '+str(i)+' ') * 3000)
              for i in range(6)]
    items.insert(2, dict(evidence_id='ext_short', quote='Краткое новое основание'))
    run = dict(external_context=dict(items=items))
    original = deepcopy(run)
    readings = note_external.research_readings(run, {'ext_previous'})
    assert sum(len(reading['text']) for reading in readings) == 60_000
    assert {reading['evidence_id'] for reading in readings} == {item['evidence_id'] for item in items[1:7]}
    assert readings[1]['next_offset'] is None
    assert len(readings[-1]['text']) < 12000
    for reading in readings:
        validate_definition('ReadWebResult', reading)
        source = next(item['quote'] for item in items if item['evidence_id'] == reading['evidence_id'])
        assert reading['offset'] == 0 and reading['text_length'] == len(source)
        assert reading['text'] == source[:len(reading['text'])]
        if reading['next_offset'] is not None:
            tail = note_external.read_web(run, dict(evidence_id=reading['evidence_id'], offset=reading['next_offset']))
            assert tail['text'] == source[reading['next_offset']:reading['next_offset']+12000]
    assert run == original
    assert note_external.research_readings(run, {item['evidence_id'] for item in items}) == []


def test_full_objective_reaches_reader_and_provider(monkeypatch):
    objective = 'Публичные сведения. ' * 15 + 'Денежные потоки за 2025 год'
    received = []
    monkeypatch.setattr(note_external, 'internal_json', lambda url, *args: received.append(parse_qs(urlsplit(url).query)))
    note_external.web_request('aist_web_fetch', dict(url='https://company.example/report'), '2026-11-08', 5, 160000,
                              objective=objective)
    assert received[0]['objective'] == [objective]
    def provider(name, args, *rest):
        received.append(args)
        return dict(results=[]), dict(bytes_read=100)
    monkeypatch.setattr(external_sources, 'provider_result', provider)
    external_sources.read_page('https://company.example/report', '2026-11-08', time.monotonic()+5, 160000,
                               objective=objective)
    assert received[1]['objective'] == objective


def test_readings_deliver_whole_available_material_after_source_breadth():
    items = [dict(evidence_id='ext_'+str(i), quote=('Текст '+str(i)+' ') * size)
             for i, size in enumerate((2000, 200, 300, 400, 500, 1800))]
    run = dict(external_context=dict(items=items))
    original = deepcopy(run)
    readings = note_external.research_readings(run, set())
    assert [part['evidence_id'] for part in readings[:6]] == [item['evidence_id'] for item in items]
    for item in items:
        parts = [part for part in readings if part['evidence_id'] == item['evidence_id']]
        assert ''.join(part['text'] for part in parts) == item['quote']
        assert parts[-1]['next_offset'] is None
        offset = 0
        for part in parts:
            validate_definition('ReadWebResult', part)
            assert part['offset'] == offset
            offset += len(part['text'])
            assert part['text_length'] == len(item['quote'])
    assert run == original


def test_readings_stop_at_existing_count_and_text_limits_with_resumable_offsets():
    items = [dict(evidence_id='ext_long', quote='я' * 80000)]
    items += [dict(evidence_id='ext_'+str(i), quote='Короткий источник') for i in range(23)]
    run = dict(external_context=dict(items=items))
    readings = note_external.research_readings(run, set())
    assert len(readings) == 24
    assert readings[0]['next_offset'] == 12000
    assert readings[-1]['evidence_id'] == 'ext_22'
    single = dict(external_context=dict(items=items[:1]))
    readings = note_external.research_readings(single, set())
    assert sum(len(part['text']) for part in readings) == 60000
    assert readings[-1]['next_offset'] == 60000
    tail = note_external.read_web(single, dict(evidence_id='ext_long', offset=readings[-1]['next_offset']))
    assert tail['text'] == items[0]['quote'][60000:72000]


def test_multiple_document_extractions_do_not_create_news_events():
    context = external_sources.snapshot('Орбита', '2026-11-08')
    context['news_search'] = dict(succeeded=20, failed=0, evidence_ids=['ext_first', 'ext_second'])
    item = dict(evidence_id='ext_first', url='https://company.example/news', publisher='company.example',
                title='Новая публикация', published='2026-01-05', retrieved_at='2026-11-08T00:00:00Z',
                page_sha256='a'*64, quote='Публичные сведения о компании, ее рынке и опубликованных результатах.',
                topics=['business'], search_target='Орбита', search_role='company', source_kind='publication')
    context['items'] = [item, dict(item, evidence_id='ext_second', url=item['url']+'#part')]
    original = deepcopy(context)
    validate_definition('ExternalContext', context)
    news = note_external.news_from_context(context)
    assert news['status'] == 'empty' and news['items'] == []
    assert context == original


@pytest.mark.parametrize('reason', ['budget_exceeded', 'run_terminal', 'outcome_unknown'])
def test_rpc_wait_checks_run_and_reaps_child(tmp_path, monkeypatch, reason):
    children = []
    original_popen = runtime.subprocess.Popen
    def launch(args, **kwargs):
        child = original_popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch)
    started = time.monotonic()
    def remaining():
        if time.monotonic() - started > .25:
            raise ApiError(reason)
        return 5
    gateway = runtime.Gateway(SimpleNamespace(gateway_port=12345), tmp_path, dict(os.environ), remaining)
    with pytest.raises(ApiError) as failure:
        gateway.request('agent', dict(idempotencyKey='run_test'))
    assert failure.value.code == reason
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None


def test_rpc_sends_whole_large_body_when_child_starts_slowly(tmp_path, monkeypatch):
    original_popen = runtime.subprocess.Popen
    script = 'import time,json,sys; time.sleep(.5); value=json.load(sys.stdin); print(json.dumps(value["params"]))'
    monkeypatch.setattr(runtime.subprocess, 'Popen',
                        lambda args, **kw: original_popen([sys.executable, '-c', script], **kw))
    gateway = runtime.Gateway(SimpleNamespace(gateway_port=12345), tmp_path, dict(os.environ), lambda: 5)
    params = dict(message='Контекст записки ' * 10000)
    assert gateway.request('agent', params) == params
    assert json.loads(next(tmp_path.glob('agent-*.json')).read_text())['returncode'] == 0


@pytest.mark.parametrize('code', ['run_terminal', 'budget_exceeded'])
def test_optional_status_poll_allows_completed_run_but_not_expired_deadline(code):
    def request(*args):
        raise ApiError(code)
    gateway = SimpleNamespace(request=request)
    if code == 'run_terminal':
        assert runtime.session_status(gateway, 'session_saved') is None
    else:
        with pytest.raises(ApiError) as failure:
            runtime.session_status(gateway, 'session_expired')
        assert failure.value.code == code
