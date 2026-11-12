import test from 'node:test';
import assert from 'node:assert/strict';
import { Client, EventHistory, SseParser, ApiError, followEvents } from '../src/client';
import { Operations, clearSession, expireSession, restoreSession, readLocal, writeLocal } from '../src/local';

class MemoryStorage implements Storage {
  private values = new Map<string,string>();
  get length() { return this.values.size; }
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
  clear() { this.values.clear(); }
  key(i: number) { return [...this.values.keys()][i] ?? null; }
}
const project = {id:'project_test',name:'Synthetic project',created_by:'user_test',created_at:'2026-10-11T00:00:00Z'};
const event = (seq: number, run = 'run_test') => ({run_id:run,seq,type:'state_changed',timestamp:'2026-10-11T00:00:00Z',call_id:null,tool_call_id:null,data:{from:null,to:'queued'}});

test('editor keeps a full note above 16 KiB, with a bounded UTF-8 envelope',()=>{
  const storage=new MemoryStorage();
  const path='/v1/projects/project_test/artifacts/artifact_test/versions';
  const payload={text:'Я'.repeat(12000)};
  const ops=new Operations('editor.save',storage);
  const prepared=ops.prepare(path,payload,'VersionView','Сохранение');
  assert.ok(new TextEncoder().encode(prepared.body).length>16384);
  assert.deepEqual(new Operations('editor.save',storage).pending,prepared);
  const over=new Operations('editor.over',storage);
  assert.throws(()=>over.prepare(path,{text:'Я'.repeat(524288)},'VersionView','Сохранение'),e=>e instanceof ApiError && e.code==='too_large');
  assert.equal(over.pending,null);
  const ordinary=new Operations('other',storage);
  assert.throws(()=>ordinary.prepare('/v1/projects',payload,'Project','Создание'));
  assert.throws(()=>ordinary.prepare(path+'/accept',payload,'VersionView','Принятие'));
  assert.equal(ordinary.pending,null);
});

test('expired session preserves editor draft and exact pending save across authorized login',()=>{
  const storage=new MemoryStorage();writeLocal('session','expired',storage);writeLocal('project','project_owner',storage);
  const draft={payload:{text:'Несохраненная правка'},revision:3};writeLocal('editor.project_owner.version',draft,storage);
  const ops=new Operations('editor.project_owner.version.save',storage);
  const envelope=ops.prepare('/v1/projects/project_owner/artifacts/artifact/versions',draft,'VersionView','Сохранение');
  expireSession(storage);assert.equal(readLocal('session',null,storage),null);
  restoreSession('renewed',['project_owner'],storage);
  assert.deepEqual(readLocal('editor.project_owner.version',null,storage),draft);
  assert.deepEqual(new Operations('editor.project_owner.version.save',storage).pending,envelope);
  clearSession(storage);assert.equal(storage.length,0);
});

test('login without access to previous project clears its draft and pending request before workspace opens',()=>{
  const storage=new MemoryStorage();writeLocal('project','project_owner',storage);
  writeLocal('editor.project_owner.version',{text:'Частная правка'},storage);
  new Operations('editor.project_owner.version.save',storage).prepare('/private',{},'VersionView','Сохранение');
  expireSession(storage);restoreSession('other-session',['project_other'],storage);
  assert.equal(storage.length,1);assert.equal(readLocal('session',null,storage),'other-session');
});

