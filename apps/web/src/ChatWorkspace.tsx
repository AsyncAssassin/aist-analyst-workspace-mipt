import React,{useEffect,useRef,useState} from 'react';
import {Client,errorText} from './client';
import {finishActiveEdit,confirmNavigation, Operations, readLocal, writeLocal} from './local';
import {ProjectWorkspace} from './Workspace';
import {WorkspaceIcon} from './WorkspaceIcon';
import {AistBrand} from './AistBrand';
import {skillDescription} from './SkillPicker';
import {ChatStart} from './ChatStart';
import {useAnchoredMenu} from './useAnchoredMenu';
import './chat-workspace.css';

type Folder={id:string;name:string;pinned:boolean;chat_count?:number};
type Chat={id:string;project_id:string;folder_project_id:string|null;title:string;last_sent_at:string};
type Opened={chat:Chat;note_id:string|null};
type Cursor={before_time:string;before_id:string};
type Route={view:'home'|'projects'|'folder'|'chat'|'skills';id:string};
type Action={action:'pin';project_id:string;pinned:boolean}|{action:'move';chat_id:string;expected_folder_id:string|null;folder_id:string|null}|{action:'hide';chat_id:string}|{action:'rename';chat_id:string;expected_title:string;title:string};
const home:Route={view:'home',id:''};

function initialRoute():Route{
  const chat=new URL(location.href).searchParams.get('chat');
  return chat?{view:'chat',id:chat}:history.state?.workspace??readLocal('chat-route',home);
}
function useChatPage(client:Client,folder:string|null,tick:number){
  const [items,setItems]=useState<Chat[]>([]),[cursor,setCursor]=useState<Cursor|null>(null);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const request=useRef<Client|null>(null);
  async function load(after:Cursor|null){
    request.current?.close();const c=client.child();request.current=c;setBusy(true);setError('');
    const q=new URLSearchParams({limit:'50',...(folder?{folder_id:folder}:{}),...(after??{})});
    try{
      const result=await c.json<{items:Chat[];next_cursor:Cursor|null}>('/v1/workspace/chats?'+q,'RecentChatPage');
      setItems(old=>after?[...old,...result.items.filter(x=>!old.some(y=>y.id===x.id))]:result.items);setCursor(result.next_cursor);
    }catch(e){if(!c.signal.aborted)setError(errorText(e));}finally{if(!c.signal.aborted)setBusy(false);}
  }
  useEffect(()=>{setItems([]);setCursor(null);if(folder!==null)void load(null);return()=>request.current?.close();},[client,folder]);
  const lastTick=useRef(tick);
  useEffect(()=>{if(lastTick.current!==tick && folder!==null)void load(null);lastTick.current=tick;},[tick]);
  return {items,error,more:cursor?<button disabled={busy} onClick={()=>void load(cursor)}>Показать ещё чаты</button>:null};
}

