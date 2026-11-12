import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { groupFindings, hasBlockingFindings, versionState, stateLabels, type Finding } from '../src/review';
import { ReviewFindings, DecisionQuestions } from '../src/ReviewFindings';

test('remark groups preserve repeated, global and unknown findings and their severity', () => {
  const findings: Finding[] = [
    {code:'unchecked_visual_observation', severity:'error', block_id:'visual', message:'Сверить изображение'},
    {code:'unchecked_source_candidate', severity:'warning', block_id:'candidate', message:'Сверить кандидата'},
    {code:'coverage_gap', severity:'warning', block_id:null, message:'Нет рейтинга'},
    {code:'period_mismatch', severity:'error', block_id:'table', message:'Проверить период'},
    {code:'future_code', severity:'info', block_id:null, message:'Новое замечание'},
    {code:'period_mismatch', severity:'error', block_id:'table', message:'Проверить период'},
  ];
  const before = structuredClone(findings);
  const groups = groupFindings(findings);
  assert.deepEqual(groups.map(group => [group.label, group.findings.length]), [
    ['Сверить с источником', 2], ['Не хватает данных', 1], ['Проверить текст и основания', 3],
  ]);
  assert.equal(groups.flatMap(group => group.findings).length, findings.length);
  assert.equal(groups[0].findings[0], findings[0]);
  const html = renderToStaticMarkup(React.createElement(ReviewFindings, {findings}));
  assert.equal((html.match(/data-finding-code=/g) ?? []).length, 6);
  assert.equal((html.match(/data-severity="error"/g) ?? []).length, 3);
  for (const finding of findings) assert.ok(html.includes(finding.message));
  assert.ok(html.includes('К сведению'));
  assert.ok(html.includes('Требует сверки перед принятием версии.'));
  assert.ok(!html.includes('Уровень проверки:'));
  assert.deepEqual(findings, before);
  assert.equal(hasBlockingFindings({checks:[{id:'check',kind:'programmatic',status:'failed',findings}]}), true);
});

import {readingQuestions} from '../src/review';
import type {VersionView} from '../src/types';

test('части таблицы объединяются только при общей причине, странице, источнике и периоде',()=>{
  const rows=['cash','equity','profit','long','other','differentSubject','differentScale','otherTable','unknownColumn'].map((id,i)=>({
    row_id:'row_'+id,label:id,period_label:i===3?'18 месяцев':'На дату',visual_observation_ids:['v'+i]}));
  const observations=rows.map((_,i)=>({observation_id:'v'+i,page:i===2?19:18,area:i===8?'Страница баланса':'Таблица «'+(i===7?'Другая':'Баланс')+'», строка «'+rows[i].label+'», столбец 30.06.23',source:{source_version_id:i===4?'second':'first'},numeric:{period:{label:'На дату'},subject:i===5?'Другой эмитент':'Эмитент',nature:'reported_actual',amount:{unit:'money',scale:i===6?'1':'1000000',currency:'RUB'}}}));
  const findings=rows.map((r,i)=>({code:'unchecked_visual_observation',severity:'error',block_id:'block_table',
    message:'строка '+(i+1)+' «'+r.label+'» ('+r.row_id+'): Не сверено.'}));
  const other={...findings[0],message:'Другая причина.',row_id:rows[0].row_id};
  const view={version:{payload:{content:{sections:[{blocks:[{kind:'table',block_id:'block_table',rows}]}]}},basis:{visual_observations:observations}},
    checks:[{findings:[...findings,other,{code:'coverage_gap',severity:'error',block_id:null,message:'Данных по рискам недостаточно.'},
    {code:'future_code',severity:'error',block_id:null,message:'Иная проверка.'}]}]} as unknown as VersionView;
  const before=structuredClone(view);
  const groups=readingQuestions(view);
  assert.equal(groups.length,11);
  assert.deepEqual(groups[0].rows,['cash','equity']);
  assert.equal(groups[0].rowId,'row_cash');
  assert.equal(groups.filter(g=>g.section==='coverage').length,1);
  assert.ok(groups.some(g=>g.text==='Иная проверка.'));
  assert.deepEqual(view,before);
});

import {decisionQuestions} from '../src/review';
test('changed calculation input points to its dependent row without hiding the check',()=>{
  const finding={code:'calculation_input_edited',severity:'error',block_id:'table',row_id:'growth',message:'Расчет использует прежнее значение.'};
  const view={version:{payload:{content:{sections:[{blocks:[{kind:'table',block_id:'table',caption:'Финансы',rows:[
    {row_id:'growth',label:'Рост выручки',period_label:'2023 к 2022',calculation_ids:['calc_growth']}
  ]}]}]}},basis:{}},checks:[{findings:[finding]}]} as unknown as VersionView;
  const result=decisionQuestions(view);
  assert.equal(result.length,1);
  assert.equal(result[0].detailsOnly,false);
  assert.deepEqual(result[0].locations,[{blockId:'table',rowId:'growth'}]);
  assert.ok(result[0].text.startsWith('Расчёт использует число до правки.'));
  assert.equal(view.checks[0].findings[0],finding);
});

test('the reading view asks about a changed value and keeps parser diagnostics available separately',()=>{
  const findings=[
    {code:'unsupported_numeric_form',severity:'error',block_id:null,message:'B2B'},
    {code:'unchecked_source_excerpt',severity:'error',block_id:null,message:'Дословная выдержка'},
    {code:'quantity_mismatch',severity:'error',block_id:null,message:'Количество сотрудников расходится с источником.'},
  ];
  const view={version:{payload:{content:{sections:[]}},basis:{}},checks:[{findings}]} as unknown as VersionView;
  assert.deepEqual(decisionQuestions(view).map(item=>item.text),['Какое количество указано в источнике?']);
  assert.equal(view.checks[0].findings.length,3);
});

