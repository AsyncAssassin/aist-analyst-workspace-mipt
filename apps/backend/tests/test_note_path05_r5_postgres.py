"""SaveVersion сохраняет строгие границы при явном добавлении собственных ссылок."""
from copy import deepcopy
import os

import pytest

from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import http
from test_draft01_r1_postgres import start, save, edit, assert_reload
from test_perf02_r4_postgres import note_source, paragraph, ref

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def unreferenced_note(source, text):
    conclusion = paragraph('block_conclusion', text, [])
    conclusion['claim']['origin'] = 'inference'
    return dict(title='Наблюдения', audience='Аналитик', sections=[dict(section_id='sec_observations', title='Ограничения', blocks=[
        paragraph('block_basis', 'Опубликован показатель собственного капитала.', [ref(source, 'fact_capital1q23')]),
        conclusion,
    ])])


@pytest.mark.parametrize('text,error', [
    ('Капитал на 31.03.2023: 3658 млн руб.', None),
    ('Капитал на 31.03.2023: 999 млн руб.', 'unit_mismatch'),
    ('Капитал на 31.03.2024: 3658 млн руб.', 'period_mismatch'),
    ('Промежуточные результаты 2024 года нельзя подтвердить по доступным основаниям.', None),
    ('Промежуточные результаты 2024 года можно подтвердить по доступным основаниям.', 'period_mismatch'),
])
def test_explicit_editorial_reference_does_not_confirm_wrong_amount_period_or_affirmation(workspace, text, error):
    w = workspace
    source, _ = note_source(w)
    content = unreferenced_note(source, text)
    original = save(w, start(w, [source]), content)
    assert any(f['code'] == 'unreferenced_claim' and f['block_id'] == 'block_conclusion' for f in original['checks'][0]['findings'])
    edited = deepcopy(content)
    edited['sections'][0]['blocks'][1]['claim']['fact_refs'] = [ref(source, 'fact_capital1q23')]
    with http(w) as client:
        child = edit(w, client, original, edited, 'human', [source])
        findings = {f['code'] for f in child['checks'][0]['findings'] if f['block_id'] == 'block_conclusion'}
        assert 'unreferenced_claim' not in findings
        assert not findings and child['checks'][0]['status'] == 'passed'
        assert child['checks'][1]['status'] == 'not_checked'
        assert child['review']['acceptance'] is None
        assert child['version']['payload']['content']['sections'][0]['blocks'][1]['claim']['text'] == text
        assert child['version']['payload']['content']['sections'][0]['blocks'][1]['claim']['origin'] == 'human_editorial'
        assert_reload(w, original)
        assert_reload(w, child)


@pytest.mark.parametrize('kind,status,code', [('absent', 422, 'validation_failed'), ('foreign', 404, 'resource_not_found')])
def test_editorial_reference_must_exist_and_belong_to_the_selected_source(workspace, kind, status, code):
    w = workspace
    source, _ = note_source(w)
    other, _ = note_source(w)
    content = unreferenced_note(source, 'Опубликован показатель собственного капитала.')
    original = save(w, start(w, [source]), content)
    edited = deepcopy(original['version']['payload'])
    edited['content']['sections'][0]['blocks'][1]['claim']['fact_refs'] = [ref(source, 'fact_absent') if kind == 'absent' else ref(other, 'fact_capital1q23')]
    v = original['version']
    with http(w) as client:
        response = client.post(f'/v1/projects/{w.project}/artifacts/{v["artifact_id"]}/versions',
                             json=dict(expected_revision=v['revision'], parent_version_id=v['id'], payload=edited),
                             headers={'Idempotency-Key':ident('key')})
        assert response.status_code == status, response.text
        assert response.json()['error']['code'] == code
        assert_reload(w, original)
