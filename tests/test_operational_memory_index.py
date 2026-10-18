"""Differential and maintenance tests for the operational-memory search projection.

``operational_memory_index`` exists so ``search_operational_memory`` stops
decoding the whole ``operational_memory`` table on every query.  The projection
is only allowed to be a *performance* change, so the central assertion here is
that the indexed backend returns exactly what the full-scan oracle returns.
"""

import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake import memory_gram_index
from vector_lake import tool_search


def _insert_memory(conn, memory_id: str, memory: dict, updated_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            str(memory.get("memory_type", "fact")),
            float(memory.get("memory_score", 0.0)),
            str(memory.get("status", "Active")),
            float(memory.get("ttl_days", 365)),
            json.dumps(memory, ensure_ascii=False),
            updated_at,
        ),
    )


def _memory(memory_id: str, text: str, memory_type: str = "fact", **extra) -> dict:
    memory = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": extra.pop("memory_key", memory_id),
        "text": text,
        "source_claim_id": f"claim_{memory_id}",
        "source_page": extra.pop("source_page", "Concept_Test.md"),
        "validity_state": extra.pop("validity_state", "active"),
        "memory_score": extra.pop("memory_score", 0.5),
        "updated_at": extra.pop("updated_at", "2026-07-14T00:00:00+00:00"),
    }
    memory.update(extra)
    return memory


@pytest.fixture
def seeded_memory(isolated_memory):
    """A corpus that exercises types, hidden states, scores and tie groups."""
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        entries = [
            ("mem_alpha", _memory("mem_alpha", "信创 HIS 集成平台 选型结论", "decision", memory_score=0.9)),
            ("mem_beta", _memory("mem_beta", "医院 电子病历六级 评级 目标", "task_state", memory_score=0.7)),
            ("mem_gamma", _memory("mem_gamma", "preferred deployment target is Kubernetes", "preference", memory_score=0.8)),
            ("mem_delta", _memory("mem_delta", "DRG 医保支付 合规 运营", memory_score=0.6)),
            ("mem_eps", _memory("mem_eps", "DRG 结算 清单 质控", memory_score=0.6)),
            ("mem_zeta", _memory("mem_zeta", "DRG 病案 首页 上传", memory_score=0.6)),
            ("mem_hidden", _memory("mem_hidden", "信创 HIS 旧结论", "decision", validity_state="superseded")),
            ("mem_unrelated", _memory("mem_unrelated", "unrelated payload")),
        ]
        for memory_id, memory in entries:
            _insert_memory(conn, memory_id, memory, memory["updated_at"])
    return isolated_memory


def _ids(memories):
    return [memory["memory_id"] for memory in memories]


def _both_backends(monkeypatch, query, **kwargs):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "legacy")
    legacy = governance_store.search_operational_memory(query, **kwargs)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "index")
    indexed = governance_store.search_operational_memory(query, **kwargs)
    return legacy, indexed


@pytest.mark.parametrize(
    "query,kwargs",
    [
        ("信创 医院 HIS 集成平台", {"top_k": 8}),
        ("DRG 医保支付", {"top_k": 3}),
        ("DRG", {"top_k": 8}),
        ("deployment target", {"top_k": 5}),
        ("unrelated", {"top_k": 5}),
        ("", {"top_k": 8}),
        ("信创", {"top_k": 8, "include_history": True}),
        ("HIS", {"top_k": 8, "memory_types": ["decision"]}),
        ("DRG", {"top_k": 8, "memory_types": ["fact"]}),
        ("信创", {"top_k": 8, "memory_types": ["preference"], "include_history": True}),
    ],
)
def test_indexed_backend_matches_the_full_scan_oracle(seeded_memory, monkeypatch, query, kwargs):
    legacy, indexed = _both_backends(monkeypatch, query, **kwargs)

    assert _ids(indexed) == _ids(legacy)


