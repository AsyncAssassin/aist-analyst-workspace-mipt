"""DRAFT-01-R2: независимые правки сохраняют корректную подпись и известные ошибки."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from app.exports import Exports
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import draft_note, export_request, http
from test_draft01_r1_postgres import assert_blocked, assert_reload, codes, edit, save, start
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def evidence(name, **result):
    if destination := os.getenv('AIST_R2_EVIDENCE'):
        Path(destination, name + '.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))


def accept(client, project, view):
    version = view['version']
    base = f'/v1/projects/{project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
    response = client.post(base + '/accept', json=dict(
        expected_revision=view['review']['revision'], content_sha256=version['content_sha256'],
        acknowledge_unchecked_semantics=True), headers={'Idempotency-Key': ident('key')})
    assert response.status_code == 200, response.text
    assert response.json()['acceptance']['content_sha256'] == version['content_sha256']
    return base, response


def test_explicit_refs_do_not_erase_another_error_in_same_paragraph_or_other_block(workspace):
    w = workspace
    source, _ = note_source(w)
    content = draft_note(source, corrected=True)
    content['sections'][0]['blocks'] = [
        dict(block_id='block_intro', kind='paragraph', claim=dict(origin='source_fact',
             text='Капитал на 31.03.2023: 3658 млн руб.; на конец 2022 года: 3217 млн руб.',
             fact_refs=[dict(source_version_id=source['id'], fact_id='fact_capital1q23')], calculation_ids=[])),
        dict(block_id='block_other', kind='paragraph', claim=dict(origin='source_fact',
             text='Облигации на 31.03.2023: 999 млн руб.',
             fact_refs=[dict(source_version_id=source['id'], fact_id='fact_bonds')], calculation_ids=[])),
    ]
    first = save(w, start(w, [source]), content)
    before = {(f['block_id'], f['code']): f['message'] for f in first['checks'][0]['findings']}
    assert before == {} and first['checks'][1]['status'] == 'not_checked'
    with http(w) as client:
        corrected = deepcopy(content)
        corrected['sections'][0]['blocks'][0]['claim']['fact_refs'].append(
            dict(source_version_id=source['id'], fact_id='fact_capital2022'))
        child = edit(w, client, first, corrected, 'human', [source])
        after = {(f['block_id'], f['code']): f['message'] for f in child['checks'][0]['findings']}
        assert ('block_intro', 'period_mismatch') not in after
        assert after == {} and child['checks'][1]['status'] == 'not_checked'
        blocks = child['version']['payload']['content']['sections'][0]['blocks']
        assert blocks[0]['claim']['text'] == content['sections'][0]['blocks'][0]['claim']['text']
        assert blocks[1]['claim']['text'] == content['sections'][0]['blocks'][1]['claim']['text']
        assert child['version']['payload']['content']['sections'][0]['blocks'][0]['claim']['origin'] == 'human_editorial'
        assert_blocked(w, client, child, unchecked_semantics=True)
        assert_reload(w, first)


def test_known_heading_survives_unrelated_edit_then_only_its_correction_clears_it(workspace):
    w = workspace
    source, _ = note_source(w)
    original = save(w, start(w, [source]), draft_note(source, corrected=True))
    with http(w) as client:
        accept(client, w.project, original)
        parent = w.runs.version(w.actor, w.project, original['version']['id'])
        content = deepcopy(parent['version']['payload']['content'])
        content['title'] = 'Выручка 999 млн RUB'
        flawed = edit(w, client, parent, content, 'human', [source])
        assert codes(flawed) == [] and flawed['checks'][1]['status'] == 'not_checked'
        denial = assert_blocked(w, client, flawed, unchecked_semantics=True)
        content = deepcopy(flawed['version']['payload']['content'])
        content['audience'] = 'Кредитный комитет'
        retained = edit(w, client, flawed, content, 'model_v2', [source])
        assert retained['checks'][0]['findings'] == flawed['checks'][0]['findings']
        retained_denial = assert_blocked(w, client, retained, unchecked_semantics=True)
        content = deepcopy(retained['version']['payload']['content'])
        content['title'] = 'Показатели компании'
        fixed = edit(w, client, retained, content, 'model_v3', [source])
        assert fixed['checks'][0]['status'] == 'passed' and codes(fixed) == []
        for view in (flawed, retained, fixed):
            assert view['version']['payload']['content']['sections'] == original['version']['payload']['content']['sections']
            assert view['version']['basis'] == original['version']['basis']
        assert_reload(w, fixed)
        evidence('heading-correction-chain', parent=parent, flawed=flawed, retained=retained,
                 fixed=fixed, denial=denial, retained_denial=retained_denial)


def test_correcting_one_of_two_headings_keeps_the_other_after_another_save(workspace):
    w = workspace
    source, _ = note_source(w)
    original = save(w, start(w, [source]), draft_note(source, corrected=True))
    with http(w) as client:
        content = deepcopy(original['version']['payload']['content'])
        content['title'] = 'Выручка 999 млн RUB'
        content['sections'][0]['title'] = 'Прибыль 888 млн RUB'
        flawed = edit(w, client, original, content, 'model_v3', [source])
        assert codes(flawed) == [] and flawed['checks'][1]['status'] == 'not_checked'
        assert flawed['version']['payload']['content']['title'] == content['title']
        assert flawed['version']['payload']['content']['sections'][0]['title'] == content['sections'][0]['title']
        content = deepcopy(flawed['version']['payload']['content'])
        content['title'] = 'Показатели компании'
        partial = edit(w, client, flawed, content, 'human', [source])
        assert partial['checks'][0]['findings'] == flawed['checks'][0]['findings'][1:]
        content = deepcopy(partial['version']['payload']['content'])
        content['audience'] = 'Кредитный комитет'
        retained = edit(w, client, partial, content, 'model_v2', [source])
        assert retained['checks'][0]['findings'] == partial['checks'][0]['findings']
        denial = assert_blocked(w, client, retained, unchecked_semantics=True)
        assert retained['version']['payload']['content']['sections'][0]['blocks'] == original['version']['payload']['content']['sections'][0]['blocks']
        evidence('two-heading-corrections', original=original, flawed=flawed, partial=partial,
                 retained=retained, denial=denial)


@pytest.mark.parametrize('mode,accepted_parent', [
    ('model_v2', True), ('model_v3', True), ('human', False), ('human', True),
])
def test_unchanged_supported_caption_does_not_block_unrelated_edit(workspace, tmp_path, mode, accepted_parent):
    """Независимая правка сохраняет корректную подпись; выпуск требует принятой версии."""
    w = workspace
    source, _ = note_source(w)
    original = save(w, start(w, [source]), draft_note(source, corrected=True))
    assert original['checks'][0]['status'] == 'passed' and codes(original) == []
    with http(w) as client:
        if accepted_parent:
            accept(client, w.project, original)
        parent = w.runs.version(w.actor, w.project, original['version']['id'])
        content = deepcopy(parent['version']['payload']['content'])
        content['audience'] = 'Кредитный комитет'
        child = edit(w, client, parent, content, mode, [source])
        assert child['version']['payload']['content']['sections'] == parent['version']['payload']['content']['sections']
        assert child['version']['basis'] == parent['version']['basis']
        assert child['checks'][0]['status'] == 'passed' and codes(child) == []
        assert_reload(w, child)
        base, acceptance = accept(client, w.project, child)
        current = w.runs.version(w.actor, w.project, child['version']['id'])
        assert current['review']['revision'] == child['review']['revision'] + 1
        assert current['review']['acceptance']['artifact_version_id'] == child['version']['id']
        release = None
        # Один accepted-DOCX; завершение собственного Export не мешает следующим тестам очереди.
        if mode == 'human' and accepted_parent:
            response = client.post(base + '/exports', json=export_request('docx', 'accepted', current['review']['revision']),
                                   headers={'Idempotency-Key': ident('key')})
            assert response.status_code == 202, response.text
            release = response.json()
            assert release['acceptance_id'] == current['review']['acceptance']['id']
            with w.engine.connect() as c:
                saved = execute(c, 'SELECT data FROM aist.exports WHERE id=:id AND project_id=:p',
                                id=release['id'], p=w.project).scalar_one()
            assert saved == release
            w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
            exports = Exports(w.runs)
            exports.process_one()
            _, ready = exports.get(w.actor, w.project, release['id'], download=True)
            assert ready['state'] == 'ready'
        assert w.runs.version(w.actor, w.project, parent['version']['id']) == parent
        evidence('caption-' + mode + ('' if accepted_parent else '-unaccepted'), parent=parent, child=child,
                 acceptance_status=acceptance.status_code, accepted=current, export=release)
