"""P2: derived projections report health as a fact, and reconcile one at a time.

Pilots are the two the 2026-09-25 session had to repair by hand: the operational-memory gram index
(in the exact projected scan for up to 13 hours) and the vector projection (501 pages short after a
mass edit).  The registry adds no new mechanism -- it gives the *existing* health checks and repair
entry points one shape, so "unhealthy" is a queryable state instead of a sentence in a log.
"""
from __future__ import annotations

import pytest

from vector_lake import db_store, periodic_catch_up, projection_registry


def test_fts_repair_reuses_the_incremental_reconciler_without_embeddings(isolated_memory, monkeypatch):
    """The narrow repair must not re-embed: an empty embeddings map is what guarantees that."""
    import json

    from vector_lake import indexer
    from vector_lake.wiki_utils import get_index_path

    db_store.init_db()
    get_index_path().write_text(
        json.dumps({"nodes": {"Concept_One": {"title": "one"}}}), encoding="utf-8"
    )
    seen = {}

    def fake_sync(index_data, bodies, embeddings_map):
        seen["index_data"] = index_data
        seen["embeddings"] = embeddings_map
        return {"total": 1, "tokenized": 1, "reused": 0, "removed": 0}

    monkeypatch.setattr(indexer, "_sync_search_index", fake_sync)

    message = projection_registry._fts_repair()

    assert seen["embeddings"] == {}, "a lexical repair must not carry vectors into the sync"
    assert seen["index_data"]["nodes"].keys() == {"Concept_One"}
    assert "reconciled" in message and "tokenized" in message


def test_claim_index_repair_reports_the_reconcile(monkeypatch):
    from vector_lake import db_store

    monkeypatch.setattr(db_store, "ensure_claim_index", lambda conn=None, force=False: {"rebuilt": 3})

    message = projection_registry._claim_index_repair()

    assert "claim_index reconciled" in message and "3" in message


def test_only_the_human_queue_lacks_an_automatic_repair():
    manual = [p.name for p in projection_registry.registry() if p.repair is None]

    assert manual == ["governance_queue"], (
        "every projection that can be rebuilt has one now; a human queue is the only by-design exception"
    )


def test_every_projection_declares_its_contract():
    pilots = projection_registry.registry()

    assert {p.name for p in pilots} == {
        "memory_gram", "vectors", "page_projection", "fts_index",
        "tantivy_mirror", "claim_index", "timeline_events", "governance_queue",
    }
    for projection in pilots:
        assert projection.authority, f"{projection.name} must name what it derives from"
        assert projection.cost, f"{projection.name} must state what a repair costs"
        assert projection.degrades, f"{projection.name} must state what degrades"
        if projection.repair is None:
            assert projection.manual_entry, (
                f"{projection.name} has no automatic repair, so it must say what a human runs"
            )


def test_a_projection_without_a_repair_is_reported_as_manual(monkeypatch):
    """No routine repair is a statement about the system, not a hole to fill with a guess."""
    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection(
                "human", "a", "c",
                lambda: (projection_registry.DEGRADED, "7 item(s) queued"),
                None, "knowledge debt", "python cli.py debt",
            ),
        ),
    )

    result = projection_registry.reconcile()["human"]

    assert result["action"] == "manual"
    assert result["entry"] == "python cli.py debt"


def _with_counts(monkeypatch, **overrides):
    """Craft a counts dict, so the verdict logic is tested without building every schema."""
    base = {
        "nodes": 100, "edges": 200, "fts_rows": 100, "claims": 50, "claim_index_rows": 50,
        "timeline_events": 10, "claims_unindexed": 0, "timeline_orphans": 0,
        "governance_items": 3, "outbox_lag": 0,
    }
    base.update(overrides)
    monkeypatch.setattr(projection_registry, "_counts", lambda conn=None: base)
    return base


def test_fts_health_reports_a_count_shortfall(monkeypatch):
    _with_counts(monkeypatch, fts_rows=97)

    state, detail = projection_registry._fts_health()

    assert state == projection_registry.DEGRADED
    assert "3 row(s) short of 100 page(s)" in detail


def test_page_projection_health_reports_outbox_lag(monkeypatch):
    _with_counts(monkeypatch, outbox_lag=4)

    state, detail = projection_registry._page_projection_health()

    assert state == projection_registry.DEGRADED
    assert "4 durable mutation(s) not yet materialised" in detail


def test_claim_index_health_reports_a_shortfall(monkeypatch):
    _with_counts(monkeypatch, claim_index_rows=48)

    state, detail = projection_registry._claim_index_health()

    assert state == projection_registry.DEGRADED
    assert "2 of 50 claim(s)" in detail


