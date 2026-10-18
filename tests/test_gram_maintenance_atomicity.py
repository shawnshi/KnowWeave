"""The rebuild's safety property changed from exclusion to a snapshot fence.

The previous contract was mutual exclusion: the projection read, the staging and the publish
were one transaction, so a writer on another connection was refused for the whole rebuild.
Measured on the live corpus 2026-09-19, that was 436 s of the 465 s rebuild (94 %), which made a
routine maintenance step an availability event -- the outbox drain stalled behind it twice on
2026-09-18 and logged lock failures for ten minutes.

The new contract keeps the exactness that the single transaction was there for, using a fence
instead of a lock: every staged document records a digest of the bytes it was read from, and the
publish drops a change marker only when the document still digests identically.  A document
written during the rebuild keeps its marker, so ``gram_index_usable()`` stays false and the read
path refuses to serve -- the same honest degradation, without blocking writers.
"""

import sqlite3
import threading

import pytest

from tests.test_gram_index_exactness import _put

from vector_lake import db_store, memory_gram_index


def _seed(count: int = 4) -> None:
    db_store.init_db()
    conn = db_store.get_connection()
    for index in range(count):
        _put(conn, f"mem_{index}", f"医院 电子病历 六级 目标 {index}")


def _writer_can_commit() -> None:
    """Assert that another connection can take the write lock and commit right now."""
    other = sqlite3.connect(db_store.get_db_path())
    try:
        other.execute("PRAGMA busy_timeout = 250")
        other.execute("BEGIN IMMEDIATE")
        other.execute("SELECT 1")
        other.commit()
    finally:
        other.close()


def _run_hooked_at(monkeypatch, hook_name: str, target, on_call: int, during):
    """Run ``target`` on a worker thread, calling ``during`` on the ``on_call``-th hook.

    ``extract_grams`` is called while building a batch and *before* that batch's transaction, so
    the hook runs with no write lock held on this connection -- which is exactly the window a
    concurrent writer now has.
    """
    original = getattr(memory_gram_index, hook_name)
    calls = {"count": 0}
    errors: list[BaseException] = []

    def hook(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == on_call:
            try:
                during()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the caller
                errors.append(exc)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_gram_index, hook_name, hook)
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="maintenance")
    worker.start()
    worker.join(120)
    assert not worker.is_alive(), "the rebuild did not finish"
    assert not errors, errors
    assert "error" not in outcome, outcome.get("error")
    assert calls["count"] >= on_call, "the hook never reached the requested call"
    return outcome["result"]


def _touch_projection(memory_id: str) -> None:
    """Write one memory from another connection, as an external writer would.

    The write goes to the canonical table, which is where every writer goes: the projection and
    the gram queue are both derived from it by triggers.  Writing the projection directly would
    now be a change no reader is told about -- the queue is keyed on content, not on the derived
    row's shape.
    """
    other = sqlite3.connect(db_store.get_db_path())
    try:
        other.execute("PRAGMA busy_timeout = 5000")
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "UPDATE operational_memory SET data_json = json_set("
            "data_json, '$.text', json_extract(data_json, '$.text') || ' changed') "
            "WHERE memory_id = ?",
            (memory_id,),
        )
        other.commit()
    finally:
        other.close()


def test_a_writer_is_not_excluded_while_the_rebuild_stages(isolated_memory, monkeypatch):
    """The availability half of the change: the write lock is free between batches."""
    _seed(6)

    def during():
        _writer_can_commit()

    result = _run_hooked_at(
        monkeypatch, "extract_grams", memory_gram_index.rebuild_memory_gram_index, 2, during
    )

    assert "Rebuilt the memory gram index" in result


def test_a_document_written_during_the_rebuild_keeps_its_marker(isolated_memory, monkeypatch):
    """The exactness half: a stale staged document must not be published as current.

    The first batch (``batch_docs=1``) stages ``mem_0``; the write lands afterwards, so the
    published postings for it are the pre-write ones.  Clearing its marker would make the read
    path serve stale postings as exact, which is precisely the silent staleness the old
    single transaction prevented by excluding writers.
    """
    _seed(5)

    def during():
        _touch_projection("mem_0")

    result = _run_hooked_at(
        monkeypatch, "extract_grams",
        lambda: memory_gram_index.rebuild_memory_gram_index(batch_docs=1),
        on_call=3, during=during,
    )

    assert "kept their change marker" in result, result
    conn = db_store.get_connection()
    marked = {row[0] for row in conn.execute("SELECT doc FROM operational_memory_gram_dirty")}
    staged_mem0 = conn.execute(
        "SELECT doc FROM operational_memory_gram_docs WHERE memory_id = 'mem_0'"
    ).fetchone()[0]
    assert staged_mem0 in marked, "the fenced document lost its marker"
    assert memory_gram_index.gram_index_usable() is False, (
        "an index holding a stale document must not report itself usable"
    )


