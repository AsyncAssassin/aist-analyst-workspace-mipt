import React, { useEffect, useRef, useState } from 'react';
import { Client, errorText } from './client';
import { Operations, readLocal, writeLocal, confirmNavigation } from './local';
import {ProposalCard,type Proposal} from './ChatEdit';
import { ChatStart, setDiscussionFiles } from './ChatStart';
import { ChatAnswer } from './ChatAnswer';
import { ProjectHome } from './ProjectHome';
import { Sources, PdfUpload } from './Sources';
import { ResultPanel, RunPanel } from './RunPanel';
import { isActive, states, type Conversation, type Message, type Project, type Registry, type Run, type Source, type SourceVersion, type VersionView } from './types';

export const SYNTHETIC = import.meta.env.VITE_AIST_SYNTHETIC === 'true';
export const QA = import.meta.env.VITE_AIST_QA === 'true';
export function Workspace({ token, onLogout, personalId }: { token: string; personalId?:string|null; onLogout: (expired?: boolean) => void }) {
  const [client] = useState(() => new Client(token, undefined, () => onLogout(true)));
  const [projects, setProjects] = useState<Project[]>([]); const [loaded, setLoaded] = useState(false);
  const [projectId, setProjectId] = useState(() => readLocal('project', ''));
  const [visit,setVisit] = useState(0);
  const [name, setName] = useState(() => readLocal('project-draft', '')); const [error, setError] = useState('');
  const [busy, setBusy] = useState(false); const [refresh, setRefresh] = useState(0);
  const [ops] = useState(() => new Operations('project-op')); const lock = useRef(false);
  useEffect(() => {
    const leave = () => client.close();
    const restore = (e: PageTransitionEvent) => { if(e.persisted) location.reload(); };
    window.addEventListener('pagehide', leave); window.addEventListener('pageshow', restore);
    return () => {client.close(); window.removeEventListener('pagehide', leave); window.removeEventListener('pageshow', restore);};
  }, [client]);
  useEffect(() => {
    const c = client.child();
    void c.list<Project>('/v1/projects', 'ProjectPage').then(list => { setProjects(list); setLoaded(true); setError(''); if(projectId && projectId!==personalId && !list.some(p => p.id === projectId)) choose(''); }).catch(e => { if(!c.signal.aborted) setError(errorText(e)); });
    return () => c.close();
  }, [client, refresh]);
  function choose(id: string) { if(!confirmNavigation())return; const saved = readLocal<Selection|null>(`selection.${id}`,null); if(saved) writeLocal(`selection.${id}`,{...saved,screen:'home'}); setVisit(n=>n+1); setProjectId(id); writeLocal('project', id); }
  async function create(retry = false) {
    if(lock.current) return; lock.current = true; setBusy(true); setError('');
    try {
      if(!retry) ops.prepare('/v1/projects', {name:name.trim()}, 'Project', 'Создание проекта');
      const p = await ops.send<Project>(client); setName(''); writeLocal('project-draft', ''); setProjects(items => items.some(x => x.id === p.id) ? items : [...items,p]); setLoaded(true); choose(p.id);
    } catch(e) { if(!client.signal.aborted) { setError(errorText(e)); setRefresh(n => n+1); } }
    finally { lock.current = false; if(!client.signal.aborted) setBusy(false); }
  }
  const project = personalId && projectId===personalId?{id:personalId,name:'Чаты с главной'}:projects.find(p => p.id === projectId);
  return <>
    {SYNTHETIC && <div className="qa-banner"><strong>Локальная проверка CHAT-01</strong><span>Ответы провайдера синтетические. Факты проверяются по PDF, расчеты выполняет AIST.</span></div>}
    {QA && <div className="qa-banner"><strong>Тестовый режим QA</strong><span>Контрольные сценарии. Автоматическое извлечение и живая модель выключены.</span></div>}
    <div className="workspace">
      <nav className="projects" aria-label="Проекты"><div className="section-head"><h2>Проекты</h2><span className="counter">{projects.length}</span></div>
        {personalId && <button onClick={()=>choose(personalId)}>Чаты с главной</button>}
        <div className="project-list">{projects.map(p => <button className={`project-button ${p.id === projectId ? 'selected' : ''}`} aria-current={p.id === projectId ? 'page' : undefined} key={p.id} onClick={() => choose(p.id)}>{p.name}</button>)}</div>
        {loaded && !projects.length && <p className="small muted">Здесь будут ваши проекты.</p>}
        <details open={!projects.length}><summary>Новый проект</summary><form onSubmit={e => {e.preventDefault(); void create();}}><label>Название проекта<input maxLength={300} value={name} disabled={busy || !!ops.pending} onChange={e => {setName(e.target.value); writeLocal('project-draft', e.target.value);}} /></label><button disabled={!name.trim() || busy || !!ops.pending}>Создать проект</button></form></details>
        {ops.pending && <button className="secondary" disabled={busy} onClick={() => void create(true)}>Проверить создание проекта</button>}
        {error && <p role="alert" className="error">{error}</p>}
        <button className="text-button" onClick={() => setRefresh(n => n+1)}>Обновить проекты</button>
        <div className="sidebar-note"><span className="eyebrow">Рабочее место</span><p>Источники, разговор и сохранённые результаты в одном проекте.</p></div>
      </nav>
      {project ? <ProjectWorkspace key={project.id+visit} parent={client} project={project} /> : <section className="welcome"><span className="eyebrow">aist. / Рабочее место аналитика</span><h1>{loaded ? 'Начните с проекта' : 'Открываем рабочее место'}</h1><p>Откройте готовую записку или создайте проект и загрузите свой PDF. Источники и история сохраняются на сервере.</p></section>}
    </div>
  </>;
}