def test_timeline_health_reports_orphans(monkeypatch):
    _with_counts(monkeypatch, timeline_orphans=3)

    state, detail = projection_registry._timeline_health()

    assert state == projection_registry.DEGRADED
    assert "3 timeline event(s)" in detail


def test_tantivy_mirror_is_healthy_when_not_in_use(monkeypatch):
    from vector_lake import tantivy_index

    monkeypatch.delenv("VECTOR_LAKE_FTS", raising=False)
    monkeypatch.setattr(tantivy_index, "enabled", lambda: False)

    state, detail = projection_registry._tantivy_health()

    assert state == projection_registry.HEALTHY
    assert "not in use" in detail


@pytest.mark.parametrize("condition", ["ready", "dirty", "blocked", "control", "missing", "error"])
def test_tantivy_health_requires_validated_token_not_counts(isolated_memory, monkeypatch, condition):
    from vector_lake import tantivy_index
    from tests.test_tantivy_index import _drain

    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    tantivy_index.reset()
    try:
        db_store.upsert_search_index("a", "oldword", "", "oldword")
        _drain()
        if condition in {"dirty", "blocked", "error"}:
            db_store.upsert_search_index("a", "newword", "", "newword")
            assert tantivy_index.stats()["docs"] == 1  # Equal counts can hide stale content.
        if condition == "blocked":
            conn = db_store.get_connection()
            conn.execute("UPDATE tantivy_mirror_queue SET attempt_count=3, last_error='fixture exhausted'")
            conn.commit()
        elif condition == "control":
            db_store.request_tantivy_rebuild()
            for _ in range(3):
                assert db_store.tantivy_mirror_admit_clear(db_store.tantivy_mirror_state()["epoch"])
            db_store.tantivy_mirror_fail_clear(db_store.tantivy_mirror_state()["epoch"], "fixture clear exhausted")
        elif condition == "missing":
            tantivy_index._incarnation_file().unlink()
        elif condition == "error":
            monkeypatch.setattr(tantivy_index, "current_token", lambda: (_ for _ in ()).throw(ValueError("fixture probe error")))
        state, detail = projection_registry._tantivy_health()
        assert state == (projection_registry.HEALTHY if condition == "ready" else projection_registry.DEGRADED)
        if condition == "blocked":
            assert "blocked=1" in detail and "fixture exhausted" in detail
        elif condition == "control":
            assert "control_attempts=3" in detail and "fixture clear exhausted" in detail
        elif condition == "error":
            assert "ValueError" in detail and "fixture probe error" in detail
        if condition != "ready":
            assert "FTS5 serving" in detail
    finally:
        tantivy_index.reset()


def test_tantivy_deferred_repair_is_not_reported_completed(isolated_memory, monkeypatch):
    from vector_lake import tantivy_index

    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    tantivy_index.reset()
    original = tantivy_index.rebuild_from_sqlite
    monkeypatch.setattr(tantivy_index, "rebuild_from_sqlite", lambda: original(max_steps=1))
    try:
        result = projection_registry.reconcile(only="tantivy_mirror")["tantivy_mirror"]
        assert result["action"] == "failed"  # Existing registry error contract, not false 'repaired'.
        assert "MirrorReplayDeferred" in result["error"]
        assert "scheduled, not completed" in result["error"]
        assert db_store.tantivy_mirror_state()["phase"] == "seeding"
        state, _ = projection_registry._tantivy_health()
        assert state == projection_registry.DEGRADED
    finally:
        tantivy_index.reset()


def test_tantivy_unavailable_dependency_is_visible(monkeypatch):
    from vector_lake import tantivy_index

    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    monkeypatch.setattr(tantivy_index, "enabled", lambda: False)
    state, detail = projection_registry._tantivy_health()
    assert state == projection_registry.DEGRADED and "unavailable" in detail


def test_governance_is_reported_as_a_human_queue(monkeypatch):
    _with_counts(monkeypatch, governance_items=14545)

    state, detail = projection_registry._governance_health()

    assert state == projection_registry.HEALTHY
    assert "14545 item(s)" in detail and "human" in detail


def test_gram_health_reports_the_degradation_with_counts(monkeypatch):
    from vector_lake import memory_gram_index

    monkeypatch.setattr(memory_gram_index, "gram_index_usable", lambda: False)
    monkeypatch.setattr(memory_gram_index, "dirty_breakdown", lambda conn=None: (20241, 9212, 11029))

    state, detail = projection_registry._gram_health()

    assert state == projection_registry.DEGRADED
    assert "9212" in detail and "exact scan" in detail


