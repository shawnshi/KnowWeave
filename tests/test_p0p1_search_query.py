"""Regression oracles for the staged P0/P1 search and query changes."""

from vector_lake import db_store, tool_query, tool_search


class _Catalog:
    def __init__(self):
        self.nodes = {
            f"Concept_Other_{i:02}": {
                "title": f"Other {i}", "type": "concept", "domain": "General",
                "status": "Active", "summary": "shared keyword",
            }
            for i in range(25)
        }
        self.nodes["Concept_Target"] = {
            "title": "Target", "type": "concept", "domain": "HIT",
            "status": "Active", "summary": "shared keyword",
        }

    def nodes_by_key(self, keys):
        return {key: self.nodes[key] for key in keys if key in self.nodes}

    def adjacency(self):
        return {}


def test_filtered_fts_recall_reaches_beyond_unfiltered_depth(monkeypatch):
    catalog = _Catalog()
    keys = list(catalog.nodes)
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([], "no embedding provider"))
    monkeypatch.setattr(
        tool_search, "_get_fts_search_results",
        lambda query, limit=50: [
            {"node_key": key, "rank": -float(26 - i)}
            for i, key in enumerate(keys[:limit])
        ],
    )
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, pool: pool)

    results, _, error = tool_search._search_scored_pages(
        "shared keyword", top_k=5, domain="HIT", catalog=catalog
    )

    assert error is None
    assert [node["_key"] for _, node in results] == ["Concept_Target"]


def test_filtered_vector_recall_reaches_beyond_unfiltered_depth(monkeypatch):
    catalog = _Catalog()
    keys = list(catalog.nodes)
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda query, limit=50: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([1.0], None))
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results",
        lambda vector, limit=50: ({key: 0.9 - i * 0.001 for i, key in enumerate(keys[:limit])}, None),
    )
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, pool: pool)

    results, _, error = tool_search._search_scored_pages(
        "shared keyword", top_k=5, domain="HIT", catalog=catalog
    )

    assert error is None
    assert [node["_key"] for _, node in results] == ["Concept_Target"]


def test_default_history_filter_reaches_active_page_behind_archived_hits(monkeypatch):
    catalog = _Catalog()
    keys = [f"Concept_Archived_{i:02}" for i in range(25)]
    for key in keys:
        catalog.nodes[key] = {
            "title": key, "type": "concept", "domain": "HIT",
            "status": "Archived", "summary": "shared keyword",
        }
    keys.append("Concept_Target")
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([], "no provider"))
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda query, limit=50: [
        {"node_key": key, "rank": -float(26 - i)}
        for i, key in enumerate(keys[:limit])
    ])
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, pool: pool)

    results, _, error = tool_search._search_scored_pages(
        "shared keyword", top_k=5, catalog=catalog
    )

    assert error is None
    assert "Concept_Target" in [node["_key"] for _, node in results]


def test_mismatched_embedding_projection_is_not_searched(monkeypatch):
    catalog = _Catalog()
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MODEL", "new-model")
    monkeypatch.setattr(db_store, "embedding_projection_state", lambda: {"model": "old-model", "dimension": 3072})
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda query, limit=50: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([1.0] * 3072, None))
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results",
        lambda vector, limit=50: ({"Concept_Target": 0.9}, None),
    )

    results, notes, error = tool_search._search_scored_pages(
        "target", top_k=5, catalog=catalog
    )

    assert error is None and results == []
    assert any("model" in note and "mismatch" in note for note in notes)


def test_query_embedding_cache_isolated_by_model(monkeypatch):
    tool_search._QUERY_EMBEDDING_CACHE.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []

    def embed(texts, **kwargs):
        calls.append(len(calls))
        return [[float(len(calls)), 1.0]]

    monkeypatch.setattr("vector_lake.embedding_scheduler.embed_texts", embed)
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MODEL", "model-a")
    first, _ = tool_search._get_query_embedding("same query")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MODEL", "model-b")
    second, _ = tool_search._get_query_embedding("same query")

    assert first != second
    assert len(calls) == 2
    tool_search._QUERY_EMBEDDING_CACHE.clear()


def test_excerpt_returns_late_evidence_with_heading_and_line():
    page = "---\ntitle: Test\n---\n## Background\n" + ("irrelevant " * 300) + (
        "\n## Technical evidence\nThe DRG settlement interface must reconcile every invoice.\n"
    )

    excerpt, locator = tool_search._relevant_excerpt(page, "DRG settlement interface")

    assert "reconcile every invoice" in excerpt
    assert "Technical evidence" in locator
    assert "L" in locator
    assert "irrelevant irrelevant" not in excerpt


