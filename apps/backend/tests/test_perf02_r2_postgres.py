"""Quarterly flow and forecast series through the normal extraction transport and ingest in a test database."""
import os

import pytest

from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_perf02_r1_postgres import HEADER, ingest, numeric_claim, qualitative, saved_amounts, upload, excluded_only

pytestmark=pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES')!='true',reason='Requires isolated PostgreSQL')

QUARTER=dict(kind='interval',label='за 1 кв. 2023 г.',start='2023-01-01',end='2023-03-31')


def cited(item,line_end,metric=None):
    item['evidence'][0]['line_end']=line_end
    if metric:
        item['metric']=metric;item['basis']['metric_label']=metric
    return item


def test_quarterly_flow_and_forecast_series_through_ingest(workspace,provider,tmp_path):
    w,p=workspace,provider
    quarter=upload(w,'quarter',[HEADER,'за 1 кв. 2023 г.','Выручка','100 млн руб.'])
    series=upload(w,'series',[HEADER,'Собственный капитал','100 млн руб.','2024 г.'])
    forecast=upload(w,'forecast-series',[HEADER,'Прогноз на 2025 год','Собственный капитал','100 млн руб.','2025 г.'])
    reply=ingest(w,p,quarter,tmp_path,'Выручка за квартал',[qualitative()])
    assert reply['ok'] and sorted(f['amount'] is None for f in reply['result']['facts'])==[False,True]
    expected=[('Выручка','100','interval','за 1 кв. 2023 г.','2023-01-01','2023-03-31','reported_actual')]
    assert saved_amounts(w,quarter)==expected
    reply=ingest(w,p,quarter,tmp_path,'Выручка за квартал как год',[numeric_claim(dict(QUARTER,end='2023-12-31'))])
    excluded_only(reply,'ambiguous_link',None)
    reply=ingest(w,p,quarter,tmp_path,'Выручка за квартал числом',[numeric_claim(QUARTER)])
    assert reply['ok'] and reply['result']['facts'][0]['period']==QUARTER
    assert saved_amounts(w,quarter)==expected*2
    reply=ingest(w,p,series,tmp_path,'Собственный капитал',[qualitative()])
    assert reply['ok'] and saved_amounts(w,series)==[('Собственный капитал','100','instant','2024 г.','2024-12-31','2024-12-31','reported_actual')]
    reply=ingest(w,p,forecast,tmp_path,'Прогноз капитала',[cited(qualitative(),5)])
    assert reply['ok'] and [f['amount'] for f in reply['result']['facts']]==[None]
    assert saved_amounts(w,forecast)==[]
    claim=cited(numeric_claim(dict(kind='instant',label='2025 г.',start='2025-12-31',end='2025-12-31')),5,'Собственный капитал')
    reply=ingest(w,p,forecast,tmp_path,'Прогноз капитала числом',[claim])
    excluded_only(reply,'ambiguous_link',None)
    assert saved_amounts(w,forecast)==[]
    assert len(p.sent)==6 and all(r['thinking']=={'type':'enabled'} for r in p.sent)
