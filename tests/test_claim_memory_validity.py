"""GC-02/GC-03: validity is independent of supersession and advances without writes."""
from datetime import datetime, timezone

import pytest

from vector_lake import db_store, memory_gram_index
from vector_lake import governance_metrics as metrics
from vector_lake import governance_store as store
from vector_lake import scoped_memory_search as scoped
from vector_lake import scoped_retrieval_index as indexes

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 2, 1, tzinfo=timezone.utc)


def claim(**extra):
    item = {"claim_id": "claim_a", "claim_text": "target", "memory_type": "preference",
            "memory_key": "target", "status": "Active", "confidence": 0.8,
            "evidence_ids": ["ev"], "evidence_support": "verified", "verified_evidence_ids": ["ev"],
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}
    item.update(extra)
    return item


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(metrics, "_utc_now", lambda: NOW)


@pytest.mark.parametrize("extra,state,reason", [
    ({"evidence_support": "declared", "verified_evidence_ids": []}, "needs-review", "declared-unverified"),
    ({"evidence_support": None, "verified_evidence_ids": []}, "needs-review", "legacy-unverified"),
    ({"evidence_ids": []}, "unsupported", "missing_evidence"),
    ({"valid_from": "2026-01-02T00:00:00"}, "not-yet-valid", "valid_from"),
])
def test_claim_support_and_future_are_not_active(extra, state, reason):
    validity = metrics.infer_claim_validity(claim(**extra))
    assert validity["validity_state"] == state
    assert reason in validity["reasons"]


@pytest.mark.parametrize("extra,state", [
    ({"evidence_ids": []}, "unsupported"),
    ({"contradicts": ["other"]}, "conflicted"),
    ({"review_after": "2025-12-31T00:00:00Z"}, "review-due"),
    ({"confidence": 0.2}, "provisional"),
    ({"evidence_support": "declared", "verified_evidence_ids": []}, "needs-review"),
])
def test_typed_singleton_and_restored_loser_keep_base_validity(extra, state):
    memory = store._memory_object_from_claim(metrics.annotate_claim_validity(claim(**extra)))
    resolved = store._resolve_memory_conflicts({"items": {memory["memory_id"]: memory}})
    assert resolved["items"][memory["memory_id"]]["validity_state"] == state
    winner = store._memory_object_from_claim(metrics.annotate_claim_validity(
        claim(claim_id="new", updated_at="2026-01-02T00:00:00Z")))
    store._mark_superseded(memory, winner, "preference:newer-authority-confidence")
    restored = store._resolve_memory_conflicts({"items": {memory["memory_id"]: memory}})
    item = restored["items"][memory["memory_id"]]
    assert item["validity_state"] == state
    assert "superseded_by" not in item
    assert item["validity_factor"] == store.VALIDITY_FACTORS[state]


def test_explicit_supersession_survives_typed_pass():
    old = store._memory_object_from_claim(metrics.annotate_claim_validity(
        claim(claim_id="old", authority_score=0.9, contradicts=["new"])))
    new = store._memory_object_from_claim(metrics.annotate_claim_validity(
        claim(claim_id="new", authority_score=0.1, updated_at="2026-01-03T00:00:00Z")))
    result = store._resolve_memory_conflicts({"items": {"old": old, "new": new}})
    assert result["items"]["new"]["validity_state"] == "superseded"
    assert result["items"]["new"]["superseded_by"] == old["memory_id"]
    assert old["validity_state"] == "conflicted"


@pytest.mark.parametrize("relations", [
    {"supersedes": ["claim_old"]},
    {"supersedes_claim_ids": ["claim_old"]},
])
@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
def test_revision_relation_fields_survive_projection_and_search(isolated_memory, monkeypatch, relations, backend):
    db_store.init_db()
    memory = store._memory_object_from_claim(metrics.annotate_claim_validity(claim(
        memory_type="fact", revision_of="claim_old", evidence_support="declared",
        verified_evidence_ids=[], **relations)))
    assert memory["supersedes_claim_ids"] == ["claim_old"]
    assert memory["revision_of"] == "claim_old"
    assert memory["validity_state"] == "needs-review"
    memory.update(domain="General", topic_cluster="target")
    store.save_memory_objects({"items": {memory["memory_id"]: memory}})
    indexes.apply(db_store.get_connection())
    memory_gram_index.rebuild_memory_gram_index()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    result = store.search_operational_memory(
        "target", top_k=1, domain="General" if backend.startswith("scoped") else None)[0]
    assert result["supersedes_claim_ids"] == ["claim_old"]
    assert result["revision_of"] == "claim_old"
    assert result["validity_state"] == "needs-review"


def require_backend(monkeypatch, backend):
    def fail(*args, **kwargs):
        pytest.fail("The selected index backend must answer, not silently fall back")

    if backend in {"index", "gram"}:
        monkeypatch.setattr(store, "_load_memory_items", fail)
    elif backend == "scoped-index":
        monkeypatch.setattr(scoped, "_scan_scope", fail)


