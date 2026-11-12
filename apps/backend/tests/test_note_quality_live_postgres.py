"""Первая живая записка: компактные ряды, новая квитанция и вопросы вместе с результатом."""
from copy import deepcopy
import os

import pytest
from test_perf02_r4_postgres import model_save_args

from app import extraction
from app.model_save import save_operation
from app.normalization import checked, fingerprint
from app.runs import execute
from app.service import ident
from app.tools import TEAM_REQUEST
from test_d15_postgres import workspace
from test_fable_postgres import native, start_fable
from test_flow01_postgres import current
from test_note_path06_r1_postgres import start, ask, own_items
from test_note_path06_r2_postgres import observation, response, packet, save
from test_aso_quality02_phase3_postgres import start as start_note, deal_note, reply_text
from test_perf02_r4_postgres import note_source

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def test_compact_financial_series_and_published_growth_survive_mixed_ingest(workspace, native, tmp_path):
    w = workspace
    source, run = start(w)
    points = []
    for metric, unit in [('Выручка', 'money'), ('EBITDA', 'money'), ('Рентабельность EBITDA', 'percent'),
                         ('Чистый долг', 'money'), ('Чистый долг/EBITDA', 'ratio'),
                         ('Оборотный капитал', 'money'), ('Изменение оборотного капитала', 'money')]:
        for year in range(2018, 2023):
            point = observation(str(year - 2020), year)
            point['numeric']['metric'] = metric
            point['numeric']['amount'].update(unit=unit, scale='1000000' if unit == 'money' else '1',
                                              currency='RUB' if unit == 'money' else None)
            point['text'] = f'{metric}: {year - 2020}, {year}, опубликованное значение.'
            points.append(point)
    assert len(points) == 35
    published = observation('7.3', 2022)
    published['text'] = 'CAGR выручки за 2018-2022: 7,3%, оценка компании.'
    published['numeric'].update(metric='CAGR выручки',
        period=dict(kind='interval', label='2018-2022', start='2018-01-01', end='2022-12-31'),
        amount=dict(value='7.3', unit='percent', scale='1', currency=None))
    payload = dict(facts=own_items(), visual_observations=[*points, published])
    before = deepcopy(payload)
    native.queue = [dict(body=response(payload))]
    result = ask(w, run, source, tmp_path)
    assert payload == before and result['status'] == 'complete'
    assert len(result['facts']) == 2 and len(result['visual_observations']) == 36
    assert [item['numeric'] for item in result['visual_observations']] == [item['numeric'] for item in payload['visual_observations']]
    assert all(item['verification_status'] == 'unchecked' for item in result['visual_observations'])
    assert result['visual_observations'][-1]['text'] == published['text']
    assert ask(w, current(w, run), source, tmp_path / 'repeat') == result
    assert len(native.requests) == 1


def test_late_classified_skips_keep_indices_and_following_data(workspace, native, tmp_path):
    from app.errors import ApiError
    from jsonschema import Draft202012Validator
    w = workspace
    source, old_run = start(w)
    native.queue = [dict(body=response(packet()))]
    old_result = ask(w, old_run, source, tmp_path / 'old')
    old_view = save(w, current(w, old_run), old_result)
    old_checkpoint = deepcopy(current(w, old_run)['checkpoint'])
    _, _, run = start_fable(w, full=True, source=source, profile_id=old_run['input']['model_profile_version_id'])
    good, qualitative = own_items()
    items = [deepcopy(good) for _ in range(64)]
    for index in (12, 63):
        items[index]['amount']['value'] = '7.26'
    items[13] = qualitative
    payload = dict(facts=items, visual_observations=[observation('7.25', 2021)])
    original = deepcopy(payload)
    native.queue = [dict(body=response(payload))]
    result = ask(w, run, source, tmp_path / 'new', question='Полный ряд и исключения')
    assert checked('GetFactsResult', result) == result and payload == original
    assert result['status'] == 'partial' and len(result['facts']) == 2
    assert len(result['visual_observations']) == 1
    assert result['visual_observations'][0]['verification_status'] == 'unchecked'
    assert [item['candidate'] for item in result['skipped']] == [13, 64]
    for index, item in zip((13, 64), result['skipped']):
        candidate = item['review_candidate']
        assert item['code'] == 'number_mismatch'
        assert candidate['candidate_index'] == index and candidate['status'] == 'not_admitted'
        assert candidate['input'] == original['facts'][index - 1]
    assert ask(w, current(w, run), source, tmp_path / 'repeat', question='Полный ряд и исключения') == result
    assert len(native.requests) == 2
    assert current(w, old_run)['checkpoint'] == old_checkpoint
    assert w.runs.version(w.actor, w.project, old_view['version']['id']) == old_view
    too_many = dict(payload, facts=[*items, deepcopy(good)])
    assert not Draft202012Validator(extraction.extraction_schema()).is_valid(too_many)
    with pytest.raises(ApiError):
        checked('ReviewCandidate', dict(result['skipped'][-1]['review_candidate'], candidate_index=65))
    with pytest.raises(ApiError):
        checked('ExtractionSkip', dict(result['skipped'][-1], candidate=65))