def test_a_quiet_rebuild_still_clears_every_marker(isolated_memory):
    """With no concurrent writer the fence drops everything, as the old path did."""
    _seed(6)
    result = memory_gram_index.rebuild_memory_gram_index()

    assert "Rebuilt the memory gram index" in result
    assert "kept their change marker" not in result
    assert memory_gram_index.pending_doc_count() == 0
    assert memory_gram_index.gram_index_usable() is True


def test_a_retired_marker_is_dropped_when_the_base_never_held_it(isolated_memory):
    """A quiet rebuild still drains the queue, retired markers included.

    A document deleted *before* the staging pass is absent from the new base, so nothing is left
    for a marker to make the read path skip -- and keeping it would leave ``pending_doc_count``
    reporting a backlog that can never clear.
    """
    _seed(4)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_index WHERE memory_id = 'mem_3'")
    assert memory_gram_index.retired_doc_count() == 1

    memory_gram_index.rebuild_memory_gram_index()

    assert memory_gram_index.pending_doc_count() == 0
    assert memory_gram_index.gram_index_usable() is True


def test_a_document_staged_then_deleted_keeps_its_marker(isolated_memory, monkeypatch):
    """The other half: the base holds its postings, so the marker has to stay.

    The document was staged before it was deleted, so the published base still contains the
    postings it had.  Clearing the marker would leave them counted until the next rebuild.
    """
    _seed(5)
    target_row = db_store.get_connection().execute(
        "SELECT doc FROM operational_memory_gram_docs WHERE memory_id = 'mem_0'"
    ).fetchone()[0]

    def during():
        other = sqlite3.connect(db_store.get_db_path())
        try:
            other.execute("PRAGMA busy_timeout = 5000")
            other.execute("BEGIN IMMEDIATE")
            # The canonical row, as every writer deletes it: the projection row goes with it through
            # the native cascade, and the gram marker is queued by the trigger on this table.
            other.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_0'")
            other.commit()
        finally:
            other.close()

    result = _run_hooked_at(
        monkeypatch, "extract_grams",
        lambda: memory_gram_index.rebuild_memory_gram_index(batch_docs=1),
        on_call=3, during=during,
    )

    assert "kept their change marker" in result, result
    marked = {
        row[0]
        for row in db_store.get_connection().execute("SELECT doc FROM operational_memory_gram_dirty")
    }
    assert target_row in marked, "the staged-then-deleted document lost its marker"
    # A *retired* marker does not gate serving: ``gram_index_usable`` counts only markers whose
    # document still exists, because a retired one only makes the read path skip postings the
    # base still holds.  That is why the marker is kept (it has work to do) while the index stays
    # usable -- the two halves of the old contract, preserved separately.
    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.retired_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is True


def test_the_rebuild_leaves_no_scratch_tables(isolated_memory):
    _seed(3)
    memory_gram_index.rebuild_memory_gram_index()

    leftovers = [
        row[0]
        for row in db_store.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('operational_memory_gram_stage','operational_memory_gram_stamp','operational_memory_gram_new')"
        )
    ]
    assert leftovers == []


def test_the_fence_restores_the_autocheckpoint_setting(isolated_memory):
    """The bulk-load pragma must not leak into the rest of the process."""
    _seed(3)
    conn = db_store.get_connection()
    before = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    memory_gram_index.rebuild_memory_gram_index()
    assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == before


@pytest.mark.parametrize("batch_docs", [1, 2, 1000])
def test_every_batch_size_produces_the_same_index(isolated_memory, batch_docs):
    """Batch size is a throughput knob; the published index must not depend on it."""
    _seed(7)
    memory_gram_index.ensure_memory_gram_index()
    memory_gram_index.rebuild_memory_gram_index(batch_docs=batch_docs)

    conn = db_store.get_connection()
    rows = list(conn.execute("SELECT gram, postings FROM operational_memory_gram ORDER BY gram"))
    assert rows, "the rebuild published nothing"
    assert memory_gram_index.gram_index_usable() is True
    scratch = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ("
            "'operational_memory_gram_stage','operational_memory_gram_stamp','operational_memory_gram_new')"
        )
    ]
    assert scratch == [], scratch
