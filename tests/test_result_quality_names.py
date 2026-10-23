"""Name-entry regressions: metadata names survive a saturated lexical window."""
import json

import pytest

from vector_lake import db_store, page_index_projection as projection, tool_search as search
from vector_lake.wiki_utils import get_index_path


@pytest.fixture
def named_catalog(isolated_memory):
    db_store.init_db()
    nodes: dict[str, dict] = {
        f"Concept_Noise{i}": dict(title=f"Noise{i}", type="concept", domain="General",
                                topic_cluster="agents", status="Active", summary="Agent systems")
        for i in range(250)
    }
    nodes["Concept_Autonomous"] = dict(title="自主执行体", aliases=["AI Agent"],
        type="concept", domain="General", topic_cluster="agents", status="Active")
    nodes["Concept_Other"] = dict(title="Other", aliases=["Shared Name"],
        type="concept", domain="General", topic_cluster="agents", status="Active")
    nodes["Concept_Twin"] = dict(title="Twin", aliases=["Shared Name"],
        type="concept", domain="General", topic_cluster="agents", status="Active")
    data = dict(nodes=nodes, weighted_edges=[])
    get_index_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    projection.reset_catalog_cache()
    projection.refresh_page_index_projection(data)
    return data


def test_projection_reads_scalar_alias(named_catalog):
    named_catalog['nodes']['Concept_Autonomous']['aliases'] = 'AI Agent'
    projection.refresh_page_index_projection(named_catalog)
    assert 'Concept_Autonomous' in search._name_entry_candidates(
        'AI Agent', projection._SqliteCatalog(), None, None, False, None)


@pytest.mark.parametrize("backend", ["file", "projection"])
@pytest.mark.parametrize("query", ["AI Agent", "自主执行体"])
def test_full_declared_name_enters_reranker_without_fts_hit(named_catalog, monkeypatch, backend, query):
    catalog = (projection._FileCatalog(named_catalog) if backend == "file"
               else projection._SqliteCatalog())
    monkeypatch.setattr(search, "_get_fts_recall_results", lambda query, limit=50, **kw: [
        dict(node_key=f"Concept_Noise{i}", rank=-float(300-i)) for i in range(min(limit, 250))])
    seen = []
    original = search._rerank_candidates_locally
    def rerank(query, candidates):
        seen.extend(n["_key"] for _, n in candidates)
        return original(query, candidates)
    monkeypatch.setattr(search, "_rerank_candidates_locally", rerank)
    rows, _, error = search._search_scored_pages(query, 5, domain="General", cluster="agents",
        catalog=catalog, allow_external_embedding=False, profile="auto")
    assert error is None
    assert "Concept_Autonomous" in seen
    assert len(seen) <= search._candidate_pool()
    assert rows[0][1]["_key"] == "Concept_Autonomous"


@pytest.mark.parametrize("query,expected", [
    ("AI Agent", {"Concept_Autonomous"}), ("ai agent", {"Concept_Autonomous"}),
    ("AI Agent 的特点", {"Concept_Autonomous"}), ("XAI Agentic", set()),
    ("自主", set()), ("Shared Name", {"Concept_Other", "Concept_Twin"}), ("A", set()),
])
def test_only_complete_names_and_ambiguous_names_are_retained(named_catalog, query, expected):
    hits = search._name_entry_candidates(query, projection._FileCatalog(named_catalog),
                                         "General", "agents", False, None)
    assert set(hits) == expected


@pytest.mark.parametrize("field,value", [("domain", "Elsewhere"), ("topic_cluster", "other"),
                                         ("status", "Archived")])
def test_name_entry_never_bypasses_filters(named_catalog, field, value):
    named_catalog["nodes"]["Concept_Autonomous"][field] = value
    hits = search._name_entry_candidates("AI Agent", projection._FileCatalog(named_catalog),
                                         "General", "agents", False, None)
    assert hits == {}


def test_name_entry_is_request_local_not_cross_root_or_generation(named_catalog):
    catalog = projection._FileCatalog(named_catalog)
    assert search._name_entry_candidates("AI Agent", catalog, None, None, False, None)
    assert search._name_entry_candidates("AI Agent", projection._FileCatalog({}), None, None, False, None) == {}
    assert search._name_entry_candidates("AI Agent", catalog, None, None, False, "type == 'vendor'") == {}
