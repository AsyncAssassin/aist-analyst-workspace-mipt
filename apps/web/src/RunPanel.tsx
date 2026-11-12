import {WorkspaceIcon} from './WorkspaceIcon';
import { AsoNote } from './AsoNote';
import { AistBrand } from './AistBrand';
import { CandidateBasis } from './CandidateBasis';
import { Exports } from './Exports';
import { NoteEditor } from './NoteEditor';
import { confirmNavigation } from './local';
import React, { useEffect, useRef, useState } from 'react';
import { Client, errorText, EventHistory, followEvents } from './client';
import { Operations, readLocal, writeLocal } from './local';
import { factAnchor, isActive, states, type Fact, type ModelCall, type Run, type RunEvent, type SourceVersion, type Usage, type VersionView, type Claim, type VisualObservation } from './types';
import { currentAction, deadlineIn, deadlinePassed, eventLabel } from './progress';
import { versionState } from './review';
import { AllFindings } from './ReviewFindings';
import { SourceReader } from './Sources';
import {useAnchoredMenu} from './useAnchoredMenu';

export function RunPanel({ parent, project, id, versions, onChange, openResult }: { parent: Client; project: string; id: string; versions:SourceVersion[]; onChange: () => void; openResult: (id: string) => void }) {
  const [client] = useState(() => parent.child());
  const [run, setRun] = useState<Run | null>(null); const [events, setEvents] = useState<RunEvent[]>([]);
  const [usage, setUsage] = useState<Usage[]>([]); const [calls, setCalls] = useState<ModelCall[]>([]); const [error, setError] = useState(''); const [loadError, setLoadError] = useState(''); const [online, setOnline] = useState(false);
  const [text, setText] = useState(() => readLocal(`input.${id}`, '')); const [busy, setBusy] = useState(false);
  const [ops] = useState(() => new Operations(`run-op.${id}`)); const [, redraw] = useState(0); const lock = useRef(false);
  const callback = useRef(onChange); callback.current = onChange;
  const resultCallback = useRef(openResult); resultCallback.current = openResult;
  const path = `/v1/projects/${project}/runs/${id}`;
  async function refresh() {
    try {
      const [r, u, m] = await Promise.all([client.json<Run>(path, 'Run'), client.list<Usage>(path + '/usage', 'UsagePage'), client.list<ModelCall>(path + '/model-calls', 'CallPage')]);
      if (!client.signal.aborted) { setRun(r); setUsage(u); setCalls(m); setLoadError(''); if(r.state==='completed' && r.result_version_id && readLocal(`auto-result.${id}`,false)){writeLocal(`auto-result.${id}`,false);resultCallback.current(r.result_version_id);} }
    } catch(e) { if (!client.signal.aborted) setLoadError(errorText(e)); }
  }
  useEffect(() => {
    let refreshing = false; let again = false;
    const update = async () => {
      if (refreshing) { again = true; return; } refreshing = true;
      do { again = false; await refresh(); if (!client.signal.aborted) callback.current(); } while (again && !client.signal.aborted);
      refreshing = false;
    };
    const history = new EventHistory(id);
    void update();
    void followEvents(client, path, history, () => { setEvents([...history.events]); void update(); }, value => { setOnline(value); if(value) void update(); });
    return () => client.close();
  }, [client, id, path]);
  // The deadline ends the waiting in the UI even before the server records the outcome.
  useEffect(() => {
    if (!run || !isActive(run)) return;
    const left = deadlineIn(run);
    if (left === null || left < 0) return;
    const timer = setInterval(() => redraw(n => n + 1), 1000);
    return () => clearInterval(timer);
  }, [run]);
  async function act(kind?: 'cancel' | 'input') {
    if (lock.current || !run) return;
    lock.current = true; setBusy(true); setError('');
    try {
      if (kind) ops.prepare(path + (kind === 'cancel' ? '/cancel' : '/input'), kind === 'cancel' ? { expected_revision: run.revision } : { expected_revision: run.revision, waiting_input_id: run.waiting_input?.id, text: text.trim() }, kind === 'cancel' ? 'Run' : 'Submission', kind === 'cancel' ? 'Отмена задачи' : 'Ответ на уточнение');
      const response = await ops.send<Run|{run:Run}>(client); setRun('run' in response ? response.run : response); setText(''); writeLocal(`input.${id}`, ''); callback.current();
    } catch(e) { if (!client.signal.aborted) { setError(errorText(e)); await refresh(); callback.current(); } }
    finally { lock.current = false; if (!client.signal.aborted) { setBusy(false); redraw(n => n+1); } }
  }
  const unknown = run?.state==='failed' && run.error?.code==='outcome_unknown';
  const readyPdfs = versions.filter(v=>v.processing_state==='ready' && v.media_type!=='application/json' && run?.input.source_versions.some(pin=>pin.source_version_id===v.id && pin.sha256===v.sha256));
  const currentUsage = usage.filter(u => !usage.some(other => other.supersedes_id === u.id));
  return <details open={!!run && (!!ops.pending||!['completed','cancelled'].includes(run.state))} className={`run-card ${run && ['completed','cancelled'].includes(run.state) ? 'settled' : ''}`}  aria-label="Ход задачи" data-run-id={id}>
    <summary className="section-head"><strong>Ход задачи</strong><span role="status" className={`status ${run?.state ?? ''}`}>{run ? states[run.state] : 'Загрузка…'}</span></summary>
    {!online && run && isActive(run) && <p className="notice small">Связь с задачей восстанавливается. Отключение браузера не отменяет её.</p>}
    {run?.state === 'recovery_required' && <p className="notice">Исход выполнения требует проверки. Новая генерация автоматически не запускается.</p>}
    {unknown ? <div className="error" role="alert"><strong>{events.some(event=>event.type==='result_saved')?'Полная записка не получена':'Результат не получен'}</strong><p>Получение ответа модели не подтверждено. Повторного обращения к модели нет. Если сумма зарезервирована, резерв сохраняется.</p>
      {readyPdfs.map(v=><p key={v.id}>{v.original_filename} · PDF готов</p>)}
      <label>Номер задачи для поддержки<input readOnly value={id} onFocus={e=>e.target.select()} /></label></div> : run?.error && <p className="error">{run.error.message}</p>}
    {run?.created_at && <p className="small muted">Прошло: {Math.max(0,Math.floor(((isActive(run) ? Date.now() : Date.parse(run.updated_at ?? run.created_at))-Date.parse(run.created_at))/1000))} с</p>}
    {run && currentAction(run, calls, events) && <p className="small" data-current-action>{currentAction(run, calls, events)}</p>}
    {run && deadlinePassed(run) && <p className="notice" data-deadline-passed>Срок задачи истек: новые обращения не отправляются, итог записывается сервером.</p>}
    {(error || loadError) && <p className="error" role="alert">{error || loadError}</p>}
    {run?.waiting_input && <form onSubmit={e => {e.preventDefault(); void act('input');}} className="clarification start-box"><p>{run.waiting_input.prompt}</p><label>Ответ на уточнение<textarea aria-label="Ответ на уточнение" onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();if(!busy&&!ops.pending&&text.trim())void act('input');}}} value={text} maxLength={20000} disabled={busy || !!ops.pending} onChange={e => { setText(e.target.value); writeLocal(`input.${id}`, e.target.value); }} /></label><button disabled={busy || !!ops.pending || !text.trim()}>Отправить уточнение</button></form>}
    {ops.pending && <div className="notice"><p>Не подтверждено: {ops.pending.label}. Повтор сохранит исходный ключ и запрос.</p><button onClick={() => void act()} disabled={busy}>Проверить исход действия</button></div>}
    <div className="actions">{run && (isActive(run)||run.state==='recovery_required') && <button className="secondary" disabled={busy || !!ops.pending} onClick={() => void act('cancel')}>Отменить задачу</button>}{run?.result_version_id && <button className="text-button" onClick={() => openResult(run.result_version_id!)}>Версия результата</button>}{(loadError||!online&&run&&isActive(run))&&<button className="text-button" onClick={() => {setError(''); void refresh();}}>Обновить состояние</button>}</div>
    {run && isActive(run) && <p className="small muted">Источники задачи зафиксированы при отправке. Новые загрузки их не меняют.</p>}
    {run?.state === 'completed' && run.result_version_id && <p className="small muted">Задача завершена: результат сохранен как версия. Завершение задачи не подтверждает ее утверждения; замечания проверок и принятие показаны в самой версии.</p>}
    <details><summary>Дополнительные сведения</summary>
      <p className="mono small">Задача: {id}<br/>Ревизия: {run?.revision} · Событий: {events.length} · Последний seq: {events.at(-1)?.seq ?? 0}</p>
      <ul className="small">{run?.input.source_versions.map(pin => <li key={pin.source_version_id} className="mono">{pin.source_version_id}<br/>SHA: {pin.sha256}</li>)}</ul>
      <h4>Учёт использования</h4><p className="small">Оценка и фактическое списание учитываются отдельно. Тестовые величины QA не являются расходом на провайдера.</p>
      {!currentUsage.length && <p className="small muted">Токены, оценка и фактическое списание пока неизвестны.</p>}
      {currentUsage.map(u => <div key={u.id} className="usage"><p>Оценка: {u.estimated_usd.value === null ? 'неизвестна' : `$${u.estimated_usd.value}`}<br/>Фактическое списание: {u.billed_usd.value === null ? 'неизвестно' : `$${u.billed_usd.value}`}</p><dl>{Object.entries(u.tokens).map(([name, value]) => <React.Fragment key={name}><dt>{name}</dt><dd>{value.value ?? 'неизвестно'}</dd></React.Fragment>)}</dl></div>)}
      <h4>Журнал событий</h4><ol className="events">{events.map(e => <li key={e.seq} data-event-seq={e.seq}><span className="mono">{e.seq}</span> {eventLabel(e)}</li>)}</ol>
    </details>
  </details>;
}

