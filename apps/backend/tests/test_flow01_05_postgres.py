"""FLOW-01.05 through accounted loopback transport in the isolated workspace database."""
from copy import deepcopy
from dataclasses import replace
import json
import os

import pytest

from app import extraction_requests, transport
from app.accounting import Accounting
from app.errors import ApiError
from app.live_profile import PROFILE
from app.openclaw_runtime import obtain_facts, execution_facts
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace, invalidate
from test_chat01_r1_postgres import provider
from test_flow01_02_postgres import MIXED, QUAL, TRLN, context, note, coverage_findings
from test_flow01_postgres import current, counts, executor, extraction
from test_perf02_r1_postgres import fact_rows, upload
from test_perf02_r3_postgres import stub, refusal, record_pauses
from test_perf02_r4_postgres import save_args, start_note_run
from test_chat01_postgres import body, conversation, send

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def ask(w, worker, run, source, question, directory):
    args = dict(refs=[], extract=dict(source_version_id=source['id'], question=question))
    return w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', args,
        obtain_facts=lambda r: obtain_facts(worker, r, args['extract'], directory, None))


@pytest.mark.parametrize('items,status', [([QUAL], 'complete'), ([QUAL, TRLN], 'partial'), ([], 'empty'),
                                         ([dict(QUAL, evidence=[dict(page=999,line_start=1,line_end=2,area='Invalid')])], 'refused')])
def test_all_known_outcomes_replay_without_post_and_allow_another_bounded_question(workspace, provider, tmp_path, items, status):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED)
    p.facts = deepcopy(items)
    first = ask(w, p.worker, run, source, 'Полный анализ', tmp_path)
    assert first['ok'] == (status != 'refused'), first
    if first['ok']:
        assert first['result']['status'] == status and first['result']['coverage']['full_document']
    baseline = counts(w, run)
    saved_facts = fact_rows(w)
    checkpoint = current(w, run)['checkpoint']
    # A new executor id and a reconstructed service use the durable operation, not a file claim.
    worker = executor(w)
    again = ask(w, worker, current(w, run), source, 'Полный анализ', tmp_path / 'restored')
    assert again['ok'] == first['ok']
    if first['ok']:
        assert again['result'] == first['result']
    else:
        assert again['error']['message'] == first['error']['message']
    assert len(p.sent) == 1 and counts(w, run) == baseline
    assert fact_rows(w) == saved_facts and current(w, run)['checkpoint'] == checkpoint
    p.facts = [QUAL]
    second = ask(w, worker, current(w, run), source, 'Другой составной вопрос', tmp_path / 'changed')
    assert second['ok'] and second['result']['coverage']['full_document']
    assert len(p.sent) == 2 and counts(w, run) == ((2, 0), (2, 0))
    ops = extraction_requests.operations(current(w, run))
    assert len(ops) == 2 and all(op['body_sha256'] for op in ops.values())
    if status == 'refused':
        w.runs.stop(run['id'], run['epoch'], 'validation_failed')
        fresh = start_note_run(w, source)
        p.facts = [QUAL]
        assert ask(w, p.worker, fresh, source, 'Полный анализ', tmp_path / 'new-run')['ok']
        assert len(p.sent) == 3
    if status in ('complete', 'partial'):
        saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(first['result']['facts'][0])))
        assert saved['ok']
        assert bool(coverage_findings(saved['result'])) == (status == 'partial')


def test_a_new_targeted_question_on_already_seen_pages_is_allowed(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED, following=[(['Logistics', 'Warehouse'], None)])
    p.facts = [QUAL]
    first = ask(w, p.worker, run, source, 'Все данные', tmp_path)
    assert first['ok'] and first['result']['coverage']['full_document']
    p.facts = []
    targeted = ask(w, p.worker, run, source, 'Выручка', tmp_path)
    assert targeted['ok'] and targeted['result']['coverage']['pages'] == [1]
    assert len(p.sent) == 2 and counts(w, run)[1] == (2, 0)
    assert [p['page'] for p in json.loads(p.sent[1]['messages'][-1]['content'])['pages']] == [1]


