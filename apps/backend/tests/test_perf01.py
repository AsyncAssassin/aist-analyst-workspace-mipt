"""Model-only compaction and source-derived qualitative labels."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from app.errors import ApiError
from app.extraction import candidate
from app.model_context import model_view
from app.openclaw_runtime import tool_schema
from test_d10 import PIN, PAGES, item


def test_compact_facts_preserve_values_sources_qualifications_and_every_quote():
    fact, _ = candidate(PIN, PAGES, item())
    second = deepcopy(fact)
    second['fact_id'] = 'fact_other'
    second['limitations'].append('Исключены отдельные операции.')
    value = dict(base_version=dict(basis=dict(facts=[fact, second])))
    original = deepcopy(value)
    compact = model_view(value)['base_version']['basis']
    assert value == original
    assert len(compact['passages']) < sum(len(f['evidence']) for f in (fact, second))
    for old, new in zip((fact, second), compact['facts']):
        for field in ('fact_id', 'metric', 'subject', 'reporting_basis', 'nature', 'period', 'amount'):
            assert old[field] == new[field]
        assert compact['fact_sources'][new['source_version_id']] == old['source']
        assert [compact['limitations'][key] for key in new['limitation_ids']] == old['limitations']
        for evidence, key in zip(old['evidence'], new['evidence_ids']):
            entry = compact['evidence'][key]
            passage = compact['passages'][entry['passage']]
            assert entry['evidence_id'] == evidence['evidence_id']
            assert entry['area'] == evidence['locator']['area']
            assert passage['source_version_id'] == evidence['source']['source_version_id']
            assert passage['page'] == evidence['locator']['pdf_page']
            start, stop = entry.get('quote_span', [0, len(passage['quote'])])
            assert evidence['quote'] == passage['quote'][start:stop]


def test_equal_evidence_ids_in_different_sources_do_not_collide():
    fact, _ = candidate(PIN, PAGES, item())
    other = deepcopy(fact)
    other['source']['source_version_id'] = 'sv_other'
    for evidence in other['evidence']:
        evidence['source']['source_version_id'] = 'sv_other'
    compact = model_view(dict(facts=[fact, other]))
    assert set(compact['facts'][0]['evidence_ids']).isdisjoint(compact['facts'][1]['evidence_ids'])


def test_flat_model_schema_rejects_historical_string_extract():
    schema = Draft202012Validator(tool_schema('aist_get_facts', source_versions=[PIN]))
    schema.validate(dict(question='Условия финансирования'))
    schema.validate(dict(refs=[dict(source_version_id=PIN['source_version_id'], fact_id='fact_test')]))
    for invalid in (dict(extract='<parameter=question>Условия', refs=[]), dict(question={'question': 'Условия'})):
        assert list(schema.iter_errors(invalid))
    multi = tool_schema('aist_get_facts', source_versions=[PIN, dict(PIN, source_version_id='sv_other')])
    assert multi['properties']['source_version_id']['enum'] == ['sv_test', 'sv_other']


def test_qualitative_label_is_literal_and_wrong_numeric_label_still_fails():
    data = item()
    data.update(metric=None, amount=None, value_text=None, basis=None, nature='source_statement')
    fact, _ = candidate(PIN, PAGES, data)
    assert fact['metric'] == PAGES[1].splitlines()[0]
    assert fact['amount'] is None
    data = item(); data['metric'] = None
    with pytest.raises(ApiError):
        candidate(PIN, PAGES, data)


def test_non_pdf_evidence_keeps_its_existing_view():
    fact, _ = candidate(PIN, PAGES, item())
    fact['evidence'][0]['locator'] = {'json_pointer': '/value'}
    assert model_view(dict(facts=[fact])) == dict(facts=[fact])


def test_only_complete_primary_passage_can_be_quoted_even_in_a_shared_context():
    from app.facts import source_excerpt_text
    from app.normalization import checked

    numeric, _ = candidate(PIN, PAGES, item())
    fact = deepcopy(numeric)
    passage = 'Проект может получить 15 млн руб. после согласования.'
    fact.update(nature='source_statement', amount=None, raw_text=passage)
    primary = fact['evidence'][0]
    primary['quote'] = passage
    neighbor = deepcopy(primary)
    neighbor.update(evidence_id='ev_neighbor', quote='Другая компания получила 40 млн руб. ' + passage)
    fact['evidence'].append(neighbor)
    compact = model_view(dict(facts=[fact]))
    view = compact['facts'][0]
    entry = compact['evidence'][view['excerpt_evidence_id']]
    start, end = entry['quote_span']
    assert compact['passages'][entry['passage']]['quote'][start:end] == passage
    assert source_excerpt_text(fact).endswith('«' + passage + '»')
    assert source_excerpt_text(dict(fact, evidence=[neighbor, primary])) is None
    assert 'excerpt_evidence_id' not in model_view(dict(facts=[numeric]))['facts'][0]
    for raw in ('От 50%', 'Доли собственников\n99,9% 0,1%\nПервая компания и вторая компания'):
        assert source_excerpt_text(dict(fact, raw_text=raw, evidence=[dict(primary, quote=raw)])) is None
    claim = dict(origin='source_excerpt', text='Цитата из PDF', fact_refs=[dict(source_version_id='sv_doc',fact_id='fact_one')], calculation_ids=[])
    assert checked('Claim',claim) == claim
    with pytest.raises(ApiError):
        checked('AnswerContent',dict(claims=[claim],missing_information=[]))


@pytest.mark.parametrize('raw', [
    '2020 2021 2022 2023 2024\nNet debt 0,1 1,9 -2,2 3,1 2,7\nNet debt/EBITDA 0,1 0,4 -0,2 0,3 0,3\nOWC -7,4 -13,8 -16,3 -17,5 -20,8',
    '2024 2025\nВыручка 120 150\nЧистая прибыль 12 15',
])
def test_financial_grid_keeps_evidence_but_cannot_be_a_new_flat_excerpt(raw):
    from app.facts import source_excerpt_text

    fact, _ = candidate(PIN, PAGES, item())
    fact.update(nature='source_statement', amount=None, raw_text=raw)
    fact['evidence'][0]['quote'] = raw
    before = deepcopy(fact)
    compact = model_view(dict(facts=[fact]))
    assert compact['facts'][0]['evidence_form'] == 'numeric_table'
    assert 'excerpt_evidence_id' not in compact['facts'][0]
    assert any(p['quote'] == raw for p in compact['passages'].values())
    assert source_excerpt_text(fact) is None
    assert source_excerpt_text(fact, retained=True) == '«' + ' '.join(raw.split()) + '»'
    assert fact == before
