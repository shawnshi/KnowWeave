import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from vector_lake import tool_graph
from vector_lake.relation_catalog import node_field


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    """Explicit catalog contracts; the sibling implementation is integration-owned."""
    module = ModuleType("vector_lake.relation_catalog")
    build = Mock(return_value={"relations": [], "diagnostics": [], "scope": {}, "truncated": False})
    vars(module)["build_relation_catalog"] = build
    vars(module)["node_field"] = node_field
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return build


def _declaration(source, target, predicate, layer="declared"):
    return {"source": source, "target": target, "predicate": predicate, "layer": layer,
            "declared": layer == "declared", "verified": False, "weight": None,
            "origin": {"page_key": source}}


def test_extract_backbone_edges_filters_invalid_and_self_loops():
    valid_nodes = {"nodeA", "nodeB", "nodeC"}
    raw_edges = [
        {"source": "nodeA", "target": "nodeB", "weight": 5.0},
        {"source": "nodeA", "target": "nodeA", "weight": 10.0},  # self loop
        {"source": "nodeA", "target": "danglingNode", "weight": 20.0},  # invalid target
        {"source": "unknownSource", "target": "nodeB", "weight": 15.0},  # invalid source
        {"source": "nodeB", "target": "nodeC", "weight": 1.0},
    ]

    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges,
        valid_node_ids=valid_nodes,
        max_edges=10,
        min_weight=0.0,
    )

    assert len(edges) == 2
    sources_targets = {(e["source"], e["target"]) for e in edges}
    assert ("nodeA", "nodeB") in sources_targets
    assert ("nodeB", "nodeC") in sources_targets
    assert degrees["nodeA"] == 1
    assert degrees["nodeB"] == 2
    assert degrees["nodeC"] == 1


def test_extract_backbone_edges_enforces_max_edges_and_retains_connectivity():
    valid_nodes = {f"node_{i}" for i in range(20)}
    raw_edges = []
    # Create 50 edges with varying weights
    for i in range(19):
        raw_edges.append({"source": f"node_{i}", "target": f"node_{i+1}", "weight": float(i + 1)})
    for i in range(30):
        raw_edges.append({"source": f"node_{i%10}", "target": f"node_{(i+5)%20}", "weight": float(i * 2)})

    max_limit = 10
    edges, degrees = tool_graph._extract_backbone_edges(
        raw_edges=raw_edges,
        valid_node_ids=valid_nodes,
        max_edges=max_limit,
        min_weight=0.0,
    )

    assert len(edges) == max_limit
    assert all(e["weight"] > 0 for e in edges)
    # Check relations are populated
    assert all("relation" in e for e in edges)


def test_build_page_graph_structure():
    index_data = {
        "nodes": {
            "Concept_AI": {"id": "nid1", "title": "AI", "categories": ["Concept"], "links": ["Product_Bot"]},
            "Product_Bot": {"id": "nid2", "title": "Bot", "categories": ["Product"], "links": []},
            "Orphan_Node": {"id": "nid3", "title": "Orphan", "categories": ["Other"], "links": []},
        },
        "communities": {"Concept_AI": 1, "Product_Bot": 1, "Orphan_Node": 2},
        "weighted_edges": [
            {"source": "Concept_AI", "target": "Product_Bot", "weight": 12.5},
        ],
        "community_labels": {"1": "AI Cluster", "2": "Other Cluster"},
    }

    page_graph = tool_graph._build_page_graph(index_data, max_edges=10)
    assert len(page_graph["nodes"]) == 3
    assert len(page_graph["edges"]) == 1

    node_map = {n["id"]: n for n in page_graph["nodes"]}
    assert node_map["Concept_AI"]["degree"] == 1
    assert node_map["Concept_AI"]["backbone_degree"] == 1
    assert not node_map["Concept_AI"]["is_orphan"]

    assert node_map["Orphan_Node"]["is_orphan"]
    assert node_map["Orphan_Node"]["backbone_degree"] == 0


def test_declared_relations_survive_the_backbone_budget_without_becoming_weights(catalog):
    catalog.return_value["relations"] = [
        _declaration("Vendor_Acme", "Product_Bot", "wikilink", "navigation"),
        _declaration("Vendor_Acme", "Product_Bot", "part-of"),
    ]
    index_data = {
        "nodes": {
            "Vendor_Acme": {"links": ["SavedBot", "Concept_Bot", "Vendor_Acme", "Missing", None],
                            "triples": [{"target": "Concept_Bot", "predicate": "part-of"},
                                        {"target": "Concept_Bot", "predicate": "part-of"},
                                        {"target": {}, "predicate": "bad"}, None]},
            "Product_Bot": {"title": "Bot"},
            "Concept_C": {}, "Concept_D": {},
        },
        "aliases": {"SavedBot": "Product_Bot"},
        "weighted_edges": [{"source": "Concept_C", "target": "Concept_D", "weight": 8}],
    }
    graph = tool_graph._build_page_graph(index_data, max_edges=1)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert len(graph["edges"]) == 1
    assert nodes["Vendor_Acme"]["declared_links"] == ["Product_Bot"]
    assert nodes["Vendor_Acme"]["declared_relations"] == [{"target": "Product_Bot", "predicate": "part-of"}]
    assert nodes["Vendor_Acme"]["degree"] == nodes["Vendor_Acme"]["backbone_degree"] == 0
    assert not nodes["Vendor_Acme"]["is_orphan"]
    assert not nodes["Product_Bot"]["is_orphan"]
    assert "Product_Bot" in nodes["Vendor_Acme"]["semantic_links"]


