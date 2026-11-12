import test, { type TestContext } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import http from 'node:http';
import net from 'node:net';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
// @ts-expect-error Server is a runtime JavaScript module.
import { createWebServer } from '../server.mjs';

async function listen(server: http.Server) {
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  return (server.address() as net.AddressInfo).port;
}
async function close(server: http.Server) {
  await new Promise<void>(resolve => {
    server.close(() => resolve());
    server.closeAllConnections();
  });
}

async function startProxy(t: TestContext, handler: http.RequestListener, root?: string) {
  const upstream = http.createServer(handler);
  const upstreamPort = await listen(upstream);
  t.after(() => close(upstream));
  const web = createWebServer({ root, apiOrigin: `http://127.0.0.1:${upstreamPort}` }) as http.Server;
  const webPort = await listen(web);
  t.after(() => close(web));
  return { web, webPort, origin: `http://127.0.0.1:${webPort}` };
}

async function rawRequest(port: number, message: string) {
  const socket = net.createConnection(port, '127.0.0.1', () => socket.write(message));
  const closed = once(socket, 'close', { signal: AbortSignal.timeout(5000) });
  socket.setEncoding('utf8');
  let response = '';
  socket.on('data', data => { response += data; });
  try {
    await closed;
    return response;
  } finally {
    socket.destroy();
  }
}

test('F01/F02, fixed upstream, query/body/headers and client cancellation', async t => {
  const root = await mkdtemp(join(tmpdir(), 'aist-d06-web-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'index.html'), 'shell');
  let cancelled: (() => void) | undefined;
  const { webPort, origin } = await startProxy(t, async (req, res) => {
    if (req.url === '/partial') {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '1000' });
      res.write('{"status":');
      setTimeout(() => res.socket?.destroy(), 40);
    } else if (req.url === '/reset') res.destroy();
    else if (req.url === '/cancel') {
      res.writeHead(200); res.write('start');
      res.on('close', () => cancelled?.());
    } else {
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk as Buffer);
      res.setHeader('Content-Type', 'application/octet-stream');
      res.setHeader('ETag', '"sha256:test"');
      res.setHeader('Content-Disposition', 'attachment; filename="test.pdf"');
      res.end(JSON.stringify({ method:req.method, url:req.url, auth:req.headers.authorization,
        key:req.headers['idempotency-key'], last:req.headers['last-event-id'], type:req.headers['content-type'], body:Buffer.concat(chunks).toString('hex') }));
    }
  }, root);
  const raw = await rawRequest(webPort, 'GET //[ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n');
  assert.match(raw, /400 Bad Request/);
  assert.equal((await fetch(origin)).status, 200);
  const start = Date.now();
  await assert.rejects(async () => (await fetch(origin+'/api/partial', {signal:AbortSignal.timeout(9000)})).text());
  assert.ok(Date.now()-start<1500, 'Partial response must end before client timeout');
  assert.equal((await fetch(origin+'/api/reset')).status,502);
  const response = await fetch(origin+'/api/echo?limit=1&after_id=src_demo', {method:'POST',
    headers:{Authorization:'Bearer synthetic', 'Idempotency-Key':'key-demo','Content-Type':'multipart/form-data; boundary=demo','Last-Event-ID':'run_demo:17'},
    body:Buffer.from([0,1,2,255])});
  assert.equal(response.headers.get('etag'),'"sha256:test"');
  assert.equal(response.headers.get('cache-control'),'no-store');
  assert.deepEqual(await response.json(), {method:'POST',url:'/echo?limit=1&after_id=src_demo',
    auth:'Bearer synthetic',key:'key-demo',last:'run_demo:17',type:'multipart/form-data; boundary=demo',body:'000102ff'});
  const closed = new Promise<void>(resolve => { cancelled=resolve; });
  const controller = new AbortController();
  const pending = await fetch(origin+'/api/cancel', {signal:controller.signal});
  assert.equal(pending.status,200);
  controller.abort();
  await Promise.race([closed,new Promise((_,reject) => setTimeout(()=>reject(new Error('Upstream not cancelled')),1500))]);
  assert.equal((await fetch(origin)).status,200);
});

