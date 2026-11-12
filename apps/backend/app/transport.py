"""Single-attempt HTTP transport; no redirect, proxy or provider framework.

The only automatic retry is the overload backoff declared in the live profile:
an identical send after a completely received HTTP 429 server_overload refusal."""
import http.client
import hashlib
import json
import random
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from sqlalchemy.exc import SQLAlchemyError
from .accounting import Accounting, OBSERVE_PAUSE, OBSERVE_SECONDS, OverloadRetry
from .database import temporarily_unavailable
from .billing_evidence import ENDPOINTS, match_refusal, refusal_payload
from .received_response import publish
from .errors import ApiError
from .http_io import ReaderConnection, ReaderHTTPSConnection
from .runs import json_value, now, row

jitter=random.random


def framed_json(response):
    lengths = response.headers.get_all('Content-Length', [])
    transfer = response.headers.get_all('Transfer-Encoding', [])
    framed = ((response.chunked and len(transfer) == 1 and not lengths)
              or (len(lengths) == 1 and not transfer and lengths[0].isascii() and lengths[0].isdigit()))
    return (framed and response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() == 'application/json'
            and response.getheader('Content-Encoding', 'identity').lower() == 'identity')


def retry_after_seconds(value):
    """Seconds named by a valid Retry-After header, as delta-seconds or an HTTP date; None otherwise."""
    if value is None:return None
    value=value.strip()
    if value.isdigit():return float(value)
    try:when=parsedate_to_datetime(value)
    except (TypeError,ValueError,IndexError):return None
    if when.tzinfo is None:when=when.replace(tzinfo=timezone.utc)
    return max(0.0,(when-datetime.now(timezone.utc)).total_seconds())


def model_request(run, purpose, request):
    """The final provider fields shared by context sizing and transport."""
    from .live_profile import PROFILE, MODEL, profile_for
    profile = profile_for(run.get('input', {}))
    if profile['transport'] == 'https_openai_responses':
        from .openai_responses import request as responses_request
        policy = run.get('configuration', {}).get('policy', {})
        cache_key = None
        if (policy.get('pricing_version') == 'openai-astra.standard.cache.v2'
                and purpose == 'agent' and 'input' in request and 'edit_request' not in run.get('input', {})):
            scope = [run['owner_id'], run['project_id'], run['id'], run['executor_session_id'], policy['pricing_version']]
            cache_key = hashlib.sha256(json_value(scope).encode()).hexdigest()
        return responses_request(profile, request, cache_key=cache_key)
    if profile['provider'] == 'anthropic':
        from copy import deepcopy
        outgoing = deepcopy(request)
        if purpose == 'extraction':
            # Preserve the complete extractor instruction and page payload in native roles.
            system = [dict(type='text', text=m['content']) for m in outgoing['messages'] if m['role'] == 'system']
            outgoing['system'] = system
            outgoing['messages'] = [m for m in outgoing['messages'] if m['role'] != 'system']
        effort = profile.get('extraction_effort', profile['effort']) if purpose == 'extraction' else profile['effort']
        outgoing.update(model=profile['model'], stream=True, max_tokens=profile['max_tokens'],
                        thinking={'type': 'adaptive'}, output_config={'effort': effort})
        if 'demo_access' in run.get('configuration', {}).get('policy', {}):
            from .openclaw_runtime import TOOLS
            if outgoing.get('mcp_servers') or any(tool.get('type', 'custom') != 'custom' or tool.get('name') not in TOOLS
                                                  for tool in outgoing.get('tools', [])):
                raise ApiError('tool_unavailable')
            for key in ('service_tier', 'speed', 'cache_control'):
                outgoing.pop(key, None)
        for key in ('temperature', 'top_p', 'top_k', 'enable_thinking', 'reasoning_effort', 'n',
                    'parallel_tool_calls', 'stream_options', 'max_completion_tokens', 'response_format'):
            outgoing.pop(key, None)
        # The installed client may offer automatic cache markers; this profile explicitly disables them.
        for block in [*outgoing.get('tools', []), *(outgoing.get('system', []) if isinstance(outgoing.get('system'), list) else [])]:
            block.pop('cache_control', None)
        for message in outgoing.get('messages', []):
            if isinstance(message.get('content'), list):
                for block in message['content']:
                    block.pop('cache_control', None)
        if 'edit_request' in run.get('input', {}):
            outgoing['max_tokens']=min(outgoing['max_tokens'],8192)
        return outgoing
    outgoing = dict(request, model=MODEL, stream=False, max_tokens=PROFILE['max_tokens'])
    for key in ('stream_options', 'max_completion_tokens', 'temperature', 'reasoning_effort'):
        outgoing.pop(key, None)
    thinking = (run['configuration'].get('extraction_thinking', PROFILE['extraction_thinking'])
                if purpose == 'extraction' else PROFILE['thinking'])
    outgoing.update(n=1, parallel_tool_calls=False, thinking={'type': thinking}, enable_thinking=thinking == 'enabled')
    if 'edit_request' in run.get('input', {}):
        outgoing['max_tokens']=min(outgoing['max_tokens'],8192)
    return outgoing


