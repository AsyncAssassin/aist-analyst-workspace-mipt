"""Independent chart layouts: source fragments, geometry and shared explicit/derived checks."""
from copy import deepcopy
import json

import pytest

from app import charts
from app.errors import ApiError
from app.extraction import candidate, extraction_request
from test_d10 import PIN


def grid(objects):
    rows = [''] * (max(y for _, _, y in objects) + 1)
    for text, x, y in sorted(objects, key=lambda v: (v[2], v[1])):
        assert x >= len(rows[y])
        rows[y] += ' ' * (x - len(rows[y])) + text
    return '\n'.join(rows)


def example(multiple=False, *, reverse=False, shift=0, vertical=0, change=None):
    """Two distinct small examples, including joined source headings and values at different heights."""
    names = ['Денежный поток, млн руб.', 'Операционная прибыль, тыс. руб.'] if multiple else ['Операционные расходы, тыс. руб.']
    values = [['130,9', '-12,8', '7,4'], ['91,6', '21,3', '18,2']] if multiple else [['-8,0', '9,2', '14,7']]
    labels = ['2018', '1Q19', '9M20'] if multiple else ['2017', '3M18', '6M19']
    objects = []
    for index, (name, numbers) in enumerate(zip(names, values)):
        left = 5 + index * 89 if multiple else 10
        objects.append((name, left, 2))
        for column, (raw, label) in enumerate(zip(numbers, labels)):
            center = left + 2 + column * 20
            objects.append((raw, round(center - len(raw) / 2), 4 + column * 2))
            objects.append((label, center - 2, 12))
    if change:
        objects = change(objects)
    # One original line holds all titles, another the first two values despite their different heights.
    headings = [text for text, _, y in objects if y == 2]
    body = [text for text, _, y in objects if y != 2]
    original = [' '.join(headings), *body]
    if reverse:
        original.reverse()
    moved = [(text, x + shift, y + vertical) for text, x, y in objects]
    return {1: '\n'.join(original), 2: 'Vector Industries\nМСФО'}, {1: grid(moved), 2: 'Vector Industries\nМСФО'}


def claim(pages, layouts, metric=None, index=0, *, qualitative=False):
    graphs = charts.context(pages[1].splitlines(), layouts[1])
    chart = next(g for g in graphs if metric is None or g['metric'] == metric)
    obs = chart['observations'][index]
    refs = [dict(page=1, line_start=f['lines'][0], line_end=f['lines'][0], area='Исходный фрагмент')
            for f in [chart['heading'], obs['value'], obs['label']]]
    refs += [dict(page=2, line_start=1, line_end=2, area='Эмитент и отчетная база')]
    return dict(metric=None if qualitative else chart['metric'], subject='Vector Industries', reporting_basis='МСФО',
                nature='source_statement' if qualitative else 'reported_actual',
                period=dict(kind='unknown', label='Не указан', start=None, end=None) if qualitative else obs['period'],
                amount=None if qualitative else dict(value=obs['value']['text'].replace(',', '.'), unit='money', currency='RUB'),
                value_text=None if qualitative else obs['value']['text'], evidence=refs,
                basis=None if qualitative else dict(page=1, metric_label=chart['metric'], unit_label=chart['unit'],
                                                    reporting_label='МСФО', reporting_page=2))


def refusal(pages, layouts, item):
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, item, layouts)
    return error.value.skip_code


def separate_claim(pages, raw, unit='тыс. руб.'):
    start = pages[1].splitlines().index('Доходы от услуг, ' + unit) + 1
    return dict(metric='Доходы от услуг', subject='Vector Industries', reporting_basis='МСФО',
                nature='reported_actual', period=dict(kind='interval', label='6M19', start='2019-01-01', end='2019-06-30'),
                amount=dict(value=raw.replace(',', '.'), unit='money', currency='RUB'), value_text=raw,
                evidence=[dict(page=1, line_start=start, line_end=start + 2, area='Самостоятельный блок'),
                          dict(page=2, line_start=1, line_end=2, area='Эмитент и отчетная база')],
                basis=dict(page=1, metric_label='Доходы от услуг', unit_label=unit,
                           reporting_label='МСФО', reporting_page=2))


