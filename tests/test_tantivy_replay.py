"""Real native/SQLite fixtures: crash windows, bounded replay and clean read gates."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import tantivy  # Missing native dependency is a failure, never a passing skip.

from vector_lake import db_store as db, tantivy_index as mirror

@pytest.fixture
def native(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    db.init_db()
    mirror.reset()
    yield isolated_memory
    mirror.reset()


def put(key="a", text="oldword", title="title", summary="summary"):
    db.upsert_search_index(key, title, summary, text)


def ready(**kwargs):
    for _ in range(32):
        result = mirror.replay_pending(**kwargs)
        assert result["commits"] <= 1
        if result["status"] == "ready":
            return result
    pytest.fail("fixture did not converge within bounded test steps")


def rows(terms, **kwargs):
    result = mirror.search_if_current(terms, **kwargs)
    assert result is not None, "fixture mirror must be current"
    return result


def token():
    result = mirror.current_token()
    assert result is not None, "fixture mirror must expose a validated token"
    return result


def enter_replay():
    assert mirror.replay_pending()["phase"] == "seeding"
    assert mirror.replay_pending()["phase"] == "replay"


def child(code, *, timeout=15):
    result = subprocess.run([sys.executable, "-B", "-s", "-c", code],
                            env=dict(os.environ), capture_output=True, text=True, timeout=timeout)
    return result


def test_binding_and_native_contract(native):
    assert tantivy.IndexWriter.wait_merging_threads.__doc__
    put("a", "alpha beta", title="alpha", summary="gamma")
    put("b", "alpha", title="other", summary="other")
    ready()
    result = rows(["alpha", "gamma"])
    assert [key for key, _ in result] == ["a"]
    assert result[0][1] < 0
    assert mirror.search_if_current(["alpha", "missing"]) == []
    assert mirror.search_if_current(["gamma"], fields=("text",)) == []
    assert mirror.search_if_current([]) == []
    db.delete_search_index("a")
    assert mirror.search_if_current(["alpha"]) is None
    ready()
    assert [key for key, _ in rows(["alpha"])] == ["b"]


@pytest.mark.parametrize("window", ["before_commit", "after_commit"])
def test_process_crash_and_idempotent_restart(native, window, tmp_path):
    enter_replay()
    put()
    hook = ("t._document=lambda *a, **k: os._exit(73)" if window == "before_commit" else
            "d.tantivy_mirror_ack=lambda *a, **k: os._exit(73)")
    result = child("import os; from vector_lake import db_store as d, tantivy_index as t; "
                   "d.init_db(); " + hook + "; t.replay_pending()")
    assert result.returncode == 73, result.stderr
    state = db.tantivy_mirror_state()
    assert state["pending"] == 1 and state["phase"] == "replay"
    assert db.get_connection().execute("SELECT attempt_count FROM tantivy_mirror_queue").fetchone()[0] == 1
    assert mirror.search_if_current(["oldword"]) is None
    if window == "after_commit":
        assert mirror.stats()["docs"] == 1
    restarted = child("import json; from vector_lake import db_store as d, tantivy_index as t; "
                      "d.init_db(); print(json.dumps(t.replay_pending()))")
    assert restarted.returncode == 0, restarted.stderr
    receipt = json.loads(restarted.stdout)
    assert receipt["acked"] == 1 and receipt["status"] == "ready"
    (tmp_path / f"native-crash-{window}.json").write_text(json.dumps({
        "crash_exit": result.returncode, "pending_after_crash": state["pending"],
        "restart_exit": restarted.returncode, "restart": receipt}, indent=2))
    assert mirror.stats()["docs"] == 1
    assert [key for key, _ in rows(["oldword"])] == ["a"]


@pytest.mark.parametrize("phase", ["clear", "seed"])
def test_control_phase_restart(native, phase):
    put("a")
    put("b")
    db.request_tantivy_rebuild()  # Capture the existing fixture rowid high-water.
    if phase == "clear":
        code = "d.tantivy_mirror_ack_clear=lambda *a, **k: os._exit(74); t.replay_pending()"
    else:
        assert mirror.replay_pending()["phase"] == "seeding"
        code = "t.replay_pending(max_rows=1); os._exit(74)"
    result = child("import os; from vector_lake import db_store as d, tantivy_index as t; d.init_db(); " + code)
    assert result.returncode == 74, result.stderr
    assert mirror.search_if_current(["oldword"]) is None
    state = db.tantivy_mirror_state()
    if phase == "seed":
        assert state["seed_after_rowid"] > 0 and state["phase"] == "seeding"
    put("new", "newword")
    ready(max_rows=1)
    assert mirror.stats()["docs"] == 3
    assert [key for key, _ in rows(["newword"])] == ["new"]


@pytest.mark.parametrize("change", ["same_key", "clear"])
def test_producer_during_native_writer_preserves_new_intent(native, monkeypatch, change):
    enter_replay()
    put()
    original = mirror._document
    changed = False

    def document(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True

            def produce():
                try:
                    if change == "clear":
                        db.clear_search_index()
                        put("new", "newword")
                    else:
                        put(text="newword")
                finally:
                    db.close_connection()

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(produce).result(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(mirror, "_document", document)
    old = mirror.replay_pending()
    assert old["acked"] == 0 and old["conflicts"] == 1
    assert old["pending"] == 1
    assert mirror.search_if_current(["oldword"]) is None
    ready()
    assert mirror.search_if_current(["oldword"]) == []
    assert [key for key, _ in rows(["newword"])] == (["new"] if change == "clear" else ["a"])
    assert mirror.stats()["docs"] == 1


def test_generation_update_between_selection_and_admission(native, monkeypatch):
    enter_replay()
    put()
    original = db.tantivy_mirror_admit

    def admit(batch, max_attempts):
        put(text="newword")
        return original(batch, max_attempts)

    with monkeypatch.context() as patch:
        patch.setattr(db, "tantivy_mirror_admit", admit)
        result = mirror.replay_pending()
    assert result["admitted"] == 0 and result["commits"] == 0 and result["conflicts"] == 1
    ready()
    assert mirror.search_if_current(["oldword"]) == []


def test_row_and_utf8_payload_caps_before_native_admission(native):
    enter_replay()
    for i in range(4):
        put(str(i), "中 文", title="", summary="")
    result = mirror.replay_pending(max_rows=3, max_bytes=20)
    assert result["admitted"] == 2 and result["payload_bytes"] == 16 and result["commits"] == 1
    assert result["pending"] == 2
    result = mirror.replay_pending(max_rows=1)
    assert result["admitted"] == 1 and result["pending"] == 1
    ready()
    assert mirror.stats()["docs"] == 4


def test_oversized_payload_not_selected_and_task_retained(native):
    enter_replay()
    put("big", "中" * 100, title="", summary="")
    put("small", "word", title="", summary="")
    conn = db.get_connection()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = mirror.replay_pending(max_bytes=30)
    finally:
        conn.set_trace_callback(None)
    assert result["rejected"] == 1 and result["acked"] == 1 and result["blocked"] == 1
    assert result["pending"] == 1
    payload_queries = [sql for sql in statements if sql.startswith("SELECT title, summary, text")]
    assert payload_queries and not any("'big'" in sql for sql in payload_queries)
    assert mirror.replay_pending(max_bytes=30)["admitted"] == 0
    assert mirror.search_if_current(["word"]) is None


def test_real_native_rollback_and_persisted_attempt_exhaustion(native, monkeypatch):
    enter_replay()
    put()
    original = mirror._owned_writer
    calls = []

    class MalformedWriter:
        def __init__(self, writer):
            self.writer = writer

        def delete_documents_by_term(self, *args):
            return self.writer.delete_documents_by_term(*args)

        def add_document(self, document):
            calls.append(True)
            return self.writer.add_json("{")  # Actual binding rejects malformed JSON.

        def commit(self):
            return self.writer.commit()

    @contextmanager
    def malformed():
        with original() as writer:
            yield MalformedWriter(writer)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_owned_writer", malformed)
        for attempt in range(1, 4):
            with pytest.raises(ValueError):
                mirror.replay_pending()
            row = db.get_connection().execute("SELECT * FROM tantivy_mirror_queue").fetchone()
            assert row["attempt_count"] == attempt and row["last_error"]
            assert len(row["last_error"]) <= 2000
            assert mirror.replay_pending()["admitted"] == 0  # backoff, not an immediate retry
            db.get_connection().execute("UPDATE tantivy_mirror_queue SET available_at=0")
            db.get_connection().commit()  # Publish fixture-only backoff clock adjustment.
        assert mirror.replay_pending()["blocked"] == 1
    assert len(calls) == 3
    assert mirror.stats()["docs"] == 0
    assert mirror._owned_writer is original
    put(text="newword")  # New generation resets retries.
    ready()
    assert [key for key, _ in rows(["newword"])] == ["a"]


def test_ack_exception_after_release_stays_dirty_and_recoverable(native, monkeypatch):
    enter_replay()
    put()

    def broken_ack(items):
        # Independent native acquisition proves writer release preceded SQL ACK.
        result = child("import tantivy; from vector_lake import tantivy_index as t; "
                       "index=tantivy.Index.open(str(t.index_dir())); writer=index.writer(); "
                       "writer.wait_merging_threads()")
        assert result.returncode == 0, result.stderr
        raise RuntimeError("fixture ACK fault")

    with monkeypatch.context() as patch:
        patch.setattr(db, "tantivy_mirror_ack", broken_ack)
        with pytest.raises(RuntimeError, match="ACK fault"):
            mirror.replay_pending()
    assert db.tantivy_mirror_state()["pending"] == 1
    assert mirror.search_if_current(["oldword"]) is None
    db.get_connection().execute("UPDATE tantivy_mirror_queue SET available_at=0")
    db.get_connection().commit()
    ready()
    assert mirror.stats()["docs"] == 1


@pytest.mark.parametrize("damage", ["missing", "marker", "version", "schema", "replace"])
def test_missing_replaced_or_incompatible_mirror_not_trusted(native, damage):
    put()
    ready()
    old = token()
    assert mirror.search_if_current(["oldword"])
    directory = mirror.index_dir()
    if damage == "missing":
        shutil.rmtree(directory)
    elif damage == "marker":
        (directory / "vl_incarnation").write_text("replacement")
    elif damage == "version":
        (directory / "vl_schema_version").write_text("999")
    elif damage == "schema":
        meta = json.loads((directory / "meta.json").read_text())
        meta["schema"][1]["options"]["indexing"]["tokenizer"] = "raw"
        (directory / "meta.json").write_text(json.dumps(meta))
    else:
        moved = directory.parent / "previous"
        shutil.copytree(directory, moved)
        shutil.rmtree(directory)
        shutil.copytree(moved, directory)  # Same marker, distinct physical directory.
        assert mirror.open_index(create=False) is None
        mirror.reset()  # Replacement detection must survive a cold process cache.
    assert mirror.search_if_current(["oldword"]) is None
    if damage == "missing":
        assert not directory.exists(), "reader must never create an empty mirror"
    assert mirror._index is None
    ready()
    assert token().epoch > old.epoch
    assert token().incarnation != old.incarnation
    assert [key for key, _ in rows(["oldword"])] == ["a"]


def test_clean_query_revision_recheck_even_after_fast_replay(native, monkeypatch):
    put()
    ready()
    before = token()
    original = mirror._search_index

    def race(*args, **kwargs):
        rows = original(*args, **kwargs)
        put(text="newword")
        ready()
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_search_index", race)
        assert mirror.search_if_current(["oldword"]) is None
    assert token().revision > before.revision
    assert mirror.search_if_current(["oldword"]) == []


def test_borrowed_sql_transaction_and_disabled_reads_never_open_native(native, monkeypatch):
    put()
    ready()
    conn = db.get_connection()
    conn.execute("SAVEPOINT caller")
    put(text="uncommitted")
    with monkeypatch.context() as patch:
        patch.setattr(mirror, "open_index", lambda **kwargs: pytest.fail("native read inside transaction"))
        assert mirror.search_if_current(["oldword"]) is None
        assert conn.execute("SELECT node_key FROM wiki_search_index WHERE wiki_search_index MATCH 'uncommitted'").fetchone()[0] == "a"
        with pytest.raises(RuntimeError, match="autocommit"):
            mirror.replay_pending()
    conn.rollback()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "fts5")
    put("offline", "offlineword")
    assert mirror.replay_pending()["status"] == "disabled"
    assert mirror.search_if_current(["oldword"]) is None
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    ready()
    assert mirror.search_if_current(["offlineword"])


def test_rebuild_is_bounded_resumable_not_partial_success(native):
    for i in range(5):
        put(str(i))
    with pytest.raises(mirror.MirrorReplayDeferred, match="not ready"):
        mirror.rebuild_from_sqlite(batch=1, max_steps=2)
    assert db.tantivy_mirror_state()["phase"] == "seeding"
    assert mirror.search_if_current(["oldword"]) is None
    ready(max_rows=1)
    assert mirror.stats()["docs"] == 5
    assert mirror.rebuild_from_sqlite() == 5


@pytest.mark.parametrize("kwargs", [{"max_rows": 101}, {"max_bytes": 4194305}, {"max_attempts": 4},
                                    {"max_rows": True}, {"max_bytes": 0}])
def test_invalid_caps_rejected_without_native_work(native, kwargs):
    with pytest.raises(ValueError):
        mirror.replay_pending(**kwargs)
    assert not mirror.index_dir().exists()


@pytest.mark.parametrize("payload_size", [10, 65536])
def test_absolute_default_caps_on_real_native_batches(native, payload_size, tmp_path):
    enter_replay()
    for i in range(101):
        put(str(i), "word " * (payload_size // 5), title="", summary="")
    first = mirror.replay_pending()
    assert first["commits"] == 1
    assert 0 < first["admitted"] <= 100 and first["payload_bytes"] <= 4194304
    if payload_size == 10:
        assert first["admitted"] == 100 and first["pending"] == 1
    else:
        assert first["admitted"] < 100 and first["pending"] > 1
    ready()
    assert mirror.stats()["docs"] == 101
    (tmp_path / f"native-caps-{payload_size}.json").write_text(json.dumps(first, indent=2))


def test_control_attempt_limit_native_failures_and_backoff(native, monkeypatch):
    original = mirror._owned_writer

    class BrokenClear:
        def __init__(self, writer):
            self.writer = writer

        def delete_all_documents(self):
            self.writer.delete_all_documents()
            self.writer.add_json("{")

        def commit(self):
            return self.writer.commit()

    @contextmanager
    def broken():
        with original() as writer:
            yield BrokenClear(writer)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_owned_writer", broken)
        for attempt in range(1, 4):
            with pytest.raises(ValueError):
                mirror.replay_pending()
            state = db.tantivy_mirror_state()
            assert state["attempt_count"] == attempt and state["last_error"]
            assert mirror.replay_pending()["commits"] == 0
            db.get_connection().execute("UPDATE tantivy_mirror_state SET available_at=0")
            db.get_connection().commit()
        assert mirror.replay_pending()["commits"] == 0
    assert db.tantivy_mirror_state()["phase"] == "needs_clear"
    db.request_tantivy_rebuild()
    ready()


def test_rebuild_disabled_is_honestly_deferred(native, monkeypatch):
    put()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "fts5")
    with pytest.raises(mirror.MirrorReplayDeferred):
        mirror.rebuild_from_sqlite()
    assert not mirror.index_dir().exists()
    assert db.tantivy_mirror_state()["phase"] == "needs_clear"


def test_metadata_read_must_recheck_exported_token(native):
    put()
    ready()
    before = token()
    assert mirror.search_if_current(["oldword"])
    put(text="newword")
    ready()
    metadata = db.get_connection().execute("SELECT text FROM wiki_search_index WHERE node_key='a'").fetchone()
    assert metadata[0] == "newword" and token() != before


def test_two_process_consumers_select_only_after_owned_admission(native, tmp_path):
    enter_replay()
    put()
    gate = native / "consumer-entered"
    release = native / "consumer-release"
    code = """
