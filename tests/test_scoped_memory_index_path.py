"""The scoped index path must be a fast path, never a different answer.

``scoped_memory_search.search_views`` answers a domain-scoped memory read from the n-gram
postings and the ``operational_memory_index`` columns when the accelerators and the gram index
are available, and from the payload scan otherwise.  The two are only allowed to differ in cost:
this file builds a corpus where both are usable, takes the scan's answer as the oracle (by
disabling the index path) and asserts the fast path returns the same ids and the same scores.
"""

import json

import pytest

from vector_lake import db_store, governance_store as store, memory_gram_index
from vector_lake import scoped_memory_search as scoped
from vector_lake import scoped_retrieval_index as indexes


def _page(conn, key: str, domain: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO page_index_nodes (node_key, node_id, title, type, status, domain, "
        "topic_cluster, node_json) VALUES (?,?,?,?,?,?,?,?)",
        (key, key.lower(), key, "concept", "Active", domain, "target", json.dumps({"id": key})),
    )


def _memory(conn, memory_id: str, text: str, source_page: str, **extra) -> None:
    payload = {
        "memory_id": memory_id,
        "memory_type": extra.pop("memory_type", "fact"),
        "memory_key": extra.pop("memory_key", memory_id),
        "text": text,
        "source_page": source_page,
        "validity_state": extra.pop("validity_state", "active"),
        "memory_score": extra.pop("memory_score", 0.5),
        "updated_at": extra.pop("updated_at", "2026-07-14T00:00:00+00:00"),
    }
    payload.update(extra)
    conn.execute(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
        (memory_id, payload["memory_type"], float(payload["memory_score"]), "Active", 365.0,
         json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
    )


@pytest.fixture
def indexed_scope(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _page(conn, "Concept_Med", "Medical_IT")
        _page(conn, "Concept_Ai", "Artificial_Intelligence")
        _page(conn, "Concept_Gen", "General")
        # Domain comes from the page; one row carries its own override, which must win.
        _memory(conn, "mem_med_1", "医院 电子病历 评级 DRG 支付", "Concept_Med.md", memory_type="decision",
                memory_score=0.9)
        _memory(conn, "mem_med_2", "医院 电子病历 六级 评级 目标", "Concept_Med.md", memory_type="task_state",
                memory_score=0.8)
        _memory(conn, "mem_med_3", "DRG 支付 合规 运营 医院", "Concept_Med.md", memory_score=0.6)
        _memory(conn, "mem_med_4", "医院 电子病历 归档", "Concept_Med.md",
                validity_state="superseded", memory_score=0.4)
        _memory(conn, "mem_override", "医院 电子病历 评级", "Concept_Gen.md", domain="Medical_IT",
                memory_score=0.7)
        _memory(conn, "mem_ai_1", "Agent 编排 医院 电子病历", "Concept_Ai.md", memory_score=0.5)
        _memory(conn, "mem_gen_1", "医院 电子病历 评级", "Concept_Gen.md", memory_score=0.3)
    indexes.apply(conn)
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    assert memory_gram_index.gram_index_usable() is True
    return conn


def _views(query, monkeypatch, *, fast: bool, domain="Medical_IT", windows=((12, False, None), (12, True, None))):
    original = scoped._index_views
    if not fast:
        monkeypatch.setattr(scoped, "_index_views", lambda *a, **k: None)
    try:
        return store.search_memory_packet_views(
            query, active_top_k=windows[0][0], history_top_k=windows[1][0], domain=domain,
        )
    finally:
        monkeypatch.setattr(scoped, "_index_views", original)


@pytest.mark.parametrize("query", ["医院 电子病历 评级", "DRG 支付", "评级", "Agent"])
@pytest.mark.parametrize("include_override_row", [True, False])
def test_the_index_path_returns_the_scan_answer(indexed_scope, monkeypatch, query, include_override_row):
    if not include_override_row:
        with db_store.transaction():
            indexed_scope.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_override'")
        assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")

    fast = _views(query, monkeypatch, fast=True)
    scan = _views(query, monkeypatch, fast=False)

    assert [[m["memory_id"] for m in window] for window in fast] == \
           [[m["memory_id"] for m in window] for window in scan]
    assert [[m["retrieval_score"] for m in window] for window in fast] == \
           [[m["retrieval_score"] for m in window] for window in scan]


def test_the_index_path_is_the_one_that_answers(indexed_scope, monkeypatch):
    """Guard: the differential above must not be comparing the scan with itself."""
    calls = []
    original_scan = scoped._scan_scope

    def counted(*args, **kwargs):
        calls.append(1)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(scoped, "_scan_scope", counted)
    store.search_memory_packet_views("医院 电子病历 评级", active_top_k=12, history_top_k=12,
                                     domain="Medical_IT")
    assert calls == []
    assert memory_gram_index.gram_index_usable() is True
    indexes.status(indexed_scope)["memory_acceleration"] is True


def test_a_dirty_gram_index_defers_to_the_scan(indexed_scope, monkeypatch):
    with db_store.transaction():
        _memory(indexed_scope, "mem_med_5", "医院 电子病历 评级 新条目", "Concept_Med.md")
    monkeypatch.setattr(memory_gram_index, "gram_index_usable", lambda: False)
    calls = []
    original_scan = scoped._scan_scope

    def counted(*args, **kwargs):
        calls.append(1)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(scoped, "_scan_scope", counted)
    active, _history = store.search_memory_packet_views(
        "医院 电子病历 评级", active_top_k=12, history_top_k=12, domain="Medical_IT",
    )
    assert calls == [1]
    assert any(m["memory_id"] == "mem_med_5" for m in active)


@pytest.mark.parametrize("cluster", ["target", "other", ""])
def test_the_index_path_answers_a_cluster_scope_too(indexed_scope, monkeypatch, cluster):
    """``cluster=`` resolves on the same two axes (own value, else the page's), so both must match."""
    calls = []
    original_scan = scoped._scan_scope

    def counted(*args, **kwargs):
        calls.append(1)
        return original_scan(*args, **kwargs)

    fast = store.search_memory_packet_views("医院 电子病历 评级", active_top_k=12, history_top_k=12,
                                            domain="Medical_IT", cluster=cluster or None)
    monkeypatch.setattr(scoped, "_scan_scope", counted)
    monkeypatch.setattr(scoped, "_index_views", lambda *a, **k: None)
    scan = store.search_memory_packet_views("医院 电子病历 评级", active_top_k=12, history_top_k=12,
                                            domain="Medical_IT", cluster=cluster or None)
    assert calls == [1]
    assert [[m["memory_id"] for m in w] for w in fast] == [[m["memory_id"] for m in w] for w in scan]


@pytest.mark.parametrize("memory_cluster", ["", "target"])
def test_cluster_owner_rows_win_over_the_page(indexed_scope, monkeypatch, memory_cluster):
    """A row that states its own cluster is classified by that value, not by its page."""
    with db_store.transaction():
        indexed_scope.execute(
            "UPDATE operational_memory SET data_json = json_set(data_json, '$.topic_cluster', ?) "
            "WHERE memory_id = 'mem_med_1'",
            (memory_cluster,),
        )
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")

    fast = store.search_memory_packet_views("医院 电子病历 评级", active_top_k=12, history_top_k=12,
                                            domain="Medical_IT", cluster="target")
    monkeypatch.setattr(scoped, "_index_views", lambda *a, **k: None)
    scan = store.search_memory_packet_views("医院 电子病历 评级", active_top_k=12, history_top_k=12,
                                            domain="Medical_IT", cluster="target")
    assert [[m["memory_id"] for m in w] for w in fast] == [[m["memory_id"] for m in w] for w in scan]
