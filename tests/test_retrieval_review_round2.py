"""Cross-surface oracles from the second independent review; synthetic data only."""
from array import array
import json
import sqlite3

import pytest

from vector_lake import tool_search as search, tool_query, retrieval_runtime as runtime
from vector_lake import chunk_index as chunks, page_index_projection as projection
from vector_lake import scoped_retrieval_index as indexes, two_stage_index as stage
from tests.test_retrieval_review_regressions import vector_database


@pytest.fixture
def deadline_catalog(monkeypatch):
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,node_json TEXT,domain TEXT,topic_cluster TEXT,status TEXT,node_id TEXT,title TEXT,type TEXT)')
    keys = [f'Source_Lexical{i:03d}' for i in range(200)]
    for key in keys:
        node = {'title': key, 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active', 'summary': 'retained lexical summary'}
        c.execute('INSERT INTO page_index_nodes VALUES (?,?,?,?,?,?,?,?)', (key, json.dumps(node), 'Medical_IT', '', 'Active', key, key, 'Source'))
    c.commit()
    clock = [100.]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(projection, 'get_connection', lambda: runtime.track_connection(c))
    catalog = projection._SqliteCatalog()
    monkeypatch.setattr(projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr(projection, 'index_file_stamp', lambda: (0, 1))
    monkeypatch.setenv('VECTOR_LAKE_CANDIDATE_DEPTH', '200')
    monkeypatch.setenv('VECTOR_LAKE_AUTHOR_FACET', 'off')
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [{'node_key': key, 'rank': -1.} for key in keys])
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_rerank_candidates_locally', lambda q, rows: rows)
    monkeypatch.setattr(search, '_vector_projection_error', lambda: None)
    def embedding(q):
        clock[0] += runtime.REQUEST_SECONDS + .1
        return [1., 0.], None
    monkeypatch.setattr(search, '_get_query_embedding', embedding)
    events = []
    monkeypatch.setattr(search.search_ledger, 'record', lambda **event: events.append(event))
    after_expiry_reads = []
    c.set_trace_callback(lambda sql: after_expiry_reads.append(sql) if clock[0] > 115 and 'FROM page_index_nodes' in sql else None)
    yield catalog, keys, events, after_expiry_reads
    c.close()


def test_rrv_01_a_sql_catalog_uses_completed_lexical_snapshot(deadline_catalog):
    catalog, keys, events, late_reads = deadline_catalog
    with runtime.request_scope('needle'):
        rows, notes, error = search._search_scored_pages('needle', 5, domain='Medical_IT', catalog=catalog, allow_external_embedding=True)
    assert error is None and len(rows) == 5
    assert all(node['_key'] in keys for _, node in rows)
    assert not late_reads
    assert notes and events[-1]['status'] == 'partial'


@pytest.mark.parametrize('endpoint', ['assemble', 'preview', 'prepare'])
@pytest.mark.parametrize('catalog_kind', ['sql', 'memory'])
def test_rrv_01_b_context_preserves_summary_without_post_deadline_reads(monkeypatch, tmp_path, deadline_catalog, endpoint, catalog_kind):
    catalog, keys, events, late_reads = deadline_catalog
    if catalog_kind == 'memory':
        snapshot = projection._FileCatalog({'nodes': catalog.nodes_by_key(keys)})
        monkeypatch.setattr(projection, 'read_catalog', lambda: (snapshot, None))
    monkeypatch.setattr(search.governance_store, 'search_memory_packet_views', lambda *a, **k: {})
    monkeypatch.setattr(search, 'build_memory_packet', lambda *a, **k: {'packet': '', 'memory_count': 0, 'warning_count': 0, 'omitted_count': 0, 'is_degraded': False})
    monkeypatch.setattr(runtime, 'read_source', lambda *a, **k: pytest.fail('new source I/O after deadline'))
    monkeypatch.setattr(tool_query, 'get_extension_root', lambda: tmp_path / 'payload-sandbox')
    monkeypatch.setattr(tool_query, 'read_template', lambda *a, **k: 'Context {{payload_path}} for {{query_str}}')
    if endpoint == 'assemble':
        result = search.assemble_context('needle', domain='Medical_IT', allow_external_embedding=True)
        assert result['wiki_page_count'] > 0 and result['retrieval_status'] == 'partial'
        payload = result['wiki_context']
    elif endpoint == 'preview':
        payload = tool_query.preview_query_context('needle', domain='Medical_IT', allow_external_embedding=True)
    else:
        tool_query.prepare_query_context('needle', domain='Medical_IT', allow_external_embedding=True)
        files = list((tmp_path / 'payload-sandbox' / 'tmp').glob('query_context_*.md'))
        assert len(files) == 1
        payload = files[0].read_text(encoding='utf-8')
    assert 'retained lexical summary' in payload and 'index-summary' in payload
    assert any(key in payload for key in keys)
    assert not late_reads and events[-1]['status'] == 'partial'


def test_rrv_term_01_serialized_byte_omission_updates_terminal_state(monkeypatch):
    a = '- **A** (source: Source_A.md:index-summary)\n  ' + '汉' * 1200 + '\n\n'
    b = '- **B** (source: Source_B.md:index-summary)\n  ' + '字' * 1200 + '\n\n'
    context = {'wiki_context': a + b, 'wiki_page_count': 2, 'budget_max': 5000,
               'retrieval_notes': [], '_wiki_spans': [('Source_A', 0), ('Source_B', len(a))]}
    events = []
    monkeypatch.setattr(search.search_ledger, 'record', lambda **event: events.append(event))
    with runtime.request_scope('needle') as state:
        state.emitted_keys = {'Source_A', 'Source_B'}
        runtime.defer_record({'query': 'needle', 'mode': 'context', 'top_k': 2,
                              'returned': [{'key': 'Source_A'}, {'key': 'Source_B'}], 'notes': [], 'status': 'complete'})
        payload = tool_query._render_context_payload('needle', context)
    assert 'Source_A' in payload and 'Source_B' not in payload
    assert len(payload.encode('utf-8')) <= 5000
    assert events[-1]['status'] == 'partial'
    assert [r['key'] for r in events[-1]['returned']] == ['Source_A']
    assert any('Wiki pages omitted' in n for n in events[-1]['notes'])


def test_rrv_term_01_index_omission_diagnostics_do_not_break_small_payloads(monkeypatch):
    context = {'wiki_context': '', 'wiki_page_count': 0, 'memory_packet': 'small memory',
               'index_summary': 'index' * 100, 'budget_max': 230,
               'retrieval_notes': ['vector retrieval unavailable']}
    events = []
    monkeypatch.setattr(search.search_ledger, 'record', lambda **event: events.append(event))
    with runtime.request_scope('needle'):
        runtime.defer_record({'query': 'needle', 'mode': 'context', 'top_k': 1,
                              'returned': [], 'notes': [], 'status': 'complete'})
        payload = tool_query._render_context_payload('needle', context)
    assert len(payload.encode('utf-8')) <= 230 and 'small memory' in payload
    assert events[-1]['status'] == 'partial'
    assert any('Index summary omitted' in n for n in events[-1]['notes'])


@pytest.mark.parametrize('stored,requested,expected', [
    ('Medical_IT', 'Medical_IT', True),
    ('Healthcare_IT', 'Healthcare_IT', True),
    # Registered macro-domain aliases and normalized spelling are an existing
    # page-search contract; get_relations keeps its separate exact-scope API.
    ('Healthcare_IT', 'Medical_IT', True),
    ('Medical_IT', 'Healthcare_IT', True),
    ('medical_it', 'Medical_IT', True),
    (' Medical_IT ', 'Medical_IT', True),
    ('\tMedical_IT\n', 'Medical_IT', True),
    ('\u00a0Medical_IT\u3000', 'Medical_IT', True),
    (' Healthcare_IT ', 'Medical_IT', True),
    ('healthcare_it', 'Medical_IT', False),
    ('Unknown_Domain', 'Medical_IT', False),
    (None, 'Medical_IT', False),
    ('Medical_IT', ' Medical_IT ', True),
    ('Medical_IT', '\tMedical_IT\n', True),
    ('Medical_IT', 'Unknown_Domain', False),
    ('Healthcare_IT', 'Unknown_Domain', False),
])
def test_r5678_01_domain_pushdown_equals_canonical_filter(monkeypatch, stored, requested, expected):
    from vector_lake import db_store
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE VIRTUAL TABLE wiki_search_index USING fts5(node_key UNINDEXED,title,summary,text)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT,status TEXT)')
    c.execute("INSERT INTO wiki_search_index VALUES ('Source_Test','needle','','needle')")
    c.execute('INSERT INTO page_index_nodes VALUES (?,?,?,?)', ('Source_Test', stored, '', 'Active'))
    c.commit()
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
    c.execute('PRAGMA query_only=ON')
    monkeypatch.setattr(db_store, 'get_connection', lambda: c)
    try:
        assert search._passes_filters({'domain': stored, 'status': 'Active'}, requested, None, False, None) is expected
        with runtime.request_scope('needle') as state:
            state.fts_scope = {'domain': requested, 'cluster': None, 'include_history': False}
            rows = search._get_fts_search_results('needle', limit=1)
        assert search._LAST_FTS_ERROR.msg is None
        assert [row['node_key'] for row in rows] == (['Source_Test'] if expected else [])
    finally:
        c.close()


