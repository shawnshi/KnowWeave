import hashlib
import json
from pathlib import Path

import pytest

from vector_lake import controlled_recompile as control, db_store, tool_ingest
from scripts import ingest_runner as runner


def _save(path, value):
    data = json.dumps(value, ensure_ascii=False).encode()
    path.write_bytes(data)
    return str(path), hashlib.sha256(data).hexdigest()


@pytest.fixture
def scope_files(isolated_memory, monkeypatch):
    (isolated_memory / "wiki" / ".meta").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(isolated_memory / "wiki" / ".meta" / "test.db"))
    db_store.init_db()
    from tests.test_mutation_coordinator import _write_purpose_contract
    _write_purpose_contract(isolated_memory)
    conn = db_store.get_connection()
    rows = []
    for i in range(78):
        p = isolated_memory / "raw" / f"public-{i:02}.md"
        p.write_text(f"# Public fixture {i}\nA synthetic public technical source.\n", encoding="utf-8")
        fp = control.fingerprint(str(p))
        state = "no_proven_current_publication" if i < 39 else "quarantine_without_current_version_proof" if i < 60 else "preserve_rejection"
        baseline = "sha256:" + fp["sha256"] if i < 39 else hashlib.md5(b"").hexdigest()
        db_store.mark_file_processed(str(p), baseline)
        old = dict(conn.execute("SELECT * FROM processed_files WHERE filepath = ?", (str(p),)).fetchone())
        rows.append({"filepath": str(p), "state": state, "current_md5": fp["md5"], "current_sha256": fp["sha256"], "size": fp["size"], "mtime_ns": fp["mtime_ns"], "old_ledger": old})
    plan_path, plan_sha = _save(isolated_memory / "plan.json", {"rows": rows})
    approval = {"version": 1, "request_id": "fixture-60", "plan_sha256": plan_sha, "read_scope": "public_only", "entries": [
        {"filepath": r["filepath"], "sha256": r["current_sha256"], "classification": "public", "classification_evidence": "Synthetic public test fixture, no real user content."} for r in rows[:60]
    ]}
    approval_path, approval_sha = _save(isolated_memory / "approval.json", approval)
    # Real native packets, but fake source briefs: no private purpose/index reads.
    seen = []
    def brief(*args, **kwargs):
        seen.append(kwargs)
        return "Compile the exact synthetic source; emit standalone or a source rejection."
    monkeypatch.setattr(tool_ingest, "_build_ingest_instructions", brief)
    return {"args": (plan_path, plan_sha, approval_path, approval_sha), "rows": rows, "approval": approval, "seen": seen, "memory": isolated_memory}


def _dispatch(fixture, count=1):
    return control.controlled_recompile(*fixture["args"], apply=True, batch_size=count)


def _claim():
    tasks = json.loads(tool_ingest.claim_ingest_tasks(limit=1))
    assert len(tasks) == 1
    return tasks[0], tasks[0]["task_packet"]["metadata"]["processed_data"]


