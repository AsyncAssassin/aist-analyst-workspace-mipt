import os
from copy import deepcopy
from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import pytest
from sqlalchemy import event

from app import note_news, note_external, versions
from app.database import make_engine
from app.errors import ApiError
from app.runs import execute
from app.service import ident
from app.worker import Worker
from test_d15_postgres import workspace
from test_perf02_r4_postgres import note_source, start_note_run, save_args, small_note

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('failure', [False, True])
def test_news_snapshot_is_run_scoped_and_edits_do_not_requery(workspace, monkeypatch, failure):
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)
    # Подменяем только распознавание на синтетическом PDF и чтение публикаций.
    def identity(pages, filename):
        # Разбор PDF не должен удерживать строку, необходимую heartbeat.
        assert w.runs.heartbeat(run['id'], run['epoch'], run['worker_id'])
        return 'Займер', ''

    monkeypatch.setattr(note_news, 'identify', identity)
    called = []

    def publication(company, inn, date, sector):
        called.append(company)
        assert w.runs.heartbeat(run['id'], run['epoch'], run['worker_id'])
        if failure:
            raise TimeoutError()
        result = note_news.snapshot('found', date, company)
        result['items'] = [
            dict(
                quote='Займер разместил облигации',
                url='https://ir.zaymer.ru/news/company-news/post-123',
                publisher='Займер, для инвесторов',
                published=date,
                page_sha256='a' * 64,
            )
        ]
        return result

    note_news.prepare(w.runs, run, publication)
    note_news.prepare(w.runs, run, publication)
    args = save_args(small_note(source, corrected=True))

    def save():
        return w.runs.tool(run['id'], run['epoch'], 'news_save', 'aist_save_artifact', args)

    first = save()
    assert first['ok'], first
    assert save() == first
    v = first['result']['version']
    news = v['basis']['news_run']
    assert called == ['Займер']
    if failure:
        assert news['status'] == 'unavailable' and not news['items']
        assert all(s['title'] != 'News Run' for s in v['payload']['content']['sections'])
        return
    assert news['status'] == 'found'
    section = next(s for s in v['payload']['content']['sections'] if s['title'] == 'News Run')
    bid = section['blocks'][0]['block_id']
    base = dict(
        expected_revision=1, parent_version_id=v['id'], require_latest=True, payload=deepcopy(v['payload'])
    )
    edited = next(
        b for s in base['payload']['content']['sections'] for b in s['blocks'] if b['block_id'] == bid
    )
    edited['claim']['text'] = 'Займер погасил облигации'

    def edit(body):
        return versions.write(
            w.runs, w.actor, w.session, w.project, v['artifact_id'], None, 'saveVersion', ident('key'), body
        )[1]

    new = edit(base)
    assert new['version']['basis']['news_run'] == news
    assert any(f['code'] == 'news_text_changed' for c in new['checks'] for f in c['findings'])
    base.update(expected_revision=2, parent_version_id=new['version']['id'])
    edited['block_id'] = 'news_untrusted'
    with pytest.raises(ApiError):
        edit(base)
    base['payload']['content']['sections'] = [
        s for s in base['payload']['content']['sections'] if s['title'] != 'News Run'
    ]
    removed = edit(base)
    assert all(s['title'] != 'News Run' for s in removed['version']['payload']['content']['sections'])
    assert w.runs.version(w.actor, w.project, v['id'])['version'] == v
    with w.engine.connect() as c:
        assert execute(c, 'select count(*) from aist.model_calls').scalar_one() == 0


def test_unmatched_private_pdf_finishes_without_an_external_request(workspace):
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)

    def unexpected(*args):
        pytest.fail('Файл неизвестной компании не должен инициировать внешний запрос')

    note_news.prepare(w.runs, run, unexpected)
    args = save_args(small_note(source, corrected=True))
    reply = w.runs.tool(run['id'], run['epoch'], 'private_save', 'aist_save_artifact', args)
    assert reply['ok']
    v = reply['result']['version']
    assert v['basis']['news_run']['status'] == 'unmatched'
    assert not any(s['title'] == 'News Run' for s in v['payload']['content']['sections'])
    # Вопрос и правка не являются запуском News Run.
    for change in (dict(requested_result_type='evidence_answer'), dict(base_version_id=v['id'])):
        note_news.prepare(None, dict(input={**run['input'], **change}), unexpected)


