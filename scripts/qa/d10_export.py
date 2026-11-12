"""Export evidence without sessions, credentials or OpenClaw configuration."""
from pathlib import Path
import json
from decimal import Decimal
from app.config import Settings
from app.database import make_engine
from app.runs import execute
from app.novita import balance
root=Path('/data/d10');out=root/'export';out.mkdir(exist_ok=True)
engine=make_engine(Settings())
with engine.begin() as c:
    execute(c,'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    snapshot={}
    for table in ('runs','messages','source_versions','facts','model_calls','model_transport_events','tool_calls','usage_entries','artifact_versions','run_events'):
        snapshot[table]=[dict(r) for r in execute(c,'SELECT * FROM aist.'+table).mappings()]
    # Session identifiers are correlations; no session tokens or token hashes are exported.
    (out/'database.json').write_text(json.dumps(snapshot,ensure_ascii=False,default=str,indent=2)+'\n')
engine.dispose()
calls=snapshot['model_calls'];usage={v['id']:v['data'] for v in snapshot['usage_entries']}
sent={v['call_id'] for v in snapshot['model_transport_events'] if v['state']=='sent'}
summary=[]
for m in calls:
    d=m['data'];u=usage[m['usage_entry_id']]
    summary.append({'run_id':m['run_id'],'model_call_id':m['id'],'purpose':d['purpose'],
        'parent_model_call_id':m['parent_model_call_id'],'sent':m['id'] in sent,
        'transport_state':d['transport_state'],'provider_outcome':d['provider_outcome'],
        'provider_completion_id':d['provider_completion_id'],'delivery':d['delivery'],
        'late_response':d['late_response'],'tokens':u['tokens'],'estimated_usd':u['estimated_usd'],
        'billed_usd':u['billed_usd'],'exposure_usd':str(m['exposure'])})
total=sum((Decimal(m['exposure']) for m in calls),Decimal(0))
estimated=sum((Decimal(m['estimated_usd']['value']) for m in summary if m['estimated_usd']['value'] is not None),Decimal(0))
(out/'accounting.json').write_text(json.dumps({'historical_post_count':6,'historical_exposure_usd':'0.09726165',
    'd10_post_count':len(sent),'total_post_count':6+len(sent),'d10_exposure_usd':str(total),
    'd10_known_estimated_usd':str(estimated),'d10_calls_without_estimate':sum(m['estimated_usd']['value'] is None for m in summary),
    'total_exposure_usd':str(total+Decimal('0.09726165')),'authorized_usd':'10',
    'provider_billed_usd':None,'calls':summary},ensure_ascii=False,indent=2)+'\n')
# Deliberately exclude native state, private sessions, configs and gateway tokens.
secrets=[Path(Settings().novita_secret_path).read_text().strip()]
session=root/'private/session.json'
if session.exists():secrets.append(json.loads(session.read_text())['token'])
for directory in sorted(root.glob('run_*')):
    config=directory/'state/openclaw.json'
    if config.exists():secrets.append(json.loads(config.read_text())['models']['providers']['novita-aist']['apiKey'])
    candidates=list((directory/'calls').glob('*'))
    candidates += [p for p in directory.iterdir() if p.is_file() and
                   (p.name in ('tools.jsonl','failure.json','gateway-process.log') or p.name.startswith(('agent-','sessions-describe-','sessions-abort-')))]
    for p in candidates:
        data=p.read_bytes()
        if any(secret.encode() in data for secret in secrets if secret):raise SystemExit('Private value in evidence; export refused')
        target=out/directory.name/p.relative_to(directory);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
for name in ('native-boundary-smoke-v2','gateway-startup-smoke-2'):
    for filename in ('summary.json','exchange.json'):
        p=root/name/filename
        if not p.exists():continue
        data=p.read_bytes()
        if any(secret.encode() in data for secret in secrets if secret):raise SystemExit('Private value in probe evidence')
        target=out/name/filename;target.parent.mkdir(exist_ok=True);target.write_bytes(data)
if __import__('sys').argv[-1]=='balance':
    target=root/'balance-after.json'
    if target.exists():raise SystemExit('After balance already read')
    try:receipt=balance(Settings().novita_secret_path)
    except Exception as e:receipt={'outcome':'unknown','error_type':type(e).__name__}
    target.write_text(json.dumps(receipt,indent=2)+'\n')
print('Evidence exported without credentials')
