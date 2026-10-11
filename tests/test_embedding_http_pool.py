"""Mock wire contract and real HTTP/1.1 accept-count oracles, dummy data only."""
import ast
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import urllib.request

import httpx
import pytest

from vector_lake import embedding_scheduler as es


@pytest.fixture(autouse=True)
def pooled(monkeypatch):
    es.reset_client_cache(reopen=True)
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy-http-key')
    monkeypatch.setenv(es.EMBEDDING_TRANSPORT_ENV, 'rest-httpx')
    monkeypatch.setenv('VECTOR_LAKE_EMBEDDING_TIMEOUT_MS', '1000')
    monkeypatch.setattr(urllib.request, 'getproxies', dict)
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda _host: True)
    yield
    es.reset_client_cache(reopen=True)


def mock_wire(monkeypatch, handler):
    built = []

    def transport(**kwargs):
        built.append(kwargs)
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, 'HTTPTransport', transport)
    return built


def response(request, status=200, payload=None, **kwargs):
    return httpx.Response(status, json=payload if payload is not None else {'embeddings': [{'values': [1.0, 2.0]}]}, request=request, **kwargs)


def test_current_model_dimension_and_key_are_per_request_not_pool_identity(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        values = [1.0, 2.0] if len(requests) == 1 else [3.0]
        return response(request, payload={'embeddings': [{'values': values}]})

    built = mock_wire(monkeypatch, handler)
    assert es._httpx_embed_contents(['first'], es.EmbeddingRateConfig(model='synthetic-a', dimension=2)) == [[1.0, 2.0]]
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy-second')
    assert es._httpx_embed_contents(['second'], es.EmbeddingRateConfig(model='synthetic-b', dimension=1)) == [[3.0]]
    assert len(built) == 1
    assert [r.headers['x-goog-api-key'] for r in requests] == ['dummy-http-key', 'dummy-second']
    assert [json.loads(r.content)['requests'][0]['model'] for r in requests] == ['models/synthetic-a', 'models/synthetic-b']
    assert all('dummy-' not in str(r.url) for r in requests)
    assert all(r.headers['accept-encoding'] == 'identity' for r in requests)
    assert all('cookie' not in r.headers for r in requests)


def test_concurrent_dummy_keys_do_not_mix(monkeypatch):
    first_read, proceed = threading.Event(), threading.Event()
    original_route = es._httpx_route
    seen = {}

    def route(endpoint):
        if threading.current_thread().name.startswith('first'):
            first_read.set()
            assert proceed.wait(2)
        return original_route(endpoint)

    def handler(request):
        text = json.loads(request.content)['requests'][0]['content']['parts'][0]['text']
        seen[text] = request.headers['x-goog-api-key']
        return response(request)

    built = mock_wire(monkeypatch, handler)
    monkeypatch.setattr(es, '_httpx_route', route)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='first') as pool:
        old = pool.submit(es._httpx_embed_contents, ['first'], es.EmbeddingRateConfig(dimension=2))
        assert first_read.wait(1)
        monkeypatch.setenv('GEMINI_API_KEY', 'dummy-concurrent-second')
        try:
            es._httpx_embed_contents(['second'], es.EmbeddingRateConfig(dimension=2))
        finally:
            proceed.set()
        old.result(3)
    assert seen == {'first': 'dummy-http-key', 'second': 'dummy-concurrent-second'}
    assert len(built) == 1


def test_no_cookies_forwarded_and_no_redirect_to_second_origin(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return response(request, headers={'set-cookie': 'dummy_cookie=yes; Path=/'}) if len(requests) < 3 else response(request, 302, headers={'location': 'https://second.invalid/collect'})

    mock_wire(monkeypatch, handler)
    config = es.EmbeddingRateConfig(dimension=2)
    for _ in range(2):
        es._httpx_embed_contents(['x'], config)
    with pytest.raises(RuntimeError, match='HTTP 302'):
        es._httpx_embed_contents(['x'], config)
    assert len(requests) == 3
    assert all(r.url.host != 'second.invalid' and 'cookie' not in r.headers for r in requests)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 401, 429, 503])
