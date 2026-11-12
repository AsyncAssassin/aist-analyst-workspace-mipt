"""FLOW-01.02: the usable part of an extraction response is saved, each exclusion keeps its reason, the strict
guarantees stay strict, and an unclosed gap blocks the acceptance of the draft.

Synthetic PDFs and accounted responses without transport; the loopback stub drives the bounded Run. No model and no paid request."""
from copy import deepcopy
import os

import pytest

from app import collection, extraction
from app.coverage import CODE as COVERAGE
from app.errors import ApiError
from app.exports import Exports
from app.extraction import APART, SKIP_REASONS, VERSION, completed_requests, ingest as ingest_payload, source_pages
from app.facts import pin_of
from app.openclaw_runtime import Boundary, obtain_facts
from app.runs import execute, json_value
from app.service import ident
from app.versions import write as write_version
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_draft01_postgres import accept, export_request
from test_flow01_postgres import agent, ask, closed_events, counts, current, executor, extraction as stub_extraction, hints, invoke, note_run, step
from test_perf02_r1_postgres import accounted_response, fact_rows, upload
from test_perf02_r3_postgres import stub
from test_perf02_r4_postgres import save_args, start_note_run

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

# One page: a qualitative passage, a verifiable quarterly amount, an unsupported scale and a table whose
# period header is printed BELOW its row (the stacked text PDF places line 13 under line 10), an unsupported
# layout; a second page repeats the table amount as plain lines. A header printed above the rows but extracted after them is verified in FLOW-01.03 (test_flow01_03_postgres).
MIXED = ['Synthetic', 'Консолидированная отчетность по МСФО', 'Бизнес компании', 'Компания поставляет оборудование клиентам.',
         'Выручка, млн руб.', '100', '3M23', 'Объем рынка', '2,2 трлн руб.',
         'Выручка по договорам 21 583 26 914 28 316', 'млн руб.', 'За 12 месяцев, закончившихся',
         '31 декабря 2021 31 декабря 2022 31 декабря 2023']
PLAIN = ['Выручка по договорам, млн руб.', '28 316', "12M'23", 'Выручка по договорам, млн руб.', '28 316', "12M'22"]
GOOD = dict(metric='Выручка', subject='Synthetic', reporting_basis='МСФО', nature='reported_actual',
            period=dict(kind='interval', label='3M23', start='2023-01-01', end='2023-03-31'),
            amount=dict(value='100', unit='money', currency='RUB'), value_text='100',
            evidence=[dict(page=1, line_start=5, line_end=7, area='Выручка за квартал')],
            basis=dict(page=1, metric_label='Выручка', unit_label='млн руб.', reporting_label='МСФО'))
QUAL = dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
            period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None,
            basis=None, evidence=[dict(page=1, line_start=3, line_end=4, area='Бизнес')])
TABLE = dict(metric='Выручка по договорам', subject='Synthetic', reporting_basis='МСФО', nature='reported_actual',
             period=dict(kind='interval', label="12M'2023", start='2023-01-01', end='2023-12-31'),
             amount=dict(value='28316', unit='money', currency='RUB'), value_text='28 316',
             evidence=[dict(page=1, line_start=10, line_end=13, area='Таблица выручки')],
             basis=dict(page=1, metric_label='Выручка по договорам', unit_label='млн руб.', reporting_label='МСФО'),
             table=dict(page=1, row_line=10, year_line=13, period_line_start=12, period_line_end=12, column=3))
TRLN = dict(metric='Объем рынка', subject='Synthetic', reporting_basis='МСФО', nature='reported_actual',
            period=dict(kind='interval', label='за 2023 год', start='2023-01-01', end='2023-12-31'),
            amount=dict(value='2.2', unit='money', currency='RUB'), value_text='2,2',
            evidence=[dict(page=1, line_start=8, line_end=9, area='Объем рынка')],
            basis=dict(page=1, metric_label='Объем рынка', unit_label='трлн руб.', reporting_label='МСФО'))
