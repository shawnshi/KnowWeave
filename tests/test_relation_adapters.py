"""Adapter-only frozen read-model contracts; no catalog sibling or lake required."""
import asyncio
import json
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from vector_lake import governance_store, mcp_server, tool_graph, tool_trace


def relation(source, target, predicate="part-of", layer="declared"):
    return {"source": source, "target": target, "predicate": predicate,
            "layer": layer, "declared": layer == "declared", "verified": False,
            "weight": None, "origin": {"page_key": source}}


def catalog_result(relations=(), *, truncated=False):
    return {"relations": list(relations), "diagnostics": [{"code": "unresolved"}],
            "scope": {"domain": "", "cluster": ""}, "truncated": truncated}


@pytest.fixture
def readers(monkeypatch):
    from vector_lake.relation_catalog import node_field

    catalog = ModuleType("vector_lake.relation_catalog")
    build = Mock(return_value=catalog_result())
    vars(catalog)["build_relation_catalog"] = build
    vars(catalog)["node_field"] = node_field
    reader = ModuleType("vector_lake.tool_relations")
    get = Mock(return_value=catalog_result())
    vars(reader)["get_relations"] = get
    monkeypatch.setitem(sys.modules, catalog.__name__, catalog)
    monkeypatch.setitem(sys.modules, reader.__name__, reader)
    return build, get


def test_graph_projects_catalog_without_parsing_or_weighting_declarations(readers):
    build, _ = readers
    relations = [relation("A", "B", "has-part"), relation("B", "A", "part-of"),
                 relation("A", "B", "supports"), relation("B", "C", "wikilink", "navigation"),
                 {**relation("A", "C", "related_to", "relatedness"), "weight": 7.0}]
    build.return_value = catalog_result(relations, truncated=True)
    nodes = {"A": {"triples": [{"target": "WRONG", "predicate": "wrong"}], "updated": "2020-01-02"},
             "B": {}, "C": {}}
    graph = tool_graph._build_page_graph({"nodes": nodes, "weighted_edges": []}, max_edges=0)
    by_id = {node["id"]: node for node in graph["nodes"]}
    assert graph["relations"] == relations
    assert graph["relations_truncated"] is True
    assert graph["relation_diagnostics"] == [{"code": "unresolved"}]
    assert by_id["A"]["declared_relations"] == [
        {"target": "B", "predicate": "has-part"}, {"target": "B", "predicate": "supports"}]
    assert by_id["B"]["declared_relations"] == [{"target": "A", "predicate": "part-of"}]
    assert by_id["B"]["declared_links"] == ["A", "C"]
    assert by_id["C"]["declared_links"] == []
    assert by_id["A"]["semantic_links"] == ["B"]
    assert by_id["A"]["updated"] == "2020-01-02"
    assert all(node["backbone_degree"] == node["degree"] == 0 for node in by_id.values())
    assert all(not node["is_orphan"] for node in by_id.values())
    assert graph["edges"] == []
    build.assert_called_once_with(nodes, include_history=True)


def test_graph_passes_legacy_aliases_without_mutating_snapshot(readers):
    build, _ = readers
    nodes = {"A": {"aliases": "Existing"}, "B": {}}
    original = json.dumps(nodes, sort_keys=True)
    tool_graph._build_page_graph({"nodes": nodes, "aliases": {"Saved": "A", "Ghost": "MISSING"}})
    supplied = build.call_args.args[0]
    assert supplied["A"]["aliases"] == ["Existing", "Saved"]
    assert "Ghost" not in str(supplied)
    assert json.dumps(nodes, sort_keys=True) == original


def test_catalog_failure_is_not_legacy_empty_data(readers):
    build, _ = readers
    build.side_effect = RuntimeError("catalog unavailable")
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        tool_graph._build_page_graph({"nodes": {}})


def test_missing_catalog_is_an_error_not_a_legacy_parser(monkeypatch):
    monkeypatch.setitem(sys.modules, "vector_lake.relation_catalog", None)
    with pytest.raises(ModuleNotFoundError):
        tool_graph._build_page_graph({"nodes": {"A": {"links": ["B"]}, "B": {}}})


def test_claim_graph_does_not_consult_document_relations(readers):
    build, _ = readers
    build.side_effect = AssertionError("claims are a separate model")
    result = tool_graph._build_claim_graph({"nodes": [{"id": "claim", "updated": "2021"}], "edges": []})
    assert result["nodes"][0]["node_kind"] == "claim"
    assert result["nodes"][0]["updated"] == "2021"
    build.assert_not_called()


