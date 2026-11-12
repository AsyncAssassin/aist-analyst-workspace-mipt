"""Регрессии P4-R01-R06 и переход от компании к внешним источникам P4-S01."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import Mock

from lxml import html
import pytest

from app import collection, http_io, news_sources, note_external, public_web
from app.live_profile import FABLE_ID


def page(url, markup):
    raw = markup.encode()
    return html.fromstring(markup), hashlib.sha256(raw).hexdigest(), url, len(raw)


def article(text, links=''):
    return '<html><meta property="article:published_time" content="2026-02-04"><article><p>' + text + '</p>' + links + '</article></html>'


@pytest.mark.parametrize('text', [
    'Орбита - сеть магазинов. Орбита сотрудничает с поставщиком https://supplier.example/',
    'Орбита - сеть магазинов. Официальный сайт поставщика: https://supplier.example/',
    'Орбита - сеть магазинов. Сайт компании Маяк: https://mayak.example/',
])
def test_related_url_is_not_company_domain(text):
    assert note_external.identity([([text], 'Орбита.pdf')])['domain'] == ''


@pytest.mark.parametrize('label', ['Официальный сайт Орбита:', 'Орбита: официальный сайт'])
def test_explicit_own_site_is_available(label):
    result = note_external.identity([(['Орбита - сеть магазинов.\n' + label + ' https://orbita.example/'], 'Орбита.pdf')])
    assert result['domain'] == 'orbita.example'


def test_ready_facts_budget_keeps_full_send_and_does_not_widen_policy():
    for policy in [900, 700]:
        bounds = collection.bounded_budget({'max_model_calls': 3, 'max_duration_seconds': policy},
            'skill_analytical-note-v3', 'analytical_note', None, FABLE_ID)
        assert bounds['max_duration_seconds'] <= policy
        assert bounds['completion_seconds'] >= bounds['send_seconds'] + 240
    assert bounds['completion_seconds'] == 840


def test_dns_deadline_reaps_child(monkeypatch):
    processes = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(http_io, 'DNS_COMMAND', 'import time; time.sleep(10)')
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        http_io.resolve('irrelevant.example', 443, start + .1)
    assert time.monotonic() - start < .6
    assert processes and all(process.poll() is not None for process in processes)


@contextmanager
def slow_server(body, pause):
    disconnected = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(pause)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), disconnected
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_internal_total_deadline_interrupts_drip_feed():
    with slow_server(b'{"items":[]}' * 100, .03) as (url, disconnected):
        start = time.monotonic()
        with pytest.raises((TimeoutError, OSError, ValueError)):
            http_io.internal_json(url, start + .2, 10000)
        assert time.monotonic() - start < .7
        assert disconnected.wait(1)


class WireSocket:
    def __init__(self, wire):
        import io
        self.wire = io.BytesIO(wire)
    def makefile(self, mode):
        return self.wire
    def settimeout(self, value):
        pass
    def setsockopt(self, *args):
        pass
    def sendall(self, value):
        pass
    def shutdown(self, how):
        pass
    def close(self):
        pass
    def do_handshake(self):
        pass


@pytest.mark.parametrize('kind', ['complete', 'truncated', 'chunked', 'broken_chunk', 'oversized'])
def test_public_body_integrity_and_byte_accounting(monkeypatch, kind):
    import http.client
    body = b'<html><p>Public text</p></html>'
    headers = b'Content-Length: ' + str(len(body) + (50 if kind == 'truncated' else 0)).encode() + b'\r\n'
    if kind in ('chunked', 'broken_chunk'):
        headers = b'Transfer-Encoding: chunked\r\n'
        body = b'%x\r\n' % len(body) + body + (b'\r\n0\r\n\r\n' if kind == 'chunked' else b'')
    sock = WireSocket(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n' + headers + b'\r\n' + body)
    monkeypatch.setattr(public_web, 'resolve', lambda *args: [(2, 1, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(public_web, 'connect_resolved', lambda *args: sock)
    monkeypatch.setattr(public_web.ssl, 'create_default_context', lambda: Mock(wrap_socket=lambda *a, **kw: sock))
    limit = 10 if kind == 'oversized' else 1000
    if kind in ('complete', 'chunked'):
        doc, sha, url, size = public_web.read('https://public.example/', time.monotonic() + 1, max_bytes=limit)
        assert 'Public text' in doc.text_content() and size == len(b'<html><p>Public text</p></html>')
    else:
        with pytest.raises((ValueError, http.client.HTTPException)) as error:
            public_web.read('https://public.example/', time.monotonic() + 1, max_bytes=limit)
        assert 0 < error.value.bytes_read <= limit


def test_public_drip_feed_stops_and_accounts_received_body(monkeypatch):
    import socket
    with slow_server(b'<html>' + b'x' * 2000 + b'</html>', .01) as (url, disconnected):
        port = urlsplit(url).port
        connect = socket.create_connection
        class PlainTLS:
            def __init__(self, sock):
                self.sock = sock
            def do_handshake(self):
                pass
            def __getattr__(self, name):
                return getattr(self.sock, name)
        monkeypatch.setattr(public_web, 'resolve', lambda *args: [(2, 1, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(public_web, 'connect_resolved',
                            lambda *args: connect(('127.0.0.1', port), timeout=1))
        monkeypatch.setattr(public_web.ssl, 'create_default_context', lambda: Mock(wrap_socket=lambda sock, **kwargs: PlainTLS(sock)))
        start = time.monotonic()
        with pytest.raises((OSError, ValueError)) as error:
            public_web.read('https://public.example/', start + .2)
        assert time.monotonic() - start < .7
        assert 0 < error.value.bytes_read < 2000
        assert disconnected.wait(1)


def test_news_queue_deadline_keeps_hourly_cache_and_request_isolation(monkeypatch):
    from datetime import date
    deadlines = []
    def collect(name, inn, as_of, read, sector):
        read('https://www.interfax.ru/business/99199')
        return dict(status='empty', company=name, as_of=as_of.isoformat(), items=[])
    def fetch(url, deadline, cookie=None, budget=None):
        deadlines.append(deadline)
    monkeypatch.setattr(news_sources, 'collect', collect)
    monkeypatch.setattr(news_sources, 'fetch', fetch)
    news_sources.hourly.cache_clear()
    try:
        start = time.monotonic()
        expired = news_sources.news('Орбита', date(2026, 11, 8), sector='retail', expires=time.time() - 1)
        assert expired['status'] == 'unavailable' and not deadlines
        news_sources.news('Орбита', date(2026, 11, 8), sector='retail', expires=time.time() + .5)
        news_sources.news('Орбита', date(2026, 11, 8), sector='retail', expires=time.time() + 20)
        assert len(deadlines) == 1 and deadlines[0] - start <= .6
        assert news_sources.request_deadline.get() is None
        news_sources.news('Маяк', date(2026, 11, 8), sector='retail')
        assert 19 < deadlines[-1] - time.monotonic() <= 20
    finally:
        news_sources.hourly.cache_clear()
