"""ASO-QUALITY-02, фаза 3: шаблонные строки, сведения из запроса и команда на пути сохранения."""
from copy import copy, deepcopy
from dataclasses import replace
import io
import hashlib
import json
import os

from docx import Document
import pytest
from pypdf import PdfReader

from app import edit_proposals as edits
from app.aso import version_date
from app.exports import Exports
from app.errors import ApiError
from app.note_renderer import renderer_versions
from app.runs import execute
from app.service import ident
from app.tools import REQUEST_ADVICE, TEAM_REQUEST, TEMPLATE_ADVICE
from app.versions import write
from test_chat01_postgres import body, conversation, send
from test_d15_postgres import workspace
from test_draft01_postgres import http
from test_draft01_r1_postgres import edit, save
from test_perf02_r4_postgres import note_source, paragraph, ref, save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')

REQUEST = 'Подготовь записку ASO по выбранному отчету. Сделка: покупка 25% акций за 500 млн руб.'
TEAM = 'И. Иванов, П. Петров'
OWNER_SUMMARY = '\n\nВладельцы: Синтетические материалы этого сценария не раскрывают владельцев.'


def start(w, source, question=REQUEST, parent=None, skill='v3'):
    request = body(sources=[source['id']], question=question)
    request.update(skill_version_id='skill_analytical-note-' + skill, requested_result_type='analytical_note')
    if parent:
        request.update(base_version_id=parent['version']['id'], expected_artifact_revision=parent['version']['revision'])
    send(w, conversation(w), request)
    return w.runs.claim('aso_quality02_phase3')


def line(block_id, text, origin):
    return dict(block_id=block_id, kind='paragraph', claim=dict(text=text, origin=origin, fact_refs=[], calculation_ids=[]))


def deal_note(source, team=None, amount=('Сумма сделки: 500 млн руб.', 'user_request'), section='Описание возможности'):
    """Записка ASO: возможность и сумма из запроса, шаблонные строки сделки и шагов, сведение из PDF."""
    content = dict(title='Синтетика: инвестиционная возможность', audience='Инвестиционный комитет', sections=[
        dict(section_id='sec_opportunity', title=section, blocks=[
            line('block_opportunity', 'Рассматривается покупка 25% акций Синтетики за 500 млн руб.', 'user_request')]),
        dict(section_id='sec_company', title='Описание Компании', blocks=[
            paragraph('block_clients', 'Прокатные фирмы сдают приборы для замера шума.', [ref(source, 'fact_clients')])]),
        dict(section_id='sec_deal', title='Описание Сделки', blocks=[
            line('block_amount', *amount),
            line('block_structure', 'Структура сделки: [TBD]', 'template')]),
        dict(section_id='sec_next', title='Следующие шаги', blocks=[
            line('block_next', 'Внутреннее обсуждение инвестиционной возможности', 'template')])])
    if team:
        content['team'] = team
    return content


def blocks(view):
    return {b['block_id']: b for s in view['version']['payload']['content']['sections'] for b in s['blocks']}


def reply_text(w, run):
    with w.engine.connect() as c:
        return execute(c, "SELECT text FROM aist.messages WHERE run_id=:r AND role='assistant'", r=run['id']).scalar_one()