def test_http_errors_preserve_status_quota_without_echo_key(monkeypatch, status):
    mock_wire(monkeypatch, lambda r: httpx.Response(status, content=b'dummy-http-key quota', request=r))
    with pytest.raises(RuntimeError, match=f'HTTP {status}') as caught:
        es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))
    assert 'quota' in str(caught.value)
    assert 'dummy-http-key' not in str(caught.value)


@pytest.mark.parametrize('payload,error', [(b'{', json.JSONDecodeError), (b'{"embeddings":[]}', es.EmbeddingResponseError), (b'{"embeddings":[{"values":[1]}]}', es.EmbeddingResponseError)])
def test_invalid_json_count_dimension_fail_not_empty(monkeypatch, payload, error):
    mock_wire(monkeypatch, lambda r: httpx.Response(200, content=payload, request=r))
    with pytest.raises(error):
        es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout])
def test_transport_faults_have_no_fallback(monkeypatch, error):
    def handler(request):
        raise error('dummy-wire', request=request)

    mock_wire(monkeypatch, handler)
    monkeypatch.setattr(es, '_rest_embed_contents', lambda *_a, **_k: pytest.fail('urllib fallback'))
    monkeypatch.setattr(es, '_create_client', lambda: pytest.fail('SDK fallback'))
    with pytest.raises(RuntimeError, match=error.__name__):
        es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))


@pytest.mark.parametrize('missing', ['httpx', 'httpcore'])
def test_optional_top_level_missing_vs_broken_import(monkeypatch, missing):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == 'httpx':
            raise ModuleNotFoundError('synthetic missing', name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', importing)
    with pytest.raises(RuntimeError if missing == 'httpx' else ModuleNotFoundError) as caught:
        es._load_httpx()
    assert ('optional httpx' in str(caught.value)) == (missing == 'httpx')


def test_tls_transport_settings_use_verifying_system_context_and_zero_retries(monkeypatch):
    built = mock_wire(monkeypatch, response)
    es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))
    settings = built[0]
    assert isinstance(settings['verify'], ssl.SSLContext)
    assert settings['verify'].verify_mode == ssl.CERT_REQUIRED
    assert settings['verify'].check_hostname is True
    assert settings['retries'] == 0
    assert settings['trust_env'] is False
    assert settings['http1'] is True and settings['http2'] is False
    assert settings['limits'].max_connections == settings['limits'].max_keepalive_connections == 8


def test_stdlib_windows_registry_routing_uses_only_injected_values(monkeypatch):
    """Execute stdlib Windows functions portably against fake winreg, no OS read."""
    config = {
        'ProxyEnable': 1,
        'ProxyServer': 'https=dummy-user:dummy-pass@proxy.invalid:8080',
        'ProxyOverride': '*.googleapis.com;<local>',
    }
    reads = []
    fake_key = SimpleNamespace(Close=lambda: None)

    def query(_key, name):
        reads.append(name)
        return config[name], 1

    fake = SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=lambda *_a: fake_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    tree = ast.parse(Path(urllib.request.__file__).read_text(encoding='utf-8'))
    names = {'getproxies_registry', 'proxy_bypass_registry', '_proxy_bypass_winreg_override'}
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    namespace: dict[str, Any] = dict(vars(urllib.request))
    # Trusted stdlib AST supplies the real parser; all registry I/O is replaced.
    exec(compile(ast.Module(body=cast(list[ast.stmt], nodes), type_ignores=[]), '<stdlib-windows-proxy>', 'exec'), namespace)  # noqa: S102
    monkeypatch.setattr(urllib.request, 'getproxies', namespace['getproxies_registry'])
    monkeypatch.setattr(urllib.request, 'proxy_bypass', namespace['proxy_bypass_registry'])
    settings = mock_wire(monkeypatch, response)
    es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))
    assert settings[-1]['proxy'] is None
    config['ProxyOverride'] = 'different.invalid'
    es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))
    assert settings[-1]['proxy'] == 'http://dummy-user:dummy-pass@proxy.invalid:8080'
    assert len(settings) == 2
    assert set(reads) == {'ProxyEnable', 'ProxyOverride', 'ProxyServer'}


