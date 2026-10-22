import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import pytest

from vector_lake import governance_metrics, governance_store as store, retrieval_runtime as runtime


@pytest.fixture(autouse=True)
def scoring_environment(monkeypatch):
    monkeypatch.setenv('VECTOR_LAKE_SEARCH_LEDGER', '0')
    monkeypatch.setattr(store, '_PURPOSE_VECTORS_CACHE', None)
    monkeypatch.setattr(store, '_PURPOSE_VECTORS_MTIME', 0)


def purpose_file(root, keyword, *, stamp=1700000000):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'purpose_vectors.json'
    path.write_text(json.dumps({'keywords': [keyword], 'weight_boost': 0.25}), encoding='utf-8')
    os.utime(path, (stamp, stamp))
    return path


def test_purpose_is_pinned_per_request_and_reloaded_next_request(tmp_path, monkeypatch):
    path = purpose_file(tmp_path / 'meta', 'before')
    monkeypatch.setattr(store, 'get_meta_dir', lambda: path.parent)
    with runtime.request_scope('synthetic scoring'):
        before = store.get_purpose_vectors()
        purpose_file(path.parent, 'after', stamp=1700000010)
        assert store.get_purpose_vectors() == before
    with runtime.request_scope('synthetic scoring'):
        assert store.get_purpose_vectors()['keywords'] == ['after']


def test_purpose_cache_separates_roots_with_equal_mtime(tmp_path, monkeypatch):
    a = purpose_file(tmp_path / 'a', 'alpha')
    b = purpose_file(tmp_path / 'b', 'beta')
    monkeypatch.setattr(store, 'get_meta_dir', lambda: a.parent)
    assert store.get_purpose_vectors()['keywords'] == ['alpha']
    monkeypatch.setattr(store, 'get_meta_dir', lambda: b.parent)
    assert store.get_purpose_vectors()['keywords'] == ['beta']


def test_purpose_does_not_stat_again_inside_request(tmp_path, monkeypatch):
    path = purpose_file(tmp_path / 'meta', 'fixed')
    monkeypatch.setattr(store, 'get_meta_dir', lambda: path.parent)
    original = type(path).stat
    calls = []
    def stat(self, *args, **kwargs):
        if self == path:
            calls.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(path), 'stat', stat)
    with runtime.request_scope('synthetic scoring'):
        store.get_purpose_vectors()
        first = len(calls)
        for _ in range(12):
            store.get_purpose_vectors()
        assert len(calls) == first