def released(w, version, tmp_path):
    """Текст шапки и записки в черновых DOCX и PDF шаблона ASO."""
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    view = w.runs.version(w.actor, w.project, version['id'])
    texts = {}
    for fmt in ('docx', 'pdf'):
        status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], ident('key'),
            dict(expected_revision=view['review']['revision'], format=fmt, template_version='aist-aso.v1',
                 renderer_version=renderer_versions('aist-aso.v1')[fmt],
                 settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft')))
        assert status == 202, entry
        exports.process_one()
        data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
        assert ready['state'] == 'ready'
        if fmt == 'docx':
            texts[fmt] = ''.join(Document(io.BytesIO(data)).element.body.itertext())
        else:
            texts[fmt] = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    return texts


def test_request_and_template_lines_are_saved_with_the_request_snapshot_and_survive_a_human_edit(workspace):
    w = workspace
    source, _ = note_source(w)
    question = REQUEST + ' Команда: ' + TEAM + '.'
    run = start(w, source, question)
    first = save(w, run, deal_note(source, team=TEAM))
    assert first['checks'][0]['status'] == 'passed' and first['checks'][0]['findings'] == []
    version = first['version']
    assert version['payload']['content']['team'] == TEAM
    saved = blocks(first)
    # Ссылку на сообщение ставит AIST, модель ее не передает.
    for block_id in ('block_opportunity', 'block_amount'):
        assert saved[block_id]['claim']['request_message_ids'] == [run['message_id']]
    assert 'request_message_ids' not in saved['block_structure']['claim']
    assert version['basis']['request_messages'] == [dict(message_id=run['message_id'], text=question)]
    assert reply_text(w, run) == version['payload']['content']['title'] + OWNER_SUMMARY

    content = deepcopy(version['payload']['content'])
    deal, steps = content['sections'][2]['blocks'], content['sections'][3]['blocks']
    deal[1]['claim']['text'] = 'Структура сделки: 2 транша'
    steps.append(line('block_meeting', 'Встреча с менеджментом через 2 недели', 'human_editorial'))
    with http(w) as client:
        child = edit(w, client, first, content, 'human', [source])
    # Свободный текст раздела сделки допускается, в следующих шагах ручное число остается замечанием.
    assert [(f['code'], f['block_id']) for f in child['checks'][0]['findings']] == [('unsupported_editorial_number', 'block_meeting')]
    assert child['version']['basis']['request_messages'] == version['basis']['request_messages']
    assert blocks(child)['block_amount']['claim'] == saved['block_amount']['claim']


def test_request_scope_template_and_message_are_checked(workspace):
    w = workspace
    source, _ = note_source(w)
    run = start(w, source, REQUEST + ' Команда: И. Иванов.')

    def refused(content, expected):
        result = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
        assert not result['ok'] and result['error']['code'] == 'validation_failed'
        assert expected in result['error']['message'], result['error']['message']

    refused(deal_note(source, section='Описание Компании'), REQUEST_ADVICE)
    refused(deal_note(source, amount=('Сумма сделки: 500 млн руб.', 'template')), TEMPLATE_ADVICE)
    foreign = deal_note(source)
    foreign['sections'][0]['blocks'][0]['claim']['request_message_ids'] = ['msg_foreign']
    refused(foreign, 'Сообщение запроса недоступно для этой версии.')
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == 0
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'running'



def test_team_is_requested_once_set_by_chat_command_and_absent_in_an_old_note(workspace, monkeypatch, tmp_path):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    source, _ = note_source(w)
    run = start(w, source)
    first = save(w, run, deal_note(source))
    assert first['version']['payload']['content']['team'] == '[TBD]'
    assert reply_text(w, run) == first['version']['payload']['content']['title'] + '\n' + TEAM_REQUEST + OWNER_SUMMARY
    run = start(w, source, parent=first)
    child = save(w, run, deal_note(source), parent=first)
    assert child['version']['payload']['content']['team'] == '[TBD]'
    assert reply_text(w, run) == child['version']['payload']['content']['title']

    chat = w.runs.get(w.actor, w.project, run['id'])['conversation_id']
    scope = (w.runs, w.actor, w.session, w.project, chat)
    proposal = edits.prepare(*scope, ident('key'), dict(version_id=child['version']['id'],
                                                        text='Измени команду на ' + TEAM + '.'))['proposal']
    assert (proposal['target'], proposal['before'], proposal['after']) == (dict(field='team'), '[TBD]', TEAM)
    applied = edits.decide(*scope, proposal['id'], ident('key'), 'apply')
    team_view = w.runs.version(w.actor, w.project, applied['applied_version_id'])
    assert team_view['version']['payload']['content']['team'] == TEAM
    assert team_view['version']['provenance']['origin'] == 'human_edit'
    assert team_view['checks'][0]['status'] == 'passed'
    assert team_view['version']['basis']['request_messages'] == child['version']['basis']['request_messages']
    texts = released(w, team_view['version'], tmp_path)
    for text in texts.values():
        assert 'Команда: ' + TEAM in text and version_date(team_view['version']['created_at']) in text

    run = start(w, source, skill='v2')
    old = save(w, run, dict(title='Синтетика: справка', audience='Аналитик', sections=[dict(
        section_id='sec_company', title='Описание Компании', blocks=[
            paragraph('block_clients', 'Прокатные фирмы сдают приборы для замера шума.', [ref(source, 'fact_clients')])])]))
    assert 'team' not in old['version']['payload']['content']
    chat = w.runs.get(w.actor, w.project, run['id'])['conversation_id']
    hint = edits.prepare(w.runs, w.actor, w.session, w.project, chat, ident('key'),
                         dict(version_id=old['version']['id'], text='Измени команду на ' + TEAM))
    assert hint == dict(kind='hint', message='У этой версии нет строки «Команда». Укажите команду в редакторе записки.')
    assert all('Команда' not in text for text in released(w, old['version'], tmp_path).values())
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0


def accept(w, view):
    version = view['version']
    return write(w.runs, w.actor, w.session, w.project, version['artifact_id'], version['id'],
                 'acceptVersion', ident('key'), dict(expected_revision=view['review']['revision'],
                 content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=True))


@pytest.mark.parametrize('question,text', [
    ('Условие: в 2027 году/кВт.Ч, цена фиксирована.', 'Условие: в 2027 году/кВт.'),
    ('Условие: в 2027 году/кВт.Ч, цена фиксирована.', 'Условие: в 2027 году/кВт.М, цена фиксирована.'),
    ('Условие: 2027 годов (план)/акция. А. Петров готовит IPO.', 'Условие: 2027 годов (план)/акция.'),
    ('Сумма сделки: 500 млн руб.', 'Сумма сделки: 500 млн USD.'),
    ('Доля фонда: 25%. Комиссия: 10%.', 'Доля фонда: 10%. Комиссия: 25%.'),
    ('Срок: 5 лет.', 'Срок: --5 лет ( после одобрения ).'),
])
def test_free_request_text_is_unchecked_and_requires_separate_human_acceptance(workspace, question, text, record_property):
    w = workspace
    source, _ = note_source(w)
    run = start(w, source, REQUEST + ' ' + question)
    view = save(w, run, deal_note(source, amount=(text, 'user_request')))
    for revision in (1, 2):
        assert view['version']['revision'] == revision
        assert view['checks'][0]['status'] == 'passed' and view['checks'][0]['findings'] == []
        assert view['checks'][0]['profile_version'] == 'note-technical.v29'
        assert view['checks'][1]['status'] == 'not_checked'
        assert view['review']['acceptance'] is None
        claim = blocks(view)['block_amount']['claim']
        assert claim['text'] == text and claim['origin'] == 'user_request'
        assert view['version']['basis']['request_messages'] == [dict(message_id=claim['request_message_ids'][0],
                                                                     text=REQUEST + ' ' + question)]
        version = view['version']
        with pytest.raises(ApiError) as rejected:
            write(w.runs, w.actor, w.session, w.project, version['artifact_id'], version['id'],
                  'acceptVersion', ident('key'), dict(expected_revision=0,
                  content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=False))
        assert rejected.value.code == 'validation_failed'
        assert w.runs.version(w.actor, w.project, version['id']) == view
        accept(w, view)
        accepted = w.runs.version(w.actor, w.project, version['id'])
        assert accepted['review']['acceptance']['content_sha256'] == version['content_sha256']
        record_property('accepted_unchecked_text', json.dumps(accepted, ensure_ascii=False))
        if revision == 1:
            parent = accepted
            run = start(w, source, 'Продолжи записку.', parent=parent)
            view = save(w, run, deepcopy(parent['version']['payload']['content']), parent=parent)
        assert w.runs.version(w.actor, w.project, parent['version']['id']) == parent


@pytest.mark.parametrize('skill', ['v2', 'v3'])
@pytest.mark.parametrize('team', ['С. Сидоров', 'иван иванов (аналитик), и а.-м. петрова'])
def test_team_text_is_unchecked_and_missing_team_inherits_without_acceptance(workspace, skill, team):
    w = workspace
    source, _ = note_source(w)
    run = start(w, source, REQUEST + ' Команда: И. Иванов.', skill=skill)
    first = save(w, run, deal_note(source, team=team))
    assert first['version']['payload']['content']['team'] == team
    assert first['checks'][0]['status'] == 'passed' and first['checks'][1]['status'] == 'not_checked'
    accept(w, first)
    parent = w.runs.version(w.actor, w.project, first['version']['id'])
    content = deepcopy(parent['version']['payload']['content'])
    content.pop('team')
    run = start(w, source, 'Продолжи записку.', parent=parent, skill=skill)
    child = save(w, run, content, parent=parent)
    assert child['version']['payload']['content']['team'] == team
    assert child['review']['acceptance'] is None
    content = deepcopy(child['version']['payload']['content'])
    content['team'] = 'А. Миронов'
    run = start(w, source, 'Продолжи записку.', parent=child, skill=skill)
    changed = save(w, run, content, parent=child)
    assert changed['version']['payload']['content']['team'] == 'А. Миронов'
    assert changed['checks'][0]['status'] == 'passed' and changed['review']['acceptance'] is None
    assert w.runs.version(w.actor, w.project, parent['version']['id']) == parent


@pytest.mark.parametrize('section,allowed', [('Описание Сделки', True), ('Описание возможности', True),
                                          ('Описание Компании', False)])
def test_free_manual_number_is_allowed_only_in_deal_sections(workspace, section, allowed):
    w = workspace
    source, _ = note_source(w)
    first = save(w, start(w, source), deal_note(source))
    content = deepcopy(first['version']['payload']['content'])
    selected = next(s for s in content['sections'] if s['title'] == section)
    selected['blocks'].append(line('block_manual', 'График платежей: 7 траншей в 2027 году.', 'human_editorial'))
    with http(w) as client:
        child = edit(w, client, first, content, 'human', [source])
    assert child['checks'][0]['status'] == ('passed' if allowed else 'failed')
    assert child['review']['acceptance'] is None
    if allowed:
        assert child['checks'][0]['findings'] == []
        accept(w, child)
    else:
        assert any(f['code'] == 'unsupported_editorial_number' and f['block_id'] == 'block_manual'
                   for f in child['checks'][0]['findings'])
        with pytest.raises(ApiError):
            accept(w, child)
    assert w.runs.version(w.actor, w.project, first['version']['id']) == first


def test_deal_paragraph_with_fact_keeps_basis_but_prose_is_unchecked_after_human_edit(workspace):
    w = workspace
    source, _ = note_source(w)
    content = deal_note(source)
    content['sections'][2]['blocks'].append(paragraph('block_capital', 'Собственный капитал 3658 млн руб.',
                                                   [ref(source, 'fact_capital1q23')]))
    first = save(w, start(w, source), content)
    assert first['checks'][0]['status'] == 'passed'
    content = deepcopy(first['version']['payload']['content'])
    content['sections'][2]['blocks'][-1]['claim']['text'] = 'Собственный капитал 3658 млн USD.'
    with http(w) as client:
        child = edit(w, client, first, content, 'human', [source])
    assert child['checks'][0]['status'] == 'passed' and child['checks'][0]['findings'] == []
    assert child['checks'][0]['profile_version'] == 'note-technical.v29'
    assert child['checks'][1]['status'] == 'not_checked'
    assert child['version']['basis'] == first['version']['basis']
    assert blocks(child)['block_capital']['claim']['text'] == 'Собственный капитал 3658 млн USD.'
    assert child['review']['acceptance'] is None
    version = child['version']
    with pytest.raises(ApiError) as rejected:
        write(w.runs, w.actor, w.session, w.project, version['artifact_id'], version['id'],
              'acceptVersion', ident('key'), dict(expected_revision=0,
              content_sha256=version['content_sha256'], acknowledge_unchecked_semantics=False))
    assert rejected.value.code == 'validation_failed'
    accept(w, child)
    assert w.runs.version(w.actor, w.project, first['version']['id']) == first


@pytest.mark.parametrize('mode', ['editor', 'chat'])
@pytest.mark.parametrize('text', ['Сумма сделки: 600 млн руб.', 'Сумма сделки: цена приобретения 500 млн руб.'])
def test_changed_request_claim_saves_and_continues_without_stale_provenance(workspace, monkeypatch, mode, text):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    source, _ = note_source(w)
    first_run = start(w, source)
    first = save(w, first_run, deal_note(source))
    # Продолжение исходной версии берет снимок родителя, а не приписывает число новому сообщению.
    run = start(w, source, 'Продолжи записку по отчету.', parent=first)
    parent = save(w, run, deepcopy(first['version']['payload']['content']), parent=first)
    assert blocks(parent)['block_amount'] == blocks(first)['block_amount']
    accept(w, parent)
    parent = w.runs.version(w.actor, w.project, parent['version']['id'])
    if mode == 'editor':
        content = deepcopy(parent['version']['payload']['content'])
        content['sections'][2]['blocks'][0]['claim']['text'] = text
        with http(w) as client:
            child = edit(w, client, parent, content, 'human', [source])
            old = parent['version']
            response = client.post(f'/v1/projects/{w.project}/artifacts/{old["artifact_id"]}/versions',
                json=dict(expected_revision=old['revision'], parent_version_id=old['id'],
                          payload=dict(old['payload'], content=content)), headers={'Idempotency-Key': ident('key')})
            assert response.status_code == 409, response.text
    else:
        scope = (w.runs, w.actor, w.session, w.project, run['conversation_id'])
        proposal = edits.prepare(*scope, ident('key'), dict(version_id=parent['version']['id'],
            text='Замени «Сумма сделки: 500 млн руб.» на «' + text + '»'))['proposal']
        applied = edits.decide(*scope, proposal['id'], ident('key'), 'apply')
        assert edits.decide(*scope, proposal['id'], ident('key'), 'apply') == applied
        child = w.runs.version(w.actor, w.project, applied['applied_version_id'])
    assert child['checks'][0]['status'] == 'passed' and child['checks'][0]['findings'] == []
    assert child['checks'][0]['profile_version'] == 'note-technical.v29'
    assert child['review']['acceptance'] is None
    edited = blocks(child)['block_amount']['claim']
    assert edited['text'] == text and edited['origin'] == 'human_editorial'
    assert 'request_message_ids' not in edited
    assert blocks(child)['block_opportunity'] == blocks(first)['block_opportunity']
    assert child['version']['basis']['request_messages'] == first['version']['basis']['request_messages']
    assert w.runs.version(w.actor, w.project, parent['version']['id']) == parent
    accept(w, child)
    accepted = w.runs.version(w.actor, w.project, child['version']['id'])
    assert accepted['review']['acceptance']['content_sha256'] == child['version']['content_sha256']
    assert accepted['review']['acceptance']['artifact_version_id'] == child['version']['id']
    run = start(w, source, 'Продолжи записку по отчету.', parent=accepted)
    next_view = save(w, run, deepcopy(child['version']['payload']['content']), parent=accepted)
    assert blocks(next_view)['block_amount']['claim'] == edited
    assert next_view['checks'][0]['status'] == 'passed' and next_view['review']['acceptance'] is None
    assert w.runs.version(w.actor, w.project, first['version']['id']) == first
    assert w.runs.version(w.actor, w.project, accepted['version']['id']) == accepted


@pytest.mark.parametrize('mode', ['editor', 'chat'])
def test_explicit_team_edit_allows_new_names_without_inheriting_acceptance(workspace, monkeypatch, mode):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    source, _ = note_source(w)
    run = start(w, source, REQUEST + ' Команда: и.   иванов, Петр Петров.')
    first = save(w, run, deal_note(source, team='И.Иванов, Петр Петров'))
    accept(w, first)
    first = w.runs.version(w.actor, w.project, first['version']['id'])
    team = 'А. Сидоров, Мария Миронова'
    if mode == 'editor':
        with http(w) as client:
            content = dict(deepcopy(first['version']['payload']['content']), team=team)
            child = edit(w, client, first, content, 'human', [source])
    else:
        scope = (w.runs, w.actor, w.session, w.project, run['conversation_id'])
        proposal = edits.prepare(*scope, ident('key'), dict(version_id=first['version']['id'],
                                                           text='Измени команду на ' + team))['proposal']
        applied = edits.decide(*scope, proposal['id'], ident('key'), 'apply')
        child = w.runs.version(w.actor, w.project, applied['applied_version_id'])
    assert child['version']['payload']['content']['team'] == team
    assert child['checks'][0]['status'] == 'passed' and child['review']['acceptance'] is None
    run = start(w, source, 'Продолжи записку.', parent=child)
    inherited = save(w, run, deepcopy(child['version']['payload']['content']), parent=child)
    assert inherited['version']['payload']['content']['team'] == team
    assert w.runs.version(w.actor, w.project, first['version']['id']) == first


@pytest.mark.parametrize('scope', ['run', 'chat', 'project', 'owner', 'assistant'])
def test_existing_foreign_message_cannot_be_inherited(workspace, scope):
    w = workspace
    foreign = copy(w)
    if scope == 'owner':
        foreign.actor, foreign.session = ident('usr'), ident('ses')
        with w.engine.begin() as c:
            execute(c, 'INSERT INTO aist.users(id,label) VALUES (:id,:label)', id=foreign.actor, label='Другой пользователь')
            execute(c, """INSERT INTO aist.sessions(id,user_id,token_hash,expires_at)
                VALUES (:id,:actor,:hash,clock_timestamp()+interval '1 hour')""",
                id=foreign.session, actor=foreign.actor, hash=ident('synthetic'))
    if scope in ('project', 'owner'):
        _, project = w.service.write(foreign.actor, 'createProject', ident('key'), {'name': 'Другой проект'})
        foreign.project = project['id']
    source, _ = note_source(foreign)
    prior_run = start(foreign, source)
    save(foreign, prior_run, deal_note(source))
    message_id = prior_run['message_id']
    if scope == 'assistant':
        with w.engine.connect() as c:
            message_id = execute(c, "SELECT id FROM aist.messages WHERE run_id=:r AND role='assistant'",
                                 r=prior_run['id']).scalar_one()
    own_source = source if scope in ('run', 'chat', 'assistant') else note_source(w)[0]
    first_run = start(w, own_source)
    first = save(w, first_run, deal_note(own_source))
    if scope == 'run':
        request = body(revision=2, sources=[source['id']], question='Продолжи записку.')
        request.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note',
                       base_version_id=first['version']['id'], expected_artifact_revision=first['version']['revision'])
        send(w, dict(id=prior_run['conversation_id']), request)
        run = w.runs.claim('request_scope')
    else:
        run = start(w, own_source, 'Продолжи записку.', parent=first)
    content = deepcopy(first['version']['payload']['content'])
    content['sections'][0]['blocks'][0]['claim']['request_message_ids'] = [message_id]
    args = save_args(content)
    args.update(expected_revision=first['version']['revision'], artifact_id=first['version']['artifact_id'],
                parent_version_id=first['version']['id'])
    result = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert not result['ok'] and 'Сообщение запроса недоступно' in result['error']['message']


