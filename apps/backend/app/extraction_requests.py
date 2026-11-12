"""Extraction identity in the Run checkpoint, committed with the existing ModelCall reservation.

No independent send ledger: each operation names the accounted attempts, including overload retries.
A receipt can be ingested after recovery without another POST; known failures remain local to this Run.
"""
import json

from .errors import ApiError
from .normalization import fingerprint
from .runs import execute, json_value, row


def operations(run):
    return (run.get('checkpoint') or {}).get('extraction_operations', {})


def save(c, run, key, value):
    entries = dict(operations(run), **{key: value})
    checkpoint = dict(run.get('checkpoint') or {}, extraction_operations=entries)
    execute(c, 'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r', v=json_value(checkpoint), r=run['id'])
    run['checkpoint'] = checkpoint


def identity_key(meta):
    value = {key: value for key, value in meta.items() if key not in ('key', 'request_sha256', 'question')}
    value['question'] = ' '.join(meta['question'].split()).casefold() if meta['question'] is not None else None
    return fingerprint(value)


def metadata(run, pin, question, coverage, request, processor_version, *, visual_input=None, images=None):
    from copy import deepcopy
    from .extraction import VERSION, extraction_schema
    from .live_profile import PROFILE, profile_for
    from .transport import model_request
    normalized = ' '.join(question.split()).casefold() if question is not None else None
    identity_request = deepcopy(request)
    blocks = identity_request['messages'][-1]['content']
    content = json.loads(blocks[0]['text'] if isinstance(blocks, list) else blocks)
    content['question'] = normalized
    if isinstance(blocks, list):
        blocks[0]['text'] = json_value(content)
    else:
        identity_request['messages'][-1]['content'] = json_value(content)
    value = dict(source=pin, question=question, version=VERSION, processor_version=processor_version, coverage=coverage,
                 schema_sha256=fingerprint(extraction_schema()),
                 mode=run['configuration'].get('extraction_thinking', PROFILE['extraction_thinking']),
                 request_sha256=fingerprint(model_request(run, 'extraction', request)),
                 normalized_request_sha256=fingerprint(model_request(run, 'extraction', identity_request)))
    if 'prior_numeric_observations' in content:
        value['prior_numeric_observations'] = deepcopy(content['prior_numeric_observations'])
    profile = profile_for(run['input'])
    if profile['transport'] != 'https_chat_completions':
        value.update(model_profile_version_id=run['input']['model_profile_version_id'],
                     model_configuration_sha256=fingerprint(profile), mode=profile['extraction_thinking'])
    if visual_input is not None:
        text_request = deepcopy(identity_request)
        text_request['messages'][-1]['content'] = json_value(content)
        value.update(visual_input=visual_input,
                     input_sha256=fingerprint(dict(request=model_request(run, 'extraction', text_request), visual=visual_input)),
                     images=[{key: image[key] for key in ('page', 'sha256', 'byte_size')} for image in images or []])
    return dict(value, key=identity_key(value))


def matching_visual(saved, prepared):
    """Reuse only the same pinned preparation and a complete, internally consistent saved identity.

    Rendering is not a read prerequisite. The authenticated PDF SHA, parameters and ordered page
    selection identify the preparation; the committed receipt additionally binds the actual image hashes.
    """
    meta = saved.get('visual_request', saved)
    if meta.get('input_sha256') != prepared.get('input_sha256') or meta.get('visual_input') != prepared.get('visual_input'):
        return False
    for field in ('source', 'version', 'processor_version', 'coverage', 'schema_sha256', 'mode',
                  'model_profile_version_id', 'model_configuration_sha256'):
        if meta.get(field) != prepared.get(field):
            raise ApiError('outcome_unknown')
    if any(key not in meta for key in prepared):
        raise ApiError('outcome_unknown')
    images = meta.get('images', [])
    if not images and saved.get('error') and not saved.get('call_ids'):
        # A deterministic refusal before rendering is itself an exact preparation outcome.
        if all(meta[key] == value for key, value in prepared.items()):
            return True
        raise ApiError('outcome_unknown')
    if (meta.get('key') != identity_key({key: meta[key] for key in prepared})
            or [image.get('page') for image in images] != prepared['visual_input']['pages']
            or any(not isinstance(image.get('sha256'), str) or len(image['sha256']) != 64
                   or not isinstance(image.get('byte_size'), int) or image['byte_size'] <= 0 for image in images)):
        raise ApiError('outcome_unknown')
    return True


def prepared_operation(run, prepared):
    if 'visual_input' not in prepared:
        return prepared
    for operation in operations(run).values():
        if matching_visual(operation, prepared):
            return {key: operation[key] for key in prepared}
    return prepared


