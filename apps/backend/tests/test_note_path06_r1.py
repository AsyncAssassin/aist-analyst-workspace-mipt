"""Проверяемая денежная фраза, без внешнего транспорта и преобразования качественных фактов."""
from copy import deepcopy
import os

import pytest

from app.errors import ApiError
from app.extraction import candidate, extraction_request
from app.live_profile import FABLE_FULL, FABLE_FULL_ID, FABLE_VISUAL_ID, input_limit, profile_for
from test_d14 import PIN

ANNUAL_REVENUE = [
    'Меридиан-Сетка обслуживает навигационные табло на вокзалах.',
    'Переход на сервисные договоры снизил число разовых заказов.',
    'При этом компания увеличила свою выручку до',
    '3,47 млрд руб. в 2024 году.',
]


def prose(lines, *, subject='Север', metric='выручка', value='7.25', raw='7,25', unit='млн руб.', year=2021, quarter=None):
    month = quarter*3 if quarter else 12
    from calendar import monthrange
    return dict(subject=subject, metric=metric, reporting_basis='Не указано', nature='reported_actual',
                amount=dict(value=value,unit='money',currency='RUB'), value_text=raw,
                period=dict(kind='interval',label=(str(quarter)+' квартал ' if quarter else '')+str(year),
                            start=f'{year}-{month-2 if quarter else 1:02d}-01', end=f'{year}-{month:02d}-{monthrange(year,month)[1]}'),
                basis=dict(page=1,metric_label=metric,unit_label=unit,reporting_label=None),
                evidence=[dict(page=1,line_start=1,line_end=len(lines),area='Собственный абзац')])


@pytest.mark.parametrize('lines,options', [
    (ANNUAL_REVENUE, dict(subject='Меридиан-Сетка',metric='выручку',value='3.47',raw='3,47',unit='млрд руб.',year=2024)),
    (['Север: выручка выросла до 7,25 млн руб. в 2021 году, доля рынка 12%.'], {}),
    (['За 2 квартал 2022 года Маяк-Линия получила прибыль', '8,13 тыс руб. от проката сигнальных огней.'],
     dict(subject='Маяк-Линия',metric='прибыль',value='8.13',raw='8,13',unit='тыс руб.',year=2022,quarter=2)),
    *[([f'Север: выручка составила 2,200000000000000001 {unit} в 2024 году.'],
       dict(value='2.200000000000000001',raw='2,200000000000000001',unit=unit,year=2024))
      for unit in ('трлн руб.', 'трлн. руб.', 'триллиона руб.')],
])
def test_own_prose_preserves_decimal_period_and_unknown_basis(lines, options):
    item=prose(lines, **options); original=deepcopy(item)
    fact,basis=candidate(PIN,{1:'\n'.join(lines)},item)
    assert item==original and fact['amount']['value']==item['amount']['value']
    assert fact['period']==item['period'] and fact['raw_text']==item['value_text']
    assert fact['reporting_basis']=='Не указано' and 'compatibility' not in basis
    assert any('Стандарт отчетности не указан' in text for text in fact['limitations'])
    assert fact['evidence'][0]['quote']=='\n'.join(lines)
    scale = {'тыс': '1000', 'млн': '1000000', 'млрд': '1000000000'}.get(
        item['basis']['unit_label'].split()[0], '1000000000000')
    assert fact['amount']['scale'] == scale
    assert fact['amount']['currency'] == 'RUB'
    assert fact['subject'] == item['subject']


@pytest.mark.parametrize('predicate', ['составит до','не выросла до','по прогнозу выросла до','может увеличиться до',
                                        'выросла не менее чем до','составила более','составила около','составила от'])
def test_qualified_achievement_never_becomes_exact_actual(predicate):
    lines=[f'Север: выручка {predicate} 7,25 млн руб. в 2021 году.']
    with pytest.raises(ApiError):candidate(PIN,{1:lines[0]},prose(lines))