export function ResultPanel({ client, project, id, onClose, onSelect, onContinue, onDiscuss, onVersionOpened, onAccepted, atExports, modern }: { client: Client; project: string; id: string; onClose: () => void; onSelect:(id:string, exports?:boolean)=>void; onAccepted:()=>void; atExports:number; modern?:boolean; onContinue?: (v:VersionView['version'],revision:number)=>void; onDiscuss?: (v:VersionView['version'])=>void; onVersionOpened?: (v:VersionView['version'])=>void }) {
  const [value, setValue] = useState<VersionView | null>(null); const [versions, setVersions] = useState<SourceVersion[]>([]);
  const [error, setError] = useState(''); const [retry, setRetry] = useState(0); const [viewer, setViewer] = useState<SourceVersion | null>(null); const [basisPage, setBasisPage] = useState(1);
  const [historyOpen,setHistoryOpen]=useState(false);
  const historyButton=useRef<HTMLButtonElement>(null),historyMenu=useRef<HTMLDivElement>(null);
  useAnchoredMenu(historyOpen,historyButton,historyMenu,focus=>{setHistoryOpen(false);if(focus)historyButton.current?.focus();});
  const versionOpened=useRef(onVersionOpened);versionOpened.current=onVersionOpened;
  const accepted=useRef(onAccepted);accepted.current=onAccepted;
  useEffect(() => {
    const c = client.child(); setValue(null); setError('');
    void (async () => {
      try {
        const v = await c.json<VersionView>(`/v1/projects/${project}/artifact-versions/${id}`, 'VersionView'); setValue(v);
        // Чтение может подтвердить принятие, ответ на которое был потерян.
        if(v.review.acceptance)accepted.current();
        if(v.version.payload.result_type==='analytical_note')versionOpened.current?.(v.version);
        const sources = await Promise.all(v.version.provenance.source_versions.map(p => c.json<SourceVersion>(`/v1/projects/${project}/source-versions/${p.source_version_id}`, 'SourceVersion'))); setVersions(sources);
      } catch(e) { if (!c.signal.aborted) setError(errorText(e)); }
    })();
    return () => c.close();
  }, [client, project, id, retry]);
  const [latestId,setLatestId]=useState('');
  const [history,setHistory]=useState<VersionView['version'][]>([]);const [head,setHead]=useState(0);const [editing,setEditing]=useState(()=>!!readLocal(`editor.${project}.${id}`,null));
  const [acceptBusy,setAcceptBusy]=useState(false);const [acceptOps]=useState(()=>new Operations(`accept.${project}.${id}`));
  useEffect(()=>{if(!value)return;const c=client.child();void Promise.all([
    c.list<VersionView['version']>(`/v1/projects/${project}/artifacts/${value.version.artifact_id}/versions`,'VersionPage'),
    c.json<{revision:number;latest_version_id:string}>(`/v1/projects/${project}/artifacts/${value.version.artifact_id}`,'Artifact')
  ]).then(([list,artifact])=>{setHistory(list);setHead(artifact.revision);setLatestId(artifact.latest_version_id);}).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});return ()=>c.close();},[client,project,value]);
  async function accept(){if(!value || acceptBusy)return;setAcceptBusy(true);setError('');try{
    if(!acceptOps.pending)acceptOps.prepare(`/v1/projects/${project}/artifacts/${value.version.artifact_id}/versions/${id}/accept`,{expected_revision:value.review.revision,content_sha256:value.version.content_sha256,acknowledge_unchecked_semantics:true},'VersionReview','Принятие версии');
    const review=await acceptOps.send<VersionView['review']>(client);setValue({...value,review});
    if(review.acceptance)accepted.current();
  }catch(e){setError(errorText(e));}finally{setAcceptBusy(false);}}
  async function openCurrent() {
    if (!value || !confirmNavigation()) return;
    try {
      const artifact = await client.json<{latest_version_id:string}>(`/v1/projects/${project}/artifacts/${value.version.artifact_id}`, 'Artifact');
      onSelect(artifact.latest_version_id);
    } catch(e) { if(!client.signal.aborted)setError(errorText(e)); }
  }
  function revealBasis(event: React.MouseEvent<HTMLElement>) {
    const link = (event.target as Element).closest('a[href^="#basis-"]');
    if (!link) return;
    const target = document.getElementById(link.getAttribute('href')!.slice(1));
    if (!target || !event.currentTarget.contains(target)) return;
    event.preventDefault();
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    }
    target.tabIndex = -1;
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'start'});
  }
  const v = value?.version; const synthetic = v?.provenance.skill_version_id === 'skill_qa-d07-v1';
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(()=>{
    if (v) {
      const target=atExports ? document.getElementById('note-exports') : heading.current;
      target?.focus({preventScroll:true});
      if(!atExports&&target?.closest('.chat-content'))target.closest('.chat-content')?.scrollTo({top:0});
      else target?.scrollIntoView({block:'start'});
    }
  },[v?.id,atExports]);
  const state = value ? versionState(value) : null;
  const title=<h2 ref={heading} tabIndex={-1}>{v?.payload.result_type==='analytical_note' ? v.payload.content.title : 'Ответ по источникам'}</h2>;
  const metadata=v&&<>
      <p className="small muted">Версия {v.revision} · {new Date(v.created_at).toLocaleString('ru-RU')} · {latestId===id?'Актуальная сохраненная версия':latestId?'Историческая версия':'Проверяем актуальность'}</p>
      <div className="reader-meta" data-version-state={state??undefined}>
        <span role="status">{state==='accepted'?'Принята человеком':'Черновик'}</span>
        <button className="text-button" onClick={()=>{const list=document.querySelector<HTMLDetailsElement>('.result-card .all-findings');if(list){list.open=true;list.scrollIntoView({block:'start'});}}}>
          Проверка версии</button>
      </div>
    </>;
  const closeButton=<button className="secondary reader-close" aria-label="К разговору" title="К разговору" onClick={()=>{if(confirmNavigation())onClose();}}>{modern?<WorkspaceIcon name="close"/>:"К разговору"}</button>;
  return <article className="result-card" aria-label="Сохранённый результат" data-version-id={id} onClick={revealBasis}>
    {!modern&&<div className="section-head"><div><p className="eyebrow">Сохранённый результат</p>{title}</div>{closeButton}</div>}
    {modern&&!v&&closeButton}
    {error && <p role="alert" className="error">{error} <button onClick={() => setRetry(n => n+1)}>Повторить чтение</button></p>}
    {!v && !error && <p>Загрузка сохранённой версии…</p>}
    {v && <>
      {!modern&&metadata}
      <div className="reader-tools">
      <span className="reader-version">Версия {v.revision}{latestId&&latestId!==id?' · история':''}</span>
      <button ref={historyButton} aria-label="История версий" title="История версий" aria-expanded={historyOpen} onClick={()=>setHistoryOpen(!historyOpen)}><WorkspaceIcon name="history"/></button>
      {historyOpen&&<div ref={historyMenu} className="reader-history" role="group" aria-label="История записки"><strong>История записки</strong>
        {history.slice().sort((a,b)=>b.revision-a.revision).map(x=><button key={x.id} aria-pressed={id===x.id} onClick={()=>{if(confirmNavigation()){setHistoryOpen(false);onSelect(x.id);}}}>
          Версия {x.revision} · {x.provenance.origin==='human_edit'?'Правка человека':x.provenance.origin==='model_edit'?'Правка модели':x.provenance.origin==='recovered_extracts'?'По сохранённым фактам':x.provenance.origin==='import'?'Копия готовой версии':'Генерация'}{id===x.id?' · выбрана':''}</button>)}
      </div>}
      <div className="actions note-actions">
        {v.payload.result_type==='analytical_note'&&<>
          {!editing&&<button className="secondary" onClick={()=>setEditing(true)} disabled={latestId!==id}>Редактировать записку</button>}
          {onContinue&&!editing&&<button className="secondary" disabled={!head} onClick={()=>{if(confirmNavigation())onContinue(v,head);}}>Продолжить от выбранной версии</button>}
        </>}
      </div>
      {!editing&&value&&v.payload.result_type==='analytical_note'&&<div id="note-exports" tabIndex={-1}><Exports openRequest={atExports} key={v.id} client={client} project={project} value={value} editing={editing}/></div>}
      {!modern&&<details className="version-controls"><summary>Сведения о версии</summary>
        {latestId&&latestId!==id&&<button className="secondary" onClick={()=>void openCurrent()}>Открыть актуальную сохраненную версию</button>}
        <label>Сохраненные версии<select aria-label="Сохраненные версии" value={id} disabled={!history.length} onChange={e=>{if(confirmNavigation())onSelect(e.target.value);}}>{history.map(x=><option key={x.id} value={x.id}>v{x.revision} · {x.provenance.origin==='human_edit'?'Правка человека':x.provenance.origin==='model_edit'?'Правка модели':x.provenance.origin==='recovered_extracts'?'По сохранённым фактам':x.provenance.origin==='import'?'Копия готовой версии':'Генерация'}</option>)}</select></label>
        <p className="small">Выбрана v{v.revision}. Актуальная версия: {head ? `v${head}` : 'загрузка'}. Родитель: {history.find(x=>x.id===v.parent_version_id)?.revision ? `v${history.find(x=>x.id===v.parent_version_id)?.revision}` : 'нет'}. Происхождение: {({import:'Копия готовой версии',recovered_extracts:'По сохранённым фактам',generation:'Генерация',human_edit:'Правка человека',model_edit:'Правка модели'} as Record<string,string>)[v.provenance.origin]??v.provenance.origin}.</p>
      </details>}
      {modern&&closeButton}
      </div>
      {modern&&!editing&&<div className="paper-head">{v.payload.result_type==='analytical_note'&&<div className="aso-brand"><AistBrand /></div>}{title}{metadata}</div>}
      {editing&&value&&<NoteEditor client={client} project={project} value={value} versions={versions} open={(s,page)=>{setBasisPage(page);setViewer(s);}} onDiscuss={onDiscuss} onSaved={(saved,files)=>{setEditing(false);onSelect(saved.version.id,files);}} onCancel={()=>setEditing(false)}/>}
      {synthetic && !v.basis.facts.length && <div className="notice"><strong>Синтетический результат QA</strong><p>Это проверка сохранения и открытия результата. Финансовая аналитика не выполнялась, живая модель не подключена.</p></div>}
      {v.payload.result_type === 'evidence_answer' ? <><h3>Утверждения</h3>{v.payload.content.claims.length ? v.payload.content.claims.map((c,i) => <section key={i}><p>{c.text}</p><ClaimLinks onOpen={s=>{setBasisPage(1);setViewer(s);}} claim={c} facts={v.basis.facts} visuals={v.basis.visual_observations} versions={versions} /><details><summary>Ссылки утверждения</summary><pre>{JSON.stringify({facts:c.fact_refs, calculations:c.calculation_ids}, null, 2)}</pre></details></section>) : <p>Подтверждённых утверждений в этой версии нет.</p>}<h3>Недостающая информация</h3><ul>{v.payload.content.missing_information.map((s,i) => <li key={i}>{s}</li>)}</ul></> : !editing&&value&&<AsoNote client={client} project={project} value={value} versions={versions} open={(source,page)=>{setBasisPage(page);setViewer(source);}}/>}

      {synthetic && !!v.basis.facts.length && <div className="notice"><strong>Контрольные данные QA</strong><p>Происхождение каждого факта указано в основаниях. Расчет выполнен инструментом AIST. Автоматическое извлечение и живая модель не включены.</p></div>}
      {!!v.basis.comparisons?.length && <section aria-label="Сопоставление документов"><h3>Сопоставление документов</h3>{v.basis.comparisons.map((x,i)=><div key={i}><p>{x.message}</p><ClaimLinks onOpen={s=>{setBasisPage(1);setViewer(s);}} claim={{fact_refs:x.fact_refs,calculation_ids:[]}} facts={v.basis.facts} visuals={v.basis.visual_observations} versions={versions}/></div>)}</section>}
      {!editing&&value&&<AllFindings value={value}/>}
      <details className="review-details"><summary>Проверки и принятие</summary>
        <p className="small muted">Сохранение и ручная правка не подтверждают происхождение данных.</p>
        {value?.checks.map(check=><p className="small" key={check.id} data-check-kind={check.kind}>{check.kind==='semantic'?'Смысловая проверка':'Программная проверка'}: {({passed:'пройдена',failed:'есть замечания',not_checked:'не проведена'} as Record<string,string>)[check.status]??check.status}</p>)}
        {state==='draft_with_remarks'&&<p className="notice">Принять эту версию нельзя по результатам проверок. Редактура текста сама по себе не подтверждает происхождение данных и не гарантирует снятие блокировки.</p>}
        {state==='unaccepted'&&<><p className="small">Проверьте содержание записки, имена команды и условия сделки. Нажимая «Принять эту версию», вы подтверждаете проверку содержания и принимаете только выбранную сохраненную версию. Автоматическая проверка смысла текста и правильности чтения изображений не выполнялась. Предупреждения об основаниях и полноте остаются у версии.</p><button disabled={acceptBusy || editing} onClick={()=>void accept()}>{acceptOps.pending?'Проверить принятие':'Принять эту версию'}</button></>}
        {state==='accepted'&&<p className="small">Принята именно v{v.revision}. Сведения о принятии доступны в деталях версии.</p>}
      </details>

      <details className="basis-details"><summary>Основания и страницы источников</summary>{!v.basis.facts.length && !v.basis.calculations.length && !v.basis.visual_observations?.length && !v.basis.review_candidates?.length ? <p>Факты и расчёты в этой версии отсутствуют.</p> : <>
        {v.basis.review_candidates?.map(item=><CandidateBasis key={item.review_candidate.candidate_id} item={item} versions={versions} onOpen={(source,page)=>{setBasisPage(page);setViewer(source);}}/>)}
        {v.basis.visual_observations?.map(o => <section className="basis-fact" id={`basis-${o.observation_id}`} key={o.observation_id}><h4>Визуальное извлечение, не проверено</h4><p>{o.text}</p><p className="small">{o.area}{o.numeric ? ` · ${o.numeric.period.label} · ${o.numeric.nature==='forecast'?'прогноз':'историческое значение'}` : ''}</p><button className="secondary" onClick={()=>{setBasisPage(o.page);setViewer(versions.find(s=>s.id===o.source.source_version_id)??null);}}>По изображению, стр. {o.page}, не проверено</button></section>)}
        {v.basis.facts.map(f => <section className="basis-fact" id={factAnchor({source_version_id:f.source.source_version_id,fact_id:f.fact_id})} key={factAnchor({source_version_id:f.source.source_version_id,fact_id:f.fact_id})}>
          <h4>{f.metric} · {f.period.label}</h4><p><strong>{f.amount ? `${f.amount.value} ${f.amount.scale === '1000000' ? 'млн ' : f.amount.scale === '1000000000000' ? 'трлн ' : f.amount.scale === '1000000000' ? 'млрд ' : f.amount.scale === '1000' ? 'тыс. ' : ''}${f.amount.currency ?? (f.amount.unit === 'percent' ? '%' : f.amount.unit)}` : f.raw_text}</strong></p>
          <p className="small">{f.subject}<br />{f.reporting_basis}</p>
          {f.evidence.map(e => <div key={e.evidence_id}><blockquote>{e.quote}</blockquote><p className="small">{e.locator.area}</p><button className="secondary" onClick={() => { setBasisPage(e.locator.pdf_page??1); setViewer(versions.find(s => s.id === f.source.source_version_id) ?? null); }}>Основание: {versions.find(s=>s.id===f.source.source_version_id)?.original_filename??f.source.source_version_id}, {e.locator.json_pointer??`страница ${e.locator.pdf_page}`}</button></div>)}
          <p className="small muted">Происхождение: {f.extraction_version}</p><ul className="small">{f.limitations.map((x,i) => <li key={i}>{x}</li>)}</ul>
        </section>)}
        {v.basis.calculations.map(c => <section className="basis-calculation" id={`basis-${c.calculation_id}`} key={c.calculation_id}><h4>Расчет роста: {c.rounded.value}%</h4><p className="mono">{c.formula_display}</p><p>Точное значение: {c.exact.numerator}/{c.exact.denominator} процента. Округление HALF_UP.</p><ul className="small">{c.limitations.map((x,i) => <li key={i}>{x}</li>)}</ul></section>)}
      </>}
      </details>
      {!!v.basis.request_messages?.length&&<details className="basis-details"><summary>Сообщения, использованные для условий сделки</summary>
        <p className="small">Сохраненный текст запроса этой версии. Сверьте с ним условия сделки: точность пересказа автоматически не проверяется.</p>
        {v.basis.request_messages.map(message=><blockquote key={message.message_id} style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{message.text}</blockquote>)}
      </details>}
      <details><summary>Исходные материалы</summary><div className="actions">{versions.map(s => <button className="secondary" key={s.id} onClick={() => {setBasisPage(1); setViewer(s);}}>{s.original_filename} · версия {s.revision}</button>)}</div></details>
      <details><summary>Сведения о версии</summary><p className="mono small">{v.id}<br/>SHA: {v.content_sha256}</p><pre>{JSON.stringify({provenance:v.provenance,review:value?.review,checks:value?.checks},null,2)}</pre></details>
    </>}
    {viewer && <SourceReader key={viewer.id + basisPage} initialPage={basisPage} client={client} project={project} version={viewer} onClose={() => setViewer(null)} />}
  </article>;
}

