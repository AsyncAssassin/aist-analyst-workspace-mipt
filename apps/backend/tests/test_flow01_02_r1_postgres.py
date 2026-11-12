"""Source binding and evidence bounds during PDF ingestion.

The extraction results delivered to a Run at its start are bound to it, so the gap of a partial result
reaches the saved draft, while a new task of the same conversation receives nothing. A table or definition
reference outside the saved text refuses the whole batch. The full pin is verified when no fact is saved.
Synthetic PDFs, accounted responses without transport and the loopback provider; no model, no paid request."""
from copy import deepcopy
import os

import pytest

from app import collection
from app.coverage import startup_entries
from app.errors import ApiError
from app.exports import Exports
from app.extraction import completed_requests, ingest as ingest_payload
from app.facts import pin_of
from app.runs import execute
from app.service import ident
from test_chat01_postgres import body, send
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_draft01_postgres import accept, export_request
from test_flow01_02_postgres import MIXED, QUAL, TABLE, TRLN, checkpoint, context, coverage_findings, extract, human_edit, ingest, note
from test_flow01_postgres import counts, current
from test_perf02_r1_postgres import accounted_response, fact_rows
from test_perf02_r4_postgres import save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

TASK = 'Подготовь справку: бизнес компании и выручка за 2023 год'
# The reference of the second candidate that points outside the saved text while its evidence stays correct (review F02).
ABSENT_REFERENCES = {
    'table_page': ('table', 'page', 'Страница таблицы отсутствует в источнике.'),
    'table_row': ('table', 'row_line', 'Строки таблицы отсутствуют в источнике.'),
    'table_year': ('table', 'year_line', 'Строки таблицы отсутствуют в источнике.'),
    'table_period': ('table', 'period_line_start', 'Строки таблицы отсутствуют в источнике.'),
    'basis_page': ('basis', 'page', 'Страница определения отсутствует в источнике.'),
    'reporting_page': ('basis', 'reporting_page', 'Страница отчетной базы отсутствует в источнике.'),
}


def follow_up(w, previous, source, text, **changes):
    """The next Run of the conversation of an ended Run: a new v3 note with this task text and the same PDF."""
    with w.engine.connect() as c:
        conversation = dict(execute(c, 'SELECT id,revision FROM aist.conversations WHERE id=:id', id=previous['conversation_id']).mappings().one())
    request = body(revision=conversation['revision'], sources=[source['id']], question=text)
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note', **changes)
    send(w, conversation, request)
    run = w.runs.claim('flow02_r1')
    assert run['conversation_id'] == previous['conversation_id']
    return run


def startup(w, run):
    """The startup context of a Run as run() prepares it: the delivered results, bound to the Run first."""
    with w.engine.begin() as c:
        locked = w.runs.locked(c, run['id'])
        w.runs.fence(c, locked, run['epoch'])
        task = execute(c, 'SELECT text FROM aist.messages WHERE id=:m', m=run['message_id']).scalar_one()
        return startup_entries(w.runs, c, locked, task)


def model_call(w, call_id):
    with w.engine.connect() as c:
        call = execute(c, 'SELECT data FROM aist.model_calls WHERE id=:id', id=call_id).scalar_one()
    return call['transport_state'], call['provider_outcome'], call['delivery']


def blocked(w, view):
    """The acceptance and the accepted release of this version are refused."""
    with pytest.raises(ApiError) as acceptance:
        accept(w, view, acknowledge_unchecked_semantics=False)
    assert acceptance.value.code == 'validation_failed'
    with pytest.raises(ApiError) as release:
        Exports(w.runs).request(w.actor, w.session, w.project, view['version']['artifact_id'], view['version']['id'],
                                ident('key'), export_request('pdf', 'accepted'))
    assert release.value.code == 'acceptance_required'