def test_new_extraction_does_not_reuse_v35_or_rewrite_its_saved_basis(workspace, native, tmp_path, monkeypatch):
    w = workspace
    with monkeypatch.context() as previous:
        previous.setattr(extraction, 'VERSION', 'pdf-lines.v35')
        previous.setattr(extraction, 'MAX_CANDIDATES', 18)
        source, old_run = start(w)
        native.queue = [dict(body=response(packet()))]
        old_result = ask(w, old_run, source, tmp_path / 'v35')
        old_view = save(w, current(w, old_run), old_result)
        old_checkpoint = deepcopy(current(w, old_run)['checkpoint'])
    _, _, new_run = start_fable(w, full=True, source=source, profile_id=old_run['input']['model_profile_version_id'])
    native.queue = [dict(body=response(packet()))]
    new_result = ask(w, new_run, source, tmp_path / 'v36')
    assert len(native.requests) == 2
    old_entry = extraction.recorded_extractions(dict(checkpoint=old_checkpoint))[0]
    new_entry = extraction.recorded_extractions(current(w, new_run))[0]
    assert old_entry['version'] == 'pdf-lines.v35' and new_entry['version'] == extraction.VERSION
    assert old_entry['schema_sha256'] != new_entry['schema_sha256']
    assert old_entry['request_key'] != new_entry['request_key']
    assert old_result['facts'][0]['fact_id'] != new_result['facts'][0]['fact_id']
    assert current(w, old_run)['checkpoint'] == old_checkpoint
    assert w.runs.version(w.actor, w.project, old_view['version']['id']) == old_view


def test_v37_does_not_reuse_v36_completed_extraction(workspace, native, tmp_path, monkeypatch):
    w = workspace
    with monkeypatch.context() as previous:
        previous.setattr(extraction, 'VERSION', 'pdf-lines.v36')
        source, old_run = start(w)
        native.queue = [dict(body=response(packet()))]
        old_result = ask(w, old_run, source, tmp_path / 'v36')
        old_view = save(w, current(w, old_run), old_result)
        old_checkpoint = deepcopy(current(w, old_run)['checkpoint'])
    _, _, new_run = start_fable(w, full=True, source=source, profile_id=old_run['input']['model_profile_version_id'])
    native.queue = [dict(body=response(packet()))]
    new_result = ask(w, new_run, source, tmp_path / 'v37')
    assert len(native.requests) == 2
    assert new_result['facts'][0]['fact_id'] != old_result['facts'][0]['fact_id']
    assert current(w, old_run)['checkpoint'] == old_checkpoint
    assert w.runs.version(w.actor, w.project, old_view['version']['id']) == old_view


