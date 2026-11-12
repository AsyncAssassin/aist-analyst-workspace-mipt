"""Whole-page selection preserves dependencies; lexical candidates alone do not prove completeness."""
from app.extraction import extraction_request
from app.extraction_context import PAGE_LINK, select_pages
import json
import pytest


def test_all_matches_and_nonadjacent_support_keep_original_lines():
    pages = {1: 'IFRS\nRevenue, million RUB\n2023 2024\n10 20\nsee page 9',
             2: 'Table continued\n30 40', 3: 'Unrelated business', 9: 'Footnote 7\nUnaudited estimates'}
    selected, context = select_pages(pages, 'Revenue')
    assert list(selected) == [1, 2, 9]
    assert not context['full_document'] and context['fallback'] is None
    data = json.loads(extraction_request(selected, 'Revenue')['messages'][-1]['content'])
    assert data['pages'][-1] == {'page': 9, 'lines': [{'line': 1, 'text': 'Footnote 7'}, {'line': 2, 'text': 'Unaudited estimates'}]}
    assert pages[3] == 'Unrelated business'
    many = {page: 'Revenue' for page in range(1, 13)}
    assert len(select_pages(many, 'Revenue')[0]) == 12


def test_unmatched_compound_part_and_zero_hits_keep_the_whole_document():
    pages = {1: 'Revenue 100', 2: 'Operating data'}
    for question in ('Revenue and EBITDA', 'Cashflows'):
        selected, coverage = select_pages(pages, question)
        assert selected == pages and coverage['fallback'] and coverage['full_document']
    assert select_pages({1: 'Revenue. see page 99', 2: 'Operations'}, 'Revenue')[1]['full_document']


def test_existing_table_headers_pull_in_a_nonadjacent_continuation():
    from test_d14 import PAGES, LAYOUT
    # Reuse the stage-03 aligned table, without changing its geometry or the ingest checks.
    pages = {1: PAGES[1], 2: 'Unrelated page', 7: PAGES[1].replace('Выручка', 'Расходы').replace('МСФО', '    ')}
    selected, coverage = select_pages(pages, 'Выручка', {1: LAYOUT[1], 7: LAYOUT[1].replace('Выручка', 'Расходы').replace('МСФО', '    ')})
    assert 7 in selected
    assert any('заголовками периодов' in reason for row in coverage['reasons'] if row['page']==7 for reason in row['reasons'])


@pytest.mark.parametrize('reference,expected', [
    ('см. стр. 3-4', [1, 2, 3, 4]), ('см. стр. 4-6', [1, 2, 4, 5, 6]), ('см. стр. 4', [1, 2, 4]),
    ('см. стр4', [1, 2, 4]), ('см. стр.4', [1, 2, 4]),
])
def test_explicit_page_range_preserves_every_referenced_page(reference, expected):
    pages = {1: 'Synthetic company', 2: 'Revenue\n100\n' + reference,
             3: 'All amounts are in million RUB.',
             4: 'The figures include a one-time sale and are not audited.',
             5: 'Related data', 6: 'Further qualification', 7: 'Contacts'}
    selected, coverage = select_pages(pages, 'Revenue')
    assert list(selected) == expected
    assert coverage['fallback'] is None and coverage['unresolved'] == []
    assert selected[4] == pages[4]


@pytest.mark.parametrize('phrase', ['см. строку Выручка', 'см. структуру выпуска'])
def test_non_page_text_does_not_create_a_dependency_or_expand_context(phrase):
    pages = {1: 'Company', 2: 'Revenue\n' + phrase, 3: 'Contacts'}
    selected, coverage = select_pages(pages, 'Revenue')
    assert PAGE_LINK.findall(pages[2]) == []
    assert list(selected) == [1, 2]
    assert coverage['unresolved'] == [] and coverage['fallback'] is None
    assert not coverage['full_document']
    sent = json.loads(extraction_request(selected, 'Revenue')['messages'][-1]['content'])
    assert sent['pages'][1] == dict(page=2, lines=[dict(line=1, text='Revenue'), dict(line=2, text=phrase)])


@pytest.mark.parametrize('reference', ['4-3', '3-99', '0-3', '3-', '-4', '3-x', 'x', '99'])
def test_invalid_link_keeps_an_explicit_limitation_without_clipping(reference):
    pages = {1: 'Synthetic', 2: 'Revenue\nсм. стр. ' + reference, 3: 'Units', 4: 'Qualification'}
    selected, coverage = select_pages(pages, 'Revenue')
    assert selected == pages and coverage['unresolved'] and coverage['fallback']
    assert not any('Явная ссылка' in reason for entry in coverage['reasons'] for reason in entry['reasons'])


def test_a_missing_interior_page_does_not_resolve_a_range_by_its_endpoints():
    pages = {1: 'Revenue. см. стр. 3-5', 3: 'Units', 5: 'Qualification'}
    selected, coverage = select_pages(pages, 'Revenue')
    assert selected == pages and coverage['unresolved']


def test_existing_full_word_page_reference_remains_supported():
    pages = {1: 'Revenue. см. страницу 4', 2: 'Contacts', 4: 'Qualification'}
    selected, coverage = select_pages(pages, 'Revenue')
    assert list(selected) == [1, 4] and not coverage['unresolved']
