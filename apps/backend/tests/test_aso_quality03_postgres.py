"""ASO collection through the real transport boundary and isolated PostgreSQL, with a loopback model."""
import os
from copy import deepcopy
from dataclasses import replace
from datetime import date
import io
import json
from pathlib import Path
import zipfile

import pytest
from pypdf import PdfReader

from app import collection, note_news, openclaw_runtime as runtime, versions
from app.errors import ApiError
from app.exports import Exports
from app.note_renderer import renderer_versions
from app.runs import execute
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_demo_live01_c_postgres import configure, extracted, native, GOOD, QUAL, MIXED
from test_flow01_02_postgres import context, ingest, passage
from test_flow01_postgres import current, executor
from test_perf02_r1_postgres import upload
from test_perf02_r4_postgres import paragraph, ref, save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def start(w, sources, *, base=None):
    request = body(sources=[s['id'] for s in sources], question='Подготовь аналитическую записку.')
    request.update(skill_version_id=collection.BOUNDED_SKILL, requested_result_type='analytical_note',
                   model_profile_version_id=w.settings.model_profile_id, base_version_id=base,
                   expected_artifact_revision=1 if base else 0)
    send(w, conversation(w), request)
    return w.runs.claim('aso-quality03')


def note(sources, facts):
    return dict(title='Synthetic: информационная справка', audience='Аналитик', sections=[dict(
        section_id='sec_business', title='Описание Компании', blocks=[
            paragraph('block_' + str(i), 'Компания поставляет оборудование клиентам.',
                      [ref(source, next(f['fact_id'] for f in facts
                           if f['source']['source_version_id'] == source['id'] and not f['amount']))])
            for i, source in enumerate(sources)])])


def test_eighteen_candidates_are_ingested_with_their_own_sources(workspace):
    w = workspace
    lines = ['Synthetic', *['Компания поставляет ' + word + ' клиентам.' for word in (
        'насосы', 'клапаны', 'трубы', 'фильтры', 'станки', 'датчики', 'панели', 'кабели', 'моторы',
        'аккумуляторы', 'контроллеры', 'редукторы', 'вентили', 'компрессоры', 'крепления', 'опоры', 'корпуса', 'детали')]]
    source, run, pages, layouts = context(w, lines)
    items = [passage(lines, i, i) for i in range(2, 20)]
    outcome, _ = ingest(w, run, pages, layouts, items, 'Продукты')
    assert len(outcome['facts']) == 18 and not outcome['skipped']
    assert {f['evidence'][0]['quote'] for f in outcome['facts']} == set(lines[1:])
    assert {f['source']['source_version_id'] for f in outcome['facts']} == {source['id']}


def test_mixed_schema_sized_response_preserves_all_candidates_and_repeat(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    lines = ['Synthetic', *[f'Компания поставляет изделие {i} клиентам.' for i in range(18)]]
    source = upload(w, 'mixed-response', lines)
    run = start(w, [source])
    items = [passage(lines, i, i) for i in range(2, 20)]
    visual = dict(page=1, area='Схема', text='Расположение изделий на схеме.', numeric=None)
    response = extracted(items)
    response['content'] = [dict(type='text', text=json.dumps(dict(facts=items, visual_observations=[visual])))]
    native.queue = [dict(body=response)]
    engine = executor(w)
    pin = run['input']['source_versions'][0]
    first = runtime.extract_context(engine, run, pin, 'Подготовь записку', collection.NOTE_TOPICS[0], tmp_path, None)
    assert first is not None, current(w, run).get('error')
    records = collection.results(current(w, run))
    assert len(records) == 1 and len(records[0]['fact_refs']) == 18
    assert len(records[0]['visual_observations']) == 1
    assert records[0]['visual_observations'][0]['verification_status'] == 'unchecked'
    repeated = runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку',
                                      collection.NOTE_TOPICS[0], tmp_path, None)
    assert repeated == first and len(native.requests) == 1
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.facts').scalar_one() == 18
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 1


