"""Готовые инструменты поиска и чтения. Запросами управляет навык OpenClaw."""
import json
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from .public_web import public_url, same_public_url
from .http_io import internal_json

TOPICS = ('owners', 'market', 'competitor_revenue', 'business', 'deals', 'news')
MAX_REQUESTS = 18
MAX_BYTES = 8_000_000
MAX_RESPONSE_BYTES = 160_000
MAX_ITEMS = 24
MAX_TEXT = 12_000
SEARCH_ORIGIN = 'https://search.parallel.ai/mcp'


def snapshot(company, as_of, status='empty', topics=None):
    return dict(company=company, as_of=as_of, status=status, collection_version='materials.v1',
                retrieved_at=datetime.now(timezone.utc).isoformat(), items=[],
                topics={key: status for key in (topics or TOPICS)}, requests=0, bytes_read=0, elapsed_seconds=0)


def provider_result(name, arguments, deadline, session_id=None):
    """Один MCP-запрос со стабильным идентификатором Run, без ключа модели."""
    if session_id:
        arguments = dict(arguments, session_id=session_id)
    response, receipt = internal_json(SEARCH_ORIGIN, deadline, MAX_RESPONSE_BYTES, body={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}},
        headers={'Accept': 'application/json, text/event-stream'}, with_receipt=True)
    try:
        if not isinstance(response, dict) or 'error' in response:
            raise ValueError('search provider unavailable')
        result = response.get('result')
        if not isinstance(result, dict) or result.get('isError'):
            raise ValueError('search provider unavailable')
        payload = result.get('structuredContent')
        if payload is None:
            payload = json.loads(next((item['text'] for item in result.get('content', []) if item.get('type') == 'text'), '{}'))
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('unreadable provider response')
    except (ValueError, AttributeError, TypeError, KeyError) as cause:
        error = ValueError('unreadable provider response')
        error.bytes_read = receipt['bytes_read']
        raise error from cause
    return payload, receipt


def search(query, deadline, session_id=None):
    payload, receipt = provider_result('web_search', dict(objective=query['objective'], search_queries=query['queries']), deadline, session_id)
    items, seen = [], set()
    for item in payload['results'][:10]:
        if not isinstance(item, dict):
            continue
        try:
            url = public_url(item.get('url'))
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        excerpts = item.get('excerpts')
        items.append(dict(url=url, title=str(item.get('title') or '')[:500],
                          snippet='\n'.join(str(s) for s in excerpts)[:3000] if isinstance(excerpts, list) else ''))
    return dict(results=items, provider='parallel-free', bytes_read=receipt['bytes_read'])


def read_page(url, as_of, deadline, max_bytes, session_id=None, objective=None):
    """Сохраняем извлеченный провайдером текст и хеш его ответа, не хеш оригинала."""
    url = public_url(url)
    if max_bytes < MAX_RESPONSE_BYTES:
        raise ValueError('insufficient response allowance')
    arguments = dict(urls=[url], full_content=False, allow_live_fetch=True)
    if objective:
        arguments['objective'] = objective
    payload, receipt = provider_result('web_fetch', arguments, deadline, session_id)
    matches = [item for item in payload['results']
               if isinstance(item, dict) and same_public_url(item.get('url'), url)]
    if len(matches) != 1 or not isinstance(matches[0].get('excerpts'), list):
        return dict(unavailable=True, bytes_read=receipt['bytes_read'])
    item = matches[0]
    text = '\n\n'.join(value for value in item['excerpts'] if isinstance(value, str)).strip()
    if len(text) < 60:
        return dict(unavailable=True, bytes_read=receipt['bytes_read'])
    published = None
    try:
        stamp = date.fromisoformat(item.get('publish_date') or '')
        if stamp <= date.fromisoformat(as_of):
            published = stamp.isoformat()
    except (TypeError, ValueError):
        pass
    acquired = item.get('acquired_at')
    if not isinstance(acquired, str):
        acquired = None
    elif acquired:
        try:
            if datetime.fromisoformat(acquired.replace('Z', '+00:00')).tzinfo is None:
                acquired = None
        except ValueError:
            acquired = None
    else:
        acquired = None
    return dict(url=url, title=' '.join(str(item.get('title') or '').split())[:300], published=published,
                page_sha256=None, response_sha256=receipt['sha256'], retrieval_kind='provider_extract', provider='parallel',
                provider_acquired_at=acquired, quote=text, truncated=True, bytes_read=receipt['bytes_read'],
                retrieved_at=datetime.now(timezone.utc).isoformat(), publisher=urlsplit(url).hostname[:100])
