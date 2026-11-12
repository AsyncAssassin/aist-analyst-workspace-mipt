"""Новые News Run сохраняются с основаниями, наследуются без переписывания истории."""
from copy import deepcopy
import os

import pytest

from app import note_news
from app.errors import ApiError
from app.live_profile import FABLE_VISUAL_ID
from app.model_save import save_operation
from app.runs import execute, json_value
from app.service import ident
from app.versions import persist
from test_d15_postgres import workspace
from test_draft01_postgres import http
from test_draft01_r1_postgres import edit
from test_aso_quality02_phase3_postgres import start, deal_note, reply_text
from test_external_context_postgres import seed_context
from test_news_events import evidence_context, event, through_boundary
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только собственная PostgreSQL')

UNKNOWN_OWNERS = dict(status='not_established', reason='Материалы о новостях не раскрывают владельцев.', excluded_sources=[])


def prepared(w):
    source, _ = note_source(w)
    run = start(w, source)
    seed_context(w, run, evidence_context())
    return source, run


@pytest.mark.parametrize('profile', ['model_novita-mimo-v1', FABLE_VISUAL_ID])
def test_boundary_optional_fields_reach_saved_version_and_message(workspace, tmp_path, profile):
    w = workspace
    source, run = prepared(w)
    protocol = dict(run, input=dict(run['input'], model_profile_version_id=profile, budget={'max_model_calls': 16}))
    command = dict(content=deal_note(source), questions=['Какие договорные ограничения не раскрыты?'], news_events=[event()],
                   ownership_review=UNKNOWN_OWNERS)
    args = through_boundary(protocol, command, tmp_path)
    result = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert result['ok'], result
    saved = result['result']
    item, = saved['version']['basis']['news_run']['items']
    assert item['published'] == '2025-08-01' and item['metadata_published'] == '2026-02-24'
    assert saved['version']['basis']['external_context'] == run['external_context']
    assert command['questions'][0] in reply_text(w, run)
    assert not any(f['code'] == 'news_text_changed' for c in saved['checks'] for f in c['findings'])
    repeat = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert repeat['result'] == saved
    assert reply_text(w, run).count(command['questions'][0]) == 1
    assert saved['review']['acceptance'] is None
    with w.engine.connect() as c:
        assert execute(c, 'SELECT count(*) FROM aist.model_calls WHERE project_id=:p', p=w.project).scalar_one() == 0
        assert execute(c, 'SELECT count(*) FROM aist.usage_entries WHERE run_id=:r', r=run['id']).scalar_one() == 0


def test_legacy_news_replaced_then_neutral_edit_preserves_new_basis(workspace):
    w = workspace
    source, run = prepared(w)
    original_context = deepcopy(run['external_context'])
    item = original_context['items'][0]
    legacy = dict(status='found', company='Орбита', as_of='2026-11-09', retrieved_at=item['retrieved_at'],
        items=[dict(quote=item['title'], url=item['url'], publisher=item['publisher'], published=item['published'],
                    page_sha256=item['page_sha256'], external_evidence_id=item['evidence_id'])])
    with w.engine.begin() as c:
        execute(c,'UPDATE aist.runs SET news_run=CAST(:news AS jsonb) WHERE id=:id', news=json_value(legacy), id=run['id'])
    args = save_operation(run,dict(content=deal_note(source), ownership_review=UNKNOWN_OWNERS))
    first = w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',args)['result']
    parent = first['version']
    assert parent['basis']['news_run'] == legacy
    before = deepcopy(first)
    selected = [event(), event(description='Открыто новое предприятие.', event_date='2025-06-12',
        date_quote='открытие нового предприятия 12 июня 2025 года.'),
        event(description='Условия программы раскрыты без точной даты.', published=None, event_date=None, date_quote='')]
    context = dict(id=None,owner_id=w.actor,project_id=w.project,input=dict(source_versions=parent['provenance']['source_versions']))
    with w.engine.begin() as c:
        second = persist(w.runs,c,context,parent['payload'],parent['artifact_id'],parent['id'],parent['revision'],human=True,news_events=selected)
    news = second['version']['basis']['news_run']
    blocks = [b for s in second['version']['payload']['content']['sections'] for b in s['blocks'] if b.get('claim',{}).get('origin')=='public_news']
    assert len(blocks) == len({b['block_id'] for b in blocks}) == 3
    assert 'Об отчете' not in '\n'.join(b['claim']['text'] for b in blocks)
    assert {b['claim']['text'] for b in blocks} == {note_news.news_text(i) for i in news['items']}
    assert second['version']['basis']['external_context'] == original_context
    assert second['version']['provenance']['origin'] == 'human_edit'
    assert not any(f['code'] == 'news_text_changed' for c in second['checks'] for f in c['findings'])
    content = deepcopy(second['version']['payload']['content'])
    content['title'] += ': редакция'
    with http(w) as client:
        third = edit(w,client,second,content,'human',[source])
    assert third['version']['basis']['news_run'] == news
    assert third['version']['basis']['external_context'] == original_context
    assert w.runs.version(w.actor,w.project,parent['id']) == before
    with w.engine.begin() as c:
        cleared = persist(w.runs,c,context,third['version']['payload'],parent['artifact_id'],third['version']['id'],third['version']['revision'],human=True,news_events=[])
    assert cleared['version']['basis']['news_run']['items'] == []
    assert all(b.get('claim',{}).get('origin')!='public_news' for s in cleared['version']['payload']['content']['sections'] for b in s['blocks'])
    assert w.runs.version(w.actor,w.project,second['version']['id'])['version']['basis']['news_run'] == news
    assert all(v['review']['acceptance'] is None for v in (second,third,cleared))


