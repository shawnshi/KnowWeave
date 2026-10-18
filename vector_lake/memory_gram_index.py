"""Exact n-gram inverted index over ``operational_memory_index``.

Why this exists
---------------

``search_operational_memory`` scores a memory by counting how many query terms
occur in its key / text / page fields, weighted 4 / 3 / 1.  ``_query_terms``
expands a CJK query into *every single character* plus *every adjacent bigram*,
so a 30-character question becomes ~59 terms and the plain SQL form of the
scorer needs ``O(rows x terms)`` ``instr()`` calls: measured 2.3 s on the live
142 117-row corpus, growing linearly forever.

An inverted index turns that into ``O(sum of the postings of the query terms)``.
Measured on the same corpus, the longest realistic query touches 223 253
postings and accumulates in 0.12 s *in pure Python* -- no numpy, no compression
dependency.

Layout
------

``operational_memory_gram(gram, postings)``
    The base index.  ``postings`` is a little-endian ``uint32`` array where each
    element is ``(doc_delta << 4) | field_mask``; ``doc_delta`` is the gap to the
    previous document so the list stays sorted and compact, and ``field_mask``
    has bit 0 = key, bit 1 = text, bit 2 = page.  Measured 414 914 distinct grams
    and 25 430 371 postings = 97 MB, against 162 MB for the dead FTS projection
    this replaced.  A posting list averages 61 entries, so decoding is cheap.

``operational_memory_gram_overlay(gram, doc, mask)`` -- **dropped**
    This table let an older release's incremental step put a document's postings in two
    structures at once, which the read path summed as if the overlay replaced the base:
    a document could be credited for a gram it no longer contained.  Nothing wrote it
    after the incremental path was removed (see the correctness section), so the table
    and every reader of it are gone; the schema prune that drops it is recorded in
    ``db_store._LEGACY_SCHEMA_PRUNES`` so existing databases converge too.

``operational_memory_gram_docs(doc, memory_id)``
    The identity map a posting's ``doc`` number names.  A document number has to outlive
    the row it was derived from: ``operational_memory_index`` is deleted and rewritten
    wholesale whenever it is re-derived, so a ``doc`` that happened to be that table's
    rowid would silently start naming a different memory afterwards -- the index would
    keep answering, from another memory's postings.  Rows here are allocated once and
    never deleted, which also means an integer is never handed to a second identity.

``operational_memory_gram_dirty(doc)``
    Documents whose base postings cannot be trusted.  A dirty document is skipped
    outright, so its stale base entries credit nothing, and a deleted document -- whose
    postings are still in the base under its old number -- is skipped the same way.
    Skipping is *all* that happens: a document leaves this queue only when a rebuild
    replaces the base, which is why the index reports itself unusable while a live
    document is queued.  The queue is written by triggers on ``operational_memory``, so
    it is keyed on content rather than on the projection and survives a rebuild of it.

Correctness
-----------

The index reproduces :func:`governance_store._memory_relevance` exactly:
``relevance = sum over terms of 4*[term in key] + 3*[term in text] + [term in page]``.
Terms of one or two characters are looked up directly.  Longer terms are
resolved by intersecting the postings of their constituent bigrams (a necessary
condition) and then verifying each candidate with ``instr``, so the result is
exact too.  ``tests/test_memory_gram_index.py`` asserts equality with the
``legacy`` full-scan oracle.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
import os
import re
import sqlite3
import sys
import time

from vector_lake import db_store
from vector_lake.db_store import get_connection, init_db, transaction

try:
    import vector_lake_core
    HAVE_CORE = True
except ImportError:
    vector_lake_core = None
    HAVE_CORE = False

log = logging.getLogger("vector-lake-memory-gram-index")

#: Bumped when a previously written base can no longer be trusted to be exact.
#: Version 1 is not: a database drained by the release that shipped it can hold no
#: live queue entry over a base that is missing a document's current grams *and* still
#: posting grams it dropped, which is indistinguishable from a clean base by counters
#: alone.  Raising the version turns every such database into a visible ``ready=False``
#: and one rebuild.
#:
#: Version 3 changed what a posting's ``doc`` means (see ``operational_memory_gram_docs``):
#: it is an identity allocated in that table, not ``operational_memory_index.rowid``.  A
#: version-2 base is still internally consistent, but its numbers are physical addresses of
#: the projection, so it cannot be read through the identity map and must be rebuilt once.
#: The queue shares the base's namespace and is cleared by that rebuild for the same reason.
GRAM_FORMAT_VERSION = 3

#: Gram *groups* packed per committed chunk in the rebuild's second pass.  Groups are whole, so
#: the chunk is bounded by the primary-key range and a commit never straddles one.
PACK_CHUNK_GRAMS = 5_000
#: Largest corpus rebuilt automatically on the read path.  The cap has nothing to do
#: with how much is queued: a base that is *stale* is never rebuilt from a read, no
#: matter how small, because the exact scan is far cheaper than a rebuild for any
#: corpus this cap admits -- at the 3 ms/document rebuild cost below, a 2 000-document
#: corpus is ~6 s to rebuild against ~32 ms to scan, so a read-path rebuild would have
#: to be amortised over ~190 searches per write.  Only a missing base (never built, a
#: truncated table, or an older format version) is built on first use, where there is
#: nothing to serve and the alternative is failing every search.
#:
#: It also bounds how long a *reader* can refuse writers.  The build runs in one
#: transaction, so a first-use build holds the write lock for its whole duration and a
#: concurrent writer fails closed once ``db_store.BEGIN_LOCK_BUDGET_SECONDS`` (20 s)
#: elapses.  At the cost above a 2 000-document build is ~6 s, which stays inside that
#: budget; raising this cap raises what a writer has to wait for, not only what a search
#: costs.
AUTO_REBUILD_MAX_DOCS = 2_000
#: Documents written since the last rebuild at which the index is due for one.
#:
#: Measured on the live lake (146 679 documents, 2026-09-18): the indexed path answers a
#: search in 0.295 s median against 0.745 s for the exact scan, so restoring it saves
#: ~0.355 s per search -- while one rebuild costs ~430 s and refuses **every** writer for
#: its duration.  A rebuild therefore only pays for itself after ~1 200 searches, and the
#: number that decides this constant is not that break-even but how long a rebuild may
#: stop writes: writes are the shared resource, and no amount of saved search time buys
#: back an ingest that failed against a 20 s lock budget.  So rebuilds stay rare
#: deliberately, and the threshold is set by tolerated staleness rather than by search
#: savings.  At the observed write rate (~tens of documents a day) 500 means one rebuild
#: every few weeks; each one costs that 430 s window once.
#:
#: This is a *maintenance* trigger, never a read-path one:
#: :func:`ensure_memory_gram_index` leaves a stale base alone at any size, and only
#: :func:`maybe_rebuild_memory_gram_index` -- the scheduled block in the watchdog and
#: ``gram-index --if-due`` -- settles the debt.
REBUILD_AFTER_WRITES = 500

#: Cost basis for the amortisation branch of :func:`rebuild_due_reason`.
#:
#: Both numbers are measured on this corpus (2026-09-25), and both have moved since
#: :data:`REBUILD_AFTER_WRITES` was chosen:
#:
#: * a rebuild now costs **~77 s** (staging 58.0 + pack 16.5 + publish 2.0) on 69 929 documents;
#:   the ~430 s the older note cites belongs to a 146 679-document corpus;
#: * the indexed path saves **~0.46 s per memory search** (782 ms indexed against 1 240 ms on the
#:   exact projected scan, same day and corpus).
#:
#: So one rebuild is repaid after roughly **166 searches**, not the ~1 200 the write-count note
#: implies.  A write count cannot express that: it fires on churn instead of on debt incurred.  Its
#: visible failure: until 2026-09-25 the only caller was the 10:00/23:00 occurrence, so a churny
#: day left every memory search on the slow path for up to 13 hours -- measured that day: 9 212
#: live dirty documents, memory retrieval 1 240 ms instead of 782 ms.
REBUILD_COST_SECONDS = 77.0
SEARCH_SECONDS_SAVED = 0.46

#: Interactive-latency trigger, off by default, in **searches since the last rebuild**.
#:
#: The amortisation branch above decides on aggregate machine time: one rebuild is repaid after
#: ~166 searches, which is the right answer for a busy host and a poor one for an interactive one.
#: Measured state on 2026-09-29: 458 live dirty documents (below the 500 churn threshold), 8 searches
#: since the last rebuild, so nothing was scheduled while *every* memory-backed query paid the
#: ~0.46 s scan -- the same shape the docstring above calls out as a churny day's failure, reached
#: by the other gate.  A host that serves a human can prefer latency over machine time and set
#: ``VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES`` (e.g. 20); 0 keeps the historical behaviour, so the
#: default is not silently changed for hosts that never asked for the trade.
DEFAULT_LATENCY_REBUILD_AFTER_SEARCHES = 0


def latency_rebuild_after_searches() -> int:
    """The operator's interactive trigger, in searches, or 0 when it is disabled."""
    raw = os.environ.get("VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES", "")
    if not raw.strip():
        return DEFAULT_LATENCY_REBUILD_AFTER_SEARCHES
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        log.warning("Ignoring VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES=%r (not an integer).", raw)
        return DEFAULT_LATENCY_REBUILD_AFTER_SEARCHES


