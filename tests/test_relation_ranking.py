"""KWR-01: relatedness is strongest pair evidence, never a count of assertions."""
import copy
import itertools

import pytest

from vector_lake import indexer


@pytest.fixture(params=[False, pytest.param(True, marks=pytest.mark.requires_native_core)], ids=["python", "native"])
def backend(request, monkeypatch):
    if request.param:
        assert indexer.vector_lake_core is not None, "existing native package required"
        assert hasattr(indexer.vector_lake_core, "fast_calculate_weighted_edges")
    monkeypatch.setattr(indexer, "HAVE_CORE", request.param)


def _nodes(predicates=("has_part",), *, reverse=(), link_a=True, link_b=False):
    return {
        "Concept_A": {"type": "concept", "links": ["Concept_B"] if link_a else [],
                      "triples": [{"predicate": p, "target": "Concept_B"} for p in predicates]},
        "Concept_B": {"type": "concept", "links": ["Concept_A"] if link_b else [],
                      "triples": [{"predicate": p, "target": "Concept_A"} for p in reverse]},
    }


def _edges(nodes):
    before = copy.deepcopy(nodes)
    edges = indexer._calculate_weighted_edges({"nodes": nodes})
    assert nodes == before, "scoring must not collapse canonical predicates or invent links"
    return sorted(edges, key=lambda e: (e["source"], e["target"]))


def _expected(weight):
    return [{"source": "Concept_A", "target": "Concept_B", "weight": weight}]


def test_canonical_predicate_weights():
    assert indexer.get_pred_weight("part-of") == indexer.get_pred_weight("has_part") == 9.0
    assert indexer.get_pred_weight("competes-with") >= indexer.get_pred_weight("related_to") == 4.5
    assert indexer.get_pred_weight("unknown_predicate") == 3.0


def test_permutations_use_max_without_erasing_distinct_predicates(backend):
    for predicates in itertools.permutations(["has_part", "related_to", "mentions"]):
        nodes = _nodes(predicates)
        assert _edges(nodes) == _expected(9.8)
        assert [t["predicate"] for t in nodes["Concept_A"]["triples"]] == list(predicates)


@pytest.mark.parametrize("forward,reverse,link_a,link_b", [
    (["has_part"] * 5, [], True, False),
    (["has_part"], ["part-of"], True, True),
    ([], ["part-of"], False, True),
    (["part-of"], [], True, False),
    (["has_part", "related_to"], ["part-of", "competes-with"], True, True),
    # Declaration may reside opposite the only navigation link.
    (["has_part"], [], False, True),
])
def test_repeated_inverse_and_single_or_bidirectional_pairs(backend, forward, reverse, link_a, link_b):
    assert _edges(_nodes(forward, reverse=reverse, link_a=link_a, link_b=link_b)) == _expected(9.8)


@pytest.mark.parametrize("predicates,reverse", [([], []), (["mentions"], []), (["mentions"] * 4, ["mentions"])])
def test_navigation_and_mentions_do_not_double_count(backend, predicates, reverse):
    assert _edges(_nodes(predicates, reverse=reverse, link_b=True)) == _expected(2.0)


@pytest.mark.parametrize("predicate", ["competes-with", "related_to"])
def test_symmetric_relations_count_once(backend, predicate):
    assert _edges(_nodes([predicate], reverse=[predicate], link_b=True)) == _expected(5.3)


@pytest.mark.parametrize("overlap,weight", [(4.0, 13.8), (0.0, 9.8)])
def test_triples_without_links_score_existing_source_candidate_only(backend, monkeypatch, overlap, weight):
    monkeypatch.setitem(indexer.RELEVANCE_WEIGHTS, "source_overlap", overlap)
    nodes = _nodes(["has_part", "mentions"], reverse=["part-of"], link_a=False)
    for node in nodes.values():
        node["sources"] = ["synthetic-source"]
    assert _edges(nodes) == _expected(weight)


