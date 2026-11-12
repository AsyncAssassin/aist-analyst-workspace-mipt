"""One ISS security, three bounded GET methods, immutable source versions."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
from urllib.parse import urlencode
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .errors import ApiError
from .normalization import checked, fingerprint, normalize
from .service import ident, row, timestamp

HOST = 'iss.moex.com'
# Far above any close price in rubles, while bounding fixed-point expansion and its copies.
MAX_PRICE_CHARS = 128
SECID = 'WUSH'
ISIN = 'RU000A105EX7'
EMITTER_ID = '13955'
EMITTER_INN = '9703103060'
METHODS = {
    'card': '/iss/securities/WUSH.json',
    'issuer': '/iss/securities.json',
    'history': '/iss/history/engines/stock/markets/shares/boards/TQBR/securities/WUSH.json',
}
LIMITATION = 'Данные MOEX ISS для внутреннего технического опыта. Биржевая цена не является выручкой компании. Проверка полей не является человеческим принятием.'


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite number')
    return json.loads(raw, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=pairs)


def rows(payload, section):
    block = payload[section]
    columns = block['columns']
    if not isinstance(columns, list) or not all(isinstance(key, str) for key in columns) or len(columns) != len(set(columns)):
        raise ValueError('invalid columns')
    result = []
    for values in block['data']:
        if not isinstance(values, list) or len(values) != len(columns):
            raise ValueError('invalid row')
        result.append(dict(zip(columns, values)))
    return result


def single(values):
    if len(values) != 1:
        raise ValueError('ambiguous or missing row')
    return values[0]


def get_iss(method, trading_date=None):
    # No caller supplies a URL, host, path or arbitrary query parameter. Redirects
    # are rejected by HTTPConnection rather than followed by a browser/opener.
    if method not in METHODS:
        raise ApiError('validation_error')
    params = {'iss.meta': 'on'}
    if method == 'issuer':
        params.update(q=SECID, **{'iss.only': 'securities'})
    if method == 'history':
        if not isinstance(trading_date, str) or date.fromisoformat(trading_date).isoformat() != trading_date:
            raise ApiError('validation_error')
        params.update({'from': trading_date, 'till': trading_date, 'iss.only': 'history'})
    path = METHODS[method] + '?' + urlencode(params)
    connection = http.client.HTTPSConnection(HOST, timeout=8)
    try:
        connection.request('GET', path, headers={'Accept': 'application/json', 'User-Agent': 'AIST-D16/1.0'})
        response = connection.getresponse()
        raw = response.read(1000001)
        if response.status != 200:
            raise ApiError('moex_unavailable')
        if len(raw) > 1000000 or 'application/json' not in response.getheader('Content-Type', ''):
            raise ApiError('moex_invalid_response')
        body = raw.decode('utf-8')
        decode(body)
        return dict(method='GET', url='https://' + HOST + METHODS[method], parameters=params,
                    request_url='https://' + HOST + path, received_at=utcnow(),
                    sha256=hashlib.sha256(raw).hexdigest(), raw_json=body)
    except (OSError, http.client.HTTPException):
        raise ApiError('moex_unavailable') from None
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError('moex_invalid_response') from None
    finally:
        connection.close()


def identify(card, issuer):
    description = rows(card, 'description')
    fields = {item['name']: item['value'] for item in description}
    if len(fields) != len(description):
        raise ValueError('duplicate description')
    security = single([item for item in rows(issuer, 'securities') if item['secid'] == SECID])
    if (fields['SECID'] != SECID or fields['ISIN'] != ISIN or fields['EMITTER_ID'] != EMITTER_ID
            or security['isin'] != ISIN or str(security['emitent_id']) != EMITTER_ID
            or security['emitent_inn'] != EMITTER_INN or security['type'] != 'common_share'
            or security['regnumber'] != fields['REGNUMBER'] or not security['emitent_title']):
        raise ApiError('moex_identity_mismatch')
    board = single([item for item in rows(card, 'boards') if item['boardid'] == 'TQBR'])
    if (board['secid'] != SECID or board['engine'] != 'stock' or board['market'] != 'shares'
            or board['currencyid'] != 'RUB' or board['is_primary'] != 1):
        raise ApiError('moex_identity_mismatch')
    trading_date = date.fromisoformat(board['history_till']).isoformat()
    return dict(secid=SECID, isin=ISIN, name=fields['NAME'], issuer=security['emitent_title'],
                emitter_id=EMITTER_ID, issuer_inn=EMITTER_INN, registration_number=fields['REGNUMBER'],
                board='TQBR', history_date=trading_date)


def normalize_price(history, trading_date):
    values = rows(history, 'history')
    if not values:
        return dict(field='LEGALCLOSEPRICE', label='Цена закрытия', value=None, currency=None,
                    currency_raw=None, trade_date=None, requested_date=trading_date,
                    unknown_reason='В ответе ISS отсутствует строка торгов за запрошенную дату.')
    observation = single(values)
    if observation['SECID'] != SECID or observation['BOARDID'] != 'TQBR' or observation['TRADEDATE'] != trading_date:
        raise ApiError('moex_identity_mismatch')
    currency = observation.get('CURRENCYID')
    if currency not in ('SUR', 'RUB', None):
        raise ValueError('unexpected currency')
    value = observation.get('LEGALCLOSEPRICE')
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError('invalid price')
        value = Decimal(value)
        if not value.is_finite() or value < 0:
            raise ValueError('invalid price')
        sign, digits, exponent = value.as_tuple()
        if len(digits) > MAX_PRICE_CHARS or abs(exponent) > MAX_PRICE_CHARS:
            raise ValueError('price exceeds fixed-point limits')
        if exponent < 0:
            length = max(len(digits), 1 - exponent) + 1  # Decimal point and leading zero if needed.
        else:
            length = 1 if value.is_zero() else len(digits) + exponent
        if length + sign > MAX_PRICE_CHARS:
            raise ValueError('price exceeds fixed-point limits')
        value = format(value, 'f')
    return dict(field='LEGALCLOSEPRICE', label='Цена закрытия', value=value,
                currency='RUB' if currency else None, currency_raw=currency,
                trade_date=trading_date, requested_date=trading_date,
                unknown_reason='Поле LEGALCLOSEPRICE отсутствует или равно null.' if value is None else
                ('Валюта торгов неизвестна.' if currency is None else None))


def fetch_snapshot(authorize):
    responses = {}
    try:
        for method in ('card', 'issuer'):
            authorize()
            responses[method] = get_iss(method)
        security = identify(decode(responses['card']['raw_json']), decode(responses['issuer']['raw_json']))
        authorize()
        responses['history'] = get_iss('history', security['history_date'])
        price = normalize_price(decode(responses['history']['raw_json']), security['history_date'])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ApiError('moex_invalid_response') from None
    normalized = normalize(dict(security=security, price=price))
    sections = snapshot_sections(normalized)
    return dict(provider='MOEX ISS', fetched_at=utcnow(), requests=responses,
                normalized=normalized, sections=sections, limitation=LIMITATION)


def snapshot_sections(normalized):
    security, price = normalized['security'], normalized['price']
    card = (f"Whoosh: {security['issuer']}. Бумага: {security['name']}. SECID {SECID}; ISIN {ISIN}; "
            f"эмитент {EMITTER_ID}, ИНН {EMITTER_INN}; регистрационный номер {security['registration_number']}. Режим TQBR.")
    value = price['value'] if price['value'] is not None else 'неизвестна'
    currency = price['currency'] or 'валюта неизвестна'
    trading_date = price['trade_date'] or 'неизвестна'
    quote = (f"Whoosh, WUSH, TQBR. Цена закрытия (history.LEGALCLOSEPRICE): {value} {currency}. "
             f"Дата торгов (TRADEDATE): {trading_date}. "
             'Это цена закрытия, а не последняя сделка (CLOSE) и не выручка компании.')
    if price['unknown_reason']:
        quote += ' ' + price['unknown_reason']
    return {'/normalized/security': card, '/normalized/price': quote}


def serialize_snapshot(snapshot):
    # Keep the original JSON strings byte-exact after UTF-8 decoding, including
    # CRLF and decomposed Unicode. Contract normalization applies only to fields.
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def snapshot_data(connection, version):
    saved = row(connection, 'SELECT payload FROM aist.moex_snapshots WHERE version_id=:id', id=version['id'])
    if not saved:
        return None, 'export_missing'
    raw = saved['payload'].encode('utf-8')
    if len(raw) != version['byte_size'] or hashlib.sha256(raw).hexdigest() != version['sha256']:
        return None, 'export_corrupt'
    return raw, None


def save_snapshot(service, connection, actor, project, snapshot):
    # Retrieval timestamps are observations, not changes in provider content.
    content_hash = fingerprint({name: dict(url=reply['url'], parameters=reply['parameters'], sha256=reply['sha256'])
                                for name, reply in snapshot['requests'].items()})
    binding = row(connection, 'SELECT * FROM aist.moex_sources WHERE project_id=:p AND owner_id=:a', p=project, a=actor)
    source = service.scoped(connection, actor, project, 'sources', binding['source_id']) if binding else None
    if source:
        current = row(connection, 'SELECT content_sha256 FROM aist.moex_snapshots WHERE version_id=:id', id=source['latest_version_id'])
        if current['content_sha256'] == content_hash:
            return source['latest_version_id'], False
    version_id, source_id = ident('sv'), source['id'] if source else ident('src')
    revision = source['revision'] + 1 if source else 1
    payload = serialize_snapshot(snapshot)
    sha = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    if not source:
        connection.execute(text('''INSERT INTO aist.sources(id,project_id,owner_id,title,latest_version_id,revision)
            VALUES (:id,:p,:a,'Whoosh: MOEX ISS',:v,1)'''), dict(id=source_id, p=project, a=actor, v=version_id))
    connection.execute(text('''INSERT INTO aist.source_versions(id,source_id,project_id,owner_id,revision,sha256,
        byte_size,original_filename,storage_name,processor_version,page_count,processing_state,media_type)
        VALUES (:v,:s,:p,:a,:revision,:sha,:size,'Whoosh-MOEX-ISS.json',NULL,'moex-iss-wush.v1',NULL,'ready','application/json')'''),
        dict(v=version_id,s=source_id,p=project,a=actor,revision=revision,sha=sha,size=len(payload.encode('utf-8'))))
    connection.execute(text('''INSERT INTO aist.moex_snapshots(version_id,content_sha256,payload,normalized,fetched_at)
        VALUES (:v,:hash,:payload,CAST(:normalized AS jsonb),:at)'''),
        dict(v=version_id,hash=content_hash,payload=payload,normalized=json.dumps(snapshot['normalized']),at=snapshot['fetched_at']))
    for section, content in snapshot['sections'].items():
        connection.execute(text('INSERT INTO aist.source_sections(version_id,sha256,section,text) VALUES (:v,:sha,:section,:text)'),
                           dict(v=version_id,sha=sha,section=section,text=content))
    connection.execute(text('''INSERT INTO aist.knowledge_materials(version_id,corpus,company,period,provenance)
        VALUES (:v,'factual','Whoosh',:period,'MOEX ISS: программная нормализация сохраненного JSON')'''),
                       dict(v=version_id,period=snapshot['normalized']['price']['trade_date']))
    if source:
        connection.execute(text('UPDATE aist.sources SET latest_version_id=:v,revision=:revision WHERE id=:s'),
                           dict(v=version_id,revision=revision,s=source_id))
    else:
        connection.execute(text('INSERT INTO aist.moex_sources(project_id,owner_id,source_id) VALUES (:p,:a,:s)'),
                           dict(p=project,a=actor,s=source_id))
    save_observations(service, connection, actor, project, version_id, snapshot)
    return version_id, True


def save_observations(service, connection, actor, project, version_id, snapshot):
    from .facts import pin_of, save_fact
    version = service.scoped(connection, actor, project, 'source_versions', version_id)
    pin = pin_of(version)
    normalized = snapshot['normalized']
    for section, metric in [('/normalized/security','Карточка бумаги MOEX'),('/normalized/price','Цена закрытия MOEX LEGALCLOSEPRICE')]:
        price = normalized['price']
        day = price['trade_date'] if section.endswith('/price') else None
        amount = None
        if day and price['value'] is not None and price['currency'] is not None:
            amount = dict(value=price['value'],unit='money',scale='1',currency=price['currency'])
        evidence = dict(evidence_id=ident('ev'),source=pin,locator=dict(json_pointer=section,area='Нормализованный раздел сохраненного JSON MOEX ISS'),
                        quote=snapshot['sections'][section])
        fact = dict(fact_id=ident('fact'),source=pin,extraction_version='moex-iss-wush.v1',metric=metric,
                    subject='ПАО «ВУШ Холдинг»',reporting_basis='MOEX ISS, TQBR',nature='reported_actual' if amount else 'source_statement',
                    period=dict(kind='instant' if day else 'unknown',label=day or 'Дата карточки не указана провайдером',start=day,end=day),
                    raw_text=evidence['quote'],amount=amount,evidence=[evidence],limitations=[LIMITATION])
        save_fact(service, connection, actor, project, pin, fact)


def status(service, actor, project):
    with service.engine.begin() as connection:
        service.scoped(connection, actor, project)
        binding = row(connection, 'SELECT * FROM aist.moex_sources WHERE project_id=:p AND owner_id=:a', p=project,a=actor)
        latest = row(connection, '''SELECT * FROM aist.moex_checks WHERE project_id=:p AND owner_id=:a AND state<>'cached'
            ORDER BY started_at DESC,id DESC LIMIT 1''', p=project,a=actor)
        successful = row(connection, '''SELECT completed_at FROM aist.moex_checks
            WHERE project_id=:p AND owner_id=:a AND state='succeeded' ORDER BY completed_at DESC LIMIT 1''', p=project,a=actor)
        version = None
        normalized = None
        if binding:
            source = service.scoped(connection,actor,project,'sources',binding['source_id'])
            version = service.version_view(connection,service.scoped(connection,actor,project,'source_versions',source['latest_version_id']))
            normalized = row(connection,'SELECT normalized FROM aist.moex_snapshots WHERE version_id=:v',v=version['id'])['normalized']
        return checked('MoexStatus', dict(version=version,normalized=normalized,
            last_success_at=timestamp(successful['completed_at']) if successful else None,
            last_error=(latest['response'].get('error') or latest['response'].get('search_error')) if latest and latest['response'] else
                ('Проверка не завершена. Повтор ключа не обращается к ISS; для новой проверки выберите обновление.' if latest else None)))


def update(service, actor, project, key, mode, *, authorize=None, settings=None):
    checked('MoexRequest', {'mode':mode})
    checked('WriteHeaders', {'Idempotency-Key':key})
    # Session-level lock spans durable attempt creation, bounded GETs and commit,
    # but releases the connection before indexing needs its own connections.
    # A crashed attempt remains visible and the same key never sends again.
    lock = 'moex:' + project
    with service.engine.connect() as connection:
        service.scoped(connection,actor,project)
        acquired = connection.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key,0))'),dict(key=lock)).scalar()
        connection.commit()
        if not acquired:
            raise ApiError('moex_busy')
        try:
            with connection.begin():
                service.scoped(connection,actor,project)
                if authorize:
                    authorize(connection)
                existing = row(connection,'SELECT * FROM aist.moex_checks WHERE project_id=:p AND owner_id=:a AND key=:key',p=project,a=actor,key=key)
                if existing:
                    if existing['mode'] != mode:
                        raise ApiError('idempotency_conflict')
                    if existing['response']:
                        return existing['response']
                    raise ApiError('outcome_unknown')
                binding = row(connection,'SELECT * FROM aist.moex_sources WHERE project_id=:p AND owner_id=:a',p=project,a=actor)
                if mode == 'get' and binding:
                    source = service.scoped(connection,actor,project,'sources',binding['source_id'])
                    previous = row(connection, '''SELECT response FROM aist.moex_checks
                        WHERE version_id=:v AND state='succeeded' ORDER BY started_at DESC,id DESC LIMIT 1''',
                        v=source['latest_version_id'])
                    response = dict(version_id=source['latest_version_id'],changed=False,error=None,
                                    search_error=previous['response'].get('search_error') if previous else None)
                    connection.execute(text('''INSERT INTO aist.moex_checks
                        (id,project_id,owner_id,key,mode,state,completed_at,version_id,response)
                        VALUES (:id,:p,:a,:key,:mode,'cached',clock_timestamp(),:v,CAST(:response AS jsonb))'''),
                        dict(id=ident('iss'),p=project,a=actor,key=key,mode=mode,v=source['latest_version_id'],response=json.dumps(response)))
                    return response
                operation = ident('iss')
                connection.execute(text('''INSERT INTO aist.moex_checks(id,project_id,owner_id,key,mode,state)
                    VALUES (:id,:p,:a,:key,:mode,'started')'''),dict(id=operation,p=project,a=actor,key=key,mode=mode))
            def authorized():
                with connection.begin():
                    service.scoped(connection,actor,project)
                    if authorize:
                        authorize(connection)
            try:
                snapshot = fetch_snapshot(authorized)
                with connection.begin():
                    if authorize:
                        authorize(connection)
                    version_id, changed = save_snapshot(service,connection,actor,project,snapshot)
                    response = dict(version_id=version_id,changed=changed,error=None,search_error=None)
                    if settings and settings.embeddings_enabled:
                        # Persist before external I/O. A crash or failed final write must
                        # leave an explicit incomplete result, never a silent success.
                        response['search_error'] = ('Снимок сохранен. Индексация не завершена. '
                            'Повтор операции не запускает ее заново; для новой попытки выберите обновление.')
                    connection.execute(text('''UPDATE aist.moex_checks SET state='succeeded',completed_at=clock_timestamp(),
                        version_id=:v,response=CAST(:response AS jsonb),observation=:observation WHERE id=:id'''),
                        dict(id=operation,v=version_id,response=json.dumps(response),observation=serialize_snapshot(snapshot)))
            except ApiError as error:
                response = dict(version_id=None,changed=False,error=error.body['error']['message'],search_error=None)
                with connection.begin():
                    connection.execute(text('''UPDATE aist.moex_checks SET state='failed',completed_at=clock_timestamp(),
                        response=CAST(:response AS jsonb) WHERE id=:id'''),dict(id=operation,response=json.dumps(response)))
                return response
        finally:
            connection.rollback()
            connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key,0))'),dict(key=lock))
            connection.commit()
    if settings and settings.embeddings_enabled:
        from .search import index_material
        try:
            index_material(service, settings, actor, project, version_id, authorize_embedding=authorize)
            response['search_error'] = None
        except (ApiError, SQLAlchemyError) as error:
            # Driver errors may contain connection details; only persist a public message.
            problem = error if isinstance(error, ApiError) else ApiError('temporarily_unavailable')
            response['search_error'] = 'Снимок сохранен. Поиск по смыслу недоступен: ' + problem.body['error']['message']
        with service.engine.begin() as connection:
            connection.execute(text('UPDATE aist.moex_checks SET response=CAST(:response AS jsonb) WHERE id=:id'),
                               dict(id=operation,response=json.dumps(response)))
    return response
