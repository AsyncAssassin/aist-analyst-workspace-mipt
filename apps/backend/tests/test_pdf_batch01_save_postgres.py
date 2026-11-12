"""Качественная ссылка рядом с визуальной: штатное сохранение и запрет принятия."""
from copy import deepcopy
from dataclasses import replace
import os

import pytest
from test_perf02_r4_postgres import model_save_args

from app.model_save import save_operation
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import http, export_request
from test_fable_postgres import native
from test_note_path06_r1_postgres import start, ask, own_items
from test_note_path06_r2_postgres import observation, response

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def test_qualitative_and_visual_rows_keep_independent_findings_and_lifecycle(workspace, native, tmp_path):
    w = workspace
    source, run = start(w)
    native.queue = [dict(body=response(dict(facts=own_items(), visual_observations=[observation('63.8', 2027)])))]
    result = ask(w, run, source, tmp_path)
    qualitative = next(f for f in result['facts'] if f['amount'] is None)
    numeric = next(f for f in result['facts'] if f['amount'] is not None)
    visual = result['visual_observations'][0]
    rows = []
    for name, basis, visual_ids in [
        ('qualitative', qualitative, []),
        ('mixed', qualitative, [visual['observation_id']]),
        ('numeric', numeric, [visual['observation_id']]),
    ]:
        rows.append(dict(row_id='row_' + name, label=visual['numeric']['metric'],
            amount=deepcopy(visual['numeric']['amount']), period_label=visual['numeric']['period']['label'],
            fact_refs=[dict(source_version_id=source['id'], fact_id=basis['fact_id'])],
            calculation_ids=[], visual_observation_ids=visual_ids))
    content = dict(title='Компания Вектор', audience='Аналитик', sections=[
        dict(title='Показатели', blocks=[dict(kind='table', caption='Сведения', rows=rows)])])
    args = save_operation(run, model_save_args(content))
    before = deepcopy(args)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert saved['ok'], saved
    view = saved['result']
    # Кроме команды [TBD], которую AIST ставит при отсутствии имен в запросе, содержание сохраняется как передано.
    expected = dict(args['payload'], content=dict(args['payload']['content'], team='[TBD]'))
    assert args == before and view['version']['payload'] == expected
    assert view['checks'][0]['profile_version'] == 'note-technical.v29'
    findings = view['checks'][0]['findings']
    assert sum(f['code'] == 'row_basis_mismatch' for f in findings) == 3
    assert sum(f['code'] == 'unchecked_visual_observation' for f in findings) == 2
    assert all(f['code'] != 'visual_row_mismatch' for f in findings)
    assert {f['code']: f['severity'] for f in findings} == {
        'row_basis_mismatch': 'error', 'unchecked_visual_observation': 'warning'}
    assert view['checks'][0]['status'] == 'failed'
    assert view['version']['basis']['visual_observations'] == [visual]
    assert qualitative in view['version']['basis']['facts']
    assert w.runs.version(w.actor, w.project, view['version']['id']) == view
    repeated = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert repeated['ok'] and repeated['result'] == view
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == 1
    w.settings = replace(w.settings, executor_mode='disabled', live_models_enabled=False)
    with http(w) as client:
        version = view['version']
        payload = deepcopy(version['payload'])
        payload['content']['sections'][0]['blocks'][0]['caption'] = 'Показатели компании Вектор'
        changed = client.post(f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions',
            json=dict(expected_revision=version['revision'], parent_version_id=version['id'], payload=payload),
            headers={'Idempotency-Key': ident('key')})
        assert changed.status_code == 201, changed.text
        edited = changed.json()
        assert edited['checks'][0]['findings'] == findings
        assert edited['version']['basis'] == version['basis']
        for current in (view, edited):
            version = current['version']
            assert client.get(f'/v1/projects/{w.project}/artifact-versions/{version["id"]}').json() == current
            assert current['checks'][1]['status'] == 'not_checked' and current['review']['acceptance'] is None
            base = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
            for acknowledge in (False, True):
                denied = client.post(base + '/accept', json=dict(expected_revision=0,
                    content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=acknowledge),
                    headers={'Idempotency-Key': ident('key')})
                assert denied.status_code == 422, denied.text
            for fmt in ('pdf', 'docx'):
                denied = client.post(base + '/exports', json=export_request(fmt, 'accepted'),
                                    headers={'Idempotency-Key': ident('key')})
                assert denied.status_code == 409, denied.text
    assert len(native.requests) == 1
