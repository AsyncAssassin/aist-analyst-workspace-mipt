"""Local period labels through the normal extraction transport and ingest in a test database."""
import io
import os
from copy import deepcopy
from dataclasses import replace

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.openclaw_runtime import obtain_facts
from app.accounting import Accounting
from app.errors import ApiError
from app.exports import Exports
from app.extraction import AMBIGUOUS_CARD, VERSION, candidate, completed_requests, ingest as ingest_payload, source_pages
from app.facts import pin_of, save_fact
from app.normalization import fingerprint
from app.runs import execute, json_value
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_perf02 import repeated_cards

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')

HEADER='Источник: данные Synthetic на 31.12.2024'
PAGE_DATE=dict(kind='instant',label='на 31.12.2024',start='2024-12-31',end='2024-12-31')
YEAR_2023=dict(kind='interval',label='за 2023 год',start='2023-01-01',end='2023-12-31')


def glyph_name(char):
    """Adobe glyph list names of Cyrillic letters; Ё/ё sit between Е/е and Ж/ж."""
    code=ord(char)
    if 0x410<=code<=0x415:return 'afii%d'%(10017+code-0x410)
    if 0x416<=code<=0x42F:return 'afii%d'%(10024+code-0x416)
    if 0x430<=code<=0x435:return 'afii%d'%(10065+code-0x430)
    if 0x436<=code<=0x44F:return 'afii%d'%(10072+code-0x436)
    return {0x401:'afii10023',0x451:'afii10071'}[code]


def cyrillic_pdf(lines, positions=None, *, following=()):
    """A text PDF whose Type1 encoding maps high bytes to named Cyrillic glyphs.

    following holds further (lines, positions) pages that share the font."""
    sheets=[(lines,positions),*following]
    chars=sorted({char for sheet,_ in sheets for line in sheet for char in line if ord(char)>127})
    codes={char:128+index for index,char in enumerate(chars)}
    writer=PdfWriter()
    differences=ArrayObject([NumberObject(128),*(NameObject('/'+glyph_name(char)) for char in chars)])
    encoding=DictionaryObject({NameObject('/Type'):NameObject('/Encoding'),NameObject('/BaseEncoding'):NameObject('/WinAnsiEncoding'),
                               NameObject('/Differences'):differences})
    font=writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                           NameObject('/BaseFont'):NameObject('/Helvetica'),NameObject('/Encoding'):writer._add_object(encoding)}))
    def encoded(line):
        out=bytearray()
        for char in line:
            byte=codes.get(char,ord(char))
            if byte in (0x28,0x29,0x5c):out+=b'\\'
            out.append(byte)
        return bytes(out)
    for sheet,placement in sheets:
        page=writer.add_blank_page(width=595,height=842)
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject()
        if placement is None:
            content = b'16 TL 72 760 Td ' + b' T* '.join(b'(' + encoded(line) + b') Tj' for line in sheet)
        else:
            assert len(placement) == len(sheet)
            content = b' '.join(f'1 0 0 1 {x} {y} Tm '.encode() + b'(' + encoded(line) + b') Tj'
                                for line, (x, y) in zip(sheet, placement))
        stream.set_data(b'BT /F1 12 Tf ' + content + b' ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    data=io.BytesIO();writer.write(data);return data.getvalue()


def upload(w,title,lines,positions=None,*,following=()):
    version=w.service.write(w.actor,'uploadSource',ident('key'),dict(title=title,expected_revision=0),w.project,None,
                            cyrillic_pdf(lines,positions,following=following),title+'.pdf')[1]['version']
    with w.engine.connect() as c:
        stored=list(execute(c,'SELECT text FROM aist.source_pages WHERE version_id=:v ORDER BY page',v=version['id']).scalars())
    assert stored==['\n'.join(lines),*('\n'.join(sheet) for sheet,_ in following)]
    return version


def qualitative():
    return dict(metric=None,subject='Synthetic',reporting_basis='Не указано',nature='source_statement',
                period=dict(kind='unknown',label='Не указано',start=None,end=None),amount=None,value_text=None,basis=None,
                evidence=[dict(page=1,line_start=1,line_end=4,area='Карточка')])


def numeric_claim(period):
    return dict(metric='Выручка',subject='Synthetic',reporting_basis=HEADER,nature='reported_actual',period=period,
                amount=dict(value='100',unit='money',currency='RUB'),value_text='100',
                evidence=[dict(page=1,line_start=1,line_end=4,area='Карточка')],
                basis=dict(page=1,metric_label='Выручка',unit_label='млн руб.',reporting_label=HEADER))


def ingest(w,p,source,tmp_path,question,facts):
    p.facts=facts
    send(w,conversation(w),body(sources=[source['id']],question='Проверка локальных подписей периода'))
    run=w.runs.claim('perf02_r1')
    args=dict(refs=[],extract=dict(source_version_id=source['id'],question=question))
    reply=w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_get_facts',args,
        obtain_facts=lambda current:obtain_facts(p.worker,current,args['extract'],tmp_path/ident('d'),None))
    w.runs.stop(run['id'],run['epoch'],'validation_failed')
    return reply


