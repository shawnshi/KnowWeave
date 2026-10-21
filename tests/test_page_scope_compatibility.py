"""Page search retains registered macro aliases; relation reads keep exact scopes."""
import json

import pytest

from vector_lake import db_store, page_index_projection as projection, tool_search
from vector_lake.tool_relations import get_relations


@pytest.fixture(autouse=True)
def schema(isolated_memory):
    db_store.init_db()


def index_data(cluster='C'):
    nodes = {
        'Concept_A': {'domain': 'Medical_IT', 'topic_cluster': cluster},
        'Concept_B': {'domain': ' Healthcare_IT ', 'topic_cluster': cluster.lower()},
        'Concept_Hidden': {'domain': 'Private', 'topic_cluster': cluster},
        'Concept_Spaced': {'domain': 'Medical_IT', 'topic_cluster': f' {cluster} '},
    }
    for key, node in nodes.items():
        node.update(title=key, type='concept', status='Active', links=[], triples=[])
    nodes['Concept_A']['triples'] = [{'predicate': 'related_to', 'target': 'Concept_B'}]
    edges = [{'source': a, 'target': b, 'weight': 2.0} for a, b in [
        ('Concept_A', 'Concept_B'), ('Concept_A', 'Concept_Hidden'),
        ('Concept_Hidden', 'Concept_B'), ('Concept_A', 'Concept_Spaced'),
    ]]
    return {'nodes': nodes, 'weighted_edges': edges}


@pytest.mark.parametrize('backend', ['file', 'projection'])
@pytest.mark.parametrize('cluster', ['C', 'Économie', 'Σύνολο', 'İstanbul', 'ОБЗОР'])
def test_page_walk_uses_same_alias_scope_as_visible_results(isolated_memory, backend, cluster):
    data = index_data(cluster)
    if backend == 'file':
        catalog = projection._FileCatalog(data)
    else:
        projection.refresh_page_index_projection(data)
        catalog = projection._SqliteCatalog()
    visible = {key for key, node in data['nodes'].items()
               if tool_search._passes_filters(node, 'Medical_IT', cluster, False, None)}
    assert visible == {'Concept_A', 'Concept_B'}
    assert catalog.scoped_adjacency(domain='Medical_IT', cluster=cluster) == {
        'Concept_A': [('Concept_B', 2.0)], 'Concept_B': [('Concept_A', 2.0)],
    }
    assert catalog.scoped_adjacency(domain='Unknown', cluster=cluster) == {}


def test_relation_api_does_not_inherit_page_alias_expansion(isolated_memory):
    data = index_data()
    (isolated_memory / 'wiki' / 'index.json').write_text(json.dumps(data), encoding='utf-8')
    projection.refresh_page_index_projection(data)
    assert tool_search._passes_filters(data['nodes']['Concept_B'], 'Medical_IT', 'C', False, None)
    assert get_relations('Concept_B', 'Medical_IT', cluster='C')['status'] == 'not_found'
    exact = get_relations('Concept_A', 'Medical_IT', cluster='C')
    assert exact['status'] == 'ok' and exact['relations'] == []