def test_projection_tracks_insert_update_and_delete(seeded_memory):
    conn = db_store.get_connection()

    def projected(memory_id):
        return conn.execute(
            "SELECT memory_type, validity_state, text_blob FROM operational_memory_index "
            "WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

    assert projected("mem_alpha")["text_blob"] == "信创 his 集成平台 选型结论"

    with db_store.transaction():
        _insert_memory(
            conn,
            "mem_alpha",
            _memory("mem_alpha", "changed text", "preference", memory_score=0.1),
            "2026-07-15T00:00:00+00:00",
        )
    updated = projected("mem_alpha")
    assert updated["memory_type"] == "preference"
    assert updated["text_blob"] == "changed text"

    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = ?", ("mem_alpha",))
    assert projected("mem_alpha") is None


def test_projection_mirrors_save_memory_objects(seeded_memory):
    store = governance_store.load_memory_objects()
    store["items"].pop("mem_beta")
    store["items"]["mem_new"] = _memory("mem_new", "brand new runtime fact")
    governance_store.save_memory_objects(store)

    conn = db_store.get_connection()
    rows = {
        row["memory_id"]
        for row in conn.execute("SELECT memory_id FROM operational_memory_index")
    }
    assert "mem_beta" not in rows
    assert "mem_new" in rows
    assert rows == {
        row["memory_id"]
        for row in conn.execute("SELECT memory_id FROM operational_memory")
    }


def test_drift_report_and_repair(seeded_memory):
    conn = db_store.get_connection()
    assert db_store.operational_memory_index_drift() == {"missing": 0, "stale": 0}

    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_index WHERE memory_id = ?", ("mem_delta",))
        conn.execute(
            "UPDATE operational_memory_index SET source_updated_at = 'tampered' WHERE memory_id = ?",
            ("mem_eps",),
        )

    assert db_store.operational_memory_index_drift() == {"missing": 1, "stale": 1}

    result = db_store.ensure_operational_memory_index(force=True)

    assert result["canonical"] == result["projected"]
    assert db_store.operational_memory_index_drift() == {"missing": 0, "stale": 0}


def test_search_repairs_a_missing_projection_row(seeded_memory, monkeypatch):
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_index WHERE memory_id = ?", ("mem_delta",))

    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "index")

    indexed = governance_store.search_operational_memory("DRG 医保支付", top_k=5)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "legacy")
    legacy = governance_store.search_operational_memory("DRG 医保支付", top_k=5)

    assert _ids(indexed) == _ids(legacy)


def test_indexed_backend_falls_back_when_the_projection_is_unusable(seeded_memory, monkeypatch):
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DROP TABLE operational_memory_index")

    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "index")
    indexed = governance_store.search_operational_memory("DRG", top_k=3)

    assert _ids(indexed) == ["mem_delta", "mem_eps", "mem_zeta"]


def test_query_embedding_cache_is_bounded_and_serves_repeats(monkeypatch):
    tool_search._QUERY_EMBEDDING_CACHE.clear()
    calls = []

    def fake_embed(texts, budget_seconds=None, durable_reservation=True):
        calls.append(list(texts))
        return [[0.25] * 4 for _ in texts]

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("vector_lake.embedding_scheduler.embed_texts", fake_embed)

    first, error = tool_search._get_query_embedding("repeat me")
    second, error_again = tool_search._get_query_embedding("repeat me")

    assert error is None and error_again is None
    assert first == second == [0.5] * 4
    assert len(calls) == 1, "a repeat query must not hit the provider again"

    for index in range(tool_search.QUERY_EMBEDDING_CACHE_SIZE + 10):
        tool_search._store_query_embedding(f"query-{index}", [float(index)])

    assert len(tool_search._QUERY_EMBEDDING_CACHE) == tool_search.QUERY_EMBEDDING_CACHE_SIZE
    assert tool_search._cached_query_embedding("repeat me") is None, "the oldest entry must be evicted"
    tool_search._QUERY_EMBEDDING_CACHE.clear()


def test_query_embedding_failure_is_not_cached(monkeypatch):
    tool_search._QUERY_EMBEDDING_CACHE.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "vector_lake.embedding_scheduler.embed_texts",
        lambda _texts, budget_seconds=None, durable_reservation=True: [],
    )

    vector, reason = tool_search._get_query_embedding("transient failure")

    assert vector == []
    assert "no vector" in reason
    assert tool_search._cached_query_embedding("transient failure") is None


