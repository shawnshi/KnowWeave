"""Retrieval regressions VL-S01/S02/S03, using only task-local synthetic pages."""
import json

import pytest

from vector_lake import db_store, page_index_projection, tantivy_index, tokenizer, tool_search
from vector_lake.wiki_utils import get_index_path


@pytest.fixture(autouse=True)
def search_defaults(monkeypatch):
    for name in (
        "VECTOR_LAKE_FUSION", "VECTOR_LAKE_EXPANSION_QUOTA", "VECTOR_LAKE_FTS",
        "VECTOR_LAKE_CANDIDATE_DEPTH", "VECTOR_LAKE_CANDIDATE_POOL",
        "VECTOR_LAKE_ENTITY_NAME_PRIORITY", "VECTOR_LAKE_AUTHOR_FACET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda query: ([], "test: no embedding"))
    monkeypatch.setattr(tool_search, "_vector_projection_error", lambda: None)
    monkeypatch.setattr(tool_search, "_PPR_INDEX", {"generation": None, "index": None})
    tool_search._LAST_FTS_ERROR.msg = None
    page_index_projection.reset_catalog_cache()
    tantivy_index.reset()
    yield
    page_index_projection.reset_catalog_cache()
    tantivy_index.reset()


def _node(title, summary=""):
    return {"title": title, "summary": summary, "type": "concept", "status": "Active", "domain": "AI_Engineering"}


def _seed_fts(nodes):
    db_store.init_db()
    for key, node in nodes.items():
        db_store.upsert_search_index(
            key, tokenizer.tokenize_joined(node["title"]),
            tokenizer.tokenize_joined(node["summary"]), "",
        )


def _keys(query, nodes, edges=(), **kwargs):
    rows, _notes, error = tool_search._search_scored_pages(
        query, top_k=20, include_history=True,
        catalog=page_index_projection._FileCatalog({"nodes": nodes, "weighted_edges": edges}),
        **kwargs,
    )
    assert error is None
    return [node["_key"] for _score, node in rows]


@pytest.mark.parametrize("backend", ["fts5", "tantivy"])
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("caught_up", [False, True])
def test_expansion_preserves_original_and_contextual_alternative(isolated_memory, monkeypatch, backend, filtered, caught_up):
    if backend == "tantivy":
        pytest.importorskip("tantivy")
    monkeypatch.setenv("VECTOR_LAKE_FTS", backend)
    nodes = {
        "Concept_Original": _node("Original", "大模型 部署"),
        "Concept_Alias": _node("Alias", "LLM 部署"),
        "Concept_NoQualifier": _node("Unrelated", "LLM tourism"),
    }
    nodes["Concept_OtherDomain"] = {**_node("Other", "LLM 部署"), "domain": "General"}
    _seed_fts(nodes)
    if backend == "tantivy" and caught_up:
        from tests.test_tantivy_index import _drain
        _drain()
    keys = _keys("大模型 部署", nodes, domain="AI_Engineering" if filtered else None)
    assert "Concept_Original" in keys
    assert "Concept_Alias" in keys
    assert "Concept_NoQualifier" not in keys
    if filtered:
        assert "Concept_OtherDomain" not in keys


@pytest.mark.parametrize("mode", ["dirty", "ready", "disabled", "mixed"])
def test_ledger_records_actual_lexical_paths(isolated_memory, monkeypatch, mode):
    from tests.test_tantivy_index import _drain

    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy" if mode != "disabled" else "fts5")
    monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {"alpha": ["beta"]})
    nodes = {"a": _node("alpha context"), "b": _node("beta context")}
    _seed_fts(nodes)
    if mode in {"ready", "mixed"}:
        _drain()
    recorded = []
    monkeypatch.setattr(tool_search.search_ledger, "record", lambda *args, **kw: recorded.append(kw))
    original = tool_search._get_fts_search_results
    calls = []

    def transition(query, limit=50):
        rows = original(query, limit)
        calls.append(query)
        if mode == "mixed" and len(calls) == 1:
            db_store.upsert_search_index("b", "beta context", "", "")
        return rows

    monkeypatch.setattr(tool_search, "_get_fts_search_results", transition)
    assert set(_keys("alpha context", nodes)) == {"a", "b"}
    assert recorded[-1]["fts_backend"] == {
        "dirty": "fts5", "ready": "tantivy", "disabled": "fts5", "mixed": "fts5+tantivy",
    }[mode]


def test_original_recall_keeps_its_slots_when_full(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_CANDIDATE_DEPTH", "2")
    nodes = {
        "Concept_Original1": _node("First", "大模型 部署"),
        "Concept_Original2": _node("Second", "大模型 部署"),
        "Concept_Alias": _node("Alias", "LLM 部署"),
    }
    _seed_fts(nodes)
    assert set(_keys("大模型 部署", nodes)) == {"Concept_Original1", "Concept_Original2"}


def test_expansion_deduplicates_and_demotes_supplemental_hits(monkeypatch):
    monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {"alpha": ["beta", "gamma"]})
    calls = []

    def fetch(query, limit=50):
        calls.append(query)
        tool_search._LAST_FTS_ERROR.msg = None
        rows = {
            "alpha context": [{"node_key": "original", "rank": -2.0}],
            "beta context": [{"node_key": "alias", "rank": -4.0}],
            "gamma context": [{"node_key": "alias", "rank": -1.0}],
        }
        return rows.get(query, [])

    monkeypatch.setattr(tool_search, "_get_fts_search_results", fetch)
    rows = tool_search._get_fts_recall_results("alpha context", limit=5)
    assert [row["node_key"] for row in rows] == ["original", "alias"]
    assert rows[0]["rank"] == -2.0
    assert -4.0 < rows[1]["rank"] < 0.0
    assert all("context" in query for query in calls)


