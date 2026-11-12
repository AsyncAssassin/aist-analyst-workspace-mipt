"""Save-format feedback keeps strict parsing and the ordinary Run error path."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.errors import ApiError, error_value
from app.model_save import content_args, save_operation, FORMAT_FAILURE_MESSAGE
from app.normalization import decode_json
from app.openclaw_runtime import Boundary
from test_perf02 import content, run


@pytest.mark.parametrize('raw,detail', [
    ('{"title":"first"} {"sections":[]}', 'position 18, line 1, column 19'),
    ('{"title":"first","title":"second"}', 'duplicate key: title'),
    ('{"value":NaN}', 'nonfinite: NaN'),
    ('{"value":Infinity}', 'nonfinite: Infinity'),
    ('{"value":-Infinity}', 'nonfinite: -Infinity'),
    ('[]', 'требуется JSON-объект'),
])
def test_content_diagnostic_is_specific_and_other_http_parsing_is_unchanged(raw, detail):
    with pytest.raises(ApiError) as failure:
        content_args({'content': raw})
    message = failure.value.body['error']['message']
    assert 'content:' in message and detail in message
    assert 'Верни цельный корректный JSON-объект content.' in message
    if raw != '[]':
        with pytest.raises(ApiError) as ordinary:
            decode_json(raw)
        assert ordinary.value.body['error']['message'] == error_value('validation_error')['message']


def test_object_and_json_string_are_one_save_operation():
    value = content()
    assert save_operation(run(), {'content': value}) == save_operation(run(), {'content': json.dumps(value)})
    with pytest.raises(ApiError):
        content_args({'content': '{"é":1,"e\\u0301":2}'})


@pytest.mark.parametrize('format_failure', [True, False])
def test_specific_feedback_and_terminal_message_do_not_replace_content_refusals(tmp_path, format_failure):
    stopped = []
    worker = SimpleNamespace(settings=SimpleNamespace(ready_facts_only=True),
                             runs=SimpleNamespace(stop=lambda *args, **kwargs: stopped.append((args, kwargs))))
    boundary = Boundary(worker, dict(run(), epoch=1), tmp_path, 'synthetic')
    reason = 'content: Extra data; position 9413, line 1, column 9414. Верни цельный корректный JSON-объект content.'
    response = ('Validation failed for tool "aist_save_artifact": content: must be object\nReceived arguments: entire document'
                if format_failure else json.dumps({'ok': False, 'error': {'message': 'Факт отсутствует.'}}))
    for index in range(2):
        boundary.issued[str(index)] = dict(name='aist_save_artifact', digest='same-command',
                                          format_error=reason if format_failure else None)
        body = {'messages': [dict(role='tool', tool_call_id=str(index), content=response)]}
        original = deepcopy(body)
        if index == 0:
            actual = boundary.save_feedback(body)
            assert actual['messages'][0]['content'] == (reason if format_failure else response)
            assert body == original
            # Re-reading the same feedback is not a second rejected command.
            assert boundary.save_feedback(body) == actual
        else:
            with pytest.raises(ApiError):
                boundary.save_feedback(body)
    assert len(stopped) == 1
    # FLOW-01: the terminal reason names the repeated content refusal instead of the generic check message.
    assert stopped[0][1]['message'] == (FORMAT_FAILURE_MESSAGE if format_failure
                                        else 'Сохранение отклонено повторно с тем же содержанием: Факт отсутствует.')
