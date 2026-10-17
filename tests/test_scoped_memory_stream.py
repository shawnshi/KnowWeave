"""Exact scoped ranking without materializing the complete authorized domain.

All payloads are synthetic. The legacy full-sort scorer is an independent oracle
for scoped DF, filtering and ordering, not a production fallback.
"""
import json
import sqlite3

import pytest

from vector_lake import governance_store as store
from vector_lake import scoped_retrieval_index as indexes


def database(count=80, padding=0, foreign=30, path=':memory:'):
    c = sqlite3.connect(str(path))
    c.execute('CREATE TABLE operational_memory(memory_id TEXT PRIMARY KEY,data_json TEXT)')
    c.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    c.execute('CREATE TABLE vec_emb_float(page_key TEXT PRIMARY KEY,embedding BLOB)')
    c.execute("INSERT INTO page_index_nodes VALUES ('Source_Parent','Medical_IT','target')")
    for i in range(count + foreign):
        item = {
            'memory_id': f'memory_{i:08d}', 'key': f'synthetic_{i}',
            'text': ('scarce ' if i % 17 == 0 or i == count - 1 else '') + 'common ' + 'x' * padding,
            'memory_score': (i % 7) / 10, 'updated_at': f'2026-09-{i % 28 + 1:02d}T00:00:00+00:00',
            'memory_type': 'fact' if i % 3 else 'decision',
            'validity_state': 'superseded' if i % 11 == 0 else 'active',
            'topic_cluster': 'target', 'source_page': 'Source_Parent.md',
        }
        if i >= count:
            item['domain'] = 'General'
            item['text'] = 'scarce ' + item['text']
        elif i % 4:
            item['domain'] = 'Healthcare_IT' if i % 2 else 'Medical_IT'
        if i == count - 1:
            item['memory_score'] = 1.0
            item['updated_at'] = '2099-01-01T00:00:00+00:00'
        c.execute('INSERT INTO operational_memory VALUES (?,?)', (item['memory_id'], json.dumps(item)))
    c.commit()
    return c


def reference(c, query, top_k=12, include_history=False, allowed_types=None):
    # Explicit synthetic oracle: inherited and direct domains are known above.
    rows = [json.loads(r[0]) for r in c.execute('SELECT data_json FROM operational_memory ORDER BY rowid')]
    scoped = [m for m in rows if m.get('domain') != 'General']
    terms = store._query_terms(query)
    return store.score_memory_items(scoped, terms, top_k, include_history=include_history,
                                    allowed_types=allowed_types,
                                    df=store._memory_term_frequencies(scoped, terms), total=len(scoped))


@pytest.mark.parametrize('accelerated', [False, True])
@pytest.mark.parametrize('padding', [0, 1800])
def test_domain_beyond_old_row_and_byte_caps_is_ranked_exactly(monkeypatch, accelerated, padding):
    c = database(count=11020, padding=padding)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    try:
        if accelerated:
            indexes.apply(c)
        expected = reference(c, 'scarce common')
        expected_history = reference(c, 'scarce common', include_history=True)
        assert store.search_operational_memory('scarce common', top_k=12, domain='Medical_IT') == expected
        active, history = store.search_memory_packet_views('scarce common', active_top_k=12,
                                                         history_top_k=12, domain='Medical_IT')
        assert active == expected
        assert history == expected_history
        assert any(m['memory_id'] == 'memory_00011019' for m in active)
        assert not c.in_transaction
    finally:
        c.close()


@pytest.mark.parametrize('query', ['scarce common', 'fact', '!!!', '合成 医疗'])
@pytest.mark.parametrize('include_history', [False, True])
def test_existing_df_type_history_and_ordering_are_unchanged(monkeypatch, query, include_history):
    c = database()
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    try:
        expected = reference(c, query, top_k=8, include_history=include_history, allowed_types={'fact'})
        assert store.search_operational_memory(query, top_k=8, domain='Healthcare_IT',
                                              memory_types=['FACT'], include_history=include_history) == expected
    finally:
        c.close()


