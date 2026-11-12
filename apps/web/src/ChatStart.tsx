import React,{useEffect,useRef,useState} from 'react';
import {ApiError,Client,errorText} from './client';
import {finishActiveEdit,Operations,operationBody,readLocal,writeLocal} from './local';
import {WorkspaceIcon} from './WorkspaceIcon';
import {useAnchoredMenu} from './useAnchoredMenu';
import {SkillPicker} from './SkillPicker';
import {useEditCommand,wantsEdit,type Proposal} from './ChatEdit';
import {ProjectMaterials,type PickedMaterials} from './ProjectMaterials';
import {PdfUpload} from './Sources';
import type {Conversation,Registry,Run,SourceVersion} from './types';

type Kind='evidence_answer'|'analytical_note';
type Pdf=Pick<SourceVersion,'id'|'original_filename'|'revision'|'page_count'>;
type Attachment=Pdf&{included:boolean};
type Draft={text:string;files:Attachment[];explicit:boolean;materials?:PickedMaterials};
type Start={project:string;conversation:string;text:string;model:string;kind:Kind;sections?:string[];ids:string[];title:string;
  createKey:string;sendKey:string;revision:number;contextId?:string;materials?:PickedMaterials};
export type Discussion={id:string;number:number;title:string;sourceIds:string[]};
type Existing={editVersion:string|null;onProposal:(proposal:Proposal)=>void;id:string;revision:number|null;active:boolean;context:Discussion|null;pins:Pdf[];
  clearContext:()=>void;onRun:(run:Run)=>void;onConflict:()=>void};
type Props={client:Client;folder:string;materialFolder?:string|null;chat?:Existing;onOpen?:(chat:string)=>void;onSkills?:()=>void};

function readDraft(scope:string,id?:string):Draft{
  const old=readLocal<Partial<Draft>&{file?:Pdf}>(scope,{});
  return {text:old.text??(id?readLocal('draft.'+id,''):''),explicit:old.explicit??false,materials:old.materials,
    files:old.files??(old.file?[{...old.file,included:true}]:[])};
}
export function setDiscussionFiles(id:string,pins:Pdf[]){
  const scope='compose.'+id,draft=readDraft(scope,id);
  // An explicit exclusion always requires the reader's decision.
  if(!draft.explicit)writeLocal(scope,{...draft,files:pins.map(p=>({...p,included:true}))});
}
function infer(text:string,count:number,context:boolean):Kind|null{
  if(!count)return 'evidence_answer';
  if(!text || /(?:подготов|созда|состав|сформир|напиш|сдела|собер)[^.!?]{0,55}(?:записк|справк|(?:^|\s)аз(?:\s|$))/iu.test(text))return 'analytical_note';
  if(context||text.endsWith('?')||/^(?:как|где|почему|сколько|что|когда|каков|уточни|объясни|поясни|сравни|расскажи)(?:\s|$)/iu.test(text))return 'evidence_answer';
  return null;
}

export const noteSections=['Описание возможности','Описание Компании','Рынок и конкуренты','Акционеры','Инвест. тезис','Описание Сделки','Потенциальные риски','Следующие шаги','News Run','Терминология','Приложения'];

export function sectionsForNote(text:string){
  // A requested structure belongs to the user's prompt, including names outside the default catalogue.
  return /(?:раздел(?:ы|ами|ах|а|ов)?(?=$|[^а-яё])|заголовк|структур[аеуы]|только (?:про |о )?финанс)/iu.test(text)?[]:noteSections;
}

function messageBody(intent:Start){
  const legacyFile=(intent as Start&{file?:string}).file;
  return {expected_revision:intent.revision??0,text:intent.text,source_version_ids:intent.ids??(legacyFile?[legacyFile]:[]),
    skill_version_id:intent.kind==='analytical_note'?'skill_analytical-note-v3':'skill_source-answer-v3',
    model_profile_version_id:intent.model,requested_result_type:intent.kind,parameters:{audience:null,sections:intent.sections??[]},
    base_version_id:null,expected_artifact_revision:0,requested_export:null,...(intent.contextId?{context_version_id:intent.contextId}:{}),
    ...(intent.materials?{project_context:{folder_id:intent.materials.folder_id,pdf_version_ids:intent.materials.pdfs.map(p=>p.id),
      chats:intent.materials.chats.map(({id,through_sequence})=>({id,through_sequence}))}}:{})};
}