def _searches_since_last_rebuild() -> int | None:
    """Searches the ledger recorded since the base was last built, or ``None`` if unknowable.

    Search count is the unit the decision is actually denominated in -- the index either saves
    ~0.46 s per search or it does not -- but the lake keeps no query table, so the ledger is the
    only place a search is visible.  ``None`` (disabled ledger, unreadable file, no build stamp)
    leaves the write-count threshold as the only gate instead of pretending the count is zero.

    The comparison is lexicographic on ISO-8601 UTC: the base stamp is written by ``gmtime()`` as
    ``YYYY-MM-DDTHH:MM:SS`` and ledger entries carry ``...Z``-style ``+00:00``, so the shared prefix
    decides.  Two events in the same second can therefore count as one search of overshoot -- a
    second, against a 166-search bar.  Rotated ledger generations are not walked, so this is a
    floor, not a total.
    """
    try:
        from vector_lake import search_ledger

        since = str(gram_index_state().get("updated_at") or "")
        if not since:
            return None
        return sum(
            1
            for entry in search_ledger.entries()
            if str(entry.get("at") or "") > since
        )
    except Exception:  # noqa: BLE001 - only the gate is lost, never the cheaper fallback
        return None


_LONG_RUN = re.compile(r"[0-9a-z]{3,}")
# Existing native releases casefold ASCII runs and cannot accept lone surrogates.
_NATIVE_GRAM_UNSAFE = re.compile(r"[A-Z\ud800-\udfff]")

# Field-mask weights, indexed by mask (bit0 key, bit1 text, bit2 page).
#
# Exported because the full-scan oracle in ``governance_store._memory_relevance`` has to produce the
# same number from the same record, and two literals are how the two paths drift apart.
_FIELD_WEIGHTS = [0] * 16
for _mask in range(8):
    _FIELD_WEIGHTS[_mask] = 4 * (_mask & 1) + 3 * ((_mask >> 1) & 1) + ((_mask >> 2) & 1)
FIELD_WEIGHTS = tuple(_FIELD_WEIGHTS)


def idf_weight(doc_frequency: int, total_docs: int) -> float:
    """How much one matching term counts: ``log(1 + N/df)``.

    The scorer used to add a flat 4/3/1 per matching field, so a term present in tens of thousands
    of records counted exactly as much as a rare one -- measured, 24 packet items fell inside a
    0.65-0.70 band with only five distinct values.  A term the whole corpus contains now
    contributes about nothing, and the terms that decide an answer are the ones that discriminate.

    ``df`` comes from the postings on the index path and from the collection on the full-scan path;
    both are the number of documents containing the term, the only definition that keeps the two
    paths equal.
    """
    total = max(int(total_docs), 1)
    df = int(doc_frequency)
    if df <= 0:
        return 0.0
    return math.log(1.0 + total / df)


def extract_grams(key_blob: str, text_blob: str, page_blob: str) -> dict[str, int]:
    """Distinct grams of one document, mapped to the fields that contain them.

    Grams are the distinct single characters, the distinct adjacent bigrams and
    the distinct alphanumeric runs of length >= 3.  Characters and bigrams are
    what ``_query_terms`` produces; the runs are indexed so that a long Latin
    token also narrows to a single posting lookup.
    """
    blobs = (key_blob or "", text_blob or "", page_blob or "")
    if HAVE_CORE and not any(_NATIVE_GRAM_UNSAFE.search(blob) for blob in blobs):
        return vector_lake_core.extract_grams(*blobs)
    grams: dict[str, int] = {}
    for bit, blob in ((1, key_blob), (2, text_blob), (4, page_blob)):
        if not blob:
            continue
        seen: list[str] = list(blob)
        seen.extend(blob[i : i + 2] for i in range(len(blob) - 1))
        seen.extend(_LONG_RUN.findall(blob))
        for gram in seen:
            grams[gram] = grams.get(gram, 0) | bit
    return grams