@pytest.mark.parametrize('stored,requested,expected', [
    ('C', 'C', True), ('c', 'C', True), (' C ', 'C', False),
    ('\tC\n', 'C', False), ('\u00a0C\u3000', 'C', False),
    ('C', ' C ', False), ('Unknown', 'C', False), (None, 'C', False),
    ('Économie', 'Économie', True), ('Économie', 'économie', True),
    ('Σύνολο', 'σύνολο', True), ('İstanbul', 'i\u0307stanbul', True),
    (' Économie ', 'Économie', False),
])
def test_cluster_pushdown_is_exact_before_limit(monkeypatch, stored, requested, expected):
    from vector_lake import db_store
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE VIRTUAL TABLE wiki_search_index USING fts5(node_key UNINDEXED,title,summary,text)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT,status TEXT)')
    c.execute("INSERT INTO wiki_search_index VALUES ('Source_Test','needle','','needle')")
    c.execute('INSERT INTO page_index_nodes VALUES (?,?,?,?)', ('Source_Test', 'Medical_IT', stored, 'Active'))
    c.commit()
    c.execute('PRAGMA query_only=ON')
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
    monkeypatch.setattr(db_store, 'get_connection', lambda: c)
    try:
        assert search._passes_filters({'domain': 'Medical_IT', 'topic_cluster': stored, 'status': 'Active'}, 'Medical_IT', requested, False, None) is expected
        with runtime.request_scope('needle') as state:
            state.fts_scope = {'domain': 'Medical_IT', 'cluster': requested, 'include_history': False}
            rows = search._get_fts_search_results('needle', limit=1)
        assert search._LAST_FTS_ERROR.msg is None
        assert [row['node_key'] for row in rows] == (['Source_Test'] if expected else [])
    finally:
        c.close()