test('rejects unsupported methods and request framing before upstream', async t => {
  const upstreamRequests: string[] = [];
  const { web, webPort, origin } = await startProxy(t, (req, res) => {
    upstreamRequests.push(req.url ?? '');
    res.end('ready');
  });
  let dispatched = 0;
  web.on('request', () => { dispatched++; });
  const raw = (method: string, headers: string, body = '') =>
    rawRequest(webPort, `${method} /api/check HTTP/1.1\r\nHost: localhost\r\n${headers}\r\n\r\n${body}`);

  for (const method of ['GET', 'HEAD']) {
    for (const [headers, body] of [
      ['Content-Length: 1', 'x'],
      ['Transfer-Encoding: chunked', '1\r\nx\r\n0\r\n\r\n'],
    ]) {
      const before = dispatched;
      const response = await raw(method, headers, body);
      assert.match(response, /^HTTP\/1\.1 400 Bad Request\r\n/);
      assert.match(response, /\r\nConnection: close\r\n/i);
      assert.equal(dispatched, before + 1);
    }
  }
  const beforeUnsupported = dispatched;
  const unsupported = await raw('POST', 'Transfer-Encoding: gzip, chunked', '0\r\n\r\n');
  assert.match(unsupported, /^HTTP\/1\.1 400 Bad Request\r\n/);
  assert.match(unsupported, /invalid_request/);
  assert.equal(dispatched, beforeUnsupported + 1);

  for (const headers of [
    'Content-Length: 0\r\nTransfer-Encoding: chunked',
    'Content-Length: 0\r\nContent-Length: 1',
  ]) {
    const before = dispatched;
    const response = await raw('POST', headers);
    assert.match(response, /^HTTP\/1\.1 400 Bad Request\r\n/);
    assert.doesNotMatch(response, /invalid_request/);
    assert.equal(dispatched, before);
  }
  for (const method of ['PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE']) {
    const response = await raw(method, 'Content-Length: 0');
    assert.match(response, /^HTTP\/1\.1 405 Method Not Allowed\r\n/);
    assert.match(response, /\r\nAllow: GET, HEAD, POST\r\n/i);
  }
  assert.deepEqual(upstreamRequests, []);
  assert.equal(await (await fetch(origin + '/api/fresh')).text(), 'ready');
  assert.deepEqual(upstreamRequests, ['/fresh']);
});

test('rejects pipelined requests after an invalid request on the same TCP connection', { timeout: 15_000 }, async t => {
  const upstreamRequests: string[] = [];
  const { webPort, origin } = await startProxy(t, (req, res) => {
    upstreamRequests.push(req.url ?? '');
    res.end('ready:' + req.url);
  });
  const nextRequest = (path: string) => `GET /api/${path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n`;
  const allowed = await rawRequest(webPort,
    'GET /api/allowed-first HTTP/1.1\r\nHost: localhost\r\n\r\n' + nextRequest('allowed-second'));
  assert.equal((allowed.match(/HTTP\/1\.1 200 OK\r\n/g) ?? []).length, 2);
  assert.match(allowed, /ready:\/allowed-first/);
  assert.match(allowed, /ready:\/allowed-second/);
  assert.deepEqual(upstreamRequests, ['/allowed-first', '/allowed-second']);
  upstreamRequests.length = 0;

  const cases = [
    { method: 'GET', headers: 'Content-Length: 1', body: 'x', status: 400 },
    { method: 'HEAD', headers: 'Content-Length: 1', body: 'x', status: 400 },
    { method: 'GET', headers: 'Transfer-Encoding: chunked', body: '1\r\nx\r\n0\r\n\r\n', status: 400 },
    { method: 'HEAD', headers: 'Transfer-Encoding: chunked', body: '1\r\nx\r\n0\r\n\r\n', status: 400 },
    { method: 'POST', headers: 'Transfer-Encoding: gzip, chunked', body: '0\r\n\r\n', status: 400 },
    { method: 'PUT', headers: 'Content-Length: 1', body: 'x', status: 405 },
  ];
  for (const [index, { method, headers, body, status }] of cases.entries()) {
    for (const precedingRequest of [false, true]) {
      // An earlier upstream response can keep the rejection queued while Node parses more requests.
      const prefix = precedingRequest ? 'GET /api/before-rejection HTTP/1.1\r\nHost: localhost\r\n\r\n' : '';
      const response = await rawRequest(webPort, prefix +
        `${method} /api/rejected-${index} HTTP/1.1\r\nHost: localhost\r\n${headers}\r\n\r\n${body}` + nextRequest(`ignored-${index}`));
      const statuses = [...response.matchAll(/HTTP\/1\.1 (\d{3}) /g)].map(match => Number(match[1]));
      assert.deepEqual(statuses, precedingRequest ? [200, status] : [status]);
      assert.match(response, /\r\nConnection: close\r\n/i);
      assert.doesNotMatch(response, /ready:\/(rejected|ignored)/);
      assert.equal(await (await fetch(origin + '/api/fresh-' + index)).text(), 'ready:/fresh-' + index);
      assert.deepEqual(upstreamRequests, [...(precedingRequest ? ['/before-rejection'] : []), '/fresh-' + index]);
      upstreamRequests.length = 0;
    }
  }
});