@pytest.mark.parametrize('field', list(ABSENT_REFERENCES))
def test_a_table_or_definition_reference_outside_the_source_refuses_the_whole_batch(workspace, field):
    """The evidence is correct and one page or line of the table or the definition does not exist: the batch
    is refused before any write, also after a local exclusion; the same table with existing references but an
    header printed below the row stays a local exclusion that keeps the qualitative passage."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED)
    section, key, message = ABSENT_REFERENCES[field]
    bad = deepcopy(TABLE)
    bad[section][key] = 999
    for order in ([QUAL, bad], [TRLN, QUAL, bad]):
        payload = dict(facts=deepcopy(order))
        original = deepcopy(payload)
        call_id = accounted_response(w, run, payload)
        before = checkpoint(w, run)
        with pytest.raises(ApiError) as refused:
            ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts, question='Выручка и бизнес')
        assert refused.value.code == 'validation_failed'
        assert refused.value.body['error']['message'] == f'Факт {len(order)} (Выручка по договорам): {message}'
        assert fact_rows(w) == [] and payload == original and checkpoint(w, run) == before
        assert model_call(w, call_id) == ('observed', 'success', 'delivered')
    with w.engine.connect() as c:
        assert completed_requests(w.runs, c, run, pin_of(version)) == []
    outcome, _ = ingest(w, run, pages, layouts, [TRLN, QUAL, TABLE], 'Выручка и бизнес')
    assert outcome['status'] == 'partial'
    assert [(s['candidate'], s['code']) for s in outcome['skipped']] == [(1, 'ambiguous_link'), (3, 'unsupported_layout')]
    assert 'расположен не над строкой' in outcome['skipped'][1]['message']
    assert [f['metric'] for f in outcome['facts']] == ['Бизнес компании'] and len(fact_rows(w)) == 1
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


@pytest.mark.parametrize('items', [[], [TABLE]], ids=['empty_response', 'all_excluded'])
@pytest.mark.parametrize('field', ['sha256', 'source_id'])
def test_the_full_pin_is_verified_when_no_fact_is_saved(workspace, items, field):
    """With the correct source_version_id, a wrong SHA or source_id of the passed pin refuses the admissible
    empty answer and the batch of excluded candidates before any record; with the correct pin both record `empty`
    with their real reasons, and the same call again returns the same result without a second entry."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED)
    wrong = dict(pin_of(version), **{field: '0' * 64 if field == 'sha256' else 'src_' + '0' * 32})
    payload = dict(facts=deepcopy(items))
    original = deepcopy(payload)
    call_id = accounted_response(w, run, payload)
    before = checkpoint(w, run)
    with pytest.raises(ApiError) as refused:
        ingest_payload(w.runs, run, pages, payload, call_id, pin=wrong, layouts=layouts, question='Выручка')
    assert refused.value.code == 'validation_failed'
    assert refused.value.body['error']['message'] == 'Версия источника или SHA не совпадают.'
    assert checkpoint(w, run) == before and fact_rows(w) == [] and payload == original
    assert model_call(w, call_id) == ('observed', 'success', 'delivered')
    with w.engine.connect() as c:
        assert completed_requests(w.runs, c, run, pin_of(version)) == []
    outcome, call_id = ingest(w, run, pages, layouts, items, 'Выручка')
    assert outcome['status'] == 'empty' and outcome['facts'] == []
    assert [s['code'] for s in outcome['skipped']] == ([] if not items else ['unsupported_layout'])
    recorded = checkpoint(w, run)['extractions']
    assert len(recorded) == 1 and recorded[0]['call_id'] == call_id and recorded[0]['source'] == pin_of(version)
    assert recorded[0]['status'] == 'empty' and recorded[0]['skipped'] == outcome['skipped']
    again, _ = ingest(w, run, pages, layouts, items, 'Выручка', call_id)
    assert again == outcome and checkpoint(w, run)['extractions'] == recorded and fact_rows(w) == []
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_the_startup_context_binds_the_completed_request_of_the_task_and_nothing_else(workspace, provider, tmp_path):
    """A new note of the conversation with the task an earlier Run extracted for receives that partial result
    and is bound to it before any step: the saved draft carries the gap, a reload and an unrelated edit keep it, a
    second delivery or the cached re-read records nothing twice and moves no counter, a continuation receives the
    results behind its base version; a business-only task of the same conversation and PDF receives nothing."""
    w, p = workspace, provider
    source, first, pages, layouts = context(w, MIXED)
    partial, call_id = ingest(w, first, pages, layouts, [QUAL, TABLE], TASK)
    assert partial['status'] == 'partial' and [s['code'] for s in partial['skipped']] == ['unsupported_layout']
    qualitative = partial['facts'][0]
    w.runs.stop(first['id'], first['epoch'], 'validation_failed')
    # The same task in the same conversation, no base version: the completed request of the task is delivered and bound.
    second = follow_up(w, first, source, TASK)
    assert second['input']['base_version_id'] is None and checkpoint(w, second) is None
    delivered = startup(w, second)
    assert [(e['question'], e['status'], e['call_id']) for e in delivered] == [(TASK, 'partial', call_id)]
    recorded = checkpoint(w, second)['extractions']
    assert recorded == delivered and recorded[0]['skipped'] == partial['skipped']
    # Delivered again and read again through the cache: recorded once, no POST, no collection counter moves.
    assert startup(w, second) == delivered
    cached, _ = extract(w, p, second, TASK, [], tmp_path)
    assert cached['ok'] and cached['result']['status'] == 'partial' and p.sent == []
    assert checkpoint(w, second)['extractions'] == recorded
    assert collection.state(current(w, second)) == {'closed': False, 'idle': 0} and counts(w, second) == ((0, 0), (0, 0))
    hint = collection.results_text(collection.results(current(w, second)))
    assert hint.count('«' + TASK + '»') == 1 and 'исключено кандидатов: 1 (unsupported_layout)' in hint
    # The draft by the qualitative fact alone, without any further extraction: saved, blocked, the remark survives.
    saved = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative)))
    assert saved['ok'], saved
    view = saved['result']
    gaps = coverage_findings(view)
    assert view['checks'][0]['status'] == 'passed' and len(gaps) == 1
    assert 'Часть запроса «' + TASK + '» не подтверждена: 1 из 1' in gaps[0]['message'] and 'unsupported_layout' in gaps[0]['message']
    blocked(w, view)
    assert w.runs.get(w.actor, w.project, second['id'])['state'] == 'completed'
    assert coverage_findings(w.runs.version(w.actor, w.project, view['version']['id'])) == gaps
    edited = human_edit(w, view, 'Компания поставляет оборудование своим клиентам.')
    assert coverage_findings(edited) == gaps and edited['version']['provenance']['run_id'] is None
    blocked(w, edited)
    # A business-only task of the same conversation and PDF: nothing delivered, nothing bound, no inherited gap.
    third = follow_up(w, second, source, 'Бизнес компании')
    assert startup(w, third) == [] and checkpoint(w, third) is None and collection.results(current(w, third)) == []
    clean = w.runs.tool(third['id'], third['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative)))
    assert clean['ok'], clean
    assert coverage_findings(clean['result']) == [] and clean['result']['checks'][0]['status'] == 'passed'
    status, review = accept(w, clean['result'])
    assert status == 200 and review['acceptance']
    # A continuation from the blocked draft receives the results behind its base version; its own checkpoint records nothing new.
    continued = follow_up(w, third, source, 'Исправь замечания', base_version_id=edited['version']['id'], expected_artifact_revision=2)
    diagnostic = deepcopy(delivered)
    for entry in diagnostic:
        for skip in entry['skipped']:
            skip.pop('review_candidate', None)
    assert startup(w, continued) == diagnostic and checkpoint(w, continued) is None
    assert checkpoint(w, second)['extractions'] == delivered
    assert p.sent == []