@pytest.mark.parametrize('change', ['value','sign','scale','currency','period','subject','basis','image_unit','image_period'])
def test_wrong_or_image_only_attributes_are_not_verified(change):
    lines=['Север: выручка выросла до 7,25 млн руб. в 2021 году.']
    item=prose(lines)
    if change=='value':item['amount']['value']='7.26'
    if change=='sign':item['amount']['value']='-7.25';item['value_text']='-7,25'
    if change in ('scale','image_unit'):item['basis']['unit_label']='млрд руб.'
    if change=='currency':item['amount']['currency']=None
    if change in ('period','image_period'):item['period'].update(start='2020-01-01',end='2020-12-31',label='2020')
    if change=='subject':item['subject']='Маяк-Линия';lines.append('Маяк-Линия')
    if change=='basis':item['reporting_basis']='МСФО';item['basis']['reporting_label']='МСФО';lines.append('МСФО')
    with pytest.raises(ApiError):candidate(PIN,{1:'\n'.join(lines)},item)


@pytest.mark.parametrize('text', [
    'Север: выручка выросла. Сумма 7,25 млн руб. в 2021 году.',
    'Север: выручка выросла до 7,25 млн руб. В 2021 году открыли офис.',
    'Север: выручка выросла до 7,25 млн руб. за 6 месяцев 2021 года.',
    'Север: выручка выросла до 7,25 млн руб. на 31.12.2021.',
    'Север: выручка выросла до 7,25 млн руб. в 2021П.',
    'Север: выручка выросла до 7,25 млн руб. за 1-2 квартал 2021 года.',
    'Север: выручка конкурента Юг выросла до 7,25 млн руб. в 2021 году.',
    'Север: выручка выросла до 7,25 млн руб. в 2021 году. Выручка выросла до 7,25 млн руб. в 2020 году.',
])
def test_no_sentence_mixing_competitor_or_ambiguous_number(text):
    with pytest.raises(ApiError):candidate(PIN,{1:text},prose([text]))


def test_profile_limits_preserve_historical_configuration():
    assert profile_for(dict(model_profile_version_id=FABLE_FULL_ID))==FABLE_FULL
    assert 'page_images' not in FABLE_FULL
    for identifier in [FABLE_FULL_ID, FABLE_VISUAL_ID, 'model_novita-mimo-v1']:
        inp=dict(model_profile_version_id=identifier)
        assert input_limit(inp,'agent')==600000
        assert input_limit(inp,'extraction')==(8*1024*1024 if identifier==FABLE_VISUAL_ID else 600000)
    instruction=extraction_request({1:'test'},'test')['messages'][0]['content']
    assert 'basis.reporting_label=null' in instruction and 'image-only' in instruction


def test_visual_note_window_keeps_old_pins_other_profiles_and_completion_reserve(monkeypatch):
    from app import collection
    policy = dict(max_model_calls=16, max_duration_seconds=1800)
    visual = collection.bounded_budget(policy, 'skill_analytical-note-v3', 'analytical_note', None, FABLE_VISUAL_ID)
    text = collection.bounded_budget(policy, 'skill_analytical-note-v3', 'analytical_note', None, FABLE_FULL_ID)
    novita = collection.bounded_budget(policy, 'skill_analytical-note-v3', 'analytical_note', None, 'model_novita-mimo-v1')
    dialogue = collection.bounded_budget(policy, 'skill_source-answer-v3', 'evidence_answer', None, FABLE_VISUAL_ID)
    assert visual['extraction_seconds'] == visual['send_seconds'] == 600
    assert visual['max_duration_seconds'] == 1800 and visual['completion_seconds'] == 360
    assert visual['max_model_calls'] == 16 and visual['max_tool_calls'] == 40
    assert text['extraction_seconds'] == 300 and novita['send_seconds'] == 180
    assert 'extraction_seconds' not in novita
    assert dialogue['extraction_seconds'] == 120 and dialogue['max_duration_seconds'] == 300
    old = dict(visual, extraction_seconds=300)
    run = dict(input={'budget': policy}, configuration={'collection': old})
    monkeypatch.setattr(collection, 'age_seconds', lambda current: 0)
    assert collection.send_seconds(run, 600, 'extraction') == 300
    assert run['configuration']['collection'] == old
    run['configuration']['collection'] = visual
    for elapsed, transport_limit, expected in [(0, 600, 600), (0, 240, 240), (1000, 600, 440),
                                                (1400, 600, 40), (1440, 600, 0)]:
        monkeypatch.setattr(collection, 'age_seconds', lambda current: elapsed)
        assert collection.send_seconds(run, transport_limit, 'extraction') == expected
    narrow = collection.bounded_budget(dict(max_model_calls=2, max_duration_seconds=400),
        'skill_analytical-note-v3', 'analytical_note', None, FABLE_VISUAL_ID)
    assert narrow['max_model_calls'] == 2 and narrow['max_duration_seconds'] == 400
    assert narrow['completion_calls'] == 2 and narrow['completion_seconds'] == 360


