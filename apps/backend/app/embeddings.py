"""Novita Qwen embeddings with bounded requests and a durable, metered cache."""
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
import hashlib
import http.client
import json
import math
import re
import unicodedata

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .errors import ApiError
from .service import row, ident

INSTRUCTION = 'Given a question, retrieve passages from financial documents that answer the question.'
CONFIG = dict(provider='novita', model='qwen/qwen3-embedding-8b', base_url='https://api.novita.ai/openai',
              dimension=4096, weight_revision=None, text_processing='page-sentences-800-overlap-160-nfkc-whitespace.v2',
              query_instruction=INSTRUCTION, normalization='L2', similarity='Cosine', max_batch=16)
PRICING = dict(price_usd_per_million_input='0.07', pricing_checked_on='2026-10-15')
INDEX_FIELDS = ('provider', 'model', 'base_url', 'dimension', 'weight_revision', 'text_processing',
                'query_instruction', 'normalization', 'similarity')
# Keep the published D15 collections, chunk IDs and paid cache. The alias applies only
# to this exact embedding configuration, never to a changed model or text processing.
LEGACY_INDEX_IDS = {'e8aa759fbf24870c': 'qwen3_677d88a30eee8bfc'}


def index_configuration(configuration):
    return {field: configuration[field] for field in INDEX_FIELDS}


def index_id(configuration):
    identity = json.dumps(index_configuration(configuration), sort_keys=True)
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return LEGACY_INDEX_IDS.get(fingerprint, 'qwen3_' + fingerprint)


INDEX_ID = index_id(CONFIG)


