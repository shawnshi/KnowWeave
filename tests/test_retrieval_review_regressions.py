"""Independent-review issue oracles: synthetic SQLite, controlled clocks, no providers."""
from array import array
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import gc
import sqlite3
import weakref

import pytest
import sqlite_vec

from vector_lake import ram_candidate_index as ram, retrieval_runtime as runtime
from vector_lake import scoped_retrieval_index as indexes, two_stage_index as stage
from vector_lake import tool_search as search
from vector_lake.retrieval_plan import plan_query


@pytest.fixture(autouse=True)
def isolated_review_state(monkeypatch):
    ram.clear()
    gc.collect()
    monkeypatch.setattr(search.search_ledger, 'record', lambda *a, **k: None)
    yield
    ram.clear()
    gc.collect()


def float_database():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE vec_emb_float(page_key TEXT PRIMARY KEY,embedding BLOB)')
    c.execute('INSERT INTO vec_emb_float VALUES (?,?)', ('Source_Review', array('f', [1.] + [0.] * 31).tobytes()))
    c.commit()
    return c


@pytest.mark.parametrize('reset', ['clear', 'fork', 'clear_then_fork'])
@pytest.mark.parametrize('retain_view', [False, True])
def test_rsp_ram_01_retained_array_blocks_second_generation(reset, retain_view):
    c = float_database()
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')
    state = {'model': 'synthetic', 'dimension': 32, 'row_count': 1, 'written_at': 'fixed'}
    lease = ram.acquire(c, state)
    assert lease is not None
    snapshot_ref = weakref.ref(lease.snapshot)
    held = [lease.snapshot.matrix[:, :1] if retain_view else lease.snapshot.matrix]
    lease.close()
    lease = None
    replacement = None
    try:
        if reset in {'clear', 'clear_then_fork'}:
            ram.clear()
        if reset in {'fork', 'clear_then_fork'}:
            ram._after_fork()
        gc.collect()
        assert snapshot_ref() is None
        replacement = ram.acquire(c, state)
        assert replacement is None, 'Retained matrix/view must force exact fallback'
        held.clear()
        gc.collect()
        replacement = ram.acquire(c, state)
        assert replacement is not None
    finally:
        held.clear()
        if replacement is not None:
            replacement.close()
        replacement = None
        ram.clear()
        c.close()


class Catalog:
    def __init__(self):
        self.nodes = {'Source_Review': {'title': 'review', 'type': 'Source', 'domain': 'Medical_IT', 'status': 'Active'}}
    def nodes_by_key(self, keys):
        return {k: self.nodes[k] for k in keys if k in self.nodes}
    filter_fields = nodes_by_key
    def adjacency(self):
        return {}


def isolate_lexical(monkeypatch):
    from vector_lake import page_index_projection
    catalog = Catalog()
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr(page_index_projection, 'index_file_stamp', lambda: (0, 1))
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [{'node_key': 'Source_Review', 'rank': -1.}])
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_rerank_candidates_locally', lambda q, rows: rows)
    monkeypatch.setattr(search, '_vector_projection_error', lambda: None)
    search._LAST_FTS_ERROR.msg = None
    return catalog


def test_rrv_01_vector_deadline_preserves_completed_lexical_arm(monkeypatch):
    catalog = isolate_lexical(monkeypatch)
    clock = [100.]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    def embedding(q):
        clock[0] += runtime.REQUEST_SECONDS + .1
        return [1., 0.], None
    monkeypatch.setattr(search, '_get_query_embedding', embedding)
    with runtime.request_scope('review'):
        rows, notes, error = search._search_scored_pages('review', 1, domain='Medical_IT', catalog=catalog, allow_external_embedding=True)
    assert error is None and [n['_key'] for _, n in rows] == ['Source_Review']
    assert any('deadline' in n.lower() or 'RetrievalBudgetExceeded' in n for n in notes)