test('skip an omitted editorial sentence and a typographic range, keep a real period discrepancy',()=>{
  const view={omitted_note_blocks:['gap'],version:{payload:{content:{sections:[]}},basis:{}},checks:[{findings:[
    {code:'unreferenced_claim',severity:'error',block_id:'gap',message:'Нет основания'},
    {code:'ambiguous_sign',severity:'error',block_id:'years',message:'Неоднозначный знак числа в абзаце: «значения за 2017-2023». Не соединяй периоды дефисом.'},
    {code:'period_mismatch',severity:'error',block_id:'actual',message:'Срок 18 месяцев указан как год.'}
  ]}]} as unknown as VersionView;
  assert.deepEqual(decisionQuestions(view).map(item=>item.blockId),['actual']);
  assert.equal(view.checks[0].findings.length,3);
});

test('repeat questions keep all distinct locations and leave the original protocol intact',()=>{
  const first={code:'period_mismatch',severity:'error',block_id:'paragraph',message:'18 месяцев не равны году.'};
  const view={version:{payload:{content:{sections:[{blocks:['paragraph','repeated'].map(block_id=>({kind:'paragraph',block_id,claim:{text:'Выручка за 18 месяцев.',fact_refs:[]}}))}]}},basis:{}},checks:[{findings:[
    first,{...first},{...first,block_id:'repeated'},
    {...first,message:'Другой период.'},{...first,code:'subject_mismatch'},
    {...first,code:'unchecked_visual_observation'},
  ]}]} as unknown as VersionView;
  const before=structuredClone(view);
  const result=decisionQuestions(view);
  assert.equal(result.length,3);
  assert.deepEqual(result[0].locations,[{blockId:'paragraph',rowId:undefined},{blockId:'repeated',rowId:undefined}]);
  assert.deepEqual(view,before);
});

test('compact questions preserve distinct causes, exact row targets and unknown diagnostics',()=>{
  const message='Период 2023-01-01 по 2023-12-31 не совпадает с сохраненным основанием. Квартал не подтверждает годовой показатель.';
  const view={version:{payload:{content:{sections:[{blocks:[
    {kind:'paragraph',block_id:'p1',claim:{text:'Выручка выросла.'}},
    {kind:'paragraph',block_id:'p2',claim:{text:'Прибыль снизилась.'}},
    {kind:'table',block_id:'t1',caption:'Финансы',rows:[{row_id:'row_value',label:'Прибыль',period_label:'2023 год'}]},
  ]}]}},basis:{}},checks:[{findings:[
    {code:'period_mismatch',severity:'error',block_id:'p1',message},
    {code:'period_mismatch',severity:'error',block_id:'p2',message},
    {code:'row_basis_mismatch',severity:'error',block_id:'t1',row_id:'row_value',message:'row_value: неверная сумма.'},
    {code:'new_check',severity:'warning',block_id:'p1',message:'internal_code: «internal_id»'},
  ]}]} as unknown as VersionView;
  const before=structuredClone(view);
  const html=renderToStaticMarkup(React.createElement(DecisionQuestions,{value:view}));
  assert.equal(decisionQuestions(view).length,4);
  assert.ok(html.includes('Требует сверки · 4'));
  for(const id of ['p1','p2','row_value','checks'])assert.ok(html.includes('href="#note-'+id+'"'));
  assert.ok(!html.includes('<details'));
  assert.ok(!html.includes('Квартал не подтверждает'));
  assert.ok(!html.includes('internal_code'));
  assert.ok(html.includes('Проверка не пройдена'));
  const original=renderToStaticMarkup(React.createElement(ReviewFindings,{findings:view.checks[0].findings}));
  assert.ok(original.includes('internal_code: «internal_id»'));
  assert.deepEqual(view,before);
  const edited=renderToStaticMarkup(React.createElement(DecisionQuestions,{value:view,blockIds:new Set(['p1'])}));
  assert.ok(!edited.includes('href="#note-row_value"'));
});

test('identical questions with different evidence remain separate',()=>{
  for(const basis of ['fact_refs','calculation_ids','visual_observation_ids','review_candidate_ids']){
  const view={version:{payload:{content:{sections:[{blocks:['a','b'].map(id=>({
    kind:'paragraph',block_id:id,claim:{text:'Выручка за год.',[basis]:basis==='fact_refs'?[{fact_id:id}]:[id]},
  }))}]}},basis:{}},checks:[{findings:['a','b'].map(id=>({
    code:'period_mismatch',severity:'error',block_id:id,message:'Период не совпадает.',
  }))}]} as unknown as VersionView;
  assert.equal(decisionQuestions(view).length,2);
  }
});


test('warning-only technical result is ready for human review, old failed is blocked', () => {
  const checks = [{id:'technical', kind:'programmatic', status:'passed', findings:[
    {code:'unchecked_visual_observation', severity:'warning', block_id:'visual', message:'Не сверено'},
    {code:'coverage_gap', severity:'warning', block_id:null, message:'Возможна неполнота'},
  ]}, {id:'semantic',kind:'semantic',status:'not_checked',findings:[]}];
  const view = {checks,review:{acceptance:null}} as unknown as VersionView;
  assert.equal(versionState(view), 'unaccepted');
  assert.equal(stateLabels[versionState(view)], 'Технические проверки пройдены, версия не принята');
  assert.equal(hasBlockingFindings(view), false);
  const old = structuredClone(view); old.checks[0].status = 'failed';
  assert.equal(versionState(old), 'draft_with_remarks');
  assert.equal(versionState(view), 'unaccepted');
});
