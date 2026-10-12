"""Independent review counterexamples CR-001..CR-005, synthetic data only."""
import hashlib
import json
from pathlib import Path
import stat

import pytest

from scripts import ingest_model_pi_subagents as pi_adapter, ingest_runner as runner
from vector_lake import db_store, ingest_model_contract, mutation_coordinator, tool_ingest
from tests.test_controlled_recompile import scope_files, _dispatch, _claim
from tests.test_mutation_coordinator import _source_content


def _ledger(p):
    row = db_store.get_connection().execute("SELECT * FROM processed_files WHERE filepath = ?", (p,)).fetchone()
    return dict(row) if row else None


def _assert_no_receipt(job):
    row = db_store.get_connection().execute("SELECT status, result_json FROM jobs WHERE job_id = ?", (job,)).fetchone()
    assert row["status"] != "finalized" and not row["result_json"]


@pytest.mark.parametrize("damage", ["marker", "marker-and-path", "marker-path-and-id"])
def test_CR001_actual_claim_not_packet_marker_controls_read_gate(scope_files, monkeypatch, damage):
    _dispatch(scope_files)
    task, p = _claim()
    before = _ledger(p["filepath"])
    p.pop("controlled_recompile")
    if damage != "marker":
        foreign = scope_files["memory"] / "raw" / "not-approved.md"
        foreign.write_text("SYNTHETIC_UNAPPROVED_CANARY", encoding="utf-8")
        p["filepath"] = str(foreign)
        p["hash"] = hashlib.md5(foreign.read_bytes()).hexdigest()
    if damage == "marker-path-and-id":
        p["job_id"] = db_store.enqueue_job("ingest", {"filepath": p["filepath"], "hash": p["hash"]})
    calls = []
    monkeypatch.setattr(runner, "current_source_publication_proven", lambda *_: False)
    monkeypatch.setattr(runner, "run_model", lambda *_: calls.append(1))
    counts, error = runner._process_task(task, False, "fixture", {})
    assert calls == [] and counts["errors"] == 1 and "dispatch validation failed" in error
    assert _ledger(scope_files["rows"][0]["filepath"]) == before
    _assert_no_receipt(task["job_id"])


def test_CR002_both_real_prompt_adapters_send_only_approved_snapshot(scope_files):
    _dispatch(scope_files)
    task, p = _claim()
    approved = Path(p["source_read_path"]).read_bytes().decode("utf-8")  # Preserve Windows CRLF bytes.
    Path(p["filepath"]).write_text("SYNTHETIC_UNAPPROVED_LATE_BYTES", encoding="utf-8")
    for hydrate in (ingest_model_contract.build_cli_prompt, pi_adapter._brief):
        brief = hydrate(task["task_packet"])
        assert json.dumps(approved, ensure_ascii=False) in brief and "SYNTHETIC_UNAPPROVED_LATE_BYTES" not in brief
        assert "source_read_path" in brief and "provenance only" in brief
        assert p["controlled_recompile"]["sha256"] in brief
        assert p["lease_token"] not in brief
        # Identity/provenance is retained, not replaced with the snapshot path.
        assert json.dumps(p["filepath"], ensure_ascii=False) in brief


@pytest.mark.parametrize("hydrate", [ingest_model_contract.build_cli_prompt, pi_adapter._brief])
def test_CR002_real_adapters_reject_modified_snapshot(scope_files, hydrate):
    _dispatch(scope_files)
    task, p = _claim()
    snapshot = Path(p["source_read_path"])
    snapshot.chmod(stat.S_IWRITE | stat.S_IREAD)
    snapshot.write_text("SYNTHETIC_TAMPERED_SNAPSHOT", encoding="utf-8")
    with pytest.raises(ValueError, match="approved SHA"):
        hydrate(task["task_packet"])