@pytest.mark.parametrize('raw', ['8,6', '9,2', '14,7'])
@pytest.mark.parametrize('before', [False, True])
def test_independent_observation_is_not_owned_by_an_equal_chart_number(raw, before):
    pages, layouts = example()
    block = 'Доходы от услуг, тыс. руб.\n' + raw + '\n6M19'
    for source in (pages, layouts):
        source[1] = block + '\n\n' + source[1] if before else source[1] + '\n\n' + block
    item = separate_claim(pages, raw)
    original = deepcopy(item)
    fact, _ = candidate(PIN, pages, item, layouts)
    assert (fact['metric'], fact['amount']['value'], fact['period']) == (item['metric'], raw.replace(',', '.'), item['period'])
    assert any(e['quote'] == block for e in fact['evidence'])
    assert item == original


def test_quoted_chart_contradiction_cannot_escape_through_a_duplicate_standalone_block():
    pages, layouts = example()
    item = claim(pages, layouts, index=1)
    item['period'] = dict(kind='interval', label='6M19', start='2019-01-01', end='2019-06-30')
    block = 'Операционные расходы, тыс. руб.\n9,2\n6M19'
    for source in (pages, layouts):
        source[1] += '\n\n' + block
    # Even a complete separate quotation cannot override the chart caption already cited.
    start = len(pages[1].splitlines()) - 2
    item['evidence'].append(dict(page=1, line_start=start, line_end=start + 2, area='Повторные подписи'))
    assert refusal(pages, layouts, item) == 'period_mismatch'


@pytest.mark.parametrize('multiple', [False, True])
@pytest.mark.parametrize('reverse,shift,vertical', [(False, 0, 0), (True, 0, 0), (True, 23, 5)])
def test_relations_survive_object_order_and_translation(multiple, reverse, shift, vertical):
    pages, layouts = example(multiple, reverse=reverse, shift=shift, vertical=vertical)
    graphs = charts.context(pages[1].splitlines(), layouts[1])
    assert len(graphs) == (2 if multiple else 1)
    for graph in graphs:
        for index in range(3):
            item = claim(pages, layouts, graph['metric'], index)
            before = deepcopy(item)
            fact, basis = candidate(PIN, pages, item, layouts)
            assert item == before
            assert fact['metric'] == graph['metric'] and fact['period'] == item['period']
            assert fact['amount']['value'] == item['amount']['value'] and basis
            assert all(e['quote'] in pages[e['locator']['pdf_page']] for e in fact['evidence'])
    request = extraction_request(pages, 'Показатели', layouts)
    context = json.loads(request['messages'][1]['content'])
    assert context['pages'][0]['chart_layout'] == graphs
    assert [line['text'] for line in context['pages'][0]['lines']] == pages[1].splitlines()


@pytest.mark.parametrize('mutation,code', [
    (lambda c: c.update(metric='Прибыль'), 'ambiguous_link'),
    (lambda c: c.update(metric='Операционная прибыль'), 'ambiguous_link'),
    (lambda c: c['basis'].update(metric_label='Операционная прибыль'), 'unit_mismatch'),
    (lambda c: c['basis'].update(unit_label='тыс. руб.'), 'unit_mismatch'),
    (lambda c: c['amount'].update(value='12.8'), 'number_mismatch'),
    (lambda c: c['amount'].update(currency='USD'), None),
    (lambda c: c['period'].update(label='2019', start='2019-01-01', end='2019-12-31'), 'period_mismatch'),
    (lambda c: c['period'].update(label='1Q19', start='2019-01-01', end='2019-06-30'), 'period_mismatch'),
    (lambda c: c['evidence'].pop(2), 'ambiguous_link'),
])
def test_numeric_substitutions_do_not_cross_series_or_fragments(mutation, code):
    pages, layouts = example(True)
    item = claim(pages, layouts, index=1)
    mutation(item)
    with pytest.raises(ApiError) as error:
        candidate(PIN, pages, item, layouts)
    if code:
        assert error.value.skip_code == code


@pytest.mark.parametrize('change', [
    lambda objects: [entry for entry in objects if entry[0] != '1Q19'],
    lambda objects: [*objects, ('65,1', 25, 10)],
    lambda objects: [(text, x + 89 if text == '-12,8' else x, y + 1 if text == '-12,8' else y) for text, x, y in objects],
])
def test_missing_period_multiple_series_and_real_movement_are_not_textual_links(change):
    pages, layouts = example(True)
    item = claim(pages, layouts, index=1)
    changed_pages, changed_layouts = example(True, change=change)
    # The same immutable text may have another geometry; an equal text order cannot override it.
    if '1Q19' in changed_pages[1]:
        assert refusal(pages, changed_layouts, item) == 'ambiguous_link'
    assert not any(g['metric'] == 'Денежный поток' for g in charts.context(changed_pages[1].splitlines(), changed_layouts[1]))


