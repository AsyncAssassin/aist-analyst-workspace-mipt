import React, { useEffect, useRef, useState } from 'react';
import { Client, errorText } from './client';
import { SourceReader } from './Sources';
import type { Pin, SourceVersion } from './types';

type Hit = { evidence: {source: Pin; locator: {pdf_page?:number;json_pointer?:string}; quote:string}; title:string; revision:number; corpus:'factual'|'samples'; score:number|null };
type Result = {method:string; hits:Hit[]; qualification:string};
export function KnowledgeSearch({client,project,versions,selected}:{client:Client;project:string;versions:SourceVersion[];selected:string[]}) {
  const [query,setQuery]=useState(''); const [scope,setScope]=useState('project');
  const [corpus,setCorpus]=useState('factual');const [method,setMethod]=useState('lexical.v1');
  const [result,setResult]=useState<Result|null>(null);const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const [viewer,setViewer]=useState<{version:SourceVersion;page:number}|null>(null);
  const pending=useRef<Client|null>(null);
  useEffect(()=>()=>pending.current?.close(),[]);
  useEffect(()=>{pending.current?.close();setResult(null);setError('');setBusy(false);},[scope,corpus,method,selected.join(','),versions.map(v=>v.id).join(',')]);
  async function search() {
    pending.current?.close();const c=client.child();pending.current=c;setBusy(true);setError('');setResult(null);
    const ids=(scope==='selected'?selected:versions.map(v=>v.id)).filter(id=>versions.some(v=>v.id===id && v.processing_state==='ready' && (v.corpus??'factual')===corpus));
    if (!ids.length) {setError('В выбранной области нет доступных материалов.');setBusy(false);return;}
    try {
      const value=await c.json<Result>(`/v1/projects/${project}/search`,'SearchEvidenceResult',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:query.trim(),source_version_ids:ids,corpus,method,limit:5})});
      if(!c.signal.aborted)setResult(value);
    } catch(e) {if(!c.signal.aborted)setError(errorText(e));}
    finally {if(!c.signal.aborted)setBusy(false);}
  }
  async function open(hit:Hit) {
    try {
      const v=await client.json<SourceVersion>(`/v1/projects/${project}/source-versions/${hit.evidence.source.source_version_id}`,'SourceVersion');
      setViewer({version:v,page:hit.evidence.locator.pdf_page??1});
    } catch(e) {if(!client.signal.aborted)setError(errorText(e));}
  }
  return <details className="knowledge-search"><summary>Найти фрагмент в источниках</summary>
    <form onSubmit={e=>{e.preventDefault();void search();}}>
      <label>Текст для поиска<input aria-label="Текст для поиска" value={query} maxLength={1000} onChange={e=>setQuery(e.target.value)} placeholder="Например, прибыль за год" /></label>
      <div className="search-options"><label>Область<select aria-label="Область" value={scope} onChange={e=>setScope(e.target.value)}><option value="project">Материалы проекта</option><option value="selected">Выбранные версии</option></select></label>
        <label>Корпус<select aria-label="Корпус" value={corpus} onChange={e=>setCorpus(e.target.value)}><option value="factual">Фактические материалы</option><option value="samples">Образцы оформления</option></select></label></div>
      <label>Метод<select aria-label="Метод" value={method} onChange={e=>setMethod(e.target.value)}><option value="semantic.qwen3.v1">По смыслу</option><option value="lexical.v1">По словам</option></select></label>
      <button disabled={busy||!query.trim()||!versions.length}>{busy?'Ищем…':'Найти фрагменты'}</button>
    </form>
    {error&&<p className="error" role="alert">{error}</p>}
    {result&&<section aria-label="Результаты поиска"><h3>{corpus==='samples'?'Образцы оформления':'Фактические материалы'}</h3>
      <p className="small muted">{result.method==='semantic.qwen3.v1'?'Семантический поиск':'Поиск по словам'} · {result.hits.length} фрагментов</p>
      {corpus==='samples'&&<p className="notice small">Образцы показывают структуру. Их числа не являются финансовыми основаниями.</p>}
      {!result.hits.length&&<p className="small">Совпадений в выбранной области нет.</p>}
      {result.hits.map((hit,i)=><article className="search-hit" key={i}><button className="text-button" onClick={()=>void open(hit)}>{hit.title}, v{hit.revision}, {hit.evidence.locator.json_pointer??`стр. ${hit.evidence.locator.pdf_page}`}</button><blockquote>{hit.evidence.quote}</blockquote></article>)}
      <p className="small muted">{result.qualification}</p>
    </section>}
    {viewer&&<SourceReader client={client} project={project} version={viewer.version} initialPage={viewer.page} onClose={()=>setViewer(null)}/>}
  </details>;
}