def test_rrv_01_does_not_swallow_host_cancellation(monkeypatch):
    catalog = isolate_lexical(monkeypatch)
    def cancelled(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(search, '_get_query_embedding', cancelled)
    with pytest.raises(KeyboardInterrupt), runtime.request_scope('review'):
        search._search_scored_pages('review', 1, domain='Medical_IT', catalog=catalog, allow_external_embedding=True)


def test_rrv_02_shared_embedding_wait_uses_remaining_request_time(monkeypatch):
    monkeypatch.setattr(search, '_QUERY_EMBEDDING_CIRCUIT_BREAKER_UNTIL', 0.)
    clock, timeouts = [100.], []
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-not-a-credential')
    key = ('synthetic', 2, 'review-follower')
    monkeypatch.setattr(search, '_embedding_cache_key', lambda q: key)
    class Flight(Future):
        def result(self, timeout=None):
            timeouts.append(timeout)
            raise FutureTimeoutError()
    flight = Flight()
    monkeypatch.setattr(search, '_QUERY_EMBEDDING_CACHE', {})
    monkeypatch.setattr(search, '_QUERY_EMBEDDING_IN_FLIGHT', {key: flight})
    with runtime.request_scope('review-follower'):
        clock[0] += runtime.REQUEST_SECONDS - 1.
        vector, error = search._get_query_embedding('review-follower')
    assert not vector and error
    assert timeouts and 0 <= timeouts[0] <= 1.
    assert not flight.cancelled() and search._QUERY_EMBEDDING_IN_FLIGHT[key] is flight


@pytest.mark.parametrize('original', [0, 30000])
def test_rrv_03_busy_timeout_shrinks_and_restores(monkeypatch, original):
    clock = [100.]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    c = sqlite3.connect(':memory:')
    c.execute(f'PRAGMA busy_timeout={original}')
    try:
        with runtime.request_scope('review'):
            runtime.track_connection(c)
            first = c.execute('PRAGMA busy_timeout').fetchone()[0]
            clock[0] += runtime.REQUEST_SECONDS - 1.
            runtime.track_connection(c)
            later = c.execute('PRAGMA busy_timeout').fetchone()[0]
            assert 0 <= later <= min(original, 1000)
            assert later <= first
        assert c.execute('PRAGMA busy_timeout').fetchone()[0] == original
        assert c.execute('SELECT 1').fetchone()[0] == 1
    finally:
        c.close()


@pytest.mark.parametrize('status', ['Active', 'Decayed', 'Deprecated', 'Archived'])
def test_r5678_01_fts_pushdown_matches_existing_status_filter(monkeypatch, status):
    from vector_lake import db_store
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE VIRTUAL TABLE wiki_search_index USING fts5(node_key UNINDEXED,title,summary,text)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT,status TEXT)')
    c.execute("INSERT INTO wiki_search_index VALUES ('Source_Review','needle','','needle')")
    c.execute('INSERT INTO page_index_nodes VALUES (?,?,?,?)', ('Source_Review', 'Medical_IT', '', status))
    c.commit()
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
    monkeypatch.setattr(db_store, 'get_connection', lambda: c)
    node = {'domain': 'Medical_IT', 'status': status}
    expected = search._passes_filters(node, 'Medical_IT', None, False, None)
    try:
        with runtime.request_scope('needle') as state:
            state.fts_scope = {'domain': 'Medical_IT', 'cluster': None, 'include_history': False}
            rows = search._get_fts_search_results('needle', limit=1)
        assert bool(rows) is expected
    finally:
        c.close()


@pytest.mark.parametrize('query', ['2026 World Health Summit', '2026 HEALTH CONFERENCE'])
def test_r5678_02_english_meeting_year_is_an_eligibility_rule(query):
    plan = plan_query(query)
    assert plan.event_query and plan.event_year == '2026'
    assert not plan.accepts({'type': 'Event', 'title': '2023 World Health Summit'})
    assert plan.accepts({'type': 'Event', 'event_date': '2026-10-01'})
    assert not plan.accepts({'type': 'Product', 'title': 'Summit'})