def test_aso_renderer_upgrade_preserves_completed_releases(workspace, tmp_path, monkeypatch):
    from app import aso_renderer
    w = workspace
    source, _ = note_source(w)
    run = start(w, source)
    view = save(w, run, deal_note(source))
    version = view['version']
    w.runs.settings = replace(w.settings, export_root=str(tmp_path / 'exports'))
    exports = Exports(w.runs)
    legacy = deepcopy(renderer_versions('aist-note.v2'))
    for fmt in ('docx', 'pdf'):
        request = dict(expected_revision=view['review']['revision'], format=fmt, template_version='aist-aso.v1',
                       renderer_version=renderer_versions('aist-aso.v1')[fmt],
                       settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft'))
        key = ident('key')
        status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], key, request)
        assert status == 202
        exports.process_one()
        old_data, old = exports.get(w.actor, w.project, entry['id'], download=True)
        old_sha = hashlib.sha256(old_data).hexdigest()
        with monkeypatch.context() as changed:
            changed.setitem(aso_renderer.RENDERERS, fmt, request['renderer_version'] + '.next')
            new_request = dict(request, renderer_version=renderer_versions('aist-aso.v1')[fmt])
            status, entry = exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], ident('key'), new_request)
            assert status == 202 and entry['id'] != old['id'] and entry['identity_sha256'] != old['identity_sha256']
            exports.process_one()
            new_data, new = exports.get(w.actor, w.project, entry['id'], download=True)
            assert new['state'] == 'ready' and new_data
            assert exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], ident('key'), new_request) == (200, new)
            assert exports.request(w.actor, w.session, w.project, version['artifact_id'], version['id'], key, request) == (200, old)
            old_after, receipt_after = exports.get(w.actor, w.project, old['id'], download=True)
            assert receipt_after == old and hashlib.sha256(old_after).hexdigest() == old_sha
    assert renderer_versions('aist-note.v2') == legacy
    assert w.runs.version(w.actor, w.project, version['id']) == view


