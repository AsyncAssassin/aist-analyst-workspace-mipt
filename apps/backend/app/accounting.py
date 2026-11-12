"""One project/Run quota for every model operation, including nested calls."""
from datetime import datetime
import time
from decimal import Decimal
from jsonschema import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from .billing_evidence import QA_SCOPE, SCOPES, invoice_target, match_refusal
from .contracts import validate_definition
from .database import temporarily_unavailable
from .errors import ApiError, error_value
from .live_profile import PROFILE, FULL_EXTRACTION_IDS, profile_for, input_limit
from .service import row, ident
from .runs import execute, json_value, now, age_seconds, TERMINAL, metered_demo

RETRY=PROFILE['automatic_retries']
# The record of a failed attempt waits for a busy Run row only this long: heartbeats and events hold it for
# milliseconds, and a longer hold leaves the attempted call to the existing recovery rather than to a wait
# that outlasts the deadline of the attempt.
RECORD_LOCK_SECONDS=0.1
OBSERVE_SECONDS = 8
OBSERVE_LOCK_SECONDS = .5
OBSERVE_PAUSE = .15


def known(value=None, reason='not observed'):
    return {'value':value,'reason':None if value is not None else reason}


def responses_cache_usage(usage):
    """Чтение и обе длительности записи входят в общее число входных токенов."""
    details = usage.get('input_tokens_details')
    if not isinstance(details, dict):
        return None
    inputs = usage.get('input_tokens')
    cached, written = details.get('cached_tokens'), details.get('cache_write_tokens')
    breakdown = details.get('cache_write_tokens_details')
    if breakdown is None and type(written) is int and written == 0:
        short, long = 0, 0
    elif isinstance(breakdown, dict) and set(breakdown) == {'ttl_30m_tokens', 'ttl_12h_tokens'}:
        short, long = breakdown['ttl_30m_tokens'], breakdown['ttl_12h_tokens']
    else:
        return None
    if (any(type(value) is not int or not 0 <= value <= 9007199254740991
            for value in (inputs, cached, written, short, long))
            or short + long != written or cached + written > inputs):
        return None
    return cached, short, long


def response_fields(payload, *, live=False, native=False, protocol=None):
    """Normalize untrusted, completely received JSON before a DB transaction."""
    body=payload if isinstance(payload,dict) else {}
    completion=body.get('id')
    valid_id=completion is None or (isinstance(completion,str) and 1<=len(completion)<=200)
    usage=body.get('usage')
    valid_container=isinstance(usage,dict)
    usage=usage if valid_container else {}
    if protocol == 'https_openai_responses':
        from .openai_responses import valid_content
        fields = dict(input=usage.get('input_tokens'), output=usage.get('output_tokens'))
        details = usage.get('input_tokens_details')
        output_details = usage.get('output_tokens_details')
        fields['cached_input'] = details.get('cached_tokens') if isinstance(details, dict) else None
        cache = responses_cache_usage(usage)
        fields['cache_write_30m_input'] = cache[1] if cache else None
        fields['cache_write_12h_input'] = cache[2] if cache else None
        fields['reasoning_output'] = output_details.get('reasoning_tokens') if isinstance(output_details, dict) else None
        tokens = {key: known(value if type(value) is int and 0 <= value <= 9007199254740991 else None,
                             'provider omitted or returned invalid usage') for key, value in fields.items()}
        for field, total in (('cached_input', 'input'), ('reasoning_output', 'output')):
            value = tokens[field]['value']
            if value is not None and (tokens[total]['value'] is None or value > tokens[total]['value']):
                tokens[field] = known(reason='invalid usage breakdown')
        valid_usage = tokens['input']['value'] is not None and tokens['output']['value'] is not None
        return completion if valid_id else None, tokens, bool(completion) and valid_id and valid_usage and valid_content(body)
    if native:
        from .anthropic_messages import valid_content
        fields = {'input': usage.get('input_tokens'), 'output': usage.get('output_tokens'),
                  'cached_input': usage.get('cache_read_input_tokens'), 'reasoning_output': None}
        output_details = usage.get('output_tokens_details')
        if isinstance(output_details, dict):
            thinking = output_details.get('thinking_tokens')
            if type(thinking) is int and type(fields['output']) is int and 0 <= thinking <= fields['output']:
                fields['reasoning_output'] = thinking
        creation = usage.get('cache_creation')
        created = usage.get('cache_creation_input_tokens')
        if isinstance(creation, dict):
            fields.update(cache_write_5m_input=creation.get('ephemeral_5m_input_tokens'),
                          cache_write_1h_input=creation.get('ephemeral_1h_input_tokens'))
        else:
            fields.update(cache_write_5m_input=0 if created == 0 else None, cache_write_1h_input=0 if created == 0 else None)
        tokens = {field: known(value if type(value) is int and 0 <= value <= 9007199254740991 else None,
                               'provider omitted or returned invalid usage') for field, value in fields.items()}
        short, long = tokens['cache_write_5m_input']['value'], tokens['cache_write_1h_input']['value']
        if short is not None and long is not None and (type(created) is not int or short + long != created):
            tokens['cache_write_5m_input'] = tokens['cache_write_1h_input'] = known(reason='cache creation breakdown unavailable')
        valid_usage = all(tokens[key]['value'] is not None for key in ('input', 'output'))
        return completion if valid_id else None, tokens, bool(completion) and valid_id and valid_usage and valid_content(body)
    tokens={}
    for field,source in (('input','prompt_tokens'),('output','completion_tokens')):
        value=usage.get(source)
        valid=type(value) is int and 0<=value<=9007199254740991
        reason='provider omitted usage' if valid_container and source not in usage else 'provider returned invalid usage'
        tokens[field]=known(value if valid else None,reason)
    for field,base in (('cached_input','input'),('reasoning_output','output')):
        detail, member = ('prompt_tokens_details','cached_tokens') if field=='cached_input' else ('completion_tokens_details','reasoning_tokens')
        nested=usage.get(detail)
        value=usage.get(field, nested.get(member) if isinstance(nested,dict) else None);limit=tokens[base]['value']
        valid=type(value) is int and limit is not None and 0<=value<=limit
        reason='provider omitted usage' if valid_container and field not in usage else 'provider returned invalid usage'
        tokens[field]=known(value if valid else None,reason)
    valid_usage=all(tokens[field]['value'] is not None for field in ('input','output'))
    choices=body.get('choices')
    live_body=bool(isinstance(choices,list) and len(choices)==1 and isinstance(choices[0],dict)
                   and isinstance(choices[0].get('message'),dict) and choices[0].get('finish_reason') in ('stop','tool_calls'))
    valid_body=isinstance(payload,dict) and (live_body if live else body.get('synthetic') is True) and valid_id and valid_container and body.get('error') is None
    return completion if valid_id else None,tokens,valid_body and valid_usage