export function ChatStart({client:parent,folder,materialFolder,chat,onOpen,onSkills}:Props){
  const [client]=useState(()=>parent.child());
  useEffect(()=>()=>client.close(),[client]);
  const scope=chat?'compose.'+chat.id:'start.'+(folder||'personal');
  const [draft,setDraft]=useState(()=>readDraft(scope,chat?.id));
  const [intent,setIntent]=useState(()=>readLocal<Start|null>(scope+'.intent',null));
  const uploadKey=scope+'.upload';
  const recoverUpload=readLocal<{path:string}|null>(uploadKey,null);
  const [pendingUpload,setPendingUpload]=useState(!!recoverUpload);
  const [project,setProject]=useState(folder||recoverUpload?.path.split('/')[3]||'');
  const [picker,setPicker]=useState<'recent'|'materials'|null>(null);
  const [recent,setRecent]=useState<Pdf[]|null>(null),[choice,setChoice]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const [uploadBusy,setUploadBusy]=useState(false);
  const lock=useRef(false);
  const input=useRef<HTMLTextAreaElement>(null);
  const uploadInput=useRef<HTMLInputElement>(null);
  const fileButton=useRef<HTMLButtonElement>(null);
  const fileMenu=useRef<HTMLDivElement>(null);
  useAnchoredMenu(picker==='recent',fileButton,fileMenu,focus=>{
    setPicker(null);
    if(focus)fileButton.current?.focus();
  });
  const contextButton=useRef<HTMLButtonElement>(null);
  const [personal]=useState(()=>new Operations(scope+'.personal'));
  const [create]=useState(()=>new Operations(scope+'.create'));
  const [submit]=useState(()=>new Operations(scope+'.send'));
  const context=chat?.context;
  const selected=draft.files.filter(f=>f.included);
  const visibleFolder=chat?materialFolder:folder;
  useEffect(()=>{
    if(!intent&&draft.materials&&draft.materials.folder_id!==visibleFolder){
      const clean={...draft,materials:undefined};writeLocal(scope,clean);setDraft(clean);setPicker(null);
    }
  },[scope,visibleFolder,draft,intent]);
  const materials=draft.materials?.folder_id===visibleFolder?draft.materials:undefined;
  const sharedPdfs=materials?.pdfs??[],sharedChats=materials?.chats??[];
  const fileCount=selected.length+sharedPdfs.length;
  const missing=context?.sourceIds.some(id=>!selected.some(f=>f.id===id)&&!sharedPdfs.some(f=>f.id===id));
  function remember(next:Draft){writeLocal(scope,next);setDraft(next);setChoice(false);}
  function checkpoint(next:Start|null){writeLocal(scope+'.intent',next);setIntent(next);}
  async function storageProject(){
    if(project)return project;
    personal.prepare('/v1/workspace/actions',{action:'personal'},'NavigationResult','Личное пространство');
    const result=await personal.send<{id:string}>(client);setProject(result.id);return result.id;
  }
  async function act(fn:()=>Promise<void>){
    if(lock.current)return;
    lock.current=true;setBusy(true);setError('');
    try{await fn();}catch(e){if(!client.signal.aborted)setError(errorText(e));}
    finally{lock.current=false;if(!client.signal.aborted)setBusy(false);}
  }
  async function openFiles(){
    if(picker==='recent'){setPicker(null);return;}
    const id=folder||project||(await client.json<{personal_id:string|null}>('/v1/workspace','WorkspaceIdentity')).personal_id;
    if(id)setProject(id);
    const items=id?(await client.json<{items:Pdf[]}>('/v1/workspace/projects/'+id+'/recent-pdfs','RecentPdfPage')).items:[];
    setRecent(items);setPicker('recent');
  }
  function attach(p:Pdf){
    if(sharedPdfs.some(f=>f.id===p.id)||(!selected.some(f=>f.id===p.id)&&fileCount>=3))return;
    const latest=readDraft(scope,chat?.id);
    remember({...latest,explicit:true,files:[...latest.files.filter(f=>f.id!==p.id),{...p,included:true}]});
    setPicker(null);setError('');
  }
  const edit=useEditCommand(client,scope,'/v1/projects/'+folder+'/conversations/'+chat?.id,chat?.revision??null,(run,proposal)=>{
    remember({...draft,text:'',materials:undefined});
    if(run){writeLocal('follow-answer.'+run.id,true);chat?.onRun(run);}
    else if(proposal)chat?.onProposal(proposal);
  });
  const editing=!!chat&&(edit.pending||wantsEdit(draft.text));
  async function send(forced?:Kind){
    if(editing&&!intent){
      await edit.send(draft.text.trim(),chat!.editVersion);
      return;
    }
    let next=intent;
    if(!next){
      if(missing||chat?.active||chat?.revision===null||picker||pendingUpload)return;
      const text=draft.text.trim();
      if(!text&&!fileCount)return;
      const kind=forced??infer(text,fileCount,!!context);
      if(!kind){setChoice(true);return;}
      const registry=await client.json<Registry>('/v1/capabilities','Registry');
      const skill=kind==='analytical_note'?'skill_analytical-note-v3':'skill_source-answer-v3';
      const model=registry.model_profiles.find(m=>m.status==='enabled'&&m.configuration_sha256);
      if(!model||registry.model_execution?.state!=='available'||!registry.skills.some(s=>s.id===skill&&s.status==='enabled')){
        setError(registry.model_execution?.message||'Исполнение недоступно. Готовые материалы доступны в проектах.');return;
      }
      const saved=await finishActiveEdit();
      const noteId=context&&kind==='evidence_answer'?(saved?.parentId===context.id?saved.id:context.id):undefined;
      const ids=selected.map(f=>f.id);
      next={project:await storageProject(),conversation:chat?.id??'',text:text||'Подготовь аналитическую записку.',
        sections:kind==='analytical_note'?sectionsForNote(text):[],ids,model:model.id,kind,title:(text.split('\n')[0]||(selected[0]??sharedPdfs[0]).original_filename).slice(0,300),
        revision:chat?.revision??0,createKey:crypto.randomUUID(),sendKey:crypto.randomUUID(),
        ...(noteId?{contextId:noteId}:{}), ...(materials&&(sharedPdfs.length||sharedChats.length)?{materials}:{})};
      operationBody('/messages',messageBody(next),'Submission');
      checkpoint(next);
    }
    const path='/v1/projects/'+next.project;
    if(!next.conversation){
      create.prepare(path+'/conversations',{title:next.title||(draft.text.trim().split('\n')[0]||draft.files[0]?.original_filename||'Новый чат').slice(0,300)},'Conversation','Создание чата',next.createKey);
      const partial=next;
      await create.send<Conversation>(client,c=>checkpoint({...partial,conversation:c.id}));
      next=readLocal<Start>(scope+'.intent',next);
    }
    submit.prepare(path+'/conversations/'+next.conversation+'/messages',messageBody(next),'Submission','Отправка сообщения',next.sendKey);
    const conversation=next.conversation;
    try{
      await submit.send<{run:Run}>(client,({run})=>{
        writeLocal((run.input.requested_result_type==='analytical_note'?'auto-result.':'follow-answer.')+run.id,true);
        const keep={...draft,text:'',materials:undefined};writeLocal('compose.'+conversation,keep);
        remember(chat?keep:{text:'',files:[],explicit:false});checkpoint(null);
        if(chat)chat.onRun(run);else {writeLocal('entry-run.'+conversation,run.id);onOpen?.(conversation);}
      });
    }catch(e){
      if(e instanceof ApiError&&!submit.pending){
        if(!chat){
          writeLocal('compose.'+conversation,draft);writeLocal('entry-error.'+conversation,errorText(e));
          remember({text:'',files:[],explicit:false});onOpen?.(conversation);
        }else chat.onConflict();
        checkpoint(null);
      }
      throw e;
    }
  }
  const locked=busy||uploadBusy||!!intent||!!chat?.active;
  const canSend=!!intent||(!locked&&(!missing||editing)&&!picker&&!pendingUpload&&chat?.revision!==null&&(!!draft.text.trim()||!!fileCount));
  const inputLabel=chat?'Сообщение AIST':'Первое сообщение';
  return <div className="start-box" aria-label="Форма сообщения">
    {context&&<div className="notice">Обсуждаем: {context.title} · v{context.number}. Правку можно посмотреть до применения.
      <button disabled={locked} onClick={()=>chat?.clearContext()}>Без контекста записки</button></div>}
    {missing&&<div role="alert">Для вопроса по этой записке нужны её исходные PDF.
      <button disabled={locked||!chat||!context||chat.pins.length!==context.sourceIds.length} onClick={()=>{
        if(chat)remember({...draft,explicit:true,files:chat.pins.map(f=>({...f,included:true}))});
      }}>Восстановить источники записки</button> Или выберите «Без контекста записки».</div>}
    {materials&&(sharedPdfs.length>0||sharedChats.length>0)&&<div className="picked-materials">{[...sharedPdfs.map(p=>({id:p.id,label:p.original_filename+' · v'+p.revision})),
      ...sharedChats.map(c=>({id:c.id,label:c.title+' · по сообщ. '+c.through_sequence}))].map(item=><button key={item.id} disabled={locked}
        aria-label={'Убрать из запроса '+item.label} onClick={()=>remember({...draft,materials:{...materials,pdfs:sharedPdfs.filter(p=>p.id!==item.id),chats:sharedChats.filter(c=>c.id!==item.id)}})}>{item.label} ×</button>)}</div>}
    {!!draft.files.length&&<div className="attached-files" aria-label="Прикреплённые PDF">{draft.files.map(f=><div key={f.id} className={'attached-file'+(f.included?'':' excluded')}>
      <WorkspaceIcon name="document"/>
      <label title={f.original_filename}><input type="checkbox" checked={f.included} disabled={locked||!!picker||(!f.included&&(fileCount>=3||sharedPdfs.some(p=>p.id===f.id)))}
        aria-label={'Использовать файл '+f.original_filename}
        onChange={e=>remember({...draft,explicit:true,files:draft.files.map(v=>v.id===f.id?{...v,included:e.target.checked}:v)})}/>
        <span><strong>{f.original_filename}</strong><small>PDF · v{f.revision}{f.included?'':' · Не используется'}</small></span></label>
      <button aria-label={'Убрать файл '+f.original_filename} disabled={locked||!!picker} onClick={()=>remember({...draft,explicit:true,files:draft.files.filter(v=>v.id!==f.id)})}><WorkspaceIcon name="close"/></button>
    </div>)}</div>}
    <textarea ref={input} aria-label={inputLabel} placeholder="Что хотите узнать или подготовить?" value={draft.text} maxLength={20000}
      disabled={busy||!!intent||edit.pending||chat?.revision===null} onChange={e=>{edit.clear();remember({...draft,text:e.target.value});}}
      onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();if(canSend)void act(send);}}}/>
    {chat?.revision===null&&<small role="status">Загружаем историю чата…</small>}
    {!!draft.files.length&&<div className="attachment-sources"><span>PDF</span><span>СПАРК · скоро</span><span>MOEX · скоро</span></div>}
    <div className="start-tools"><button ref={fileButton} className="start-tool" disabled={locked||pendingUpload} aria-expanded={picker==='recent'} onClick={()=>void act(openFiles)}>
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m8 13 7-7a3 3 0 0 1 4.2 4.2l-9 9a5 5 0 0 1-7.1-7.1l9.5-9.5M6 15l8-8"/></svg> Файл</button>
      {picker==='recent'&&<div ref={fileMenu} className="pdf-picker" role="group" aria-label="Выбрать PDF">
        <button type="button" className="pdf-upload-action" disabled={fileCount>=3||busy} onClick={()=>{uploadInput.current?.click();setPicker(null);fileButton.current?.focus();}}><WorkspaceIcon name="plus"/>С компьютера</button>
        <h3>Недавние PDF</h3>
        {recent?.length?recent.map(f=><button type="button" title={f.original_filename} key={f.id}
          disabled={fileCount>=3||sharedPdfs.some(v=>v.id===f.id)||selected.some(v=>v.id===f.id)}
          onClick={()=>{attach(f);input.current?.focus();}}><WorkspaceIcon name="document"/><span>{f.original_filename}</span><small>v{f.revision}</small></button>)
          :<p>Недавних PDF пока нет</p>}
        {fileCount>=3&&<small>Уже выбраны три PDF.</small>}
        <small className="pdf-upload-hint">До 16 МиБ и 40 страниц. При отправке запроса PDF передаётся модели.</small>
      </div>}
      {visibleFolder&&<button ref={contextButton} className="start-tool" disabled={locked||pendingUpload} aria-label="Добавить контекст проекта" title="Добавить материалы проекта" aria-expanded={picker==='materials'}
        onClick={()=>setPicker(picker==='materials'?null:'materials')}> <WorkspaceIcon name="folder"/>Контекст</button>}
      <SkillPicker disabled={locked} onChoose={()=>input.current?.focus()} onSkills={onSkills}/>
      <button className="selected-skill" disabled={locked} title="Закреплённый навык. Открыть описание" aria-label="Подробнее о навыке «Аналитическая записка», закреплён" onClick={onSkills}>
        <WorkspaceIcon name="pin"/><span>Аналитическая записка</span></button>
      <button className="send-small" aria-label={intent||edit.pending?'Продолжить отправку':'Отправить сообщение'} disabled={busy||!canSend}
        onClick={()=>void act(send)}>{intent?'↻':'↑'}</button></div>
    {picker==='materials'&&visibleFolder&&<ProjectMaterials trigger={contextButton} client={client} chatId={chat?.id} attached={selected.map(v=>v.id)}
      picked={materials??{folder_id:visibleFolder,pdfs:[],chats:[]}} onChange={value=>remember({...draft,materials:value})} onClose={()=>setPicker(null)}/>}
    <div className="composer-upload"><PdfUpload client={client} project={project} sources={[]} versions={[]} recoveryScope={uploadKey}
      compact inputRef={uploadInput} resolveProject={storageProject} onPendingChange={setPendingUpload}
      onBusy={setUploadBusy} onUploaded={file=>{attach(file);input.current?.focus();}} onRefresh={()=>setRecent(null)}/></div>
    {choice&&!intent&&!picker&&<div>Что сделать по выбранному PDF?
      <button onClick={()=>void act(()=>send('evidence_answer'))}>Спросить</button><button onClick={()=>void act(()=>send('analytical_note'))}>Подготовить записку</button></div>}
    {edit.hint&&<p role="status">{edit.hint}</p>}
    {edit.choice&&<section className="edit-choices"><strong>Какое место изменить?</strong>{edit.choice.plan.choices.map(c=>
      <button disabled={locked} key={JSON.stringify(c.target)} onClick={()=>void act(()=>edit.send(edit.choice!.text,edit.choice!.plan.version_id,c.target))}>{c.label}</button>)}</section>}
    {edit.pending&&!busy&&<p role="status">Для проверки правки продолжите отправку. {edit.canDiscard&&<button onClick={edit.discard}>Отменить подготовку</button>}</p>}
    {intent&&<p role="status">Отправка начата. Продолжение проверит тот же запрос.</p>}
    {chat?.active&&!intent&&<p role="status">Дождитесь результата текущей задачи.</p>}
    {error&&<p role="alert">{error}</p>}
  </div>;
}
