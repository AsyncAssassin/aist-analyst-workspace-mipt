"""Local fault/change probes. These are not fresh responses from MOEX or Novita."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from sqlalchemy import text
from app import moex
from app.errors import ApiError
from app.facts import pin_of, resolve_fact
from app.search import ui_search
from app.service import Service, ident
from app.storage import Storage
from app.domain import growth
from test_d15_postgres import workspace, provider, qdrant
from test_d16 import snapshot

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.fixture
def iss(monkeypatch):
    calls=[]
    def fetch(authorize):
        authorize(); calls.append('GET set'); return snapshot()
    monkeypatch.setattr(moex,'fetch_snapshot',fetch)
    return calls


def perform(w,key='first',mode='get',settings=None):
    return moex.update(w.service,w.actor,w.project,"d16-operation-"+key,mode,settings=settings)


def test_save_repeat_refresh_restart_and_pinned_old_facts(workspace,iss,monkeypatch):
    w=workspace
    first=perform(w);assert first['changed'] and len(iss)==1
    assert perform(w)==first and len(iss)==1
    assert perform(w,'get-again')['version_id']==first['version_id'] and len(iss)==1
    same=perform(w,'refresh','refresh');assert not same['changed'] and same['version_id']==first['version_id'] and len(iss)==2
    with w.engine.begin() as c:
        old=w.service.scoped(c,w.actor,w.project,'source_versions',first['version_id']);pin=pin_of(old)
    changed=snapshot();changed['requests']['history']['sha256']='a'*64
    changed['normalized']['price']['value']='40.25';changed['sections']=moex.snapshot_sections(changed['normalized'])
    monkeypatch.setattr(moex,'fetch_snapshot',lambda authorize:changed)
    second=perform(w,'new-price','refresh');assert second['changed'] and second['version_id']!=first['version_id']
    reconnected=Service(w.engine,Storage(w.settings.storage_root))
    state=moex.status(reconnected,w.actor,w.project);assert state['normalized']['price']['value']=='40.25'
    with w.engine.begin() as c:
        facts=c.execute(text('SELECT data FROM aist.facts WHERE source_version_id=:v'),dict(v=old['id'])).scalars().all()
        old_price=next(f for f in facts if f['amount'])
        resolved=resolve_fact(w.service,c,dict(owner_id=w.actor,project_id=w.project,input=dict(source_versions=[pin])),dict(source_version_id=old['id'],fact_id=old_price['fact_id']))
        assert resolved['data']['amount']['value']=='17.25'
        assert c.execute(text('SELECT count(*) FROM aist.source_pages WHERE version_id=:v'),dict(v=old['id'])).scalar()==0
    raw,version=reconnected.page_or_file(w.actor,w.project,old['id']);assert version['page_count'] is None
    assert json.loads(raw)['normalized']['price']['value']=='17.25'
    assert perform(w)==first


def test_failed_refresh_keeps_snapshot_and_success_time(workspace,iss,monkeypatch):
    w=workspace;first=perform(w);before=moex.status(w.service,w.actor,w.project)
    calls=[]
    def unavailable(authorize):calls.append(1);raise ApiError('moex_unavailable')
    monkeypatch.setattr(moex,'fetch_snapshot',unavailable)
    failure=perform(w,'outage','refresh');assert failure['error']
    assert perform(w,'outage','refresh')==failure and calls==[1]
    after=moex.status(w.service,w.actor,w.project)
    assert after['version']==before['version'] and after['last_success_at']==before['last_success_at'] and after['last_error']


def test_project_and_owner_isolation_before_get(workspace,iss):
    w=workspace
    with pytest.raises(ApiError):moex.update(w.service,ident('usr'),w.project,'d16-operation-isolation','get')
    with pytest.raises(ApiError):moex.update(w.service,w.actor,ident('prj'),'d16-operation-isolation','get')
    assert not iss
    first=perform(w)
    _,other=w.service.write(w.actor,'createProject',ident('key'),{'name':'Other company'})
    assert moex.status(w.service,w.actor,other['id'])['version'] is None
    with pytest.raises(ApiError):w.service.page_or_file(w.actor,other['id'],first['version_id'])
    args=dict(query='Whoosh',source_version_ids=[first['version_id']],limit=5,method='lexical.v1')
    with pytest.raises(ApiError):ui_search(w.service,w.settings,w.actor,other['id'],args)
    assert len(iss)==1


def test_json_search_index_and_no_fact_creation_on_search(workspace,iss,provider,qdrant):
    w=workspace;first=perform(w,settings=w.settings)
    assert first['search_error'] is None
    with w.engine.begin() as c:
        before=c.execute(text('SELECT count(*) FROM aist.facts WHERE source_version_id=:v'),dict(v=first['version_id'])).scalar()
    for method in ('lexical.v1','semantic.qwen3.v1'):
        result=ui_search(w.service,w.settings,w.actor,w.project,dict(query='Цена закрытия Whoosh',source_version_ids=[first['version_id']],limit=5,method=method))
        assert result['items'] and all('json_pointer' in e['locator'] and 'pdf_page' not in e['locator'] for e in result['items'])
    sent=len(provider.sent)
    assert perform(w,settings=w.settings)==first
    assert len(provider.sent)==sent
    with w.engine.begin() as c:
        assert before==c.execute(text('SELECT count(*) FROM aist.facts WHERE source_version_id=:v'),dict(v=first['version_id'])).scalar()


def test_absent_price_saved_as_unknown(workspace,monkeypatch):
    w=workspace;value=snapshot();value['normalized']['price'].update(value=None,unknown_reason='Нет значения LEGALCLOSEPRICE')
    value['sections']=moex.snapshot_sections(value['normalized'])
    monkeypatch.setattr(moex,'fetch_snapshot',lambda authorize:value)
    first=perform(w)
    assert moex.status(w.service,w.actor,w.project)['normalized']['price']['value'] is None
    with w.engine.begin() as c:
        facts=c.execute(text('SELECT data FROM aist.facts WHERE source_version_id=:v'),dict(v=first['version_id'])).scalars().all()
        assert all(f['amount'] is None for f in facts)


def test_concurrent_repeat_fetches_once(workspace,monkeypatch):
    w=workspace;entered=threading.Event();release=threading.Event();calls=[]
    def waiting(authorize):calls.append(1);entered.set();assert release.wait(5);return snapshot()
    monkeypatch.setattr(moex,'fetch_snapshot',waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(perform,w)
        assert entered.wait(5)
        try:
            with pytest.raises(ApiError) as error:perform(w)
            assert error.value.code=='moex_busy'
        finally:release.set()
        result=first.result()
    assert perform(w)==result and calls==[1]


def test_crashed_attempt_never_resends_with_same_key(workspace,monkeypatch):
    w=workspace;calls=[]
    def crash(authorize):calls.append(1);raise RuntimeError('simulated process interruption')
    monkeypatch.setattr(moex,'fetch_snapshot',crash)
    with pytest.raises(RuntimeError):perform(w)
    with pytest.raises(ApiError) as error:perform(w)
    assert error.value.code=='outcome_unknown' and calls==[1]
    assert moex.status(w.service,w.actor,w.project)['last_error']


def test_cached_get_key_stays_on_its_version_after_update(workspace,iss,monkeypatch):
    w=workspace;first=perform(w);cached=perform(w,'cached-get')
    changed=snapshot();changed['requests']['history']['sha256']='b'*64
    monkeypatch.setattr(moex,'fetch_snapshot',lambda authorize:changed)
    second=perform(w,'new-check','refresh');assert second['version_id']!=first['version_id']
    assert perform(w,'cached-get')==cached


def test_http_json_download_and_read_do_not_fetch(workspace,iss):
    import secrets
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth import token_hash
    w=workspace;token='aist_'+secrets.token_urlsafe(32)
    with w.engine.begin() as c:
        c.execute(text('UPDATE aist.sessions SET token_hash=:hash WHERE id=:id'),dict(hash=token_hash(token),id=w.session))
    # Unit HTTP test does not enable embedding I/O.
    from dataclasses import replace
    app=create_app(replace(w.settings,embeddings_enabled=False))
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+token
        path=f'/v1/projects/{w.project}/moex/wush'
        assert client.get(path).json()['version'] is None and not iss
        response=client.post(path,json={'mode':'get','url':'https://example.com'},headers={'Idempotency-Key':'d16-http-invalid'})
        assert response.status_code==422 and not iss
        response=client.post(path,json={'mode':'get'},headers={'Idempotency-Key':'d16-http-success'})
        assert response.status_code==200,response.text
        version=response.json()['version_id'];status=client.get(path).json()
        assert status['version']['media_type']=='application/json' and status['version']['page_count'] is None
        raw=client.get(f'/v1/projects/{w.project}/source-versions/{version}/file')
        assert raw.status_code==200 and 'source.json' in raw.headers['Content-Disposition']
        assert json.loads(raw.content)['normalized']['price']['value']=='17.25'
        assert client.get(f'/v1/projects/{w.project}/source-versions/{version}/page?page=1').status_code==404
        assert len(iss)==1
        assert client.post(path,json={'mode':'get'},headers={'Idempotency-Key':'d16-http-success'}).json()==response.json()
        assert len(iss)==1


def test_json_cannot_be_replaced_by_pdf_upload(workspace,iss):
    from pdf_fixtures import pdf_bytes
    w=workspace;first=perform(w);version=w.service.get(w.actor,w.project,'source_versions',first['version_id'])
    with pytest.raises(ApiError) as error:
        w.service.write(w.actor,'uploadSourceVersion','d16-pdf-replacement',dict(title='Replacement',expected_revision=1),
                        w.project,version['source_id'],pdf_bytes(),'replacement.pdf')
    assert error.value.code=='unsupported_source'


def test_price_cannot_be_used_as_2022_revenue(workspace,iss):
    from test_domain import fact, calculation, BASIS
    w=workspace;first=perform(w)
    with w.engine.begin() as c:
        facts=c.execute(text('SELECT data FROM aist.facts WHERE source_version_id=:v'),dict(v=first['version_id'])).scalars().all()
    price=next(f for f in facts if f['amount'])
    revenue=fact('120',2022,'revenue')
    args,_=calculation()
    with pytest.raises(ApiError):
        growth('calc_d16_price_revenue',args,{'current':dict(data=price,calculation_basis={}),
                                             'previous':dict(data=revenue,calculation_basis=BASIS)})


def test_changed_provider_bytes_run_through_adapter(workspace,monkeypatch):
    import hashlib
    from test_d16 import retained
    w=workspace;calls=[];price={'value':17.25}
    def get(method,trading_date=None):
        calls.append((method,trading_date))
        payload=json.loads(retained(method))
        if method=='history':
            assert trading_date=='2026-10-14'
            payload['history']['data'][0][payload['history']['columns'].index('LEGALCLOSEPRICE')]=price['value']
        raw=json.dumps(payload,ensure_ascii=False)
        return dict(method='GET',url='https://iss.moex.com'+moex.METHODS[method],parameters={},
                    request_url='https://iss.moex.com'+moex.METHODS[method],received_at=moex.utcnow(),
                    sha256=hashlib.sha256(raw.encode()).hexdigest(),raw_json=raw)
    monkeypatch.setattr(moex,'get_iss',get)
    first=perform(w);assert len(calls)==3
    price['value']=40.25
    second=perform(w,'changed-transport','refresh')
    assert second['version_id']!=first['version_id'] and len(calls)==6
    raw,_=w.service.page_or_file(w.actor,w.project,second['version_id'])
    saved=json.loads(raw)
    assert saved['normalized']['price']['value']=='40.25'
    assert moex.rows(moex.decode(saved['requests']['history']['raw_json']),'history')[0]['LEGALCLOSEPRICE']==moex.Decimal('40.25')
    assert perform(w,'changed-transport','refresh')==second and len(calls)==6


def test_answer_uses_normalized_price_without_parsing_registration_as_sign(workspace,iss):
    from app.tools import resolve_basis
    w=workspace;first=perform(w)
    with w.engine.begin() as c:
        version=w.service.scoped(c,w.actor,w.project,'source_versions',first['version_id'])
        facts=c.execute(text('SELECT data FROM aist.facts WHERE source_version_id=:v'),dict(v=first['version_id'])).scalars().all()
        price=next(f for f in facts if f['amount']);card=next(f for f in facts if not f['amount'])
        def claim(f,text):
            return dict(origin='source_fact',text=text,calculation_ids=[],fact_refs=[dict(source_version_id=version['id'],fact_id=f['fact_id'])])
        payload=dict(result_type='evidence_answer',schema_version='1.0.0',content=dict(missing_information=[],claims=[
            claim(card,'В MOEX ISS сохранена бумага Synthetic issuer for WUSH: Synthetic WUSH fixture, SECID WUSH, режим TQBR.'),
            claim(price,'Цена закрытия LEGALCLOSEPRICE составляет 17.25 RUB на дату торгов 2026-10-14.')]))
        run=dict(owner_id=w.actor,project_id=w.project,input=dict(source_versions=[pin_of(version)]))
        findings=[];basis=resolve_basis(w.runs,c,run,payload,findings=findings)
        assert len(basis['facts'])==2 and not findings
        payload['content']['claims']=[claim(card,'Выручка 13955 RUB.')]
        with pytest.raises(ApiError):resolve_basis(w.runs,c,run,payload)
        payload['content']['claims']=[claim(price,'Цена закрытия 0 RUB на дату торгов 2026-10-14.')]
        with pytest.raises(ApiError):resolve_basis(w.runs,c,run,payload)
