"""Run transactions. Executor identity and authority never come from model arguments."""
from datetime import datetime, timezone
import json
import hashlib
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .service import row, ident, timestamp
from .contracts import validate_definition, PACKAGE
from .normalization import checked, fingerprint
from .errors import ApiError, error_value
from . import worker_availability
from pathlib import Path
from decimal import Decimal

TERMINAL = {'completed','failed','cancelled','budget_exceeded'}
LIVE_MESSAGES = {
    'available': 'Можно отправить новую задачу.',
    'disabled': 'Генерация выключена. Готовые результаты, правка и экспорт доступны.',
    'owner_denied': 'Генерация для этой учетной записи не включена. Готовые результаты, правка и экспорт доступны.',
    'temporarily_unavailable': 'Исполнитель временно недоступен. Повторите отправку после восстановления.',
    'busy': 'Демо занято. Дождитесь завершения текущей задачи и обновите состояние.',
    'budget_exceeded': 'Доступного бюджета недостаточно для новой отправки. Готовые результаты, правка и экспорт доступны.',
}


METERED_DEMO_VERSION = 'demo-live01.accounting-only.v1'


def metered_demo(policy):
    return 'demo_access' in policy and policy['run']['policy_version'] == METERED_DEMO_VERSION


def validate_demo_policy(policy):
    """Accept only the owner-approved series ceilings without resetting prior exposure."""
    access = policy['demo_access']
    if policy.get('budget_scope') == 'openai-authorized-total':
        from .live_profile import OPENAI_ID
        for field in ('total_authorized_usd', 'project_limit', 'quote_usd', 'input_per_million',
                      'output_per_million', 'long_context_input_per_million', 'long_context_output_per_million'):
            checked('UnsignedDecimal', policy[field])
            if Decimal(policy[field]) <= 0:
                raise ValueError('Positive OpenAI monetary limits required')
        checked('UnsignedDecimal', policy['run']['limit'])
        if (set(access) != {'enabled', 'owner_ids'} or type(access['enabled']) is not bool
                or not isinstance(access['owner_ids'], list)
                or any(not isinstance(owner, str) or not owner.startswith('usr_') for owner in access['owner_ids'])
                or policy.get('model_profile_version_id') != OPENAI_ID
                or policy.get('pricing_version') not in ('openai-astra.standard.no-cache.v1', 'openai-astra.standard.cache.v2')
                or metered_demo(policy) or Decimal(policy['run']['limit']) <= 0
                or Decimal(policy['quote_usd']) != 24 or Decimal(policy['historical_exposure_usd']) != 0
                or Decimal(policy['input_per_million']) != 10 or Decimal(policy['output_per_million']) != 50
                or type(policy.get('long_context_threshold_tokens')) is not int or policy['long_context_threshold_tokens'] != 272000
                or Decimal(policy['long_context_input_per_million']) != 20
                or Decimal(policy['long_context_output_per_million']) != 75):
            raise ValueError('Invalid OpenAI access or budget policy')
        if policy['pricing_version'] == 'openai-astra.standard.cache.v2':
            rates = dict(cache_read_per_million='1', cache_write_30m_per_million='12.5', cache_write_12h_per_million='25',
                         long_context_cache_read_per_million='2', long_context_cache_write_30m_per_million='25',
                         long_context_cache_write_12h_per_million='50')
            for field, expected in rates.items():
                checked('UnsignedDecimal', policy.get(field))
                if Decimal(policy[field]) != Decimal(expected):
                    raise ValueError('Invalid OpenAI cache tariff')
        return
    if metered_demo(policy):
        valid_limits = all(value is None for value in
                           (policy['total_authorized_usd'], policy['project_limit'], policy['run']['limit']))
    else:
        valid_limits = (Decimal(policy['total_authorized_usd']) in (50, 75, 100)
                        and Decimal(policy['project_limit']) == 50 and 0 < Decimal(policy['run']['limit']) <= 50)
    if (set(access) != {'enabled', 'owner_ids'} or type(access['enabled']) is not bool
            or not isinstance(access['owner_ids'], list)
            or any(not isinstance(owner, str) or not owner.startswith('usr_') for owner in access['owner_ids'])
            or policy['budget_scope'] != 'anthropic-authorized-total'
            or not valid_limits or Decimal(policy['quote_usd']) != 15
            or Decimal(policy['historical_exposure_usd']) != 0
            or Decimal(policy['input_per_million']) != 10 or Decimal(policy['output_per_million']) != 50):
        raise ValueError('Invalid demo access or budget policy')


def live_failure(state):
    error = ApiError('budget_exceeded' if state == 'budget_exceeded' else 'temporarily_unavailable' if state in ('busy', 'temporarily_unavailable') else 'tool_unavailable')
    error.body['error']['message'] = LIVE_MESSAGES[state]
    return error


def utcnow():
    """The Run clock; tests replace it to simulate elapsed time without waiting."""
    return datetime.now(timezone.utc)


def now():
    return timestamp(utcnow())


def age_seconds(r):
    """Seconds since the creation of the Run; created_at is immutable, so any saved row is current."""
    return (utcnow()-r['created_at']).total_seconds()


def execute(c, sql, **params):
    return c.execute(text(sql), params)


def json_value(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',',':'))