def _pack(postings) -> bytes:
    """``[(doc, mask), ...]`` in ascending doc order -> packed little-endian blob."""
    if HAVE_CORE:
        return vector_lake_core.pack_postings(list(postings))
    from array import array

    values = array("I")
    previous = 0
    for doc, mask in postings:
        values.append(((doc - previous) << 4) | (mask & 0x0F))
        previous = doc
    return values.tobytes()


def _entries(blob: bytes):
    """Packed blob -> ``[(doc, mask), ...]``."""
    if HAVE_CORE:
        return vector_lake_core.unpack_postings(blob)
    from array import array

    values = array("I")
    values.frombytes(blob)
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    doc = 0
    out = []
    for value in values:
        doc += value >> 4
        out.append((doc, value & 0x0F))
    return out


def _accumulate(blob: bytes, weights: list[int], accumulator: dict, skip=None) -> None:
    """Fold one posting list into ``accumulator`` (``doc -> relevance``)."""
    if HAVE_CORE:
        res = vector_lake_core.accumulate_postings(blob, weights, set(skip) if skip else None)
        for doc, score in res.items():
            accumulator[doc] = accumulator.get(doc, 0) + score
        return
    from array import array

    values = array("I")
    values.frombytes(blob)
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    doc = 0
    if skip:
        for value in values:
            doc += value >> 4
            if doc in skip:
                continue
            mask = value & 0x0F
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]
    else:
        for value in values:
            doc += value >> 4
            mask = value & 0x0F
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]


# --- state ------------------------------------------------------------------


def _projection_doc_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0]


def gram_index_state() -> dict:
    conn = get_connection()
    row = conn.execute(
        "SELECT format_version, doc_count, gram_count, postings_count, base_docs, updated_at "
        "FROM operational_memory_gram_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return {"ready": False, "format_version": None, "doc_count": 0, "gram_count": 0,
                "postings_count": 0, "base_docs": 0, "updated_at": None, "base_present": False}
    state = dict(row)
    # The state row is a cache; confirm the base table actually holds something so
    # a truncated base degrades to the exact scan instead of answering "no hits".
    state["base_present"] = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM operational_memory_gram LIMIT 1)"
    ).fetchone()[0] == 1
    state["ready"] = (
        int(state["format_version"] or 0) == GRAM_FORMAT_VERSION
        and int(state["gram_count"] or 0) > 0
        and bool(state["base_present"])
    )
    return state


def pending_doc_count(conn=None) -> int:
    conn = conn or get_connection()
    return conn.execute("SELECT COUNT(*) FROM operational_memory_gram_dirty").fetchone()[0]


def live_dirty_doc_count(conn=None) -> int:
    """Dirty documents that still exist; the rest are markers for deleted ones."""
    conn = conn or get_connection()
    return conn.execute(
        "SELECT COUNT(*) FROM operational_memory_gram_dirty AS d "
        "WHERE EXISTS (SELECT 1 FROM operational_memory_gram_docs AS g "
        "JOIN operational_memory_index AS i ON i.memory_id = g.memory_id "
        "WHERE g.doc = d.doc)"
    ).fetchone()[0]


def retired_doc_count(conn=None) -> int:
    """Dirty markers for deleted documents, whose base postings must keep being skipped."""
    conn = conn or get_connection()
    return pending_doc_count(conn) - live_dirty_doc_count(conn)


