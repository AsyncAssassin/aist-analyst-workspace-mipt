"""Responses wire format. AIST retains transport, accounting and tool authority."""
from copy import deepcopy
import json
from pathlib import Path

from .errors import ApiError


REQUEST_FIELDS = {
    'model', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls',
    'reasoning', 'text', 'max_output_tokens', 'stream', 'store', 'background',
    'truncation', 'include', 'service_tier', 'prompt_cache_options',
    'prompt_cache_retention', 'prompt_cache_key', 'metadata',
    'temperature', 'top_p', 'top_logprobs', 'logprobs',
}
ITEM_FIELDS = {
    'message': {'type', 'id', 'role', 'content', 'status', 'phase'},
    'reasoning': {'type', 'id', 'summary', 'content', 'encrypted_content', 'status'},
    'function_call': {'type', 'id', 'call_id', 'name', 'arguments', 'status'},
    'function_call_output': {'type', 'id', 'call_id', 'output', 'status'},
}
MAX_EVENT_BYTES = 8 * 1024 * 1024


def credential(path):
    token = Path(path).read_text().strip().removeprefix('OPENAI_API_KEY=')
    if not token or '\n' in token or '\r' in token:
        raise ValueError('Invalid OpenAI credential')
    return token


def with_context(request, context):
    outgoing = deepcopy(request)
    message = dict(role='developer', content=context)
    if outgoing.get('prompt_cache_key'):
        # Текущий бюджет меняется каждый шаг и не входит в повторяемый префикс.
        outgoing['input'].append(message)
    else:
        outgoing['input'].insert(0, message)
    return outgoing


def validate_input(items):
    if not isinstance(items, list) or not items:
        raise ApiError('validation_error')
    for item in items:
        if not isinstance(item, dict):
            raise ApiError('validation_error')
        kind = item.get('type', 'message')
        if kind not in ITEM_FIELDS or set(item) - ITEM_FIELDS[kind]:
            raise ApiError('validation_error')
        if kind == 'message':
            if item.get('role') not in ('system', 'developer', 'user', 'assistant'):
                raise ApiError('validation_error')
            if 'phase' in item and (item['role'] != 'assistant' or item['phase'] not in ('commentary', 'final_answer')):
                raise ApiError('validation_error')
            content = item.get('content')
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                raise ApiError('validation_error')
            for block in content:
                if not isinstance(block, dict):
                    raise ApiError('validation_error')
                allowed = ({'type', 'image_url', 'detail'} if block.get('type') == 'input_image'
                           else {'type', 'text', 'annotations', 'logprobs'})
                if block.get('type') not in ('input_text', 'output_text', 'input_image') or set(block) - allowed:
                    raise ApiError('validation_error')
                if block['type'] == 'input_image':
                    if not isinstance(block.get('image_url'), str) or not block['image_url'].startswith('data:image/jpeg;base64,'):
                        raise ApiError('validation_error')
                elif not isinstance(block.get('text'), str):
                    raise ApiError('validation_error')
        elif kind == 'reasoning':
            if not isinstance(item.get('encrypted_content'), str) or not isinstance(item.get('summary', []), list):
                raise ApiError('validation_error')
            content = item.get('content', [])
            if not isinstance(content, list) or any(
                    not isinstance(block, dict) or set(block) != {'type', 'text'}
                    or block['type'] != 'reasoning_text' or not isinstance(block['text'], str)
                    for block in content):
                raise ApiError('validation_error')
        elif not isinstance(item.get('call_id'), str) or not item['call_id']:
            raise ApiError('validation_error')
        elif kind == 'function_call':
            if not isinstance(item.get('arguments'), str) or not isinstance(item.get('name'), str):
                raise ApiError('validation_error')
        elif not isinstance(item.get('output'), str):
            raise ApiError('validation_error')


