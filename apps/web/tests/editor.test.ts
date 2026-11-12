import test from 'node:test';
import assert from 'node:assert/strict';
import {pendingChanges,type Payload} from '../src/editorMerge';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {NoteEditor} from '../src/NoteEditor';
import {Client} from '../src/client';
import type {VersionView} from '../src/types';
import entities from '../../../contracts/v1/examples/entities.json';

test('table editor keeps the exact trillion payload and decimal input', () => {
  const view=structuredClone(entities.records.note_v1_view.value) as unknown as VersionView;
  view.version.payload.content.sections=[{section_id:'sec_scale',title:'Финансы',blocks:[{
    block_id:'block_scale',kind:'table',caption:'Выручка',rows:[{row_id:'row_scale',label:'Выручка',period_label:'2024',
      amount:{value:'2.200000000000000001',unit:'money',scale:'1000000000000',currency:'RUB'},fact_refs:[],calculation_ids:[]}]}]}];
  view.checks=[];
  const before=JSON.stringify(view), storage=Object.getOwnPropertyDescriptor(globalThis,'sessionStorage');
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,value:{getItem:()=>null}});
  try {
    const html=renderToStaticMarkup(React.createElement(NoteEditor,{client:new Client('test'),project:'project_scale',value:view,
      versions:[],open:()=>{},onSaved:()=>{},onCancel:()=>{}}));
    assert.match(html,/2[.,]200000000000000001/);
    assert.equal(JSON.stringify(view),before);
  } finally {
    if(storage)Object.defineProperty(globalThis,'sessionStorage',storage);
    else Reflect.deleteProperty(globalThis,'sessionStorage');
  }
});

test('external paragraph editing retains evidence and detects changed concurrent basis',()=>{
  const payload:Payload={result_type:'analytical_note',content:{claims:[],missing_information:[],sections:[{
    section_id:'sec_company',title:'Компания',blocks:[{kind:'paragraph',block_id:'block_external',claim:{
      text:'Исходное описание',origin:'external_context',fact_refs:[],calculation_ids:[],external_evidence_ids:['ext_original']}}]}]}};
  const draft=structuredClone(payload),head=structuredClone(payload);
  const paragraph=draft.content.sections![0].blocks[0];
  if(paragraph.kind!=='paragraph')throw Error();
  paragraph.claim.text='Уточненное описание';
  const changes=pendingChanges(payload,draft,head);
  assert.equal(changes[0].compatible,true);changes[0].apply(head);
  assert.deepEqual(head.content.sections![0].blocks[0],paragraph);
  const concurrent=structuredClone(payload),live=concurrent.content.sections![0].blocks[0];
  if(live.kind!=='paragraph')throw Error();
  live.claim.external_evidence_ids=['ext_other'];
  assert.equal(pendingChanges(payload,draft,concurrent)[0].compatible,false);
});

test('concurrent paragraph survives a cell edit, changed basis prevents transfer',()=>{
 const payload:Payload={result_type:'analytical_note',content:{claims:[],missing_information:[],sections:[{section_id:'s',title:'Компания',blocks:[
   {kind:'paragraph',block_id:'p',claim:{text:'Раньше',origin:'human_editorial',fact_refs:[],calculation_ids:[]}},
   {kind:'table',block_id:'t',caption:'Прибыль',rows:[{row_id:'r',label:'Прибыль',period_label:'2023',amount:{value:'1',scale:'1',unit:'money',currency:'RUB'},fact_refs:[{source_version_id:'s',fact_id:'f'}],calculation_ids:[]}]}]}]}};
 const draft=structuredClone(payload),head=structuredClone(payload);
 const items=(p:Payload)=>p.content.sections![0].blocks;
 const t=items(draft)[1],p=items(head)[0],live=items(head)[1];if(t.kind!=='table'||p.kind!=='paragraph'||live.kind!=='table')throw Error();
 t.rows[0].amount.value='2';p.claim.text='Правка другого автора';
 const changes=pendingChanges(payload,draft,head);assert.equal(changes.length,1);assert.ok(changes[0].compatible);
 const merged=structuredClone(head);changes[0].apply(merged);assert.deepEqual(items(merged),[p,t]);
 live.rows[0].fact_refs[0].fact_id='другое';assert.equal(pendingChanges(payload,draft,head)[0].compatible,false);
});

test('team edit is transferred to a newer version and absent team stays absent',()=>{
 const payload:Payload={result_type:'analytical_note',content:{claims:[],missing_information:[],title:'Пример',team:'[TBD]',sections:[]}};
 const draft=structuredClone(payload),head=structuredClone(payload);
 draft.content.team='И. Иванов';head.content.title='Пример после правки';
 const [change]=pendingChanges(payload,draft,head);
 assert.equal(change.label,'Команда');assert.ok(change.compatible);
 const merged=structuredClone(head);change.apply(merged);
 assert.equal(merged.content.team,'И. Иванов');assert.equal(merged.content.title,'Пример после правки');
 const old:Payload={result_type:'analytical_note',content:{claims:[],missing_information:[],title:'Старая',sections:[]}};
 assert.deepEqual(pendingChanges(old,structuredClone(old),structuredClone(old)),[]);
});
