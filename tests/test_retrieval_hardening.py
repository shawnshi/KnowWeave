"""Synthetic regression oracles for KW-01..04 and KW-08..12; never use the live vault."""

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from vector_lake import db_store, governance_store, tool_search as search


def _memory(key, domain=None, source='Source_Public.md', cluster='c'):
    row = {
        'memory_id': key, 'memory_key': key, 'memory_type': 'fact',
        'text': 'needle factual evidence', 'memory_score': 0.8,
        'validity_state': 'active', 'updated_at': '2026-10-16T00:00:00+00:00',
        'source_page': source, 'topic_cluster': cluster,
    }
    if domain is not None:
        row['domain'] = domain
    return row


def _seed_memories(rows):
    db_store.init_db()
    with db_store.transaction():
        db_store.get_connection().executemany(
            'INSERT INTO operational_memory (memory_id,memory_type,score,status,data_json,updated_at) '
            'VALUES (?,\'fact\',0.8,\'active\',?,?)',
            [(r['memory_id'], json.dumps(r), r['updated_at']) for r in rows],
        )


def test_memory_scope_is_exact_and_precedes_topk():
    rows = [_memory(f'other{i}', 'Other', 'raw/Medical_IT/fake.md') for i in range(50)]
    rows.append(_memory('target', 'Medical_IT'))
    _seed_memories(rows)
    found = governance_store.search_operational_memory('needle', top_k=1, domain='Medical_IT')
    assert [m['memory_id'] for m in found] == ['target']
    assert 'target' in search.format_operational_memory_results('needle', top_k=1, domain='Medical_IT')


def test_memory_scope_resolves_source_metadata_without_substring_guessing():
    _seed_memories([_memory('target'), _memory('unknown', source='Missing.md')])
    node = {'title': 'Public', 'domain': 'Medical_IT', 'topic_cluster': 'c'}
    with db_store.transaction():
        db_store.get_connection().execute(
            'INSERT INTO page_index_nodes (node_key,node_id,domain,topic_cluster,node_json) '
            'VALUES (\'Source_Public\',\'public\',\'Medical_IT\',\'c\',?)', (json.dumps(node),),
        )
    found = governance_store.search_operational_memory(
        'needle', domain='Healthcare_IT', cluster='c', top_k=5,
    )
    assert [m['memory_id'] for m in found] == ['target']
    assert governance_store.search_operational_memory('needle', domain='Medical_IT', cluster='cc') == []


def test_scoped_memory_does_not_fall_back_to_unscoped_on_database_error(monkeypatch):
    def fail(*args, **kwargs):
        raise db_store.sqlite3.OperationalError('synthetic scoped failure')
    monkeypatch.setattr(governance_store, '_iter_scoped_memory_payloads', fail)
    monkeypatch.setattr(governance_store, '_load_memory_items', lambda: pytest.fail('unscoped fallback'))
    with pytest.raises(db_store.sqlite3.OperationalError):
        governance_store.search_operational_memory('needle', domain='Medical_IT')


def test_memory_packet_refreshes_between_requests(monkeypatch):
    state = {'text': 'synthetic-old'}
    def views(query, **kwargs):
        return ([{**_memory('m'), 'text': state['text']}], [])
    monkeypatch.setattr(governance_store, 'search_memory_packet_views', views)
    monkeypatch.setattr('vector_lake.memory_gram_index.gram_index_usable', lambda: True)
    assert 'synthetic-old' in search.build_memory_packet('same')['packet']
    state['text'] = 'synthetic-new'
    assert 'synthetic-new' in search.build_memory_packet('same')['packet']


@pytest.mark.parametrize('budget', [0, 1, 30, 60, 100, 500])
def test_memory_packet_never_exceeds_small_budget(monkeypatch, budget):
    monkeypatch.setattr(governance_store, 'search_memory_packet_views', lambda q, **kw: ([_memory('m')], []))
    monkeypatch.setattr('vector_lake.memory_gram_index.gram_index_usable', lambda: True)
    packet = search.build_memory_packet('probe', max_chars=budget)
    assert len(packet['packet']) <= budget
    assert packet['omitted_count'] == 1


