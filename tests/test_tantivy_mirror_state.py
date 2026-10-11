"""Real SQLite state protocol tests; no native index or external service is opened."""
import sqlite3

import pytest

from vector_lake import db_store as db


@pytest.fixture
def conn(isolated_memory, monkeypatch):
    db.init_db()

    def forbidden():
        raise AssertionError("producer must not open a native mirror")

    monkeypatch.setattr(db, "_tantivy", forbidden)
    return db.get_connection()


def put(key="a", text="words"):
    db.upsert_search_index(key, "title", "summary", text, content_hash="hash")


def state(conn):
    return dict(conn.execute("SELECT * FROM tantivy_mirror_state").fetchone())


def queue(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM tantivy_mirror_queue ORDER BY node_key")]


def enter_replay(conn, incarnation="fixture-1"):
    epoch = state(conn)["epoch"]
    assert db.tantivy_mirror_admit_clear(epoch)
    assert db.tantivy_mirror_ack_clear(epoch, incarnation)
    while state(conn)["phase"] == "seeding":
        db.tantivy_mirror_seed(epoch, max_rows=1)
    return epoch


@pytest.mark.parametrize("owner", ["managed", "begin", "savepoint"])
def test_caller_rollback_owns_fts_queue_and_revision(conn, owner):
    before = state(conn)
    if owner == "managed":
        with pytest.raises(RuntimeError, match="caller abort"), db.transaction():
            put()
            raise RuntimeError("caller abort")
    else:
        conn.execute("BEGIN" if owner == "begin" else "SAVEPOINT caller")
        put()
        assert conn.in_transaction
        assert queue(conn)
        assert db.tantivy_mirror_read_token() is None
        conn.rollback()
    assert state(conn) == before
    assert not queue(conn)
    assert conn.execute("SELECT COUNT(*) FROM wiki_search_index").fetchone()[0] == 0


def test_release_local_savepoint_never_commits_caller(conn):
    conn.execute("BEGIN")
    conn.execute("SAVEPOINT caller")
    put()
    conn.execute("ROLLBACK TO caller")
    assert not queue(conn)
    put("b")
    conn.execute("RELEASE caller")
    assert conn.in_transaction
    observer = sqlite3.connect(db.get_db_path())
    try:
        assert observer.execute("SELECT COUNT(*) FROM tantivy_mirror_queue").fetchone()[0] == 0
        conn.commit()
        assert observer.execute("SELECT node_key FROM tantivy_mirror_queue").fetchone()[0] == "b"
    finally:
        observer.close()


@pytest.mark.parametrize("operation", ["upsert", "delete", "cascade", "clear"])
def test_enqueue_fault_rolls_back_current_operation_even_if_caught(conn, operation):
    put()
    before, pending = state(conn), queue(conn)
    conn.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON tantivy_mirror_queue "
                 "BEGIN SELECT RAISE(ABORT, 'enqueue fault'); END")
    conn.execute("CREATE TRIGGER fail_clear BEFORE DELETE ON tantivy_mirror_queue "
                 "BEGIN SELECT RAISE(ABORT, 'enqueue fault'); END")
    conn.execute("BEGIN")
    action = {"upsert": lambda: put(text="replacement"), "delete": lambda: db.delete_search_index("a"),
              "cascade": lambda: db.delete_node_cascade("a"), "clear": db.clear_search_index}[operation]
    with pytest.raises(sqlite3.IntegrityError, match="enqueue fault"):
        action()
    assert conn.in_transaction
    assert state(conn) == before
    assert queue(conn) == pending
    assert conn.execute("SELECT text FROM wiki_search_index").fetchone()[0] == "words"
    conn.commit()
    assert state(conn) == before


def test_schema_missing_in_caller_transaction_does_not_commit(conn):
    conn.execute("DROP TABLE tantivy_mirror_queue")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO alias_registry VALUES ('caller', 'value', 'now')")
    with pytest.raises(RuntimeError, match="Initialize Tantivy"):
        put()
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM alias_registry").fetchone()[0] == 0


def test_schema_sentinels_adopt_legacy_fixture_and_seed_existing_fts(conn):
    put()
    conn.execute("DROP TABLE tantivy_mirror_queue")
    conn.execute("DROP TABLE tantivy_mirror_state")
    db._INITIALIZED_DB_PATHS.discard(str(db.get_db_path().resolve()))
    db.init_db()
    assert {"tantivy_mirror_queue", "tantivy_mirror_state"} <= set(db._SCHEMA_SENTINELS)
    assert state(conn)["phase"] == "needs_clear"
    assert state(conn)["seed_through_rowid"] > 0
    assert db.tantivy_mirror_read_token() is None
    enter_replay(conn)
    assert [row["node_key"] for row in queue(conn)] == ["a"]