@pytest.mark.parametrize("drift", ["ledger", "current-receipt"])
def test_CR003_same_request_retry_revalidates_ledger_and_receipt(scope_files, drift):
    _dispatch(scope_files)
    task, p = _claim()
    assert db_store.update_job_status(p["job_id"], "failed", "synthetic failure", claim=task)
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET available_at = '2000-01-01' WHERE job_id = ?", (p["job_id"],)); conn.commit()
    if drift == "ledger":
        conn.execute("UPDATE processed_files SET processed_at = 'new owner state' WHERE filepath = ?", (p["filepath"],)); conn.commit()
    else:
        from tests.test_controlled_recompile import _publish_current
        _publish_current(p["filepath"], p["hash"])
        later = db_store.enqueue_job("ingest", {"filepath": p["filepath"], "hash": p["hash"]})
        conn.execute("UPDATE jobs SET status = 'finalized', result_json = ? WHERE job_id = ?", (json.dumps({"integration": {"disposition": "rejected", "reason": "A valid later current-byte rejection."}}), later)); conn.commit()
    before = _ledger(p["filepath"])
    with pytest.raises(ValueError, match="drifted|current-version finalization"):
        _dispatch(scope_files)
    row = conn.execute("SELECT status, retries FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["status"] == "failed" and row["retries"] == 1
    assert _ledger(p["filepath"]) == before


def test_CR003_post_handoff_ledger_change_prevents_model(scope_files, monkeypatch):
    _dispatch(scope_files)
    task, p = _claim()
    conn = db_store.get_connection()
    conn.execute("UPDATE processed_files SET processed_at = 'new owner state' WHERE filepath = ?", (p["filepath"],)); conn.commit()
    before = _ledger(p["filepath"])
    calls = []
    monkeypatch.setattr(runner, "run_model", lambda *_: calls.append(1))
    counts, _ = runner._process_task(task, False, "fixture", {})
    assert calls == [] and counts["errors"] == 1 and _ledger(p["filepath"]) == before
    _assert_no_receipt(p["job_id"])


def test_CR003_commit_transaction_rechecks_ledger(scope_files, monkeypatch):
    _dispatch(scope_files)
    _, p = _claim()
    from tests.test_mutation_coordinator import _source_content
    p["integration"] = {"disposition": "standalone", "reason": "No authorized target relations for this source.", "relations": []}
    real = db_store.validate_ingest_job_finalization
    calls = []
    def race(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            conn = db_store.get_connection()
            conn.execute("UPDATE processed_files SET processed_at = 'competing state' WHERE filepath = ?", (p["filepath"],))
        return real(*args, **kwargs)
    monkeypatch.setattr(db_store, "validate_ingest_job_finalization", race)
    result = tool_ingest.finalize_ingest([{"filename": p["canonical_name"], "content": _source_content()}], p)
    assert "ledger drifted" in result
    assert _ledger(p["filepath"])["file_hash"] == scope_files["rows"][0]["old_ledger"]["file_hash"]
    _assert_no_receipt(p["job_id"])


def _publish_same_owner_source(p):
    content = _source_content().replace("sources: [raw/test.pdf]", "sources: " + json.dumps([p["filepath"]]))
    mutation_coordinator.execute_mutation_batch([{"filename": p["canonical_name"], "content": content}])


def test_CR004_same_owner_source_version_drift_prevents_model_and_rejection(scope_files, monkeypatch):
    _dispatch(scope_files)
    task, p = _claim()
    before = _ledger(p["filepath"])
    _publish_same_owner_source(p)
    calls = []
    monkeypatch.setattr(runner, "run_model", lambda *_: calls.append(1))
    p["integration"] = {"disposition": "rejected", "reason": "Synthetic strategy rejection should not pass a drifted Source.", "relations": []}
    assert "Source version drifted" in tool_ingest.finalize_ingest([], p)
    counts, _ = runner._process_task(task, False, "fixture", {})
    assert calls == [] and counts["errors"] == 1
    assert _ledger(p["filepath"]) == before
    _assert_no_receipt(p["job_id"])


@pytest.mark.parametrize("source_ingested_only", [True, False])
def test_CR005_historical_ledger_cannot_close_controlled_terminal_failure(scope_files, source_ingested_only):
    _dispatch(scope_files)
    task, p = _claim()
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET status = 'failed', retries = 3 WHERE job_id = ?", (p["job_id"],)); conn.commit()
    result = db_store.close_terminal_failed_jobs(source_ingested_only=source_ingested_only)
    assert p["job_id"] in result["kept"] and p["job_id"] not in result["closed"]
    row = conn.execute("SELECT status, error_msg FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["status"] == "failed" and "source was ingested" not in str(row["error_msg"])
    # A rejection is not publication; a real current Source is required.
    later = db_store.enqueue_job("ingest", {"filepath": p["filepath"], "hash": p["hash"]})
    conn.execute("UPDATE jobs SET status = 'finalized', result_json = ? WHERE job_id = ?", (json.dumps({"integration": {"disposition": "rejected", "reason": "Valid current-version source rejection."}}), later)); conn.commit()
    assert p["job_id"] in db_store.close_terminal_failed_jobs()["kept"]
    from tests.test_controlled_recompile import _publish_current
    _publish_current(p["filepath"], p["hash"])
    assert p["job_id"] in db_store.close_terminal_failed_jobs()["closed"]


def test_CR004_retry_cannot_rebaseline_source_or_rewrite_failed_payload(scope_files):
    _dispatch(scope_files)
    task, p = _claim()
    assert db_store.update_job_status(p["job_id"], "failed", "synthetic failure", claim=task)
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET available_at = '2000-01-01' WHERE job_id = ?", (p["job_id"],)); conn.commit()
    before = dict(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone())
    _publish_same_owner_source(p)
    with pytest.raises(ValueError, match="Source version drifted"):
        _dispatch(scope_files)
    after = dict(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone())
    assert after == before  # Payload, attempt count, packet and first baseline all retained.
    assert _ledger(p["filepath"]) == scope_files["rows"][0]["old_ledger"]


@pytest.mark.parametrize("reason", [runner.REJECT_DUPLICATE, runner.REJECT_MISSING_SOURCE])
@pytest.mark.parametrize("source_ingested_only", [True, False])
def test_CR005_real_mechanical_receipt_cannot_close_controlled_failure(scope_files, reason, source_ingested_only):
    from vector_lake.controlled_recompile import current_version_proven
    _dispatch(scope_files)
    task, p = _claim()
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET status = 'failed', retries = 3 WHERE job_id = ?", (p["job_id"],)); conn.commit()
    payload = {k: p[k] for k in ("filepath", "hash", "canonical_name", "source_hash", "source_projection_hash", "ingest_contract_version", "integration_candidates")}
    normal = db_store.enqueue_job("ingest", payload)
    # Real normal claim/finalizer, with the exact reasons generated by the normal Runner.
    packet = scope_files["memory"] / "mechanical-packet.json"
    packet.write_text(json.dumps({"metadata": {"processed_data": {**payload, "job_id": normal}}}), encoding="utf-8")
    db_store.mark_job_awaiting_subagent(normal, str(packet))
    _, normal_data = _claim()
    normal_data["integration"] = {"disposition": "rejected", "reason": reason, "relations": []}
    result = tool_ingest.finalize_ingest([], normal_data)
    assert "Successfully" not in result and "disposition" in result
    assert not current_version_proven(p["filepath"], p["controlled_recompile"]["sha256"], p["hash"])
    kept = db_store.close_terminal_failed_jobs(source_ingested_only=source_ingested_only)
    assert p["job_id"] in kept["kept"] and p["job_id"] not in kept["closed"]
    row = conn.execute("SELECT status, error_msg FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["status"] == "failed" and "source was ingested" not in str(row["error_msg"])
