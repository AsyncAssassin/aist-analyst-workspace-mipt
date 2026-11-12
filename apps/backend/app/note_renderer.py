"""Шаблон aist-note.v1: буквальное сохраненное содержание, DOCX -> PDF."""
from pathlib import Path
import re
import hashlib
from importlib.metadata import version as package_version
from functools import lru_cache
from collections import Counter
import tempfile
import zipfile
from xml.etree import ElementTree as ET
from docx import Document
from docx.shared import Cm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pypdf import PdfReader
from .document_process import document_command

BUILD = 'notepath06r2.1-docx1.2.0-lo25.2.3u8-dj2.37'
RENDERERS = {'docx': 'aist-docx.' + BUILD, 'pdf': 'aist-pdf.' + BUILD}
VALIDATOR = 'aist-release.d14.1'
FONT = 'DejaVu Sans'
DOCX_METADATA_TITLE_LIMIT = 255


def renderer_versions(template):
    if template == 'aist-note.v1':
        return RENDERERS
    if template == 'aist-note.v2':
        from .note_standard import RENDERERS as standard
        return standard
    if template == 'aist-aso.v1':
        from .aso_renderer import RENDERERS as aso_versions
        return aso_versions
    return {}


def registrations(registry):
    for item in registry['renderers']:
        versions = renderer_versions(item['templates'][0])
        item.update(version=versions[item['format']], status='enabled')
    return registry


def amount(a):
    # Decimal spelling and scale stay exact; no float, rounding or conversion.
    integer, dot, fraction = a['value'].partition('.')
    integer = re.sub(r'(?<=\d)(?=(\d{3})+$)', ' ', integer)
    value = integer + (',' + fraction if dot else '')
    scale = {'1': '', '1000': 'тыс. ', '1000000': 'млн ', '1000000000': 'млрд ', '1000000000000': 'трлн '}.get(a['scale'], '×' + a['scale'] + ' ')
    unit = a['currency'] or {'percent': '%', 'count': 'ед.', 'ratio': 'коэф.'}.get(a['unit'], a['unit'])
    return value + ('' if unit == '%' and not scale else ' ') + scale + unit


def qualitative_quote(fact):
    # Первое evidence может подтверждать эмитента, а не само утверждение.
    compact = lambda text: re.sub(r'\s+', '', text).casefold()
    metric = compact(fact['metric'])
    quotes = [e['quote'] for e in fact['evidence'] if metric and metric in compact(e.get('quote', ''))]
    return min(quotes, key=len) if quotes else fact['raw_text']


def content_plan(version, settings, checks):
    content, basis = version['payload']['content'], version['basis']
    indices = {(f['source']['source_version_id'],f['fact_id']): str(i) for i, f in enumerate(basis['facts'], 1)}
    indices.update({c['calculation_id']: str(i) for i, c in enumerate(basis['calculations'], len(indices)+1)})
    def refs(obj):
        return [indices[(r['source_version_id'],r['fact_id'])] for r in obj['fact_refs']] + [indices[c] for c in obj['calculation_ids']]
    documents={d['source']['source_version_id']:d for d in basis.get('sources',[])}
    def document(f):
        vid=f['source']['source_version_id'];d=documents.get(vid)
        return (d['original_filename']+' · версия '+str(d['revision'])) if d else vid
    from .visual_observations import annotation, NOTICE
    from .review_candidates import annotation as candidate_annotation
    observations = {item['observation_id']: item for item in basis.get('visual_observations', [])}
    plan = [('title', content['title'])]
    if settings['release_mode'] == 'draft':
        plan.append(('p', 'ЧЕРНОВИК'))
    if any(c['status'] == 'failed' for c in checks):
        plan.append(('p', 'В сохраненной версии есть ошибки проверок. Выпуск является черновым.'))
    if observations:
        plan.append(('p', NOTICE))
    for section in content['sections']:
        plan.append(('h', section['title']))
        for b in section['blocks']:
            if b['kind'] == 'paragraph':
                plan.append(('p', b['claim']['text']))
                if mark := annotation(b['claim'], observations):
                    plan.append(('p', mark))
                if mark := candidate_annotation(b['claim'], basis):
                    plan.append(('p', mark))
                if refs(b['claim']): plan.append(('refs', refs(b['claim'])))
            else:
                plan.append(('caption', b['caption']))
                plan.append(('table', [(r['label'] + ('\n' + annotation(r, observations) if r.get('visual_observation_ids') else ''), r['period_label'], amount(r['amount']), refs(r)) for r in b['rows']]))
    if basis.get('comparisons'):
        plan.append(('h','Сопоставление документов'))
        for comparison in basis['comparisons']:
            plan.append(('p',comparison['message']))
            plan.append(('refs',refs(dict(fact_refs=comparison['fact_refs'],calculation_ids=[]))))
    plan.append(('h', 'Основания и ограничения'))
    for f in basis['facts']:
        plan.append(('basis', (indices[(f['source']['source_version_id'],f['fact_id'])], f['metric'] + '. ' + f['period']['label'])))
        plan.append(('p', f['subject'] + '. ' + f['reporting_basis'] + '.'))
        plan.append(('p', amount(f['amount']) if f['amount'] else qualitative_quote(f)))
        pages = sorted({e['locator']['pdf_page'] for e in f['evidence'] if 'pdf_page' in e['locator']})
        if pages:
            locations = 'Страницы PDF: ' + ', '.join(map(str, pages))
        else:
            sections = sorted({e['locator']['json_pointer'] for e in f['evidence']})
            locations = 'Раздел JSON: ' + ', '.join(sections)
        plan.append(('p', 'Документ: '+document(f)+'. ' + locations + '.'))
    for c in basis['calculations']:
        plan.append(('basis', (indices[c['calculation_id']], 'Расчет: ' + amount(c['rounded']))))
        plan.append(('p', c['formula_display']))
        plan.append(('p', 'Входы: ' + '; '.join(i['name'] + ' = основание ' + indices[(i['fact']['source_version_id'],i['fact']['fact_id'])] for i in c['inputs']) + '.'))
        plan.append(('p', 'Точное значение: ' + c['exact']['numerator'] + '/' + c['exact']['denominator'] + '. Округление: ' + c['rounding']['mode'] + ', знаков: ' + str(c['rounding']['places']) + '.'))
    limitations = dict.fromkeys(x for b in [*basis['facts'], *basis['calculations']] for x in b['limitations'])
    for text in limitations:
        # Сведения о проверках остаются в VersionView, в выпуске нужны ограничения данных.
        for status in (
            'Автоматически проверены ссылки, исходное написание числа и подпись периода.',
            'Полная смысловая и визуальная проверка не проведена.',
            'Проверка ссылок не является смысловой проверкой текста или человеческим принятием.',
        ):
            text = text.replace(status, '')
        if text.strip(): plan.append(('p', text.strip()))
    return plan


