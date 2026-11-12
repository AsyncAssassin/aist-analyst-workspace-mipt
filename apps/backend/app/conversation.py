"""Bounded server history. Conversation text is context, never financial evidence."""
from .errors import ApiError
from .runs import execute, json_value

HISTORY_MESSAGES = 12
HISTORY_BYTES = 12000
DIALOGUE_BYTES = 24000


def note_context(service, connection, actor, project, body, pins):
    """Freeze a readable version without making it the parent or target of the answer."""
    version_id = body.get('context_version_id')
    if version_id is None:
        return None
    if (body['skill_version_id'] != 'skill_source-answer-v3' or body['requested_result_type'] != 'evidence_answer'
            or body['base_version_id'] is not None or body['expected_artifact_revision'] != 0):
        raise ApiError('unsupported_result_type')
    saved = service.scoped(connection, actor, project, 'artifact_versions', version_id)
    if saved['data']['payload']['result_type'] != 'analytical_note':
        raise ApiError('unsupported_result_type')
    allowed = {(pin['source_version_id'], pin['sha256']) for pin in pins}
    required = {(pin['source_version_id'], pin['sha256']) for pin in saved['data']['provenance']['source_versions']}
    if not required <= allowed:
        raise ApiError('resource_not_found')
    from .versions import view
    return view(connection, saved)


def snapshot(service, connection, actor, project, conversation, pins, *, through=None,
             max_messages=HISTORY_MESSAGES, max_bytes=HISTORY_BYTES, allowed_chats=None):
    service.scoped(connection, actor, project, 'conversations', conversation)
    rows = list(execute(connection, '''SELECT m.*, r.input AS run_input FROM aist.messages m
        LEFT JOIN aist.runs r ON r.id=m.run_id AND r.owner_id=m.owner_id AND r.project_id=m.project_id
        WHERE m.owner_id=:actor AND m.project_id=:project AND m.conversation_id=:conversation
        AND m.sequence<=:through ORDER BY m.sequence DESC LIMIT :limit''', actor=actor, project=project,
        conversation=conversation, through=through if through is not None else 9007199254740991, limit=max_messages + 1).mappings())
    allowed = {(p['source_version_id'], p['sha256']) for p in pins}
    if pins and through is not None:
        # Match only registrations of the exact selected origin, including its SHA.
        related = execute(connection, """SELECT v.id,v.sha256 FROM aist.source_versions v
            JOIN aist.source_versions pin ON coalesce(v.origin_version_id,v.id)=coalesce(pin.origin_version_id,pin.id)
            WHERE v.owner_id=:actor AND v.project_id=:project AND pin.owner_id=:actor AND pin.id=ANY(:ids)""",
            actor=actor, project=project, ids=[p['source_version_id'] for p in pins]).mappings()
        allowed.update((v['id'], v['sha256']) for v in related)
    messages = []
    truncated = len(rows) > max_messages
    for message in rows[:max_messages]:
        item = {k: message[k] for k in ('id', 'sequence', 'role', 'text', 'artifact_version_id')}
        run_input = message.get('run_input') or {}
        sources = {(pin['source_version_id'], pin['sha256']) for pin in run_input.get('source_versions', [])}
        context_id = run_input.get('context_version_id')
        if context_id:
            context = service.scoped(connection, actor, project, 'artifact_versions', context_id)['data']
            sources |= {(pin['source_version_id'], pin['sha256']) for pin in context['provenance']['source_versions']}
            item['context_version_id'] = context_id
        if message['artifact_version_id']:
            version = service.scoped(connection, actor, project, 'artifact_versions', message['artifact_version_id'])['data']
            sources |= {(p['source_version_id'], p['sha256']) for p in version['provenance']['source_versions']}
        nested = (run_input.get('project_context') or {}).get('chats', [])
        depends_on_other_history = any((allowed_chats or {}).get(chat['id'], 0) < chat['through_sequence']
                                       for chat in nested)
        if not sources <= allowed or depends_on_other_history:
            item.update(text=('Сообщение относилось к переписке, исключённой из запроса.' if depends_on_other_history else
                              'Сообщение относилось к источникам, которые теперь исключены.'),
                        artifact_version_id=None, excluded_sources=True)
            item.pop('context_version_id', None)
        if len(json_value([item, *messages]).encode()) > max_bytes:
            truncated = True
            if through is not None and not messages:
                item['truncated_text'] = True
                original_text = item.pop('text')
                room = max_bytes - len(json_value([dict(item, text='')]).encode())
                item['text'] = original_text.encode()[:max(0, room)].decode(errors='ignore')
                # Escaped characters can take more bytes in JSON than in UTF-8.
                while len(json_value([item]).encode()) > max_bytes and item['text']:
                    item['text'] = item['text'][:len(item['text'])//2]
                if item['text']:
                    messages.append(item)
            break
        messages.insert(0, item)
    return dict(messages=messages, truncated=truncated, max_messages=max_messages, max_bytes=max_bytes)


def selected_history(service, c, actor, current, pins, chats):
    if not chats:
        return snapshot(service, c, actor, current['project_id'], current['id'], pins)
    count = len(chats) + 1
    # Reserve the serialized headers too, so the complete input stays within one history budget.
    headers = [{k: chat[k] for k in ('id','title','through_sequence')} for chat in chats]
    overhead = len(json_value(headers).encode()) + 256 * count
    bounds = dict(max_messages=HISTORY_MESSAGES // count, max_bytes=(HISTORY_BYTES - overhead) // count)
    result = snapshot(service, c, actor, current['project_id'], current['id'], pins, allowed_chats={x['id']:x['through_sequence'] for x in chats}, **bounds)
    result['project_chats'] = [dict(header, **snapshot(service, c, actor, chat['project_id'], chat['id'],
        pins, through=chat['through_sequence'], **bounds)) for chat, header in zip(chats, headers)]
    return result


def append_dialogue(checkpoint, role, text):
    checkpoint = dict(checkpoint or {})
    dialogue = [*checkpoint.get('dialogue', []), dict(role=role, text=text)]
    if len(json_value(dialogue).encode()) > DIALOGUE_BYTES:
        raise ApiError('too_large')
    return dict(checkpoint, dialogue=dialogue)