def request(profile, outgoing, *, cache_key=None):
    """Pin controls after converting only AIST's direct extraction/rewrite messages."""
    outgoing = deepcopy(outgoing)
    if 'messages' in outgoing:
        items = []
        for message in outgoing.pop('messages'):
            content = message['content']
            if isinstance(content, list):
                blocks = []
                for block in content:
                    if block['type'] == 'text':
                        blocks.append(dict(type='input_text', text=block['text']))
                    elif block['type'] == 'image':
                        source = block['source']
                        blocks.append(dict(type='input_image', detail='original',
                                           image_url='data:' + source['media_type'] + ';base64,' + source['data']))
                    else:
                        raise ApiError('validation_error')
                content = blocks
            items.append(dict(role=message['role'], content=content))
        outgoing['input'] = items
        if outgoing.pop('response_format', None) is not None:
            outgoing['text'] = dict(format=dict(type='json_object'))
    if set(outgoing) - REQUEST_FIELDS:
        raise ApiError('validation_error')
    validate_input(outgoing.get('input'))
    tools = outgoing.get('tools', [])
    if not isinstance(tools, list):
        raise ApiError('validation_error')
    names = set()
    for tool in tools:
        if (not isinstance(tool, dict) or tool.get('type') != 'function'
                or set(tool) - {'type', 'name', 'description', 'parameters', 'strict'}
                or not isinstance(tool.get('name'), str) or tool['name'] in names
                or not isinstance(tool.get('parameters'), dict)):
            raise ApiError('tool_unavailable')
        names.add(tool['name'])
        tool['strict'] = False
    choice = outgoing.get('tool_choice', 'auto')
    if isinstance(choice, dict):
        if (set(choice) != {'type', 'name'} or choice['type'] != 'function'
                or not isinstance(choice['name'], str) or choice['name'] not in names):
            raise ApiError('tool_unavailable')
    elif choice not in ('auto', 'none', 'required'):
        raise ApiError('tool_unavailable')
    for field in ('temperature', 'top_p', 'top_logprobs', 'logprobs', 'prompt_cache_retention', 'prompt_cache_key'):
        outgoing.pop(field, None)
    outgoing.update(model=profile['model'], reasoning=dict(effort=profile['effort'], mode='standard'),
                    max_output_tokens=profile['max_tokens'], stream=True, store=False, background=False,
                    truncation='disabled', include=['reasoning.encrypted_content'], service_tier='default',
                    prompt_cache_options=dict(mode='explicit'), parallel_tool_calls=False)
    if cache_key:
        # Только начальная задача. История модели и результатов инструментов остается побайтно прежней.
        for item in outgoing['input']:
            if item.get('type', 'message') != 'message' or item.get('role') == 'assistant':
                break
            if item.get('role') != 'user':
                continue
            content = item['content']
            if isinstance(content, str):
                content = [dict(type='input_text', text=content)]
            if content and content[-1].get('type') == 'input_text':
                content[-1]['prompt_cache_breakpoint'] = dict(mode='explicit')
                item['content'] = content
                outgoing['prompt_cache_key'] = cache_key
                outgoing['prompt_cache_options'] = dict(mode='explicit', ttl='30m')
            break
    return outgoing


def response(raw):
    """The terminal Response is authoritative; deltas never authorize execution."""
    data = raw.decode('utf-8').replace('\r\n', '\n')
    position = 0
    terminal = None
    response_id = None
    while position < len(data):
        end = data.find('\n\n', position)
        if end < 0:
            if data[position:].strip():
                raise ValueError('Incomplete Responses event')
            break
        frame = data[position:end]
        position = end + 2
        if len(frame.encode()) > MAX_EVENT_BYTES:
            raise ValueError('Responses event exceeds limit')
        payload = '\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith('data:'))
        if not payload:
            continue
        if payload == '[DONE]' and terminal is not None:
            continue
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError('Invalid Responses stream order')
        kind = event.get('type')
        if kind == 'keepalive':
            # Служебный сигнал не содержит ответа, usage или разрешения на вызов инструмента.
            sequence = event.get('sequence_number')
            if set(event) != {'type', 'sequence_number'} or type(sequence) is not int or sequence < 0:
                raise ValueError('Invalid Responses keepalive')
            continue
        if terminal is not None:
            raise ValueError('Invalid Responses stream order')
        if kind == 'error':
            terminal = dict(status='failed', error=event, output=[], id=response_id, usage=None)
        elif kind in ('response.created', 'response.in_progress', 'response.completed', 'response.failed', 'response.incomplete'):
            value = event.get('response')
            if not isinstance(value, dict) or not isinstance(value.get('id'), str):
                raise ValueError('Missing Response identity')
            if response_id is not None and value['id'] != response_id:
                raise ValueError('Changed Response identity')
            response_id = value['id']
            if kind in ('response.completed', 'response.failed', 'response.incomplete'):
                if value.get('status') != kind.removeprefix('response.'):
                    raise ValueError('Conflicting terminal Response')
                terminal = value
        elif not isinstance(kind, str) or not kind.startswith('response.'):
            raise ValueError('Unsupported Responses event')
    if terminal is None:
        raise ValueError('Missing terminal Response')
    return terminal