def dirty_breakdown(conn=None) -> tuple[int, int, int]:
    """``(total, live, retired)`` for the queue, read in a single statement.

    Three separate ``COUNT`` calls can disagree: they are separate reads, and
    another connection clearing the queue between them makes ``retired`` come out
    negative.  Deriving all three from one snapshot cannot.
    """
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(EXISTS ("
        "SELECT 1 FROM operational_memory_gram_docs AS g "
        "JOIN operational_memory_index AS i ON i.memory_id = g.memory_id "
        "WHERE g.doc = d.doc"
        ")), 0) "
        "FROM operational_memory_gram_dirty AS d"
    ).fetchone()
    total, live = int(row[0]), int(row[1])
    return total, live, total - live


def skip_doc_set(conn=None) -> set[int]:
    """Documents whose base postings are stale (edited since the base, or deleted)."""
    conn = conn or get_connection()
    return {int(row[0]) for row in conn.execute("SELECT doc FROM operational_memory_gram_dirty")}


def _record_state(conn, doc_count: int, gram_count: int, postings_count: int, base_docs: int) -> None:
    conn.execute(
        "INSERT INTO operational_memory_gram_state "
        "(singleton, format_version, doc_count, gram_count, postings_count, base_docs, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "format_version = excluded.format_version, doc_count = excluded.doc_count, "
        "gram_count = excluded.gram_count, postings_count = excluded.postings_count, "
        "base_docs = excluded.base_docs, updated_at = excluded.updated_at",
        (
            GRAM_FORMAT_VERSION, doc_count, gram_count, postings_count, base_docs,
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        ),
    )


# --- maintenance ------------------------------------------------------------


def _document_digest(key_blob, text_blob, page_blob) -> str:
    """Digest of the three indexed fields as they were read.

    The staging pass records one of these per document.  At publish time the digest is
    recomputed for every document that still carries a change marker and compared: a match
    proves the projection row is byte-identical to what was staged, so its base postings are
    current and the marker can be dropped.  A mismatch -- or a missing stamp -- keeps it.

    Bytes rather than a timestamp, deliberately: ``updated_rank`` has millisecond precision, so
    two writes inside one millisecond compare equal and a timestamp fence would drop the marker
    for postings that are already stale.  That is exactly the silent staleness this index must
    never serve.
    """
    import hashlib

    digest = hashlib.sha256()
    for blob in (key_blob or "", text_blob or "", page_blob or ""):
        digest.update(str(blob).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def _drop_markers_whose_snapshot_is_current(conn) -> tuple[int, int]:
    """Drop the change markers the freshly published base has already accounted for.

    Returns ``(dropped, kept)``.  Three cases, and only the last is kept:

    * **live, digest matches** -- the projection row is byte-identical to what was staged, so the
      published postings are current and the marker is finished.
    * **retired before the snapshot** (absent from the projection and never staged) -- the base
      does not contain it at all, so there is nothing left for a marker to make the read path
      skip.  Dropping it is what lets a quiet rebuild drain the queue, which the previous
      implementation did by clearing the whole queue.
    * **staged, then deleted** (absent from the projection but present in the stamp table) -- the
      base holds the postings that were staged, so the marker has to stay: it is what makes the
      read path skip them.

    A live document that was written during the rebuild keeps its marker too (digest differs),
    and a document created during it has no stamp at all and is kept.
    """
    stamps = dict(conn.execute("SELECT doc, digest FROM operational_memory_gram_stamp"))
    rows = conn.execute(
        "SELECT d.doc AS doc, "
        "       i.memory_id AS memory_id, "
        "       i.key_blob AS key_blob, i.text_blob AS text_blob, i.page_blob AS page_blob "
        "FROM operational_memory_gram_dirty AS d "
        "LEFT JOIN operational_memory_gram_docs AS g ON g.doc = d.doc "
        "LEFT JOIN operational_memory_index AS i ON i.memory_id = g.memory_id"
    ).fetchall()
    if not rows:
        return 0, 0
    clean = []
    for row in rows:
        doc = row["doc"]
        stamp = stamps.get(doc)
        if row["memory_id"] is None:
            if stamp is None:
                clean.append(doc)
            continue
        if stamp and stamp == _document_digest(row["key_blob"], row["text_blob"], row["page_blob"]):
            clean.append(doc)
    if clean:
        conn.executemany(
            "DELETE FROM operational_memory_gram_dirty WHERE doc = ?", [(doc,) for doc in clean]
        )
    return len(clean), len(rows) - len(clean)


def rebuild_memory_gram_index(dry_run: bool = False, batch_docs: int = 2000) -> str:
    """Rebuild the base index from ``operational_memory_index``, batched, with a fenced publish.

    Staging happens in temporary tables so SQLite performs the external sort; holding 26 M
    postings in a Python dict would need gigabytes.

    **Why it is batched.**  Measured on the live corpus 2026-09-19, the staging pass (projection
    read + gram extraction + insert) is 436 s of a 465 s rebuild -- 94 %.  Running all of it in
    one transaction held the database write lock for those 436 s, which refused every other
    writer and made a routine maintenance step an availability event.  It now commits one short
    transaction per ``batch_docs`` batch (about 6 s at the measured rate), so the outbox consumer
    interleaves instead of being blocked, and the publish stays a single short transaction.

    **What replaced the lock.**  The single transaction also made the snapshot atomic, and that
    atomicity was load-bearing: publishing a base built from a mixed snapshot while clearing the
    whole change queue would erase the marker for a document whose staged postings are stale, and
    the read path would then serve it as current.  The fence replaces the exclusion: every staged
    document records a digest of the bytes it was read from, and the publish drops a change marker
    only when the document still digests identically.  Anything written during the rebuild keeps
    its marker, so :func:`gram_index_usable` stays false until a quiet rebuild -- the same honest
    degradation as before, without the write-lock hold.
    """
    conn = get_connection()
    init_db()
    if dry_run:
        state = gram_index_state()
        return (
            f"[DRY RUN] Would rebuild the memory gram index from "
            f"{_projection_doc_count(conn)} document(s); the current base covers "
            f"{int(state['base_docs'] or 0)} document(s) with "
            f"{int(state['gram_count'] or 0)} gram(s) and "
            f"{pending_doc_count(conn)} pending change(s)."
        )

    scratch_tables = (
        "operational_memory_gram_stage",
        "operational_memory_gram_stamp",
        "operational_memory_gram_new",
    )
    # Bulk load: an auto-checkpoint after every commit copies WAL pages back into the main
    # database in the committing thread, and this rebuild commits once per batch.  Deferring it
    # to the scheduled ``wal_checkpoint(TRUNCATE)`` that follows the rebuild removes that I/O
    # without changing durability (the WAL is still written and replayed).
    previous_autocheckpoint = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    conn.execute("PRAGMA wal_autocheckpoint=0")
    phase_seconds: dict[str, float] = {}

    def _phase(name: str, started_at: float) -> None:
        phase_seconds[name] = time.perf_counter() - started_at

    for scratch in scratch_tables:
        conn.execute(f"DROP TABLE IF EXISTS {scratch}")
    # Every document in the base needs an identity before it can be staged, and a rebuild is the
    # only place that can allocate the missing ones in bulk: a write allocates its own through
    # the insert trigger, but a database written by a release that had no identity table has
    # none for its existing corpus.  ``INSERT OR IGNORE`` rather than the trigger's ``NOT EXISTS``
    # guard because this is a top-level statement, not a nested trigger body.
    #
    # The queue is then cleared when it was written under a different format version: its numbers
    # name the *previous* base's document namespace, and this rebuild discards that base whole, so
    # every marker in it is moot.  Keeping them would resolve them against identities they were
    # never about -- over-conservatively at best, and this is the one rebuild that cannot afford
    # to leave the index unusable afterwards, since the operator has to run it by hand.
    previous_format = gram_index_state().get("format_version")
    with transaction():
        conn.execute(
            "INSERT OR IGNORE INTO operational_memory_gram_docs (memory_id) "
            "SELECT memory_id FROM operational_memory"
        )
        if previous_format != GRAM_FORMAT_VERSION:
            conn.execute("DELETE FROM operational_memory_gram_dirty")
    conn.execute(
        "CREATE TABLE operational_memory_gram_stage ("
        "gram TEXT NOT NULL, doc INTEGER NOT NULL, mask INTEGER NOT NULL, "
        "PRIMARY KEY (gram, doc)) WITHOUT ROWID"
    )
    conn.execute(
        "CREATE TABLE operational_memory_gram_stamp ("
        "doc INTEGER PRIMARY KEY, digest TEXT NOT NULL) WITHOUT ROWID"
    )
    conn.execute(
        "CREATE TABLE operational_memory_gram_new (gram TEXT PRIMARY KEY, postings BLOB NOT NULL)"
    )

    # Phase 1 -- staged postings plus the per-document fence, one committed transaction per
    # batch.  Keyset pagination on ``rowid`` with the cursor closed before the commit: SQLite
    # plans ``rowid > ?`` as an INTEGER PRIMARY KEY search, and closing it matters.  A read
    # snapshot that spans a commit by another connection cannot be upgraded to a write
    # transaction in WAL mode (SQLITE_BUSY_SNAPSHOT), so a long-lived cursor here makes the next
    # batch fail closed for the whole lock budget -- reproduced as a 20 s timeout with an
    # external 0.16 s write in between.
    staged_docs = 0
    last_doc = 0
    _started = time.perf_counter()
    while True:
        # Keyset pagination on the *identity* number, not on the projection's rowid: the projection
        # can be re-derived while this runs, which renumbers its rows but not a document's identity.
        rows = conn.execute(
            "SELECT g.doc AS doc, i.key_blob, i.text_blob, i.page_blob "
            "FROM operational_memory_gram_docs AS g "
            "JOIN operational_memory_index AS i ON i.memory_id = g.memory_id "
            "WHERE g.doc > ? ORDER BY g.doc LIMIT ?",
            (last_doc, max(1, int(batch_docs))),
        ).fetchall()
        if not rows:
            break
        payload: list[tuple[str, int, int]] = []
        stamps: list[tuple[int, str]] = []
        for row in rows:
            blobs = (row["key_blob"] or "", row["text_blob"] or "", row["page_blob"] or "")
            grams = extract_grams(*blobs)
            payload.extend((gram, row["doc"], mask) for gram, mask in grams.items())
            stamps.append((row["doc"], _document_digest(*blobs)))
        with transaction():
            if payload:
                # Sorted inserts, not incidentally: the staging table is a WITHOUT ROWID b-tree
                # keyed on ``(gram, doc)`` while the payload is built in document order, so an
                # unsorted batch touches scattered leaf pages for every row.  Measured on a 5 M-row
                # b-tree, one 360 000-row batch costs 3.32 s unsorted against 0.66 s sorted -- and
                # the gap widens with the table, which is what put a ~21 s write-lock hold inside
                # a late batch of the live rebuild.
                payload.sort()
                conn.executemany(
                    "INSERT OR REPLACE INTO operational_memory_gram_stage (gram, doc, mask) VALUES (?, ?, ?)",
                    payload,
                )
            conn.executemany(
                "INSERT OR REPLACE INTO operational_memory_gram_stamp (doc, digest) VALUES (?, ?)",
                stamps,
            )
        last_doc = rows[-1]["doc"]
        staged_docs += len(rows)
    _phase("stage", _started)

    # Phase 2 -- pack staging into a private next-generation base, one transaction per chunk of
    # *whole* gram groups.  The chunk bounds come from the ordered gram list, so a group is
    # never split across chunks: paging on ``gram > last`` alone would silently drop the rest of
    # a group that straddled the boundary, and carrying the buffer across chunks instead means
    # re-reading groups.  ``gram >= ? AND gram <= ?`` is a range search on the primary key, which
    # a compound ``(gram, doc) > (?, ?)`` keyset is not -- that planned as a full scan and made
    # the rebuild 2.4x slower.
    gram_list = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT gram FROM operational_memory_gram_stage ORDER BY gram"
        )
    ]
    gram_count = postings_count = 0
    _started = time.perf_counter()
    for start_index in range(0, len(gram_list), PACK_CHUNK_GRAMS):
        chunk = gram_list[start_index : start_index + PACK_CHUNK_GRAMS]
        rows = conn.execute(
            "SELECT gram, doc, mask FROM operational_memory_gram_stage "
            "WHERE gram >= ? AND gram <= ? ORDER BY gram, doc",
            (chunk[0], chunk[-1]),
        ).fetchall()
        pending: list[tuple[str, bytes]] = []
        buffer: list[tuple[int, int]] = []
        current_gram: str | None = None
        for gram, doc, mask in rows:
            if gram != current_gram:
                if current_gram is not None:
                    pending.append((current_gram, _pack(buffer)))
                    gram_count += 1
                    postings_count += len(buffer)
                current_gram = gram
                buffer = []
            buffer.append((doc, mask))
        if current_gram is not None:
            pending.append((current_gram, _pack(buffer)))
            gram_count += 1
            postings_count += len(buffer)
        if pending:
            with transaction():
                conn.executemany(
                    "INSERT OR REPLACE INTO operational_memory_gram_new (gram, postings) VALUES (?, ?)",
                    pending,
                )
    _phase("pack", _started)

    # Phase 3 -- publish.  One short transaction: fence the change markers, swap the new base in
    # by DDL, and record the state.  ``DELETE`` + ``INSERT ... SELECT`` was tried first and
    # costs ~20 s for 421 575 grams / 105 MB of blobs inside the transaction -- a window longer
    # than the 20 s writer-lock budget, so a concurrent writer could still be refused once per
    # rebuild (measured: a 23.5 s lock wait).  ``DROP`` + ``RENAME`` is atomic, transactional and
    # effectively free; nothing references the table by name (the change markers live in
    # ``operational_memory_gram_dirty``), and the renamed table brings its own index.
    _started = time.perf_counter()
    with transaction():
        dropped, kept = _drop_markers_whose_snapshot_is_current(conn)
        conn.execute("DROP TABLE operational_memory_gram")
        conn.execute("ALTER TABLE operational_memory_gram_new RENAME TO operational_memory_gram")
        _record_state(conn, staged_docs, gram_count, postings_count, staged_docs)
        for scratch in ("operational_memory_gram_stage", "operational_memory_gram_stamp"):
            conn.execute(f"DROP TABLE IF EXISTS {scratch}")
    _phase("publish", _started)
    try:
        conn.execute(f"PRAGMA wal_autocheckpoint={int(previous_autocheckpoint or 1000)}")
    except sqlite3.Error as exc:  # noqa: BLE001 - a lost pragma only costs I/O later
        log.warning("Could not restore wal_autocheckpoint: %s", exc)
    # Reclaim what deferring the auto-checkpoint just accumulated.  Without this a manual
    # ``cli.py gram-index --apply`` left a multi-gigabyte WAL until the next scheduled
    # occurrence -- measured at 2 051 MB after one live rebuild.  Best effort: the scheduled
    # block is the backstop, and a busy reader legitimately defers the truncate.
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            log.info("Rebuild finished with a deferred WAL checkpoint (busy=%s pages=%s)", *checkpoint[:2])
    except sqlite3.Error as exc:
        log.warning("Post-rebuild WAL checkpoint skipped: %s", exc)
    log.info(
        "Gram rebuild phases: %s (batch_docs=%d)",
        ", ".join(f"{name}={seconds:.1f}s" for name, seconds in phase_seconds.items()),
        batch_docs,
    )

    fence = (
        f" {kept} document(s) written during the rebuild kept their change marker "
        f"({dropped} cleared by the snapshot fence)."
        if kept
        else ""
    )
    return (
        f"Rebuilt the memory gram index: {gram_count} gram(s), {postings_count} posting(s) "
        f"over {staged_docs} document(s).{fence}"
    )