def failure(message, code='validation_failed'):
    error = ApiError(code)
    error.body['error']['message'] = message
    error.run_continues = True
    return error


def guard(c, run, meta):
    """Under the Run lock, reject uncertain sends before allowing bounded thematic collection."""
    if row(c, """SELECT id FROM aist.model_calls WHERE run_id=:r
              AND data->>'transport_state' IN ('reserved','attempted','sent','unknown') LIMIT 1""", r=run['id']):
        raise ApiError('outcome_unknown')
    from .collection import limits
    bounds = limits(run)
    if bounds is not None and bounds['max_extractions'] > 0:
        # The durable operation handles exact repeats; collection enforces quotas, closure and the reserve.
        return
    if not meta['coverage']['full_document']:
        return
    for prior in operations(run).values():
        if prior['source'] != meta['source'] or not prior['coverage']['full_document']:
            continue
        sent = row(c, """SELECT id FROM aist.model_calls WHERE run_id=:r AND id=ANY(:ids)
                       AND data->>'transport_state'<>'not_sent' LIMIT 1""", r=run['id'], ids=prior['call_ids'])
        if sent:
            from .collection import results_text
            from .extraction import recorded_extractions
            available = [entry for entry in recorded_extractions(run) if not entry.get('limitation')]
            cause = ' Причина прежнего отказа: ' + prior['error']['message'] if prior.get('error') else ''
            raise failure('Полный PDF уже передавался в этом Run с известным исходом. Повтор полного документа '
                          'не выполняется. ' + results_text(available) + cause
                          + ' Недостающие сведения отрази ограничением черновика или уточни на части страниц.')


def reserve(c, run, meta, call_id, retry_of):
    previous = operations(run).get(meta['key'])
    if retry_of is None:
        if previous:
            raise ApiError('outcome_unknown')
        guard(c, run, meta)
        previous = dict(meta, call_ids=[])
    elif (previous is None or previous['call_ids'][-1] != retry_of
          or previous.get('body_sha256') != meta.get('body_sha256')):
        raise ApiError('outcome_unknown')
    save(c, run, meta['key'], dict(previous, call_ids=[*previous['call_ids'], call_id]))


def observed(c, run, call, payload):
    for key, operation in operations(run).items():
        if call['id'] in operation['call_ids']:
            # The receipt and the accounting outcome commit together. Late/suppressed replies never become facts.
            if call['provider_outcome'] == 'success' and call['delivery'] == 'delivered':
                save(c, run, key, dict(operation, response=payload, response_call_id=call['id']))
            return


def record_failure(c, run, meta, error, call_id=None):
    """Keep a server refusal as request history, including its coverage and accounted call.

    Neither a send guard nor a rejected candidate establishes a missing observation in the note.
    Document checks use coverage.unresolved, classified skipped observations and the note's own bases.
    """
    from .extraction import record_result
    limitation = error.body['error']['message']
    entry = dict(source=meta['source'], version=meta['version'], question=meta['question'],
                 request_key=meta['key'], call_id=call_id or 'context_' + meta['key'], fact_refs=[],
                 limitation=limitation, error=error.body['error'], coverage=meta['coverage'])
    record_result(c, run, entry)
    operation = operations(run).get(meta['key']) or dict(meta, call_ids=[])
    save(c, run, meta['key'], dict(operation, error=error.body['error']))


def saved_failure(error):
    refused = failure(error['message'], error['code'])
    refused.body['error'] = dict(error)
    return refused


def outcome(runs, c, run, meta):
    """Return a completed outcome/receipt, or None. The caller has checked fence and the canonical source."""
    operation = operations(run).get(meta['key'])
    if not operation:
        return None
    if 'error' in operation:
        raise saved_failure(operation['error'])
    if 'result' in operation:
        from .extraction import reused_result
        return dict(result=reused_result(runs, c, run, operation['result']))
    last = row(c, 'SELECT data FROM aist.model_calls WHERE run_id=:r AND id=:id',
               r=run['id'], id=operation['call_ids'][-1])['data']
    if last['transport_state'] in ('unknown', 'attempted', 'sent', 'reserved'):
        raise ApiError('outcome_unknown')
    if last['provider_outcome'] != 'success' or last['delivery'] != 'delivered':
        raise saved_failure(last['error'] or ApiError('outcome_unknown').body['error'])
    if 'response' not in operation:
        raise ApiError('outcome_unknown')
    return dict(response=operation['response'], call_id=operation['response_call_id'])
