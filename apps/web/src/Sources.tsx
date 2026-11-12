import { Moex, JsonSnapshot } from './Moex';
import { KnowledgeSearch } from './KnowledgeSearch';
import React, { useEffect, useRef, useState } from 'react';
import { readLocal, writeLocal } from './local';
import { ApiError, Client, errorText } from './client';
import type { PageContent, Source, SourceVersion } from './types';

const processingLabels: Record<string,string> = {queued:'В очереди',processing:'Обрабатывается',ready:'Готов',failed:'Не обработан'};
type UploadTicket = {path:string; key:string; name:string; size:number; sha:string; metadata:string};
type UploadProps = { client: Client; project: string; sources: Source[]; versions: SourceVersion[]; recoveryScope?:string; onBusy?:(busy:boolean)=>void; onUploaded: (v: SourceVersion) => void; onRefresh: () => void;
  compact?:boolean; inputRef?:React.RefObject<HTMLInputElement>; resolveProject?:()=>Promise<string>; onPendingChange?:(pending:boolean)=>void };
export function PdfUpload({client, project, sources, versions, onUploaded, onRefresh, recoveryScope, onBusy, compact=false, inputRef, resolveProject, onPendingChange}: UploadProps) {
  const scope=recoveryScope??`upload.${project}`;
  const [target,setTarget]=useState(''),[title,setTitle]=useState(''),[file,setFile]=useState<File|null>(null);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [ticket,setTicket]=useState<UploadTicket|null>(()=>readLocal(scope,null));
  const lock=useRef(false),localInput=useRef<HTMLInputElement>(null);
  const input=inputRef??localInput;
  function save(next:UploadTicket|null){writeLocal(scope,next);setTicket(next);onPendingChange?.(!!next);}
  async function upload(selected:File){
    if(lock.current)return;
    const file=selected;
    setFile(file);
    lock.current=true;setBusy(true);onBusy?.(true);setError('');
    try{
      if(file.size>16*1024*1024){setError(errorText(new ApiError(413,'too_large')));return;}
      const hash=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
      const sha=Array.from(new Uint8Array(hash),n=>n.toString(16).padStart(2,'0')).join('');
      client.signal.throwIfAborted();
      if(ticket && (sha!==ticket.sha||file.size!==ticket.size)){
        setError('Содержимое отличается. Для восстановления выберите исходный PDF.');return;
      }
      if(!ticket&&!file.name.toLowerCase().endsWith('.pdf'))throw new ApiError(415,'unsupported_source');
      const source=sources.find(s=>s.id===target);
      // The native picker opens before any asynchronous project preparation.
      // Recovery always uses the path and key saved for the original upload.
      const destination=ticket?null:(project||await resolveProject?.());
      client.signal.throwIfAborted();
      if(!ticket&&!destination)throw new Error('Missing upload project');
      const op=ticket??{path:'/v1/projects/'+destination+'/sources'+(source?'/'+source.id+'/versions':''),
        key:crypto.randomUUID(),name:file.name,size:file.size,sha,
        metadata:JSON.stringify({title:source?.title??(title.trim()||file.name),expected_revision:source?.revision??0})};
      save(op);
      const body=new FormData();
      body.set('metadata',op.metadata);body.set('file',new File([file],op.name,{type:'application/pdf'}));
      const receipt=await client.json<{version:SourceVersion}>(op.path,'UploadReceipt',{method:'POST',headers:{'Idempotency-Key':op.key},body});
      client.signal.throwIfAborted();save(null);setFile(null);setTitle('');onUploaded(receipt.version);
      if(input.current)input.current.value='';
    }catch(e){
      if(!client.signal.aborted){
        // A local selection error cannot disprove an earlier committed upload.
        if(e instanceof ApiError && e.status<500&&![401,408,429].includes(e.status)){save(null);if(e.status===409)onRefresh();}
        setError(errorText(e));
      }
    }finally{lock.current=false;if(!client.signal.aborted){setBusy(false);onBusy?.(false);}}
  }
  return <section className={'upload'+(compact?' compact-upload':'')} hidden={compact&&!busy&&!ticket&&!error}><form onSubmit={e=>{e.preventDefault();if(file)void upload(file);}}>
    {!!sources.length&&<label>Куда загрузить<select aria-label="Куда загрузить" value={target} disabled={busy||!!ticket} onChange={e=>setTarget(e.target.value)}><option value="">Новый источник</option>{sources.filter(s=>!versions.some(v=>v.source_id===s.id&&v.media_type==='application/json')).map(s=><option key={s.id} value={s.id}>{s.title}: новая версия</option>)}</select></label>}
    {!target&&!recoveryScope&&<details><summary>Название источника</summary><input aria-label="Название источника" maxLength={300} value={title} disabled={busy||!!ticket} onChange={e=>setTitle(e.target.value)}/></details>}
    {!compact&&<><p className="small muted">PDF до 16 МиБ и 40 страниц. При отправке запроса выбранные материалы передаются внешней модели.</p>
    <details className="pdf-requirements small muted"><summary>Требования к PDF</summary>
      <p>Подходят презентации и сканы. Файл должен открываться без пароля и содержать хотя бы одну непустую страницу. До 200 000 символов текста на страницу.</p>
    </details></>}
    <input className="visually-hidden" aria-label="Файл PDF" tabIndex={-1} ref={input} type="file" accept="application/pdf,.pdf" disabled={busy} onChange={e=>{const selected=e.target.files?.[0];e.target.value='';if(selected)void upload(selected);}}/>
    {busy?<p role="status">Загружаем «{file?.name}»… <progress aria-label="Загрузка PDF"/></p>:ticket&&<p role="status">Загрузка «{ticket.name}» не подтверждена. {file?'Можно проверить тот же запрос.':'Выберите тот же файл для восстановления.'}</p>}
    {error&&<p role="alert">{error}</p>}
    {(!compact||ticket||error)&&<button type="button" disabled={busy} onClick={()=>input.current?.click()}>{ticket?'Выбрать исходный PDF':file?'Выбрать другой PDF':recoveryScope?'Выбрать PDF на компьютере':'Загрузить PDF'}</button>}
    {file&&ticket&&!busy&&<button>Проверить загрузку</button>}
    {compact&&ticket&&!busy&&<button type="button" onClick={()=>{
      if(window.confirm('Убрать незавершённую загрузку из черновика? Если PDF уже принят, он останется в недавних.')){
        save(null);setFile(null);setError('');
      }
    }}>Убрать загрузку из черновика</button>}
    {compact&&error&&!ticket&&!busy&&<button type="button" onClick={()=>{setError('');setFile(null);}}>Закрыть</button>}
  </form></section>;
}

