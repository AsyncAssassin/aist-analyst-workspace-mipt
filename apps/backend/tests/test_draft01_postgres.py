"""DRAFT-01 on PostgreSQL with technical profile v29 and separate human acceptance."""
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
import secrets
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from app.auth import token_hash
from app.database import make_engine
from app.errors import ApiError
from app.exports import Exports
from app.facts import pin_of
from app.main import create_app
from app.note_renderer import renderer_versions
from app.openclaw_runtime import execution_message
from app.runs import Runs, execute
from app.service import Service, ident
from app.storage import Storage
from app.versions import listing, persist, write as write_version
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_perf02_r1_postgres import upload
from test_perf02_r4_postgres import MONEY, note_source, paragraph, ref, save_args, start_note_run, table_row

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')

FLAWED = 'Прокатные фирмы сдают приборы для замера шума; 30% предпринимателей.'
CORRECTED = 'Прокатные фирмы сдают приборы для замера шума.'


def structure_table(source):
    return dict(block_id='block_structure', kind='table', caption='Структура финансирования на 31.03.2023', rows=[
        table_row('row_capital', 'Собственный капитал', '1 кв. 2023г.', '3658', [ref(source, 'fact_capital1q23')]),
        table_row('row_funding', 'Привлеченные средства', 'на 31.03.2023', '4392', [ref(source, 'fact_funding')]),
        table_row('row_bonds', 'Облигации', 'на 31.03.2023', '943', [ref(source, 'fact_bonds')])])


def draft_note(source, *, corrected=False):
    """A correct table and one editable paragraph whose percent comes from a qualitative quote."""
    return dict(title='Синтетика: черновик записки', audience='Аналитик', sections=[dict(section_id='sec_fin', title='Финансовые показатели', blocks=[
        paragraph('block_roa', 'По аудированной отчетности за 1Q23 ROE составил 27,3%, ROA 11,6%.', [ref(source, 'fact_roe'), ref(source, 'fact_roa')]),
        structure_table(source),
        paragraph('block_clients', CORRECTED if corrected else FLAWED, [ref(source, 'fact_clients')])])])


def model_draft(w, *, row_mismatch=False):
    source, facts = note_source(w)
    run = start_note_run(w, source)
    content = draft_note(source)
    if row_mismatch:
        content['sections'][0]['blocks'][1]['rows'][2]['amount']['value'] = '942'
    args = save_args(content)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert saved['ok'], saved
    return source, run, args, saved['result']


def http(w):
    token = 'aist_' + secrets.token_urlsafe(32)
    with w.engine.begin() as c:
        c.execute(text('UPDATE aist.sessions SET token_hash=:hash WHERE id=:id'), dict(hash=token_hash(token), id=w.session))
    client = TestClient(create_app(replace(w.settings, embeddings_enabled=False)))
    client.headers['Authorization'] = 'Bearer ' + token
    return client


def accept(w, view, **changes):
    version = view['version']
    request = dict(expected_revision=view['review']['revision'], content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=True)
    request.update(changes)
    return write_version(w.runs, w.actor, w.session, w.project, version['artifact_id'], version['id'], 'acceptVersion', ident('key'), request)


def export_request(fmt, mode, revision=0):
    return dict(expected_revision=revision, format=fmt, template_version='aist-note.v2', renderer_version=renderer_versions('aist-note.v2')[fmt],
                settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode=mode))


def released_text(w, view, mode, tmp_path):
    """DOCX XML and PDF text of a finished release of the selected version."""
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    version = view['version']
    texts = {}
    for fmt in ('docx', 'pdf'):
        status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], ident('key'),
                                        export_request(fmt, mode, view['review']['revision']))
        assert status == 202, entry
        exports.process_one()
        data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
        assert ready['state'] == 'ready' and ready['settings']['release_mode'] == mode
        if fmt == 'pdf':
            texts[fmt] = ''.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                texts[fmt] = archive.read('word/document.xml').decode()
    return texts