def test_failed_supplement_preserves_original_rows_and_reports_error(monkeypatch):
    monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {"alpha": ["beta"]})

    def fetch(query, limit=50):
        tool_search._LAST_FTS_ERROR.msg = "test: supplement failed" if query.startswith("beta") else None
        return [] if query.startswith("beta") else [{"node_key": "original", "rank": -2.0}]

    monkeypatch.setattr(tool_search, "_get_fts_search_results", fetch)
    assert tool_search._get_fts_recall_results("alpha context", limit=5) == [
        {"node_key": "original", "rank": -2.0}
    ]
    assert tool_search._LAST_FTS_ERROR.msg == "test: supplement failed"


def test_expansion_is_bounded_to_eight_substitutions(monkeypatch):
    monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {"alpha": [f"beta{i}" for i in range(12)]})
    calls = []

    def fetch(query, limit=50):
        calls.append(query)
        tool_search._LAST_FTS_ERROR.msg = None
        return []

    monkeypatch.setattr(tool_search, "_get_fts_search_results", fetch)
    assert tool_search._get_fts_recall_results("alpha context", limit=5) == []
    assert len(calls) == 9  # original plus eight bounded substitutions
    assert tool_search._LAST_FTS_ERROR.msg is None


def test_partial_fts_failure_does_not_replace_good_hits_with_file_fallback(isolated_memory, monkeypatch):
    monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {"alpha": ["beta"]})

    def fetch(query, limit=50):
        tool_search._LAST_FTS_ERROR.msg = "test: supplement failed" if query.startswith("beta") else None
        return [] if query.startswith("beta") else [{"node_key": "original", "rank": -2.0}]

    monkeypatch.setattr(tool_search, "_get_fts_search_results", fetch)
    nodes = {"original": _node("Unrelated title", "alpha context")}
    rows, notes, error = tool_search._search_scored_pages(
        "alpha context", 5, include_history=True,
        catalog=page_index_projection._FileCatalog({"nodes": nodes}),
    )
    assert error is None and [node["_key"] for _score, node in rows] == ["original"]
    assert "test: supplement failed" in notes


@pytest.mark.parametrize("fusion", ["sum", "rrf"])
@pytest.mark.parametrize("engine", ["prepared", "legacy", "python"])
def test_disconnected_zero_mass_pages_never_enter_pool(isolated_memory, monkeypatch, fusion, engine):
    if engine != 'python' and not tool_search.HAVE_CORE:
        pytest.skip('native-only PPR engine; Python engine remains mandatory in fallback lane')
    monkeypatch.setenv("VECTOR_LAKE_FUSION", fusion)
    monkeypatch.setenv("VECTOR_LAKE_EXPANSION_QUOTA", "5")
    if engine == "python":
        monkeypatch.setattr(tool_search, "HAVE_CORE", False)
    elif engine == "legacy":
        monkeypatch.delattr(tool_search.vector_lake_core, "prepared_personalized_pagerank")
    nodes = {key: _node(key) for key in ["seed", "reachable", "disconnected_x", "disconnected_y"]}
    edges = [
        {"source": "seed", "target": "reachable", "weight": 1.0},
        {"source": "disconnected_x", "target": "disconnected_y", "weight": 1.0},
    ]
    _seed_fts({"seed": nodes["seed"]})
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, rows: rows)
    assert set(_keys("seed", nodes, edges)) == {"seed", "reachable"}


def test_changed_adjacency_rebuilds_cache_even_with_same_generation(monkeypatch):
    if not tool_search.HAVE_CORE:
        pytest.skip('native-only prepared PPR cache; native CI hard-requires this ABI')
    old = {"seed": [("old", 1.0)], "old": [("seed", 1.0)]}
    new = {"seed": [("new", 1.0)], "new": [("seed", 1.0)]}
    first = tool_search._ppr_index(old, 7)
    again = tool_search._ppr_index(old, 7)
    changed = tool_search._ppr_index(new, 7)
    assert first is again
    assert changed is not first
    scores = dict(tool_search.vector_lake_core.prepared_personalized_pagerank(changed, ["seed"], 0.85, 2))
    assert scores["new"] > 0
    assert "old" not in scores


@pytest.mark.parametrize("start_on_projection", [False, True])
def test_file_fallback_switch_uses_the_current_graph(isolated_memory, monkeypatch, start_on_projection):
    _seed_fts({"seed": _node("seed")})
    monkeypatch.setattr(tool_search, "_rerank_candidates_locally", lambda query, rows: rows)
    generation = None
    for step, target in enumerate(["old", "new_with_a_different_file_size"]):
        data = {
            "nodes": {"seed": _node("seed"), target: _node(target)},
            "weighted_edges": [{"source": "seed", "target": target, "weight": 1.0}],
        }
        get_index_path().write_text(json.dumps(data), encoding="utf-8")
        if step == 0 and start_on_projection:
            page_index_projection.refresh_page_index_projection(data)
        catalog, note = page_index_projection.read_catalog()
        if step == 0 and start_on_projection:
            assert catalog.source != "index.json" and note is None
        else:
            assert catalog.source == "index.json" and note
        rows, _notes, error = tool_search._search_scored_pages("seed", 10, include_history=True, catalog=catalog)
        assert error is None
        assert {node["_key"] for _score, node in rows} == {"seed", target}
        if generation is None:
            generation = page_index_projection.adjacency_generation()
        assert page_index_projection.adjacency_generation() == generation
