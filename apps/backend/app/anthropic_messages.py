"""Native Messages framing at the existing accounted transport boundary."""
from copy import deepcopy
import json
import time
from pathlib import Path


class StreamAudit:
    """Receipt timings only; the complete response parser still decides whether a tool may run."""
    def __init__(self, path, started, limit):
        self.path = path
        self.started = started
        self.pending = b''
        self.thinking_blocks = set()
        self.data = dict(attempt_limit_seconds=limit, bytes=0, events=0, malformed_events=0,
                         stages_seconds=dict.fromkeys(('send_started', 'sent', 'headers', 'first_event',
                             'thinking_closed', 'tool_use_started', 'arguments_started', 'message_stop',
                             'read_complete', 'accounting_complete', 'attempt_finished')))

    def mark(self, stage):
        if self.data['stages_seconds'][stage] is None:
            self.data['stages_seconds'][stage] = time.monotonic() - self.started

    def feed(self, chunk):
        self.data['bytes'] += len(chunk)
        self.pending = (self.pending + chunk).replace(b'\r\n', b'\n')
        while b'\n\n' in self.pending:
            frame, self.pending = self.pending.split(b'\n\n', 1)
            data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b'data:'))
            if not data:
                continue
            self.mark('first_event')
            self.data['events'] += 1
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError):
                self.data['malformed_events'] += 1
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'content_block_start':
                block = event.get('content_block') or {}
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'thinking':
                    self.thinking_blocks.add(event.get('index'))
                elif block.get('type') == 'tool_use':
                    self.mark('tool_use_started')
            elif kind == 'content_block_stop' and event.get('index') in self.thinking_blocks:
                self.mark('thinking_closed')
            elif kind == 'content_block_delta':
                delta = event.get('delta') or {}
                if not isinstance(delta, dict):
                    continue
                if delta.get('type') == 'input_json_delta' and delta.get('partial_json'):
                    self.mark('arguments_started')
            elif kind == 'message_stop':
                self.mark('message_stop')
        self.write()

    def write(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        temporary.replace(self.path)


def credential(path):
    token = Path(path).read_text().strip().removeprefix('ANTHROPIC_API_KEY=')
    if not token or '\n' in token or '\r' in token:
        raise ValueError('Invalid Anthropic credential')
    return token


def with_context(request, context):
    outgoing = deepcopy(request)
    system = outgoing.get('system', [])
    if isinstance(system, str):
        system = [dict(type='text', text=system)]
    outgoing['system'] = [dict(type='text', text=context), *system]
    return outgoing


def response(raw):
    """Only a message_stop with all blocks closed constitutes a complete stream."""
    message = None
    open_blocks = {}
    stopped = False
    for event in raw.decode().replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        if not data:
            continue
        value = json.loads(data)
        kind = value['type']
        if stopped:
            raise ValueError('Event after message_stop')
        if kind == 'message_start':
            if message is not None:
                raise ValueError('Duplicate message_start')
            message = deepcopy(value['message'])
            message['content'] = []
        elif kind == 'content_block_start':
            index = value['index']
            if message is None or index != len(message['content']):
                raise ValueError('Unexpected block index')
            block = deepcopy(value['content_block'])
            message['content'].append(block)
            open_blocks[index] = ''
        elif kind == 'content_block_delta':
            index = value['index']
            if index not in open_blocks:
                raise ValueError('Delta outside a block')
            block, delta = message['content'][index], value['delta']
            if delta['type'] == 'input_json_delta':
                open_blocks[index] += delta['partial_json']
            elif delta['type'] in ('text_delta', 'thinking_delta', 'signature_delta'):
                field = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}[delta['type']]
                block[field] = block.get(field, '') + delta[field]
            else:
                raise ValueError('Unsupported native content delta')
        elif kind == 'content_block_stop':
            index = value['index']
            partial = open_blocks.pop(index)
            if partial:
                message['content'][index]['input'] = json.loads(partial)
        elif kind == 'message_delta':
            message.update(value['delta'])
            message['usage'].update(value.get('usage', {}))
        elif kind == 'message_stop':
            if message is None or open_blocks or not message.get('stop_reason'):
                raise ValueError('Incomplete native response')
            stopped = True
        elif kind != 'ping':
            raise ValueError('Native stream error')
    if not stopped:
        raise ValueError('Missing message_stop')
    return message


def to_sse(message):
    """Give OpenClaw a complete, already accounted response using its native stream protocol."""
    events = [dict(type='message_start', message=dict(message, content=[], stop_reason=None, stop_sequence=None))]
    for index, original in enumerate(message['content']):
        block = deepcopy(original)
        delta = None
        if block['type'] == 'tool_use':
            delta = dict(type='input_json_delta', partial_json=json.dumps(block['input'], ensure_ascii=False))
            block['input'] = {}
        events.append(dict(type='content_block_start', index=index, content_block=block))
        if delta:
            events.append(dict(type='content_block_delta', index=index, delta=delta))
        events.append(dict(type='content_block_stop', index=index))
    events.extend([dict(type='message_delta', delta=dict(stop_reason=message['stop_reason'], stop_sequence=message.get('stop_sequence')),
                        usage=message['usage']), dict(type='message_stop')])
    return ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events)


def valid_content(body):
    content = body.get('content')
    if body.get('type') != 'message' or body.get('role') != 'assistant' or not isinstance(content, list) or not content:
        return False
    ids = set()
    useful = False
    for block in content:
        if not isinstance(block, dict):
            return False
        kind = block.get('type')
        if kind == 'tool_use':
            identifier = block.get('id')
            if (not isinstance(identifier, str) or not identifier or identifier in ids
                    or not isinstance(block.get('name'), str) or not isinstance(block.get('input'), dict)):
                return False
            ids.add(identifier)
            useful = True
        elif kind == 'text':
            if not isinstance(block.get('text'), str):
                return False
            useful = useful or bool(block['text'].strip())
        elif kind == 'thinking':
            if not isinstance(block.get('thinking'), str) or not isinstance(block.get('signature'), str) or not block['signature']:
                return False
        elif kind == 'redacted_thinking':
            if not isinstance(block.get('data'), str):
                return False
        else:
            return False
    return useful and body.get('stop_reason') == ('tool_use' if ids else 'end_turn')