def test_model_note_with_unchecked_prose_is_a_readable_unaccepted_draft(workspace, tmp_path):
    w = workspace
    source, run, args, view = model_draft(w)
    version, check = view['version'], view['checks'][0]
    assert check['profile_version'] == 'note-technical.v29' and check['kind'] == 'programmatic'
    assert check['status'] == 'passed' and check['findings'] == [] and view['review']['acceptance'] is None
    assert view['checks'][1]['kind'] == 'semantic' and view['checks'][1]['status'] == 'not_checked'
    # The paragraph is saved as written; the table and every basis are intact.
    blocks = {b['block_id']: b for s in version['payload']['content']['sections'] for b in s['blocks']}
    assert blocks['block_clients']['claim']['text'] == FLAWED and blocks['block_clients']['claim']['origin'] == 'source_fact'
    assert [r['amount']['value'] for r in blocks['block_structure']['rows']] == ['3658', '4392', '943']
    assert {f['fact_id'] for f in version['basis']['facts']} == {'fact_roe', 'fact_roa', 'fact_capital1q23', 'fact_funding', 'fact_bonds', 'fact_clients'}
    state = w.runs.get(w.actor, w.project, run['id'])
    assert state['state'] == 'completed' and state['result_version_id'] == version['id'] and state['error'] is None
    history = w.runs.listing(w.actor, w.project, 'messages', {}, run['conversation_id'])['items']
    assert [m['role'] for m in history] == ['user', 'assistant'] and history[1]['artifact_version_id'] == version['id']
    # Read through a fresh connection: the draft, its checks and the history survive a reload.
    fresh = Runs(Service(make_engine(w.settings), Storage(w.settings.storage_root)), w.settings)
    try:
        assert fresh.version(w.actor, w.project, version['id']) == view
        assert [item['id'] for item in listing(fresh, w.actor, w.project, version['artifact_id'], {})['items']] == [version['id']]
    finally:
        fresh.engine.dispose()
    # Acceptance requires explicit acknowledgment of unchecked semantics.
    with pytest.raises(ApiError) as blocked:
        accept(w, view, acknowledge_unchecked_semantics=False)
    assert blocked.value.code == 'validation_failed'
    with http(w) as client:
        base = f'/v1/projects/{w.project}/artifacts/{version["artifact_id"]}/versions/{version["id"]}'
        response = client.post(base + '/accept', json=dict(expected_revision=0, content_sha256=version['content_sha256'],
                                                           acknowledge_unchecked_semantics=False), headers={'Idempotency-Key': ident('key')})
        assert response.status_code == 422 and response.json()['error']['code'] == 'validation_failed', response.text
        response = client.post(base + '/exports', json=export_request('pdf', 'accepted'), headers={'Idempotency-Key': ident('key')})
        assert response.status_code == 409 and response.json()['error']['code'] == 'acceptance_required', response.text
        assert client.get(f'/v1/projects/{w.project}/artifact-versions/{version["id"]}').json() == view
    assert w.runs.version(w.actor, w.project, version['id'])['review']['acceptance'] is None
    # The draft release keeps the original paragraph and has a visible draft marker.
    texts = released_text(w, view, 'draft', tmp_path)
    for fmt, text_value in texts.items():
        assert 'ЧЕРНОВИК' in text_value, fmt
        assert '30% предпринимателей' in text_value and 'Структура финансирования на 31.03.2023' in text_value, fmt
    # A replay of the same save returns the same version without a second write or a second message.
    again = w.runs.tool(run['id'], run['epoch'], ident('repeat'), 'aist_save_artifact', args)
    assert again['ok'] and again['result']['version']['id'] == version['id']
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 1
        assert execute(c, 'SELECT count(*) FROM aist.messages WHERE run_id=:r', r=run['id']).scalar() == 2


