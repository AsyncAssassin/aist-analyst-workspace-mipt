"""Связи секторов проходят хранение, чтение и локальный транспорт извлечения."""
from copy import deepcopy
import json
import os

import pytest
from sqlalchemy.exc import InternalError

from app.errors import ApiError
from app.extraction import source_pages
from app.openclaw_runtime import obtain_facts
from app.runs import execute
from app.service import ident
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_pdf_diagrams import sector_pdf

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


def upload(w, data):
    return w.service.write(w.actor, 'uploadSource', ident('key'),
        dict(title='Synthetic diagram', expected_revision=0), w.project, None, data, 'diagram.pdf')[1]['version']


def test_region_metadata_survives_storage_read_tool_transport_and_literal_ingest(workspace, provider, tmp_path):
    w, p = workspace, provider
    version = upload(w, sector_pdf(pages=3))
    page = w.service.page_or_file(w.actor, w.project, version['id'], page=3)
    region = page['diagram_regions'][0]
    lines = page['text'].splitlines()
    assert [lines[i-1] for i in region['label_lines']] == ['ABC']
    assert [lines[i-1] for i in region['content_lines']] == ['121', 'million']
    assert 'diagram_regions' not in w.service.page_or_file(w.actor, w.project, version['id'], page=1)
    send(w, conversation(w), body(sources=[version['id']], question='Financing'))
    run = w.runs.claim('diagram_links')
    read = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_read_source',
                      dict(source_version_id=version['id'], page=3))
    assert read['ok'] and read['result'] == page
    p.facts = [dict(metric=None, subject='Synthetic issuer', reporting_basis='Не указано',
        nature='source_statement', period=dict(kind='unknown', label='Не указан', start=None, end=None),
        amount=None, value_text=None, basis=None,
        evidence=[dict(page=3, line_start=line, line_end=line, area='Сектор диаграммы')
                  for line in [*region['label_lines'], *region['content_lines'], 1]])]
    result = obtain_facts(p.worker, run, dict(source_version_id=version['id'], question='Financing'), tmp_path, None)
    assert len(result['facts']) == 1
    evidence = result['facts'][0]['evidence']
    assert [e['quote'] for e in evidence[:4]] == ['ABC', '121', 'million', 'Synthetic issuer']
    assert all(e['quote'] in page['text'] for e in evidence)
    supplied = json.loads(p.sent[0]['messages'][1]['content'])['pages']
    assert supplied[2]['diagram_regions'] == page['diagram_regions']
    assert [line['text'] for line in supplied[2]['lines']] == lines
    assert all('diagram_regions' not in other for other in supplied[:2])
    with w.engine.connect() as c:
        stored = source_pages(w.runs, c, run, version['id'], diagrams=True)
        assert stored == {1: [], 2: [], 3: page['diagram_regions']}
    with pytest.raises(InternalError), w.engine.begin() as c:
        execute(c, "UPDATE aist.source_pages SET diagram_regions='[]' WHERE version_id=:v", v=version['id'])


def test_metadata_keeps_source_scope_pin_and_file_integrity_checks(workspace):
    w = workspace
    selected = upload(w, sector_pdf())
    other = upload(w, sector_pdf(amounts=('999', '888', '777')))
    send(w, conversation(w), body(sources=[selected['id']], question='Financing'))
    run = w.runs.claim('diagram_scope')
    with w.engine.connect() as c:
        with pytest.raises(ApiError):
            source_pages(w.runs, c, run, other['id'], diagrams=True)
        changed = deepcopy(run)
        changed['input']['source_versions'][0]['sha256'] = 'a' * 64
        with pytest.raises(ApiError):
            source_pages(w.runs, c, changed, selected['id'], diagrams=True)
    with pytest.raises(ApiError):
        w.service.page_or_file(ident('usr'), w.project, selected['id'], page=1)
    with pytest.raises(ApiError):
        w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_read_source',
                    dict(source_version_id=other['id'], page=1))
    # Повреждение копии в изолированном хранилище не даёт прочитать даже метаданные.
    with w.engine.connect() as c:
        version = w.service.scoped(c, w.actor, w.project, 'source_versions', selected['id'])
    w.service.storage.path(version['storage_name']).write_bytes(b'broken')
    with pytest.raises(ApiError), w.engine.begin() as c:
        source_pages(w.runs, c, run, selected['id'], diagrams=True)
