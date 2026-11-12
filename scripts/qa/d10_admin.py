import json
from pathlib import Path
import subprocess
import sys
from app.config import Settings
from app.novita import balance
root=Path('/data/d10');root.mkdir(parents=True,exist_ok=True,mode=0o700)
p=root/'balance-before.json'
if p.exists():
    result=json.loads(p.read_text())
else:
    try:result=balance(Settings().novita_secret_path)
    except Exception as e:result={'outcome':'unknown','error_type':type(e).__name__,'model_posts':0}
    p.write_text(json.dumps(result,indent=2)+'\n')
if result.get('http_status')!=200:raise SystemExit('Balance not observed')
assert __import__('decimal').Decimal(result['USD']['availableBalance'])>1
user=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','create-user','--label','D10 live analyst']))
session=json.loads(subprocess.check_output([sys.executable,'-m','app.cli','issue-session','--user-id',user['user_id'],'--ttl-seconds','604800']))
private=root/'private';private.mkdir(mode=0o700,exist_ok=True)
p=private/'session.json';p.write_text(json.dumps({'user':user,**session}));p.chmod(0o600)
print('Balance recorded; own user/session saved privately; model POSTs 0')
