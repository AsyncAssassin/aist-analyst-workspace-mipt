import React, { useEffect, useRef, useState } from 'react';
import { ApiError, Client, errorText } from './client';
import { Operations, readLocal, writeLocal } from './local';
import { isActive, states, type Conversation, type Registry, type Run, type SourceVersion, type VersionView } from './types';

type Note = {artifact: string; title: string; revision: number; savedAt:string; conversation: string};
type Intent = {conversation: string; sourceIds: string[]; skill: string; model: string; text: string};
type Props = {
  client: Client; project: string; conversations: Conversation[]; runs: Run[]; registry: Registry | null;
  selected: SourceVersion[]; conversation: string; children: React.ReactNode;
  onOpen: (conversation: string, run: string, version: string) => void; onRefresh: () => void;
};

export function ProjectHome({client:parent, project, conversations, runs, registry, selected, conversation, children, onOpen, onRefresh}: Props) {
  const [client] = useState(()=>parent.child());
  useEffect(()=>()=>client.close(),[client]);
  const path = `/v1/projects/${project}`;
  const [notes,setNotes] = useState<Note[]>([]);
  const [error,setError] = useState('');
  const [reading,setReading] = useState(true);
  const [prompt,setPrompt] = useState('');
  const [busy,setBusy] = useState(false);
  const lock = useRef(false);
  const scope = 'prepare-note.'+project;
  const [creation] = useState(() => new Operations(scope+'.conversation'));
  const [submission] = useState(() => new Operations(scope+'.message'));
  const pending = readLocal<Intent|null>(scope,null);
  useEffect(() => {
    const c = client.child();
    async function load() {
      const seen = new Map<string, string>();
      for (const run of runs) {
        if (!run.result_version_id || run.input.requested_result_type !== 'analytical_note') continue;
        const saved = await c.json<VersionView>(path+'/artifact-versions/'+run.result_version_id,'VersionView');
        if (saved.version.payload.result_type === 'analytical_note') seen.set(saved.version.artifact_id,run.conversation_id);
      }
      const entries = await Promise.all(Array.from(seen,async ([artifact, conversation]) => {
        const head = await c.json<{latest_version_id:string}>(path+'/artifacts/'+artifact,'Artifact');
        const {version} = await c.json<VersionView>(path+'/artifact-versions/'+head.latest_version_id,'VersionView');
        return {artifact, conversation, title:version.payload.content.title ?? 'Аналитическая записка', revision:version.revision, savedAt:version.created_at};
      }));
      setNotes(entries); setReading(false);
    }
    void load().catch(e => {if (!c.signal.aborted) {setError(errorText(e)); setReading(false);}});
    return () => c.close();
  },[client,path,runs]);

  async function open(note: Note) {
    try {
      const head = await client.json<{latest_version_id:string}>(path+'/artifacts/'+note.artifact,'Artifact');
      onOpen(note.conversation,'',head.latest_version_id);
    } catch(e) { if (!client.signal.aborted) setError(errorText(e)); }
  }

  const skill = registry?.skills.filter(s=>s.status==='enabled' && s.bundle_sha256 && s.result_type==='analytical_note')
    .sort((a,b)=>b.version.localeCompare(a.version,undefined,{numeric:true}))[0];
  const model = registry?.model_profiles.find(m=>m.status==='enabled' && m.configuration_sha256);
  const execution = registry?.model_execution;
  const available = skill && model && (!execution || execution.state==='available');
  const current = runs.find(r=>isActive(r) || r.state==='recovery_required');
  const last = current ?? [...runs].sort((a,b)=>(b.created_at??'').localeCompare(a.created_at??''))[0];
  const tooMany = !!skill && selected.length>skill.max_sources_execution;
  async function prepare() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try {
      let intent = readLocal<Intent|null>(scope,null);
      if (!intent) {
        if (!available || !selected.length || tooMany || current) return;
        const reusable = conversations.find(c => c.id===conversation) ?? conversations.find(c => c.revision===0 || c.title==='Подготовка записки');
        if (reusable && new Operations('submit.'+reusable.id).pending) {
          setError('Сначала проверьте исход отправки в обсуждении.');
          return;
        }
        intent = {conversation:reusable?.id ?? '', sourceIds:selected.map(v=>v.id), skill:skill.id, model:model.id,
          text:prompt.trim() || 'Подготовь аналитическую записку по выбранным источникам.'};
        writeLocal(scope,intent);
      }
      if (!intent.conversation) {
        creation.prepare(path+'/conversations',{title:'Подготовка записки'},'Conversation','Создание обсуждения');
        const conv = await creation.send<Conversation>(client, c=>writeLocal(scope,{...intent,conversation:c.id}));
        intent = {...intent,conversation:conv.id};
      }
      const target = path+'/conversations/'+intent.conversation;
      if (!submission.pending) {
        const conv = await client.json<Conversation>(target,'Conversation');
        submission.prepare(target+'/messages',{expected_revision:conv.revision,text:intent.text,source_version_ids:intent.sourceIds,
          skill_version_id:intent.skill,model_profile_version_id:intent.model,requested_result_type:'analytical_note',
          parameters:{audience:null,sections:[]},base_version_id:null,expected_artifact_revision:0,requested_export:null},
          'Submission','Подготовка записки');
      }
      const chosen = intent.conversation;
      await submission.send<{run:Run}>(client, ({run}) => {
        writeLocal(`auto-result.${run.id}`,true);
        onOpen(chosen,run.id,'');
        writeLocal(scope,null);
      });
      onRefresh();
    } catch(e) {
      if (!client.signal.aborted) {
        if (!creation.pending && !submission.pending && e instanceof ApiError && e.status>=400 && e.status<500) writeLocal(scope,null);
        setError(errorText(e)); onRefresh();
      }
    } finally { lock.current=false; if (!client.signal.aborted) setBusy(false); }
  }
  const start = <div className="prepare-note">
    {selected.length ? <p>{selected.map(v=>v.original_filename+' · версия '+v.revision).join(', ')} · Готов</p> :
      <p>Выберите готовый документ в источниках проекта.</p>}
    <details><summary>Уточнить задачу</summary><label>Что включить в записку<textarea value={prompt} maxLength={20000} disabled={busy||!!pending} onChange={e=>setPrompt(e.target.value)}/></label></details>
    {tooMany && <p className="notice">Выберите до {skill.max_sources_execution} источников.</p>}
    {!available && execution && <p className="notice">{execution.message}</p>}
    <button disabled={busy||!!pending||!available||!selected.length||tooMany||!!current} onClick={()=>void prepare()}>Подготовить записку</button>
  </div>;
  return <section className="project-home">
    {reading && <p role="status">Читаем сохраненные записки…</p>}
    {notes.map(note=><div className="note-entry" key={note.artifact}>
      <h2>{note.title}</h2><p className="small muted">Версия {note.revision} · {new Date(note.savedAt).toLocaleString('ru-RU')} · {conversations.find(c=>c.id===note.conversation)?.title}</p>
      <button onClick={()=>void open(note)}>Открыть актуальную записку</button>
    </div>)}
    {pending && <div className="notice" role="status"><strong>{busy ? 'Подготавливаем задачу…' : creation.pending ? 'Создание обсуждения не подтверждено.' : submission.pending ? 'Отправка задачи не подтверждена.' : 'Подготовка записки прервана.'}</strong>
      {!busy && <button onClick={()=>void prepare()}>{creation.pending ? 'Проверить создание обсуждения' : submission.pending ? 'Проверить исход отправки' : 'Продолжить подготовку'}</button>}</div>}
    {last && (current || !['completed','cancelled'].includes(last.state)) && <p className="notice">Последняя задача · {states[last.state]} <button className="secondary" onClick={()=>onOpen(last.conversation_id,last.id,'')}>Открыть ход задачи</button></p>}
    {children || (notes.length ? <details><summary>Новая записка по источникам</summary>{start}</details> : !reading && <><h2>Новая записка</h2>{start}</>)}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
