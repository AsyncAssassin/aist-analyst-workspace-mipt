"""Number boundaries, local monetary evidence and model-only save arguments."""
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app.errors import ApiError
from app.extraction import AMBIGUOUS_CARD, candidate
from app.model_save import content_args, save_operation, failed_command_digest
from app.openclaw_runtime import Boundary, tool_schema
from app.source_cards import label_agrees, numeric_cards
from app.tools import number_tokens, check_units, check_periods
from test_d10 import PIN


@pytest.mark.parametrize('source_quote', [False, True])
def test_numbers_keep_line_and_year_boundaries(source_quote):
    text = '1 кв 2023 2023 / 2022\n271млн ₽ / мес. -1 234,50; +8,7; 31.03.2023'
    assert number_tokens(text, source_quote=source_quote) == {
        Decimal(1), Decimal(2023), Decimal(2022), Decimal(271), Decimal('-1234.50'), Decimal('8.7'), Decimal(31), Decimal(3)}
    assert number_tokens('2\n872 2023\n271', source_quote=source_quote) == {2, 872, 2023, 271}
    assert number_tokens('2\u00a0872; 12\u202f345\u202f678,9', source_quote=source_quote) == {2872, Decimal('12345678.9')}


def card_source():
    pages = {1: 'Источник: данные Synthetic на 30.06.2024\nСредняя сумма\nзайма\n6,5млн ₽\nОбщий объем\nсредств\n(на 31.03.2024)\n1 234млн ₽'}
    layouts = {1: '6,5 млн ₽                    Общий объем\nСредняя сумма                средств\nзайма                        (на 31.03.2024)\n                             1 234 млн ₽'}
    item = dict(metric=None, subject='Synthetic', reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown',label='Не указано',start=None,end=None), amount=None,value_text=None,basis=None,
                evidence=[dict(page=1,line_start=1,line_end=8,area='Карточки')])
    return pages, layouts, item


def test_cards_bind_number_unit_metric_and_own_or_page_date():
    pages,layouts,item=card_source()
    cards=numeric_cards(pages,layouts,item)
    assert len(cards)==2
    facts=[candidate(PIN,pages,c,layouts)[0] for c in cards]
    assert [(f['amount']['value'], f['period']['end']) for f in facts] == [('6.5','2024-06-30'),('1234','2024-03-31')]
    assert all(f['amount']['scale']=='1000000' for f in facts)
    for key,value in [('value','7.5'),('value','-6.5'),('currency','USD')]:
        bad=deepcopy(cards[0]);bad['amount'][key]=value
        with pytest.raises(ApiError):candidate(PIN,pages,bad,layouts)
    bad=deepcopy(cards[0]);bad['period'].update(start='2024-03-31',end='2024-03-31',label='на 31.03.2024')
    with pytest.raises(ApiError):candidate(PIN,pages,bad,layouts)
    bad=deepcopy(cards[0]);bad['period']['kind']='interval'
    with pytest.raises(ApiError):candidate(PIN,pages,bad,layouts)
    assert numeric_cards(pages,{},item)==[]
    moved={1:layouts[1].replace('Средняя сумма                ','       Средняя сумма         ')}
    assert len(numeric_cards(pages,moved,item))==1


def test_qualitative_quote_never_authorizes_arbitrary_money():
    with pytest.raises(ApiError):check_units('Сумма 1234 млн ₽', [])
    with pytest.raises(ApiError):check_periods('за 2024 год',[dict(kind='instant',label='на 30.06.2024',start='2024-06-30',end='2024-06-30')])


def content():
    return dict(title='Информационная справка',audience='Аналитики',sections=[dict(title='Обзор',blocks=[
        dict(kind='paragraph',claim=dict(text='Исходное описание.',origin='source_fact',fact_refs=[dict(source_version_id='sv_test',fact_id='fact_test')],calculation_ids=[]))])])


def run():
    return dict(id='run_test',input=dict(requested_result_type='analytical_note',expected_artifact_revision=0,target_artifact_id=None,base_version_id=None))


