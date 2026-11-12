import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import entities from '../../../contracts/v1/examples/entities.json';
import http from '../../../contracts/v1/http.json';
import { contractIdentity, serializeDefinition, validateDefinition, versionLookup, object, type Json } from '../src/contracts';

test('external paragraph preserves evidence IDs and cannot use financial references', () => {
  const claim = {text:'Опубликованные сведения о рынке.', origin:'external_context',
    fact_refs:[], calculation_ids:[], external_evidence_ids:['ext_public']};
  assert.deepEqual(JSON.parse(serializeDefinition('Claim', claim)), claim);
  assert.throws(()=>validateDefinition('Claim', {...claim, external_evidence_ids:[]}));
  assert.throws(()=>validateDefinition('Claim', {...claim, calculation_ids:['calc_private']}));
  assert.throws(()=>validateDefinition('Claim', {...claim, origin:'source_fact'}));
});

const records = entities.records;

test('Amount preserves trillion and previous scales without converting Decimal strings', () => {
  for (const scale of ['1','1000','1000000','1000000000','1000000000000']) {
    const amount={value:'2.200000000000000001',unit:'money',scale,currency:'RUB'};
    assert.deepEqual(JSON.parse(serializeDefinition('Amount',amount)),amount);
  }
  assert.throws(()=>validateDefinition('Amount',{value:'2.2',unit:'money',scale:'1000000000000000',currency:'RUB'}));
  assert.throws(()=>validateDefinition('Amount',{value:'2.2',unit:'percent',scale:'1000000000000',currency:null}));
});

test('optional note questions roundtrip without extending evidence answers', () => {
  const args = {idempotency_key:'save_questions', expected_revision:0, artifact_id:null, parent_version_id:null,
    payload:records.note_v1_view.value.version.payload};
  const questions=['Уточните определение показателя 2024 года.', 'Каковы актуальные условия сделки?'];
  assert.deepEqual(JSON.parse(serializeDefinition('SaveArtifactArgs', args)), args);
  assert.deepEqual(JSON.parse(serializeDefinition('SaveArtifactArgs', {...args,questions})), {...args,questions});
  for(const invalid of [null, ['x'.repeat(1001)], Array(6).fill('Вопрос'), [''], [1]])
    assert.throws(()=>validateDefinition('SaveArtifactArgs',{...args,questions:invalid}));
  assert.throws(()=>validateDefinition('SaveArtifactArgs',{
    ...args,questions,payload:records.answer_view.value.version.payload}));
});

test('D04-F01 navigates from Run, Message and result_saved without artifact_id', () => {
  const run = records.question_completed.value;
  assert.equal(run.input.target_artifact_id, null);
  const expected = versionLookup(run.project_id, run.result_version_id);
  assert.equal(expected, '/v1/projects/prj_demo/artifact-versions/av_answer-v1');
  const message = records.question_messages.value.items.find(m => m.artifact_version_id !== null)!;
  assert.equal(versionLookup(message.project_id, message.artifact_version_id), expected);
  const event = records.sse_event.value;
  assert.equal(event.run_id, run.id);
  assert.equal(versionLookup(run.project_id, event.data.artifact_version_id), expected);
  assert.equal(records.answer_view.value.version.artifact_id, records.artifact_answer.value.id);
  assert.deepEqual(http.operations.getVersionById.responses, http.operations.getVersion.responses);
});

test('historical result stays selected after the Artifact head changes', () => {
  const run = records.note_completed.value;
  assert.notEqual(run.result_version_id, records.artifact_note_v3.value.latest_version_id);
  assert.equal(versionLookup(run.project_id, run.result_version_id), '/v1/projects/prj_demo/artifact-versions/av_note-v1');
});

test('schema adapter preserves Decimal strings, null and IDs and rejects JSON money numbers', () => {
  const view = records.answer_view.value;
  assert.deepEqual(JSON.parse(serializeDefinition('VersionView', view)), view);
  assert.equal(view.review.acceptance, null);
  const mutated = JSON.parse(JSON.stringify(view)) as Json;
  function mutate(value: Json): boolean {
    if (value && typeof value === 'object' && !Array.isArray(value) && value.value === records.growth.value.rounded.value) { value.value = Number(value.value); return true; }
    return !!value && typeof value === 'object' && Object.values(value).some(item => mutate(item));
  }
  assert.ok(mutate(mutated));
  assert.throws(() => validateDefinition('VersionView', mutated));
  assert.throws(() => validateDefinition('PathProjectVersion', { project_id: 'prj_demo', version_id: 'av_note-v1', artifact_id: 'art_note' }));
  assert.equal(object(validateDefinition('VersionView', view)).review !== null, true);
});

test('frontend identity matches the actual shared schema bytes', () => {
  const bytes = readFileSync(new URL('../../../contracts/v1/schemas/aist.schema.json', import.meta.url));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), contractIdentity.schema_sha256);
  assert.equal(http.contract_version, contractIdentity.contract_version);
});

