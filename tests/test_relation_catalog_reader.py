"""Synthetic contracts for complete declarations, scoped reads and pre-walk isolation."""
import copy
import json
import sqlite3
from typing import Any

import pytest

from vector_lake import db_store
from vector_lake import page_index_projection as pip
from vector_lake import tool_search
from vector_lake.relation_catalog import RelationInputError, build_relation_catalog
from vector_lake.tool_relations import get_relations


def node(**kwargs):
    return {"title": "", "aliases": [], "domain": "General", "topic_cluster": "C",
            "status": "Active", "links": [], "triples": [], **kwargs}


def fixture_nodes():
    return {
        "Concept_A": node(title="Alpha", triples=[{"predicate": "has_part", "target": "Beta"},
                                                 {"predicate": "related_to", "target": "Concept_B"}],
                          links=["Concept_B"]),
        "Concept_B": node(title="Beta", triples=[{"predicate": "part-of", "target": "Concept_A"}]),
        "Concept_C": node(),
        "Concept_Secret": node(domain="Private", title="secret title"),
    }


def test_complete_directed_layers_are_pure_and_independent_of_pruning():
    nodes = fixture_nodes()
    before = copy.deepcopy(nodes)
    result = build_relation_catalog(nodes, [])
    assert nodes == before
    rows = result["relations"]
    assert {(r["source"], r["target"], r["predicate"], r["layer"]) for r in rows} == {
        ("Concept_A", "Concept_B", "has_part", "declared"),
        ("Concept_A", "Concept_B", "related_to", "declared"),
        ("Concept_B", "Concept_A", "part-of", "declared"),
        ("Concept_A", "Concept_B", "wikilink", "navigation"),
    }
    assert all(r["verified"] is False and r["weight"] is None for r in rows)
    assert all(r["origin"]["page_key"] == r["source"] for r in rows)
    nodes["Concept_A"]["triples"] *= 3
    assert build_relation_catalog(nodes)["relations"] == rows


def test_scope_is_both_endpoints_exact_and_diagnostics_redact_targets():
    nodes = fixture_nodes()
    nodes["Concept_A"]["links"] += ["Concept_Secret", "missing-private-name"]
    nodes["Concept_A"]["triples"] += [{"predicate": "knows", "target": "secret title"}]
    nodes["Concept_Secret"]["links"] = ["Concept_A"]
    nodes["Concept_B"]["topic_cluster"] = "other"
    result = build_relation_catalog(nodes, domain="General", cluster="C")
    assert result["relations"] == []
    encoded = json.dumps(result)
    assert all(s not in encoded for s in ("Concept_Secret", "secret title", "missing-private-name", "Concept_B"))
    assert result["diagnostics"][0]["code"] == "unresolved_target"
    assert build_relation_catalog(nodes, domain="general")["relations"] == []


def test_ambiguity_and_exact_filename_precedence_without_id_resolution():
    nodes = fixture_nodes()
    nodes["Concept_B"].update(aliases=["Shared"], id="opaque-id")
    nodes["Concept_C"].update(aliases=["Shared"])
    nodes["Concept_A"]["triples"] = [{"predicate": "p", "target": target}
                                            for target in ("Shared", "opaque-id", "Concept_B")]
    result = build_relation_catalog(nodes)
    assert [r["target"] for r in result["relations"] if r["layer"] == "declared"] == ["Concept_B", "Concept_A"]
    assert {d["code"] for d in result["diagnostics"]} == {"ambiguous_target", "unresolved_target"}


def test_unique_title_precedes_ambiguous_core_and_filename_precedes_alias():
    nodes = {"Concept_A": node(links=["Product_B", "Exact Title"]),
             "Product_B": node(), "Vendor_B": node(),
             "Concept_C": node(title="Exact Title", aliases=["Product_B"]),
             "Concept_D": node(title="Exact-Title")}
    result = build_relation_catalog(nodes)
    assert {(r["source"], r["target"]) for r in result["relations"]} == {
        ("Concept_A", "Product_B"), ("Concept_A", "Concept_C")}
    assert result["diagnostics"] == []


def test_metadata_entity_shape_history_relatedness_and_limits():
    nodes = {"A": {"domain": "General", "topic_cluster": "C", "status": "Active",
                   "metadata": {"triples": [{"predicate": "p", "target": "B"}], "links": ["B"]}},
             "B": node(status="Archived")}
    assert build_relation_catalog(nodes)["relations"] == []
    result = build_relation_catalog(nodes, [{"source": "A", "target": "B", "weight": 2}], include_history=True)
    assert {r["layer"] for r in result["relations"]} == {"declared", "navigation", "relatedness"}
    assert next(r for r in result["relations"] if r["layer"] == "relatedness")["weight"] == 2
    assert build_relation_catalog(nodes, include_history=True, max_relations=1)["truncated"]
    assert build_relation_catalog(nodes, include_history=True, max_relations=0)["relations"] == []
    assert build_relation_catalog({}) == {"relations": [], "diagnostics": [], "scope": {"domain": "", "cluster": "", "include_history": False}, "truncated": False}
    many = {"A": node(links=[f"missing-{i}" for i in range(300)])}
    capped = build_relation_catalog(many)
    assert len(capped["diagnostics"]) <= 200 and capped["truncated"]


