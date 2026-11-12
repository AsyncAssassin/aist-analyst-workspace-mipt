"""Материалы поиска не доказывают семантику; сеть принимает полные ответы в общем сроке."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import socket
import ssl
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import http_io, news_sources, public_web


@pytest.mark.parametrize('value,canonical', [
    ('https://EXAMPLE.com:443#top', 'https://example.com/'),
    ('HTTPS://пример.рф/?Q=%2F+%20&Q=2#текст', 'https://xn--e1afmkfd.xn--p1ai/?Q=%2F+%20&Q=2'),
    ('https://example.com/Path?', 'https://example.com/Path?'),
    ('https://example.com/' + 'x' * 2028, 'https://example.com/' + 'x' * 2028),
])
def test_public_url_is_idempotent(value, canonical):
    assert public_web.public_url(value) == canonical
    assert public_web.public_url(canonical) == canonical


@pytest.mark.parametrize('value', [
    'https://8.8.8.8/', 'https://1.1.1.1/x', 'https://127.1/', 'https://0x7f.0.0.1/',
    'https://2130706433/', 'https://017700000001/', 'https://0127.0.0.1/', 'https://1.1.1.0x1/',
    'https://[2001:4860:4860::8888]/', 'https://[64:ff9b::8.8.8.8]/', 'https://[64:ff9b::169.254.169.254]/',
    'https://[64:ff9b::10.0.0.5]/', 'https://[::ffff:8.8.8.8]/', 'https://[::8.8.8.8]/',
    'https://[::127.0.0.1]/', 'https://[2001:4860:4860::0.0.136.136]/',
    'https://example.com./a', 'https://test.internal./', 'https://example.com:/',
    'https://example.com:bogus/', 'https://example.com:0443/', 'https://@example.com/', 'https://x@example.com/',
    ' https://example.com/', 'https://exam\tple.com/', 'https://example.com/\n', 'https://example.com/\x7f',
    'https://example.com/\\a', 'https://example.com//other.example/', 'https://example.com/a%2',
    'https://example.com/' + 'x' * 2029, 'https://пример.рф/' + 'x' * 2028,
])
def test_public_url_rejects_ambiguous_and_overlong_input(value):
    with pytest.raises(ValueError):
        public_web.public_url(value)


def socket_answers(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address) for address in addresses]


@pytest.fixture
def local_tls(monkeypatch, tmp_path):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    hosts = ['public.example', 'raexpert.ru', 'ir.henderson.ru', 'ir.zaymer.ru']
    names = ','.join('DNS:' + host for host in hosts)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=public.example', '-addext', 'subjectAltName=' + names], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    state = SimpleNamespace(connects=[], requests=[], resolved=[], sni=[], routes={},
                            addresses=['8.8.4.4'], trusted=True)
    tls.set_servername_callback(lambda sock, name, ctx: state.sni.append(name))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.requests.append((self.headers['Host'], self.path))
            status, headers, data = state.routes[self.path]
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server, socket.socket() as refused:
        refused.bind(('127.0.0.1', 0))
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        connect = socket.socket.connect
        context = ssl.create_default_context
        def redirect_connect(sock, address):
            state.connects.append(address)
            return connect(sock, {'8.8.4.4': server.server_address, '8.8.8.8': refused.getsockname()}[address[0]])
        def resolve(host, port, deadline):
            state.resolved.append((host, port, deadline))
            return socket_answers(*[(address, port) for address in state.addresses])
        monkeypatch.setattr(public_web, 'resolve', resolve)
        monkeypatch.setattr(socket.socket, 'connect', redirect_connect)
        monkeypatch.setattr(ssl, 'create_default_context', lambda: context(cafile=str(cert)) if state.trusted else context())
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
        monkeypatch.setattr(http_io, 'resolve', resolve)
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize('addresses,attempts,success', [
    (['8.8.8.8', '8.8.4.4'], ['8.8.8.8', '8.8.4.4'], True),
    (['8.8.4.4', '8.8.8.8'], ['8.8.4.4'], True),
    (['8.8.8.8'], ['8.8.8.8'], False),
    ([], [], False),
    (['10.0.0.1'], [], False),
    (['8.8.4.4', '10.0.0.1'], [], False),
    (['not-a-number'], [], False),
])
def test_public_dns_fallback_and_all_answers_check(local_tls, addresses, attempts, success):
    local_tls.addresses = addresses
    local_tls.routes['/wire?'] = 200, [('Content-Type', 'text/html')], b'<p>Test document</p>'
    url = 'https://public.example/wire?'
    deadline = time.monotonic() + 3
    if success:
        doc, _, final_url, _ = public_web.read(url, deadline)
        assert doc.text_content() == 'Test document' and final_url == url
        assert local_tls.requests == [('public.example', '/wire?')] and local_tls.sni == ['public.example']
    else:
        with pytest.raises((ValueError, OSError)):
            public_web.read(url, deadline)
        assert not local_tls.requests and not local_tls.sni
    assert local_tls.resolved == [('public.example', 443, deadline)]
    assert local_tls.connects == [(address, 443) for address in attempts]


def test_public_reader_stops_at_untrusted_certificate(local_tls, monkeypatch):
    local_tls.trusted = False
    monkeypatch.delenv('SSL_CERT_FILE')
    local_tls.addresses = ['8.8.4.4', '8.8.8.8']
    with pytest.raises(ssl.SSLCertVerificationError):
        public_web.read('https://public.example/', time.monotonic()+3)
    assert local_tls.connects == [('8.8.4.4', 443)]
    assert not local_tls.requests


def test_internal_https_fallback_preserves_host_sni_and_receipt(local_tls):
    local_tls.addresses = ['8.8.8.8', '8.8.4.4']
    body = b'{"ok":true}'
    local_tls.routes['/rpc'] = 200, [('Content-Type', 'application/json')], body
    value, receipt = http_io.internal_json('https://public.example/rpc', time.monotonic()+3, 100, with_receipt=True)
    assert value == {'ok': True}
    assert receipt == dict(bytes_read=len(body), sha256=hashlib.sha256(body).hexdigest())
    assert local_tls.requests == [('public.example', '/rpc')] and local_tls.sni == ['public.example']
    assert local_tls.connects == [('8.8.8.8', 443), ('8.8.4.4', 443)]


@pytest.mark.parametrize('location', [
    'https://other.example/blocked', '//other.example/blocked', 'http://public.example/blocked',
    'https://user@public.example/blocked', 'https://public.example:444/blocked',
    'https://127.0.0.1/blocked', '///blocked', '/start#cycle', None,
])
def test_public_reader_does_not_contact_forbidden_redirect(local_tls, location):
    local_tls.routes['/start'] = 302, [('Location', location)] if location else [], b''
    with pytest.raises(ValueError):
        public_web.read('https://public.example/start', time.monotonic()+3)
    assert local_tls.requests == [('public.example', '/start')]
    assert local_tls.connects == [('8.8.4.4', 443)]


def test_public_reader_same_origin_uses_one_deadline_and_limits_hops(local_tls):
    local_tls.routes.update({'/start': (302, [('Location', '/next?')], b''),
                             '/next?': (302, [('Location', 'https://PUBLIC.EXAMPLE:443/done#part')], b''),
                             '/done': (200, [('Content-Type', 'text/html')], b'<p>Done</p>')})
    deadline = time.monotonic()+3
    doc, _, final_url, _ = public_web.read('https://public.example/start', deadline, redirects=2)
    assert doc.text_content() == 'Done' and final_url == 'https://public.example/done'
    assert local_tls.resolved == [('public.example', 443, deadline)] * 3
    assert local_tls.requests == [('public.example', path) for path in ('/start', '/next?', '/done')]
    local_tls.requests.clear()
    with pytest.raises(ValueError, match='too many redirects'):
        public_web.read('https://public.example/start', time.monotonic()+3, redirects=1)
    assert local_tls.requests == [('public.example', path) for path in ('/start', '/next?')]
    local_tls.requests.clear()
    with pytest.raises(ValueError, match='too many redirects'):
        public_web.read('https://public.example/start', time.monotonic()+3, redirects=0)
    assert local_tls.requests == [('public.example', '/start')]


@pytest.mark.parametrize('xml', [False, True])
def test_legacy_news_handles_unknown_charset_and_serves_following_request(local_tls, xml):
    feed = '/sitemap-feed-1.xml'
    xml_index = f'<sitemapindex><sitemap><loc>https://ir.henderson.ru{feed}</loc></sitemap></sitemapindex>'.encode()
    xml_feed = (b'<urlset><url><loc>https://ir.henderson.ru/press-releases/tpost/kollektsiy</loc>'
                b'<lastmod>2026-01-01</lastmod></url></urlset>')
    name, path, mime, body = (
        ('HENDERSON', '/sitemap-feeds.xml', 'application/xml', xml_index) if xml else
        ('Совкомбанк', '/database/companies/sovkombank/', 'text/html',
         '<html><p>ИНН</p><p>0000000000</p></html>'.encode()))
    local_tls.routes[path] = 200, [('Content-Type', mime + '; charset=x-fixture-invalid')], body
    news_sources.hourly.cache_clear()
    try:
        with TestClient(news_sources.app, raise_server_exceptions=False) as client:
            failed = client.get('/news', params=dict(name=name, as_of='2026-11-08'))
            assert failed.status_code == 200 and failed.json()['status'] == 'unavailable'
            assert failed.json()['items'] == []
            local_tls.routes[path] = 200, [('Content-Type', mime + '; charset=utf-8')], body
            local_tls.routes[feed] = 200, [('Content-Type', 'application/xml')], xml_feed
            again = client.get('/news', params=dict(name=name, as_of='2026-11-09'))
            assert again.status_code == 200
            assert again.json()['status'] == 'empty'
        assert [request[1] for request in local_tls.requests] == [path, path] + ([feed] if xml else [])
        local_tls.routes[path] = 200, [('Content-Type', mime + '; charset=x-fixture-invalid')], body
        with pytest.raises(ValueError, match='unsupported public content charset') as error:
            public_web.read('https://' + local_tls.requests[0][0] + path, time.monotonic()+3, xml=xml)
        assert error.value.bytes_read == len(body)
    finally:
        news_sources.hourly.cache_clear()


def test_legacy_news_keeps_partial_results_after_unsupported_charset(local_tls):
    index = '/news/company-news'
    first, second = index + '/post-1', index + '/post-2'
    title = 'Займер заключил договор'
    listing = ''.join(f'<a href="{path}"><span class="_date_">{day}.11.2026</span>'
                      f'<span class="_title_">{title}</span></a>'
                      for path, day in [(first, '01'), (second, '02')]).encode()
    article = f'<h1>{title}</h1><p class="_date_">01.11.2026</p>'.encode()
    valid = [('Content-Type', 'text/html; charset=utf-8')]
    local_tls.routes = {index: (200, valid, listing), first: (200, valid, article),
                        second: (200, [('Content-Type', 'text/html; charset=x-fixture-invalid')], b'<p>Unavailable</p>')}
    news_sources.hourly.cache_clear()
    try:
        with TestClient(news_sources.app, raise_server_exceptions=False) as client:
            reply = client.get('/news', params=dict(name='Займер', as_of='2026-11-08'))
        assert reply.status_code == 200 and reply.json()['status'] == 'partial'
        item, = reply.json()['items']
        assert item['quote'] == title and item['page_sha256'] == hashlib.sha256(article).hexdigest()
        assert sorted(path for _, path in local_tls.requests) == sorted([index, first, second])
    finally:
        news_sources.hourly.cache_clear()


def test_public_xml_does_not_expand_external_entities(local_tls, tmp_path):
    from lxml import etree
    local_file = tmp_path / 'synthetic-marker.txt'
    local_file.write_text('Synthetic XML marker A21')
    body = f'<!DOCTYPE root [<!ENTITY marker SYSTEM "{local_file.as_uri()}">]><root>&marker;</root>'.encode()
    local_tls.routes['/xml'] = 200, [('Content-Type', 'application/xml')], body
    doc, digest, _, size = public_web.read('https://public.example/xml', time.monotonic()+3, xml=True)
    assert etree.tostring(doc) == b'<root>&marker;</root>'
    assert digest == hashlib.sha256(body).hexdigest() and size == len(body)


@pytest.mark.parametrize('protocol,closing,length', [
    ('HTTP/1.0', True, True), ('HTTP/1.1', True, True),
    ('HTTP/1.1', False, True), ('HTTP/1.0', True, False),
])
def test_complete_tls_response_is_accepted(monkeypatch, tmp_path, protocol, closing, length):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=public.example', '-addext', 'subjectAltName=DNS:public.example'], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    names = []
    context.set_servername_callback(lambda sock, name, ctx: names.append(name))
    body = b'<html><p>Complete public article</p></html>'
    class Handler(BaseHTTPRequestHandler):
        protocol_version = protocol
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if length:
                self.send_header('Content-Length', str(len(body)))
            if closing:
                self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    original_context = ssl.create_default_context
    connect = socket.socket.connect
    def redirect_connect(sock, address):
        assert address == ('8.8.8.8', 443)
        return connect(sock, server.server_address)
    monkeypatch.setattr(public_web, 'resolve', lambda *args: [(2, 1, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(socket.socket, 'connect', redirect_connect)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: original_context(cafile=str(cert)))
    try:
        doc, sha, url, size = public_web.read('https://public.example/', time.monotonic() + 2)
        assert size == len(body) and sha == hashlib.sha256(body).hexdigest()
        assert doc.text_content() == 'Complete public article' and names == ['public.example']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_internal_tls_connect_and_handshake_share_deadline(monkeypatch):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    stopped = threading.Event()
    def accept():
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(1)
            try:
                while connection.recv(4096):
                    pass
            except OSError:
                pass
        stopped.set()
    thread = threading.Thread(target=accept)
    thread.start()
    connect = socket.socket.connect
    def slow_connect(sock, address):
        time.sleep(.14)
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', slow_connect)
    monkeypatch.setattr(http_io, 'resolve', lambda *args: [(2, 1, 6, '', listener.getsockname())])
    start = time.monotonic()
    try:
        with pytest.raises((OSError, ValueError)):
            http_io.internal_json('https://public.example:' + str(listener.getsockname()[1]) + '/', start + .20, 1000)
        assert time.monotonic() - start < .27
        assert stopped.wait(.1)
    finally:
        thread.join(2)
        listener.close()
    assert not thread.is_alive()


@pytest.fixture
def internal_wire():
    state = SimpleNamespace(first=[], second=[], lines=[], mode='ok')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def parse_request(self):
            state.lines.append((self.server.server_address[0], self.raw_requestline))
            return super().parse_request()

        def do_GET(self):
            self.handle_request(None)

        def do_POST(self):
            if self.headers['Content-Length'] is not None:
                self.handle_request(self.rfile.read(int(self.headers['Content-Length'])))

        def handle_request(self, payload):
            first = self.server.server_address[0] == '127.0.0.1'
            (state.first if first else state.second).append((self.command, self.path, self.headers['Host'], payload))
            mode = state.mode if first else 'ok'
            if mode == 'lost':
                return
            self.send_response({'status': 503, 'redirect': 302}.get(mode, 200))
            if mode == 'redirect':
                self.send_header('Location', 'https://other.example/')
            self.send_header('Content-Length', '100' if mode == 'partial' else '2')
            self.end_headers()
            self.wfile.write(b'{}')

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as first:
        state.port = first.server_port
        with ThreadingHTTPServer(('127.0.0.2', state.port), Handler) as second:
            servers = [first, second]
            threads = [threading.Thread(target=server.serve_forever) for server in servers]
            for thread in threads:
                thread.start()
            try:
                yield state
            finally:
                for server, thread in zip(servers, threads):
                    server.shutdown()
                    thread.join()


@pytest.mark.parametrize('addresses,success', [
    (['127.0.0.3', '127.0.0.2'], True), (['127.0.0.2', '127.0.0.3'], True),
    (['127.0.0.3', '127.0.0.4'], False), ([], False),
])
def test_internal_dns_fallback_uses_numeric_addresses(monkeypatch, internal_wire, addresses, success):
    port = internal_wire.port
    resolved = []
    def resolve(host, port, deadline):
        resolved.append((host, port, deadline))
        return socket_answers(*[(address, port) for address in addresses])
    def unexpected_dns(*args, **kwargs):
        pytest.fail('TCP connect must reuse the numeric DNS address')
    monkeypatch.setattr(http_io, 'resolve', resolve)
    monkeypatch.setattr(socket, 'getaddrinfo', unexpected_dns)
    deadline = time.monotonic() + 3
    url = f'http://internal.example:{port}/wire?a=2'
    if success:
        assert http_io.internal_json(url, deadline, 100) == {}
        assert internal_wire.second == [('GET', '/wire?a=2', f'internal.example:{port}', None)]
    else:
        with pytest.raises(OSError):
            http_io.internal_json(url, deadline, 100)
        assert not internal_wire.second
    assert not internal_wire.first and resolved == [('internal.example', port, deadline)]


@pytest.mark.parametrize('body', [None, dict(synthetic='A21')])
@pytest.mark.parametrize('mode', ['lost', 'partial', 'status', 'redirect'])
def test_internal_json_does_not_resend_after_http_send(monkeypatch, internal_wire, mode, body):
    from http.client import HTTPException
    port = internal_wire.port
    internal_wire.mode = mode
    monkeypatch.setattr(http_io, 'resolve', lambda *args: socket_answers(('127.0.0.1', port), ('127.0.0.2', port)))
    with pytest.raises((HTTPException, ValueError)) as failure:
        http_io.internal_json(f'http://internal.example:{port}/wire', time.monotonic()+3, 150, body=body)
    assert len(internal_wire.first) == 1 and not internal_wire.second
    method, path, host, payload = internal_wire.first[0]
    assert (method, path, host) == ('POST' if body else 'GET', '/wire', f'internal.example:{port}')
    assert payload == (b'{"synthetic": "A21"}' if body else None)
    if mode == 'partial':
        assert failure.value.bytes_read == 2


def test_internal_json_does_not_resend_after_partial_http_write(monkeypatch, internal_wire):
    port = internal_wire.port
    monkeypatch.setattr(http_io, 'resolve', lambda *args: socket_answers(('127.0.0.1', port), ('127.0.0.2', port)))
    sent = socket.socket.sendall
    def partial_write(sock, data):
        if data.startswith(b'POST ') and sock.getpeername() == ('127.0.0.1', port):
            sent(sock, data.split(b'\r\n', 1)[0] + b'\r\n')
            raise BrokenPipeError('synthetic partial write')
        return sent(sock, data)
    monkeypatch.setattr(socket.socket, 'sendall', partial_write)
    with pytest.raises(BrokenPipeError):
        http_io.internal_json(f'http://internal.example:{port}/wire', time.monotonic()+3, 100, body={'fixture': True})
    limit = time.monotonic()+1
    while not internal_wire.lines and time.monotonic() < limit:
        time.sleep(.01)
    assert internal_wire.lines == [('127.0.0.1', b'POST /wire HTTP/1.1\r\n')]
    assert not internal_wire.first and not internal_wire.second


def test_internal_tcp_deadline_does_not_start_next_connect(monkeypatch):
    attempts = []
    deadline = time.monotonic() + .15
    def expire(sock, address):
        attempts.append(address)
        time.sleep(max(0, deadline - time.monotonic()) + .01)
        raise TimeoutError('fixture reached deadline')
    monkeypatch.setattr(socket.socket, 'connect', expire)
    monkeypatch.setattr(http_io, 'resolve', lambda *args: socket_answers(('127.0.0.1', 80), ('127.0.0.2', 80)))
    with pytest.raises(TimeoutError):
        http_io.internal_json('http://internal.example/wire', deadline, 100)
    assert attempts == [('127.0.0.1', 80)]
