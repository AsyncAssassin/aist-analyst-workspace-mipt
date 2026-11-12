import { contractIdentity } from './contracts';

export type Connection = { state: 'ready' | 'not_ready' | 'unavailable'; message: string };

export async function checkConnection(): Promise<Connection> {
  try {
    const response = await fetch('/api/readyz', { signal: AbortSignal.timeout(7000), cache: 'no-store' });
    if (response.status !== 200 && response.status !== 503)
      return { state: 'unavailable', message: 'Нет соединения с сервисом. Проверьте локальный запуск и повторите попытку.' };
    const value = await response.json();
    if (value.contract_version !== contractIdentity.contract_version || value.schema_sha256 !== contractIdentity.schema_sha256)
      return { state: 'unavailable', message: 'Версии сборки не совпадают. Требуется обновить локальную сборку.' };
    if (response.status === 200 && value.status === 'ready')
      return { state: 'ready', message: 'Соединение установлено. Локальная сборка готова.' };
    if (response.status === 503 && value.reason === 'database_unavailable')
      return { state: 'not_ready', message: 'Сервис отвечает, но база данных недоступна. Повторите проверку после ее запуска.' };
    if (response.status === 503 && value.reason === 'migration_required')
      return { state: 'not_ready', message: 'Базе данных требуется обновление. Примените миграции локальной сборки.' };
    return { state: 'unavailable', message: 'Сервис пока не готов. Повторите проверку соединения.' };
  } catch {
    return { state: 'unavailable', message: 'Нет соединения с сервисом. Проверьте локальный запуск и повторите попытку.' };
  }
}
