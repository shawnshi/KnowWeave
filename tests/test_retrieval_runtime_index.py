"""Explicit index acceleration, revision invariants and request-boundary oracles."""
import json
import sqlite3
from array import array

import pytest
from vector_lake import scoped_retrieval_index as indexes, retrieval_runtime as runtime
from vector_lake import governance_store, ram_candidate_index as ram


def memory_database():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE operational_memory(memory_id TEXT PRIMARY KEY,data_json TEXT)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    c.execute('CREATE TABLE vec_emb_float(page_key TEXT PRIMARY KEY,embedding BLOB)')
    c.execute("INSERT INTO page_index_nodes VALUES ('Source_Parent','Medical_IT','target')")
    for i in range(200):
        item = {'memory_id': str(i), 'domain': 'Medical_IT' if i % 10 == 0 else 'General', 'topic_cluster': 'target'}
        c.execute('INSERT INTO operational_memory VALUES (?,?)', (str(i), json.dumps(item)))
    c.execute('INSERT INTO operational_memory VALUES (?,?)', ('fallback', json.dumps({'memory_id': 'fallback', 'source_page': 'Source_Parent.md'})))
    c.commit()
    return c


def test_reader_never_creates_accelerators(monkeypatch):
    c = memory_database()
    monkeypatch.setattr(governance_store, 'get_connection', lambda: c)
    before = list(c.execute('SELECT name FROM sqlite_master ORDER BY name'))
    assert len(governance_store._scoped_memory_items('Medical_IT', None)) == 21
    assert indexes.scoped_sql(c, ['medical_it']) is None
    assert list(c.execute('SELECT name FROM sqlite_master ORDER BY name')) == before


def test_explicit_indexes_preserve_scope_and_remove_full_memory_scan(monkeypatch):
    c = memory_database()
    monkeypatch.setattr(governance_store, 'get_connection', lambda: c)
    expected = governance_store._scoped_memory_items('Medical_IT', 'target')
    assert indexes.apply(c)['memory_acceleration']
    assert governance_store._scoped_memory_items('Medical_IT', 'target') == expected
    sql, params = indexes.scoped_sql(c, ['medical_it'], 'target')
    plan = [r[3] for r in c.execute('EXPLAIN QUERY PLAN ' + sql, params)]
    assert not any('SCAN m' in detail for detail in plan)
    assert any('idx_rsp_memory_domain' in detail for detail in plan)
    assert any('idx_rsp_memory_source' in detail for detail in plan)
    assert len(governance_store._scoped_memory_items('General', None)) == 180


def test_accelerator_apply_failure_rolls_back_schema():
    c = memory_database()
    c.execute("INSERT INTO operational_memory VALUES ('bad','not-json')")
    c.commit()
    with pytest.raises(sqlite3.DatabaseError):
        indexes.apply(c)
    names = {r[0] for r in c.execute('SELECT name FROM sqlite_master')}
    assert not set(indexes.INDEXES) & names
    assert not c.in_transaction


def test_revision_covers_raw_writes_and_rollback_but_not_unrelated_tables():
    c = memory_database()
    indexes.apply(c)
    initial = indexes.vector_revision(c)
    c.execute("INSERT INTO operational_memory VALUES ('noise','{}')")
    c.commit()
    assert indexes.vector_revision(c) == initial
    c.execute('INSERT INTO vec_emb_float VALUES (?,?)', ('Source_Test', array('f', [1.0] + [0.0] * 31).tobytes()))
    c.commit()
    after_insert = indexes.vector_revision(c)
    assert after_insert == initial + 1
    c.execute("UPDATE vec_emb_float SET embedding=? WHERE page_key='Source_Test'", (array('f', [0.0, 1.0] + [0.0] * 30).tobytes(),))
    assert indexes.vector_revision(c) == after_insert + 1
    c.rollback()
    assert indexes.vector_revision(c) == after_insert


