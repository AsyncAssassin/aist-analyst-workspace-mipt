import test from 'node:test';
import assert from 'node:assert/strict';
import { checkConnection } from '../src/api';
import { contractIdentity } from '../src/contracts';

test('connection handles readiness, mismatch and transport failure', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ ...contractIdentity, status: 'ready' }));
    assert.equal((await checkConnection()).state, 'ready');
    globalThis.fetch = async () => new Response(JSON.stringify({ ...contractIdentity, status: 'not_ready', reason: 'database_unavailable' }), { status: 503 });
    assert.equal((await checkConnection()).state, 'not_ready');
    globalThis.fetch = async () => new Response(JSON.stringify({ ...contractIdentity, schema_sha256: 'other', status: 'ready' }));
    assert.equal((await checkConnection()).state, 'unavailable');
    globalThis.fetch = async () => new Response('{\"status\":\"unavailable\"}', { status: 502 });
    assert.match((await checkConnection()).message, /Нет соединения/);
    globalThis.fetch = async () => { throw new TypeError('Connection refused'); };
    assert.equal((await checkConnection()).state, 'unavailable');
  } finally { globalThis.fetch = original; }
});