# --- readiness --------------------------------------------------------------


def gram_index_usable() -> bool:
    """True when the base is a complete, current snapshot and can answer exactly.

    What this reports on is exactness, not speed, so the base has to be the only
    thing an answer could come from: a **live document in the queue** has base
    postings that are stale -- they still contain grams the document no longer has --
    so serving the base credits a document for terms it dropped, and the read path has
    no way to tell.

    ``retired`` markers are harmless: they name deleted documents, whose base
    postings the read path skips outright, so they cannot contribute.  That is why
    the queue is read as a breakdown rather than as a total.

    There used to be a second condition -- no row in the overlay table -- because that
    table let a document's postings live in two structures at once while the read path
    summed them as if the overlay replaced the base.  The table and its machinery are gone;
    what stands in its place is the schema check in :func:`ensure_memory_gram_index`, which
    refuses to serve a database where that drop has not been applied.  Without it the
    predicate would be weaker than the previous release for exactly one state: an overlay
    table left with rows by a process running a removed release.

    No incremental step restores this condition, and none exists any more: the only
    thing that changes the base is :func:`rebuild_memory_gram_index`, which is the
    documented recovery.  Dispatching on the degraded state instead is what
    :func:`rebuild_due_reason` is for.
    """
    try:
        if not gram_index_state()["ready"]:
            return False
        _, live, _ = dirty_breakdown()
        return live == 0
    except Exception:  # noqa: BLE001 - a missing table simply means "not usable"
        return False