type NoteContext = { id: string; number: number; title: string; sourceIds: string[] };
type Selection = { screen?: 'home'|'conversation'; conversation: string; selected: string[]; known: string[]; run: string; result: string; context?: NoteContext | null; explicitSources?: boolean };
export function ProjectWorkspace({parent, project, opened, onActivity, onNoteReady, onSkills}: {parent: Client; project: Pick<Project,'id'|'name'>; opened?: {id:string;note:string|null;folder:string|null}; onActivity?:()=>void; onNoteReady?:()=>void; onSkills?:()=>void}) {
  const [client] = useState(() => parent.child());
  const [selection, setSelection] = useState<Selection>(() => {
    const empty:Selection={conversation:'',selected:[],known:[],run:'',result:''};
    const old=readLocal<Selection>(`selection.${project.id}`,empty);
    const saved=opened?readLocal<Selection>('chat-selection.'+opened.id,old.conversation===opened.id?old:empty):old;
    if(!opened)return saved;
    // An existing conversation opens on its messages. An explicitly opened note survives reload.
    return {...(saved.conversation===opened.id?saved:empty),screen:'conversation',conversation:opened.id,
      run:saved.run||readLocal('entry-run.'+opened.id,''),result:(saved.conversation===opened.id?saved.result:'')||opened.note||''};
  });
  const [pendingSources, setPendingSources] = useState<string[] | null>(null);
  const [sources, setSources] = useState<Source[]>([]); const [versions, setVersions] = useState<SourceVersion[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]); const [runs, setRuns] = useState<Run[]>([]);
  const latestNote = runs.find(r=>r.conversation_id===opened?.id && r.input.requested_result_type==='analytical_note' && r.result_version_id)?.result_version_id;
  const lastNote = useRef(opened?.note);
  useEffect(()=>{
    if(latestNote && latestNote!==lastNote.current){lastNote.current=latestNote;onNoteReady?.();}
  },[latestNote,onNoteReady]);
  const [refresh, setRefresh] = useState(0); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  const [title, setTitle] = useState(() => readLocal(`conversation-draft.${project.id}`, '')); const [busy, setBusy] = useState(false);
  const [registry, setRegistry] = useState<Registry | null>(null); const [ops] = useState(() => new Operations(`conversation-op.${project.id}`)); const lock = useRef(false);
  const path = `/v1/projects/${project.id}`;
  const selectionRef = useRef(selection); selectionRef.current = selection;
  function select(patch: Partial<Selection>) { if(patch.conversation && patch.conversation!==selection.conversation && !confirmNavigation())return; if(patch.context !== undefined || patch.conversation !== undefined) setPendingSources(null); const next = {...selectionRef.current, ...patch}; selectionRef.current=next; writeLocal(opened?'chat-selection.'+opened.id:`selection.${project.id}`, next); setSelection(next); }
  function selectSources(ids: string[]) {
    if(selection.context?.sourceIds.some(id => !ids.includes(id))) {
      setPendingSources(ids);
    } else select({selected:ids, explicitSources:true});
  }
  useEffect(() => () => client.close(), [client]);
  useEffect(() => {
    const c = client.child(); setError('');
    void (async () => {
      try {
        const [s, conversations, r, capabilities] = await Promise.all([opened?Promise.resolve([] as Source[]):c.list<Source>(path + '/sources','SourcePage'), opened?c.json<Conversation>(path+'/conversations/'+opened.id,'Conversation').then(x=>[x]):c.list<Conversation>(path + '/conversations','ConversationPage'), c.list<Run>(path + '/runs','RunPage',opened?{conversation_id:opened.id}:{}), c.json<Registry>('/v1/capabilities','Registry')]);
        setSources(s); setConversations(conversations); setRuns(r); setRegistry(capabilities);
        const ids = [...new Set([...s.map(x => x.latest_version_id), ...selectionRef.current.known, ...r.filter(x=>!opened||x.conversation_id===opened.id).flatMap(x => x.input.source_versions.map(p => p.source_version_id))])];
        const results = await Promise.allSettled(ids.map(id => c.json<SourceVersion>(path + '/source-versions/' + id+(opened?'?view=metadata':''), 'SourceVersion')));
        if (c.signal.aborted) return;
        setLoading(false); setVersions(results.flatMap(result => result.status === 'fulfilled' ? [result.value] : []));
        if(results.some(r => r.status === 'rejected')) setError('Часть версий источников недоступна. Обновите данные перед отправкой.');
      } catch(e) { if(!c.signal.aborted) {setError(errorText(e)); setLoading(false);} }
    })(); return () => c.close();
  }, [client, path, refresh]);
  async function create(retry = false) {
    if(lock.current) return; lock.current = true; setBusy(true); setError('');
    try {
      if(!retry) ops.prepare(path + '/conversations', {title:title.trim()}, 'Conversation', 'Создание разговора');
      const conv = await ops.send<Conversation>(client); select({screen:'conversation',conversation:conv.id,run:'',result:'',context:null}); setTitle(''); writeLocal(`conversation-draft.${project.id}`, ''); setRefresh(n => n+1);
    } catch(e) { if(!client.signal.aborted) setError(errorText(e)); }
    finally { lock.current = false; if(!client.signal.aborted) setBusy(false); }
  }
  const conv = conversations.find(c => c.id === selection.conversation);
  const conversationRuns = runs.filter(r => r.conversation_id === conv?.id);
  const home = selection.screen !== 'conversation';
  const ready = selection.selected.flatMap(id=>versions.filter(v=>v.id===id && v.processing_state==='ready'));
  const locked = conversationRuns.some(r=>isActive(r)||r.state==='recovery_required');
  function onUploaded(v: SourceVersion) {
    select({...(!selection.explicitSources && !selection.context && !locked && v.processing_state==='ready' ? {selected:[v.id]} : {}),
      known:[...new Set([...selection.known,...versions.map(s=>s.id),v.id])]});
    setRefresh(n=>n+1);
  }
  const sourceProps = {client,project:project.id,sources,versions,onUploaded,onRefresh:()=>setRefresh(n=>n+1)};
  function openConversation(conversation: string, run: string, result: string) {
    writeLocal(`result-open.${conversation}`,!!result);
    select({screen:'conversation',conversation,run,result,context:null});
  }
  return <div className="project-workspace">
    {!opened && <header className="project-heading"><div><span className="eyebrow">Проект</span><h1>{project.name}</h1></div><button className="secondary" onClick={() => setRefresh(n => n+1)}>Обновить данные</button></header>}
    {error && <div role="alert" className="error project-error">{error}</div>}
    {pendingSources && <div className="notice" role="alert"><p>Этот источник нужен для обсуждения выбранной записки. Исключить его и сбросить контекст записки?</p><div className="actions"><button onClick={()=>{select({selected:pendingSources,context:null,explicitSources:true});setPendingSources(null);}}>Исключить и сбросить контекст</button><button className="secondary" onClick={()=>setPendingSources(null)}>Сохранить выбор</button></div></div>}
    <div className={opened?"chat-conversation-column":"work-columns"}><main id="conversation" className="conversation panel">
      {loading && <p role="status">Загружаем проект…</p>}
      {!loading && home && <ProjectHome client={client} project={project.id} conversations={conversations} runs={runs} registry={registry}
        selected={ready} conversation={selection.conversation} onOpen={openConversation} onRefresh={sourceProps.onRefresh}>
        {!sources.length && <><h2>Загрузить PDF</h2><PdfUpload {...sourceProps}/></>}
      </ProjectHome>}
      {!home && !opened && <button className="text-button" onClick={()=>{if(confirmNavigation())select({screen:'home'});}}>К проекту</button>}
      {home && <details className="discussions"><summary>Обсуждения проекта · {conversations.length}</summary><div className="conversation-picker"><label>Разговор<select aria-label="Разговор" value="" onChange={e=>{const id=e.target.value; const latest=runs.filter(r=>r.conversation_id===id).sort((a,b)=>(b.created_at??'').localeCompare(a.created_at??''))[0]; openConversation(id,latest?.id??'','');}}><option value="">Выберите разговор</option>{conversations.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}</select></label>
        <details open={!conversations.length}><summary>Новый разговор</summary><form onSubmit={e => {e.preventDefault(); void create();}}><label>Название разговора<input maxLength={300} disabled={busy || !!ops.pending} value={title} onChange={e => {setTitle(e.target.value); writeLocal(`conversation-draft.${project.id}`, e.target.value);}} /></label><button disabled={busy || !!ops.pending || !title.trim()}>Создать разговор</button></form></details>
      </div></details>}
      {ops.pending && <button className="secondary" onClick={() => void create(true)} disabled={busy}>Проверить создание разговора</button>}
      {!home && conv && <ConversationView materialFolder={opened?.folder} onSkills={onSkills} modern={!!opened} key={conv.id} parent={client} project={project.id} conversation={conv} runs={conversationRuns} explicitSources={!!selection.explicitSources} selected={selection.selected.filter(id => versions.some(v => v.id === id && v.processing_state === 'ready'))} versions={versions} runId={selection.run} resultId={selection.result} context={selection.context ?? null} onSelect={select} refreshProject={() => setRefresh(n => n+1)} onSent={onActivity} registry={registry} />}
    </main>
    {!opened&&<Sources {...sourceProps} centralUpload={home && !sources.length} locked={locked} selected={selection.selected} onSelect={selectSources}/>}
    </div>
  </div>;
}