def test_content_schema_generates_stable_operation_and_preserves_existing_ids():
    args={'content':content()}
    Draft202012Validator(tool_schema('aist_save_artifact')).validate(args)
    original=deepcopy(args)
    first=save_operation(run(),args)
    assert args==original and first==save_operation(run(),args)
    section=first['payload']['content']['sections'][0]
    assert section['section_id'].startswith('section_') and section['blocks'][0]['block_id'].startswith('block_')
    legacy=dict(first,payload=json.dumps(first['payload']))
    assert save_operation(run(),legacy)==first
    assert failed_command_digest(legacy)==failed_command_digest(args)
    base={'payload':first['payload']}
    edit=run();edit['input'].update(target_artifact_id='art_test',base_version_id='av_test',expected_artifact_revision=4)
    revised={'content':deepcopy(first['payload']['content'])}
    revised['content']['sections'][0]['blocks'][0]['claim']['text']='Другое описание.'
    saved=save_operation(edit,revised,base)
    assert saved['expected_revision']==4 and saved['parent_version_id']=='av_test'
    assert saved['payload']['content']['sections'][0]['blocks'][0]['block_id']==section['blocks'][0]['block_id']


def test_questions_are_optional_note_arguments_and_part_of_the_save_identity():
    args = dict(content=content())
    old = save_operation(run(), args)
    from app.normalization import fingerprint
    assert old['idempotency_key'] == 'save_' + fingerprint(dict(run=run()['id'], content=args['content']))
    questions = ['Уточните определение показателя 2024 года.', 'Какие условия сделки остаются открытыми?']
    command = dict(args, questions=questions)
    saved = save_operation(run(), command)
    assert saved['questions'] == questions and 'questions' not in saved['payload']['content']
    assert saved['idempotency_key'] != old['idempotency_key']
    assert save_operation(run(), saved) == saved
    assert failed_command_digest(command) == failed_command_digest(saved)
    assert failed_command_digest(command) != failed_command_digest(args)
    Draft202012Validator(tool_schema('aist_save_artifact')).validate(command)
    assert 'questions' not in tool_schema('aist_save_artifact', 'evidence_answer')['properties']
    answer_run = deepcopy(run())
    answer_run['input']['requested_result_type'] = 'evidence_answer'
    answer = dict(claims=[], missing_information=['Нужен исходный документ.'])
    assert 'questions' not in save_operation(answer_run, dict(content=answer))
    with pytest.raises(ApiError):
        save_operation(answer_run, dict(content=answer, questions=questions))


@pytest.mark.parametrize('questions', [None, 'Вопрос', [1], [''], ['x' * 1001], ['Вопрос'] * 6])
def test_question_shape_is_bounded_without_a_semantic_validator(questions):
    with pytest.raises(ApiError):
        save_operation(run(), dict(content=content(), questions=questions))


@pytest.mark.parametrize('bad',['{"content":{},"content":{}}','{"content": NaN}','{"content":{', '[]'])
def test_string_json_is_strict(bad):
    with pytest.raises(ApiError):content_args(bad)


def test_repeated_openclaw_schema_failure_stops_before_next_model_post(tmp_path):
    stopped=[]
    w=SimpleNamespace(settings=SimpleNamespace(ready_facts_only=False),runs=SimpleNamespace(stop=lambda *args,**kwargs:stopped.append((args,kwargs))))
    r=dict(run(),epoch=1)
    boundary=Boundary(w,r,tmp_path,'token')
    error='Validation failed for tool "aist_save_artifact":\n  - content: missing sections\n\nReceived arguments:\n'+json.dumps({'content':content()})
    for i in range(2):
        boundary.issued[str(i)]=dict(name='aist_save_artifact',digest=failed_command_digest({'content':content()}))
        body={'messages':[dict(role='tool',tool_call_id=str(i),content=error)]}
        if i==0:
            feedback=boundary.save_feedback(body)
            assert 'Received arguments' not in feedback['messages'][0]['content']
            assert 'sections' in feedback['messages'][0]['content']
        else:
            with pytest.raises(ApiError):boundary.save_feedback(body)
    assert len(stopped)==1
    assert stopped[0][1]['message']=='Модель вернула документ в некорректном формате. Черновик не сохранен'