def seed():
    db_store.init_db()
    memories = {}
    for i in range(30):
        c = claim(claim_id=f"expired_{i}", claim_text="target target high", memory_type="fact",
                  valid_to="2026-01-15T00:00:00Z")
        m = store._memory_object_from_claim(metrics.annotate_claim_validity(c))
        memories[m["memory_id"]] = m
    cases = {
        "live": {},
        "future": {"valid_from": "2026-01-20T00:00:00Z"},
        "far_future": {"valid_from": "2026-03-01T00:00:00Z"},
        "review": {"review_after": "2026-01-15T00:00:00Z"},
        "archived": {"status": "Archived"},
        "superseded": {},
        "expired": {"valid_to": "2025-12-01T00:00:00Z"},
        "ttl_only": {"ttl_days": 1},
    }
    for name, extra in cases.items():
        c = claim(claim_id=name, claim_text="target", memory_key=name, memory_type="fact", **extra)
        m = store._memory_object_from_claim(metrics.annotate_claim_validity(c))
        if name == "superseded":
            store._mark_superseded(m, {"memory_id": "winner"}, "explicit-contradiction:authority-confidence-recency")
        memories[m["memory_id"]] = m
    for m in memories.values():
        m["domain"] = "General"
        m["topic_cluster"] = "target"
    store.save_memory_objects({"items": memories})
    conn = db_store.get_connection()
    indexes.apply(conn)
    memory_gram_index.rebuild_memory_gram_index()
    return conn


@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
@pytest.mark.parametrize("query", ["target", ""])
def test_time_progression_filters_before_topk_and_returns_current_state(isolated_memory, monkeypatch, backend, query):
    conn = seed()
    snapshot = list(conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id"))
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    domain = "General" if backend.startswith("scoped") else None
    cluster = "target" if domain else None
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    before = store.search_operational_memory(query, top_k=100, domain=domain, cluster=cluster)
    assert "future" not in {m["source_claim_id"] for m in before}
    assert "far_future" not in {m["source_claim_id"] for m in before}
    monkeypatch.setattr(metrics, "_utc_now", lambda: LATER)
    after = store.search_operational_memory(query, top_k=100, domain=domain, cluster=cluster)
    by_claim = {m["source_claim_id"]: m for m in after}
    assert set(by_claim) == {"live", "future", "review", "ttl_only"}
    assert by_claim["future"]["validity_state"] == "active"
    assert by_claim["review"]["validity_state"] == "review-due"
    assert by_claim["ttl_only"]["validity_state"] == "active"  # TTL is not a hard expiry.
    assert store.search_operational_memory(query, top_k=1, domain=domain, cluster=cluster)[0]["source_claim_id"] in by_claim
    history = store.search_operational_memory(query, top_k=100, include_history=True, domain=domain, cluster=cluster)
    history = {m["source_claim_id"]: m for m in history}
    assert history["archived"]["validity_state"] in {"archived", "expired"}
    assert history["superseded"]["validity_state"] == "superseded"
    assert history["expired"]["validity_state"] == "expired"
    assert history["far_future"]["validity_state"] == "not-yet-valid"
    assert [tuple(r) for r in snapshot] == [tuple(r) for r in conn.execute(
        "SELECT data_json FROM operational_memory ORDER BY memory_id")]


@pytest.mark.parametrize("state", ["expired", "archived"])
def test_terminal_legacy_memory_is_not_reactivated(state):
    memory = store._memory_object_from_claim(metrics.annotate_claim_validity(claim()))
    memory["validity_state"] = state
    assert store._current_memory(memory, LATER)["validity_state"] == state


def test_scoped_candidate_budget_falls_back_before_excluding_lower_valid_rows(isolated_memory, monkeypatch):
    seed()
    monkeypatch.setattr(metrics, "_utc_now", lambda: LATER)
    monkeypatch.setattr(scoped, "MAX_INDEX_CANDIDATES", 16)
    scans = []
    original = scoped._scan_scope

    def counted(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(scoped, "_scan_scope", counted)
    result = store.search_operational_memory("target", top_k=4, domain="General")
    assert scans == [1]
    assert {m["source_claim_id"] for m in result} == {"live", "future", "review", "ttl_only"}


@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
def test_current_score_selects_candidates_before_the_window(isolated_memory, monkeypatch, backend):
    db_store.init_db()
    items = {}
    # Stored scores favor short-lived memories. After time advances, the long-lived
    # memory must be considered even though it was outside the old topK window.
    for i in range(31):
        c = claim(claim_id=str(i), memory_type="fact", ttl_days=1 if i < 30 else 365,
                  confidence=0.9 if i < 30 else 0.8)
        m = store._memory_object_from_claim(metrics.annotate_claim_validity(c))
        m.update(store.score_memory_object(m, now=NOW))
        m.update(domain="General", topic_cluster="target")
        items[m["memory_id"]] = m
    store.save_memory_objects({"items": items})
    indexes.apply(db_store.get_connection())
    memory_gram_index.rebuild_memory_gram_index()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    monkeypatch.setattr(metrics, "_utc_now", lambda: LATER)
    domain = "General" if backend.startswith("scoped") else None
    result = store.search_operational_memory("", top_k=1, domain=domain)
    assert result[0]["source_claim_id"] == "30"
    assert result[0]["freshness_score"] < 1.0