def test_other_metric_in_the_same_sentence_cannot_borrow_revenue():
    text='Север: прибыль сократилась, а выручка составила 7,25 млн руб. в 2021 году.'
    with pytest.raises(ApiError) as rejected:
        candidate(PIN,{1:text},prose([text],metric='прибыль'))
    assert rejected.value.skip_code=='ambiguous_link'
    assert candidate(PIN,{1:text},prose([text]))[0]['amount']['value']=='7.25'


def test_other_subject_in_the_same_sentence_cannot_borrow_revenue():
    text='Север разработала сервис, а Юг получила выручку 7,25 млн руб. в 2021 году.'
    with pytest.raises(ApiError) as rejected:
        candidate(PIN,{1:text},prose([text],subject='Север',metric='выручку'))
    assert rejected.value.skip_code=='ambiguous_link'
    assert candidate(PIN,{1:text},prose([text],subject='Юг',metric='выручку'))[0]['amount']['value']=='7.25'


def test_render_full_neutral_chart_footnote_and_reordered_pages(tmp_path):
    import base64
    import io
    from pathlib import Path
    from pypdf import PdfReader,PdfWriter
    from pypdf.generic import DecodedStreamObject,NameObject
    from app.pdf_images import render
    from app.live_profile import FABLE_VISUAL
    from test_perf02_r1_postgres import cyrillic_pdf
    lines=['North: revenue (million RUB)','2.4','3.8','2022','2027 P',
           '* 2027 P is forecast. Published amounts, no audit statement.']
    positions=[(70,760),(100,520),(330,570),(100,290),(330,290),(45,30)]
    data=cyrillic_pdf(lines,positions,following=[(['Second page','Separate subject'],None)])
    reader=PdfReader(io.BytesIO(data));writer=PdfWriter()
    stream=DecodedStreamObject();stream.set_data(reader.pages[0]['/Contents'].get_data()+
        b' q 0.1 0.4 0.7 rg 90 320 80 180 re f 320 320 80 230 re f Q')
    reader.pages[0][NameObject('/Contents')]=stream
    for page in reader.pages:writer.add_page(page)
    original=io.BytesIO();writer.write(original)
    reversed_writer=PdfWriter()
    for page in reversed(reader.pages):reversed_writer.add_page(page)
    reversed_data=io.BytesIO();reversed_writer.write(reversed_data)
    parameters=FABLE_VISUAL['page_images']
    images=render(original.getvalue(),[1,2],parameters,60)
    swapped=render(reversed_data.getvalue(),[1,2],parameters,60)
    assert [i['sha256'] for i in images]==[i['sha256'] for i in reversed(swapped)]
    assert [i['page'] for i in swapped]==[1,2]
    destination=Path(os.environ.get('AIST_FLOW01_EVIDENCE',tmp_path))
    (destination/'neutral-chart.jpg').write_bytes(base64.b64decode(images[0]['data']))
    (destination/'neutral-chart.pdf').write_bytes(original.getvalue())


@pytest.mark.parametrize('failure',['process','deadline'])
def test_render_failure_is_a_local_limitation_without_partial_images(monkeypatch,failure):
    import subprocess
    from app import pdf_images
    from app.live_profile import FABLE_VISUAL
    def broken(*args,**kwargs):
        if failure=='process':raise OSError('Test unavailable renderer')
        raise subprocess.TimeoutExpired('pdftoppm',1)
    monkeypatch.setattr(pdf_images.subprocess,'run',broken)
    with pytest.raises(ApiError) as refused:pdf_images.render(b'%PDF-1.4', [1,2], FABLE_VISUAL['page_images'], 1)
    assert refused.value.run_continues and 'POST не отправлен' in refused.value.body['error']['message']