class Runs:
    def __init__(self, service, settings):
        self.service, self.engine, self.settings = service, service.engine, settings
        self.policy = None
        if settings.policy_path:
            self.policy = json.loads(Path(settings.policy_path).read_text())
            checked('BudgetPolicy', self.policy['run'])
            priced_fields = ('quote_usd', 'input_per_million', 'output_per_million')
            if not metered_demo(self.policy):
                priced_fields += ('project_limit',)
                checked('UnsignedDecimal', self.policy['run']['limit'])
            for k in priced_fields:
                checked('UnsignedDecimal',self.policy[k])
                if Decimal(self.policy[k])<=0: raise ValueError('positive server policy required')
            if 'demo_access' in self.policy:
                validate_demo_policy(self.policy)
            if 'unbilled_errors' in self.policy:
                from .billing_evidence import validate_rules
                validate_rules(self.policy['unbilled_errors'])
        from .note_renderer import registrations
        self.registry = registrations(json.loads((PACKAGE/'registry.json').read_text()))
        if settings.executor_mode == 'openclaw':
            from .live_profile import delivered, profile_for
            self.registry = delivered(self.registry, settings.model_profile_id)
            provider = profile_for({'model_profile_version_id': settings.model_profile_id})['provider']
            scope = provider + '-authorized-total'
            if not self.policy or self.policy.get('budget_scope') != scope:
                raise ValueError('The selected provider requires its own budget policy')

    def live_access(self, actor, *, required=False):
        guarded = required or (self.policy is not None and 'demo_access' in self.policy)
        if self.settings.executor_mode != 'openclaw' or not self.settings.live_models_enabled:
            return 'disabled' if guarded else None
        if not guarded:
            return None
        # Operators replace the shared policy file atomically. Re-read it at every POST,
        # so revocation does not depend on restarting a worker with an old Run snapshot.
        try:
            policy = json.loads(Path(self.settings.policy_path).read_text())
            validate_demo_policy(policy)
            if self.policy.get('budget_scope') == 'openai-authorized-total':
                pinned = {key: value for key, value in self.policy.items() if key != 'demo_access'}
                current = {key: value for key, value in policy.items() if key != 'demo_access'}
                if current != pinned:
                    return 'disabled'
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, ApiError):
            return 'disabled'
        if not policy['demo_access']['enabled']:
            return 'disabled'
        if actor not in policy['demo_access']['owner_ids']:
            return 'owner_denied'
        return None

    def require_live_access(self, actor, *, required=False):
        reason = self.live_access(actor, required=required)
        if reason:
            raise live_failure(reason)

    def worker_unavailable(self, c):
        return self.settings.executor_mode == 'openclaw' and not worker_availability.available(c, self)

    def admission(self, c, actor, project=None):
        reason = self.live_access(actor)
        if reason:
            return reason
        if self.settings.executor_mode == 'openclaw' and not self.settings.live_models_enabled:
            return 'disabled'
        if self.worker_unavailable(c):
            return 'temporarily_unavailable'
        if not self.policy or 'demo_access' not in self.policy:
            return None
        # Same lock order as Accounting: own project/Run, then the common ledger.
        # No row lock on another owner's Run is taken while holding the shared lock.
        execute(c, 'SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))', scope=self.policy['budget_scope'])
        active = row(c, """SELECT id FROM aist.runs WHERE configuration->>'executor'='openclaw'
            AND (state IN ('queued','running','waiting_for_user','recovery_required')
                 OR (worker_id IS NOT NULL AND lease_until>clock_timestamp())) LIMIT 1""")
        if active:
            return 'busy'
        from .accounting import money_available
        candidate = dict(project_id=project, input={'budget': self.policy['run']}, configuration={'policy': self.policy})
        if not money_available(c, candidate, 1):
            return 'budget_exceeded'
        return None

    def capabilities(self, actor):
        with self.engine.begin() as c:
            state = self.admission(c, actor) or ('disabled' if self.settings.executor_mode == 'disabled' else 'available')
        return dict(self.registry, model_execution={'state': state, 'message': LIVE_MESSAGES[state]})

    def project(self,c,actor,project,lock=False):
        return self.service.scoped(c,actor,project,lock=lock)

    def scoped(self,c,actor,project,run,lock=False):
        return self.service.scoped(c,actor,project,'runs',run,lock=lock)

    def view(self,r):
        keys=('id','project_id','conversation_id','message_id','revision','state','input','request_sha256',
              'result_version_id','error','waiting_input','last_event_seq','created_at','updated_at')
        v={k:timestamp(r[k]) if k in ('created_at','updated_at') else r[k] for k in keys}
        v['created_by']=r['owner_id']
        return validate_definition('Run',v)

    def message_view(self,m):
        keys=('id','project_id','conversation_id','sequence','role','text','run_id','artifact_version_id','created_at')
        return validate_definition('Message',{k:timestamp(m[k]) if k=='created_at' else m[k] for k in keys})

    def event(self,c,r,kind,data,call=None,tool=None):
        seq=execute(c,'UPDATE aist.runs SET last_event_seq=last_event_seq+1 WHERE id=:id RETURNING last_event_seq',id=r['id']).scalar_one()
        event=validate_definition('RunEvent',dict(run_id=r['id'],seq=seq,timestamp=now(),call_id=call,tool_call_id=tool,type=kind,data=data))
        execute(c,'INSERT INTO aist.run_events(run_id,seq,data) VALUES (:run,:seq,CAST(:data AS jsonb))',run=r['id'],seq=seq,data=json_value(event))
        r['last_event_seq']=seq
        return event

    def transition(self,c,r,state,error=None,waiting=None,result=None):
        old=r['state']
        if old in TERMINAL: raise ApiError('run_terminal')
        allowed={'queued':{'running','cancelled','failed','budget_exceeded'},
            'running':{'waiting_for_user','completed','failed','cancelled','budget_exceeded','recovery_required'},
            'waiting_for_user':{'running','cancelled','failed','budget_exceeded'},
            'recovery_required':{'running','completed','failed','cancelled','budget_exceeded'}}
        if state not in allowed[old]: raise ApiError('validation_error')
        execute(c,'''UPDATE aist.runs SET state=:state,revision=revision+1,error=CAST(:error AS jsonb),
          waiting_input=CAST(:waiting AS jsonb),result_version_id=:result,updated_at=clock_timestamp() WHERE id=:id''',
          id=r['id'],state=state,error=json_value(error) if error is not None else None,waiting=json_value(waiting) if waiting is not None else None,result=result)
        r.update(state=state,revision=r['revision']+1,error=error,waiting_input=waiting,result_version_id=result)
        self.event(c,r,'state_changed',{'from':old,'to':state})

    def append_message(self,c,r,role,content,version=None):
        conv=self.service.scoped(c,r['owner_id'],r['project_id'],'conversations',r['conversation_id'],lock=True)
        mid=ident('msg'); sequence=conv['revision']+1
        execute(c,'''INSERT INTO aist.messages(id,project_id,owner_id,conversation_id,sequence,role,text,run_id,artifact_version_id)
          VALUES (:id,:p,:a,:conv,:seq,:role,:content,:run,:version)''',id=mid,p=r['project_id'],a=r['owner_id'],conv=r['conversation_id'],seq=sequence,role=role,content=content,run=r['id'],version=version)
        execute(c,'UPDATE aist.conversations SET revision=:seq WHERE id=:id',seq=sequence,id=r['conversation_id'])
        return mid

    def receipt(self,c,actor,project,operation,target,key):
        return row(c,'''SELECT * FROM aist.run_receipts WHERE actor_id=:a AND project_id=:p AND operation=:op AND target=:t AND key=:key''',a=actor,p=project,op=operation,t=target,key=key)

    def receipt_view(self,c,receipt):
        r=self.scoped(c,receipt['actor_id'],receipt['project_id'],receipt['run_id'])
        if receipt['operation']=='cancelRun':return self.view(r)
        m=row(c,'SELECT * FROM aist.messages WHERE id=:id',id=receipt['message_id'])
        return validate_definition('Submission',{'message':self.message_view(m),'run':self.view(r)})

    def write(self,actor,session,project,target,operation,key,body):
        body=dict(body)
        if 'source_version_ids' in body:
            if len(set(body['source_version_ids']))!=len(body['source_version_ids']):raise ApiError('validation_error')
            body['source_version_ids']=sorted(body['source_version_ids'])
        sha=fingerprint(body)
        c=self.engine.connect(); tx=c.begin(); committing=False
        try:
            self.project(c,actor,project,True)
            if operation=='submitMessage':
                current=self.service.scoped(c,actor,project,'conversations',target,lock=True)
            else: current=self.scoped(c,actor,project,target,True)
            self.session_alive(c,{'session_id':session,'owner_id':actor})
            old=self.receipt(c,actor,project,operation,target,key)
            if old:
                if old['fingerprint']!=sha:raise ApiError('idempotency_conflict')
                result=self.receipt_view(c,old); tx.commit();return 200,result
            if operation!='submitMessage' and current['state'] in TERMINAL and current['state']!='cancelled':
                raise ApiError('run_terminal')
            if operation=='cancelRun' and current['state']=='cancelled':
                # A new key still leaves a durable receipt for an already cancelled Run.
                pass
            elif current['revision']!=body['expected_revision']:
                raise ApiError('revision_conflict',body['expected_revision'],current['revision'])
            mid=None
            if operation=='submitMessage':
                reason = self.admission(c, actor, project)
                if reason:
                    raise live_failure(reason)
                active = row(c, '''SELECT id FROM aist.runs WHERE owner_id=:a AND project_id=:p
                    AND conversation_id=:conv AND state IN ('queued','running','waiting_for_user','recovery_required')''',
                    a=actor,p=project,conv=target)
                if active:raise ApiError('revision_conflict',body['expected_revision'],current['revision'])
                edit_plan=None
                if 'edit_request' in body:
                    from .edit_proposals import rewrite_input
                    edit_plan, ids = rewrite_input(self,c,actor,project,target,body)
                    body = dict(body, source_version_ids=ids)
                selection = body.get('project_context')
                project_pdfs, project_chats = [], []
                if selection:
                    from .project_materials import select_materials
                    project_pdfs, project_chats = select_materials(c, actor, current, selection)
                    ids = sorted(set(body['source_version_ids']) | {p['local_version_id'] for p in project_pdfs})
                    body = dict(body, source_version_ids=ids)
                config=self.eligibility(c,actor,project,body)
                from .extraction import VERSION as EXTRACTION_VERSION
                config['extraction_version'] = EXTRACTION_VERSION
                from .live_profile import FULL_EXTRACTION_IDS, profile_for
                config['extraction_thinking'] = (profile_for(body)['extraction_thinking'] if body['model_profile_version_id'] in FULL_EXTRACTION_IDS
                                                else self.settings.extraction_thinking)
                from .conversation import selected_history
                config['conversation_context'] = {} if edit_plan else selected_history(self.service, c, actor, current, config['pins'], project_chats)
                rid=ident('run');mid=ident('msg')
                input_value={k:body[k] for k in ('skill_version_id','model_profile_version_id','requested_result_type','parameters','base_version_id','expected_artifact_revision','requested_export')}
                # Keep legacy request fingerprints and Run inputs unchanged when the optional field is absent.
                if 'context_version_id' in body:
                    input_value['context_version_id'] = body['context_version_id']
                if selection:
                    input_value['project_context'] = dict(folder_id=selection['folder_id'], pdfs=project_pdfs,
                        chats=[{k: chat[k] for k in ('id','title','through_sequence')} for chat in project_chats])
                budget=config['policy']['run']
                from .collection import bounded_budget
                bounds=bounded_budget(budget,body['skill_version_id'],body['requested_result_type'],body['base_version_id'],body['model_profile_version_id'])
                if edit_plan is not None:
                    config['edit_plan']=edit_plan
                    input_value['edit_request']=body['edit_request']
                    budget=dict(budget,max_model_calls=1,max_duration_seconds=min(300,budget['max_duration_seconds']))
                elif bounds is not None:
                    # FLOW-01: a first note generation keeps the stricter of the server policy and the collection limits.
                    config['collection']=bounds
                    budget=dict(budget,max_model_calls=bounds['max_model_calls'],max_duration_seconds=bounds['max_duration_seconds'])
                    if 'max_tool_calls' in bounds:
                        budget['max_tool_calls'] = min(budget['max_tool_calls'], bounds['max_tool_calls'])
                input_value.update(source_versions=config.pop('pins'),target_artifact_id=config.pop('target_artifact_id',None),budget=budget)
                checked('RunInput',input_value)
                execute(c,'''INSERT INTO aist.runs(id,project_id,owner_id,conversation_id,message_id,session_id,state,input,configuration,request_sha256)
                  VALUES (:id,:p,:a,:conv,:mid,:session,'queued',CAST(:input AS jsonb),CAST(:config AS jsonb),:sha)''',id=rid,p=project,a=actor,conv=target,mid=mid,session=session,input=json_value(input_value),config=json_value(config),sha=sha)
                execute(c,'''INSERT INTO aist.messages(id,project_id,owner_id,conversation_id,sequence,role,text,run_id)
                  VALUES (:id,:p,:a,:conv,:seq,'user',:content,:run)''',id=mid,p=project,a=actor,conv=target,seq=current['revision']+1,content=body['text'],run=rid)
                execute(c,'UPDATE aist.conversations SET revision=revision+1 WHERE id=:id',id=target)
                for pin in input_value['source_versions']:
                    execute(c,'''INSERT INTO aist.run_sources(run_id,project_id,owner_id,source_version_id,sha256) VALUES (:r,:p,:a,:v,:sha)''',r=rid,p=project,a=actor,v=pin['source_version_id'],sha=pin['sha256'])
                r=self.scoped(c,actor,project,rid);self.event(c,r,'state_changed',{'from':None,'to':'queued'})
            else:
                r=current;rid=r['id']
                if operation=='cancelRun':
                    if r['state']!='cancelled':
                        if r['state'] in TERMINAL:raise ApiError('run_terminal')
                        self.event(c,r,'cancel_requested',{'actor_id':actor})
                        self.transition(c,r,'cancelled')
                else:
                    if r['state'] in TERMINAL:raise ApiError('run_terminal')
                    if r['state']!='waiting_for_user' or r['waiting_input']['id']!=body['waiting_input_id']:
                        raise ApiError('revision_conflict',body['expected_revision'],r['revision'])
                    if not r['checkpoint'] or not r['checkpoint'].get('safe_resume'):
                        raise ApiError('outcome_unknown')
                    if age_seconds(r) >= r['input']['budget']['max_duration_seconds']:
                        raise ApiError('budget_exceeded')
                    self.session_alive(c,r)
                    if r['configuration']['executor'] == 'openclaw':
                        if r['input']['model_profile_version_id'] != self.settings.model_profile_id:
                            raise ApiError('tool_unavailable')
                        self.require_live_access(actor, required='demo_access' in r['configuration']['policy'])
                        if not self.settings.live_models_enabled or self.worker_unavailable(c):
                            raise live_failure('temporarily_unavailable')
                    from .conversation import append_dialogue
                    checkpoint=append_dialogue(r['checkpoint'],'user',body['text'])
                    checkpoint['turn']=checkpoint.get('turn',0)+1
                    checkpoint['safe_resume']=False
                    mid=self.append_message(c,r,'user',body['text'])
                    execute(c,'UPDATE aist.runs SET checkpoint=CAST(:cp AS jsonb) WHERE id=:id',
                            cp=json_value(checkpoint),id=rid)
                    if r['configuration']['executor']=='openclaw':
                        execute(c,'''UPDATE aist.runs SET executor_run_id=:id,executor_session_id=:session
                            WHERE id=:id''',id=rid,session='agent:main:explicit:'+rid+':turn-'+str(checkpoint['turn']))
                    self.transition(c,r,'running')
                    execute(c,'UPDATE aist.runs SET worker_id=NULL,lease_until=NULL WHERE id=:id',id=rid)
            execute(c,'''INSERT INTO aist.run_receipts(actor_id,project_id,operation,target,key,fingerprint,run_id,message_id)
              VALUES (:a,:p,:op,:t,:key,:sha,:r,:m)''',a=actor,p=project,op=operation,t=target,key=key,sha=sha,r=rid,m=mid)
            result=self.receipt_view(c,self.receipt(c,actor,project,operation,target,key))
            committing=True;tx.commit()
            return (200 if operation=='cancelRun' else 202),result
        except BaseException:
            if not committing:
                tx.rollback();raise
            c.close()
            try:
                with self.engine.begin() as fresh:
                    self.project(fresh,actor,project,True)
                    old=self.receipt(fresh,actor,project,operation,target,key)
                    if old and old['fingerprint']==sha:return 200,self.receipt_view(fresh,old)
            except SQLAlchemyError:pass
            raise ApiError('outcome_unknown') from None
        finally:c.close()

    def eligibility(self,c,actor,project,body):
        if self.settings.executor_mode == 'disabled':
            raise ApiError('tool_unavailable')
        pins=[]
        for vid in body['source_version_ids']:
            v=self.service.scoped(c,actor,project,'source_versions',vid)
            from .search import require_factual
            require_factual(c,vid)
            self.service.inspect(c,v)
            if v['processing_state']!='ready':raise ApiError('source_not_ready')
            pins.append({k:v[vk] for k,vk in [('source_id','source_id'),('source_version_id','id'),('sha256','sha256')]})
        if not len(pins)<=3 or (not pins and body['requested_result_type']!='evidence_answer'):raise ApiError('source_limit')
        expected_type = body['requested_result_type']
        allowed_types = ('analytical_note', 'evidence_answer') if self.settings.executor_mode == 'openclaw' else ('evidence_answer',)
        if expected_type not in allowed_types:raise ApiError('unsupported_result_type')
        if body['requested_export'] is not None:raise ApiError('unsupported_format')
        target=None
        if body['base_version_id'] is not None:
            from .versions import parent_version
            base=self.service.scoped(c,actor,project,'artifact_versions',body['base_version_id'])
            target=base['artifact_id']
            head,saved,_=parent_version(self,c,actor,project,target,base['id'],body['expected_artifact_revision'])
            if head['result_type']!=body['requested_result_type']:raise ApiError('unsupported_result_type')
            if pins!=saved['provenance']['source_versions']:raise ApiError('resource_not_found')
        elif body['expected_artifact_revision']!=0:raise ApiError('revision_conflict',body['expected_artifact_revision'],0)
        # QA fixture versions are deliberately separate from planned product registry entries.
        if self.settings.executor_mode == 'openclaw':
            skill=next((s for s in self.registry['skills'] if s['id']==body['skill_version_id'] and s['status']=='enabled'),None)
            model=next((m for m in self.registry['model_profiles'] if m['id']==body['model_profile_version_id'] and m['status']=='enabled'),None)
            if not skill or not model or skill['result_type']!=expected_type:raise ApiError('tool_unavailable')
            from .live_profile import profile_for
            if profile_for(body)['transport'] != 'https_chat_completions' and (body['skill_version_id'] not in ('skill_analytical-note-v3', 'skill_source-answer-v3') or body['base_version_id'] is not None):
                raise ApiError('tool_unavailable')
            if len(pins)>skill['max_sources_execution']:raise ApiError('source_limit')
            kind = 'artifact_version' if body['base_version_id'] else 'source_version'
            if kind not in skill['input_kinds']:raise ApiError('tool_unavailable')
        elif self.settings.executor_mode!='qa-scripted' or body['skill_version_id']!='skill_qa-d07-v1' or body['model_profile_version_id']!='model_qa-d07-v1' or not self.policy:
            raise ApiError('tool_unavailable')
        elif len(pins)!=1:raise ApiError('source_limit')
        from .conversation import note_context
        context = note_context(self.service, c, actor, project, body, pins)
        version=self.policy['run']['policy_version']
        old=row(c,'SELECT * FROM aist.project_policies WHERE project_id=:p AND version=:v',p=project,v=version)
        if old and old['data']!=self.policy:raise ApiError('temporarily_unavailable')
        if not old:execute(c,'INSERT INTO aist.project_policies(project_id,owner_id,version,data) VALUES (:p,:a,:v,CAST(:d AS jsonb))',p=project,a=actor,v=version,d=json_value(self.policy))
        if self.settings.executor_mode == 'openclaw':
            configuration = {'target_artifact_id':target,'pins':pins,'policy':self.policy,'executor':'openclaw','skill_sha256':skill['bundle_sha256'],
                             'model_configuration_sha256':model['configuration_sha256']}
            if profile_for(body)['transport'] != 'https_chat_completions':
                configuration['model_profile'] = profile_for(body)
            if context is not None:
                configuration['note_context'] = context
            return configuration
        return {'target_artifact_id':target,'pins':pins,'policy':self.policy,'executor':'qa-scripted','skill_sha256':fingerprint({'fixture':'d07-scripted-v1'}),
                'model_configuration_sha256':fingerprint({'fixture':'d07-http-v1','policy':self.policy})}

    def get(self,actor,project,run):
        with self.engine.connect() as c:return self.view(self.scoped(c,actor,project,run))

    def listing(self,actor,project,table,q,target=None):
        definitions={'runs':('RunPage',self.view),'messages':('MessagePage',self.message_view),
            'model_calls':('CallPage',lambda r:validate_definition('ModelCall',r['data'])),
            'tool_calls':('ToolCallPage',lambda r:validate_definition('ToolCall',r['data'])),
            'usage_entries':('UsagePage',lambda r:validate_definition('UsageEntry',
                {key: value for key, value in r['data'].items() if key != 'settlement'}))}
        definition,mapper=definitions[table]
        with self.engine.connect() as c:
            self.project(c,actor,project)
            params={'p':project,'a':actor,'target':target,'limit':q.get('limit',50)+1}
            if table=='messages':
                self.service.scoped(c,actor,project,'conversations',target)
                scope='project_id=:p AND owner_id=:a AND conversation_id=:target';order='sequence'
            elif table=='runs':
                scope = ('project_id=:p AND owner_id=:a AND EXISTS (SELECT 1 FROM aist.conversations c '
                         'WHERE c.id=runs.conversation_id AND c.hidden_at IS NULL)')
                if chat := q.get('conversation_id'):
                    self.service.scoped(c,actor,project,'conversations',chat)
                    scope += ' AND conversation_id=:chat'
                    params['chat'] = chat
                order='created_at,id'
            else:
                self.scoped(c,actor,project,target)
                scope='run_id=:target';order='created_at,id'
            extra=''
            if q.get('after_id'):
                cursor=row(c,f'SELECT * FROM aist.{table} WHERE {scope} AND id=:id',**params,id=q['after_id'])
                if not cursor:raise ApiError('cursor_invalid')
                if table=='messages':extra=' AND sequence>:seq';params['seq']=cursor['sequence']
                else:extra=' AND (created_at,id)>(:created,:cursor)';params.update(created=cursor['created_at'],cursor=cursor['id'])
            values=[dict(x) for x in execute(c,f'SELECT * FROM aist.{table} WHERE {scope}{extra} ORDER BY {order} LIMIT :limit',**params).mappings()]
            more=len(values)>=params['limit'];values=values[:params['limit']-1]
            return validate_definition(definition,{'items':[mapper(v) for v in values],'next_after_id':values[-1]['id'] if more else None})

    def events(self,actor,project,run,q,last=None):
        page, _ = self.event_batch(actor, project, run, q, last)
        return page

    def event_batch(self,actor,project,run,q,last=None):
        with self.engine.begin() as c:
            # One snapshot even when a concurrent call publishes an event.
            execute(c,'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            r=self.scoped(c,actor,project,run)
            after=q.get('after_seq',0)
            if last is not None:
                checked('SseHeaders',{'Last-Event-ID':last})
                rid,seq=last.rsplit(':',1)
                if len(seq)>16 or rid!=run:raise ApiError('cursor_invalid')
                seq=int(seq)
                if 'after_seq' in q and after!=seq:raise ApiError('cursor_invalid')
                after=seq
            if after>r['last_event_seq']:raise ApiError('cursor_invalid')
            values=[x[0] for x in execute(c,'SELECT data FROM aist.run_events WHERE run_id=:r AND seq>:seq ORDER BY seq LIMIT :lim',r=run,seq=after,lim=q.get('limit',100)+1)]
            more=len(values)>q.get('limit',100);values=values[:q.get('limit',100)]
            page = validate_definition('EventPage', {'events':values,'next_after_seq':values[-1]['seq'] if values else after,'has_more':more})
            # The worker can still record the late provider response after a terminal transition.
            settled = r['state'] in TERMINAL and (r['worker_id'] is None or r['configuration']['executor'] != 'openclaw')
            return page, settled and not more

    def version(self,actor,project,version):
        with self.engine.connect() as c:
            v=self.service.scoped(c,actor,project,'artifact_versions',version)
            from .versions import view
            return view(c,v)

    def session_alive(self,c,r):
        active=execute(c,'''SELECT 1 FROM aist.sessions s JOIN aist.users u ON u.id=s.user_id
            WHERE s.id=:s AND s.user_id=:a AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.disabled_at IS NULL AND (u.access_expires_at IS NULL OR u.access_expires_at>clock_timestamp())
            FOR SHARE OF u,s''',s=r['session_id'],a=r['owner_id']).scalar_one_or_none()
        if not active:raise ApiError('unauthenticated')

    def fence(self,c,r,epoch,allow_completed=False):
        self.session_alive(c,r)
        valid=execute(c,'SELECT lease_until>clock_timestamp() FROM aist.runs WHERE id=:id',id=r['id']).scalar_one()
        if r['epoch']!=epoch or not valid:raise ApiError('outcome_unknown')
        if r['state']!='running' and not (allow_completed and r['state']=='completed'):raise ApiError('run_terminal')
        if age_seconds(r)>r['input']['budget']['max_duration_seconds']:raise ApiError('budget_exceeded')

    def claim(self,worker):
        with self.engine.begin() as c:
            r=row(c,'''SELECT * FROM aist.runs
              WHERE (state='queued' OR (state='running' AND worker_id IS NULL AND checkpoint IS NOT NULL))
                AND configuration->>'executor'=:executor
                AND (:executor<>'openclaw' OR input->>'model_profile_version_id'=:profile)
              ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1''',
              executor=self.settings.executor_mode,profile=self.settings.model_profile_id)
            if not r:return None
            try:self.session_alive(c,r)
            except ApiError:
                self.transition(c,r,'failed',error_value('unauthenticated'));return None
            for pin in r['input']['source_versions']:
                v=self.service.scoped(c,r['owner_id'],r['project_id'],'source_versions',pin['source_version_id'])
                self.service.inspect(c,v)
                if v['processing_state']!='ready':
                    self.transition(c,r,'failed',error_value('source_not_ready'));return None
            if r['state']=='queued':self.transition(c,r,'running')
            execute(c,'''UPDATE aist.runs SET epoch=epoch+1,worker_id=:w,lease_until=clock_timestamp()+:seconds*interval '1 second',
              executor_run_id=coalesce(executor_run_id,:er),executor_session_id=coalesce(executor_session_id,:es) WHERE id=:id''',w=worker,seconds=self.settings.lease_seconds,er=r['id'] if self.settings.executor_mode=='openclaw' else ident('exec'),es='agent:main:explicit:'+r['id'] if self.settings.executor_mode=='openclaw' else ident('session'),id=r['id'])
            return row(c,'SELECT * FROM aist.runs WHERE id=:id',id=r['id'])

    def heartbeat(self,run,epoch,worker):
        with self.engine.begin() as c:
            return execute(c,'''UPDATE aist.runs SET lease_until=clock_timestamp()+:seconds*interval '1 second'
              WHERE id=:r AND epoch=:epoch AND worker_id=:w AND lease_until>clock_timestamp()
              AND state IN ('running','waiting_for_user','completed','cancelled','failed','budget_exceeded') RETURNING id''',r=run,epoch=epoch,w=worker,seconds=self.settings.lease_seconds).scalar_one_or_none()

    def release_finished(self, run, epoch, worker):
        with self.engine.begin() as c:
            execute(c, '''UPDATE aist.runs SET worker_id=NULL,lease_until=NULL
                WHERE id=:r AND epoch=:epoch AND worker_id=:w AND lease_until>clock_timestamp()
                AND state IN ('completed','cancelled','failed','budget_exceeded','waiting_for_user')''',
                r=run, epoch=epoch, w=worker)

    def locked(self,c,run):
        base=row(c,'SELECT project_id,owner_id FROM aist.runs WHERE id=:id',id=run)
        if not base:raise ApiError('resource_not_found')
        self.project(c,base['owner_id'],base['project_id'],True)
        return self.scoped(c,base['owner_id'],base['project_id'],run,True)

    def recover(self,owned=None):
        with self.engine.connect() as c:
            sql="""SELECT id FROM aist.runs WHERE (lease_until<=clock_timestamp() AND worker_id IS NOT NULL)
                OR (configuration->'policy' ? 'demo_access' AND
                    (state='recovery_required' OR (state='waiting_for_user' AND
                     created_at + (input->'budget'->>'max_duration_seconds')::int * interval '1 second' <= :now)))"""
            params={'now': utcnow()}
            if owned is not None:sql+=' OR worker_id=:worker';params['worker']=owned
            ids=[x[0] for x in execute(c,sql,**params)]
        for rid in ids:
            with self.engine.begin() as c:
                r=self.locked(c,rid)
                demo = 'demo_access' in r['configuration']['policy']
                if demo and r['state'] == 'waiting_for_user' and age_seconds(r) >= r['input']['budget']['max_duration_seconds']:
                    self.transition(c, r, 'budget_exceeded', error_value('budget_exceeded'))
                    execute(c, 'UPDATE aist.runs SET epoch=epoch+1,worker_id=NULL,lease_until=NULL WHERE id=:r', r=rid)
                    continue
                if demo and r['state'] == 'recovery_required':
                    from .accounting import Accounting
                    Accounting(self).unknown_active(c, r, 'recovery without replay')
                    from .interrupted_note import recover_extraction
                    try:
                        with c.begin_nested():
                            recover_extraction(self, c, r)
                    except ApiError as error:
                        if error.code not in ('unauthenticated', 'tool_unavailable', 'resource_not_found', 'source_not_ready'):
                            self.append_message(c, r, 'assistant', 'Извлечение прервалось. Сохранённые материалы '
                                                'не прошли проверку для записи черновика.')
                    self.transition(c, r, 'failed', error_value('outcome_unknown'))
                    self.event(c, r, 'recovery_recorded', {'reason': 'Unknown outcome retained in ledger; no POST replay'})
                    execute(c, 'UPDATE aist.runs SET epoch=epoch+1,worker_id=NULL,lease_until=NULL WHERE id=:r', r=rid)
                    continue
                expired=execute(c,'SELECT lease_until<=clock_timestamp() FROM aist.runs WHERE id=:r',r=rid).scalar_one()
                if not expired and r['worker_id']!=owned:continue
                from .accounting import Accounting
                Accounting(self).unknown_active(c,r,'worker lease or shutdown deadline expired')
                if r['state']=='running':
                    self.transition(c,r,'recovery_required',error_value('outcome_unknown'))
                    self.event(c,r,'recovery_recorded',{'reason':'Executor context interrupted; no automatic POST replay'})
                execute(c,'UPDATE aist.runs SET epoch=epoch+1,worker_id=NULL,lease_until=NULL WHERE id=:r',r=rid)

        # During shutdown the worker holds its persistence lock. The next worker
        # can read the saved bytes after shutdown has fenced this epoch.
        if owned is None and ids:
            from .received_response import recover_observed
            for rid in ids:
                recover_observed(self, rid, wait=False)

    def checkpoint(self,run,epoch,value):
        with self.engine.begin() as c:
            r=self.locked(c,run);self.fence(c,r,epoch)
            execute(c,'UPDATE aist.runs SET checkpoint=CAST(:v AS jsonb) WHERE id=:r',v=json_value(value),r=run)

    def attach_executor(self, run, epoch, executor_run, executor_session):
        with self.engine.begin() as c:
            r=self.locked(c,run);self.fence(c,r,epoch)
            if not isinstance(executor_run,str) or not 1<=len(executor_run)<=200 or executor_session != r['executor_session_id'] or r['executor_run_id'] not in (run,executor_run):
                raise ApiError('resource_not_found')
            execute(c,'UPDATE aist.runs SET executor_run_id=:e WHERE id=:r',e=executor_run,r=run)

    def stop(self,run,epoch,code, *, message=None):
        with self.engine.begin() as c:
            r=self.locked(c,run)
            if r['state'] in TERMINAL or r['epoch']!=epoch:return
            error=error_value(code)
            if message is not None:error['message']=message
            self.transition(c,r,'budget_exceeded' if code=='budget_exceeded' else 'failed',error)

    def tool(self,run,epoch,executor_id,name,args,model=None,*,obtain_facts=None,read_task=None,research=None,research_id=None):
        from .note_external import WEB_TOOLS, web_tool, research_tool
        try:
            if name in WEB_TOOLS:
                return web_tool(self, run, epoch, executor_id, name, args, model, research_id)
            if name == 'aist_research':
                return research_tool(self, run, epoch, executor_id, args, model, research)
            return self._tool_once(run,epoch,executor_id,name,args,model,obtain_facts=obtain_facts,read_task=read_task)
        except SQLAlchemyError:
            # Do not repeat work after an uncertain acknowledgement. Reconcile the durable reply.
            try:
                with self.engine.begin() as c:
                    r=self.locked(c,run)
                    old=row(c,'SELECT * FROM aist.tool_calls WHERE run_id=:r AND executor_tool_call_id=:e',r=run,e=executor_id)
                    self.fence(c,r,epoch,allow_completed=bool(old and name=='aist_save_artifact'))
                    if old and old['data']['arguments_sha256']==fingerprint({'name':name,'args':args}):return old['reply']
            except SQLAlchemyError:pass
            raise ApiError('outcome_unknown') from None

    def _authorize_tool(self, connection, run_id, epoch, executor_id, name, args, model):
        run = self.locked(connection, run_id)
        if 'edit_request' in run['input']:
            raise ApiError('tool_unavailable')
        saved = row(connection, 'SELECT * FROM aist.tool_calls WHERE run_id=:run AND executor_tool_call_id=:executor',
                    run=run_id, executor=executor_id)
        if saved is None and name=='aist_save_artifact':
            saved=row(connection, """SELECT * FROM aist.tool_calls WHERE run_id=:run
                AND data->>'name'='aist_save_artifact' AND data->>'state'='completed'
                AND data->>'arguments_sha256'=:digest LIMIT 1""",
                run=run_id,digest=fingerprint({'name':name,'args':args}))
            if saved:
                saved['reply']=dict(saved['reply'],tool_call_id=executor_id)
        self.fence(connection, run, epoch, allow_completed=bool(saved and name == 'aist_save_artifact'))
        arguments_hash = fingerprint({'name': name, 'args': args})
        if saved and saved['data']['arguments_sha256'] != arguments_hash:
            raise ApiError('idempotency_conflict')
        if name in ('aist_web_search', 'aist_web_fetch'):
            from .note_external import close_inactive_web
            close_inactive_web(self, connection, run)
        if not saved:
            reserved = row(connection, """SELECT w.*,t.reply FROM aist.web_requests w
                LEFT JOIN aist.tool_calls t ON t.id=w.id WHERE w.run_id=:run
                AND w.executor_tool_call_id=:executor LIMIT 1""",
                run=run_id, executor=executor_id)
            if reserved:
                if reserved['arguments_sha256'] != arguments_hash:
                    raise ApiError('idempotency_conflict')
                reply = reserved['reply'] or dict(ok=False, error=dict(error_value('outcome_unknown'), run_id=run_id))
                reply['tool_call_id'] = executor_id
                return run, {'reply': reply}
        if name == 'aist_get_facts' and isinstance(args.get('extract'), dict) and args['extract'].get('source_version_id'):
            # A cached empty result or refusal still requires the pinned, intact source.
            from .extraction import source_pages
            from .search import require_factual
            source_pages(self, connection, run, args['extract']['source_version_id'])
            require_factual(connection, args['extract']['source_version_id'])
        if saved:
            return run, saved
        if name == 'aist_read_source':
            checked('ReadSourceArgs', args)
            if args['source_version_id'] not in {pin['source_version_id'] for pin in run['input']['source_versions']}:
                raise ApiError('resource_not_found')
            self.service.scoped(connection, run['owner_id'], run['project_id'], 'source_versions', args['source_version_id'])
        if name not in ('aist_read_source', 'aist_request_input', 'aist_save_artifact', 'aist_get_facts', 'aist_search_evidence', 'aist_calculate', 'aist_web_search', 'aist_web_fetch', 'aist_read_task', 'aist_read_web', 'aist_research'):
            raise ApiError('tool_unavailable')
        if (run.get('external_context') or {}).get('research_pending') and name not in ('aist_web_search', 'aist_web_fetch', 'aist_read_web'):
            raise ApiError('outcome_unknown')
        if model and not row(connection, 'SELECT id FROM aist.model_calls WHERE id=:id AND run_id=:run', id=model, run=run_id):
            raise ApiError('resource_not_found')
        count = execute(connection, 'SELECT count(*) FROM aist.tool_calls WHERE run_id=:run', run=run_id).scalar_one()
        pending_web = execute(connection, "SELECT count(*) FROM aist.web_requests WHERE run_id=:run AND state='reserved'", run=run_id).scalar_one()
        if pending_web and not name.startswith('aist_web_'):
            raise ApiError('outcome_unknown')
        count += pending_web
        # Родительское исследование также должно поместиться в лимит после дочерних инструментов.
        count += int(bool((run.get('external_context') or {}).get('research_pending')))
        if count >= run['input']['budget']['max_tool_calls']:
            raise ApiError('budget_exceeded')
        # Replaying a known invalid operation cannot create another provider POST.
        # A changed question/locator has a different existing operation hash.
        if name == 'aist_get_facts' and args.get('extract') is not None:
            previous = row(connection, """SELECT * FROM aist.tool_calls
                WHERE run_id=:run AND data->>'arguments_sha256'=:digest
                  AND data->'error'->>'code' IN ('validation_error', 'validation_failed')
                ORDER BY data->>'ended_at' DESC LIMIT 1""", run=run_id, digest=arguments_hash)
            if previous:
                previous['reply'] = dict(previous['reply'], tool_call_id=executor_id)
                return run, previous
        return run, None

    def _record_tool(self, connection, run, epoch, executor_id, name, args, model, started_at, result, error, *,
                     reserved_id=None, arguments_sha256=None):
        tool_id = reserved_id or ident('tool')
        data = dict(id=tool_id, run_id=run['id'], model_call_id=model, executor_tool_call_id=executor_id, name=name,
                    arguments_sha256=arguments_sha256 or fingerprint({'name': name, 'args': args}),
                    state='completed', result_id=None,
                    error=None, started_at=started_at, ended_at=now())
        if error is None:
            reply = dict(ok=True, tool_call_id=executor_id, result=result)
            if isinstance(result, dict):
                data['result_id'] = result.get('version', {}).get('id') or result.get('calculation_id')
        else:
            # The executor correlation may contain a compound Responses identifier.
            # Public errors refer to the durable AIST ToolCall, not the executor string.
            failure = dict(error.body['error'], run_id=run['id'], tool_call_id=tool_id)
            reply = dict(ok=False, tool_call_id=executor_id, error=failure)
            data.update(state='failed', error=failure)
            # A refused nested extraction that closed a bounded collection keeps the Run (collection.overload_refusal).
            if error.code in ('tool_failed', 'provider_error') and run['state'] == 'running' and not getattr(error, 'run_continues', False):
                self.transition(connection, run, 'failed', failure)
        validate_definition('ToolCall', data)
        execute(connection, """INSERT INTO aist.tool_calls
            (id,run_id,project_id,owner_id,executor_tool_call_id,epoch,data,reply)
            VALUES (:id,:run,:project,:owner,:executor,:epoch,CAST(:data AS jsonb),CAST(:reply AS jsonb))""",
            id=tool_id, run=run['id'], project=run['project_id'], owner=run['owner_id'], executor=executor_id,
            epoch=epoch, data=json_value(data), reply=json_value(reply))
        self.event(connection, run, 'tool_finished', {'tool_call_id': tool_id, 'state': data['state'], 'error': data['error']},
                   call=model, tool=tool_id)
        return reply

    def _tool_once(self, run_id, epoch, executor_id, name, args, model=None, *, obtain_facts=None, read_task=None):
        from .tools import invoke
        semantic_search = name == 'aist_search_evidence' and args.get('method') == 'semantic.qwen3.v1'
        extraction = name == 'aist_get_facts' and args.get('extract') is not None
        with self.engine.begin() as connection:
            run, saved = self._authorize_tool(connection, run_id, epoch, executor_id, name, args, model)
            if saved is not None:
                return saved['reply']
            started_at = now()
            from .note_external import close_research
            if name == 'aist_read_web':
                from .note_external import read_web
                result = read_web(run, args)
                return self._record_tool(connection, run, epoch, executor_id, name, args, model, started_at, result, None)
            close_research(connection, run, finalized=name == 'aist_save_artifact')
            if name == 'aist_read_task':
                checked('ReadTaskArgs', args)
                if read_task is None:
                    raise ApiError('tool_unavailable')
                result = dict(message=read_task(run))
                return self._record_tool(connection, run, epoch, executor_id, name, args, model, started_at, result, None)
            if extraction:
                try:
                    checked('GetFactsArgs', args)
                except ApiError as failure:
                    return self._record_tool(connection, run, epoch, executor_id, name, args, model, started_at, None, failure)
            if not semantic_search and not extraction:
                result, error = None, None
                try:
                    result = invoke(self, connection, run, epoch, name, args)
                except ApiError as failure:
                    error = failure
                return self._record_tool(connection, run, epoch, executor_id, name, args, model, started_at, result, error)

        # Retrieval releases the Run lock. Providers authorize again at reserve/send;
        # the final transaction below suppresses results after cancel or lease loss.
        result, error = None, None
        try:
            if extraction:
                if obtain_facts is None:raise ApiError('tool_unavailable')
                result = obtain_facts(run)
            else:
                result = invoke(self, None, run, epoch, name, args)
        except ApiError as failure:
            error = failure
        with self.engine.begin() as connection:
            run, saved = self._authorize_tool(connection, run_id, epoch, executor_id, name, args, model)
            if saved is not None:
                return saved['reply']
            return self._record_tool(connection, run, epoch, executor_id, name, args, model, started_at, result, error)
