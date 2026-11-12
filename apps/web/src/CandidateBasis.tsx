import React from 'react';
import type { ReviewCandidateBasis, SourceVersion } from './types';

export function candidatePages(candidate:ReviewCandidateBasis['review_candidate']):number[] {
  const value=candidate.input;
  const pages=new Set(value.evidence.map(link=>link.page));
  if(value.basis){pages.add(value.basis.page);if(value.basis.reporting_page)pages.add(value.basis.reporting_page);}
  if(value.table)pages.add(value.table.page);
  return [...pages].sort((a,b)=>a-b);
}

export function CandidateBasis({item,versions,onOpen}:{item:ReviewCandidateBasis;versions:SourceVersion[];onOpen:(source:SourceVersion,page:number)=>void}) {
  const candidate=item.review_candidate, value=candidate.input;
  const source=versions.find(version=>version.id===candidate.source.source_version_id);
  return <section className="basis-fact" id={`basis-${candidate.candidate_id}`}>
    <h4>Требует сверки с источником</h4>
    <p className="notice small">Все сведения кандидата непроверены. Они не являются допущенным фактом или основанием расчета.</p>
    <p>{value.subject}{value.metric ? ` · ${value.metric}` : ''}</p>
    <p>Текст кандидата: {value.value_text??'не указан'}</p>
    {value.amount&&<p className="small">Заявленное значение: {value.amount.value}; единица: {value.basis?.unit_label||({money:'денежная сумма',percent:'процент'}[value.amount.unit]??value.amount.unit)}{value.amount.currency ? `; валюта: ${value.amount.currency}` : ''}.</p>}
    <p className="small">Заявленный период: {value.period.label}{value.period.start ? ` (${value.period.start} - ${value.period.end})` : ''}. Отчетная база: {value.reporting_basis}.</p>
    <p className="small">Заявленный характер: {value.nature==='reported_actual'?'историческое значение':'сведения источника'}. Правильность не подтверждена.</p>
    <p>Причина: {item.reason}. {item.message}</p>
    <div className="actions">{candidatePages(candidate).map(page=><button type="button" className="secondary" key={page} disabled={!source} onClick={()=>source&&onOpen(source,page)}>Источник: {source?.original_filename??'Исходный документ'}, страница {page}</button>)}</div>
  </section>;
}