@pytest.mark.parametrize('name',['Север','Севера'])
def test_payment_counterparty_is_not_the_revenue_subject(name):
    text=f'Юг получила от {name} выручку 7,25 млн руб. в 2021 году.'
    with pytest.raises(ApiError):candidate(PIN,{1:text},prose([text],subject='Север',metric='выручку'))
    assert candidate(PIN,{1:text},prose([text],subject='Юг',metric='выручку'))[0]['amount']['value']=='7.25'


def test_long_note_preserves_text_and_unsearchable_images_while_focusing_themes():
    from app.extraction_context import note_image_pages, select_pages
    pages = {p: ('Обычное раскрытие компании содержит описание продуктов и клиентов. ' * 4) for p in range(1, 26)}
    pages.update({1:'', 7:'Схема', 18:'Выручка EBITDA фактические результаты. ' + pages[2],
                  19:'Цели по выручке и EBITDA. ' + pages[2]})
    selected, _ = select_pages(pages, 'Сопоставимые финансовые показатели, EBITDA, выручка и цели')
    images = note_image_pages(selected, 'EBITDA, выручка и цели')
    assert set(selected) == set(pages) and {1, 7, 18, 19} <= set(images) < set(pages)
    assert note_image_pages({p:'' for p in pages}, 'Найти компанию') == list(pages)


def test_business_extraction_includes_market_slide_with_topic_only_in_heading():
    from app.collection import NOTE_TOPICS
    from app.extraction_context import note_image_pages
    pages = {p: ('Клиенты, продукты, конкуренты, стратегия и риски компании на рынке. ' * 4) for p in range(1, 16)}
    pages[14] = 'Размер отрасли по оценке эмитента за последние пять лет. ' * 3
    layouts = {14: 'Рынок и конкуренты\nДоля компании в продажах'}
    assert 14 in note_image_pages(pages, NOTE_TOPICS[1], layouts)


@pytest.mark.parametrize('raw,tail,value', [
    ('1,234.5', '234.5', '234.5'),
    ('1,234,567', '234,567', '234.567'),
    ('1.234,5', '234,5', '234.5'),
])
def test_punctuated_number_never_authorizes_its_tail(raw, tail, value):
    from app.source_cards import MONEY

    line = f'Север: выручка составила {raw} млн руб. в 2021 году.'
    assert not MONEY.search(line)
    with pytest.raises(ApiError) as refused:
        candidate(PIN, {1: line}, prose([line], raw=tail, value=value))
    assert refused.value.skip_code == 'number_mismatch'


@pytest.mark.parametrize('raw,value', [('1 234,5','1234.5'), ('1 234,5','1234.5'),
                                       ('−1 234.5','-1234.5'), ('+1,234','1.234')])
def test_labeled_source_number_preserves_value_and_printed_span(raw, value):
    from decimal import Decimal
    from app.source_cards import MONEY

    line = f'Север: выручка составила {raw} млн руб. в 2021 году, доля рынка 12%.'
    match = MONEY.search(line)
    assert match.span('value') == (line.index(raw), line.index(raw) + len(raw))
    item = prose([line], raw=raw, value=value)
    item['evidence'][0].update(line_start=2, line_end=2)
    fact, _ = candidate(PIN, {1: 'Справка', 2: 'Показатели компании\n' + line},
                        dict(item, evidence=[dict(item['evidence'][0], page=2)],
                             basis=dict(item['basis'], page=2)))
    assert fact['amount'] == dict(value=value, currency='RUB', scale='1000000', unit='money')
    assert Decimal(fact['amount']['value']) == Decimal(value)
    assert fact['raw_text'] == raw and fact['period'] == item['period']
    assert fact['evidence'][0]['locator']['pdf_page'] == 2 and fact['evidence'][0]['quote'] == line