def test_declared_relation_resolution_refuses_ambiguous_core_names(catalog):
    catalog.return_value["relations"] = [
        _declaration("Vendor_Acme", "Product_Bot", "wikilink", "navigation"),
    ]
    graph = tool_graph._build_page_graph({"nodes": {
        "Vendor_Acme": {"links": ["Bot", "Product_Bot"]},
        "Product_Bot": {"title": "Bot"}, "Concept_Bot": {"title": "Bot"},
    }, "aliases": {"Product_Bot": "Concept_Bot"}, "weighted_edges": []})
    source = next(node for node in graph["nodes"] if node["id"] == "Vendor_Acme")
    assert source["declared_links"] == ["Product_Bot"]
    assert source["declared_relations"] == []


def test_page_states_remain_separate_and_seed_does_not_imply_unreleased():
    graph = tool_graph._build_page_graph({"nodes": {
        "Product_Seed": {"status": "seed", "epistemic_status": "unverified"},
        "Product_Released": {"status": "active", "metadata": {
            "epistemic_status": "supported", "lifecycle_status": "released"}},
        "Product_Absent": {},
        "Product_Legacy": {"metadata": {"epistemic-status": "seed"}},
    }})
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["Product_Seed"]["status"] == "seed"
    assert nodes["Product_Seed"]["epistemic_status"] == "unverified"
    assert nodes["Product_Seed"]["lifecycle_status"] is None
    assert nodes["Product_Released"]["status"] == "active"
    assert nodes["Product_Released"]["epistemic_status"] == "supported"
    assert nodes["Product_Released"]["lifecycle_status"] == "released"
    assert nodes["Product_Absent"]["epistemic_status"] is None
    assert nodes["Product_Absent"]["lifecycle_status"] is None
    assert nodes["Product_Legacy"]["epistemic_status"] == "seed"
    assert nodes["Product_Legacy"]["lifecycle_status"] is None


def test_catalog_context_passes_through_without_rewriting_or_promoting_evidence(catalog):
    relation = _declaration("Product_A", "Concept_B", "part-of") | {
        "subject_scope": "document", "resolution_status": "resolved",
        "context": {"claim_ids": ["synthetic-claim"], "note": "<img onerror=attack()>"},
        "temporal": {"valid_from": "2026-01-01", "valid_until": None},
        "provenance_status": "unverified", "epistemic_status": "unknown",
        "lifecycle_status": "unknown",
    }
    catalog.return_value["relations"] = [relation]
    graph = tool_graph._build_page_graph({"nodes": {"Product_A": {}, "Concept_B": {}},
                                          "weighted_edges": []}, max_edges=0)
    assert graph["relations"] == [relation]
    assert graph["relations"][0]["weight"] is None
    assert graph["relations"][0]["verified"] is False
    assert graph["edges"] == []


def test_claim_validity_record_and_epistemic_states_are_not_substituted():
    graph = tool_graph._build_claim_graph({"nodes": [{"id": "synthetic-claim",
        "validity_state": "active", "status": "seed", "epistemic_status": "unverified"}]})
    claim = graph["nodes"][0]
    assert (claim["validity_state"], claim["status"], claim["epistemic_status"]) == ("active", "seed", "unverified")
    assert "lifecycle_status" not in claim


def test_build_graph_payload_meta_auditing():
    index_data = {
        "nodes": {f"Page_{i}": {"title": f"P{i}", "categories": ["Page"]} for i in range(10)},
        "communities": {},
        "weighted_edges": [
            {"source": f"Page_{i}", "target": f"Page_{i+1}", "weight": float(i + 1)}
            for i in range(9)
        ],
        "governance_metrics": {"validity_state_counts": {"active": 5}},
    }
    claim_data = {
        "nodes": [{"id": "claim_1", "name": "C1"}],
        "edges": [],
    }

    payload = tool_graph._build_graph_payload(
        index_data=index_data,
        claim_graph_data=claim_data,
        max_page_edges=5,  # limit to 5 while 9 exist
    )

    assert "meta" in payload
    meta = payload["meta"]
    assert meta["total_page_nodes"] == 10
    assert meta["total_page_edges"] == 9
    assert meta["rendered_page_edges"] == 5
    assert meta["pruned"] is True
    assert len(payload["pageGraph"]["edges"]) == 5


def test_visualize_vector_lake_respects_headless_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_NO_BROWSER", "1")
    opened = []
    monkeypatch.setattr(tool_graph.webbrowser, "open", lambda url: opened.append(url))

    index_file = tmp_path / "index.json"
    index_file.write_text('{"nodes": {}, "weighted_edges": []}', encoding="utf-8")
    monkeypatch.setattr(tool_graph, "get_index_path", lambda: index_file)
    monkeypatch.setattr(tool_graph, "get_claim_graph_path", lambda: tmp_path / "claim_graph.json")

    # Call with open_browser=False
    res = tool_graph.visualize_vector_lake(output_dir=str(tmp_path), open_browser=False)
    assert "Opened graph in browser" in res or "Visualized" in res
    assert len(opened) == 0

    # Call with open_browser=True, but environment variable VECTOR_LAKE_NO_BROWSER=1 is set
    res2 = tool_graph.visualize_vector_lake(output_dir=str(tmp_path), open_browser=True)
    assert len(opened) == 0