def test_fake_windows_proxy_bypass_auth_and_origin_rotations(monkeypatch):
    settings = mock_wire(monkeypatch, response)
    bypass, proxy = [False], ['http://dummy-user:dummy-pass@proxy.invalid:8080']
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: bypass[0])
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'https': proxy[0], 'http': proxy[0]})
    config = es.EmbeddingRateConfig(dimension=2)
    es._httpx_embed_contents(['x'], config)
    assert settings[-1]['proxy'] == proxy[0]
    bypass[0] = True
    es._httpx_embed_contents(['x'], config)
    assert len(settings) == 2 and settings[-1]['proxy'] is None
    monkeypatch.setattr(es, 'REST_ENDPOINT', 'http://127.0.0.1:12345/{model}')
    es._httpx_embed_contents(['x'], config)
    assert len(settings) == 3
    # NO_PROXY is synthetic and prevents actual registry reads; injected bypass
    # models urllib's registry/ProxyOverride decision, not a live registry test.
    assert cast(Any, urllib.request).proxy_bypass_environment('example.invalid', {'no': '*'})


@pytest.mark.parametrize('proxy', ['socks5://proxy.invalid', 'http://proxy.invalid/path', 'http://proxy.invalid:bad', {'invalid': True}])
def test_incompatible_proxy_fails_before_send(monkeypatch, proxy):
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda _host: False)
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'https': proxy})
    monkeypatch.setattr(es, '_create_httpx_client', lambda *_a: pytest.fail('bad proxy reached constructor'))
    with pytest.raises(RuntimeError, match='incompatible'):
        es._httpx_embed_contents(['x'], es.EmbeddingRateConfig(dimension=2))