def excluded_only(reply,code,text):
    """FLOW-01.02: the only candidate is excluded with this reason; the extraction completed without usable facts."""
    assert reply['ok'] and reply['result']['status']=='empty' and reply['result']['facts']==[], reply
    assert [s['code'] for s in reply['result']['skipped']]==[code] and reply['result']['skipped'][0]['message']
    if text is not None:
        assert text in reply['result']['skipped'][0]['message']
    from app.extraction import SKIP_REASONS
    assert reply['result']['skipped'][0]['reason'] == SKIP_REASONS[code]


def saved_amounts(w,source):
    with w.engine.connect() as c:
        return sorted((f['metric'],f['amount']['value'],f['period']['kind'],f['period']['label'],f['period']['start'],f['period']['end'],f['nature'])
                      for f in execute(c,'SELECT data FROM aist.facts WHERE source_version_id=:v',v=source['id']).scalars() if f['amount'])


def test_forecast_and_annual_labels_through_ingest(workspace,provider,tmp_path):
    w,p=workspace,provider
    forecast=upload(w,'forecast',[HEADER,'Прогноз на 2025 год','Выручка','100 млн руб.'])
    annual=upload(w,'annual',[HEADER,'за 2023 год','Выручка','100 млн руб.'])
    reply=ingest(w,p,forecast,tmp_path,'Прогноз выручки',[qualitative()])
    assert reply['ok'] and [f['amount'] for f in reply['result']['facts']]==[None]
    assert saved_amounts(w,forecast)==[]
    reply=ingest(w,p,forecast,tmp_path,'Прогноз выручки числом',[numeric_claim(PAGE_DATE)])
    excluded_only(reply,'ambiguous_link',None)
    assert saved_amounts(w,forecast)==[]
    reply=ingest(w,p,annual,tmp_path,'Выручка за год с датой страницы',[numeric_claim(PAGE_DATE)])
    excluded_only(reply,'ambiguous_link',None)
    assert saved_amounts(w,annual)==[]
    reply=ingest(w,p,annual,tmp_path,'Выручка за год',[qualitative()])
    assert reply['ok'] and sorted(f['amount'] is None for f in reply['result']['facts'])==[False,True]
    expected=[('Выручка','100','interval','за 2023 год','2023-01-01','2023-12-31','reported_actual')]
    assert saved_amounts(w,annual)==expected
    reply=ingest(w,p,annual,tmp_path,'Выручка за год числом',[numeric_claim(YEAR_2023)])
    assert reply['ok'] and reply['result']['facts'][0]['period']==YEAR_2023
    # The model's own numeric item is a second extraction item with the same observation.
    assert saved_amounts(w,annual)==expected*2
    assert len(p.sent)==5 and all(r['thinking']=={'type':'enabled'} for r in p.sent)


REPEATED_POSITIONS = [(72, 800), (72, 730), (72, 710), (320, 730), (320, 710), (72, 620), (72, 600)]


def accounted_response(w, run, payload):
    """An accounted extraction response carrying this payload, with no provider transport."""
    ledger = Accounting(w.runs)
    call = ledger.reserve(run['id'], run['epoch'], 'extraction')
    response = dict(id='synthetic-quality02', choices=[dict(
        finish_reason='stop', message=dict(role='assistant', content=json_value(payload)))],
        usage=dict(prompt_tokens=100, completion_tokens=50))
    assert ledger.observe(run['id'], run['epoch'], call['id'], 200, response)
    return call['id']