def test_monthly_card_uses_its_own_column_and_quarter():
    pages={1:'Источник: данные Synthetic на 31.03.2024\nТекущий объем\n120млн ₽ / мес.\n2023\n150млн ₽ / мес.\n1 кв. 2024\nОжидаемый рост\n2024 / 2023'}
    layout={1:'120 млн ₽ / мес.       150 млн ₽ / мес.      +50%\nТекущий объем          Текущий объем        Ожидаемый рост\n2023                   1 кв 2024            2024 / 2023'}
    item=card_source()[2];item['evidence']=[dict(page=1,line_start=1,line_end=8,area='Показатели')]
    cards=numeric_cards(pages,layout,item)
    assert {c['amount']['value'] for c in cards}=={'120','150'}
    f=next(candidate(PIN,pages,c,layout)[0] for c in cards if c['amount']['value']=='150')
    assert f['period']['kind']=='interval' and f['period']['start']=='2024-01-01' and f['period']['end']=='2024-03-31'
    assert not number_tokens(check_periods('За 1 кв. 2024',[f['period']]))
    with pytest.raises(ApiError):check_periods('За 2024 год',[f['period']])
    quarter=next(c for c in cards if c['amount']['value']=='150')
    bad=deepcopy(quarter)
    bad['period'].update(label='2023',start='2023-01-01',end='2023-12-31')
    with pytest.raises(ApiError):candidate(PIN,pages,bad,layout)
    assert not any(c['amount']['value']=='50' for c in cards)
    # The card's own quarter marker, with or without the flow preposition, names its interval; nothing else does.
    assert all(label_agrees(label,quarter) for label in ('1 кв. 2024','1кв.2024','за 1 кв. 2024'))
    assert not any(label_agrees(label,quarter) for label in ('2024-03-31','за 2024 год','1 кв. 2023','2 кв. 2024'))


def test_ready_facts_mode_blocks_extraction_before_transport_or_accounting():
    import threading
    from app.config import Settings
    from app.transport import Transport
    with pytest.raises(ValueError):Settings(ready_facts_only=True,embeddings_enabled=True)
    settings=Settings(ready_facts_only=True)
    worker_runs=SimpleNamespace(settings=settings,engine=None)
    transport=Transport(worker_runs,threading.Event(),threading.Event(),threading.RLock(),{},threading.Lock())
    with pytest.raises(ApiError) as error:transport.perform({}, {'purpose':'extraction'},request={})
    assert error.value.code=='tool_unavailable'


def test_percent_card_keeps_literal_metric_scale_and_date():
    pages={1:'Источник: данные Synthetic за 2024 год\n12,5%\nСредняя ставка\n8,5%\nСтавка фондирования'}
    layouts={1:'12,5%                 8,5%\nСредняя ставка        Ставка фондирования'}
    item=card_source()[2];item['evidence']=[dict(page=1,line_start=1,line_end=5,area='Ставки')]
    cards=numeric_cards(pages,layouts,item)
    assert {c['metric'] for c in cards}=={'Средняя ставка','Ставка фондирования'}
    for c in cards:
        fact,_=candidate(PIN,pages,c,layouts)
        assert fact['amount']['unit']=='percent' and fact['amount']['scale']=='1' and fact['amount']['currency'] is None
    bad=deepcopy(cards[0]);bad['metric']='Ставка фондирования'
    with pytest.raises(ApiError):candidate(PIN,pages,bad,layouts)


def test_nonfinancial_quantity_is_literal_and_cannot_authorize_money():
    from app.tools import check_source_quantities
    quotes=['Средний срок 2,8года. От оформления заявки 23 минуты. Более 20 тыс. активных займов.']
    assert not number_tokens(check_source_quantities('Срок 2,8 года; время 23 минуты; более 20 тыс. активных займов.',quotes))
    for bad in ['Срок 3,8 года','Время -23 минуты','Срок 23 года']:
        with pytest.raises(ApiError):check_source_quantities(bad,quotes)
    assert number_tokens(check_source_quantities('Сумма 23 млн ₽; ставка 23%.',quotes))=={23}


