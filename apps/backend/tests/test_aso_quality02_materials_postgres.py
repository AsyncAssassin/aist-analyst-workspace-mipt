"""Сохранение и правка заметки на собственном PDF и локальном ответе извлечения."""
from copy import deepcopy
from dataclasses import replace
import os

import pytest

from app.errors import ApiError
from app.facts import pin_of
from app.runs import execute
from app.service import ident
from app.versions import write
from test_d15_postgres import workspace
from test_fable_postgres import native
from test_note_path06_r1_postgres import ask, start
from test_note_path06_r2_postgres import codes, observation, packet, response, save

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def test_synthetic_save_read_and_mutations(workspace, native, tmp_path, monkeypatch):
    w = workspace
    source, run = start(w)
    payload = packet()
    margin = observation('23.8', 2022)
    margin.update(text='Рентабельность EBITDA за 2022 год: 23,8%.')
    margin['numeric'].update(metric='EBITDA margin', amount=dict(value='23.8', unit='percent', scale='1', currency=None))
    payload['visual_observations'][0] = margin
    native.queue = [dict(body=response(payload))]
    extracted = ask(w, run, source, tmp_path)
    assert extracted['status'] == 'complete' and len(native.requests) == 1
    assert all(item['source'] == pin_of(source) for item in extracted['visual_observations'])
    parent = save(w, run, extracted)
    version = parent['version']
    assert codes(parent, 'block_table') == {'unchecked_visual_observation'}

    def forbidden(*args, **kwargs):
        pytest.fail('Чтение и ручная правка не запускают извлечение или провайдер')

    for target in ('app.transport.Transport.perform', 'app.embeddings.Embeddings.vectors',
                   'app.openclaw_runtime.run', 'app.openclaw_runtime.obtain_facts',
                   'app.extraction.ingest', 'http.client.HTTPConnection.connect',
                   'http.client.HTTPSConnection.connect'):
        monkeypatch.setattr(target, forbidden)
    w.runs.settings = replace(w.settings, executor_mode='disabled', live_models_enabled=False, embeddings_enabled=False)

    def snapshots():
        with w.engine.connect() as connection:
            return {table: list(execute(connection, 'SELECT to_jsonb(t) FROM aist.' + table + ' t').scalars())
                    for table in ('facts', 'runs', 'model_calls', 'embedding_calls', 'version_acceptances')}

    before = snapshots()
    request = dict(expected_revision=version['revision'], parent_version_id=version['id'],
                   payload=deepcopy(version['payload']))
    key = ident('key')

    def save_manual(key, request):
        return write(w.runs, w.actor, w.session, w.project, version['artifact_id'], None, 'saveVersion', key, request)

    status, saved = save_manual(key, request)
    assert status == 201
    assert saved['version']['parent_version_id'] == version['id']
    assert saved['version']['revision'] == version['revision'] + 1
    assert saved['version']['payload'] == version['payload']
    # Независимая ручная правка сохраняет основания и разбор владельцев.
    expected_basis = dict(version['basis'])
    assert expected_basis['ownership_review']['status'] == 'not_established'
    assert saved['version']['basis'] == expected_basis
    assert saved['checks'][0]['profile_version'] == 'note-technical.v29'
    assert saved['checks'][1]['status'] == 'not_checked'
    assert saved['review']['acceptance'] is None
    assert save_manual(key, request) == (200, saved)
    assert w.runs.version(w.actor, w.project, saved['version']['id']) == saved
    with pytest.raises(ApiError) as foreign:
        w.runs.version('usr_foreign', w.project, saved['version']['id'])
    assert foreign.value.code == 'resource_not_found'

    head = saved
    for text in ('Сеть поставила оборудование 28 клиентам в 2024 году.',
                 'На первом этапе может заработать второй сервисный участок.',
                 'Мощность сборочной линии вырастет до 6 млн деталей в месяц.',
                 'Доля собственных поставок в выручке составила 8,2%.'):
        edited = deepcopy(version['payload'])
        blocks = edited['content']['sections'][0]['blocks']
        claim = next(block['claim'] for block in blocks if block['block_id'] == 'block_visual')
        claim['text'] = text
        status, head = save_manual(ident('key'), dict(request, expected_revision=head['version']['revision'], payload=edited))
        assert status == 201
        claim['origin'] = 'human_editorial'
        assert head['version']['payload'] == edited
        assert head['version']['basis'] == saved['version']['basis']
        assert head['checks'][0]['findings'] == saved['checks'][0]['findings']
        assert head['checks'][1]['status'] == 'not_checked' and head['review']['acceptance'] is None

    amount = margin['numeric']['amount']
    for index, change in [
        (0, dict(label='EBITDA')), (0, dict(label='Чистая маржа')),
        (0, dict(label='Скорректированная EBITDA')), (0, dict(label='Adjusted EBITDA margin')),
        (0, dict(period_label='2023')), (0, dict(amount=dict(amount, value='238'))),
        (0, dict(amount=dict(amount, unit='money', currency='RUB'))),
        (1, dict(period_label='2026')), (1, dict(period_label='6 мес. по 30.06.27')),
    ]:
        edited = deepcopy(version['payload'])
        blocks = edited['content']['sections'][0]['blocks']
        rows = next(block['rows'] for block in blocks if block['block_id'] == 'block_table')
        row_id = rows[index]['row_id']
        rows[index].update(change)
        status, head = save_manual(ident('key'), dict(request, expected_revision=head['version']['revision'], payload=edited))
        assert status == 201
        row_findings = {f['code'] for f in head['checks'][0]['findings'] if f.get('row_id') == row_id}
        assert {'visual_row_mismatch', 'unchecked_visual_observation'} <= row_findings
        assert head['version']['basis'] == expected_basis
        assert head['review']['acceptance'] is None

    assert w.runs.version(w.actor, w.project, version['id']) == parent
    assert snapshots() == before and len(native.requests) == 1
    with w.engine.connect() as connection:
        assert execute(connection, 'SELECT count(*) FROM aist.artifact_versions').scalar_one() == 15