test('lost acknowledgement and reload reuse exact body/key; next deliberate operation gets a new key', async () => {
  const old = globalThis.fetch; const storage = new MemoryStorage(); const captured: RequestInit[] = [];
  const client = new Client('synthetic-secret');
  try {
    globalThis.fetch = async (_url, init) => {captured.push(init!); if(captured.length === 1) throw new TypeError('lost reply'); return new Response(JSON.stringify(project));};
    const first = new Operations('submit.conv',storage);
    const prepared = first.prepare('/v1/projects', {name:'Original body',source_version_ids:['version_1']}, 'Project', 'Synthetic operation');
    await assert.rejects(first.send(client));
    const restored = new Operations('submit.conv',storage);
    assert.deepEqual(restored.prepare('/changed', {name:'Edited draft'}, 'Project', 'ignored'),prepared);
    await restored.send(client);
    assert.equal(captured[0].body,captured[1].body);
    assert.equal(new Headers(captured[0].headers).get('Idempotency-Key'),new Headers(captured[1].headers).get('Idempotency-Key'));
    assert.equal(restored.pending,null);
    assert.notEqual(restored.prepare('/v1/projects',{name:'New operation'},'Project','New').key,prepared.key);
    assert.ok(!JSON.stringify([...Array(storage.length)].map((_,i)=>storage.getItem(storage.key(i)!))).includes('synthetic-secret'));
  } finally {client.close(); globalThis.fetch=old;}
});
test('409 is a confirmed rejection, never retried automatically; new CAS is deliberate', async () => {
  const old=globalThis.fetch;const client=new Client('synthetic');const storage=new MemoryStorage();const ops=new Operations('submit',storage);let calls=0;
  try {
    globalThis.fetch=async()=>{calls++;return new Response(JSON.stringify({error:{code:'revision_conflict'}}),{status:409});};
    const oldKey=ops.prepare('/v1/projects', {expected_revision:0}, 'Project','test').key;
    await assert.rejects(ops.send(client),e=>e instanceof ApiError && e.status===409);
    assert.equal(ops.pending,null);assert.equal(calls,1);
    assert.notEqual(ops.prepare('/v1/projects',{expected_revision:2},'Project','test').key,oldKey);
  } finally{client.close();globalThis.fetch=old;}
});
test('storage failure prevents submission and simultaneous retries send once',async()=>{
  const old=globalThis.fetch;const storage=new MemoryStorage();const broken=new MemoryStorage();broken.setItem=()=>{throw new Error('quota');};
  assert.throws(()=>new Operations('submit',broken).prepare('/v1/projects',{},'Project','test'));
  const client=new Client('synthetic');const ops=new Operations('submit',storage);ops.prepare('/v1/projects',{},'Project','test');let finish!:()=>void;let count=0;
  try{
    globalThis.fetch=async()=>{count++;await new Promise<void>(resolve=>{finish=resolve;});return new Response(JSON.stringify(project));};
    const first=ops.send(client);await assert.rejects(ops.send(client),/in flight/);finish();await first;assert.equal(count,1);
  }finally{client.close();globalThis.fetch=old;}
});
test('SSE parses split frames, CRLF and heartbeat; reconnect deduplicates by contiguous seq',()=>{
  const parser=new SseParser();const frame=(seq:number)=>`id: run_test:${seq}\r\nevent: state_changed\r\ndata: ${JSON.stringify(event(seq))}\r\n\r\n`;
  const stream=': keepalive\r\n\r\n'+frame(1)+frame(2);let parsed: ReturnType<SseParser['feed']>=[];
  for(let i=0;i<stream.length;i+=7) parsed.push(...parser.feed(stream.slice(i,i+7)));
  const h=new EventHistory('run_test');h.add(parsed);h.add([event(2),event(3)]);assert.equal(h.seq,3);assert.equal(h.events.length,3);
  assert.throws(()=>h.add([event(5)]),/gap/);assert.equal(h.seq,3);
  assert.throws(()=>h.add([event(4,'run_other')]),/Foreign/);assert.equal(h.seq,3);
  h.add([event(4),event(5)]);assert.equal(h.seq,5);
});
test('context change aborts requests, drops late body, retains unresolved envelope only in old scope and clears session data',async()=>{
  const old=globalThis.fetch;const parent=new Client('synthetic');const child=parent.child();const storage=new MemoryStorage();const ops=new Operations('submit.old',storage);let finish!:()=>void;
  try{
    writeLocal('session','synthetic',storage);writeLocal('selection.project',{run:'run_old'},storage);storage.setItem('unrelated','keep');
    ops.prepare('/v1/projects',{},'Project','test');
    globalThis.fetch=async()=>{await new Promise<void>(resolve=>{finish=resolve;});return new Response(JSON.stringify(project));};
    const pending=ops.send(child);parent.close();clearSession(storage);finish();await assert.rejects(pending);
    assert.equal(child.signal.aborted,true);assert.equal(ops.pending,null);assert.equal(storage.length,1);assert.equal(storage.getItem('unrelated'),'keep');
    await assert.rejects(child.json('/v1/projects','Project'));
  }finally{parent.close();globalThis.fetch=old;}
});


test('network return interrupts a stale stream and recovers from last seq without a POST', async () => {
  const oldFetch = globalThis.fetch;
  const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const online = new EventTarget();
  Object.defineProperty(globalThis, 'window', { value: online, configurable: true, writable: true });
  const client = new Client('synthetic'); const history = new EventHistory('run_test');
  const paths: string[] = []; let connected!: () => void;
  const ready = new Promise<void>(resolve => { connected = resolve; });
  try {
    globalThis.fetch = async (url, init) => {
      paths.push(String(url));
      assert.ok(!init?.method || init.method === 'GET');
      if (String(url).includes('/events/stream')) {
        return new Response(new ReadableStream({start(controller) {
          init?.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted','AbortError')), {once:true});
        }}), {headers:{'Content-Type':'text/event-stream'}});
      }
      const seq = String(url).includes('after_seq=1') ? 2 : 1;
      return new Response(JSON.stringify({events:[event(seq)],next_after_seq:seq,has_more:false}));
    };
    const running = followEvents(client, '/v1/projects/project_test/runs/run_test', history, () => {if (history.seq === 2) client.close();}, value => {if(value) connected();});
    await ready; online.dispatchEvent(new Event('online'));
    await running;
    assert.deepEqual(history.events.map(e=>e.seq), [1,2]);
    assert.ok(paths.some(p=>p.endsWith('/events?after_seq=1&limit=100')));
  } finally {
    client.close(); globalThis.fetch = oldFetch;
    if (oldWindow) Object.defineProperty(globalThis,'window',oldWindow);
    else Reflect.deleteProperty(globalThis,'window');
  }
});

