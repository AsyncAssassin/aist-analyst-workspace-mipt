import { ApiError, Client } from './client';

const prefix = 'aist.d08.';
export function readLocal<T>(key: string, fallback: T, storage: Storage = sessionStorage): T {
  try { return JSON.parse(storage.getItem(prefix + key) ?? 'null') ?? fallback; } catch { return fallback; }
}
export function writeLocal(key: string, value: unknown, storage: Storage = sessionStorage) {
  storage.setItem(prefix + key, JSON.stringify(value));
}
export function clearSession(storage: Storage = sessionStorage) {
  for (let i = storage.length - 1; i >= 0; i--) { const key = storage.key(i); if (key?.startsWith(prefix)) storage.removeItem(key); }
}
export function expireSession(storage: Storage = sessionStorage) {
  storage.removeItem(prefix + 'session');
}
export function restoreSession(token: string, authorizedProjects: string[], storage: Storage = sessionStorage) {
  // Доступ к прежнему проекту проверен сервером новой сессией до открытия Workspace.
  const project = readLocal('project', '', storage);
  if (!project || !authorizedProjects.includes(project)) clearSession(storage);
  writeLocal('session', token, storage);
}
export type Operation = { key: string; path: string; body: string; definition: string; label: string };
export function operationBody(path:string,payload:unknown,definition:string):string {
  const body = JSON.stringify(payload);
  const maximum = definition === 'VersionView' && /^\/v1\/projects\/[^/]+\/artifacts\/[^/]+\/versions$/.test(path) ? 1048576 : 16384;
  if (new TextEncoder().encode(body).length > maximum) throw new ApiError(422, 'too_large');
  return body;
}
export class Operations {
  private busy = false;
  constructor(private scope: string, private storage: Storage = sessionStorage) {}
  get pending(): Operation | null { return readLocal(this.scope, null, this.storage); }
  prepare(path: string, payload: unknown, definition: string, label: string, key?: string): Operation {
    const existing = this.pending;
    if (existing) return existing;
    const body = operationBody(path,payload,definition);
    const op = { key: key ?? crypto.randomUUID(), path, body, definition, label };
    writeLocal(this.scope, op, this.storage); // Never POST before the exact retry envelope is durable locally.
    return op;
  }
  async send<T>(client: Client, confirmed?: (result: T) => void): Promise<T> {
    if (this.busy) throw new Error('Operation in flight');
    const op = this.pending;
    if (!op) throw new Error('Missing operation');
    this.busy = true;
    try {
      const result = await client.json<T>(op.path, op.definition, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': op.key }, body: op.body });
      client.signal.throwIfAborted();
      confirmed?.(result);
      this.storage.removeItem(prefix + this.scope);
      return result;
    } catch (error) {
      if (!client.signal.aborted && error instanceof ApiError && error.status >= 400 && error.status < 500 && ![401,408,429].includes(error.status)) this.storage.removeItem(prefix + this.scope);
      throw error;
    } finally { this.busy = false; }
  }
}

let unsavedEditor=false;
export function setUnsavedEditor(value:boolean){unsavedEditor=value;}
export function hasUnsavedEditor(){return unsavedEditor;}
export function confirmNavigation(){return !unsavedEditor || window.confirm('Есть несохраненная правка. Покинуть редактор?');}

type EditorResult={id:string;parentId:string};
let finishEdit:(()=>Promise<EditorResult|undefined>)|null=null;
export function bindEditor(save:typeof finishEdit){finishEdit=save;}
export async function finishActiveEdit(){
  if(!finishEdit)return null;
  const saved=await finishEdit();
  if(!saved)throw Error('Сначала завершите сохранение правки в записке.');
  return saved;
}