def quality_context(w):
    """A new synthetic source and an accounted response, with no provider transport."""
    from test_perf02_r4_postgres import start_note_run

    w.settings = replace(w.settings, embeddings_enabled=False)
    w.runs.settings = w.settings
    text, _, qualitative_item, direct = repeated_cards()
    version = upload(w, 'quality02', text[1].splitlines(), REPEATED_POSITIONS)
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    payload = dict(facts=[direct, qualitative_item])
    return version, run, pages, layouts, payload, accounted_response(w, run, payload)


def two_page_context(w):
    """The F03 source: one unambiguous card on page 1, the repeated cards and their neighbor on page 2.

    The direct candidate quotes the page 1 number and only the company line of page 2."""
    from test_perf02_r4_postgres import start_note_run

    w.settings = replace(w.settings, embeddings_enabled=False)
    w.runs.settings = w.settings
    text, _, qualitative_item, direct = repeated_cards()
    first = ['Источник: данные Synthetic на 30.06.2024', 'Средняя ставка', '9,1%']
    version = upload(w, 'quality02-r1', first, [(72, 800), (72, 730), (72, 710)],
                     following=[(text[1].splitlines(), REPEATED_POSITIONS)])
    run = start_note_run(w, version)
    with w.engine.connect() as c:
        pages = source_pages(w.runs, c, run)
        layouts = source_pages(w.runs, c, run, layout=True)
    single = dict(direct, amount=dict(direct['amount'], value='9.1'), value_text='9,1',
                  evidence=[dict(page=1, line_start=3, line_end=3, area='Ставка'),
                            dict(page=2, line_start=1, line_end=1, area='Наименование компании')])
    passage = dict(qualitative_item, evidence=[dict(page=2, line_start=1, line_end=7, area='Показатели')])
    payload = dict(facts=[single, passage])
    return version, run, pages, layouts, payload, accounted_response(w, run, payload)


def fact_rows(w):
    with w.engine.connect() as c:
        return list(execute(c, 'SELECT * FROM aist.facts WHERE project_id=:p ORDER BY fact_id', p=w.project).mappings())


