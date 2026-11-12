import {execFileSync} from 'node:child_process';

const origin = new URL(process.env.AIST_QA_WEB_ORIGIN ?? 'about:blank');
if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' ||
    origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
  throw new Error('Set AIST_QA_WEB_ORIGIN to the QA web origin on http://127.0.0.1');
}
export const webOrigin = origin.origin;

export function compose(...args) {
  return execFileSync('docker', [
    'compose', '--env-file', '.env.local', '-f', 'infra/compose.local.yaml', ...args,
  ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}
