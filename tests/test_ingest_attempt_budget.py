"""A deterministically rejecting ingest task must become terminal, not retry forever.

The runner's failure branches only incremented a local counter, so the job stayed in
``subagent_processing`` holding a one-hour lease and ``claim_ingest_tasks`` (which re-claims
the oldest first) re-ran the same failing model call every hour indefinitely.  Observed live
on 2026-09-19: ``Error finalizing ingestion: Strict Naming Violation:
'Concept_Formula_售前技能评估四维十二项评分.md'`` at ``errors: 2`` with nothing bounding it.

``scripts/ingest_runner.py`` already documented "C3 bounded attempts; never re-claim a job to
try once more".  These tests pin that the budget is consumed and that the cap is one value
shared by the dispatcher and the recorder.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from vector_lake import db_store, tool_ingest


def _enqueue(conn, job_id="job-deadletter"):
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO jobs "
            "(job_id, task_type, payload, status, retries, error_msg, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, "ingest", json.dumps({"filepath": "x.md"}), "queued", 0, "",
             datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    return job_id


def _make_dispatchable(conn, job_id):
    """Move the backoff out of the way so the assertion is about the cap, not the clock."""
    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET available_at = ? WHERE job_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id),
        )


def test_the_attempt_budget_is_consumed_and_then_the_job_is_terminal(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    job_id = _enqueue(conn)

    message = tool_ingest.record_ingest_failure(job_id, "finalize rejected: strict naming")
    assert "1/3" in message
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["retries"] == 1
    assert row["status"] == "failed"
    assert "strict naming" in row["error_msg"]
    assert row["lease_until"] is None, "the lease must be released so the budget can be spent"

    # Still inside the budget: the dispatcher may hand it out again.
    _make_dispatchable(conn, job_id)
    dispatch = db_store.claim_pending_jobs(limit=5)
    assert [job["job_id"] for job in dispatch] == [job_id]

    tool_ingest.record_ingest_failure(job_id, "finalize rejected: strict naming", claim=dispatch[0])
    final = tool_ingest.record_ingest_failure(job_id, "finalize rejected: strict naming")
    assert "budget spent" in final
    assert conn.execute("SELECT retries FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0] == db_store.MAX_INGEST_ATTEMPTS

    # Terminal: neither consumer may touch it again.
    _make_dispatchable(conn, job_id)
    assert db_store.claim_pending_jobs(limit=5) == []
    assert json.loads(tool_ingest.claim_ingest_tasks(limit=5)) == []


def test_the_failure_stays_visible_with_its_reason(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    job_id = _enqueue(conn, "job-visible")
    tool_ingest.record_ingest_failure(job_id, "model seam: child JSON invalid")

    row = conn.execute("SELECT error_msg, status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert "child JSON invalid" in row["error_msg"]


def test_an_unknown_job_is_reported_not_raised(isolated_memory):
    db_store.init_db()
    assert "not in the jobs table" in tool_ingest.record_ingest_failure("missing-job", "whatever")


def test_the_cap_has_one_owner():
    """``claim_pending_jobs`` and the recorder must not be able to drift apart."""
    dispatch = inspect.getsource(db_store.claim_pending_jobs)
    assert "MAX_INGEST_ATTEMPTS" in dispatch
    assert "retries < 3" not in dispatch, "the cap must not be a second literal"

    recorder = inspect.getsource(tool_ingest.record_ingest_failure)
    assert "MAX_INGEST_ATTEMPTS" in recorder


@pytest.mark.parametrize(
    "branch_reason",
    ["unusable task packet", "dispatch validation failed", "rejection could not be finalized", "model seam", "finalize rejected"],
)
def test_content_failure_branches_have_bounded_budget_recorders(branch_reason):
    """Content/packet failures spend source budget; backend/host faults must not."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "ingest_runner.py").read_text(
        encoding="utf-8"
    )
    assert f'f"{branch_reason}' in source, branch_reason
    # Packet/explicit dispatch validation, source drift and finalizer rejection;
    # OS/backend/host runtime faults do not spend this budget.
    assert source.count("record_ingest_failure(") == 6


def test_a_transient_failure_does_not_spend_the_attempt_budget(isolated_memory):
    """A version conflict is retryable; terminalizing it abandons a source a retry would ingest.

    Live evidence 2026-09-19: 8 of 11 terminal failures were ``Canonical version conflict`` --
    the page moved between the model reading it and ``finalize_ingest`` writing it.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    job_id = _enqueue(conn, "job-transient")

    message = tool_ingest.record_ingest_failure(
        job_id, "finalize rejected: Actual version conflict for Source_x.md"
    )

    assert "transient" in message
    row = conn.execute("SELECT retries, status, lease_until FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["retries"] == 0, "a retryable failure consumed the terminal budget"
    assert row["status"] == "failed"
    assert row["lease_until"] is None

    _make_dispatchable(conn, job_id)
    assert [job["job_id"] for job in db_store.claim_pending_jobs(limit=5)] == [job_id]


@pytest.mark.parametrize(
    "reason,transient",
    [
        ("Canonical version conflict for Source_x.md", True),
        ("ACTUAL VERSION CONFLICT", True),
        ("Ingest job x is no longer finalizable", True),
        ("Schema Violation: Missing required frontmatter field 'updated'.", False),
        ("Tag Collision: [端侧推理] is already an entity", False),
        ("Strict Naming Violation: 'Concept_Formula_x.md'", False),
    ],
)
def test_transient_classification_is_narrow(reason, transient):
    assert tool_ingest.is_transient_failure(reason) is transient


def test_a_terminal_failed_job_can_be_re_enqueued(isolated_memory):
    """Otherwise the dead job holds the idempotency key and the source is stuck forever."""
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {"filepath": "x.md", "hash": "abc123", "canonical_name": "Source_x.md"}
    first = db_store.enqueue_job("ingest", payload)
    for _ in range(db_store.MAX_INGEST_ATTEMPTS):
        tool_ingest.record_ingest_failure(first, "Schema Violation: missing field")
    assert conn.execute("SELECT status FROM jobs WHERE job_id = ?", (first,)).fetchone()[0] == "failed"

    # A plain enqueue still deduplicates (that is what stops double scheduling)...
    assert db_store.enqueue_job("ingest", payload) == first

    # ...but the scan, which knows the source is still un-ingested, supersedes it.
    second = db_store.enqueue_job("ingest", payload, replace_terminal=True)
    assert second != first
    fresh = conn.execute("SELECT status, retries FROM jobs WHERE job_id = ?", (second,)).fetchone()
    assert fresh["status"] == "queued"
    assert fresh["retries"] == 0
    # History is kept, not deleted.
    old = conn.execute("SELECT idempotency_key, error_msg FROM jobs WHERE job_id = ?", (first,)).fetchone()
    assert old["idempotency_key"] is None
    assert "superseded by a fresh dispatch" in old["error_msg"]


def test_a_completed_job_is_never_superseded(isolated_memory):
    """Re-ingesting a finished source would duplicate published pages."""
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {"filepath": "y.md", "hash": "def456", "canonical_name": "Source_y.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status = 'finalized' WHERE job_id = ?", (job_id,))

    assert db_store.enqueue_job("ingest", payload, replace_terminal=True) == job_id
