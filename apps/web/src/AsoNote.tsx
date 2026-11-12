import React,{useEffect,useState} from 'react';
import {Client,errorText} from './client';
import {factAnchor,type SourceVersion,type VersionView,type Claim,type NewsRun} from './types';

type Link={source:string;page:number};
type Cell={text:string;row_id:string|null;links:Link[];marked:boolean;quotes:string[];styled_runs?:{text:string;bold:boolean;italic:boolean;highlight:boolean}[]};
type Block={publication?:NewsRun['items'][number];list_level?:1|2;id:string;kind:'paragraph'|'table';caption:string;headers:string[];rows:Cell[][];notes:string[];marked:boolean};
type Layout={version_id:string;sha256:string;date:string;team?:string;sections:{title:string;blocks:Block[]}[]};

function marked(c:Cell){
  if(c.styled_runs)return c.styled_runs.map((r,i)=>{
    let value:React.ReactNode=r.text;
    if(r.bold)value=<strong>{value}</strong>;
    if(r.italic)value=<em>{value}</em>;
    return <React.Fragment key={i}>{r.highlight?<mark>{value}</mark>:value}</React.Fragment>;
  });
  if(!c.marked)return c.text;
  if(!c.quotes.length)return <mark>{c.text}</mark>;
  const pieces:React.ReactNode[]=[];
  let offset=0;
  for(const quote of [...c.quotes].sort((a,b)=>c.text.indexOf(a)-c.text.indexOf(b))){
    const start=c.text.indexOf(quote),end=start+quote.length;
    if(end<=offset)continue;
    pieces.push(c.text.slice(offset,Math.max(offset,start)),<mark key={start+':'+end}>{c.text.slice(Math.max(offset,start),end)}</mark>);
    offset=end;
  }
  return <>{pieces}{c.text.slice(offset)}</>;
}

