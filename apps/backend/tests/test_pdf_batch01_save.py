"""Метаданные периода и качественные основания не прерывают проверку черновика."""
from copy import deepcopy

import pytest

from app.tools import period_date_numbers, row_remarks
from test_note_path05_r4 import change, qualitative, remarks
from test_note_path06_r3 import annual_fact


@pytest.mark.parametrize('label', [
    '2021-2023', '2005 - 12 мес. 2023 гг.', '2022 - 2023',
    '2016-2018 гг.', 'за 2016 - 2018 гг.', '1998 - 9 мес. 2018 гг.',
])
def test_range_metadata_does_not_parse_as_a_signed_amount(label):
    source = qualitative(['Компания Вектор выпускает оборудование.'],
                         dict(kind='interval', label=label, start='2016-01-01', end='2018-12-31'))
    assert not remarks('Компания Вектор выпускает оборудование.', [source])
    assert not remarks('Выручка за 2018 год составила 46 млн руб.', [source, annual_fact('46', 2018)])


@pytest.mark.parametrize('text,code', [
    ('Выручка за 2018 год составила 47 млн руб.', 'unit_mismatch'),
    ('Выручка за 2018 год составила -46 млн руб.', 'unit_mismatch'),
    ('Выручка за 2018 год составила -+46 млн руб.', 'ambiguous_sign'),
    ('Выручка за 2018 год составила 46 млн USD.', 'unit_mismatch'),
    ('Выручка за 2018 год составила 46 млрд руб.', 'unit_mismatch'),
    ('Выручка за 2017 год составила 46 млн руб.', 'period_mismatch'),
    ('Выручка за 1Q18 составила 46 млн руб.', 'period_mismatch'),
    ('Выручка за 2018 год составила 2016 млн руб.', 'unit_mismatch'),
    ('Выручка за 2018 год составила 73 млн руб.', 'unsupported_numbers'),
    ('Период 2016-2018.', 'ambiguous_sign'),
])
def test_period_metadata_does_not_authorize_claim_substitutions(text, code):
    source = qualitative(['Компания Вектор выпускает оборудование.'],
                         dict(kind='interval', label='2016-2018, показатель 73', start='2016-01-01', end='2018-12-31'))
    assert code in remarks(text, [source, annual_fact('46', 2018)])


def test_unknown_dates_are_not_inferred_from_label():
    source = qualitative(['Компания Вектор выпускает оборудование.'],
                         dict(kind='unknown', label='1998 - 9 мес. 2018 гг.', start=None, end=None))
    before = deepcopy(source)
    assert period_date_numbers(source['period']) == set()
    assert not remarks('Компания Вектор выпускает оборудование.', [source])
    assert 'period_mismatch' in remarks('Компания работала в 2018 году.', [source])
    assert source == before


def test_calculation_input_metadata_uses_dates_without_changing_basis():
    calc, inputs = change('80')
    for source in inputs.values():
        source['period']['label'] = '2014 - 2019 гг.'
    before = deepcopy((calc, inputs))
    assert not remarks('Выручка снизилась на 20%.', calculations=[calc], inputs=inputs)
    assert 'unit_mismatch' in remarks('Выручка выросла на 20%.', calculations=[calc], inputs=inputs)
    assert (calc, inputs) == before


@pytest.mark.parametrize('value', ['0', '46'])
def test_qualitative_amount_is_not_zero_or_a_numeric_basis(value):
    source = qualitative(['Компания Вектор выпускает оборудование.'])
    source['metric'] = 'Выручка'
    claim = dict(label=source['metric'], amount=dict(value=value, unit='money', scale='1000000', currency='RUB'),
                 period_label='Не указано', calculation_ids=[])
    before = deepcopy((claim, source))
    result = row_remarks(None, None, None, claim, [source], [])
    assert result == [('row_basis_mismatch',
                      f'Сумма {value} млн RUB и период «Не указано» не совпадают с основанием. '
                      'Качественное основание не содержит числовой суммы.')]
    assert (claim, source) == before
    source['amount'] = dict(claim['amount'], value='0')
    result = row_remarks(None, None, None, claim, [source], [])
    assert bool(result) == (value != '0')
    if result:
        assert 'Основание: 0 млн RUB' in result[0][1]