def responses_estimate(body, policy):
    """Резерв снимается только при полном usage по закрепленному тарифу Standard."""
    if not isinstance(body, dict) or body.get('service_tier') != 'default':
        return None
    usage = body.get('usage')
    if not isinstance(usage, dict):
        return None
    inputs, outputs = usage.get('input_tokens'), usage.get('output_tokens')
    if any(type(value) is not int or not 0 <= value <= 9007199254740991 for value in (inputs, outputs)):
        return None
    if 'total_tokens' in usage and (type(usage['total_tokens']) is not int or usage['total_tokens'] != inputs + outputs):
        return None
    details = usage.get('input_tokens_details', {})
    output_details = usage.get('output_tokens_details', {})
    if not isinstance(details, dict) or not isinstance(output_details, dict):
        return None
    cached, short, long = 0, 0, 0
    if policy.get('pricing_version') == 'openai-astra.standard.cache.v2':
        cache = responses_cache_usage(usage)
        if cache is None:
            return None
        cached, short, long = cache
    elif policy.get('pricing_version') == 'openai-astra.standard.no-cache.v1':
        for field in ('cached_tokens', 'cache_write_tokens'):
            if field in details and (type(details[field]) is not int or details[field] != 0):
                return None
    else:
        return None
    if 'reasoning_tokens' in output_details:
        reasoning = output_details['reasoning_tokens']
        if type(reasoning) is not int or not 0 <= reasoning <= outputs:
            return None
    prefix = 'long_context_' if inputs > policy['long_context_threshold_tokens'] else ''
    value = (Decimal(inputs - cached - short - long) * Decimal(policy[prefix + 'input_per_million'])
             + Decimal(outputs) * Decimal(policy[prefix + 'output_per_million']))
    if policy['pricing_version'] == 'openai-astra.standard.cache.v2':
        value += (Decimal(cached) * Decimal(policy[prefix + 'cache_read_per_million'])
                  + Decimal(short) * Decimal(policy[prefix + 'cache_write_30m_per_million'])
                  + Decimal(long) * Decimal(policy[prefix + 'cache_write_12h_per_million']))
    return format(value / Decimal(1000000), 'f')


def response_cost(run, payload, tokens, valid_response, *, native, responses):
    inputs,outputs=tokens['input']['value'],tokens['output']['value']
    estimate=None
    if inputs is not None and outputs is not None:
        p=run['configuration']['policy']
        estimate=format((Decimal(inputs)*Decimal(p['input_per_million'])+Decimal(outputs)*Decimal(p['output_per_million']))/Decimal(1000000),'f')
        if responses:
            estimate = responses_estimate(payload, p)
        if native:
            caches = [('cached_input','cache_read_per_million'), ('cache_write_5m_input','cache_write_5m_per_million'),
                      ('cache_write_1h_input','cache_write_1h_per_million')]
            if all(tokens[field]['value'] is not None and (tokens[field]['value'] == 0 or price in p)
                   for field, price in caches):
                extra = sum(Decimal(tokens[field]['value']) * Decimal(p[price]) for field, price in caches
                            if tokens[field]['value'])
                estimate = format(Decimal(estimate) + extra / Decimal(1000000), 'f')
            else:
                estimate = None
    if responses and valid_response:
        valid_response = payload.get('model') == profile_for(run['input'])['model'] and estimate is not None
    return estimate, valid_response


class OverloadRetry:
    """A proposed identical send after an overload refusal; observe() publishes the granted pause."""
    def __init__(self,retry_after,jitter):
        self.retry_after=retry_after;self.jitter=jitter;self.pause=None


def overload_refusal(status,payload):
    """A completely received HTTP 429 typed server_overload at the root or in error, without any generation."""
    if status!=RETRY['http_status'] or not isinstance(payload,dict) or payload.get('choices') is not None:return False
    error=payload.get('error')
    kind=payload.get('type') if 'type' in payload else error.get('type') if isinstance(error,dict) else None
    return kind==RETRY['error_type']