def ensure_memory_gram_index() -> bool:
    """Report whether the index can answer exactly, building an absent base first.

    This used to drain a bounded backlog by materialising documents into an overlay and
    then merging some of it back.  That is the sequence that produced the wrong answers
    :func:`gram_index_usable` now describes: the materialisation declared a document's
    stale base postings authoritative, so a document was credited for a gram it no longer
    contained, and merging afterwards did not reach that gram.  No drain can restore
    exactness -- which is why that machinery has been removed rather than left unused, and
    why the read path does not attempt one.

    A *stale* base is deliberately left alone: the caller falls back to the exact
    scan, which is cheaper than a rebuild for any corpus
    :data:`AUTO_REBUILD_MAX_DOCS` admits.  Only an *absent* base -- never built, a
    truncated table, or an older :data:`GRAM_FORMAT_VERSION` -- is built here, and
    only for a corpus small enough for the build to be bounded, because a database
    with no base has nothing to serve and every search would otherwise be exact-scanned
    forever.
    """
    conn = get_connection()
    try:
        init_db()
        # Serving is only allowed once the schema has converged past the overlay table.  The
        # table is dropped by a recorded migration, so a database that still has it -- a
        # deferred prune, a read-only snapshot, or a writer from a removed release that
        # recreated it -- degrades to the exact scan instead of answering from a base whose
        # postings may be split across two structures.
        if db_store.GRAM_OVERLAY_DROP not in db_store.applied_schema_prunes():
            return False
        if gram_index_usable():
            return True
        if not gram_index_state()["ready"] and _projection_doc_count(conn) <= AUTO_REBUILD_MAX_DOCS:
            rebuild_memory_gram_index()
        return gram_index_usable()
    except Exception as exc:  # noqa: BLE001 - any fault must degrade, never raise
        log.warning("Memory gram index unusable (%s: %s); falling back to the exact scan.", type(exc).__name__, exc)
        return False


def writes_since_rebuild(conn=None) -> int:
    """Live dirty documents: those whose base postings no longer match them.

    The count is in documents rather than transactions because that is what a search
    pays for: each such document makes :func:`gram_index_usable` false and sends every
    later search down the exact scan.  Retired markers are excluded -- they name deleted
    documents, whose base postings the read path skips outright.
    """
    _, live, _ = dirty_breakdown(conn)
    return live


def rebuild_due_reason(conn=None) -> str | None:
    """Why a rebuild is due, or ``None`` when it is not.

    Three reasons.  Two are not matters of degree: a base that cannot answer a search at all --
    absent (never built, truncated), written by an older :data:`GRAM_FORMAT_VERSION` -- is due
    whatever the write count says.  A write-count threshold alone reports "not due" for that state
    *forever*, and that is exactly the state a format version bump or a restore leaves a large
    corpus in: the read path refuses to build an absent base above :data:`AUTO_REBUILD_MAX_DOCS`,
    so nothing else would ever settle it.  Reporting the reason rather than a bare boolean is also
    what keeps the dry-run text honest, since "0 documents written" cannot explain why a rebuild is
    proposed.

    The third reason is the amortisation one, and it exists because the first two are consulted only
    when someone asks: writes answer "has the corpus churned", never "is the slow path being paid
    for".  With :data:`REBUILD_COST_SECONDS` and :data:`SEARCH_SECONDS_SAVED` measured, the debt is
    computable, so it is compared instead of guessed.
    """
    conn = conn or get_connection()
    try:
        state = gram_index_state()
        if not state["ready"]:
            return "no usable base (never built, truncated, or an older format version)"
    except sqlite3.OperationalError:
        # The index tables do not exist yet.  That is a real state on a database whose
        # first writer has not run init_db, and a rebuild is what creates them.
        return "the index tables are absent"
    live = writes_since_rebuild(conn)
    max_age = float(os.environ.get("VECTOR_LAKE_MEMORY_GRAM_MAX_BASE_AGE_SECONDS", "3600"))
    if max_age < 0:
        raise ValueError("gram maximum base age must be non-negative")
    if live > 0 and max_age > 0:
        stamp = state.get("updated_at")
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            # Existing state stamps were written with gmtime(), without an offset.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = time.time() - parsed.timestamp()
        except (TypeError, ValueError):
            return "dirty base has no valid rebuild timestamp"
        if age >= max_age:
            return f"dirty base age {age:.0f}s exceeds maximum {max_age:.0f}s ({live} pending documents)"
    if live >= REBUILD_AFTER_WRITES:
        return f"{live} document(s) written since the last rebuild (threshold {REBUILD_AFTER_WRITES})"
    # Interactive host: has the slow path been paid for often enough to justify a rebuild before the
    # churn threshold?  Kept separate from the amortisation branch so the two policies stay legible.
    latency_bar = latency_rebuild_after_searches()
    if live > 0 and latency_bar > 0:
        searched_now = _searches_since_last_rebuild()
        if searched_now is not None and searched_now >= latency_bar:
            return (
                f"the indexed path is off with {live} document(s) pending and {searched_now} "
                f"search(es) since the last rebuild (interactive bar {latency_bar}); each of those "
                f"paid ~{SEARCH_SECONDS_SAVED}s on the projected scan"
            )
    if live > 0:
        searched = _searches_since_last_rebuild()
        if searched is not None:
            saved = searched * SEARCH_SECONDS_SAVED
            if saved >= REBUILD_COST_SECONDS:
                return (
                    f"{live} document(s) written and {searched} search(es) since the last rebuild; "
                    f"at {SEARCH_SECONDS_SAVED}s saved per search that is {saved:.0f}s of slow path "
                    f"against a ~{REBUILD_COST_SECONDS:.0f}s rebuild"
                )
    return None


