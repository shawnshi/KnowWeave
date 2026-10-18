"""What the ledger records must let a slow call be explained, not just noticed.

Three defects made the production ledger unreadable and are pinned here:

* a page search nested inside a context assembly was recorded as ``mode="page"`` with the
  assembly's duration, so the two surfaces were indistinguishable;
* nothing said which process answered or how old it was, so a restarted (cold) process looked
  like a slow one;
* nothing said whether a call rebuilt the graph or parsed the catalog, so the most expensive
  per-request work could only be inferred from latency.
"""

import json

import pytest

from vector_lake import db_store, governance_store, retrieval_runtime as runtime
from vector_lake import search_ledger, tool_search, tool_query


def _page(conn, key: str, domain: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO page_index_nodes (node_key, node_id, title, type, status, domain, "
        "topic_cluster, node_json) VALUES (?,?,?,?,?,?,?,?)",
        (key, key.lower(), key, "concept", "Active", domain, "target", json.dumps({"id": key})),
    )


def _memory(conn, memory_id: str, text: str, page: str, domain: str) -> None:
    payload = {"memory_id": memory_id, "memory_type": "fact", "memory_key": memory_id,
               "text": text, "source_page": page, "validity_state": "active",
               "memory_score": 0.5, "updated_at": "2026-07-14T00:00:00+00:00"}
    conn.execute(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
        (memory_id, "fact", 0.5, "Active", 365.0, json.dumps(payload, ensure_ascii=False),
         payload["updated_at"]),
    )


@pytest.fixture
def recorded(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _page(conn, "Concept_Telemetry", "Medical_IT")
        _memory(conn, "mem_telemetry", "医院 电子病历 评级 目标", "Concept_Telemetry.md",
                "Medical_IT")
        # A lexical hit is what seeds the graph walk the counter test observes.
        from vector_lake import tokenizer
        db_store.upsert_search_index("Concept_Telemetry",
                                     tokenizer.tokenize_joined("医院 电子病历 评级 目标"), "", "")
    rows: list[dict] = []
    monkeypatch.setattr(search_ledger, "record", lambda **payload: rows.append(payload))
    monkeypatch.setattr(runtime.time, "monotonic", runtime.time.monotonic)
    return rows


def test_a_page_search_and_a_context_assembly_are_told_apart(recorded):
    with runtime.request_scope("电子病历 评级", top_k=5):
        tool_search.search_vector_lake("电子病历 评级", top_k=5, domain="Medical_IT")
    with runtime.request_scope("电子病历 评级", max_chars=200000):
        tool_query.assemble_context("电子病历 评级", domain="Medical_IT", profile="auto")

    search_entry, context_entry = recorded[0], recorded[-1]
    assert search_entry["entry_point"] == "search"
    assert context_entry["entry_point"] == "context"
    # The stage keeps its own duration; the whole request is recorded beside it.  They used to be
    # the same number because ``_finish`` overwrote ``elapsed_ms`` with the scope total.
    assert context_entry["elapsed_ms"] < context_entry["request_total_ms"]
    assert context_entry["elapsed_ms"] >= 0


def test_every_entry_names_the_process_that_answered(recorded):
    with runtime.request_scope("电子病历 评级", top_k=5):
        tool_search.search_vector_lake("电子病历 评级", top_k=5, domain="Medical_IT")
    process = recorded[0]["process"]
    assert process["pid"] == runtime.PROCESS_ID
    assert process["token"] == runtime.PROCESS_TOKEN
    assert process["uptime_ms"] >= 0


def test_structural_work_is_counted_per_request(recorded, monkeypatch):
    monkeypatch.setattr(tool_search, "_PPR_INDEX",
                        {"generation": None, "adj": None, "index": None})
    from vector_lake import page_index_projection
    page_index_projection._invalidate_adjacency()

    with runtime.request_scope("电子病历 评级", top_k=5):
        tool_search.search_vector_lake("电子病历 评级", top_k=5, domain="Medical_IT")
        # The rebuild is forced inside the request, which is what a cold graph walk does there.
        page_index_projection.adjacency()
    cold = recorded[-1].get("cache_events") or {}
    assert cold.get("adjacency_build") == 1

    with runtime.request_scope("电子病历 评级", top_k=5):
        tool_search.search_vector_lake("电子病历 评级", top_k=5, domain="Medical_IT")
    warm = recorded[-1].get("cache_events") or {}
    assert "adjacency_build" not in warm
