import React, {useEffect,useRef,useState,type RefObject} from 'react';
import {useAnchoredMenu} from './useAnchoredMenu';
import {Client,errorText} from './client';
import type {SourceVersion} from './types';

type Pdf = Pick<SourceVersion,'id'|'original_filename'|'revision'|'page_count'>;
type ChatBoundary = {id:string;title:string;through_sequence:number};
export type PickedMaterials = {folder_id:string;pdfs:Pdf[];chats:ChatBoundary[]};
type Options = Pick<PickedMaterials,'pdfs'|'chats'> & {more:boolean};

export function ProjectMaterials({client, trigger, picked, chatId, attached, onChange, onClose}:{
  trigger:RefObject<HTMLButtonElement>;client:Client;picked:PickedMaterials;chatId?:string;attached:string[];
  onChange:(next:PickedMaterials)=>void;onClose:()=>void;
}){
  const popup=useRef<HTMLElement>(null);
  useAnchoredMenu(true,trigger,popup,focus=>{onClose();if(focus)trigger.current?.focus();});
  const [search,setSearch]=useState(''),[options,setOptions]=useState<Options|null>(null),[error,setError]=useState('');
  useEffect(()=>{
    const c=client.child();setOptions(null);setError('');
    const timer=setTimeout(()=>void c.json<Options>('/v1/workspace/projects/'+picked.folder_id+'/materials?search='+encodeURIComponent(search),'MaterialOptions')
      .then(setOptions).catch(e=>{if(!c.signal.aborted)setError(errorText(e));}),100);
    return ()=>{clearTimeout(timer);c.close();};
  },[client,picked.folder_id,search]);
  const chats=options?.chats.filter(item=>item.id!==chatId)??[];
  return <section ref={popup} className="project-materials" aria-label="Материалы проекта">
    <header><h3>Добавить в следующий запрос</h3><button type="button" onClick={()=>{onClose();trigger.current?.focus();}}>Готово</button></header><p>До трёх PDF и трёх чатов на один запрос. Для реплик о документе выберите и его PDF.</p>
    <input autoFocus type="search" aria-label="Найти материал проекта" placeholder="Название файла или чата" maxLength={300} value={search} onChange={e=>setSearch(e.target.value)}/>
    <div className="material-list">{error?<p role="alert">{error}</p>:!options?<small role="status">Загружаем материалы…</small>:<>
      {!!options.pdfs.length&&<strong>Документы</strong>}{options.pdfs.map(pdf=>{
        const checked=picked.pdfs.some(v=>v.id===pdf.id),local=attached.includes(pdf.id);
        return <label key={pdf.id}><input type="checkbox" checked={checked||local}
          disabled={local||(!checked&&picked.pdfs.length+attached.length>=3)}
          onChange={()=>onChange({...picked,pdfs:checked?picked.pdfs.filter(v=>v.id!==pdf.id):[...picked.pdfs,pdf]})}/>
          <span>{pdf.original_filename} · v{pdf.revision}</span></label>;})}
      {!!chats.length&&<strong>Переписка</strong>}{chats.map(chat=>{
        const selected=picked.chats.find(v=>v.id===chat.id);
        return <label key={chat.id}><input type="checkbox" checked={!!selected} disabled={!selected&&picked.chats.length>=3}
          onChange={()=>onChange({...picked,chats:selected?picked.chats.filter(v=>v.id!==chat.id):[...picked.chats,chat]})}/>
          <span>{chat.title} · по сообщ. {selected?.through_sequence??chat.through_sequence}</span></label>;})}
      {!options.pdfs.length&&!chats.length&&<p>{search?'Материалы не найдены.':'Других чатов и PDF пока нет.'}</p>}
      {options.more&&<small>Уточните название, чтобы найти остальные материалы.</small>}
    </>}</div>
  </section>;
}
