"""The embedding client's first-use cost must be payable off the request path.

``import google.genai`` measured 3.52 s and ``genai.Client(...)`` 1.31 s on the operator
machine, and both were paid inside the first embedding-needing call: a fresh ``search``
took 5.02 s against 0.76 s for the next one in the same server process.  The prewarm is
therefore an optimisation with a *fallback* contract, and these tests pin both halves of
it: it must never raise into a server start, and it must never run when there is nothing
to warm.
"""

from __future__ import annotations

import threading
import time

import pytest

from vector_lake import embedding_scheduler


@pytest.fixture(autouse=True)
def _clean_client_cache(monkeypatch):
    # Prewarming exists for the SDK path only: the REST transport (the default) never builds a
    # client, so these tests pin the transport they are about.
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")
    embedding_scheduler.reset_client_cache(reopen=True)
    yield
    embedding_scheduler.reset_client_cache(reopen=True)


def test_prewarm_is_skipped_without_an_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert embedding_scheduler.prewarm_client() is False
    assert embedding_scheduler.start_prewarm_thread() is None


def test_prewarm_can_be_disabled_by_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_PREWARM", "off")
    assert embedding_scheduler.start_prewarm_thread() is None


def test_prewarm_swallows_a_client_construction_failure(monkeypatch):
    """A server that cannot build the client still has to serve lexical search."""

    def _explode():
        raise RuntimeError("no credentials in this environment")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(embedding_scheduler, "_create_client", _explode)
    monkeypatch.setattr(embedding_scheduler, "_DEFAULT_SDK_FACTORY", _explode)

    assert embedding_scheduler.prewarm_client() is False


def test_prewarm_caches_the_client_so_the_next_call_is_free(monkeypatch):
    builds = []

    class _Client:
        def close(self):
            pass

    def _factory():
        builds.append(1)
        return _Client()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(embedding_scheduler, "_create_client", _factory)
    monkeypatch.setattr(embedding_scheduler, "_DEFAULT_SDK_FACTORY", _factory)

    assert embedding_scheduler.prewarm_client() is True
    assert len(builds) == 1
    # The read path's own accessor must be served from the prewarmed cache.
    with embedding_scheduler._borrow_sdk_client() as client:
        assert isinstance(client, _Client)
    assert len(builds) == 1


def test_thread_is_daemonic_and_started_once(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_PREWARM", "on")
    started = threading.Event()

    def _slow_prewarm():
        started.set()
        time.sleep(0.05)
        return False

    monkeypatch.setattr(embedding_scheduler, "prewarm_client", _slow_prewarm)

    thread = embedding_scheduler.start_prewarm_thread()
    assert thread is not None
    assert thread.daemon is True
    assert thread.name == "embedding-client-prewarm"
    assert started.wait(timeout=5)
    thread.join(timeout=5)