def test_sql_state_and_score_share_one_evaluation(monkeypatch):
    calls = []
    def current(memory, now=None):
        calls.append((memory['memory_id'], now))
        return {**memory, 'validity_state': 'active', 'memory_score': 0.75}
    monkeypatch.setattr(store, '_current_memory', current)
    conn = sqlite3.connect(':memory:')
    payload = json.dumps({'memory_id': 'm', 'text': 'synthetic public value'})
    try:
        with runtime.request_scope('synthetic scoring'):
            store._query_memory_functions(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert conn.execute('SELECT memory_current_state(?),memory_current_score(?)', (payload, payload)).fetchone() == ('active', 0.75)
            assert conn.execute('SELECT memory_current_score(?)', (payload,)).fetchone()[0] == 0.75
            assert len(calls) == 1
    finally:
        conn.close()


def test_sql_cache_distinguishes_payload_and_scoring_time(monkeypatch):
    calls = []
    def current(memory, now=None):
        calls.append((memory['memory_score'], now))
        return {**memory, 'validity_state': 'active'}
    monkeypatch.setattr(store, '_current_memory', current)
    conn = sqlite3.connect(':memory:')
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        with runtime.request_scope('synthetic scoring'):
            for scoring_time, score in ((now, 0.1), (now, 0.2), (now + timedelta(days=1), 0.2)):
                store._query_memory_functions(conn, scoring_time)
                raw = json.dumps({'memory_id': 'same', 'memory_score': score})
                assert conn.execute('SELECT memory_current_score(?)', (raw,)).fetchone()[0] == score
            assert len(calls) == 3
    finally:
        conn.close()


def test_sql_cache_has_fixed_capacity_and_is_cleared_on_exception(monkeypatch):
    monkeypatch.setattr(store, '_current_memory', lambda m, now=None: {**m, 'validity_state': 'active'})
    conn = sqlite3.connect(':memory:')
    try:
        with pytest.raises(RuntimeError):
            with runtime.request_scope('synthetic scoring') as state:
                store._query_memory_functions(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
                for i in range(4100):
                    conn.execute('SELECT memory_current_score(?)', (json.dumps({'memory_id': str(i), 'memory_score': 0.5}),)).fetchone()
                assert len(state.memory_evaluations) <= 4096
                raise RuntimeError('synthetic abort')
        assert not state.memory_evaluations
        assert state.purpose_snapshot is None
        assert runtime.current() is None
    finally:
        conn.close()


def test_sql_errors_are_not_cached_as_empty_scores():
    conn = sqlite3.connect(':memory:')
    try:
        with runtime.request_scope('synthetic scoring'):
            store._query_memory_functions(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
            with pytest.raises(sqlite3.OperationalError):
                conn.execute('SELECT memory_current_score(?)', ('{broken',)).fetchone()
            assert conn.execute('SELECT memory_current_score(?)', (json.dumps({'memory_id': 'ok', 'memory_score': 0.4, 'validity_state': 'active'}),)).fetchone()[0] == 0.4
    finally:
        conn.close()


def test_scoring_time_is_request_local_and_explicit_now_is_preserved(monkeypatch):
    ticks = iter([datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)])
    monkeypatch.setattr(governance_metrics, '_utc_now', lambda: next(ticks))
    with runtime.request_scope('synthetic scoring'):
        assert store._memory_now() == store._memory_now()
    with runtime.request_scope('synthetic scoring'):
        assert store._memory_now().day == 2


def test_purpose_snapshots_are_isolated_across_concurrent_requests(tmp_path, monkeypatch):
    paths = [purpose_file(tmp_path / str(i), str(i)) for i in range(4)]
    root = ContextVar('synthetic_root')
    monkeypatch.setattr(store, 'get_meta_dir', lambda: root.get())
    def worker(i):
        root.set(paths[i].parent)
        with runtime.request_scope('synthetic scoring'):
            return [store.get_purpose_vectors()['keywords'] for _ in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(worker, range(4)))
    assert values == [[[str(i)]] * 8 for i in range(4)]


def test_explicit_sql_scoring_time_overrides_the_request_clock(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(governance_metrics, '_utc_now', lambda: now)
    raw = json.dumps({'memory_id': 'future', 'validity_state': 'active', 'memory_score': 0.4,
                      'valid_from': '2026-01-02T00:00:00+00:00'})
    conn = sqlite3.connect(':memory:')
    try:
        with runtime.request_scope('synthetic scoring'):
            assert store._memory_now() == now
            store._query_memory_functions(conn, now)
            assert conn.execute('SELECT memory_current_state(?)', (raw,)).fetchone()[0] == 'not-yet-valid'
            store._query_memory_functions(conn, now + timedelta(days=2))
            assert conn.execute('SELECT memory_current_state(?)', (raw,)).fetchone()[0] == 'active'
    finally:
        conn.close()


def test_missing_and_malformed_purpose_keep_existing_default(tmp_path, monkeypatch):
    root = tmp_path / 'meta'
    root.mkdir()
    monkeypatch.setattr(store, 'get_meta_dir', lambda: root)
    with runtime.request_scope('synthetic scoring'):
        assert store.get_purpose_vectors() == {'keywords': [], 'weight_boost': 0.0}
    (root / 'purpose_vectors.json').write_text('{broken', encoding='utf-8')
    with runtime.request_scope('synthetic scoring'):
        assert store.get_purpose_vectors() == {'keywords': [], 'weight_boost': 0.0}


def test_sql_evaluation_does_not_persist_outside_request(monkeypatch):
    calls = []
    def current(memory, now=None):
        calls.append(memory)
        return {**memory, 'validity_state': 'active'}
    monkeypatch.setattr(store, '_current_memory', current)
    conn = sqlite3.connect(':memory:')
    raw = json.dumps({'memory_id': 'outside', 'memory_score': 0.4})
    try:
        store._query_memory_functions(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
        for _ in range(2):
            conn.execute('SELECT memory_current_score(?)', (raw,)).fetchone()
        assert len(calls) == 2
    finally:
        conn.close()


def test_root_switch_drops_scores_derived_from_the_previous_purpose(tmp_path, monkeypatch):
    a = purpose_file(tmp_path / 'a', 'alpha')
    b = purpose_file(tmp_path / 'b', 'beta')
    root = [a.parent]
    monkeypatch.setattr(store, 'get_meta_dir', lambda: root[0])
    monkeypatch.setattr(store, '_current_memory', lambda m, now=None: {
        **m, 'validity_state': store.get_purpose_vectors()['keywords'][0], 'memory_score': 0.5})
    conn = sqlite3.connect(':memory:')
    raw = json.dumps({'memory_id': 'same'})
    try:
        with runtime.request_scope('synthetic scoring'):
            store._query_memory_functions(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert conn.execute('SELECT memory_current_state(?)', (raw,)).fetchone()[0] == 'alpha'
            root[0] = b.parent
            assert conn.execute('SELECT memory_current_state(?)', (raw,)).fetchone()[0] == 'beta'
            purpose_file(a.parent, 'changed', stamp=1700000010)
            root[0] = a.parent
            assert conn.execute('SELECT memory_current_state(?)', (raw,)).fetchone()[0] == 'changed'
    finally:
        conn.close()
