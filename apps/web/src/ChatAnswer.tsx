import {stateLabels, versionState} from './review';
import {WorkspaceIcon} from './WorkspaceIcon';
import React, { useEffect, useState } from 'react';
import { Client, errorText } from './client';
import { DecisionQuestions } from './ReviewFindings';
import { SourceReader } from './Sources';
import type { Claim, Fact, SourceVersion, VersionView } from './types';

// Read the immutable version linked by the message, never the artifact's current head.
export function ChatAnswer({client,project,id,message,versions,onOpen,refreshVersion,modern,contextId}:{contextId?:string;modern?:boolean;refreshVersion?:string;client:Client;project:string;id:string;message:string;versions:SourceVersion[];onOpen:(id:string,exports?:boolean)=>void}) {
  const [value,setValue]=useState<VersionView|null>(null);
  const [latest,setLatest]=useState<VersionView|null>(null);
  const [error,setError]=useState(''); const [retry,setRetry]=useState(0);
  const [viewer,setViewer]=useState<{version:SourceVersion;page:number}|null>(null);
  useEffect(()=>{
    const c=client.child();
    void c.json<VersionView>(`/v1/projects/${project}/artifact-versions/${id}`,'VersionView')
      .then(async original=>{
        setValue(original);setLatest(null);setError('');
        if(original.version.payload.result_type==='analytical_note'){
          const artifact=await c.json<{latest_version_id:string}>(`/v1/projects/${project}/artifacts/${original.version.artifact_id}`,'Artifact');
          const head=artifact.latest_version_id===id?original:await c.json<VersionView>(
            `/v1/projects/${project}/artifact-versions/${artifact.latest_version_id}`,'VersionView');
          setLatest(head);
        }
      }).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return ()=>c.close();
  },[client,project,id,retry,refreshVersion]);
  if(error)return <><p>{message}</p><p className="error">{error} <button className="text-button" onClick={()=>setRetry(n=>n+1)}>Прочитать основания повторно</button></p></>;
  if(!value)return <p>{message}</p>;
  const v=value.version;
  if(v.payload.result_type==='analytical_note'){
    if(!latest)return <p>Открываем карточку записки…</p>;
    const head=latest.version;
    const card=<section className="note-card" data-card-version={id} data-latest-version={head.id}>
      <small>Аналитическая записка · актуальная v{head.revision}</small><h3>{head.payload.content.title}</h3>
      <p>{stateLabels[versionState(latest)]}</p>

      <div className="actions"><button onClick={()=>onOpen(head.id)}>Открыть актуальную записку</button>
        {modern&&<button className="secondary" onClick={()=>onOpen(head.id,true)}><WorkspaceIcon name="download"/>Скачать</button>}
        {head.id!==id&&<button className="text-button" onClick={()=>onOpen(id)}>Версия сообщения · v{v.revision}</button>}</div>
    </section>;
    const context=head.basis.external_context;
    const topicNames:Record<string,string>={owners:'собственники',market:'рынок',competitor_revenue:'материалы о конкурентах',business:'бизнес и клиенты',deals:'публичные сделки',news:'новости'};
    const gaps=context&&Object.entries(context.topics).filter(([,status])=>!['found','collected'].includes(status)).map(([topic,status])=>
      (topicNames[topic]??topic)+': '+({empty:'материалы не отобраны',unavailable:'не удалось получить пригодный текст',unmatched:'компания не определена',budget:'поиск не завершен'}[status]??status));
    const news=head.basis.news_run;
    const newsStatus=news&&{
      found:'События сохранены со ссылками на источники.',
      partial:'Часть источников недоступна. Сохранены доступные публикации.',
      empty:'События для News Run не отобраны. Это не подтверждает отсутствие новостей.',
      not_searched:'Поиск новостей не выполнялся.',
      unmatched:'Не удалось однозначно связать компанию из материалов с публичным источником.',
      unavailable:'Не удалось получить публикации. Записка подготовлена по выбранным материалам.',
    }[news.status];
    const warnings=<DecisionQuestions value={latest} onNavigate={target=>{
      location.hash=target;onOpen(head.id);document.getElementById(target)?.scrollIntoView({block:'center'});
    }}/>;
    return <><p className="note-ready">{message || 'Записка готова.'}</p>{card}{context&&<p className="small muted">Первоначальный поиск · {context.as_of}. {context.status==='unmatched'?'Не удалось однозначно определить компанию по материалам.':<>Собранные материалы не подтверждают полноту темы. {gaps?.length?gaps.join('; ')+'.':'Смысл, принадлежность сведений и актуальность проверяет читатель.'}</>}</p>}{news&&<p className="small muted">News Run · {news.as_of}. {news.scope==='market'?'Подборка по рынку. ':''}{newsStatus}</p>}{warnings}</>;
  }
  return <div className="chat-basis">
    {contextId&&<EditedCells client={client} project={project} id={contextId} answer={v.payload.content.claims} facts={v.basis.facts} onOpen={onOpen}/>}
    {v.payload.content.claims.map((claim,index)=><div key={index}><p>{claim.text}</p>
      <AnswerSources claim={claim} facts={v.basis.facts} versions={versions} onOpen={(version,page)=>setViewer({version,page})}/>
    </div>)}
    {v.payload.content.missing_information.map((line,index)=><p key={index}>{line}</p>)}
    {message && ![...v.payload.content.claims.map(c=>c.text),...v.payload.content.missing_information].join('\n').startsWith(message) && <p>{message}</p>}
    <details><summary>Основания и расчеты</summary>
    {v.basis.facts.map(f=>{
      const source=versions.find(s=>s.id===f.source.source_version_id);
      const locations=[...new Set(f.evidence.map(e=>e.locator.pdf_page ? `стр. ${e.locator.pdf_page}` : e.locator.json_pointer))].join(', ');
      return <details key={`${f.source.source_version_id}:${f.fact_id}`}>
        <summary>{f.metric} · {f.period.label} · {locations}</summary>
        {f.evidence.map(e=><div key={e.evidence_id}><blockquote>{e.quote}</blockquote>
          {source&&<button className="text-button" onClick={()=>setViewer({version:source,page:e.locator.pdf_page??1})}>Основание: {source.original_filename}, {e.locator.pdf_page ? `страница ${e.locator.pdf_page}` : 'снимок'}</button>}
        </div>)}
      </details>;
    })}
    {v.basis.calculations.map(c=><details key={c.calculation_id}><summary>Расчет: {c.rounded.value.replace('.',',')}%</summary><p className="mono small">{c.formula_display}</p><p className="small">Точное значение: {c.exact.numerator}/{c.exact.denominator} процента. Округление HALF_UP.</p></details>)}
    </details>
    {value.checks.some(c=>c.status==='failed')&&<p className="error">В проверке ответа есть замечания. Откройте сохраненную версию.</p>}
    <button className="text-button small" onClick={()=>onOpen(id)}>Сохраненная версия и проверки</button>
    {!v.basis.facts.length && <div className="actions">{v.provenance.source_versions.map(pin=>{
      const source=versions.find(s=>s.id===pin.source_version_id);
      return source && <button className="text-button small" key={source.id} onClick={()=>setViewer({version:source,page:1})}>Источник: {source.original_filename}</button>;
    })}</div>}
    {viewer&&<SourceReader key={viewer.version.id+viewer.page} client={client} project={project} version={viewer.version} initialPage={viewer.page} onClose={()=>setViewer(null)}/>}
  </div>;
}