export function Sources({ client, project, sources, versions, selected, onSelect, onUploaded, onRefresh, centralUpload=false, locked=false }: { centralUpload?: boolean; locked?: boolean; client: Client; project: string; sources: Source[]; versions: SourceVersion[]; selected: string[]; onSelect: (ids: string[]) => void; onUploaded: (v: SourceVersion) => void; onRefresh: () => void }) {
  const [viewer, setViewer] = useState<SourceVersion | null>(null);
  return <aside className="sources panel" aria-label="Источники проекта">
    <div className="section-head"><h2>Источники</h2><span className="counter">{sources.length}</span></div>
    <details><summary>Дополнительные источники</summary><Moex client={client} project={project} onSaved={onUploaded} /></details>
    <p className="muted small">Выберите 1-3 источника одной компании. Для каждого документа сохраняется своя версия.</p>
    {!sources.length && <p className="empty">Добавьте первый источник проекта.</p>}
    <div className="source-list">{locked&&<p className="small muted">Чтобы сменить источники, дождитесь ответа или отмените задачу.</p>}{sources.map(source => {
      const known = versions.filter(v => v.source_id === source.id).sort((a,b) => b.revision - a.revision);
      const chosen = known.filter(v => selected.includes(v.id) && v.corpus!=='samples');
      return <section className="source-item" key={source.id}>
        <h3>{source.title}</h3>
        {known.map(v => <div className="source-version" key={v.id}>
          <label className="check"><input type="checkbox" disabled={locked || v.processing_state !== 'ready' || (!selected.includes(v.id) && selected.length >= 3)} checked={selected.includes(v.id)} onChange={e => onSelect(e.target.checked ? [...selected,v.id] : selected.filter(id => id !== v.id))} />
            <span>Версия {v.revision}<small>{v.corpus==='samples'?'Образец оформления':v.id === source.latest_version_id ? 'Текущая' : 'Сохранённая'} · {v.media_type==='application/json'?'JSON':`${v.page_count ?? '?'} стр.`} · {processingLabels[v.processing_state] ?? v.processing_state}</small></span></label>
          <button className="text-button" onClick={() => setViewer(v)} aria-label={`Открыть ${source.title}, версия ${v.revision}`}>Читать</button>
        </div>)}
        {chosen.length>0 && <p className="selection-note">Для задачи: {chosen.map(v=>'версия '+v.revision).join(', ')}</p>}
      </section>;
    })}</div>
    {!centralUpload && <details open={!sources.length}><summary>Загрузить PDF или новую версию</summary><PdfUpload {...{client,project,sources,versions,onUploaded,onRefresh}} /></details>}
    <KnowledgeSearch client={client} project={project} versions={versions} selected={selected} />
    <p className="small muted footnote">Показаны текущие версии, загрузки этой сессии и версии из сохранённых задач.</p>
    {viewer && <SourceReader key={viewer.id} client={client} project={project} version={viewer} onClose={() => setViewer(null)} />}
  </aside>;
}

