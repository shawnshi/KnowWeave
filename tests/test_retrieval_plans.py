"""Deterministic scope, year and task-ranking oracles; no real corpus or provider."""
import pytest

from vector_lake.retrieval_plan import plan_query, PlannedCatalog
from vector_lake import tool_search as search


def test_analysis_recognizes_ascii_product_adjacent_to_chinese():
    plan = plan_query('WiNEX的竞争优势及不足')
    assert plan.profile == 'analysis'
    assert plan.supplemental_queries == ('WiNEX',)
    assert 'WiNEX' not in str(plan.diagnostics())


def test_event_year_ignores_ingestion_timestamps():
    plan = plan_query('2026年全国卫生健康工作会议要点')
    assert plan.event_year == '2026' and plan.profile == 'balanced'
    assert not plan.accepts({'title': '2023年全国深化医改会议', 'created': '2026-01-01'})
    assert plan.year_status({'title': '会议材料', 'updated': '2026-01-01'}) == 'unverified'
    assert plan.year_status({'title': '会议材料', 'event_date': '2026-01-05'}) == 'metadata_year_match'
    assert not plan.accepts({'title': '2026全国卫生健康委员会', 'type': 'Institution'})


def test_multi_year_event_comparison_is_not_forced_into_one_year():
    plan = plan_query('对比2023与2026年全国卫生健康工作会议')
    assert plan.event_year is None and plan.year_ambiguous
    assert plan.accepts({'title': '2023年全国卫生健康工作会议', 'type': 'Event'})
    assert plan.accepts({'title': '2026年全国卫生健康工作会议', 'type': 'Event'})


def test_entity_alias_and_source_profiles_are_explicit_tiers():
    person = (1.0, {'_key': 'Person_Test', 'title': 'Shawn Shi', 'aliases': ['师成'], 'type': 'Person'})
    news = (3.0, {'_key': 'Source_News', 'title': '师成发表演讲', 'type': 'Source'})
    assert plan_query('师成是谁').order([news, person])[0] == person
    assert plan_query('原文', 'source').order([person, news])[0] == news
    with pytest.raises(ValueError):
        plan_query('query', 'unknown')


class Catalog:
    def __init__(self, nodes):
        self.nodes = nodes
    def nodes_by_key(self, keys):
        return {k: self.nodes[k] for k in keys if k in self.nodes}
    def filter_fields(self, keys):
        return {k: {n: self.nodes[k].get(n) for n in ('domain', 'status', 'topic_cluster')}
                for k in keys if k in self.nodes}
    def adjacency(self):
        return {}


def _isolate(monkeypatch, nodes):
    catalog = Catalog(nodes)
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_rerank_candidates_locally', lambda q, rows: rows)
    monkeypatch.setattr(search.search_ledger, 'record', lambda *a, **k: None)
    monkeypatch.setenv('VECTOR_LAKE_AUTHOR_FACET', 'boost')
    return catalog


def test_year_constraint_participates_in_widening_not_post_topk(monkeypatch):
    nodes = {f'old{i}': {'title': f'2023年医改会议{i}', 'type': 'Event', 'domain': 'Medical_IT', 'status': 'Active'} for i in range(30)}
    nodes['new'] = {'title': '2026年全国卫生健康工作会议', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}
    nodes['unknown'] = {'title': '卫生工作会议背景', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}
    catalog = _isolate(monkeypatch, nodes)
    calls = []
    def fetch(query, limit=50, **kwargs):
        calls.append(limit)
        return [{'node_key': key, 'rank': -100.0 if key == 'unknown' else -1.0} for key in list(nodes)[:limit]]
    monkeypatch.setattr(search, '_get_fts_recall_results', fetch)
    rows, notes, error = search._search_scored_pages(
        '2026年全国卫生健康工作会议要点', 2, domain='Medical_IT', catalog=catalog,
        allow_external_embedding=False, profile='auto',
    )
    assert error is None and calls[:2] == [25, 50]
    assert [n['_key'] for _, n in rows] == ['new', 'unknown']
    assert any('Event year unverified' in note for note in notes)


