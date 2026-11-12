import test from 'node:test';
import assert from 'node:assert/strict';
import { currentAction, deadlinePassed, eventLabel } from '../src/progress';
import type { ModelCall, Run, RunEvent } from '../src/types';

const run = (state: Run['state']): Run => ({ id: 'run_1', conversation_id: 'c', message_id: 'm', revision: 1, state, input: { source_versions: [], skill_version_id: 'skill_analytical-note-v3', budget: { max_duration_seconds: 600 } }, last_event_seq: 0, result_version_id: null, error: null, waiting_input: null, created_at: '2026-10-20T10:00:00Z' });
const call = (id: string, purpose: string, transport_state: string, parent: string | null = null, http: number | null = 200): ModelCall => ({ id, purpose, transport_state, parent_model_call_id: parent, http_status: http });
const closed: RunEvent[] = [{ run_id: 'run_1', seq: 5, type: 'collection_closed', timestamp: '2026-10-20T10:01:00Z', data: { reason: 'extractions', message: 'использованы все разрешенные извлечения' } }];

test('current action names the extraction, the completion mode and the deadline without a percentage', () => {
  assert.equal(currentAction(run('queued'), [], []), 'Ожидание исполнителя.');
  assert.equal(currentAction(run('running'), [], []), 'Подготовка исполнителя.');
  assert.equal(currentAction(run('running'), [call('a1', 'agent', 'observed')], []), 'Выполнено шагов исполнителя: 1, извлечений: 0. Проверка результата шага.');
  assert.equal(currentAction(run('running'), [call('a1', 'agent', 'observed'), call('e1', 'extraction', 'sent', 'a1')], []), 'Извлечение оснований из PDF (1-е): ожидание ответа модели.');
  // An overload retry of the same extraction is not a second extraction.
  assert.equal(currentAction(run('running'), [call('a1', 'agent', 'observed'), call('e1', 'extraction', 'observed', 'a1', 429), call('e2', 'extraction', 'sent', 'e1')], []), 'Извлечение оснований из PDF (1-е): ожидание ответа модели.');
  assert.equal(currentAction(run('running'), [call('a1', 'agent', 'observed'), call('a2', 'agent', 'sent', 'a1')], closed), 'Составление записки по имеющимся основаниям (шаг 2): ожидание ответа модели.');
  assert.equal(currentAction(run('running'), [call('a1', 'agent', 'observed')], closed), 'Сбор оснований завершён: составление и сохранение записки.');
  assert.equal(currentAction(run('waiting_for_user'), [], []), null);
  assert.equal(currentAction(run('completed'), [], []), null);
  assert.equal(eventLabel(closed[0]), 'Сбор оснований завершён: использованы все разрешенные извлечения');
  assert.equal(eventLabel({ ...closed[0], type: 'state_changed', data: { from: 'running', to: 'budget_exceeded' } }), 'Достигнут лимит');
  assert.equal(deadlinePassed(run('running'), Date.parse('2026-10-20T10:09:59Z')), false);
  assert.equal(deadlinePassed(run('running'), Date.parse('2026-10-20T10:10:01Z')), true);
  assert.equal(deadlinePassed(run('budget_exceeded'), Date.parse('2026-10-20T11:00:00Z')), false);
});
