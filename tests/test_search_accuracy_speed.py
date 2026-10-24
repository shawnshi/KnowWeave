"""Accuracy, bounded caching and single-flight contracts; no live provider calls."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest

from vector_lake import db_store, embedding_scheduler, page_index_projection, tokenizer, tool_search as ts


@pytest.fixture(autouse=True)
def local_state(monkeypatch):
    for key in (
        "VECTOR_LAKE_FUSION", "VECTOR_LAKE_EXPANSION_QUOTA", "VECTOR_LAKE_CANDIDATE_DEPTH",
        "VECTOR_LAKE_CANDIDATE_POOL", "VECTOR_LAKE_ENTITY_NAME_PRIORITY", "VECTOR_LAKE_AUTHOR_FACET",
        "VECTOR_LAKE_RERANK_WEIGHT", "VECTOR_LAKE_FTS", "VECTOR_LAKE_SOURCE_RANK_PENALTY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ts, "_QUERY_EMBEDDING_CACHE", OrderedDict())
    monkeypatch.setattr(ts, "_QUERY_EMBEDDING_IN_FLIGHT", {}, raising=False)
    monkeypatch.setattr(ts, "_PPR_INDEX", {"generation": None, "adj": None, "index": None})
    monkeypatch.setattr(ts, "_vector_projection_error", lambda: None)
    monkeypatch.setattr(embedding_scheduler, "load_embedding_rate_config", lambda: SimpleNamespace(model="fixture", dimension=2))
    if hasattr(ts, "_cached_rerank_tokens"):
        ts._cached_rerank_tokens.cache_clear()
    yield
    if hasattr(ts, "_cached_rerank_tokens"):
        ts._cached_rerank_tokens.cache_clear()


def _node(title, **fields):
    return {"title": title, "summary": "", "type": "concept", "status": "Active", "domain": "AI_Engineering", "topic_cluster": "wanted", **fields}


def _seed_fts():
    db_store.init_db()
    db_store.upsert_search_index("seed", "seed", "", "")


def test_case_variants_have_identical_lexical_scores():
    candidates = [(0.5, {"_key": "llm", **_node("LLM")}),
                  (1.0, {"_key": "weather", **_node("weather")}),
                  (0.0, {"_key": "generic", **_node("generic")})]
    upper = ts._rerank_candidates_locally("LLM", candidates)
    lower = ts._rerank_candidates_locally("llm", candidates)
    assert upper == lower
    assert lower[0][1]["_key"] == "llm"
    assert candidates[0][1]["title"] == "LLM", "normalization must not mutate stored metadata"


@pytest.mark.parametrize("engine", ["rust", "python"])
@pytest.mark.parametrize("selector", ["domain", "cluster", "history", "filter_expr"])
def test_graph_filters_before_quota_and_backfills(isolated_memory, monkeypatch, engine, selector):
    _seed_fts()
    if engine == "python":
        monkeypatch.setattr(ts, "HAVE_CORE", False)
    kwargs = {"include_history": True}
    bad = {}
    if selector == "domain":
        bad = {"domain": "General"}
        kwargs["domain"] = "AI_Engineering"
    elif selector == "cluster":
        bad = {"topic_cluster": "other"}
        kwargs["cluster"] = "wanted"
    elif selector == "history":
        bad = {"status": "Archived"}
        kwargs["include_history"] = False
    else:
        bad = {"type": "source"}
        kwargs["filter_expr"] = "type == 'concept'"
    nodes = {"seed": _node("seed"), "valid": _node("valid")}
    nodes.update({f"bad{i}": _node(f"bad{i}", **bad) for i in range(7)})
    edges = [{"source": "seed", "target": key, "weight": 100.0 if key.startswith("bad") else 1.0}
             for key in nodes if key != "seed"]

    class Catalog(page_index_projection._FileCatalog):
        def __init__(self):
            super().__init__({"nodes": nodes, "weighted_edges": edges})
            self.full_reads = []

        def nodes_by_key(self, keys):
            keys = list(keys)
            self.full_reads.extend(keys)
            return super().nodes_by_key(keys)

    catalog = Catalog()
    rows, _notes, error = ts._search_scored_pages("seed", 5, catalog=catalog, **kwargs)
    assert error is None
    assert {node["_key"] for _score, node in rows} == {"seed", "valid"}
    if selector != "filter_expr":
        assert not any(key.startswith("bad") for key in catalog.full_reads)


def test_token_cache_reuses_and_invalidates_content_and_backend(monkeypatch):
    if not ts.HAVE_CORE:
        pytest.skip('native BM25 token-cache path; core fallback keeps upstream scores unchanged')
    original = tokenizer.tokenize_joined
    calls = []

    def counted(text, separator=" "):
        calls.append(text)
        return original(text, separator)

    monkeypatch.setattr(tokenizer, "tokenize_joined", counted)
    candidates = [(1.0, {"_key": "a", **_node("LLM")}), (0.0, {"_key": "b", **_node("weather")})]
    ts._rerank_candidates_locally("llm", candidates)
    ts._rerank_candidates_locally("llm", candidates)
    assert len(calls) == 2
    candidates[0][1]["summary"] = "updated"
    ts._rerank_candidates_locally("llm", candidates)
    assert len(calls) == 3
    monkeypatch.setattr(tokenizer, "backend_name", lambda: "different-fixture-backend")
    ts._rerank_candidates_locally("llm", candidates)
    assert len(calls) == 5


def test_token_cache_bound_and_oversized_bypass():
    cached = ts._cached_rerank_tokens
    for i in range(cached.cache_info().maxsize + 2):
        cached(f"LLM token {i}", "fixture")
    assert cached.cache_info().currsize == cached.cache_info().maxsize
    cached.cache_clear()
    huge = "x" * (ts.RERANK_TOKEN_CACHE_MAX_CHARS + 1)
    candidates = [(1.0, {"_key": "a", **_node(huge)}), (0.0, {"_key": "b", **_node(huge)})]
    ts._rerank_candidates_locally("llm", candidates)
    assert cached.cache_info().currsize == 0


def _concurrent_same_query(monkeypatch, fail=False):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    barrier = threading.Barrier(8)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    lock = threading.Lock()

    def provider(texts, budget_seconds=None, durable_reservation=True):
        assert 0 < budget_seconds <= ts.QUERY_EMBEDDING_BUDGET_SECONDS
        assert durable_reservation is False
        with lock:
            calls.append(tuple(texts))
        entered.set()
        if not release.wait(2):
            raise TimeoutError("fixture release deadline")
        if fail:
            raise RuntimeError("fixture provider failure")
        return [[1.0, 0.0]]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", provider)

    def request():
        barrier.wait(timeout=2)
        return ts._get_query_embedding("same query")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request) for _ in range(8)]
        try:
            assert entered.wait(1)
            time.sleep(0.05)
        finally:
            release.set()
        results = [future.result(timeout=3) for future in futures]
    return calls, results


def test_singleflight_shares_one_request_and_independent_result_lists(monkeypatch):
    calls, results = _concurrent_same_query(monkeypatch)
    assert len(calls) == 1
    assert all(values == [1.0, 0.0] and error is None for values, error in results)
    results[0][0][0] = 9.0
    assert results[1][0] == [1.0, 0.0]
    assert ts._cached_query_embedding("same query")[0] == 1.0
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT


def test_singleflight_failure_shared_but_not_cached(monkeypatch):
    calls, results = _concurrent_same_query(monkeypatch, fail=True)
    assert len(calls) == 1
    assert all(not values and "RuntimeError" in error for values, error in results)
    assert ts._cached_query_embedding("same query") is None
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT
    _values, error = ts._get_query_embedding("same query")
    assert len(calls) == 2 and "RuntimeError" in error


def test_follower_deadline_does_not_cancel_owner(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    monkeypatch.setattr(ts, "QUERY_EMBEDDING_BUDGET_SECONDS", 1.0)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def provider(*args, **kwargs):
        calls.append(1)
        entered.set()
        if not release.wait(2):
            raise TimeoutError("fixture release deadline")
        return [[1.0, 0.0]]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(ts._get_query_embedding, "timeout query")
        try:
            assert entered.wait(1)
            monkeypatch.setattr(ts, "QUERY_EMBEDDING_BUDGET_SECONDS", 0.01)
            values, error = ts._get_query_embedding("timeout query")
            assert values == [] and "in-flight wait budget" in error
            assert len(calls) == 1
        finally:
            release.set()
        assert owner.result(timeout=2) == ([1.0, 0.0], None)
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT


def test_different_queries_do_not_serialize_provider_calls(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    entered = {key: threading.Event() for key in ("a", "b")}
    release = threading.Event()

    def provider(texts, **kwargs):
        entered[texts[0]].set()
        if not release.wait(2):
            raise TimeoutError("fixture release deadline")
        return [[1.0, 0.0]]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ts._get_query_embedding, key) for key in entered]
        try:
            assert all(event.wait(1) for event in entered.values())
        finally:
            release.set()
        assert all(future.result(timeout=2)[1] is None for future in futures)
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT


def test_inflight_capacity_returns_explicit_degradation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    monkeypatch.setattr(ts, "QUERY_EMBEDDING_MAX_IN_FLIGHT", 0, raising=False)
    called = []
    monkeypatch.setattr(embedding_scheduler, "embed_texts", lambda *a, **k: called.append(1) or [[1.0, 0.0]])
    values, error = ts._get_query_embedding("capacity query")
    assert values == [] and "capacity" in error
    assert not called


def test_empty_provider_response_not_cached(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    monkeypatch.setattr(embedding_scheduler, "embed_texts", lambda *a, **k: [])
    assert ts._get_query_embedding("empty") == ([], "embedding provider returned no vector")
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT and not ts._QUERY_EMBEDDING_CACHE


def test_same_query_different_models_and_dimensions_are_independent(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")
    local = threading.local()
    entered = {"a": threading.Event(), "b": threading.Event()}
    release = threading.Event()
    monkeypatch.setattr(ts, "_embedding_cache_key", lambda query: (local.model, local.dimension, query))

    def provider(texts, **kwargs):
        entered[local.model].set()
        if not release.wait(2):
            raise TimeoutError("fixture release deadline")
        return [[1.0] + [0.0] * (local.dimension - 1)]

    monkeypatch.setattr(embedding_scheduler, "embed_texts", provider)

    def request(model, dimension):
        local.model, local.dimension = model, dimension
        return ts._get_query_embedding("same text")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(request, "a", 2), pool.submit(request, "b", 3)
        try:
            assert all(event.wait(1) for event in entered.values())
        finally:
            release.set()
        assert a.result(timeout=2) == ([1.0, 0.0], None)
        assert b.result(timeout=2) == ([1.0, 0.0, 0.0], None)
    assert set(ts._QUERY_EMBEDDING_CACHE) == {("a", 2, "same text"), ("b", 3, "same text")}
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT


def test_owner_abort_releases_pending_state(monkeypatch):
    class ProviderAbort(BaseException):
        pass

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-not-a-credential")

    def provider(*args, **kwargs):
        raise ProviderAbort("fixture host cancellation")

    monkeypatch.setattr(embedding_scheduler, "embed_texts", provider)
    with pytest.raises(ProviderAbort):
        ts._get_query_embedding("aborted")
    assert not ts._QUERY_EMBEDDING_IN_FLIGHT and not ts._QUERY_EMBEDDING_CACHE


def test_graph_batches_backfill_disappearing_or_changed_payloads():
    class Catalog:
        batches = []
        full_reads = []

        def filter_fields(self, keys):
            self.batches.append(list(keys))
            return {key: _node(key, status="Archived" if int(key[1:]) < 80 else "Active") for key in keys}

        def nodes_by_key(self, keys):
            self.full_reads.extend(keys)
            return {key: _node(key, status="Archived" if key == "n081" else "Active")
                    for key in keys if key != "n080"}

    catalog = Catalog()
    rows = ts._filtered_graph_expansions([(f"n{i:03d}", 1.0) for i in range(100)], catalog, 5, None, None, False, None)
    assert [key for key, _weight, _node_data in rows] == [f"n{i:03d}" for i in range(82, 87)]
    assert max(map(len, catalog.batches)) <= 32
    assert all(int(key[1:]) >= 80 for key in catalog.full_reads)


def test_optional_timings_preserve_results_and_cover_early_errors(isolated_memory):
    _seed_fts()
    catalog = page_index_projection._FileCatalog({"nodes": {"seed": _node("seed")}})
    plain = ts._search_scored_pages("seed", 5, include_history=True, catalog=catalog)
    timings = {"stale": 123.0}
    measured = ts._search_scored_pages("seed", 5, include_history=True, catalog=catalog, timings=timings)
    assert measured == plain and "stale" not in timings
    assert {"catalog", "query_processing", "fts", "embedding", "vector_search", "materialize", "ppr", "pool", "rerank", "final_order", "ledger", "total"} <= timings.keys()
    assert all(isinstance(value, float) and value >= 0 for value in timings.values())
    assert sum(value for key, value in timings.items() if key != "total" and "." not in key) <= timings["total"] + 0.001
    children = {key: value for key, value in timings.items() if key.startswith("query_processing.")}
    assert set(children) == {"query_processing.name_candidates", "query_processing.keyword_intent", "query_processing.local_expansion"}
    assert sum(children.values()) <= timings["query_processing"] + 0.001
    failed_timings = {}
    _rows, _notes, error = ts._search_scored_pages("seed", 5, timings=failed_timings)
    assert error and failed_timings["total"] >= 0