def test_mcp_relations_exact_domain_forwarding_and_schema(readers):
    _, get = readers
    result = {**catalog_result([relation("B", "A")]), "scope": {"domain": "General", "cluster": "C"}}
    get.return_value = result
    assert json.loads(mcp_server.get_relations("A", "General", "C", 2, 3, True)) == result
    get.assert_called_once_with(entity_name="A", domain="General", cluster="C", depth=2, limit=3,
                                include_relatedness=True)
    tools = asyncio.run(mcp_server.mcp.list_tools())
    schema = next(tool for tool in tools if tool.name == "get_relations").parameters
    assert "domain" in schema["required"]
    assert "entity_name" in schema["required"]
    assert schema["properties"]["include_relatedness"]["default"] is False


@pytest.mark.parametrize("domain", ["", "  ", None])
def test_mcp_relations_refuses_missing_domain_before_reader(readers, domain):
    _, get = readers
    with pytest.raises(ValueError, match="domain"):
        mcp_server.get_relations("A", domain)
    get.assert_not_called()


def test_mcp_relations_dependency_failure_stays_error(readers):
    _, get = readers
    get.side_effect = OSError("reader unavailable")
    with pytest.raises(OSError, match="reader unavailable"):
        mcp_server.get_relations("A", "General")


def test_mcp_relations_invalid_limits_stay_reader_errors(readers):
    _, get = readers
    get.side_effect = ValueError("limit must be nonnegative")
    with pytest.raises(ValueError, match="limit"):
        mcp_server.get_relations("A", "General", limit=-1)
    assert get.call_args.kwargs["limit"] == -1


def test_rename_scope_is_forwarded_without_changing_dry_run(monkeypatch):
    from vector_lake import tool_rename
    delegate = Mock(return_value="preview")
    monkeypatch.setattr(tool_rename, "rename_vector_lake_entity", delegate)
    assert mcp_server.rename_entity("A", "B", domain="General", cluster="C") == "preview"
    delegate.assert_called_once_with("A", "B", dry_run=True, domain="General", cluster="C")


def test_trace_scoped_relations_use_shared_reader_without_global_provenance(readers, monkeypatch):
    _, get = readers
    get.return_value = {**catalog_result([relation("B", "A", "supports")], truncated=True),
                        "scope": {"domain": "General", "cluster": "C"}}
    monkeypatch.setattr(tool_trace.provenance, "build_trace_for_query",
                        Mock(side_effect=AssertionError("scoped trace must not query global claims")))
    monkeypatch.setattr(governance_store, "ensure_canonical_store_populated",
                        Mock(side_effect=AssertionError("trace must not bootstrap storage")))
    output = tool_trace.trace_vector_lake("A", domain="General", cluster="C")
    assert output.startswith("=== Document Relations ===")
    payload = json.loads(output.split("=== Document Relations ===\n", 1)[1])
    assert payload == get.return_value
    assert payload["relations"][0]["source"] == "B"
    assert payload["relations"][0]["verified"] is False
    get.assert_called_once_with(entity_name="A", domain="General", cluster="C")


def test_trace_default_text_unchanged_and_no_bootstrap(readers, monkeypatch):
    _, get = readers
    monkeypatch.setattr(tool_trace.provenance, "build_trace_for_query", lambda value: {"items": []})
    monkeypatch.setattr(governance_store, "ensure_canonical_store_populated",
                        Mock(side_effect=AssertionError("trace must not bootstrap storage")))
    assert tool_trace.trace_vector_lake("A") == "No provenance trace found."
    get.assert_not_called()


def test_scoped_trace_reader_failure_is_not_no_data(readers, monkeypatch):
    _, get = readers
    get.side_effect = OSError("relations unavailable")
    monkeypatch.setattr(tool_trace.provenance, "build_trace_for_query", lambda value: {"items": []})
    with pytest.raises(OSError, match="relations unavailable"):
        tool_trace.trace_vector_lake("A", domain="General")


def test_trace_refuses_cluster_without_exact_domain(readers):
    with pytest.raises(ValueError, match="domain"):
        tool_trace.trace_vector_lake("A", cluster="C")


def test_mcp_trace_forwards_explicit_scope_but_keeps_legacy_call(monkeypatch):
    delegate = Mock(return_value="trace")
    monkeypatch.setattr(mcp_server.tools, "trace_vector_lake", delegate)
    assert mcp_server.trace_vector_lake("A") == "trace"
    assert mcp_server.trace_vector_lake("A", domain="General", cluster="C") == "trace"
    assert delegate.call_args_list[0].args == ("A",)
    assert delegate.call_args_list[0].kwargs == {}
    assert delegate.call_args_list[1].kwargs == {"domain": "General", "cluster": "C"}
