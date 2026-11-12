import { validateDefinition } from './contracts';
import type { RunEvent } from './types';

export class ApiError extends Error {
  constructor(public status: number, public code: string) { super(code); }
}
export function errorText(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Нет ответа от сервиса. Проверьте соединение.';
  const messages: Record<string, string> = {
    invalid_credentials: 'Неверный логин или пароль либо срок доступа закончился.',
    login_rate_limited: 'Слишком много попыток входа. Подождите пять минут.',
    moex_busy: 'Проверка MOEX ISS уже выполняется. Повторите проверку результата с тем же ключом.',
    embedding_unavailable: 'Novita Embeddings недоступна. Поиск по смыслу не выполнен.',
    embedding_rate_limited: 'Novita ограничила частоту запросов. Повторите поиск позже.',
    embedding_invalid_response: 'Novita вернула некорректные векторы. Поиск по смыслу не выполнен.',
    search_index_unavailable: 'Индекс поиска недоступен. Повторите поиск после восстановления индекса.',
    search_not_indexed: 'Выбранные материалы еще не проиндексированы.',
    sample_not_evidence: 'Образец оформления нельзя использовать как фактическое основание.',
    unauthenticated: 'Сессия недействительна или истекла. Войдите с новой сессией.',
    resource_not_found: 'Объект недоступен в этой сессии. Обновите список.',
    unsupported_source: 'Не удалось прочитать PDF. Проверьте, что файл открывается без пароля и содержит хотя бы одну непустую страницу.',
    too_large: 'Файл или запрос превышает допустимый размер.',
    source_limit: 'Превышен лимит источников.',
    source_not_ready: 'Источник пока не готов или повреждён. Выберите доступную версию.',
    temporarily_unavailable: 'Сервис временно недоступен. Повторите действие после восстановления.',
    tool_unavailable: 'Исполнитель недоступен в этой конфигурации. Задача не запущена.',
    revision_conflict: 'Данные уже изменились. Обновите состояние перед новой отправкой.',
    run_terminal: 'Задача уже завершена. Показано актуальное состояние.',
    idempotency_conflict: 'Ключ операции уже связан с другим запросом. Автоматическая отправка остановлена.',
    cursor_invalid: 'Позиция истории устарела. Обновите данные.',
    budget_exceeded: 'Серверный лимит исчерпан. Задача не запущена.',
    validation_failed: 'Содержание не прошло проверку оснований. Проверьте числа, единицы и периоды. Текст правки сохранен.',
    validation_error: 'Проверьте поля запроса и допустимый размер текста.',
    export_missing: 'Сохраненный файл отсутствует в хранилище.',
    export_corrupt: 'Сохраненный файл поврежден.',
    outcome_unknown: 'Сервис пока не подтвердил результат операции. Повторите эту же операцию с сохранённым ключом.',
  };
  return messages[error.code] ?? (error.status === 409 ? 'Состояние изменилось. Обновите данные перед новым действием.' : 'Сервис недоступен. Повторите проверку соединения.');
}

export async function loginWithPassword(login: string, password: string, signal: AbortSignal): Promise<string> {
  const response = await fetch('/api/v1/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password }), signal, cache: 'no-store',
  });
  const value = await response.json();
  if (!response.ok) throw new ApiError(response.status, value.error?.code ?? 'temporarily_unavailable');
  validateDefinition('LoginResponse', value);
  return value.token;
}

