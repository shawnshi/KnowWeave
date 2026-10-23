"""Exact declared edition equivalence is restricted to whole publication questions."""
import pytest

from vector_lake import page_index_projection as projection, tool_search as search


def node(title, *, identity='edition', domain='General', cluster='specs', kind='standard', aliases=None):
    return dict(id=identity, title=title, type=kind, domain=domain, topic_cluster=cluster,
                aliases=aliases or [], status='Active')


def resolve(monkeypatch, query, nodes, domain='General', cluster=None):
    catalog = projection._FileCatalog({'nodes': nodes})
    monkeypatch.setattr(projection, 'read_catalog', lambda: (catalog, None))
    return search._fact_entity_context(query, domain, cluster)


@pytest.mark.parametrize('query,number', [
    ('Spec 第十版发布日期', '10'), ('Spec第十版的发布日期？', '10'),
    ('Spec 第12版发布时间。', '12'), ('Spec 第十二版何时发布', '12'),
    ('Spec 第二十一版什么时候发布？', '21'), ('Spec 第21版首次发布日期', '21'),
    ('Spec 第一版发布日期', '1'), ('Spec 第九十九版发布日期', '99'),
    ('Spec 第100版发布日期', '100'), ('Spec 第123456版发布日期', '123456'),
    ('  Spec第十版 的 发布日期？  ', '10'), ('İris 第十版发布日期', '10'),
])
@pytest.mark.parametrize('separator', [' ', '-', '_'])
def test_whole_publication_question_matches_explicit_declared_integer(monkeypatch, query, number, separator):
    stem = 'İris' if query.startswith('İris') else 'Spec'
    title = stem + separator + number
    result = resolve(monkeypatch, query, {'Standard_Target': node(title)})
    assert result is not None and result[0] == 'Standard_Target'


@pytest.mark.parametrize('kind', ['concept', 'product', 'standard'])
def test_alias_and_typed_key_are_existing_declarations(monkeypatch, kind):
    result = resolve(monkeypatch, 'Atlas第十二版发布日期', {
        'Concept_Unrelated': node('Other', kind=kind, aliases=['Atlas-12'])})
    assert result is not None
    result = resolve(monkeypatch, 'Atlas第十二版发布日期', {
        'Concept_Atlas-12': node('Other', kind=kind)})
    assert result is not None


@pytest.mark.parametrize('query', [
    'Spec 第九版发布日期', 'Spec 第十一版发布日期', 'Spec 第0版发布日期',
    'Spec 第Ⅹ版发布日期', 'Spec 第X版发布日期', 'Spec 第10.0版发布日期',
    'Spec 第一百版发布日期', 'Spec 第两版发布日期', 'Spec 第十版与Unknown比较发布日期',
    'Spec 第十版和Spec 第十一版发布日期', 'Spec 第十版发布日期及Other的定义',
    '比较 Spec 第十版发布日期', 'Spec 第十一版与Spec 第十版发布日期',
])
@pytest.mark.parametrize('with_known_edition', [False, True])
def test_unknown_unsupported_or_comparison_never_falls_back_to_generic(monkeypatch, query, with_known_edition):
    nodes = {'Standard_Parent': node('Spec', identity='parent')}
    if with_known_edition:
        nodes['Standard_Target'] = node('Spec 10')
    assert resolve(monkeypatch, query, nodes) is None


def test_out_of_scope_edition_cannot_borrow_parent_or_correct_number(monkeypatch):
    nodes = {'Standard_Parent': node('Spec', identity='parent'),
             'Standard_Target': node('Spec 10', domain='Medical_IT')}
    assert resolve(monkeypatch, 'Spec 第十版发布日期', nodes) is None
    nodes['Standard_Target']['domain'] = 'General'
    nodes['Standard_Target']['topic_cluster'] = 'elsewhere'
    assert resolve(monkeypatch, 'Spec 第十版发布日期', nodes, cluster='specs') is None


def test_two_declared_identities_stay_ambiguous_but_same_identity_groups(monkeypatch):
    nodes = {'Standard_A': node('Spec 10', identity='one'),
             'Standard_B': node('Spec-10', identity='two')}
    assert resolve(monkeypatch, 'Spec第十版发布日期', nodes) is None
    nodes['Standard_B']['id'] = 'one'
    assert resolve(monkeypatch, 'Spec第十版发布日期', nodes) is not None


def test_unversioned_literal_path_and_page_candidates_are_not_rewritten(monkeypatch):
    nodes = {'Standard_Parent': node('Spec', identity='parent'), 'Standard_Target': node('Spec 10')}
    result = resolve(monkeypatch, 'Spec 的定义', nodes)
    assert result is not None and result[0] == 'Standard_Parent'
    catalog = projection._FileCatalog({'nodes': nodes})
    assert set(search._name_entry_candidates('Spec第十版发布日期', catalog, 'General', None, True, None)) == {'Standard_Parent'}
    assert resolve(monkeypatch, 'Spec 第十版发布日期', {'Source_Target': node('Spec 10', kind='source')}) is None


def test_original_text_span_is_preserved_and_candidate_budget_is_unchanged(monkeypatch):
    request = projection._edition_publication_request('  İris第十版的发布日期？')
    assert request is not None
    assert '  İris第十版的发布日期？'[slice(*request[2])] == 'İris第十版'
    nodes = {'Standard_A': node('Spec 10', identity='one'), 'Standard_B': node('Spec-10', identity='two')}
    monkeypatch.setenv('VECTOR_LAKE_CANDIDATE_DEPTH', '1')
    assert resolve(monkeypatch, 'Spec 第十版发布日期', nodes) is None