@pytest.mark.parametrize("kwargs", [{"nodes": []}, {"nodes": {"A": None}}, {"max_relations": -1},
    {"max_relations": True}, {"max_relations": 50001}, {"domain": []}, {"cluster": 1},
    {"include_history": "yes"}, {"domain": None}, {"weighted_edges": {}},
    {"weighted_edges": [{"source": "A", "target": "B", "weight": float("nan")}]},
    {"weighted_edges": [{"source": "A", "target": "B", "weight": 10**1000}]},
    {"nodes": {"A": node(triples={})}}, {"nodes": {"A": node(links=[1])}},
    {"nodes": {"A": node(triples=[{"target": "B"}])}}])
def test_invalid_shapes_raise_classified_input_errors(kwargs):
    args = {"nodes": fixture_nodes(), **kwargs}
    with pytest.raises(RelationInputError) as error:
        build_relation_catalog(**args)
    assert error.value.code == "invalid_input"


@pytest.fixture
def projected(isolated_memory):
    db_store.init_db()
    nodes = fixture_nodes()
    nodes["Concept_B"]["links"] = ["Concept_C"]
    index = {"nodes": nodes, "weighted_edges": [{"source": "Concept_A", "target": "Concept_C", "weight": 7}]}
    path = isolated_memory / "wiki" / "index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    pip.refresh_page_index_projection(index)
    return nodes


