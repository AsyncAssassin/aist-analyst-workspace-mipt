"""Selected archive_calculations/fact_archive rules adapted to contract v1.

No model calls, implicit FX, rescaling, prose-number extraction or human acceptance.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from fractions import Fraction
import re
from .errors import ApiError
from .normalization import checked, normalize


def refuse(message, code='validation_failed'):
    error = ApiError(code)
    error.body['error']['message'] = message
    raise error


def known(value):
    return bool(value and value.strip().casefold() not in {
        'unknown', 'неизвестно', 'не установлено', 'не указано', 'n/a', '?', '-'})


def normalized_fact(candidate, pin):
    fact = checked('Fact', normalize(deepcopy(candidate)))
    if fact['source'] != pin or any(e['source'] != pin for e in fact['evidence']):
        refuse('Основание относится к другой версии или SHA источника.')
    if fact['amount'] is not None:
        value = fact['amount']['value']
        if len(value) > 200:
            refuse('Числовое значение слишком длинное.')
        fact['amount']['value'] = format(Decimal(value), 'f')
    period = fact['period']
    if period['kind'] != 'unknown':
        start, end = date.fromisoformat(period['start']), date.fromisoformat(period['end'])
        if start > end or (period['kind'] == 'instant' and start != end):
            refuse('Некорректные границы периода.')
    return fact


def whitespace(text):
    # evidence_verifier._normalize: whitespace only, without spelling corrections.
    return re.sub(r'\s+', ' ', text).strip()


def locate_quote(text, quote):
    return bool(whitespace(quote)) and whitespace(quote) in whitespace(text)


def growth(calculation_id, args, records):
    checked('CalculateArgs', args)
    inputs = {i['name']: i['fact'] for i in args['inputs']}
    if set(inputs) != {'previous', 'current'}:
        refuse('Нужны ровно два входа: previous и current.')
    if inputs['previous'] == inputs['current']:
        refuse('Нужны два разных периода.')
    a, b = records['current']['data'], records['previous']['data']
    if a['source']['source_version_id']!=b['source']['source_version_id']:
        refuse('Для роста нужна одна совместимая пара из одного опубликованного ряда; смешивание источников не поддержано.')
    for field, label in [('subject', 'Компания или периметр'), ('reporting_basis', 'Основа отчетности'), ('metric', 'Показатель')]:
        if not all(known(f[field]) for f in (a, b)):
            refuse(label + ': недостаточно сведений.')
        if a[field] != b[field]:
            refuse(label + ': входы несовместимы.')
    bases = [records[n]['calculation_basis'] for n in ('current', 'previous')]
    if any(not all(known(x.get(k)) for k in ('definition', 'compatibility')) for x in bases):
        refuse('Не установлены определение показателя и общая база сопоставимости.')
    if bases[0] != bases[1]:
        refuse('Различаются определения показателя или база сопоставимости.')
    if any(f['nature'] != 'reported_actual' for f in (a, b)):
        refuse('Нужны фактические данные, не прогноз или утверждение без числового факта.')
    amounts = [f['amount'] for f in (a, b)]
    if any(x is None or x['unit'] != 'money' or x['currency'] not in ('RUB', 'USD', 'EUR') for x in amounts):
        refuse('Нужны структурированные денежные значения с известной валютой и масштабом.')
    if any(amounts[0][k] != amounts[1][k] for k in ('unit', 'currency', 'scale')):
        refuse('Несовместимы единицы, валюта или масштаб денежных входов.')
    periods = [f['period'] for f in (a, b)]
    if any(p['kind'] != 'interval' for p in periods):
        refuse('Не установлен сопоставимый период потока.')
    starts = [date.fromisoformat(p['start']) for p in periods]
    ends = [date.fromisoformat(p['end']) for p in periods]
    # Retain the old closed flow-period set: quarter, half-year, 9M, year.
    allowed = {(1,1,3,31),(4,1,6,30),(7,1,9,30),(10,1,12,31),
               (1,1,6,30),(7,1,12,31),(1,1,9,30),(1,1,12,31)}
    if any((s.month,s.day,e.month,e.day) not in allowed for s,e in zip(starts,ends)):
        refuse('Для роста поддерживаются квартал, полугодие, девять месяцев или год.')
    # Calendar interval identity, not day count (leap years remain comparable).
    if any(s.year != e.year or s > e for s, e in zip(starts, ends)) or not all(
        new.year == old.year + 1 and (new.month, new.day) == (old.month, old.day)
        for new, old in (starts, ends)
    ):
        refuse('Для роста нужны одинаковые интервалы соседних лет.')
    current, previous = [Fraction(Decimal(x['value'])) for x in amounts]
    if previous == 0:
        refuse('Знаменатель равен нулю.')
    if previous < 0:
        refuse('Отрицательная база не допускает обычную интерпретацию роста.')
    exact = (current - previous) / previous * 100
    places = args['rounding']['places']
    # Exact HALF_UP at the output, avoiding a context-rounded decimal division.
    scaled = abs(exact) * 10**places
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    whole += int(2 * rest >= scaled.denominator)
    digits = str(whole).zfill(places + 1)
    rounded = (('-' if exact < 0 and whole else '') +
               (digits[:-places] + '.' + digits[-places:] if places else digits))
    limitations = list(dict.fromkeys(x for f in (b, a) for x in f['limitations']))
    limitations.append('Расчет по опубликованным значениям; точность входов не повышается. Проверка ссылок не является смысловой проверкой текста или человеческим принятием.')
    return checked('Calculation', dict(calculation_id=calculation_id,
        formula_id='growth_percent.v1', formula_display='(current - previous) / previous * 100',
        inputs=[{'name':n,'fact':inputs[n]} for n in ('previous','current')],
        exact={'numerator':str(exact.numerator),'denominator':str(exact.denominator)},
        rounded={'value':rounded,'unit':'percent','scale':'1','currency':None},
        rounding=args['rounding'], limitations=limitations))


def compare_observations(left, right):
    """Сверка представления чисел не доказывает одинаковый финансовый периметр."""
    from decimal import ROUND_HALF_UP, localcontext
    a,b=left['data'],right['data']
    refs=[dict(source_version_id=f['source']['source_version_id'],fact_id=f['fact_id']) for f in (a,b)]
    result=dict(fact_refs=refs,value_relation='not_comparable',comparability='not_established',message='')
    key=lambda s:re.sub(r'\s+','',s).casefold()
    for field,label in [('subject','компания или периметр'),('metric','показатель')]:
        if not all(known(f[field]) for f in (a,b)) or key(a[field])!=key(b[field]):
            return result|{'message':'Сопоставление не выполнено: различается или не установлен '+label+'.'}
    if any(a['period'][k]!=b['period'][k] for k in ('kind','start','end')) or a['period']['kind']=='unknown':
        return result|{'message':'Сопоставление не выполнено: различаются или не установлены периоды.'}
    amounts=[f['amount'] for f in (a,b)]
    if any(x is None or x['unit']!='money' or not x['currency'] for x in amounts) or any(amounts[0][k]!=amounts[1][k] for k in ('unit','currency')):
        return result|{'message':'Сопоставление не выполнено: различаются или не установлены валюта и единица.'}
    bases=[x.get('calculation_basis',{}) for x in (left,right)]
    comparable=(a['reporting_basis']==b['reporting_basis'] and known(a['reporting_basis']) and bases[0]==bases[1]
                and all(known(bases[0].get(k)) for k in ('definition','compatibility')))
    result['comparability']='established' if comparable else 'not_established'
    with localcontext() as ctx:
        ctx.prec=500
        values=[Decimal(x['value'])*Decimal(x['scale']) for x in amounts]
        quanta=[Decimal(10)**Decimal(x['value']).as_tuple().exponent*Decimal(x['scale']) for x in amounts]
        coarse=0 if quanta[0]>=quanta[1] else 1;fine=1-coarse
        if values[0]==values[1]:
            result.update(value_relation='equal',message='Суммы равны после явного учета масштаба. Исходные значения сохранены.')
        elif (values[fine]/quanta[coarse]).quantize(Decimal(1),rounding=ROUND_HALF_UP)*quanta[coarse]==values[coarse]:
            result.update(value_relation='rounding_consistent',message='Различие чисел согласуется с округлением до точности более крупной опубликованной единицы (HALF_UP). Это числовая сверка; исходные значения не заменяются.')
        else:
            result.update(value_relation='conflict',message='Расхождение сумм не объясняется округлением до опубликованной точности. Оба значения сохранены, выбор и усреднение не выполнялись.')
    if not comparable:result['message']+=' Одинаковые определения и периметр между документами не подтверждены. Смешивать источники для расчета роста нельзя.'
    return result