@pytest.mark.parametrize('change,error_code', [
    ('cancelled', 'run_terminal'), ('epoch', 'outcome_unknown'),
    ('lease', 'outcome_unknown'), ('session', 'unauthenticated'),
])
def test_news_cannot_write_after_execution_or_access_changed(workspace, monkeypatch, change, error_code):
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)
    monkeypatch.setattr(note_news, 'identify', lambda pages, filename: ('Familia', ''))
    lookups = []

    def publication(company, inn, as_of, sector):
        lookups.append(company)
        with w.engine.begin() as c:
            current = w.runs.locked(c, run['id'])
            if change == 'cancelled':
                w.runs.transition(c, current, 'cancelled')
            elif change == 'epoch':
                execute(c, 'UPDATE aist.runs SET epoch=epoch+1 WHERE id=:r', r=run['id'])
            elif change == 'lease':
                execute(c, "UPDATE aist.runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:r", r=run['id'])
            else:
                execute(c, 'UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:s', s=w.session)
        return note_news.snapshot('empty', as_of, company)

    with pytest.raises(ApiError) as error:
        note_news.prepare(w.runs, run, publication)
    assert error.value.code == error_code
    with w.engine.connect() as c:
        saved = execute(c, 'SELECT news_run FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        assert saved['status'] == 'unavailable' and saved['company'] == ''
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
    assert lookups == ['Familia']


def test_committed_context_with_lost_ack_does_not_start_model_or_repeat_search(workspace, monkeypatch, tmp_path):
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)
    worker = Worker(replace(w.settings, openclaw_root=str(tmp_path / 'openclaw')))
    lookups = []
    lose_ack = False
    committed = worker.engine.dialect.do_commit

    def identify(materials):
        nonlocal lose_ack
        lookups.append(materials)
        lose_ack = True
        return dict(company='Familia', inn='', sector='', domain='')

    def commit(connection):
        nonlocal lose_ack
        committed(connection)
        if lose_ack:
            lose_ack = False
            raise ConnectionError('Synthetic lost commit acknowledgement')

    def unexpected(*args):
        pytest.fail('Неопределённая запись внешнего контекста не разрешает отправку модели')

    monkeypatch.setattr(note_external, 'identity', identify)
    monkeypatch.setattr(worker.engine.dialect, 'do_commit', commit)
    monkeypatch.setattr('app.openclaw_runtime.run', unexpected)
    try:
        worker.job(run)
        with w.engine.connect() as c:
            saved = execute(c, 'SELECT * FROM aist.runs WHERE id=:r', r=run['id']).mappings().one()
            assert saved['external_context']['status'] == 'empty'
            assert saved['external_context']['company'] == 'Familia'
            assert saved['state'] == 'failed'
            assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0
            assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        with pytest.raises(ApiError):
            note_external.prepare_all(w.runs, saved)
        assert len(lookups) == 1
    finally:
        worker.engine.dispose()


def test_context_lock_timeout_stops_before_model_and_exposes_holder(workspace, monkeypatch, tmp_path):
    # Искусственное удержание строки проверяет отказ, а не причину исторического инцидента.
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)
    worker = Worker(replace(w.settings, openclaw_root=str(tmp_path / 'openclaw')))
    blocker = make_engine(w.settings)
    ready, release, timed_out = Event(), Event(), Event()
    blocker_pid = []

    def hold():
        with blocker.begin() as c:
            blocker_pid.append(execute(c, 'SELECT pg_backend_pid()').scalar_one())
            execute(c, 'SELECT id FROM aist.runs WHERE id=:r FOR UPDATE', r=run['id']).one()
            ready.set()
            assert release.wait(10)

    def on_error(context):
        if getattr(context.original_exception, 'pgcode', None) == '57014':
            timed_out.set()
            release.set()

    def unexpected(*args):
        pytest.fail('После таймаута записи модель не должна вызываться')

    monkeypatch.setattr('app.openclaw_runtime.run', unexpected)
    event.listen(worker.engine, 'handle_error', on_error)
    holders = []
    observed = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            holders.append(pool.submit(hold))
            assert ready.wait(5)
            job = pool.submit(worker.job, run)
            # Наблюдаем только PID и вид ожидания, без текста и параметров запросов.
            with w.engine.connect() as c:
                deadline = monotonic() + 5
                while monotonic() < deadline and not job.done() and not timed_out.wait(.02):
                    observed = execute(c, '''SELECT pid, pg_blocking_pids(pid) AS blockers, wait_event_type
                        FROM pg_stat_activity WHERE datname=current_database()
                        AND :holder=ANY(pg_blocking_pids(pid))''', holder=blocker_pid[0]).mappings().all()
                    if observed:
                        break
            job.result(timeout=10)
            for holder in holders:
                holder.result(timeout=5)
        assert timed_out.is_set() and observed
        assert observed[0]['blockers'] == blocker_pid and observed[0]['wait_event_type'] == 'Lock'
        with w.engine.connect() as c:
            saved = execute(c, 'SELECT state,external_context FROM aist.runs WHERE id=:r', r=run['id']).mappings().one()
            assert saved['state'] == 'failed' and saved['external_context'] is None
            assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0
            assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
    finally:
        release.set()
        worker.engine.dispose()
        blocker.dispose()