def test_actual_db_read_is_readonly_scoped_complete_and_depth_bounded(projected, monkeypatch):
    path = db_store.get_db_path()
    before = path.read_bytes()
    statements = []
    connect = sqlite3.connect

    def observe(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs.get("uri")
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr("vector_lake.tool_relations.sqlite3.connect", observe)
    one = get_relations("Alpha", "General", cluster="C")
    assert one["status"] == "ok" and len(one["relations"]) == 4
    assert all("Concept_C" not in (r["source"], r["target"]) for r in one["relations"])
    two = get_relations("Alpha", "General", depth=2, include_relatedness=True)
    assert len(two["relations"]) == 6
    assert not one["truncated"]
    assert get_relations("Alpha", "General", limit=1)["truncated"]
    assert get_relations("Alpha", "General", limit=0)["truncated"]
    assert get_relations("Alpha", "General", depth=0)["relations"] == []
    assert get_relations("Concept_Secret", "General")["status"] == "not_found"
    assert get_relations("Alpha", "general")["status"] == "not_found"
    assert get_relations("Alpha", "General", cluster="missing")["status"] == "not_found"
    assert path.read_bytes() == before
    payload_queries = [q for q in statements if "SELECT node_key, node_json" in q]
    assert payload_queries and all("WHERE" in q and "domain = 'General'" in q for q in payload_queries)
    assert all("entities" not in q and "raw" not in q for q in statements)
    assert "secret" not in json.dumps(one).lower()


def test_alias_strings_that_look_like_json_are_not_misparsed(projected):
    projected["Concept_B"]["aliases"] = "[Alias"
    pip.refresh_page_index_projection({"nodes": projected, "weighted_edges": []})
    result = get_relations("[Alias", "General")
    assert result["status"] == "ok" and result["entity"] == "Concept_B"


def test_db_ambiguity_cross_scope_and_no_hidden_target_in_diagnostics(projected):
    conn = db_store.get_connection()
    nodes = copy.deepcopy(projected)
    nodes["Concept_A"]["links"] = ["Shared"]
    nodes["Concept_A"]["aliases"] = ["Shared"]
    nodes["Concept_Secret"]["aliases"] = ["Shared"]
    pip.refresh_page_index_projection({"nodes": nodes, "weighted_edges": []})
    assert get_relations("Shared", "General")["status"] == "ambiguous"
    result = get_relations("Concept_A", "General")
    assert "ambiguous_target" in {d["code"] for d in result["diagnostics"]}
    assert "Concept_Secret" not in json.dumps(result)
    conn.execute("UPDATE page_index_nodes SET node_json='not-json' WHERE node_key='Concept_Secret'")
    conn.commit()
    # Hidden malformed relation payloads must not be materialized; identity JSON corruption is a read error.
    assert get_relations("Concept_A", "General")["status"] == "error"


def test_stale_projection_access_error_and_valid_empty_are_not_conflated(projected, isolated_memory, monkeypatch):
    empty = get_relations("Concept_C", "General", depth=0)
    assert empty["status"] == "ok" and empty["relations"] == []
    index = isolated_memory / "wiki" / "index.json"
    index.write_text(index.read_text(encoding="utf-8") + " ", encoding="utf-8")
    stale = get_relations("Alpha", "General")
    assert stale["status"] == "unavailable" and stale["error"]["code"] == "projection_stale"

    def denied(*args, **kwargs):
        raise PermissionError("synthetic")

    monkeypatch.setattr("vector_lake.tool_relations.sqlite3.connect", denied)
    denied_result = get_relations("Alpha", "General")
    assert denied_result["status"] == "error" and denied_result["error"]["type"] == "PermissionError"


def test_missing_corrupt_and_empty_db_are_distinct(isolated_memory):
    assert get_relations("A", "General")["status"] == "unavailable"
    assert not db_store.get_db_path().exists()
    db_store.init_db()
    assert get_relations("A", "General")["status"] == "not_found"
    db_store.close_connection()
    db_store.get_db_path().write_bytes(b"not sqlite")
    result = get_relations("A", "General")
    assert result["status"] == "error" and result["error"]["code"] == "read_error"


@pytest.mark.parametrize("kwargs", [{"domain": ""}, {"entity_name": ""}, {"domain": None},
    {"depth": -1}, {"depth": 6}, {"depth": True}, {"limit": -1}, {"limit": 2001},
    {"include_relatedness": 1}])
def test_public_input_errors_are_not_empty_results(kwargs):
    with pytest.raises(RelationInputError):
        get_relations(**{"entity_name": "A", "domain": "General", **kwargs})


@pytest.mark.parametrize("engine", ["python", pytest.param("native", marks=pytest.mark.requires_native_core)])
@pytest.mark.parametrize("selector", ["domain", "cluster"])
@pytest.mark.parametrize("source", ["file", "projection", "legacy"])
def test_ppr_hidden_bridge_blocked_before_propagation(isolated_memory, monkeypatch, engine, selector, source):
    db_store.init_db()
    db_store.upsert_search_index("seed", "seed", "", "")
    monkeypatch.setenv("VECTOR_LAKE_FTS", "fts5")
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([], "synthetic"))
    monkeypatch.setattr(tool_search, "_vector_projection_error", lambda: None)
    monkeypatch.setattr(tool_search.search_ledger, "record", lambda **kw: None)
    monkeypatch.setattr(tool_search, "HAVE_CORE", engine == "native")
    if engine == "native":
        assert tool_search.vector_lake_core is not None
    nodes = {key: node(title=key) for key in ("seed", "hidden", "reachable", "allowed")}
    nodes["hidden"].update(domain="Private" if selector == "domain" else "General",
                           topic_cluster="other")
    edges = [{"source": "seed", "target": "hidden", "weight": 100},
             {"source": "hidden", "target": "reachable", "weight": 100},
             {"source": "seed", "target": "allowed", "weight": 1}]
    index = {"nodes": nodes, "weighted_edges": edges}
    file_catalog = pip._FileCatalog(index)
    if source == "projection":
        (isolated_memory / "wiki" / "index.json").write_text(json.dumps(index), encoding="utf-8")
        pip.refresh_page_index_projection(index)
        catalog = pip._SqliteCatalog()
    elif source == "legacy":
        class LegacyCatalog:
            nodes_by_key = file_catalog.nodes_by_key
            filter_fields = file_catalog.filter_fields
            adjacency = file_catalog.adjacency
        catalog = LegacyCatalog()
    else:
        catalog = file_catalog
    kwargs: dict[str, Any] = {selector: "General" if selector == "domain" else "C"}
    rows, notes, error = tool_search._search_scored_pages("seed", 10, include_history=True, catalog=catalog, **kwargs)
    assert error is None
    assert not any("PPR" in n for n in notes)
    assert {n["_key"] for _, n in rows} == {"seed", "allowed"}
    # The unscoped legacy walk still reaches the bridge and its second hop.
    rows, _, error = tool_search._search_scored_pages("seed", 10, include_history=True, catalog=catalog)
    assert error is None
    assert "reachable" in {n["_key"] for _, n in rows}


def test_sqlite_scoped_adjacency_filters_before_edges_or_payloads(projected):
    conn = db_store.get_connection()
    conn.execute("INSERT INTO page_index_edges VALUES (5, 'Concept_A', 'Concept_Secret', 10)")
    conn.commit()
    adj = pip._SqliteCatalog().scoped_adjacency(domain="General", cluster="C")
    assert "Concept_Secret" not in adj
    assert adj == {"Concept_A": (("Concept_C", 7.0),), "Concept_C": (("Concept_A", 7.0),)}
    assert "Concept_Secret" in pip.adjacency()
