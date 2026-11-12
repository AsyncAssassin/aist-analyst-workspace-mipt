"""Read-only verification of captured live evidence after execution."""
from pathlib import Path
import json
import sys

root=Path(sys.argv[1]);run_id=sys.argv[2]
db=json.loads((root/'database.json').read_text())
run=next(r for r in db['runs'] if r['id']==run_id)
assert run['state']=='completed'
calls=[m for m in db['model_calls'] if m['run_id']==run_id]
by_id={m['id']:m for m in calls}
records=[];replay=[]
for m in calls:
    path=root/run_id/'calls'
    request=json.loads((path/(m['id']+'-request.json')).read_text())
    response=json.loads((path/(m['id']+'-response.json')).read_text())
    assert m['data']['provider_completion_id']==response['id']
    assert m['data']['transport_state']=='observed' and m['data']['provider_outcome']=='success'
    parent=by_id.get(m['parent_model_call_id'])
    if parent and parent['data']['purpose']=='agent':
        prior=json.loads((path/(parent['id']+'-response.json')).read_text())['choices'][0]['message']
        original=prior.get('tool_calls',[])
        for tool in original:
            history=next(h for h in request['messages'] if h['role']=='assistant' and any(t['id']==tool['id'] for t in h.get('tool_calls',[])))
            replayed=next(t for t in history['tool_calls'] if t['id']==tool['id'])
            assert replayed['function']['name']==tool['function']['name']
            assert json.loads(replayed['function']['arguments'])==json.loads(tool['function']['arguments'])
            assert history.get('reasoning_content')==prior.get('reasoning_content')
            assert any(h['role']=='tool' and h['tool_call_id']==tool['id'] for h in request['messages'])
        replay.append({'model_call_id':m['id'],'parent_model_call_id':parent['id'],'original_ids_and_reasoning':True})
    for tool in response['choices'][0]['message'].get('tool_calls',[]):
        saved=next(t for t in db['tool_calls'] if t['run_id']==run_id and t['executor_tool_call_id']==tool['id'])
        assert saved['data']['model_call_id']==m['id']
        assert saved['data']['name']==tool['function']['name']
        records.append({'model_call_id':m['id'],'provider_completion_id':response['id'],
            'executor_tool_call_id':tool['id'],'tool_call_id':saved['id'],
            'name':saved['data']['name'],'state':saved['data']['state']})
assert replay and records
facts=[f for f in db['facts'] if f['project_id']==run['project_id']]
assert facts and all(f['provenance']['review'] is None for f in facts)
assert all(all(e['location']=='whitespace_exact' for e in f['provenance']['locations']) for f in facts)
assert all(f['provenance']['semantic_status']=='not_checked' for f in facts)
summary={'passed':True,'run_id':run_id,'executor_run_id':run['executor_run_id'],
    'executor_session_id':run['executor_session_id'],'artifact_version_id':run['result_version_id'],
    'model_posts':len(calls),'facts':len(facts),'fact_quotes_located_without_override':True,
    'correlation':records,'replay':replay}
(root/'correlation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'model_posts':len(calls),'tool_calls':len(records),'replays':len(replay)}))