def test_malformed_payload_repeat_ignores_only_server_operation_fields():
    original=dict(payload='{"content":',idempotency_key='first',expected_revision=0,artifact_id=None,parent_version_id=None)
    assert failed_command_digest(original)==failed_command_digest(dict(original,idempotency_key='second'))
    assert failed_command_digest(original)!=failed_command_digest(dict(original,payload='{"content":{}'))


def test_conflicting_local_card_dates_do_not_fall_back_to_page_date():
    pages,layouts,item=card_source()
    pages[1]=pages[1].replace('(на 31.03.2024)','(на 31.03.2024)\n(на 30.06.2024)')
    layouts[1]=layouts[1].replace('                             1 234 млн ₽','                             (на 30.06.2024)\n                             1 234 млн ₽')
    item['evidence'][0]['line_end']=9
    assert {c['amount']['value'] for c in numeric_cards(pages,layouts,item)}=={'6.5'}


def repeated_cards(company='Synthetic', caption='Средняя ставка', values=('13,6', '21,4'), reverse=False):
    """Two distinct observations with a shared caption and one unambiguous neighbor."""
    blocks = [[caption, value + '%'] for value in values]
    if reverse:
        blocks.reverse()
    lines = ['Источник: данные ' + company + ' на 30.06.2024',
             *blocks[0], *blocks[1], 'Размер портфеля', '560 млн руб.']
    # Each caption is aligned with its amount; the two columns carry no instrument labels.
    layout = '\n'.join([
        blocks[0][0].ljust(35) + blocks[1][0],
        blocks[0][1].ljust(35) + blocks[1][1], '', '', 'Размер портфеля', '560 млн руб.'])
    item = dict(metric=None, subject=company, reporting_basis='Не указано', nature='source_statement',
                period=dict(kind='unknown', label='Не указано', start=None, end=None),
                amount=None, value_text=None, basis=None,
                evidence=[dict(page=1, line_start=1, line_end=len(lines), area='Показатели')])
    direct = dict(item, metric=caption, reporting_basis=lines[0], nature='reported_actual',
                  period=dict(kind='instant', label='на 30.06.2024', start='2024-06-30', end='2024-06-30'),
                  amount=dict(value=blocks[0][1][:-1].replace(',', '.'), unit='percent', currency=None),
                  value_text=blocks[0][1][:-1], table=None,
                  evidence=[dict(page=1, line_start=2, line_end=3, area='Левая карточка')])
    return {1: '\n'.join(lines)}, {1: layout}, item, direct


@pytest.mark.parametrize('company,caption,values,reverse', [
    ('Synthetic', 'Средняя ставка', ('13,6', '21,4'), False),
    ('Тестовый эмитент', 'Доходность', ('7,3', '29,8'), True),
])
def test_repeated_caption_keeps_one_qualitative_passage_and_the_clear_neighbor(company, caption, values, reverse):
    pages, layouts, item, _ = repeated_cards(company, caption, values, reverse)
    original = deepcopy(item)
    fact, basis = candidate(PIN, pages, item, layouts)
    assert fact['amount'] is None and fact['nature'] == 'source_statement' and basis == {}
    assert fact['limitations'].count(AMBIGUOUS_CARD) == 1
    cards = numeric_cards(pages, layouts, item)
    assert [(c['metric'], c['amount']['value']) for c in cards] == [('Размер портфеля', '560')]
    neighbor, _ = candidate(PIN, pages, cards[0], layouts)
    assert neighbor['amount'] == dict(value='560', unit='money', currency='RUB', scale='1000000')
    assert AMBIGUOUS_CARD not in neighbor['limitations']
    assert item == original and fact['evidence'][0]['quote'] == pages[1]