def test_four_producers_are_atomic_and_never_directly_write_native(conn):
    put()
    first = queue(conn)[0]
    assert first["operation"] == "upsert"
    assert first["fts_rowid"] == conn.execute("SELECT rowid FROM wiki_search_index").fetchone()[0]
    db.delete_search_index("a")
    second = queue(conn)[0]
    assert second["operation"] == "delete" and second["generation"] > first["generation"]
    put("b")
    db.delete_node_cascade("b")
    assert queue(conn)[1]["operation"] == "delete"
    old_epoch = state(conn)["epoch"]
    db.clear_search_index()
    assert state(conn)["epoch"] == old_epoch + 1
    assert not queue(conn)
    assert state(conn)["phase"] == "needs_clear"


def test_generation_coalescing_and_old_ack_failure_admission_do_not_touch_newer_intent(conn):
    put()
    enter_replay(conn)
    old_batch = db.tantivy_mirror_select()
    admitted = db.tantivy_mirror_admit(old_batch)
    put(text="new")
    latest = queue(conn)[0]
    assert db.tantivy_mirror_ack(admitted) == 0
    assert db.tantivy_mirror_fail(admitted, "old error", retry_delay=0) == 0
    assert db.tantivy_mirror_admit(old_batch) == ()
    assert queue(conn) == [latest]
    assert latest["attempt_count"] == 0 and latest["last_error"] is None
    batch = db.tantivy_mirror_select()
    assert batch.intents[0].text == "new"
    assert db.tantivy_mirror_ack(db.tantivy_mirror_admit(batch)) == 1


def test_clear_new_epoch_preserves_new_writes_and_rejects_old_control_ack(conn):
    put()
    old_epoch = enter_replay(conn)
    old = db.tantivy_mirror_admit(db.tantivy_mirror_select())
    db.clear_search_index()
    put("new")
    new_epoch = state(conn)["epoch"]
    assert new_epoch > old_epoch
    assert db.tantivy_mirror_ack(old) == 0
    assert not db.tantivy_mirror_ack_clear(old_epoch, "old")
    assert not db.tantivy_mirror_mark_ready(old_epoch, "fixture-1")
    assert db.tantivy_mirror_select().intents == ()
    enter_replay(conn, "fixture-2")
    assert [item.node_key for item in db.tantivy_mirror_select().intents] == ["new"]


def test_keyset_seed_honors_high_water_and_never_overwrites_newer_intent(conn):
    for key in ("a", "b", "c"):
        put(key)
    epoch = db.request_tantivy_rebuild()
    upper = state(conn)["seed_through_rowid"]
    assert db.tantivy_mirror_admit_clear(epoch)
    assert db.tantivy_mirror_ack_clear(epoch, "seed")
    assert db.tantivy_mirror_seed(epoch, 1)["scanned"] == 1
    put("b", "changed")
    db.delete_search_index("c")
    put("d")
    latest = queue(conn)
    calls = []
    conn.set_trace_callback(calls.append)
    try:
        while state(conn)["phase"] == "seeding":
            db.tantivy_mirror_seed(epoch, 1)
    finally:
        conn.set_trace_callback(None)
    assert not any("OFFSET" in call.upper() for call in calls)
    assert state(conn)["seed_through_rowid"] == upper
    assert all(row in queue(conn) for row in latest)
    operations = {row["node_key"]: row["operation"] for row in queue(conn)}
    assert operations == {"a": "upsert", "b": "upsert", "c": "delete", "d": "upsert"}
    batch = db.tantivy_mirror_select()
    assert next(item for item in batch.intents if item.node_key == "b").text == "changed"


def test_control_crashes_exhaust_attempts_and_errors_are_bounded(conn):
    epoch = state(conn)["epoch"]
    for _ in range(3):
        assert db.tantivy_mirror_admit_clear(epoch)
        assert db.tantivy_mirror_fail_clear(epoch, "e" * 3000, retry_delay=0)
    assert not db.tantivy_mirror_admit_clear(epoch)
    assert state(conn)["attempt_count"] == 3
    assert len(state(conn)["last_error"]) == 2000
    assert db.request_tantivy_rebuild() > epoch
    assert state(conn)["attempt_count"] == 0