def test_two_pdfs_share_two_calls_and_both_must_reach_the_note(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    sources = [upload(w, 'issuer-' + str(i), MIXED) for i in range(2)]
    run = start(w, sources)
    native.queue = [dict(body=extracted([QUAL])), dict(body=extracted([QUAL]))]
    facts, _ = runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path)
    assert len(native.requests) == 2 and len(facts) == 2
    assert {e['question'] for e in collection.results(current(w, run))} == {collection.NOTE_COMBINED_TOPIC}
    assert {f['source']['source_version_id'] for f in facts} == {s['id'] for s in sources}
    omitted = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(sources[:1], facts)))
    assert not omitted['ok'] and 'выбранных PDF' in omitted['error']['message']
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(sources, facts)))
    assert saved['ok'], saved
    assert len(saved['result']['version']['basis']['facts']) == 2


def test_three_uncached_pdfs_stop_before_a_model_post(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    sources = [upload(w, 'issuer-' + str(i), MIXED) for i in range(3)]
    run = start(w, sources)
    assert runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path) == (None, None)
    assert not native.requests
    result = current(w, run)
    assert result['state'] == 'failed' and not result['result_version_id']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 0
        assert 'не более двух' in execute(c, "SELECT text FROM aist.messages WHERE role='assistant'").scalar_one()


def test_prepared_note_does_not_accumulate_facts_from_unrelated_past_extractions(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    sources = [upload(w, 'issuer-' + str(i), MIXED) for i in range(2)]
    prior = start(w, sources)
    native.queue = [dict(body=extracted([GOOD]))]
    old, _ = runtime.extract_context(executor(w), prior, prior['input']['source_versions'][0],
        'Подготовь аналитическую записку.', 'Выручка за прошлый период', tmp_path, None)
    old_fact = old['facts'][0]
    w.runs.stop(prior['id'], prior['epoch'], 'validation_failed')
    w.runs.release_finished(prior['id'], prior['epoch'], prior['worker_id'])
    run = start(w, sources)
    native.queue = [dict(body=extracted([QUAL])), dict(body=extracted([QUAL]))]
    facts, _ = runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path)
    assert len(facts) == 2 and len(native.requests) == 3
    assert all(f['fact_id'] != old_fact['fact_id'] for f in facts)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT data FROM aist.facts WHERE fact_id=:id', id=old_fact['fact_id']).scalar_one() == old_fact
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    following = start(w, sources)
    reused, parent = runtime.execution_facts(executor(w), following, 'Подготовь аналитическую записку.', tmp_path)
    assert reused == facts and parent is None and len(native.requests) == 3


def test_three_pdfs_can_reuse_exact_material_without_reposting(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    sources = [upload(w, 'issuer-' + str(i), MIXED) for i in range(3)]
    prior = start(w, sources[:2])
    native.queue = [dict(body=extracted([QUAL])), dict(body=extracted([QUAL]))]
    prior_facts, _ = runtime.execution_facts(executor(w), prior, 'Подготовь аналитическую записку.', tmp_path)
    assert prior_facts is not None and len(prior_facts) == 2, current(w, prior).get('error')
    w.runs.stop(prior['id'], prior['epoch'], 'validation_failed')
    w.runs.release_finished(prior['id'], prior['epoch'], prior['worker_id'])
    run = start(w, sources)
    native.queue = [dict(body=extracted([QUAL]))]
    facts, _ = runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path)
    assert len(native.requests) == 3 and len(facts) == 3
    repeated, _ = runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path)
    assert repeated == facts and len(native.requests) == 3
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(sources, facts)))
    assert saved['ok'], saved
    assert {f['source']['source_version_id'] for f in saved['result']['version']['basis']['facts']} == {s['id'] for s in sources}
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    cached_run = start(w, sources)
    reused, parent_call = runtime.execution_facts(executor(w), cached_run, 'Подготовь аналитическую записку.', tmp_path)
    assert len(reused) == 3 and len(native.requests) == 3
    assert parent_call is None  # No accounting parent from a different Run.