def with_context(request, context, protocol):
    if protocol == 'https_openai_responses':
        from .openai_responses import with_context as responses_context
        return responses_context(request, context)
    if protocol == 'https_anthropic_messages':
        from .anthropic_messages import with_context as native_context
        return native_context(request, context)
    return dict(request, messages=[dict(role='system', content=context), *request['messages']])


class Transport:
    def __init__(self,runs,draining,closed,persistence,connections,connection_lock):
        self.runs=runs;self.ledger=Accounting(runs);self.draining=draining;self.closed=closed
        self.persistence=persistence;self.connections=connections;self.connection_lock=connection_lock

    def perform(self,r,step,parent=None,*,request=None,audit=None,runtime_context=None,on_close=None,deadline=None):
        """One logical send with its granted overload retries. on_close(c, run) composes the service context
        again when the collection of a bounded Run closes right before the first POST of an agent request;
        the body composed then is the final body of the send, repeated exactly by its retries."""
        if self.draining.is_set() or self.closed.is_set():raise ApiError('outcome_unknown')
        from .live_profile import FABLE_ID
        if step.get('purpose') == 'extraction' and (self.runs.settings.ready_facts_only or r['input']['model_profile_version_id'] == FABLE_ID):
            raise ApiError('tool_unavailable')
        live=request is not None
        send=dict(live=live)
        from .collection import limits
        if 'min_extraction_seconds' in (limits(r) or {}):
            send['deadline'] = time.monotonic() + self.send_seconds(r, step.get('purpose', 'agent'))
        if deadline is not None:
            send['deadline'] = min(send.get('deadline', deadline), deadline)
        if live:
            from .live_profile import profile_for, input_limit
            profile = profile_for(r['input'])
            native = profile['provider'] == 'anthropic'
            protocol = profile['transport']
            from .normalization import fingerprint
            if self.runs.settings.executor_mode!='openclaw' or not self.runs.settings.live_models_enabled:
                raise ApiError('tool_unavailable')
            outgoing=model_request(r, step.get('purpose'), request)
            # Изменение остатка бюджета не разрешает повтор той же отправки.
            digest=fingerprint(outgoing)
            if on_close is not None:send.update(context=on_close,outgoing=dict(outgoing))
            send['native'] = native
            send['protocol'] = protocol
            send['response_bytes'] = profile.get('response_bytes', 2000000)
            if runtime_context is not None:
                outgoing = with_context(outgoing, runtime_context, protocol)
            if protocol == 'https_openai_responses':
                digest=fingerprint(outgoing)
            # Limits are checked against the actual sent body, including tool schemas/history.
            body=json_value(outgoing).encode()
            if len(body)>input_limit(r['input'], step.get('purpose')):raise ApiError('budget_exceeded')
            audit=Path(audit);audit.mkdir(parents=True,exist_ok=True,mode=0o700)
            if step.get('extraction') is not None:
                step = dict(step, extraction=dict(step['extraction'], body_sha256=hashlib.sha256(body).hexdigest()))
                digest = fingerprint(dict(run=r['id'], operation=step['extraction']['key']))
            claim=audit/(digest+'.claim')
            try:
                with claim.open('x') as f:f.write('single provider submission\n')
            except FileExistsError:raise ApiError('outcome_unknown') from None
            if protocol == 'https_openai_responses':
                from .openai_responses import credential
                token = credential(self.runs.settings.openai_secret_path)
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
            elif native:
                from .anthropic_messages import credential
                token = credential(self.runs.settings.anthropic_secret_path)
                headers = {'Content-Type': 'application/json', 'x-api-key': token, 'anthropic-version': '2023-06-01'}
            else:
                from .novita import credential
                token = credential(self.runs.settings.novita_secret_path)
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
            host, path = ENDPOINTS[protocol]
            send.update(body=body,path=path,audit=audit,claim=claim,token=token,host=host,headers=headers)
        else:
            url=urlsplit(self.runs.settings.qa_url)
            if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','simulator'):raise ValueError('local simulator required')
        refused=None
        while True:
            call,payload,delivered,pause=self.attempt(r,step,parent,refused,send)
            if pause is None:
                return (call,payload) if live and delivered else (call if delivered else None)
            # The durable claim stays: the only exception is this granted retry of a recorded overload refusal.
            with send['claim'].open('a') as f:f.write(f'retry granted after {call}: HTTP 429 server_overload, pause {pause:.1f} s\n')
            if not self.pause(r,pause):return None
            refused=call

    def attempt(self,r,step,parent,refused,send):
        """One accounted POST. A retry names the recorded overload refusal it repeats as its parent."""
        live=send['live']
        with self.persistence:
            if self.closed.is_set():raise ApiError('outcome_unknown')
            call=self.ledger.reserve(r['id'],r['epoch'],step.get('purpose','agent'),parent_model=refused or parent,retry_of=refused,
                                     extraction=step.get('extraction'),deadline=send.get('deadline'))
        if live:
            c=ReaderHTTPSConnection(send['host'],443,timeout=3)
            path,headers,body=send['path'],send['headers'],send['body']
            (send['audit']/(call['id']+'-request.json')).write_bytes(body)
        else:
            url=urlsplit(self.runs.settings.qa_url)
            c=ReaderConnection(url.hostname,url.port or 80,timeout=min(3,self.runs.settings.transport_seconds))
            path='/completion';headers=None
            body=json_value({'run_id':r['id'],'call_id':call['id'],'synthetic':True,'step':step})
        with self.connection_lock:self.connections[call['id']]=c
        connected=False; sending=[False];response=None
        # One monotonic deadline covers the whole attempt: the connection, the wait for the lock, the POST, the
        # headers and the body; no phase, no composed context and no received chunk restarts it. The watchdog
        # shuts this connection down at the deadline, whatever http.client is waiting for; it belongs to this
        # attempt alone and is released with it, so a late interruption never reaches the next attempt.
        started = time.monotonic()
        seconds = self.send_seconds(r, step.get('purpose', 'agent'))
        deadline=started+seconds
        if send.get('deadline') is not None:
            deadline = min(deadline, send['deadline'])
            seconds = max(0.0, deadline - started)
        timings = None
        if send.get('native'):
            from .anthropic_messages import StreamAudit
            timings = StreamAudit(send['audit']/(call['id']+'-timings.json'), started, seconds)
            timings.write()
        watchdog=threading.Timer(max(0.0,deadline-time.monotonic()),c.interrupt);watchdog.daemon=True;watchdog.start()
        try:
            # http.client hands the socket over only after the connection and the TLS handshake, where the watchdog
            # cannot reach: they wait no longer than the rest of the deadline instead.
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('attempt deadline before the connection')
            c.timeout=min(c.timeout,remaining)
            c.connect();connected=True
            with self.persistence:
                if self.closed.is_set() or self.draining.is_set():
                    self.ledger.mark_unknown(r['id'],call['id'],'shutdown before send',not_sent=True)
                    raise ApiError('outcome_unknown')
                def begin_send():
                    if self.draining.is_set() or self.closed.is_set():raise ApiError('outcome_unknown')
                    sending[0]=True
                    if timings is not None:timings.mark('send_started')
                compose=None
                if refused is None and send.get('context') is not None:
                    # The same request with the service context of the closed collection, recorded as the request of
                    # this call whether it is sent or withheld; a retry keeps the body of the refusal it repeats.
                    def compose(connection,current):
                        outgoing=with_context(send['outgoing'],send['context'](connection,current),send.get('protocol'))
                        composed=json_value(outgoing).encode()
                        (send['audit']/(call['id']+'-request.json')).write_bytes(composed)
                        return composed
                sent=self.ledger.send(r['id'],r['epoch'],call['id'],c,path,body,on_send=begin_send,headers=headers,deadline=deadline,compose=compose)
                if sent is None:return call['id'],None,False,None
                if timings is not None:
                    timings.mark('sent');timings.write()
                # The bytes that left are the final body of this logical send: a granted retry repeats them exactly.
                if live:send['body']=sent
            c.sock.settimeout(max(0.0,deadline-time.monotonic()))
            response=c.getresponse();status=response.status
            if timings is not None:
                timings.mark('headers');timings.write()
            raw=bytearray()
            request_id = response.getheader('request-id') if send.get('native') else None
            if send.get('protocol') in ('https_openai_responses', 'https_chat_completions'):
                request_id = response.getheader('x-request-id')
            if live:
                if request_id and send['token'] in request_id:
                    request_id = None
                (send['audit']/(call['id']+'-headers.json')).write_text(json_value(dict(status=status,request_id=request_id)))
            response_limit = send.get('response_bytes', 2000000)
            while len(raw)<=response_limit:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('response deadline')
                c.reader_socket.settimeout(remaining)
                chunk=response.read1(min(65536,response_limit+1-len(raw)))
                if not chunk:
                    if response.length not in (None,0):raise http.client.IncompleteRead(bytes(raw),response.length)
                    break
                raw.extend(chunk)
                if timings is not None:timings.feed(chunk)
                if response.isclosed():
                    if response.length not in (None,0):raise http.client.IncompleteRead(bytes(raw),response.length)
                    break
            # The response is complete: parsing and accounting are no longer under the network deadline.
            watchdog.cancel()
            if len(raw)>response_limit:raise ValueError('response size limit before complete receipt')
            if timings is not None and not c.interrupted:
                timings.mark('read_complete');timings.write()
            if live:
                if send['token'].encode() in raw:raise ValueError('secret echo suppressed')
                suffix = '-response.sse' if send.get('protocol') in ('https_anthropic_messages', 'https_openai_responses') and status == 200 else '-response.json'
                (send['audit']/(call['id']+suffix)).write_bytes(raw)
            observed_at=now()
            if send.get('protocol') == 'https_openai_responses' and status == 200:
                from .openai_responses import response as responses_response
                payload = responses_response(raw)
                (send['audit']/(call['id']+'-response.json')).write_text(json_value(payload))
            elif send.get('native') and status == 200:
                from .anthropic_messages import response as native_response
                payload = native_response(raw)
                (send['audit']/(call['id']+'-response.json')).write_text(json_value(payload))
            else:
                try:payload=json.loads(raw)
                except (ValueError,UnicodeError,RecursionError):payload=None
            retry=OverloadRetry(retry_after_seconds(response.getheader('Retry-After')),jitter()) if live and send.get('protocol') == 'https_chat_completions' and 'edit_request' not in r['input'] else None
            refusal = None
            if (live and 400 <= status <= 599 and r['configuration']['policy'].get('unbilled_errors') and not c.interrupted
                    and framed_json(response)):
                endpoint = send['host'] + send['path']
                typed_error = refusal_payload(raw)
                if match_refusal(r, status, typed_error, endpoint, request_id) is not None:
                    refusal = publish(self.runs.settings.openclaw_root, send['audit'], r, call, sent, raw,
                                      send['protocol'], observed_at, request_id, status=status, endpoint=endpoint)
                    payload = typed_error
            record_deadline = time.monotonic() + OBSERVE_SECONDS if live and (status == 200 or refusal) else None
            if live and status == 200 and isinstance(payload, dict):
                publish(self.runs.settings.openclaw_root, send['audit'], r, call, sent, raw,
                        send['protocol'], observed_at, request_id)
            while True:
                try:
                    with self.persistence:
                        if self.closed.is_set():
                            return call['id'],payload,False,None
                        delivered=self.ledger.observe(r['id'],r['epoch'],call['id'],status,payload,observed_at=observed_at,retry=retry,
                            request_id=request_id,native=send.get('native',False),protocol=send.get('protocol'),deadline=record_deadline,closed=self.closed,refusal=refusal)
                    break
                except SQLAlchemyError as error:
                    if (record_deadline is None or not temporarily_unavailable(error)
                            or record_deadline - time.monotonic() <= OBSERVE_PAUSE):
                        raise
                    if self.closed.wait(OBSERVE_PAUSE):
                        return call['id'],payload,False,None
            if timings is not None:timings.mark('accounting_complete')
            return call['id'],payload,delivered and not self.closed.is_set(),retry.pause if retry else None
        except Exception:
            if live and 'raw' in locals() and raw and send['token'].encode() not in raw:
                (send['audit']/(call['id']+'-partial.bin')).write_bytes(raw)
            with self.persistence:
                if not self.closed.is_set():
                    try:
                        self.ledger.mark_unknown(r['id'],call['id'],'transport interrupted; no automatic retry',not_sent=not connected or not sending[0])
                        if timings is not None:timings.mark('accounting_complete')
                    except Exception:pass
            raise ApiError('outcome_unknown') from None
        finally:
            watchdog.cancel()
            if response is not None:response.close()
            c.close()
            with self.connection_lock:self.connections.pop(call['id'],None)
            if timings is not None:
                timings.mark('attempt_finished');timings.write()

    def send_seconds(self,r,purpose='agent'):
        """The transport limit, bounded for a collecting Run by its send limit and the remaining Run time."""
        from .collection import send_seconds
        return send_seconds(r,self.runs.settings.transport_seconds,purpose)

    def pause(self,r,seconds):
        """Wait out one granted backoff without holding a lock or a database connection.

        Worker shutdown, cancellation, another epoch, a lost lease or the Run deadline end it early
        and forbid the next send."""
        deadline=time.monotonic()+seconds
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:return True
            if self.draining.wait(min(1.0,remaining)) or self.closed.is_set():return False
            with self.runs.engine.connect() as c:
                current=row(c,'''SELECT state,epoch,lease_until>clock_timestamp() AS leased,
                    extract(epoch from clock_timestamp()-created_at) AS age FROM aist.runs WHERE id=:r''',r=r['id'])
            if (not current or current['state']!='running' or current['epoch']!=r['epoch'] or not current['leased']
                    or current['age']>r['input']['budget']['max_duration_seconds']):
                return False


def interrupt_connection(c):
    c.interrupt()
