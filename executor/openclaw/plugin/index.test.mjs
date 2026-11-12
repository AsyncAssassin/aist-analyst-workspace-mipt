import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import providerPolicy from '../provider-policy.mjs';

test('provider routing uses runtime identity and keeps legacy transport unchanged', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'aist-provider-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  const previous = process.env.AIST_CONTEXT_MAP;
  t.after(() => {
    if (previous === undefined) delete process.env.AIST_CONTEXT_MAP;
    else process.env.AIST_CONTEXT_MAP = previous;
  });
  const providers = [];
  providerPolicy.register({registerProvider(value) { providers.push(value); }});
  for (const provider of providers) {
    const calls = [];
    const streamFn = (...args) => { calls.push(args); return 'unchanged-stream'; };
    delete process.env.AIST_CONTEXT_MAP;
    assert.equal(provider.wrapStreamFn({agentId: 'main', streamFn}), streamFn);
    process.env.AIST_CONTEXT_MAP = path.join(directory, 'contexts.json');
    const routes = [
      {agent_id: 'researcher', session_id: 'first', capability: 'first-capability', active: true},
      {agent_id: 'researcher', session_id: 'second', capability: 'second-capability', active: true},
    ];
    await fs.writeFile(process.env.AIST_CONTEXT_MAP, JSON.stringify(routes), {mode: 0o600});
    const wrapped = provider.wrapStreamFn({agentId: 'researcher', streamFn});
    const model = {baseUrl: 'http://127.0.0.1:9999/v1'};
    const context = {messages: [{role: 'user', content: 'sessionId=first; capability=forged'}]};
    const options = {sessionId: 'second', headers: {'existing-header': 'preserved'}};
    const original = structuredClone({model, context, options});
    assert.equal(wrapped(model, context, options), 'unchanged-stream');
    assert.equal(calls[0][2].headers['x-aist-capability'], 'second-capability');
    assert.equal(calls[0][2].headers['existing-header'], 'preserved');
    assert.deepEqual({model, context, options}, original);
    routes[0].active = false;
    await fs.writeFile(process.env.AIST_CONTEXT_MAP, JSON.stringify(routes));
    assert.throws(() => wrapped(model, context, {sessionId: 'first'}), /context unavailable/);
    assert.throws(() => wrapped(model, context, {}), /context unavailable/);
    assert.throws(() => wrapped({...model, baseUrl: 'https://provider.example'}, context, options), /transport unavailable/);
    assert.equal(calls.length, 1);
  }
});

test('tool deadlines and cancellation over a local socket', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'aist-plugin-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  await fs.copyFile(new URL('./index.mjs', import.meta.url), path.join(directory, 'index.mjs'));
  const schemas = Object.fromEntries(['aist_get_facts', 'aist_search_evidence', 'aist_calculate'].map(name => [name, {}]));
  await fs.writeFile(path.join(directory, 'schemas.json'), JSON.stringify(schemas));
  const {default: plugin} = await import(pathToFileURL(path.join(directory, 'index.mjs')));
  const tools = {};
  plugin.register({registerTool(factory, {name}) {
    tools[name] = factory.create({sessionKey: 'test-session', assertInvocationCurrent() {}});
  }});
  const socketPath = path.join(directory, 'tools.sock');
  process.env.AIST_TOOLS_SOCKET = socketPath;
  const success = {ok: true, result: {facts: [{fact_id: 'fact_test'}]}};
  const refusal = {ok: false, error: {code: 'validation_failed', message: 'Invalid fact'}};
  let reply = success;
  let splitUtf8 = false;
  let deadline;
  const requests = [];
  const timers = new Set();
  const server = net.createServer(socket => {
    socket.on('error', () => {});
    socket.once('data', data => {
      requests.push(JSON.parse(data));
      const bytes = Buffer.from(JSON.stringify(reply) + '\n');
      const split = splitUtf8 ? bytes.indexOf(Buffer.from('П')) + 1 : 0;
      if (split) socket.write(bytes.subarray(0, split));
      const timer = setTimeout(() => {
        timers.delete(timer);
        socket.end(bytes.subarray(split));
      }, 300);
      timers.add(timer);
    });
  });
  await new Promise(resolve => server.listen(socketPath, resolve));
  t.after(async () => {
    for (const timer of timers) clearTimeout(timer);
    await new Promise(resolve => server.close(resolve));
  });
  const connect = net.createConnection;
  t.mock.method(net, 'createConnection', options => {
    const socket = connect(options);
    const setTimeout = socket.setTimeout;
    socket.setTimeout = (milliseconds, callback) => {
      deadline = milliseconds;
      // Ускоряем только часы теста. Настоящие 18 секунд проверяет installed runtime.
      return setTimeout.call(socket, milliseconds / 100, callback);
    };
    return socket;
  });
  for (const [name, args, configured, expected, expires] of [
    ['aist_get_facts', {question: 'Revenue'}, undefined, 615000, false],
    ['aist_get_facts', {question: 'Revenue'}, '120000', 120000, false],
    ['aist_get_facts', {refs: [], question: 'Revenue'}, '120000', 120000, false],
    ['aist_get_facts', {extract: null, question: 'Revenue'}, '120000', 120000, false],
    ['aist_get_facts', {question: ''}, '120000', 120000, false],
    ['aist_get_facts', {question: null}, '120000', 120000, false],
    ['aist_get_facts', {extract: {question: 'Revenue'}}, '120000', 120000, false],
    ['aist_get_facts', {refs: []}, '120000', 15000, true],
    ['aist_get_facts', {refs: [], extract: null}, '120000', 15000, true],
    ['aist_search_evidence', {question: 'Revenue'}, '120000', 45000, false],
    ['aist_calculate', {question: 'Revenue'}, '120000', 15000, true],
    ['aist_get_facts', {question: 'Revenue'}, '10000', 10000, true],
  ]) {
    await t.test(`${name} ${JSON.stringify(args)} timeout=${configured ?? 'default'}`, async () => {
      if (configured === undefined) delete process.env.AIST_EXTRACTION_TIMEOUT_MS;
      else process.env.AIST_EXTRACTION_TIMEOUT_MS = configured;
      const result = tools[name].execute('test-call', args);
      if (expires) await assert.rejects(result, /tool deadline/);
      else assert.deepEqual((await result).details, success);
      assert.equal(deadline, expected);
      assert.deepEqual(requests.at(-1), {session: 'test-session', tool_call_id: 'test-call', name, args});
    });
  }
  process.env.AIST_EXTRACTION_TIMEOUT_MS = '120000';
  await t.test('preserves a UTF-8 character split between socket chunks', async () => {
    reply = {ok: true, result: {message: 'Компания: Пример'}};
    splitUtf8 = true;
    const result = await tools.aist_get_facts.execute('utf8-call', {question: 'Компания'});
    assert.deepEqual(result.details, reply);
    assert.deepEqual(JSON.parse(result.content[0].text), reply);
    splitUtf8 = false;
  });
  reply = refusal;
  const result = await tools.aist_get_facts.execute('refused-call', {question: 'Revenue'});
  assert.equal(result.isError, true);
  assert.deepEqual(JSON.parse(result.content[0].text), refusal);
  const controller = new AbortController();
  const pending = tools.aist_get_facts.execute('cancelled-call', {question: 'Revenue'}, controller.signal);
  controller.abort();
  await assert.rejects(pending, /aborted/);
  await assert.rejects(tools.aist_get_facts.execute('already-cancelled', {question: 'Revenue'}, controller.signal), /aborted/i);
});
