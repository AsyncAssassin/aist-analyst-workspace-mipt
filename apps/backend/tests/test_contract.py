import copy
import hashlib
import json
import re
from jsonschema.exceptions import ValidationError
from app.contracts import FORMATS, PACKAGE, SCHEMA_SHA256, CONTRACT_VERSION, validate_definition, serialize_definition
import pytest

RECORDS = json.loads((PACKAGE / "examples/entities.json").read_text())["records"]
HTTP = json.loads((PACKAGE / "http.json").read_text())


def fixture(name):
    return copy.deepcopy(RECORDS[name]["value"])


def lookup_from_response(project, version):
    parameters = {"project_id": project, "version_id": version}
    validate_definition("PathProjectVersion", parameters)
    route = HTTP["operations"]["getVersionById"]["path"]
    assert set(re.findall(r"{([^}]+)}", route)) == set(parameters)
    return route.format(**parameters)


def test_f01_run_to_version_without_artifact_id():
    run = fixture("question_completed")
    assert run["input"]["target_artifact_id"] is None
    url = lookup_from_response(run["project_id"], run["result_version_id"])
    case = next(c for c in json.loads((PACKAGE / "examples/http-chain.json").read_text())["cases"] if c["id"] == "07-answer")
    assert case["operation"] == "getVersionById"
    assert case["path"] == {"project_id": run["project_id"], "version_id": run["result_version_id"]}
    assert url.endswith("/artifact-versions/" + fixture("answer_view")["version"]["id"])
    assert fixture("answer_view")["version"]["artifact_id"] == fixture("artifact_answer")["id"]


def test_f01_restores_message_and_result_saved():
    run = fixture("question_completed")
    message = next(m for m in fixture("question_messages")["items"] if m["artifact_version_id"] is not None)
    event = fixture("sse_event")
    assert event["type"] == "result_saved" and event["run_id"] == run["id"]
    expected = lookup_from_response(run["project_id"], run["result_version_id"])
    assert lookup_from_response(message["project_id"], message["artifact_version_id"]) == expected
    # project_id берется из snapshot Run того же потока, event не содержит его.
    assert lookup_from_response(run["project_id"], event["data"]["artifact_version_id"]) == expected


def test_f01_historical_version_does_not_follow_head():
    run, head, view = fixture("note_completed"), fixture("artifact_note_v3"), fixture("note_v1_view")
    assert run["result_version_id"] != head["latest_version_id"]
    url = lookup_from_response(run["project_id"], run["result_version_id"])
    assert url.endswith("/" + view["version"]["id"])
    assert view["version"]["artifact_id"] == head["id"]
    assert view["version"]["revision"] < head["revision"]


def test_f01_preserves_old_route_auth_errors_and_response():
    old, new = HTTP["operations"]["getVersion"], HTTP["operations"]["getVersionById"]
    assert old["path"] == "/v1/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}"
    for field in ("method", "responses", "errors", "headers", "query", "request", "idempotency"):
        assert old[field] == new[field]
    assert "unauthenticated" in HTTP["common_errors"] and "resource_not_found" in HTTP["common_errors"]
    with pytest.raises(ValidationError):
        validate_definition("PathProjectVersion", {"project_id": "prj_demo", "version_id": "av_note-v1", "artifact_id": "art_note"})


def test_representative_round_trip_and_rejection():
    view = fixture("answer_view")
    assert json.loads(serialize_definition("VersionView", view)) == view
    assert view["review"]["acceptance"] is None
    mutated = copy.deepcopy(view)
    # Выбираем реальную Decimal-строку из структуры, без ручного определения всех полей.
    def replace_decimal(value):
        if isinstance(value, dict):
            if value.get("value") == fixture("growth")["rounded"]["value"]:
                value["value"] = float(value["value"])
                return True
            return any(replace_decimal(item) for item in value.values())
        if isinstance(value, list):
            return any(replace_decimal(item) for item in value)
        return False
    assert replace_decimal(mutated)
    with pytest.raises(ValidationError):
        serialize_definition("VersionView", mutated)
    mutated = copy.deepcopy(view); mutated["version"]["owner"] = "usr_private"
    with pytest.raises(ValidationError):
        serialize_definition("VersionView", mutated)


def test_identity_covers_actual_schema():
    identity = json.loads((PACKAGE / "identity.json").read_text())
    assert identity["contract_version"] == CONTRACT_VERSION
    assert identity["schema_sha256"] == SCHEMA_SHA256
    assert SCHEMA_SHA256 == hashlib.sha256((PACKAGE / "schemas/aist.schema.json").read_bytes()).hexdigest()


def test_error_message_limit_carries_complete_save_feedback():
    base = dict(code='validation_failed', message='x' * 20000, retryable=False, run_id=None, call_id=None,
                tool_call_id=None, expected_revision=None, current_revision=None)
    validate_definition('Error', base)
    validate_definition('ToolFailure', dict(ok=False, tool_call_id='call_feedback', error=base))
    with pytest.raises(ValidationError):
        validate_definition('Error', dict(base, message='x' * 20001))
    schema = json.loads((PACKAGE / 'schemas/aist.schema.json').read_text())['$defs']
    assert schema['Error']['properties']['message']['maxLength'] == 20000
    assert schema['Finding']['properties']['message']['maxLength'] == 1000



@pytest.mark.parametrize('stamp', ['2026-02-30T20:59:59Z', '2026-12-31T24:00:00Z',
                                  '2026-12-31T20:60:00Z', '2026-12-31T21:00:00+00:00', '2026-12-31T21:00:00Z\n'])
def test_timestamp_requires_valid_calendar_time_and_z(stamp):
    assert validate_definition('Timestamp', '2024-02-29T21:00:00Z')
    with pytest.raises(ValidationError):
        validate_definition('Timestamp', stamp)


def test_uri_format_and_field_restrictions():
    from jsonschema import Draft202012Validator

    uri = Draft202012Validator({'type': 'string', 'format': 'uri'}, format_checker=FORMATS)
    assert all(uri.is_valid(value) for value in ('https://example.com/a%20b', 'urn:isbn:1234', 'mailto:info@example.com'))
    for value in ('relative/path', 'https://exa mple.com/a', 'https://example.com/a%ZZ', 'https://example.com/<b>', 'https://example.com/\n'):
        assert not uri.is_valid(value)
    context = {'company': '', 'as_of': '2026-02-30', 'status': 'empty', 'items': [],
               'retrieved_at': '2026-02-28T00:00:00Z'}
    with pytest.raises(ValidationError) as error:
        validate_definition('NewsRun', context)
    assert list(error.value.path) == ['as_of']
    assert validate_definition('NewsRun', dict(context, as_of='2026-02-28'))
    item = dict(quote='Новость', publisher='Компания', published='2026-02-28', page_sha256='b'*64,
                url='https://www.interfax.ru/business/1')
    assert validate_definition('NewsItem', item)
    with pytest.raises(ValidationError):
        validate_definition('NewsItem', dict(item, url='urn:isbn:1234'))