test('sequential and concurrent clients use separate upstream connections', async t => {
  const sockets = new Set<net.Socket>();
  const closed: Promise<unknown>[] = [];
  const observed: string[] = [];
  const { origin } = await startProxy(t, (req, res) => {
    if (!sockets.has(req.socket)) {
      sockets.add(req.socket);
      closed.push(once(req.socket, 'close'));
    }
    observed.push(`${req.method} ${req.url} ${req.headers.authorization}`);
    res.writeHead(200, { 'Content-Length': Buffer.byteLength(req.url ?? '') });
    res.end(req.url);
  });
  const client = new http.Agent({ keepAlive: true, maxSockets: 1 });
  t.after(() => client.destroy());
  const request = (method: string, id: string) => new Promise<string>((resolve, reject) => {
    const outgoing = http.request(origin + '/api/check?id=' + id, {
      method, agent: client, headers: { Authorization: 'Bearer synthetic-' + id, 'Content-Length': '0' },
    }, async response => {
      try {
        assert.equal(response.statusCode, 200);
        assert.equal(response.headers['content-length'], String(Buffer.byteLength('/check?id=' + id)));
        const chunks: Buffer[] = [];
        for await (const chunk of response) chunks.push(chunk as Buffer);
        resolve(Buffer.concat(chunks).toString());
      } catch (error) { reject(error); }
    });
    outgoing.on('error', reject);
    outgoing.end();
  });
  assert.equal(await request('GET', 'first'), '/check?id=first');
  assert.equal(await request('HEAD', 'second'), '');
  const ids = ['third', 'fourth', 'fifth'];
  const concurrent = await Promise.all(ids.map(async id => {
    const response = await fetch(origin + '/api/check?id=' + id, {
      headers: { Authorization: 'Bearer synthetic-' + id },
    });
    assert.equal(response.status, 200);
    return response.text();
  }));
  assert.deepEqual(concurrent, ids.map(id => '/check?id=' + id));
  assert.deepEqual(observed.sort(), [
    'GET /check?id=first Bearer synthetic-first',
    'HEAD /check?id=second Bearer synthetic-second',
    ...ids.map(id => `GET /check?id=${id} Bearer synthetic-${id}`),
  ].sort());
  assert.equal(sockets.size, 5);
  await Promise.all(closed);
});

