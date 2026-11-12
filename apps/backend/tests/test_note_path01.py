"""Standalone qualitative passages keep their wording and the existing card boundaries."""
from copy import deepcopy

import pytest

from app.errors import ApiError
from app.extraction import APART, candidate
from test_d14 import PIN
from test_flow01_03 import placed
from test_flow01_04 import passage


COMPANY = 'Vector Works'
FIRST = ['Обслуживание оборудования', 'Завод не', 'обслуживает оборудование зимой.',
         'Аварийные заявки принимаются круглый год.']
SECOND = ['Работа с дилерами', 'Поставки через региональных партнеров',
          'Условия зависят от региона.']
NOTE = 'Примечание: сведения относятся к действующим договорам.'
SOURCE = 'Источник: данные Vector Works'


def document(side_by_side):
    pages = {1: '\n'.join([*FIRST, *SECOND, SOURCE, NOTE]), 2: COMPANY}
    if side_by_side:
        layout = '\n'.join(placed((7, left), (83, right))
                           for left, right in zip(FIRST, [*SECOND, '']))
    else:
        layout = '\n'.join([*FIRST, '', '', *SECOND])
    return pages, {1: layout + '\n\n' + SOURCE + '\n' + NOTE, 2: COMPANY}


def item(pages, lines):
    references = [dict(page=2, line_start=1, line_end=1, area='Компания'),
                  dict(page=1, line_start=8, line_end=8, area='Источник'),
                  dict(page=1, line_start=9, line_end=9, area='Сноска')]
    return passage(pages[1], lines, *references, subject=COMPANY)


@pytest.mark.parametrize('side_by_side', [False, True])
@pytest.mark.parametrize('lines', [FIRST, FIRST[1:], SECOND, [FIRST[0]], [SECOND[1]]])
def test_complete_paragraph_heading_and_list_item_keep_exact_text(side_by_side, lines):
    pages, layouts = document(side_by_side)
    payload = item(pages, lines)
    before = deepcopy(payload)
    fact, _ = candidate(PIN, pages, payload, layouts)
    assert fact['raw_text'] == '\n'.join(lines)
    assert fact['metric'] == lines[0] and fact['amount'] is None
    assert {COMPANY, SOURCE, NOTE, '\n'.join(lines)} <= {e['quote'] for e in fact['evidence']}
    assert payload == before


@pytest.mark.parametrize('side_by_side', [False, True])
@pytest.mark.parametrize('disguise', [False, True])
def test_foreign_properties_are_not_admitted_under_another_heading(side_by_side, disguise):
    pages, layouts = document(side_by_side)
    payload = item(pages, [FIRST[0]])
    # A gap and an area called a footnote do not turn another card's properties into a reference.
    payload['evidence'].insert(1, dict(page=1, line_start=6, line_end=7,
                                     area='Сноска' if disguise else 'Свойства'))
    before = deepcopy(payload)
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, payload, layouts)
    assert error.value.skip_code == 'ambiguous_link'
    assert error.value.body['error']['message'] == APART
    assert payload == before
