import React, {useEffect, useRef, useState} from 'react';
import {Client, errorText} from './client';
import {Operations, readLocal} from './local';
import {useAnchoredMenu} from './useAnchoredMenu';
import {WorkspaceIcon} from './WorkspaceIcon';
import {readPublishedExports} from './export-publication';
import type {VersionView} from './types';

export type ExportValue = {template_version:string;renderer_version:string;id:string;artifact_version_id:string;revision:number;release_check:{issues:string[]};format:'docx'|'pdf';state:string;settings:{theme:string;release_mode:string};file:{filename:string;sha256:string;byte_size:number;media_type:string}|null;error:{message:string}|null};
type Renderer={format:string;version:string;templates:string[];status:string};
const labels:Record<string,string>={queued:'В очереди',rendering:'Подготовка файла',ready:'Готов',failed:'Выпуск завершился ошибкой',recovery_required:'Исход выпуска требует сверки',missing:'Файл отсутствует',corrupt:'Файл поврежден'};
const formats=['docx','pdf'] as const;

function exportFailure(item: ExportValue) {
  if (item.state === 'failed' && item.release_check.issues.includes('Renderer: TimeoutExpired')) {
    return `Рендер не завершился вовремя. Передайте оператору ID выпуска ${item.id} и ревизию ${item.revision} для повтора.`;
  }
  return labels[item.state]+'. Автоматическая пересборка не выполняется.';
}