def test_trillion_fact_visual_note_and_exports_preserve_source_amount(workspace, native, tmp_path):
    import io
    from docx import Document
    from pypdf import PdfReader
    from app.exports import Exports
    from app.note_renderer import renderer_versions
    from app.runs import json_value
    from test_note_path06_r1 import prose
    from test_perf02_r1_postgres import upload
    from test_perf02_r4_postgres import save_args
    from app.live_profile import FABLE_VISUAL_ID
    w = workspace
    lines = ['Север: выручка составила 2,2 трлн руб. в 2024 году.']
    source = upload(w, 'Сумма в исходном масштабе', lines)
    _, _, run = start_fable(w, full=True, source=source, profile_id=FABLE_VISUAL_ID)
    fact = prose(lines, value='2.2', raw='2,2', unit='трлн руб.', year=2024)
    visual = observation('2.2', 2028, forecast=True)
    visual['text'] = 'Целевая выручка: 2,2 трлн руб., 2028 год.'
    visual['numeric']['metric'] = 'Целевая выручка'
    visual['numeric']['amount']['scale'] = '1000000000000'
    native.queue = [dict(body=response(dict(facts=[fact], visual_observations=[visual])))]
    result = ask(w, run, source, tmp_path)
    assert len(result['facts']) == len(result['visual_observations']) == 1
    money = result['facts'][0]
    image = result['visual_observations'][0]
    amount = dict(value='2.2', unit='money', scale='1000000000000', currency='RUB')
    assert money['amount'] == image['numeric']['amount'] == amount
    assert image['verification_status'] == 'unchecked'
    assert ask(w, current(w, run), source, tmp_path / 'repeat') == result
    assert len(native.requests) == 1
    rows = [dict(row_id='row_actual', label='Выручка', period_label='2024', amount=amount,
                 fact_refs=[dict(source_version_id=source['id'], fact_id=money['fact_id'])], calculation_ids=[]),
            dict(row_id='row_target', label='Целевая выручка', period_label='2028', amount=amount,
                 fact_refs=[], calculation_ids=[], visual_observation_ids=[image['observation_id']])]
    content = dict(title='Север', audience='Аналитик', sections=[dict(section_id='sec_finance', title='Финансовые показатели',
        blocks=[dict(block_id='block_amounts', kind='table', caption='Опубликованные значения', rows=rows)])])
    args = save_args(content)
    saved = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert saved['ok'], saved
    view = saved['result']
    assert w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)['result'] == view
    assert w.runs.version(w.actor, w.project, view['version']['id']) == view
    assert view['review']['acceptance'] is None
    target = tmp_path / 'trillion'; target.mkdir()
    (target / 'version.json').write_text(json_value(view))
    exports = Exports(w.runs)
    for fmt in ('docx', 'pdf'):
        request = dict(expected_revision=0, format=fmt, template_version='aist-aso.v1',
                       renderer_version=renderer_versions('aist-aso.v1')[fmt],
                       settings=dict(locale='ru-RU', paper='A4', theme='standard', release_mode='draft'))
        _, entry = exports.request(w.actor, w.session, w.project, view['version']['artifact_id'], view['version']['id'], ident('key'), request)
        exports.process_one()
        data, ready = exports.get(w.actor, w.project, entry['id'], download=True)
        assert ready['state'] == 'ready', ready
        (target / ('note.' + fmt)).write_bytes(data)
        if fmt == 'pdf':
            text = '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(data)).pages)
        else:
            doc = Document(io.BytesIO(data))
            text = '\n'.join(p.text for table in doc.tables for row in table.rows for cell in row.cells for p in cell.paragraphs)
        assert 'трлн' in text and '2,2' in text and '2200' not in text
        assert exports.get(w.actor, w.project, entry['id'], download=True)[0] == data


@pytest.mark.parametrize('questions', [None, [], ['Как определяется показатель 2024 года?', 'Уточните актуальные условия сделки.']])
def test_note_questions_share_transaction_and_idempotency_with_version(workspace, questions):
    w = workspace
    source, _ = note_source(w)
    run = start_note(w, source)
    command = model_save_args(deal_note(source))
    if questions is not None:
        command['questions'] = questions
    args = save_operation(run, command)
    tool_id = ident('tool')
    first = w.runs.tool(run['id'], run['epoch'], tool_id, 'aist_save_artifact', args)
    assert first['ok'], first
    view = first['result']
    expected = view['version']['payload']['content']['title'] + '\n' + TEAM_REQUEST
    if questions:
        expected += '\n\n' + '\n'.join(questions)
    expected += '\n\nВладельцы: ' + command['ownership_review']['reason']
    assert reply_text(w, run) == expected
    repeated = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', args)
    assert repeated['ok'] and repeated['result'] == view
    assert reply_text(w, run).count(TEAM_REQUEST) == 1
    assert 'questions' not in view['version']['payload']['content']
    with w.engine.connect() as c:
        assert execute(c, "SELECT count(*) FROM aist.messages WHERE run_id=:r AND role='assistant'", r=run['id']).scalar_one() == 1
        assert execute(c, 'SELECT count(*) FROM aist.artifact_versions WHERE project_id=:p', p=w.project).scalar_one() == 1
        saved_hash = execute(c, "SELECT data->>'arguments_sha256' FROM aist.tool_calls WHERE run_id=:r", r=run['id']).scalar_one()
        assert saved_hash == fingerprint(dict(name='aist_save_artifact', args=args))
    assert w.runs.get(w.actor, w.project, run['id'])['state'] == 'completed'