def link(p, number):
    anchor = OxmlElement('w:hyperlink'); anchor.set(qn('w:anchor'), 'basis' + number)
    r = OxmlElement('w:r'); pr = OxmlElement('w:rPr'); color = OxmlElement('w:color'); color.set(qn('w:val'), '245E55'); pr.append(color); r.append(pr)
    t = OxmlElement('w:t'); t.text = '[' + number + ']'; r.append(t); anchor.append(r); p._p.append(anchor)


def build_docx(plan, theme, path):
    # Layout primitives reused from D01-B-R2: A4, named styles, repeating headers.
    doc = Document(); section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.5 if theme == 'compact' else 1.8)
    section.left_margin = section.right_margin = Cm(2)
    for name, size in [('Normal', 10 if theme == 'compact' else 10.5), ('Title', 16), ('Heading 1', 13), ('Heading 2', 11), ('Caption', 10)]:
        style = doc.styles[name]; style.font.name = FONT; style.font.size = Pt(size); style.font.color.rgb = RGBColor(0,0,0)
        style.paragraph_format.space_after = Pt(3 if theme == 'compact' else 5)
        style.paragraph_format.line_spacing = 1.0 if theme == 'compact' else 1.05
        if name.startswith('Heading'): style.paragraph_format.space_before = Pt(9)
        style.paragraph_format.widow_control = True
    for fonts in doc.styles.element.xpath('.//w:rFonts'):
        for attribute in list(fonts.attrib): del fonts.attrib[attribute]
        for family in ('ascii','hAnsi','eastAsia','cs'): fonts.set(qn('w:'+family),FONT)
    for style in doc.styles:
        for border in style.element.xpath('.//w:pBdr'): border.getparent().remove(border)
    doc.core_properties.author = 'AIST'; doc.core_properties.title = plan[0][1][:DOCX_METADATA_TITLE_LIMIT]
    in_basis = False
    for index, (kind, data) in enumerate(plan):
        if kind == 'table':
            table = doc.add_table(rows=1, cols=4); table.autofit = False
            widths = [5.5, 3.2, 4.5, 3.8]
            for col, width in zip(table.columns, widths): col.width = Cm(width)
            for cell, title in zip(table.rows[0].cells, ['Показатель','Период','Значение','Основания']): cell.text = title
            repeat = OxmlElement('w:tblHeader'); table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for row in data:
                cells = table.add_row().cells
                for cell, text in zip(cells[:3], row[:3]): cell.text = text
                for n in row[3]: link(cells[3].paragraphs[0], n)
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    cell.width = Cm(widths[ci]); pr = cell._tc.get_or_add_tcPr()
                    margins = OxmlElement('w:tcMar')
                    for side in ['top','left','bottom','right']:
                        el=OxmlElement('w:'+side);el.set(qn('w:w'),'90');el.set(qn('w:type'),'dxa');margins.append(el)
                    pr.append(margins)
                    borders=OxmlElement('w:tcBorders')
                    for side in ['top','left','bottom','right']:
                        el=OxmlElement('w:'+side);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');borders.append(el)
                    pr.append(borders)
                    if ri == 0:
                        shade=OxmlElement('w:shd');shade.set(qn('w:fill'),'E5EEEB');pr.append(shade)
                    for p in cell.paragraphs:
                        p.paragraph_format.space_after=Pt(2)
                        for run in p.runs: run.bold = ri == 0
            doc.add_paragraph()
        elif kind == 'refs':
            p=doc.add_paragraph('Основания: ')
            p.paragraph_format.keep_together=True
            for n in data: link(p,n)
        elif kind == 'basis':
            in_basis = True
            n,title=data;p=doc.add_paragraph('['+n+'] '+title,'Heading 2')
            start=OxmlElement('w:bookmarkStart');start.set(qn('w:id'),n);start.set(qn('w:name'),'basis'+n)
            end=OxmlElement('w:bookmarkEnd');end.set(qn('w:id'),n)
            p._p.insert(1,start);p._p.append(end)
        else:
            if kind == 'h': in_basis = False
            p=doc.add_paragraph(data, {'title':'Title','h':'Heading 1','caption':'Caption'}.get(kind,'Normal'))
            if kind == 'caption' or (index+1<len(plan) and plan[index+1][0]=='refs'): p.paragraph_format.keep_with_next=True
            if kind == 'p' and in_basis:
                in_basis = not data.startswith('Документ: ')
                p.paragraph_format.keep_with_next = in_basis
    doc.save(path)


