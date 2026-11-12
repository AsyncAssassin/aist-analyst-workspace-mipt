"""0006 -> 0007: существующие данные и receipts остаются читаемыми."""
import json,subprocess,sys
from pathlib import Path
from alembic import command
from alembic.config import Config
from app.config import Settings
from app.database import make_engine
from app.service import Service,ident
from app.runs import Runs,execute
from app.storage import Storage
from app.normalization import fingerprint
from app.versions import write
from app.exports import Exports
from app.note_renderer import RENDERERS
cfg=Config('/workspace/infra/alembic.ini');command.upgrade(cfg,'0006_editor_versions')
subprocess.check_call([sys.executable,'/workspace/scripts/qa/d11_seed.py'])
s=json.loads(Path('/evidence/private/state.json').read_text());settings=Settings();engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings)
before=runs.version(s['actor'],s['project'],s['version']);v=before['version']
key=ident('key');body=dict(expected_revision=0,content_sha256=v['content_sha256'],acknowledge_unchecked_semantics=True)
_,review=write(runs,s['actor'],s['session']['session_id'],s['project'],s['artifact'],s['version'],'acceptVersion',key,body)
with engine.connect() as c:
 old=execute(c,'SELECT count(*) FROM aist.receipts').scalar()
command.upgrade(cfg,'head')
status,again=write(runs,s['actor'],s['session']['session_id'],s['project'],s['artifact'],s['version'],'acceptVersion',key,body)
assert status==200 and again==review
assert runs.version(s['actor'],s['project'],s['version'])['version']==v
exports=Exports(runs);status,e=exports.request(s['actor'],s['session']['session_id'],s['project'],s['artifact'],s['version'],ident('key'),dict(expected_revision=1,format='docx',template_version='aist-note.v1',renderer_version=RENDERERS['docx'],settings=dict(locale='ru-RU',paper='A4',theme='standard',release_mode='accepted')))
assert status==202 and e['acceptance_id']==review['acceptance']['id']
with engine.connect() as c:
 assert execute(c,'SELECT count(*) FROM aist.receipts').scalar()==old+1
 assert execute(c,'SELECT version_num FROM alembic_version').scalar()=='0007_exports'
result=dict(passed=True,upgrade='0006 -> 0007',version_sha=v['content_sha256'],prior_receipts=old,acceptance_receipt_replayed=True,new_export=e['id'])
Path('/evidence/result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
