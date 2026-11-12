import { isActive, states, type ModelCall, type Run, type RunEvent } from './types';

const labels: Record<string, string> = { result_saved: 'Сохранена версия результата', user_input_required: 'Запрошено уточнение', tool_finished: 'Завершён шаг', model_observed: 'Получен ответ исполнителя', usage_recorded: 'Обновлён учёт', cancel_requested: 'Запрошена отмена', recovery_recorded: 'Записано восстановление', collection_closed: 'Сбор оснований завершён' };

export function eventLabel(e: RunEvent): string {
  if (e.type === 'state_changed') return states[e.data.to as Run['state']];
  if (e.type === 'collection_closed') return `${labels.collection_closed}: ${String(e.data.message ?? '')}`;
  return labels[e.type] ?? e.type;
}

// Milliseconds left of the Run deadline, counted from its creation; null without a known budget.
export function deadlineIn(run: Run, now = Date.now()): number | null {
  const seconds = run.input.budget?.max_duration_seconds;
  if (!seconds || !run.created_at) return null;
  return new Date(run.created_at).getTime() + seconds * 1000 - now;
}

export function deadlinePassed(run: Run, now = Date.now()): boolean {
  const left = deadlineIn(run, now);
  return isActive(run) && run.state !== 'waiting_for_user' && left !== null && left < 0;
}

// What the task is doing now, derived from the saved calls and events: no invented percentage.
export function currentAction(run: Run, calls: ModelCall[], events: RunEvent[]): string | null {
  if (!isActive(run) || run.state === 'waiting_for_user') return null;
  if (run.state === 'queued') return 'Ожидание исполнителя.';
  const closed = events.some(e => e.type === 'collection_closed');
  const byId = new Map(calls.map(c => [c.id, c]));
  const retry = (c: ModelCall) => { const parent = c.parent_model_call_id ? byId.get(c.parent_model_call_id) : undefined; return !!parent && parent.purpose === c.purpose && parent.http_status === 429; };
  const extractions = calls.filter(c => c.purpose === 'extraction' && !retry(c)).length;
  const steps = calls.filter(c => c.purpose === 'agent' && !retry(c)).length;
  const pending = calls.find(c => ['reserved', 'attempted', 'sent'].includes(c.transport_state));
  if (pending?.purpose === 'extraction') return `Извлечение оснований из PDF (${extractions}-е): ожидание ответа модели.`;
  if (pending) return closed ? `Составление записки по имеющимся основаниям (шаг ${steps}): ожидание ответа модели.` : `Шаг исполнителя ${steps}: ожидание ответа модели.`;
  if (closed) return 'Сбор оснований завершён: составление и сохранение записки.';
  return steps ? `Выполнено шагов исполнителя: ${steps}, извлечений: ${extractions}. Проверка результата шага.` : 'Подготовка исполнителя.';
}