def test_editor_corrects_the_paragraph_in_a_new_version_accepted_separately(workspace, tmp_path):
    w = workspace
    source, run, args, draft = model_draft(w)
    corrected = deepcopy(draft['version']['payload'])
    corrected['content']['sections'][0]['blocks'][2]['claim']['text'] = CORRECTED
    status, fixed = write_version(w.runs, w.actor, w.session, w.project, draft['version']['artifact_id'], None, 'saveVersion', ident('key'),
                                  dict(expected_revision=1, parent_version_id=draft['version']['id'], payload=corrected))
    assert status == 201 and fixed['checks'][0]['status'] == 'passed' and fixed['checks'][0]['findings'] == []
    assert fixed['review']['acceptance'] is None and fixed['version']['revision'] == 2
    assert fixed['version']['parent_version_id'] == draft['version']['id'] and fixed['version']['provenance']['origin'] == 'human_edit'
    # Исправление абзаца сохраняет корректную дату, суммы и основания соседней таблицы.
    assert fixed['version']['payload']['content']['sections'][0]['blocks'][1] == draft['version']['payload']['content']['sections'][0]['blocks'][1]
    assert fixed['version']['basis'] == draft['version']['basis']
    edited = fixed['version']['payload']['content']['sections'][0]['blocks'][2]['claim']
    assert edited['origin'] == 'human_editorial' and edited['fact_refs'] == [ref(source, 'fact_clients')]
    # The earlier version keeps its own prose, check results and acceptance state.
    assert w.runs.version(w.actor, w.project, draft['version']['id']) == draft
    with w.engine.connect() as c:
        head = execute(c, 'SELECT revision,latest_version_id FROM aist.artifacts WHERE id=:id', id=draft['version']['artifact_id']).first()
    assert tuple(head) == (2, fixed['version']['id'])
    # Acceptance is a separate decision about exactly this version and SHA.
    with pytest.raises(ApiError) as wrong:
        accept(w, fixed, content_sha256=draft['version']['content_sha256'])
    assert wrong.value.code == 'validation_failed'
    status, review = accept(w, fixed)
    assert status == 200 and review['acceptance']['artifact_version_id'] == fixed['version']['id']
    assert review['acceptance']['content_sha256'] == fixed['version']['content_sha256'] and review['acceptance']['acknowledge_unchecked_semantics']
    accepted = w.runs.version(w.actor, w.project, fixed['version']['id'])
    assert accepted['review']['acceptance'] == review['acceptance'] and accepted['review']['revision'] == 1
    assert w.runs.version(w.actor, w.project, draft['version']['id'])['review']['acceptance'] is None
    with pytest.raises(ApiError) as still:
        accept(w, draft, acknowledge_unchecked_semantics=False)
    assert still.value.code == 'validation_failed'
    # The accepted release of the corrected version carries no draft marker; the draft cannot be released as accepted.
    texts = released_text(w, accepted, 'accepted', tmp_path)
    for fmt, text_value in texts.items():
        assert 'ЧЕРНОВИК' not in text_value and 'замечания проверок' not in text_value and CORRECTED in text_value, fmt
    exports = Exports(w.runs)
    with pytest.raises(ApiError) as refused:
        exports.request(w.actor, w.session, w.project, draft['version']['artifact_id'], draft['version']['id'], ident('key'), export_request('docx', 'accepted'))
    assert refused.value.code == 'acceptance_required'


