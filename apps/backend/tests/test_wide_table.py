"""Граница широкой таблицы, без догадки о периоде или сдвига столбца."""
import pytest

from app import tables
from test_flow01_03 import placed, centered


@pytest.mark.parametrize('count', [11, 12])
def test_wide_year_table_keeps_first_and_last_columns(count):
    years = [str(2010+i) for i in range(count)]
    values = [str(100+i) for i in range(count)]
    header = placed(*(centered(year, 50+20*i) for i, year in enumerate(years)))
    row = placed((0,'Выручка, млрд руб.'), *(centered(value,50+20*i) for i,value in enumerate(values)))
    original = [' '.join(years), 'Выручка, млрд руб. '+' '.join(values)]
    layout = header+'\n'+row
    for column in (1,count):
        result = tables.resolve(original,layout,dict(row_line=2,year_line=1,period_line_start=1,period_line_end=1,column=column))
        assert result['cells'] == [values[column-1]]
        assert result['column_label'] == years[column-1] and result['explicit']
    assert len(tables.page_tables(original,layout)[0]['columns']) == count


def test_wider_or_ambiguous_header_is_not_guessed():
    wide = tables.layout_rows(placed(*(centered(str(2000+i),50+20*i) for i in range(13))))[0]
    assert tables.header_columns(wide) is None
    ambiguous = tables.layout_rows(placed((0,'2021-2022'),(25,'2023-2024')))[0]
    assert tables.header_columns(ambiguous) is None
