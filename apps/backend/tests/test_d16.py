"""Normalization uses hand-authored ISS-shaped responses; no retained exchange data."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app import moex
from app.contracts import validate_definition
from app.errors import ApiError

FIXTURES = Path(__file__).parent / 'fixtures/moex'


def retained(method):
    return (FIXTURES / f'moex-{method}-initial.json').read_text()


def snapshot():
    replies = {name: dict(method='GET', url='https://iss.moex.com'+moex.METHODS[name], parameters={},
                          request_url='https://iss.moex.com'+moex.METHODS[name], received_at=moex.utcnow(),
                          sha256=hashlib.sha256(retained(name).encode()).hexdigest(), raw_json=retained(name))
               for name in ('card','issuer','history')}
    security = moex.identify(moex.decode(retained('card')), moex.decode(retained('issuer')))
    price = moex.normalize_price(moex.decode(retained('history')), security['history_date'])
    normalized = dict(security=security, price=price)
    return dict(provider='MOEX ISS', fetched_at=moex.utcnow(), requests=replies,
                normalized=normalized, sections=moex.snapshot_sections(normalized), limitation=moex.LIMITATION)


def test_retained_identity_and_exact_price():
    value = snapshot()
    security, price = value['normalized']['security'], value['normalized']['price']
    assert (security['secid'], security['isin'], security['emitter_id'], security['issuer_inn']) == ('WUSH','RU000A105EX7','13955','9703103060')
    assert price == dict(field='LEGALCLOSEPRICE',label='Цена закрытия',value='17.25',currency='RUB',currency_raw='SUR',
                         trade_date='2026-10-14',requested_date='2026-10-14',unknown_reason=None)
    assert moex.rows(moex.decode(retained('history')), 'history')[0]['CLOSE'] == Decimal('17.11')
    validate_definition('MoexNormalized',value['normalized'])


@pytest.mark.parametrize('field,value',[('isin','RU000OTHER'),('emitent_id',1),('emitent_inn','0000000000'),('type','futures')])
def test_same_name_does_not_identify_company(field,value):
    issuer = json.loads(retained('issuer')); issuer['securities']['data'][0][issuer['securities']['columns'].index(field)] = value
    with pytest.raises(ApiError,match='') as error:
        moex.identify(moex.decode(retained('card')),issuer)
    assert error.value.code == 'moex_identity_mismatch'


@pytest.mark.parametrize('value,expected',[(None,None),(0,'0'),(Decimal('17.2500'),'17.2500')])
def test_null_zero_and_decimal_are_distinct(value,expected):
    history = moex.decode(retained('history'))
    history['history']['data'][0][history['history']['columns'].index('LEGALCLOSEPRICE')] = value
    price = moex.normalize_price(history,'2026-10-14')
    assert price['value'] == expected
    assert bool(price['unknown_reason']) == (value is None)
    assert price['trade_date'] == '2026-10-14'


def test_missing_price_and_no_history():
    history=moex.decode(retained('history'));index=history['history']['columns'].index('LEGALCLOSEPRICE')
    history['history']['columns'].pop(index);history['history']['data'][0].pop(index)
    assert moex.normalize_price(history,'2026-10-14')['value'] is None
    history['history']['data']=[]
    price=moex.normalize_price(history,'2026-10-14')
    assert price['value'] is None and price['trade_date'] is None and price['currency'] is None


@pytest.mark.parametrize('field,value',[('SECID','OTHER'),('BOARDID','OTHER'),('TRADEDATE','2026-10-13')])
def test_history_must_match_requested_security_board_and_day(field,value):
    history=moex.decode(retained('history'));history['history']['data'][0][history['history']['columns'].index(field)]=value
    with pytest.raises(ApiError) as error:moex.normalize_price(history,'2026-10-14')
    assert error.value.code=='moex_identity_mismatch'


@pytest.mark.parametrize('value',[True,-1,Decimal('NaN'), '17.25'])
def test_invalid_price_is_rejected(value):
    history=moex.decode(retained('history'));history['history']['data'][0][history['history']['columns'].index('LEGALCLOSEPRICE')]=value
    with pytest.raises(ValueError):moex.normalize_price(history,'2026-10-14')


def test_transport_only_allows_fixed_methods_and_does_not_follow_redirect(monkeypatch):
    requests=[]
    class Connection:
        def __init__(self,host,timeout):assert host=='iss.moex.com' and timeout==8
        def request(self,method,path,headers):requests.append((method,path))
        def getresponse(self):return SimpleNamespace(status=302,read=lambda size:b'',getheader=lambda *a:'application/json')
        def close(self):pass
    monkeypatch.setattr(moex.http.client,'HTTPSConnection',Connection)
    with pytest.raises(ApiError):moex.get_iss('https://example.com')
    assert requests==[]
    with pytest.raises(ApiError) as error:moex.get_iss('card')
    assert error.value.code=='moex_unavailable' and len(requests)==1


def test_json_locator_never_claims_pdf_page():
    validate_definition('Locator',{'json_pointer':'/normalized/price','area':'Цена закрытия'})
    with pytest.raises(Exception):validate_definition('Locator',{'json_pointer':'/normalized/price','pdf_page':1,'area':'Цена закрытия'})


def test_archive_serialization_preserves_original_json_bytes():
    value = snapshot()
    raw = '{\r\n"text":"e\u0301", "price":17.250\r\n}'
    value['requests']['history']['raw_json'] = raw
    value['requests']['history']['sha256'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    restored = json.loads(moex.serialize_snapshot(value))
    assert restored['requests']['history']['raw_json'].encode('utf-8') == raw.encode('utf-8')
    assert restored['requests']['history']['sha256'] == hashlib.sha256(restored['requests']['history']['raw_json'].encode('utf-8')).hexdigest()


def price_history(raw):
    return ('{"history":{"columns":["SECID","BOARDID","TRADEDATE","CURRENCYID","LEGALCLOSEPRICE"],'
            '"data":[["WUSH","TQBR","2026-10-14","SUR",' + raw + ']]}}')


@pytest.mark.parametrize('raw,expected', [
    ('1E127', '1' + '0'*127),
    ('1E-126', '0.' + '0'*125 + '1'),
    ('0E-126', '0.' + '0'*126),
    ('9'*128, '9'*128),
    ('17.2500', '17.2500'),
])
def test_price_within_fixed_point_limits(raw, expected):
    history = moex.decode(price_history(raw))
    price = moex.normalize_price(history, '2026-10-14')
    assert price['value'] == expected and price['trade_date'] == '2026-10-14'
    assert price['currency'] == 'RUB'


@pytest.mark.parametrize('raw', ['1E128', '1E-127', '1E+999999999', '1E-999999999',
                                 '0E-999999999', '0E+999999999', '0.'+'1'*129, '9'*129])
def test_excessive_price_fails_through_snapshot(raw, monkeypatch):
    def reply(name, *args):
        return {'raw_json': price_history(raw) if name == 'history' else retained(name)}

    monkeypatch.setattr(moex, 'get_iss', reply)
    with pytest.raises(ApiError) as error:
        moex.fetch_snapshot(lambda: None)
    assert error.value.code == 'moex_invalid_response'