import {factAnchor} from '../src/types';
test('fact anchors distinguish identical fact IDs in separate PDF versions',()=>{
  assert.notEqual(factAnchor({source_version_id:'sv_a-fact_b',fact_id:'fact_c'}),factAnchor({source_version_id:'sv_a',fact_id:'fact_b-fact_c'}));
  assert.notEqual(factAnchor({source_version_id:'sv_first',fact_id:'same'}),factAnchor({source_version_id:'sv_second',fact_id:'same'}));
});

test('DRAFT-01: version state is derived from checks and acceptance; findings name their block', async () => {
  const { versionState, findingPlace, hasBlockingFindings, blockFindings, stateLabels } = await import('../src/review');
  const finding = { code: 'row_basis_mismatch', severity: 'error', block_id: 'block_table', message: 'строка 3 «Облигации» (row_bonds): Сумма 826 млн RUB и период «на 31.03.2023» не совпадают с основанием.' };
  const version = { payload: { content: { title: 'Записка', sections: [
    { section_id: 'sec_one', title: 'Финансовые показатели компании за первый квартал', blocks: [
      { block_id: 'block_text', kind: 'paragraph', claim: { text: 'Клиенты имеют незакредитованное транспортное средство; 30% предпринимателей.', origin: 'source_fact', fact_refs: [], calculation_ids: [] } },
      { block_id: 'block_table', kind: 'table', caption: 'Структура финансирования на 31.03.2023', rows: [] } ] } ] } } };
  const programmatic = (status: string, findings: typeof finding[]) => ({ id: 'check_p', kind: 'programmatic', status, findings });
  const semantic = { id: 'check_s', kind: 'semantic', status: 'not_checked', findings: [] };
  const draft = { version, review: { revision: 0, validation_ids: [], acceptance: null }, checks: [programmatic('failed', [finding]), semantic] } as any;
  const clean = { version, review: { revision: 0, validation_ids: [], acceptance: null }, checks: [programmatic('passed', []), semantic] } as any;
  const accepted = { ...clean, review: { revision: 1, validation_ids: [], acceptance: { id: 'accept_1', actor_id: 'usr_1', created_at: '2026-10-19T00:00:00Z', content_sha256: 'a'.repeat(64) } } };
  assert.equal(versionState(draft), 'draft_with_remarks');
  assert.equal(versionState(clean), 'unaccepted');
  assert.equal(versionState(accepted), 'accepted');
  // A warning-only finding does not block; a failed status blocks even without findings; an error finding blocks even with status passed.
  assert.equal(hasBlockingFindings({ checks: [programmatic('passed', [{ ...finding, severity: 'warning' }])] }), false);
  assert.equal(hasBlockingFindings({ checks: [programmatic('failed', [])] }), true);
  assert.equal(hasBlockingFindings({ checks: [programmatic('passed', [finding])] }), true);
  assert.ok(stateLabels.draft_with_remarks.includes('принять нельзя'));
  assert.equal(findingPlace(draft, 'block_table'), 'Раздел 1 «Финансовые показатели компании за первый…», блок 2, таблица «Структура финансирования на 31.03.2023»');
  assert.equal(findingPlace(draft, 'block_text'), 'Раздел 1 «Финансовые показатели компании за первый…», блок 1, абзац «Клиенты имеют незакредитованное транспор…»');
  assert.equal(findingPlace(draft, 'block_missing'), 'Блок block_missing');
  assert.equal(findingPlace(draft, null), '');
  assert.deepEqual(blockFindings(draft, 'block_table'), [finding]);
  assert.deepEqual(blockFindings(draft, 'block_text'), []);
});

test('password login sends exact password only in bounded POST and validates the session', async () => {
  const { loginWithPassword } = await import('../src/client');
  const old = globalThis.fetch;
  const password = '  e\u0301\r\nprivate-password  ';
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/v1/auth/login');
      assert.equal(init?.method, 'POST');
      assert.deepEqual(JSON.parse(String(init?.body)), {login:' Tester01 ', password});
      assert.equal(new Headers(init?.headers).get('Authorization'), null);
      return new Response(JSON.stringify({session_id:'ses_test',token:'aist_'+'a'.repeat(43),expires_at:'2026-11-01T00:00:00Z'}));
    };
    assert.equal(await loginWithPassword(' Tester01 ', password, new AbortController().signal), 'aist_'+'a'.repeat(43));
    globalThis.fetch = async () => new Response(JSON.stringify({error:{code:'invalid_credentials'}}),{status:401});
    await assert.rejects(loginWithPassword('tester01',password,new AbortController().signal), e=>e instanceof ApiError && e.code==='invalid_credentials');
  } finally { globalThis.fetch=old; }
});