def test_initial_extraction_also_blocks_a_second_full_document(workspace, provider, tmp_path):
    w, p = workspace, provider
    source = upload(w, 'initial-extraction', MIXED)
    request = body(sources=[source['id']], question='Полная справка')
    request.update(skill_version_id='skill_analytical-note-v1', requested_result_type='analytical_note')
    send(w, conversation(w), request)
    run = w.runs.claim('initial-extraction')
    p.facts = [QUAL]
    facts, call = execution_facts(p.worker, run, 'Полная справка', tmp_path)
    assert facts and call
    second = ask(w, p.worker, run, source, 'Другой вопрос', tmp_path)
    assert not second['ok'] and 'Полный PDF уже' in second['error']['message'] and len(p.sent) == 1


def test_known_receipt_reingests_after_interruption_without_a_post(workspace, provider, tmp_path, monkeypatch):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED)
    p.facts = [QUAL]
    original = __import__('app.openclaw_runtime', fromlist=['ingest']).ingest
    monkeypatch.setattr('app.openclaw_runtime.ingest', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('interrupted before ingest')))
    with pytest.raises(RuntimeError):
        obtain_facts(p.worker, run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    assert len(p.sent) == 1
    monkeypatch.setattr('app.openclaw_runtime.ingest', original)
    restored = obtain_facts(executor(w), current(w, run), dict(source_version_id=source['id'], question='Все данные'), tmp_path / 'restored', None)
    assert restored['status'] == 'complete' and len(p.sent) == 1


@pytest.mark.parametrize('change', ['cancel', 'revoke', 'epoch'])
def test_saved_result_still_requires_current_authority(workspace, provider, tmp_path, change):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED)
    p.facts = []
    assert ask(w, p.worker, run, source, 'Все данные', tmp_path)['ok']
    invalidate(w, run, change)
    with pytest.raises(ApiError):
        obtain_facts(p.worker, run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    assert len(p.sent) == 1


def test_oversized_context_is_recorded_without_truncation_or_post(workspace, provider, tmp_path, monkeypatch):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED)
    monkeypatch.setitem(PROFILE, 'input_bytes', 1)
    result = ask(w, p.worker, run, source, 'Нет совпадений', tmp_path)
    assert not result['ok'] and 'Тело не обрезано' in result['error']['message']
    assert p.sent == [] and counts(w, run) == ((0, 0), (0, 0))
    assert current(w, run)['checkpoint']['extractions'][0]['limitation']



def test_429_reuses_identical_body_and_operation(workspace, stub, monkeypatch, tmp_path):
    w = workspace
    source, run, _, _ = context(w, MIXED)
    worker = executor(w)
    pauses = record_pauses(monkeypatch)
    stub.queue.extend([refusal(), extraction([QUAL])])
    result = ask(w, worker, run, source, 'Все данные', tmp_path)
    assert result['ok'] and pauses == [10.0]
    assert len(stub.received) == 2 and stub.received[0] == stub.received[1]
    assert counts(w, run) == ((1, 1), (1, 1))
    op = next(iter(extraction_requests.operations(current(w, run)).values()))
    assert len(op['call_ids']) == 2
    assert ask(w, worker, run, source, 'Все данные', tmp_path / 'replay')['ok']
    assert len(stub.received) == 2


def test_unknown_outcome_cannot_be_bypassed_by_question_or_recovery(workspace, stub, tmp_path, monkeypatch):
    w = workspace
    source, run, _, _ = context(w, MIXED)
    worker = executor(w)
    stub.queue.append(dict(behavior='close'))
    with pytest.raises(ApiError):
        obtain_facts(worker, run, dict(source_version_id=source['id'], question='Все данные'), tmp_path, None)
    before = counts(w, run)
    assert current(w, run)['state'] == 'recovery_required'
    monkeypatch.setattr('app.extraction_context.VERSION', 'pdf-context.future')
    w.runs.recover(owned=run['worker_id'])
    for question in ('Все данные', 'Выручка'):
        with pytest.raises(ApiError):
            obtain_facts(executor(w), current(w, run), dict(source_version_id=source['id'], question=question), tmp_path, None)
    assert counts(w, run) == before and len(stub.received) == 1