def convert(docx, directory):
    with tempfile.TemporaryDirectory(prefix='aist-lo-') as profile:
        command=['/usr/bin/libreoffice', '-env:UserInstallation='+Path(profile).as_uri(), '--headless', '--convert-to', 'pdf:writer_pdf_Export:{"ExportBookmarksToPDFDestination":{"type":"boolean","value":"true"},"ExportBookmarks":{"type":"boolean","value":"true"}}', '--outdir', str(directory), str(docx)]
        document_command(command, seconds=60, home=profile)
    pdf=directory/'note.pdf'
    if not pdf.is_file(): raise ValueError('PDF converter produced no file')
    return pdf


def normalize(text): return re.sub(r'\s+', '', text)


def verify(docx, pdf, plan, *, expected_paragraphs=None, font_name='DejaVuSans', external_urls=(), page_footer=None):
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with zipfile.ZipFile(docx) as z:
        if z.testzip(): raise ValueError('Invalid DOCX zip')
        root=ET.fromstring(z.read('word/document.xml'))
        relationships=ET.fromstring(z.read('word/_rels/document.xml.rels'))
    paragraphs = []
    for paragraph in root.findall('.//w:p', ns):
        parts = []
        for node in paragraph.iter():
            if node.tag == qn('w:t'):
                parts.append(node.text or '')
            elif node.tag == qn('w:tab'):
                parts.append('\t')
            elif node.tag == qn('w:cr') or (node.tag == qn('w:br') and
                                             node.get(qn('w:type'), 'textWrapping') == 'textWrapping'):
                parts.append('\n')
        paragraphs.append(''.join(parts))
    expected=[]
    for kind,data in plan:
        if kind in ('p','h','title','caption'): expected.append(data)
        elif kind=='basis': expected.append('['+data[0]+'] '+data[1])
        elif kind=='refs': expected.append('Основания: '+''.join('['+n+']' for n in data))
        elif kind=='table':
            expected.extend(['Показатель','Период','Значение','Основания'])
            for r in data: expected.extend([*r[:3], ''.join('['+n+']' for n in r[3])])
    if expected_paragraphs is not None:
        expected = expected_paragraphs
    if [x for x in paragraphs if x] != [x for x in expected if x]: raise ValueError('DOCX content mismatch')
    anchors={x.get(qn('w:name')) for x in root.findall('.//w:bookmarkStart',ns)}
    links=root.findall('.//w:hyperlink',ns)
    targets={r.get('Id'):r.get('Target') for r in relationships
             if r.get('TargetMode')=='External' and r.get('Type','').endswith('/hyperlink')}
    external=[targets.get(x.get(qn('r:id'))) for x in links if x.get(qn('r:id'))]
    if Counter(external)!=Counter(external_urls): raise ValueError('Unexpected DOCX publication links')
    links=[x for x in links if not x.get(qn('r:id'))]
    if any(x.get(qn('w:anchor')) not in anchors for x in links): raise ValueError('Broken DOCX reference')
    reader=PdfReader(pdf, strict=True)
    if not reader.pages: raise ValueError('Empty PDF')
    text=''.join(p.extract_text() or '' for p in reader.pages)
    # Ordered comparison permits repeated table headers across pages, never missing content.
    compact=normalize(text); position=0
    if page_footer is not None:
        # LibreOffice emits the footer first on each page. Check its exact title
        # and live page fields before excluding it from flowing body text.
        bodies = []
        for index, page in enumerate(reader.pages, 1):
            page_text = normalize(page.extract_text() or '')
            footer = normalize(page_footer + f' {index} / {len(reader.pages)}')
            if not page_text.startswith(footer):
                raise ValueError('PDF page footer mismatch')
            bodies.append(page_text[len(footer):])
        compact = ''.join(bodies)
    for item in expected:
        item=normalize(item)
        if not item: continue
        found=compact.find(item,position)
        if found < 0: raise ValueError('PDF content mismatch: '+item[:80])
        position=found+len(item)
    pdf_links=[a.get_object() for p in reader.pages for a in p.get('/Annots',[]) if a.get_object().get('/Subtype')=='/Link']
    urls={str(a.get_object().get('/URI')) for link in pdf_links if (a:=link.get('/A')) and a.get_object().get('/S')=='/URI'}
    if urls!=set(external_urls): raise ValueError('PDF publication links changed')
    destinations=reader.named_destinations
    wanted=Counter()
    for link in links:
        destination=destinations.get('/'+link.get(qn('w:anchor')))
        if destination is None or reader.get_destination_page_number(destination) is None:
            raise ValueError('Broken PDF reference destination')
        wanted[tuple(destination.dest_array)]+=1
    actual=Counter(tuple(link.get('/Dest',[])) for link in pdf_links)
    if wanted-actual: raise ValueError('PDF references lost or redirected')
    fonts=document_command(['/usr/bin/pdffonts', str(pdf)], seconds=15).decode().splitlines()[2:]
    allowed_fonts = (font_name,) if isinstance(font_name, str) else font_name
    if not fonts or any(not any(name in x for name in allowed_fonts) or not re.search(r'\byes\s+yes\s+yes\b',x) for x in fonts): raise ValueError('Unexpected or unembedded font')
    bbox=document_command(['/usr/bin/pdftotext', '-bbox', str(pdf), '-'], seconds=15)
    tree=ET.fromstring(bbox);nsx={'x':'http://www.w3.org/1999/xhtml'}
    for page in tree.findall('.//x:page',nsx):
        for word in page.findall('.//x:word',nsx):
            if float(word.attrib['xMin'])<15 or float(word.attrib['yMin'])<15 or float(word.attrib['xMax'])>float(page.attrib['width'])-15 or float(word.attrib['yMax'])>float(page.attrib['height'])-15: raise ValueError('Text outside safe page bounds')
    return {'pages':len(reader.pages),'links':len(links),'fonts':fonts}


