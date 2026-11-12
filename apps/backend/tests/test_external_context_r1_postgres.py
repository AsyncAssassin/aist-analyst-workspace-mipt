"""Исправленный сбор через настоящий внутренний HTTP и сохранение точной версии."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from app import note_external
from app.runs import execute
from app.errors import ApiError
from test_d15_postgres import workspace
from test_aso_quality02_phase3_postgres import accept, deal_note, start
from test_draft01_r1_postgres import save
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def test_http_discovery_is_saved_once_and_competitor_is_not_company(workspace, monkeypatch):
    w = workspace
    w.runs.policy['run']['max_model_calls'] = 16
    source, _ = note_source(w)
    run = start(w, source)
    queries = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
            queries.append(q)
            if urlsplit(self.path).path == '/web-search':
                args = json.loads(q['arguments'])
                assert args['search_target'] == 'Маяк'
                result = dict(results=[dict(url='https://company.example/report', title='Отчет Маяка', snippet='Поисковая выдержка')], bytes_read=100)
            else:
                assert q['url'] == 'https://company.example/report'
                result = dict(url=q['url'], title='Отчет Маяка', published='2026-01-01',
                    quote='Маяк раскрыл выручку группы за 2025 год: 20 млн рублей. Данные приведены в годовом отчете группы.',
                    publisher='company.example', page_sha256='a'*64, retrieved_at='2026-11-08T00:00:00+00:00',
                    bytes_read=500, truncated=False)
            data = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    monkeypatch.setenv('AIST_NEWS_ORIGIN', 'http://127.0.0.1:' + str(server.server_port))
    monkeypatch.setattr(note_external, 'identity', lambda _: dict(company='Орбита', inn='', sector='retail', domain=''))
    try:
        note_external.prepare_all(w.runs, run)
        assert not queries
        query = dict(objective='Выручка конкурента', queries=['Маяк выручка годовой отчет'],
                     search_target='Маяк', search_role='competitor', topics=['competitor_revenue'])
        def research(current, task, research_id):
            found = w.runs.tool(run['id'], run['epoch'], 'search', 'aist_web_search', query, research_id=research_id)
            args = dict(search_id=found['result']['search_id'], url='https://company.example/report')
            read = w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', args, research_id=research_id)
            assert read['ok']
            assert w.runs.tool(run['id'], run['epoch'], 'fetch', 'aist_web_fetch', args, research_id=research_id) == read
            return dict(status='collected', summary='Сохранена отчетность конкурента.')
        w.runs.tool(run['id'], run['epoch'], 'research', 'aist_research',
                    dict(company='Орбита', objective='Выручка конкурента'), research=research)
        with w.engine.connect() as c:
            snapshot = execute(c, 'SELECT external_context FROM aist.runs WHERE id=:r', r=run['id']).scalar_one()
        note_external.prepare_all(w.runs, run)
        assert len(queries) == 2
        assert snapshot['topics']['competitor_revenue'] == 'collected'
        evidence = next(item for item in snapshot['items'] if item['search_target'] == 'Маяк')
        content = deal_note(source)
        content['sections'][1]['blocks'].append(dict(kind='paragraph', block_id='competitor_report', claim=dict(
            origin='external_context', text=evidence['quote'], external_evidence_ids=[evidence['evidence_id']], fact_refs=[], calculation_ids=[])))
        saved = save(w, run, content)
        snapshot['research_closed'] = True
        assert saved['version']['basis']['external_context'] == snapshot
        accept(w, saved)
        with pytest.raises(ApiError) as refused:
            note_external.prepare_all(w.runs, run)
        assert refused.value.code == 'run_terminal'
        assert len(queries) == 2
        with w.engine.connect() as c:
            assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE project_id=:p', p=w.project).scalar_one() == 0
        assert note_external.saved_context({}, saved['version']) == snapshot
    finally:
        server.shutdown(); thread.join(); server.server_close()