@pytest.mark.parametrize('area', ['Облигации', 'Правая карточка', 'Произвольная подпись'])
@pytest.mark.parametrize('only_amount', [False, True])
def test_direct_whole_line_and_selected_lines_cannot_hide_the_other_card(area, only_amount):
    pages, layouts, item, direct = repeated_cards()
    direct['evidence'][0]['area'] = area
    if only_amount:
        direct['evidence'][0]['line_start'] = 3
    original = deepcopy(direct)
    fact, basis = candidate(PIN, pages, direct, layouts)
    assert fact['amount'] is None and fact['nature'] == 'source_statement' and basis == {}
    assert AMBIGUOUS_CARD in fact['limitations']
    assert fact['source'] == PIN and fact['evidence'][0]['quote'].endswith('13,6%')
    assert direct == original
    item['evidence'] = [dict(page=1, line_start=2, line_end=3, area=area)]
    assert numeric_cards(pages, layouts, item) == []
    assert AMBIGUOUS_CARD in candidate(PIN, pages, item, layouts)[0]['limitations']


@pytest.mark.parametrize('defect', ['number', 'unit', 'currency', 'period', 'reference', 'structure', 'nature', 'table'])
def test_ambiguity_does_not_make_invalid_numeric_data_admissible(defect):
    pages, layouts, _, direct = repeated_cards()
    if defect == 'number':
        direct['amount']['value'] = '99'
    elif defect == 'unit':
        direct['amount']['unit'] = 'money'
    elif defect == 'currency':
        direct['amount']['currency'] = 'RUB'
    elif defect == 'period':
        direct['period'].update(label='на 30.06.2023', start='2023-06-30', end='2023-06-30')
    elif defect == 'reference':
        direct['evidence'][0]['line_end'] = 999
    elif defect == 'structure':
        direct['unexpected'] = True
    elif defect == 'nature':
        direct['nature'] = 'invented'
    else:
        direct['table'] = dict(page=1, row_line=3, year_line=2, period_line_start=1, period_line_end=1, column=1)
    with pytest.raises(ApiError):
        candidate(PIN, pages, direct, layouts)


@pytest.mark.parametrize('mode', ['single', 'periods', 'headings', 'local-labels'])
def test_duplicate_words_need_confirmed_context_not_a_blanket_ban(mode):
    header = 'Источник: данные Альфа за 2024 год'
    if mode == 'single':
        blocks = [['Средняя ставка', '13,6%']]
    elif mode == 'periods':
        blocks = [['Средняя ставка', 'за 2023 год', '13,6%'], ['Средняя ставка', 'за 2022 год', '21,4%']]
    elif mode == 'headings':
        blocks = [['Займы', 'Средняя ставка', '13,6%'], ['Кредиты', 'Средняя ставка', '21,4%']]
    else:
        blocks = [['Средняя ставка займов', '13,6%'], ['Средняя ставка кредитов', '21,4%']]
    lines = [header, *(line for block in blocks for line in block)]
    layout = '\n'.join(''.join(block[row].ljust(35) for block in blocks) for row in range(len(blocks[0])))
    item = repeated_cards(company='Альфа')[2]
    item['evidence'][0]['line_end'] = len(lines)
    pages, layouts = {1: '\n'.join(lines)}, {1: layout}
    cards = numeric_cards(pages, layouts, item)
    assert len(cards) == len(blocks)
    facts = [candidate(PIN, pages, card, layouts)[0] for card in cards]
    assert [f['amount']['value'] for f in facts] == ['13.6', '21.4'][:len(blocks)]
    assert all(f['amount']['unit'] == 'percent' and AMBIGUOUS_CARD not in f['limitations'] for f in facts)
    if mode == 'periods':
        assert [f['period']['end'] for f in facts] == ['2023-12-31', '2022-12-31']
    else:
        assert all(f['period']['end'] == '2024-12-31' for f in facts)