def retry_history(c,run):
    """Retries already sent in this Run: each call whose parent is a recorded overload refusal."""
    rows=execute(c,'''SELECT m.data AS retry,p.data AS refused FROM aist.model_calls m
        JOIN aist.model_calls p ON p.id=m.parent_model_call_id AND p.run_id=m.run_id
        WHERE m.run_id=:r AND p.data->>'http_status'=:status AND p.data->>'provider_outcome'='error'
        ORDER BY m.created_at,m.id''',r=run,status=str(RETRY['http_status'])).mappings()
    return [(v['retry'],v['refused']) for v in rows]


def attempt_counts(c,run):
    """Ordinary sends and overload retries of a Run, counted from saved calls only."""
    total=execute(c,'SELECT count(*) FROM aist.model_calls WHERE run_id=:r',r=run).scalar_one()
    retries=len(retry_history(c,run))
    return total-retries,retries


def extraction_counts(c,run):
    """Logical extractions (first sends) and their overload retries of a Run, from saved calls only."""
    total=execute(c,"SELECT count(*) FROM aist.model_calls WHERE run_id=:r AND data->>'purpose'='extraction'",r=run).scalar_one()
    retries=sum(1 for retry,_ in retry_history(c,run) if retry['purpose']=='extraction')
    return total-retries,retries