def rebuild_due(conn=None) -> bool:
    """True when a rebuild is due: the index is behind, or it cannot serve at all."""
    return rebuild_due_reason(conn) is not None


def maybe_rebuild_memory_gram_index(dry_run: bool = False) -> str:
    """Rebuild if the index has fallen far enough behind, otherwise say it has not.

    The cadence's entry point.  A stale base is never rebuilt from the read path, so the
    debt is settled here: the scheduled maintenance block in the watchdog and
    ``gram-index --if-due`` both run when holding the write lock for the length of a build
    is acceptable.  The watchdog runs this *before* its WAL checkpoint, which is what
    reclaims the WAL this transaction grows.
    """
    reason = rebuild_due_reason()
    if reason is None:
        return (
            f"Memory gram index is not due: {writes_since_rebuild()} document(s) written "
            f"since the last rebuild, threshold {REBUILD_AFTER_WRITES}."
        )
    if dry_run:
        total, _, retired = dirty_breakdown()
        return (
            f"Memory gram index is due: {reason}; {retired} retired marker(s) of {total} "
            "queued. Rebuild with `gram-index --if-due --apply`."
        )
    return rebuild_memory_gram_index(dry_run=False)


# --- query ------------------------------------------------------------------


def _term_base(gram: str, conn) -> bytes | None:
    row = conn.execute(
        "SELECT postings FROM operational_memory_gram WHERE gram = ?", (gram,)
    ).fetchone()
    return row[0] if row else None


def term_document_frequencies(terms: list[str], skip_docs: set[int] | None = None,
                              docs: set[int] | None = None) -> dict[str, int]:
    """How many documents contain each term, counted over the three indexed fields.

    ``memory_type`` is deliberately excluded: the postings carry no type bit, so a df that counted
    type matches would be one the index cannot reproduce -- and the full-scan oracle has to produce
    the same factor for the two paths to stay equal.

    ``docs`` restricts the count to a document set (an authorized scope).  A scoped search needs
    document frequency *inside its scope* -- that is the number the payload-based oracle computes --
    and the postings are corpus-wide, so the matched set is intersected before it is counted.
    """
    conn = get_connection()
    frequencies: dict[str, int] = {}
    for term in terms:
        if not term or term in frequencies:
            continue
        folded: dict[int, int] = {}
        if len(term) <= 2:
            blob = _term_base(term, conn)
            if blob is not None:
                _accumulate(blob, _FIELD_WEIGHTS, folded, skip_docs)
        else:
            _accumulate_composite([term], _FIELD_WEIGHTS, folded)
        if docs is not None:
            frequencies[term] = sum(1 for doc in folded if doc in docs)
        else:
            frequencies[term] = len(folded)
    return frequencies


def accumulate_relevance(
    terms: list[str],
    skip_docs: set[int] | None = None,
    total_docs: int | None = None,
    df: dict[str, int] | None = None,
    docs: set[int] | None = None,
) -> dict[int, float]:
    """Exact relevance per document id for ``terms``, optionally restricted to ``docs``.

    ``skip_docs`` holds dirty documents whose base postings are stale.  They are credited
    nothing, not corrected: the queue suppresses them, so a search that finds any live
    dirty document is refused before it gets here (see :func:`gram_index_usable`).

    ``docs`` keeps only the given document ids.  The accumulation runs corpus-wide and the result
    is filtered, which is exact because a document's score is a function of its own postings and
    the weights: nothing in it depends on which other documents are admitted.
    """
    conn = get_connection()
    weights = _FIELD_WEIGHTS
    total = int(total_docs or gram_index_state().get("base_docs") or 0)
    frequencies = df if df is not None else term_document_frequencies(terms, skip_docs)
    if HAVE_CORE and all(not term or len(term) <= 2 for term in terms):
        term_blobs = []
        factors = {}
        for term in terms:
            if term:
                blob = _term_base(term, conn)
                if blob is not None:
                    term_blobs.append((term, blob))
                    factors[term] = idf_weight(frequencies.get(term, 0), total)
        # The native batch omits zero-IDF documents; the legacy scorer retains them.
        # Mixed long terms also keep their existing per-term reduction order.
        if all(factor > 0.0 for factor in factors.values()):
            scored = vector_lake_core.fast_accumulate_terms(term_blobs, factors, skip_docs, weights)
            return {doc: value for doc, value in scored.items() if docs is None or doc in docs}
    accumulator: dict[int, float] = {}

    def _fold(term: str, folded: dict[int, int]) -> None:
        """Scale one term's matches by its inverse document frequency, then merge."""
        if not folded:
            return
        factor = idf_weight(frequencies.get(term, 0), total)
        for doc, weight in folded.items():
            accumulator[doc] = accumulator.get(doc, 0.0) + factor * weight

    for term in terms:
        if not term:
            continue
        folded: dict[int, int] = {}
        if len(term) <= 2:
            blob = _term_base(term, conn)
            if blob is not None:
                _accumulate(blob, weights, folded, skip_docs)
        else:
            # One term at a time, because *this* term's document frequency is what scales its
            # matches; folding several together would apply one factor to all of them.
            _accumulate_composite([term], weights, folded)
        _fold(term, folded)
    if docs is not None:
        return {doc: value for doc, value in accumulator.items() if doc in docs}
    return accumulator