@pytest.mark.parametrize('arm', ['lexical', 'vector'])
def test_r5678_02_year_widens_unscoped_history_enabled(monkeypatch, arm):
    from tests.test_retrieval_plans import _isolate
    nodes = {f'old{i}': {'title': f'2023 conference {i}', 'type': 'Event', 'status': 'Active'} for i in range(30)}
    nodes['new'] = {'title': '2026 World Health Summit', 'type': 'Event', 'status': 'Active'}
    catalog = _isolate(monkeypatch, nodes)
    monkeypatch.setattr(search, '_vector_projection_error', lambda: None)
    monkeypatch.setattr(search, '_get_query_embedding', lambda q: ([1., 0.], None))
    monkeypatch.setenv('VECTOR_LAKE_METADATA_FIRST', '0')
    calls = []
    def fts(q, limit=50, **kwargs):
        if arm != 'lexical':
            return []
        calls.append(limit)
        return [{'node_key': k, 'rank': -1.} for k in list(nodes)[:limit]]
    def vectors(v, limit=25):
        if arm != 'vector':
            return {}, None
        calls.append(limit)
        return {k: .9 for k in list(nodes)[:limit]}, None
    monkeypatch.setattr(search, '_get_fts_recall_results', fts)
    monkeypatch.setattr(search, '_get_vector_search_results', vectors)
    rows, _, error = search._search_scored_pages('2026 World Health Summit', 1, catalog=catalog,
        domain=None, include_history=True, profile='balanced', allow_external_embedding=True)
    assert error is None and calls[:2] == [25, 50]
    assert [n['_key'] for _, n in rows] == ['new']