export class Client {
  readonly controller = new AbortController();
  constructor(private token: string, parent?: AbortSignal, private unauthorized?: () => void) {
    if (parent?.aborted) this.controller.abort();
    else parent?.addEventListener('abort', () => this.close(), { once: true, signal: this.controller.signal });
  }
  get signal() { return this.controller.signal; }
  close() { this.controller.abort(); }
  child() { return new Client(this.token, this.signal, this.unauthorized); }
  async response(path: string, init: RequestInit = {}, streaming = false): Promise<Response> {
    this.signal.throwIfAborted();
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${this.token}`);
    const fileTransfer = init.body instanceof FormData || path.endsWith('/file');
    const deadline = fileTransfer ? 180000 : path.endsWith('/search') ? 45000 : path.endsWith('/moex/wush') ? 60000 : 25000;
    const signal = AbortSignal.any([this.signal, ...(init.signal ? [init.signal] : []), ...(!streaming ? [AbortSignal.timeout(deadline)] : [])]);
    const response = await fetch('/api' + path, { ...init, headers, signal, cache: 'no-store' });
    this.signal.throwIfAborted();
    if (!response.ok) {
      const value = await response.json().catch(() => ({}));
      this.signal.throwIfAborted();
      if (response.status === 401) this.unauthorized?.();
      throw new ApiError(response.status, value.error?.code ?? 'temporarily_unavailable');
    }
    return response;
  }
  async json<T>(path: string, definition: string, init?: RequestInit): Promise<T> {
    const response = await this.response(path, init);
    const data = await response.json();
    this.signal.throwIfAborted();
    validateDefinition(definition, data);
    return data as T;
  }
  async list<T>(path: string, definition: string, filters:Record<string,string>={}): Promise<T[]> {
    const items: T[] = []; let cursor: string | null = null;
    do {
      const query = new URLSearchParams({...filters,limit:'100',...(cursor?{after_id:cursor}:{})});
      const page: { items: T[]; next_after_id: string | null } = await this.json(path+'?'+query, definition);
      items.push(...page.items); cursor = page.next_after_id;
    } while (cursor);
    return items;
  }
}

// Cursor is derived only from contiguous, validated events received for this run.
export class EventHistory {
  events: RunEvent[] = [];
  constructor(readonly run: string) {}
  get seq() { return this.events.at(-1)?.seq ?? 0; }
  add(values: RunEvent[]) {
    for (const event of values) {
      validateDefinition('RunEvent', event);
      if (event.run_id !== this.run) throw new Error('Foreign event');
      if (event.seq <= this.seq) continue;
      if (event.seq !== this.seq + 1) throw new Error('Event gap');
      this.events.push(event);
    }
  }
}
export class SseParser {
  private pending = '';
  finished = false;
  feed(text: string): RunEvent[] {
    this.pending += text;
    const result: RunEvent[] = [];
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(this.pending))) {
      const block = this.pending.slice(0, match.index);
      this.pending = this.pending.slice(match.index + match[0].length);
      if (block === ': run-finished') this.finished = true;
      const data = block.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
      if (data) result.push(JSON.parse(data));
    }
    if (this.pending.length > 1000000) throw new Error('Event too large');
    return result;
  }
}
export async function followEvents(client: Client, path: string, history: EventHistory, changed: () => void, connection: (ready: boolean) => void) {
  let attempt = new AbortController();
  const restart = () => attempt.abort();
  if (typeof window !== 'undefined') { window.addEventListener('online', restart); window.addEventListener('offline', restart); }
  try {
  while (!client.signal.aborted) {
    attempt = new AbortController();
    let heartbeat: ReturnType<typeof setTimeout> | undefined;
    try {
      let more: boolean;
      do {
        const page = await client.json<{ events: RunEvent[]; has_more: boolean }>(`${path}/events?after_seq=${history.seq}&limit=100`, 'EventPage');
        const before = history.seq; history.add(page.events); more = page.has_more;
        if (history.seq !== before) changed();
      } while (more);
      const response = await client.response(`${path}/events/stream?after_seq=${history.seq}`, { headers: { Accept: 'text/event-stream', 'Last-Event-ID': `${history.run}:${history.seq}` }, signal: attempt.signal }, true);
      connection(true);
      const reader = response.body!.getReader(); const decoder = new TextDecoder(); const parser = new SseParser();
      try {
        while (!client.signal.aborted) {
          clearTimeout(heartbeat);
          heartbeat = setTimeout(() => attempt.abort(), 8000);
          const part = await reader.read(); client.signal.throwIfAborted(); attempt.signal.throwIfAborted();
          if (part.done) break;
          const before = history.seq; history.add(parser.feed(decoder.decode(part.value, { stream: true })));
          if (history.seq !== before || parser.finished) changed();
          if (parser.finished) return;
        }
      } finally { clearTimeout(heartbeat); await reader.cancel().catch(() => {}); reader.releaseLock(); }
    } catch (error) {
      if (client.signal.aborted || (error instanceof ApiError && error.status === 401)) return;
    }
    finally { clearTimeout(heartbeat); }
    if (client.signal.aborted) return;
    connection(false);
    await new Promise<void>(resolve => {
      const done = () => { clearTimeout(timer); client.signal.removeEventListener('abort', done); resolve(); };
      const timer = setTimeout(done, 1800); client.signal.addEventListener('abort', done, { once: true });
    });
  }
  } finally {
    attempt.abort();
    if (typeof window !== 'undefined') { window.removeEventListener('online', restart); window.removeEventListener('offline', restart); }
  }
}