# A card page: two captioned amounts with their own period lines; the left caption is interleaved in the
# text order with the right one, so the derived left card cannot anchor its literal label.
HEADER = 'Источник: данные Synthetic на 31.12.2024'
CARDS = [HEADER, 'Выручка сегмента', 'Прибыль', '100 млн руб.', 'оборудования', 'за 2023 год', '200 млн руб.', 'за 2022 год']
CARD_POSITIONS = [(72, 800), (72, 760), (320, 740), (72, 720), (72, 740), (72, 700), (320, 720), (320, 700)]
BOND = [HEADER, 'Облигации', 'на 31.12.2024', '943 млн руб.']


def plain_closer(lines, label, year):
    start = 1 if year == 2023 else 4
    return dict(metric='Выручка по договорам', subject='Synthetic', reporting_basis='МСФО', nature='reported_actual',
                period=dict(kind='interval', label=label, start=f'{year}-01-01', end=f'{year}-12-31'),
                amount=dict(value='28316', unit='money', currency='RUB'), value_text='28 316',
                evidence=[dict(page=2, line_start=start, line_end=start + 2, area='Выручка за год')],
                basis=dict(page=2, metric_label='Выручка по договорам', unit_label='млн руб.', reporting_label='МСФО', reporting_page=1))


def passage(lines, start, end):
    return dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None), amount=None, value_text=None,
                basis=None, evidence=[dict(page=1, line_start=start, line_end=end, area='Карточки')])


def context(w, lines, positions=None, *, following=()):
    version = upload(w, 'flow02-' + ident('src')[-6:], lines, positions, following=following)
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    return version, run, pages, layouts


def ingest(w, run, pages, layouts, items, question, call_id=None):
    payload = dict(facts=deepcopy(items))
    original = deepcopy(payload)
    call_id = call_id or accounted_response(w, run, payload)
    outcome = ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts, question=question)
    assert payload == original
    return outcome, call_id


def summary(outcome):
    facts = sorted(((f['metric'], f['amount']['value'] if f['amount'] else None) for f in outcome['facts']),
                   key=lambda pair: (pair[0], pair[1] or ''))
    skipped = sorted((s['metric'], s['code'], s['candidate'], json_value(s['derived'])) for s in outcome['skipped'])
    return outcome['status'], facts, skipped


def checkpoint(w, run):
    with w.engine.connect() as c:
        return execute(c, 'SELECT checkpoint FROM aist.runs WHERE id=:id', id=run['id']).scalar_one()


def note(fact, text='Компания поставляет оборудование клиентам.'):
    ref = dict(source_version_id=fact['source']['source_version_id'], fact_id=fact['fact_id'])
    return dict(title='Synthetic: информационная справка', audience='Аналитик', sections=[dict(section_id='sec_business', title='Бизнес', blocks=[
        dict(block_id='block_business', kind='paragraph', claim=dict(text=text, origin='source_fact', fact_refs=[ref], calculation_ids=[]))])])


def coverage_findings(view):
    return [f for f in view['checks'][0]['findings'] if f['code'] == COVERAGE]


def human_edit(w, view, text):
    version = view['version']
    payload = deepcopy(version['payload'])
    payload['content']['sections'][0]['blocks'][0]['claim']['text'] = text
    status, edited = write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], None, 'saveVersion', ident('key'),
                                   dict(expected_revision=version['revision'], parent_version_id=version['id'], payload=payload))
    assert status == 201
    return edited


def extract(w, p, run, question, facts, directory, tool_id=None):
    """One accounted extraction through the tool path on the loopback provider; the same tool ID replays."""
    p.facts = facts
    args = dict(refs=[], extract=dict(source_version_id=run['input']['source_versions'][0]['source_version_id'], question=question))
    tool_id = tool_id or ident('tool')
    reply = w.runs.tool(run['id'], run['epoch'], tool_id, 'aist_get_facts', args,
                        obtain_facts=lambda current: obtain_facts(p.worker, current, args['extract'], directory / ident('d'), None))
    return reply, tool_id


