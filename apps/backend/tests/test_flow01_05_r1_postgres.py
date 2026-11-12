"""FLOW-01.05-R1: actual request pages, refusal history and current document findings."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app import extraction_context, extraction_requests, openclaw_runtime
from app.errors import ApiError
from app.exports import Exports
from app.extraction import recorded_extractions
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace
from test_chat01_r1_postgres import provider
from test_draft01_postgres import accept, export_request
from test_flow01_02_postgres import (
    MIXED, PLAIN, QUAL, TABLE, context, coverage_findings, human_edit, ingest, note, plain_closer,
)
from test_flow01_05_postgres import ask
from test_flow01_postgres import counts, current
from test_perf02_r1_postgres import fact_rows
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def evidence(name, data):
    if directory := os.getenv('AIST_FLOW01_EVIDENCE'):
        Path(directory, name + '.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('phrase', ['см. строку Выручка', 'см. структуру выпуска'])
def test_non_page_text_reaches_the_provider_and_replays_without_post(workspace, provider, tmp_path, phrase):
    w, p = workspace, provider
    source, run, pages, _ = context(w, ['Company'], following=[
        (['Revenue', phrase], None), (['Contacts'], None)])
    reply = ask(w, p.worker, run, source, 'Revenue', tmp_path)
    assert reply['ok'] and reply['result']['status'] == 'empty'
    assert reply['result']['facts'] == [] and reply['result']['skipped'] == []
    assert len(p.sent) == 1
    sent = json.loads(p.sent[0]['messages'][-1]['content'])
    assert [page['page'] for page in sent['pages']] == [1, 2]
    for page in sent['pages']:
        assert page['lines'] == [dict(line=i + 1, text=line) for i, line in enumerate(pages[page['page']].splitlines())]
    entries = recorded_extractions(current(w, run))
    assert len(entries) == 1
    coverage = entries[0]['coverage']
    assert coverage['unresolved'] == [] and coverage['fallback'] is None
    assert not coverage['full_document']
    repeated = ask(w, p.worker, current(w, run), source, 'Revenue', tmp_path / 'replay')
    assert repeated['ok'] and repeated['result'] == reply['result']
    assert recorded_extractions(current(w, run)) == entries
    assert len(p.sent) == 1 and counts(w, run) == ((1, 0), (1, 0))
    evidence('non-page-' + ('row' if 'строку' in phrase else 'structure'),
        dict(phrase=phrase, reply=reply, repeated=repeated, entries=entries, requests=p.sent, posts=len(p.sent),
             database=w.settings.db_name))


@pytest.mark.parametrize('reference,expected', [('3-4', [1, 2, 3, 4]), ('4', [1, 2, 4]), ('3-99', None)])
def test_explicit_reference_in_the_physical_request(workspace, provider, tmp_path, monkeypatch, reference, expected):
    w, p = workspace, provider
    source, run, pages, _ = context(w, ['Synthetic company'], following=[
        (['Revenue', '100', 'см. стр. ' + reference], None),
        (['All amounts are in million RUB.'], None),
        (['The figures include a one-time sale and are not audited.'], None), (['Contacts'], None)])
    original_ingest = openclaw_runtime.ingest
    canonical_inputs = []

    def capture(*args, **kwargs):
        canonical_inputs.append(args[2])
        return original_ingest(*args, **kwargs)

    monkeypatch.setattr(openclaw_runtime, 'ingest', capture)
    reply = ask(w, p.worker, run, source, 'Revenue', tmp_path)
    entries = recorded_extractions(current(w, run))
    if expected is None:
        assert not reply['ok'] and p.sent == [] and canonical_inputs == []
        assert entries[0]['coverage']['unresolved'] and entries[0]['limitation']
        assert counts(w, run) == ((0, 0), (0, 0))
    else:
        assert reply['ok'] and len(p.sent) == 1
        assert canonical_inputs == [pages] and sorted(pages) == [1, 2, 3, 4, 5]
        sent = json.loads(p.sent[0]['messages'][-1]['content'])
        assert [page['page'] for page in sent['pages']] == expected
        for page in sent['pages']:
            assert page['lines'] == [dict(line=i + 1, text=line) for i, line in enumerate(pages[page['page']].splitlines())]
        assert next(page for page in sent['pages'] if page['page'] == 4)['lines'][0]['text'] == pages[4]
        assert not entries[0]['coverage']['unresolved']
    evidence('reference-' + reference, dict(reply=reply, entries=entries, requests=p.sent, database=w.settings.db_name))


@pytest.mark.parametrize('refusal', ['invalid_candidate', 'another_full_pdf', None])
def test_correct_note_and_manual_version_can_be_accepted_despite_request_history(workspace, provider, tmp_path, refusal):
    """The two F02 review probes and their positive control use the same note and explicit human acceptance."""
    w, p = workspace, provider
    following = [] if refusal == 'another_full_pdf' else [(['Logistics'], None)]
    source, run, _, _ = context(w, MIXED, following=following)
    refused = None
    if refusal == 'invalid_candidate':
        wrong = dict(deepcopy(QUAL), evidence=[dict(page=999, line_start=1, line_end=2, area='Invalid')])
        p.facts = [wrong]
        refused = ask(w, p.worker, run, source, 'Бизнес компании', tmp_path)
        assert not refused['ok'] and fact_rows(w) == []
    p.facts = [deepcopy(QUAL)]
    correct = ask(w, p.worker, current(w, run), source, 'Бизнес', tmp_path)
    assert correct['ok'] and correct['result']['status'] == 'complete'
    if refusal == 'another_full_pdf':
        second = ask(w, p.worker, current(w, run), source, 'Бизнес компании', tmp_path)
        assert second['ok'] and second['result']['coverage']['full_document']
        repeated = ask(w, p.worker, current(w, run), source, 'Бизнес компании', tmp_path)
        assert repeated['ok'] and repeated['result'] == second['result']
    posts = 2 if refusal is not None else 1
    if refused:
        # A new tool ID must replay the refusal, including after a valid answer, without another POST.
        repeated = ask(w, p.worker, current(w, run), source, 'Бизнес компании', tmp_path)
        assert {k: v for k, v in repeated['error'].items() if k != 'tool_call_id'} == {
            k: v for k, v in refused['error'].items() if k != 'tool_call_id'}
    assert len(p.sent) == posts
    entries = recorded_extractions(current(w, run))
    failures = [entry for entry in entries if entry.get('error')]
    assert len(failures) == (1 if refused else 0)
    if refused:
        assert failures[0]['error']['code'] == refused['error']['code']
        assert failures[0]['error']['message'] == refused['error']['message'] and failures[0]['limitation']
        assert failures[0]['coverage'] and failures[0]['call_id']
    before = counts(w, run)
    assert before == ((posts, 0), (posts, 0))
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact',
                        save_args(note(correct['result']['facts'][0])))
    if refused:
        assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
        assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
        w.runs.stop(run['id'], run['epoch'], 'validation_failed')
        w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
        saving_run = start_note_run(w, source)
        assert ask(w, p.worker, saving_run, source, 'Бизнес', tmp_path)['ok']
        saved = w.runs.tool(saving_run['id'], saving_run['epoch'], ident('tool'), 'aist_save_artifact',
                            save_args(note(correct['result']['facts'][0])))
    assert saved['ok'] and saved['result']['checks'][0]['status'] == 'passed'
    assert saved['result']['review']['acceptance'] is None
    edited = human_edit(w, saved['result'], 'Компания поставляет оборудование клиентам. Подготовлена справка о бизнесе.')
    assert not coverage_findings(edited) and edited['checks'][0]['status'] == 'passed'
    assert edited['review']['acceptance'] is None
    status, accepted = accept(w, edited)
    assert status == 200 and accepted['acceptance']
    assert recorded_extractions(current(w, run)) == entries
    assert counts(w, run) == before and len(p.sent) == posts
    evidence('acceptance-' + str(refusal), dict(failures=failures, operations=extraction_requests.operations(current(w, run)),
        saved=saved['result'], edited=edited, acceptance=accepted, posts=posts, database=w.settings.db_name))


def test_refusal_history_and_an_independent_skipped_observation_keep_separate_meanings(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, run, pages, layouts = context(w, MIXED, following=[(PLAIN, None), (['Logistics'], None)])
    p.facts = [dict(deepcopy(QUAL), evidence=[dict(page=999, line_start=1, line_end=2, area='Invalid')])]
    assert not ask(w, p.worker, run, source, 'Бизнес компании', tmp_path)['ok']
    p.facts = [QUAL, TABLE]
    partial = ask(w, p.worker, current(w, run), source, 'Финансовые показатели', tmp_path)
    assert partial['ok'] and partial['result']['status'] == 'partial'
    p.facts = [QUAL]
    complete = ask(w, p.worker, current(w, run), source, 'Бизнес', tmp_path)
    assert complete['ok'] and complete['result']['status'] == 'complete'
    entries = recorded_extractions(current(w, run))
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact',
                        save_args(note(complete['result']['facts'][0])))
    assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    saving_run = start_note_run(w, source)
    assert ask(w, p.worker, saving_run, source, 'Финансовые показатели', tmp_path)['ok']
    assert ask(w, p.worker, current(w, saving_run), source, 'Бизнес', tmp_path)['ok']
    saved = w.runs.tool(saving_run['id'], saving_run['epoch'], ident('tool'), 'aist_save_artifact',
                        save_args(note(complete['result']['facts'][0])))
    assert saved['ok']
    view = saved['result']
    gaps = coverage_findings(view)
    assert len(gaps) == 1 and 'unsupported_layout' in gaps[0]['message']
    edited = human_edit(w, view, 'Компания поставляет оборудование своим клиентам.')
    assert coverage_findings(edited) == gaps
    with pytest.raises(ApiError, match='validation_failed'):
        accept(w, edited, acknowledge_unchecked_semantics=False)
    with pytest.raises(ApiError, match='acceptance_required'):
        Exports(w.runs).request(w.actor, w.session, w.project, edited['version']['artifact_id'], edited['version']['id'],
                                ident('key'), export_request('pdf', 'accepted'))
    later = start_note_run(w, source)
    ingest(w, later, pages, layouts, [plain_closer(PLAIN, "12M'22", 2022)], 'Выручка за 2022 год')
    still = human_edit(w, edited, 'Компания поставляет оборудование клиентам по всей стране.')
    assert coverage_findings(still) == gaps
    ingest(w, later, pages, layouts, [plain_closer(PLAIN, "12M'23", 2023)], 'Выручка за 2023 год')
    corrected = human_edit(w, still, 'Компания поставляет оборудование клиентам.')
    assert corrected['checks'][0]['status'] == 'passed' and not coverage_findings(corrected)
    assert accept(w, corrected)[1]['acceptance']
    assert recorded_extractions(current(w, run)) == entries and len(p.sent) == 3
    evidence('independent-gap', dict(entries=entries, original=view, unrelated=edited, wrong_period=still,
        corrected=corrected, posts=len(p.sent), database=w.settings.db_name))


def test_unresolved_dependency_is_not_cleared_by_a_complete_answer_or_manual_edit(workspace, provider, tmp_path):
    w, p = workspace, provider
    source, run, _, _ = context(w, MIXED, following=[(['Logistics', 'см. стр. 99'], None)])
    refused = ask(w, p.worker, run, source, 'Logistics', tmp_path)
    assert not refused['ok'] and p.sent == []
    p.facts = [QUAL]
    complete = ask(w, p.worker, current(w, run), source, 'Бизнес', tmp_path)
    assert complete['ok'] and complete['result']['status'] == 'complete'
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact',
                        save_args(note(complete['result']['facts'][0])))
    assert not saved['ok'] and saved['error']['code'] == 'validation_failed'
    assert 'Сбор запрошенных материалов не завершён' in saved['error']['message']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.version_acceptances').scalar_one() == 0
    assert len(p.sent) == 1
    evidence('unresolved-dependency', dict(entries=recorded_extractions(current(w, run)), refused=saved,
        posts=len(p.sent), database=w.settings.db_name))


def test_context_version_does_not_relabel_an_old_cached_result(workspace, provider, tmp_path, monkeypatch):
    w, p = workspace, provider
    source, old, _, _ = context(w, MIXED)
    version = extraction_context.VERSION
    monkeypatch.setattr(extraction_context, 'VERSION', 'pdf-context.v2')
    first = ask(w, p.worker, old, source, 'Бизнес', tmp_path)
    assert first['ok']
    old_entries = recorded_extractions(current(w, old))
    assert old_entries[0]['coverage']['context_version'] == 'pdf-context.v2'
    monkeypatch.setattr(extraction_context, 'VERSION', version)
    # The selector version belongs to operation identity; the bounded quota still permits a new operation.
    updated = ask(w, p.worker, current(w, old), source, 'Бизнес', tmp_path)
    assert updated['ok'] and recorded_extractions(current(w, old))[-1]['coverage']['context_version'] == version
    assert len(p.sent) == 2
    w.runs.stop(old['id'], old['epoch'], 'validation_failed')
    fresh = start_note_run(w, source)
    second = ask(w, p.worker, fresh, source, 'Бизнес', tmp_path)
    assert second['ok'] and recorded_extractions(current(w, fresh))[0]['coverage']['context_version'] == version
    assert second['result'] == updated['result'] and len(p.sent) == 2
    assert recorded_extractions(current(w, old))[0] == old_entries[0]
