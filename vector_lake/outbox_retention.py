"""Retention for ``mutation_outbox``: free the payload weight, keep the dedup record.

Measured 2026-09-25 on the live lake: 41 787 rows, of which 41 679 ``completed`` and 108
``superseded``, holding **79.6 MB** of ``payload_text`` (every row carries the page content it wrote),
going back to 2026-07-12.  Nothing pruned it.

Why this nulls the payload instead of deleting the row -- three readers depend on the row itself:

* ``enqueue_mutation`` matches ``idempotency_key`` against **terminal** rows and *revives* the match
  (``status='pending'`` with the new payload) rather than inserting a second row.  Deleting old rows
  would turn a repeated logical write into a second mutation;
* ``is_managed_projection_state`` takes the latest row for a filename and compares its payload to
  decide whether an incoming filesystem event is the daemon's own write (echo suppression).  The row
  must exist; only rows older than the window lose their payload, and by then the latest row for any
  file actually being written is a fresher one;
* ``_ensure_idempotency_index`` builds its unique index over *non-terminal* states only, so a
  retention sweep cannot collide with it.  Nothing here touches a non-terminal row.

``payload_text`` is the only column with real weight (filename/idempotency_key/status/timestamps are
small), so nulling it recovers essentially all of the 79.6 MB while leaving the ledger's meaning
intact.  The space lands on SQLite's freelist -- ``auto_vacuum=0`` here, so the file only shrinks
when :func:`vector_lake.db_store.reclaim_free_space` runs.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("vector-lake-outbox-retention")

#: Terminal rows older than this lose their payload.  Long enough that a retry or a replayed repair
#: from the same working session still revives the original row; short enough to bound the file.
DEFAULT_KEEP_DAYS = 30
TERMINAL_STATES = ("completed", "superseded", "cancelled", "failed")


def keep_days() -> int:
    """``VECTOR_LAKE_OUTBOX_PAYLOAD_KEEP_DAYS``, or :data:`DEFAULT_KEEP_DAYS`."""
    raw = str(os.environ.get("VECTOR_LAKE_OUTBOX_PAYLOAD_KEEP_DAYS", "")).strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        return max(0, int(float(raw)))
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_OUTBOX_PAYLOAD_KEEP_DAYS=%r", raw)
        return DEFAULT_KEEP_DAYS


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def plan_outbox_payload_retention(conn=None, days: int | None = None) -> dict:
    """Read-only: what a sweep would free, and what it would leave alone."""
    from vector_lake import db_store

    conn = conn or db_store.get_connection()
    window = keep_days() if days is None else int(days)
    cutoff = _cutoff(window)
    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    row = conn.execute(
        "SELECT COUNT(*) AS rows_, COALESCE(SUM(LENGTH(payload_text)), 0) AS bytes_"
        f" FROM mutation_outbox WHERE status IN ({placeholders})"
        " AND payload_text IS NOT NULL AND COALESCE(completed_at, started_at, created_at) < ?",
        (*TERMINAL_STATES, cutoff),
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM mutation_outbox WHERE payload_text IS NOT NULL"
    ).fetchone()
    return {
        "keep_days": window,
        "cutoff": cutoff,
        "rows_to_strip": int(row["rows_"]),
        "bytes_to_strip": int(row["bytes_"]),
        "rows_with_payload": int(total["n"]),
    }


def prune_outbox_payloads(conn=None, days: int | None = None, dry_run: bool = False) -> dict:
    """Null the payload of terminal rows outside the window.  Never deletes or moves a row."""
    from vector_lake import db_store

    conn = conn or db_store.get_connection()
    plan = plan_outbox_payload_retention(conn, days)
    if dry_run or plan["rows_to_strip"] == 0:
        return {**plan, "dry_run": True, "stripped": 0}

    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    has_digest = "payload_sha256" in db_store._table_columns(conn, "mutation_outbox")
    with db_store.transaction():
        if has_digest:
            rows_to_digest = conn.execute(
                "SELECT id, payload_text FROM mutation_outbox"
                f" WHERE status IN ({placeholders}) AND payload_text IS NOT NULL"
                " AND payload_sha256 IS NULL"
                " AND COALESCE(completed_at, started_at, created_at) < ?",
                (*TERMINAL_STATES, plan["cutoff"]),
            ).fetchall()
            if rows_to_digest:
                conn.executemany(
                    "UPDATE mutation_outbox SET payload_sha256 = ? WHERE id = ?",
                    [
                        (hashlib.sha256(str(row["payload_text"]).encode("utf-8")).hexdigest(), int(row["id"]))
                        for row in rows_to_digest
                    ],
                )
        cursor = conn.execute(
            "UPDATE mutation_outbox SET payload_text = NULL"
            f" WHERE status IN ({placeholders}) AND payload_text IS NOT NULL"
            " AND COALESCE(completed_at, started_at, created_at) < ?",
            (*TERMINAL_STATES, plan["cutoff"]),
        )
        stripped = cursor.rowcount
    log.info(
        "Outbox payload retention: stripped %d row(s) older than %s day(s), ~%.1f MB freed to the freelist.",
        stripped, plan["keep_days"], plan["bytes_to_strip"] / 1e6,
    )
    return {**plan, "dry_run": False, "stripped": int(stripped)}