def test_mixed_batch_keeps_usable_facts_and_records_each_exclusion_in_any_order(workspace):
    """Check 1: the correct number and the qualitative passage are saved, the unsupported table and scale are
    excluded with their reason, the order of the candidates changes nothing, and a repeat returns the same."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED)
    seen = []
    for number, order in enumerate(([GOOD, TABLE, QUAL, TRLN], [TRLN, QUAL, TABLE, GOOD]), 1):
        outcome, call_id = ingest(w, run, pages, layouts, order, 'Смешанный пакет ' + str(number))
        status, facts, skipped = summary(outcome)
        assert status == 'partial' and facts == [('Бизнес компании', None), ('Выручка', '100')]
        assert skipped == sorted([('Выручка по договорам', 'unsupported_layout', order.index(TABLE) + 1, 'null'),
                                  ('Объем рынка', 'ambiguous_link', order.index(TRLN) + 1, 'null')])
        by_code = {s['code']: s for s in outcome['skipped']}
        assert 'расположен не над строкой' in by_code['unsupported_layout']['message']
        assert by_code['ambiguous_link']['message'] and by_code['ambiguous_link']['reason'] == SKIP_REASONS['ambiguous_link']
        assert all(s['reason'] == SKIP_REASONS[s['code']] for s in outcome['skipped'])
        assert by_code['unsupported_layout']['observation'] == dict(metric='Выручка по договорам', subject='Synthetic', reporting_basis='МСФО',
            period=dict(kind='interval', label="12M'2023", start='2023-01-01', end='2023-12-31'), unit='money', currency='RUB')
        assert all(f['extraction_version'] == VERSION and f['source'] == pin_of(version) for f in outcome['facts'])
        # The same call ingested again: the same facts, status and reasons, no new rows.
        rows = fact_rows(w)
        again, _ = ingest(w, run, pages, layouts, order, 'Смешанный пакет ' + str(number), call_id)
        assert again == outcome and fact_rows(w) == rows
        seen.append({f['fact_id'] for f in outcome['facts']})
    assert seen[0] == seen[1] and len(fact_rows(w)) == 2
    numeric = next(f for f in outcome['facts'] if f['amount'])
    assert numeric['amount'] == dict(value='100', unit='money', currency='RUB', scale='1000000') and numeric['period']['label'] == '3M23'
    with w.engine.connect() as c:
        entries = completed_requests(w.runs, c, run, pin_of(version))
    assert len(entries) == 2 and all(e['status'] == 'partial' and len(e['skipped']) == 2 and len(e['fact_refs']) == 2 for e in entries)
    assert all(e['call_id'] and e['question'].startswith('Смешанный пакет') for e in entries)
    hint = collection.results_text(entries)
    assert hint.count('исключено кандидатов: 2 (ambiguous_link, unsupported_layout)') == 2
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_an_impossible_period_of_a_qualitative_candidate_is_excluded_before_the_save(workspace):
    """Check 1 on the archived defect: an instant period spanning a year on a qualitative passage excludes that
    passage with its reason instead of refusing the batch at the save; the other passage is saved."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED)
    spanning = dict(QUAL, period=dict(kind='instant', label='2027 г. (план запуска)', start='2027-01-01', end='2027-12-31'),
                    evidence=[dict(page=1, line_start=8, line_end=9, area='Планы')])
    outcome, _ = ingest(w, run, pages, layouts, [spanning, QUAL], 'Планы компании')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Бизнес компании', None)]
    assert skipped == [('Объем рынка', 'period_mismatch', 1, 'null')] and outcome['skipped'][0]['message'] == 'Некорректные границы периода.'
    assert outcome['skipped'][0]['observation']['period'] == spanning['period'] and len(fact_rows(w)) == 1
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_a_local_error_of_a_derived_card_excludes_that_card_only(workspace):
    """Check 2: the verified qualitative parent and the correct neighboring card are saved; the card whose
    literal caption cannot be anchored is excluded with its place, and its number reaches no calculation."""
    w = workspace
    version, run, pages, layouts = context(w, CARDS, CARD_POSITIONS)
    outcome, _ = ingest(w, run, pages, layouts, [passage(CARDS, 2, 8)], 'Сегменты')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Выручка сегмента', None), ('Прибыль', '200')]
    assert skipped == [('Выручка сегмента оборудования', 'ambiguous_link', 1, '{"page":1,"line_start":4,"line_end":4}')]
    assert 'отсутствует в источнике: metric' in outcome['skipped'][0]['message']
    neighbor = next(f for f in outcome['facts'] if f['amount'])
    assert neighbor['amount'] == dict(value='200', unit='money', currency='RUB', scale='1000000')
    assert neighbor['period'] == dict(kind='interval', label='за 2022 год', start='2022-01-01', end='2022-12-31')
    parent = next(f for f in outcome['facts'] if not f['amount'])
    assert parent['nature'] == 'source_statement' and parent['evidence'][0]['quote'] == '\n'.join(CARDS[1:8])
    rows = fact_rows(w)
    assert len(rows) == 2 and not any(row['data']['amount'] and row['data']['amount']['value'] == '100' for row in rows)
    # The excluded number has no fact: a calculation cannot reference it.
    refused = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_calculate', dict(
        formula_id='growth_percent.v1', rounding=dict(places=2, mode='HALF_UP'), inputs=[
            dict(name='previous', fact=dict(source_version_id=version['id'], fact_id=neighbor['fact_id'])),
            dict(name='current', fact=dict(source_version_id=version['id'], fact_id='fact_' + 'a' * 32))]))
    assert not refused['ok'] and refused['error']['code'] == 'resource_not_found'
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


