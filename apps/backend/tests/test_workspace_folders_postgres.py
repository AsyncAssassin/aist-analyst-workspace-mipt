"""Concurrency, folder scope and immutable conversation history for the new workspace."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.errors import ApiError
from app.navigation import change
from app.service import ident
from test_d15_postgres import workspace, publish_fixture_worker

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Isolated PostgreSQL required')


def test_concurrent_personal_and_move_replay(workspace):
    w = workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda key: change(w.service, w.actor, {'action':'personal'}, key)['id'],
                            [ident('key'), ident('key')]))
    assert ids[0] == ids[1]
    assert ids[0] not in {p['id'] for p in w.service.listing(w.actor,'projects',{})['items']}
    chat = w.service.write(w.actor,'createConversation',ident('key'),{'title':'Личная записка'},ids[0])[1]
    key = ident('key')
    move = dict(action='move',chat_id=chat['id'],expected_folder_id=None,folder_id=w.project)
    change(w.service,w.actor,move,key)
    change(w.service,w.actor,dict(move,expected_folder_id=w.project,folder_id=None),ident('key'))
    change(w.service,w.actor,move,key)
    with w.engine.connect() as c:
        actual = c.execute(text('SELECT project_id,folder_project_id,revision FROM aist.conversations WHERE id=:id'),
                           {'id':chat['id']}).one()
        assert tuple(actual) == (ids[0],None,0)
    with pytest.raises(ApiError,match='idempotency_conflict') as caught:
        change(w.service,w.actor,dict(move,folder_id=None),key)
    assert caught.value.status == 409
    change(w.service,w.actor,dict(action='hide',chat_id=chat['id']),ident('key'))
    assert not w.service.listing(w.actor,'conversations',{},ids[0])['items']
    with pytest.raises(ApiError) as caught:
        w.runs.listing(w.actor,ids[0],'messages',{},chat['id'])
    assert caught.value.status == 404


def test_recent_pdfs_bounded_without_opening_storage(workspace, monkeypatch):
    from types import SimpleNamespace
    from app.navigation import recent_pdfs
    from app.search import register
    from test_d15_postgres import upload

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    req = SimpleNamespace(state=SimpleNamespace(actor=w.actor),
                          app=SimpleNamespace(state=SimpleNamespace(service=w.service)), query_params={})
    eligible = [upload(w)['version']['id'] for _ in range(21)]
    sample = upload(w)
    descendant = upload(w, previous=sample)
    register(w.service, w.actor, w.project, sample['version']['id'], 'samples', None, None, 'Образец')
    failed = upload(w)['version']['id']
    with w.engine.begin() as connection:
        connection.execute(text("UPDATE aist.source_versions SET processing_state='failed',error='{}'::jsonb WHERE id=:id"),
                           {'id': failed})
    monkeypatch.setattr(w.service.storage, 'read', lambda *_: pytest.fail('Recent list read PDF bytes'))
    items = recent_pdfs(w.project, req)['items']
    assert [item['id'] for item in items] == eligible[-20:][::-1]
    assert descendant['version']['id'] not in {item['id'] for item in items}
    foreign = ident('usr')
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', foreign)
    req.state.actor = foreign
    with pytest.raises(ApiError) as error:
        recent_pdfs(w.project, req)
    assert error.value.status == 404


def test_material_selection_and_history_are_frozen_per_send(workspace, monkeypatch):
    from app.project_materials import options
    from app.search import register, require_factual
    from test_chat01_postgres import body, conversation, send
    from test_d15_postgres import upload

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    src = upload(w)['version']
    sibling = conversation(w)
    _, first = send(w, sibling, body(sources=[src['id']], question='Сравните продажи в штуках'))
    w.runs.write(w.actor,w.session,w.project,first['run']['id'],'cancelRun',ident('key'),dict(expected_revision=first['run']['revision']))
    personal = change(w.service,w.actor,dict(action='personal'),ident('key'))['id']
    target = w.service.write(w.actor,'createConversation',ident('key'),dict(title='Перемещённый чат'),personal)[1]['id']
    def move(folder, previous):
        return change(w.service,w.actor,dict(action='move',chat_id=target,folder_id=folder,expected_folder_id=previous),ident('key'))
    move(w.project, None)
    selection = dict(folder_id=w.project,pdf_version_ids=[src['id']],chats=[dict(id=sibling['id'],through_sequence=1)])
    command = dict(body(question='В каких единицах сравниваем?'),project_context=selection)
    key = ident('key')
    def submit(data, idem):
        return w.runs.write(w.actor,w.session,personal,target,'submitMessage',idem,data)
    with w.engine.begin() as c:
        visible = options(c,w.actor,w.project,'')
        assert src['id'] in [p['id'] for p in visible['pdfs']]
        assert dict(id=sibling['id'],title=sibling['title'],through_sequence=1) in visible['chats']
    with pytest.raises(ApiError):
        submit(dict(command,project_context=dict(selection,chats=[dict(id=sibling['id'],through_sequence=9)])),ident('key'))
    with w.engine.connect() as c:
        assert not c.execute(text('SELECT 1 FROM aist.source_versions WHERE project_id=:p'),dict(p=personal)).first()
    _, extra = send(w,sibling,body(revision=1,question='Более поздний вопрос'))
    w.runs.write(w.actor,w.session,w.project,extra['run']['id'],'cancelRun',ident('key'),dict(expected_revision=extra['run']['revision']))
    _, sent = submit(command,key)
    run=sent['run']
    alias=run['input']['source_versions'][0]['source_version_id']
    assert alias!=src['id'] and run['input']['project_context']['pdfs'][0]['local_version_id']==alias
    def history(run_id):
        with w.engine.connect() as c:
            return c.execute(text('SELECT configuration FROM aist.runs WHERE id=:id'),dict(id=run_id)).scalar_one()['conversation_context']
    observed=history(run['id'])['project_chats'][0]
    assert observed['through_sequence']==1 and [m['text'] for m in observed['messages']]==['Сравните продажи в штуках']
    with w.engine.connect() as c:
        rows=c.execute(text('SELECT storage_name FROM aist.source_versions WHERE id=ANY(:ids)'),dict(ids=[src['id'],alias])).scalars().all()
        assert len(rows)==2 and len(set(rows))==1
    w.runs.write(w.actor,w.session,personal,run['id'],'cancelRun',ident('key'),dict(expected_revision=run['revision']))
    _, plain=submit(body(revision=1,question='Отдельный вопрос'),ident('key'))
    assert plain['run']['input']['source_versions']==[] and 'project_context' not in plain['run']['input']
    assert 'Сравните продажи' not in str(history(plain['run']['id']))
    w.runs.write(w.actor,w.session,personal,plain['run']['id'],'cancelRun',ident('key'),dict(expected_revision=plain['run']['revision']))
    _, again = submit(dict(command,expected_revision=2),ident('key'))
    assert again['run']['input']['source_versions']==run['input']['source_versions']
    with w.engine.connect() as c:
        registered = c.execute(text('SELECT count(*) FROM aist.source_versions WHERE project_id=:p'),dict(p=personal)).scalar_one()
        assert registered==1
    w.runs.write(w.actor,w.session,personal,again['run']['id'],'cancelRun',ident('key'),dict(expected_revision=again['run']['revision']))
    move(None, w.project)
    assert submit(command,key)[1]['run']['id']==run['id']
    with pytest.raises(ApiError) as denied:
        submit(dict(command,expected_revision=3),ident('key'))
    assert denied.value.status==404
    move(w.project, None)
    register(w.service,w.actor,w.project,src['id'],'samples',None,None,'Формат')
    with w.engine.connect() as c:
        assert not options(c,w.actor,w.project,'')['pdfs']
        with pytest.raises(ApiError):
            require_factual(c,alias)
    with pytest.raises(ApiError):
        submit(dict(command,expected_revision=3),ident('key'))


def test_history_selection_checks_folder_visibility_and_total_size(workspace, monkeypatch):
    from app.project_materials import options
    from app.runs import json_value
    from test_chat01_postgres import body, conversation, send
    from test_d15_postgres import upload
    from types import SimpleNamespace

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    target = conversation(w)
    chats = [conversation(w) for _ in range(3)]
    for chat in chats:
        _, item = send(w,chat,body(question='Текст ' + 'Я'*4500))
        seed = item['run']
        w.runs.write(w.actor,w.session,w.project,seed['id'],'cancelRun',ident('key'),dict(expected_revision=seed['revision']))
    request = body()
    selection = dict(folder_id=w.project,pdf_version_ids=[],chats=[dict(id=c['id'],through_sequence=1) for c in chats])
    foreign = ident('usr')
    with w.engine.begin() as c:
        c.execute(text('INSERT INTO aist.users(id,label) VALUES (:id,:label)'), dict(id=foreign,label='Второй владелец'))
    their_project = w.service.write(foreign,'createProject',ident('key'),dict(name='Чужая папка'))[1]['id']
    other = SimpleNamespace(actor=foreign,project=their_project,service=w.service)
    their_pdf = upload(other)['version']
    their_chat = conversation(other)
    for selection_patch in (dict(pdf_version_ids=[their_pdf['id']]),dict(chats=[dict(id=their_chat['id'],through_sequence=1)]),
                            dict(folder_id=their_project),dict(chats=[dict(id=target['id'],through_sequence=1)])):
        with pytest.raises(ApiError) as denied:
            send(w,target,dict(request,project_context=dict(selection,**selection_patch)))
        assert denied.value.status==404
    with w.engine.connect() as c:
        listed = options(c,w.actor,w.project,'')
        assert their_pdf['id'] not in [v['id'] for v in listed['pdfs']]
        assert their_chat['id'] not in [v['id'] for v in listed['chats']]
    _, out = send(w,target,dict(request,project_context=selection))
    with w.engine.connect() as c:
        config=c.execute(text('SELECT configuration FROM aist.runs WHERE id=:id'),dict(id=out['run']['id'])).scalar_one()
    history=config['conversation_context']
    assert len(json_value(history).encode())<=12000
    assert all(chat['truncated'] and chat['messages'][0]['truncated_text'] and chat['messages'][0]['text'].startswith('Текст')
               for chat in history['project_chats'])


def test_note_titles_follow_the_first_message_and_preserve_manual_titles(workspace):
    from app.navigation import chat_views
    from test_chat01_postgres import body, send
    from test_perf02_r4_postgres import note_source

    w = workspace
    source, _ = note_source(w)
    prompt = 'Подготовь записку по материалам'
    chats = []
    for title in (prompt, 'Для встречи'):
        chat = w.service.write(w.actor, 'createConversation', ident('key'), {'title': title}, w.project)[1]
        req = body(sources=[source['id']], question=prompt + '\nСравни выручку.')
        req.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note')
        send(w, chat, req)
        chats.append(chat['id'])
    with w.engine.begin() as c:
        rows = c.execute(text('SELECT * FROM aist.conversations WHERE id=ANY(:ids)'), {'ids': chats}).mappings().all()
        def names():
            return {r['id']: r['title'] for r in chat_views(c, w.actor, rows)}
        assert names() == {chats[0]: 'Записка по файлу ' + source['original_filename'], chats[1]: 'Для встречи'}
        c.execute(text("""UPDATE aist.runs SET news_run=jsonb_build_object('company','Компания А')
                          WHERE conversation_id=ANY(:ids)"""), {'ids': chats})
        assert names() == {chats[0]: 'Записка: Компания А', chats[1]: 'Для встречи'}


def test_rename_is_scoped_replayable_and_preserves_manual_title(workspace, monkeypatch):
    from app.navigation import chat_views
    from app.project_materials import options, select_materials
    from test_chat01_postgres import body, send

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    prompt = 'Подготовь аналитическую записку.'
    chat = w.service.write(w.actor, 'createConversation', ident('key'), {'title': prompt}, w.project)[1]
    key = ident('key')
    command = dict(action='rename', chat_id=chat['id'], expected_title=prompt, title='Для обсуждения')
    change(w.service, w.actor, command, key)
    with pytest.raises(ApiError, match='revision_conflict'):
        change(w.service, w.actor, dict(command, title='Устаревшая правка'), ident('key'))
    with pytest.raises(ApiError, match='validation_error'):
        change(w.service, w.actor, dict(command, title='   '), ident('key'))
    # Even the original automatic prompt is a valid manually chosen name.
    change(w.service, w.actor, dict(command, expected_title='Для обсуждения', title=prompt), ident('key'))
    assert change(w.service, w.actor, command, key) == {'id': chat['id']}
    with pytest.raises(ApiError, match='resource_not_found'):
        change(w.service, 'foreign', command, ident('key'))
    req = body(question=prompt)
    send(w, chat, req)
    with w.engine.begin() as c:
        c.execute(text("UPDATE aist.runs SET news_run=jsonb_build_object('company','Другое название') WHERE conversation_id=:id"), {'id': chat['id']})
        saved = c.execute(text('SELECT * FROM aist.conversations WHERE id=:id'), {'id': chat['id']}).mappings().one()
        assert chat_views(c, w.actor, [saved])[0]['title'] == prompt
        assert saved['project_id'] == w.project and saved['revision'] == 1

    renamed = dict(command, expected_title=prompt, title='Проверка названия в контексте')
    change(w.service, w.actor, renamed, ident('key'))
    with w.engine.begin() as c:
        c.execute(text('UPDATE aist.conversations SET folder_project_id=:folder WHERE id=:id'),
                  {'folder': w.project, 'id': chat['id']})
        assert options(c, w.actor, w.project, 'названия')['chats'][0]['title'] == renamed['title']
        _, selected = select_materials(c, w.actor,
            {'id': 'another', 'project_id': w.project, 'folder_project_id': w.project, 'hidden_at': None},
            {'folder_id': w.project, 'pdf_version_ids': [], 'chats': [{'id': chat['id'], 'through_sequence': 1}]})
        assert selected[0]['title'] == renamed['title']


def test_note_can_use_a_project_pdf_through_http_and_repeat_without_a_duplicate(workspace, monkeypatch):
    from test_chat01_postgres import body, conversation
    from test_draft01_postgres import http
    from test_perf02_r4_postgres import note_source
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    source, _ = note_source(w)
    chat = conversation(w)
    command = body(question='Подготовь записку по выбранному материалу проекта')
    command.update(skill_version_id='skill_analytical-note-v3', requested_result_type='analytical_note',
                   project_context=dict(folder_id=w.project, pdf_version_ids=[source['id']], chats=[]))
    path = f'/v1/projects/{w.project}/conversations/{chat["id"]}/messages'
    headers = {'Idempotency-Key': ident('key')}
    publish_fixture_worker(w)
    with http(w) as client:
        first = client.post(path, json=command, headers=headers)
        assert first.status_code == 202, first.text
        repeated = client.post(path, json=command, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == first.json()
        run = first.json()['run']
        assert [pin['source_version_id'] for pin in run['input']['source_versions']] == [source['id']]
        empty = dict(command, project_context=dict(folder_id=w.project, pdf_version_ids=[], chats=[]))
        assert client.post(path, json=empty, headers={'Idempotency-Key': ident('key')}).status_code == 422
        del empty['project_context']
        assert client.post(path, json=empty, headers={'Idempotency-Key': ident('key')}).status_code == 422
    with w.engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM aist.runs WHERE conversation_id=:id'),dict(id=chat['id'])).scalar_one() == 1
        assert c.execute(text('SELECT count(*) FROM aist.messages WHERE conversation_id=:id'),dict(id=chat['id'])).scalar_one() == 1
