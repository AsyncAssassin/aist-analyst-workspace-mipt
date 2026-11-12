// Состояние сохраненной версии выводится из существующих проверок и принятия, без отдельной машины состояний.
import type { VersionView } from './types';

export type VersionState = 'accepted' | 'draft_with_remarks' | 'unaccepted';
export type Finding = VersionView['checks'][number]['findings'][number];

export function hasBlockingFindings(view: Pick<VersionView, 'checks'>): boolean {
  return view.checks.some(check => check.status === 'failed' || check.findings.some(f => f.severity === 'error'));
}

export function versionState(view: Pick<VersionView, 'checks' | 'review'>): VersionState {
  if (view.review.acceptance) return 'accepted';
  return hasBlockingFindings(view) ? 'draft_with_remarks' : 'unaccepted';
}

export const stateLabels: Record<VersionState, string> = {
  accepted: 'Принята человеком',
  draft_with_remarks: 'Черновик с замечаниями, принять нельзя',
  unaccepted: 'Технические проверки пройдены, версия не принята',
};

export function excerpt(text: string, limit = 40): string {
  const compact = text.split(/\s+/).join(' ').trim();
  return compact.length <= limit ? compact : compact.slice(0, limit).trimEnd() + '…';
}

export const findingGroups = {
  source: 'Сверить с источником',
  coverage: 'Не хватает данных',
  text: 'Проверить текст и основания',
};

export function findingGroup(code: string): keyof typeof findingGroups {
  if (code === 'unchecked_visual_observation' || code === 'unchecked_source_candidate' || code === 'unchecked_source_excerpt') return 'source';
  if (code === 'coverage_gap') return 'coverage';
  return 'text';
}

// Group the original entries, including repeats and unknown codes. This never changes validation.
export function groupFindings(findings: Finding[]) {
  return (Object.keys(findingGroups) as (keyof typeof findingGroups)[]).map(key => ({
    key, label: findingGroups[key], findings: findings.filter(f => findingGroup(f.code) === key),
  }));
}

// Место замечания: раздел, номер блока, выдержка абзаца или подпись таблицы, как в сообщениях сервера.
export function findingPlace(view: Pick<VersionView, 'version'>, blockId: string | null): string {
  if (!blockId) return '';
  const sections = view.version.payload.content.sections ?? [];
  for (const [si, section] of sections.entries()) {
    for (const [bi, block] of section.blocks.entries()) {
      if (block.block_id !== blockId) continue;
      const kind = block.kind === 'paragraph' ? `абзац «${excerpt(block.claim.text)}»` : `таблица «${excerpt(block.caption)}»`;
      return `Раздел ${si + 1} «${excerpt(section.title)}», блок ${bi + 1}, ${kind}`;
    }
  }
  return `Блок ${blockId}`;
}

export function blockFindings(view: Pick<VersionView, 'checks'>, blockId: string): Finding[] {
  return view.checks.flatMap(check => check.findings.filter(f => f.block_id === blockId));
}

// Сводка для чтения. Исходные проверки сохранённой версии остаются в деталях без изменений.
// Эти статусы описывают способ извлечения или пределы автоматической проверки.
// Все исходные записи остаются в полном протоколе версии.
const processFindings = new Set([
  'unchecked_visual_observation','unchecked_source_candidate','unchecked_source_excerpt',
  'unsupported_numeric_form','coverage_gap',
]);
function needsDecision(finding: Finding) {
  if(finding.code==='ambiguous_sign'){
    const literal=/«([^»]+)»/.exec(finding.message)?.[1];
    if(literal && /(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}$/.test(literal) && finding.message.includes('Не соединяй периоды дефисом'))return false;
  }
  return finding.severity !== 'info' && !processFindings.has(finding.code);
}

export const readingLabels={source:'Что требует сверки',unparsed:'Не распознано автоматически',coverage:'Ограничения полноты'};