def test_reasoning_is_pinned_only_for_new_runs(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, old, _, _ = context(w, MIXED)
    assert old['configuration']['extraction_thinking'] == 'enabled'
    w.runs.settings = replace(w.settings, extraction_thinking='disabled')
    p.facts = []
    assert ask(w, p.worker, old, source, 'Все данные', tmp_path / 'old')['ok']
    w.runs.stop(old['id'], old['epoch'], 'validation_failed')
    fresh = start_note_run(w, source)
    assert fresh['configuration']['extraction_thinking'] == 'disabled'
    assert ask(w, p.worker, fresh, source, 'Все данные', tmp_path / 'new')['ok']
    assert [(r['thinking']['type'], r['enable_thinking']) for r in p.sent] == [('enabled', True), ('disabled', False)]
    assert transport.model_request(fresh, 'agent', {})['thinking'] == {'type': 'disabled'}


def test_continuation_nonadjacent_footnote_and_ingest_keep_the_whole_canonical_source(workspace, provider, tmp_path, monkeypatch):
    from test_flow01_02_postgres import GOOD
    from app import openclaw_runtime
    w, p = workspace, provider
    source, run, pages, _ = context(w, MIXED, following=[
        (['Продолжение таблицы', 'Выручка', 'см. стр. 4'], None),
        (['Logistics'], None), (['Примечание 7', 'Данные за 2023 год не аудированы.'], None), (['Contacts'], None)])
    canonical = openclaw_runtime.ingest
    ingested = []

    def capture(*args, **kwargs):
        ingested.append(args[2])
        return canonical(*args, **kwargs)

    monkeypatch.setattr(openclaw_runtime, 'ingest', capture)
    p.facts = [QUAL, GOOD]
    result = ask(w, p.worker, run, source, 'Выручка', tmp_path)
    assert result['ok'] and result['result']['status'] == 'complete'
    assert any(f['amount'] and f['amount']['value'] == '100' for f in result['result']['facts'])
    assert any(f['amount'] is None for f in result['result']['facts'])
    assert ingested == [pages] and sorted(pages) == [1, 2, 3, 4, 5]
    data = json.loads(p.sent[0]['messages'][-1]['content'])
    assert [p['page'] for p in data['pages']] == [1, 2, 4]
    for page in data['pages']:
        assert page['lines'] == [dict(line=i+1, text=t) for i,t in enumerate(pages[page['page']].splitlines())]


def test_identity_is_committed_before_post_and_not_sent_does_not_claim_full_coverage(workspace, stub, tmp_path):
    w = workspace
    source, run, _, _ = context(w, MIXED)
    worker = executor(w)
    witnessed = []

    def on_request(number):
        operation = next(iter(extraction_requests.operations(current(w, run)).values()))
        witnessed.append(operation)
        assert operation['call_ids'] and operation['source']['sha256'] == source['sha256']
        assert operation['processor_version'] and operation['schema_sha256'] and operation['body_sha256']

    stub.on_request = on_request
    stub.queue.append(extraction([QUAL]))
    assert ask(w, worker, run, source, 'Все данные', tmp_path)['ok']
    assert len(witnessed) == 1
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    fresh = start_note_run(w, source)
    meta = dict(witnessed[0], key='never-sent')
    meta.pop('call_ids')
    ledger = Accounting(w.runs)
    call = ledger.reserve(fresh['id'], fresh['epoch'], 'extraction', extraction=meta)
    with w.engine.begin() as c:
        current_run = w.runs.locked(c, fresh['id'])
        ledger.withhold(c, current_run, call, ApiError('budget_exceeded').body['error'])
    baseline = counts(w, fresh)
    with w.engine.begin() as c:
        current_run = w.runs.locked(c, fresh['id'])
        extraction_requests.guard(c, current_run, dict(meta, key='another-operation'))
    assert counts(w, fresh) == baseline == ((1, 0), (1, 0))


def test_unresolved_page_dependency_keeps_a_limitation_without_post(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, run, _, _ = context(w, ['Synthetic', 'Выручка. см. стр. 99'], following=[(['Other'], None)])
    reply = ask(w, p.worker, run, source, 'Выручка', tmp_path)
    assert not reply['ok'] and 'Не разрешена зависимость' in reply['error']['message']
    assert p.sent == [] and counts(w, run) == ((0, 0), (0, 0))
    entry = current(w, run)['checkpoint']['extractions'][0]
    assert entry['coverage']['unresolved'] and entry['limitation']


def test_same_tool_id_cached_empty_result_checks_pdf_integrity(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED)
    p.facts = []
    reply = ask(w, p.worker, run, source, 'Все данные', tmp_path)
    assert reply['ok']
    args = dict(refs=[], extract=dict(source_version_id=source['id'], question='Все данные'))
    w.service.storage.path(source['id'] + '.pdf').write_bytes(b'corrupted synthetic PDF')
    with pytest.raises(ApiError, match='source_not_ready'):
        w.runs.tool(run['id'], run['epoch'], reply['tool_call_id'], 'aist_get_facts', args)
    assert len(p.sent) == 1
