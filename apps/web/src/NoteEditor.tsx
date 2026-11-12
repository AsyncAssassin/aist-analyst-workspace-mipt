import React,{useEffect,useRef,useState} from 'react';
import {ApiError,Client,errorText} from './client';
import {Operations,readLocal,writeLocal,setUnsavedEditor,bindEditor} from './local';
import {type NoteBlock,type ParagraphBlock,type SourceVersion,type VersionView} from './types';
import {AllFindings,ReviewFindings} from './ReviewFindings';
import {EditableParagraph} from './EditableParagraph';
import {pendingChanges,type Payload} from './editorMerge';
import {noteSections} from './ChatStart';

type Section=NonNullable<Payload['content']['sections']>[number];
type Table=Extract<NoteBlock,{kind:'table'}>;
type Layout={sections:{blocks:{id:string;publication?:{url:string;publisher:string};headers:string[];rows:{row_id:string|null;text:string;links:{source:string;page:number}[]}[][]}[]}[]};
type Draft={payload:Payload;revision:number;removed?:({section:Section;index:number}|{sectionId:string;block:NoteBlock;index:number})[]};
const blank=():ParagraphBlock=>({block_id:'block_'+crypto.randomUUID(),kind:'paragraph',claim:{text:'',origin:'human_editorial',fact_refs:[],calculation_ids:[]}});
const amountUnit=(r:Table['rows'][number])=>[({'1000':'тыс.','1000000':'млн','1000000000':'млрд','1000000000000':'трлн'} as Record<string,string>)[r.amount.scale],
  r.amount.currency==='RUB'?'руб.':r.amount.currency??({percent:'%',ratio:'раз',count:'шт.'} as Record<string,string>)[r.amount.unit]].filter(Boolean).join(' ');

