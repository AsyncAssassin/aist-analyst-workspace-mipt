"""Проверка оснований для освобождения резерва и индивидуальной сверки."""
import json
from pathlib import Path
import re

from jsonschema import ValidationError

from .contracts import validate_definition
from .errors import ApiError
from .live_profile import profile_for

ENDPOINTS = {
    'https_openai_responses': ('api.openai.com', '/v1/responses'),
    'https_anthropic_messages': ('api.anthropic.com', '/v1/messages'),
    'https_chat_completions': ('api.novita.ai', '/openai/v1/chat/completions'),
}
SCOPES = frozenset(provider + '-authorized-total' for provider in ('anthropic', 'novita', 'openai'))
QA_SCOPE = 'qa-scripted'


def unique_object(pairs):
    value = dict(pairs)
    if len(pairs) != len(value):
        raise ValueError('duplicate evidence field')
    return value


def validate_rules(value):
    validate_definition('UnbilledProviderRules', value)
    identities = {(rule['rule_id'], rule['version']) for rule in value}
    if len(identities) != len(value):
        raise ValueError('duplicate rule identity')


def refusal_payload(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return None


def match_refusal(run, status, payload, endpoint, request_id):
    policy = run['configuration']['policy']
    rules = policy.get('unbilled_errors', [])
    if rules == [] or not isinstance(payload, dict) or not 400 <= status <= 599 or not isinstance(request_id, str):
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', request_id):
        return None
    try:
        validate_rules(rules)
    except (ValidationError, ValueError):
        return None
    profile = profile_for(run['input'])
    protocol, provider = profile['transport'], profile['provider']
    if (run['configuration']['executor'] != 'openclaw' or protocol not in ENDPOINTS
            or endpoint != ''.join(ENDPOINTS[protocol])):
        return None

    error = payload.get('error')
    if protocol == 'https_anthropic_messages':
        if (payload.get('type') != 'error' or not set(payload) <= {'type', 'error', 'request_id'}
                or payload.get('request_id', request_id) != request_id):
            return None
    elif protocol == 'https_chat_completions' and 'error' not in payload:
        error = payload
    elif set(payload) != {'error'}:
        return None
    if not isinstance(error, dict) or not set(error) <= {'type', 'code', 'message', 'param'}:
        return None
    if protocol == 'https_anthropic_messages' and set(error) != {'type', 'message'}:
        return None
    if not isinstance(error.get('message'), str) or not error['message']:
        return None
    if error.get('param') is not None and not isinstance(error['param'], str):
        return None
    match = dict(provider=provider, protocol=protocol, endpoint=endpoint,
                 profile_id=run['input']['model_profile_version_id'], budget_scope=policy.get('budget_scope'),
                 pricing_version=policy['pricing_version'], http_status=status,
                 error_type=error.get('type'), error_code=error.get('code'))
    matching = [rule for rule in rules if all(rule[key] == value for key, value in match.items())]
    return matching[0] if len(matching) == 1 else None


def invoice_target(run, call):
    profile = profile_for(run['input'])
    data = call['data']
    qa = run['configuration']['executor'] == QA_SCOPE
    return dict(run_id=run['id'], call_id=call['id'], owner_id=run['owner_id'], project_id=run['project_id'],
                profile_id=data['model_profile_version_id'], provider='qa' if qa else profile['provider'],
                protocol='qa_scripted' if qa else profile['transport'],
                budget_scope=QA_SCOPE if qa else run['configuration']['policy'].get('budget_scope'),
                provider_request_id=data['provider_request_id'], provider_completion_id=data['provider_completion_id'])


def read_invoice(path):
    from .received_response import read_evidence
    try:
        path = Path(path).absolute()
        payload = json.loads(read_evidence(Path('/'), path.relative_to('/'), 8192).decode('utf-8'),
                             object_pairs_hook=unique_object)
        return validate_definition('OperatorBillingEvidence', payload)
    except (OSError, ValueError, RecursionError, ValidationError):
        raise ApiError('validation_error') from None
