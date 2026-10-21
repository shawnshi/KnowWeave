"""RSP-20261016 regression oracles. Synthetic sources, no provider or live Wiki."""
import sqlite3
from types import SimpleNamespace

import pytest

from vector_lake import tool_search as search, chunk_index as chunks, governance_store
from vector_lake.retrieval_plan import plan_query, PlannedCatalog


class Catalog:
    def __init__(self, nodes=None, graph=None):
        self.nodes = nodes or {'Source_Audit': {'title': 'Audit', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}}
        self.graph = graph or {}
    def nodes_by_key(self, keys):
        return {k: self.nodes[k] for k in keys if k in self.nodes}
    def filter_fields(self, keys):
        return self.nodes_by_key(keys)
    def adjacency(self):
        return self.graph


def isolate(monkeypatch):
    from vector_lake import page_index_projection
    catalog = Catalog()
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr(page_index_projection, 'index_file_stamp', lambda: (0, 1))
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_rerank_candidates_locally', lambda q, rows: rows)
    monkeypatch.setattr(search.search_ledger, 'record', lambda *a, **k: None)
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [{'node_key': 'Source_Audit', 'rank': -1.0}])
    search._LAST_FTS_ERROR.msg = None
    return catalog


@pytest.mark.parametrize('domain,kernel', [
    ('Medical_IT', 'fast_personalized_pagerank'),
    (None, 'prepared_personalized_pagerank'),
], ids=['scoped', 'unscoped'])
def test_ppr_failure_preserves_scored_pages(monkeypatch, domain, kernel):
    catalog = isolate(monkeypatch)
    catalog.graph = {'Source_Audit': [('Source_Next', 1.0)], 'Source_Next': [('Source_Audit', 1.0)]}
    calls = []
    def broken(*a):
        calls.append(a)
        raise RuntimeError('DO_NOT_EXPOSE_PPR_DETAILS')
    monkeypatch.setattr(search, 'HAVE_CORE', True)
    monkeypatch.setattr(search, 'vector_lake_core', SimpleNamespace(**{kernel: broken}))
    monkeypatch.setattr(search, '_ppr_index', lambda *a: object())
    rows, notes, error = search._search_scored_pages('audit', 1, domain=domain, catalog=catalog, allow_external_embedding=False)
    assert len(calls) == 1, 'fault injection must reach the selected PPR kernel'
    assert error is None and rows[0][1]['_key'] == 'Source_Audit'
    assert any('PPR' in n and 'RuntimeError' in n for n in notes)
    assert 'DO_NOT_EXPOSE' not in str(notes)


def test_memory_failure_preserves_wiki_context(monkeypatch, tmp_path):
    isolate(monkeypatch)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    (tmp_path / 'Source_Audit.md').write_text('auditable evidence', encoding='utf-8')
    def broken(*a, **k):
        raise sqlite3.OperationalError('DO_NOT_EXPOSE_MEMORY_DETAILS')
    monkeypatch.setattr(governance_store, 'search_memory_packet_views', broken)
    result = search.assemble_context('audit', domain='Medical_IT', profile='balanced')
    assert 'auditable evidence' in result['wiki_context']
    assert 'OperationalError' in str(result)
    assert 'DO_NOT_EXPOSE' not in str(result)


@pytest.mark.parametrize('bad_line', ['bad', None, {}, -1])
def test_malformed_chunk_span_does_not_break_other_evidence(bad_line):
    content = '# Title\nactual evidence\n'
    node = {'_chunk_source_hash': chunks.content_hash(content), '_chunk_hits': [
        {'start_line': bad_line, 'end_line': 2, 'text': 'actual evidence'},
        {'start_line': 2, 'end_line': 2, 'text': 'actual evidence'},
    ]}
    text, locator, warning = search._evidence_for_node(content, 'audit', node)
    assert 'actual evidence' in text and 'sha256:' in locator
    assert warning and 'span' in warning.lower()


def test_weak_event_keyword_does_not_exclude_product():
    plan = plan_query('Discourse论坛系统的优势和不足')
    assert plan.accepts({'type': 'Product', 'title': 'Discourse'})


