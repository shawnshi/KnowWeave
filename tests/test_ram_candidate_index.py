"""Real SQLite/NumPy fixtures; no canonical knowledge DB or external provider."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from vector_lake import (
    db_store,
    ram_candidate_index as ram,
    tool_search,
    two_stage_index as stage,
)

DIM = 32


@pytest.fixture(autouse=True)
def reset_ram(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "auto")
    ram.clear()
    yield
    ram.clear()


def _connection(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "ram.sqlite"
    conn = _connection(path)
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_embeddings USING vec0(page_key TEXT PRIMARY KEY,embedding float[{DIM}])"
    )
    conn.execute(
        "CREATE TABLE vec_embedding_projection(id INTEGER PRIMARY KEY,model TEXT,dimension INTEGER)"
    )
    conn.execute(
        "INSERT INTO vec_embedding_projection VALUES(1,'synthetic-v1',?)", (DIM,)
    )
    rng = np.random.default_rng(3072)
    base = rng.standard_normal(DIM, dtype=np.float32)
    base /= np.linalg.norm(base)
    vectors = base + 0.04 * rng.standard_normal((96, DIM), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    keys = [f"p_{i:03d}" for i in range(len(vectors))]
    conn.executemany(
        "INSERT INTO vec_embeddings VALUES(?,?)",
        ((k, v.tobytes()) for k, v in zip(keys, vectors)),
    )
    with conn:
        stage.build(conn)
    yield conn, path, vectors, keys
    conn.close()


def _replace(conn, key, vector):
    with conn:
        conn.execute("DELETE FROM vec_embeddings WHERE page_key=?", (key,))
        conn.execute("INSERT INTO vec_embeddings VALUES(?,?)", (key, vector.tobytes()))
        stage.mirror_write(conn, key, vector.tobytes())


def _exact(conn, vector, limit=10):
    return [
        (row[0], float(row[1]))
        for row in conn.execute(
            "SELECT page_key,distance FROM vec_embeddings WHERE embedding MATCH ? AND k=? ORDER BY distance",
            (vector.tobytes(), limit),
        )
    ]


def _search(conn, vector, limit=10):
    return stage.search(conn, vector.tobytes(), limit, state=stage.status(conn))


def test_auto_ram_keeps_original_sql_distances(corpus):
    conn, _, vectors, _ = corpus
    for q in vectors[::13]:
        expected = _exact(conn, q)
        actual = _search(conn, q)
        assert dict(actual) == dict(expected)
        assert actual[0][1] == 0.0
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0


def test_modes_and_missing_numpy_keep_rollback_arms(corpus, monkeypatch):
    conn, _, vectors, _ = corpus
    with stage.query_view(conn) as view:
        assert view.backend == "ram"
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "two_stage")
    with stage.query_view(conn) as view:
        assert view.backend == "two_stage" and view.lease is None
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "legacy")
    with stage.query_view(conn) as view:
        assert view.backend == "legacy" and view.state is None
    assert _search(conn, vectors[0]) == _exact(conn, vectors[0])
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "ram")
    monkeypatch.setattr(ram, "_numpy", lambda: None)
    with stage.query_view(conn) as view:
        assert view.backend == "two_stage"
    assert _search(conn, vectors[0])


def test_broken_numpy_dependency_is_not_missing_capability(corpus, monkeypatch):
    conn, _, _, _ = corpus

    def broken():
        raise ModuleNotFoundError("broken NumPy dependency", name="numpy._core")

    monkeypatch.setattr(ram, "_numpy", broken)
    with pytest.raises(ModuleNotFoundError):
        with stage.query_view(conn):
            pass
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0


def test_matrix_capacity_falls_back_without_loading(corpus, monkeypatch):
    conn, _, _, _ = corpus
    monkeypatch.setattr(ram, "MAX_MATRIX_BYTES", 1)

    def unexpected(*args):
        raise AssertionError("oversize matrix must not be materialized")

    monkeypatch.setattr(ram, "_load", unexpected)
    with stage.query_view(conn) as view:
        assert view.backend == "two_stage"


def test_parallel_admission_is_bounded(corpus):
    conn, _, _, _ = corpus
    acquired = []
    try:
        for _ in range(ram.MAX_ACTIVE_VIEWS):
            assert ram._SLOTS.acquire(blocking=False)
            acquired.append(True)
        with stage.query_view(conn) as view:
            assert view.backend == "two_stage"
    finally:
        for _ in acquired:
            ram._SLOTS.release()


@pytest.mark.parametrize("external", [False, True])
def test_unchanged_content_stamp_changes_do_not_rematerialize(
    corpus, monkeypatch, external
):
    conn, path, vectors, _ = corpus
    conn.execute("CREATE TABLE fixture_noise(id INTEGER PRIMARY KEY,value INTEGER)")
    conn.execute("INSERT INTO fixture_noise VALUES(1,0)")
    conn.commit()
    before = _search(conn, vectors[0])
    matrix = ram._CURRENT.matrix

    def forbidden(*args):
        pytest.fail("unchanged vectors must not allocate/norm a second matrix")

    monkeypatch.setattr(ram, "_load", forbidden)
    writer = _connection(path) if external else conn
    try:
        writer.execute("UPDATE fixture_noise SET value=1 WHERE id=1")
        writer.commit()
        assert _search(conn, vectors[0]) == before
        assert ram._CURRENT.matrix is matrix
    finally:
        if external:
            writer.close()


@pytest.mark.parametrize("position", [0, -1])
def test_unmanaged_raw_sql_vector_update_survives_byte_validation(corpus, position):
    conn, path, vectors, keys = corpus
    _search(conn, vectors[0])
    previous = ram._CURRENT
    other = _connection(path)
    try:
        vector = vectors[0] * 0.5
        with other:
            for table in ("vec_embeddings", "vec_emb_float"):
                other.execute(
                    f"DELETE FROM {table} WHERE page_key=?", (keys[position],)
                )
                other.execute(
                    f"INSERT INTO {table}(page_key,embedding) VALUES(?,?)",
                    (keys[position], vector.tobytes()),
                )
        actual = _search(conn, vector)
        assert dict(actual) == dict(_exact(conn, vector))
        assert ram._CURRENT is not previous
        assert ram._CURRENT.matrix[position].tobytes() == vector.tobytes()
    finally:
        other.close()


def test_warm_queries_reuse_matrix_and_first_other_connection_verifies_content(
    corpus, monkeypatch
):
    conn, path, vectors, _ = corpus
    calls = []
    verified = []
    original = ram._load
    match = ram._matches_current

    def counting(*args):
        calls.append(args[0])
        return original(*args)

    def checking(*args):
        verified.append(args[0])
        return match(*args)

    monkeypatch.setattr(ram, "_load", counting)
    monkeypatch.setattr(ram, "_matches_current", checking)
    with stage.query_view(conn) as view:
        first = view.lease.snapshot
    with stage.query_view(conn) as view:
        assert view.lease.snapshot is first
    assert len(calls) == 1
    other = _connection(path)
    try:
        with stage.query_view(other) as view:
            assert view.lease.snapshot is first
        assert len(calls) == 1
        assert verified == [other]  # Full bytes, never another connection's version.
        assert _search(other, vectors[1])
        assert len(calls) == 1 and verified == [other]
    finally:
        other.close()


def test_same_count_external_overwrite_and_delete_invalidate(corpus):
    conn, path, vectors, keys = corpus
    _search(conn, vectors[0])
    other = _connection(path)
    try:
        before = other.execute("PRAGMA data_version").fetchone()[0]
        _replace(other, keys[7], vectors[0])
        assert other.execute("PRAGMA data_version").fetchone()[0] == before
        with stage.query_view(other) as view:
            assert keys[7] in stage.shortlist_keys(
                other, vectors[0].tobytes(), 3, state=view.state
            )
        actual = _search(conn, vectors[0])
        assert keys[7] in dict(actual) and dict(actual)[keys[7]] == 0
        with other:
            other.execute("DELETE FROM vec_embeddings WHERE page_key=?", (keys[0],))
            stage.mirror_delete(other, [keys[0]])
            other.execute(
                "INSERT INTO vec_embeddings VALUES(?,?)",
                ("p_new", vectors[0].tobytes()),
            )
            stage.mirror_write(other, "p_new", vectors[0].tobytes())
        actual = dict(_search(conn, vectors[0]))
        assert keys[0] not in actual and actual["p_new"] == 0
    finally:
        other.close()


def test_committed_own_overwrite_invalidates_even_when_data_version_does_not(corpus):
    conn, _, vectors, keys = corpus
    _search(conn, vectors[0])
    version = conn.execute("PRAGMA data_version").fetchone()[0]
    _replace(conn, keys[11], vectors[0])
    assert conn.execute("PRAGMA data_version").fetchone()[0] == version
    assert dict(_search(conn, vectors[0]))[keys[11]] == 0


def test_borrowed_savepoint_never_caches_or_owns_rollback(corpus):
    conn, _, vectors, keys = corpus
    _search(conn, vectors[0])
    conn.execute("BEGIN")
    conn.execute("SAVEPOINT pending")
    conn.execute("DELETE FROM vec_embeddings WHERE page_key=?", (keys[8],))
    conn.execute(
        "INSERT INTO vec_embeddings VALUES(?,?)", (keys[8], vectors[0].tobytes())
    )
    stage.mirror_write(conn, keys[8], vectors[0].tobytes())
    with stage.query_view(conn) as view:
        assert not view.owned and view.backend == "two_stage"
        assert dict(_search(conn, vectors[0]))[keys[8]] == 0
    assert conn.in_transaction
    total = conn.total_changes
    conn.execute("ROLLBACK TO pending")
    assert conn.total_changes == total
    with stage.query_view(conn) as view:
        assert view.backend == "two_stage"
    conn.rollback()
    assert keys[8] not in {k for k, d in _search(conn, vectors[0]) if d == 0}


def test_external_commit_between_selection_and_rerank_stays_on_pinned_snapshot(corpus):
    conn, path, vectors, keys = corpus
    other = _connection(path)
    try:
        with stage.query_view(conn) as view:
            before = stage.shortlist_keys(
                conn, vectors[0].tobytes(), 64, state=view.state
            )
            with other:
                other.execute("DELETE FROM vec_embeddings WHERE page_key=?", (keys[0],))
                stage.mirror_delete(other, [keys[0]])
                other.execute(
                    "INSERT INTO vec_embeddings VALUES(?,?)",
                    ("p_new", vectors[0].tobytes()),
                )
                stage.mirror_write(other, "p_new", vectors[0].tobytes())
            old = dict(stage.rerank_keys(conn, vectors[0].tobytes(), before, 10))
            assert keys[0] in old and "p_new" not in old
        new = dict(_search(conn, vectors[0]))
        assert keys[0] not in new and new["p_new"] == 0
    finally:
        other.close()


def test_projection_change_disables_stale_shadow(corpus):
    conn, _, vectors, _ = corpus
    _search(conn, vectors[0])
    with conn:
        conn.execute("UPDATE vec_embedding_projection SET model='different-model'")
    with stage.query_view(conn) as view:
        assert view.backend == "legacy"
    assert _search(conn, vectors[0]) == _exact(conn, vectors[0])


def test_corruption_surfaces_and_releases_transaction_and_slots(corpus):
    conn, _, vectors, keys = corpus
    _search(conn, vectors[0])
    with conn:
        conn.execute(
            "UPDATE vec_emb_float SET embedding=? WHERE page_key=?", (b"bad", keys[0])
        )
    with pytest.raises(ValueError, match="Malformed"):
        _search(conn, vectors[0])
    assert (
        not conn.in_transaction and conn.execute("PRAGMA query_only").fetchone()[0] == 0
    )
    acquired = []
    try:
        for _ in range(ram.MAX_ACTIVE_VIEWS):
            assert ram._SLOTS.acquire(blocking=False)
            acquired.append(True)
    finally:
        for _ in acquired:
            ram._SLOTS.release()


@pytest.mark.parametrize(
    "bad",
    [b"bad", bytes(4 * (DIM - 1)), np.full(DIM, np.nan, dtype=np.float32).tobytes()],
)
def test_invalid_query_is_not_an_empty_answer(corpus, bad):
    conn, _, _, _ = corpus
    with pytest.raises(ValueError):
        with stage.query_view(conn) as view:
            stage.shortlist_keys(conn, bad, 10, state=view.state)
    assert not conn.in_transaction


def test_ties_and_all_candidate_depths_have_stable_prefixes(corpus):
    conn, _, vectors, keys = corpus
    for key in keys[1:20]:
        _replace(conn, key, vectors[0])
    with stage.query_view(conn) as view:
        blob = vectors[0].tobytes()
        a = stage.shortlist_keys(conn, blob, 5, state=view.state)
        b = stage.shortlist_keys(conn, blob, 64, state=view.state)
        assert b[:5] == a == keys[:5]
        scores = stage.rerank_keys(conn, blob, b, 10)
        assert all(d == 0 for _, d in scores)


def test_near_identity_tail_is_not_lost_to_dot_cancellation(corpus):
    conn, _, vectors, keys = corpus
    q = vectors[0].copy()
    rng = np.random.default_rng(1)
    for key in keys:
        row = q + 1e-6 * rng.standard_normal(DIM, dtype=np.float32)
        row /= np.linalg.norm(row)
        _replace(conn, key, row)
    _replace(conn, keys[-1], q)
    actual = _search(conn, q, 1)
    assert actual[0] == (keys[-1], 0.0)


def test_query_view_is_readonly_and_restores_flags_after_exception(corpus):
    conn, _, _, _ = corpus
    with pytest.raises(sqlite3.OperationalError):
        with stage.query_view(conn):
            conn.execute("DELETE FROM vec_emb_float")
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    conn.execute("PRAGMA query_only=ON")
    with stage.query_view(conn):
        pass
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    conn.execute("PRAGMA query_only=OFF")


def test_empty_projection_returns_empty_not_an_exception(corpus):
    conn, _, vectors, keys = corpus
    with conn:
        conn.execute("DELETE FROM vec_embeddings")
        stage.mirror_delete(conn, keys)
    assert _search(conn, vectors[0]) == []


class Catalog:
    def __init__(self, keys):
        self.keys = set(keys)
        self.reads = []

    def filter_fields(self, keys):
        self.reads.append(tuple(keys))
        return {
            k: {
                "domain": "Even" if int(k[2:]) % 2 == 0 else "Odd",
                "topic_cluster": "c",
                "status": "Active",
            }
            for k in keys
            if k in self.keys
        }

    nodes_by_key = filter_fields


def test_filtered_widening_reuse_and_backend_switch(corpus, monkeypatch):
    conn, _, vectors, keys = corpus
    monkeypatch.setattr(db_store, "get_connection", lambda: conn)
    catalog = Catalog(keys)
    reuse = {}
    q = list(map(float, vectors[0]))
    small, err, n = tool_search._get_vector_search_results_filtered(
        q, 10, catalog, "Even", None, False, None, reuse=reuse
    )
    assert err is None and n == 10 and small
    larger, err, n = tool_search._get_vector_search_results_filtered(
        q, 20, catalog, "Even", None, False, None, reuse=reuse
    )
    assert err is None and n == 20 and len(larger) >= len(small)
    assert all(int(k[2:]) % 2 == 0 for k in larger)
    assert reuse["stamp"][2] == "ram"
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "two_stage")
    tool_search._get_vector_search_results_filtered(
        q, 20, catalog, "Even", None, False, None, reuse=reuse
    )
    assert reuse["stamp"][2] == "two_stage"


def test_filtered_legacy_reverts_to_plain_arm(corpus, monkeypatch):
    conn, _, vectors, keys = corpus
    monkeypatch.setattr(db_store, "get_connection", lambda: conn)
    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "legacy")
    result, error, n = tool_search._get_vector_search_results_filtered(
        list(map(float, vectors[0])), 10, Catalog(keys), None, None, False, None
    )
    assert result is None and error is None and n == 0


def test_filtered_status_hint_reuses_only_owned_unchanged_generations(
    corpus, monkeypatch
):
    conn, path, vectors, keys = corpus
    monkeypatch.setattr(db_store, "get_connection", lambda: conn)
    count = []
    original = stage.status

    def counted(c):
        count.append(c)
        return original(c)

    monkeypatch.setattr(stage, "status", counted)
    catalog = Catalog(keys)
    reuse = {}
    q = list(map(float, vectors[0]))

    def call(depth):
        result, error, _ = tool_search._get_vector_search_results_filtered(
            q, depth, catalog, "Even", None, False, None, reuse=reuse
        )
        assert error is None and result is not None
        return result

    call(10)
    call(20)
    assert len(count) == 1
    other = _connection(path)
    try:
        _replace(other, keys[6], vectors[0])
        assert keys[6] in call(30)
        assert len(count) == 2
    finally:
        other.close()
    conn.execute("BEGIN")
    try:
        call(40)
        call(50)
        assert len(count) == 4  # Never reuse hints from an outer transaction/savepoint.
        assert reuse["status_hint"] is None
    finally:
        conn.rollback()


def test_status_hint_schema_change_is_not_trusted(corpus, monkeypatch):
    conn, _, _, _ = corpus
    with stage.query_view(conn) as view:
        hint = (view.state_token, view.state)
    conn.execute("CREATE TABLE changed_schema(id INTEGER)")
    conn.commit()
    calls = []
    original = stage.status

    def counted(c):
        calls.append(c)
        return original(c)

    monkeypatch.setattr(stage, "status", counted)
    with stage.query_view(conn, status_hint=hint) as view:
        assert view.backend == "ram"
    assert len(calls) == 1


def test_bindings_are_bounded_and_eviction_reverifies(corpus, monkeypatch):
    conn, path, vectors, _ = corpus
    calls = []
    original = ram._matches_current

    def counting(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(ram, "_matches_current", counting)
    _search(conn, vectors[0])
    others = []
    try:
        for _ in range(ram.MAX_CONNECTION_BINDINGS + 2):
            other = _connection(path)
            others.append(other)
            _search(other, vectors[0])
            assert len(ram._BINDINGS) <= ram.MAX_CONNECTION_BINDINGS
        assert conn not in ram._BINDINGS
        before = len(calls)
        _search(conn, vectors[0])
        assert len(calls) == before + 1
    finally:
        for other in others:
            other.close()


def test_concurrent_cold_rebuild_is_serial_and_admission_falls_back(
    corpus, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    _, path, vectors, _ = corpus
    workers = ram.MAX_ACTIVE_VIEWS + 2
    barrier = threading.Barrier(workers)
    guard = threading.Lock()
    active = 0
    peak = 0
    original = ram._load

    def slow_load(*args):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)  # Deliberate race window, not a provider/background wait.
            return original(*args)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(ram, "_load", slow_load)

    def query(_):
        conn = _connection(path)
        try:
            barrier.wait(timeout=10)
            with stage.query_view(conn) as view:
                rows = stage.search(conn, vectors[0].tobytes(), 10, state=view.state)
                assert rows[0][1] == 0
                return view.backend
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(query, range(workers)))
    assert peak == 1
    assert outcomes.count("two_stage") >= 2
    assert outcomes.count("ram") == ram.MAX_ACTIVE_VIEWS


def test_key_heap_capacity_is_policy_fallback_not_no_data(corpus, monkeypatch):
    conn, _, vectors, _ = corpus
    monkeypatch.setattr(ram, "MAX_KEY_HEAP_BYTES", 1)
    with stage.query_view(conn) as view:
        assert view.backend == "two_stage"
        assert _search(conn, vectors[0])


def test_fork_reset_callbacks_clear_bindings_and_keep_old_lease_close_safe(corpus):
    # Exercise reset functions on Windows; this is not an OS fork/SQLite-fork acceptance test.
    conn, _, _, _ = corpus
    with stage.query_view(conn) as view:
        old_slots = view.lease._slots
        ram._after_fork()
        stage._reset_views_after_fork()
        assert ram._CURRENT is None and not ram._BINDINGS
        assert ram._SLOTS is not old_slots
        view.lease.close()
        view.lease.close()  # Idempotent; cannot over-release the new semaphore.
    assert not conn.in_transaction
