import React, {useEffect} from 'react';
import { findingPlace, groupFindings, decisionQuestions, type Finding } from './review';
import type { VersionView } from './types';

// The reader and editor show the same original messages and severity.
export function ReviewFindings({ findings, value, blockIds }: { findings: Finding[]; value?: VersionView; blockIds?: Set<string> }) {
  return <>{groupFindings(findings).filter(group => group.findings.length).map(group =>
    <details className="finding-group" key={group.key} data-finding-group={group.key}>
      <summary>{group.label}: {group.findings.length}</summary>
      <ul>{group.findings.map((finding, index) => <li className="finding" key={index} data-finding-code={finding.code} data-severity={finding.severity}>
        {value && <strong>{findingPlace(value, finding.block_id) || 'К записке в целом'}: </strong>}
        {finding.message} {value&&finding.block_id&&(!blockIds||blockIds.has(finding.block_id))&&!value.omitted_note_blocks?.includes(finding.block_id)&&<a href={'#note-'+finding.block_id}>К месту в записке</a>}
        <span className="finding-severity">{finding.severity==='error'?'Требует сверки перед принятием версии.':finding.severity==='warning'?'Предупреждение':'К сведению'}</span>
      </li>)}</ul>
    </details>
  )}</>;
}

function revealChecks(){
  const panel=document.getElementById('note-checks');
  if(!(panel instanceof HTMLDetailsElement))return;
  panel.open=true;
  const protocol=panel.querySelector('details');
  if(protocol)protocol.open=true;
  panel.scrollIntoView({block:'center'});panel.focus();
}

export function AllFindings({value,blockIds}:{value:VersionView;blockIds?:Set<string>}){
  useEffect(()=>{
    const reveal=()=>{if(location.hash==='#note-checks')revealChecks();};
    reveal();window.addEventListener('hashchange',reveal);return()=>window.removeEventListener('hashchange',reveal);
  },[value.version.id]);
  const findings=value.checks.flatMap(check=>check.findings);
  return <details className="all-findings" id="note-checks" tabIndex={-1}><summary>Проверка версии</summary>
    <DecisionQuestions value={value} blockIds={blockIds}/>
    <details><summary>Полный протокол · {findings.length}</summary><ReviewFindings findings={findings} value={value} blockIds={blockIds}/></details>
  </details>;
}

export function DecisionQuestions({value,blockIds,onNavigate}:{value:VersionView;blockIds?:Set<string>;onNavigate?:(target:string)=>void}){
  const decisions=decisionQuestions(value);
  if(!decisions.length)return null;
  function navigate(e:React.MouseEvent<HTMLAnchorElement>,target:string){
    if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
    e.preventDefault();
    if(onNavigate)onNavigate(target);
    else {location.hash=target;document.getElementById(target)?.scrollIntoView({block:'center'});}
    if(target==='note-checks')revealChecks();
  }
  return <section className="decision-questions" aria-label="Требует сверки">
    <h4>Требует сверки · {decisions.length}</h4>
    <p className="reading-key">Жёлтым в записке выделены места для сверки с источником.</p>
    <ol>{decisions.map((f,i)=>{
      const places=f.locations.filter(p=>!blockIds||blockIds.has(p.blockId));
      const first=places[0],target=first&&!f.detailsOnly?'note-'+(first.rowId??first.blockId):'note-checks';
      return <li key={i}>
        <a className="decision-question" href={'#'+target} onClick={e=>navigate(e,target)}><span className="question-number" aria-hidden="true">{i+1}</span><span>{f.text}</span></a>
        {places.length>1&&<div className="question-places">Также в записке: {places.slice(1).map((p,n)=>{
          const id='note-'+(p.rowId??p.blockId);
          return <a key={id} href={'#'+id} onClick={e=>navigate(e,id)}>Фрагмент {n+2}</a>;
        })}</div>}
      </li>;
    })}</ol>
  </section>;
}
