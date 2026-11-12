"""HTTP с прерываемым чтением и отдельным ограниченным DNS-запросом."""
import hashlib
import http.client
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


class ReaderConnection(http.client.HTTPConnection):
    """Retain the socket when http.client hands its ownership to HTTPResponse."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reader_socket = None
        self.interrupted = False
        self.reader_lock = threading.Lock()

    def connect(self):
        super().connect()
        self.track_reader(self.sock)

    def track_reader(self, sock):
        with self.reader_lock:
            self.reader_socket = sock
            if self.interrupted:
                self._shutdown_reader()

    def _shutdown_reader(self):
        if self.reader_socket is not None:
            try:
                self.reader_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def interrupt(self):
        with self.reader_lock:
            self.interrupted = True
            self._shutdown_reader()
        # The reader owns response.close(). Closing its BufferedReader here can
        # wait for the very read we must interrupt; shutdown wakes that read.


class ReaderHTTPSConnection(ReaderConnection, http.client.HTTPSConnection):
    def connect(self):
        # Регистрируем сокет до TLS: connect и handshake делят один общий срок.
        http.client.HTTPConnection.connect(self)
        self.track_reader(self.sock)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host,
                                             do_handshake_on_connect=False)
        self.track_reader(self.sock)
        self.sock.do_handshake()


# getaddrinfo нельзя отменить в потоке. Процесс не получает ключи или текст PDF.
DNS_COMMAND = "import json,socket,sys; print(json.dumps(socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)))"


def resolve(host, port, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('DNS deadline')
    try:
        result = subprocess.run([sys.executable, '-c', DNS_COMMAND, host, str(port)],
                                capture_output=True, timeout=remaining, check=True, env={'LANG': 'C.UTF-8'})
    except subprocess.TimeoutExpired as error:
        # subprocess.run kills and waits for its child before raising.
        raise TimeoutError('DNS deadline') from error
    except subprocess.CalledProcessError as error:
        raise OSError('DNS resolution failed') from error
    return json.loads(result.stdout)


def connect_resolved(addresses, deadline):
    """Перебирает только TCP-соединения с уже разрешенными числовыми адресами."""
    if not addresses:
        raise OSError('no DNS addresses')
    for index, (family, kind, protocol, _, address) in enumerate(addresses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('TCP deadline')
        sock = None
        try:
            sock = socket.socket(family, kind, protocol)
            # Оставляем время остальным адресам, если этот адрес не отвечает.
            sock.settimeout(min(5, remaining / (len(addresses) - index)))
            sock.connect(tuple(address))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('TCP deadline')
            sock.settimeout(remaining)
            return sock
        except OSError:
            if sock is not None:
                sock.close()
            if index == len(addresses) - 1:
                raise


def dns_addresses(host, port, deadline, *, resolver=resolve):
    """Проверяет числовые адреса и сохраняет порядок DNS без повторов."""
    addresses = []
    seen = set()
    answers = resolver(host, port, deadline)
    if not isinstance(answers, (list, tuple)):
        raise OSError('invalid DNS answers')
    for answer in answers:
        try:
            family, kind, protocol, _, address = answer
            ip = ipaddress.ip_address(address[0])
            if (not isinstance(address[0], str) or '%' in address[0]
                    or family != (socket.AF_INET if ip.version == 4 else socket.AF_INET6)
                    or kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP)
                    or type(address[1]) is not int or address[1] != port
                    or len(address) != (2 if ip.version == 4 else 4)
                    or any(type(number) is not int or not 0 <= number <= limit
                           for number, limit in zip(address[2:], (0xfffff, 0xffffffff)))):
                raise ValueError('invalid socket address')
            key = family, tuple(address)
        except (ValueError, TypeError, IndexError) as error:
            raise OSError('invalid DNS answer') from error
        if key not in seen:
            addresses.append(answer)
            seen.add(key)
    return addresses


def internal_json(url, deadline, max_bytes, *, body=None, headers=None, with_receipt=False):
    """Адрес сервиса задает приложение, не модель или публикация."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('invalid internal service URL')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    addresses = dns_addresses(parts.hostname, port, deadline, resolver=resolve)
    cls = ReaderHTTPSConnection if parts.scheme == 'https' else ReaderConnection
    connection = cls(parts.hostname, port, timeout=max(.001, deadline - time.monotonic()))
    connection._create_connection = lambda *args, **kwargs: connect_resolved(addresses, deadline)
    timer = threading.Timer(max(0, deadline - time.monotonic()), connection.interrupt)
    timer.daemon = True
    timer.start()
    response = None
    data = b''
    try:
        payload = json.dumps(body).encode() if body is not None else None
        request_headers = {'Content-Type': 'application/json'} if payload is not None else {}
        request_headers.update(headers or {})
        connection.connect()
        if connection.interrupted or time.monotonic() >= deadline:
            raise TimeoutError('internal HTTP deadline')
        connection.request('POST' if payload is not None else 'GET',
                           parts.path + ('?' + parts.query if parts.query else ''), body=payload, headers=request_headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('internal HTTP status')
        data = response.read(max_bytes + 1)
        if connection.interrupted or time.monotonic() >= deadline:
            raise TimeoutError('internal HTTP deadline')
        if len(data) > max_bytes or response.length not in (None, 0):
            raise ValueError('incomplete or oversized internal response')
        value = json.loads(data)
        return (value, dict(bytes_read=len(data), sha256=hashlib.sha256(data).hexdigest())) if with_receipt else value
    except (ValueError, http.client.IncompleteRead) as error:
        error.bytes_read = len(data) + len(getattr(error, 'partial', b''))
        raise
    finally:
        timer.cancel()
        timer.join()
        if response is not None:
            response.close()
        connection.close()
