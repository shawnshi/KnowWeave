"""Synthetic lifecycle oracles: observable construction/close, not cache shape."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast
import threading
import time

import pytest

from vector_lake import embedding_scheduler as es


class Client:
    def __init__(self, on_close=None):
        self.closes = 0
        self.on_close = on_close
        self.models = SimpleNamespace()

    def close(self):
        self.closes += 1
        if self.on_close:
            self.on_close()


@pytest.fixture(autouse=True)
def lifecycle(monkeypatch):
    es.reset_client_cache(reopen=True)
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy-lifecycle')
    monkeypatch.setenv(es.EMBEDDING_TRANSPORT_ENV, 'sdk')
    monkeypatch.setenv('VECTOR_LAKE_EMBEDDING_TIMEOUT_MS', '1000')
    yield
    es.reset_client_cache(reopen=True)


def test_prewarm_races_first_requests_construct_once(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    built = []

    def factory():
        client = Client()
        built.append(client)
        entered.set()
        assert release.wait(2)
        return client

    monkeypatch.setattr(es, '_create_client', factory)
    monkeypatch.setattr(es, '_DEFAULT_SDK_FACTORY', factory)

    def request():
        with es._borrow_sdk_client(deadline=time.monotonic() + 2) as client:
            assert client.closes == 0
            return client

    with ThreadPoolExecutor(max_workers=5) as pool:
        warm = pool.submit(es.prewarm_client)
        assert entered.wait(1)
        futures = [pool.submit(request) for _ in range(4)]
        release.set()
        assert warm.result(3) is True
        assert all(f.result(3) is built[0] for f in futures)
    assert len(built) == 1
    es.reset_client_cache()
    assert built[0].closes == 1


def test_reset_drains_last_old_lease_then_new_generation(monkeypatch):
    built = []

    def factory():
        built.append(Client())
        return built[-1]

    monkeypatch.setattr(es, '_create_client', factory)
    with es._borrow_sdk_client() as old:
        with es._borrow_sdk_client() as other_old:
            assert other_old is old
            es.reset_client_cache()
            with es._borrow_sdk_client() as new:
                assert new is not old
            assert old.closes == 0
        assert old.closes == 0
    assert old.closes == 1
    es.reset_client_cache()
    assert [c.closes for c in built] == [1, 1]


@pytest.mark.parametrize('operation', ['reset', 'shutdown'])
def test_late_constructor_is_closed_never_published(monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    client = Client()

    def factory():
        entered.set()
        assert release.wait(2)
        return client

    monkeypatch.setattr(es, '_create_client', factory)

    def request():
        with es._borrow_sdk_client():
            pytest.fail('invalidated constructor was published')

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        assert entered.wait(1)
        (es.reset_client_cache if operation == 'reset' else es.shutdown_embedding_clients)()
        release.set()
        with pytest.raises(es.EmbeddingClientLifecycleError):
            future.result(3)
    assert client.closes == 1


def test_two_live_generations_block_third_until_drain(monkeypatch):
    built = []

    def factory():
        built.append(Client())
        return built[-1]

    monkeypatch.setattr(es, '_create_client', factory)
    with es._borrow_sdk_client() as first:
        es.reset_client_cache()
        with es._borrow_sdk_client() as second:
            es.reset_client_cache()
            with (
                pytest.raises(es.EmbeddingBudgetExceeded),
                es._borrow_sdk_client(deadline=time.monotonic() + 0.03),
            ):
                pytest.fail('third generation admitted')
            assert len(built) == 2
            assert first.closes == second.closes == 0
        assert second.closes == 1
        with es._borrow_sdk_client() as third:
            assert third is not first
            assert len(built) == 3
    assert first.closes == 1


def test_closing_generation_is_counted_and_close_is_outside_lock():
    manager = es._ClientManager()
    closing, release = threading.Event(), threading.Event()

    def slow_close():
        closing.set()
        assert release.wait(2)

    first = Client(slow_close)
    with manager.borrow('a', lambda: first):
        pass
    with ThreadPoolExecutor(max_workers=1) as pool:
        reset = pool.submit(manager.invalidate)
        assert closing.wait(1)
        try:
            with manager.borrow('b', Client) as second:
                manager.invalidate()
                with (
                    pytest.raises(es.EmbeddingBudgetExceeded),
                    manager.borrow('c', Client, deadline=time.monotonic() + 0.03),
                ):
                    pytest.fail('closing slot not counted')
                assert second.closes == 0
        finally:
            release.set()
        reset.result(3)
    assert first.closes == second.closes == 1


def test_close_failure_retains_slot_does_not_mask_request(caplog):
    manager = es._ClientManager()

    def fail_close():
        raise RuntimeError('dummy-secret-must-not-log')

    clients = [Client(fail_close), Client(fail_close)]
    for i, client in enumerate(clients):
        with (
            pytest.raises(ValueError, match='original request'),
            manager.borrow(i, lambda client=client: client),
        ):
            manager.invalidate()
            raise ValueError('original request')
    with (
        pytest.raises(es.EmbeddingBudgetExceeded),
        manager.borrow('third', Client, deadline=time.monotonic() + 0.02),
    ):
        pytest.fail('close-failed resources unbounded')
    assert [c.closes for c in clients] == [1, 1]
    assert 'dummy-secret' not in caplog.text
    assert 'close_failed' in caplog.text


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_factory_failures_free_constructing_slot(error):
    manager = es._ClientManager()

    def fail():
        raise error('synthetic')

    with pytest.raises(error), manager.borrow('a', fail):
        pass
    with manager.borrow('a', Client) as client:
        assert client.closes == 0
    manager.invalidate()
    assert client.closes == 1


def test_sdk_factory_and_base_timeout_rotate_but_key_does_not(monkeypatch):
    built = []

    def factory():
        built.append(Client())
        return built[-1]

    monkeypatch.setattr(es, '_create_client', factory)
    with es._borrow_sdk_client() as first:
        pass
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy-next')
    with es._borrow_sdk_client() as same:
        assert same is first
    monkeypatch.setenv('VECTOR_LAKE_EMBEDDING_TIMEOUT_MS', '1500')
    with es._borrow_sdk_client() as timed:
        assert timed is not first
    assert first.closes == 1
    monkeypatch.setattr(es, '_create_client', Client)
    with es._borrow_sdk_client() as replacement:
        assert replacement is not timed
    assert timed.closes == 1


def test_shutdown_requires_explicit_reopen_and_drains_active(monkeypatch):
    monkeypatch.setattr(es, '_create_client', Client)
    with es._borrow_sdk_client() as old:
        es.shutdown_embedding_clients()
        es.shutdown_embedding_clients()
        es.reset_client_cache()
        assert old.closes == 0
        with pytest.raises(es.EmbeddingClientLifecycleError), es._borrow_sdk_client():
            pass
    assert old.closes == 1
    es.reset_client_cache(reopen=True)
    with es._borrow_sdk_client() as new:
        assert new is not old


@pytest.mark.parametrize('method', ['borrow', 'invalidate'])
def test_simulated_pid_mismatch_refuses_before_inherited_lock(method):
    manager = es._ClientManager()

    class InheritedLock:
        def __enter__(self):
            pytest.fail('inherited lock accessed')

    manager.condition = cast(Any, InheritedLock())
    manager.pid = -1
    with pytest.raises(es.EmbeddingClientLifecycleError, match='spawn'):
        if method == 'borrow':
            with manager.borrow('a', Client):
                pass
        else:
            manager.invalidate()


def test_injected_sdk_is_caller_owned_even_on_failure(monkeypatch):
    client = Client()
    client.models = SimpleNamespace(embed_content=lambda **_kwargs: (_ for _ in ()).throw(ValueError('synthetic')))
    monkeypatch.setattr(es, '_provider_contents', lambda x: x)
    class Limiter(es.MinuteRateLimiter):
        def reserve(self, *_args, **_kwargs):
            pass

    limiter = Limiter(es.EmbeddingRateConfig())
    with pytest.raises(ValueError, match='synthetic'):
        es._request_embeddings(client, ['x'], 1, es.EmbeddingRateConfig(dimension=2, max_retries=0), limiter)
    es.reset_client_cache()
    es.shutdown_embedding_clients()
    assert client.closes == 0


@pytest.mark.parametrize('name', ['GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_GENAI_USE_ENTERPRISE', 'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_GENAI_CLIENT_MODE', 'GOOGLE_GEMINI_BASE_URL'])
def test_unsafe_prewarm_configuration_skips_without_sdk_or_adc(monkeypatch, name):
    monkeypatch.setenv(name, 'synthetic')
    assert es.prewarm_client() is False
    assert es.start_prewarm_thread() is None