def fused_term_frequencies_and_relevance(
    terms: list[str],
    skip_docs: set[int] | None = None,
    total_docs: int | None = None,
    docs: set[int] | None = None,
) -> tuple[dict[str, int], dict[int, float]]:
    """Compute (df, relevance) in a single pass over postings without redundant decoding.

    Delegates short term postings decoding, scope filtering, and IDF accumulation directly
    to vector_lake_core.fast_fused_frequencies_and_relevance when available.
    """
    conn = get_connection()
    weights = _FIELD_WEIGHTS
    total = int(total_docs or gram_index_state().get("base_docs") or 0)

    # Separate short terms (len <= 2) and composite long terms (len > 2)
    short_terms = [t for t in terms if t and len(t) <= 2]
    long_terms = [t for t in terms if t and len(t) > 2]

    frequencies: dict[str, int] = {}
    accumulator: dict[int, float] = {}

    if HAVE_CORE and hasattr(vector_lake_core, "fast_fused_frequencies_and_relevance"):
        term_blobs: list[tuple[str, bytes]] = []
        for t in short_terms:
            blob = _term_base(t, conn)
            if blob is not None:
                term_blobs.append((t, blob))
            else:
                frequencies[t] = 0
        if term_blobs:
            df_rust, rel_rust = vector_lake_core.fast_fused_frequencies_and_relevance(
                term_blobs, total, skip_docs or None, docs or None, list(weights)
            )
            frequencies.update(df_rust)
            accumulator.update(rel_rust)
            for t in short_terms:
                if t not in frequencies:
                    frequencies[t] = 0
    else:
        # Fallback pure-Python fused pass
        decoded_blobs: dict[str, dict[int, int] | None] = {}
        for term in short_terms:
            if term in decoded_blobs:
                continue
            blob = _term_base(term, conn)
            if blob is None:
                decoded_blobs[term] = None
                frequencies[term] = 0
                continue
            if HAVE_CORE:
                decoded_blobs[term] = vector_lake_core.accumulate_postings(blob, weights, skip_docs or None)
            else:
                d: dict[int, int] = {}
                _accumulate(blob, weights, d, skip_docs)
                decoded_blobs[term] = d

            if docs is not None:
                frequencies[term] = sum(1 for doc in decoded_blobs[term] if doc in docs)
            else:
                frequencies[term] = len(decoded_blobs[term])

        for term in short_terms:
            folded = decoded_blobs.get(term)
            if not folded:
                continue
            factor = idf_weight(frequencies.get(term, 0), total)
            if factor <= 0.0:
                continue
            for doc, weight in folded.items():
                if docs is None or doc in docs:
                    accumulator[doc] = accumulator.get(doc, 0.0) + factor * weight

    # Composite terms (length > 2) verified via bigrams
    for term in long_terms:
        d = {}
        _accumulate_composite([term], weights, d)
        if term not in frequencies:
            df = sum(1 for doc in d if doc in docs) if docs is not None else len(d)
            frequencies[term] = df
        else:
            df = frequencies[term]

        factor = idf_weight(df, total)
        if factor <= 0.0:
            continue
        for doc, weight in d.items():
            if docs is None or doc in docs:
                accumulator[doc] = accumulator.get(doc, 0.0) + factor * weight

    return frequencies, accumulator


def _composite_candidates(term: str, conn) -> set[int]:
    """Documents that could contain ``term`` (every constituent bigram present)."""
    bigrams = {term[i : i + 2] for i in range(len(term) - 1)}
    blobs = []
    for bigram in bigrams:
        blob = _term_base(bigram, conn)
        if not blob:
            return set()
        blobs.append(blob)
    native = getattr(vector_lake_core, "intersect_postings", None) if HAVE_CORE else None
    if native is not None and sum(map(len, blobs)) <= 64 * 1024 * 1024:
        return set(native(blobs))
    candidates: set[int] | None = None
    for blob in sorted(blobs, key=len):
        docs = {doc for doc, _ in _entries(blob)}
        if candidates is None:
            candidates = docs
        else:
            candidates.intersection_update(docs)
        if not candidates:
            return set()
    return candidates or set()


def _accumulate_composite(terms: list[str], weights: list[int], accumulator: dict) -> None:
    """Resolve terms of length >= 3 by bigram intersection plus exact verification."""
    conn = get_connection()
    candidates = {term: _composite_candidates(term, conn) for term in terms}
    needed: set[int] = set()
    for docs in candidates.values():
        needed.update(docs)
    if not needed:
        return
    blobs: dict[int, tuple[str, str, str]] = {}
    ordered = sorted(needed)
    for start in range(0, len(ordered), 4000):
        chunk = ordered[start : start + 4000]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT g.doc AS doc, i.key_blob, i.text_blob, i.page_blob "
            f"FROM operational_memory_gram_docs AS g "
            f"JOIN operational_memory_index AS i ON i.memory_id = g.memory_id "
            f"WHERE g.doc IN ({placeholders})",
            chunk,
        ):
            blobs[int(row["doc"])] = (row["key_blob"], row["text_blob"], row["page_blob"])
    for term in terms:
        for doc in candidates[term]:
            fields = blobs.get(doc)
            if fields is None:
                continue
            mask = 0
            if term in fields[0]:
                mask |= 1
            if term in fields[1]:
                mask |= 2
            if term in fields[2]:
                mask |= 4
            if mask:
                accumulator[doc] = accumulator.get(doc, 0) + weights[mask]


def backend_requested() -> str:
    return str(os.environ.get("VECTOR_LAKE_MEMORY_SEARCH", "gram") or "gram").strip().lower()


def memory_gram_index_report() -> str:
    """Human-readable status for the operator surfaces.

    The verdict leads, because ``ready=True`` describes the *base* while ``usable``
    describes what a search actually gets; reporting the base first produced a line that
    read as healthy on a lake where every memory retrieval was falling back to the
    O(rows x terms) scan.  When the index cannot serve, the line names the cost the caller
    is paying and the remedy.
    """
    conn = get_connection()
    init_db()
    state = gram_index_state()
    usable = gram_index_usable()
    total, live, retired = dirty_breakdown(conn)
    if usable:
        verdict = "usable=True (memory search is served by the n-gram index)"
    else:
        verdict = (
            "usable=False (memory search is falling back to the exact projected scan; "
            "rebuild with `python cli.py gram-index --if-due --apply`)"
        )
        # The read path refuses at one dirty document while maintenance only rebuilds at
        # REBUILD_AFTER_WRITES, so this range is a state where nothing is scheduled to
        # happen.  Naming it is the difference between a known trade and a silent one.
        if live and not rebuild_due(conn):
            verdict += (
                f" -- {live} document(s) written, {REBUILD_AFTER_WRITES} needed before a "
                "rebuild is due, so the indexed path stays off until then"
            )
    return (
        f"memory gram index: {verdict} "
        f"ready={state['ready']} format={state['format_version']} "
        f"grams={int(state['gram_count'] or 0)} base_docs={int(state['base_docs'] or 0)} "
        f"projection_docs={_projection_doc_count(conn)} "
        f"pending={total} (live={live} retired={retired}) usable={usable} "
        f"backend={backend_requested()}"
    )
