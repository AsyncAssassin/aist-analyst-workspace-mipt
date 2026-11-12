import React,{useEffect,useRef,useState} from 'react';
import {ApiError,Client,errorText} from './client';
import {Operations,finishActiveEdit,readLocal,writeLocal} from './local';
import type {Registry,Run} from './types';

// Правка команды относится к шапке версии и не имеет раздела и блока.
type Target={section_id?:string;block_id?:string;row_id?:string;field?:string};
export type Proposal={id:string;version_id:string;base_sha:string;revision:number;place:string;target:Target;command:string;stale:boolean;
  before:string;after:string;state:'pending'|'rejected'|'applied';applied_version_id:string|null;run_id:string|null;anchor:number};
type Plan={kind:'created';proposal:Proposal}|{kind:'hint';message:string}|
  {kind:'rewrite';version_id:string;target:Target}|{kind:'choose';version_id:string;choices:{target:Target;label:string}[]};
type Rewrite={plan:Extract<Plan,{kind:'rewrite'}>;text:string;key:string;revision:number};
// JavaScript word boundaries are ASCII-only, including around Russian verbs.
export function wantsEdit(text:string){return /^(?:пожалуйста[, ]+)?(?:замени|заменить|поменяй|исправь|измени|сократи|укороти|сделай (?:короче|кратче))(?=\s|$)/iu.test(text.trim());}

export function useEditCommand(client:Client, scope:string, path:string, revision:number|null, finish:(run?:Run,proposal?:Proposal)=>void) {
  const [prepare]=useState(()=>new Operations(scope+'.proposal')), [submit]=useState(()=>new Operations(scope+'.rewrite-submit'));
  const [rewrite,setRewrite]=useState(()=>readLocal<Rewrite|null>(scope+'.rewrite',null));
  const [choice,setChoice]=useState<{text:string;plan:Extract<Plan,{kind:'choose'}>}|null>(null);
  const [hint,setHint]=useState('');
  const clear=()=>{setChoice(null);setHint('');};
  const saveRewrite=(v:Rewrite|null)=>{writeLocal(scope+'.rewrite',v);setRewrite(v);};
  async function send(text:string, version:string|null, target?:Target){
    clear();
    let next=rewrite;
    try{
      if(!next&&!submit.pending){
        if(!prepare.pending){
          const saved=await finishActiveEdit();
          prepare.prepare(path+'/edit-proposals',{version_id:saved?.id??version,text,...(target?{target}:{})},'EditPlan','Предложение правки');
        }
        const op=prepare.pending!;
        const original=JSON.parse(op.body).text as string;
        const result=await prepare.send<Plan>(client,r=>{
          if(r.kind==='rewrite'){next={plan:r,text:original,key:op.key,revision:revision!};saveRewrite(next);}
        });
        if(result.kind==='hint'){setHint(result.message);return;}
        if(result.kind==='choose'){setChoice({text:original,plan:result});return;}
        if(result.kind==='created'){finish(undefined,result.proposal);return;}
      }
      if(next&&!submit.pending){
        const registry=await client.json<Registry>('/v1/capabilities','Registry');
        const model=registry.model_profiles.find(m=>m.status==='enabled'&&m.configuration_sha256);
        if(!model||registry.model_execution?.state!=='available'){setHint(registry.model_execution?.message??'Модель недоступна.');return;}
        submit.prepare(path+'/messages',{expected_revision:next.revision,text:next.text,source_version_ids:[],
          skill_version_id:'skill_source-answer-v3',model_profile_version_id:model.id,requested_result_type:'evidence_answer',
          parameters:{audience:null,sections:[]},base_version_id:null,expected_artifact_revision:0,requested_export:null,
          edit_request:{version_id:next.plan.version_id,target:next.plan.target}},'Submission','Сокращение абзаца',next.key);
      }
      await submit.send<{run:Run}>(client,reply=>{saveRewrite(null);finish(reply.run);});
    }catch(e){
      if(e instanceof ApiError&&e.status<500&&!prepare.pending&&!submit.pending){
        saveRewrite(null);
        if(e.code==='revision_conflict'){setHint('Версия изменилась. Откройте актуальную записку и предложите правку заново.');return;}
        if(e.code==='resource_not_found'){setHint('Откройте чат с сохранённой запиской.');return;}
      }
      throw e;
    }
  }
  return {send,choice,hint,clear,canDiscard:!!rewrite&&!submit.pending,discard:()=>{saveRewrite(null);clear();},pending:!!prepare.pending||!!submit.pending||!!rewrite};
}

export function ProposalCard({client,path,value,onDecision,onOpen,stale,messageId}:{client:Client;path:string;value:Proposal;
  onDecision:(p:Proposal)=>void;onOpen:(id:string)=>void;stale:boolean;messageId?:string}){
  const [ops]=useState(()=>new Operations('decision.'+value.id)),lock=useRef(false);
  const [busy,setBusy]=useState(false),[conflict,setConflict]=useState(false),[error,setError]=useState('');
  useEffect(()=>{
    if(value.state!=='pending'&&ops.pending)writeLocal('decision.'+value.id,null);
  },[value.state,value.id,ops]);
  const display=(text:string)=>value.target.field==='amount'?
    text.split('.').map((part,i)=>i?part:part.replace(/\B(?=(\d{3})+$)/g,' ')).join(','):text;
  async function decide(decision:'apply'|'reject'){
    if(lock.current)return;
    lock.current=true;setBusy(true);setError('');
    try{
      if(!ops.pending){
        if(decision==='apply')await finishActiveEdit();
        ops.prepare(path+'/edit-proposals/'+value.id,{decision},'EditProposal','Решение по правке');
      }
      onDecision(await ops.send<Proposal>(client));
    }catch(e){
      if(e instanceof ApiError&&e.code==='revision_conflict'){setConflict(true);onDecision(value);}
      else if(!client.signal.aborted)setError(errorText(e));
    }finally{lock.current=false;if(!client.signal.aborted)setBusy(false);}
  }
  return <article className="message assistant edit-proposal" aria-label="Предложение правки" data-proposal-id={value.id} data-message-id={messageId} tabIndex={-1}>
    <strong>Предложение для версии {value.revision}</strong><p>{value.place}</p>
    <div className="edit-diff"><div><b>Было</b><p>{display(value.before)}</p></div><div><b>Предлагается</b><p>{display(value.after)}</p></div></div>
    {value.state==='applied'?<button onClick={()=>onOpen(value.applied_version_id!)}>Открыть применённую правку · v{value.revision+1}</button>:
      value.state==='rejected'?<p>Предложение отклонено</p>:<>
        {(value.stale||stale||conflict)&&<p role="status">Появилась более новая версия. Для неё нужна новая правка.</p>}
        {ops.pending?<button disabled={busy} onClick={()=>void decide('apply')}>Проверить решение</button>:<div className="actions">
          <button className="apply-edit" disabled={busy||value.stale||stale||conflict} onClick={()=>void decide('apply')}>Применить</button>
          <button className="reject-edit" disabled={busy} onClick={()=>void decide('reject')}>Отклонить</button></div>}
      </>}
    {error&&<p role="alert">{error}</p>}
  </article>;
}