def test_one_saved_fact_does_not_skip_other_material_themes(workspace, native, tmp_path):
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'issuer', MIXED)
    prior = start(w, [source])
    native.queue = [dict(body=extracted([GOOD]))]
    runtime.extract_context(executor(w), prior, prior['input']['source_versions'][0], 'Подготовь аналитическую записку.',
                            collection.NOTE_TOPICS[0], tmp_path, None)
    w.runs.stop(prior['id'], prior['epoch'], 'validation_failed')
    w.runs.release_finished(prior['id'], prior['epoch'], prior['worker_id'])
    run = start(w, [source])
    native.queue = [dict(body=extracted([QUAL]))]
    facts, _ = runtime.execution_facts(executor(w), run, 'Подготовь аналитическую записку.', tmp_path)
    assert facts is not None, current(w, run).get('error')
    assert len(facts) == 2 and len(native.requests) == 2
    assert {e['question'] for e in collection.results(current(w, run))} == set(collection.NOTE_TOPICS)
    assert len({f['fact_id'] for f in facts}) == 2
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
    w.runs.release_finished(run['id'], run['epoch'], run['worker_id'])
    cached_run = start(w, [source])
    reused, parent = runtime.execution_facts(executor(w), cached_run, 'Подготовь аналитическую записку.', tmp_path)
    assert {f['fact_id'] for f in reused} == {f['fact_id'] for f in facts}
    assert len(native.requests) == 2 and parent is None


@pytest.mark.parametrize('interruption', ['none', 'before_ingest', 'unknown'])
def test_second_pass_receives_frozen_financial_checklist_without_duplicate_post(
        workspace, native, tmp_path, monkeypatch, interruption):
    w = workspace
    configure(w, tmp_path, visual=True)
    source = upload(w, 'two-passes', MIXED)
    run = start(w, [source])
    engine = executor(w)
    pin = run['input']['source_versions'][0]
    visual = dict(page=1, area='Таблица', text='Долг/EBITDA 0,3 за 2024 год', numeric=dict(
        metric='Долг/EBITDA', subject='Synthetic', amount=dict(value='0.3', unit='ratio', scale='1', currency=None),
        period=dict(kind='interval', label='2024 год', start='2024-01-01', end='2024-12-31'), nature='reported_actual'))
    response = extracted([GOOD])
    response['content'] = [dict(type='text', text=json.dumps(dict(facts=[GOOD], visual_observations=[visual])))]
    native.queue = [dict(body=response)]
    first, call = runtime.extract_context(engine, run, pin, 'Подготовь записку', collection.NOTE_TOPICS[0], tmp_path, None)
    assert len(first['facts']) == 1 and first['visual_observations'][0]['verification_status'] == 'unchecked'
    native.queue = [dict(close=True)] if interruption == 'unknown' else [dict(body=extracted([QUAL]))]
    original_ingest = runtime.ingest
    if interruption == 'before_ingest':
        def interrupt(*args, **kwargs):
            raise RuntimeError('Synthetic interruption after receipt')
        monkeypatch.setattr(runtime, 'ingest', interrupt)
        with pytest.raises(RuntimeError, match='after receipt'):
            runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку', collection.NOTE_TOPICS[1], tmp_path, call)
        monkeypatch.setattr(runtime, 'ingest', original_ingest)
    elif interruption == 'unknown':
        with pytest.raises(ApiError):
            runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку', collection.NOTE_TOPICS[1], tmp_path, call)
    else:
        runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку', collection.NOTE_TOPICS[1], tmp_path, call)
    sent = json.loads(native.requests[1]['messages'][0]['content'][0]['text'])
    checklist = sent['prior_numeric_observations']
    assert len(checklist) == 2
    assert {item['evidence_kind'] for item in checklist} == {'saved_text', 'visual_unchecked'}
    assert any(item['amount'] == visual['numeric']['amount'] and item['period'] == visual['numeric']['period']
               for item in checklist)
    # A later receipt must not change the input of an already sent business operation.
    with w.engine.begin() as c:
        latest = w.runs.locked(c, run['id'])
        entry = deepcopy(collection.results(latest)[0])
        entry.update(call_id='later_receipt', request_key='later_receipt')
        entry['visual_observations'][0]['numeric']['amount']['value'] = '9.9'
        from app.extraction import record_result
        record_result(c, latest, entry)
        assert collection.prior_numeric_observations(w.runs, c, latest, pin, collection.NOTE_TOPICS[1]) == checklist
        other_pin = dict(pin, source_version_id='sv_other')
        assert collection.prior_numeric_observations(w.runs, c, latest, other_pin, collection.NOTE_TOPICS[1]) == []
    if interruption == 'unknown':
        assert current(w, run)['error']['code'] == 'outcome_unknown'
        with pytest.raises(ApiError) as refused:
            runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку', collection.NOTE_TOPICS[1], tmp_path, call)
        assert refused.value.code == 'run_terminal'
    else:
        second, _ = runtime.extract_context(engine, current(w, run), pin, 'Подготовь записку', collection.NOTE_TOPICS[1], tmp_path, call)
        assert len(second['facts']) == 1 and not second['skipped']
    assert len(native.requests) == 2
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 2


