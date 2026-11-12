"""Сохранение, повтор, наследование и выпуск внешнего контекста без сетевых сервисов."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest

from app import note_external
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace
from test_draft01_postgres import http
from test_draft01_r1_postgres import edit, save
from test_aso_quality02_phase3_postgres import accept, deal_note, released, start
from test_perf02_r4_postgres import note_source, save_args
from test_external_context import batch, claim_and_block

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def prepare(w, monkeypatch, legacy=False):
    source, _ = note_source(w)
    run = start(w, source)
    result, _ = batch()
    if legacy:
        result.pop('collection_version')
        result['status'] = 'found'
        result['topics'] = {key: 'found' if value == 'collected' else value for key, value in result['topics'].items()}
        for item in result['items']:
            item['subject'] = item.pop('search_target')
            item.pop('search_role')
    result['as_of'] = run['created_at'].date().isoformat()
    seed_context(w, run, result)
    return source, run, result, None


def seed_context(w, run, context):
    """Загружаем сохраненный снимок; сетевой путь проверяется в test_web_research_postgres."""
    from app.contracts import validate_definition
    validate_definition('ExternalContext', context)
    with w.engine.begin() as c:
        execute(c, 'UPDATE aist.runs SET external_context=CAST(:value AS jsonb) WHERE id=:id',
                value=json.dumps(context), id=run['id'])
    run['external_context'] = deepcopy(context)


def no_ownership(snapshot):
    return dict(status='not_established', reason='Материал раскрывает выручку, не состав участников.',
                excluded_sources=[dict(external_evidence_id=item['evidence_id'],
                    reason='Выдержка о выручке не раскрывает владельцев.')
                    for item in snapshot['items'] if 'owners' in item['topics']])


@pytest.mark.parametrize('legacy', [False, True])
def test_snapshot_reused_by_retry_and_version_then_edit_accept_and_release(workspace, monkeypatch, tmp_path, legacy):
    w = workspace
    source, run, snapshot, lookup = prepare(w, monkeypatch, legacy)
    note_external.prepare_all(w.runs, run)
    assert run['external_context'] == snapshot
    content = deal_note(source)
    _, block = claim_and_block(snapshot)
    content['sections'][1]['blocks'].append(block)
    first = save(w, run, content, ownership_review=no_ownership(snapshot))
    assert first['version']['basis']['external_context'] == run['external_context']
    assert first['checks'][0]['status'] == 'passed'
    assert first['checks'][0]['profile_version'] == 'note-technical.v29'
    accept(w, first)
    accepted = w.runs.version(w.actor, w.project, first['version']['id'])
    assert accepted['review']['acceptance']
    texts = released(w, first['version'], tmp_path)
    for text in texts.values():
        assert 'Орбита раскрыла выручку за 2025 год.' in text
        assert 'interfax.ru' not in text
    changed = deepcopy(content)
    changed['sections'][1]['blocks'][-1]['claim']['text'] += ' Редактор уточнил описание.'
    with http(w) as client:
        child = edit(w, client, accepted, changed, 'human', [source])
    assert child['version']['basis']['external_context'] == accepted['version']['basis']['external_context']
    assert child['review']['acceptance'] is None
    assert child['checks'][0]['status'] == 'passed'
    assert child['checks'][0]['profile_version'] == first['checks'][0]['profile_version']
    if destination := os.getenv('AIST_PHASE4_EVIDENCE'):
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'versions.json').write_text(json.dumps(dict(parent=accepted, child=child), ensure_ascii=False, indent=2))
        for file in Path(w.runs.settings.export_root).rglob('note.*'):
            if file.suffix in ('.docx', '.pdf'):
                shutil.copyfile(file, directory / file.name)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE project_id=:p', p=w.project).scalar_one() == 0


@pytest.mark.parametrize('mutation', ['foreign_id', 'wrong_origin', 'fact_ref'])
def test_external_reference_cannot_be_forged(workspace, monkeypatch, mutation):
    w = workspace
    source, run, snapshot, _ = prepare(w, monkeypatch)
    content = deal_note(source)
    claim, block = claim_and_block(snapshot)
    if mutation == 'foreign_id':
        claim['external_evidence_ids'] = ['ext_other_run']
    elif mutation == 'wrong_origin':
        claim['origin'] = 'human_editorial'
    else:
        claim['fact_refs'] = [dict(source_version_id=source['id'], fact_id='fact_clients')]
    content['sections'][1]['blocks'].append(block)
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert not reply['ok']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == 0


def test_old_version_and_model_child_do_not_start_collection(workspace, monkeypatch):
    w = workspace
    source, _ = note_source(w)
    run = start(w, source)
    old = save(w, run, deal_note(source))
    lookup = Mock(side_effect=AssertionError('Old version must not search'))
    monkeypatch.setattr(note_external, 'web_request', lookup)
    child_run = start(w, source, parent=old)
    note_external.prepare_all(w.runs, child_run)
    child = save(w, child_run, deepcopy(old['version']['payload']['content']), old)
    assert 'external_context' not in child['version']['basis']
    lookup.assert_not_called()


def test_saved_evidence_of_another_run_does_not_authorize_an_id(workspace, monkeypatch):
    w = workspace
    source, run, snapshot, _ = prepare(w, monkeypatch)
    save(w, run, deal_note(source), ownership_review=no_ownership(snapshot))
    other = start(w, source)
    content = deal_note(source)
    _, block = claim_and_block(snapshot)
    content['sections'][1]['blocks'].append(block)
    result = w.runs.tool(other['id'], other['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert not result['ok']
