"""Tests for explicit degradation reporting and filter-contract enforcement."""
import json

import pytest

from vector_lake import db_store, indexer, wiki_utils
from vector_lake.tool_search import _passes_filters, search_vector_lake


def _node(**overrides):
    node = {
        "_key": "Vendor_Acme",
        "title": "Acme",
        "domain": "Healthcare_IT",
        "topic_cluster": "HIS",
        "status": "Active",
        "type": "vendor",
    }
    node.update(overrides)
    return node


def test_filter_gate_rejects_other_domain():
    assert _passes_filters(_node(), "Healthcare_IT", None, False, None) is True
    assert _passes_filters(_node(), "Biomedicine", None, False, None) is False


def test_filter_gate_rejects_other_cluster():
    assert _passes_filters(_node(), None, "HIS", False, None) is True
    assert _passes_filters(_node(), None, "EMR", False, None) is False


def test_filter_gate_hides_non_active_status():
    assert _passes_filters(_node(status="Deprecated"), None, None, False, None) is False
    assert _passes_filters(_node(status="Deprecated"), None, None, True, None) is True


def test_filter_gate_applies_filter_expr():
    assert _passes_filters(_node(), None, None, False, "type == 'vendor'") is True
    assert _passes_filters(_node(), None, None, False, "type == 'concept'") is False


def test_filter_gate_rejects_on_bad_filter_expr():
    # A malformed expression must not silently open the gate.
    assert _passes_filters(_node(), None, None, False, "this is not python(") is False


def test_manual_write_refuses_invalid_content(isolated_memory):
    """The blanket except-pass previously disabled the defense hook for whole error classes."""
    target = wiki_utils.get_wiki_dir() / "Concept_Broken.md"
    content = "\n".join([
        "---", "id: concept_broken", "title: Broken", "type: concept", "domain: General",
        "status: NotARealStatus", "epistemic-status: evergreen", "categories: [Concept]",
        "strategic_scope: core", "evidence_tier: primary", "tags: [t1]",
        "updated: 2026-01-01T00:00:00+00:00", "links: []", "sources: []", "---", "",
        "# Broken", "",
        "## 1. 编译事实 (Compiled Truth)", "",
        "### 物理机制 (Mechanism)", "", "body", "",
        "### 适用与失效边界 (Boundaries)", "", "body", "",
        "## 2. 证据时间线 (Evidence Timeline)", "",
        "- [2026-01-01] [Observation] entry。", "",
    ])
    with pytest.raises(Exception):
        wiki_utils.atomic_write_text(target, content)
    assert not target.exists()


def test_search_reports_vector_degradation_visibly(isolated_memory, monkeypatch):
    db_store.init_db()
    indexer.generate_index()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # The key branch is only reachable when the caller opts in: with the default
    # ``allow_external_embedding=False`` the note is the (informational) policy line, not a
    # degradation, and a test that demanded [DEGRADED] without opting in was asserting a
    # contract the tool does not have.
    result = search_vector_lake("Acme HIS vendor", top_k=3, allow_external_embedding=True)

    assert "[DEGRADED]" in result
    assert "GEMINI_API_KEY" in result

    default = search_vector_lake("Acme HIS vendor", top_k=3)
    assert "External embedding disabled by request policy" in default


def test_graph_expansion_respects_domain_filter(isolated_memory, monkeypatch):
    db_store.init_db()
    wiki = wiki_utils.get_wiki_dir()
    nodes = {
        "Vendor_Acme": {"id": "vendor_acme", "title": "Acme", "type": "vendor",
                        "domain": "Healthcare_IT", "topic_cluster": "HIS", "status": "Active",
                        "aliases": [], "links": ["Concept_Other"], "sources": [],
                        "raw_text": "Acme HIS vendor body", "triples": []},
        "Concept_Other": {"id": "concept_other", "title": "Other", "type": "concept",
                          "domain": "Biomedicine", "topic_cluster": "Clinical", "status": "Active",
                          "aliases": [], "links": [], "sources": [],
                          "raw_text": "unrelated clinical concept", "triples": []},
    }
    (wiki / "Vendor_Acme.md").write_text("---\n---\nAcme body", encoding="utf-8")
    (wiki / "Concept_Other.md").write_text("---\n---\nOther body", encoding="utf-8")
    db_store.upsert_search_index("Vendor_Acme", "Acme", "", "Acme HIS vendor body")
    (wiki / "index.json").write_text(json.dumps({
        "nodes": nodes,
        "weighted_edges": [{"source": "Vendor_Acme", "target": "Concept_Other", "weight": 9.0}],
        "aliases": {},
        "categories": [],
        "error_log": [],
        "graph_state": {"dirty": False},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    filtered = search_vector_lake("Acme", top_k=5, domain="Healthcare_IT")
    assert "**Other**" not in filtered

    unfiltered = search_vector_lake("Acme", top_k=5)
    assert "**Other**" in unfiltered