def test_all_candidates_excluded_and_duplicates_only_count_as_no_progress(workspace, stub, tmp_path):
    """Check 3 and 5: a batch of excluded candidates is an empty result with its reasons, an admissible empty answer
    stays empty without reasons, neither proves the absence of data, and batches without new usable facts close
    the collection like refused ones."""
    w = workspace
    source = upload(w, 'flow02-empty', MIXED, following=[(['Чистый долг не раскрыт'], None), (['Logistics'], None)])
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path, 'synthetic')
    tool, = step(boundary, stub, agent(ask('Таблица и рынок')), 1)
    stub.queue = [stub_extraction([TABLE, TRLN])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] and reply['result']['status'] == 'empty' and reply['result']['facts'] == []
    assert [s['code'] for s in reply['result']['skipped']] == ['unsupported_layout', 'ambiguous_link']
    assert collection.state(current(w, run))['idle'] == 1 and fact_rows(w) == []
    tool, = step(boundary, stub, agent(ask('Чистый долг')), 2)
    assert 'извлечений выполнено 1 из 3' in hints(stub)[-1]
    stub.queue = [stub_extraction([])]
    reply = invoke(boundary, run, tool)
    assert reply['ok'] and reply['result'] == dict(facts=[], status='empty', skipped=[], coverage=reply['result']['coverage'])
    assert reply['result']['coverage']['pages'] == [1, 2]
    state = collection.state(current(w, run))
    assert state['idle'] == 2 and state['closed'] and state['reason'] == 'idle'
    assert [e['data']['reason'] for e in closed_events(w, run)] == ['idle'] and counts(w, run) == ((4, 0), (2, 0))
    tool, = step(boundary, stub, agent(ask('Выручка')), 3)
    hint = hints(stub)[-1]
    assert 'Режим завершения' in hint and '«Таблица и рынок»: материал не получен; исключено кандидатов: 2 (ambiguous_link, unsupported_layout)' in hint
    assert '«Чистый долг»: материал не получен' in hint and '«Чистый долг»: материал не получен; исключено' not in hint
    assert invoke(boundary, run, tool)['error']['code'] == 'budget_exceeded' and len(stub.received) == 5
    # A new Run: partial batches whose facts are all already saved are no progress either.
    run = note_run(w, source)
    boundary = Boundary(executor(w), run, tmp_path / 'duplicates', 'synthetic')
    first, second = step(boundary, stub, agent(ask('Пакет 1'), ask('Выручка')), 1)
    for tool, items, idle in ((first, [GOOD, QUAL, TABLE], 0), (second, [GOOD, TABLE], 1)):
        stub.queue = [stub_extraction(items)]
        reply = invoke(boundary, run, tool)
        assert reply['ok'] and reply['result']['status'] == 'partial' and len(reply['result']['skipped']) == 1
        assert collection.state(current(w, run))['idle'] == idle
    third, = step(boundary, stub, agent(ask('Бизнес')), 2)
    stub.queue = [stub_extraction([QUAL, TRLN])]
    reply = invoke(boundary, run, third)
    assert reply['ok'] and reply['result']['status'] == 'partial' and reply['result']['skipped'][0]['code'] == 'ambiguous_link'
    state = collection.state(current(w, run))
    assert state['idle'] == 2 and state['reason'] == 'idle' and len(fact_rows(w)) == 2
    assert counts(w, run) == ((5, 0), (3, 0))