def test_revision_avoids_unrelated_write_recheck_without_stale_vectors(monkeypatch):
    c = memory_database()
    indexes.apply(c)
    c.execute('INSERT INTO vec_emb_float VALUES (?,?)', ('Source_Test', array('f', [1.0] + [0.0] * 31).tobytes()))
    c.commit()
    ram.clear()
    checks = []
    original = ram._matches_current
    def checked(*a):
        checks.append(1)
        return original(*a)
    monkeypatch.setattr(ram, '_matches_current', checked)
    def acquire():
        c.execute('BEGIN')
        state = {'model': 'synthetic', 'dimension': 32, 'row_count': 1, 'written_at': 'fixed', 'vector_revision': indexes.vector_revision(c)}
        lease = ram.acquire(c, state)
        assert lease is not None
        lease.close()
        c.rollback()
    try:
        acquire()
        c.execute("INSERT INTO operational_memory VALUES ('unrelated','{}')")
        c.commit()
        acquire()
        assert checks == []
    finally:
        ram.clear()
        c.close()


def test_sql_deadline_is_enforced_and_handler_restored(monkeypatch):
    c = sqlite3.connect(':memory:')
    original = c.execute('PRAGMA busy_timeout').fetchone()[0]
    monkeypatch.setattr(runtime, 'REQUEST_SECONDS', .002)
    with runtime.request_scope('synthetic'):
        runtime.track_connection(c)
        with pytest.raises(sqlite3.OperationalError):
            c.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<10000000) SELECT sum(n) FROM x').fetchone()
    assert c.execute('PRAGMA busy_timeout').fetchone()[0] == original
    assert c.execute('SELECT 1').fetchone()[0] == 1


def test_terminal_ledger_includes_work_after_candidate_scoring(monkeypatch):
    from vector_lake import search_ledger
    clock = [10.0]
    records = []
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(search_ledger, 'record', lambda **k: records.append(k))
    with runtime.request_scope('synthetic'):
        runtime.defer_record({'query': 'synthetic', 'mode': 'page', 'top_k': 1, 'returned': [], 'status': 'complete'})
        clock[0] += .075
    assert records[0]['elapsed_ms'] == pytest.approx(75)
    assert records[0]['timings']['endpoint_total'] == pytest.approx(75)


def test_scoped_fts_filters_before_candidate_limit(monkeypatch):
    from vector_lake import tool_search, db_store
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE VIRTUAL TABLE wiki_search_index USING fts5(node_key UNINDEXED,title,summary,text)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT,status TEXT)')
    for i in range(100):
        key = f'Source_Foreign{i}'
        c.execute('INSERT INTO wiki_search_index VALUES (?,?,?,?)', (key, 'needle', '', 'needle needle needle'))
        c.execute('INSERT INTO page_index_nodes VALUES (?,?,?,?)', (key, 'General', '', 'Active'))
    c.execute("INSERT INTO wiki_search_index VALUES ('Source_Allowed','needle','','needle')")
    c.execute("INSERT INTO page_index_nodes VALUES ('Source_Allowed','Medical_IT','','Active')")
    c.commit()
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
    monkeypatch.setattr(db_store, 'get_connection', lambda: c)
    with runtime.request_scope('needle') as state:
        state.fts_scope = {'domain': 'Medical_IT', 'cluster': None, 'include_history': False}
        rows = tool_search._get_fts_search_results('needle', limit=1)
    assert [r['node_key'] for r in rows] == ['Source_Allowed']


def test_ledger_rotation_and_append_are_serialized(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from vector_lake import search_ledger
    path = tmp_path / 'ledger.jsonl'
    monkeypatch.setenv('VECTOR_LAKE_SEARCH_LEDGER', '1')
    monkeypatch.setattr(search_ledger, 'ledger_path', lambda: path)
    monkeypatch.setattr(search_ledger, 'MAX_BYTES', 1000000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: search_ledger.record(str(i), mode='page', top_k=1, returned=[], status='complete'), range(40)))
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 40 and all(row['status'] == 'complete' for row in rows)


def test_failed_request_records_error_not_no_match(monkeypatch):
    from vector_lake import search_ledger
    records = []
    monkeypatch.setattr(search_ledger, 'record', lambda **k: records.append(k))
    with pytest.raises(RuntimeError), runtime.request_scope('synthetic'):
        raise RuntimeError('DO_NOT_EXPOSE_REQUEST_DETAILS')
    assert records[0]['status'] == 'failed' and records[0]['error'] == 'retrieval_failed'
    assert 'DO_NOT_EXPOSE' not in str(records)
