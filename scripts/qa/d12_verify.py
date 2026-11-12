"""Сверка контрольных файлов, сохраненных данных и ограничений БД."""
import hashlib,json,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from pypdf import PdfReader
from sqlalchemy.exc import DBAPIError
from app.config import Settings
from app.database import make_engine
from app.runs import Runs,execute
from app.service import Service
from app.storage import Storage
from app.exports import Exports
from app.normalization import fingerprint
from app.note_renderer import content_plan,verify
settings=Settings();engine=make_engine(settings);runs=Runs(Service(engine,Storage(settings.storage_root)),settings);exports=Exports(runs)
s=json.loads(Path('/evidence/private/state.json').read_text());b=json.loads(Path('/evidence/evidence/browser/result.json').read_text())
v1=runs.version(s['actor'],s['project'],s['version']);original=json.loads(Path('/inputs/version.json').read_text());original['version']['project_id']=s['project'];assert v1==original
v2=runs.version(s['actor'],s['project'],b['v2']);v3=runs.version(s['actor'],s['project'],b['v3']);assert v2['review']['acceptance'] and v3['review']['acceptance'] is None
assert v2['checks'][1]['status']=='not_checked';assert v2['version']['basis']==original['version']['basis']
sha=hashlib.sha256((Path(settings.storage_root)/(s['source']+'.pdf')).read_bytes()).hexdigest();assert sha==v1['version']['provenance']['source_versions'][0]['sha256']
root=Path('/evidence/evidence/browser');files=[];release_reports={}
def docx_text(path):
 with zipfile.ZipFile(path) as z:r=ET.fromstring(z.read('word/document.xml'))
 return '\n'.join(x.text or '' for x in r.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
for name in ['accepted-v2','draft-v3','compact-v3']:
 selected=v2 if name.startswith('accepted') else v3
 plan=content_plan(selected['version'],dict(release_mode='accepted' if name.startswith('accepted') else 'draft'),selected['checks'])
 release_reports[name]=verify(root/(name+'.docx'),root/(name+'.pdf'),plan)
 for format in ['docx','pdf']:
  path=root/(name+'.'+format);data=path.read_bytes();text=docx_text(path) if format=='docx' else '\n'.join(p.extract_text() for p in PdfReader(path).pages)
  normalized=''.join(text.split())
  # verify() already compares the rendered values and basis from the selected version.
  for item in ['Редакторская оговорка: перед использованием записки необходимо сверить актуальность исходных материалов.',
               'Выручка ВУШ Холдинг, млн RUB; рост выручки, %; периоды по источнику',
               'Смысловая проверка не проведена.']:
   assert ''.join(item.split()) in normalized,(name,format,item)
  assert ('Выпуск принятой версии.' if name.startswith('accepted') else 'ЧЕРНОВИК.') in text
  files.append(dict(name=path.name,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),pages=len(PdfReader(path).pages) if format=='pdf' else None))
assert docx_text(root/'draft-v3.docx')==docx_text(root/'compact-v3.docx')
with engine.connect() as c:
 rows=[dict(r) for r in execute(c,'SELECT id,data,render_count,cleanup FROM aist.exports ORDER BY id').mappings()]
 calls=execute(c,'SELECT count(*) FROM aist.model_calls').scalar();assert calls==0
 counts={k:execute(c,'SELECT count(*) FROM aist.'+k).scalar() for k in ['artifact_versions','version_acceptances','facts','receipts']}
for r in rows:
 if r['data']['state']=='ready':
  data,problem=exports.files.read(r['data']);assert not problem
# Guards must reject descriptor replacement, identity rewrite, and implicit retry.
blocked=[];target=b['exports'][0]['id']
for name,sql,id in [
 ('descriptor',"UPDATE aist.exports SET data=jsonb_set(data,'{file,sha256}',to_jsonb(repeat('0',64))) WHERE id=:id",target),
 ('identity',"UPDATE aist.exports SET data=jsonb_set(data,'{renderer_version}','\"other\"') WHERE id=:id",target),
 ('retry',"UPDATE aist.exports SET data=jsonb_set(data,'{state}','\"queued\"') WHERE id=:id",next(r['id'] for r in rows if r['data']['state']=='failed'))]:
 try:
  with engine.begin() as c:execute(c,sql,id=id)
 except DBAPIError:blocked.append(name)
 else:raise AssertionError('DB guard missing: '+name)
result=dict(passed=True,source_sha256=sha,v1_sha256=v1['version']['content_sha256'],v2_sha256=v2['version']['content_sha256'],v2_acceptance=v2['review']['acceptance'],v3_acceptance=None,semantic='not_checked',model_calls=calls,provider_posts=0,counts=counts,files=files,release_reports=release_reports,theme_content_equal=True,blocked_mutations=blocked,exports=rows)
Path('/evidence/evidence/final-state.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print('Control files, persisted bytes, source, acceptance and DB guards verified')
