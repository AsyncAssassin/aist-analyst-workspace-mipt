from copy import deepcopy
import pytest
from app.aso import EDITED_VALUES_NOTE, presentation, omit_editorial_boilerplate
from test_note_standard import sample


def test_display_typography_keeps_sources_values_links_and_mark_offsets(tmp_path):
    from docx import Document
    from pypdf import PdfReader
    from app import aso_renderer
    from app.aso import display_text

    saved = sample()
    saved.update(id='av_typography', content_sha256='0' * 64, revision=1, created_at='2026-11-10T00:00:00Z')
    content = saved['payload']['content']
    content.update(title='Компания \u2014 обзор', team='Отдел \u2013 аналитики')
    section = content['sections'][0]
    section['title'] = 'Бизнес \u2014 показатели'
    paragraph, table = section['blocks']
    text = 'Компания \u2014 сеть. Период: 2022\u20132023.'
    paragraph.update(block_id='point', marks=[dict(start=0, end=12, style='bold')])
    paragraph['claim'].update(text=text, origin='human_editorial')
    table.update(block_id='financials', caption='Продажи \u2014 динамика')
    for index, (row, fact) in enumerate(zip(table['rows'], saved['basis']['facts'])):
        row.update(row_id=f'row_{index}', label='LFL продажи, %')
        row['amount'].update(unit='percent', currency=None, scale='1', value='-10.4' if index else '12.7')
        fact['metric'] = 'LFL продажи'
    before = deepcopy(saved)
    error = dict(code='quantity_mismatch', severity='error', block_id='point', message='Проверить.', quote='2022\u20132023')
    view = presentation(saved, [dict(findings=[error])])
    cell = view['sections'][0]['blocks'][0]['rows'][0][0]
    assert cell['quotes'] == ['2022-2023']
    assert ''.join(run['text'] for run in cell['styled_runs'] if run['bold']) == 'Компания - с'
    values = view['sections'][0]['blocks'][1]['rows'][0]
    assert [cell['text'] for cell in values] == ['Изменение сопоставимых продаж (LFL), %', '12,7', '(10,4)']
    assert [cell['row_id'] for cell in values] == [None, 'row_0', 'row_1']
    assert all(cell['links'] == [dict(source='sv_sample', page=11)] for cell in values[1:])
    url = 'https://example.org/a\u2014b'
    assert display_text('Текст \u2014 ' + url) == 'Текст - ' + url

    aso_renderer.render(saved, dict(release_mode='draft', theme='standard'), [], tmp_path)
    doc = Document(tmp_path / 'note.docx')
    texts = [node.text or '' for node in doc.element.xpath('.//w:t')]
    texts += [p.text for p in doc.sections[0].footer.paragraphs]
    texts += [page.extract_text() for page in PdfReader(tmp_path / 'note.pdf').pages]
    assert not any('\u2013' in text or '\u2014' in text for text in texts)
    assert saved == before