def test_ambiguous_mixed_ingest_preserves_old_fact_replay_and_draft_boundary(workspace):
    from test_perf02_r4_postgres import paragraph, ref, save_args
    from test_draft01_postgres import accept, export_request

    w = workspace
    version, run, pages, layouts, payload, call_id = quality_context(w)
    original = deepcopy(payload)
    pin = pin_of(version)
    question = 'Средняя ставка и размер портфеля'
    # Earlier fixtures use each version's actual pin/item identity and limitations: the v7 ID
    # covered pin and item only, the QUALITY-02 v8 ID added the version and limitations, and the
    # QUALITY-02-R1 v9 check still saved this balance with an annual label as qualitative evidence.
    # They are created in this empty test database, never read from a working database.
    passage_item = dict(payload['facts'][1], metric=pages[1].splitlines()[0])
    earlier = []
    for old_version in ('pdf-lines.v7', 'pdf-lines.v8', 'pdf-lines.v9'):
        if old_version == 'pdf-lines.v9':
            contradicting = dict(payload['facts'][0], period=dict(payload['facts'][0]['period'], label='за 2024 год'))
            old, _ = candidate(pin, pages, payload['facts'][0], layouts)
            old['period'] = contradicting['period']
            old_item = dict(contradicting, nature='source_statement', amount=None, value_text=None, basis=None)
            old_item.pop('table')
        else:
            old, _ = candidate(pin, pages, payload['facts'][1], layouts)
            old_item = passage_item
        old['extraction_version'] = old_version
        if old_version == 'pdf-lines.v7':
            old['limitations'].remove(AMBIGUOUS_CARD)
            old['fact_id'] = 'fact_' + fingerprint(dict(pin=pin, item=old_item))[:32]
        else:
            old['fact_id'] = 'fact_' + fingerprint(dict(pin=pin, item=old_item, version=old_version, limitations=old['limitations']))[:32]
        request = dict(source=pin, version=old_version, question=question, call_id=call_id,
                       request_key=fingerprint(dict(source=pin, version=old_version, question=question.casefold())),
                       fact_refs=[dict(source_version_id=version['id'], fact_id=old['fact_id'])])
        earlier.append((old, request))
    with w.engine.begin() as c:
        for old, _ in earlier:
            save_fact(w.service, c, w.actor, w.project, pin, old)
        execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:id',
                v=json_value(dict(extractions=[request for _, request in earlier])), id=run['id'])
        assert completed_requests(w.runs, c, run, pin, question) == []
    previous = deepcopy(fact_rows(w))
    assert len(previous) == 3
    outcome = ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts, question=question)
    facts = outcome['facts']
    assert payload == original and outcome['status'] == 'complete' and outcome['skipped'] == []
    assert len(facts) == 3
    numeric = [f for f in facts if f['amount'] is not None]
    qualitative_facts = [f for f in facts if f['amount'] is None]
    assert [(f['metric'], f['amount']['value'], f['amount']['unit']) for f in numeric] == [('Размер портфеля', '560', 'money')]
    assert len(qualitative_facts) == 2
    assert all(f['nature'] == 'source_statement' and f['limitations'].count(AMBIGUOUS_CARD) == 1 for f in qualitative_facts)
    assert all(f['source'] == pin and f['extraction_version'] == VERSION for f in facts)
    rows = fact_rows(w)
    assert len(rows) == 6 and all(row in rows for row in previous)
    assert {f['fact_id'] for f in facts}.isdisjoint({old['fact_id'] for old, _ in earlier})
    # The v9 evidence with the contradicting label stays as it was and is never re-issued as verified.
    assert [row['data']['extraction_version'] for row in rows if row['data']['period']['label'] == 'за 2024 год'] == ['pdf-lines.v9']
    assert all(row['provenance']['semantic_status'] == 'not_checked' for row in rows)
    assert all(row['calculation_basis'] == {} for row in rows if row['data']['amount'] is None)
    assert ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts, question=question) == outcome
    assert fact_rows(w) == rows and payload == original
    with w.engine.connect() as c:
        completed = completed_requests(w.runs, c, run, pin, question)
        assert len(completed) == 1 and completed[0]['version'] == VERSION
        assert {ref['fact_id'] for ref in completed[0]['fact_refs']}.isdisjoint({old['fact_id'] for old, _ in earlier})
        assert execute(c, 'SELECT count(*) FROM aist.embedding_calls WHERE owner_id=:owner', owner=w.actor).scalar_one() == 0
    # Only the new qualitative evidence supports this percent. No legacy numeric fact
    # or correct neighbor enters the draft's basis.
    qualitative_fact = next(f for f in qualitative_facts if f['metric'] == 'Средняя ставка')
    content = dict(title='Проверка основания ставки', audience='Аналитик', sections=[
        dict(section_id='section_rates', title='Ставка', blocks=[
            paragraph('block_rate', 'Средняя ставка составила 13,6%.', [ref(version, qualitative_fact['fact_id'])])])])
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert saved['ok'], saved
    view = saved['result']
    assert view['checks'][0]['status'] == 'passed'
    assert not any(f['severity'] == 'error' for f in view['checks'][0]['findings'])
    assert view['checks'][1]['status'] == 'not_checked'
    assert view['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['text'] == 'Средняя ставка составила 13,6%.'
    assert [f['fact_id'] for f in view['version']['basis']['facts']] == [qualitative_fact['fact_id']]
    assert view['review']['acceptance'] is None
    with pytest.raises(ApiError) as refused:
        accept(w, view, acknowledge_unchecked_semantics=False)
    assert refused.value.code == 'validation_failed'
    with pytest.raises(ApiError) as refused:
        Exports(w.runs).request(w.actor, w.session, w.project, view['version']['artifact_id'], view['version']['id'],
                               ident('key'), export_request('pdf', 'accepted'))
    assert refused.value.code == 'acceptance_required'


# The unchanged guarantee: a broken reference, structure, accounting, source state, layout or origin refuses the
# whole batch before any write. FLOW-01.02 changed the expectation for a content defect of one candidate: its
# number, unit, period or label excludes that candidate with a classified reason and the admissible ones are saved.
STRICT_DEFECTS = {'reference': None, 'structure': None, 'unaccounted': 'resource_not_found', 'layout': None,
                  'unavailable': 'source_not_ready', 'foreign': 'resource_not_found'}
LOCAL_DEFECTS = {'number': 'number_mismatch', 'period': 'period_mismatch', 'label': 'period_mismatch',
                 'annual_label': 'period_mismatch', 'iso_date': 'period_mismatch', 'unit': 'unit_mismatch'}


@pytest.mark.parametrize('defect', [*LOCAL_DEFECTS, *STRICT_DEFECTS])
def test_ambiguous_ingest_keeps_strict_refusals_and_batch_atomicity(workspace, monkeypatch, defect):
    """The erroneous element follows the admissible ones: a strict defect refuses the whole batch before any
    write; a local content defect excludes only that candidate and saves the rest in one transaction."""
    w = workspace
    version, run, pages, layouts, payload, call_id = quality_context(w)
    bad = deepcopy(payload['facts'][0])
    if defect == 'number':
        bad['amount']['value'] = '99'
    elif defect == 'reference':
        bad['evidence'][0]['line_end'] = 999
    elif defect == 'period':
        bad['period'].update(label='на 30.06.2023', start='2023-06-30', end='2023-06-30')
    elif defect == 'label':
        # The confirmed dates stay, only the label names another date.
        bad['period']['label'] = 'на 30.06.2023'
    elif defect == 'annual_label':
        # R1-F01: an annual flow instead of the confirmed balance, the dates unchanged.
        bad['period']['label'] = 'за 2024 год'
    elif defect == 'iso_date':
        # R1-F01: another calendar date of the same year.
        bad['period']['label'] = '2024-12-31'
    elif defect == 'unit':
        bad['amount']['unit'] = 'money'
    elif defect == 'structure':
        bad['unexpected'] = True
    elif defect == 'unaccounted':
        call_id = 'call_absent'
    elif defect == 'layout':
        layouts = {1: layouts[1] + '\nИзмененная страница'}
    elif defect == 'unavailable':
        monkeypatch.setattr(w.service, 'inspect', lambda c, version: (None, 'missing'))
    pin = pin_of(version)
    if defect == 'foreign':
        other = upload(w, 'other-quality02', pages[1].splitlines())
        pin = pin_of(other)
    payload['facts'].append(bad)
    original = deepcopy(payload)
    with w.engine.connect() as c:
        before = execute(c, 'SELECT checkpoint FROM aist.runs WHERE id=:id', id=run['id']).scalar_one()
    if defect in STRICT_DEFECTS:
        with pytest.raises(ApiError) as refused:
            ingest_payload(w.runs, run, pages, payload, call_id, pin=pin, layouts=layouts, question='Проверка отказа')
        assert refused.value.code == (STRICT_DEFECTS[defect] or 'validation_failed')
        assert fact_rows(w) == [] and payload == original
        with w.engine.connect() as c:
            assert execute(c, 'SELECT checkpoint FROM aist.runs WHERE id=:id', id=run['id']).scalar_one() == before
    else:
        outcome = ingest_payload(w.runs, run, pages, payload, call_id, pin=pin, layouts=layouts, question='Проверка отказа')
        assert payload == original and outcome['status'] == 'partial' and len(outcome['facts']) == 3
        excluded, = outcome['skipped']
        assert (excluded['candidate'], excluded['metric'], excluded['code']) == (3, 'Средняя ставка', LOCAL_DEFECTS[defect])
        if defect in ('label', 'annual_label', 'iso_date'):
            assert 'подпись' in excluded['message'] and excluded['observation']['period']['label'] == bad['period']['label']
        assert not any(f['amount'] and f['metric'] == 'Средняя ставка' for f in outcome['facts'])
        assert len(fact_rows(w)) == 3
        with w.engine.connect() as c:
            recorded = execute(c, 'SELECT checkpoint FROM aist.runs WHERE id=:id', id=run['id']).scalar_one()['extractions']
        assert recorded[-1]['status'] == 'partial' and recorded[-1]['skipped'] == outcome['skipped']
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')


@pytest.mark.parametrize('variant', ['short_caption', 'equivalent_date', 'iso_date', 'both'])
def test_short_caption_and_equivalent_date_keep_the_mixed_batch(workspace, variant):
    """F01 and F02 through ingest on modified copies of the inputs, not on original model responses."""
    w = workspace
    version, run, pages, layouts, payload, call_id = quality_context(w)
    direct = payload['facts'][0]
    if variant in ('short_caption', 'both'):
        direct['metric'] = 'ставка'
    if variant != 'short_caption':
        direct['period']['label'] = '2024-06-30' if variant == 'iso_date' else '30.06.2024'
        payload['facts'].reverse()
    original = deepcopy(payload)
    outcome = ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts)
    facts = outcome['facts']
    assert payload == original and len(facts) == 3 and outcome['status'] == 'complete'
    numeric = [f for f in facts if f['amount'] is not None]
    assert [(f['metric'], f['amount']['value'], f['amount']['unit']) for f in numeric] == [('Размер портфеля', '560', 'money')]
    qualitative_facts = [f for f in facts if f['amount'] is None]
    assert sorted(f['metric'] for f in qualitative_facts) == sorted([direct['metric'], pages[1].splitlines()[0]])
    assert all(f['nature'] == 'source_statement' and f['limitations'].count(AMBIGUOUS_CARD) == 1
               and f['extraction_version'] == VERSION and f['source'] == pin_of(version) for f in qualitative_facts)
    rate = next(f for f in qualitative_facts if f['metric'] == direct['metric'])
    assert rate['period'] == direct['period'] and rate['raw_text'] == rate['evidence'][0]['quote'] == 'Средняя ставка\n13,6%'
    rows = fact_rows(w)
    assert len(rows) == 3 and all(row['calculation_basis'] == {} for row in rows if row['data']['amount'] is None)
    assert not any(row['data']['amount'] and row['data']['amount']['unit'] == 'percent' for row in rows)
    assert ingest_payload(w.runs, run, pages, payload, call_id, layouts=layouts) == outcome
    assert fact_rows(w) == rows and payload == original


