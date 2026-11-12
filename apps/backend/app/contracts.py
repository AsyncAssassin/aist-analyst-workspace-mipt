"""Проверяемая сериализация по единственной JSON Schema, без ручных DTO."""
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "contracts/v1"
SCHEMA_BYTES = (PACKAGE / "schemas/aist.schema.json").read_bytes()
SCHEMA = json.loads(SCHEMA_BYTES)
CONTRACT_VERSION = json.loads((PACKAGE / "http.json").read_text())["contract_version"]
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_BYTES).hexdigest()
FORMATS = FormatChecker()


def require_full_format(name):
    check, errors = FORMATS.checkers[name]

    @FORMATS.checks(name, raises=errors)
    def full_value(value):
        # The two optional checkers use a regex ending in $, which allows a trailing newline.
        return (not isinstance(value, str) or value == value.strip()) and check(value)


require_full_format('uri')
require_full_format('date-time')


def validate_definition(name, value):
    if name not in SCHEMA["$defs"]:
        raise ValueError("Unknown contract definition")
    Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{name}"},
                         format_checker=FORMATS).validate(value)
    return value


def serialize_definition(name, value):
    validate_definition(name, value)
    wire = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    validate_definition(name, json.loads(wire))
    return wire
