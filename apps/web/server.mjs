import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const apiMethods = ['GET', 'HEAD', 'POST'];
const requestHeaders = ['authorization', 'idempotency-key', 'content-type', 'content-length', 'accept', 'last-event-id'];
const responseHeaders = ['content-type', 'content-length', 'content-disposition', 'etag', 'retry-after'];

export function createWebServer({ root = resolve('dist'), apiOrigin = process.env.AIST_API_ORIGIN ?? 'http://backend:8000' } = {}) {
  const upstream = new URL(apiOrigin);
  if (upstream.protocol !== 'http:' || upstream.username || upstream.password) throw new Error('Invalid API origin configuration');
  const rejectedSockets = new WeakSet();
  const rejectRequest = (req, res, status) => {
    rejectedSockets.add(req.socket);
    const headers = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'Connection': 'close' };
    if (status === 405) headers.Allow = apiMethods.join(', ');
    res.writeHead(status, headers);
    res.end('{"status":"invalid_request"}');
  };
  return http.createServer(async (req, res) => {
    // Node can dispatch another request from the same read before the close response flushes.
    if (rejectedSockets.has(req.socket)) return;
    res.setHeader('X-Content-Type-Options', 'nosniff');
    const readOnly = req.method === 'GET' || req.method === 'HEAD';
    const length = req.headers['content-length'];
    const encoding = req.headers['transfer-encoding'];
    if (readOnly && (encoding !== undefined || (length !== undefined && !/^0+$/.test(length)))) {
      rejectRequest(req, res, 400);
      return;
    }
    let target;
    try {
      if (!req.url?.startsWith('/') || req.url.startsWith('//')) throw new Error('Invalid target');
      target = new URL(req.url, 'http://localhost');
    } catch {
      res.writeHead(400, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      res.end('{"status":"invalid_request"}');
      return;
    }
    if (target.pathname.startsWith('/api/')) {
      if (!apiMethods.includes(req.method)) {
        rejectRequest(req, res, 405);
        return;
      }
      if (encoding !== undefined && encoding.trim().toLowerCase() !== 'chunked') {
        rejectRequest(req, res, 400);
        return;
      }
      const headers = {};
      for (const name of requestHeaders) if (req.headers[name] !== undefined) headers[name] = req.headers[name];
      // IncomingMessage decodes chunk framing. ClientRequest writes new chunk framing.
      if (encoding !== undefined) headers['transfer-encoding'] = 'chunked';
      const url = new URL(upstream);
      url.pathname = target.pathname.slice(4);
      url.search = target.search;
      let reply;
      let finished = false;
      const fail = () => {
        if (finished) return;
        finished = true;
        clearTimeout(deadline);
        reply?.destroy();
        proxy.destroy();
        if (res.destroyed) return;
        if (res.headersSent) res.destroy();
        else {
          res.writeHead(502, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
          res.end('{"status":"unavailable"}');
        }
      };
      const proxy = http.request(url, { method: req.method, headers, agent: false }, incoming => {
        reply = incoming;
        if (res.destroyed) { incoming.destroy(); proxy.destroy(); return; }
        const returned = { 'Cache-Control': 'no-store' };
        for (const name of responseHeaders) if (incoming.headers[name] !== undefined) returned[name] = incoming.headers[name];
        if (incoming.statusCode === 200 && incoming.headers['content-type']?.startsWith('text/event-stream')) {
          clearTimeout(deadline);
          proxy.setTimeout(15000, fail);
          res.setHeader('X-Accel-Buffering', 'no');
        }
        res.writeHead(incoming.statusCode ?? 502, returned);
        incoming.on('aborted', fail);
        incoming.on('error', fail);
        incoming.on('close', () => { if (!incoming.complete) fail(); });
        incoming.on('end', () => { finished = true; clearTimeout(deadline); });
        incoming.pipe(res);
      });
      const timeout = req.method === 'POST' && req.headers['content-type']?.startsWith('multipart/form-data') ? 170000 : 60000;
      const deadline = setTimeout(fail, timeout);
      proxy.on('error', fail);
      proxy.setTimeout(timeout, fail);
      req.on('aborted', fail);
      req.on('error', fail);
      res.on('error', fail);
      res.on('close', () => {
        clearTimeout(deadline);
        if (!res.writableFinished) {
          finished = true;
          reply?.destroy();
          proxy.destroy();
        }
      });
      if (readOnly) proxy.end();
      else req.pipe(proxy);
      return;
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405); res.end(); return; }
    try {
      const file = resolve(root, '.' + decodeURIComponent(target.pathname === '/' ? '/index.html' : target.pathname));
      if (!file.startsWith(root + sep)) { res.writeHead(404); res.end(); return; }
      const bytes = await readFile(file);
      res.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-cache' });
      res.end(req.method === 'HEAD' ? undefined : bytes);
    } catch { res.writeHead(404); res.end(); }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = createWebServer().listen(8080, '0.0.0.0');
  let draining = false;
  const stop = () => {
    if (draining) return;
    draining = true;
    server.close(() => process.exit(0));
    setTimeout(() => { server.closeAllConnections(); }, 5000).unref();
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}
