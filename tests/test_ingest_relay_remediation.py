"""Relay invariants: retry classification, fenced failure, slot-sized claims and outcomes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import ingest_runner as runner
from vector_lake import db_store, tool_ingest
from vector_lake.ingest_errors import IngestFailureKind, classify_ingest_failure


@pytest.mark.parametrize("reason,kind,transient,redispatch", [
    ("integration target_hash is stale for Concept_x.md", IngestFailureKind.VERSION_CONFLICT, True, True),
    ("integration target_projection_hash is stale for Concept_x.md", IngestFailureKind.VERSION_CONFLICT, True, True),
    ("ingest source_hash is stale", IngestFailureKind.VERSION_CONFLICT, True, True),
    ("source_projection_hash is stale", IngestFailureKind.VERSION_CONFLICT, True, True),
    ("Canonical version conflict", IngestFailureKind.VERSION_CONFLICT, True, True),
    ("Job x lease_token does not match the current lease", IngestFailureKind.LEASE_LOST, True, True),
    ("Job x lease has expired", IngestFailureKind.LEASE_LOST, True, True),
    ("source changed before model dispatch", IngestFailureKind.SOURCE_CHANGED, False, True),
    ("integration relation requires target_hash", IngestFailureKind.PAYLOAD_INVALID, False, False),
    ("Schema Violation: categories", IngestFailureKind.PAYLOAD_INVALID, False, False),
])
def test_repair_and_retry_share_one_classification(reason, kind, transient, redispatch):
    assert classify_ingest_failure(reason) == kind
    assert tool_ingest.is_transient_failure(reason) is transient
    assert runner._version_conflict(reason) is redispatch


@pytest.mark.parametrize("field", ["target_hash", "target_projection_hash", "source_hash", "source_projection_hash"])
def test_stale_versions_do_not_spend_or_abandon_the_source(isolated_memory, field):
    db_store.init_db()
    job = db_store.enqueue_job("ingest", {"filepath": "x.md", "hash": "abc"})
    for _ in range(db_store.MAX_INGEST_ATTEMPTS + 1):
        message = tool_ingest.record_ingest_failure(job, f"finalize rejected: integration {field} is stale")
        assert "transient" in message
    row = db_store.get_connection().execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone()
    assert row["retries"] == 0
    assert db_store.list_abandoned_sources() == []


def _leased_job():
    db_store.init_db()
    job = db_store.enqueue_job("ingest", {"filepath": "x.md", "hash": "abc"})
    db_store.mark_job_awaiting_subagent(job, "synthetic-packet.json")
    return db_store.claim_subagent_jobs(limit=1)[0]


def _job_snapshot(job):
    return dict(db_store.get_connection().execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone())


@pytest.mark.parametrize("reason", ["Schema Violation", "integration target_hash is stale"])
def test_late_failure_cannot_change_the_new_lease(isolated_memory, reason):
    stale = _leased_job()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), stale["job_id"],
        ))
    current = db_store.claim_subagent_jobs(limit=1)[0]
    assert current["lease_generation"] > stale["lease_generation"]
    before = _job_snapshot(current["job_id"])
    assert "ignored" in tool_ingest.record_ingest_failure(stale["job_id"], reason, claim=stale)
    assert db_store.release_job_for_retry(stale["job_id"], "shadow", claim=stale) is False
    assert db_store.update_job_status(stale["job_id"], "failed", "packet unreadable", claim=stale) is False
    assert _job_snapshot(current["job_id"]) == before
    assert db_store.list_abandoned_sources() == []


@pytest.mark.parametrize("status", ["finalized", "superseded", "completed"])
def test_late_or_unclaimed_failure_cannot_reopen_terminal_job(isolated_memory, status):
    stale = _leased_job()
    db_store.update_job_status(stale["job_id"], status)
    before = _job_snapshot(stale["job_id"])
    for claim in (stale, None):
        assert "ignored" in tool_ingest.record_ingest_failure(stale["job_id"], "Schema Violation", claim=claim)
        assert db_store.release_job_for_retry(stale["job_id"], "shadow", claim=claim) is False
    assert _job_snapshot(stale["job_id"]) == before


def test_live_failure_requires_and_accepts_exact_owner(isolated_memory):
    claim = _leased_job()
    before = _job_snapshot(claim["job_id"])
    assert "ignored" in tool_ingest.record_ingest_failure(claim["job_id"], "Schema Violation")
    assert _job_snapshot(claim["job_id"]) == before
    assert "1/3" in tool_ingest.record_ingest_failure(claim["job_id"], "Schema Violation", claim=claim)
    after = _job_snapshot(claim["job_id"])
    assert after["status"] == "failed" and after["retries"] == 1
    assert after["lease_token"] is None


def test_expiry_without_reclaim_also_refuses_failure(isolated_memory):
    claim = _leased_job()
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), claim["job_id"],
        ))
    before = _job_snapshot(claim["job_id"])
    assert "ignored" in tool_ingest.record_ingest_failure(claim["job_id"], "Schema Violation", claim=claim)
    assert _job_snapshot(claim["job_id"]) == before


def test_expiry_during_failure_does_not_mark_source_abandoned(isolated_memory, monkeypatch):
    claim = _leased_job()
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE jobs SET retries=2 WHERE job_id=?", (claim["job_id"],))
    before = _job_snapshot(claim["job_id"])
    checks = iter([True, False])
    monkeypatch.setattr(db_store, "_ingest_failure_claim_current", lambda *args: next(checks))
    assert "ignored" in tool_ingest.record_ingest_failure(claim["job_id"], "Schema Violation", claim=claim)
    assert _job_snapshot(claim["job_id"]) == before
    assert db_store.list_abandoned_sources() == []


@pytest.mark.parametrize("generation", [None, True, 1.5, "not-an-integer"])
def test_malformed_generation_cannot_release_live_claim(isolated_memory, generation):
    claim = _leased_job()
    before = _job_snapshot(claim["job_id"])
    malformed = {**claim, "lease_generation": generation}
    assert "ignored" in tool_ingest.record_ingest_failure(claim["job_id"], "Schema Violation", claim=malformed)
    assert db_store.release_job_for_retry(claim["job_id"], "shadow", claim=malformed) is False
    assert _job_snapshot(claim["job_id"]) == before


def test_serial_runner_claims_only_when_it_can_execute(monkeypatch):
    queued = list(range(5))
    started = []
    claims = []

    def claim(limit):
        claims.append((limit, len(started)))
        return json.dumps([{"job_id": queued.pop(0)}] if queued else [])

    def process(task, *args):
        started.append(task["job_id"])
        return {"finalized": 1}, ""

    monkeypatch.setattr(runner, "claim_ingest_tasks", claim)
    monkeypatch.setattr(runner, "_process_task", process)
    monkeypatch.setattr(runner, "raw_publication_index", lambda: {})
    batch = runner.process_once(5, False, "fake", {})
    assert claims == [(1, index) for index in range(5)]
    assert started == list(range(5))
    assert batch["claimed"] == batch["finalized"] == 5


@pytest.mark.parametrize("response", ["not json", "null", "{}", '["not a task"]'])
def test_invalid_claim_response_is_an_error_not_an_empty_queue(monkeypatch, response):
    monkeypatch.setattr(runner, "claim_ingest_tasks", lambda **_: response)
    monkeypatch.setattr(runner, "_process_task", lambda *_: pytest.fail("invalid task executed"))
    with pytest.raises(ValueError, match="Ingest task claim response"):
        runner.process_once(5, False, "fake", {})


def test_parallel_runner_refills_free_slot_before_slow_task_finishes(monkeypatch):
    import threading

    release = threading.Event()
    slow_started = threading.Event()
    fast_started = threading.Event()
    queued = list(range(4))
    claims = []

    def claim(limit):
        claims.append(limit)
        if len(claims) == 2:
            assert slow_started.is_set() and fast_started.is_set()
            assert not release.is_set()
            release.set()
        tasks = queued[:limit]
        del queued[:limit]
        return json.dumps([{"job_id": job} for job in tasks])

    def process(task, *args):
        if task["job_id"] == 0:
            slow_started.set()
            assert release.wait(5)
        elif task["job_id"] == 1:
            assert slow_started.wait(5)
            fast_started.set()
        return {"finalized": 1}, ""

    monkeypatch.setattr(runner, "claim_ingest_tasks", claim)
    monkeypatch.setattr(runner, "_process_task", process)
    monkeypatch.setattr(runner, "raw_publication_index", lambda: {})
    batch = runner.process_once(4, False, "fake", {}, concurrency=2)
    assert claims[:2] == [2, 1]
    assert sum(claims) == 4
    assert batch["claimed"] == batch["finalized"] == 4


@pytest.mark.parametrize("disposition,counter", [
    ("standalone", "published"), ("integrated", "published"), ("rejected", "content-rejected"),
])
def test_model_outcomes_do_not_conflate_rejection_and_publication(monkeypatch, disposition, counter):
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    monkeypatch.setattr(runner, "run_model", lambda *_: ({
        "files_written": [], "integration": {"disposition": disposition},
    }, ""))
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: "Successfully finalized ingestion")
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": {"job_id": "synthetic"}}}}
    monkeypatch.setattr(runner, "record_ingest_failure", lambda *_args, **_kwargs: None)
    counts, error = runner._process_task(task, False, "fake", {})
    if disposition == "rejected":
        assert error and counts["finalized"] == counts["published"] == counts["content-rejected"] == 0
        return
    assert not error
    assert counts["finalized"] == counts[counter] == 1
    assert sum(counts[key] for key in ("published", "content-rejected", "duplicate-closed", "missing-source-closed")) == 1


@pytest.mark.parametrize("verdict", ["duplicate", "missing-source"])
def test_nonmodel_closures_have_their_own_counts(monkeypatch, verdict):
    monkeypatch.setattr(runner, "classify", lambda *_: verdict)
    monkeypatch.setattr(runner, "run_model", lambda *_: pytest.fail("unnecessary model call"))
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: "Successfully finalized ingestion")
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": {"job_id": "synthetic"}}}}
    monkeypatch.setattr(runner, "record_ingest_failure", lambda *_args, **_kwargs: None)
    counts, error = runner._process_task(task, False, "fake", {})
    if verdict == "missing-source":
        assert error and counts["errors"] == 1 and counts["finalized"] == counts["missing-source-closed"] == 0
        return
    assert not error and counts["finalized"] == counts[f"{verdict}-closed"] == 1
    assert counts["published"] == counts["content-rejected"] == 0


def test_repaired_outcome_uses_the_accepted_disposition(monkeypatch):
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    outputs = iter(["standalone", "integrated"])
    monkeypatch.setattr(runner, "run_model", lambda *_: ({
        "files_written": [], "integration": {"disposition": next(outputs)},
    }, ""))
    results = iter(["Schema Violation", "Successfully finalized ingestion"])
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: next(results))
    monkeypatch.setattr(runner, "REPAIR_ATTEMPTS", 1)
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": {"job_id": "synthetic"}}}}
    counts, error = runner._process_task(task, False, "fake", {})
    assert not error and counts["published"] == counts["finalized"] == 1
    assert counts["content-rejected"] == 0


@pytest.mark.parametrize("totals", [{"finalized": 10}, {"finalized": 3, "published": 2, "content-rejected": 1}])
def test_health_reports_unknown_legacy_outcomes_and_separate_outbox(isolated_memory, totals):
    from vector_lake.runtime_health import assess_runtime_health
    from vector_lake.wiki_utils import get_meta_dir

    db_store.init_db()
    runtime = get_meta_dir() / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "runner_status.json").write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(), "totals": totals,
    }), encoding="utf-8")
    db_store.enqueue_mutation("Concept_x.md", "update", "synthetic")
    health = assess_runtime_health()
    detail = health["detail"]["runner"]
    assert detail["outcomes"]["published"] == totals.get("published")
    assert detail["outcomes"]["content-rejected"] == totals.get("content-rejected")
    assert detail["projection_outbox_pending"] == 1
    assert detail["projection_outbox_failed"] == 0


@pytest.mark.parametrize("backend,scope", [
    ("pi", "executable_resolution_only"), ("gemini", "headless_flags"),
    ("codex", "headless_flags_and_feature_gates"), ("custom", "not_checked"),
])
def test_backend_check_distinguishes_static_boundary_from_verification(monkeypatch, backend, scope):
    from vector_lake import ingest_backend, ingest_cli

    monkeypatch.setattr(ingest_backend, "find_cli_binary", lambda *_: "fake-cli")
    monkeypatch.setattr(ingest_cli, "check_cli", lambda *_: ingest_cli.CliCapabilities("fake-cli"))
    selection = ingest_backend.IngestBackend(backend, "test-command", "cli")
    report = ingest_backend.check_ingest_backend(selection)
    assert report["capability_scope"] == scope
    assert report["authentication_verified"] is False
    assert report["boundary_verified"] is False
    assert report["capability_checked"] is (backend != "custom")
    assert report["execution_boundary"] and report["local_retention"]
    if backend == "pi":
        assert "parent_uses_host_pi_policy" in report["execution_boundary"]
    elif backend == "custom":
        assert "unverified" in report["execution_boundary"]


def test_fresh_custom_check_never_executes_the_command(isolated_memory):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(runner.__file__).resolve()
    result = subprocess.run([
        sys.executable, str(script), "--check", "--model-cmd", "this-command-must-never-be-launched",
    ], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["effective_backend"] == "custom"
    assert report["capability_scope"] == "not_checked"
    assert report["capability_checked"] is report["boundary_verified"] is report["authentication_verified"] is False
    assert not (isolated_memory / "wiki" / ".meta" / "runtime" / "runner_status.json").exists()
