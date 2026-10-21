"""Exact qualification oracle and dead-posting fanout regression (no candidate cap)."""
import copy
import math
import random
import time

import pytest
from vector_lake import indexer


class CountedKey(str):
    comparisons = 0
    def __lt__(self, other):
        type(self).comparisons += 1
        return super().__lt__(other)


def _config(monkeypatch, affinity=0.5, overlap=2.0):
    monkeypatch.setattr(indexer, "HAVE_CORE", False)
    monkeypatch.setattr(indexer, "TYPE_AFFINITY", {"concept": {"concept": affinity}})
    monkeypatch.setattr(indexer, "RELEVANCE_WEIGHTS", {"direct_link": 5.0, "type_affinity": 1.0, "common_neighbor": 1.0, "source_overlap": overlap})


def _reference(nodes):
    """Pairwise specification for canonical fixture links (not an inverted index)."""
    links = {key: frozenset(node.get("links", [])) for key, node in nodes.items()}
    degree = {key: 1.0 / math.log(len(node.get("links", []))) * indexer.RELEVANCE_WEIGHTS["common_neighbor"] if len(node.get("links", [])) > 1 else 0.0 for key, node in nodes.items()}
    multipliers = {key: math.sqrt(node.get("decay_weight", 1.0) * max(0.1, node.get("alignment_score", 100.0) / 100.0)) for key, node in nodes.items()}
    weights = {}
    for key, node in nodes.items():
        weights[key] = {}
        for triple in node.get("triples", []):
            target = triple["target"]
            weight = indexer.get_pred_weight(triple.get("predicate", "mentions"))
            weights[key][target] = max(weights[key].get(target, weight), weight)
    output = []
    for a in nodes:
        for b in nodes:
            if a >= b:
                continue
            shared_sources = set(nodes[a].get("sources", [])) & set(nodes[b].get("sources", []))
            shared_neighbors = links[a] & links[b]
            if not (b in links[a] or a in links[b] or shared_sources or shared_neighbors):
                continue
            score = 0.0
            declared = [weights[source][target] for source, target in ((a, b), (b, a)) if target in weights[source]]
            if declared:
                score += max(declared)
            elif b in links[a] or a in links[b]:
                score += indexer.get_pred_weight("mentions")
            if shared_sources:
                score += len(shared_sources) * indexer.RELEVANCE_WEIGHTS["source_overlap"]
            if shared_neighbors:
                # Same summation order as the established scorer's links_a loop.
                score += sum(degree.get(neighbor, 0.0) for neighbor in links[a] if neighbor in links[b])
            type_a, type_b = nodes[a].get("type", "concept").lower(), nodes[b].get("type", "concept").lower()
            score += indexer.TYPE_AFFINITY.get(type_a, {}).get(type_b, 0.5) * indexer.RELEVANCE_WEIGHTS["type_affinity"]
            relevance = round(score * (multipliers[a] * multipliers[b]), 3)
            if relevance >= 1.5:
                output.append({"source": a, "target": b, "weight": relevance})
    return indexer.dedupe_and_prune_edges(output)


def test_unresolved_zero_neighbor_does_not_expand_a_quadratic_bucket(monkeypatch, record_property):
    _config(monkeypatch)
    count = 800
    nodes = {CountedKey(f"Concept_{i:04}"): {"type": "concept", "links": ["Missing"]} for i in range(count)}
    CountedKey.comparisons = 0
    started = time.perf_counter()
    edges = indexer._calculate_weighted_edges({"nodes": nodes}, alias_map={})
    comparisons = CountedKey.comparisons
    record_property("zero_neighbor_key_comparisons", comparisons)
    record_property("zero_neighbor_ms", round((time.perf_counter() - started) * 1000, 3))
    assert edges == []
    assert comparisons < 50 * count


def test_zero_source_weight_does_not_expand_an_inert_bucket(monkeypatch, record_property):
    _config(monkeypatch, overlap=0.0)
    count = 800
    nodes = {CountedKey(f"Concept_{i:04}"): {"type": "concept", "sources": ["shared"]} for i in range(count)}
    CountedKey.comparisons = 0
    assert indexer._calculate_weighted_edges({"nodes": nodes}, alias_map={}) == []
    record_property("zero_source_key_comparisons", CountedKey.comparisons)
    assert CountedKey.comparisons < 50 * count


@pytest.mark.parametrize("affinity", [0.5, 1.49949, 1.4995, 1.5, 2.0, float("nan"), float("inf")])
@pytest.mark.parametrize("decay", [1.0, 4.0, float("nan"), float("inf")])
def test_affinity_rounding_and_nonfinite_fallback_match_pairwise_oracle(monkeypatch, affinity, decay):
    _config(monkeypatch, affinity=affinity)
    nodes = {"Concept_A": {"type": "concept", "links": ["Missing"], "decay_weight": decay}, "Concept_B": {"type": "concept", "links": ["Missing"]}}
    assert indexer._calculate_weighted_edges({"nodes": copy.deepcopy(nodes)}, alias_map={}) == _reference(nodes)


def test_deterministic_mixed_evidence_matches_pairwise_oracle(monkeypatch):
    rng = random.Random(1707)
    for overlap in (0.0, 2.0, -1.0):
        _config(monkeypatch, overlap=overlap)
        for _case in range(30):
            keys = [f"Concept_{i}" for i in range(8)]
            nodes = {}
            for key in keys:
                links = [other for other in keys + ["Missing"] if other != key and rng.random() < 0.22]
                nodes[key] = {"type": "concept", "links": links,
                              "sources": [source for source in ["s0", "s1", "s2"] if rng.random() < 0.3],
                              "triples": [{"predicate": "mentions", "target": target} for target in links if rng.random() < 0.25],
                              "decay_weight": rng.choice([0.0, 0.25, 1.0, 4.0]),
                              "alignment_score": rng.choice([0.0, 50.0, 100.0, 200.0])}
            assert indexer._calculate_weighted_edges({"nodes": copy.deepcopy(nodes)}, alias_map={}) == _reference(nodes)


def test_positive_degree_neighbor_and_direct_evidence_are_not_dropped(monkeypatch):
    _config(monkeypatch, overlap=0.0)
    nodes = {"Concept_A": {"links": ["Concept_Hub", "Concept_B"]}, "Concept_B": {"links": ["Concept_Hub"]}, "Concept_Hub": {"links": ["outside_1", "outside_2"]}}
    expected = _reference(nodes)
    assert expected
    assert indexer._calculate_weighted_edges({"nodes": copy.deepcopy(nodes)}, alias_map={}) == expected