def test_saved_parent_continues_without_collection(workspace, monkeypatch):
    from test_perf02_r4_postgres import note_source, start_note_run, small_note
    w = workspace
    source, _ = note_source(w)
    prior = start_note_run(w, source)
    saved = w.runs.tool(prior['id'], prior['epoch'], ident('tool'), 'aist_save_artifact',
                        save_args(small_note(source, corrected=True)))
    assert saved['ok'], saved
    version = saved['result']['version']
    run = start(w, [source], base=version['id'])

    def unexpected(*args, **kwargs):
        pytest.fail('Продолжение выбранной версии не должно запускать извлечение')

    monkeypatch.setattr(runtime, 'extract_context', unexpected)
    facts, call = runtime.execution_facts(executor(w), run, 'Уточни формулировки', None)
    assert facts == version['basis']['facts'] and call is None


def test_model_table_is_translated_but_manual_label_and_history_are_preserved(workspace, monkeypatch):
    import test_perf02_r4_postgres as fixture
    w = workspace
    monkeypatch.setattr(fixture, 'LINES', ['Синтетика', 'Revenue 100 млн руб. за 2024 год'])
    monkeypatch.setattr(fixture, 'SPECS', [('fact_revenue', 'Revenue', 2,
        dict(fixture.MONEY, value='100'), dict(kind='interval', label='2024 год', start='2024-01-01', end='2024-12-31'))])
    source, facts = fixture.note_source(w)
    run = fixture.start_note_run(w, source)
    row = fixture.table_row('row_revenue', 'Revenue, млн руб.', '2024 год', '100', [ref(source, 'fact_revenue')])
    content = dict(title='Синтетика', audience='Аналитик', sections=[dict(section_id='sec_fin',
        title='Финансовые показатели', blocks=[dict(block_id='block_fin', kind='table', caption='Выручка', rows=[row])])])
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert saved['ok'], saved
    first = saved['result']['version']
    model_row = first['payload']['content']['sections'][0]['blocks'][0]['rows'][0]
    assert model_row == dict(row, label='Выручка')
    assert first['basis']['facts'][0]['metric'] == 'Revenue'
    changed = deepcopy(first['payload'])
    changed['content']['sections'][0]['blocks'][0]['rows'][0]['label'] = 'Revenue'
    key = ident('key')
    args = dict(expected_revision=1, parent_version_id=first['id'], require_latest=True, payload=changed)
    status, second = versions.write(w.runs, w.actor, w.session, w.project, first['artifact_id'], None, 'saveVersion', key, args)
    assert status == 201
    assert second['version']['payload'] == changed
    assert versions.write(w.runs, w.actor, w.session, w.project, first['artifact_id'], None, 'saveVersion', key, args) == (200, second)
    assert w.runs.version(w.actor, w.project, first['id'])['version'] == first
    assert second['version']['basis']['facts'] == first['basis']['facts']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 2
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0