@pytest.mark.parametrize('parent_team,change,require_latest', [
    ('И. Иванов', 'omit', False), ('И. Иванов', 'omit', True),
    ('[TBD]', 'omit', True), (None, 'omit', True),
    ('И. Иванов', '[TBD]', True), ('И. Иванов', 'А.-М. Петрова', True),
    (None, 'А.-М. Петрова', False),
])
def test_http_team_inheritance_preserves_explicit_value_history_and_retry(workspace, parent_team, change, require_latest):
    w = workspace
    source, _ = note_source(w)
    parent = save(w, start(w, source, skill='v2'), deal_note(source, team=parent_team))
    accept(w, parent)
    parent = w.runs.version(w.actor, w.project, parent['version']['id'])
    v = parent['version']
    content = deepcopy(v['payload']['content'])
    content.pop('team', None)
    content['audience'] = 'Совет директоров'
    if change != 'omit':
        content['team'] = change
    request = dict(expected_revision=v['revision'], parent_version_id=v['id'],
                   payload=dict(v['payload'], content=content))
    if require_latest:
        request['require_latest'] = True
    headers = {'Idempotency-Key': ident('key')}
    url = f'/v1/projects/{w.project}/artifacts/{v["artifact_id"]}/versions'
    with http(w) as client:
        response = client.post(url, json=request, headers=headers)
        assert response.status_code == 201, response.text
        child = response.json()
        expected = parent_team if change == 'omit' else change
        assert child['version']['payload']['content'].get('team') == expected
        assert ('team' in child['version']['payload']['content']) == (expected is not None)
        assert child['review']['acceptance'] is None and child['checks'][0]['status'] == 'passed'
        assert child['version']['payload']['content']['audience'] == 'Совет директоров'
        assert child['version']['content_sha256'] != v['content_sha256']
        repeated = client.post(url, json=request, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == child
        # Неизмененный payload по пути редактора возвращает ту же версию.
        unchanged = client.post(url, json=dict(expected_revision=child['version']['revision'],
            parent_version_id=child['version']['id'], require_latest=True, payload=child['version']['payload']),
            headers={'Idempotency-Key': ident('key')})
        assert unchanged.status_code == 200 and unchanged.json() == child
    assert w.runs.version(w.actor, w.project, v['id']) == parent


@pytest.mark.parametrize('team', ['', None, ['И. Иванов'], 'А' * 301])
def test_http_invalid_team_is_not_replaced_by_parent(workspace, team):
    w = workspace
    source, _ = note_source(w)
    parent = save(w, start(w, source), deal_note(source, team='И. Иванов'))
    v = parent['version']
    content = dict(deepcopy(v['payload']['content']), team=team)
    with http(w) as client:
        url = f'/v1/projects/{w.project}/artifacts/{v["artifact_id"]}'
        response = client.post(url+'/versions', json=dict(expected_revision=v['revision'],
            parent_version_id=v['id'], payload=dict(v['payload'], content=content)), headers={'Idempotency-Key': ident('key')})
        assert response.status_code == 422, response.text
        assert client.get(url).json()['revision'] == v['revision']
    assert w.runs.version(w.actor, w.project, v['id']) == parent