def test_only_competing_observations_are_removed_from_the_same_caption():
    pages, layouts, item, _ = repeated_cards()
    lines = pages[1].splitlines()[:5] + ['Средняя ставка', '(на 30.06.2023)', '9,1%']
    pages = {1: '\n'.join(lines)}
    layouts = {1: '\n'.join([
        'Средняя ставка'.ljust(35) + 'Средняя ставка'.ljust(35) + 'Средняя ставка',
        '13,6%'.ljust(35) + '21,4%'.ljust(35) + '(на 30.06.2023)',
        ' ' * 70 + '9,1%'])}
    item['evidence'][0]['line_end'] = len(lines)
    cards = numeric_cards(pages, layouts, item)
    assert [(c['amount']['value'], c['period']['end']) for c in cards] == [('9.1', '2023-06-30')]
    fact, _ = candidate(PIN, pages, cards[0], layouts)
    assert fact['amount']['value'] == '9.1' and AMBIGUOUS_CARD not in fact['limitations']
    assert AMBIGUOUS_CARD in candidate(PIN, pages, item, layouts)[0]['limitations']


def test_literal_typography_normalization_cannot_bypass_card_admission():
    pages, layouts, _, direct = repeated_cards(caption='Комиссия')
    # The existing literal attribute check treats Cyrillic м and Latin m alike.
    direct['metric'] = 'Коmиссия'
    fact, basis = candidate(PIN, pages, direct, layouts)
    assert fact['amount'] is None and basis == {} and AMBIGUOUS_CARD in fact['limitations']


def two_page_cards(company='Synthetic', caption='Средняя ставка', value='9,1', competitor=None):
    """One card on page 1; page 2 repeats the caption on two competing cards and keeps the clear neighbor.

    competitor prints a second card with the same caption and period on page 1 itself."""
    pages, layouts, _, direct = repeated_cards(company, caption)
    first = ['Источник: данные ' + company + ' на 30.06.2024', caption, value + '%']
    layout = caption + '\n' + value + '%'
    if competitor:
        first += [caption, competitor + '%']
        layout = '\n'.join([caption.ljust(35) + caption, (value + '%').ljust(35) + competitor + '%'])
    single = dict(direct, amount=dict(direct['amount'], value=value.replace(',', '.')), value_text=value,
                  evidence=[dict(page=1, line_start=3, line_end=3, area='Ставка')])
    return {1: '\n'.join(first), 2: pages[1]}, {1: layout, 2: layouts[1]}, single


@pytest.mark.parametrize('company,caption,values', [
    ('Synthetic', 'Средняя ставка', ('13,6', '21,4')),
    ('Тестовый эмитент', 'Эффективная доходность', ('7,3', '29,8')),
])
def test_short_caption_binds_the_number_to_its_own_observation(company, caption, values):
    """F01: the quoted number, not the model's caption, identifies the observation and its competitors."""
    pages, layouts, _, direct = repeated_cards(company, caption, values)
    direct['metric'] = caption.split()[-1]
    original = deepcopy(direct)
    fact, basis = candidate(PIN, pages, direct, layouts)
    assert fact['amount'] is None and fact['nature'] == 'source_statement' and basis == {}
    assert AMBIGUOUS_CARD in fact['limitations'] and fact['metric'] == direct['metric'] and direct == original
    direct['period']['label'] = '30.06.2024'
    assert candidate(PIN, pages, direct, layouts)[0]['amount'] is None
    # An admissible shortening of an unambiguous card does not forbid the numeric fact.
    pages, layouts, single = two_page_cards(company, caption)
    single['metric'] = caption.split()[-1]
    fact, _ = candidate(PIN, pages, single, layouts)
    assert fact['amount']['value'] == '9.1' and fact['nature'] == 'reported_actual' and AMBIGUOUS_CARD not in fact['limitations']