def test_synthetic_publications_reach_first_version_and_exports_once(workspace, monkeypatch, tmp_path):
    company = 'Меридиан-Сетка'
    items = [
        dict(quote='Меридиан-Сетка начала обслуживание станционных табло.',
             published='2026-01-15', url='https://www.interfax.ru/business/990019001',
             publisher='Интерфакс', page_sha256='b' * 64),
        dict(quote='Меридиан-Сетка открыла склад запасных модулей.',
             published='2026-02-18', url='https://www.interfax.ru/business/990019002',
             publisher='Интерфакс', page_sha256='c' * 64),
    ]
    verify_publication_exports(workspace, monkeypatch, tmp_path, company, items)


def verify_publication_exports(workspace, monkeypatch, tmp_path, company, items):
    from test_perf02_r4_postgres import note_source, start_note_run, small_note
    w = workspace
    source, _ = note_source(w)
    run = start_note_run(w, source)
    monkeypatch.setattr(note_news, 'identify', lambda pages, filename: (company, ''))
    calls = []

    def publication(name, inn, as_of, sector):
        calls.append(name)
        result = note_news.snapshot('found', as_of, name)
        result['items'] = deepcopy(items)
        return result

    note_news.prepare(w.runs, run, publication)
    note_news.prepare(w.runs, run, publication)
    args = save_args(small_note(source, corrected=True))
    assert all(s['title'] != 'News Run' for s in args['payload']['content']['sections'])
    first = w.runs.tool(run['id'], run['epoch'], 'news_save', 'aist_save_artifact', args)
    assert first['ok'], first
    assert w.runs.tool(run['id'], run['epoch'], 'news_save', 'aist_save_artifact', args) == first
    initial = first['result']['version']
    assert initial['basis']['news_run']['items'] == items
    news = [s for s in initial['payload']['content']['sections'] if s['title'] == 'News Run']
    assert len(news) == 1 and len(news[0]['blocks']) == len(items)
    assert [b['claim']['text'] for b in news[0]['blocks']] == [note_news.news_text(i) for i in items]
    changed = deepcopy(initial['payload'])
    changed['content']['sections'] = [s for s in changed['content']['sections'] if s['title'] != 'News Run']
    removed = versions.write(w.runs, w.actor, w.session, w.project, initial['artifact_id'], None,
        'saveVersion', ident('key'), dict(expected_revision=1, parent_version_id=initial['id'],
                                         require_latest=True, payload=changed))[1]['version']
    assert removed['basis']['news_run'] == initial['basis']['news_run']
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    output = Path(os.getenv('AIST_ASO_QUALITY03_EVIDENCE', str(tmp_path))) / company
    output.mkdir(parents=True, exist_ok=True)
    evidence = []
    for label, version, present in [('first', initial, True), ('removed', removed, False)]:
        view = w.runs.version(w.actor, w.project, version['id'])
        assert view['version'] == version
        for fmt in ('docx', 'pdf'):
            status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'],
                ident('key'), dict(expected_revision=view['review']['revision'], format=fmt,
                    template_version='aist-aso.v1', renderer_version=renderer_versions('aist-aso.v1')[fmt],
                    settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft')))
            assert status == 202
            exports.process_one()
            data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
            assert ready['state'] == 'ready'
            (output / (label + '.' + fmt)).write_bytes(data)
            if fmt == 'docx':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    text = archive.read('word/document.xml').decode()
                    links = archive.read('word/_rels/document.xml.rels').decode()
            else:
                pages = PdfReader(io.BytesIO(data)).pages
                text = ''.join(page.extract_text() or '' for page in pages)
                links = str([a.get_object().get('/A') for page in pages for a in page.get('/Annots', [])])
            assert ('News Run' in text) is present
            # Modern keeps the publisher in the basis and renders a publication link with its date.
            assert ('ссылка' in text) is present
            for item in items:
                assert (item['url'] in links) is present
                if present:
                    assert ''.join(item['quote'].split()) in ''.join(text.split())
                    assert date.fromisoformat(item['published']).strftime('%d.%m.%Y') in text
            evidence.append(dict(version=version['id'], export=entry['id'], format=fmt, news_present=present))
    (output / 'evidence.json').write_text(json.dumps(dict(initial=initial, removed=removed, exports=evidence),
                                                    ensure_ascii=False, indent=2))
    assert calls == [company]
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0
