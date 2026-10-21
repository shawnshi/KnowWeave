"""Common capped graph contract: incident degree and deterministic same-weight admission."""
from collections import Counter
import pytest
from vector_lake import indexer


def _degree(edges):
    return Counter(endpoint for edge in edges for endpoint in (edge['source'], edge['target']))


def _nodes(n):
    keys = [f'Concept_Cap{i:02}' for i in range(n)]
    return {key: {'type':'concept','links':[k for k in keys if k != key], 'sources':[], 'triples':[]} for key in keys}


def _pairs(edges):
    return {(edge['source'],edge['target']) for edge in edges}


def test_runtime_graph_enforces_existing_incident_degree_fifteen():
    edges = indexer._calculate_weighted_edges({'nodes':_nodes(64)})
    degree = _degree(edges)
    assert edges and max(degree.values()) <= indexer.MAX_EDGES_PER_NODE == 15


def test_capped_equal_weight_runtime_graph_matches_common_lex_contract(monkeypatch):
    nodes = _nodes(64)
    actual = indexer._calculate_weighted_edges({'nodes':nodes})
    monkeypatch.setattr(indexer, 'HAVE_CORE', False)
    expected = indexer._calculate_weighted_edges({'nodes':_nodes(64)})
    assert _pairs(actual) == _pairs(expected)
    # This fixture's identical finite score is not universal scoring/qualifying-set parity.


@pytest.mark.requires_native_core
@pytest.mark.parametrize('cap',[0,1,2,5,15,64])
def test_native_cap_counts_both_incident_endpoints_and_stable_ties(cap):
    core = indexer.vector_lake_core
    assert core is not None
    keys = list(_nodes(64))
    def calc(order):
        payload={k:{'type':'concept','links':[j for j in keys if j != k],'sources':[],'triples':{},'multiplier':1.0,'degree_weight':0.0} for k in order}
        return core.fast_calculate_weighted_edges(payload,{'concept':{'concept':2.0}},0.0,1.5,cap)
    actual = calc(keys)
    reverse = calc(list(reversed(keys)))
    expected = indexer.dedupe_and_prune_edges([{'source':a,'target':b,'weight':2.0} for i,a in enumerate(keys) for b in keys[i+1:]],max_edges_per_node=cap)
    assert _pairs(actual) == _pairs(reverse) == _pairs(expected)
    assert not actual if cap == 0 else max(_degree(actual).values(),default=0) <= cap
