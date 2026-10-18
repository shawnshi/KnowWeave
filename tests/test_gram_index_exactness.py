"""The index is only allowed to answer when its answer is the exact one.

``tests/test_memory_gram_index.py`` checks the index against the full-table scan
while the base is current.  This file covers the states in between: what the read
path must refuse to serve, and why draining the queue is not a route out of them.
"""

import json

import pytest

from vector_lake import db_store, governance_store, memory_gram_index


def _put(conn, memory_id: str, text: str, memory_type: str = "fact", score: float = 0.6) -> None:
    payload = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_id,
        "text": text,
        "source_page": f"Concept_{memory_id}.md",
        "validity_state": "active",
        "memory_score": score,
        "updated_at": "2026-07-14T00:00:00+00:00",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO operational_memory "
            "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
            (memory_id, memory_type, score, "Active", 365.0,
             json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
        )


def _ids(backend: str, query: str, **kwargs):
    import os

    os.environ["VECTOR_LAKE_MEMORY_SEARCH"] = backend
    return [m["memory_id"] for m in governance_store.search_operational_memory(query, **kwargs)]


@pytest.fixture
def one_doc(isolated_memory):
    """A corpus whose base is a rebuild snapshot of a single document."""
    db_store.init_db()
    conn = db_store.get_connection()
    _put(conn, "mem_x", "医保 支付 合规")
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    assert memory_gram_index.gram_index_usable() is True
    return isolated_memory


def test_a_document_is_not_credited_for_a_gram_it_dropped(one_doc):
    """The measured defect, at the level the read path consumes.

    A document is edited to drop a two-character term entirely.  The base still holds a
    posting for that term, so it must not reach the accumulator -- the queue entry is what
    suppresses it.  This used to be set up by running the incremental maintenance sequence
    (materialise, then merge); that machinery is gone, and the state it produced is not
    reachable any more, but the invariant it exposed is the one under test.
    """
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")

    relevance = memory_gram_index.accumulate_relevance(
        ["医保"], skip_docs=memory_gram_index.skip_doc_set(conn)
    )

    assert relevance == {}, f"the dropped gram still credits the document: {relevance}"


def test_the_search_agrees_with_the_full_scan_after_a_dropped_gram(one_doc):
    """Only the *refused* path is exercised here, and it is still worth pinning.

    Both sides below are the projected scan, because the edit makes the index refuse
    (that refusal is what ``test_a_write_defers_to_the_exact_scan`` and the cadence
    tests assert directly).  What this adds is that the fallback is not a difference in
    results.
    """
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")

    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5)
    assert _ids("gram", "影像 云平台", top_k=5) == _ids("legacy", "影像 云平台", top_k=5)


def test_retired_markers_alone_still_allow_the_fast_path(one_doc):
    """A deleted document is removed by being queued, so it cannot block serving."""
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_x'")

    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.retired_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is True
    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5) == []


def test_a_read_leaves_the_index_state_where_it_found_it(one_doc):
    """Settling the debt is maintenance's job, and a read must not move any of it.

    This replaces a test that spied on the two removed incremental functions.  Counting
    the state instead of the calls is the stronger claim: it also catches a new read-path
    step that writes anything at all.
    """
    _put(db_store.get_connection(), "mem_x", "影像 云平台 部署")
    before = (
        memory_gram_index.pending_doc_count(),
        memory_gram_index.gram_index_state()["updated_at"],
    )

    assert _ids("gram", "影像 云平台", top_k=5) == _ids("legacy", "影像 云平台", top_k=5)

    after = (
        memory_gram_index.pending_doc_count(),
        memory_gram_index.gram_index_state()["updated_at"],
    )
    assert after == before, "a search settled (or disturbed) index state"


def test_an_absent_base_is_still_built_on_first_use(one_doc):
    """The one case where a read pays for a rebuild: there is nothing to serve."""
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_gram")

    assert memory_gram_index.gram_index_state()["ready"] is False
    assert memory_gram_index.ensure_memory_gram_index() is True
    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5)


def test_a_base_from_the_previous_release_is_not_trusted(one_doc):
    """A version-1 base can satisfy the serving predicate over a stale posting.

    The release that shipped version 1 drained the queue and merged the (since dropped)
    overlay, which leaves no live marker over a base that both misses a document's
    current grams and still posts the ones it dropped.  No counter tells that state apart
    from a clean one, so the format version has to.
    """
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_gram_dirty WHERE doc IN "
                     "(SELECT doc FROM operational_memory_gram_docs)")
        conn.execute("UPDATE operational_memory_gram_state SET format_version = 1")

    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.gram_index_usable() is False

    assert memory_gram_index.ensure_memory_gram_index() is True
    assert memory_gram_index.gram_index_state()["format_version"] == memory_gram_index.GRAM_FORMAT_VERSION
    assert memory_gram_index.accumulate_relevance(
        ["医保"], skip_docs=memory_gram_index.skip_doc_set(conn)
    ) == {}


def test_an_unconverged_schema_does_not_serve(one_doc):
    """The tripwire the dropped table used to be.

    With the overlay table gone, the only thing that could make the base unservable was the
    queue -- so a database where an old release's process recreated the overlay table (or
    where the prune was deferred, or that is a read-only snapshot) would be served again with
    postings split across two structures, which is exactly the state that produced wrong
    answers before.  The read path now requires the drop to be on the ledger.
    """
    from vector_lake import db_store

    conn = db_store.get_connection()
    before = memory_gram_index.gram_index_usable()
    assert before is True
    assert db_store.GRAM_OVERLAY_DROP in db_store.applied_schema_prunes()

    # An old-release writer's DDL, and a row in it: the previous release refused to serve here.
    with db_store.transaction():
        conn.execute(
            "CREATE TABLE operational_memory_gram_overlay ("
            "gram TEXT NOT NULL, doc INTEGER NOT NULL, mask INTEGER NOT NULL, "
            "PRIMARY KEY (gram, doc)) WITHOUT ROWID"
        )
        conn.execute(
            "INSERT INTO operational_memory_gram_overlay (gram, doc, mask) VALUES (?, ?, ?)",
            ("医保", 1, 1),
        )
        conn.execute("DELETE FROM schema_migrations WHERE name = ?", (db_store.GRAM_OVERLAY_DROP,))

    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.gram_index_usable() is True, "the base itself is still current"
    assert memory_gram_index.ensure_memory_gram_index() is False, "an unconverged schema served"

    # And the migration converges it back.
    assert db_store.apply_legacy_schema_prunes() == [db_store.GRAM_OVERLAY_DROP]
    assert memory_gram_index.ensure_memory_gram_index() is True