@contextmanager
def loopback(mode='normal'):
    class Server(ThreadingHTTPServer):
        daemon_threads = False

        def __init__(self, *args):
            self.accepted = 0
            self.requests = []
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()
            super().__init__(*args)

        def get_request(self):
            conn, addr = super().get_request()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(1)
            with self.lock:
                self.accepted += 1
            return conn, addr

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            server = cast(Server, self.server)
            body = self.rfile.read(int(self.headers['Content-Length']))
            with server.lock:
                server.requests.append((json.loads(body), self.headers.get('x-goog-api-key')))
                number = len(server.requests)
            if mode == 'disconnect' and number == 1:
                self.close_connection = True
                return
            data = json.dumps({'embeddings': [{'values': [1.0, 2.0]}]}).encode()
            try:
                if mode == 'headers':
                    time.sleep(0.2)
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if mode == 'body':
                    time.sleep(0.2)
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            pass

    server = Server(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
    worker.start()
    try:
        yield server
    finally:
        es.reset_client_cache(reopen=True)
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()


def test_dispatch_limiter_matches_base_reservation_contract(monkeypatch):
    def request(client, contents, request_tokens, config, limiter, **kwargs):
        assert client is None and contents == ['public synthetic'] and request_tokens == 7
        assert isinstance(limiter, es.MinuteRateLimiter)
        assert kwargs['durable_reservation'] is False
        assert limiter.reserve(request_tokens=11) is None
        assert limiter.reserve(12, 123.25, False) is None
        assert limiter.reserve(13, 456.5) is None
        assert limiter.reserve(14, durable=False) is None
        assert limiter.reserve(15, deadline=789.75, durable=True) is None
        return [[1.0, 2.0]]

    monkeypatch.setattr(es, '_request_embeddings', request)
    values, reservations = dispatch(monkeypatch, SimpleNamespace(server_port=1), 'rest-httpx')
    assert values == [[1.0, 2.0]]
    assert reservations == [
        (11, {'deadline': None, 'durable': True}),
        (12, {'deadline': 123.25, 'durable': False}),
        (13, {'deadline': 456.5, 'durable': True}),
        (14, {'deadline': None, 'durable': False}),
        (15, {'deadline': 789.75, 'durable': True}),
    ]


def dispatch(monkeypatch, server, transport, budget: float = 2, retries=0):
    monkeypatch.setattr(es, 'REST_ENDPOINT', f'http://127.0.0.1:{server.server_port}/{{model}}')
    monkeypatch.setenv(es.EMBEDDING_TRANSPORT_ENV, transport)
    config = es.EmbeddingRateConfig(dimension=2, max_retries=retries)
    reservations = []
    class Limiter(es.MinuteRateLimiter):
        def reserve(self, request_tokens: int, deadline: float | None = None, durable: bool = True):
            reservations.append((request_tokens, {'deadline': deadline, 'durable': durable}))

    limiter = Limiter(config)
    values = es._request_embeddings(None, ['public synthetic'], 7, config, limiter, budget_seconds=budget, durable_reservation=False)
    return values, reservations


def test_real_http11_interleaved_reuse_concurrent_reset_drain(monkeypatch, record_property):
    counts = {}
    for transport in ['rest', 'rest-httpx']:
        with loopback() as server:
            times = []
            for _ in range(6):
                started = time.perf_counter()
                values, reservations = dispatch(monkeypatch, server, transport)
                times.append((time.perf_counter() - started) * 1000)
                assert values == [[1.0, 2.0]] and len(reservations) == 1
                assert reservations[0][1]['durable'] is False
            counts[transport] = server.accepted
            assert len(server.requests) == 6
            record_property(transport + '_sequential_ms', json.dumps(times))
            record_property(transport + '_sequential_accepts', server.accepted)
            if transport == 'rest-httpx':
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(dispatch, monkeypatch, server, transport) for _ in range(4)]
                    assert all(f.result(3)[0] == [[1.0, 2.0]] for f in futures)
                before_reset = server.accepted
                es.reset_client_cache()
                dispatch(monkeypatch, server, transport)
                assert server.accepted == before_reset + 1
                assert len(server.requests) == 11
                record_property('concurrent_reset_total_accepts', server.accepted)
    assert counts == {'rest': 6, 'rest-httpx': 1}


@pytest.mark.parametrize('mode', ['headers', 'body'])
@pytest.mark.parametrize('transport', ['rest', 'rest-httpx'])
def test_real_slow_response_deadline_no_extra_requests(monkeypatch, mode, transport, record_property):
    with loopback(mode) as server:
        # This oracle measures wire inactivity, not cold TLS/client acquisition.
        # A 50ms cold budget may legitimately expire before any POST is issued.
        dispatch(monkeypatch, server, transport)
        before_requests = len(server.requests)
        before_accepts = server.accepted
        started = time.perf_counter()
        with pytest.raises(es.EmbeddingBudgetExceeded):
            dispatch(monkeypatch, server, transport, budget=0.05, retries=2)
        elapsed = time.perf_counter() - started
        assert elapsed < 1
        assert len(server.requests) == before_requests + 1, (len(server.requests), before_requests)
        expected_accepts = before_accepts + (1 if transport == 'rest' else 0)
        assert server.accepted == expected_accepts, (server.accepted, expected_accepts)
        record_property(f'{transport}_{mode}_elapsed_ms', elapsed * 1000)
        record_property('actual_POSTs_in_deadline_attempt', len(server.requests) - before_requests)


def test_server_disconnect_has_no_hidden_httpx_retry(monkeypatch):
    with loopback('disconnect') as server:
        sleeps = []
        monkeypatch.setattr(es.time, 'sleep', lambda seconds: sleeps.append(seconds))
        values, reservations = dispatch(monkeypatch, server, 'rest-httpx', retries=1)
        assert values == [[1.0, 2.0]]
        assert len(server.requests) == len(reservations) == server.accepted == 2
        assert sleeps == [1.0]


