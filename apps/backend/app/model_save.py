"""Model content becomes the existing source-checked save operation."""
from copy import deepcopy

from .domain import refuse
from .errors import ApiError
from .normalization import checked, fingerprint, decode_json


FORMAT_FAILURE_MESSAGE = 'Модель вернула документ в некорректном формате. Черновик не сохранен'
FORMAT_CORRECTION = 'Верни цельный корректный JSON-объект content.'


def strict_object(value, field='arguments'):
    if isinstance(value, str):
        try:
            value = decode_json(value, diagnostic=True)
        except ApiError as error:
            refuse(f"{field}: {error.body['error']['message']}. {FORMAT_CORRECTION}")
    if not isinstance(value, dict):
        refuse(f'{field}: требуется JSON-объект. {FORMAT_CORRECTION}')
    return value


def content_args(args):
    """A strict conversion also replays archived public tool arguments."""
    args = strict_object(args)
    optional = {key: args[key] for key in ('questions', 'news_events', 'ownership_review') if key in args}
    fields = set(args) - optional.keys()
    if fields == {'content'}:
        return {'content': strict_object(args['content'], 'content'), **optional}
    if fields == {'payload', 'idempotency_key', 'artifact_id', 'parent_version_id', 'expected_revision'}:
        payload = strict_object(args['payload'], 'payload')
        if set(payload) == {'content', 'schema_version', 'result_type'} and payload['schema_version'] == '1.0.0':
            return {'content': strict_object(payload['content'], 'payload.content'), **optional}
    refuse('Сохранение принимает content с текстом, структурой и ссылками, questions, news_events и ownership_review для записки.')


def save_operation(run, args, base=None):
    command = deepcopy(content_args(args))
    content = command['content']
    existing = {'section_id': set(), 'block_id': set(), 'row_id': set()}
    if base:
        for section in base['payload']['content'].get('sections', []):
            existing['section_id'].add(section['section_id'])
            for block in section['blocks']:
                existing['block_id'].add(block['block_id'])
                existing['row_id'].update(row['row_id'] for row in block.get('rows', []))
    # Ignore invented IDs when comparing operations. Identity of retained blocks
    # remains part of the content, including moved human editorial paragraphs.
    objects = []
    for si, section in enumerate(content.get('sections', [])):
        objects.append((section, 'section_id', f'section/{si}'))
        for bi, block in enumerate(section.get('blocks', [])):
            objects.append((block, 'block_id', f'section/{si}/block/{bi}'))
            for ri, row in enumerate(block.get('rows', [])):
                objects.append((row, 'row_id', f'section/{si}/block/{bi}/row/{ri}'))
    for obj, key, _ in objects:
        if obj.get(key) not in existing[key]:
            obj.pop(key, None)
    operation = fingerprint({'run': run['id'], **command})
    for obj, key, path in objects:
        if key not in obj:
            obj[key] = key.split('_')[0] + '_' + fingerprint({'operation': operation, 'path': path})[:32]
    state = run['input']
    return checked('SaveArtifactArgs', dict(
        idempotency_key='save_' + operation, expected_revision=state['expected_artifact_revision'],
        artifact_id=state['target_artifact_id'], parent_version_id=state['base_version_id'],
        payload=dict(result_type=state['requested_result_type'], schema_version='1.0.0', content=content),
        **{key: command[key] for key in ('questions', 'news_events', 'ownership_review') if key in command}))


def failed_command_digest(args, base=None):
    try:
        command = deepcopy(content_args(args))
        content = command['content']
    except ApiError:
        if isinstance(args,dict):
            args={key:value for key,value in args.items()
                  if key not in ('idempotency_key','expected_revision','artifact_id','parent_version_id')}
        return fingerprint(args)
    existing = set()
    if base:
        for section in base['payload']['content'].get('sections', []):
            existing.add(section['section_id'])
            for block in section['blocks']:
                existing.add(block['block_id'])
                existing.update(row['row_id'] for row in block.get('rows', []))
    def strip_ids(value):
        if isinstance(value, dict):
            return {key: strip_ids(item) for key, item in value.items()
                    if key not in ('section_id', 'block_id', 'row_id') or (isinstance(item,str) and item in existing)}
        if isinstance(value, list):
            return [strip_ids(item) for item in value]
        return value
    normalized = strip_ids(content)
    # Preserve the historical digest for commands without the optional field.
    optional = {key: command[key] for key in ('questions', 'news_events', 'ownership_review') if key in command}
    return fingerprint(dict(content=normalized, **optional) if optional else normalized)
