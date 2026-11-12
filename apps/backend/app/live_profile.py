"""Delivered files, rather than the planned contract registry, enable execution."""
import hashlib
import json
from .contracts import ROOT
from .normalization import fingerprint
from .errors import ApiError

BUNDLE = ROOT / 'executor/openclaw/skills/analytical-note/SKILL.md'
BUNDLES = {
    'skill_source-answer-v3': ('evidence_answer', ROOT / 'executor/openclaw/skills/source-answer-v3/SKILL.md'),
    'skill_source-answer-v2': ('evidence_answer', ROOT / 'executor/openclaw/skills/source-answer-v2/SKILL.md'),
    'skill_analytical-note-v1': ('analytical_note', BUNDLE),
    'skill_analytical-note-v2': ('analytical_note', ROOT / 'executor/openclaw/skills/analytical-note-v2/SKILL.md'),
    'skill_analytical-note-v3': ('analytical_note', ROOT / 'executor/openclaw/skills/analytical-note-v3/SKILL.md'),
    'skill_source-answer-v1': ('evidence_answer', ROOT / 'executor/openclaw/skills/source-answer/SKILL.md'),
}
MODEL = 'xiaomimimo/mimo-v2.6-pro'
# The only automatic retry: an identical send after a completely received HTTP 429
# refusal typed server_overload, at most three per Run across the agent and nested
# extractions, paused 10, 20 and 40 seconds plus up to 20% jitter, never shorter than
# Retry-After and never beyond 180 seconds in total or the Run deadline. Every other
# refusal, timeout, broken or unknown outcome keeps the no-retry rule.
OVERLOAD_RETRY = dict(http_status=429, error_type='server_overload', max_per_run=3, base_seconds=[10, 20, 40],
                      jitter_ratio=0.2, max_total_wait_seconds=180, honor_retry_after=True)
# Tool orchestration must not spend the entire answer on hidden planning.
# Extraction keeps its separate reasoning mode and the same validation.
PROFILE = dict(model=MODEL, provider='novita', input_bytes=600000, max_tokens=32000,
               transport='https_chat_completions', reasoning_history=True, thinking='disabled', extraction_thinking='enabled',
               automatic_retries=OVERLOAD_RETRY, tools_transport='unix_socket')
# FLOW-01: a first generation of a v3 note collects its basis within a bounded loop: at most these
# ordinary calls including nested extractions, at most these logical extractions, two completed
# extractions in a row without new usable facts end the collection early; the completion calls and
# seconds stay reserved; one physical send ends within send_seconds and the remaining Run time.
# Kept apart from PROFILE so that the model configuration hash of created Runs does not change.
COLLECTION = dict(max_model_calls=16, max_extractions=3, completion_calls=3, idle_extractions=2,
                  max_duration_seconds=1800, send_seconds=180, completion_seconds=180)

FABLE_ID = 'model_anthropic-fable-xhigh-v1'
FABLE = dict(model='claude-fable-5-1', provider='anthropic', input_bytes=600000, max_tokens=64000,
             transport='https_anthropic_messages', thinking='adaptive', effort='xhigh',
             automatic_retries=0, tools_transport='unix_socket', prompt_cache=False)
FABLE_FULL_ID = 'model_anthropic-fable-xhigh-v2'
FABLE_FULL = dict(FABLE, extraction_thinking='adaptive')
FABLE_VISUAL_ID = 'model_anthropic-fable-xhigh-v3'
FABLE_VISUAL = dict(FABLE_FULL, extraction_effort='high', extraction_input_bytes=8 * 1024 * 1024,
                    page_images=dict(version='poppler-jpeg.v1', long_side=1600, quality=85))
FABLE_FULL_IDS = (FABLE_FULL_ID, FABLE_VISUAL_ID)
OPENAI_ID = 'model_openai-astra-xhigh-v1'
OPENAI = dict(model='gpt-6-astra', provider='openai', input_bytes=600000, max_tokens=64000,
              transport='https_openai_responses', effort='xhigh', extraction_thinking='xhigh',
              extraction_effort='xhigh', reasoning_mode='standard', service_tier='default',
              automatic_retries=0, tools_transport='unix_socket', prompt_cache=False,
              extraction_input_bytes=8 * 1024 * 1024, response_bytes=16 * 1024 * 1024,
              response_event_bytes=8 * 1024 * 1024, image_detail='original',
              page_images=dict(version='poppler-jpeg.v1', long_side=1600, quality=85))
FULL_EXTRACTION_IDS = (*FABLE_FULL_IDS, OPENAI_ID)


def profile_for(run_input):
    identifier = run_input.get('model_profile_version_id')
    if identifier == OPENAI_ID:
        return OPENAI
    if identifier == FABLE_VISUAL_ID:
        return FABLE_VISUAL
    if identifier == FABLE_FULL_ID:
        return FABLE_FULL
    if identifier == FABLE_ID:
        return FABLE
    if identifier is None or identifier in ('model_novita-mimo-v1', 'model_qa-d07-v1'):
        return PROFILE
    raise ApiError('tool_unavailable')


def input_limit(run_input, purpose):
    profile = profile_for(run_input)
    return profile.get('extraction_input_bytes', profile['input_bytes']) if purpose == 'extraction' else profile['input_bytes']


def bundle_for(skill_id, result_type):
    entry = BUNDLES.get(skill_id)
    if not entry or entry[0] != result_type:
        raise ApiError('tool_unavailable')
    return entry[1]


def verify_pins(configuration, run_input):
    bundle = bundle_for(run_input['skill_version_id'], run_input['requested_result_type'])
    if (configuration.get('skill_sha256')!=hashlib.sha256(bundle.read_bytes()).hexdigest() or
            configuration.get('model_configuration_sha256')!=fingerprint(profile_for(run_input))):
        raise ApiError('tool_unavailable')


def delivered(registry, model_profile_id='model_novita-mimo-v1'):
    value = json.loads(json.dumps(registry))
    native = profile_for({'model_profile_version_id': model_profile_id})['transport'] != 'https_chat_completions'
    for skill in value['skills']:
        if native and skill['id'] not in ('skill_analytical-note-v3', 'skill_source-answer-v3'):
            skill.update(status='planned', bundle_sha256=None)
            continue
        if skill['id'] in BUNDLES:
            bundle = bundle_for(skill['id'], skill['result_type'])
            skill.update(status='enabled', bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
    for model in value['model_profiles']:
        if model['id'] == model_profile_id:
            model.update(status='enabled', configuration_sha256=fingerprint(profile_for({'model_profile_version_id': model['id']})))
        else:
            model.update(status='planned', configuration_sha256=None)
    return value
