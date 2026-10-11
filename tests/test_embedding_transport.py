"""The embedding transport: the same endpoint, without the SDK's per-process import.

Measured on 2026-09-19, first embedding in a fresh process:

| path | wall | ``google.genai`` imported |
|---|---|---|
| SDK (``genai.Client().models.embed_content``) | 8.33 s | yes (3.5 s import + 1.3 s client) |
| REST (``batchEmbedContents`` over ``urllib``) | **1.54 s** | **no** |

The two returned **bit-identical** vectors for the same text (cosine 1.00000000), which is what
makes the cheaper path a transport change rather than a behaviour change.  The SDK stays as the
explicit alternative, not an automatic error fallback; the point of these tests is that the default path never pays the import, that the
request and response shapes are what the endpoint expects, and that error semantics still match
what the retry loop classifies.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from http.client import HTTPMessage

import pytest

from vector_lake import embedding_scheduler


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _config():
    return embedding_scheduler.load_embedding_rate_config()


@pytest.fixture(autouse=True)
def _rest_transport(monkeypatch):
    monkeypatch.delenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, raising=False)
    embedding_scheduler.reset_client_cache()
    yield
    embedding_scheduler.reset_client_cache()


def test_default_urllib_retains_legacy_redirect_behavior_not_httpx_policy():
    """Legacy 302 converts POST to GET and retains custom key; opt-in rejects it.

    This is a stdlib-handler probe, not a real provider or redirect security claim.
    """
    request = urllib.request.Request(
        "https://first.invalid/embed", data=b"{}",
        headers={"x-goog-api-key": "dummy-legacy-redirect"}, method="POST",
    )
    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
        request, io.BytesIO(), 302, "Found", HTTPMessage(), "https://second.invalid/collect",
    )
    assert redirected is not None
    assert redirected.get_method() == "GET"
    assert redirected.get_header("X-goog-api-key") == "dummy-legacy-redirect"


def test_the_default_transport_is_rest(monkeypatch):
    assert embedding_scheduler.embedding_transport() == "rest"
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")
    assert embedding_scheduler.embedding_transport() == "sdk"
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "nonsense")
    assert embedding_scheduler.embedding_transport() == "rest", "an unknown value must not disable embeddings"


def test_the_request_is_the_batch_embed_contents_shape(monkeypatch):
    """The URL, the key header and the per-item ``content.parts`` shape the endpoint requires."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    config = _config()
    captured = {}

    def _urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(
            json.dumps({"embeddings": [{"values": [0.5] * config.dimension}]}).encode("utf-8")
        )

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    values = embedding_scheduler._rest_embed_contents(["hello"], config)

    assert captured["url"].endswith(f"/models/{config.model}:batchEmbedContents")
    assert captured["headers"].get("X-goog-api-key") == "test-key"
    assert captured["body"]["requests"][0]["content"]["parts"][0]["text"] == "hello"
    assert captured["body"]["requests"][0]["model"] == f"models/{config.model}"
    assert len(values[0]) == config.dimension


@pytest.mark.parametrize(
    "payload",
    [
        {"embeddings": []},
        {"embeddings": [{"values": [0.0, 0.0]}]},
    ],
)
def test_a_wrong_shape_is_refused_not_trusted(monkeypatch, payload):
    """Same validation the SDK path applies, so a bad response cannot enter the index."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(json.dumps(payload).encode("utf-8")),
    )

    with pytest.raises(embedding_scheduler.EmbeddingResponseError):
        embedding_scheduler._rest_embed_contents(["hello"], _config())


def test_an_http_error_carries_its_status_and_body(monkeypatch):
    """The retry loop classifies quota errors from the message text, so 429 has to survive."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def _fail(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", HTTPMessage(), io.BytesIO(b'{"error":"quota"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    with pytest.raises(RuntimeError) as excinfo:
        embedding_scheduler._rest_embed_contents(["hello"], _config())

    message = str(excinfo.value)
    assert "429" in message
    assert "quota" in message


def test_a_missing_key_is_an_error_not_a_silent_empty_result(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        embedding_scheduler._rest_embed_contents(["hello"], _config())


def test_embed_texts_does_not_import_the_sdk_on_the_default_path(monkeypatch):
    """The whole point: the 3.5 s import must not happen for a REST round trip."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        embedding_scheduler, "_rest_embed_contents",
        lambda contents, cfg, **_kwargs: [[0.1] * cfg.dimension for _ in contents],
    )
    client_calls = []
    monkeypatch.setattr(embedding_scheduler, "_create_client", lambda: client_calls.append(1))

    values = embedding_scheduler.embed_texts(["hello"], budget_seconds=5)

    assert len(values) == 1
    assert client_calls == [], "the SDK client was built on the REST path"


def test_the_sdk_path_is_still_available(monkeypatch):
    """The explicitly selected SDK path must remain available."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")
    config = _config()

    class _Models:
        def embed_content(self, model=None, contents=None, **_kwargs):
            return type(
                "R", (), {"embeddings": [type("E", (), {"values": [0.2] * config.dimension})()]}
            )()

    class _Client:
        models = _Models()

        def close(self):
            pass

    monkeypatch.setattr(embedding_scheduler, "_create_client", lambda: _Client())
    values = embedding_scheduler.embed_texts(["hello"], budget_seconds=5)

    assert len(values) == 1
    assert len(values[0]) == config.dimension


def test_prewarming_is_skipped_on_the_rest_path(monkeypatch):
    """Warming a client the REST path never uses would be pure cost."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    built = []
    monkeypatch.setattr(embedding_scheduler, "_create_client", lambda: built.append(1))

    assert embedding_scheduler.prewarm_client() is False
    assert built == []
    assert embedding_scheduler.start_prewarm_thread() is None

    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")


def test_the_backfill_never_builds_an_sdk_client_on_the_rest_path(isolated_memory, monkeypatch):
    """Independent review, finding 1: the *bulk* path built the client unconditionally.

    ``embedding_backfill`` called ``_shared_client()`` no matter the transport, so it paid the
    4.8 s import and raised ``ImportError`` on a host without ``google-genai`` before
    ``_request_embeddings`` could post over REST -- the exact cost the REST transport exists to
    remove, on the path that embeds the most text.
    """
    from vector_lake import db_store
    from vector_lake.embedding_scheduler import embedding_backfill

    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        embedding_scheduler, "_create_client",
        lambda: pytest.fail("an SDK client was built on the REST path"),
    )
    monkeypatch.setattr(
        embedding_scheduler, "_rest_embed_contents",
        lambda contents, cfg, **_kwargs: [[0.4] * cfg.dimension for _ in contents],
    )

    result = embedding_backfill(
        {"nodes": {"Concept_A": {"title": "A"}, "Concept_B": {"title": "B"}}},
        dry_run=False,
    )

    assert result["embedded"] == 2, result
    assert result["failed_batches"] == 0
    assert result.get("last_error", "") == ""
