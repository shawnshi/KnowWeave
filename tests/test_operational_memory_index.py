"""Differential and maintenance tests for the operational-memory search projection.

``operational_memory_index`` exists so ``search_operational_memory`` stops
decoding the whole ``operational_memory`` table on every query.  The projection
is only allowed to be a *performance* change, so the central assertion here is
that the indexed backend returns exactly what the full-scan oracle returns.
"""

import json

import pytest

from vector_lake import db_store, governance_store
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