def test_equivalent_date_label_keeps_the_result_and_a_contradicting_one_is_refused():
    """F02 and R1-F01: the label may spell the confirmed date differently but may not name another period."""
    pages, layouts, _, direct = repeated_cards()
    clear_pages, clear_layouts, single = two_page_cards()
    for label in ('на 30.06.2024', '30.06.2024', '2024-06-30'):
        direct['period']['label'] = label
        fact, basis = candidate(PIN, pages, direct, layouts)
        assert fact['amount'] is None and basis == {} and AMBIGUOUS_CARD in fact['limitations']
        assert fact['period'] == direct['period']
        single['period']['label'] = label
        fact, _ = candidate(PIN, clear_pages, single, clear_layouts)
        assert fact['amount']['value'] == '9.1' and fact['period']['label'] == label and AMBIGUOUS_CARD not in fact['limitations']
    for item, item_pages, item_layouts in ((direct, pages, layouts), (single, clear_pages, clear_layouts)):
        # Another calendar date, an annual flow instead of the balance, a bare quarter or year of a
        # rate, and an impossible date all contradict the card, whatever year they share with it.
        for label in ('на 30.06.2023', '31.12.2024', '2024-12-31', 'за 2024 год', '2 кв. 2024', '2 кв. 2023', '2023 г.',
                      '2024-06-31', '31.06.2024'):
            bad = deepcopy(item)
            bad['period']['label'] = label
            with pytest.raises(ApiError) as refused:
                candidate(PIN, item_pages, bad, item_layouts)
            assert refused.value.code == 'validation_failed', label
        bad = deepcopy(item)
        bad['period'].update(label='на 31.12.2024', start='2024-12-31', end='2024-12-31')
        with pytest.raises(ApiError):
            candidate(PIN, item_pages, bad, item_layouts)
        bad = deepcopy(item)
        bad['period']['kind'] = 'interval'
        with pytest.raises(ApiError):
            candidate(PIN, item_pages, bad, item_layouts)


@pytest.mark.parametrize('company,value', [('Synthetic', '9,1'), ('Тестовый эмитент', '4,7')])
def test_additional_page_reference_keeps_the_number_on_its_own_page(company, value):
    """F03: competitors are sought where the number is printed, not on another cited page."""
    pages, layouts, single = two_page_cards(company, value=value)
    expected = value.replace(',', '.')
    assert candidate(PIN, pages, single, layouts)[0]['amount']['value'] == expected
    single['evidence'].append(dict(page=2, line_start=1, line_end=1, area='Наименование компании'))
    for order in (list, reversed):
        item = dict(single, evidence=list(order(single['evidence'])))
        original = deepcopy(item)
        fact, basis = candidate(PIN, pages, item, layouts)
        assert fact['amount']['value'] == expected and fact['nature'] == 'reported_actual' and basis == {}
        assert AMBIGUOUS_CARD not in fact['limitations'] and item == original
        assert {e['locator']['pdf_page'] for e in fact['evidence']} == {1, 2}
    # Citing only the number line does not hide a competitor on the number's own page.
    pages, layouts, single = two_page_cards(company, value=value, competitor='13,6')
    single['evidence'].append(dict(page=2, line_start=1, line_end=1, area='Наименование компании'))
    fact, basis = candidate(PIN, pages, single, layouts)
    assert fact['amount'] is None and basis == {} and AMBIGUOUS_CARD in fact['limitations']