def test_query_embedding_is_unit_normalized_before_caching(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-test-only')
    monkeypatch.setattr('vector_lake.embedding_scheduler.embed_texts', lambda *a, **k: [[2.0, 0.0]])
    search._QUERY_EMBEDDING_CACHE.clear()
    values, error = search._get_query_embedding('synthetic norm probe')
    assert error is None and values == [1.0, 0.0]
    assert math.isclose(math.hypot(*values), 1.0)
    search._QUERY_EMBEDDING_CACHE.clear()


@pytest.mark.parametrize('values', [[0.0, 0.0], [float('nan'), 1.0], [float('inf'), 1.0]])
def test_embedding_response_rejects_invalid_numeric_contract(values):
    from vector_lake.embedding_scheduler import _validated_response_values, EmbeddingResponseError
    response = SimpleNamespace(embeddings=[SimpleNamespace(values=values)])
    with pytest.raises(EmbeddingResponseError):
        _validated_response_values(response, 1, 2)


@pytest.mark.parametrize('values', [[0.0, 0.0], [float('nan'), 1.0], [float('inf'), 1.0]])
def test_document_embedding_rejects_invalid_vectors_before_database_access(monkeypatch, values):
    monkeypatch.setattr(db_store, 'get_connection', lambda: pytest.fail('invalid vector reached database'))
    with pytest.raises(ValueError, match='finite and non-zero'):
        db_store.upsert_embedding('Synthetic', values)


def test_document_normalization_handles_large_finite_values_without_overflow():
    import struct
    db_store.init_db()
    db_store.upsert_embedding('Synthetic', [1e308, 1e308] + [0.0] * 3070)
    blob = db_store.get_connection().execute(
        "SELECT embedding FROM vec_embeddings WHERE page_key='Synthetic'"
    ).fetchone()[0]
    assert math.isclose(math.hypot(*struct.unpack('<3072f', blob)), 1.0, abs_tol=1e-6)


def test_mcp_entrypoints_refuse_missing_scope_before_retrieval(monkeypatch):
    from vector_lake import mcp_server
    monkeypatch.setattr(mcp_server.tools, 'search_vector_lake', lambda *a, **k: pytest.fail('retrieved'))
    for invoke in [lambda: mcp_server.search_vector_lake('q'),
                   lambda: mcp_server.preview_query_context('q'),
                   lambda: mcp_server.query_logic_lake('q')]:
        with pytest.raises(ValueError, match='Explicit domain'):
            invoke()


def _empty_catalog():
    return SimpleNamespace(nodes_by_key=lambda keys: {}, node_summary_lines=lambda limit: [], adjacency=lambda: {})


def test_local_query_never_requests_external_embedding_and_logs_scope(monkeypatch):
    captured = []
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [])
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_get_query_embedding', lambda q: pytest.fail('external query'))
    monkeypatch.setattr(search, '_vector_projection_error', lambda: pytest.fail('vector access'))
    monkeypatch.setattr(search.search_ledger, 'record', lambda *a, **k: captured.append(k))
    monkeypatch.setenv('VECTOR_LAKE_AUTHOR_FACET', 'off')
    rows, notes, error = search._search_scored_pages(
        'needle', 3, domain='Medical_IT', catalog=_empty_catalog(), allow_external_embedding=False,
    )
    assert error is None and rows == []
    assert search.split_retrieval_notes(notes)[1] == []
    assert captured[0]['selectors']['domain'] == 'Medical_IT'
    assert captured[0]['configuration']['allow_external_embedding'] is False
    assert 'fts' in captured[0]['timings']


def test_unexpected_fts_failure_is_returned_not_just_logged(monkeypatch):
    def fail(*a, **k):
        raise RuntimeError('synthetic private input must not be echoed')
    monkeypatch.setattr(search, '_get_fts_recall_results', fail)
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setenv('VECTOR_LAKE_AUTHOR_FACET', 'off')
    _, notes, _ = search._search_scored_pages(
        'needle', 3, domain='Medical_IT', catalog=_empty_catalog(), allow_external_embedding=False,
    )
    assert 'FTS arm failed: RuntimeError' in notes
    assert not any('synthetic private input' in note for note in notes)


def test_scoped_context_does_not_render_global_index_or_strategy(monkeypatch, tmp_path):
    scope_calls = []
    def views(query, **kwargs):
        scope_calls.append(kwargs)
        return ([], [])
    def forbidden(*a, **k):
        pytest.fail('unscoped metadata read')
    monkeypatch.setattr(governance_store, 'search_memory_packet_views', views)
    monkeypatch.setattr('vector_lake.memory_gram_index.gram_index_usable', lambda: True)
    monkeypatch.setattr('vector_lake.purpose_contract.render_strategy_directive', forbidden)
    catalog = SimpleNamespace(node_summary_lines=forbidden)
    monkeypatch.setattr('vector_lake.page_index_projection.read_catalog', lambda: (catalog, None))
    forwarded = []
    def ranked(*a, **kwargs):
        forwarded.append(kwargs)
        return ([], [], None)
    monkeypatch.setattr(search, '_search_scored_pages', ranked)
    path = tmp_path / 'index.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(search, 'get_index_path', lambda: path)
    result = search.assemble_context('needle', domain='Medical_IT', cluster='c')
    assert result['purpose'] == '' and result['index_summary'] == ''
    assert scope_calls == [{'domain': 'Medical_IT', 'cluster': 'c', '_display_groups': True}]
    assert forwarded[0]['domain'] == 'Medical_IT'
    assert forwarded[0]['allow_external_embedding'] is False


