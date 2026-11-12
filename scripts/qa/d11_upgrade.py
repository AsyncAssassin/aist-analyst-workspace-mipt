"""Проверка сохранности данных после 0005 -> 0006."""
import json
from pathlib import Path
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute
from app.service import Service
from app.storage import Storage
from app.normalization import fingerprint
settings=Settings();engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings)
s=json.loads(Path('/evidence/private/state.json').read_text());v=runs.version(s['actor'],s['project'],s['version'])
assert fingerprint({k:v['version'][k] for k in ('payload','basis')})=='55c50ecd18f68c18e5b33d57efbe8038f90a962c6ccfc4ed168e6741c876e2ea'
assert v['review']['revision']==0 and v['review']['acceptance'] is None
with engine.begin() as c:
    rev=execute(c,'SELECT version_num FROM alembic_version').scalar_one()
    assert rev=='0006_editor_versions'
    assert execute(c,'SELECT count(*) FROM aist.version_acceptances').scalar_one()==0
Path('/evidence/result.json').write_text(json.dumps(dict(passed=True,upgrade='0005 -> 0006',legacy_sha=v['version']['content_sha256'],review=v['review']),indent=2))
print('Upgrade preserves saved v1 and its checks')