def test_a_failing_health_probe_is_reported_as_degraded_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (projection_registry.Projection("x", "auth", "cost", boom, lambda: "n/a", "something"),),
    )

    report = projection_registry.status()

    assert report["x"]["state"] == projection_registry.DEGRADED
    assert "probe exploded" in report["x"]["detail"]


@pytest.fixture
def empty_lake(isolated_memory):
    db_store.init_db()
    return db_store.get_connection()


def _add_node(conn, key):
    with db_store.transaction():
        conn.execute(
            "INSERT INTO page_index_nodes (node_key, node_id, title, type, status, domain,"
            " topic_cluster, node_json) VALUES (?, ?, ?, 'concept', 'Active', 'General', 'x', '{}')",
            (key, key.lower(), key),
        )


def _add_vector(conn, key, stamp=True):
    import struct

    # ``vec_embeddings`` is a sqlite-vec ``vec0`` table fixed at 3072 dimensions, so the payload has
    # to be that wide even for a test fixture.
    blob = struct.pack("3072f", *([0.0] * 3072))
    with db_store.transaction():
        conn.execute(
            "INSERT INTO vec_embeddings (page_key, embedding) VALUES (?, ?)",
            (key, blob),
        )
        if stamp:
            conn.execute(
                "INSERT INTO vec_embedding_inputs (node_key, content_digest, updated_at)"
                " VALUES (?, 'digest', '2026-09-25T00:00:00+00:00')",
                (key,),
            )


def test_vector_health_counts_missing(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_node(empty_lake, "Concept_Two")
    _add_vector(empty_lake, "Concept_One")

    state, detail = projection_registry._vector_health()

    assert state == projection_registry.DEGRADED
    assert "1/2 vectors" in detail and "1 missing" in detail


def test_vector_health_counts_unstamped(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_vector(empty_lake, "Concept_One", stamp=False)

    state, detail = projection_registry._vector_health()

    assert state == projection_registry.DEGRADED
    assert "1 unstamped" in detail and "0 missing" in detail


def test_vector_health_is_green_when_complete(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_vector(empty_lake, "Concept_One")

    assert projection_registry._vector_health() == (
        projection_registry.HEALTHY,
        "1/1 vectors, 0 missing, 0 unstamped",
    )


def test_reconcile_repairs_only_the_unhealthy_and_contains_failures(monkeypatch):
    called: list[str] = []
    repaired = set()

    def healthy():
        return projection_registry.HEALTHY, "fine"

    def sick():
        return projection_registry.DEGRADED, "behind"

    def good_health():
        return healthy() if "good" in repaired else sick()

    def good_repair():
        called.append("good")
        repaired.add("good")
        return "repaired"

    def bad_repair():
        called.append("bad")
        raise RuntimeError("repair exploded")

    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection("ok", "a", "c", healthy, lambda: called.append("never"), "none"),
            projection_registry.Projection("good", "a", "c", good_health, good_repair, "something"),
            projection_registry.Projection("bad", "a", "c", sick, bad_repair, "something"),
        ),
    )

    results = projection_registry.reconcile()

    assert results["ok"]["action"] == "none", "a healthy projection is not repaired"
    assert "never" not in called
    assert results["good"]["action"] == "repaired"
    assert results["bad"]["action"] == "failed", "a repair fault is contained, not raised"
    assert "repair exploded" in results["bad"]["error"]


def test_status_line_is_compact_and_marks_degradation(monkeypatch):
    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection(
                "ok", "a", "c", lambda: (projection_registry.HEALTHY, "fine"), lambda: "", "none"
            ),
            projection_registry.Projection(
                "sick", "a", "c", lambda: (projection_registry.DEGRADED, "12 behind"), lambda: "", "x"
            ),
        ),
    )

    line = projection_registry.status_line()

    assert "ok=healthy" in line
    assert "sick=degraded(12 behind)" in line


@pytest.fixture
def raw_tree(isolated_memory):
    """One un-ingested source, so a sweep has something to do while reporting projections."""
    raw = isolated_memory / "raw"
    (raw / "news").mkdir(parents=True, exist_ok=True)
    (raw / "news" / "one.md").write_text("# one\n", encoding="utf-8")
    db_store.init_db()
    return raw


def test_the_sweep_reports_projection_health(raw_tree, monkeypatch):
    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection(
                "pilot", "authority", "cheap",
                lambda: (projection_registry.DEGRADED, "7 behind"), lambda: "", "search",
            ),
        ),
    )

    summary = periodic_catch_up.catch_up_once()
    line = periodic_catch_up.describe(summary)

    assert "projections=[" in line
    assert "pilot=degraded(7 behind)" in line
    assert summary["errors"] == []