def test_dry_run_never_registers_or_dispatches(scope_files):
    conn = db_store.get_connection()
    result = control.controlled_recompile(*scope_files["args"])
    assert result["selected"] == 60 and not result["production_writes"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert not scope_files["seen"]


@pytest.mark.parametrize("damage", ["unknown", "private", "bad-evidence", "bad-sha", "duplicate", "removed", "other-target", "bool-version"])
def test_scope_rejects_unapproved_or_mismatched_sources(scope_files, damage):
    a = scope_files["approval"]
    if damage in {"unknown", "private"}:
        a["entries"][0]["classification"] = damage
    elif damage == "bad-evidence":
        a["entries"][0]["classification_evidence"] = ""
    elif damage == "bad-sha":
        a["entries"][0]["sha256"] = "0" * 64
    elif damage == "duplicate":
        a["entries"][1] = dict(a["entries"][0])
    elif damage == "removed":
        a["entries"].pop()
    elif damage == "other-target":
        a["entries"][0]["filepath"] = scope_files["rows"][-1]["filepath"]
    else:
        a["version"] = True
    path, digest = _save(scope_files["memory"] / "bad-approval.json", a)
    with pytest.raises(ValueError):
        control.controlled_recompile(*scope_files["args"][:2], path, digest, apply=True)
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert not scope_files["seen"]


def test_artifact_hash_or_raw_drift_blocks_before_any_write(scope_files):
    path = Path(scope_files["rows"][0]["filepath"])
    path.write_text("Changed current version", encoding="utf-8")
    with pytest.raises(ValueError, match="drifted"):
        _dispatch(scope_files)
    with pytest.raises(ValueError, match="changed"):
        control.controlled_recompile(scope_files["args"][0], "0" * 64, *scope_files["args"][2:])
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_dedicated_jobs_preserve_ledger_history_and_rejections(scope_files):
    conn = db_store.get_connection()
    old = db_store.enqueue_job("ingest", {"filepath": scope_files["rows"][0]["filepath"], "hash": scope_files["rows"][0]["current_md5"]})
    db_store.update_job_status(old, "finalized")
    before_job = dict(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (old,)).fetchone())
    before_ledger = [dict(r) for r in conn.execute("SELECT * FROM processed_files ORDER BY filepath")]
    result = _dispatch(scope_files, 60)
    assert result["enqueued"] == 60 and result["state"] == "DISPATCHED_NOT_COMPLETE"
    assert dict(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (old,)).fetchone()) == before_job
    assert [dict(r) for r in conn.execute("SELECT * FROM processed_files ORDER BY filepath")] == before_ledger
    payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM jobs WHERE task_type = 'ingest_recompile'")]
    assert not {r["filepath"] for r in scope_files["rows"][60:]} & {p["filepath"] for p in payloads}
    assert all(p["integration_candidates"] == [] for p in payloads)
    assert all("No Wiki candidate" in k["index_context"] and "purpose_context" in k for k in scope_files["seen"])
    assert _dispatch(scope_files, 60)["enqueued"] == 0
    # Baseline consumer SQL excludes the new kind, including expired leases.
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest' AND status IN ('awaiting_subagent','subagent_processing')").fetchone()[0] == 0
    tasks = json.loads(tool_ingest.claim_ingest_tasks(limit=60))
    assert len(tasks) == 60
    assert all(t["lease_token"] for t in tasks)


def test_controlled_force_is_not_an_ordinary_duplicate_exception(scope_files, monkeypatch):
    _dispatch(scope_files)
    _, processed = _claim()
    monkeypatch.setattr(runner, "current_source_publication_proven", lambda *_: True)
    assert runner.classify(processed, {}) == "needs-model"
    plain = {"filepath": processed["filepath"]}
    assert runner.classify(plain, {}) == "duplicate"
    with pytest.raises(ValueError):
        runner.classify({**processed, "job_id": "not-a-job"}, {})
    altered = {**processed, "controlled_recompile": {**processed["controlled_recompile"], "sha256": "0" * 64}}
    with pytest.raises(ValueError, match="marker"):
        runner.classify(altered, {})


@pytest.mark.parametrize("damage", ["bytes", "revoked", "lease", "context", "packet-context", "marker-removed"])
def test_lease_request_bytes_and_context_are_fenced(scope_files, damage):
    _dispatch(scope_files)
    _, p = _claim()
    conn = db_store.get_connection()
    if damage == "bytes":
        Path(p["filepath"]).write_text("New bytes", encoding="utf-8")
    elif damage == "revoked":
        db_store.update_job_status(p["controlled_recompile"]["request_job_id"], "cancelled")
    elif damage == "lease":
        p["lease_token"] = "wrong"
    elif damage == "context":
        row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
        payload = json.loads(row[0]); payload["integration_candidates"] = [{"target": "Unrelated"}]
        conn.execute("UPDATE jobs SET payload = ? WHERE job_id = ?", (json.dumps(payload), p["job_id"]))
        conn.commit()
    elif damage == "packet-context":
        p["integration_candidates"] = [{"target": "Unrelated"}]
    else:
        p.pop("controlled_recompile")
    with pytest.raises(ValueError):
        db_store.validate_ingest_job_finalization(p["job_id"], p)


