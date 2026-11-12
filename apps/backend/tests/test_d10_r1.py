"""D10-R1 signs and bounded local request reads, without a provider."""
from decimal import Decimal
import json
import socket
import threading
import time

import pytest
from app.errors import ApiError
from app.openclaw_runtime import PrivateHTTPServer, ProviderHandler
from app.tools import number_tokens


@pytest.mark.parametrize('text,expected', [
    ('Рост -13.82%', '-13.82'), ('Рост −13,82%', '-13.82'),
    ('Рост +13.82%', '13.82'), ('Рост 13,82%', '13.82'),
    # Решение 3А.4: дефис с пробелом в тексте записки разделяет части фразы, минус стоит вплотную к числу.
    ('Значение - 1 234,56', '1234.56'), ('Значение -1 234,56', '-1234.56'),
    ('Значение \u2013 1 234,56', '1234.56'), ('Убыток \u201312', '-12'), ('Штамм COVID-19', '19'),
    ('Значение −1\u00a0234.56', '-1234.56'),
    ('Значение +1\u202f234,56', '1234.56'),
])
def test_signed_numerals(text, expected):
    assert number_tokens(text) == {Decimal(expected)}


# Смешение дефиса и тире у числа неоднозначно так же, как двойной знак и дефис между числами.
@pytest.mark.parametrize('text', ['--13.82', '+-13,82', '− -13.82', '13-14', '+ -12', '10\u2013-15', '10-\u201315'])
def test_ambiguous_sign_is_rejected(text):
    with pytest.raises(ApiError):
        number_tokens(text)


def test_short_dash_between_numbers_is_a_range():
    assert number_tokens('Маржа 10\u201315%') == {Decimal(10), Decimal(15)}
    assert number_tokens('Маржа 10%\u201315%') == {Decimal(10), Decimal(15)}
    assert number_tokens('2025\u20132027 годы') == {Decimal(2025), Decimal(2027)}


def test_pdf_quote_reads_a_word_hyphen_like_the_note_and_keeps_a_detached_sign():
    # Пересказ «среди топ-15» должен совпасть с числом своей цитаты PDF.
    assert number_tokens('среди топ-15 производителей', source_quote=True) == number_tokens('среди топ-15') == {Decimal(15)}
    assert number_tokens('Убыток - 15 млн руб.', source_quote=True) == {Decimal(-15)}


def test_dates_periods_and_grouping_keep_their_numerals():
    assert number_tokens('9M’21 2021-01-01 2021-09-30; 9M’22 2022-09-30') == {
        Decimal(n) for n in (9, 21, 2021, 1, 30, 22, 2022)}
    assert number_tokens('4 586; 7\u00a0193; 1\u202f234,56') == {
        Decimal('4586'), Decimal('7193'), Decimal('1234.56')}


class NoModel:
    token = 'synthetic-r1'
    profile = {'provider': 'novita'}
    calls = 0

    def model(self, body):
        self.calls += 1
        raise AssertionError('Incomplete requests must not dispatch')


@pytest.mark.parametrize('mode', ['partial_headers', 'partial_body', 'trickle_headers'])
def test_incomplete_http_closes_without_client_disconnect(mode, record_property):
    server = PrivateHTTPServer(('127.0.0.1', 0), ProviderHandler)
    server.boundary = NoModel()
    server.read_timeout = .25
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    serving.start()
    client = socket.create_connection(server.server_address, timeout=2)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (server.shutdown(), server.server_close(), closed.set()))
    raw = b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
    if mode == 'partial_body':
        raw += b'Authorization: Bearer synthetic-r1\r\nContent-Length: 10\r\n\r\n{'
    try:
        client.sendall(raw)
        # Wait for handler registration before exercising server_close.
        until = time.monotonic() + 2
        while not isinstance(server._threads, list) or not server._threads:
            assert time.monotonic() < until
            time.sleep(.005)
        handlers = list(server._threads)
        start = time.monotonic()
        closer.start()
        if mode == 'trickle_headers':
            while not closed.wait(.03):
                try: client.sendall(b'x')
                except OSError: break
                assert time.monotonic() - start < 1.5
        assert closed.wait(1.5)
        elapsed = time.monotonic() - start
        response = b''
        while chunk := client.recv(4096):
            response += chunk
        assert elapsed < 1.5
        assert server.boundary.calls == 0
        assert all(not handler.is_alive() for handler in handlers)
        record_property('case', mode)
        record_property('closed_seconds', round(elapsed, 4))
        record_property('model_calls', server.boundary.calls)
        record_property('connection_eof_before_client_close', True)
    finally:
        client.close()
        if not closer.ident: closer.start()
        closer.join(3); serving.join(3)
    assert not closer.is_alive() and not serving.is_alive()


def test_complete_request_disarms_timer_and_close_waits_for_late_handler():
    entered = threading.Event(); release = threading.Event(); accounted = threading.Event()
    closed = threading.Event()
    class Late:
        token = 'synthetic-r1'
        profile = {'provider': 'novita'}
        def model(self, body):
            entered.set()
            assert release.wait(3)
            accounted.set()
            return {'result': 'observed'}
    server = PrivateHTTPServer(('127.0.0.1', 0), ProviderHandler)
    server.boundary = Late(); server.read_timeout = .1
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    serving.start()
    client = socket.create_connection(server.server_address, timeout=2)
    closer = threading.Thread(target=lambda: (server.shutdown(), server.server_close(), closed.set()))
    try:
        client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer synthetic-r1\r\nContent-Length: 2\r\n\r\n{}')
        assert entered.wait(2)
        closer.start()
        assert not closed.wait(.3)
        release.set()
        assert closed.wait(2) and accounted.is_set()
        response = b''
        while chunk := client.recv(4096): response += chunk
        assert response.startswith(b'HTTP/1.0 200')
        assert json.loads(response.split(b'\r\n\r\n', 1)[1]) == {'result': 'observed'}
    finally:
        release.set(); client.close()
        if not closer.ident: closer.start()
        closer.join(3); serving.join(3)
    assert not closer.is_alive() and not serving.is_alive()