import os, time, json
from pathlib import Path
from vector_lake import db_store as d, tantivy_index as t
d.init_db()
original=d.tantivy_mirror_select
def select(*a, **k):
    Path(os.environ['ENTER']).write_text('owned')
    deadline=time.monotonic()+8
    while not Path(os.environ['RELEASE']).exists():
        if time.monotonic()>deadline: raise TimeoutError('fixture barrier')
        time.sleep(.02)
    return original(*a, **k)
d.tantivy_mirror_select=select
print(json.dumps(t.replay_pending()), flush=True)
"""
    env = dict(os.environ, ENTER=str(gate), RELEASE=str(release))
    first = subprocess.Popen([sys.executable, "-B", "-s", "-c", code], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        import time
        deadline = time.monotonic() + 5
        while not gate.exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        from filelock import Timeout
        contender = mirror._index_lock()
        with pytest.raises(Timeout):
            contender.acquire(timeout=0)  # Selection barrier must already hold ownership.
        put(text="newword")  # Producer proceeds while native ownership is held.
        second = subprocess.Popen([sys.executable, "-B", "-s", "-c",
                                   "import json; from vector_lake import db_store as d, tantivy_index as t; d.init_db(); print(json.dumps(t.replay_pending()))"],
                                  env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        release.write_text("go")
        out1, err1 = first.communicate(timeout=12)
        out2, err2 = second.communicate(timeout=12)
        assert first.returncode == 0, err1
        assert second.returncode == 0, err2
        receipts = [json.loads(out1), json.loads(out2)]
        assert sum(row["acked"] for row in receipts) == 1
        (tmp_path / "native-consumers.json").write_text(json.dumps(receipts, indent=2))
    finally:
        release.write_text("go")
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert mirror.search_if_current(["oldword"]) == []
    assert [key for key, _ in rows(["newword"])] == ["a"]


def test_one_file_lock_per_replay_scope_and_direct_cleanup(native, monkeypatch):
    enter_replay()
    put()
    original = mirror._index_lock
    count = []

    @contextmanager
    def lock():
        count.append(True)
        with original():
            yield

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_index_lock", lock)
        assert mirror.replay_pending()["acked"] == 1
    assert len(count) == 1
    with pytest.raises(ValueError, match="scope fault"), mirror._write_session() as writer:
        writer.delete_all_documents()
        raise ValueError("scope fault")
    assert mirror.search_if_current(["oldword"])
    result = child("from vector_lake import tantivy_index as t; t.upsert('other','otherword','','otherword')")
    assert result.returncode == 0, result.stderr
