"""Portable live-backend graph checks; explicitly diagnose, not normalize, legacy drift."""
from collections import Counter
import copy

import pytest

from vector_lake import indexer


@pytest.fixture
def graph_contract(monkeypatch):
    monkeypatch.setattr(indexer, 'TYPE_AFFINITY', {'concept': {'concept': 0.5}})
    # Navigation contributes 0.4 * 5 = 2 once per pair, independently of direction.
    monkeypatch.setattr(indexer, 'RELEVANCE_WEIGHTS', {'direct_link': 5.0, 'type_affinity': 1.0, 'source_overlap': 2.0, 'common_neighbor': 1.0})


def _calculate(nodes):
    original = copy.deepcopy(nodes)
    edges = indexer._calculate_weighted_edges({'nodes': nodes}, alias_map={key: key for key in nodes})
    assert nodes == original, 'temporary graph calculation fields leaked into input'
    return {(edge['source'], edge['target']): edge['weight'] for edge in edges}


@pytest.mark.parametrize('shape,expected', [('direct', 2.5), ('reciprocal', 2.5), ('source', 2.5), ('neighbor', 1.943)])
def test_live_backend_qualifies_and_scores_basic_evidence(graph_contract, shape, expected):
    a, b = 'Concept_A.md', 'Concept_B.md'
    nodes = {a: {'type': 'concept', 'links': []}, b: {'type': 'concept', 'links': []}}
    if shape in {'direct', 'reciprocal'}:
        nodes[a]['links'] = [b]
    if shape == 'reciprocal':
        nodes[b]['links'] = [a]
    if shape == 'source':
        nodes[a]['sources'] = nodes[b]['sources'] = ['Source_Common.md']
    if shape == 'neighbor':
        nodes[a]['links'] = nodes[b]['links'] = ['Concept_M.md']
        nodes['Concept_M.md'] = {'type': 'concept', 'links': ['Missing_X', 'Missing_Y']}
    oracle = {(a, b): expected}
    if shape == 'neighbor':
        oracle.update({(a, 'Concept_M.md'): 2.5, (b, 'Concept_M.md'): 2.5})
    assert _calculate(nodes) == oracle


def test_live_backend_accepts_exact_threshold_and_rejects_nan(graph_contract, monkeypatch):
    a, b = 'Concept_A.md', 'Concept_B.md'
    # 0.4 * 2.5 navigation + 0.5 affinity is exactly the admission threshold.
    monkeypatch.setitem(indexer.RELEVANCE_WEIGHTS, 'direct_link', 2.5)
    nodes = {a: {'links': [b]}, b: {'links': []}}
    assert _calculate(nodes) == {(a, b): 1.5}
    nodes[a]['decay_weight'] = float('nan')
    # NaN is not equal to itself: qualify via direct invocation, not _calculate's input oracle.
    assert indexer._calculate_weighted_edges({'nodes': nodes}, alias_map={a: a, b: b}) == []


def test_live_backend_drops_zero_contribution_unresolved_shared_neighbors(graph_contract):
    a, b = 'Concept_A.md', 'Concept_B.md'
    assert _calculate({a: {'links': ['Missing_Common']}, b: {'links': ['Missing_Common']}}) == {}


def test_live_backend_incident_degree_contract_is_enforced(graph_contract, monkeypatch, record_property):
    monkeypatch.setitem(indexer.RELEVANCE_WEIGHTS, 'common_neighbor', 0.0)
    keys = [f'Concept_N{value:02}.md' for value in range(64)]
    edges = _calculate({key: {'links': [other for other in keys if other != key]} for key in keys})
    counts = Counter(endpoint for pair in edges for endpoint in pair)
    maximum = max(counts.values(), default=0)
    record_property('ci_core_graph_backend', 'native' if indexer.HAVE_CORE else 'fallback')
    record_property('ci_core_python_incident_cap', indexer.MAX_EDGES_PER_NODE)
    record_property('ci_core_observed_max_incident_degree', maximum)
    record_property('ci_core_native_prune_parity_unverified', bool(indexer.HAVE_CORE and maximum > indexer.MAX_EDGES_PER_NODE))
    assert edges and all(source < target for source, target in edges)
    # This enforces only the shared incident cap, not universal scoring parity.
    assert maximum <= indexer.MAX_EDGES_PER_NODE