def test_model_rejection_does_not_finalize_or_update_ledger(scope_files, monkeypatch):
    result = _dispatch(scope_files)
    task, p = _claim()
    monkeypatch.setattr(runner, "current_source_publication_proven", lambda *_: True)
    calls = []
    def model(*_):
        calls.append(1)
        return {"files_written": [], "integration": {"disposition": "rejected", "reason": "This synthetic source offers no supported insight relevant to the strategy.", "relations": []}}, ""
    monkeypatch.setattr(runner, "run_model", model)
    counts, error = runner._process_task(task, False, "fixture-only", {})
    assert error
    assert len(calls) == 1
    assert counts["content-rejected"] == 0 and counts["finalized"] == 0
    row = db_store.get_connection().execute("SELECT * FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["status"] == "failed" and row["retries"] == 0 and row["result_json"] is None
    assert db_store.get_processed_files()[p["filepath"]] == scope_files["rows"][0]["old_ledger"]["file_hash"]
    assert result["request_job_id"] != p["job_id"]


def test_stale_during_finalize_rolls_back_ledger_and_receipt(scope_files, monkeypatch):
    _dispatch(scope_files)
    _, p = _claim()
    old = scope_files["rows"][0]["old_ledger"]["file_hash"]
    p["integration"] = {"disposition": "rejected", "reason": "Synthetic source has no strategy contribution in this test case.", "relations": []}
    real = db_store.validate_ingest_job_finalization
    calls = []
    def race(*args):
        calls.append(1)
        if len(calls) == 2:
            Path(p["filepath"]).write_text("Changed after first validation", encoding="utf-8")
        return real(*args)
    monkeypatch.setattr(db_store, "validate_ingest_job_finalization", race)
    answer = tool_ingest.finalize_ingest([], p)
    assert "Error" in answer or "error" in answer.lower()
    conn = db_store.get_connection()
    assert conn.execute("SELECT file_hash FROM processed_files WHERE filepath = ?", (p["filepath"],)).fetchone()[0] == old
    row = conn.execute("SELECT status, result_json FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["status"] != "finalized" and not row["result_json"]


def test_failed_controlled_job_stays_out_of_ordinary_dispatch_and_spends_budget(scope_files):
    _dispatch(scope_files)
    task, p = _claim()
    assert db_store.update_job_status(p["job_id"], "failed", "fixture failure", claim=task)
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET available_at = '2000-01-01' WHERE job_id = ?", (p["job_id"],))
    conn.commit()
    assert db_store.claim_pending_jobs() == []
    assert _dispatch(scope_files)["enqueued"] == 1
    row = conn.execute("SELECT retries FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert row["retries"] == 1
    conn.execute("UPDATE jobs SET status = 'failed', retries = 3, available_at = '2000-01-01' WHERE job_id = ?", (p["job_id"],))
    conn.commit()
    next_result = _dispatch(scope_files)
    assert p["job_id"] not in next_result["job_ids"]


def test_active_normal_source_and_ledger_drift_block_and_rollback_request(scope_files):
    conn = db_store.get_connection()
    p = scope_files["rows"][0]["filepath"]
    job = db_store.enqueue_job("ingest", {"filepath": p, "hash": "historical"})
    db_store.mark_job_awaiting_subagent(job, "fixture-only")
    with pytest.raises(ValueError, match="active"):
        _dispatch(scope_files)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest_recompile_request'").fetchone()[0] == 0
    db_store.update_job_status(job, "cancelled")
    conn.execute("UPDATE processed_files SET processed_at = 'changed' WHERE filepath = ?", (p,))
    conn.commit()
    with pytest.raises(ValueError, match="ledger drifted"):
        _dispatch(scope_files)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest_recompile_request'").fetchone()[0] == 0


def _publish_current(filepath, file_hash):
    from tests.test_mutation_coordinator import _source_content
    from vector_lake.mutation_coordinator import execute_mutation_plan
    from vector_lake.wiki_utils import canonical_source_name

    content = tool_ingest._stamp_source_hash(_source_content(), file_hash, filepath)
    ok, message = execute_mutation_plan(canonical_source_name(filepath), content=content)
    assert ok, message


def test_current_publication_receipt_stops_new_handoff(scope_files):
    r = scope_files["rows"][0]
    _publish_current(r["filepath"], r["current_md5"])
    conn = db_store.get_connection()
    job = db_store.enqueue_job("ingest", {"filepath": r["filepath"], "hash": r["current_md5"]})
    conn.execute("UPDATE jobs SET status = 'finalized', result_json = ? WHERE job_id = ?", (json.dumps({"integration": {"disposition": "rejected", "reason": "A real current-version rejection."}}), job))
    conn.commit()
    with pytest.raises(ValueError, match="current-version finalization"):
        _dispatch(scope_files)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest_recompile_request'").fetchone()[0] == 0


def test_failed_version_cannot_reset_budget_by_changing_request_id(scope_files):
    _dispatch(scope_files)
    task, p = _claim()
    conn = db_store.get_connection()
    conn.execute("UPDATE jobs SET status = 'failed', retries = 3, lease_token = NULL, lease_owner = NULL, lease_until = NULL WHERE job_id = ?", (p["job_id"],))
    conn.commit()
    approval = scope_files["approval"]
    approval["request_id"] = "different-request-same-bytes"
    path, digest = _save(scope_files["memory"] / "other-request.json", approval)
    result = control.controlled_recompile(*scope_files["args"][:2], path, digest, apply=True)
    row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (result["job_ids"][0],)).fetchone()
    assert json.loads(row[0])["filepath"] != p["filepath"]


def test_stale_task_is_contained_before_model_call(scope_files, monkeypatch):
    _dispatch(scope_files)
    task, p = _claim()
    Path(p["filepath"]).write_text("A different version", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "run_model", lambda *_: calls.append(1))
    counts, error = runner._process_task(task, False, "fixture-only", {})
    assert counts["errors"] == 1 and "dispatch validation failed" in error and calls == []
    assert db_store.get_processed_files()[p["filepath"]] == scope_files["rows"][0]["old_ledger"]["file_hash"]


def test_real_host_standalone_publish_has_current_receipt(scope_files, monkeypatch):
    from tests.test_mutation_coordinator import _source_content
    _dispatch(scope_files)
    task, p = _claim()
    content = _source_content().replace("sources: [raw/test.pdf]", "sources: " + json.dumps([p["filepath"]]))
    response = {"files_written": [{"filename": p["canonical_name"], "content": content}], "integration": {"disposition": "standalone", "reason": "No authorized existing target candidates; this test source stands alone.", "relations": []}}
    monkeypatch.setattr(runner, "current_source_publication_proven", lambda *_: True)
    monkeypatch.setattr(runner, "run_model", lambda *_: (response, ""))
    counts, error = runner._process_task(task, False, "fixture-only", {})
    assert not error, error
    assert counts["published"] == 1 and counts["finalized"] == 1 and counts["duplicate-closed"] == 0
    row = db_store.get_connection().execute("SELECT result_json FROM jobs WHERE job_id = ?", (p["job_id"],)).fetchone()
    assert json.loads(row[0])["controlled_recompile"] == p["controlled_recompile"]
    output = scope_files["memory"] / "wiki" / p["canonical_name"]
    assert output.exists() and p["hash"] in output.read_text(encoding="utf-8")


def test_canonical_filename_collision_is_rejected_before_db_handoff(scope_files):
    rows = scope_files["rows"]
    path = scope_files["memory"] / "raw" / "other-folder" / Path(rows[0]["filepath"]).name
    path.parent.mkdir()
    path.write_text("Another synthetic public source", encoding="utf-8")
    fp = control.fingerprint(str(path))
    rows[1].update(filepath=str(path), current_md5=fp["md5"], current_sha256=fp["sha256"], size=fp["size"], mtime_ns=fp["mtime_ns"])
    plan, plan_sha = _save(scope_files["memory"] / "collision-plan.json", {"rows": rows})
    approval = scope_files["approval"]
    approval["plan_sha256"] = plan_sha
    approval["entries"][1].update(filepath=str(path), sha256=fp["sha256"])
    a, a_sha = _save(scope_files["memory"] / "collision-approval.json", approval)
    with pytest.raises(ValueError, match="collide"):
        control.controlled_recompile(plan, plan_sha, a, a_sha, apply=True)
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_existing_source_foreign_ownership_blocks_dispatch(scope_files):
    from vector_lake.wiki_utils import canonical_source_name
    conn = db_store.get_connection()
    name = canonical_source_name(scope_files["rows"][0]["filepath"])
    conn.execute("INSERT INTO entities (entity_id, canonical_name, data_json, type, status) VALUES (?, ?, ?, ?, ?)",
                 ("foreign-source", name, json.dumps({"page_key": name[:-3], "sources": ["raw/foreign.md"]}), "source", "Active"))
    conn.commit()
    with pytest.raises(ValueError, match="belongs to another"):
        _dispatch(scope_files)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type = 'ingest_recompile_request'").fetchone()[0] == 0