export function ChatWorkspace({client,onLogout}:{client:Client;onLogout:()=>void}){
  const [route,setRoute]=useState<Route>(initialRoute);
  const [back,setBack]=useState<Route>(()=>readLocal('skills-return',home));
  const [folders,setFolders]=useState<Folder[]>([]),[search,setSearch]=useState('');
  const [folderCursor,setFolderCursor]=useState<string|null>(null);
  const [tick,setTick]=useState(0),[opened,setOpened]=useState<Opened|null>(null);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [mobile,setMobile]=useState(false),[menu,setMenu]=useState<'actions'|'move'|'hide'|'rename'|null>(null);
  const [chatName,setChatName]=useState('');
  const [newFolder,setNewFolder]=useState(false),[name,setName]=useState('');
  const [ops]=useState(()=>new Operations('folder-action'));
  const [projects]=useState(()=>new Operations('folder-create'));
  const lock=useRef(false),folderRequest=useRef<Client|null>(null);
  const recentList=useRef<HTMLDivElement>(null),moveButton=useRef<HTMLButtonElement>(null);
  const actionBox=useRef<HTMLDivElement>(null);
  const actionButton=useRef<HTMLButtonElement>(null);
  useAnchoredMenu(!!menu&&!newFolder,actionButton,actionBox,focus=>{setMenu(null);if(focus)actionButton.current?.focus();});
  const recent=useChatPage(client,'',tick),children=useChatPage(client,route.view==='folder'?route.id:null,tick);
  const query=route.view==='projects'?search:'';
  const selected=opened?.chat.id===route.id?opened:null;
  const [folderInfo,setFolderInfo]=useState<{id:string;name:string}|null>(null);
  const folder=folders.find(p=>p.id===route.id)??(folderInfo?.id===route.id?folderInfo:null);

  async function loadFolders(after:string|null){
    folderRequest.current?.close();const c=client.child();folderRequest.current=c;
    const q=new URLSearchParams({limit:'50',search:query,...(after?{after_id:after}:{})});
    try{const page=await c.json<{items:Folder[];next_after_id:string|null}>('/v1/workspace/projects?'+q,'FolderPage');
      setFolders(old=>after?[...old,...page.items.filter(f=>!old.some(x=>x.id===f.id))]:page.items);setFolderCursor(page.next_after_id);
    }catch(e){if(!c.signal.aborted)setError(errorText(e));}
  }
  useEffect(()=>{void loadFolders(null);return()=>folderRequest.current?.close();},[client,query,tick]);
  useEffect(()=>{
    const c=client.child();
    if(route.view==='chat')void c.json<Opened>('/v1/workspace/chats/'+route.id,'OpenedChat').then(setOpened).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    if(route.view==='folder')void c.json<Folder>('/v1/projects/'+route.id,'Project').then(setFolderInfo).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return()=>c.close();
  },[client,route.view,route.id,tick]);
  useEffect(()=>{
    history.replaceState({workspace:initialRoute()},'');
    const pop=()=>{setRoute(initialRoute());setMenu(null);setMobile(false);setNewFolder(false);};
    function esc(e:KeyboardEvent){if(e.key==='Escape'){setMobile(false);if(actionBox.current)actionButton.current?.focus();setMenu(null);setNewFolder(false);}}
    window.addEventListener('popstate',pop);window.addEventListener('keydown',esc);
    return()=>{window.removeEventListener('popstate',pop);window.removeEventListener('keydown',esc);};
  },[]);
  function go(next:Route){
    if(!confirmNavigation())return;
    const url=new URL(location.href);if(next.view==='chat')url.searchParams.set('chat',next.id);else url.searchParams.delete('chat');
    url.hash='';history.pushState({workspace:next},'',url);writeLocal('chat-route',next);
    setRoute(next);setMenu(null);setNewFolder(false);setMobile(false);setError('');setSearch('');
  }
  async function skills(){if(route.view==='skills')return;
    try{await finishActiveEdit();}catch{setError('Сначала завершите сохранение записки.');return;}writeLocal('skills-return',route);setBack(route);go({view:'skills',id:''});}
  function bump(){setTick(n=>n+1);}
  async function run(fn:()=>Promise<void>){
    if(lock.current)return;lock.current=true;setBusy(true);setError('');
    try{await fn();}catch(e){if(!client.signal.aborted)setError(errorText(e));}
    finally{lock.current=false;if(!client.signal.aborted)setBusy(false);}
  }
  async function change(a?:Action){
    if(a)ops.prepare('/v1/workspace/actions',a,'NavigationResult','Изменение навигации');
    const pending=ops.pending;if(!pending)return;
    const action:Action=JSON.parse(pending.body);
    await ops.send(client);
    if(action.action==='hide' && route.id===action.chat_id)go(home);
    if(action.action==='move')setOpened(old=>old&&old.chat.id===action.chat_id?{...old,chat:{...old.chat,folder_project_id:action.folder_id}}:old);
    if(action.action==='rename')setOpened(old=>old&&old.chat.id===action.chat_id?{...old,chat:{...old.chat,title:action.title}}:old);
    setMenu(null);bump();
  }
  function move(to:string|null){if(selected)void run(()=>change({action:'move',chat_id:selected.chat.id,expected_folder_id:selected.chat.folder_project_id,folder_id:to}));}
  async function createFolder(){
    if(!projects.pending&&!name.trim())return;
    projects.prepare('/v1/projects',{name:name.trim()},'Project','Новый проект');
    const p=await projects.send<Folder>(client);setName('');setNewFolder(false);
    if(menu==='move'&&selected)await change({action:'move',chat_id:selected.chat.id,expected_folder_id:selected.chat.folder_project_id,folder_id:p.id});
    else {go({view:'folder',id:p.id});bump();}
  }
  function openChat(id:string){go({view:'chat',id});}
  const projectMore=folderCursor?<button onClick={()=>void loadFolders(folderCursor)}>Показать ещё проекты</button>:null;
  const form=(id:string)=><ChatStart key={id} client={client} folder={id} onOpen={chat=>{openChat(chat);bump();if(recentList.current)recentList.current.scrollTop=0;}} onSkills={skills}/>;
  const chatFolder=selected?.chat.folder_project_id;
  const pageTitle=route.view==='chat'?selected?.chat.title:route.view==='folder'?folder?.name:route.view==='projects'?'Проекты':route.view==='skills'?'Навыки':'';
  const collapseEmpty = !search.trim() && folders.some(p=>p.pinned||p.chat_count!==0);
  const visibleFolders = collapseEmpty ? folders.filter(p=>p.pinned||p.chat_count!==0) : folders;
  const emptyFolders = collapseEmpty ? folders.filter(p=>!p.pinned&&p.chat_count===0) : [];
  function folderCards(items:Folder[]) {
    return <div className="folder-grid">{items.map(p=><article key={p.id} className="folder-card"><button className="folder-title" onClick={()=>go({view:'folder',id:p.id})}><WorkspaceIcon name="folder"/><strong>{p.name}</strong>{p.chat_count!=null&&<small>Чатов: {p.chat_count}</small>}{p.pinned&&<small className="pinned-label">Закреплён</small>}</button>
          <button className="project-pin" aria-label={p.pinned?'Открепить проект':'Закрепить проект'} title={p.pinned?'Открепить проект':'Закрепить проект'} aria-pressed={p.pinned} disabled={busy} onClick={()=>void run(()=>change({action:'pin',project_id:p.id,pinned:!p.pinned}))}><WorkspaceIcon name="pin"/></button></article>)}</div>;
  }
  return <div className="chat-shell">
    {mobile&&<button className="drawer-shade" aria-label="Закрыть меню" onClick={()=>setMobile(false)}/>}
    <nav className={'chat-sidebar'+(mobile?' shown':'')} aria-label="Навигация">
      <button className="workspace-brand" aria-label="AIST, новый чат" onClick={()=>go(home)}><AistBrand/></button>
      <button className="close-drawer" onClick={()=>setMobile(false)}>Закрыть меню</button><button className="new-chat" onClick={()=>go(home)}><WorkspaceIcon name="plus"/>Новый чат</button>
      <button aria-current={route.view==='projects'||route.view==='folder'?'page':undefined} onClick={()=>go({view:'projects',id:''})}><WorkspaceIcon name="folder"/>Проекты</button><button aria-current={route.view==='skills'?'page':undefined} onClick={skills}><WorkspaceIcon name="layers"/>Навыки</button>
      <small className="recent-label">Недавние чаты</small><div className="recent-list" ref={recentList}>{recent.items.map(c=><button aria-current={route.view==='chat'&&route.id===c.id?'page':undefined}
        key={c.id} title={c.title} onClick={()=>openChat(c.id)}>{c.title}</button>)}{recent.more}{recent.error&&<p role="alert">{recent.error}</p>}</div>
      <div className="account-line"><span>Личное пространство</span><button className="logout-link" onClick={onLogout}>Выйти</button></div></nav>
    <div className="workspace-body"><header className="workspace-bar">
      <button className="drawer-open" aria-expanded={mobile} aria-label="Открыть меню" onClick={()=>setMobile(true)}><WorkspaceIcon name="menu"/></button>
      {chatFolder&&<><button className="breadcrumb-link" onClick={()=>go({view:'folder',id:chatFolder})}>{folders.find(p=>p.id===chatFolder)?.name??'К проекту'}</button><span aria-hidden="true">/</span></>}
      <span className="workspace-title" title={pageTitle}>{pageTitle}</span>
      {selected&&route.view==='chat'&&<div className="chat-menu-anchor"><button className="chat-more" ref={actionButton} aria-label="Действия с чатом" title="Действия с чатом" aria-expanded={!!menu} onClick={()=>setMenu(menu?null:'actions')}><WorkspaceIcon name="more"/></button>
        {menu&&<div className="chat-actions" ref={actionBox} aria-label="Меню чата">
          {menu==='actions'&&<><button onClick={()=>{setChatName(selected.chat.title);setMenu('rename');}}>Переименовать</button><button ref={moveButton} onClick={()=>setMenu('move')}>{selected.chat.folder_project_id?'Изменить проект':'Добавить в проект'}</button>
          {selected.chat.folder_project_id&&<button onClick={()=>move(null)}>Убрать из проекта</button>}<button onClick={()=>setMenu('hide')}>Удалить чат</button></>}
          {menu==='move'&&<><button onClick={()=>{setMenu('actions');requestAnimationFrame(()=>moveButton.current?.focus());}}>Назад</button>
          {folders.map(p=><button disabled={busy} key={p.id} onClick={()=>move(p.id)}>{p.name}</button>)}{projectMore}<button onClick={()=>setNewFolder(true)}>Создать проект</button></>}
          {menu==='rename'&&<form onSubmit={e=>{e.preventDefault();if(chatName.trim())void run(()=>change({action:'rename',chat_id:selected.chat.id,expected_title:selected.chat.title,title:chatName.trim()}));}}>
            <label>Название чата<input autoFocus maxLength={300} required value={chatName} onChange={e=>setChatName(e.target.value)}/></label><button disabled={busy||!chatName.trim()}>Сохранить название</button><button type="button" onClick={()=>setMenu('actions')}>Отмена</button></form>}
          {menu==='hide'&&<><p>Убрать чат из вашей истории?</p><button disabled={busy} onClick={()=>void run(()=>change({action:'hide',chat_id:selected.chat.id}))}>Подтвердить удаление</button><button onClick={()=>setMenu(null)}>Отмена</button></>}
        </div>}
      </div>}
    </header><section className={'chat-content view-'+route.view} id={route.view==='chat'?undefined:'conversation'}>
      {error&&<p role="alert">{error}</p>}{ops.pending&&!busy&&<button onClick={()=>void run(()=>change())}>Проверить действие</button>}
      {projects.pending&&!busy&&!newFolder&&<button onClick={()=>void run(createFolder)}>Проверить создание проекта</button>}
      {route.view==='home'&&<div className="chat-home"><h1>Приступим к анализу</h1><p>Задайте вопрос или подготовьте записку по документу.</p>{form('')}</div>}
      {route.view==='skills'&&<div className="skills-page"><h1>Навыки</h1><article className="skill-card"><WorkspaceIcon name="document"/><h2>Аналитическая записка</h2>
        <p>{skillDescription}</p><button onClick={()=>go(back.view==='skills'?home:back)}>Выбрать и вернуться</button></article></div>}
      {route.view==='projects'&&<div className="folder-page"><div className="action-header"><h1>Проекты</h1><button className="new-folder" onClick={()=>setNewFolder(true)}><WorkspaceIcon name="plus"/>Новый проект</button></div>
        <label className="folder-search"><WorkspaceIcon name="search"/><input aria-label="Найти проект" placeholder="Найти проект" maxLength={300} value={search} onChange={e=>setSearch(e.target.value)}/></label>
        {folderCards(visibleFolders)}
        {emptyFolders.length>0&&<details className="empty-folders"><summary>Пустые проекты · {emptyFolders.length}</summary>{folderCards(emptyFolders)}</details>}
        {!folders.length&&<p>Проекты не найдены.</p>}{projectMore}</div>}
      {route.view==='folder'&&<div className="folder-page"><h1>{folder?.name??'Проект'}</h1>{folder&&form(route.id)}<h2 className="project-chats-heading">Чаты проекта</h2>
        {children.items.map(c=><button className="folder-chat" key={c.id} onClick={()=>openChat(c.id)}><WorkspaceIcon name="chat"/><span>{c.title}</span><span aria-hidden="true">›</span></button>)}{children.more}{children.error&&<p role="alert">{children.error}</p>}</div>}
      {route.view==='chat'&&selected&&<>
        <ProjectWorkspace key={selected.chat.id} parent={client} project={{id:selected.chat.project_id,name:selected.chat.title}}
          onSkills={skills} onNoteReady={bump} opened={{id:selected.chat.id,note:selected.note_id,folder:selected.chat.folder_project_id}} onActivity={()=>{bump();if(recentList.current)recentList.current.scrollTop=0;}}/></>}
      {newFolder&&<form className="create-folder" aria-label="Создание проекта" onSubmit={e=>{e.preventDefault();void run(createFolder);}}>
        <label>Название проекта<input autoFocus required maxLength={300} value={name} onChange={e=>setName(e.target.value)}/></label>
        <button disabled={busy||!name.trim()}>Создать проект</button><button type="button" onClick={()=>setNewFolder(false)}>Отмена</button></form>}
    </section></div></div>;
}