def test_r5678_02_weak_software_keyword_stays_non_temporal():
    plan = plan_query('2026 Discourse论坛系统的优势和不足')
    assert plan.event_year is None and plan.accepts({'type': 'Product', 'title': 'Discourse'})


def accelerator_database():
    c = float_database()
    c.execute('CREATE TABLE operational_memory(memory_id TEXT PRIMARY KEY,data_json TEXT)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    c.commit()
    return c


@pytest.mark.parametrize('collision', ['wrong_table', 'wrong_table_uppercase', 'wrong_expression', 'wrong_json_path', 'wrong_trigger', 'wrong_revision_table'])
def test_r5678_03_incompatible_managed_schema_is_not_available_or_repaired(collision):
    c = accelerator_database()
    try:
        if collision == 'wrong_table':
            c.execute('CREATE INDEX idx_rsp_memory_domain ON page_index_nodes(domain)')
        elif collision == 'wrong_table_uppercase':
            c.execute('CREATE INDEX IDX_RSP_MEMORY_DOMAIN ON page_index_nodes(domain)')
        elif collision == 'wrong_expression':
            c.execute('CREATE INDEX idx_rsp_memory_domain ON operational_memory(memory_id)')
        elif collision == 'wrong_json_path':
            c.execute("CREATE INDEX idx_rsp_memory_domain ON operational_memory(lower(COALESCE(NULLIF(json_extract(data_json, '$.do main'), ''), '')))")
        elif collision == 'wrong_trigger':
            c.execute('CREATE TRIGGER rsp_vector_update AFTER UPDATE ON vec_emb_float BEGIN SELECT 1; END')
        else:
            c.execute('CREATE TABLE retrieval_vector_revision(singleton TEXT,revision TEXT)')
        c.commit()
        before = list(c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
        with pytest.raises(ValueError, match='[Ii]ncompatible'):
            indexes.apply(c)
        assert list(c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')) == before
        assert not c.in_transaction
        status = indexes.status(c)
        assert not status['memory_acceleration'] and not status['vector_revision']
        assert indexes.scoped_sql(c, ['medical_it']) is None
    finally:
        indexes._CACHE.pop(c, None)
        c.close()


def vector_database():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    c.execute('CREATE TABLE vec_embedding_projection(id INTEGER PRIMARY KEY,model TEXT,dimension INTEGER)')
    c.execute("INSERT INTO vec_embedding_projection VALUES (1,'synthetic',32)")
    c.execute('CREATE VIRTUAL TABLE vec_embeddings USING vec0(page_key TEXT PRIMARY KEY,embedding FLOAT[32])')
    c.execute('INSERT INTO vec_embeddings VALUES (?,?)', ('Source_Review', array('f', [1.] + [0.] * 31).tobytes()))
    stage.build(c)
    c.commit()
    return c


@pytest.mark.parametrize('backend', ['ram', 'two_stage', 'busy_auto'])
def test_rrv_04_no_revision_generation_tracks_content_not_only_row_count(monkeypatch, backend):
    c = vector_database()
    monkeypatch.setenv('VECTOR_LAKE_VECTOR_INDEX', 'auto' if backend == 'busy_auto' else backend)
    if backend == 'busy_auto':
        for _ in range(ram.MAX_ACTIVE_VIEWS):
            assert ram._SLOTS.acquire(False)
    generations = []
    try:
        for values in ([1.] + [0.] * 31, [0., 1.] + [0.] * 30):
            blob = array('f', values).tobytes()
            c.execute('UPDATE vec_emb_float SET embedding=? WHERE page_key=?', (blob, 'Source_Review'))
            c.execute('UPDATE vec_embeddings SET embedding=? WHERE page_key=?', (blob, 'Source_Review'))
            c.commit()
            with runtime.request_scope('review') as state:
                with stage.query_view(c):
                    generations.append(state.generations.get('vectors'))
        assert generations[0] is not None and generations[0] != generations[1]
    finally:
        if backend == 'busy_auto':
            for _ in range(ram.MAX_ACTIVE_VIEWS):
                ram._SLOTS.release()
        ram.clear()
        c.close()