def _oversized_context(budget=230):
    return {
        "memory_packet": "small memory", "memory_count": 1,
        "memory_warning_count": 0, "memory_omitted_count": 0,
        "wiki_context": "- **Page**\n" + "a" * 350,
        "wiki_page_count": 1, "index_summary": "Index text", "purpose": "",
        "retrieval_notes": ["vector retrieval unavailable"],
        "budget_used": 380, "budget_max": budget,
    }


def test_final_serialized_query_context_stays_within_character_budget():
    context = _oversized_context()
    payload = tool_query._render_context_payload("question", context)

    assert len(payload) <= context["budget_max"]
    assert "small memory" in payload
    assert "vector retrieval unavailable" in payload
    assert "Page" not in payload  # An entire over-budget page is omitted.


def test_non_ascii_serialized_context_respects_utf8_byte_ceiling():
    context = _oversized_context(budget=600)
    context["wiki_context"] = "- **Page**\n" + "医疗证据" * 60
    payload = tool_query._render_context_payload("question", context)

    assert len(payload.encode("utf-8")) <= context["budget_max"]
    assert "Wiki pages omitted" in payload


def test_context_preview_does_not_create_proposal_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: _oversized_context())

    preview = tool_query.preview_query_context("question")

    assert "small memory" in preview
    assert not (tmp_path / "tmp").exists()


def test_mcp_exposes_inline_preview_separately_from_proposal(monkeypatch):
    from vector_lake import mcp_server

    monkeypatch.setattr(mcp_server.tools, "preview_query_context", lambda query, **scope: f"inline:{query}:{scope['domain']}", raising=False)
    assert "preview_query_context" in mcp_server.registered_tool_names()
    assert mcp_server.preview_query_context("q", domain="Medical_IT") == "inline:q:Medical_IT"


def test_filtered_recall_cap_is_explicit_when_eligible_page_lies_beyond_it(monkeypatch):
    catalog = _Catalog()
    monkeypatch.setattr(tool_search, "MAX_FILTERED_RECALL_CANDIDATES", 25, raising=False)
    keys = list(catalog.nodes)

    rows, error, note = tool_search._filtered_recall(
        lambda limit: ([{"node_key": key} for key in keys[:limit]], None),
        catalog, 5, "HIT", None, False, None,
    )

    assert error is None and rows == []
    assert "cap" in note and "incomplete" in note


def test_xml_evidence_locator_remains_parseable_with_heading_punctuation(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "Concept_Target.md").write_text(
        "## A's & B\nThis DRG claim has a source.\n", encoding="utf-8"
    )
    monkeypatch.setattr(tool_search, "get_wiki_dir", lambda: wiki)
    monkeypatch.setattr(
        tool_search, "_search_scored_pages",
        lambda *args, **kwargs: ([(1.0, {"_key": "Concept_Target", "title": "Target"})], [], None),
    )

    xml = tool_search.search_vector_lake("DRG", as_xml=True)
    node = ET.fromstring(xml)
    assert node.attrib["Locator"].startswith("L1 (A's & B)")


def test_filtered_vector_drain_names_the_selectors_not_a_missing_projection(monkeypatch):
    """A drained candidate set is a selector outcome; ``embedding-backfill`` cannot fix a filter."""
    catalog = _Catalog()
    general = [key for key in catalog.nodes if key != "Concept_Target"]
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda query, limit=50: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([1.0], None))
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results",
        lambda vector, limit=50: ({key: 0.9 for key in general[:limit]}, None),
    )
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, pool: pool)

    results, notes, error = tool_search._search_scored_pages(
        "shared keyword", top_k=5, domain="HIT", catalog=catalog
    )

    assert error is None and results == []
    assert any("selectors" in note for note in notes), notes
    assert not any("embedding-backfill" in note for note in notes), notes


def test_unrecorded_projection_with_stored_vectors_is_not_searched(monkeypatch):
    """Vectors stored without a recorded projection are unverifiable, so the arm must stay off."""
    catalog = _Catalog()
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(db_store, "embedding_projection_state", lambda: {})
    monkeypatch.setattr(db_store, "count_embeddings", lambda: 5)
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda query, limit=50: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([1.0], None))
    monkeypatch.setattr(
        tool_search, "_get_vector_search_results",
        lambda vector, limit=50: ({"Concept_Target": 0.9}, None),
    )

    results, notes, error = tool_search._search_scored_pages(
        "target", top_k=5, catalog=catalog
    )

    assert error is None and results == []
    assert any("unverified" in note for note in notes), notes