def test_author_cache_retains_only_bounded_revisions(monkeypatch):
    from vector_lake import author_facet
    class Connection:
        revision = 0
        def execute(self, sql):
            value = (100, str(self.revision)) if 'FROM jobs' in sql else (
                (10, 'fixed') if 'page_index_state' in sql else (0, 'main', ':memory:')
            )
            return SimpleNamespace(fetchone=lambda: value)
    conn = Connection()
    monkeypatch.setattr(db_store, 'get_connection', lambda: conn)
    monkeypatch.setattr(author_facet, '_cache', {})
    monkeypatch.setattr(author_facet, '_latest_canonical_names', lambda p: {'x': 'Source_Public.md'})
    monkeypatch.setattr(author_facet, '_trace_derived_page_keys', lambda p: frozenset())
    for version in range(100):
        conn.revision = version
        assert 'Source_Public' in author_facet.author_page_keys()
    assert len(author_facet._cache) <= 8


def test_balanced_question_retains_distinct_positive_and_negative_evidence():
    content = '# WiNEX\n## 优势\n' + ('WiNEX 优势。' * 500) + '\n## 不足\n关键风险：迁移成本。\n'
    text, locator = search._evidence_excerpts(content, 'WiNEX 竞争优势及不足')
    assert '优势' in text and '迁移成本' in text
    assert '; ' in locator and len(text) <= 2500


def test_normal_filter_rejection_is_information_not_service_failure():
    note = 'FTS: 1 ranked candidate(s) matched but none passed the domain/cluster/history/filter selectors (domain=Medical_IT)'
    information, failures = search.split_retrieval_notes([note])
    assert information == [note] and failures == []


def test_ledger_records_scope_versions_and_timings_without_raw_query_or_unknown_fields():
    from vector_lake import search_ledger
    search_ledger.record(
        'synthetic-public-query', mode='page', top_k=3, returned=[],
        selectors={'domain': 'Medical_IT', 'filter_expr': 'must-not-retain'},
        index_generation='synthetic-index-stamp',
        configuration={'candidate_depth': 25, 'secret': 'must-not-retain'},
        timings={'fts': 1.234},
    )
    raw = search_ledger.ledger_path().read_text(encoding='utf-8')
    row = search_ledger.entries()[-1]
    assert 'synthetic-public-query' not in raw and 'must-not-retain' not in raw
    assert row['selectors'] == {'domain': 'Medical_IT'}
    assert row['index_generation'] != 'synthetic-index-stamp'
    assert row['configuration'] == {'candidate_depth': 25}
    assert row['timings_ms'] == {'fts': 1.23}


def test_rerank_native_failure_preserves_candidates_and_emits_diagnostic(monkeypatch):
    monkeypatch.setattr(search, 'HAVE_CORE', False)
    candidates = [(1.0, {'title': 'A'}), (0.5, {'title': 'B'})]
    assert search._rerank_candidates_locally('needle', candidates) == candidates
    assert 'unavailable' in search._LAST_RERANK_NOTE.message
    search._rerank_candidates_locally('needle', candidates[:1])
    assert search._LAST_RERANK_NOTE.message is None


def test_scoped_dry_run_trace_does_not_run_global_provenance(monkeypatch, tmp_path):
    from vector_lake import tool_query
    context = {'wiki_context': 'scoped-only', 'wiki_page_count': 1, 'memory_packet': '',
               'index_summary': '', 'purpose': '', 'retrieval_notes': [], 'budget_max': 5000}
    def assemble(query, **scope):
        assert scope['domain'] == 'Medical_IT'
        return context
    monkeypatch.setattr(tool_query, 'assemble_context', assemble)
    monkeypatch.setattr(tool_query, 'get_extension_root', lambda: tmp_path)
    monkeypatch.setattr(tool_query, 'read_template', lambda *a, **k: 'Payload {{payload_path}} Query {{query_str}}')
    monkeypatch.setattr(tool_query.provenance, 'build_trace_for_query', lambda *a: pytest.fail('global trace'))
    result = tool_query.prepare_query_context('needle', dry_run=True, domain='Medical_IT')
    assert 'scoped-only' in result and 'Domain: Medical_IT' in result


def test_fresh_mcp_protocol_scope_and_egress_contract(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from vector_lake import mcp_server
    calls = []
    def ranked(*args, **kwargs):
        calls.append(kwargs)
        return ([], [], None)
    monkeypatch.setattr(search, '_search_scored_pages', ranked)
    async def scenario():
        async with Client(mcp_server.mcp) as client:
            tools = await client.list_tools()
            tool = next(t for t in tools if t.name == 'search_vector_lake')
            assert tool.input_schema['properties']['allow_external_embedding']['default'] is False
            with pytest.raises(ToolError, match='Explicit domain'):
                await client.call_tool('search_vector_lake', {'query': 'synthetic-private-query'})
            assert calls == []
            await client.call_tool('search_vector_lake', {'query': 'synthetic-private-query', 'domain': 'Medical_IT'})
            await client.call_tool('search_vector_lake', {'query': 'WiNEX', 'domain': 'Medical_IT', 'allow_external_embedding': True})
    asyncio.run(scenario())
    assert calls[0]['domain'] == 'Medical_IT' and calls[0]['allow_external_embedding'] is False
    assert calls[1]['allow_external_embedding'] is True
