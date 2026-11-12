import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['ready','db-down','recovered','backend-down'])
parser.add_argument('--api')
parser.add_argument('--web', default='http://127.0.0.1:30811')
parser.add_argument('--output',required=True);args=parser.parse_args()
api = args.api or args.web + '/api'


def fetch(url):
    try:
        with urlopen(url,timeout=8) as response:return {'status':response.status,'body':response.read().decode()}
    except HTTPError as e:return {'status':e.code,'body':e.read().decode()}


results={'shell':fetch(args.web+'/'),'proxy':fetch(args.web+'/api/readyz')}
assert results['shell']['status']==200
if args.phase=='backend-down':
    assert results['proxy']['status']==502 and results['proxy']['body']=='{"status":"unavailable"}'
else:
    results['health']=fetch(api+'/healthz');results['ready']=fetch(api+'/readyz')
    assert results['health']['status']==200
    expected=503 if args.phase=='db-down' else 200
    assert results['ready']['status']==results['proxy']['status']==expected
    if expected==503:assert json.loads(results['ready']['body'])['reason']=='database_unavailable'
for value in results.values():
    for forbidden in ('local-only-example','Traceback','psycopg','postgresql://'):assert forbidden not in value['body']
Path(args.output).write_text(json.dumps({'phase':args.phase,'passed':True,'responses':results},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'phase':args.phase,'passed':True,'statuses':{k:v['status'] for k,v in results.items()}}))