def test_editorial_new_source_is_scoped_and_old_source_cannot_be_rewritten(workspace):
    w = workspace
    source, run = prepared(w)
    first = w.runs.tool(run['id'],run['epoch'],ident('tool'),'aist_save_artifact',
        save_operation(run,dict(content=deal_note(source), ownership_review=UNKNOWN_OWNERS)))['result']
    parent = first['version']
    context = dict(id=None,owner_id=w.actor,project_id=w.project,input=dict(source_versions=parent['provenance']['source_versions']))
    fresh = deepcopy(parent['basis']['external_context'])
    fresh['items'].append(dict(fresh['items'][0], evidence_id='ext_new',url='https://company.example/new',
        title='Новая публикация',published=None,page_sha256='b'*64,quote='Дата публикации не указана. Компания описала ассортимент и новые клиентские группы в отдельном материале.'))
    payload = deepcopy(parent['payload'])
    payload['content']['sections'][1]['blocks'].append(dict(kind='paragraph',block_id='new_context',claim=dict(
        text='Компания описала ассортимент и клиентские группы.',origin='external_context',external_evidence_ids=['ext_new'],fact_refs=[],calculation_ids=[])))
    with w.engine.begin() as c:
        with pytest.raises(ApiError):
            persist(w.runs,c,context,payload,parent['artifact_id'],parent['id'],parent['revision'],human=True)
    with w.engine.begin() as c:
        saved = persist(w.runs,c,context,payload,parent['artifact_id'],parent['id'],parent['revision'],human=True,
            editorial_external_context=fresh,news_events=[])
    assert saved['version']['basis']['external_context'] == fresh
    assert w.runs.version(w.actor,w.project,parent['id']) == first
    forged = deepcopy(fresh);forged['items'][0]['quote'] += ' Подмена прежнего основания.'
    with w.engine.begin() as c:
        with pytest.raises(ApiError):
            persist(w.runs,c,context,saved['version']['payload'],parent['artifact_id'],saved['version']['id'],saved['version']['revision'],human=True,editorial_external_context=forged)
    assert w.runs.version(w.actor,w.project,saved['version']['id']) == saved
    content = deepcopy(saved['version']['payload']['content'])
    content['title'] += ': новая редакция'
    with http(w) as client:
        child = edit(w,client,saved,content,'human',[source])
    assert child['version']['basis']['external_context'] == fresh
    assert child['version']['basis']['news_run'] == saved['version']['basis']['news_run']


def test_legacy_news_survives_new_renderer_and_ordinary_edit(workspace):
    w = workspace
    source, run = prepared(w)
    with w.monkeypatch.context() as patch:
        patch.setattr(note_news, 'news_text', note_news.legacy_news_text)
        first = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact',
            save_operation(run, dict(content=deal_note(source), news_events=[event()], ownership_review=UNKNOWN_OWNERS)))['result']
    parent = deepcopy(first)
    content = deepcopy(first['version']['payload']['content'])
    content['title'] += ': новая редакция'
    with http(w) as client:
        child = edit(w, client, first, content, 'human', [source])
    assert not any(f['code'] == 'news_text_changed' for check in child['checks'] for f in check['findings'])
    assert w.runs.version(w.actor, w.project, first['version']['id']) == parent
    from app.aso import presentation
    blocks = [b for section in presentation(child['version'], child['checks'])['sections'] for b in section['blocks']]
    publication, = [block for block in blocks if block.get('publication')]
    assert publication['rows'][0][0]['text'] == note_news.news_text(publication['publication'])
    assert publication['publication']['url'] == evidence_context()['items'][0]['url']
    assert child['review']['acceptance'] is None