def render(version, settings, checks, directory, template_version='aist-note.v1'):
    if template_version == 'aist-aso.v1':
        from .aso_renderer import render as aso_render
        return aso_render(version, settings, checks, directory)
    if template_version == 'aist-note.v2':
        from .note_standard import render as standard_render
        return standard_render(version, settings, checks, directory)
    if template_version != 'aist-note.v1':
        raise ValueError('Unknown template')
    verify_environment()
    plan=content_plan(version,settings,checks);docx=directory/'note.docx'
    build_docx(plan,settings['theme'],docx);pdf=convert(docx,directory)
    return verify(docx,pdf,plan)


@lru_cache(maxsize=1)
def verify_environment():
    for name, expected in [('python-docx','1.2.0'),('lxml','6.0.2')]:
        if package_version(name)!=expected: raise RuntimeError('Renderer package mismatch')
    for name, expected in [('libreoffice-core','4:25.2.3-2+deb13u8'),('libreoffice-writer','4:25.2.3-2+deb13u8'),('fonts-dejavu-core','2.37-8'),('poppler-utils','25.03.0-5+deb13u4')]:
        if document_command(['/usr/bin/dpkg-query','-W','-f=${Version}',name], seconds=5).decode()!=expected: raise RuntimeError('Renderer system package mismatch')
    for name, expected in [('DejaVuSans.ttf','57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e'),('DejaVuSans-Bold.ttf','a4c5bc453ca281d90ea079e596da7ae0dfeb5777497c29ec254e76d97ff6f890')]:
        if hashlib.sha256((Path('/usr/share/fonts/truetype/dejavu')/name).read_bytes()).hexdigest()!=expected: raise RuntimeError('Renderer font mismatch')