function ClaimLinks({claim,facts,visuals,versions,onOpen}:{claim:Pick<Claim,'fact_refs'|'calculation_ids'|'visual_observation_ids'|'review_candidate_ids'>;facts:Fact[];visuals?:VisualObservation[];versions:SourceVersion[];onOpen:(version:SourceVersion)=>void}) {
  return <span className="claim-links">{claim.fact_refs.map(ref=>{
    const fact=facts.find(f=>f.fact_id===ref.fact_id&&f.source.source_version_id===ref.source_version_id);
    const document=versions.find(v=>v.id===ref.source_version_id);
    const locations=[...new Set(fact?.evidence.map(e=>e.locator.json_pointer??`стр. ${e.locator.pdf_page}`)??[])].join(', ');
    if(document?.media_type==='application/json')return <button className="text-button" key={factAnchor(ref)} onClick={()=>onOpen(document)}>{document.original_filename} · v{document.revision} · {locations}</button>;
    return <a key={factAnchor(ref)} href={`#${factAnchor(ref)}`}>{document?.original_filename??'Источник'} · {locations||'?'}</a>;
  })}{claim.review_candidate_ids?.map(id=><a key={id} href={`#basis-${id}`}>Требует сверки с источником</a>)}{claim.visual_observation_ids?.map(id=>{const observation=visuals?.find(o=>o.observation_id===id);return <a key={id} href={`#basis-${id}`}>Визуальное извлечение, не проверено · стр. {observation?.page??'?'}</a>;})}{claim.calculation_ids.map(id=><a key={id} href={`#basis-${id}`}>Расчет</a>)}</span>;
}
