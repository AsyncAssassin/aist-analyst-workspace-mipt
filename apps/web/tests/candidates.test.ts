import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { candidatePages, CandidateBasis } from '../src/CandidateBasis';
import { validateDefinition } from '../src/contracts';
import type { ReviewCandidateBasis } from '../src/types';

const item:ReviewCandidateBasis={code:'unsupported_layout',reason:'Не поддержана структура',message:'Период не связан со значением.',review_candidate:{
  candidate_id:'candidate_fixture',source:{source_id:'src_fixture',source_version_id:'sv_fixture',sha256:'a'.repeat(64)},status:'not_admitted',
  input:{metric:'Выручка',subject:'Meridian',reporting_basis:'Не указано',nature:'reported_actual',value_text:'137',
    amount:{value:'137',unit:'money',currency:'RUB'},period:{kind:'unknown',label:'Не указано',start:null,end:null},
    evidence:[{page:5,line_start:1,line_end:2,area:'Таблица'},{page:2,line_start:1,line_end:1,area:'Эмитент'}],
    basis:{page:3,reporting_page:7,metric_label:'Выручка',unit_label:'млн руб.',reporting_label:null},table:{page:5}}}};

test('candidate pages cover evidence, basis and table once without changing input',()=>{
  const before=structuredClone(item);
  assert.deepEqual(candidatePages(item.review_candidate),[2,3,5,7]);
  assert.deepEqual(item,before);
  const html=renderToStaticMarkup(React.createElement(CandidateBasis,{item,versions:[],onOpen:()=>{}}));
  assert.ok(html.includes('Требует сверки с источником'));
  assert.ok(html.includes('Текст кандидата: 137'));
  assert.ok(html.includes('Правильность не подтверждена'));
  assert.ok(!html.includes('unsupported_layout'));
  assert.ok(!html.includes('Визуальное извлечение'));
});

test('only a note paragraph can carry candidate IDs with the required origin',()=>{
  const claim={text:'Предварительное значение',origin:'source_candidate',fact_refs:[],calculation_ids:[],review_candidate_ids:['candidate_fixture']};
  assert.deepEqual(validateDefinition('Claim',claim),claim);
  for(const invalid of [{...claim,origin:'inference'},{...claim,review_candidate_ids:[]},
    {text:claim.text,origin:claim.origin,fact_refs:[],calculation_ids:[]},{...claim,review_candidates:[item]}]) {
    assert.throws(()=>validateDefinition('Claim',invalid));
  }
  assert.throws(()=>validateDefinition('AnswerContent',{claims:[claim],missing_information:[]}));
});
