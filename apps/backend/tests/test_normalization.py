import pytest
from app.errors import ApiError
from app.normalization import parse_json, fingerprint
from app.storage import safe_filename


def test_normalized_fingerprint_and_strict_json():
    assert fingerprint({'name':'Café\r\nX'})==fingerprint({'name':'Cafe\u0301\nX'})
    assert fingerprint({'name':'X '})!=fingerprint({'name':'X'})
    for raw in ['{"name":"a","name":"b"}', '{"name":NaN}', '{"name":"x","owner_id":"usr_fake"}', '{"name":"x","sha256":"bad"}']:
        with pytest.raises(ApiError): parse_json(raw,'CreateProject')
    assert safe_filename('C:\\private\\a\x00.pdf')=='a.pdf'
    assert safe_filename('../../')=='source.pdf'
