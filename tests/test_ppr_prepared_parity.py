"""The prepared PPR index must be a pure optimisation: identical output, dropped when the graph moves.

Two risks are pinned here.  First, exactness: the Rust index only removes allocations, so any
difference at all means the accumulation order moved -- and the tricky cases are the ones where the
legacy function's *key set* is observable (a zero-score node still inserts its neighbours).  Second,
staleness: the index is cached per graph generation, so a new graph must not reuse the old index.
"""

from __future__ import annotations

import pytest

# Native-only parity tests: the explicit fallback lane has no core by design.
# The native CI entrypoint separately requires this ABI before collection.
core = pytest.importorskip('vector_lake_core', reason='native-only prepared PPR parity')
from vector_lake import tool_search

requires_prepared = pytest.mark.skipif(
    not hasattr(core, "prepared_personalized_pagerank"),
    reason="the installed vector_lake_core predates the prepared PPR index",
)

EDGE_CASES = {
    "empty graph": ({}, ["a"]),
    "empty seeds": ({"a": [("b", 1.0)]}, []),
    "single node": ({"a": []}, ["a"]),
    "dangling target": ({"a": [("z", 1.0)]}, ["a"]),
    "unknown seed beside known": ({"a": [("b", 1.0)]}, ["a", "ghost"]),
    "unknown seed only": ({"a": [("b", 1.0)]}, ["ghost"]),
    "duplicate seeds": ({"a": [("b", 1.0)], "b": [("a", 1.0)]}, ["a", "a", "b"]),
    "zero-weight row": ({"a": [("b", 0.0)], "b": [("a", 1.0)]}, ["a"]),
    "sum non-positive row": ({"a": [("b", 1.0), ("c", -1.0)], "c": [("a", 1.0)]}, ["a"]),
    "non-ascii ordering": (
        {"概念_甲": [("概念_乙", 1.0)], "概念_乙": [], "Concept_a": [("概念_甲", 2.0)]},
        ["概念_甲"],
    ),
    "score tie": ({"a": [("x", 1.0)], "b": [("x", 1.0)], "x": []}, ["a", "b"]),
    "self loop": ({"a": [("a", 0.5), ("b", 0.5)], "b": [("a", 1.0)]}, ["a"]),
}


@requires_prepared
@pytest.mark.parametrize("steps", [0, 2])
@pytest.mark.parametrize("name", sorted(EDGE_CASES))
def test_prepared_index_is_bit_identical(name, steps):
    adj, seeds = EDGE_CASES[name]
    legacy = core.fast_personalized_pagerank(adj, list(seeds), 0.85, steps)
    index = core.PprIndex(adj)
    assert core.prepared_personalized_pagerank(index, list(seeds), 0.85, steps) == legacy


@requires_prepared
@pytest.mark.parametrize(
    "seeds, expected",
    [
        (["ghost"], [("ghost", 1.0)]),
        (["a", "ghost"], [("a", 0.5), ("ghost", 0.5)]),
        (["ghost", "ghost"], [("ghost", 1.0)]),
    ],
)
def test_zero_step_retains_unknown_and_deduplicated_seeds(seeds, expected):
    adj = {"a": []}
    assert core.fast_personalized_pagerank(adj, seeds, 0.85, 0) == expected
    assert core.prepared_personalized_pagerank(core.PprIndex(adj), seeds, 0.85, 0) == expected


@requires_prepared
@pytest.mark.parametrize("steps", [0, 1, 2, 5])
@pytest.mark.parametrize("alpha", [0.0, 0.5, 0.85, 1.0])
def test_prepared_index_matches_across_parameters(alpha, steps):
    adj = {"a": [("b", 1.0)], "b": [("c", 2.0)], "c": [("a", 1.0), ("x", 3.0)]}
    legacy = core.fast_personalized_pagerank(adj, ["a", "c"], alpha, steps)
    assert core.prepared_personalized_pagerank(core.PprIndex(adj), ["a", "c"], alpha, steps) == legacy


@requires_prepared
def test_index_reports_the_same_graph_it_was_built_from():
    adj = {"a": [("b", 1.0), ("b", 2.0)], "b": [("a", 1.0)]}
    index = core.PprIndex(adj)
    assert len(index) == 2
    assert index.edge_count() == 3


def test_ppr_index_is_dropped_when_the_graph_generation_changes(monkeypatch):
    """A cached index keyed on the wrong thing would answer from a graph that no longer exists."""
    if not hasattr(core, "PprIndex"):
        pytest.skip("the installed vector_lake_core predates the prepared PPR index")
    calls = []

    class FakeIndex:
        def __init__(self, adj):
            calls.append(adj)

    monkeypatch.setattr(tool_search.vector_lake_core, "PprIndex", FakeIndex)
    monkeypatch.setattr(tool_search, "_PPR_INDEX", {"generation": None, "index": None})

    adj = {"a": [("b", 1.0)]}
    first = tool_search._ppr_index(adj, 1)
    again = tool_search._ppr_index(adj, 1)
    assert first is again and len(calls) == 1

    tool_search._ppr_index(adj, 2)
    assert len(calls) == 2, "a new graph generation must rebuild the index"