def test_neutral_analysis_profile_does_not_apply_global_author_or_source_bias(monkeypatch):
    nodes = {
        'Source_Report': {'title': 'WiNEX report', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'},
        'Concept_Opinion': {'title': 'Opinion', 'type': 'Concept', 'domain': 'Medical_IT', 'status': 'Active'},
    }
    catalog = _isolate(monkeypatch, nodes)
    monkeypatch.setattr(search.author_facet, 'author_page_keys', lambda: pytest.fail('global author preference leaked'))
    def fetch(query, limit=50, **kwargs):
        assert kwargs['supplemental_queries'] == ('WiNEX',)
        return [{'node_key': 'Source_Report', 'rank': -2.0}, {'node_key': 'Concept_Opinion', 'rank': -1.5}]
    monkeypatch.setattr(search, '_get_fts_recall_results', fetch)
    rows, notes, error = search._search_scored_pages(
        'WiNEX优势及不足', 2, domain='Medical_IT', catalog=catalog,
        allow_external_embedding=False, profile='auto',
    )
    assert error is None and rows[0][1]['_key'] == 'Source_Report'
    assert rows[0][0] == 2.0
    assert not any('AUTHOR_FACET' in note for note in notes)


def test_chunk_database_failure_retains_page_paths_without_leaking_error_text(monkeypatch):
    import sqlite3
    nodes = {'Source_Report': {'title': 'Report', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}}
    catalog = _isolate(monkeypatch, nodes)
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [{'node_key': 'Source_Report', 'rank': -1.0}])
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError('DO_NOT_LEAK_DETAILS')
    monkeypatch.setattr(search, '_scoped_chunk_candidates', broken)
    rows, notes, error = search._search_scored_pages('query', 1, domain='Medical_IT', catalog=catalog,
        profile='balanced', use_chunks=True, allow_external_embedding=False)
    assert error is None and rows[0][1]['_key'] == 'Source_Report'
    assert any('Chunk retrieval failed: DatabaseError' in note for note in notes)
    assert 'DO_NOT_LEAK_DETAILS' not in str(notes)


def test_entity_profile_promotes_exact_alias_not_incidental_mention(monkeypatch):
    nodes = {
        'Source_News': {'title': '师成发表演讲', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'},
        'Person_Test': {'title': 'Shawn Shi', 'aliases': ['师成'], 'type': 'Person', 'domain': 'Medical_IT', 'status': 'Active'},
    }
    catalog = _isolate(monkeypatch, nodes)
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [
        {'node_key': 'Source_News', 'rank': -10.0}, {'node_key': 'Person_Test', 'rank': -1.0}])
    rows, _, _ = search._search_scored_pages('师成', 2, domain='Medical_IT', catalog=catalog,
                                           allow_external_embedding=False, profile='auto')
    assert [n['_key'] for _, n in rows] == ['Person_Test', 'Source_News']


@pytest.mark.parametrize('command', ['search', 'query'])
@pytest.mark.parametrize('domain', ['', '   '])
def test_cli_empty_domain_is_refused_before_retrieval(monkeypatch, command, domain):
    import sys
    from vector_lake import cli_app, tools
    monkeypatch.setattr(cli_app, '_configure_stdout', lambda: None)
    monkeypatch.setattr(cli_app, '_load_env', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['cli.py', command, 'needle', '--domain', domain])
    monkeypatch.setattr(tools, 'search_vector_lake', lambda *a, **k: pytest.fail('unscoped retrieval'))
    monkeypatch.setattr(tools, 'prepare_query_context', lambda *a, **k: pytest.fail('unscoped context'))
    assert cli_app.main() == 1


def test_new_cli_branches_do_not_shadow_existing_json_output(monkeypatch, capsys):
    import sys
    from vector_lake import cli_app, operational_status
    monkeypatch.setattr(cli_app, '_configure_stdout', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['cli.py', 'ops-status', '--db', 'synthetic.db', '--json'])
    monkeypatch.setattr(operational_status, 'collect_status', lambda *a, **k: {'synthetic': True, 'database': {'state': 'healthy'}})
    assert 'json' not in cli_app.main.__code__.co_varnames
    assert cli_app.main() == 0
    assert '"synthetic": true' in capsys.readouterr().out


def test_planned_metadata_lookup_uses_full_nodes_for_event_constraints():
    base = Catalog({'old': {'title': '2023工作会议', 'domain': 'Medical_IT'},
                    'new': {'title': '2026工作会议', 'domain': 'Medical_IT'}})
    filtered = PlannedCatalog(base, plan_query('2026工作会议'))
    assert set(filtered.filter_fields(['old', 'new'])) == {'new'}