export function NoteEditor({client,project,value,onSaved,onCancel,versions,open,onDiscuss}:{client:Client;project:string;value:VersionView;versions:SourceVersion[];
  open:(source:SourceVersion,page:number)=>void;onSaved:(v:VersionView,files?:boolean)=>void;onCancel:()=>void;onDiscuss?:(v:VersionView['version'])=>void}){
  const v=value.version,scope='editor.'+project+'.'+v.id;
  const [draft,setDraft]=useState<Draft>(()=>readLocal(scope,{payload:structuredClone(v.payload),revision:v.revision}));
  const [ops]=useState(()=>new Operations(scope+'.save'));
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[latest,setLatest]=useState<VersionView|null>(null);
  const [selected,setSelected]=useState<number[]>([]),[adding,setAdding]=useState(false),[name,setName]=useState(''),[body,setBody]=useState('');
  const [layout,setLayout]=useState<Layout|null>(null),[focus,setFocus]=useState('');
  const form=useRef<HTMLFormElement>(null),saving=useRef(false);
  const content=draft.payload.content,sections=content.sections??[];
  const dirty=JSON.stringify(draft.payload)!==JSON.stringify(v.payload);
  useEffect(()=>{const c=client.child();void c.json<Layout>('/v1/projects/'+project+'/artifact-versions/'+v.id+'/aso','AsoView')
    .then(setLayout).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});return()=>c.close();},[client,project,v.id]);
  useEffect(()=>{setUnsavedEditor(dirty||!!ops.pending);const leave=(e:BeforeUnloadEvent)=>{if(dirty||ops.pending){e.preventDefault();e.returnValue='';}};
    window.addEventListener('beforeunload',leave);return()=>{setUnsavedEditor(false);window.removeEventListener('beforeunload',leave);};},[dirty,ops,busy]);
  function edit(change:(p:Draft)=>void){const next=structuredClone(draft);change(next);writeLocal(scope,next);setDraft(next);}
  function section(p:Draft,id:string){return p.payload.content.sections!.find(s=>s.section_id===id)!;}
  function editBlock<T extends NoteBlock>(sid:string,block:T,change:(b:T)=>void){
    edit(p=>change(section(p,sid).blocks.find(b=>b.block_id===block.block_id) as T));
  }
  async function save(next:'read'|'files'|'discussion'='read'){
    if(saving.current||latest||(!ops.pending&&!form.current?.reportValidity()))return;
    if(sections.some(s=>s.blocks.some(b=>b.kind==='paragraph'&&!b.claim.text.trim()))){setError('Заполните пустой абзац или удалите его.');return;}
    saving.current=true;setBusy(true);setError('');
    try{
      let saved=value;
      if(dirty||ops.pending){
        if(!ops.pending)ops.prepare('/v1/projects/'+project+'/artifacts/'+v.artifact_id+'/versions',
          {expected_revision:v.revision,parent_version_id:v.id,require_latest:true,payload:draft.payload},'VersionView','Сохранение версии');
        saved=await ops.send<VersionView>(client);
      }
      writeLocal(scope,null);setUnsavedEditor(false);onSaved(saved,next==='files');
      if(next==='discussion')onDiscuss?.(saved.version);
      return {id:saved.version.id,parentId:v.id};
    }catch(e){
      setError(errorText(e));
      if(e instanceof ApiError&&e.code==='revision_conflict'){
        try{const head=await client.json<{latest_version_id:string}>('/v1/projects/'+project+'/artifacts/'+v.artifact_id,'Artifact');
          const fresh=await client.json<VersionView>('/v1/projects/'+project+'/artifact-versions/'+head.latest_version_id,'VersionView');
          setLatest(fresh);setSelected([]);
        }catch(readError){setError(errorText(readError));}
      }
    }finally{saving.current=false;setBusy(false);}
  }
  useEffect(()=>{bindEditor(()=>save());return()=>bindEditor(null);});
  function close(){if(!dirty||window.confirm('Отменить несохранённые правки?')){writeLocal(scope,null);setUnsavedEditor(false);onCancel();}}
  const changes=latest?pendingChanges(v.payload,draft.payload,latest.version.payload):[];
  function transfer(){
    if(!latest)return;
    const copy=structuredClone(latest.version.payload);
    changes.forEach((c,i)=>{if(c.compatible&&selected.includes(i))c.apply(copy);});
    if(!copy.content.sections?.length){setError('Сохраните хотя бы один раздел.');return;}
    writeLocal('editor.'+project+'.'+latest.version.id,{payload:copy,revision:latest.version.revision});
    writeLocal(scope,null);setUnsavedEditor(false);onSaved(latest);
  }
  function sourceLinks(blockId:string,rowId?:string){
    const block=layout?.sections.flatMap(s=>s.blocks).find(b=>b.id===blockId);
    if(block?.publication)return <a className="page-ref" href={block.publication.url} target="_blank" rel="noopener noreferrer" title={block.publication.publisher}>ссылка</a>;
    const cells=block?.rows.flat().filter(c=>!rowId||c.row_id===rowId)??[];
    const links=new Map(cells.flatMap(c=>c.links).map(link=>[link.source+':'+link.page,link]));
    return [...links.entries()].map(([id,link])=>{
      const source=versions.find(s=>s.id===link.source);
      return source&&<button key={id} className="page-ref" type="button" title={source.original_filename} onClick={()=>open(source,link.page)}>стр. {link.page}</button>;
    });
  }
  function tableEditor(sid:string,b:Table){
    const saved=layout?.sections.flatMap(s=>s.blocks).find(x=>x.id===b.block_id);
    const byId=new Map(b.rows.map(r=>[r.row_id,r]));
    const amount=(r:Table['rows'][number])=><input aria-label={'Значение: '+r.label+', '+r.period_label} data-row-id={r.row_id} inputMode="decimal" required maxLength={100}
      pattern={'-?(0|[1-9][0-9]*)([.,][0-9]+)?'} title="Введите число без разделителей тысяч" value={r.amount.value}
      onChange={e=>editBlock(sid,b,t=>{t.rows.find(item=>item.row_id===r.row_id)!.amount.value=e.target.value.replace(/\s/g,'').replace(',','.');})}/>;
    return <div className="editable-table" key={b.block_id}>
      <input className="edit-caption" aria-label="Подпись таблицы" value={b.caption} required maxLength={500} onChange={e=>editBlock(sid,b,t=>{t.caption=e.target.value;})}/>
      {saved&&<div className="aso-scroll"><table><thead><tr>{saved.headers.map((h,i)=><th key={i}>{h}</th>)}</tr></thead>
        <tbody>{saved.rows.map((row,i)=><tr key={i}>{row.map((cell,j)=>{const r=cell.row_id?byId.get(cell.row_id):null;return <td key={j}>{r?<>{amount(r)}<small>{amountUnit(r)}</small></>:cell.text}</td>;})}</tr>)}</tbody></table></div>}
      <details open={!saved||undefined}><summary>Строки по периодам и источники</summary>{b.rows.map(r=><div className="edit-detail-row" key={r.row_id}>
        <label>Показатель<input required value={r.label} maxLength={300} onChange={e=>editBlock(sid,b,t=>{t.rows.find(x=>x.row_id===r.row_id)!.label=e.target.value;})}/></label>
        <label>Период<input required value={r.period_label} maxLength={300} onChange={e=>editBlock(sid,b,t=>{t.rows.find(x=>x.row_id===r.row_id)!.period_label=e.target.value;})}/></label>
        {!saved&&amount(r)}{sourceLinks(b.block_id,r.row_id)}</div>)}</details>
    </div>;
  }
  const available=noteSections.filter(n=>!sections.some(s=>s.title===n));
  const active=sections.flatMap(s=>s.blocks).find(b=>b.block_id===focus);
  return <form ref={form} className="note-editor aso-note" aria-label="Редактор записки" onSubmit={e=>{e.preventDefault();void save();}}>
    <div className="edit-tools"><strong>Редактирование · v{v.revision}</strong>
      {(['bold','italic'] as const).map((cmd,i)=><button type="button" disabled={!active||busy||!!latest} key={cmd} aria-label={i?'Курсив':'Полужирный'} onMouseDown={e=>e.preventDefault()} onClick={()=>{
        const anchor=document.getSelection()?.anchorNode;
        const el=(anchor instanceof Element?anchor:anchor?.parentElement)?.closest('[contenteditable]');
        if(el&&form.current?.contains(el))document.execCommand(cmd);
      }}>{i?<i>К</i>:<b>Ж</b>}</button>)}
      <button type="button" disabled={active?.kind!=='paragraph'||busy||!!latest} onMouseDown={e=>e.preventDefault()} onClick={()=>edit(p=>{
        const b=p.payload.content.sections!.flatMap(s=>s.blocks).find(b=>b.block_id===focus);
        if(b?.kind==='paragraph'){if(b.list_level===2)delete b.list_level;else b.list_level=b.list_level===1?2:1;}
      })}>Список</button>
      <button type="submit" disabled={busy||!!latest||(!dirty&&!ops.pending)}>{ops.pending?'Проверить сохранение':'Сохранить новую версию'}</button>
      <button type="button" disabled={busy||!!ops.pending} onClick={close}>Отмена</button></div>
    {error&&<p role="alert" className="error">{error}</p>}
    {latest&&<div className="notice"><strong>Открыта более новая версия v{latest.version.revision}</strong><p>Выберите свои правки для переноса. Совпавшее место можно сверить и изменить вручную в новой версии.</p>
      {changes.map((c,i)=><details key={i}><summary><label><input type="checkbox" disabled={!c.compatible} checked={selected.includes(i)} onChange={e=>setSelected(e.target.checked?[...selected,i]:selected.filter(n=>n!==i))}/>{c.label}</label></summary>
        <p>Ваша правка: {describe(c.after)}</p><p>В актуальной версии: {describe(c.current)}</p></details>)}
      <button type="button" onClick={transfer}>Открыть актуальную версию с выбранными правками</button></div>}
    <fieldset disabled={busy||!!ops.pending||!!latest}>
    <input className="edit-title" aria-label="Название записки" value={content.title} required maxLength={300} onChange={e=>edit(p=>{p.payload.content.title=e.target.value;})}/>
    {sections.map(s=><section key={s.section_id} className="aso-section"><header><h3>{s.title}</h3>
      <button type="button" className="remove-sec" aria-label={'Удалить раздел '+s.title} disabled={sections.length===1} onClick={()=>edit(p=>{
        const rows=p.payload.content.sections!,i=rows.findIndex(item=>item.section_id===s.section_id);(p.removed??=[]).push({section:rows.splice(i,1)[0],index:i});
      })}>Удалить</button></header>
      <div>{s.blocks.map(b=><div key={b.block_id} className="aso-block" id={'note-'+b.block_id} data-edit-block={b.block_id} onFocus={()=>setFocus(b.kind==='paragraph'?b.block_id:'')}>
        {b.kind==='paragraph'?<><EditableParagraph disabled={busy||!!ops.pending||!!latest} block={b} onChange={(text,marks)=>editBlock(s.section_id,b,item=>{
          item.claim.text=text;if(marks.length)item.marks=marks;else delete item.marks;
        })}/>{sourceLinks(b.block_id)}</>:tableEditor(s.section_id,b)}
        {s.blocks.length>1&&<button type="button" className="remove-block" aria-label={'Удалить блок '+(s.blocks.indexOf(b)+1)+' из '+s.title} onClick={()=>edit(p=>{
          const blocks=section(p,s.section_id).blocks,i=blocks.findIndex(x=>x.block_id===b.block_id);
          (p.removed??=[]).push({sectionId:s.section_id,index:i,block:blocks.splice(i,1)[0]});
        })}>Удалить блок</button>}
        <ReviewFindings findings={value.checks.flatMap(c=>c.findings.filter(f=>f.block_id===b.block_id))}/>
      </div>)}<button className="edit-add" type="button" onClick={()=>edit(p=>section(p,s.section_id).blocks.push(blank()))}>Добавить абзац</button></div></section>)}
    <div className="edit-bottom">{!!draft.removed?.length&&<button type="button" onClick={()=>edit(p=>{const item=p.removed!.pop()!;
        if('section' in item)p.payload.content.sections!.splice(item.index,0,item.section);
        else section(p,item.sectionId).blocks.splice(item.index,0,item.block);
      })}>{'section' in draft.removed[draft.removed.length-1]?'Вернуть раздел':'Вернуть блок'}</button>}
      {!!available.length&&<button type="button" onClick={()=>{setName(available[0]);setBody('');setAdding(!adding);}}>Добавить раздел</button>}
      {adding&&<div><label>Раздел шаблона<select value={name} onChange={e=>setName(e.target.value)}>{available.map(n=><option key={n}>{n}</option>)}</select></label>
        <label>Первый абзац<textarea value={body} onChange={e=>setBody(e.target.value)} maxLength={20000}/></label>
        <button type="button" disabled={!body.trim()||!available.includes(name)} onClick={()=>{edit(p=>{
          const b=blank();b.claim.text=body.trim();const rows=p.payload.content.sections!,i=rows.findIndex(s=>noteSections.indexOf(s.title)>noteSections.indexOf(name));
          rows.splice(i<0?rows.length:i,0,{section_id:'sec_'+crypto.randomUUID(),title:name,blocks:[b]});
        });setAdding(false);}}>Вставить раздел</button></div>}
      <details><summary>Реквизиты</summary><label>Аудитория<input value={content.audience} maxLength={500} onChange={e=>edit(p=>{p.payload.content.audience=e.target.value;})}/></label>
        {/* Пустое поле возвращает [TBD]; у старой версии без команды строка в шапке не появляется. */}
        <label>Команда<input value={content.team==='[TBD]'?'':content.team??''} placeholder="[TBD]" maxLength={300} onChange={e=>edit(p=>{
          if(e.target.value.trim())p.payload.content.team=e.target.value;
          else if(v.payload.content.team!==undefined)p.payload.content.team='[TBD]';
          else delete p.payload.content.team;
        })}/></label></details>
      <button type="button" onClick={()=>void save('files')}>Сохранить и скачать</button>
      {onDiscuss&&<button type="button" onClick={()=>void save('discussion')}>Сохранить и обсудить</button>}</div>
    </fieldset>
    <AllFindings value={value} blockIds={new Set(sections.flatMap(section=>section.blocks.map(block=>block.block_id)))}/>
  </form>;
}

function describe(v:unknown):string{
  if(v===undefined)return 'Удалено';
  if(v&&typeof v==='object'){
    if('claim' in v)return String((v.claim as {text:string}).text);
    if('value' in v)return String(v.value);
    if('title' in v)return String(v.title);
  }
  return typeof v==='string'?v:JSON.stringify(v);
}