export function PdfReader({ client, project, version, onClose, initialPage = 1 }: { client: Client; project: string; version: SourceVersion; onClose: () => void; initialPage?: number }) {
  const [page, setPage] = useState(initialPage); const [text, setText] = useState<PageContent | null>(null);
  const [blob, setBlob] = useState(''); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  const [image,setImage]=useState(''),[zoom,setZoom]=useState(1),[fit,setFit]=useState(0);
  const viewport=useRef<HTMLDivElement>(null), picture=useRef<HTMLImageElement>(null);
  const zoomPoint=useRef<{x:number;y:number;rx:number;ry:number}|null>(null);
  function fitPage(){
    const v=viewport.current,i=picture.current;
    if(v&&i?.naturalWidth)setFit(Math.min(v.clientWidth,v.clientHeight*i.naturalWidth/i.naturalHeight));
  }
  useEffect(()=>{
    if(!viewport.current)return;
    const observer=new ResizeObserver(fitPage);observer.observe(viewport.current);
    return()=>observer.disconnect();
  },[]);
  React.useLayoutEffect(()=>{
    const v=viewport.current,i=picture.current,pt=zoomPoint.current;
    if(!v||!i)return;
    if(zoom===1){v.scrollTo(0,0);}else if(pt){const r=i.getBoundingClientRect();v.scrollBy(r.left+r.width*pt.rx-pt.x,r.top+r.height*pt.ry-pt.y);}
    zoomPoint.current=null;
  },[zoom]);
  function toggleZoom(e:React.MouseEvent<HTMLButtonElement>){
    const r=picture.current?.getBoundingClientRect();
    if(r&&e.detail>0)zoomPoint.current={x:e.clientX,y:e.clientY,rx:(e.clientX-r.left)/r.width,ry:(e.clientY-r.top)/r.height};
    setZoom(zoom===1?1.5:1);
  }
  useEffect(()=>{
    const c=client.child();let url='';setImage('');setZoom(1);setFit(0);
    void c.response('/v1/projects/'+project+'/source-versions/'+version.id+'/preview?page='+page).then(r=>r.blob()).then(b=>{
      if(!c.signal.aborted){url=URL.createObjectURL(b);setImage(url);}
    }).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return()=>{c.close();if(url)URL.revokeObjectURL(url);};
  },[client,project,version.id,page,retry]);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; dialog.current?.showModal(); return () => { previous?.focus({preventScroll:true}); }; }, []);
  useEffect(() => {
    const c = client.child(); let url = ''; setBlob('');
    void c.response(`/v1/projects/${project}/source-versions/${version.id}/file`).then(r => r.blob()).then(bytes => {
      if (c.signal.aborted) return;
      url = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' })); setBlob(url);
    }).catch(e => { if (!c.signal.aborted) setError(errorText(e)); });
    const revoke = () => { if (url) { URL.revokeObjectURL(url); url = ''; } };
    c.signal.addEventListener('abort', revoke, { once: true });
    return () => { c.close(); revoke(); };
  }, [client, project, version.id, retry]);
  useEffect(() => {
    const c = client.child(); setText(null); setError('');
    void c.json<PageContent>(`/v1/projects/${project}/source-versions/${version.id}/page?page=${page}`, 'PageContent').then(setText).catch(e => { if (!c.signal.aborted) setError(errorText(e)); });
    return () => c.close();
  }, [client, project, version.id, page, retry]);
  return <dialog className="pdf-dialog source-pdf-dialog" ref={dialog} onCancel={e => { e.preventDefault(); onClose(); }} aria-labelledby="pdf-title">
    <div className="section-head"><div><h2 id="pdf-title">{version.original_filename}</h2><p className="small muted">Версия {version.revision} · {version.corpus==='samples'?'Образец структуры, не финансовый источник':'Оригинал PDF'}</p></div><button className="secondary" onClick={onClose}>Закрыть PDF</button></div>
    <div className="pdf-toolbar"><label>Страница<select aria-label="Страница" value={page} onChange={e => setPage(Number(e.target.value))}>{Array.from({length: version.page_count ?? 1}, (_, i) => <option key={i+1}>{i+1}</option>)}</select></label>{blob && <a className="button secondary" href={blob} download={version.original_filename}>Скачать оригинал</a>}</div>
    {error && <div role="alert" className="error">{error} <button className="secondary" onClick={() => setRetry(n => n+1)}>Повторить чтение</button></div>}
    <div className="pdf-toolbar"><button aria-label="Уменьшить" disabled={zoom===1||!image} onClick={()=>setZoom(Math.max(1,zoom-.5))}>−</button>
      <output>{Math.round(zoom*100)}%</output><button aria-label="Увеличить" disabled={zoom===3||!image} onClick={()=>setZoom(Math.min(3,zoom+.5))}>+</button>
      <button aria-pressed={zoom===1} disabled={!image} onClick={()=>{setZoom(1);viewport.current?.scrollTo(0,0);}}>Страница целиком</button></div>
    <div className="source-viewport" ref={viewport}>{image?<button aria-label={zoom===1?'Приблизить страницу':'Показать страницу целиком'} className="source-zoom" onClick={toggleZoom} style={{width:fit?fit*zoom:undefined,cursor:zoom===1?'zoom-in':'zoom-out'}}>
      <img ref={picture} onLoad={fitPage} src={image} alt={'Оригинал PDF, страница '+page}/></button>:<p>Открываем страницу…</p>}</div>
    <details className="page-text"><summary>Текст страницы {page}</summary><pre>{text ? text.text || 'На странице нет текстового слоя. См. изображение выше.' : 'Загрузка…'}</pre></details>

  </dialog>;
}

export function SourceReader(props: {client:Client;project:string;version:SourceVersion;onClose:()=>void;initialPage?:number}) {
  return props.version.media_type==='application/json' ? <JsonSnapshot {...props}/> : <PdfReader {...props}/>;
}