def test_bare_year_and_quarter_labels_follow_the_card_caption_not_the_model():
    """The confirmed card decides: a bare year or quarter closes a period only under a stock caption,
    and an annual flow never names a balance, whichever caption the model sends."""
    header = 'Источник: данные Synthetic на 31.12.2024'
    blocks = [['Собственный капитал / активы', '2022 г.', '18,5%'], ['Собственный капитал / активы', '1 кв. 2023г.', '19,0%'],
              ['Рентабельность', 'за 2022 год', '12,0%']]
    lines = [header, *(line for block in blocks for line in block)]
    layout = '\n'.join(''.join(block[row].ljust(35) for block in blocks) for row in range(3))
    pages, layouts = {1: '\n'.join(lines)}, {1: layout}
    item = repeated_cards()[2]
    item['evidence'][0]['line_end'] = len(lines)
    cards = {card['value_text']: card for card in numeric_cards(pages, layouts, item)}
    assert {key: (c['period']['kind'], c['period']['start'], c['period']['end']) for key, c in cards.items()} == {
        '18,5': ('instant', '2022-12-31', '2022-12-31'), '19,0': ('instant', '2023-03-31', '2023-03-31'),
        '12,0': ('interval', '2022-01-01', '2022-12-31')}
    equivalent = {'18,5': ('2022', '2022 год', '31.12.2022', '2022-12-31', 'на 31.12.2022'),
                  '19,0': ('1 кв. 2023', '1кв.2023', 'на 31.03.2023', '2023-03-31'),
                  '12,0': ('за 2022 г.', 'за2022год')}
    contradicting = {'18,5': ('за 2022 год', '2023 г.', '3 кв. 2022', '2022-06-30'),
                     '19,0': ('за 1 кв. 2023', '2023 г.', '1 кв. 2024'),
                     '12,0': ('2022 г.', '2022', '31.12.2022', '2022-12-31', 'за 2023 год', 'за 4 кв. 2022')}
    for value_text, card in cards.items():
        for label in equivalent[value_text]:
            assert label_agrees(label, card), (value_text, label)
        for label in contradicting[value_text]:
            assert not label_agrees(label, card), (value_text, label)
    # Through candidate(): the model's shortened caption does not change the rule of the stock balances.
    for value_text in ('18,5', '19,0'):
        card = cards[value_text]
        for label in equivalent[value_text]:
            fact, _ = candidate(PIN, pages, dict(card, period=dict(card['period'], label=label), metric='капитал'), layouts)
            assert fact['amount']['value'] == card['amount']['value'] and fact['period']['label'] == label
            assert fact['nature'] == 'reported_actual' and AMBIGUOUS_CARD not in fact['limitations']
        for label in contradicting[value_text]:
            with pytest.raises(ApiError) as refused:
                candidate(PIN, pages, dict(card, period=dict(card['period'], label=label), metric='капитал'), layouts)
            assert refused.value.code == 'validation_failed' and 'подпись' in refused.value.body['error']['message'], (value_text, label)
    # An interval candidate with any other label still meets the existing interval-format check first.
    with pytest.raises(ApiError) as refused:
        candidate(PIN, pages, dict(cards['12,0'], period=dict(cards['12,0']['period'], label='за 2022 г.')), layouts)
    assert 'не поддержан' in refused.value.body['error']['message']


def test_explicit_interval_label_must_match_both_boundaries_and_the_kind():
    """One boundary control: the card's own label is left unsupported so only the rule decides."""
    def card(kind, start, end):
        return dict(metric='Выручка', period=dict(kind=kind, label='6M', start=start, end=end))
    assert label_agrees('за 2 кв. 2024', card('interval', '2024-04-01', '2024-06-30'))
    assert not label_agrees('за 2 кв. 2024', card('interval', '2024-01-01', '2024-06-30'))
    assert not label_agrees('за 2 кв. 2024', card('instant', '2024-06-30', '2024-06-30'))
    assert label_agrees('за 2024 год', card('interval', '2024-01-01', '2024-12-31'))
    assert not label_agrees('за 2024 год', card('instant', '2024-12-31', '2024-12-31'))


def test_several_printed_places_of_one_number_need_the_complete_caption():
    """The same number quoted from two different cards: only the complete literal caption may choose one."""
    header = 'Источник: данные Synthetic на 30.06.2024'
    pages = {1: '\n'.join([header, 'Средняя ставка', '9,1%']), 2: '\n'.join([header, 'Доходность', '9,1%'])}
    layouts = {1: 'Средняя ставка\n9,1%', 2: 'Доходность\n9,1%'}
    _, _, _, direct = repeated_cards()
    item = dict(direct, amount=dict(direct['amount'], value='9.1'), value_text='9,1',
                evidence=[dict(page=1, line_start=3, line_end=3, area='Ставка'), dict(page=2, line_start=3, line_end=3, area='Доходность')])
    for metric in ('Средняя ставка', 'Доходность'):
        fact, _ = candidate(PIN, pages, dict(item, metric=metric), layouts)
        assert fact['amount']['value'] == '9.1' and fact['metric'] == metric and AMBIGUOUS_CARD not in fact['limitations']
    fact, basis = candidate(PIN, pages, dict(item, metric='ставка'), layouts)
    assert fact['amount'] is None and basis == {} and AMBIGUOUS_CARD in fact['limitations']
