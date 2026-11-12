"""The product sends the qualitative instruction through its ordinary extraction transport."""
import json
import os
from pathlib import Path

import pytest

from app.extraction import extraction_request, extraction_schema
from app.openclaw_runtime import execution_facts
from app.runs import json_value
from test_chat01_postgres import body, conversation, send
from test_chat01_r1_postgres import provider
from test_d15_postgres import workspace
from test_flow01_02_postgres import MIXED, QUAL, context
from test_flow01_05_postgres import ask
from test_perf02_r1_postgres import upload

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('targeted', [False, True])
def test_product_transport_delivers_standalone_passage_instruction(workspace, provider, tmp_path, targeted):
    w, p = workspace, provider
    p.facts = [QUAL]
    if targeted:
        source, run, _, _ = context(w, MIXED)
        result = ask(w, p.worker, run, source, 'Бизнес компании и ограничения', tmp_path)
        assert result['ok'] and result['result']['status'] == 'complete'
    else:
        source = upload(w, 'instruction-delivery', MIXED)
        request = body(sources=[source['id']], question='Бизнес компании и ограничения')
        request.update(skill_version_id='skill_analytical-note-v1', requested_result_type='analytical_note')
        send(w, conversation(w), request)
        run = w.runs.claim('note-path01')
        facts, call = execution_facts(p.worker, run, 'Бизнес компании и ограничения', tmp_path)
        assert len(facts) == 1 and call
    assert len(p.sent) == 1
    sent = p.sent[0]
    instruction = sent['messages'][0]['content']
    expected = extraction_request({1: 'Neutral source'}, 'Business',
                                  question='Бизнес компании и ограничения' if targeted else None,
                                  aso_note=targeted)
    assert instruction == expected['messages'][0]['content']
    assert 'Group related qualitative passages' not in instruction
    assert 'self-contained claim from one printed block' in instruction
    assert 'subject, negation and essential qualification' in instruction
    assert 'Several sentences' in instruction and 'need not end with a period' in instruction
    assert 'separate fact for each selected card' in instruction
    assert 'most relevant' in instruction and 'Aim for 64 material candidates across both lists' in instruction
    assert 'footnote or issuer identity as a separate evidence range' in instruction
    context_sent = json.loads(sent['messages'][1]['content'])
    assert context_sent['output_schema'] == extraction_schema()
    assert context_sent['output_schema']['properties']['facts']['maxItems'] == 64
    assert [line['text'] for line in context_sent['pages'][0]['lines']] == MIXED
    if os.getenv('AIST_FLOW01_EVIDENCE'):
        Path(os.environ['AIST_FLOW01_EVIDENCE'], f'01-dispatch-{targeted}.json').write_text(
            json_value(dict(database=w.settings.db_name, transport='loopback fixture', request=sent,
                            external_model_posts=0, embeddings_posts=0)))
    w.runs.stop(run['id'], run['epoch'], 'validation_failed')