def test_two_page_source_keeps_the_unambiguous_number_with_an_additional_reference(workspace):
    """F03 through ingest: an extra company reference on another page, in either order, keeps the numeric fact."""
    w = workspace
    version, run, pages, layouts, payload, call_id = two_page_context(w)
    saved = []
    for order in (list, reversed):
        ordered = deepcopy(payload)
        ordered['facts'][0]['evidence'] = list(order(ordered['facts'][0]['evidence']))
        call_id = accounted_response(w, run, ordered)
        original = deepcopy(ordered)
        outcome = ingest_payload(w.runs, run, pages, ordered, call_id, layouts=layouts)
        facts = outcome['facts']
        assert ordered == original and len(facts) == 3 and outcome['status'] == 'complete'
        rate = next(f for f in facts if f['metric'] == 'Средняя ставка')
        assert rate['nature'] == 'reported_actual' and rate['amount'] == dict(value='9.1', unit='percent', currency=None, scale='1')
        assert AMBIGUOUS_CARD not in rate['limitations'] and rate['raw_text'] == '9,1'
        assert [e['locator']['pdf_page'] for e in rate['evidence']][:2] == [e['page'] for e in ordered['facts'][0]['evidence']]
        passage = next(f for f in facts if f['amount'] is None)
        assert passage['nature'] == 'source_statement' and passage['limitations'].count(AMBIGUOUS_CARD) == 1
        assert [(f['metric'], f['amount']['value']) for f in facts if f['amount'] and f['metric'] != 'Средняя ставка'] == [('Размер портфеля', '560')]
        assert ingest_payload(w.runs, run, pages, ordered, call_id, layouts=layouts) == outcome
        saved.append(facts)
    rows = fact_rows(w)
    # The reversed citation order is a different input; the passage and the neighbor are shared.
    assert len(rows) == 4 and {f['fact_id'] for facts in saved for f in facts} == {row['fact_id'] for row in rows}
    for label in ('30.06.2024', '2024-06-30'):
        equivalent = deepcopy(payload)
        equivalent['facts'][0]['period']['label'] = label
        facts = ingest_payload(w.runs, run, pages, equivalent, accounted_response(w, run, equivalent), layouts=layouts)['facts']
        rate = next(f for f in facts if f['metric'] == 'Средняя ставка')
        assert rate['amount']['value'] == '9.1' and rate['period']['label'] == label and AMBIGUOUS_CARD not in rate['limitations']
    rows = fact_rows(w)
    assert len(rows) == 6
    # FLOW-01.02: a contradicting label excludes the rate with its reason; the passage and the neighbor stay saved.
    for label in ('на 30.06.2023', 'за 2024 год', '2024-12-31'):
        contradicting = deepcopy(payload)
        contradicting['facts'][0]['period']['label'] = label
        call_id = accounted_response(w, run, contradicting)
        outcome = ingest_payload(w.runs, run, pages, contradicting, call_id, layouts=layouts)
        assert outcome['status'] == 'partial' and [s['code'] for s in outcome['skipped']] == ['period_mismatch']
        assert outcome['skipped'][0]['observation']['period']['label'] == label
        assert sorted(((f['metric'], f['amount']['value'] if f['amount'] else None) for f in outcome['facts']), key=str) == sorted(
            [('Размер портфеля', '560'), (pages[2].splitlines()[0], None)], key=str)
        assert fact_rows(w) == rows
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
