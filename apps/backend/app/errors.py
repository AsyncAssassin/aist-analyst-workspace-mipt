import json
import uuid
from .contracts import PACKAGE, validate_definition

STATUSES = json.loads((PACKAGE / 'http.json').read_text())['error_statuses']
MESSAGES = {
    'invalid_credentials': 'Неверный логин или пароль либо срок доступа закончился.',
    'login_rate_limited': 'Слишком много попыток входа. Подождите пять минут.',
    'moex_unavailable': 'MOEX ISS недоступен. Прежний снимок сохранен.',
    'moex_invalid_response': 'Ответ MOEX ISS не прошел проверку. Прежний снимок сохранен.',
    'moex_identity_mismatch': 'SECID, ISIN или сведения эмитента MOEX не совпали. Прежний снимок сохранен.',
    'moex_busy': 'Проверка MOEX ISS уже выполняется. Повторите тот же ключ после завершения.',
    'embedding_unavailable': 'Сервис Novita Embeddings недоступен. Поиск по смыслу не выполнен.',
    'embedding_rate_limited': 'Novita ограничила частоту запросов. Повторите поиск позже.',
    'embedding_invalid_response': 'Novita вернула некорректные embeddings. Поиск по смыслу не выполнен.',
    'search_index_unavailable': 'Индекс Qdrant недоступен или не соответствует сохраненным материалам.',
    'search_not_indexed': 'Выбранные материалы еще не проиндексированы. Запустите команду индексации.',
    'sample_not_evidence': 'Образец оформления нельзя использовать как фактическое основание или вход расчета.',
    'unauthenticated': 'Нужна действующая сессия.',
    'resource_not_found': 'Ресурс не найден.',
    'validation_error': 'Запрос не соответствует контракту.',
    'idempotency_conflict': 'Этот ключ уже использован с другим запросом.',
    'revision_conflict': 'Ревизия изменилась.',
    'cursor_invalid': 'Курсор не принадлежит этому списку.',
    'unsupported_source': 'Поддерживается текстовый PDF, не требующий пароль, с текстом на каждой странице.',
    'too_large': 'Превышен лимит файла или обработки PDF.',
    'temporarily_unavailable': 'Сервис временно недоступен.',
    'outcome_unknown': 'Исход сохранения требует сверки. Повторите запрос с тем же ключом.',
    'source_not_ready': 'Версия источника недоступна для чтения страниц.',
    'export_missing': 'Сохраненный файл отсутствует.',
    'export_corrupt': 'Сохраненный файл поврежден.',
}


def error_value(code, expected=None, current=None):
    return validate_definition('Error', dict(code=code, message=MESSAGES.get(code, {'run_terminal':'Задача завершена.', 'tool_unavailable':'Исполнитель или обязательный инструмент недоступен.', 'tool_failed':'Инструмент завершился ошибкой.', 'budget_exceeded':'Лимит проекта или задачи исчерпан.', 'provider_error':'Получен отказ модельного сервиса.', 'unsupported_result_type':'Этот тип результата не поддерживается.', 'source_limit':'Количество источников не соответствует выбранному навыку.', 'validation_failed':'Результат не прошел проверку.'}.get(code, 'Операция отклонена.')),
        retryable=code in ('temporarily_unavailable', 'outcome_unknown'), run_id=None,
        call_id=None, tool_call_id=None, expected_revision=expected, current_revision=current))


class ApiError(Exception):
    def __init__(self, code, expected=None, current=None):
        self.code = code
        self.status = STATUSES[code]
        self.body = validate_definition('ErrorResponse', {
            'error': error_value(code, expected, current), 'request_id': 'req_' + uuid.uuid4().hex})