def normalize(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip()


def input_text(value, query=False):
    value = normalize(value)
    return f'Instruct: {INSTRUCTION}\nQuery: {value}' if query else value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def chunks(value):
    """Sentence-aware windows, exact page offsets; never start inside a word."""
    boundaries = [match.end() for match in re.finditer(r'(?<=[.!?])\s+|\n\s*\n', value)]
    start = 0
    while start < len(value):
        end = min(start + 800, len(value))
        if end < len(value):
            sentences = [boundary for boundary in boundaries if start + 200 <= boundary <= end]
            if sentences:
                end = sentences[-1]
            else:
                words = list(re.finditer(r'\s+', value[start + 200:end]))
                if words:
                    end = start + 200 + words[-1].end()
        if normalize(value[start:end]):
            yield start, end, value[start:end]
        if end == len(value):
            break
        overlap = [boundary for boundary in boundaries if max(start + 1, end - 160) <= boundary < end]
        start = overlap[0] if overlap else end


def response_vectors(body, count):
    if not isinstance(body, dict) or body.get('model') != CONFIG['model']:
        raise ApiError('embedding_invalid_response')
    data = body.get('data')
    if not isinstance(data, list) or len(data) != count:
        raise ApiError('embedding_invalid_response')
    found = {}
    for item in data:
        if not isinstance(item, dict):
            raise ApiError('embedding_invalid_response')
        index, vector = item.get('index'), item.get('embedding')
        if type(index) is not int or not 0 <= index < count or index in found:
            raise ApiError('embedding_invalid_response')
        if not isinstance(vector, list) or len(vector) != CONFIG['dimension']:
            raise ApiError('embedding_invalid_response')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise ApiError('embedding_invalid_response')
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ApiError('embedding_invalid_response')
        found[index] = [value / norm for value in vector]
    return [found[index] for index in range(count)]


def novita_request(inputs, secret_path, *, dispatch=None):
    from .novita import credential
    try:
        token = credential(secret_path)
    except (OSError, ValueError):
        raise ApiError('embedding_unavailable') from None
    connection = http.client.HTTPSConnection('api.novita.ai', timeout=18)
    body = json.dumps(dict(model=CONFIG['model'], input=inputs, encoding_format='float'))
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': 'AIST-D15/1.0'}
    try:
        # Serialize authorization with cancel/revocation only through the bounded send.
        # Waiting for the provider response must leave the Run available to heartbeat.
        with dispatch() if dispatch is not None else nullcontext():
            connection.request('POST', '/openai/embeddings', body, headers)
        response = connection.getresponse()
        raw = response.read(4000001)
        if response.status == 429:
            raise ApiError('embedding_rate_limited')
        if response.status != 200:
            raise ApiError('embedding_unavailable')
        if len(raw) > 4000000 or token.encode() in raw:
            raise ApiError('embedding_invalid_response')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ApiError('embedding_invalid_response') from None
    except (OSError, http.client.HTTPException):
        raise ApiError('embedding_unavailable') from None
    finally:
        connection.close()


class Embeddings:
    def __init__(self, engine, settings):
        self.engine = engine
        self.settings = settings

    def configuration(self, connection):
        expected = index_configuration(CONFIG)
        connection.execute(text('''INSERT INTO aist.embedding_indexes(id,configuration)
            VALUES (:index_id,CAST(:configuration AS jsonb)) ON CONFLICT DO NOTHING'''),
            dict(index_id=INDEX_ID, configuration=json.dumps(expected)))
        saved = row(connection, 'SELECT configuration FROM aist.embedding_indexes WHERE id=:id', id=INDEX_ID)
        try:
            compatible = index_configuration(saved['configuration']) == expected
        except (KeyError, TypeError):
            compatible = False
        if not compatible:
            raise ApiError('search_index_unavailable')

    def reserve(self, connection, owner, hashes, purpose, pricing):
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('novita-authorized-total',0))"))
        exposure = connection.execute(text('''SELECT coalesce((SELECT sum(exposure) FROM aist.embedding_calls),0)+
            coalesce((SELECT sum(m.exposure) FROM aist.model_calls m JOIN aist.runs r ON r.id=m.run_id
            WHERE r.configuration->>'executor'='openclaw'),0)''')).scalar_one()
        policy = json.loads(Path(self.settings.policy_path).read_text())
        # An optional shared ceiling; each batch still reserves one cent in the ledger.
        ceiling = policy['total_authorized_usd']
        if ceiling is not None and exposure + Decimal(policy['historical_exposure_usd']) + Decimal('.01') > Decimal(ceiling):
            raise ApiError('budget_exceeded')
        call_id = ident('emb')
        connection.execute(text('''INSERT INTO aist.embedding_calls
            (id,index_id,owner_id,purpose,input_hashes,state,exposure,pricing)
            VALUES (:id,:index_id,:owner,:purpose,CAST(:hashes AS jsonb),'attempted',.01,CAST(:pricing AS jsonb))'''),
            dict(id=call_id, index_id=INDEX_ID, owner=owner, purpose=purpose,
                 hashes=json.dumps(hashes), pricing=json.dumps(pricing)))
        return call_id

    def observe(self, connection, call_id, owner, hashes, vectors, body, pricing):
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else None
        tokens = usage.get('prompt_tokens') if usage else None
        cost = None
        if type(tokens) is int and 0 <= tokens <= 1000000:
            cost = Decimal(tokens) * Decimal(pricing['price_usd_per_million_input']) / 1000000
        for input_hash, vector in zip(hashes, vectors):
            connection.execute(text('''INSERT INTO aist.embedding_cache(index_id,owner_id,input_sha256,vector,call_id)
                VALUES (:index_id,:owner,:input_hash,CAST(:vector AS jsonb),:call_id)'''),
                dict(index_id=INDEX_ID, owner=owner, input_hash=input_hash, vector=json.dumps(vector), call_id=call_id))
        connection.execute(text('''UPDATE aist.embedding_calls SET state='observed',http_status=200,
            usage=CAST(:usage AS jsonb),estimated_usd=:cost,exposure=coalesce(:cost,.01),
            observed_at=clock_timestamp() WHERE id=:id'''), dict(id=call_id, usage=json.dumps(usage), cost=cost))

    def vectors(self, owner, inputs, purpose, *, authorize=None):
        if not 1 <= len(inputs) <= CONFIG['max_batch'] or any(not value or len(value) > 1500 for value in inputs):
            raise ApiError('validation_error')
        hashes = [digest(value) for value in inputs]
        inputs_by_hash = dict(zip(hashes, inputs))
        # The owner/index lock deduplicates concurrent UI and worker requests.
        with self.engine.connect() as connection:
            lock_key = f'embeddings:{INDEX_ID}:{owner}'
            connection.execute(text('SELECT pg_advisory_lock(hashtextextended(:key,0))'), dict(key=lock_key))
            connection.commit()
            try:
                with connection.begin():
                    self.configuration(connection)
                    cached = {item['input_sha256']: item['vector'] for item in connection.execute(text('''
                        SELECT input_sha256,vector FROM aist.embedding_cache
                        WHERE index_id=:index_id AND owner_id=:owner AND input_sha256=ANY(:hashes)'''),
                        dict(index_id=INDEX_ID, owner=owner, hashes=hashes)).mappings()}
                missing = list(dict.fromkeys(input_hash for input_hash in hashes if input_hash not in cached))
                if not missing:
                    return [cached[input_hash] for input_hash in hashes]
                if not self.settings.embeddings_enabled:
                    raise ApiError('embedding_unavailable')
                pricing = dict(PRICING)
                with connection.begin():
                    if authorize is not None:
                        authorize(connection)
                    call_id = self.reserve(connection, owner, missing, purpose, pricing)

                request_started = False

                @contextmanager
                def dispatch():
                    nonlocal request_started
                    with connection.begin():
                        if authorize is not None:
                            authorize(connection)
                        request_started = True
                        yield
                        connection.execute(text("UPDATE aist.embedding_calls SET state='sent' WHERE id=:id"), dict(id=call_id))

                body = None
                try:
                    body = novita_request([inputs_by_hash[input_hash] for input_hash in missing],
                                          self.settings.novita_secret_path, dispatch=dispatch)
                    vectors = response_vectors(body, len(missing))
                except (ApiError, SQLAlchemyError) as error:
                    code = error.code if isinstance(error, ApiError) else 'outcome_unknown'
                    with connection.begin():
                        connection.execute(text('''UPDATE aist.embedding_calls SET state=:state,error_code=:code,
                            observed_at=clock_timestamp(),http_status=:status,exposure=:exposure WHERE id=:id'''),
                            dict(id=call_id, state='unknown' if request_started else 'not_sent', code=code,
                                 status=429 if code == 'embedding_rate_limited' else (200 if body is not None else None),
                                 exposure=Decimal('.01') if request_started else Decimal('0')))
                    if isinstance(error, SQLAlchemyError):
                        raise ApiError('outcome_unknown') from None
                    raise
                with connection.begin():
                    self.observe(connection, call_id, owner, missing, vectors, body, pricing)
                cached.update(zip(missing, vectors))
                return [cached[input_hash] for input_hash in hashes]
            finally:
                if connection.in_transaction():
                    connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key,0))'), dict(key=lock_key))
                connection.commit()