function AnswerSources({claim,facts,versions,onOpen}: {
  claim:Claim; facts:Fact[]; versions:SourceVersion[]; onOpen:(source:SourceVersion,page:number)=>void;
}) {
  const groups = new Map<SourceVersion,Set<number|string>>();
  for (const ref of claim.fact_refs) {
    const source = versions.find(v=>v.id===ref.source_version_id);
    const fact = facts.find(f=>f.fact_id===ref.fact_id && f.source.source_version_id===ref.source_version_id);
    if (!source || !fact) continue;
    const places = groups.get(source) ?? new Set<number|string>();
    for (const {locator} of fact.evidence) {
      const place = locator.pdf_page ?? locator.json_pointer;
      if (place!==undefined) places.add(place);
    }
    if (places.size) groups.set(source,places);
  }
  return <>{Array.from(groups,([source,places])=><div className="answer-links" key={source.id}>
    <span>{source.original_filename}</span>
    {Array.from(places,place=>{
      const label = typeof place==='number' ? `стр. ${place}` : place;
      return <button key={place} className="text-button" aria-label={source.original_filename+' · '+label}
        onClick={()=>onOpen(source,typeof place==='number'?place:1)}>{label}</button>;
    })}
  </div>)}</>;
}

function EditedCells({client,project,id,answer,facts,onOpen}:{client:Client;project:string;id:string;answer:Claim[];facts:Fact[];onOpen:(id:string)=>void}){
  const [value,setValue]=useState<VersionView|null>(null),[failed,setFailed]=useState(false);
  useEffect(()=>{const c=client.child();setValue(null);setFailed(false);
    void c.json<VersionView>('/v1/projects/'+project+'/artifact-versions/'+id,'VersionView').then(setValue).catch(()=>{if(!c.signal.aborted)setFailed(true);});
    return()=>c.close();
  },[client,project,id]);
  if(failed)return <p role="alert">Не удалось сверить ответ с редакторскими правками. <button className="text-button" onClick={()=>onOpen(id)}>Открыть выбранную версию</button></p>;
  if(!value||value.version.id!==id)return null;
  const refs=new Set(answer.flatMap(claim=>claim.fact_refs.map(r=>r.source_version_id+':'+r.fact_id)));
  const disputed=new Set(value.checks.flatMap(c=>c.findings.filter(f=>f.code==='row_basis_mismatch'||f.code==='row_label_mismatch').map(f=>f.row_id)));
  const tableRows=value.version.payload.content.sections?.flatMap(s=>s.blocks.flatMap(b=>b.kind==='table'?b.rows:[]))??[];
  const visibleFacts=new Set(tableRows.flatMap(r=>r.fact_refs.map(f=>f.source_version_id+':'+f.fact_id)));
  const missing=value.version.provenance.origin==='human_edit'?facts.filter(f=>f.amount&&refs.has(f.source.source_version_id+':'+f.fact_id)&&!visibleFacts.has(f.source.source_version_id+':'+f.fact_id)):[];
  const rows=tableRows.filter(r=>disputed.has(r.row_id)&&r.fact_refs.some(f=>refs.has(f.source_version_id+':'+f.fact_id)));
  if(!rows.length&&!missing.length)return null;
  return <aside className="notice"><strong>В выбранной записке v{value.version.revision}</strong>
    {rows.map(r=><p key={r.row_id}>{r.label}, {r.period_label}: {r.amount.value.replace('.',',')} {({1000:'тыс.',1000000:'млн',1000000000:'млрд',1000000000000:'трлн'} as Record<string,string>)[r.amount.scale]} {r.amount.currency??r.amount.unit}. Требует сверки.</p>)}
    {missing.map(f=><p key={f.source.source_version_id+':'+f.fact_id}>В таблицах этой версии отсутствует строка по показателю «{f.metric}» за {f.period.label} из ответа.</p>)}
    <button className="text-button" onClick={()=>onOpen(id)}>Открыть эту версию</button><p>Ниже приведён ответ по источникам.</p></aside>;
}
