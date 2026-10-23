"""Full-name span containment narrows fact anchors, not page Search candidates."""
import pytest

from vector_lake import page_index_projection as projection, tool_search as search


@pytest.mark.parametrize('titles,query,expected', [
    (['Nova', 'Suite', 'Nova Suite'], 'Nova Suite 的定义', 'Nova Suite'),
    (['AI', 'AI智能体'], 'AI智能体 的定义', 'AI智能体'),
    (['架构', '企业架构(EA)'], '企业架构(EA) 的定义', '企业架构(EA)'),
    (['RAG', '增强生成-RAG', '检索增强生成-RAG'], '检索增强生成-RAG 的定义', '检索增强生成-RAG'),
    (['Nova', 'Suite', 'Nova Suite'], 'Nova Suite 与 Nova 的比较', None),
    (['Nova', 'Suite', 'Nova Suite'], 'Nova 的定义及 Nova Suite', None),
    (['Nova Suite', 'Suite Plus'], 'Nova Suite Plus 的定义', None),
    (['Shared Name', 'Shared Name'], 'Shared Name 的定义', None),
    (['Nova Suite', 'Other'], 'Nova Suite 与 Other 的比较', None),
    (['André', 'Andréa'], 'Andréa 的定义', 'Andréa'),
    (['İris', 'İris Garden'], 'İris Garden 的定义', 'İris Garden'),
    (['医疗', '医疗健康'], '医疗健康的定义', '医疗健康'),
])
def test_nested_name_spans_preserve_real_ambiguity(monkeypatch, titles, query, expected):
    nodes = {f'Concept_{i}': {'id': f'id-{i}', 'title': title, 'domain': 'General'}
             for i, title in enumerate(titles)}
    catalog = projection._FileCatalog({'nodes': nodes})
    monkeypatch.setattr(projection, 'read_catalog', lambda: (catalog, None))
    result = search._fact_entity_context(query, 'General')
    assert (result[1]['title'] if result else None) == expected
    assert set(search._name_entry_candidates(query, catalog, 'General', None, True, None)) == {
        k for k, node in nodes.items() if search._complete_name_mentions(query, k, node)
    }


def test_out_of_scope_long_names_do_not_shadow_visible_identity(monkeypatch):
    nodes = {'Concept_Short': {'id': 'short', 'title': 'Nova', 'domain': 'General'},
             'Concept_Long': {'id': 'long', 'title': 'Nova Suite', 'domain': 'Medical_IT'}}
    monkeypatch.setattr(projection, 'read_catalog', lambda: (projection._FileCatalog({'nodes': nodes}), None))
    result = search._fact_entity_context('Nova Suite 的定义', 'General')
    assert result is not None and result[0] == 'Concept_Short'


def test_same_canonical_identity_is_not_false_ambiguity(monkeypatch):
    nodes = {'Concept_A': {'id': 'same', 'title': 'Nova Suite', 'domain': 'General'},
             'Concept_B': {'id': 'same', 'title': 'Nova Suite', 'domain': 'General'}}
    monkeypatch.setattr(projection, 'read_catalog', lambda: (projection._FileCatalog({'nodes': nodes}), None))
    result = search._fact_entity_context('Nova Suite 的定义', 'General')
    assert result is not None and result[1]['id'] == 'same'


def test_name_candidate_budget_fails_closed(monkeypatch):
    nodes = {f'Concept_{i}': {'id': str(i), 'title': name, 'domain': 'General'}
             for i, name in enumerate(['Nova', 'Suite', 'Nova Suite'])}
    monkeypatch.setattr(projection, 'read_catalog', lambda: (projection._FileCatalog({'nodes': nodes}), None))
    monkeypatch.setenv('VECTOR_LAKE_CANDIDATE_DEPTH', '2')
    assert search._fact_entity_context('Nova Suite 的定义', 'General') is None
