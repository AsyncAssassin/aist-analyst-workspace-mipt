from decimal import Decimal
import pytest
from app.accounting import exposure
from app.config import Settings
from app.executor import OpenClawExecutor
from app.errors import ApiError


def test_active_reservation_and_individually_confirmed_exposure():
    u={'reserved_usd':'0.30000001','estimated_usd':{'value':'0.01000001'},'billed_usd':{'value':'0.02000001'}}
    for state in ('reserved','attempted','sent'):
        assert exposure({'transport_state':state},u)==Decimal('0.30000001')
    assert exposure({'transport_state':'observed'},u)==Decimal('0.02000001')
    assert exposure({'transport_state':'unknown'},u)==Decimal('0.02000001')
    u['billed_usd']['value']='0'
    assert exposure({'transport_state':'unknown'},u)==Decimal('0')
    u['billed_usd']['value']=None
    assert exposure({'transport_state':'unknown'},u)==Decimal('0.30000001')
    assert exposure({'transport_state':'observed'},u)==Decimal('0.01000001')
    u['estimated_usd']['value']=None
    assert exposure({'transport_state':'observed'},u)==Decimal('0.30000001')


def test_qa_requires_explicit_injection_and_normal_runtime_is_disabled():
    assert Settings(executor_mode='disabled').executor_mode=='disabled'
    with pytest.raises(ValueError,match='explicit simulator'):
        Settings(executor_mode='qa-scripted',qa_url='',policy_path='')


def test_openclaw_rpc_preserves_exact_run_session_correlation():
    requests=[];attachments=[]
    class Gateway:
        def request(self,method,params):
            requests.append((method,params))
            return {'runId':'exec_actual'} if method=='agent' else {'status':'aborted','abortedRunId':params['runId']}
    adapter=OpenClawExecutor(Gateway(),lambda *x:attachments.append(x))
    r={'id':'run_one','epoch':7,'executor_session_id':'session_one'}
    pair=adapter.start(r,'Synthetic message');adapter.cancel(pair['executor_run_id'],pair['executor_session_id'])
    assert attachments==[('run_one',7,'exec_actual','session_one')]
    assert requests[1]==('sessions.abort',{'key':'session_one','runId':'exec_actual'})
    class Wrong:
        def request(self,*a):return {'status':'aborted','abortedRunId':'exec_other'}
    with pytest.raises(ApiError):OpenClawExecutor(Wrong(),lambda *a:None).cancel('exec_actual','session_one')
