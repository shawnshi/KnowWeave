from datetime import datetime, timezone

import pytest

from tests.test_claim_memory_validity import claim, require_backend
from vector_lake import db_store, governance_metrics as metrics, governance_store as store, memory_gram_index
from vector_lake import scoped_memory_search as scoped, scoped_retrieval_index as indexes

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER = datetime(2026, 2, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(store, "_memory_now", lambda: NOW)
    monkeypatch.setattr(metrics, "_utc_now", lambda: NOW)


def memory(name, **extra):
    return store._memory_object_from_claim(metrics.annotate_claim_validity(
        claim(**{"claim_id": name, "memory_type": "fact", "memory_key": name, **extra})))


def resolve(*items):
    return store._resolve_memory_conflicts({"items": {item["memory_id"]: item for item in items}})


@pytest.mark.parametrize("extra", [
    {"valid_to": "2025-12-01T00:00:00Z"},
    {"valid_from": "2027-01-01T00:00:00Z"},
    {"evidence_support": "declared", "verified_evidence_ids": []},
    {"evidence_ids": []},
    {"review_after": "2025-12-01T00:00:00Z"},
])
def test_invalid_superseder_does_not_hide_valid_predecessor(extra):
    old = memory("old")
    new = memory("new", supersedes=["old"], **extra)
    resolve(old, new)
    assert old["validity_state"] == "active"
    assert new["validity_state"] != "active"


@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
def test_predecessor_resumes_when_superseder_expires_without_writes(isolated_memory, monkeypatch, backend):
    old = memory("old")
    new = memory("new", supersedes=["old"], valid_to="2026-01-10T00:00:00Z")
    resolved = resolve(old, new)
    assert old["validity_state"] == "superseded"
    assert old["superseded_by"] == new["memory_id"]
    for item in resolved["items"].values():
        item.update(domain="General", topic_cluster="target")
    db_store.init_db()
    store.save_memory_objects(resolved)
    conn = db_store.get_connection()
    indexes.apply(conn)
    memory_gram_index.rebuild_memory_gram_index()
    snapshot = [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")]
    monkeypatch.setattr(store, "_memory_now", lambda: LATER)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    found = store.search_operational_memory("target", top_k=1, domain="General" if backend.startswith("scoped") else None)
    assert [item["source_claim_id"] for item in found] == ["old"]
    assert found[0]["validity_state"] == "active"
    assert "superseded_by" not in found[0]
    assert [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")] == snapshot


def test_multiple_superseders_remain_effective_until_all_expire():
    old = memory("old")
    early = memory("early", supersedes=["old"], valid_to="2026-01-10T00:00:00Z")
    late = memory("late", supersedes=["old"], valid_to="2026-03-01T00:00:00Z")
    resolve(old, early, late)
    assert store._current_memory(old, LATER)["validity_state"] == "superseded"
    assert store._current_memory(old, datetime(2026, 4, 1, tzinfo=timezone.utc))["validity_state"] == "active"
    assert all("text" not in basis for basis in old["supersession_bases"])


def test_explicit_cycles_are_rejected():
    left = memory("left", supersedes=["right"])
    right = memory("right", supersedes=["left"])
    with pytest.raises(ValueError, match="Cyclic"):
        resolve(left, right)


def test_unverified_new_typed_memory_does_not_override_verified_predecessor():
    old = memory("old", memory_type="preference", memory_key="same")
    new = memory("new", memory_type="preference", memory_key="same", updated_at="2026-01-02T00:00:00Z", evidence_support="declared", verified_evidence_ids=[])
    resolve(old, new)
    assert old["validity_state"] == "active"
    assert new["validity_state"] == "superseded"
    restored = store._current_memory(new, restore_supersession=True)
    assert restored["validity_state"] == "needs-review"


def test_explicit_supersession_is_reversible_when_declaration_is_removed():
    old = memory("old")
    new = memory("new", supersedes=["old"])
    resolve(old, new)
    assert old["validity_state"] == "superseded"
    new["supersedes_claim_ids"] = []
    resolve(old, new)
    assert old["validity_state"] == "active"


@pytest.mark.parametrize("expired_initially", [True, False])
def test_expired_contradictor_cannot_keep_valid_predecessor_hidden(expired_initially):
    old = memory("old", authority_score=0.2)
    new = memory("new", authority_score=0.99, contradicts=["old"],
                 valid_to="2025-12-01T00:00:00Z" if expired_initially else "2026-01-10T00:00:00Z")
    resolve(old, new)
    assert old["validity_state"] == ("active" if expired_initially else "superseded")
    assert store._current_memory(old, LATER)["validity_state"] == "active"


def test_unverified_typed_competitors_are_not_declared_current_by_recency():
    first = memory("first", memory_type="preference", memory_key="same", evidence_support="declared", verified_evidence_ids=[])
    second = memory("second", memory_type="preference", memory_key="same", evidence_support="declared", verified_evidence_ids=[], updated_at="2026-01-02T00:00:00Z")
    resolve(first, second)
    assert first["validity_state"] == second["validity_state"] == "needs-review"


@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
def test_future_explicit_supersession_activates_without_a_write(isolated_memory, monkeypatch, backend):
    old = memory("old", claim_text="target target target target")
    future = memory("future", supersedes=["old"], valid_from="2026-01-20T00:00:00Z")
    resolved = resolve(old, future)
    assert store._current_memory(old, NOW)["validity_state"] == "active"
    for item in resolved["items"].values():
        item.update(domain="General", topic_cluster="target")
    db_store.init_db()
    store.save_memory_objects(resolved)
    conn = db_store.get_connection()
    indexes.apply(conn)
    memory_gram_index.rebuild_memory_gram_index()
    snapshot = [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")]
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    kwargs = {"domain": "General"} if backend.startswith("scoped") else {}
    assert [item["source_claim_id"] for item in store.search_operational_memory("target", top_k=1, **kwargs)] == ["old"]
    monkeypatch.setattr(store, "_memory_now", lambda: LATER)
    found = store.search_operational_memory("target", top_k=5, **kwargs)
    assert [item["source_claim_id"] for item in found] == ["future"]
    assert found[0]["validity_state"] == "active"
    assert [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")] == snapshot


@pytest.mark.parametrize("backend", ["legacy", "index", "gram", "scoped-index", "scoped-scan"])
def test_pending_explicit_basis_survives_typed_competition(isolated_memory, monkeypatch, backend):
    old = memory("old", memory_type="preference", memory_key="same")
    temporary = memory("temporary", memory_type="preference", memory_key="same", updated_at="2026-01-02T00:00:00Z", valid_to="2026-01-10T00:00:00Z")
    future = memory("future", memory_type="preference", memory_key="same", valid_from="2026-01-20T00:00:00Z", supersedes=["old"])
    resolved = resolve(old, temporary, future)
    assert old["validity_state"] == "superseded"
    for item in resolved["items"].values():
        item.update(domain="General", topic_cluster="target")
    db_store.init_db()
    store.save_memory_objects(resolved)
    conn = db_store.get_connection()
    indexes.apply(conn)
    memory_gram_index.rebuild_memory_gram_index()
    snapshot = [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")]
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", backend if not backend.startswith("scoped") else "gram")
    if backend == "scoped-scan":
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **kw: None)
    require_backend(monkeypatch, backend)
    kwargs = {"domain": "General"} if backend.startswith("scoped") else {}
    assert [item["source_claim_id"] for item in store.search_operational_memory("target", top_k=5, **kwargs)] == ["temporary"]
    monkeypatch.setattr(store, "_memory_now", lambda: LATER)
    found = store.search_operational_memory("target", top_k=5, **kwargs)
    assert [item["source_claim_id"] for item in found] == ["future"]
    assert store._current_memory(old, LATER)["validity_state"] == "superseded"
    assert [tuple(row) for row in conn.execute("SELECT data_json FROM operational_memory ORDER BY memory_id")] == snapshot