def test_retry_generation_exhaustion_retains_work_and_new_intent_resets_it(conn):
    put()
    enter_replay(conn)
    for _ in range(3):
        batch = db.tantivy_mirror_select()
        assert len(batch.intents) == 1
        admitted = db.tantivy_mirror_admit(batch)
        assert db.tantivy_mirror_fail(admitted, "failure" * 1000, retry_delay=0) == 1
    assert not db.tantivy_mirror_select().intents
    assert db.tantivy_mirror_state()["blocked"] == 1
    assert len(queue(conn)[0]["last_error"]) == 2000
    put(text="repair")
    assert db.tantivy_mirror_state()["blocked"] == 0
    assert db.tantivy_mirror_select().intents[0].text == "repair"


def test_size_before_text_materialization_utf8_row_budget_and_oversize_retention(conn):
    put("huge", "中" * 1000)
    put("small", "中")
    enter_replay(conn)
    calls = []
    conn.set_trace_callback(calls.append)
    try:
        batch = db.tantivy_mirror_select(max_rows=2, max_bytes=40)
    finally:
        conn.set_trace_callback(None)
    assert [item.node_key for item in batch.rejected] == ["huge"]
    assert batch.rejected[0].text is None
    assert [item.node_key for item in batch.intents] == ["small"]
    assert batch.payload_bytes == len("smalltitlesummary中".encode())
    payload_queries = [call for call in calls if call.startswith("SELECT title, summary, text")]
    assert len(payload_queries) == 1 and "'small'" in payload_queries[0]
    assert db.tantivy_mirror_admit(batch) == batch.intents
    assert queue(conn)[0]["attempt_count"] == 3
    assert queue(conn)[0]["last_error"]
    assert db.tantivy_mirror_ack(batch.intents) == 1
    assert db.tantivy_mirror_state()["pending"] == 1
    assert not db.tantivy_mirror_mark_ready(state(conn)["epoch"], "fixture-1")


def test_total_bytes_and_candidate_rows_are_bounded_before_loading_text(conn):
    for key in ("a", "b", "c"):
        put(key, "中")
    enter_replay(conn)
    batch = db.tantivy_mirror_select(max_rows=2, max_bytes=32)
    assert len(batch.intents) == 2
    assert batch.payload_bytes == 32
    batch = db.tantivy_mirror_select(max_rows=2, max_bytes=31)
    assert len(batch.intents) == 1
    assert batch.payload_bytes == 16


def test_missing_fts_pointer_is_blocked_not_silent_delete_or_empty_success(conn):
    put()
    enter_replay(conn)
    conn.execute("UPDATE tantivy_mirror_queue SET fts_rowid = 999")
    conn.commit()
    batch = db.tantivy_mirror_select()
    assert not batch.intents
    assert len(batch.rejected) == 1
    assert batch.rejected[0].error and "missing" in batch.rejected[0].error
    db.tantivy_mirror_admit(batch)
    assert db.tantivy_mirror_state()["blocked"] == 1


def test_ready_token_uses_fresh_autocommit_view_and_revision_not_just_clean_flag(conn):
    put()
    epoch = enter_replay(conn)
    assert not db.tantivy_mirror_mark_ready(epoch, "wrong")
    assert not db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    db.tantivy_mirror_ack(db.tantivy_mirror_admit(db.tantivy_mirror_select()))
    assert db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    before = db.tantivy_mirror_read_token()
    assert before and before.incarnation == "fixture-1"
    put(text="new")
    assert db.tantivy_mirror_read_token() is None
    db.tantivy_mirror_ack(db.tantivy_mirror_admit(db.tantivy_mirror_select()))
    assert db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    after = db.tantivy_mirror_read_token()
    assert after and before != after and before.epoch == after.epoch
    conn.execute("BEGIN")
    assert db.tantivy_mirror_read_token() is None
    with pytest.raises(RuntimeError, match="autocommit"):
        db.tantivy_mirror_select()
    conn.rollback()