def test_dash_normalization_does_not_highlight_another_equal_range():
    saved = sample()
    saved.update(id='av_highlight', content_sha256='0' * 64, revision=1, created_at='2026-11-10T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    block = section['blocks'][0]
    section['blocks'] = [block]
    block['block_id'] = 'point'
    block['claim']['text'] = 'План: 2022\u20132023. Факт: 2022\u20142023.'
    error = dict(code='quantity_mismatch', severity='error', block_id='point', message='Проверить.', quote='2022\u20142023')
    cell = presentation(saved, [dict(findings=[error])])['sections'][0]['blocks'][0]['rows'][0][0]
    assert cell['text'] == 'План: 2022-2023. Факт: 2022-2023.'
    assert cell['quotes'] == [] and not cell['marked']


def test_short_section_stays_together_without_chaining_long_sections(tmp_path):
    from docx import Document
    from pypdf import PdfReader
    from app import aso_renderer

    saved = sample()
    saved.update(id='av_short_section', content_sha256='0' * 64, revision=1, created_at='2026-11-10T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    base = section['blocks'][0]
    section['blocks'] = []
    for index in range(20):
        point = deepcopy(base)
        point.update(block_id=f'point_{index}')
        point['claim']['text'] = f'Пункт {index + 1}. ' + 'Компания развивает собственную логистику и сеть магазинов. ' * 3
        section['blocks'].append(point)
    steps = []
    for index, text in enumerate(('Уточнить параметры сделки.', 'Согласовать дальнейшие переговоры.')):
        point = deepcopy(base)
        point.update(block_id=f'step_{index}')
        point['claim']['text'] = text
        steps.append(point)
    saved['payload']['content']['sections'].append(dict(title='Следующие шаги', blocks=steps))
    aso_renderer.render(saved, dict(release_mode='draft', theme='standard'), [], tmp_path)
    doc = Document(tmp_path / 'note.docx')
    paragraphs = {p.text: p for p in doc.paragraphs}
    assert all(not paragraphs[b['claim']['text']].paragraph_format.keep_with_next for b in section['blocks'])
    assert paragraphs[steps[0]['claim']['text']].paragraph_format.keep_with_next
    assert not paragraphs[steps[1]['claim']['text']].paragraph_format.keep_with_next
    pages = [page.extract_text() for page in PdfReader(tmp_path / 'note.pdf').pages]
    assert len(pages) > 1
    assert sum(all(b['claim']['text'] in page for b in steps) and 'Следующие шаги' in page for page in pages) == 1


def test_aso_exports_keep_list_hierarchy_marks_and_appendix(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    from pypdf import PdfReader
    from app import aso_renderer
    from app.note_renderer import normalize

    saved = sample()
    saved.update(id='av_layout', content_sha256='0'*64, revision=1, created_at='2026-11-05T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    base, table = section['blocks']
    table['block_id'] = 'financials'
    for i, row in enumerate(table['rows']):
        row['row_id'] = f'row_{i}'
    items = []
    for depth, text in enumerate(('Основное направление бизнеса.', 'Деталь направления.\nОтдельная строка.',
                                  'Условие: объём зависит от спроса.')):
        item = deepcopy(base)
        item.update(block_id=f'point_{depth}', marks=[dict(start=0, end=6, style='bold')])
        item['claim']['text'] = text
        if depth:
            item['list_level'] = depth
        items.append(item)
    section['blocks'] = [*items, table]
    appendix = deepcopy(base)
    appendix.update(block_id='appendix')
    appendix['claim']['text'] = 'Дополнительные условия договора.'
    saved['payload']['content']['sections'].append(dict(title='Приложения', blocks=[appendix]))
    before = deepcopy(saved)
    aso_renderer.render(saved, dict(release_mode='draft', theme='standard'), [], tmp_path)

    doc = Document(tmp_path/'note.docx')
    # Pages inserts empty pages around a page-break paragraph between body grids.
    # A new section preserves the appendix boundary without that import defect.
    assert len(doc.sections) == 2
    assert not doc.element.xpath('.//w:pageBreakBefore')
    paragraphs = doc.paragraphs
    points = [next(p for p in paragraphs if p.text == item['claim']['text']) for item in items]
    assert [p.style.name for p in points] == ['List Bullet', 'List Bullet 2', 'List Bullet 3']
    assert all(p.runs[0].bold and p.runs[0].text == item['claim']['text'][:6]
               for p, item in zip(points, items))
    numbering = doc.part.numbering_part.element
    for style, marker in [('ListBullet', '•'), ('ListBullet2', '○'), ('ListBullet3', '▪')]:
        level = next(level for level in numbering.findall('.//' + qn('w:lvl'))
                     if level.find(qn('w:pStyle')) is not None
                     and level.find(qn('w:pStyle')).get(qn('w:val')) == style)
        assert level.find(qn('w:lvlText')).get(qn('w:val')) == marker
    pages = [page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages]
    assert len(pages) == 2 and 'Приложения' not in pages[0] and 'Приложения' in pages[1]
    for item in [*items, appendix]:
        assert normalize(item['claim']['text']) in normalize(''.join(pages))
    assert any(len(t.columns) >= 3 and len(t.rows) > 1 for t in doc.tables)
    assert saved == before


def test_aso_appendix_after_full_body_does_not_create_an_empty_page(tmp_path):
    from app import aso_renderer
    from app.note_renderer import normalize
    from pypdf import PdfReader

    saved = sample()
    saved.update(id='av_boundary', content_sha256='0'*64, revision=1, created_at='2026-11-06T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    base = section['blocks'][0]
    section['blocks'] = []
    # Длинный раздел проверяет перенос перед приложением без пустой страницы.
    for index in range(58):
        item = deepcopy(base)
        item['block_id'] = f'point_{index}'
        item['claim']['text'] = (f'Направление {index+1}. Компания развивает сеть магазинов и собственную логистику. '
                                 'Поставки товаров осуществляются через распределительные центры.')
        section['blocks'].append(item)
    # This ending fills the second page while leaving too little room for a
    # standalone paragraph containing a break after its empty line.
    section['blocks'][-1]['claim']['text'] += '\nПродолжение описания компании.' * 2
    appendix = deepcopy(base)
    appendix['block_id'] = 'appendix'
    appendix['claim']['text'] = 'Дополнительные условия договора.'
    saved['payload']['content']['sections'].append(dict(title='Приложения', blocks=[appendix]))

    aso_renderer.render(saved, dict(theme='standard', release_mode='draft'), [], tmp_path)
    pages = [page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages]
    assert len(pages) == 4 and all(page.strip() for page in pages)
    assert all('Приложения' not in page for page in pages[:-1])
    assert 'Приложения' in pages[-1]
    for block in [*section['blocks'], appendix]:
        assert normalize(block['claim']['text']) in normalize(''.join(pages))


def test_uncertain_cell_uses_original_row_id_after_pivot_even_if_amounts_match():
    saved = sample()
    saved.update(id='av_test', content_sha256='0'*64, revision=1, created_at='2026-11-07T00:00:00Z')
    paragraph, table = saved['payload']['content']['sections'][0]['blocks']
    paragraph['block_id'], table['block_id'] = 'block_p', 'block_t'
    for i, (r, f) in enumerate(zip(table['rows'], saved['basis']['facts'])):
        r['row_id'] = 'row_'+str(i)
        r['amount']['value'] = f['amount']['value'] = '100'
        f['evidence'][0]['locator']['pdf_page'] = i+2
    finding = dict(code='test', block_id='block_t', row_id='row_1', message='Сверить показатель', severity='error')
    before = deepcopy(saved)
    table_view = presentation(saved,[{'findings':[finding]}])['sections'][0]['blocks'][1]
    _, first, second = table_view['rows'][0]
    assert first['text'] == second['text'] == '100'
    assert not first['marked'] and second['marked']
    assert [c['links'][0]['page'] for c in (first, second)] == [2, 3]
    assert saved == before


def test_drop_only_standalone_publisher_notice_keep_risk_and_qualifications():
    def paragraph(text):
        return {'kind':'paragraph','claim':{'text':text}}
    risk = paragraph('Компания может нарушить ковенант при снижении выручки.')
    mixed = paragraph('Материал носит информационный характер. Договор предусматривает штраф.')
    content = {'sections':[{'title':'Риски','blocks':[risk, paragraph('Настоящий документ не является публичной офертой.'), mixed]}]}
    assert omit_editorial_boilerplate(content)
    assert content['sections'][0]['blocks'] == [risk, mixed]


def test_ipo_presentation_purpose_is_not_a_deal_but_terms_are():
    def block(text):
        return {'kind':'paragraph', 'claim':{'text':text}}
    offer = block('Предложение включает 10% акций компании по цене 100 руб. за акцию.')
    content = {'sections':[{'title':'Описание возможности','blocks':[
        block('Презентация апреля 2024 года подготовлена в связи с планируемым публичным предложением акций ПАО «Пример».'), offer]}]}
    assert omit_editorial_boilerplate(content)
    assert content['sections'][0]['blocks'] == [offer]


def test_catalogue_collects_supported_blocks_without_synthesizing_sections():
    from app.aso import organize_sections

    sections = [{'title': title, 'blocks': [{'block_id': block}]} for title, block in [
        ('Глоссарий', 'def'), ('Рынок', 'market'), ('Конкуренты', 'peers'),
        ('Инвестиционная привлекательность', 'thesis'), ('Бизнес', 'about')]]
    content = {'sections': deepcopy(sections)}
    requested = ['Описание Компании', 'Рынок и конкуренты', 'Акционеры', 'Инвест. тезис', 'Терминология']
    organize_sections(content, requested)
    assert [(s['title'], [b['block_id'] for b in s['blocks']]) for s in content['sections']] == [
        ('Описание Компании', ['about']), ('Рынок и конкуренты', ['market', 'peers']),
        ('Инвест. тезис', ['thesis']), ('Терминология', ['def'])]

    custom = {'sections': deepcopy(sections)}
    organize_sections(custom, ['Конкуренты', 'Глоссарий'])
    assert custom['sections'] == sections


def test_outline_covers_late_topics_and_wrapped_headings_with_a_fixed_size():
    from app.extraction_context import outline_pages

    rows = [{'page': page, 'lead': f'Розничные продукты. Выпуск {page}'} for page in range(1, 55)]
    rows.extend([{'page': 55, 'lead': '3,7\n5,9\n12 500\nСравнение долей на\nрынке платежей'},
                 {'page': 56, 'lead': 'Система управления\nСовет директоров'},
                 {'page': 57, 'lead': 'Глоссарий'},
                 {'page': 58, 'lead': 'Риски отрасли'}])
    pages = outline_pages(rows)
    assert len(pages) == 40
    assert [p['page'] for p in pages] == sorted({p['page'] for p in pages})
    assert pages[0]['page'] == 1
    by_page = {p['page']: p for p in pages}
    assert by_page[55]['possible_sections'] == ['Рынок и конкуренты']
    assert by_page[57]['possible_sections'] == ['Терминология']
    assert by_page[58]['possible_sections'] == ['Потенциальные риски']
    assert not outline_pages([rows[-3]])[0].get('possible_sections')
    assert outline_pages([{'page': 1, 'lead': '\n 1\n'}]) == []


def test_editor_keeps_disputed_value_in_its_column_without_changing_the_fact():
    saved = sample()
    saved.update(id='av_edit', content_sha256='0'*64, revision=2, created_at='2026-11-07T00:00:00Z',
                 provenance={'origin':'human_edit'})
    para, table = saved['payload']['content']['sections'][0]['blocks']
    para['block_id'], table['block_id'] = 'block_p','block_t'
    for i, item in enumerate(table['rows']):
        item['row_id'] = f'row_{i}'
    item = table['rows'][1]
    item['amount'] = dict(item['amount'], value='123456')
    warning = dict(code='row_basis_mismatch', block_id='block_t',row_id=item['row_id'],message='Сумма',severity='error')
    view = presentation(saved,[{'findings':[warning]}])['sections'][0]['blocks'][1]
    asserted = next(c for line in view['rows'] for c in line if c.get('row_id')==item['row_id'])
    assert view['headers'][0]=='Показатель' and view['headers'][1]!='Период'
    assert asserted['marked'] and asserted['text']=='123 456' and asserted['links']
    original_fact = next(f for f in saved['basis']['facts'] if f['fact_id']==item['fact_refs'][0]['fact_id'])
    assert original_fact['amount']['value']!='123456'


def test_marks_do_not_change_authorship_and_overlap_with_findings():
    from app.aso import styled_runs
    from app.versions import editorial_payload
    old={'payload':{'content':{'sections':[{'blocks':[{'block_id':'block_a','kind':'paragraph','claim':{
        'text':'Первый факт','origin':'source_fact','fact_refs':[{'fact_id':'f','source_version_id':'s'}]}}]}]}}}
    payload=deepcopy(old['payload'])
    block=payload['content']['sections'][0]['blocks'][0]
    block['marks']=[{'start':0,'end':8,'style':'both'}]
    assert editorial_payload(payload,old)['content']['sections'][0]['blocks'][0]['claim']==block['claim']
    runs=styled_runs(dict(text='Первый факт',marked=True,quotes=['факт']),block['marks'])
    assert ''.join(r['text'] for r in runs)=='Первый факт'
    assert any(r['text']=='ф' and r['highlight'] and r['italic'] and r['bold'] for r in runs)


def test_outline_finds_teaser_subheadings_in_both_columns_without_exposing_body():
    from app.extraction_context import outline_pages

    lead = 'Тизер\nО компании\nКраткая справка\n' + 'Другая строка.\n' * 90
    layout = lead + 'Описание компании   Обзор рынка:\nРиски пока не оценивались.'
    page, = outline_pages([dict(page=1, lead=layout)])
    assert 'Рынок и конкуренты' in page['possible_sections']
    assert 'Потенциальные риски' not in page['possible_sections']
    assert len(page['lead']) <= 100 and 'Другая строка' not in page['lead']


def test_aso_exports_keep_currency_in_front_of_each_amount(tmp_path):
    from app import aso_renderer
    from app.note_renderer import normalize
    from pypdf import PdfReader

    saved = sample()
    saved.update(id='av_test', content_sha256='0'*64, revision=1, created_at='2026-02-01T00:00:00Z')
    amount_line = 'Кредитные линии: ₽160 млрд. Выпуск облигаций: ₽17,5 млрд.'
    paragraph = saved['payload']['content']['sections'][0]['blocks'][0]
    paragraph.update(block_id='b1')
    paragraph['claim']['text'] = amount_line
    saved['payload']['content']['sections'][0]['blocks'] = [paragraph]
    aso_renderer.render(saved, {'theme':'standard', 'release_mode':'draft'}, [], tmp_path)
    text = ''.join(page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages)
    assert normalize(amount_line) in normalize(text)


def test_unlocated_finding_stays_in_review_and_unique_month_stays_highlighted():
    saved = sample()
    saved.update(id='av_example', content_sha256='0'*64, revision=1, created_at='2026-11-07T00:00:00Z')
    paragraph = saved['payload']['content']['sections'][0]['blocks'][0]
    paragraph.update(block_id='b1')
    paragraph['claim']['text'] = 'Решение приняли в декабре 2023 года. Заявку рассмотрели отдельно.'
    saved['payload']['content']['sections'][0]['blocks'] = [paragraph]
    finding = dict(code='unsupported_numeric_form', block_id='b1', message='Сверить период', severity='error')
    first = presentation(saved, [{'findings':[finding]}])['sections'][0]['blocks'][0]
    assert not first['rows'][0][0]['marked']
    assert first['notes'] == []
    dated = dict(finding, code='period_mismatch', message='Месяц события «декабре 2023 года» не совпадает с основанием.')
    both = presentation(saved, [{'findings':[finding, dated]}])['sections'][0]['blocks'][0]
    assert both['rows'][0][0]['quotes'] == ['декабре 2023 года']
    assert both['rows'][0][0]['marked'] and both['notes'] == first['notes']


def test_warning_source_type_has_no_highlight_but_error_quote_and_manual_marks_remain():
    saved = sample()
    saved.update(id='av_warning', content_sha256='0'*64, revision=1, created_at='2026-11-09T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    paragraph = section['blocks'][0]
    paragraph.update(block_id='block_visual', marks=[dict(start=0, end=4, style='bold'), dict(start=5, end=9, style='italic')])
    paragraph['claim'].update(text='Сеть: рост 28%.', visual_observation_ids=['visual_one'])
    saved['basis']['visual_observations'] = [dict(observation_id='visual_one', page=1,
        source=dict(source_version_id='sv_visual'), numeric=dict(amount=dict(value='28')))]
    section['blocks'] = [paragraph]
    warning = dict(code='unchecked_visual_observation', severity='warning', block_id='block_visual', message='Не проверено.')
    first = presentation(saved, [dict(findings=[warning])])['sections'][0]['blocks'][0]['rows'][0][0]
    assert not first['marked'] and first['quotes'] == []
    assert any(run['bold'] for run in first['styled_runs'])
    assert any(run['italic'] for run in first['styled_runs'])
    assert not any(run['highlight'] for run in first['styled_runs'])
    error = dict(code='quantity_mismatch', severity='error', block_id='block_visual', message='Не совпадает.', quote='28%')
    second = presentation(saved, [dict(findings=[warning, error])])['sections'][0]['blocks'][0]['rows'][0][0]
    assert second['marked'] and second['quotes'] == ['28%']
    assert any(run['highlight'] and run['text'] == '28%' for run in second['styled_runs'])


def test_marked_draft_file_holds_only_the_note_and_status_in_properties(tmp_path):
    from docx import Document
    from pypdf import PdfReader
    from app import aso_renderer
    saved = sample()
    saved.update(id='av_legend', content_sha256='0'*64, revision=3, created_at='2026-11-06T21:30:00Z')
    section = saved['payload']['content']['sections'][0]
    paragraph = section['blocks'][0]
    paragraph['block_id'] = 'block_locations'
    paragraph['claim']['text'] = 'Сеть насчитывает 17 офисов.'
    section['blocks'] = [paragraph]
    finding = dict(code='quantity_mismatch', severity='error', block_id='block_locations',
                   quote='17 офисов', message='Не совпадает количество офисов.')
    # Экран по-прежнему выделяет спорный фрагмент.
    view = presentation(saved, [{'findings':[finding]}])
    assert view['sections'][0]['blocks'][0]['rows'][0][0]['quotes'] == ['17 офисов']
    aso_renderer.render(saved, dict(release_mode='draft', theme='standard'), [{'findings':[finding]}], tmp_path)
    document = Document(tmp_path/'note.docx')
    assert not document.element.xpath('.//w:highlight')
    assert (document.core_properties.content_status, document.core_properties.version) == ('Черновик', '3')
    text = ''.join(page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages)
    assert not any(word in text for word in ('Черновик', 'версия', 'Жёлтым', 'Команда'))
    # 21:30 UTC 6 ноября по Москве уже 7 ноября.
    assert view['date'] == '7 ноября 2026 г.' and view['date'] in text


def test_historical_excerpt_keeps_quotation_formatting_and_original_payload():
    from app.facts import LEGACY_EXCERPT_PREFIX
    saved = sample()
    saved.update(id='av_old_excerpt', content_sha256='0'*64, revision=1, created_at='2026-11-07T00:00:00Z')
    section = saved['payload']['content']['sections'][0]
    block = section['blocks'][0]
    block['block_id'] = 'quote'
    text = '«В таблице 17 офисов.»'
    block['claim'].update(origin='source_excerpt', text=LEGACY_EXCERPT_PREFIX + text)
    offset = len(LEGACY_EXCERPT_PREFIX)
    block['marks'] = [dict(start=offset+1,end=offset+2,style='bold')]
    section['blocks'] = [block]
    before=deepcopy(saved)
    cell=presentation(saved, [])['sections'][0]['blocks'][0]['rows'][0][0]
    assert cell['text']==text
    assert ''.join(r['text'] for r in cell['styled_runs'] if r['bold'])=='В'
    assert saved==before


def test_generation_and_historical_reading_keep_facts_and_drop_editorial_gap():
    from app.aso import reading_paragraph

    fact = 'Компанию переименовали в «Пример».'
    suffix = 'Даты этих событий в доступных основаниях не указаны.'
    gap = 'Параметры дивидендной политики по доступным основаниям подтвердить нельзя.'
    block = {'kind': 'paragraph', 'block_id': 'b1', 'claim': {
        'origin': 'source_fact', 'text': fact + ' ' + suffix, 'fact_refs': [], 'calculation_ids': []},
        'marks': [{'start': 0, 'end': len(fact) + 4, 'style': 'bold'}]}
    empty = dict(block, block_id='b2', claim=dict(block['claim'], origin='inference', text=gap))
    content = {'title': 'Пример', 'sections': [{'title': 'Компания', 'blocks': [block]},
                                               {'title': 'Дивиденды', 'blocks': [empty]}]}
    saved = dict(id='av_example', revision=1, content_sha256='a'*64, created_at='2026-11-07T00:00:00Z',
                 payload={'content': content},
                 basis={'facts': [], 'calculations': []})
    original = deepcopy(content)
    reading = presentation(saved, [])
    assert len(reading['sections']) == 1
    cell = reading['sections'][0]['blocks'][0]['rows'][0][0]
    assert cell['text'] == fact and cell['styled_runs'][0]['text'] == fact
    assert content == original
    assert omit_editorial_boilerplate(content)
    assert len(content['sections']) == 1
    assert block['claim']['text'] == fact and block['marks'][0]['end'] == len(fact)
    for protected in ('source_excerpt', 'human_editorial', 'source_candidate', 'public_news'):
        assert reading_paragraph({'origin': protected, 'text': gap})[0] == gap
    for meaningful in ('Портфель вырос на 20%, но точную долю по доступным основаниям подтвердить нельзя.',
                       'Параметры дивидендной политики изменились, но размер выплат по доступным основаниям подтвердить нельзя.',
                       'Оценки рынка различаются из-за разного состава участников.'):
        assert reading_paragraph({'origin': 'inference', 'text': meaningful})[0] == meaningful

    candidate = 'Значения приводятся предварительно и требуют сверки с источником. Прибыль: 30 млн руб.'
    number = candidate.index('30')
    clean, marks = reading_paragraph({'origin': 'source_candidate', 'text': candidate},
                                    [{'start': number, 'end': number + 2, 'style': 'bold'}])
    assert clean == 'Значения приводятся предварительно. Прибыль: 30 млн руб.'
    assert clean[marks[0]['start']:marks[0]['end']] == '30'
    risk = 'Ожидается введение регулирования; условия будущего регулирования по доступным основаниям не ясны. Размер риска зависит от цен.'
    assert reading_paragraph({'origin': 'source_fact', 'text': risk})[0] == 'Ожидается введение регулирования. Размер риска зависит от цен.'


def test_unreferenced_prose_next_to_a_financial_table_does_not_clip_the_pdf(tmp_path):
    from app import aso_renderer
    from docx import Document
    from docx.oxml.ns import qn
    from pypdf import PdfReader

    saved = sample()
    saved.update(id='av_layout',content_sha256='0'*64,revision=1,created_at='2026-11-04T00:00:00Z')
    paragraph, table = saved['payload']['content']['sections'][0]['blocks']
    paragraph.update(block_id='one',claim=dict(origin='human_editorial',text='Отчёт для акционеров.',
                                               fact_refs=[],calculation_ids=[]))
    table['block_id'] = 'two'
    for i,row in enumerate(table['rows']):
        row['row_id'] = 'r'+str(i)
    aso_renderer.render(saved,dict(theme='standard',release_mode='draft'),[],tmp_path)
    body = list(Document(tmp_path/'note.docx').element.body)
    assert not any(a.tag == b.tag == qn('w:tbl') for a,b in zip(body,body[1:]))
    assert '4 ноября 2026 г.' in PdfReader(tmp_path/'note.pdf').pages[0].extract_text()


def test_aso_table_notation_marks_years_and_negatives_without_changing_cells():
    saved = sample()
    saved.update(id='av_notation', content_sha256='0'*64, revision=1, created_at='2026-11-07T00:00:00Z')
    table = saved['payload']['content']['sections'][0]['blocks'][1]
    table['block_id'] = 'matrix'
    saved['payload']['content']['sections'][0]['blocks'] = [table]
    facts = saved['basis']['facts']
    for fact, year, nature, value in zip(facts, (2025, 2027), ('reported_actual', 'forecast'), ('-475.00', '542')):
        fact.update(nature=nature, period=dict(kind='interval', label=str(year), start=f'{year}-01-01', end=f'{year}-12-31'))
        fact['amount']['value'] = value
    for fact, value in zip(list(facts), ('-2.5', '3.1')):
        margin = deepcopy(fact)
        margin.update(fact_id='m' + fact['fact_id'], metric='Маржа EBITDA',
                      amount=dict(value=value, unit='percent', currency=None, scale='1'))
        facts.append(margin)
    table['rows'] = [dict(row_id='r_' + f['fact_id'], label=f['metric'], period_label=f['period']['label'],
                          amount=deepcopy(f['amount']), fact_refs=[dict(source_version_id='sv_sample', fact_id=f['fact_id'])],
                          calculation_ids=[]) for f in facts]
    before = deepcopy(saved)
    view = presentation(saved, [])['sections'][0]['blocks'][0]
    assert view['headers'][1:] == ['2025Ф', '2027П']
    cells = {row[0]['text']: row[1:] for row in view['rows']}
    revenue, margin = cells['Выручка, млн руб.'], cells['Маржа EBITDA, %']
    assert [c['text'] for c in revenue] == ['(475,00)', '542'] and [c['text'] for c in margin] == ['н.п.', '3,1']
    # Обозначение относится только к отображению: ячейка сохраняет адрес строки и ссылку на страницу.
    assert [c['row_id'] for c in revenue + margin] == ['r_f1', 'r_f2', 'r_mf1', 'r_mf2']
    assert all(c['links'] for c in revenue + margin)
    assert saved == before


def test_aso_file_header_without_source_lines_and_compact_news_link(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    from pypdf import PdfReader
    from app import aso_renderer
    from app.note_news import block_id, news_text

    def text(element):
        return ''.join(t.text or '' for t in element.iter(qn('w:t')))

    saved = sample()
    # 20:59 UTC 6 ноября по Москве еще 6 ноября.
    saved.update(id='av_header', content_sha256='0'*64, revision=2, created_at='2026-11-06T20:59:00Z')
    content = saved['payload']['content']
    content['team'] = 'И. Иванов'
    saved['basis']['sources'] = [dict(source=dict(source_version_id='sv_sample'), original_filename='Отчет.pdf')]
    other = deepcopy(saved['basis']['facts'][0])
    other.update(fact_id='f_other', evidence=[dict(locator=dict(pdf_page=12), quote='Сохраненная цитата')])
    saved['basis']['facts'].append(other)

    def point(name, fact_id):
        return dict(kind='paragraph', block_id=name, claim=dict(text='Пункт ' + name + '.', origin='source_fact',
            fact_refs=[dict(source_version_id='sv_sample', fact_id=fact_id)], calculation_ids=[]))
    news = dict(url='https://example.org/news', page_sha256='a'*64, quote='Компания открыла склад.',
                published='2026-10-01', publisher='Пример-Медиа')
    saved['basis']['news_run'] = dict(items=[news])
    content['sections'] = [
        dict(title='Описание Компании', blocks=[point('a', 'f1'), point('b', 'f1'), point('c', 'f_other')]),
        dict(title='News Run', blocks=[dict(kind='paragraph', block_id=block_id(news), claim=dict(
            text=news_text(news), origin='public_news', fact_refs=[], calculation_ids=[]))])]
    aso_renderer.render(saved, dict(theme='standard', release_mode='accepted'), [], tmp_path)

    document = Document(tmp_path/'note.docx')
    header, = document.tables
    assert [text(p) for p in header._tbl.iter(qn('w:p'))] == ['  aist.', '6 ноября 2026 г.']
    lines = [text(p._p) for p in document.paragraphs if text(p._p)]
    assert lines == [content['title'], 'Команда: И. Иванов', 'Описание Компании',
                     'Пункт a.', 'Пункт b.', 'Пункт c.', 'News Run', news_text(news).replace('\n', '') + ' ссылка']
    screen = presentation(saved, [])['sections'][0]['blocks']
    assert [[(link['source'], link['page']) for link in block['rows'][0][0]['links']] for block in screen] == [
        [('sv_sample', 11)], [('sv_sample', 11)], [('sv_sample', 12)]]
    link, = document.element.iter(qn('w:hyperlink'))
    assert text(link) == 'ссылка'
    assert link.getparent() is document.paragraphs[-1]._p
    label = next(p for p in document.paragraphs if p.text == 'Описание Компании')
    assert label.style.name == 'Heading 1' and label.style.paragraph_format.keep_with_next
    assert not document.element.xpath('.//w:vMerge')
    footer = document.sections[0].footer
    assert [field.get(qn('w:instr')) for field in footer._element.iter(qn('w:fldSimple'))] == ['PAGE', 'NUMPAGES']
    assert (document.core_properties.content_status, document.core_properties.version) == ('Принятая версия', '2')
    pdf = ''.join(page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages)
    assert 'Команда: И. Иванов' in pdf and '6 ноября 2026 г.' in pdf
    assert 'Отчет.pdf' not in pdf and 'стр.' not in pdf


def test_modern_docx_embeds_complete_fonts_with_reversible_keys(tmp_path):
    from uuid import UUID
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn
    from app import aso_renderer

    document = Document()
    aso_renderer.embed_fonts(document)
    document.save(tmp_path / 'fonts.docx')
    reopened = Document(tmp_path / 'fonts.docx')
    font_table = reopened.part.part_related_by(RT.FONT_TABLE)
    table = parse_xml(font_table.blob)
    for family, style, filename in aso_renderer.FONTS:
        font = next(node for node in table if node.get(qn('w:name')) == family)
        embedded = font.find(qn('w:embed' + style))
        data = bytearray(font_table.related_parts[embedded.get(qn('r:id'))].blob)
        mask = UUID(embedded.get(qn('w:fontKey'))).bytes[::-1]
        for index in range(32):
            data[index] ^= mask[index % 16]
        assert bytes(data) == (aso_renderer.ASSETS / filename).read_bytes()


def test_file_has_no_notes_under_tables_while_screen_keeps_them(tmp_path):
    from docx import Document
    from app import aso_renderer

    def written(saved, name):
        directory = tmp_path / name
        directory.mkdir()
        aso_renderer.render(saved, dict(theme='standard', release_mode='draft'), [], directory)
        return '\n'.join(t.text or '' for t in Document(directory/'note.docx').element.xpath('.//w:t'))

    saved = sample()
    saved.update(id='av_notes', content_sha256='0'*64, revision=1, created_at='2026-11-08T00:00:00Z')
    paragraph, block = saved['payload']['content']['sections'][0]['blocks']
    paragraph['block_id'], block['block_id'] = 'intro', 'financials'
    for index, row in enumerate(block['rows']):
        row['row_id'] = f'row_{index}'
    # Решение пользователя 8 ноября: под таблицей в файле нет ни документа, ни строки основания.
    assert presentation(saved, [])['sections'][0]['blocks'][1]['notes'] == ['Спектромер, Данные презентации']
    matrix = written(saved, 'matrix')
    assert 'Сохраненная подпись таблицы' in matrix and '1Q22 (факт)' in matrix and 'Спектромер' not in matrix

    forecast = saved['basis']['facts'][1]
    forecast.update(nature='forecast', reporting_basis='Прогноз компании')
    forecast['source']['source_version_id'] = block['rows'][1]['fact_refs'][0]['source_version_id'] = 'sv_second'
    saved['basis']['sources'] = [dict(source=dict(source_version_id=source), original_filename=name, revision=1)
                                 for source, name in (('sv_sample', 'Первый.pdf'), ('sv_second', 'Второй.pdf'))]
    # Разные основания и документы строгая матрица не объединяет. Экран сохраняет основание,
    # характер данных и документ каждой строки, а файл показывает прогноз заголовком колонки.
    notes = ' '.join(presentation(saved, [])['sections'][0]['blocks'][1]['notes'])
    assert 'Прогноз компании, прогноз' in notes and 'источник 2, Второй.pdf' in notes
    mixed = written(saved, 'mixed')
    assert 'Сохраненная подпись таблицы' in mixed and '475,00' in mixed
    assert '1Q23 (прогноз)' in mixed and 'Период' not in mixed
    assert not any(part in mixed for part in ('Спектромер', 'Данные презентации', 'Прогноз компании', 'источник', '.pdf'))


def test_long_table_parts_keep_headers_on_every_page(tmp_path):
    import re
    from docx import Document
    from pypdf import PdfReader
    from app import aso_renderer

    saved = sample()
    saved.update(id='av_long', content_sha256='0'*64, revision=1, created_at='2026-11-07T00:00:00Z')
    table = saved['payload']['content']['sections'][0]['blocks'][1]
    table['block_id'] = 'long'
    template = saved['basis']['facts'][0]
    table['rows'] = []
    for index in range(60):
        fact = deepcopy(template)
        fact.update(fact_id=f'f_{index}', metric=f'Статья {index} с длинным названием для переноса строки в колонке')
        fact['amount'] = dict(fact['amount'], value=str(100 + index))
        saved['basis']['facts'].append(fact)
        table['rows'].append(dict(row_id=f'r{index}', label=fact['metric'], period_label=fact['period']['label'],
                                  amount=deepcopy(fact['amount']), calculation_ids=[],
                                  fact_refs=[dict(source_version_id='sv_sample', fact_id=fact['fact_id'])]))
    saved['payload']['content']['sections'][0]['blocks'] = [table]
    aso_renderer.render(saved, dict(theme='standard', release_mode='draft'), [], tmp_path)

    continued = [p for p in Document(tmp_path/'note.docx').element.xpath('.//w:p')
                 if ''.join(t.text or '' for t in p.xpath('.//w:t')).endswith('(продолжение)')]
    assert continued
    seen = []
    for page in (page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages):
        rows = [int(n) for n in re.findall(r'Статья (\d+) ', page)]
        assert not rows or 'Показатель' in page
        seen.extend(rows)
    assert seen == list(range(60))


def continuation_table_version(row_count, years):
    """Синтетическая матрица для разбиения по строкам и группам периодов."""
    saved = sample()
    saved.update(id='av_continuation', content_sha256='0'*64, revision=1, created_at='2026-11-07T21:15:00Z')
    intro, table = saved['payload']['content']['sections'][0]['blocks']
    intro['block_id'], table['block_id'] = 'intro', 'matrix'
    intro['claim']['text'] = ('Компания развивает производство и сеть распределительных центров.\n' * 20).strip()
    template = saved['basis']['facts'][0]
    saved['basis']['facts'], table['rows'] = [], []
    for index in range(row_count):
        for year in years:
            fact = deepcopy(template)
            fact.update(fact_id=f'f_{index}_{year}', metric=f'Статья {index} с подробным названием для переноса строки',
                        period=dict(kind='interval', label=str(year), start=f'{year}-01-01', end=f'{year}-12-31'))
            fact['amount']['value'] = str(index * 100 + year)
            saved['basis']['facts'].append(fact)
            table['rows'].append(dict(row_id=f'r_{index}_{year}', label=fact['metric'], period_label=str(year),
                amount=deepcopy(fact['amount']), fact_refs=[dict(source_version_id='sv_sample', fact_id=fact['fact_id'])],
                calculation_ids=[]))
    appendix = deepcopy(intro)
    appendix['block_id'] = 'appendix'
    appendix['claim']['text'] = 'Приложение 1. Дополнительные условия.'
    saved['payload']['content']['sections'].append(dict(title='Приложения', blocks=[appendix]))
    return saved


@pytest.mark.parametrize('row_count,years', [(40, range(2020, 2027)), (2, range(2020, 2027)),
                                            (40, [2020]), (2, [2020])])
def test_every_table_part_has_caption_and_headers(tmp_path, row_count, years):
    import re
    from collections import Counter
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Cm
    from pypdf import PdfReader
    from app import aso_renderer

    saved = continuation_table_version(row_count, years)
    before = deepcopy(saved)
    aso_renderer.render(saved, dict(theme='standard', release_mode='draft'), [], tmp_path)
    pages = [page.extract_text() for page in PdfReader(tmp_path/'note.pdf').pages]
    assert all(page.strip() for page in pages)
    assert 'Приложения' in pages[-1] and all('Приложения' not in page for page in pages[:-1])
    text = '\n'.join(pages)
    headers = text.count('Показатель')
    assert text.count('Сохраненная подпись таблицы') == headers
    for page in pages:
        assert 'Статья' not in page or 'Показатель' in page
        lines = page.splitlines()
        for index, line in enumerate(lines):
            if 'Сохраненная подпись таблицы' in line:
                assert any('Показатель' in following for following in lines[index + 1:])
    groups = 2 if len(years) == 7 else 1
    assert Counter(map(int, re.findall(r'Статья (\d+) ', text))) == Counter({i: groups for i in range(row_count)})
    doc = Document(tmp_path/'note.docx')
    assert not doc.element.xpath('.//w:tc//w:tbl')
    financial = [t for t in doc.tables if t.rows[0]._tr.xpath('./w:trPr/w:tblHeader')]
    # В общей сетке заголовки находятся внутри таблицы и не повторяются в Pages.
    assert financial and len(financial) <= headers
    captions = [t.rows[0].cells[0].text for t in financial]
    assert captions == ['Сохраненная подпись таблицы, млн руб.'] + [
        'Сохраненная подпись таблицы, млн руб. (продолжение)'] * (len(financial) - 1)
    assert text.count('(продолжение)') >= len(financial) - 1
    for table in financial:
        assert sum(col.width for col in table.columns) == pytest.approx(Cm(aso_renderer.BODY_WIDTH), abs=Cm(.01))
        indent = table._tbl.tblPr.find(qn('w:tblInd'))
        assert indent.get(qn('w:w')) == '0'
        assert table.rows[0].cells[0].paragraphs[0].paragraph_format.left_indent is None
        assert table.rows[0].cells[0].text.startswith('Сохраненная подпись таблицы')
        assert table.rows[1].cells[0].text == 'Показатель'
        for row in table.rows[:2]:
            assert row._tr.xpath('./w:trPr/w:tblHeader')
            assert row._tr.xpath('./w:trPr/w:trHeight[@w:hRule="atLeast"]')
    assert not doc.element.xpath('.//w:highlight')
    assert not doc.tables[-1].rows[-1]._tr.xpath('./w:trPr/w:trHeight[@w:val="20"][@w:hRule="exact"]')
    assert doc.core_properties.content_status == 'Черновик' and doc.core_properties.version == '1'
    original_rows = saved['payload']['content']['sections'][0]['blocks'][1]['rows']
    shown = presentation(saved, [])['sections'][0]['blocks'][1]['rows']
    assert sorted(c['row_id'] for row in shown for c in row if c['row_id']) == sorted(r['row_id'] for r in original_rows)
    assert saved == before


@pytest.mark.parametrize('header', ['9М 2025 (факт)', 'Девять месяцев\n2025 года (прогноз)'])
def test_financial_header_can_grow_without_losing_period_text(header):
    from docx import Document
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from app import aso_renderer

    doc = Document()
    headers = ['Показатель', '2023Ф', '2024Ф', header, '2025Ф']
    aso_renderer.add_financial_table(doc, headers, [[dict(text='Выручка'), dict(text='888'),
        dict(text='740'), dict(text='1 104'), dict(text='1 205')]], aso_renderer.column_widths(5), 'Сохраненная подпись')
    table = doc.tables[0]
    assert [cell.text for cell in table.rows[1].cells] == headers
    assert table.rows[1].height_rule == WD_ROW_HEIGHT_RULE.AT_LEAST
    assert table.rows[1].height.pt >= 28
    assert [cell.text for cell in table.rows[2].cells] == ['Выручка', '888', '740', '1 104', '1 205']


def test_rejected_strict_matrix_still_puts_each_row_in_its_own_period_column():
    from app import note_standard as standard
    saved = sample()
    saved.update(id='av_periods', content_sha256='0'*64, revision=2, created_at='2026-11-08T00:00:00Z',
                 provenance={'origin': 'human_edit'})
    paragraph, block = saved['payload']['content']['sections'][0]['blocks']
    paragraph['block_id'], block['block_id'] = 'intro', 'financials'
    facts = saved['basis']['facts']
    # Подпись строки переведена, а основание названо по-английски: строгая матрица отказывает всей таблице.
    for fact in facts:
        fact['metric'] = 'Net revenue'
    for index, item in enumerate(block['rows']):
        item.update(row_id=f'row_{index}', label='Чистая выручка')
    debt = deepcopy(facts[1])
    debt.update(fact_id='f_debt', metric='Net debt/EBITDA', amount=dict(value='0.3', unit='ratio', scale='1', currency=None),
                period=dict(kind='instant', label='1Q23', start='2023-03-31', end='2023-03-31'))
    conflict = deepcopy(facts[1])
    conflict.update(fact_id='f_conflict', amount=dict(facts[1]['amount'], value='800'))
    facts.extend([debt, conflict])

    def row(row_id, label, period, fact):
        return dict(row_id=row_id, label=label, period_label=period, amount=deepcopy(fact['amount']), calculation_ids=[],
                    fact_refs=[dict(source_version_id='sv_sample', fact_id=fact['fact_id'])])

    block['rows'] += [row('row_debt', 'Чистый долг/EBITDA, x', '1Q23', debt),
                      # Основание называет 1Q23, а строка 2023 (оценка): строка остается в колонке своего периода.
                      dict(row('row_estimate', 'Чистая выручка', '2023 (оценка)', facts[1]),
                           amount=dict(facts[1]['amount'], value='700')),
                      row('row_conflict', 'Чистая выручка', '1Q23', conflict)]
    by_id = {(f['source']['source_version_id'], f['fact_id']): f for f in facts}
    assert not standard.financial_projection(block, by_id)['pivoted']
    # Значение 700 вписано редактором вместо суммы основания, проверка отметила его строку.
    edited = dict(code='row_basis_mismatch', block_id='financials', row_id='row_estimate', message='Сумма', severity='error')
    before = deepcopy(saved)

    view = presentation(saved, [{'findings': [edited]}])['sections'][0]['blocks'][1]
    assert view['headers'] == ['Показатель', '1Q22 (факт)', '1Q23 (факт)', '2023 (оценка)']
    # Остаток на конец квартала стоит в колонке квартала; второе значение той же ячейки идет отдельной строкой.
    assert [[c['text'] for c in line] for line in view['rows']] == [
        ['Чистая выручка (за период), млн руб.', '475,00', '542', '-'],
        ['Чистый долг/EBITDA, x (на конец периода)', '-', '0,3', '-'],
        ['Чистая выручка, млн руб.', '-', '-', '700'],
        ['Чистая выручка (за период), млн руб.', '-', '800', '-']]
    assert [[c['row_id'] for c in line[1:]] for line in view['rows']] == [
        ['row_0', 'row_1', None], [None, 'row_debt', None], [None, None, 'row_estimate'], [None, 'row_conflict', None]]
    assert all(c['links'] for line in view['rows'] for c in line[1:] if c['row_id'])
    assert [c['row_id'] for line in view['rows'] for c in line[1:] if c['marked']] == ['row_estimate']
    assert EDITED_VALUES_NOTE in view['notes']
    assert view['caption'] == 'Сохраненная подпись таблицы' and saved == before


def test_unit_named_inside_a_label_or_caption_is_not_repeated():
    saved = sample()
    saved.update(id='av_units', content_sha256='0'*64, revision=1, created_at='2026-11-08T00:00:00Z')
    paragraph, block = saved['payload']['content']['sections'][0]['blocks']
    paragraph['block_id'], block['block_id'] = 'intro', 'financials'
    for index, (item, fact) in enumerate(zip(block['rows'], saved['basis']['facts'])):
        item.update(row_id=f'row_{index}', label='Выручка, млн руб. (МСФО)')
        fact['metric'] = item['label']
    block['caption'] = 'Выручка в млн руб. по МСФО'
    assert presentation(saved, [])['sections'][0]['blocks'][1]['caption'] == 'Выручка в млн руб. по МСФО'

    margin = deepcopy(saved['basis']['facts'][1])
    margin.update(fact_id='f_margin', metric='Маржа EBITDA', amount=dict(value='3.1', unit='percent', scale='1', currency=None))
    saved['basis']['facts'].append(margin)
    block['rows'].append(dict(row_id='row_margin', label='Маржа EBITDA', period_label='1Q23', calculation_ids=[],
                              amount=deepcopy(margin['amount']),
                              fact_refs=[dict(source_version_id='sv_sample', fact_id='f_margin')]))
    view = presentation(saved, [])['sections'][0]['blocks'][1]
    assert [line[0]['text'] for line in view['rows']] == ['Выручка, млн руб. (МСФО)', 'Маржа EBITDA, %']