test('JSON snapshots preserve section locators and never expose a fictional PDF page', () => {
  const record = records.source_ready;
  const version = {...record.value, media_type:'application/json', page_count:null};
  assert.deepEqual(JSON.parse(serializeDefinition('SourceVersion', version)), version);
  assert.throws(() => validateDefinition('SourceVersion', {...version,page_count:1}));
  const locator = {json_pointer:'/normalized/price',area:'Цена закрытия'};
  assert.deepEqual(JSON.parse(serializeDefinition('Locator',locator)),locator);
  assert.throws(() => validateDefinition('Locator',{...locator,pdf_page:1}));
});

test('external materials keep search targets separate from legacy subjects', () => {
  const item = {evidence_id:'ext_material',search_target:'Компания',search_role:'company',topics:['owners'],
    url:'https://public.example/article',publisher:'public.example',title:'Описание',published:null,
    retrieved_at:'2026-11-08T12:00:00Z',page_sha256:'a'.repeat(64),quote:'Сохраненный материал для анализа. Принадлежность сведений и их актуальность оценивает читатель.',source_kind:'publication'};
  const context = {collection_version:'materials.v1',company:'Компания',as_of:'2026-11-08',status:'collected',
    retrieved_at:'2026-11-08T12:00:00Z',items:[item],topics:{owners:'collected'},requests:1,bytes_read:1000,elapsed_seconds:1};
  assert.deepEqual(JSON.parse(serializeDefinition('ExternalContext', context)), context);
  assert.throws(()=>validateDefinition('ExternalContext',{...context,items:[{...item,subject:'Компания'}]}));
  assert.throws(()=>validateDefinition('ExternalContext',{...context,topics:{owners:'found'}}));
  const {search_target,search_role,...legacyItem}=item;
  const {collection_version,...legacyContext}=context;
  const legacy={...legacyContext,status:'found',topics:{owners:'found'},items:[{...legacyItem,subject:search_target}]};
  assert.deepEqual(JSON.parse(serializeDefinition('ExternalContext',legacy)),legacy);
});

test('an analytical note can use a project PDF while chat-only context still needs a PDF', () => {
  const message = {expected_revision:0,text:'Подготовь записку',source_version_ids:[],
    skill_version_id:'skill_analytical-note-v3',model_profile_version_id:'model_novita-mimo-v1',
    requested_result_type:'analytical_note',parameters:{audience:null,sections:[]},
    base_version_id:null,expected_artifact_revision:0,requested_export:null};
  const context={folder_id:'prj_project',pdf_version_ids:['sv_public'],chats:[]};
  const selected={...message,project_context:context};
  assert.deepEqual(validateDefinition('CreateMessageRun',selected),selected);
  assert.throws(()=>validateDefinition('CreateMessageRun',message));
  assert.throws(()=>validateDefinition('CreateMessageRun',{...message,project_context:{...context,pdf_version_ids:[],chats:[{id:'conv_other',through_sequence:1}]}}));
});


test('imported provenance roundtrips without changing existing origins', () => {
  const source = records.answer_view.value.version.provenance;
  for (const origin of ['generation','human_edit','model_edit','conversion','recovered_extracts','import']) {
    const value = {...source, origin, derived_from_version_id: origin === 'import' ? 'av_original' : null};
    assert.deepEqual(JSON.parse(serializeDefinition('Provenance', value)), value);
  }
  assert.throws(() => validateDefinition('Provenance', {...source,origin:'unknown_import'}));
});

test('selected news events preserve nullable dates and original metadata', () => {
  const event={external_evidence_id:'ext_news',description:'Компания открыла предприятие.',published:null,event_date:null,date_quote:''};
  const args={idempotency_key:'selected_news',expected_revision:0,artifact_id:null,parent_version_id:null,
    payload:records.note_v1_view.value.version.payload,news_events:[event]};
  assert.deepEqual(JSON.parse(serializeDefinition('SaveArtifactArgs',args)),args);
  assert.throws(()=>validateDefinition('SaveArtifactArgs',{...args,payload:records.answer_view.value.version.payload}));
  assert.throws(()=>validateDefinition('NewsEvent',{...event,published:'2025-08-01'}));
  const item={external_evidence_id:'ext_news',quote:event.description,published:null,event_date:null,date_quote:'',
    metadata_published:'2026-02-24',retrieved_at:'2026-11-09T00:00:00Z',url:'https://public.example/event',publisher:'Компания',page_sha256:'b'.repeat(64)};
  assert.deepEqual(JSON.parse(serializeDefinition('NewsItem',item)),item);
  const legacy={quote:'Историческая новость',published:'2025-08-01',url:'https://www.interfax.ru/business/123456',publisher:item.publisher,page_sha256:item.page_sha256};
  assert.deepEqual(JSON.parse(serializeDefinition('NewsItem',legacy)),legacy);
  assert.throws(()=>validateDefinition('NewsItem',{...legacy,published:null}));
});
