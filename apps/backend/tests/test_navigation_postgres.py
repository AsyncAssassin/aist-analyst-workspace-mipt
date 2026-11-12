"""HTTP cursor validation and page ordering against the workspace database."""

import os

import pytest
from sqlalchemy import event

from app.runs import execute
from test_chat01_postgres import conversation
from test_d15_postgres import workspace
from test_draft01_postgres import http

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def test_chat_cursor_validates_before_navigation_sql_and_keeps_order(workspace, monkeypatch):
    w = workspace
    monkeypatch.setenv('AIST_CHAT_UI_OWNERS', w.actor)
    chats = [conversation(w)['id'] for _ in range(3)]
    with w.engine.begin() as c:
        for chat, stamp in zip(chats, ['2026-11-12T20:59:59Z', '2026-11-12T21:00:00Z', '2026-11-12T21:00:00Z']):
            execute(c, 'UPDATE aist.conversations SET last_sent_at=:stamp WHERE id=:id', stamp=stamp, id=chat)

    with http(w) as client:
        statements = []

        @event.listens_for(client.app.state.engine, 'before_cursor_execute')
        def record(_connection, _cursor, statement, *_):
            if 'aist.conversations' in statement:
                statements.append(statement)

        url = '/v1/workspace/chats'
        for invalid in ('garbage', '2026-02-30T12:00:00Z', '2026-11-12T24:00:00Z', '2026-11-12T20:59:60Z'):
            response = client.get(url, params={'before_time': invalid, 'before_id': chats[0]})
            assert (response.status_code, response.json()['error']['code']) == (422, 'validation_error')
            assert response.json()['error']['message'] == 'Запрос не соответствует контракту.'
        response = client.get(url, params={'before_time': '2026-11-12T21:00:00Z'})
        assert (response.status_code, response.json()['error']['code']) == (409, 'cursor_invalid')
        assert statements == []

        newest, next_newest = sorted(chats[1:], reverse=True)
        response = client.get(url, params={'limit': 1})
        assert response.status_code == 200 and [x['id'] for x in response.json()['items']] == [newest]
        cursor = response.json()['next_cursor']
        assert cursor == dict(before_id=newest, before_time='2026-11-12T21:00:00Z')
        response = client.get(url, params={**cursor, 'before_time': '2026-11-13T00:00:00+03:00', 'limit': 2})
        assert response.status_code == 200
        assert [x['id'] for x in response.json()['items']] == [next_newest, chats[0]]
        assert response.json()['next_cursor'] is None