export function AsoNote({client,project,value,versions,open}:{client:Client;project:string;value:VersionView;versions:SourceVersion[];open:(s:SourceVersion,page:number)=>void}){
  const v=value.version;
  const [layout,setLayout]=useState<Layout|null>(null),[error,setError]=useState(''),[attempt,setAttempt]=useState(0);
  useEffect(()=>{
    const c=client.child();
    void c.json<Layout>('/v1/projects/'+project+'/artifact-versions/'+v.id+'/aso','AsoView').then(data=>{
      if(data.version_id!==v.id||data.sha256!==v.content_sha256)throw Error('Version mismatch');
      setLayout(data);setError('');
    }).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return()=>c.close();
  },[client,project,v.id,v.content_sha256,attempt]);
  useEffect(()=>{
    if(!layout||!location.hash.startsWith('#note-'))return;
    const block=document.getElementById(location.hash.slice(1));
    if(block)block.scrollIntoView({block:'center'});
  },[layout]);
  function links(items:Link[]){
    return items.map(l=>{
      const s=versions.find(s=>s.id===l.source);
      return s&&<button type="button" key={s.id+':'+l.page} className="page-ref" title={s.original_filename} onClick={event=>{event.currentTarget.focus({preventScroll:true});open(s,l.page);}}>
        {versions.length>1?s.original_filename+' · ':''}стр. {l.page}</button>;
    });
  }
  function basis(block:Block){
    const saved=v.payload.content.sections?.flatMap(s=>s.blocks).find(item=>item.block_id===block.id);
    if(!saved)return null;
    const publication=block.publication;
    if(publication?.date_quote!==undefined){
      const source=v.basis.external_context?.items.find(item=>item.evidence_id===publication.external_evidence_id);
      return <><p>Дата публикации: {publication.published ?? 'не установлена'}. Дата события: {publication.event_date ?? 'не установлена'}.</p>
        {publication.metadata_published!==publication.published&&<p>В метаданных источника: {publication.metadata_published ?? 'дата не указана'}. Метаданные не использованы как подтверждение даты публикации.</p>}
        {publication.date_quote&&<blockquote>{publication.date_quote}</blockquote>}
        {source&&<a href={source.url} target="_blank" rel="noopener noreferrer">{source.title || source.publisher}</a>}
        <small>Получено {publication.retrieved_at?.slice(0,10)}. Выдержка сохранена в основании; смысл выбранной даты проверяет читатель.</small></>;
    }
    const external = saved.kind==='paragraph' ? v.basis.external_context?.items.filter(item=>saved.claim.external_evidence_ids?.includes(item.evidence_id)) ?? [] : [];
    const externalLinks = external.map(item=><details key={item.evidence_id} className="block-basis"><summary>{item.publisher} · {item.published ?? 'дата публикации неизвестна'}</summary>
      <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title || item.publisher}</a>
      <p>{item.quote}</p><small>{item.retrieval_kind==='provider_extract'&&<>Текст получен через поискового провайдера. </>}{item.truncated&&<>Сохранен фрагмент источника. </>}{item.search_target&&<>Цель поиска: {item.search_target}. </>}Получено {item.retrieved_at.slice(0,10)}. Смысл, принадлежность сведений и актуальность проверяет читатель.</small></details>);
    if(externalLinks.length)return externalLinks;
    const items:Pick<Claim,'fact_refs'|'calculation_ids'|'visual_observation_ids'|'review_candidate_ids'>[]=saved.kind==='paragraph'?[saved.claim]:saved.rows;
    const targets = new Map<string, {label: string; detail?: string}>();
    for (const item of items) {
      for (const id of item.review_candidate_ids ?? []) {
        const input = v.basis.review_candidates?.find(c => c.review_candidate.candidate_id === id)?.review_candidate.input;
        const detail = input && [input.metric || input.basis?.metric_label, input.period.label, input.value_text].filter(Boolean).join(' · ');
        targets.set('basis-' + id, {label: 'Предварительные сведения', detail});
      }
      for (const id of item.visual_observation_ids ?? []) {
        const observation = v.basis.visual_observations?.find(o => o.observation_id === id);
        const detail = observation && [observation.numeric?.metric || observation.area, observation.numeric?.period.label, 'стр. ' + observation.page].filter(Boolean).join(' · ');
        targets.set('basis-' + id, {label: 'Визуальное наблюдение', detail});
      }
      for (const id of item.calculation_ids) {
        const calculation = v.basis.calculations.find(c => c.calculation_id === id);
        const input = calculation?.inputs[0]?.fact;
        const fact = input && v.basis.facts.find(f => f.fact_id === input.fact_id && f.source.source_version_id === input.source_version_id);
        const detail = calculation && [fact?.metric, calculation.rounded.value.replace('.', ',') + '%'].filter(Boolean).join(' · ');
        targets.set('basis-' + id, {label: 'Расчёт', detail});
      }
      for (const ref of item.fact_refs) {
        const source = versions.find(s => s.id === ref.source_version_id);
        if (source?.media_type === 'application/json') {
          const fact = v.basis.facts.find(f => f.fact_id === ref.fact_id && f.source.source_version_id === ref.source_version_id);
          targets.set(factAnchor(ref), {label: source.original_filename, detail: fact?.metric});
        }
      }
    }
    const entries = [...targets];
    if (entries.length <= 1) return entries.map(([id, {label, detail}]) =>
      <a className="page-ref" key={id} title={detail} href={'#' + id}>{label}</a>);
    const labels = new Set(entries.map(([, item]) => item.label));
    return <details className="block-basis"><summary>{labels.size === 1 ? entries[0][1].label : 'Основания фрагмента'} · {entries.length}</summary>
      <ul>{entries.map(([id, {label, detail}], index) => <li key={id}><a href={'#' + id}>
        {detail ? (labels.size === 1 ? detail : label + ' · ' + detail) : label + ' ' + (index + 1)}
      </a></li>)}</ul></details>;
  }
  if(error)return <p role="alert">{error} <button onClick={()=>setAttempt(n=>n+1)}>Повторить чтение</button></p>;
  if(!layout||layout.version_id!==v.id)return <p>Открываем записку…</p>;
  // Шапка ASO та же, что в файлах: команда версии и дата ее создания по Москве.
  return <div className="aso-note"><p className="aso-head"><span>{layout.team!==undefined&&'Команда: '+layout.team}</span><span>{layout.date}</span></p>
    {layout.sections.map(group=><section className="aso-section note-section" key={group.blocks[0].id}>
    <h3>{group.title}</h3><div>{group.blocks.map(b=><div id={'note-'+b.id} className="aso-block" key={b.id}>
      {b.kind==='paragraph'?<p className={b.list_level?'note-bullet level-'+b.list_level:undefined}>{marked(b.rows[0][0])} {links(b.rows[0][0].links)}</p>:<><div className="aso-scroll"><table>
        <caption>{b.marked?<mark>{b.caption}</mark>:b.caption}</caption>
        <thead><tr>{b.headers.map((h,i)=><th key={i}>{h}</th>)}</tr></thead><tbody>{b.rows.map((row,i)=><tr key={i}>{row.map((c,j)=><td key={j} id={c.row_id?'note-'+c.row_id:undefined} data-row-id={c.row_id||undefined}
          title={c.links.map(l=>'стр. '+l.page).join(', ')||undefined}>{marked(c)}</td>)}</tr>)}</tbody>
      </table></div>
      {/* Страницы ячеек сведены под таблицей; адрес каждой ячейки остается в данных и подсказке. */}
      <p className="table-sources">{links([...new Map(b.rows.flat().flatMap(c=>c.links).map(l=>[l.source+':'+l.page,l])).values()])}</p></>}
      {b.publication&&<><a className="page-ref" href={b.publication.url} target="_blank" rel="noopener noreferrer" title={b.publication.publisher}>ссылка</a>
        {!b.rows[0][0].text.includes(b.publication.quote)&&<details className="block-basis"><summary>Исходная формулировка</summary><p>«{b.publication.quote}»</p></details>}</>}
      {b.notes.map((n,i)=><small key={i}>{n}</small>)}
      {(() => {
        const evidence=basis(b);
        if(!evidence || Array.isArray(evidence) && !evidence.length)return null;
        return <details className="block-basis note-evidence"><summary>Основания</summary>{evidence}</details>;
      })()}
    </div>)}</div></section>)}</div>;
}
