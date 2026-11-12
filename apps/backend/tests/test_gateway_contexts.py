"""Частные маршруты двух сеансов: позднее исследование не меняет родителя учета."""
import json
import stat
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

import pytest

from app import openclaw_runtime as runtime
from app.errors import ApiError
from app.executor import OpenClawExecutor


def test_oversized_context_stops_with_exact_reason_before_model_dispatch(tmp_path):
    boundary = SimpleNamespace(profile={'provider': 'openai', 'transport': 'https_openai_responses', 'input_bytes': 64},
        token='synthetic', r={'id': 'run_synthetic', 'epoch': 1}, runs=Mock(), model=Mock())
    server = runtime.PrivateHTTPServer(('127.0.0.1', 0), runtime.ProviderHandler)
    server.boundary = boundary
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    serving.start()
    try:
        request = Request(f'http://127.0.0.1:{server.server_port}/v1/responses', data=b' ' * 65,
                          headers={'Authorization': 'Bearer synthetic'})
        with pytest.raises(HTTPError) as failure:
            urlopen(request)
        assert json.load(failure.value)['error']['code'] == 'budget_exceeded'
        boundary.model.assert_not_called()
        assert boundary.runs.stop.call_args.args == ('run_synthetic', 1, 'budget_exceeded')
        assert 'Новый запрос к модели не отправлен' in boundary.runs.stop.call_args.kwargs['message']
    finally:
        server.shutdown(); server.server_close(); serving.join()


@pytest.mark.parametrize('provider,path,authorization', [
    ('anthropic', '/v1/messages', {'x-api-key': 'gateway-capability'}),
    ('novita', '/v1/chat/completions', {'Authorization': 'Bearer gateway-capability'}),
])
def test_private_http_routes_exact_context_and_rejects_late_or_forged_identity(tmp_path, provider, path, authorization):
    contexts = runtime.RunContexts(tmp_path)
    calls = []
    def boundary(key, token):
        return SimpleNamespace(r={'executor_session_id': key}, token=token, closed=False, profile={'provider': provider},
                               lock=threading.Lock(), model=lambda body: calls.append((key, body)) or {'context': key})
    analyst = boundary('agent:main:explicit:run', 'gateway-capability')
    analyst.profile = {'provider': provider}
    contexts.register('main', analyst)
    first = boundary('agent:researcher:explicit:first', 'first-capability')
    first_identity = contexts.register('researcher', first)
    server = runtime.PrivateHTTPServer(('127.0.0.1', 0), runtime.ProviderHandler)
    server.boundary, server.contexts = analyst, contexts
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    serving.start()
    def post(identity, capability, expected=200):
        headers = dict(authorization, **{'x-aist-agent-id': identity['agentId'],
                       'x-aist-session-id': identity['sessionId'], 'x-aist-capability': capability})
        request = Request(f'http://127.0.0.1:{server.server_port}{path}', data=b'{"sessionId":"forged-in-body"}', headers=headers)
        if expected == 200:
            return json.loads(urlopen(request).read())
        with pytest.raises(HTTPError) as failure:
            urlopen(request)
        assert failure.value.code == expected
    try:
        assert post(first_identity, first.token) == {'context': first.r['executor_session_id']}
        contexts.close(first)
        second = boundary('agent:researcher:explicit:second', 'second-capability')
        second_identity = contexts.register('researcher', second)
        post(first_identity, first.token, 423)
        post(second_identity, first.token, 423)
        post(dict(second_identity, agentId='main'), second.token, 423)
        post(dict(second_identity, sessionId='unknown'), second.token, 423)
        assert post(second_identity, second.token) == {'context': second.r['executor_session_id']}
        assert [key for key, _ in calls] == [first.r['executor_session_id'], second.r['executor_session_id']]
        assert contexts.tool(second.r['executor_session_id']) is second
        with pytest.raises(ApiError):
            contexts.tool(first.r['executor_session_id'])
        with pytest.raises(ApiError):
            contexts.register('researcher', first)
        assert stat.S_IMODE(contexts.path.stat().st_mode) == 0o600
        assert not contexts.path.with_suffix('.tmp').exists()
    finally:
        server.shutdown(); server.server_close(); serving.join()


def test_closed_boundary_does_not_dispatch_model_or_tool(tmp_path):
    worker = SimpleNamespace(runs=None)
    current = runtime.Boundary(worker, {'input': {}, 'executor_session_id': 'closed'}, tmp_path, 'synthetic')
    current.closed = True
    with pytest.raises(ApiError) as model:
        current.model({})
    with pytest.raises(ApiError) as tool:
        current.tool(dict(session='closed', tool_call_id='old', name='aist_web_search', args={}))
    assert model.value.code == tool.value.code == 'resource_not_found'


@pytest.mark.parametrize('active', [False, True, None])
def test_research_finished_between_status_and_abort_requires_exact_inactive_session(active):
    requests = []
    def request(method, params):
        requests.append((method, params))
        if method == 'sessions.abort':
            return dict(status='no-active-run', abortedRunId=None)
        return dict(session={'hasActiveRun': active})
    executor = OpenClawExecutor(SimpleNamespace(request=request), None)
    if active is False:
        assert executor.cancel('research-run', 'research-session', allow_finished=True)['status'] == 'no-active-run'
    else:
        with pytest.raises(ApiError) as error:
            executor.cancel('research-run', 'research-session', allow_finished=True)
        assert error.value.code == 'outcome_unknown'
    assert requests == [('sessions.abort', dict(key='research-session', runId='research-run')),
                        ('sessions.describe', dict(key='research-session'))]


def test_unconfirmed_or_foreign_abort_is_not_accepted_as_finished():
    for response in [dict(status='unavailable'), dict(status='aborted', abortedRunId='foreign')]:
        gateway = SimpleNamespace(request=lambda method, params: response)
        with pytest.raises(ApiError) as error:
            OpenClawExecutor(gateway, None).cancel('research-run', 'research-session', allow_finished=True)
        assert error.value.code == 'outcome_unknown'