export function Exports({client,project,value,editing,openRequest=0}:{openRequest?:number;client:Client;project:string;value:VersionView;editing:boolean}) {
  const v=value.version, path=`/v1/projects/${project}/artifacts/${v.artifact_id}/versions/${v.id}/exports`;
  const [request]=useState(()=>client.child());
  useEffect(()=>()=>request.close(),[request]);
  const [items,setItems]=useState<ExportValue[]>([]),[renderers,setRenderers]=useState<Renderer[]>([]);
  const [theme,setTheme]=useState('standard'),[mode,setMode]=useState('draft'),[busy,setBusy]=useState<string|null>(null),[error,setError]=useState('');
  const [preview,setPreview]=useState<ExportValue|null>(null),[advanced,setAdvanced]=useState(false);
  const [expanded,setExpanded]=useState(false),[refresh,setRefresh]=useState(0);
  const [waiting,setWaiting]=useState<string[]>([]);
  const button=useRef<HTMLButtonElement>(null),menu=useRef<HTMLDivElement>(null),lock=useRef(false);
  useAnchoredMenu(expanded,button,menu,focus=>{setExpanded(false);if(focus)button.current?.focus();});
  useEffect(()=>{if(openRequest)setExpanded(true);},[openRequest]);
  const [ops]=useState(()=>({docx:new Operations(`export.${project}.${v.id}.docx`),pdf:new Operations(`export.${project}.${v.id}.pdf`)}));
  const pending=!!ops.docx.pending||!!ops.pdf.pending;
  const draft=readLocal<{payload:unknown}|null>(`editor.${project}.${v.id}`,null);
  const unsaved=editing || !!draft&&JSON.stringify(draft.payload)!==JSON.stringify(v.payload) || !!readLocal(`editor.${project}.${v.id}.save`,null);
  useEffect(()=>{
    void request.json<{renderers:Renderer[]}>('/v1/capabilities','Registry').then(r=>setRenderers(r.renderers))
      .catch(e=>{if(!request.signal.aborted)setError(errorText(e));});
  },[request]);
  useEffect(()=>{
    if(!expanded&&!waiting.length&&!advanced)return;
    const c=request.child();let timer:ReturnType<typeof setTimeout>;
    async function load(){
      try{
        const rows=await readPublishedExports(()=>c.list<ExportValue>(path,'ExportPage'),c.signal);setItems(rows);
        if(rows.some(row=>['queued','rendering'].includes(row.state)))timer=setTimeout(()=>void load(),1200);
      }catch(e){if(!c.signal.aborted)setError(errorText(e));}
    }
    void load();return()=>{clearTimeout(timer);c.close();};
  },[request,path,expanded,waiting,advanced,refresh]);
  async function download(item:ExportValue){
    const response=await request.response(`/v1/projects/${project}/exports/${item.id}/file`);
    const bytes=await response.blob();request.signal.throwIfAborted();
    const url=URL.createObjectURL(bytes),a=document.createElement('a');a.href=url;a.download=item.file!.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function renderer(format:string){return renderers.find(r=>r.format===format&&r.status==='enabled'&&r.templates.includes('aist-aso.v1'));}
  function matching(rows:ExportValue[],format:string){return rows.find(item=>item.format===format&&item.artifact_version_id===v.id&&
    item.template_version==='aist-aso.v1'&&item.renderer_version===renderer(format)?.version&&item.settings.theme===theme&&item.settings.release_mode===mode);}
  async function release(format:'docx'|'pdf'){
    if(lock.current||unsaved)return;
    lock.current=true;setBusy(format);setError('');
    try{
      const op=ops[format];
      let item:ExportValue;
      if(op.pending)item=await op.send<ExportValue>(request);
      else{
        // Read first: an existing failed or unknown export must never cause a new release.
        const rows=await request.list<ExportValue>(path,'ExportPage');setItems(rows);
        const existing=matching(rows,format);
        if(existing)item=existing;
        else{
          const selected=renderer(format);if(!selected)throw Error('Renderer unavailable');
          op.prepare(path,{expected_revision:value.review.revision,format,template_version:'aist-aso.v1',renderer_version:selected.version,
            settings:{locale:'ru-RU',paper:'A4',release_mode:mode,theme}},'Export','Выпуск '+format.toUpperCase());
          item=await op.send<ExportValue>(request);
        }
      }
      if(item.state==='recovery_required'){
        const rows=await readPublishedExports(()=>request.list<ExportValue>(path,'ExportPage'),request.signal);
        item=rows.find(row=>row.id===item.id)??item;
      }
      setItems(rows=>[item,...rows.filter(row=>row.id!==item.id)]);
      if(item.state==='ready')await download(item);
      else if(['queued','rendering'].includes(item.state))setWaiting(ids=>ids.includes(item.id)?ids:[...ids,item.id]);
      else setError(exportFailure(item));
      setRefresh(n=>n+1);
    }catch(e){if(!request.signal.aborted)setError(errorText(e));}
    finally{lock.current=false;if(!request.signal.aborted)setBusy(null);}
  }
  useEffect(()=>{
    const completed=items.filter(item=>waiting.includes(item.id)&&!['queued','rendering'].includes(item.state));
    if(!completed.length)return;
    setWaiting(ids=>ids.filter(id=>!completed.some(item=>item.id===id)));
    for(const item of completed){
      if(item.state==='ready')void download(item).catch(e=>{if(!request.signal.aborted)setError(errorText(e));});
      else setError(exportFailure(item));
    }
  },[items,waiting]);
  return <section className="exports" aria-label="Выпуск файлов">
    <button ref={button} aria-expanded={expanded} onClick={()=>setExpanded(!expanded)}><WorkspaceIcon name="download"/>Скачать</button>
    {expanded&&<div ref={menu} className="download-menu" aria-label="Скачать выбранную версию">
      <span className="small muted">Версия {v.revision}</span>
      {unsaved?<p>Сначала сохраните или отмените правки.</p>:formats.map(format=>{
        const item=matching(items,format),preparing=item&&['queued','rendering'].includes(item.state);
        return <button key={format} disabled={!!busy||!renderer(format)} onClick={()=>void release(format)}>
          <WorkspaceIcon name="document"/>{format==='docx'?'Word':'PDF'}
          {(busy===format||preparing)&&<small>Готовим файл...</small>}{ops[format].pending&&!busy&&<small>Восстановить запрос</small>}
        </button>;
      })}
      <button className="text-button small" onClick={()=>{setExpanded(false);setAdvanced(true);}}>Настройки и прежние файлы</button>
    </div>}
    {waiting.length>0&&!expanded&&<span role="status" className="small">Готовим файл...</span>}
    {error&&<p className="error" role="alert">{error}</p>}
    {advanced&&<div className="export-options"><div className="section-head"><h3>Файлы версии {v.revision}</h3><button onClick={()=>setAdvanced(false)}>Закрыть настройки</button></div>
      <div className="export-controls">
        <label>Оформление<select aria-label="Оформление выпуска" disabled={!!busy||pending||waiting.length>0} value={theme} onChange={e=>setTheme(e.target.value)}><option value="standard">Стандартное</option><option value="compact">Компактное</option></select></label>
        <label>Статус выпуска<select aria-label="Статус выпуска" disabled={!!busy||pending||waiting.length>0} value={mode} onChange={e=>setMode(e.target.value)}><option value="draft">Черновик</option><option value="accepted" disabled={!value.review.acceptance}>Принятая версия</option></select></label>
        {formats.map(format=><button key={format} disabled={!!busy||unsaved||!renderer(format)} onClick={()=>void release(format)}>Скачать {format==='docx'?'Word':'PDF'}</button>)}
      </div>
      <ul className="export-list">{items.map(item=><li key={item.id} data-export-id={item.id} data-state={item.state}>
        <span>{item.format.toUpperCase()} · {item.settings.release_mode==='draft'?'Черновик':'Принятая версия'} · {item.settings.theme==='compact'?'Компактное':'Стандартное'} · {labels[item.state]} · {item.renderer_version}</span>
        {item.state==='ready'&&<><button disabled={unsaved} onClick={()=>void download(item).catch(e=>setError(errorText(e)))}>Скачать {item.format.toUpperCase()}</button>{item.format==='pdf'&&<button onClick={()=>setPreview(item)}>Просмотреть PDF</button>}</>}
        {['failed','missing','corrupt','recovery_required'].includes(item.state)&&<p className="small">{item.state==='failed'?exportFailure(item):item.error?.message}</p>}
      </li>)}</ul>
    </div>}
    {preview&&<ExportPreview client={request} project={project} item={preview} onClose={()=>setPreview(null)}/>}
  </section>;
}

function ExportPreview({client,project,item,onClose}:{client:Client;project:string;item:ExportValue;onClose:()=>void}){
  const dialog=useRef<HTMLDialogElement>(null);const [url,setUrl]=useState(''),[error,setError]=useState('');
  useEffect(()=>{const previous=document.activeElement as HTMLElement|null;dialog.current?.showModal();return()=>previous?.focus();},[]);
  useEffect(()=>{const c=client.child();let blob='';void c.response(`/v1/projects/${project}/exports/${item.id}/file`).then(r=>r.blob()).then(b=>{if(!c.signal.aborted){blob=URL.createObjectURL(new Blob([b],{type:'application/pdf'}));setUrl(blob);}}).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});return()=>{c.close();if(blob)URL.revokeObjectURL(blob);};},[client,project,item.id]);
  return <dialog ref={dialog} className="pdf-dialog export-preview" aria-label="Готовый PDF" onCancel={e=>{e.preventDefault();onClose();}}><div className="section-head"><h2>PDF выбранной версии</h2><button className="secondary" onClick={onClose}>Закрыть PDF выпуска</button></div>{error?<p role="alert" className="error">{error}</p>:url?<object data={url} type="application/pdf" aria-label="PDF выпуска"><p>Встроенный просмотр недоступен. Скачайте сохраненный PDF.</p></object>:<p>Загрузка PDF...</p>}</dialog>;
}