def test_a_stale_source_rowid_is_detected_and_repaired(seeded_memory):
    """A table rebuild moves every memory rowid and bypasses the trigger that records it.

    The column is a physical identity, so it can rot without any content change.  A bounded sample
    has to notice, and the next initialisation has to restore it -- otherwise the bridge from
    posting doc ids to payloads silently names the wrong row.
    """
    conn = db_store.get_connection()
    with db_store.transaction():
        db_store.ensure_operational_memory_index(conn, force=True)
    assert db_store._om_source_rowid_is_stale(conn) is False

    with db_store.transaction():
        conn.execute("UPDATE operational_memory_index SET source_rowid = source_rowid + 1000000")

    assert db_store._om_source_rowid_is_stale(conn) is True
    db_store._INITIALIZED_DB_PATHS.clear()
    db_store.init_db()

    assert db_store._om_source_rowid_is_stale(conn) is False
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM operational_memory_index i "
        "JOIN operational_memory m ON m.memory_id = i.memory_id WHERE i.source_rowid <> m.rowid"
    ).fetchone()[0]
    assert mismatched == 0


def test_an_unset_source_rowid_is_unknown_not_stale(seeded_memory):
    """Rows written before the column existed carry 0; that is not evidence of drift."""
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("UPDATE operational_memory_index SET source_rowid = 0")
    assert db_store._om_source_rowid_is_stale(conn) is False


def test_a_database_from_before_the_identity_map_converges(seeded_memory):
    """The DDL has to run on an existing database, not only on a fresh one.

    ``init_db`` skips its whole schema transaction when the sentinels are present, which is what
    keeps a reader from waiting behind a writer's lock on every start.  A release that adds a table
    is therefore invisible to it unless the table is a sentinel -- and the read path that resolves
    document numbers through it would then meet the previous release's triggers on a database that
    looks complete.  Dropping the table and the triggers is that database.
    """
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DROP TABLE operational_memory_gram_docs")
        for trigger in ("trg_om_gram_dirty_insert", "trg_om_gram_dirty_update",
                        "trg_om_gram_dirty_delete"):
            conn.execute(f"DROP TRIGGER {trigger}")

    db_store._INITIALIZED_DB_PATHS.clear()
    db_store.init_db()

    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operational_memory_gram_docs'"
    ).fetchone()[0] == 1
    attached = {
        str(row[0])
        for row in conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_om_gram_dirty_%'"
        )
    }
    assert attached == {"operational_memory"}, attached


def test_rebuilding_the_index_keeps_the_gram_base_usable(seeded_memory):
    """The gram index must not be invalidated by a rebuild of the projection under it.

    This is the acceptance evidence for separating a document's identity from its address.  The
    postings name document numbers; while those numbers *were* ``operational_memory_index.rowid``,
    re-deriving the projection deleted and re-inserted every row, so the base kept numbers that now
    named other memories -- and the index reported itself usable the whole time.  One force rebuild
    restored rowids, one migration left them shifted, and either way the answers came from the
    wrong document with nothing to notice it by.

    With the identity map in place the numbers are allocated once and never reused, so the same
    force rebuild leaves the base addressable and current.
    """
    conn = db_store.get_connection()
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    assert memory_gram_index.gram_index_usable() is True
    before = {row[0] for row in conn.execute("SELECT doc FROM operational_memory_gram_docs")}

    with db_store.transaction():
        db_store.ensure_operational_memory_index(conn, force=True)

    assert {row[0] for row in conn.execute("SELECT doc FROM operational_memory_gram_docs")} == before, (
        "a projection rebuild must not renumber an identity"
    )
    assert memory_gram_index.gram_index_usable() is True, (
        "the base went stale behind a projection rebuild"
    )
    assert memory_gram_index.pending_doc_count(conn) == 0


def test_a_projection_rebuild_leaves_the_answers_unchanged(seeded_memory, monkeypatch):
    """Same acceptance, read as a result rather than as a predicate.

    ``gram_index_usable()`` is a count of markers; what has to survive is the answer.  Moving every
    projection row id is precisely what a re-derived projection does -- the canonical table's row
    ids were in the tens of millions on the live lake while the projection's were 1..N -- and a base
    addressed by rowid would now score other memories' payloads, which is a *plausible* answer, not
    an empty one, and therefore invisible without the oracle to compare it to.
    """
    conn = db_store.get_connection()
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    query = "信创 医院 集成平台"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "legacy")
    oracle = [item["memory_id"] for item in governance_store.search_operational_memory(query, top_k=8)]
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "gram")
    before = [item["memory_id"] for item in governance_store.search_operational_memory(query, top_k=8)]
    assert before and before == oracle

    with db_store.transaction():
        conn.execute("UPDATE operational_memory_index SET rowid = rowid + 1000000")

    after = [item["memory_id"] for item in governance_store.search_operational_memory(query, top_k=8)]
    assert after == before == oracle, "the indexed path changed its answer when addresses moved"
