"""Adapted substantive rules from baseline archive_calculations_analysis and evidence_verifier."""
from copy import deepcopy
import pytest
from app.domain import growth, normalized_fact, locate_quote
from app.errors import ApiError

PIN={'source_id':'s_one','source_version_id':'sv_one','sha256':'a'*64}
BASIS={'definition':'Выручка без НДС','compatibility':'Одна серия отчетности'}

def fact(value='100', year=2022, fid='f_previous'):
    return dict(fact_id=fid,source=PIN,extraction_version='synthetic-unit.v1',metric='Выручка',subject='Synthetic Company',
        reporting_basis='МСФО',nature='reported_actual',period=dict(kind='interval',label=f'1Q{year}',start=f'{year}-01-01',end=f'{year}-03-31'),
        raw_text=value,amount=dict(value=value,unit='money',currency='RUB',scale='1000000'),
        evidence=[dict(evidence_id='e_one',source=PIN,locator=dict(pdf_page=1,area='Строка выручки',bbox_pt=None,coordinate_system='pdf_points_top_left'),quote=value)],limitations=[])

def calculation(previous='475', current='542', places=2):
    args={'formula_id':'growth_percent.v1','inputs':[{'name':n,'fact':{'source_version_id':'sv_one','fact_id':'f_'+n}} for n in ('previous','current')],'rounding':{'places':places,'mode':'HALF_UP'}}
    records={n:{'data':fact(v,y,'f_'+n),'calculation_basis':deepcopy(BASIS)} for n,v,y in [('previous',previous,2022),('current',current,2023)]}
    return args, records

def test_two_control_fractions():
    for previous,current,num,den,rounded in [('475','542','268','19','14.11'),('4586','7193','130350','2293','56.85')]:
        a,r=calculation(previous,current);v=growth('c_one',a,r)
        assert v['exact']=={'numerator':num,'denominator':den};assert v['rounded']['value']==rounded

@pytest.mark.parametrize('previous,current,places,expected', [('100','101.005',2,'1.01'),('100','98.995',2,'-1.01'),('3','4',6,'33.333333'),('100','100.5',0,'1'),('100','99.5',0,'-1'),('100','0',2,'-100.00')])
def test_exact_half_up_no_float(previous,current,places,expected):
    a,r=calculation(previous,current,places);assert growth('c_one',a,r)['rounded']['value']==expected

@pytest.mark.parametrize('field,value,reason', [('subject','Other Company','Компания'),('reporting_basis','РСБУ','Основа'),('metric','EBITDA','Показатель'),('nature','forecast','фактические'),('subject','unknown','недостаточно'),('reporting_basis','неизвестно','недостаточно'),('metric','?','недостаточно')])
def test_compatibility_refusals(field,value,reason):
    a,r=calculation();r['current']['data'][field]=value
    with pytest.raises(ApiError) as err:growth('c_one',a,r)
    assert reason in err.value.body['error']['message']

@pytest.mark.parametrize('field,value', [('unit','ratio'),('currency','USD'),('scale','1'),('currency','XXX')])
def test_no_fx_or_rescaling(field,value):
    a,r=calculation();r['current']['data']['amount'][field]=value
    with pytest.raises(ApiError):growth('c_one',a,r)

@pytest.mark.parametrize('patch', [dict(kind='unknown',start=None,end=None),dict(start='2023-01-01',end='2023-12-31'),dict(start='2024-01-01',end='2024-03-31'),dict(start='2023-04-01',end='2023-06-30'),dict(kind='instant',start='2023-03-31',end='2023-03-31')])
def test_period_refusals(patch):
    a,r=calculation();r['current']['data']['period'].update(patch)
    with pytest.raises(ApiError):growth('c_one',a,r)

@pytest.mark.parametrize('previous,reason', [('0','нулю'),('-1','Отрицательная')])
def test_denominator(previous,reason):
    a,r=calculation(previous)
    with pytest.raises(ApiError) as err:growth('c_one',a,r)
    assert reason in err.value.body['error']['message']

def test_missing_numbers_do_not_come_from_prose():
    a,r=calculation();r['previous']['data']['amount']=None;r['previous']['data']['raw_text']='Выручка 475 млн руб.'
    with pytest.raises(ApiError):growth('c_one',a,r)

@pytest.mark.parametrize('basis', [{},{'definition':'Выручка'},{'definition':'Adjusted revenue','compatibility':'Одна серия отчетности'}])
def test_definition_required(basis):
    a,r=calculation();r['current']['calculation_basis']=basis
    with pytest.raises(ApiError):growth('c_one',a,r)

def test_duplicate_inputs_rejected():
    a,r=calculation();a['inputs'][1]=a['inputs'][0]
    with pytest.raises(ApiError):growth('c_one',a,r)

def test_source_and_evidence_pin_binding():
    f=fact();assert normalized_fact(f,PIN)==f
    for patch in ['fact','evidence']:
        f=deepcopy(fact())
        (f if patch=='fact' else f['evidence'][0])['source']['sha256']='b'*64
        with pytest.raises(ApiError):normalized_fact(f,PIN)

def test_locator_whitespace_only_not_semantics():
    assert locate_quote('Выручка\n  475 млн руб.', 'Выручка 475 млн руб.')
    assert not locate_quote('Выручка 475 млн руб.', 'Выручка 542 млн руб.')
    assert not locate_quote('Выручка 475 млн руб.', 'Выручка | 475')
    assert not locate_quote('Выручка 475 млн руб.', '')


def test_arbitrary_daily_intervals_not_in_old_closed_period_set():
    a,r=calculation()
    for n,year in [('previous',2022),('current',2023)]:
        r[n]['data']['period'].update(start=f'{year}-01-01',end=f'{year}-01-02')
    with pytest.raises(ApiError):growth('c_one',a,r)