test('POST framing preserves JSON, multipart PDF and chunked bytes, and downloads preserve headers', async t => {
  const pdf = Buffer.concat([Buffer.from('%PDF-1.4\n'), Buffer.from([0, 1, 254, 255])]);
  const docx = Buffer.from([0x50, 0x4b, 3, 4, 255, 0, 16]);
  const downloads = [
    { ext: 'pdf', type: 'application/pdf', bytes: pdf },
    { ext: 'docx', type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', bytes: docx },
  ];
  const { origin } = await startProxy(t, async (req, res) => {
    const download = downloads.find(file => req.url === '/download/' + file.ext);
    if (download) {
      res.writeHead(200, {
        'Content-Type': download.type, 'Content-Length': download.bytes.length,
        'Content-Disposition': `attachment; filename="test.${download.ext}"`, ETag: '"sha256:test"',
      });
      res.end(download.bytes);
      return;
    }
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({
      bytes: Buffer.concat(chunks).toString('hex'), type: req.headers['content-type'],
      length: req.headers['content-length'], encoding: req.headers['transfer-encoding'],
    }));
  });
  const json = Buffer.from(JSON.stringify({ message: 'Тест' }));
  const multipart = Buffer.concat([
    Buffer.from('--demo\r\nContent-Disposition: form-data; name="file"; filename="test.pdf"\r\nContent-Type: application/pdf\r\n\r\n'),
    pdf, Buffer.from('\r\n--demo--\r\n'),
  ]);
  for (const [type, body] of [
    ['application/json', json],
    ['multipart/form-data; boundary=demo', multipart],
  ] as const) {
    const response = await fetch(origin + '/api/echo', { method: 'POST', headers: { 'Content-Type': type }, body });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { bytes: body.toString('hex'), type, length: String(body.length) });
  }
  const chunked = await new Promise<unknown>((resolve, reject) => {
    const request = http.request(origin + '/api/echo', { method: 'POST' }, async response => {
      try {
        assert.equal(response.statusCode, 200);
        const chunks: Buffer[] = [];
        for await (const chunk of response) chunks.push(chunk as Buffer);
        resolve(JSON.parse(Buffer.concat(chunks).toString()));
      } catch (error) { reject(error); }
    });
    request.on('error', reject);
    request.write(pdf.subarray(0, 4));
    request.end(pdf.subarray(4));
  });
  assert.deepEqual(chunked, { bytes: pdf.toString('hex'), encoding: 'chunked' });
  for (const { ext, type, bytes } of downloads) {
    const response = await fetch(origin + '/api/download/' + ext);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), type);
    assert.equal(response.headers.get('content-length'), String(bytes.length));
    assert.equal(response.headers.get('content-disposition'), `attachment; filename="test.${ext}"`);
    assert.equal(response.headers.get('etag'), '"sha256:test"');
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), bytes);
  }
});

test('active SSE survives the 60-second deadline and cancels upstream', { timeout: 75_000 }, async t => {
  let resumeFrom: string | undefined;
  let accept: string | undefined;
  let upstreamClosed: Promise<unknown> | undefined;
  const { origin } = await startProxy(t, (req, res) => {
    resumeFrom = req.headers['last-event-id'] as string | undefined;
    accept = req.headers.accept;
    upstreamClosed = once(req.socket, 'close');
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
    res.write('id: run_demo:18\ndata: started\n\n');
    const heartbeat = setInterval(() => res.write(': heartbeat\n\n'), 5000);
    const nextEvent = setTimeout(() => res.write('id: run_demo:19\ndata: active\n\n'), 61_500);
    res.once('close', () => {
      clearInterval(heartbeat);
      clearTimeout(nextEvent);
    });
  });
  const controller = new AbortController();
  t.after(() => controller.abort());
  const started = performance.now();
  const response = await fetch(origin + '/api/events', {
    signal: controller.signal, headers: { 'Last-Event-ID': 'run_demo:17', Accept: 'text/event-stream' },
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-accel-buffering'), 'no');
  assert.equal(resumeFrom, 'run_demo:17');
  assert.equal(accept, 'text/event-stream');
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let events = '';
  while (!events.includes('id: run_demo:19\ndata: active\n\n')) {
    const chunk = await reader.read();
    assert.equal(chunk.done, false);
    events += decoder.decode(chunk.value, { stream: true });
  }
  const elapsed = performance.now() - started;
  assert.ok(elapsed > 60_000);
  assert.ok(events.startsWith('id: run_demo:18\ndata: started\n\n'));
  assert.ok((events.match(/: heartbeat\n\n/g) ?? []).length >= 6);
  t.diagnostic(`SSE delivered after ${Math.round(elapsed)} ms`);
  await reader.cancel();
  assert.ok(upstreamClosed);
  const timeout = AbortSignal.timeout(1500);
  await Promise.race([upstreamClosed, once(timeout, 'abort').then(() => assert.fail('Upstream socket still open'))]);
});