def test_reader_preserves_schema_connection_flags_and_caller_transaction(monkeypatch):
    c = database()
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    before = list(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    flags = c.execute('PRAGMA query_only').fetchone()[0]
    try:
        store.search_operational_memory('scarce', domain='Medical_IT')
        assert not c.in_transaction
        assert c.execute('PRAGMA query_only').fetchone()[0] == flags
        assert list(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name')) == before
        c.execute('BEGIN')
        c.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.memory_score',0.99) WHERE memory_id='memory_00000017'")
        expected = reference(c, 'scarce')
        assert store.search_operational_memory('scarce', top_k=12, domain='Medical_IT') == expected
        assert c.in_transaction
        assert c.execute("SELECT json_extract(data_json,'$.memory_score') FROM operational_memory WHERE memory_id='memory_00000017'").fetchone()[0] == .99
        c.rollback()
        assert c.execute("SELECT json_extract(data_json,'$.memory_score') FROM operational_memory WHERE memory_id='memory_00000017'").fetchone()[0] != .99
    finally:
        c.close()


def test_both_views_share_df_and_have_independent_returned_payloads(monkeypatch):
    c = database()
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    original = store._memory_term_frequencies
    calls = []
    def count_df(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, '_memory_term_frequencies', count_df)
    try:
        active, history = store.search_memory_packet_views('scarce', active_top_k=12,
                                                         history_top_k=12, domain='Medical_IT')
        assert calls == [1]
        common = set(m['memory_id'] for m in active) & set(m['memory_id'] for m in history)
        assert common
        key = sorted(common)[0]
        a = next(m for m in active if m['memory_id'] == key)
        h = next(m for m in history if m['memory_id'] == key)
        a['text'] = 'caller mutation'
        assert h['text'] != 'caller mutation'
    finally:
        c.close()


def test_df_and_rank_use_one_snapshot_while_another_connection_commits(monkeypatch, tmp_path):
    path = tmp_path / 'snapshot.sqlite3'
    c = database(path=path)
    c.execute('PRAGMA journal_mode=WAL')
    expected = reference(c, 'scarce')
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    original = store._memory_term_frequencies
    def concurrent_commit(*args, **kwargs):
        df = original(*args, **kwargs)
        other = sqlite3.connect(str(path))
        try:
            other.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.text','concurrent mutation')")
            other.commit()
        finally:
            other.close()
        return df
    monkeypatch.setattr(store, '_memory_term_frequencies', concurrent_commit)
    try:
        assert store.search_operational_memory('scarce', top_k=12, domain='Medical_IT') == expected
        assert not c.in_transaction
        assert c.execute("SELECT json_extract(data_json,'$.text') FROM operational_memory LIMIT 1").fetchone()[0] == 'concurrent mutation'
    finally:
        c.close()


@pytest.mark.parametrize('accelerated', [False, True])
def test_sql_projection_refuses_oversized_payload_transfer(monkeypatch, accelerated):
    c = database(count=2, foreign=0, padding=1000)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    try:
        if accelerated:
            indexes.apply(c)
        rows = list(store._iter_scoped_memory_payloads(c, 'Medical_IT', None, max_payload_bytes=100))
        assert len(rows) == 2
        assert all(raw is None and size > 100 for raw, size in rows)
    finally:
        c.close()


@pytest.mark.parametrize('limit', ['rows', 'bytes', 'result_rows', 'result_bytes'])
def test_budget_exhaustion_is_explicit_not_truncated_or_unscoped(monkeypatch, limit):
    from vector_lake import scoped_memory_search as stream, retrieval_runtime as runtime
    c = database(count=6, foreign=0)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    monkeypatch.setattr(store, '_load_memory_items', lambda: pytest.fail('unscoped fallback'))
    if limit == 'rows':
        monkeypatch.setattr(stream, 'MAX_SCANNED_ROWS', 8)
    elif limit == 'bytes':
        monkeypatch.setattr(stream, 'MAX_SCANNED_BYTES', 10)
    elif limit == 'result_rows':
        monkeypatch.setattr(stream, 'MAX_RETAINED_ROWS', 1)
    else:
        largest = c.execute('SELECT max(length(CAST(data_json AS BLOB))) FROM operational_memory').fetchone()[0]
        monkeypatch.setattr(stream, 'MAX_RETAINED_BYTES', largest + 1)
    try:
        with pytest.raises(runtime.RetrievalBudgetExceeded, match='budget exhausted'):
            store.search_operational_memory('!!!', top_k=4, domain='Medical_IT', include_history=True)
        assert not c.in_transaction
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM operational_memory').fetchone()[0] == 6
    finally:
        c.close()


@pytest.mark.parametrize('borrowed', [False, True])
@pytest.mark.parametrize('failure', [ValueError, KeyboardInterrupt])
def test_df_failure_closes_streams_and_preserves_transaction_ownership(monkeypatch, borrowed, failure):
    c = database(count=6)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    original = store._iter_scoped_memory_payloads
    closed = []
    def observed(*args, **kwargs):
        source = original(*args, **kwargs)
        try:
            yield from source
        finally:
            source.close()
            closed.append(True)
    def fail_df(memories, terms):
        next(iter(memories))
        raise failure('synthetic DF failure')
    monkeypatch.setattr(store, '_iter_scoped_memory_payloads', observed)
    monkeypatch.setattr(store, '_memory_term_frequencies', fail_df)
    if borrowed:
        c.execute('BEGIN')
    try:
        with pytest.raises(failure, match='synthetic DF failure'):
            store.search_operational_memory('scarce', domain='Medical_IT')
        assert closed == [True]
        assert c.in_transaction is borrowed
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
    finally:
        c.close()


def test_caller_transaction_cannot_be_mutated_between_the_two_passes(monkeypatch):
    c = database(count=6)
    c.execute('BEGIN')
    c.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.memory_score',0.88)")
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    original = store._memory_term_frequencies
    def mutate_after_df(*args, **kwargs):
        df = original(*args, **kwargs)
        c.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.text','wrong generation')")
        return df
    monkeypatch.setattr(store, '_memory_term_frequencies', mutate_after_df)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            store.search_operational_memory('scarce', domain='Medical_IT')
        assert c.in_transaction
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
        row = c.execute("SELECT json_extract(data_json,'$.text'),json_extract(data_json,'$.memory_score') FROM operational_memory LIMIT 1").fetchone()
        assert row[0] != 'wrong generation' and row[1] == .88
        c.rollback()
    finally:
        c.close()


def test_retracks_sql_timeout_after_frequency_pass(monkeypatch):
    from vector_lake import retrieval_runtime as runtime
    c = database(count=6)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    clock = [100.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    original_df, original_track = store._memory_term_frequencies, runtime.track_connection
    timeouts = []
    def delayed_df(*args, **kwargs):
        result = original_df(*args, **kwargs)
        clock[0] += 13.5
        return result
    def track(conn):
        result = original_track(conn)
        timeouts.append(conn.execute('PRAGMA busy_timeout').fetchone()[0])
        return result
    monkeypatch.setattr(store, '_memory_term_frequencies', delayed_df)
    monkeypatch.setattr(runtime, 'track_connection', track)
    before = c.execute('PRAGMA busy_timeout').fetchone()[0]
    try:
        with runtime.request_scope('scarce'):
            assert store.search_operational_memory('scarce', domain='Medical_IT')
        assert timeouts[0] == before
        assert timeouts[-1] <= 1500
        assert c.execute('PRAGMA busy_timeout').fetchone()[0] == before
        assert not c.in_transaction
    finally:
        c.close()


def test_deadline_after_df_reports_failure_and_releases_snapshot(monkeypatch):
    from vector_lake import retrieval_runtime as runtime, search_ledger
    c = database(count=6)
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    clock = [100.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    records = []
    monkeypatch.setattr(search_ledger, 'record', lambda **kwargs: records.append(kwargs))
    original = store._memory_term_frequencies
    def expired_df(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 16
        return result
    monkeypatch.setattr(store, '_memory_term_frequencies', expired_df)
    try:
        with pytest.raises(runtime.RetrievalBudgetExceeded), runtime.request_scope('scarce'):
            store.search_operational_memory('scarce', domain='Medical_IT')
        assert records[-1]['status'] == 'failed'
        assert not c.in_transaction
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
    finally:
        c.close()


def test_stable_full_sort_ties_preserve_row_order_even_for_duplicate_json_ids(monkeypatch):
    c = database(count=8, foreign=0)
    c.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.memory_id','duplicate','$.memory_score','not-numeric','$.updated_at','invalid')")
    c.commit()
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    try:
        expected = reference(c, '!!!', top_k=5, include_history=True)
        result = store.search_operational_memory('!!!', top_k=5, domain='Medical_IT', include_history=True)
        assert result == expected
        assert [m['key'] for m in result] == [f'synthetic_{i}' for i in range(5)]
    finally:
        c.close()


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-inf'])
def test_nonfinite_scores_are_explicit_data_failures_not_unordered_rankings(monkeypatch, value):
    # Do not change shared/unscoped score coercion to make heap ordering convenient.
    c = database(count=2, foreign=0)
    c.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.memory_score',?) WHERE memory_id='memory_00000001'", (value,))
    c.commit()
    monkeypatch.setattr(store, 'get_connection', lambda: c)
    try:
        with pytest.raises(ValueError, match='Non-finite scoped memory score'):
            store.search_operational_memory('scarce', domain='Medical_IT')
        assert not c.in_transaction
        assert c.execute('PRAGMA query_only').fetchone()[0] == 0
    finally:
        c.close()