def test_year_eligibility_drives_chunk_document_widening(monkeypatch, tmp_path):
    content = 'current evidence'
    digest = chunks.content_hash(content)
    nodes = {f'Source_Old{i}': {'title': '2023年工作会议', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'} for i in range(25)}
    nodes['Source_Current'] = {'title': '2026年工作会议', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}
    limits = []
    def reader(*a, top_k=25, **k):
        limits.append(top_k)
        return [{'page_key': key, 'source_hash': digest, 'hits': []} for key in list(nodes)[:top_k]], []
    monkeypatch.setattr(chunks, 'search_chunks', reader)
    monkeypatch.setattr(chunks, 'read_page', lambda p: content)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    plan = plan_query('2026年工作会议')
    rows, notes = search._scoped_chunk_candidates('2026年工作会议', 'Medical_IT', None, False, None, PlannedCatalog(Catalog(nodes), plan), plan)
    assert [r['page_key'] for r in rows] == ['Source_Current']
    assert max(limits) > 25


def test_mcp_protocol_concurrent_requests_keep_request_local_state(monkeypatch, tmp_path):
    import asyncio
    from fastmcp import Client
    from vector_lake import mcp_server
    isolate(monkeypatch)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    (tmp_path / 'Source_Audit.md').write_text('actual protocol evidence', encoding='utf-8')
    records = []
    monkeypatch.setattr(search.search_ledger, 'record', lambda **k: records.append(k))
    async def run():
        async with Client(mcp_server.mcp) as client:
            results = await asyncio.gather(*[client.call_tool('search_vector_lake', {'query': f'audit{i}', 'domain': 'Medical_IT'}) for i in range(16)])
            assert all(any('actual protocol evidence' in getattr(block, 'text', '') for block in result.content) for result in results)
    asyncio.run(run())
    assert len(records) == 16 and len({r['query'] for r in records}) == 16
    assert all(r['status'] == 'complete' and 'endpoint_total' in r['timings'] for r in records)


def test_provider_error_details_are_not_returned(monkeypatch):
    from vector_lake import embedding_scheduler
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-key')
    monkeypatch.setattr(search, '_embedding_cache_key', lambda q: ('synthetic', 2, q))
    def broken(*a, **k):
        raise RuntimeError('DO_NOT_EXPOSE_PROVIDER_DETAILS')
    monkeypatch.setattr(embedding_scheduler, 'embed_texts', broken)
    values, error = search._get_query_embedding('synthetic-RSP-provider')
    assert not values and error is not None and 'RuntimeError' in error and 'DO_NOT_EXPOSE' not in error


def test_vector_error_details_are_not_returned(monkeypatch):
    from vector_lake import db_store
    def broken():
        raise sqlite3.OperationalError('DO_NOT_EXPOSE_SQL_DETAILS')
    monkeypatch.setattr(db_store, 'get_connection', broken)
    values, error = search._get_vector_search_results([1.0, 0.0])
    assert values == {} and error is not None and 'OperationalError' in error and 'DO_NOT_EXPOSE' not in error


def test_failed_dependency_is_not_business_no_match(monkeypatch):
    isolate(monkeypatch)
    def failed(*a, **k):
        search._LAST_FTS_ERROR.msg = 'FTS query failed: OperationalError'
        return []
    monkeypatch.setattr(search, '_get_fts_recall_results', failed)
    result = search.search_vector_lake('audit', domain='Medical_IT', allow_external_embedding=False)
    assert 'No matching pages.' not in result
    assert 'failed' in result.lower()


def test_large_source_is_bounded_before_evidence_render(monkeypatch, tmp_path):
    isolate(monkeypatch)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    (tmp_path / 'Source_Audit.md').write_text('x' * (600 * 1024), encoding='utf-8')
    result = search.search_vector_lake('audit', domain='Medical_IT', profile='balanced')
    assert 'budget' in result.lower() or 'limit' in result.lower()
    assert len(result) < 10000


@pytest.mark.parametrize('query,top_k', [('x' * 5000, 5), ('audit', 1000000)])
def test_public_workload_limits_are_enforced(query, top_k):
    with pytest.raises(ValueError):
        search.search_vector_lake(query, top_k=top_k, domain='Medical_IT')
