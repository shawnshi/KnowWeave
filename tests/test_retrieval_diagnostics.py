"""User-facing retrieval notes must distinguish policy information from lost recall."""

import asyncio

import pytest

from vector_lake import tool_query, tool_search


@pytest.mark.parametrize("mode", ["boost", "filter"])
def test_author_policy_is_information_not_degradation(monkeypatch, mode):
    note = f"VECTOR_LAKE_AUTHOR_FACET={mode}: 1 of 2 candidates"
    monkeypatch.setattr(tool_search, "_search_scored_pages", lambda *a, **k: ([], [note], None))

    result = tool_search.search_vector_lake("WiNEX", domain="Medical_IT")

    assert "[INFO] Retrieval notes:" in result
    assert note in result
    assert "[DEGRADED]" not in result


def test_real_degradation_is_not_hidden_by_author_information(monkeypatch):
    info = "VECTOR_LAKE_AUTHOR_FACET=boost: 1 of 2 candidates"
    warning = "vector: filtered recall cap (4096) reached; results may be incomplete"
    monkeypatch.setattr(
        tool_search, "_search_scored_pages", lambda *a, **k: ([], [info, warning], None)
    )

    result = tool_search.search_vector_lake("WiNEX", domain="Medical_IT")

    assert f"[DEGRADED] Retrieval notes: {warning}" in result
    assert f"[INFO] Retrieval notes: {info}" in result


def test_unclassified_note_remains_a_degradation(monkeypatch):
    warning = "unknown retrieval backend failed"
    monkeypatch.setattr(tool_search, "_search_scored_pages", lambda *a, **k: ([], [warning], None))

    assert f"[DEGRADED] Retrieval notes: {warning}" in tool_search.search_vector_lake("probe")


def test_query_packet_preserves_note_severity():
    info = "VECTOR_LAKE_AUTHOR_FACET=boost: 1 of 2 candidates"
    warning = "vector arm failed: unavailable"
    context = {
        "memory_packet": "", "wiki_context": "", "index_summary": "", "purpose": "",
        "retrieval_notes": [info, warning], "budget_max": 4000,
    }

    result = tool_query._render_context_payload("probe", context)

    assert f"--- RETRIEVAL NOTES (information) ---\n- {info}" in result
    assert f"--- RETRIEVAL NOTES (degradations) ---\n- {warning}" in result
    assert len(result.encode("utf-8")) <= context["budget_max"]


def test_domain_guidance_uses_real_categories_not_a_global_general_fallback():
    from vector_lake import mcp_server

    from fastmcp import Client

    async def registered_domain_description():
        async with Client(mcp_server.mcp) as client:
            tools = await client.list_tools()
            tool = next(tool for tool in tools if tool.name == "search_vector_lake")
            return tool.input_schema["properties"]["domain"]["description"]

    description = asyncio.run(registered_domain_description())

    assert "Medical_IT" in description
    assert "Policy_and_Governance" in description
    assert "General is a literal category, not all domains" in description
    assert "'HIT', 'Clinical'" not in description


def test_sparse_similarity_cap_reports_depth_even_when_metadata_accepts_all(monkeypatch):
    monkeypatch.setattr(tool_search, "MAX_FILTERED_RECALL_CANDIDATES", 16)
    calls = []

    class Catalog:
        def nodes_by_key(self, keys):
            return {key: {"domain": "General", "status": "Active"} for key in keys}

    def fetch(limit):
        calls.append(limit)
        return {"a": 0.8, "b": 0.7}, None

    rows, error, note = tool_search._filtered_recall(
        fetch, Catalog(), 5, "General", None, False, None, raw_count=lambda: calls[-1]
    )

    assert error is None and list(rows) == ["a", "b"]
    assert calls == [5, 10, 16]
    assert "results may be incomplete" in note
    assert "raw_examined=16" in note
    assert "target_depth=5" in note
    assert "selected=2" in note