def test_real_inflight_reset_serves_new_socket_before_old_response(monkeypatch, record_property):
    """Reset must retire a real in-flight connection, not cancel or reuse it."""
    import hashlib

    entered, release = threading.Event(), threading.Event()
    events, posts, clients, close_calls, reservations = [], [], [], [], []
    lock = threading.Lock()
    original_factory = es._create_httpx_client
    monkeypatch.setenv('VECTOR_LAKE_EMBEDDING_TIMEOUT_MS', '3000')

    def event(name, **details):
        with lock:
            events.append({'name': name, 'ns': time.perf_counter_ns(), **details})

    def generations():
        with es._HTTPX_CLIENTS.condition:
            return [{'epoch': g.epoch, 'state': g.state, 'borrowers': g.borrowers,
                     'client_id': next((i + 1 for i, c in enumerate(clients) if c is g.client), None)}
                    for g in es._HTTPX_CLIENTS.generations]

    def factory(proxy, timeout):
        client = original_factory(proxy, timeout)
        clients.append(client)
        identity = len(clients)
        original_close = client.close
        event('client_constructed', client_id=identity)

        def tracked_close():
            snapshot = generations()
            close_calls.append({'client_id': identity, 'generations': snapshot,
                                'ns': time.perf_counter_ns()})
            event('close_begin', client_id=identity)
            original_close()
            event('close_complete', client_id=identity, is_closed=client.is_closed)

        monkeypatch.setattr(client, 'close', tracked_close)
        return client

    monkeypatch.setattr(es, '_create_httpx_client', factory)

    class Server(ThreadingHTTPServer):
        daemon_threads = False

        def __init__(self, *args):
            self.connections = {}
            self.accepts = []
            self.workers = []
            super().__init__(*args)

        def get_request(self):
            conn, address = super().get_request()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(1)
            identity = len(self.accepts) + 1
            self.connections[conn] = identity
            self.accepts.append({'id': identity, 'ns': time.perf_counter_ns(),
                                 'peer_port': address[1]})
            event('accepted', connection_id=identity)
            return conn, address

        def process_request_thread(self, request, client_address):
            self.workers.append(threading.current_thread())
            super().process_request_thread(request, client_address)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            server = cast(Server, self.server)
            body = self.rfile.read(int(self.headers['Content-Length']))
            payload = json.loads(body)
            text = payload['requests'][0]['content']['parts'][0]['text']
            identity = server.connections[self.request]
            posts.append({'connection_id': identity, 'text': text,
                          'ns': time.perf_counter_ns(), 'body_sha256': hashlib.sha256(body).hexdigest()})
            event('POST_received', connection_id=identity, text=text)
            assert self.headers['x-goog-api-key'] == 'dummy-http-key'
            if text == 'held synthetic':
                event('old_response_held', connection_id=identity)
                entered.set()
                if not release.wait(4):
                    event('server_hold_expired', connection_id=identity)
                    self.close_connection = True
                    return
                values = [1.0, 2.0]
            else:
                assert text == 'new synthetic'
                values = [3.0, 4.0]
            data = json.dumps({'embeddings': [{'values': values}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            event('response_sent', connection_id=identity)

        def log_message(self, format, *args):
            pass

    server = Server(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
    worker.start()
    monkeypatch.setattr(es, 'REST_ENDPOINT', f'http://127.0.0.1:{server.server_port}/{{model}}')
    config = es.EmbeddingRateConfig(dimension=2, max_retries=0)

    class Limiter(es.MinuteRateLimiter):
        def reserve(self, request_tokens, deadline=None, durable=True):
            reservations.append({'tokens': request_tokens, 'durable': durable,
                                 'ns': time.perf_counter_ns()})

    limiter = Limiter(config)

    def request(text):
        return es._request_embeddings(None, [text], 7, config, limiter,
                                      budget_seconds=5, durable_reservation=False)

    snapshots = {}
    before_release = {}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(request, 'held synthetic')
            try:
                assert entered.wait(2), 'old real POST never reached held server'
                assert len(clients) == len(server.accepts) == len(posts) == 1
                snapshots['before_reset'] = generations()
                event('reset_begin')
                es.reset_client_cache()
                event('reset_complete')
                snapshots['after_reset'] = generations()
                assert not clients[0].is_closed and close_calls == []
                assert old.running() and not old.done()
                assert request('new synthetic') == [[3.0, 4.0]]
                event('new_request_complete')
                snapshots['after_new_response'] = generations()
                before_release = {'old_running': old.running(), 'old_done': old.done(),
                                  'old_client_closed': clients[0].is_closed,
                                  'accepted_ids': [a['id'] for a in server.accepts],
                                  'POST_connection_ids': [p['connection_id'] for p in posts],
                                  'close_calls': len(close_calls)}
                assert before_release == {'old_running': True, 'old_done': False,
                                          'old_client_closed': False, 'accepted_ids': [1, 2],
                                          'POST_connection_ids': [1, 2], 'close_calls': 0}
                conn = next(c for c, identity in server.connections.items() if identity == 1)
                conn.settimeout(0.02)
                try:
                    with pytest.raises(socket.timeout):
                        conn.recv(1, socket.MSG_PEEK)
                    event('old_socket_has_no_early_eof')
                finally:
                    conn.settimeout(1)
            finally:
                event('release_old_response')
                release.set()
            assert old.result(timeout=5) == [[1.0, 2.0]]
            event('old_request_complete')
        snapshots['after_old_release'] = generations()
        assert clients[0].is_closed and not clients[1].is_closed
        assert [c['client_id'] for c in close_calls] == [1]
        retired = snapshots['after_reset']
        assert len(retired) == 1 and retired[0]['state'] == 'retired' and retired[0]['borrowers'] == 1
        assert len(snapshots['after_new_response']) == 2
        new_generation = next(g for g in snapshots['after_new_response'] if g['client_id'] == 2)
        assert new_generation['epoch'] > retired[0]['epoch'] and new_generation['state'] == 'active'
        assert len(snapshots['after_old_release']) == 1
        assert snapshots['after_old_release'][0]['client_id'] == 2
        es.shutdown_embedding_clients()
        es.shutdown_embedding_clients()
        snapshots['after_shutdown'] = generations()
        assert snapshots['after_shutdown'] == []
        assert [c['client_id'] for c in close_calls] == [1, 2]
        assert len(posts) == len(reservations) == len(server.accepts) == 2
        assert all(not r['durable'] for r in reservations)
        assert all(c.is_closed for c in clients)
        for close in close_calls:
            resource = next(g for g in close['generations'] if g['client_id'] == close['client_id'])
            assert resource['borrowers'] == 0 and resource['state'] == 'closing'
        names = [e['name'] for e in events]
        assert names.index('new_request_complete') < names.index('release_old_response') < names.index('close_begin')
        assert 'server_hold_expired' not in names
    finally:
        release.set()
        es.reset_client_cache(reopen=True)
        for client in clients:
            if not client.is_closed:
                client.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()
        assert all(not thread.is_alive() for thread in server.workers)
    record_property('live_inflight_reset', json.dumps({
        'module_path': es.__file__, 'source_sha256': hashlib.sha256(Path(es.__file__).read_bytes()).hexdigest(),
        'events': events, 'accepted_connections': server.accepts, 'POSTs': posts,
        'reservations': reservations, 'before_release': before_release,
        'generations': snapshots, 'close_calls': close_calls,
        'real_httpx_clients': [type(c).__module__ + '.' + type(c).__name__ for c in clients],
        'server_worker_threads_joined': len(server.workers), 'all_clients_closed': all(c.is_closed for c in clients),
    }))
