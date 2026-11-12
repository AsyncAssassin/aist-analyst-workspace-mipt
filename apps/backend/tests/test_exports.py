from copy import deepcopy
from pathlib import Path
import hashlib
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, NameObject, NumberObject
from app.note_renderer import amount, content_plan, render, verify, build_docx
from app.exports import ExportFiles


def sample():
    return dict(revision=2,payload={'content':{'title':'Записка','audience':'Редакторы','sections':[{'title':'Вывод','blocks':[
      {'kind':'paragraph','claim':{'text':'Ручной текст & < >.\nВторая строка.','fact_refs':[],'calculation_ids':[]}},
      {'kind':'table','caption':'Сохраненная подпись','rows':[{'label':'Выручка','period_label':'9M’21','amount':{'value':'4586','scale':'1000000','currency':'RUB','unit':'money'},'fact_refs':[{'source_version_id':'sv_sample','fact_id':'f'}],'calculation_ids':[]}]}]}]}},basis={'facts':[{'source':{'source_version_id':'sv_sample'},'fact_id':'f','metric':'Выручка','period':{'label':'9M’21'},'subject':'Компания','reporting_basis':'МСФО','amount':{'value':'4586','scale':'1000000','currency':'RUB','unit':'money'},'evidence':[{'locator':{'pdf_page':11}}],'limitations':['Ограничение сохранено.']}],'calculations':[]})


def test_amount_uses_exact_decimal_and_scale():
    assert amount(dict(value='-7193.00',scale='1000000',currency='RUB',unit='money'))=='-7 193,00 млн RUB'
    assert amount(dict(value='56.85',scale='1',currency=None,unit='percent'))=='56,85%'
    assert amount(dict(value='9007199254740999',scale='1',currency='USD',unit='money'))=='9 007 199 254 740 999 USD'


def test_themes_do_not_change_content():
    v=sample();old=deepcopy(v)
    assert content_plan(v,dict(theme='standard',release_mode='draft'),[])==content_plan(v,dict(theme='compact',release_mode='draft'),[])
    assert v==old


@pytest.mark.parametrize('theme',['standard','compact'])
def test_real_converter_preserves_text_tables_links_and_fonts(tmp_path,theme):
    checks=[dict(kind='semantic',status='not_checked')]
    report=render(sample(),dict(theme=theme,release_mode='draft'),checks,tmp_path)
    assert report['pages']>=1 and report['links']==1
    plan=content_plan(sample(),dict(release_mode='draft'),checks)
    plan[0]=('title','Подмененный заголовок')
    with pytest.raises(ValueError,match='content mismatch'):verify(tmp_path/'note.docx',tmp_path/'note.pdf',plan)
    plan=content_plan(sample(),dict(release_mode='draft'),checks)
    writer=PdfWriter(clone_from=tmp_path/'note.pdf')
    next(a.get_object() for p in writer.pages for a in p.get('/Annots',[]) if a.get_object().get('/Subtype')=='/Link')[NameObject('/Dest')]=ArrayObject([NumberObject(999)])
    writer.write(tmp_path/'redirected.pdf')
    with pytest.raises(ValueError,match='PDF references lost or redirected'):
        verify(tmp_path/'note.docx',tmp_path/'redirected.pdf',plan)


def test_saved_bytes_missing_corrupt_and_no_renderer(tmp_path):
    files=ExportFiles(tmp_path);e=dict(id='exp_'+'a'*32,format='pdf',file=dict(sha256=hashlib.sha256(b'saved').hexdigest(),byte_size=5))
    assert files.read(e)[1]=='export_missing'
    files.path(e).write_bytes(b'saved');assert files.read(e)==(b'saved',None)
    files.path(e).write_bytes(b'other');assert files.read(e)[1]=='export_corrupt'
    files.path(e).unlink();files.path(e).symlink_to(tmp_path/'other');assert files.read(e)[1]=='export_corrupt'


def test_cleanup_requires_owned_unchanged_unpublished_file(tmp_path):
    files=ExportFiles(tmp_path);e=dict(id='exp_'+'a'*32,format='pdf')
    stage=files.stage(e);p=stage/'note.pdf';p.write_bytes(b'candidate');inventory=files.inventory(e)
    p.write_bytes(b'changed')
    with pytest.raises(OSError,match='Changed'):files.cleanup(e,inventory)
    assert p.read_bytes()==b'changed'
    inventory=files.inventory(e);ready=files.path(e);ready.write_bytes(b'published');files.cleanup(e,inventory)
    assert ready.read_bytes()==b'published' and not p.exists()