export function readingQuestions(view: VersionView) {
  type Item = {code:string; blockId:string|null; rowId?:string; text:string; rows:string[]; section:keyof typeof readingLabels; reason?:string};
  const items:Item[]=[];
  const grouped=new Map<string,Item>();
  const blocks=view.version.payload.content.sections?.flatMap(s=>s.blocks)??[];
  const observations=new Map(view.version.basis.visual_observations?.map(o=>[o.observation_id,o]));
  for(const f of view.checks.flatMap(c=>c.findings)){
    const block=blocks.find(b=>b.block_id===f.block_id);
    if(f.code==='unchecked_visual_observation'&&block?.kind==='table'){
      // В ранних версиях идентификатор строки был только в префиксе сообщения.
      const legacy=/^строка \d+ «[^»]+» \((row_[\w-]+)\): /;
      const rowId=f.row_id??legacy.exec(f.message)?.[1];
      const row=block.rows.find(r=>r.row_id===rowId);
      // Сверять вместе можно строки одной таблицы, одной страницы и одного сопоставимого столбца.
      // Несколько источников или наблюдений у строки требуют самостоятельного разбора.
      const obs=row?.visual_observation_ids?.length===1?observations.get(row.visual_observation_ids[0]):undefined;
      const numeric=obs?.numeric;
      if(row&&obs&&numeric){
        const reason=f.message.replace(legacy,'');
        const {unit,scale,currency}=numeric.amount;
        const location=/^Таблица «([^»]+)», (?:строка «[^»]+», )?столбец (.+)$/i.exec(obs.area);
        // Без общей подписи исходной таблицы и колонки оставляем отдельную строку.
        const sourceColumn=location?[location[1],location[2]]:row.row_id;
        const key=JSON.stringify([sourceColumn,block.block_id,f.code,f.severity,reason,obs.source.source_version_id,obs.page,
          row.period_label,numeric.period,numeric.subject,numeric.nature,unit,scale,currency]);
        let item=grouped.get(key);
        if(!item){
          item={code:f.code,blockId:f.block_id,rowId:row.row_id,text:'Сверить значения со столбцом на стр. '+obs.page+' ('+row.period_label+')',rows:[],section:'source'};
          grouped.set(key,item);items.push(item);
        }
        item.rows.push(row.label);
        continue;
      }
    }
    const scope=/^Часть запроса «([^»]+)» не подтверждена:/.exec(f.message);
    const sign=f.code==='ambiguous_sign'?/«([^»]+)»/.exec(f.message)?.[1]:undefined;
    const years=sign?.match(/(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}/);
    const simple=years?'Уточнить, как в источнике сгруппированы годы '+years[0]+'.':
      sign?'Уточнить знак числа в тексте «'+sign+'».':
      f.code==='unreferenced_claim'&&block?.kind==='paragraph'?'Нет сохранённого основания для «'+excerpt(block.claim.text,82)+'».':
      f.code==='coverage_gap'&&scope?'Недостаточно подтверждённых сведений по запросу: «'+scope[1]+'».':
      f.code==='unchecked_source_candidate'&&block?.kind==='paragraph'?'Сверить предварительные сведения: «'+excerpt(block.claim.text,95)+'».':
      f.code==='unchecked_visual_observation'&&block?.kind==='paragraph'?'Сверить сведения с изображением: «'+excerpt(block.claim.text,95)+'».':
      f.code==='unsupported_numeric_form'&&f.message.startsWith('Числовая формулировка не распознана полностью.')?
        'Сверить смысл чисел в формулировке: '+(f.message.match(/Нераспознанные фрагменты: (.+?)\. Год пишите/)?.[1]??(block?.kind==='paragraph'?excerpt(block.claim.text):f.message))+'.':
      f.message;
    const rowId=block?.kind==='table'&&block.rows.some(r=>r.row_id===f.row_id)?f.row_id:undefined;
    items.push({code:f.code,blockId:f.block_id,rowId,text:simple,reason:f.message,rows:[],section:f.code==='coverage_gap'?'coverage':f.code==='unsupported_numeric_form'?'unparsed':'source'});
  }
  return items;
}

export function decisionQuestions(view: VersionView) {
  const omitted = new Set(view.omitted_note_blocks);
  const checks=view.checks.map(check=>({...check,findings:check.findings.filter(f=>
    needsDecision(f) && !(f.block_id && omitted.has(f.block_id)))}));
  const grouped=new Map<string,ReturnType<typeof readingQuestions>[number]&{detailsOnly:boolean;locations:{blockId:string;rowId?:string}[]}>();
  const blocks=view.version.payload.content.sections?.flatMap(s=>s.blocks)??[];
  for(const item of readingQuestions({...view,checks})){
    const block=blocks.find(b=>b.block_id===item.blockId);
    const row=block?.kind==='table'?block.rows.find(r=>r.row_id===item.rowId):undefined;
    const context=row?[row.label,row.period_label].filter(Boolean).join(', '):block?.kind==='paragraph'?block.claim.text:block?.kind==='table'?block.caption:'';
    // Short display labels are not identities: distinct reasons and subjects remain separate.
    const claim=block?.kind==='paragraph'?block.claim:undefined;
    const basis=row?[row.fact_refs,row.calculation_ids,row.visual_observation_ids]:claim?
      [claim.fact_refs,claim.calculation_ids,claim.visual_observation_ids,claim.review_candidate_ids]:item.blockId;
    const key=JSON.stringify([item.code,item.reason??item.text,item.section,context,basis]);
    let question=grouped.get(key);
    if(!question){question={...item,text:decisionLabel(item.code,item.reason??item.text,context),detailsOnly:!decisionPrompts[item.code],locations:[]};grouped.set(key,question);}
    if(item.blockId&&!question.locations.some(p=>p.blockId===item.blockId&&p.rowId===item.rowId))
      question.locations.push({blockId:item.blockId,rowId:item.rowId});
  }
  return [...grouped.values()];
}

const decisionPrompts:Record<string,string>={
    period_mismatch:'Какой период указан в источнике?',
    quantity_mismatch:'Какое количество указано в источнике?',
    subject_mismatch:'К какой компании относится показатель?',
    unit_mismatch:'В каких единицах приведён показатель?',
    row_basis_mismatch:'Какое значение подтверждает источник?',
    row_label_mismatch:'Как называется показатель в источнике?',
    calculation_input_edited:'Расчёт использует число до правки. Нужно согласовать его с изменённым значением.',
    conflicting_financial_cell:'Какое из значений относится к этому показателю и периоду?',
    unreferenced_claim:'Чем подтверждается утверждение?',
    ambiguous_link:'Какой источник относится к утверждению?',
    ambiguous_sign:'Какой знак у значения в источнике?',
    unsupported_editorial_number:'Чем подтверждается число?',
    rating_date_unspecified:'На какую дату указан рейтинг?',
    unreferenced_editorial_heading:'Чем подтверждается заголовок?',
    incomplete_financial_matrix:'Каких периодов или показателей не хватает в таблице?',
};
function decisionLabel(code:string,message:string,context:string) {
  const prompt=decisionPrompts[code]??'Проверка не пройдена. Откройте подробности.';
  const quoted=/«([^»]+)»/.exec(message)?.[1];
  const subject=context||(decisionPrompts[code]?quoted:undefined);
  return subject?prompt+' «'+excerpt(subject,85)+'»':prompt;
}
