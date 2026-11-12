"""One failed operation, flat model arguments and purpose-specific reasoning."""
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from app.live_profile import MODEL, PROFILE
from app.errors import ApiError
from app.openclaw_runtime import Boundary, obtain_facts
from app.runs import execute
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import candidate, provider, upload_pdf
from test_d15_postgres import workspace

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('malformed', [False, True])
def test_known_failed_operation_is_not_sent_again_with_new_tool_id(workspace, provider, tmp_path, malformed):
    w, p = workspace, provider
    source = upload_pdf(w)
    send(w, conversation(w), body(sources=[source['id']], question='Условия финансирования'))
    run = w.runs.claim('perf01_repeat')
    p.facts = [candidate()]
    p.facts[0]['metric'] = 'Invented financing label'
    args = dict(refs=[], extract='<parameter=question>Условия' if malformed else
                dict(source_version_id=source['id'], question='Условия финансирования'))
    first = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', args,
        obtain_facts=lambda current: obtain_facts(p.worker, current, args['extract'], tmp_path, None))
    if malformed:
        assert first['error']['code'] == 'validation_error'
        def forbidden(current):
            pytest.fail('Repeated operation reached extraction')
        second = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', args, obtain_facts=forbidden)
        assert second['error']['code'] == first['error']['code']
    else:
        # FLOW-01.02: the invented label excludes that candidate with its reason; the operation completed
        # without usable facts, and the same question under a new tool ID reads the saved result, no POST.
        assert first['ok'] and first['result']['status'] == 'empty' and first['result']['skipped'][0]['code'] == 'ambiguous_link'
        p.facts = []
        second = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', args,
            obtain_facts=lambda current: obtain_facts(p.worker, current, args['extract'], tmp_path, None))
        assert second['result'] == first['result']
    assert len(p.sent) == (0 if malformed else 1)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.facts WHERE project_id=:p', p=w.project).scalar() == 0
    # Новый вопрос не разрешает повторную отправку полного PDF в том же Run.
    corrected = dict(refs=[], extract=dict(source_version_id=source['id'], question='Revenue 3M23'))
    p.facts = [candidate()]
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_get_facts', corrected,
        obtain_facts=lambda current: obtain_facts(p.worker, current, corrected['extract'], tmp_path, None))
    if malformed:
        assert reply['ok'] and len(p.sent) == 1
    else:
        assert not reply['ok'] and reply['error']['code'] == 'validation_failed'
        assert len(p.sent) == 1
        with w.engine.connect() as c:
            assert execute(c, 'SELECT count(*) FROM aist.facts WHERE project_id=:p', p=w.project).scalar() == 0


@pytest.mark.parametrize('mode', ['enabled', 'disabled'])
def test_flat_question_and_extraction_mode_reach_real_transport(workspace, provider, tmp_path, monkeypatch, mode):
    w, p = workspace, provider
    source = upload_pdf(w)
    send(w, conversation(w), body(sources=[source['id']], question='Revenue'))
    run = w.runs.claim('perf01_modes')
    monkeypatch.setitem(PROFILE, 'extraction_thinking', mode)
    p.args = dict(question='Revenue 3M23')
    p.facts = [candidate()]
    boundary = Boundary(p.worker, run, tmp_path, 'synthetic')
    payload = boundary.model(dict(model=MODEL, messages=[dict(role='user', content='Revenue')],
                                  thinking={'type': 'enabled'}, enable_thinking=True, reasoning_effort='high'))
    issued = payload['choices'][0]['message']['tool_calls'][0]
    result = boundary.tool(dict(session=run['executor_session_id'], tool_call_id=issued['id'], name='aist_get_facts', args=p.args))
    assert result['ok']
    assert p.sent[0]['thinking'] == {'type': 'disabled'}
    assert p.sent[0]['enable_thinking'] is False
    assert p.sent[1]['thinking'] == {'type': run['configuration']['extraction_thinking']}
    assert p.sent[1]['enable_thinking'] is True
    assert all(s['stream'] is False and s['max_tokens'] == 32000 and 'reasoning_effort' not in s for s in p.sent)
    assert json.loads(p.sent[1]['messages'][1]['content'])['question'] == 'Revenue 3M23'


@pytest.mark.parametrize('partial_tool', [False, True], ids=['reasoning-only', 'partial-tool'])
def test_length_response_is_accounted_without_actions_or_retry(workspace, provider, tmp_path, partial_tool):
    w, p = workspace, provider
    source = upload_pdf(w)
    send(w, conversation(w), body(sources=[source['id']], question='Prepare a source-backed note'))
    run = w.runs.claim('length_replay')
    request = dict(model=MODEL, messages=[dict(role='user', content='Use the saved facts')],
                   thinking={'type': 'enabled'}, enable_thinking=True, max_tokens=32000,
                   stream=False, n=1, parallel_tool_calls=False)
    response = dict(id='saved_completion', choices=[dict(index=0, finish_reason='length',
        message=dict(role='assistant', content='', reasoning_content='Unfinished planning'))],
        usage=dict(prompt_tokens=200, completion_tokens=40,
                   completion_tokens_details=dict(reasoning_tokens=40)))
    replay = os.getenv('AIST_MODEL_REPLAY')
    if replay:
        requests = list(Path(replay).glob('*-request.json'))
        responses = list(Path(replay).glob('*-response.json'))
        assert len(requests) == len(responses) == 1
        request = json.loads(requests[0].read_text())
        response = json.loads(responses[0].read_text())
    original = deepcopy(request)
    assert response['choices'][0]['finish_reason'] == 'length'
    if partial_tool:
        response['choices'][0]['message']['tool_calls'] = [dict(id='call_incomplete', type='function',
            function=dict(name='aist_save_artifact', arguments='{"payload":'))]
    p.response = response
    audit = tmp_path / 'replay'
    result = p.worker.transport.perform(run, {'purpose': 'agent'}, request=request, audit=audit)
    assert result is None
    assert request == original
    assert p.sent == [dict(original, thinking={'type': 'disabled'}, enable_thinking=False)]
    assert json.loads(next(audit.glob('*-response.json')).read_text()) == response
    current = w.runs.get(w.actor, w.project, run['id'])
    assert current['state'] == 'failed'
    assert current['error']['code'] == 'provider_error'
    assert 'finish_reason=length' in current['error']['message']
    boundary = Boundary(p.worker, run, audit, 'synthetic')
    with pytest.raises(ApiError):
        boundary.model(request)
    assert len(p.sent) == 1
    with w.engine.connect() as c:
        calls = execute(c, 'SELECT data FROM aist.model_calls WHERE run_id=:r', r=run['id']).scalars().all()
        assert len(calls) == 1
        call = calls[0]
        assert call['transport_state'] == 'observed' and call['provider_outcome'] == 'error'
        assert call['error'] == current['error']
        usage = execute(c, 'SELECT data FROM aist.usage_entries WHERE id=:id', id=call['usage_entry_id']).scalar_one()
        assert usage['reservation_state'] == 'released' and usage['evidence_kind'] == 'provider_response'
        assert usage['tokens']['input']['value'] == response['usage']['prompt_tokens']
        assert usage['tokens']['output']['value'] == response['usage']['completion_tokens']
        assert usage['tokens']['reasoning_output']['value'] == response['usage']['completion_tokens_details']['reasoning_tokens']
        for table in ('tool_calls', 'artifact_versions', 'facts'):
            assert execute(c, f'SELECT count(*) FROM aist.{table} WHERE project_id=:p', p=w.project).scalar_one() == 0