def valid_content(body, model=None):
    if body.get('status') != 'completed' or body.get('error') is not None:
        return False
    if model is not None and body.get('model') != model:
        return False
    reasoning = body.get('reasoning')
    if reasoning is not None and (not isinstance(reasoning, dict)
            or reasoning.get('effort', 'xhigh') != 'xhigh' or reasoning.get('mode', 'standard') != 'standard'):
        return False
    output = body.get('output')
    if not isinstance(output, list) or not output:
        return False
    ids, calls = set(), set()
    useful = False
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
            return False
        ids.add(item['id'])
        kind = item.get('type')
        if item.get('status', 'completed') != 'completed':
            return False
        if kind == 'function_call':
            call_id = item.get('call_id')
            if (not isinstance(call_id, str) or not call_id or call_id in calls or '|' in call_id or '|' in item['id']
                    or len(call_id + '|' + item['id']) > 200 or item.get('async') is True
                    or not isinstance(item.get('name'), str) or not isinstance(item.get('arguments'), str)):
                return False
            try:
                if not isinstance(json.loads(item['arguments']), dict):
                    return False
            except (ValueError, RecursionError):
                return False
            calls.add(call_id)
            useful = True
        elif kind == 'message':
            if item.get('role') != 'assistant' or item.get('phase', 'final_answer') not in ('commentary', 'final_answer'):
                return False
            if not isinstance(item.get('content'), list):
                return False
            for block in item['content']:
                if not isinstance(block, dict) or block.get('type') != 'output_text' or not isinstance(block.get('text'), str):
                    return False
                useful = useful or bool(block['text'].strip())
        elif kind == 'reasoning':
            if not isinstance(item.get('encrypted_content'), str):
                return False
        else:
            return False
    return useful


def final_text(body):
    if not valid_content(body) or any(item['type'] == 'function_call' for item in body['output']):
        raise ApiError('validation_failed')
    messages = [item for item in body['output'] if item['type'] == 'message' and item.get('phase', 'final_answer') == 'final_answer']
    if len(messages) != 1:
        raise ApiError('validation_failed')
    return ''.join(block['text'] for block in messages[0]['content'])


def tool_id(item):
    return item['call_id'] + '|' + item['id']


def to_sse(body):
    """Replay a fully accounted terminal object to the installed OpenClaw parser."""
    events = [dict(type='response.created', response=dict(body, status='in_progress', output=[]))]
    for index, item in enumerate(body['output']):
        initial = deepcopy(item)
        if item['type'] == 'message':
            initial['content'] = []
        elif item['type'] == 'function_call':
            initial['arguments'] = ''
        events.append(dict(type='response.output_item.added', output_index=index, item=initial))
        if item['type'] == 'message':
            for part_index, block in enumerate(item['content']):
                identity = dict(output_index=index, item_id=item['id'], content_index=part_index)
                events.append(dict(type='response.content_part.added', **identity, part=dict(block, text='')))
                events.append(dict(type='response.output_text.delta', **identity, delta=block['text']))
                events.append(dict(type='response.output_text.done', **identity, text=block['text']))
                events.append(dict(type='response.content_part.done', **identity, part=block))
        elif item['type'] == 'function_call':
            events.append(dict(type='response.function_call_arguments.delta', output_index=index, item_id=item['id'], delta=item['arguments']))
            events.append(dict(type='response.function_call_arguments.done', output_index=index, item_id=item['id'], arguments=item['arguments']))
        events.append(dict(type='response.output_item.done', output_index=index, item=item))
    events.append(dict(type='response.completed', response=body))
    return ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(dict(event, sequence_number=index), ensure_ascii=False) + '\n\n'
                   for index, event in enumerate(events))
