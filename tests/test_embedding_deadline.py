"""VL-A05: caller deadlines cover preparation and provider attempts."""
import http.server
import json
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from vector_lake import embedding_scheduler as scheduler


@pytest.fixture(autouse=True)
def _lifecycle():
    scheduler.reset_client_cache(reopen=True)
    yield
    scheduler.reset_client_cache(reopen=True)


class Limiter(scheduler.MinuteRateLimiter):
    def __init__(self):
        pass

    def reserve(self, *_args, **_kwargs):
        pass


def _configure(monkeypatch, transport="sdk"):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TRANSPORT", transport)
    config = scheduler.EmbeddingRateConfig(dimension=2, max_retries=2)
    monkeypatch.setattr(scheduler, "load_embedding_rate_config", lambda: config)
    monkeypatch.setattr(scheduler, "_provider_contents", lambda texts: list(texts))
    monkeypatch.setattr(scheduler, "MinuteRateLimiter", lambda _config: Limiter())
    return config


def test_client_preparation_cannot_restart_the_caller_budget(monkeypatch):
    _configure(monkeypatch)
    clock, requests = [0.0], []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    def embed(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    def client():
        clock[0] = 2.0
        return SimpleNamespace(models=SimpleNamespace(embed_content=embed), close=lambda: None)

    monkeypatch.setattr(scheduler, "_create_client", client)
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler.embed_texts(["synthetic"], budget_seconds=0.5)
    assert requests == []


def test_sdk_timeout_uses_remaining_budget_and_disables_sdk_retries(monkeypatch):
    config = _configure(monkeypatch)
    clock, calls = [0.0], []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    class DelayedLimiter(Limiter):
        def reserve(self, *_args, **_kwargs):
            clock[0] = 0.3

    def embed(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    client = SimpleNamespace(models=SimpleNamespace(embed_content=embed))
    assert scheduler._request_embeddings(client, ["synthetic"], 1, config, DelayedLimiter(), budget_seconds=0.5) == [[1.0, 2.0]]
    options = calls[0]["config"]["http_options"]
    assert 1 <= options["timeout"] <= 200
    assert options["retry_options"]["attempts"] == 1
    # Validate against the installed SDK's real request schema, without networking.
    from google.genai import types
    validated = types.EmbedContentConfig.model_validate(calls[0]["config"])
    assert validated.http_options is not None
    assert validated.http_options.timeout == options["timeout"]


@pytest.mark.parametrize("transport", ["sdk", "rest", "rest-httpx"])
def test_expired_provider_result_is_not_accepted(monkeypatch, transport):
    config = _configure(monkeypatch, transport)
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    def late_values(*_args, **_kwargs):
        clock[0] = 1.0
        return [[1.0, 2.0]]

    if transport in {"rest", "rest-httpx"}:
        target = "_rest_embed_contents" if transport == "rest" else "_httpx_embed_contents"
        monkeypatch.setattr(scheduler, target, late_values)
        client = None
    else:
        def late_sdk(**kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in late_values()])
        client = SimpleNamespace(models=SimpleNamespace(embed_content=late_sdk))
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler._request_embeddings(client, ["synthetic"], 1, config, Limiter(), budget_seconds=0.2)


@pytest.mark.parametrize("transport", ["sdk", "rest"])
def test_actual_delayed_headers_obey_remaining_timeout(monkeypatch, record_property, transport):
    config = _configure(monkeypatch, transport)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(0.4)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"embeddings": [{"values": [1.0, 2.0]}]}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the deadline closes this synthetic connection.

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    original = urllib.request.urlopen

    def loopback(request, timeout):
        local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/", data=request.data, headers=dict(request.header_items()), method="POST")
        return original(local, timeout=timeout)

    client = None
    if transport == "rest":
        monkeypatch.setattr(urllib.request, "urlopen", loopback)
    else:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key="synthetic-test-key", http_options=types.HttpOptions(
            base_url=f"http://127.0.0.1:{server.server_port}",
            retry_options=types.HttpRetryOptions(attempts=1),
        ))
    started = time.perf_counter()
    try:
        with pytest.raises(scheduler.EmbeddingBudgetExceeded):
            scheduler._request_embeddings(client, ["synthetic"], 1, config, Limiter(), budget_seconds=0.15)
        elapsed = time.perf_counter() - started
        record_property("deadline_elapsed_ms", round(elapsed * 1000, 3))
        assert elapsed < 0.6
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
    assert not worker.is_alive()
    if client is not None:
        client.close()


def test_preparation_expiry_does_not_acquire_a_client(monkeypatch):
    _configure(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])
    def slow_tokens(_text):
        clock[0] = 1.0
        return 1
    monkeypatch.setattr(scheduler, "estimate_embedding_tokens", slow_tokens)
    monkeypatch.setattr(scheduler, "_create_client", lambda: pytest.fail("expired work acquired a client"))
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler.embed_texts(["synthetic"], budget_seconds=0.2)


@pytest.mark.parametrize("budget", [float("nan"), float("inf")])
def test_nonfinite_budget_rejected_before_client_acquisition(monkeypatch, budget):
    _configure(monkeypatch)
    monkeypatch.setattr(scheduler, "_create_client", lambda: pytest.fail("invalid budget acquired a client"))
    with pytest.raises(ValueError, match="finite"):
        scheduler.embed_texts(["synthetic"], budget_seconds=budget)


def test_late_vector_never_enters_query_cache_and_reset_preserves_existing(monkeypatch):
    from collections import OrderedDict

    from vector_lake import tool_search

    _configure(monkeypatch, "rest-httpx")
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tool_search, "_QUERY_EMBEDDING_CACHE", OrderedDict())
    monkeypatch.setattr(tool_search, "_QUERY_EMBEDDING_IN_FLIGHT", {})
    tool_search._store_query_embedding("existing synthetic", [1.0, 2.0])
    scheduler.reset_client_cache()
    assert tool_search._get_query_embedding("existing synthetic") == ([1.0, 2.0], None)

    def late(*_args, **_kwargs):
        clock[0] = 100.0
        return [[1.0, 2.0]]

    monkeypatch.setattr(scheduler, "_httpx_embed_contents", late)
    vector, error = tool_search._get_query_embedding("late synthetic")
    assert vector == [] and error is not None and "EmbeddingBudgetExceeded" in error
    assert tool_search._cached_query_embedding("late synthetic") is None
    assert tool_search._QUERY_EMBEDDING_IN_FLIGHT == {}


def test_late_backfill_is_failed_not_persisted(isolated_memory, monkeypatch):
    from vector_lake import db_store

    db_store.init_db()
    _configure(monkeypatch, "rest-httpx")
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    def late(*_args, **_kwargs):
        clock[0] = 100.0
        return [[1.0, 2.0]]

    monkeypatch.setattr(scheduler, "_httpx_embed_contents", late)
    monkeypatch.setattr(db_store, "upsert_embedding", lambda *_a, **_k: pytest.fail("late vector persisted"))
    result = scheduler.embedding_backfill({"nodes": {"Concept_Synthetic": {"title": "Synthetic"}}}, dry_run=False, budget_seconds=0.2)
    assert result["embedded"] == 0 and result["failed_batches"] == 1
    assert db_store.embedding_projection_state() == {}
    row = db_store.get_connection().execute("SELECT status FROM embedding_runs WHERE run_id=?", (result["run_id"],)).fetchone()
    assert row["status"] == "failed"


def test_subsecond_configured_transport_timeout_is_preserved(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", "150")
    assert scheduler._transport_timeout(None) == 0.15
