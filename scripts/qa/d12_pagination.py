"""D12-F01: реальные HTTP-страницы и PostgreSQL, worker должен быть остановлен.

Запуск после штатного D11 QA-import: --state содержит actor, project, artifact,
session. Создает одну ручную версию и четыре queued Export. Только идентификатор
и часы при создании подменяются для детерминированной проверки порядка.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import httpx
from app.config import Settings
from app.database import make_engine
from app.exports import Exports
from app.note_renderer import RENDERERS
from app.runs import Runs
from app.service import Service, ident
from app.storage import Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', default='http://backend:8000')
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    settings = Settings()
    engine = make_engine(settings)
    exports = Exports(Runs(Service(engine, Storage(settings.storage_root)), settings))
    client = httpx.Client(base_url=args.base_url, timeout=30,
                        headers={'Authorization': 'Bearer ' + state['session']['token']})
    project = f"/v1/projects/{state['project']}"
    artifact = project + f"/artifacts/{state['artifact']}"
    evidence = dict(passed=False, checks=[], requests=[], created=[])

    def flush():
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    def check(name):
        evidence['checks'].append(name)
        flush()
        print(name, flush=True)

    def get(path, params=None, status=200, code=None, actor=client):
        response = actor.get(path, params=params)
        value = response.json()
        evidence['requests'].append(dict(path=str(response.url), status=response.status_code, body=value))
        flush()
        assert response.status_code == status, (response.status_code, value)
        if code:
            assert value['error']['code'] == code, value
        return value

    def page(after=None, limit=None):
        params = {}
        if after is not None:
            params['after_id'] = after
        if limit is not None:
            params['limit'] = limit
        return get(url, params)

    def ids(value):
        return [item['id'] for item in value['items']]

    try:
        head = get(artifact)
        parent = get(project + '/artifact-versions/' + head['latest_version_id'])['version']
        payload = deepcopy(parent['payload'])
        payload['content']['audience'] = 'Регрессия порядка выпусков D12-F01'
        response = client.post(artifact + '/versions', headers={'Idempotency-Key': ident('key')},
                               json=dict(expected_revision=head['revision'], parent_version_id=parent['id'], payload=payload))
        assert response.status_code == 201, response.text
        version = response.json()['version']['id']
        evidence['version'] = version
        url = artifact + '/versions/' + version + '/exports'
        assert page() == dict(items=[], next_after_id=None)
        check('empty_page')
        suffix = ident('exp')[8:]
        stamp = datetime.now(timezone.utc).replace(microsecond=0)

        def create(prefix, when, fmt, theme):
            eid = 'exp_' + prefix * 4 + suffix
            body = dict(expected_revision=0, format=fmt, template_version='aist-note.v1',
                        renderer_version=RENDERERS[fmt],
                        settings=dict(locale='ru-RU', paper='A4', theme=theme, release_mode='draft'))
            with patch('app.exports.ident', return_value=eid), patch('app.exports.now', return_value=when):
                status, value = exports.request(state['actor'], state['session']['session_id'],
                    state['project'], state['artifact'], version, ident('key'), body)
            assert status == 202 and value['id'] == eid
            evidence['created'].append(value)
            flush()
            return eid

        first = create('f', stamp.isoformat().replace('+00:00', 'Z'), 'docx', 'standard')
        assert ids(page()) == [first]
        second = create('b', (stamp + timedelta(microseconds=100000)).isoformat().replace('+00:00', 'Z'), 'pdf', 'standard')
        assert second < first
        assert ids(page(first)) == [second]
        check('later_lower_id_is_returned_after_cursor')
        third = create('a', evidence['created'][1]['created_at'], 'docx', 'compact')
        assert ids(page()) == [first, third, second]
        check('chronological_order_and_equal_time_id_tiebreak')
        one = page(limit=2)
        assert ids(one) == [first, third] and one['next_after_id'] == third
        two = page(one['next_after_id'], 2)
        assert ids(two) == [second] and two['next_after_id'] is None
        assert page(second, 2) == dict(items=[], next_after_id=None)
        check('exclusive_cursor_page_boundary_and_terminal_page')
        fourth = create('0', (stamp + timedelta(seconds=1)).isoformat().replace('+00:00', 'Z'), 'pdf', 'compact')
        assert fourth < second and ids(page(second)) == [fourth]
        assert ids(page()) == [first, third, second, fourth]
        check('append_after_exhausted_page_does_not_rewrite_prefix')
        collected, cursor = [], None
        while True:
            current = page(cursor, 1)
            collected += ids(current)
            cursor = current['next_after_id']
            if cursor is None:
                break
        assert collected == [first, third, second, fourth]
        assert page(limit=4)['next_after_id'] is None
        check('limit_one_and_exact_limit_without_duplicates')
        get(url, {'after_id': 'exp_' + 'e' * 32}, 409, 'cursor_invalid')
        get(artifact + '/versions/' + parent['id'] + '/exports', {'after_id': first}, 409, 'cursor_invalid')
        check('absent_and_other_version_cursors_rejected')
        _, other = exports.service.write(state['actor'], 'createProject', ident('key'), {'name': 'D12-F01 scope'})
        get(f"/v1/projects/{other['id']}/artifacts/{state['artifact']}/versions/{version}/exports",
            {'after_id': first}, 404, 'resource_not_found')
        check('foreign_project_cannot_read_version_or_cursor')
        user = json.loads(subprocess.check_output([sys.executable, '-m', 'app.cli', 'create-user', '--label', 'D12-F01 owner']))
        session = json.loads(subprocess.check_output([sys.executable, '-m', 'app.cli', 'issue-session', '--user-id', user['user_id']]))
        try:
            with httpx.Client(base_url=args.base_url, timeout=30, headers={'Authorization': 'Bearer ' + session['token']}) as foreign:
                get(url, {'after_id': first}, 404, 'resource_not_found', foreign)
        finally:
            subprocess.check_output([sys.executable, '-m', 'app.cli', 'revoke-session', '--session-id', session['session_id']])
        check('foreign_owner_cannot_read_version_or_cursor')
        evidence['passed'] = True
    finally:
        flush()
        client.close()
        engine.dispose()


if __name__ == '__main__':
    main()
