"""Новые табличные периоды в штатном ingest; все данные и ответы тестовые."""
from copy import deepcopy
import os

import pytest

from app.errors import ApiError
from app import extraction
from app.extraction import ingest
from app.facts import pin_of
from test_d15_postgres import workspace
from test_fable_postgres import audit, extracted, native, start_fable
from test_flow01_02_postgres import checkpoint
from test_flow01_03_postgres import passage, uploaded
from test_flow01_05_postgres import ask
from test_flow01_postgres import current, executor
from test_note_path05_r2 import document_items, table_item
from test_perf02_r1_postgres import accounted_response, fact_rows

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('strict', [None, 'pin', 'reference', 'structure'])
def test_calendar_date_mixed_batch_is_local_but_bad_authority_is_atomic(workspace, strict):
    w = workspace
    label = 'на 15 мая 2026 г.'
    source, run, pages, layouts = uploaded(w, 'Тестовый календарный остаток', [document_items(label)])
    good = table_item(pages, label, 'instant', '2026-05-15', '2026-05-15', metric='Активы')
    wrong = deepcopy(good)
    wrong['period'].update(kind='interval', start='2026-01-01', end='2026-12-31')
    payload = dict(facts=[good, wrong, passage(pages, 1)])
    pin = pin_of(source)
    if strict == 'pin':
        pin = dict(pin, sha256='0' * 64)
    elif strict == 'reference':
        payload['facts'][-1]['evidence'][0]['line_start'] = 999
        payload['facts'][-1]['evidence'][0]['line_end'] = 999
    elif strict == 'structure':
        payload['facts'][-1]['extra'] = 'Недопустимое поле'
    original = deepcopy(payload)
    call = accounted_response(w, run, payload)
    before = checkpoint(w, run)
    if strict:
        with pytest.raises(ApiError) as refused:
            ingest(w.runs, run, pages, payload, call, pin, layouts, question='Остаток и описание бизнеса')
        assert refused.value.code == 'validation_failed'
        assert fact_rows(w) == [] and checkpoint(w, run) == before
    else:
        result = ingest(w.runs, run, pages, payload, call, pin, layouts, question='Остаток и описание бизнеса')
        assert result['status'] == 'partial' and len(result['facts']) == 2
        assert [(item['candidate'], item['code']) for item in result['skipped']] == [(2, 'period_mismatch')]
        assert next(f for f in result['facts'] if f['amount'])['period'] == good['period']
        rows, recorded = fact_rows(w), checkpoint(w, run)
        assert ingest(w.runs, run, pages, payload, call, pin, layouts, question='Остаток и описание бизнеса') == result
        assert fact_rows(w) == rows and checkpoint(w, run) == recorded
    assert payload == original
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_new_extraction_version_does_not_relabel_or_reuse_old_cached_facts(workspace, native, monkeypatch, tmp_path):
    from test_flow01_02_postgres import QUAL
    w = workspace
    version = extraction.VERSION
    monkeypatch.setattr(extraction, 'VERSION', 'pdf-lines.v24')
    source, _, run = start_fable(w, full=True)
    native.queue = [dict(body=extracted([QUAL])), dict(body=extracted([QUAL]))]
    first = ask(w, executor(w), current(w, run), source, 'Описание бизнеса', tmp_path)
    assert first['ok']
    previous_rows = deepcopy(fact_rows(w))
    old_entries = deepcopy(checkpoint(w, run)['extractions'])
    monkeypatch.setattr(extraction, 'VERSION', version)
    stale = ask(w, executor(w), current(w, run), source, 'Описание бизнеса', tmp_path)
    assert not stale['ok'] and len(native.requests) == 1
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    old_run = run
    _, _, run = start_fable(w, full=True, source=source)
    second = ask(w, executor(w), current(w, run), source, 'Описание бизнеса', tmp_path)
    assert second['ok'] and len(native.requests) == 2
    assert first['result']['facts'][0]['extraction_version'] == 'pdf-lines.v24'
    assert second['result']['facts'][0]['extraction_version'] == version
    assert first['result']['facts'][0]['fact_id'] != second['result']['facts'][0]['fact_id']
    rows = fact_rows(w)
    assert all(row in rows for row in previous_rows) and len(rows) == 2
    assert checkpoint(w, old_run)['extractions'] == old_entries
    assert ask(w, executor(w), current(w, run), source, 'Описание бизнеса', tmp_path)['result'] == second['result']
    assert len(native.requests) == 2 and len(audit(w, old_run)[0]) == len(audit(w, run)[0]) == 1 and fact_rows(w) == rows
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