def test_boundaries_refuse_while_substitutions_with_valid_references_are_drafts(workspace):
    w = workspace
    source, facts = note_source(w)
    _, other = w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Другой проект'})
    elsewhere = SimpleNamespace(**{**vars(w), 'project': other['id']})
    foreign = upload(elsewhere, 'foreign', ['Чужой документ: выручка 100 млн руб.'])
    run = start_note_run(w, source)

    def attempt(blocks):
        content = dict(title='Границы сохранения', audience='Аналитик', sections=[dict(section_id='sec_one', title='Проверка', blocks=blocks)])
        return w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))

    refused = attempt([paragraph('block_foreign', 'Выручка составила 100 млн руб.', [dict(source_version_id=foreign['id'], fact_id='fact_foreign')])])
    assert not refused['ok'] and refused['error']['code'] == 'resource_not_found'
    refused = attempt([paragraph('block_absent', 'Выручка за 2022 год составила 3157 млн руб.', [ref(source, 'fact_absent')])])
    assert not refused['ok'] and refused['error']['code'] == 'validation_failed'
    assert '(block_absent): Факт fact_absent отсутствует' in refused['error']['message']
    clean = save_args(draft_note(source, corrected=True))['payload']
    pinned = dict(run['input'], source_versions=[dict(pin_of(source), sha256='0' * 64)])
    with w.engine.begin() as c, pytest.raises(ApiError) as mismatch:
        persist(w.runs, c, dict(id=run['id'], owner_id=w.actor, project_id=w.project, input=pinned), clean)
    assert mismatch.value.code == 'resource_not_found'
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 0
    # v29 leaves linked prose semantically unchecked; typed rows remain strict.
    # Neither technical outcome accepts the saved draft automatically.
    substitutions = {
        'currency': ([paragraph('block_usd', 'Собственный капитал на 31.03.2023 составил 3658 млн USD.', [ref(source, 'fact_capital1q23')])],
                     []),
        'period': ([paragraph('block_year', 'За 2022 год собственный капитал составил 3658 млн руб.', [ref(source, 'fact_capital1q23')]),
                    dict(block_id='block_rows', kind='table', caption='Капитал', rows=[
                        table_row('row_year', 'Собственный капитал', '2022', '3658', [ref(source, 'fact_capital1q23')])])],
                   [('row_basis_mismatch', 'block_rows')]),
    }
    for name, (blocks, expected) in substitutions.items():
        own = start_note_run(w, source)
        content = dict(title='Подмена ' + name, audience='Аналитик', sections=[dict(section_id='sec_one', title='Проверка', blocks=blocks)])
        saved = w.runs.tool(own['id'], own['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
        assert saved['ok'], saved
        check = saved['result']['checks'][0]
        assert check['profile_version'] == 'note-technical.v29'
        assert check['status'] == ('failed' if expected else 'passed'), name
        assert [(f['code'], f['block_id']) for f in check['findings']] == expected, name
        assert saved['result']['checks'][1]['status'] == 'not_checked'
        assert saved['result']['review']['acceptance'] is None
        if expected:
            with pytest.raises(ApiError):
                accept(w, saved['result'])
    # A clean version is saved without a false error.
    own = start_note_run(w, source)
    saved = w.runs.tool(own['id'], own['epoch'], ident('tool'), 'aist_save_artifact', save_args(draft_note(source, corrected=True)))
    assert saved['ok'] and saved['result']['checks'][0]['status'] == 'passed' and saved['result']['checks'][0]['findings'] == []


def test_continuation_from_a_draft_passes_its_remarks_to_the_executor(workspace):
    w = workspace
    source, run, args, draft = model_draft(w, row_mismatch=True)
    request = body(sources=[source['id']], question='Исправь замечания и сократи записку')
    request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note',
                   base_version_id=draft['version']['id'], expected_artifact_revision=1)
    send(w, conversation(w), request)
    continued = w.runs.claim('draft01_continuation')
    assert continued['input']['base_version_id'] == draft['version']['id']
    message = json.loads(execution_message(w.runs, continued, request['text'], []).rsplit('\n\n', 1)[-1])
    assert message['base_version']['id'] == draft['version']['id'] and message['facts'] == []
    programmatic, semantic = message['base_version_checks']
    assert programmatic['status'] == 'failed' and [f['code'] for f in programmatic['findings']] == ['row_basis_mismatch']
    assert programmatic['findings'][0]['block_id'] == 'block_structure' and semantic == dict(kind='semantic', status='not_checked', findings=[])
    assert 'base_version_checks' in message['instruction']


def test_short_answer_with_a_content_remark_is_still_refused(workspace):
    w = workspace
    source, facts = note_source(w)
    send(w, conversation(w), body(sources=[source['id']], question='Какой ROA за 1Q23?'))
    run = w.runs.claim('draft01_answer')

    def attempt(text_value):
        payload = dict(result_type='evidence_answer', schema_version='1.0.0', content=dict(missing_information=[], claims=[
            dict(text=text_value, origin='source_fact', fact_refs=[ref(source, 'fact_roa')], calculation_ids=[])]))
        return w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact',
                           dict(idempotency_key=ident('key'), expected_revision=0, artifact_id=None, parent_version_id=None, payload=payload))

    refused = attempt('ROA за 1Q23 составил 11,7%.')
    assert not refused['ok'] and refused['error']['code'] == 'validation_failed'
    assert refused['error']['message'].startswith('Утверждение 1 «ROA за 1Q23 составил 11,7%.»: Числа 11.7')
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar() == 0
    answered = attempt('ROA за 1Q23 составил 11,6%.')
    assert answered['ok'] and answered['result']['checks'][0]['status'] == 'passed'
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'completed'
    assert MONEY['currency'] == 'RUB'
