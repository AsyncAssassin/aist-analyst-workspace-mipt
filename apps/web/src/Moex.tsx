import React, { useEffect, useRef, useState } from 'react';
import { Client, errorText } from './client';
import { Operations, writeLocal } from './local';
import type { SourceVersion } from './types';

type Normalized = {security:{secid:string;isin:string;issuer:string;issuer_inn:string;board:string};price:{label:string;value:string|null;currency:string|null;trade_date:string|null;unknown_reason:string|null}};
type Status = {version:SourceVersion|null;normalized:Normalized|null;last_success_at:string|null;last_error:string|null};
type Update = {version_id:string|null;changed:boolean;error:string|null;search_error?:string|null};
type Snapshot = {fetched_at:string;normalized:Normalized;sections:Record<string,string>;limitation:string;requests:Record<string,{url:string;request_url:string;parameters:Record<string,string>;received_at:string;sha256:string;raw_json:string}>};

export function Moex({client,project,onSaved}:{client:Client;project:string;onSaved:(v:SourceVersion)=>void}) {
  const [status,setStatus]=useState<Status|null>(null);const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);const [viewer,setViewer]=useState<SourceVersion|null>(null);
  const [ops]=useState(()=>new Operations(`moex.${project}`));const lock=useRef(false);
  const path=`/v1/projects/${project}/moex/wush`;
  useEffect(()=>{const c=client.child();void c.json<Status>(path,'MoexStatus').then(setStatus).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});return ()=>c.close();},[client,path]);
  async function update(fresh=false) {
    if(lock.current)return;lock.current=true;setBusy(true);setError('');
    try {
      if(fresh)writeLocal(`moex.${project}`,null);
      if(!ops.pending)ops.prepare(path,{mode:fresh||status?.version?'refresh':'get'},'MoexUpdate','Сведения MOEX');
      const result=await ops.send<Update>(client);
      const next=await client.json<Status>(path,'MoexStatus');setStatus(next);
      if(result.error)setError(result.error);
      if(result.search_error)setError(result.search_error);
      if(next.version)onSaved(next.version);
    } catch(e) {if(!client.signal.aborted)setError(errorText(e));}
    finally {lock.current=false;if(!client.signal.aborted)setBusy(false);}
  }
  const price=status?.normalized?.price;
  return <section className="moex-card" aria-label="Whoosh на Московской бирже">
    <h3>Whoosh · MOEX ISS</h3>
    {price&&<><p><strong>{price.label}: {price.value??'неизвестна'} {price.currency??''}</strong><br/>Дата торгов: {price.trade_date??'неизвестна'}</p>
      {price.unknown_reason&&<p className="notice small">{price.unknown_reason}</p>}
      <p className="small muted">Последняя успешная проверка: {status?.last_success_at?new Date(status.last_success_at).toLocaleString('ru-RU'):'неизвестна'}</p></>}
    {(error||status?.last_error)&&<p className="error" role="alert">{error||status?.last_error}</p>}
    <div className="actions"><button disabled={busy||!status} onClick={()=>void update()}>{busy?'Проверяем ISS…':ops.pending?'Проверить результат операции':status?.version?'Обновить сведения':'Получить сведения'}</button>
      {error&&ops.pending&&!busy&&<button className="secondary" onClick={()=>void update(true)}>Начать новую проверку ISS</button>}
      {status?.version&&<button className="text-button" onClick={()=>setViewer(status.version)}>Открыть снимок v{status.version.revision}</button>}</div>
    <p className="small muted">Обновление выполняется по нажатию. Данные для внутреннего технического опыта.</p>
    {viewer&&<JsonSnapshot client={client} project={project} version={viewer} onClose={()=>setViewer(null)}/>}
  </section>;
}

export function JsonSnapshot({client,project,version,onClose}:{client:Client;project:string;version:SourceVersion;onClose:()=>void}) {
  const [snapshot,setSnapshot]=useState<Snapshot|null>(null);const [error,setError]=useState('');
  const [download,setDownload]=useState('');const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const previous=document.activeElement as HTMLElement|null;dialog.current?.showModal();return ()=>previous?.focus();},[]);
  useEffect(()=>{
    const c=client.child();let url='';
    void c.response(`/v1/projects/${project}/source-versions/${version.id}/file`).then(r=>r.text()).then(raw=>{
      if(c.signal.aborted)return;
      setSnapshot(JSON.parse(raw));url=URL.createObjectURL(new Blob([raw],{type:'application/json'}));setDownload(url);
    }).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return ()=>{c.close();if(url)URL.revokeObjectURL(url);};
  },[client,project,version.id]);
  return <dialog className="pdf-dialog snapshot-dialog" ref={dialog} onCancel={e=>{e.preventDefault();onClose();}} aria-label="Сохраненный снимок MOEX ISS">
    <div className="section-head"><div><h2>Whoosh · MOEX ISS</h2><p>Сохраненный JSON · версия {version.revision}</p></div><button className="secondary" onClick={onClose}>Закрыть снимок</button></div>
    {error&&<p className="error" role="alert">{error}</p>}
    {snapshot&&<><p>Получен: {new Date(snapshot.fetched_at).toLocaleString('ru-RU')}</p><p className="mono small">SHA-256 снимка: {version.sha256}</p>
      {Object.entries(snapshot.sections).map(([pointer,content])=><section key={pointer}><h3>{pointer.endsWith('/price')?'Цена закрытия':'Карточка бумаги'}</h3><p>{content}</p><p className="mono small">Раздел: {pointer}</p></section>)}
      <p className="notice small">{snapshot.limitation}</p>
      <h3>Происхождение</h3>{Object.entries(snapshot.requests).map(([method,request])=><section key={method}><p><a href={request.request_url} target="_blank" rel="noreferrer">{request.url}</a></p><p className="small">GET · {request.received_at}<br/>SHA-256 ответа: {request.sha256}</p><pre>{JSON.stringify(request.parameters,null,2)}</pre><details><summary>Исходный JSON: {method}</summary><pre>{request.raw_json}</pre></details></section>)}
      {download&&<a className="button secondary" href={download} download={version.original_filename}>Скачать сохраненный JSON</a>}</>}
  </dialog>;
}
