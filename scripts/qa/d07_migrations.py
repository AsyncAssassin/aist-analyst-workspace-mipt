"""Empty upgrade, accepted D06 data preservation and idempotent upgrade."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from sqlalchemy import text
from app.config import Settings
from app.database import make_engine,probe_database
from app.service import Service
from app.storage import Storage
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'apps/backend/tests'))
from pdf_fixtures import pdf_bytes
p=argparse.ArgumentParser();p.add_argument('--direct-head',action='store_true');p.add_argument('--output',default='/evidence/migrations.json');args=p.parse_args()
settings=Settings();engine=make_engine(settings)

def state():
    with engine.connect() as c:
        schema=c.execute(text("SELECT oid FROM pg_namespace WHERE nspname='aist'")).scalar_one_or_none()
        tables=[dict(x) for x in c.execute(text("SELECT c.relname,c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='aist' AND c.relkind='r' ORDER BY c.relname")).mappings()]
        heads=list(c.execute(text('SELECT version_num FROM alembic_version')).scalars()) if c.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() else []
    return {'schema_oid':schema,'tables':tables,'heads':heads,'readiness':probe_database(engine)}

def upgrade(version):subprocess.run([sys.executable,'-m','alembic','-c','/workspace/infra/alembic.ini','upgrade',version],check=True)

def originals():
    with engine.connect() as c:
        values={t:[dict(x) for x in c.execute(text(f'SELECT * FROM aist.{t} ORDER BY 1')).mappings()] for t in ['users','sessions','projects','sources','source_versions','source_pages','conversations','receipts']}
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()

empty=state();assert empty['schema_oid'] is None
result={'empty':empty}
if not args.direct_head:
    upgrade('0001_foundation');foundation=state();assert foundation['heads']==['0001_foundation'];result['foundation']=foundation
    upgrade('0002_projects_sources');d06=state();assert len(d06['tables'])==8 and d06['readiness']['reason']=='migration_required'
    with engine.begin() as c:c.execute(text("INSERT INTO aist.users(id,label) VALUES ('usr_migration','Synthetic migration')"))
    service=Service(engine,Storage(settings.storage_root))
    _,project=service.write('usr_migration','createProject','migration-project',{'name':'D06 preserved'})
    service.write('usr_migration','createConversation','migration-conversation',{'title':'D06 preserved'},project['id'])
    service.write('usr_migration','uploadSource','migration-source',{'title':'D06 PDF','expected_revision':0},project['id'],data=pdf_bytes(),filename='synthetic.pdf')
    before=originals();result.update(d06=d06,d06_data_sha256=before)
upgrade('head');full=state();assert full['heads']==['0004_run_invariants'] and full['readiness']['reason'] is None
if not args.direct_head:
    assert originals()==before and full['schema_oid']==d06['schema_oid']
    assert all(next(t for t in full['tables'] if t['relname']==old['relname'])==old for old in d06['tables'])
upgrade('head');repeat=state();assert full==repeat
result.update(passed=True,full=full,repeat=repeat,d06_data_preserved=not args.direct_head)
Path(args.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'passed':True,'tables':len(full['tables']),'preserved':not args.direct_head}))
