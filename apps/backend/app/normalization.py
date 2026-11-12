import hashlib
import json
import unicodedata
from jsonschema import ValidationError
from .contracts import validate_definition
from .errors import ApiError


def normalize(value):
    if isinstance(value, str):
        return unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n'))
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = normalize(key)
            if key in result:
                raise ApiError('validation_error')
            result[key] = normalize(item)
        return result
    return value


def visible_text(value):
    """Текст для сверки слов запроса: невидимые символы не разделяют и не скрывают буквы.

    Удаляются символы форматирования и управления, кроме пробельных (U+200B, мягкий перенос),
    и надстрочные знаки, которые остаются после NFC (ударение). Сохраненный текст не меняется."""
    return ''.join(char for char in unicodedata.normalize('NFC', value)
                   if char.isspace() or unicodedata.category(char)[0] not in 'CM')


def canonical(value):
    return json.dumps(normalize(value), sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def checked(name, value):
    try:
        return validate_definition(name, value)
    except (ValidationError, ValueError, TypeError):
        raise ApiError('validation_error') from None


def decode_json(data, *, diagnostic=False, normalize_strings=True):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate key: ' + key[:80])
            value[key] = item
        return value
    def invalid(value):
        raise ValueError('nonfinite: ' + value)
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
        if normalize_strings:
            value = normalize(value)
    except (ValueError, UnicodeError, RecursionError) as error:
        refusal = ApiError('validation_error')
        if diagnostic:
            if isinstance(error, json.JSONDecodeError):
                detail = f'{error.msg}; position {error.pos}, line {error.lineno}, column {error.colno}'
            else:
                detail = str(error)[:160] or type(error).__name__
            refusal.body['error']['message'] = detail
        raise refusal from None
    return value


def parse_json(data, definition):
    return checked(definition, decode_json(data))