function ConversationView({ materialFolder, onSkills, modern, parent, project, conversation, runs, explicitSources, selected, versions, runId, resultId, context, onSelect, refreshProject, onSent, registry }: { materialFolder?:string|null; onSkills?:()=>void; modern:boolean; parent: Client; project: string; conversation: Conversation; runs: Run[]; explicitSources: boolean; selected: string[]; versions: SourceVersion[]; runId: string; resultId: string; context: NoteContext | null; onSelect: (patch: Partial<Selection>) => void; refreshProject: () => void; onSent?:()=>void; registry: Registry | null }) {
  const [client] = useState(() => parent.child()); const [messages, setMessages] = useState<Message[]>([]);
  const [proposals,setProposals]=useState<Proposal[]>([]);
  const [revision, setRevision] = useState<number | null>(null); const [text, setText] = useState(() => readLocal(`draft.${conversation.id}`, ''));
  const [error, setError] = useState(()=>readLocal('entry-error.'+conversation.id,'')); const [loadError, setLoadError] = useState(''); const [busy, setBusy] = useState(false); const [revisionTick, setRevisionTick] = useState(0);
  const [ops] = useState(() => new Operations(`submit.${conversation.id}`)); const lock = useRef(false);
  const path = `/v1/projects/${project}/conversations/${conversation.id}`;
  const skills=registry?.skills.filter(s=>s.status==='enabled' && s.bundle_sha256) ?? [];
  const models=registry?.model_profiles.filter(m=>m.status==='enabled' && m.configuration_sha256) ?? [];
  const [showResult,setShowResult]=useState(()=>readLocal(`result-open.${conversation.id}`,false));
  const [base,setBase]=useState<{id:string;revision:number;number:number;sourceIds:string[]}|null>(()=>readLocal(`base.${conversation.id}`,null));
  const [pendingDiscussion, setPendingDiscussion] = useState<NoteContext | null>(null);
  const chatWindowScroll=useRef(0);
  const [atExports,setAtExports]=useState(0);
  const [reviewRefresh,setReviewRefresh]=useState(0);
  function openResult(id:string, exports=false){if(showResult&&id!==resultId&&!confirmNavigation())return;setAtExports(n=>exports?n+1:0);if(runId)writeLocal(`auto-result.${runId}`,false);if(!showResult)chatWindowScroll.current=document.querySelector('.chat-content')?.scrollTop??window.scrollY;setShowResult(true);writeLocal(`result-open.${conversation.id}`,true);onSelect({result:id});}
  function closeResult(){setShowResult(false);requestAnimationFrame(()=>{const pane=document.querySelector('.chat-content');(pane??window).scrollTo(0,chatWindowScroll.current);});writeLocal(`result-open.${conversation.id}`,false);}
  function continueVersion(v:VersionView['version'],revision:number){setMode('note');writeLocal(`mode.${conversation.id}`,'note');const next={id:v.id,revision,number:v.revision,sourceIds:v.provenance.source_versions.map(p=>p.source_version_id)};setBase(next);writeLocal(`base.${conversation.id}`,next);setSkillId(v.provenance.source_versions.length>1?'skill_analytical-note-v3':'skill_analytical-note-v2');writeLocal(`skill.${conversation.id}`,v.provenance.source_versions.length>1?'skill_analytical-note-v3':'skill_analytical-note-v2');closeResult();}
  const [mode,setMode]=useState<'chat'|'note'>(()=>readLocal(`mode.${conversation.id}`,base?'note':'chat'));
  const [skillId,setSkillId]=useState(()=>readLocal(`skill.${conversation.id}`,base?'skill_analytical-note-v2':''));const [modelId,setModelId]=useState('');
  const [audience,setAudience]=useState(()=>readLocal(`audience.${conversation.id}`,''));
  const [sections,setSections]=useState(()=>readLocal(`sections.${conversation.id}`,''));
  const skill=mode==='chat' ? skills.find(s=>s.id==='skill_source-answer-v3') : skills.find(s=>s.id===skillId && s.result_type==='analytical_note') ?? skills.find(s=>s.id==='skill_analytical-note-v3');
  const model=models.find(m=>m.id===modelId) ?? models[0];
  const available=!!registry && (QA || !!(skill && model)) && (!registry.model_execution || registry.model_execution.state==='available');
  const activeRun=runs.find(r=>isActive(r)||r.state==='recovery_required');
  const feed=useRef<HTMLDivElement>(null);
  const questionField = useRef<HTMLTextAreaElement>(null);
  const followBottom = useRef(readLocal(`chat-follow.${conversation.id}`,true));
  const restoredScroll = useRef(false);
  const [unread,setUnread] = useState(()=>readLocal(`new-answer.${conversation.id}`,''));
  function jumpToAnswer(id:string) {
    const article = feed.current?.querySelector<HTMLElement>(`[data-message-id="${id}"]`);
    if (!article) return;
    article.focus({preventScroll:true});
    article.scrollIntoView({block:'start'});
    setUnread(''); writeLocal(`new-answer.${conversation.id}`,'');
  }
  useEffect(()=>{
    const node = feed.current;
    if (!node || !messages.length) return;
    if (!restoredScroll.current && !showResult) {
      node.scrollTop=readLocal(`chat-scroll.${conversation.id}`,node.scrollHeight);
      restoredScroll.current=true;
    }
    const answer=messages.find(m=>m.role==='assistant' && m.run_id && readLocal(`follow-answer.${m.run_id}`,false));
    if (!answer) return;
    writeLocal(`follow-answer.${answer.run_id}`,false);
    if (!showResult && followBottom.current && !document.querySelector('dialog[open]')) jumpToAnswer(answer.id);
    else {setUnread(answer.id);writeLocal(`new-answer.${conversation.id}`,answer.id);}
  },[messages,showResult,conversation.id]);
  function rememberScroll(){
    const node=feed.current;
    if(!node || showResult || !restoredScroll.current)return;
    followBottom.current=node.scrollHeight-node.clientHeight-node.scrollTop<80;
    writeLocal(`chat-scroll.${conversation.id}`,node.scrollTop);writeLocal(`chat-follow.${conversation.id}`,followBottom.current);
  }
  function chooseMode(next:'chat'|'note'){setMode(next);writeLocal(`mode.${conversation.id}`,next);if(next==='chat'){setBase(null);writeLocal(`base.${conversation.id}`,null);}}
  function discussVersion(v: VersionView['version']) {
    const sourceIds = v.provenance.source_versions.map(p => p.source_version_id);
    const next = {id:v.id, number:v.revision, title:v.payload.content.title ?? 'Записка', sourceIds};
    if(modern){
      setDiscussionFiles(conversation.id,versions.filter(f=>sourceIds.includes(f.id)));
      onSelect({context:next});closeResult();
      requestAnimationFrame(()=>document.querySelector<HTMLTextAreaElement>('[aria-label="Сообщение AIST"]')?.focus());
      return;
    }
    const missing = sourceIds.filter(id => !selected.includes(id));
    if(missing.length && (explicitSources || selected.length || missing.some(id=>!versions.some(v=>v.id===id && v.processing_state==='ready')))) {
      setPendingDiscussion(next);
      requestAnimationFrame(()=>document.getElementById('confirm-discussion')?.focus());
      return;
    }
    startDiscussion(next);
  }
  function openedVersion(v:VersionView['version']) {
    if(!modern||context?.id===v.id)return;
    const sourceIds=v.provenance.source_versions.map(p=>p.source_version_id);
    setDiscussionFiles(conversation.id,versions.filter(f=>sourceIds.includes(f.id)));
    onSelect({context:{id:v.id,number:v.revision,title:v.payload.content.title??'Записка',sourceIds}});
  }
  function startDiscussion(next: NoteContext) {
    chooseMode('chat');
    onSelect({context:next, selected:[...new Set([...selected, ...next.sourceIds])]});
    setPendingDiscussion(null);
    closeResult();
    requestAnimationFrame(()=>questionField.current?.focus());
  }

  useEffect(() => () => client.close(), [client]);
  useEffect(() => {
    const c = client.child();
    void (async () => {
      try {
        // Read the CAS revision before history; concurrent writes produce a safe conflict.
        const conv = await c.json<Conversation>(path, 'Conversation');
        const list = await c.list<Message>(path + '/messages', 'MessagePage');
        if(modern)setProposals(await c.list<Proposal>(path+'/edit-proposals','EditProposalPage'));
        setMessages(list); setRevision(conv.revision); setLoadError('');
      } catch(e) { if(!c.signal.aborted) setLoadError(errorText(e)); }
    })(); return () => c.close();
  }, [client, path, revisionTick, conversation.revision]);
  function changed() {setRevisionTick(n => n+1); refreshProject();}
  function proposalCard(p:Proposal){
    return <React.Fragment key={p.id}>{!p.run_id&&<article className="message user"><strong>Вы</strong><p>{p.command}</p></article>}
      <ProposalCard messageId={p.run_id?messages.find(m=>m.run_id===p.run_id&&m.sequence===p.anchor)?.id:undefined} client={client} path={path} value={p} stale={proposals.some(other=>other.version_id===p.version_id&&other.state==='applied')}
        onOpen={openResult} onDecision={next=>{setProposals(all=>all.map(x=>x.id===next.id?next:x));
          if(next.applied_version_id) {onSelect({result:next.applied_version_id,context:null});}
          changed();}}/></React.Fragment>;
  }
  async function submit(retry = false) {
    if(lock.current || (!retry && activeRun)) return; lock.current = true; setBusy(true); setError('');
    try {
      if(!retry) ops.prepare(path + '/messages', { expected_revision:revision, text:text.trim(), source_version_ids:base?.sourceIds??selected, skill_version_id:QA ? 'skill_qa-d07-v1' : skill?.id, model_profile_version_id:QA ? 'model_qa-d07-v1' : model?.id, requested_result_type:QA ? 'evidence_answer' : skill?.result_type, parameters:skill?.result_type==='analytical_note'?{audience:audience.trim()||null,sections:sections.split(',').map(s=>s.trim()).filter(Boolean)}:{audience:null,sections:[]}, base_version_id:base?.id??null, expected_artifact_revision:base?.revision??0, requested_export:null, ...(mode==='chat' && context ? {context_version_id:context.id} : {}) }, 'Submission', 'Отправка задачи');
      const response = await ops.send<{run:Run}>(client); writeLocal(response.run.input.requested_result_type==='analytical_note' ? `auto-result.${response.run.id}` : `follow-answer.${response.run.id}`,true); onSelect({run:response.run.id, result:''}); setText(''); writeLocal(`draft.${conversation.id}`, ''); changed();onSent?.();
    } catch(e) { if(!client.signal.aborted) {setError(errorText(e)); changed();} }
    finally {lock.current = false; if(!client.signal.aborted) setBusy(false);}
  }
  return <>
    {!modern && !(resultId && showResult) && <div className="conversation-title"><h2>{conversation.title}</h2><span className="small muted">Сообщения сохраняются</span></div>}
    {pendingDiscussion && <div className="notice" role="alert"><p>Для обсуждения «{pendingDiscussion.title}» v{pendingDiscussion.number} нужно выбрать ее источники.</p><div className="actions"><button id="confirm-discussion" onClick={()=>startDiscussion(pendingDiscussion)}>Добавить источники и обсудить</button><button className="secondary" onClick={()=>setPendingDiscussion(null)}>Оставить прежний выбор</button></div></div>}
    {modern&&resultId&&<nav className="note-panes" aria-label="Панели чата"><button aria-pressed={!showResult} onClick={()=>{if(confirmNavigation())closeResult();}}>Чат</button>
      <button aria-pressed={showResult} onClick={()=>openResult(resultId)}>Записка</button></nav>}
    <div className={modern ? 'chat-thread-layout'+(resultId&&showResult?' reading-panes':'') : ''}>
    {resultId && showResult && <ResultPanel modern={modern} atExports={atExports} key={resultId} client={client} project={project} id={resultId} onClose={closeResult} onSelect={openResult} onVersionOpened={openedVersion} onAccepted={()=>setReviewRefresh(n=>n+1)} onDiscuss={available && !busy && !ops.pending && !activeRun && skills.some(s=>s.id==='skill_source-answer-v3') ? discussVersion : undefined} onContinue={model?.id==='model_novita-mimo-v1' && skills.some(s=>s.id==='skill_analytical-note-v2') ? continueVersion : undefined} />}
    <div className="discussion-pane" hidden={!modern&&!!resultId&&showResult}>
      {resultId && (!modern || !showResult&&!messages.some(m=>m.artifact_version_id)) && <button className="secondary" onClick={()=>openResult(resultId)}>Открыть выбранный результат</button>}
      <div className="history-wrap">
        <div className="messages" aria-label="История сообщений" ref={feed} onScroll={rememberScroll}>
          {proposals.filter(p=>p.anchor===0).map(proposalCard)}
          {messages.length ? messages.map(m=>{
            const task=runs.find(r=>r.id===m.run_id);
            const contextId=task?.input.context_version_id;
            const anchored=proposals.filter(p=>p.anchor===m.sequence);
            const proposalReply=m.role==='assistant'&&anchored.some(p=>p.run_id&&p.run_id===m.run_id);
            return <React.Fragment key={m.id}>{!proposalReply&&<article className={`message ${m.role}`} key={m.id} data-message-id={m.id} aria-label={m.role==='assistant'?'Ответ ассистента':undefined} tabIndex={-1}>
              <div className="message-meta">{m.role==='user'?<strong>Вы</strong>:!modern&&<strong>aist.</strong>}
                <time>{new Date(m.created_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}</time>
              </div>
              {modern&&m.role==='user'&&m.id===task?.message_id&&task.input.source_versions.map(pin=>{
                const v=versions.find(f=>f.id===pin.source_version_id);
                const shared=task.input.project_context?.pdfs.find(p=>p.local_version_id===pin.source_version_id);
                if(shared)return <small key={pin.source_version_id} className="message-pdf">{shared.original_filename} · PDF · v{shared.revision}</small>;
                return v&&<small className="message-pdf" title={v.original_filename} key={v.id}>{v.original_filename} · {v.media_type==='application/json'?'JSON':'PDF'}</small>;
              })}
              {modern&&m.role==='user'&&task?.message_id===m.id&&task.input.project_context?.chats.map(c=>
                <small className="message-pdf" key={c.id}>Переписка: {c.title} · по сообщ. {c.through_sequence}</small>)}
              {m.artifact_version_id
                ? <ChatAnswer contextId={contextId??undefined} modern={modern} refreshVersion={`${resultId}:${reviewRefresh}`} client={client} project={project} id={m.artifact_version_id} message={m.text} versions={versions} onOpen={openResult}/>
                : <p>{m.text}</p>}
              <div className="actions">
                {m.run_id && m.role==='user' && <button className="text-button" onClick={()=>onSelect({run:m.run_id!})}>{task?states[task.state]:'Ход задачи'}</button>}
                {contextId && <button className="text-button" onClick={()=>openResult(contextId)}>Записка из контекста вопроса</button>}
              </div>
            </article>}{anchored.map(proposalCard)}</React.Fragment>;
          }) : <div className="chat-welcome"><h3>Обсудим вашу задачу</h3><p>Помогу разобраться в материалах, проверить показатели и расчеты. Можно начать без PDF.</p><p className="small muted">Для подготовки документа выберите «Создать записку».</p></div>}
        </div>
        {unread && <button className="secondary" onClick={()=>jumpToAnswer(unread)}>К новому ответу</button>}
      </div>
      {(activeRun?.id||runId) && <RunPanel key={activeRun?.id||runId} parent={client} project={project} versions={versions} id={activeRun?.id||runId} onChange={changed} openResult={openResult} />}
      {modern&&!ops.pending?<>{(loadError||error)&&<p role="alert">{loadError||error}</p>}
        {!activeRun&&registry?.model_execution?.state!=='available'&&registry?.model_execution?.message&&<p className="notice">{registry.model_execution.message}</p>}
        {activeRun?.state!=='waiting_for_user'&&<ChatStart materialFolder={materialFolder} onSkills={onSkills} key={context?.id??conversation.id} client={client} folder={project} chat={{
          editVersion:(showResult?resultId:null)||context?.id||null,onProposal:p=>{setProposals(all=>[...all.filter(x=>x.id!==p.id),p]);changed();closeResult();requestAnimationFrame(()=>feed.current?.scrollTo(0,feed.current.scrollHeight));},
          id:conversation.id,revision,active:!!activeRun,context,pins:versions.filter(v=>context?.sourceIds.includes(v.id)),
          clearContext:()=>onSelect({context:null}),onConflict:changed,onRun:run=>{
            writeLocal('entry-error.'+conversation.id,'');setError('');onSelect({run:run.id,result:''});changed();onSent?.();
          }
        }}/>}</>:<form className="composer" hidden={activeRun?.state==='waiting_for_user'} onSubmit={e => {e.preventDefault(); void submit();}}>
        {!available && <div className="notice"><p>{registry?.model_execution?.message ?? 'Генерация выключена. Готовые записки, правка и экспорт доступны.'}</p></div>}
        <div className="chat-mode" aria-label="Режим работы"><button type="button" className={mode==='chat'?'selected':'secondary'} aria-pressed={mode==='chat'} disabled={busy||!!ops.pending||!!activeRun} onClick={()=>chooseMode('chat')}>Разговор</button><button type="button" className={mode==='note'?'selected':'secondary'} aria-pressed={mode==='note'} disabled={busy||!!ops.pending||!!activeRun} onClick={()=>chooseMode('note')}>Создать записку</button></div>
        {!QA && available && <details className="technical-options"><summary>Дополнительные настройки</summary><div className="execution-options">{mode==='note'&&<label>Версия навыка<select aria-label="Навык" value={skill?.id} onChange={e=>{setSkillId(e.target.value);writeLocal(`skill.${conversation.id}`,e.target.value);}} disabled={busy||!!ops.pending||!!activeRun}>{skills.filter(s=>s.result_type==='analytical_note').map(s=><option key={s.id} value={s.id}>Аналитическая записка · v{s.version}</option>)}</select></label>}<label>Модель<select aria-label="Модель" value={model?.id} onChange={e=>setModelId(e.target.value)} disabled={busy||!!ops.pending||!!activeRun}>{models.map(m=><option key={m.id} value={m.id}>{m.model}</option>)}</select></label></div>
        {skill?.result_type==='analytical_note' && <div className="execution-options"><label>Аудитория<input aria-label="Аудитория" value={audience} maxLength={200} disabled={busy || !!ops.pending} placeholder="Например, руководитель" onChange={e=>{setAudience(e.target.value);writeLocal(`audience.${conversation.id}`,e.target.value);}}/></label><label>Разделы через запятую<input aria-label="Разделы через запятую" value={sections} maxLength={2000} disabled={busy || !!ops.pending} placeholder="Главное, Показатели, Ограничения" onChange={e=>{setSections(e.target.value);writeLocal(`sections.${conversation.id}`,e.target.value);}}/></label></div>}</details>}
        {base&&<div className="notice">Продолжение от сохраненной v{base.number}. Ожидаемая голова: v{base.revision}. Несохраненная форма не отправляется.<button type="button" className="text-button" onClick={()=>{setBase(null);writeLocal(`base.${conversation.id}`,null);}}>Без выбранной версии</button></div>}
        {mode==='chat' && context && <div className="notice note-context"><strong>Обсуждаем: {context.title} · v{context.number}</strong><p>Ответ учитывает эту сохраненную версию. Записка останется без изменений.</p><div className="actions"><button type="button" className="text-button" onClick={()=>openResult(context.id)}>Открыть версию из контекста</button><button type="button" className="text-button" disabled={busy||!!ops.pending||!!activeRun} onClick={()=>onSelect({context:null})}>Без контекста записки</button></div></div>}
        <label>{mode==='chat'?'Сообщение AIST':'Задача для записки'}<textarea ref={questionField} aria-label="Сообщение AIST" placeholder={mode==='chat'?'Задайте вопрос или опишите задачу':'Какую записку нужно подготовить?'} value={text} maxLength={20000} disabled={busy || !!ops.pending} onChange={e => {setText(e.target.value); writeLocal(`draft.${conversation.id}`,e.target.value);}} /></label>
        <div className="selected-sources">{selected.length ? selected.map(id => {const v=versions.find(v=>v.id===id)!; return <span className="source-chip" key={id}>{v.original_filename} · v{v.revision}</span>;}) : <span className="small muted">Можно обсудить задачу без PDF. Для фактов выберите источник.</span>}</div>
        {!QA && skill && (base?.sourceIds.length??selected.length)>skill.max_sources_execution && <p className="notice">Этот навык принимает до {skill.max_sources_execution} PDF. Для нескольких документов выберите записку v3.</p>}
        {(error || loadError) && <p className="error" role="alert">{error || loadError}</p>}
        {ops.pending ? <div className="notice" role="status">{busy ? 'Отправляем задачу…' : <><p><strong>Отправка не подтверждена.</strong> Сохранены текст и версии источников.</p><button type="button" onClick={() => void submit(true)}>Проверить исход отправки</button></>}</div> : <div className="composer-actions"><span className="small muted">{QA ? 'Ответит тестовый симулятор' : 'Черновик сохраняется в этой вкладке'}</span><button disabled={busy || !available || revision === null || !text.trim() || !!activeRun || (mode==='chat' && !!context?.sourceIds.some(id=>!selected.includes(id))) || (mode==='note' && !(base?.sourceIds.length??selected.length)) || (!QA && (base?.sourceIds.length??selected.length)>(skill?.max_sources_execution??1))}>{busy ? 'Отправляем…' : activeRun ? 'Дождитесь ответа' : mode==='note' ? 'Подготовить записку' : 'Отправить'}</button></div>}
      </form>}
    </div></div>
  </>;
}
