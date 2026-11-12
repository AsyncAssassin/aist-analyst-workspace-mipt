"""Смешанный абзац v29 сохраняет непроверенную прозу и неизменные основания."""
from copy import deepcopy
import os

import pytest

from app.facts import pin_of
from app.service import ident
from test_d15_postgres import workspace
from test_fable_postgres import native
from test_note_path06_r1_postgres import start, ask, own_items
from test_note_path06_r2_postgres import observation, response, edit, codes
from test_perf02_r4_postgres import save_args

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Только отдельная PostgreSQL')


def test_mixed_fragment_save_reload_and_independent_corrections(workspace, native, tmp_path):
    w = workspace
    source, run = start(w)
    native.queue = [dict(body=response(dict(facts=own_items(), visual_observations=[
        observation('29.6', 2019), observation('7.25', 2021), observation('65', 2030, forecast=True)])))]
    result = ask(w, run, source, tmp_path)
    ordinary = next(f for f in result['facts'] if f['amount'])
    observations = result['visual_observations']
    series = 'Диаграмма содержит ряд выручки за 2019 и 2021 годы и прогноз компании на 2030 год.'
    money = 'Текстовое основание подтверждает выручку 7,25 млн руб. в 2021 году.'
    claim = dict(text=series + ' ' + money, origin='inference', calculation_ids=[],
                 fact_refs=[dict(source_version_id=source['id'], fact_id=ordinary['fact_id'])],
                 visual_observation_ids=[o['observation_id'] for o in observations])
    content = dict(title='Проверка смешанного абзаца', audience='Аналитик', sections=[
        dict(section_id='section_mixed', title='Сведения', blocks=[
            dict(block_id='block_mixed', kind='paragraph', claim=claim)])])
    reply = w.runs.tool(run['id'], run['epoch'], ident('tool'), 'aist_save_artifact', save_args(content))
    assert reply['ok'], reply
    view = reply['result']
    assert codes(view) == {'unchecked_visual_observation'}
    assert view['checks'][0]['profile_version'] == 'note-technical.v29'
    baseline = deepcopy(view)
    for changed in [
        money.replace('7,25', '29,6'),
        money.replace('руб.', 'USD'),
        money.replace('млн', 'млрд'),
        money.replace('2021', '2019'),
        money.replace('2021', '2030'),
    ]:
        payload = deepcopy(view['version']['payload'])
        payload['content']['sections'][0]['blocks'][0]['claim']['text'] = series + ' ' + changed
        view = edit(w, view, payload)
        assert codes(view) == {'unchecked_visual_observation'}
        assert view['checks'][0]['status'] == 'passed'
        assert all(f['severity'] == 'warning' for f in view['checks'][0]['findings'])
        assert view['version']['basis'] == baseline['version']['basis']
        assert w.runs.version(w.actor, w.project, view['version']['id']) == view
    payload = deepcopy(view['version']['payload'])
    payload['content']['sections'][0]['blocks'][0]['claim']['text'] = money + '\n' + series
    view = edit(w, view, payload)
    assert codes(view) == {'unchecked_visual_observation'}
    assert all(o['source'] == pin_of(source) and o['verification_status'] == 'unchecked'
               for o in view['version']['basis']['visual_observations'])
    assert view['checks'][1]['status'] == 'not_checked' and view['review']['acceptance'] is None
    assert w.runs.version(w.actor, w.project, baseline['version']['id']) == baseline
    assert len(native.requests) == 1