@pytest.mark.parametrize("neighbor,weight", [("Missing", 9.8), ("Concept_Hub", 11.964)])
def test_triples_score_existing_common_neighbor_candidate(backend, neighbor, weight):
    nodes = _nodes(["has_part", "mentions"], reverse=["part-of"], link_a=False)
    for node in nodes.values():
        node["links"] = [neighbor]
    expected = _expected(weight)
    if neighbor == "Concept_Hub":
        nodes[neighbor] = {"type": "concept", "links": ["Outside_1", "Outside_2"]}
        expected += [{"source": key, "target": neighbor, "weight": 2.0} for key in ("Concept_A", "Concept_B")]
    assert _edges(nodes) == sorted(expected, key=lambda e: (e["source"], e["target"]))


def test_triples_alone_do_not_create_candidates(backend):
    assert _edges(_nodes(["has_part"], reverse=["part-of"], link_a=False)) == []


def test_unresolved_or_ambiguous_declarations_do_not_guess_endpoint(backend):
    nodes = _nodes([], link_a=False)
    nodes["Concept_A"]["triples"] = [
        {"predicate": "has_part", "target": "Shared"},
        {"predicate": "has_part", "target": "Missing"},
    ]
    nodes["Concept_B"]["title"] = "Shared"
    nodes["Concept_C"] = {"type": "concept", "title": "Shared", "links": []}
    for node in nodes.values():
        node["sources"] = ["synthetic-source"]
    assert {edge["weight"] for edge in _edges(nodes)} == {4.8}


def test_alias_targets_and_duplicates_keep_the_max(backend):
    nodes = _nodes(["has_part", "mentions"], link_b=True)
    nodes["Concept_B"]["aliases"] = ["Alias-B"]
    nodes["Concept_A"]["triples"][0]["target"] = "Alias-B"
    assert _edges(nodes) == _expected(9.8)


def test_pair_scorer_uses_same_max_and_preserves_input():
    nodes = _nodes(["mentions", "has_part", "related_to"], reverse=["part-of"], link_b=True)
    before = copy.deepcopy(nodes)
    a, b = copy.deepcopy(nodes["Concept_A"]), copy.deepcopy(nodes["Concept_B"])
    a["_key"], b["_key"] = "Concept_A", "Concept_B"
    assert indexer.calculate_relevance(a, b, nodes) == 9.8
    assert nodes == before


@pytest.mark.requires_native_core
def test_native_numeric_projection_preserves_links_and_existing_budget(monkeypatch):
    assert indexer.vector_lake_core is not None
    original = indexer.vector_lake_core.fast_calculate_weighted_edges
    captured = {}

    def capture(payload, affinities, overlap, minimum, cap):
        captured.update(copy.deepcopy(payload))
        assert cap == indexer.MAX_EDGES_PER_NODE == 15
        assert minimum == 1.5
        return original(payload, affinities, overlap, minimum, cap)

    monkeypatch.setattr(indexer, "HAVE_CORE", True)
    monkeypatch.setattr(indexer.vector_lake_core, "fast_calculate_weighted_edges", capture)
    nodes = _nodes([], reverse=["part-of"], link_a=True)
    assert _edges(nodes) == _expected(9.8)
    assert captured["Concept_A"]["links"] == ["Concept_B"]
    assert captured["Concept_B"]["links"] == []
    assert captured["Concept_A"]["triples"] == {"Concept_B": 9.0}
    assert captured["Concept_B"]["triples"] == {"Concept_A": 0.0}


@pytest.mark.requires_native_core
def test_backend_parity_with_mixed_nodes_and_order(monkeypatch):
    assert indexer.vector_lake_core is not None
    nodes = _nodes(["mentions", "has_part", "related_to"], reverse=["part-of"], link_b=True)
    nodes["Concept_C"] = {"type": "concept", "links": ["Concept_A", "Concept_B"],
                          "triples": [{"predicate": "competes-with", "target": "Concept_A"}]}
    for node in nodes.values():
        node["sources"] = ["synthetic-source"]
    monkeypatch.setattr(indexer, "HAVE_CORE", False)
    python_edges = _edges(copy.deepcopy(nodes))
    monkeypatch.setattr(indexer, "HAVE_CORE", True)
    assert _edges(dict(reversed(list(copy.deepcopy(nodes).items())))) == python_edges
