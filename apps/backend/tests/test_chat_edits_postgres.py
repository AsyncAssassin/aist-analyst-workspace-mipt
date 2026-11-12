"""Version and decision invariants for a proposal in a real chat."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import os

import pytest
from app import edit_proposals as edits
from app.errors import ApiError
from app.runs import execute
from app.service import ident
from test_d15_postgres import workspace
from test_perf02_r4_postgres import corrected_version
from test_chat01_postgres import conversation

pytestmark = pytest.mark.skipif(
    os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Isolated PostgreSQL required'
)


def test_preview_apply_reject_and_conflicting_sessions(workspace, monkeypatch):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    base = corrected_version(w)
    chat = w.runs.get(w.actor, w.project, base['provenance']['run_id'])['conversation_id']
    scope = (w.runs, w.actor, w.session, w.project, chat)
    body = dict(version_id=base['id'], text='Замени «943» на «944»')
    key = ident('key')

    def preview(key):
        return edits.prepare(*scope, key, body)['proposal']

    p = preview(key)
    assert preview(key) == p and p['before'] == '943' and p['after'] == '944'
    assert edits.decide(*scope, p['id'], ident('key'), 'reject')['applied_version_id'] is None
    with pytest.raises(ApiError):
        edits.decide(*scope, p['id'], ident('key'), 'apply')
    second = preview(ident('key'))
    old = preview(ident('key'))
    decision_key = ident('key')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: edits.decide(*scope, second['id'], decision_key, 'apply'), range(2)))
    assert results[0] == results[1]
    saved = w.runs.version(w.actor, w.project, results[0]['applied_version_id'])
    assert saved['version']['parent_version_id'] == base['id']
    assert saved['version']['basis'] == base['basis']
    assert any(f['code'] == 'row_basis_mismatch' for q in saved['checks'] for f in q['findings'])
    with pytest.raises(ApiError, match='revision_conflict'):
        edits.decide(*scope, old['id'], ident('key'), 'apply')
    assert next(
        i for i in edits.listing(w.runs, w.actor, w.project, chat, {})['items'] if i['id'] == old['id']
    )['stale']
    with pytest.raises(ApiError):
        edits.prepare(w.runs, w.actor, w.session, w.project, conversation(w)['id'], ident('key'), body)
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 2
        assert execute(c, 'SELECT count(*) FROM aist.model_calls').scalar_one() == 0


def test_rewrite_target_and_input_are_limited_and_invalid_responses_fail_closed(
    workspace, monkeypatch, tmp_path
):
    from test_chat01_postgres import body, send

    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    base = corrected_version(w)
    chat = w.runs.get(w.actor, w.project, base['provenance']['run_id'])['conversation_id']
    conv = w.service.get(w.actor, w.project, 'conversations', chat)
    scope = (w.runs, w.actor, w.session, w.project, chat)
    command = 'Сократи первый абзац'
    choice = edits.prepare(*scope, ident('key'), dict(version_id=base['id'], text=command))
    request = dict(
        body(revision=conv['revision'], question=command),
        edit_request={k: choice[k] for k in ('version_id', 'target')},
    )
    key = ident('key')
    _, sent = send(w, conv, request, key)
    assert send(w, conv, request, key)[1] == sent
    run = w.runs.claim('qa')
    assert run['id'] == sent['run']['id'] and run['input']['budget']['max_model_calls'] == 1
    assert (
        run['input']['budget']['max_duration_seconds'] <= 300
        and run['configuration']['conversation_context'] == {}
    )
    for response in [
        dict(stop_reason='max_tokens', content=[]),
        dict(stop_reason='end_turn', content=[dict(type='tool_use')]),
        dict(stop_reason='end_turn', content=[dict(type='text', text='')]),
    ]:
        with pytest.raises(ApiError):
            edits.rewritten_text(response, True, 'Исходный текст')
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.edit_proposals').scalar_one() == 0

    def local_reply(*args, **kwargs):
        assert not kwargs['request'].get('tools')
        assert len(kwargs['request']['messages']) == 2
        return 'synthetic', dict(choices=[dict(finish_reason='stop', message=dict(content='Краткий текст.'))])

    edits.run_rewrite(
        SimpleNamespace(runs=w.runs, transport=SimpleNamespace(perform=local_reply)), run, tmp_path
    )
    completed = w.runs.get(w.actor, w.project, run['id'])
    assert completed['state'] == 'completed' and completed['result_version_id'] is None
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1
    saved = edits.listing(w.runs, w.actor, w.project, chat, {})['items'][0]
    assert saved['after'] == 'Краткий текст.' and saved['run_id'] == run['id'] and saved['state'] == 'pending'


def test_unique_occurrence_and_formatting():
    b = {
        'block_id': 'block_a',
        'kind': 'paragraph',
        'claim': {'text': 'Тест. Тест.'},
        'marks': [dict(start=0, end=4, style='bold')],
    }
    v = dict(
        id='av_a', payload={'content': {'sections': [dict(section_id='sec_a', title='Бизнес', blocks=[b])]}}
    )
    assert edits.plan(v, 'Замени «Тест» на «Итог»')['kind'] == 'hint'
    b['claim']['text'] = 'Банк. Основной текст.'
    edits.reshape_marks(b, 'Организация. Основной текст.', 'Замени «Банк» на «Организация»')
    assert b['marks'] == [dict(start=0, end=11, style='bold')]


def test_apply_and_reject_race_has_only_one_terminal_decision(workspace, monkeypatch):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    base = corrected_version(w)
    chat = w.runs.get(w.actor, w.project, base['provenance']['run_id'])['conversation_id']
    scope = (w.runs, w.actor, w.session, w.project, chat)
    proposal = edits.prepare(*scope, ident('key'), dict(version_id=base['id'], text='Замени 943 на 945'))[
        'proposal'
    ]

    def decision(action):
        try:
            return edits.decide(*scope, proposal['id'], ident('key'), action)['state']
        except ApiError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decision, ['apply', 'reject']))
    assert results.count('revision_conflict') == 1
    final = edits.listing(w.runs, w.actor, w.project, chat, {})['items'][0]
    assert final['state'] in results
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 1 + (
            final['state'] == 'applied'
        )


def test_decision_http_receipts_survive_rollback_lost_reply_and_concurrency(workspace, monkeypatch):
    from app.service import Service
    from test_draft01_postgres import http

    w = workspace
    foreign = ident('usr')
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', ','.join([w.actor, foreign]))
    base = corrected_version(w)
    chat = w.runs.get(w.actor, w.project, base['provenance']['run_id'])['conversation_id']
    wrong_chat = conversation(w)['id']
    _, wrong_project = w.service.write(w.actor, 'createProject', ident('key'), {'name': 'Другой проект'})
    root = f'/v1/projects/{w.project}/conversations/{chat}/edit-proposals'
    scope = (w.runs, w.actor, w.session, w.project, chat)
    body = dict(version_id=base['id'], text='Замени «943» на «944»')
    first, second, stale = (edits.prepare(*scope, ident('key'), body)['proposal']['id'] for _ in range(3))
    decision_key = ident('key')

    def counts():
        with w.engine.connect() as c:
            versions = execute(c, 'SELECT count(*) FROM aist.artifact_versions').scalar_one()
            receipts = execute(c, """SELECT target,key FROM aist.receipts WHERE operation='decideEdit'
                AND actor_id=:a AND project_scope=:p AND proposal_id=target""",
                a=w.actor, p=w.project).all()
        return versions, set(receipts)

    with http(w) as client:
        def decide(proposal, action='apply', key=decision_key, url=root):
            return client.post(f'{url}/{proposal}', json={'decision': action}, headers={'Idempotency-Key': key})

        def failure(response, status, code):
            assert (response.status_code, response.json()['error']['code']) == (status, code)

        writer = Service.write_receipt

        def fail_after_receipt(*args, **kwargs):
            writer(*args, **kwargs)
            raise ApiError('temporarily_unavailable')

        with monkeypatch.context() as patch:
            patch.setattr(Service, 'write_receipt', fail_after_receipt)
            failure(decide(first), 503, 'temporarily_unavailable')
        assert counts() == (1, set())
        assert edits.listing(w.runs, w.actor, w.project, chat, {})['items'][0]['state'] == 'pending'

        dialect = client.app.state.engine.dialect
        commit = dialect.do_commit

        def commit_then_lose_reply(connection):
            commit(connection)
            raise RuntimeError('synthetic lost commit acknowledgement')

        with monkeypatch.context() as patch:
            patch.setattr(dialect, 'do_commit', commit_then_lose_reply)
            failure(decide(first), 503, 'temporarily_unavailable')
        assert counts() == (2, {(first, decision_key)})

        with ThreadPoolExecutor(max_workers=2) as pool:
            repeated = list(pool.map(lambda _: decide(first), range(2)))
        assert [reply.status_code for reply in repeated] == [200, 200]
        result = repeated[0].json()
        assert repeated[1].json() == result and result['state'] == 'applied'
        saved = w.runs.version(w.actor, w.project, result['applied_version_id'])['version']
        assert saved['parent_version_id'] == base['id'] and saved['basis'] == base['basis']
        failure(decide(first, 'reject'), 409, 'idempotency_conflict')
        new_key = ident('key')
        assert decide(first, key=new_key).json() == result
        failure(decide(first, 'reject', key=ident('key')), 409, 'revision_conflict')
        failure(decide(stale), 409, 'revision_conflict')

        rejected = decide(second, 'reject')
        assert rejected.status_code == 200 and rejected.json()['applied_version_id'] is None
        assert decide(second, 'reject').json() == rejected.json()
        failure(decide(second), 409, 'idempotency_conflict')
        failure(decide(first, url=root.replace(chat, wrong_chat)), 404, 'resource_not_found')
        failure(decide(first, url=root.replace(w.project, wrong_project['id'])), 404, 'resource_not_found')

        other_session = ident('ses')
        with w.engine.begin() as c:
            execute(c, "INSERT INTO aist.users(id,label) VALUES (:a,'Synthetic second owner')", a=foreign)
            execute(c, """INSERT INTO aist.sessions(id,user_id,token_hash,expires_at)
                VALUES (:s,:a,'other-owner-synthetic',clock_timestamp()+interval '1 hour')""",
                s=other_session, a=foreign)
        with http(SimpleNamespace(engine=w.engine, settings=w.settings, session=other_session)) as other:
            failure(other.post(f'{root}/{first}', json={'decision': 'apply'},
                               headers={'Idempotency-Key': decision_key}), 404, 'resource_not_found')
        with w.engine.begin() as c:
            execute(c, 'UPDATE aist.sessions SET revoked_at=clock_timestamp() WHERE id=:s', s=w.session)
        failure(decide(first), 401, 'unauthenticated')

    assert counts() == (2, {(first, decision_key), (first, new_key), (second, decision_key)})
