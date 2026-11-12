"""Ограниченное чтение публичного HTTPS без cookies, прокси и переходов в частную сеть."""
import hashlib
import ipaddress
import re
import ssl
import time
import threading
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from .http_io import ReaderHTTPSConnection, connect_resolved, dns_addresses, resolve

MAX_URL_LENGTH = 2048
HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
REQUEST_TARGET = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9a-fA-F]{2})+")


def url_text(value):
    if (not isinstance(value, str) or not 0 < len(value) <= MAX_URL_LENGTH
            or any(not char.isprintable() or char.isspace() or char == '\\' for char in value)):
        raise ValueError('invalid URL text')


def public_url(value):
    url_text(value)
    parts = urlsplit(value)
    host, separator, port = parts.netloc.partition(':')
    if parts.scheme != 'https' or not host or (separator and port != '443'):
        raise ValueError('public HTTPS required')
    host = host.encode('idna').decode('ascii').lower()
    labels = host.split('.')
    if (len(host) > 253 or len(labels) < 2 or any(not HOST_LABEL.fullmatch(label) for label in labels)
            or labels[-1] in ('localhost', 'local', 'internal')
            or labels[-1].isdigit() or re.fullmatch(r'0x[0-9a-f]*', labels[-1])):
        raise ValueError('public host required')
    # Keep the original request target, including an explicitly empty query.
    target = (parts.path or '/') + ('?' + parts.query if '?' in value.split('#', 1)[0] else '')
    if target.startswith('//') or not REQUEST_TARGET.fullmatch(target):
        raise ValueError('invalid HTTPS request target')
    canonical = 'https://' + host + target
    if len(canonical) > MAX_URL_LENGTH:
        raise ValueError('normalized URL too long')
    return canonical


def same_public_url(value, canonical):
    try:
        return public_url(value) == canonical
    except ValueError:
        return False


def read(url, deadline, *, max_bytes=1_500_000, headers=None, xml=False, redirects=3):
    """Локальное HTTPS-чтение, переходы только в пределах исходного origin."""
    url = public_url(url)
    visited = set()
    for hop in range(redirects + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError('public read deadline')
        parts = urlsplit(url)
        visited.add(url)
        addresses = dns_addresses(parts.hostname, 443, deadline, resolver=resolve)
        ips = [ipaddress.ip_address(answer[4][0]) for answer in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise ValueError('non-public DNS answer')
        remaining = min(5, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('public read deadline')
        connection = ReaderHTTPSConnection(parts.hostname, timeout=remaining, context=ssl.create_default_context())
        connection._create_connection = lambda *args, **kwargs: connect_resolved(addresses, deadline)
        timer = threading.Timer(max(0, deadline - time.monotonic()), connection.interrupt)
        timer.daemon = True
        timer.start()
        data = bytearray()
        response = None
        try:
            connection.connect()
            if connection.interrupted or time.monotonic() >= deadline:
                raise TimeoutError('public read deadline')
            connection.request('GET', url[len('https://' + parts.netloc):],
                               headers={'User-Agent': 'Mozilla/5.0 (compatible; AIST/1.0)',
                                        'Accept-Encoding': 'identity', **(headers or {})})
            stream = connection.sock
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if hop == redirects:
                    raise ValueError('too many redirects')
                locations = response.headers.get_all('Location', [])
                if len(locations) != 1:
                    raise ValueError('missing or ambiguous redirect location')
                location = locations[0]
                url_text(location)
                if urlsplit(location).scheme:
                    target = public_url(location)
                else:
                    if location.startswith('///'):
                        raise ValueError('ambiguous redirect location')
                    relative = urljoin(url, location)
                    if location.split('#', 1)[0].endswith('?'):
                        relative = relative.split('#', 1)[0].split('?', 1)[0] + '?'
                    target = url if location.startswith('#') else public_url(relative)
                if urlsplit(target).netloc != parts.netloc or target in visited:
                    raise ValueError('redirect outside origin or cycle')
                url = target
                continue
            if response.status != 200:
                raise ValueError('public HTTP status ' + str(response.status))
            mime = response.getheader('Content-Type', '').split(';')[0].lower()
            if mime not in (('text/xml', 'application/xml', 'application/rss+xml') if xml else
                            ('text/html', 'application/xhtml+xml')):
                raise ValueError('unsupported public content type')
            while len(data) < max_bytes:
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('public read deadline')
                stream.settimeout(min(5, remaining))
                part = response.read1(min(65536, max_bytes - len(data)))
                if not part:
                    break
                data.extend(part)
            if connection.interrupted or time.monotonic() >= deadline:
                raise TimeoutError('public read deadline')
            if response.length not in (None, 0):
                raise ValueError('incomplete public response')
            if len(data) >= max_bytes:
                raise ValueError('public page too large')
            charset = response.headers.get_content_charset()
            try:
                if xml:
                    parser = etree.XMLParser(encoding=charset, resolve_entities=False, no_network=True)
                    doc = etree.fromstring(bytes(data), parser)
                else:
                    parser = html.HTMLParser(encoding=charset or 'utf-8')
                    doc = html.fromstring(bytes(data), parser=parser)
            except LookupError as error:
                raise ValueError('unsupported public content charset') from error
            return doc, hashlib.sha256(data).hexdigest(), url, len(data)
        except Exception as error:
            error.bytes_read = len(data) + len(getattr(error, 'partial', b''))
            raise
        finally:
            timer.cancel()
            timer.join()
            if response is not None:
                response.close()
            connection.close()
    raise ValueError('too many redirects')
