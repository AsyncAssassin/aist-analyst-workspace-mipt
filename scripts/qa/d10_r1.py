"""D10-R1: real PostgreSQL saves and offline compatibility of the accepted snapshot.

Importing d10_boundaries first runs the existing eight D10 integration groups.
All transport responses there are synthetic loopback responses.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import d10_boundaries as qa
from app.tools import resolve_basis
from app.normalization import fingerprint
from app.errors import ApiError

records = []


def source_pdf(previous, current):
    writer = qa.PdfWriter(); page = writer.add_blank_page(595, 842)
    font = qa.DictionaryObject({qa.NameObject('/Type'): qa.NameObject('/Font'),
        qa.NameObject('/Subtype'): qa.NameObject('/Type1'), qa.NameObject('/BaseFont'): qa.NameObject('/Helvetica')})
    page[qa.NameObject('/Resources')] = qa.DictionaryObject({qa.NameObject('/Font'):
        qa.DictionaryObject({qa.NameObject('/F1'): writer._add_object(font)})})
    lines = ['Synthetic Company', 'Revenue, million RUB', 'IFRS', previous, "9M'21", current, "9M'22"]
    stream = qa.DecodedStreamObject()
    stream.set_data(('BT /F1 14 Tf 72 760 Td ' + ' 0 -24 Td '.join('(' + x + ') Tj' for x in lines) + ' ET').encode())
    page[qa.NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO(); writer.write(out); return out.getvalue()


def fixture(previous='123', current='140'):
    with patch.object(qa, 'pdf', return_value=source_pdf(previous, current)):
        run, _ = qa.setup()
    with qa.engine.begin() as conn: pages = qa.source_pages(qa.runs, conn, run)
    items = deepcopy(qa.items)
    for item, value, year in zip(items, (previous, current), (2021, 2022)):
        item.update(value_text=value)
        item['amount']['value'] = value
        item['period'].update(label=f'9M’{str(year)[2:]}', start=f'{year}-01-01', end=f'{year}-09-30')
    call = qa.ledger.reserve(run['id'], run['epoch'], 'extraction')
    qa.ledger.observe(run['id'], run['epoch'], call['id'], 200, qa.VALID)
    facts = qa.ingest(qa.runs, run, pages, {'facts': items}, call['id'])
    refs = [{'source_version_id': f['source']['source_version_id'], 'fact_id': f['fact_id']} for f in facts]
    return run, facts, refs


def payload(text, refs, calculations):
    return {'result_type': 'analytical_note', 'schema_version': '1.0.0', 'content': {
        'title': 'D10-R1 synthetic sign check', 'audience': 'QA', 'sections': [{
            'section_id': 'section_sign', 'title': 'Signed value', 'blocks': [{
                'block_id': 'block_sign', 'kind': 'paragraph', 'claim': {
                    'text': text, 'origin': 'calculation' if calculations else 'source_fact',
                    'fact_refs': refs, 'calculation_ids': calculations}}]}]}}


def counts(run):
    with qa.engine.connect() as conn:
        return {table: qa.execute(conn, f'SELECT count(*) FROM aist.{table} WHERE project_id=:p',
                                 p=run['project_id']).scalar_one()
                for table in ('artifacts', 'artifact_versions')}


def save_case(name, run, facts, refs, calculation, good, bad):
    calculations = [calculation['calculation_id']] if calculation else []
    expected = {'facts': sorted(facts, key=lambda f: (f['source']['source_version_id'], f['fact_id'])),
                'calculations': [calculation] if calculation else []}
    refusals = []
    for index, text in enumerate(bad):
        before = counts(run)
        reply = qa.runs.tool(run['id'], run['epoch'], f'call_bad_{index}', 'aist_save_artifact',
                             qa.args(payload(text, refs, calculations)))
        after = counts(run)
        assert not reply['ok'] and reply['error']['code'] == 'validation_failed', reply
        assert before == after == {'artifacts': 0, 'artifact_versions': 0}
        with qa.engine.connect() as conn:
            state = qa.row(conn, 'SELECT state, result_version_id FROM aist.runs WHERE id=:r', r=run['id'])
        assert state == {'state': 'running', 'result_version_id': None}
        refusals.append({'text': text, 'reply': reply, 'counts_before': before, 'counts_after': after, 'run': state})
    saved_payload = payload(good, refs, calculations)
    reply = qa.runs.tool(run['id'], run['epoch'], 'call_good', 'aist_save_artifact', qa.args(saved_payload))
    assert reply['ok'], reply
    view = reply['result']; version = view['version']
    assert version['payload'] == saved_payload and version['basis'] == expected
    assert version['content_sha256'] == fingerprint({'payload': saved_payload, 'basis': expected})
    read = qa.runs.version(qa.user['user_id'], run['project_id'], version['id'])
    assert read == view
    assert [check['status'] for check in view['checks']] == ['passed', 'not_checked']
    assert view['review']['acceptance'] is None
    assert counts(run) == {'artifacts': 1, 'artifact_versions': 1}
    records.append({'case': name, 'refusals': refusals, 'saved': view, 'read_back_identical': True,
                    'basis_identical_to_saved_facts_and_calculation': True, 'passed': True})
    print(name + ' passed', flush=True)


for negative in (False, True):
    good_values = ['13.82', '+13,82'] if not negative else ['-13.82', '−13,82']
    for good in good_values:
        run, facts, refs = fixture(current='106' if negative else '140')
        calculation = qa.runs.tool(run['id'], run['epoch'], 'call_calc', 'aist_calculate', {
            'formula_id': 'growth_percent.v1', 'inputs': [{'name': name, 'fact': ref}
                for name, ref in zip(('previous', 'current'), refs)], 'rounding': {'places': 2, 'mode': 'HALF_UP'}})['result']
        assert calculation['rounded']['value'] == ('-13.82' if negative else '13.82')
        wrong = ['13.82', '+13,82'] if negative else ['-13.82', '-13,82', '−13.82', '−13,82', '- 13.82']
        wrong += ['--13.82', '+-13,82']
        save_case('calculation_' + good, run, facts, [], calculation,
                  f'Рост выручки составил {good}%.', [f'Рост выручки составил {v}%.' for v in wrong])

for negative in (False, True):
    value = '-1234.56' if negative else '1234.56'
    for separator in (' ', '\u00a0', '\u202f'):
        run, facts, refs = fixture(previous=value)
        sign = '−' if negative else '+'
        good = f'Выручка за 9M’21 с 2021-01-01 по 2021-09-30: {sign}1{separator}234,56 млн RUB.'
        wrong = ['1234.56', '+1234,56'] if negative else ['-1234.56', '−1 234,56']
        save_case('fact_' + value + '_' + hex(ord(separator)), run, [facts[0]], [refs[0]], None,
                  good, [f'Выручка {v} млн RUB.' for v in wrong])

run, facts, refs = fixture(previous='4586', current='7193')
save_case('periods_dates_grouped_positive', run, facts, refs, None,
          'Выручка 9M’21: 4 586 млн RUB; 9M’22: 7 193 млн RUB. Периоды с 2021-01-01 по 2021-09-30 и с 2022-01-01 по 2022-09-30.', [])

# Replay only the persistence lookups from the immutable snapshot. The real
# resolve_basis validation executes unchanged, with no live database or model.
path = Path('/input/version.json'); raw = path.read_bytes()
view = json.loads(raw); version = view['version']; saved_basis = version['basis']
facts_by_ref = {(f['source']['source_version_id'], f['fact_id']): f for f in saved_basis['facts']}
calcs = {calc['calculation_id']: calc for calc in saved_basis['calculations']}
def fact_lookup(service, conn, run, ref):
    return {'data': facts_by_ref[(ref['source_version_id'], ref['fact_id'])]}
def calc_lookup(conn, query, **params):
    return {'reply': {'result': calcs[params['id']]}}
with patch('app.tools.resolve_fact', side_effect=fact_lookup), patch('app.tools.row', side_effect=calc_lookup):
    resolved = resolve_basis(SimpleNamespace(service=None), None,
                             {'id': 'snapshot', 'project_id': 'snapshot', 'owner_id': 'snapshot'}, version['payload'])
assert resolved == saved_basis
assert fingerprint({'payload': version['payload'], 'basis': resolved}) == version['content_sha256']
assert path.read_bytes() == raw
compatibility = {'passed': True, 'method': 'real resolve_basis with snapshot-backed read-only lookups',
    'file_sha256': hashlib.sha256(raw).hexdigest(), 'version_id': version['id'],
    'content_sha256': version['content_sha256'], 'basis_identical': True, 'source_unchanged': True,
    'model_calls': 0}
(qa.OUT / 'whoosh-compatibility.json').write_text(json.dumps(compatibility, ensure_ascii=False, indent=2) + '\n')
(qa.OUT / 'signed-number-saves.json').write_text(json.dumps({'passed': True, 'external_model_posts': 0,
    'saved_cases': len(records), 'rejected_saves': sum(len(r['refusals']) for r in records), 'records': records}, ensure_ascii=False, indent=2) + '\n')
qa.engine.dispose()
print(json.dumps({'saved_cases': len(records), 'rejected_saves': sum(len(r['refusals']) for r in records),
                  'whoosh_compatible': True, 'external_model_posts': 0}))