@pytest.mark.parametrize('status', ['Active', 'Decayed', 'Deprecated', 'Archived', ''])
@pytest.mark.parametrize('arm', ['lexical', 'vector'])
def test_r5678_chunk_status_agrees_with_pages(tmp_path, status, arm):
    path = tmp_path / 'chunks.sqlite3'
    body = '# Synthetic\nneedle\n'
    parts = chunks.split_document('Source_Test', body)
    chunks.replace_document(path, 'Source_Test', body, 'Medical_IT', status=status,
                            vectors=[[1., 0.]] * len(parts), model='synthetic')
    rows, _ = chunks.search_chunks('needle' if arm == 'lexical' else 'absenttoken', 'Medical_IT',
        query_vector=[1., 0.] if arm == 'vector' else None, model='synthetic', path=path)
    expected = search._passes_filters({'status': status, 'domain': 'Medical_IT'}, 'Medical_IT', None, False, None)
    assert bool(rows) is expected


def test_r5678_chunk_status_is_applied_before_vector_quota(tmp_path, monkeypatch):
    path = tmp_path / 'chunks.sqlite3'
    monkeypatch.setattr(chunks, 'MAX_CANDIDATES', 2)
    for i in range(3):
        chunks.replace_document(path, f'Source_Archived{i}', 'synthetic', 'Medical_IT',
                                status='archived', vectors=[[1., 0.]], model='synthetic')
    chunks.replace_document(path, 'Source_Retained', 'synthetic', 'Medical_IT',
                            status='decayed', vectors=[[.9, .4]], model='synthetic')
    rows, _ = chunks.search_chunks('absenttoken', 'Medical_IT', query_vector=[1., 0.],
                                   model='synthetic', top_k=1, path=path)
    assert [r['page_key'] for r in rows] == ['Source_Retained']


def test_rrv_term_01_untrusted_summary_cannot_define_serialization_boundaries(monkeypatch):
    wiki = '- **A** (source: Source_A.md:index-summary)\n  ' + '汉' * 1000 + '\n- **FAKE**\n text\n\n'
    context = {'wiki_context': wiki, 'wiki_page_count': 1, 'budget_max': 1800,
               'retrieval_notes': [], '_wiki_spans': [('Source_A', 0)]}
    monkeypatch.setattr(search.search_ledger, 'record', lambda **event: None)
    with runtime.request_scope('needle') as state:
        state.emitted_keys = {'Source_A'}
        payload = tool_query._render_context_payload('needle', context)
        assert 'Source_A' not in payload and not state.emitted_keys
        assert state.problems


@pytest.mark.parametrize('write_path', ['mirrored', 'scorer_only'])
def test_rrv_04_rev_borrowed_transaction_rollback_does_not_reuse_content_identity(monkeypatch, write_path):
    c = vector_database()
    c.execute('CREATE TABLE operational_memory(memory_id TEXT PRIMARY KEY,data_json TEXT)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    c.commit()
    indexes.apply(c)
    monkeypatch.setenv('VECTOR_LAKE_VECTOR_INDEX', 'auto')
    generations = []
    try:
        for values in ([0., 1.] + [0.] * 30, [0., 0., 1.] + [0.] * 29):
            c.execute('BEGIN')
            blob = array('f', values).tobytes()
            if write_path == 'mirrored':
                c.execute('UPDATE vec_emb_float SET embedding=? WHERE page_key=?', (blob, 'Source_Review'))
            c.execute('UPDATE vec_embeddings SET embedding=? WHERE page_key=?', (blob, 'Source_Review'))
            with runtime.request_scope('needle') as state:
                with stage.query_view(c) as view:
                    assert not view.owned and view.lease is None
                    generations.append(state.generations['vectors'])
            c.rollback()
        assert generations[0] != generations[1]
        assert indexes.vector_revision(c) == 0
    finally:
        c.rollback()
        indexes._CACHE.pop(c, None)
        c.close()