@pytest.mark.parametrize('defect', ['schema', 'reference', 'foreign', 'sha', 'model_call', 'access', 'unknown', 'write'])
def test_strict_refusals_stay_atomic_after_a_local_exclusion(workspace, monkeypatch, defect):
    """Check 4: a correct candidate, a locally excluded one and then a strict violation: nothing is saved, the
    checkpoint keeps its state and the accounted model response is untouched."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED)
    pin = pin_of(version)
    good, bad = deepcopy(GOOD), None
    if defect == 'schema':
        bad = deepcopy(TABLE)
        bad['table']['column'] = '3'
    elif defect == 'reference':
        bad = deepcopy(GOOD)
        bad['evidence'][0]['line_end'] = 999
    elif defect == 'foreign':
        pin = pin_of(upload(w, 'flow02-other', MIXED))
    elif defect == 'sha':
        pin = dict(pin, sha256='0' * 64)
    payload = dict(facts=[TRLN, QUAL, good] + ([bad] if bad else []))
    original = deepcopy(payload)
    call_id = accounted_response(w, run, payload)
    if defect == 'model_call':
        call_id = 'call_absent'
    elif defect == 'access':
        with w.engine.begin() as c:
            execute(c, 'UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:s', s=w.session)
    elif defect == 'unknown':
        def broken(*args, **kwargs):
            raise RuntimeError('synthetic failure')
        monkeypatch.setattr(extraction, 'inspect_cards', broken)
    elif defect == 'write':
        saves = []
        real_save = extraction.save_fact

        def failing_save(*args, **kwargs):
            saves.append(1)
            if len(saves) == 2:
                raise ApiError('temporarily_unavailable')
            return real_save(*args, **kwargs)
        monkeypatch.setattr(extraction, 'save_fact', failing_save)
    before = checkpoint(w, run)
    with pytest.raises(RuntimeError if defect == 'unknown' else ApiError) as refused:
        ingest_payload(w.runs, run, pages, payload, call_id, pin=pin, layouts=layouts, question='Строгий отказ')
    if defect == 'schema':
        assert refused.value.code == 'validation_failed' and 'поле facts.3.table' in refused.value.body['error']['message']
    if defect == 'reference':
        assert refused.value.code == 'validation_failed'
        assert 'Факт 4 (Выручка): Строки основания отсутствуют.' in refused.value.body['error']['message']
    if defect in ('foreign', 'model_call'):
        assert refused.value.code == 'resource_not_found'
    if defect == 'sha':
        assert refused.value.code == 'validation_failed' and 'SHA не совпадают' in refused.value.body['error']['message']
    if defect == 'access':
        assert refused.value.code == 'unauthenticated'
    if defect == 'write':
        assert refused.value.code == 'temporarily_unavailable' and len(saves) == 2
    assert fact_rows(w) == [] and payload == original and checkpoint(w, run) == before
    with w.engine.connect() as c:
        if defect != 'model_call':
            call = execute(c, 'SELECT data FROM aist.model_calls WHERE id=:id', id=call_id).scalar_one()
            assert (call['transport_state'], call['provider_outcome'], call['delivery']) == ('observed', 'success', 'delivered')
        assert completed_requests(w.runs, c, run, pin_of(version)) == []


def test_replay_cache_reuse_and_an_old_record_return_the_same_outcome_without_a_post(workspace, provider, monkeypatch, tmp_path):
    """Check 5 and 7: the saved reply replays under the same tool ID, another Run with the same question gets the
    cached facts with the same status and reasons and records them, an entry without diagnostics is returned
    as it is, a changed processing version replays the fixed operation but presents no old cache as new."""
    w, p = workspace, provider
    version = upload(w, 'flow02-cache', MIXED)
    first = start_note_run(w, version)
    question = 'Выручка и бизнес компании'
    reply, tool_id = extract(w, p, first, question, [GOOD, TABLE, QUAL], tmp_path)
    assert reply['ok'] and reply['result']['status'] == 'partial' and [s['code'] for s in reply['result']['skipped']] == ['unsupported_layout']
    assert len(reply['result']['facts']) == 2 and len(p.sent) == 1
    again, _ = extract(w, p, first, question, [], tmp_path, tool_id)
    assert again == reply and len(p.sent) == 1
    # Another conversation, the same source and question: the cached result carries its limitations and is recorded.
    second = start_note_run(w, version)
    cached, _ = extract(w, p, second, question, [], tmp_path)
    assert cached['ok'] and cached['result'] == reply['result'] and len(p.sent) == 1
    recorded = checkpoint(w, second)['extractions']
    assert len(recorded) == 1 and recorded[0]['status'] == 'partial' and recorded[0]['question'] == question
    assert recorded[0]['call_id'] == checkpoint(w, first)['extractions'][0]['call_id']
    qualitative = next(f for f in cached['result']['facts'] if not f['amount'])
    saved = w.runs.tool(second['id'], second['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative)))
    assert saved['ok'], saved
    gaps = coverage_findings(saved['result'])
    assert len(gaps) == 1 and 'Часть запроса «' + question + '» не подтверждена: 1 из 1' in gaps[0]['message'] and 'unsupported_layout' in gaps[0]['message']
    assert saved['result']['review']['acceptance'] is None
    # An unrelated task of the same project: its own complete extraction, no inherited remark.
    third = start_note_run(w, version)
    own, _ = extract(w, p, third, 'Бизнес компании', [QUAL], tmp_path)
    assert own['ok'] and own['result']['status'] == 'complete' and own['result']['skipped'] == [] and len(p.sent) == 2
    clean = w.runs.tool(third['id'], third['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(own['result']['facts'][0])))
    assert clean['ok'] and coverage_findings(clean['result']) == [] and clean['result']['checks'][0]['status'] == 'passed'
    # A record saved before this diagnostics: its facts are returned without a status that was never checked.
    legacy = 'Прежний вопрос'
    with w.engine.begin() as c:
        cp = checkpoint(w, first)
        entry = dict(request_key=extraction.request_key(pin_of(version), legacy), source=pin_of(version), version=VERSION,
                     question=legacy, fact_refs=[dict(source_version_id=version['id'], fact_id=qualitative['fact_id'])], call_id=cp['extractions'][0]['call_id'])
        execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:id',
                v=json_value(dict(cp, extractions=[*cp['extractions'], entry])), id=first['id'])
    fourth = start_note_run(w, version)
    old, _ = extract(w, p, fourth, legacy, [], tmp_path)
    assert old['ok'] and old['result'] == dict(facts=[qualitative]) and len(p.sent) == 2
    # A new processing version: the fixed tool operation replays, the old cache is not presented as processed by it.
    monkeypatch.setattr(extraction, 'VERSION', 'pdf-lines.v99')
    assert extract(w, p, first, question, [], tmp_path, tool_id)[0] == reply and len(p.sent) == 2
    with w.engine.connect() as c:
        assert completed_requests(w.runs, c, fourth, pin_of(version), question) == []
    monkeypatch.undo()
    # Recovery of the first Run keeps the saved operation: a later Run still reads it without a POST.
    w.runs.recover(owned=first['worker_id'])
    assert w.runs.get(w.actor, w.project, first['id'])['state'] == 'recovery_required'
    fifth = start_note_run(w, version)
    assert extract(w, p, fifth, question, [], tmp_path)[0]['result'] == reply['result'] and len(p.sent) == 2


def test_a_draft_with_a_material_gap_is_saved_unaccepted_and_the_remark_survives_until_the_basis_is_fixed(workspace, tmp_path):
    """Check 6 and 7: the draft is saved with a blocking coverage remark; acceptance and the accepted release are
    refused; a reload and an unrelated edit keep the remark; an equal amount of another period closes nothing;
    the matching basis closes its gap and leaves the other one."""
    w = workspace
    version, run, pages, layouts = context(w, MIXED, following=[(PLAIN, None)])
    outcome, _ = ingest(w, run, pages, layouts, [GOOD, TABLE, QUAL, TRLN], 'Финансовые показатели')
    assert outcome['status'] == 'partial' and len(outcome['skipped']) == 2
    qualitative = next(f for f in outcome['facts'] if not f['amount'])
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(qualitative)))
    assert saved['ok'], saved
    view = saved['result']
    assert view['checks'][0]['status'] == 'passed' and view['review']['acceptance'] is None
    gaps = coverage_findings(view)
    assert [f['block_id'] for f in view['checks'][0]['findings']] == [None] and len(gaps) == 1
    assert '2 из 2' in gaps[0]['message'] and '(ambiguous_link, unsupported_layout)' in gaps[0]['message']
    assert 'не доказывает отсутствие сведений в источнике' in gaps[0]['message']
    with pytest.raises(ApiError) as blocked:
        accept(w, view, acknowledge_unchecked_semantics=False)
    assert blocked.value.code == 'validation_failed'
    with pytest.raises(ApiError) as release:
        Exports(w.runs).request(w.actor, w.session, w.project, view['version']['artifact_id'], view['version']['id'],
                                ident('key'), export_request('pdf', 'accepted'))
    assert release.value.code == 'acceptance_required'
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'completed'
    reloaded = w.runs.version(w.actor, w.project, view['version']['id'])
    assert coverage_findings(reloaded) == gaps
    edited = human_edit(w, view, 'Компания поставляет оборудование своим клиентам.')
    assert coverage_findings(edited) == gaps and edited['version']['provenance']['run_id'] is None
    with pytest.raises(ApiError):
        accept(w, edited, acknowledge_unchecked_semantics=False)
    # The same amount for another period closes nothing; the matching observation closes its gap only.
    later = start_note_run(w, version)
    ingest(w, later, pages, layouts, [plain_closer(PLAIN, "12M'22", 2022)], 'Выручка за 2022 год')
    still = human_edit(w, edited, 'Компания поставляет оборудование клиентам по всей стране.')
    assert coverage_findings(still) == gaps
    closing, _ = ingest(w, later, pages, layouts, [plain_closer(PLAIN, "12M'23", 2023)], 'Выручка за 2023 год')
    assert closing['status'] == 'complete' and closing['facts'][0]['period']['end'] == '2023-12-31'
    narrowed = human_edit(w, still, 'Компания поставляет оборудование клиентам.')
    remaining = coverage_findings(narrowed)
    assert len(remaining) == 1 and '1 из 2' in remaining[0]['message'] and '(ambiguous_link)' in remaining[0]['message']
    with pytest.raises(ApiError):
        accept(w, narrowed, acknowledge_unchecked_semantics=False)


def test_an_excluded_duplicate_of_an_admitted_observation_leaves_no_gap(workspace):
    """Check 7: the model's own wrong copy of a card is excluded, the card derived from the verified passage is
    saved with the same observation, so the draft carries no coverage remark."""
    w = workspace
    version, run, pages, layouts = context(w, BOND)
    duplicate = dict(metric='Облигации', subject='Synthetic', reporting_basis=HEADER, nature='reported_actual',
                     period=dict(kind='instant', label='на 31.12.2024', start='2024-12-31', end='2024-12-31'),
                     amount=dict(value='944', unit='money', currency='RUB'), value_text='943',
                     evidence=[dict(page=1, line_start=2, line_end=4, area='Облигации')],
                     basis=dict(page=1, metric_label='Облигации', unit_label='млн руб.', reporting_label=HEADER))
    outcome, _ = ingest(w, run, pages, layouts, [passage(BOND, 2, 4), duplicate], 'Облигации')
    status, facts, skipped = summary(outcome)
    assert status == 'partial' and facts == [('Облигации', None), ('Облигации', '943')]
    assert skipped == [('Облигации', 'number_mismatch', 2, 'null')]
    bonds = next(f for f in outcome['facts'] if f['amount'])
    assert bonds['period']['end'] == '2024-12-31' and bonds['reporting_basis'] == HEADER
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(note(bonds, 'Облигации размещены.')))
    assert saved['ok'], saved
    assert coverage_findings(saved['result']) == [] and saved['result']['checks'][0]['status'] == 'passed'
    status, review = accept(w, saved['result'])
    assert status == 200 and review['acceptance']