def test_duplicate_legacy_keys_cannot_become_ready(conn):
    put()
    db.upsert_search_index("a", "duplicate", "", "", replace_existing=False)
    with pytest.raises(RuntimeError, match="Duplicate"):
        db.request_tantivy_rebuild()
    # Reproduce additive adoption of an old FTS fixture with duplicate keys.
    conn.execute("DROP TABLE tantivy_mirror_queue")
    conn.execute("DROP TABLE tantivy_mirror_state")
    db._INITIALIZED_DB_PATHS.discard(str(db.get_db_path().resolve()))
    db.init_db()
    epoch = state(conn)["epoch"]
    assert db.tantivy_mirror_admit_clear(epoch)
    assert db.tantivy_mirror_ack_clear(epoch, "duplicate")
    with pytest.raises(RuntimeError, match="Duplicate"):
        db.tantivy_mirror_seed(epoch)
    assert state(conn)["phase"] == "seeding"
    assert db.tantivy_mirror_read_token() is None


def test_duplicate_keys_beyond_initial_seed_high_water_cannot_become_ready(conn):
    put()
    epoch = enter_replay(conn)
    db.upsert_search_index("a", "duplicate", "", "", replace_existing=False)
    db.tantivy_mirror_ack(db.tantivy_mirror_admit(db.tantivy_mirror_select()))
    with pytest.raises(RuntimeError, match="Duplicate"):
        db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    assert db.tantivy_mirror_read_token() is None


@pytest.mark.parametrize("kwargs", [{"max_rows": 0}, {"max_rows": 101}, {"max_rows": True},
                                   {"max_bytes": 0}, {"max_bytes": 4194305}, {"max_attempts": 4}])
def test_input_limits_reject_instead_of_clamping(conn, kwargs):
    with pytest.raises(ValueError):
        db.tantivy_mirror_select(**kwargs)


@pytest.mark.parametrize("action", [put, db.clear_search_index])
def test_missing_control_row_never_commits_fts_only_work(conn, action):
    put()
    conn.execute("DELETE FROM tantivy_mirror_state")
    conn.commit()
    before = queue(conn)
    with pytest.raises(RuntimeError, match="Missing Tantivy"):
        action()
    assert not conn.in_transaction
    assert queue(conn) == before
    assert conn.execute("SELECT text FROM wiki_search_index").fetchone()[0] == "words"


def test_durable_seed_cursor_and_admission_survive_reopened_connection(conn):
    for key in ("a", "b", "c"):
        put(key)
    epoch = db.request_tantivy_rebuild()
    db.tantivy_mirror_admit_clear(epoch)
    db.tantivy_mirror_ack_clear(epoch, "persisted")
    db.tantivy_mirror_seed(epoch, 1)
    before = state(conn)
    db.close_connection()
    conn = db.get_connection()
    assert state(conn) == before
    while state(conn)["phase"] == "seeding":
        db.tantivy_mirror_seed(epoch, 1)
    admitted = db.tantivy_mirror_admit(db.tantivy_mirror_select(max_rows=1))
    db.close_connection()
    conn = db.get_connection()
    assert queue(conn)[0]["attempt_count"] == 1
    assert db.tantivy_mirror_ack(admitted) == 1
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tantivy_mirror_queue)")}
    assert not {"text", "title", "summary", "payload"} & columns


def test_fresh_token_observes_another_sqlite_connection_commit(conn):
    put()
    epoch = enter_replay(conn)
    db.tantivy_mirror_ack(db.tantivy_mirror_admit(db.tantivy_mirror_select()))
    assert db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    before = db.tantivy_mirror_read_token()
    writer = sqlite3.connect(db.get_db_path())
    writer.row_factory = sqlite3.Row
    try:
        # Both connections retain independent WAL views; the producer seam runs on the writer.
        db._LOCAL.conn = writer
        put(text="external commit")
    finally:
        db._LOCAL.conn = conn
        writer.close()
    assert db.tantivy_mirror_read_token() is None
    db.tantivy_mirror_ack(db.tantivy_mirror_admit(db.tantivy_mirror_select()))
    assert db.tantivy_mirror_mark_ready(epoch, "fixture-1")
    assert db.tantivy_mirror_read_token() != before


def test_future_available_at_prevents_hot_retries(conn):
    put()
    enter_replay(conn)
    admitted = db.tantivy_mirror_admit(db.tantivy_mirror_select())
    db.tantivy_mirror_fail(admitted, "later", retry_delay=3600)
    assert db.tantivy_mirror_select().intents == ()
    with pytest.raises(ValueError):
        db.tantivy_mirror_fail(admitted, "bad delay", retry_delay=-1)
    with pytest.raises(ValueError):
        db.tantivy_mirror_ack_clear(state(conn)["epoch"], "")