def moment(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def limit_lock_wait(c,seconds):
    """Row locks of this transaction are waited for at most this long: PostgreSQL lock_timeout, local to it."""
    execute(c,"SET LOCAL lock_timeout='"+str(max(1,int(seconds*1000)))+"'")


def oversized_error(size, limit=PROFILE['input_bytes']):
    """The error of a ModelCall whose request composed for the completion mode exceeds the input limit."""
    return dict(error_value('budget_exceeded'),message='Запрос к модели с контекстом завершения превышает лимит размера ('
        +str(size)+' байт при лимите '+str(limit)+' байт): POST не отправлен, результат не сохранен.')


def waited_seconds(history):
    return sum((moment(retry['attempted_at'])-moment(refused['observed_at'])).total_seconds() for retry,refused in history)


def retry_pause(used,retry_after,jitter):
    """Exponential backoff with jitter before retry number used+1; a valid Retry-After only lengthens it."""
    if used>=RETRY['max_per_run']:return None
    pause=RETRY['base_seconds'][used]*(1+RETRY['jitter_ratio']*jitter)
    return max(pause,retry_after) if retry_after is not None else pause


def exposure(call, usage):
    state = call['transport_state']
    if state in ('reserved', 'attempted', 'sent'):
        return Decimal(usage['reserved_usd'])
    billed = usage['billed_usd']['value']
    if billed is not None:
        return Decimal(billed)
    if state == 'unknown':
        return Decimal(usage['reserved_usd'])
    estimated = usage['estimated_usd']['value']
    return Decimal(estimated if estimated is not None else usage['reserved_usd'])


def lock_scope(c, scope):
    if scope in SCOPES or scope == QA_SCOPE:
        execute(c, 'SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))', scope=scope)


def money_available(c, run, quotes):
    """Use the common ledger lock for admission and send reservations, including unknown calls.

    quotes counts only amounts not yet in model_calls. At the POST boundary the current
    attempt is already reserved; an extraction still needs one additional completion quote.
    """
    policy = run['configuration']['policy']
    scope = policy.get('budget_scope')
    amount = Decimal(policy['quote_usd']) * quotes
    if metered_demo(policy) or (scope in SCOPES and policy['total_authorized_usd'] is not None):
        lock_scope(c, scope)
        if metered_demo(policy):
            # Reserve and record every transport as usual; only monetary admission is disabled.
            return True
        common = execute(c, """SELECT coalesce(sum(m.exposure),0) FROM aist.model_calls m
            JOIN aist.runs r ON r.id=m.run_id WHERE r.configuration->>'executor'='openclaw'
            AND r.configuration->'policy'->>'budget_scope'=:scope""", scope=scope).scalar_one()
        if scope == 'novita-authorized-total':
            common += execute(c, 'SELECT coalesce(sum(exposure),0) FROM aist.embedding_calls').scalar_one()
        if common + Decimal(policy['historical_exposure_usd']) + amount > Decimal(policy['total_authorized_usd']):
            return False
    total = execute(c, 'SELECT coalesce(sum(exposure),0) FROM aist.model_calls WHERE project_id=:p', p=run['project_id']).scalar_one()
    local = execute(c, 'SELECT coalesce(sum(exposure),0) FROM aist.model_calls WHERE run_id=:r', r=run.get('id')).scalar_one()
    return total + amount <= Decimal(policy['project_limit']) and local + amount <= Decimal(run['input']['budget']['limit'])


class Accounting:
    def __init__(self,runs):self.runs,self.engine=runs,runs.engine

    def require_settled(self, c, run, current_call=None):
        """No SDK recovery or a second context may repeat an unsettled Astra send."""
        if profile_for(run['input'])['transport'] != 'https_openai_responses':
            return
        pending = row(c, """SELECT id FROM aist.model_calls WHERE run_id=:r
            AND (:current IS NULL OR id<>:current)
            AND data->>'transport_state' IN ('reserved','attempted','sent','unknown') LIMIT 1""",
            r=run['id'], current=current_call)
        if pending:
            raise ApiError('outcome_unknown')

    def usage(self,c,r,call,*,tokens=None,estimate=None,billed=None,kind='reservation',released=False,reason='not observed',uid=None,reserved=None,settlement=None):
        previous=call.get('usage_entry_id')
        old=row(c,'SELECT data FROM aist.usage_entries WHERE id=:id',id=previous) if previous else None
        old=old['data'] if old else None
        if reserved is not None:
            # An operator may refine an unknown call's upper bound from its exact
            # request and a provider token count. This is not observed usage.
            validate_definition('UnsignedDecimal', reserved)
            if (kind != 'reconciliation' or released or tokens is not None or estimate is not None or billed is not None
                    or profile_for(r['input'])['transport'] != 'https_openai_responses'
                    or call['transport_state'] != 'unknown' or old is None or old['reservation_state'] != 'held'
                    or not Decimal('0') < Decimal(reserved) <= Decimal(old['reserved_usd'])):
                raise ApiError('validation_error')
        if billed is not None and (kind != 'provider_invoice' or settlement is None):
            raise ApiError('validation_error')
        reservation = reserved if reserved is not None else (
            old['reserved_usd'] if old else r['configuration']['policy']['quote_usd'])
        token_values=tokens or (old['tokens'] if old else {k:known(reason=reason) for k in ('input','output','cached_input','reasoning_output')})
        value=dict(id=uid or ident('usage'),run_id=r['id'],model_call_id=call['id'],supersedes_id=previous,
            tokens=token_values,estimated_usd=known(estimate,reason) if estimate is not None or not old or kind=='provider_response' else old['estimated_usd'],
            billed_usd=known(billed,'per-call billed not available') if billed is not None or not old else old['billed_usd'],
            pricing_version=r['configuration']['policy']['pricing_version'],reserved_usd=reservation,
            reservation_state='released' if released or billed is not None or (old and old['billed_usd']['value'] is not None)
            else 'held',evidence_kind=kind,created_at=now())
        validate_definition('UsageEntry',value)
        if settlement is not None:
            definition = 'OperatorBillingEvidence' if kind == 'provider_invoice' else 'PolicyRefusalEvidence'
            value['settlement'] = validate_definition(definition, settlement)
        execute(c,'''INSERT INTO aist.usage_entries(id,run_id,model_call_id,supersedes_id,data)
          VALUES (:id,:r,:call,:prev,CAST(:v AS jsonb))''',id=value['id'],r=r['id'],call=call['id'],prev=previous,v=json_value(value))
        call['usage_entry_id']=value['id']
        self.runs.event(c,r,'usage_recorded',{'usage_entry_id':value['id']},call=call['id'])
        return value

    def save_call(self,c,call,usage):
        validate_definition('ModelCall',call)
        policy = execute(c, "SELECT configuration->'policy' FROM aist.runs WHERE id=:r", r=call['run_id']).scalar_one()
        lock_scope(c, policy.get('budget_scope'))
        previous=row(c,'SELECT data FROM aist.model_calls WHERE id=:id',id=call['id'])
        if previous and previous['data']['transport_state']!=call['transport_state']:
            execute(c,"INSERT INTO aist.model_transport_events(call_id,seq,state) SELECT :id,coalesce(max(seq),0)+1,:state FROM aist.model_transport_events WHERE call_id=:id",id=call['id'],state=call['transport_state'])
        execute(c,'UPDATE aist.model_calls SET data=CAST(:data AS jsonb),usage_entry_id=:u,exposure=:cost WHERE id=:id',
            data=json_value(call),u=call['usage_entry_id'],cost=exposure(call,usage),id=call['id'])

    def reserve(self,run,epoch,purpose='agent',parent_model=None,parent_tool=None,*,retry_of=None,extraction=None,deadline=None):
        cid=ident('call')
        c=self.engine.connect();tx=c.begin();committing=False
        try:
            if deadline is not None:
                if time.monotonic() >= deadline:
                    raise ApiError('budget_exceeded')
                limit_lock_wait(c, deadline - time.monotonic())
            r=self.runs.locked(c,run);self.runs.fence(c,r,epoch)
            self.require_settled(c, r)
            policy=r['configuration']['policy'];budget=r['input']['budget'];quote=Decimal(policy['quote_usd'])
            self.runs.require_live_access(r['owner_id'], required='demo_access' in policy)
            quotes = 2 if purpose == 'extraction' and 'demo_access' in policy else 1
            if not money_available(c, r, quotes):
                raise ApiError('budget_exceeded')
            for parent,table in ((parent_model,'model_calls'),(parent_tool,'tool_calls')):
                if parent and not row(c,f'SELECT id FROM aist.{table} WHERE id=:id AND run_id=:r',id=parent,r=run):raise ApiError('resource_not_found')
            if retry_of is not None:
                if profile_for(r['input'])['transport'] != 'https_chat_completions':
                    raise ApiError('tool_unavailable')
                # Only a recorded overload refusal of the same purpose may be repeated.
                refused=row(c,'SELECT data FROM aist.model_calls WHERE id=:id AND run_id=:r',id=retry_of,r=run)['data']
                if (refused['purpose']!=purpose or refused['transport_state']!='observed'
                        or refused['http_status']!=RETRY['http_status'] or refused['provider_outcome']!='error'):
                    raise ApiError('resource_not_found')
            # Retries have their own small quota; the money limits see every attempt.
            ordinary,retries=attempt_counts(c,run)
            exhausted=retries>=RETRY['max_per_run'] if retry_of is not None else ordinary>=budget['max_model_calls']
            if exhausted:
                raise ApiError('budget_exceeded')
            call=dict(id=cid,run_id=run,parent_tool_call_id=parent_tool,parent_model_call_id=parent_model,purpose=purpose,
                model_profile_version_id=r['input']['model_profile_version_id'],transport_state='attempted',provider_outcome='pending',
                provider_request_id=None,provider_completion_id=None,http_status=None,attempted_at=now(),sent_at=None,observed_at=None,
                late_response=False,delivery='pending',usage_entry_id=None,error=None)
            if extraction is not None:
                from .extraction_requests import reserve
                reserve(c, r, extraction, cid, retry_of)
            u=self.usage(c,r,call)
            validate_definition('ModelCall',call)
            execute(c,'''INSERT INTO aist.model_calls(id,run_id,project_id,owner_id,epoch,parent_model_call_id,parent_tool_call_id,data,usage_entry_id,exposure)
              VALUES (:id,:r,:p,:a,:epoch,:pm,:pt,CAST(:data AS jsonb),:u,:cost)''',id=cid,r=run,p=r['project_id'],a=r['owner_id'],epoch=epoch,pm=parent_model,pt=parent_tool,data=json_value(call),u=u['id'],cost=quote)
            for seq,state in enumerate(('reserved','attempted'),1):
                execute(c,'INSERT INTO aist.model_transport_events(call_id,seq,state) VALUES (:id,:seq,:state)',id=cid,seq=seq,state=state)
            committing=True;tx.commit();return call
        except BaseException:
            if not committing:tx.rollback();raise
            c.close()
            # An attempted call discovered here is NEVER sent by this invocation.
            try:
                with self.engine.connect() as fresh:
                    existing=row(fresh,'SELECT id FROM aist.model_calls WHERE id=:id',id=cid)
                if existing:self.mark_unknown(run,cid,'commit acknowledgement lost before transport')
            except SQLAlchemyError:pass
            raise ApiError('outcome_unknown') from None
        finally:c.close()

    def call(self,c,run,call):
        value=row(c,'SELECT * FROM aist.model_calls WHERE id=:id AND run_id=:r FOR UPDATE',id=call,r=run)
        if not value:raise ApiError('resource_not_found')
        return value

    def send(self,run,epoch,call,connection,path,body,on_send=None,headers=None,*,deadline=None,compose=None):
        """The POST of a reserved call under the Run lock (cancel/save use the same lock): the body it sent,
        None when this boundary withheld it. A bounded sendall precedes the sent evidence.

        The absolute deadline of the attempt bounds the wait for the lock and forbids a POST that it already
        passed, after the connection or the wait. For a collecting Run (FLOW-01) the completion reserve is
        checked again here with the actual state, the saved calls and the seconds left to that deadline. When
        it no longer fits the collection closes first: the first POST of an agent request is composed again
        for the completion mode (compose) and, within the input limit, sent as the final body of its logical
        send; a nested extraction, a 429 retry or a composed request beyond the limit is recorded as not sent."""
        from .collection import collecting, state, shortfall, close, withheld_error
        if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('send deadline before the POST')
        c=self.engine.connect();tx=c.begin();committing=False
        try:
            if deadline is not None:limit_lock_wait(c,deadline-time.monotonic())
            r=self.runs.locked(c,run);self.runs.fence(c,r,epoch)
            m=self.call(c,run,call);data=m['data']
            if m['epoch']!=epoch or data['transport_state']!='attempted':raise ApiError('outcome_unknown')
            self.require_settled(c, r, call)
            if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('send deadline before the POST')
            withheld=None
            full_agent = r['input']['model_profile_version_id'] in FULL_EXTRACTION_IDS and data['purpose'] == 'agent'
            if r['input']['model_profile_version_id'] in FULL_EXTRACTION_IDS and data['purpose'] == 'extraction' and state(r)['closed']:
                withheld = state(r)['reason']
            if deadline is not None and collecting(r):
                if full_agent:
                    # This composer call is already counted. Close only further collection;
                    # its own response window never has to fit the extraction reserve.
                    from .collection import send_seconds
                    seconds = (send_seconds(r, self.runs.settings.transport_seconds, 'extraction') if 'min_extraction_seconds' in r['configuration']['collection'] else
                               min(self.runs.settings.transport_seconds, r['configuration']['collection']['extraction_seconds']))
                    withheld = shortfall(c, r, attempt_seconds=seconds)
                else:
                    withheld=shortfall(c,r,reserved=True,attempt_seconds=deadline-time.monotonic())
            if withheld is not None:
                current=close(self.runs,c,r,withheld)
                if full_agent:
                    # The signed native history is immutable. The next extraction tool reply
                    # communicates closure; this same accounted agent POST can still finish.
                    pass
                elif compose is None:
                    body=None
                    self.withhold(c,r,data,withheld_error(current))
                    if data['purpose']!='extraction':
                        # An agent retry that stopped fitting keeps the overload refusal it repeats as the terminal reason.
                        self.runs.transition(c,r,'failed',dict(error_value('provider_error'),message='Модельный сервис перегружен '
                            '(HTTP 429 server_overload). Повторная отправка после паузы не выполнена: остаток срока или '
                            'обращений задачи не вмещает отправку и резерв завершения.'))
                else:
                    body=compose(c,r)
                    if len(body)>input_limit(r['input'], data['purpose']):
                        # Nothing is cut and no older body returns: the size is the known cause and the Run ends with it.
                        failure=oversized_error(len(body));body=None
                        self.withhold(c,r,data,failure)
                        self.runs.transition(c,r,'budget_exceeded',failure)
            if body is not None and len(body) > input_limit(r['input'], data['purpose']):
                failure = oversized_error(len(body), input_limit(r['input'], data['purpose']))
                body = None
                self.withhold(c, r, data, failure)
            if body is not None:
                self.runs.require_live_access(r['owner_id'], required='demo_access' in r['configuration']['policy'])
                # A finalized charge may have raised exposure since this call reserved its quote.
                if r['configuration']['executor'] == 'openclaw' and not money_available(c, r, 0):
                    body = None
                    self.withhold(c, r, data, error_value('budget_exceeded'))
                    self.runs.transition(c, r, 'budget_exceeded', error_value('budget_exceeded'))
            if body is not None:
                if on_send is not None:on_send()
                connection.request('POST',path,body=body,headers=headers or {'Content-Type':'application/json'})
                data.update(transport_state='sent',sent_at=now())
                u=row(c,'SELECT data FROM aist.usage_entries WHERE id=:id',id=data['usage_entry_id'])['data']
                self.save_call(c,data,u)
            committing=True;tx.commit()
            return body
        except BaseException:
            if not committing:tx.rollback();raise
            c.close()
            try:
                with self.engine.connect() as fresh:
                    saved=row(fresh,'SELECT data,epoch FROM aist.model_calls WHERE id=:id AND run_id=:r',id=call,r=run)
                if saved and saved['epoch']==epoch and saved['data']['transport_state'] in ('sent','not_sent'):
                    return body if saved['data']['transport_state']=='sent' else None
            except SQLAlchemyError:pass
            raise ApiError('outcome_unknown') from None
        finally:c.close()

    def withhold(self,c,r,data,error):
        """The POST was never transmitted: the boundary refused the reserved call with a known cause."""
        data.update(transport_state='not_sent',provider_outcome='not_sent',delivery='suppressed',error=error)
        tokens={k:known(0) for k in ('input','output','cached_input','reasoning_output')}
        u=self.usage(c,r,data,tokens=tokens,estimate='0',kind='reconciliation',released=True)
        self.save_call(c,data,u)

    def observe(self,run,epoch,call,status,payload,*,observed_at=None,retry=None,request_id=None,native=False,protocol=None,
                deadline=None,saved_response=None,closed=None,refusal=None):
        from .collection import retry_reserve, collecting
        completion,tokens,valid_response=response_fields(
            payload, live=saved_response is not None or self.runs.settings.executor_mode=='openclaw', native=native, protocol=protocol)
        responses = protocol == 'https_openai_responses'
        overloaded=not native and not responses and overload_refusal(status,payload)
        uid=ident('usage');c=self.engine.connect();tx=c.begin();committing=False
        try:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ApiError('outcome_unknown')
                limit_lock_wait(c, min(remaining, OBSERVE_LOCK_SECONDS))
            r=self.runs.locked(c,run);m=self.call(c,run,call);data=m['data']
            if m['epoch'] != epoch:
                return 'insufficient_evidence' if saved_response is not None else False
            if saved_response is not None:
                from .received_response import binding, active
                if data['transport_state'] == 'observed':
                    return 'already_accounted'
                if (data['transport_state'] not in ('sent', 'unknown') or not data['sent_at']
                        or binding(r, m) != saved_response):
                    return 'insufficient_evidence'
                if active(c, r, epoch):
                    return 'active'
            late=saved_response is not None or r['state']!='running' or r['epoch']!=epoch or r['worker_id'] is None
            if not late:
                try:
                    self.runs.fence(c, r, epoch)
                except ApiError:
                    late = True
            if data['transport_state'] == 'observed':
                return not late and data['delivery']=='delivered' and data['provider_outcome']=='success'
            settlement = None
            if status == 200:
                estimate, valid_response = response_cost(r,payload,tokens,valid_response,native=native,responses=responses)
            else:
                estimate, tokens = None, None
                if refusal is not None:
                    rule = match_refusal(r, status, payload, refusal['endpoint'], request_id)
                    if rule is not None:
                        settlement = dict(rule_id=rule['rule_id'], version=rule['version'], http_status=status,
                                          request_sha256=refusal['request_sha256'],
                                          response_sha256=refusal['response_sha256'])
                        estimate = '0'
            if saved_response is not None and settlement is None and (status != 200 or not valid_response or estimate is None):
                return 'insufficient_evidence'
            success=status==200 and valid_response
            failure=None if success else error_value('provider_error')
            if failure is not None and status != 200:
                failure['message'] = ('Модельный сервис отклонил запрос. Правило учета подтверждает нулевую оценку.'
                                      if settlement is not None else
                                      'Модельный сервис отклонил запрос. Резерв сохранен до сверки стоимости.')
            choices=payload.get('choices') if isinstance(payload,dict) else None
            if (not success and status==200 and isinstance(choices,list) and len(choices)==1
                    and isinstance(choices[0],dict) and choices[0].get('finish_reason')=='length'):
                failure['message']='Модель исчерпала лимит ответа (finish_reason=length). Незавершенный ответ не применен.'
            pause=None
            if overloaded:
                # Financial classification does not grant a retry. The Run quota, wait and deadline still apply.
                history=retry_history(c,run)
                if retry is not None and not late and r['state']=='running':
                    pause=retry_pause(len(history),retry.retry_after,retry.jitter)
                    remaining=r['input']['budget']['max_duration_seconds']-age_seconds(r)
                    # A bounded Run also keeps the next send and, while collecting, the completion reserve after the pause.
                    if pause is not None and (waited_seconds(history)+pause>RETRY['max_total_wait_seconds'] or pause+retry_reserve(r)>remaining):
                        pause=None
                failure['message']='Модельный сервис перегружен (HTTP 429 server_overload). '+(
                    f'Повторная отправка {len(history)+1} из {RETRY["max_per_run"]} через {pause:.1f} с.' if pause is not None
                    else 'Повторная отправка не разрешена: лимит повторов, ожидания или срок задачи исчерпан.')
            data.update(transport_state='observed',provider_outcome='success' if success else 'error',http_status=status,
                observed_at=observed_at or now(),provider_completion_id=completion,
                late_response=late,delivery='suppressed' if late else 'delivered',error=failure)
            if request_id is not None and isinstance(request_id,str) and 1 <= len(request_id) <= 200:
                data['provider_request_id'] = request_id
            u=self.usage(c,r,data,tokens=tokens,estimate=estimate,
                kind='reconciliation' if settlement is not None else 'provider_response',
                released=estimate is not None, reason='provider omitted or returned invalid usage',
                uid=uid, settlement=settlement)
            self.save_call(c,data,u)
            from .extraction_requests import observed
            observed(c, r, data, payload)
            self.runs.event(c,r,'model_observed',{'model_call_id':call,'outcome':data['provider_outcome'],'late':late},call=call)
            if saved_response is None and r['state']=='running' and r['epoch']==epoch:
                if success:
                    try:self.runs.session_alive(c,r)
                    except ApiError:self.runs.transition(c,r,'failed',error_value('unauthenticated'))
                elif not late and pause is None and not (overloaded and data['purpose']=='extraction' and collecting(r)):
                    # A refused nested extraction of a bounded Run ends the collection, not the Run (collection.overload_refusal).
                    self.runs.transition(c,r,'failed',failure)
            if (deadline is not None and time.monotonic() >= deadline
                    or closed is not None and closed.is_set() and not late):
                raise ApiError('outcome_unknown')
            committing=True;tx.commit()
            # Published only after the commit: an uncertain acknowledgement never permits a send.
            if retry is not None:retry.pause=pause
            return 'recovered' if saved_response is not None else success and not late
        except BaseException as error:
            if not committing:tx.rollback();raise
            c.close()
            try:
                with self.engine.connect() as fresh:
                    old=row(fresh,'SELECT data FROM aist.usage_entries WHERE id=:id',id=uid)
                    saved=row(fresh,'SELECT data FROM aist.model_calls WHERE id=:id AND run_id=:r',id=call,r=run)
                if old and saved:
                    if saved_response is not None:
                        return 'recovered'
                    if deadline is not None:
                        # Confirm this observation on a new connection, then check the
                        # current fence before returning bytes to the old boundary.
                        with self.engine.begin() as fresh:
                            current = self.runs.locked(fresh, run)
                            try:
                                self.runs.fence(fresh, current, epoch)
                            except ApiError:
                                return False
                    if overloaded:
                        # A granted retry may have been committed; after a lost acknowledgement the Run ends instead.
                        try:self.runs.stop(run,epoch,'provider_error')
                        except (ApiError,SQLAlchemyError):pass
                    return saved['data']['delivery']=='delivered' and saved['data']['provider_outcome']=='success'
            except SQLAlchemyError:pass
            if deadline is not None and temporarily_unavailable(error):
                raise
            raise ApiError('outcome_unknown') from None
        finally:c.close()

    def unknown_active(self,c,r,reason):
        values=list(execute(c,"SELECT * FROM aist.model_calls WHERE run_id=:r AND data->>'transport_state' IN ('reserved','attempted','sent') FOR UPDATE",r=r['id']).mappings())
        for v in values:
            data=v['data'];data.update(transport_state='unknown',provider_outcome='unknown',delivery='suppressed',error=error_value('outcome_unknown'))
            u=self.usage(c,r,data,kind='reconciliation',reason=reason)
            self.save_call(c,data,u)
            self.runs.event(c,r,'model_observed',{'model_call_id':data['id'],'outcome':'unknown','late':r['state'] in TERMINAL},call=data['id'])

    def mark_unknown(self,run,call,reason,not_sent=False):
        with self.engine.begin() as c:
            limit_lock_wait(c,RECORD_LOCK_SECONDS)
            r=self.runs.locked(c,run);m=self.call(c,run,call);d=m['data']
            if d['transport_state'] in ('observed', 'not_sent'):
                return
            same_epoch = r['epoch'] == m['epoch']
            if not_sent and d['transport_state'] in ('reserved', 'attempted'):
                self.withhold(c,r,d,error_value('provider_error'))
                if same_epoch and r['state']=='running':self.runs.transition(c,r,'failed',error_value('provider_error'))
            else:
                if d['transport_state']!='unknown':
                    d.update(transport_state='unknown',provider_outcome='unknown',delivery='suppressed',error=error_value('outcome_unknown'))
                    u=self.usage(c,r,d,kind='reconciliation',reason=reason);self.save_call(c,d,u)
                    self.runs.event(c,r,'model_observed',{'model_call_id':call,'outcome':'unknown','late':r['state'] in TERMINAL},call=call)
                if same_epoch and r['state']=='running':
                    self.runs.transition(c,r,'recovery_required',error_value('outcome_unknown'))
                    self.runs.event(c,r,'recovery_recorded',{'reason':reason})

    def billing_run(self, r):
        if not r:
            return False
        executor = r['configuration']['executor']
        return executor == 'openclaw' or executor == self.runs.settings.executor_mode == QA_SCOPE

    def billing_call(self, run, call):
        with self.engine.connect() as c:
            execute(c, 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            r = row(c, 'SELECT * FROM aist.runs WHERE id=:id', id=run)
            m = row(c, 'SELECT * FROM aist.model_calls WHERE id=:id AND run_id=:r', id=call, r=run)
            if not m or not self.billing_run(r):
                raise ApiError('resource_not_found')
            u = row(c, 'SELECT data FROM aist.usage_entries WHERE id=:id', id=m['usage_entry_id'])['data']
            from .received_response import active
            state = m['data']['transport_state']
            eligible = state == 'observed' or (state == 'unknown' and not active(c, r, m['epoch']))
            reason = 'awaiting_individual_confirmation' if eligible else 'transport_not_eligible'
            if u['reservation_state'] == 'released':
                reason = 'reservation_released'
            return dict(invoice_target(r, m), transport_state=state, can_reconcile=eligible,
                        provider_outcome=m['data']['provider_outcome'], http_status=m['data']['http_status'],
                        current_usage_id=u['id'], exposure_usd=format(m['exposure'], 'f'),
                        reservation_state=u['reservation_state'], reserved_usd=u['reserved_usd'],
                        estimated_usd=u['estimated_usd'], billed_usd=u['billed_usd'], reason=reason,
                        required_evidence='final_individual_billing_confirmation')

    def invoice(self, run, call, evidence, *, apply=False):
        """Apply an attested per-call charge with a compare-and-swap on the usage chain."""
        try:
            validate_definition('OperatorBillingEvidence', evidence)
            if evidence['run_id'] != run or evidence['call_id'] != call or moment(evidence['confirmed_at']) > moment(now()):
                raise ValueError('invalid invoice scope or time')
        except (ValidationError, ValueError):
            raise ApiError('validation_error') from None
        committing = False
        try:
            with self.engine.begin() as c:
                if apply:
                    limit_lock_wait(c, OBSERVE_LOCK_SECONDS)
                    r = self.runs.locked(c, run)
                    m = self.call(c, run, call)
                else:
                    execute(c, 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    r = row(c, 'SELECT * FROM aist.runs WHERE id=:id', id=run)
                    m = row(c, 'SELECT * FROM aist.model_calls WHERE id=:id AND run_id=:r', id=call, r=run)
                if not m or not self.billing_run(r):
                    raise ApiError('resource_not_found')
                target = invoice_target(r, m)
                scope = target['budget_scope']
                if scope != evidence['budget_scope'] or (scope not in SCOPES and scope != QA_SCOPE):
                    raise ApiError('validation_error')
                if apply:
                    lock_scope(c, scope)
                previous = self.invoice_replay(c, evidence)
                if previous is not None:
                    return previous
                if any(evidence[key] != value for key, value in target.items()):
                    raise ApiError('validation_error')
                from .received_response import active
                d = m['data']
                if d['transport_state'] != 'observed' and (
                        d['transport_state'] != 'unknown' or active(c, r, m['epoch'])):
                    raise ApiError('outcome_unknown')
                if moment(evidence['confirmed_at']) < moment(d['sent_at'] or d['attempted_at']):
                    raise ApiError('validation_error')
                if evidence['expected_usage_id'] != m['usage_entry_id']:
                    raise ApiError('revision_conflict')
                if not apply:
                    return dict(status='valid', call_id=call, usage_entry_id=m['usage_entry_id'])
                u = self.usage(c, r, d, billed=evidence['amount_usd'], kind='provider_invoice', settlement=evidence)
                self.save_call(c, d, u)
                committing = True
            return dict(status='recorded', call_id=call, usage_entry_id=u['id'])
        except SQLAlchemyError:
            if committing:
                # Check the operation on a new connection after a lost commit acknowledgement.
                with self.engine.connect() as fresh:
                    previous = self.invoice_replay(fresh, evidence)
                if previous is not None:
                    return previous
            raise

    def invoice_replay(self, c, evidence):
        rows = list(execute(c, """
            SELECT id, data->'settlement' AS evidence FROM aist.usage_entries
            WHERE data->>'evidence_kind'='provider_invoice'
            AND data->'settlement'->>'budget_scope'=:scope
            AND (data->'settlement'->>'operation_id'=:operation
                OR (data->'settlement'->>'account_scope'=:account AND data->'settlement'->>'line_id'=:line
                    AND (data->'settlement'->>'source_id'=:source OR data->'settlement'->>'source_sha256'=:sha)))
        """, scope=evidence['budget_scope'], operation=evidence['operation_id'], account=evidence['account_scope'],
            source=evidence['source_id'], line=evidence['line_id'], sha=evidence['source_sha256']).mappings())
        if rows:
            if len(rows) != 1 or rows[0]['evidence'] != evidence:
                raise ApiError('idempotency_conflict')
            return dict(status='already_recorded', call_id=evidence['call_id'], usage_entry_id=rows[0]['id'])

    def aggregate(self,value):
        validate_definition('BillingAggregate',value)
        with self.engine.begin() as c:execute(c,'INSERT INTO aist.billing_aggregates(id,data) VALUES (:id,CAST(:d AS jsonb))',id=value['id'],d=json_value(value))