def test_repeated_amounts_need_their_own_period_and_do_not_authorize_uncited_values():
    change = lambda objects: [('9,2' if text == '-8,0' else text, x, y) for text, x, y in objects]
    pages, layouts = example(change=change)
    item = claim(pages, layouts, index=1, qualitative=True)
    before = deepcopy(item)
    derived = list(charts.derived(pages, layouts, item))
    assert len(derived) == 1 and derived[0][0]['period']['label'] == '3M18'
    assert candidate(PIN, pages, derived[0][0], layouts)[0]['amount']['value'] == '9.2'
    item['evidence'].pop(2)
    assert list(charts.derived(pages, layouts, item)) == []
    assert before['amount'] is None


def test_qualitative_derivation_uses_the_explicit_numeric_verifier():
    pages, layouts = example(True)
    numeric = claim(pages, layouts, index=2)
    quoted = claim(pages, layouts, index=2, qualitative=True)
    derived = list(charts.derived(pages, layouts, quoted))
    assert len(derived) == 1
    explicit, _ = candidate(PIN, pages, numeric, layouts)
    implicit, _ = candidate(PIN, pages, derived[0][0], layouts)
    for key in ('metric', 'amount', 'period', 'subject', 'reporting_basis', 'raw_text', 'limitations'):
        assert explicit[key] == implicit[key]
    quoted['evidence'].pop(1)
    assert list(charts.derived(pages, layouts, quoted)) == []


def test_forecast_cannot_use_adjacent_text_fallback():
    pages, layouts = example(change=lambda objects: [(text.replace('6M19', '6M19 (прогноз)'), x - 5 if text == '6M19' else x, y)
                                                     for text, x, y in objects])
    item = claim(pages, layouts, index=2)
    item['period']['label'] = '6M19'
    assert refusal(pages, layouts, item) == 'period_mismatch'
    qualitative = claim(pages, layouts, index=2, qualitative=True)
    assert list(charts.derived(pages, layouts, qualitative)) == []


def test_debt_balance_is_a_closing_date_not_a_flow():
    pages, layouts = example(change=lambda objects: [(text.replace('Операционные расходы', 'Чистый долг'), x, y)
                                                     for text, x, y in objects])
    item = claim(pages, layouts, index=2)
    assert item['period']['kind'] == 'instant' and item['period']['start'] == item['period']['end'] == '2019-06-30'
    assert candidate(PIN, pages, item, layouts)[0]['period'] == item['period']
    item['period'].update(kind='interval', start='2019-01-01')
    assert refusal(pages, layouts, item) == 'period_mismatch'


def test_ambiguous_headings_do_not_fall_back_to_text_order():
    pages, layouts = example(True)
    item = claim(pages, layouts, index=1)
    shifted = {1: layouts[1].replace('Денежный поток, млн руб.', '     Денежный поток, млн руб.'), 2: layouts[2]}
    # The raw value and original label still exist, but the heading no longer assigns the axes.
    assert refusal(pages, shifted, item) == 'ambiguous_link'


def test_ambiguous_series_keeps_its_separate_neighbor():
    pages, layouts = example(True, change=lambda objects: [*objects, ('65,1', 25, 10)])
    assert [g['metric'] for g in charts.context(pages[1].splitlines(), layouts[1])] == ['Операционная прибыль']
    item = claim(pages, layouts, 'Операционная прибыль', 1)
    assert candidate(PIN, pages, item, layouts)[0]['amount']['value'] == '21.3'


def test_same_named_competing_plot_is_not_hidden_by_an_unsupported_column():
    def change(objects):
        return [(text.replace('Операционная прибыль, тыс. руб.', 'Денежный поток, млн руб.'), x, y)
                for text, x, y in objects] + [('65,1', 114, 10)]
    pages, layouts = example(True)
    item = claim(pages, layouts, index=1)
    pages, layouts = example(True, change=change)
    assert charts.context(pages[1].splitlines(), layouts[1]) == []
    assert refusal(pages, layouts, item) == 'ambiguous_link'


def test_invalid_source_year_does_not_crash_the_request_builder():
    assert charts.period('6M0000', 'Выручка') is None
    pages, layouts = example(change=lambda objects: [(text.replace('6M19', '6M0000'), x, y) for text, x, y in objects])
    assert extraction_request(pages, 'Показатели', layouts)['messages']


def test_chart_partition_search_stays_within_the_small_supported_class():
    assert charts.partitions([(0, (0, 10, 'Выручка, млн руб.'))] * 20, [(0, (0, 4, '2024'))] * 100) is None
