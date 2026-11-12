import test from 'node:test';
import assert from 'node:assert/strict';
import {readPublishedExports} from '../src/export-publication';

test('publication checkpoint is read through to the same ready file', async () => {
  const states = ['recovery_required', 'recovery_required', 'ready'];
  let reads = 0;
  const rows = await readPublishedExports(async () => [{id: 'export1', state: states[reads++]}],
    new AbortController().signal, async () => {});
  assert.deepEqual(rows, [{id: 'export1', state: 'ready'}]);
  assert.equal(reads, 3);
});

test('a durable unknown stays unknown after bounded reads', async () => {
  let reads = 0;
  const rows = await readPublishedExports(async () => { reads++; return [{state: 'recovery_required'}]; },
    new AbortController().signal, async () => {});
  assert.equal(rows[0].state, 'recovery_required');
  assert.equal(reads, 6);
});

test('leaving the version prevents further reads during publication', async () => {
  const controller = new AbortController(); let reads = 0;
  await assert.rejects(readPublishedExports(async () => { reads++; return [{state: 'recovery_required'}]; },
    controller.signal, async () => controller.abort()), {name: 'AbortError'});
  assert.equal(reads, 1);
});

test('ready and failed files are returned immediately without retry', async () => {
  for (const state of ['ready', 'failed', 'missing', 'corrupt']) {
    const rows = await readPublishedExports(async () => [{state}], new AbortController().signal,
      async () => {throw Error('Unexpected delay');});
    assert.equal(rows[0].state, state);
  }
});
