"""Responses preserves native history and fails closed before tool execution."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app import openai_responses as responses
from app.accounting import response_fields, responses_estimate
from app.edit_proposals import rewritten_text
from app.errors import ApiError
from app.live_profile import OPENAI, OPENAI_ID, FABLE_VISUAL_ID, profile_for
from app.runs import validate_demo_policy
from app.transport import model_request


def policy(*, cache=False):
    value = json.loads((Path(__file__).parents[3] / 'executor/openclaw/policy.openai-astra.json').read_text())
    if not cache:
        value['pricing_version'] = 'openai-astra.standard.no-cache.v1'
        value['run']['policy_version'] = 'openai-astra.live.v1'
        value = {key: item for key, item in value.items() if 'cache_' not in key}
    return value


@pytest.mark.parametrize('research,available', [
    ({'research_version': 'agent.v2'}, True),
    ({'research_version': 'agent.v2', 'research_closed': True}, False),
    ({'research_version': 'agent.v1'}, False),
    ({}, False),
])
def test_pdf_completion_hint_keeps_separate_public_research(research, available):
    from app import collection
    run = dict(configuration={'collection': {}}, external_context=research,
               checkpoint={'collection': {'closed': True, 'message': 'использованы все разрешенные извлечения'}})
    hint = collection.context(None, None, run)
    assert 'Новое извлечение (aist_get_facts с question)' in hint
    assert 'сервер отклонит их без отправки' in hint
    assert ('aist_research и aist_read_web' in hint) is available


def reply(*, tool=False, inputs=100, outputs=20):
    output = [dict(type='reasoning', id='rs_fixture', encrypted_content='opaque-reasoning', summary=[], content=[])]
    if tool:
        output.append(dict(type='function_call', id='fc_fixture', call_id='call_fixture', name='aist_get_facts',
                           arguments='{"refs": []}', status='completed'))
    else:
        output.extend([
            dict(type='message', id='msg_commentary', role='assistant', phase='commentary', status='completed',
                 content=[dict(type='output_text', text='Проверяю источник.', annotations=[])]),
            dict(type='message', id='msg_final', role='assistant', phase='final_answer', status='completed',
                 content=[dict(type='output_text', text='Краткий ответ.', annotations=[])]),
        ])
    return dict(id='resp_fixture', object='response', model='gpt-6-astra', status='completed', output=output,
                error=None, service_tier='default', reasoning=dict(effort='xhigh', mode='standard'),
                usage=dict(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs+outputs,
                           input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                           output_tokens_details=dict(reasoning_tokens=10)))


@pytest.mark.parametrize('tool', [False, True])
def test_terminal_stream_preserves_original_items_and_phase(tool):
    body = reply(tool=tool)
    before = deepcopy(body)
    decoded = responses.response(responses.to_sse(body).encode())
    assert decoded == before == body
    assert responses.valid_content(decoded, 'gpt-6-astra')
    if tool:
        assert responses.tool_id(decoded['output'][1]) == 'call_fixture|fc_fixture'
    else:
        assert responses.final_text(decoded) == 'Краткий ответ.'
        assert rewritten_text(decoded, False, 'Длинный исходный абзац, который нужно сократить.', responses=True) == 'Краткий ответ.'


def test_keepalive_does_not_change_completion_or_authorize_partial_tools():
    body = reply(tool=True)
    heartbeat = dict(type='keepalive', sequence_number=17)
    created = dict(type='response.created', response=dict(body, output=[], status='in_progress'))
    completed = dict(type='response.completed', response=body)
    def stream(events):
        return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    assert responses.response(stream([heartbeat, created, heartbeat, completed, heartbeat])) == body
    for events in ([created, heartbeat], [created, heartbeat, completed, heartbeat, completed]):
        with pytest.raises(ValueError):
            responses.response(stream(events))
    for invalid in (dict(heartbeat, response=body), dict(heartbeat, sequence_number=True),
                    dict(heartbeat, sequence_number=-1), dict(type='keepalive')):
        with pytest.raises(ValueError):
            responses.response(stream([created, invalid, completed]))


@pytest.mark.parametrize('mutation', ['missing_terminal', 'incomplete_frame', 'conflicting_id', 'after_terminal', 'conflicting_status'])
def test_broken_stream_never_becomes_a_completion(mutation):
    body = reply(tool=True)
    created = dict(type='response.created', response=dict(body, output=[], status='in_progress'))
    completed = dict(type='response.completed', response=body)
    if mutation == 'conflicting_id':
        created['response']['id'] = 'resp_other'
    if mutation == 'conflicting_status':
        completed['response']['status'] = 'incomplete'
    events = [created] if mutation == 'missing_terminal' else [created, completed]
    if mutation == 'after_terminal':
        events.append(completed)
    raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    if mutation == 'incomplete_frame':
        raw = raw.rstrip()
    with pytest.raises(ValueError):
        responses.response(raw.encode())


@pytest.mark.parametrize('kind', ['configuration_update', 'additional_tools', 'item_reference', 'compaction'])
def test_history_cannot_override_effort_or_supply_remote_state(kind):
    with pytest.raises(ApiError):
        responses.request(OPENAI, dict(model=OPENAI['model'], input=[dict(type=kind, reasoning=dict(effort='low'))]))


@pytest.mark.parametrize('content', [[], [dict(type='reasoning_text', text='Synthetic reasoning fixture.')]])
def test_reasoning_content_is_replayed_unchanged(content):
    item = dict(type='reasoning', id='rs_fixture', summary=[], content=content, encrypted_content='opaque-fixture')
    outgoing = responses.request(OPENAI, dict(input=[item]))
    assert outgoing['input'] == [item]


@pytest.mark.parametrize('content', [None, 'text', [dict(type='configuration_update', text='override')],
                                   [dict(type='reasoning_text', text='fixture', extra=True)]])
def test_reasoning_content_rejects_unknown_shapes(content):
    with pytest.raises(ApiError):
        responses.request(OPENAI, dict(input=[dict(type='reasoning', summary=[], content=content, encrypted_content='opaque')]))


@pytest.mark.parametrize('field,value', [('previous_response_id', 'resp_prior'), ('conversation', 'conv_prior'), ('context_management', [])])
def test_hidden_context_controls_are_rejected(field, value):
    with pytest.raises(ApiError):
        responses.request(OPENAI, dict(input=[dict(role='user', content='Вопрос')], **{field: value}))


@pytest.mark.parametrize('choice', [dict(type='function', name=[]), dict(type='web_search', name='search'),
                                  dict(type='function', name='unavailable')])
def test_tool_choice_cannot_enable_an_undeclared_tool(choice):
    with pytest.raises(ApiError):
        responses.request(OPENAI, dict(input=[dict(role='user', content='Вопрос')], tool_choice=choice))


@pytest.mark.parametrize('purpose,edit', [('agent', False), ('extraction', False), ('agent', True)])
def test_direct_paths_pin_xhigh_and_preserve_output_budget(purpose, edit):
    run = dict(input=dict(model_profile_version_id=OPENAI_ID), configuration={})
    if edit:
        run['input']['edit_request'] = {}
    request = dict(messages=[dict(role='system', content='Верни JSON.'), dict(role='user', content='Данные')])
    if purpose == 'extraction':
        request['response_format'] = dict(type='json_object')
        request['messages'][-1]['content'] = [dict(type='text', text='Страница'),
            dict(type='image', source=dict(type='base64', media_type='image/jpeg', data='fixture'))]
    outgoing = model_request(run, purpose, request)
    assert outgoing['model'] == 'gpt-6-astra'
    assert outgoing['reasoning'] == dict(effort='xhigh', mode='standard')
    assert outgoing['max_output_tokens'] == 64000
    assert outgoing['prompt_cache_options'] == dict(mode='explicit')
    assert outgoing['store'] is False and outgoing['truncation'] == 'disabled'
    if purpose == 'extraction':
        assert outgoing['text']['format']['type'] == 'json_object'
        assert outgoing['input'][-1]['content'][1] == dict(type='input_image', detail='original', image_url='data:image/jpeg;base64,fixture')


def test_function_schema_remains_non_strict_and_history_is_not_normalized():
    body = reply(tool=True)
    request = dict(input=[dict(role='user', content='Задача'), *body['output']],
                   tools=[dict(type='function', name='aist_get_facts', parameters={'type': 'object', 'properties': {}}, strict=True)])
    outgoing = responses.request(OPENAI, request)
    assert outgoing['tools'][0]['strict'] is False
    assert outgoing['input'][1:] == body['output']
    assert request['tools'][0]['strict'] is True
    request['tools'][0]['async'] = True
    with pytest.raises(ApiError):
        responses.request(OPENAI, request)


def test_cache_marker_in_content_is_rejected():
    with pytest.raises(ApiError):
        responses.request(OPENAI, dict(input=[dict(role='user', content=[dict(type='input_text', text='Текст',
            prompt_cache_breakpoint=dict(mode='explicit'))])]))


def test_cache_prefix_is_stable_and_scope_is_server_owned():
    run = dict(id='run_one', owner_id='usr_one', project_id='project_one', executor_session_id='agent:analyst:one',
               input=dict(model_profile_version_id=OPENAI_ID), configuration=dict(policy=policy(cache=True)))
    source = dict(input=[dict(role='developer', content='Правила'), dict(role='user', content='Источники')],
                  prompt_cache_key='client_key', prompt_cache_options=dict(mode='implicit', ttl='12h'))
    before = deepcopy(source)
    first = model_request(run, 'agent', source)
    history = [*reply(tool=True)['output'], dict(type='function_call_output', call_id='call_fixture', output='{"facts":[]}')]
    second = model_request(run, 'agent', dict(source, input=[*source['input'], *history]))
    assert first['input'] == second['input'][:2]
    assert second['input'][2:] == history
    assert source == before
    assert first['prompt_cache_options'] == dict(mode='explicit', ttl='30m')
    assert first['input'][1]['content'] == [dict(type='input_text', text='Источники', prompt_cache_breakpoint=dict(mode='explicit'))]
    assert first['prompt_cache_key'] == second['prompt_cache_key'] != 'client_key'
    assert len(first['prompt_cache_key']) == 64
    for field in ('owner_id', 'project_id', 'id', 'executor_session_id'):
        assert model_request(dict(run, **{field: 'other'}), 'agent', source)['prompt_cache_key'] != first['prompt_cache_key']
    for budget, outgoing in [('Осталось 8', first), ('Осталось 7', second)]:
        final = responses.with_context(outgoing, budget)
        assert final['input'][:-1] == outgoing['input']
        assert final['input'][-1] == dict(role='developer', content=budget)
    legacy = model_request(dict(run, configuration=dict(policy=policy())), 'agent', source)
    assert legacy['input'] == source['input'] and 'prompt_cache_key' not in legacy
    assert responses.with_context(legacy, 'Бюджет')['input'][0]['content'] == 'Бюджет'
    for purpose, request in [('extraction', source), ('agent', dict(messages=source['input']))]:
        assert 'prompt_cache_key' not in model_request(run, purpose, request)
    edited = dict(run, input=dict(run['input'], edit_request={}))
    assert 'prompt_cache_key' not in model_request(edited, 'agent', source)
    assert first['model'] == 'gpt-6-astra' and first['reasoning'] == dict(effort='xhigh', mode='standard')
    assert first['service_tier'] == 'default'


def cached_reply(*, inputs=10000, cached=4000, short=3000, long=1000):
    value = reply(inputs=inputs)
    value['usage']['input_tokens_details'] = dict(cached_tokens=cached, cache_write_tokens=short+long,
        cache_write_tokens_details=dict(ttl_30m_tokens=short, ttl_12h_tokens=long))
    return value


@pytest.mark.parametrize('inputs,cached,short,long,expected', [
    (10000, 4000, 3000, 1000, '0.0875'),
    (10000, 0, 10000, 0, '0.126'),
    (10000, 10000, 0, 0, '0.011'),
    (272000, 272000, 0, 0, '0.273'),
    (272001, 272000, 1, 0, '0.545525'),
])
def test_cache_prices_replace_ordinary_input_without_double_counting(inputs, cached, short, long, expected):
    from app.contracts import validate_definition
    value = cached_reply(inputs=inputs, cached=cached, short=short, long=long)
    assert responses_estimate(value, policy(cache=True)) == expected
    assert responses_estimate(value, policy()) is None
    _, tokens, valid = response_fields(value, protocol='https_openai_responses')
    validate_definition('UsageTokens', tokens)
    assert valid and tokens['input']['value'] == inputs
    assert tokens['cached_input']['value'] == cached
    assert tokens['cache_write_30m_input']['value'] == short
    assert tokens['cache_write_12h_input']['value'] == long


@pytest.mark.parametrize('mutation', ['no_details', 'no_reads', 'no_writes', 'no_ttl', 'one_ttl', 'extra_ttl',
    'negative', 'boolean', 'ttl_boolean', 'ttl_sum', 'overflow', 'wrong_tier', 'total', 'reasoning'])
def test_cache_usage_must_be_complete_and_consistent(mutation):
    value = cached_reply()
    usage = value['usage']
    details = usage['input_tokens_details']
    ttl = details['cache_write_tokens_details']
    if mutation == 'no_details':usage.pop('input_tokens_details')
    elif mutation == 'no_reads':details.pop('cached_tokens')
    elif mutation == 'no_writes':details.pop('cache_write_tokens')
    elif mutation == 'no_ttl':details.pop('cache_write_tokens_details')
    elif mutation == 'one_ttl':ttl.pop('ttl_12h_tokens')
    elif mutation == 'extra_ttl':ttl['ttl_other_tokens'] = 1
    elif mutation == 'negative':details['cached_tokens'] = -1
    elif mutation == 'boolean':details['cached_tokens'] = True
    elif mutation == 'ttl_boolean':ttl['ttl_30m_tokens'] = True
    elif mutation == 'ttl_sum':ttl['ttl_30m_tokens'] += 1
    elif mutation == 'overflow':details['cached_tokens'] = 7000
    elif mutation == 'wrong_tier':value['service_tier'] = 'flex'
    elif mutation == 'total':usage['total_tokens'] += 1
    else:usage['output_tokens_details']['reasoning_tokens'] = 21
    assert responses_estimate(value, policy(cache=True)) is None


def test_zero_cache_writes_accept_archived_usage_but_not_unknown_counters():
    value = reply()
    assert responses_estimate(value, policy(cache=True)) == '0.002'
    value['usage']['input_tokens_details'] = dict(cached_tokens=0)
    assert responses_estimate(value, policy(cache=True)) is None
    assert responses_estimate(value, policy()) == '0.002'


def test_cache_policy_pins_rates_without_changing_access_or_ceiling():
    value = policy(cache=True)
    validate_demo_policy(value)
    assert value['total_authorized_usd'] == policy()['total_authorized_usd']
    assert value['demo_access'] == policy()['demo_access'] == dict(enabled=False, owner_ids=[])
    for field in ('cache_read_per_million', 'cache_write_30m_per_million', 'cache_write_12h_per_million',
                  'long_context_cache_read_per_million', 'long_context_cache_write_30m_per_million',
                  'long_context_cache_write_12h_per_million'):
        with pytest.raises(ValueError):
            validate_demo_policy(dict(value, **{field: '0.1'}))


@pytest.mark.parametrize('inputs,expected', [(272000, '2.721'), (272001, '5.44152')])
def test_long_context_price_applies_to_entire_request(inputs, expected):
    assert responses_estimate(reply(inputs=inputs), policy()) == expected


def test_optional_reasoning_counter_does_not_destroy_known_cost():
    body = reply()
    body['usage']['output_tokens_details'] = {}
    body['usage']['input_tokens_details'] = {}
    _, tokens, valid = response_fields(body, live=True, protocol='https_openai_responses')
    assert valid and tokens['reasoning_output']['value'] is None
    assert tokens['cached_input']['value'] is None
    assert responses_estimate(body, policy()) == '0.002'


@pytest.mark.parametrize('kind', ['cached', 'write', 'tier', 'boolean', 'reasoning_overflow', 'total'])
def test_unpriced_usage_keeps_the_reservation(kind):
    body = reply()
    if kind in ('cached', 'write'):
        body['usage']['input_tokens_details']['cached_tokens' if kind == 'cached' else 'cache_write_tokens'] = 1
    elif kind == 'tier':
        body['service_tier'] = 'priority'
    elif kind == 'boolean':
        body['usage']['input_tokens'] = True
    elif kind == 'reasoning_overflow':
        body['usage']['output_tokens_details']['reasoning_tokens'] = 21
    else:
        body['usage']['total_tokens'] = 119
    assert responses_estimate(body, policy()) is None


def test_openai_policy_is_separate_from_old_demo_permission():
    value = policy()
    validate_demo_policy(value)
    assert value['total_authorized_usd'] == '80'
    for key, wrong in [('budget_scope', 'anthropic-authorized-total'), ('quote_usd', '15'),
                       ('model_profile_version_id', FABLE_VISUAL_ID), ('long_context_input_per_million', '10')]:
        invalid = dict(value, **{key: wrong})
        with pytest.raises(ValueError):
            validate_demo_policy(invalid)
    with pytest.raises(ApiError):
        profile_for(dict(model_profile_version_id='model_unknown-v1'))


@pytest.mark.parametrize('kind', ['incomplete', 'refusal', 'other_model', 'lower_effort', 'duplicate_call'])
def test_no_tool_is_admitted_from_unusable_response(kind):
    body = reply(tool=True)
    if kind == 'incomplete':
        body['status'] = 'incomplete'
    elif kind == 'refusal':
        body['output'] = [dict(type='message', id='msg_refusal', role='assistant',
                               content=[dict(type='refusal', refusal='Отказ')])]
    elif kind == 'other_model':
        body['model'] = 'other'
    elif kind == 'lower_effort':
        body['reasoning']['effort'] = 'high'
    else:
        body['output'].append(dict(body['output'][-1], id='fc_second'))
    assert not responses.valid_content(body, 'gpt-6-astra')
