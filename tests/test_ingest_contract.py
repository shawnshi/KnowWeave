import json
import re
import sqlite3
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, mcp_server, mutation_coordinator, tool_ingest
from vector_lake import ingest_worker
from vector_lake.ingest_worker import _ingest_finalization_proven, process_jobs
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_ingest import (
    _read_relevant_index_context,
    claim_ingest_tasks,
    list_ingest_tasks,
)
from vector_lake.wiki_utils import projection_hash
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def _manifest_record(target, target_hash):
    """The candidate record the packet would carry for ``target``."""
    if not target_hash:
        key = target[:-3] if target.endswith(".md") else target
        target_hash = governance_store.canonical_page_versions({key}).get(key, "")
    try:
        content = tool_ingest._read_canonical_target_content(target, target_hash)
    except ValueError:
        # A deliberately stale token (the staleness tests): the markdown projection is then the
        # only readable form, and the version check reports the staleness before any hash compare.
        content = (tool_ingest.get_wiki_dir() / target).read_text(encoding="utf-8")
    return {
        "target": target,
        "target_hash": target_hash,
        "target_projection_hash": projection_hash(content),
        "type": "concept",
        "title": target,
        "summary": "",
        "match_score": 999,
        "match_reasons": ["test-fixture"],
    }


def _finalize(files, processed_data):
    """``finalize_ingest`` with the packet's v2 dispatch manifest filled in from its relations.

    The manifest is derived from what each test already names, so the tests keep exercising the
    finalize path without every one of them restating the packet contract.  What the manifest
    *bounds* is tested on its own in ``test_ingest_manifest.py``; because it is derived here,
    these tests say nothing about the allowlist.
    """
    integration = processed_data.get("integration")
    relations = integration.get("relations") if isinstance(integration, dict) else None
    if relations and "integration_candidates" not in processed_data:
        manifest = []
        for relation in relations:
            record = _manifest_record(
                str(relation.get("target") or ""), str(relation.get("target_hash") or "")
            )
            relation.setdefault("target_projection_hash", record["target_projection_hash"])
            manifest.append(record)
        processed_data = {**processed_data, "integration_candidates": manifest}
        # These legacy fixtures synthesize the authorized packet from their declared targets;
        # bind that same fixture allowlist durably. Real tampering tests never use this helper.
        conn = db_store.get_connection()
        row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (processed_data["job_id"],)).fetchone()
        queued = json.loads(row["payload"])
        queued["integration_candidates"] = manifest
        with db_store.transaction():
            conn.execute("UPDATE jobs SET payload = ? WHERE job_id = ?",
                         (json.dumps(queued), processed_data["job_id"]))
    return mcp_server.tools.finalize_ingest(files, processed_data)


def _concept_content(title="Target Concept"):
    return f"""---
id: concept_target
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/original.md]
strategic_scope: core
evidence_tier: primary
---
## 1. 编译事实

Target compiled truth.

## 2. 证据时间线
"""


def _synthesis_content(title="Target Synthesis"):
    return f"""---
id: synthesis_target
title: {title}
type: synthesis
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/original.md]
strategic_scope: core
evidence_tier: primary
---
## 核心合成论点 (Core Synthesized Claims)

Target synthesis.

## 支撑拓扑 (Supporting Topology)
"""

def test_finalize_ingest_accepts_payload_file_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path))
    files = [{"filename": "Source_Test.md", "content": "body"}]
    processed = {"filepath": "raw/test.pdf", "hash": "abc123"}
    files_path = tmp_path / "files.json"
    raw_path = tmp_path / "raw.json"
    files_path.write_text(json.dumps(files), encoding="utf-8")
    raw_path.write_text(json.dumps(processed), encoding="utf-8")
    captured = {}

    def fake_finalize(actual_files, actual_processed):
        captured["files"] = actual_files
        captured["processed"] = actual_processed
        return "ok"

    monkeypatch.setattr(mcp_server.tools, "finalize_ingest", fake_finalize)
    result = mcp_server.finalize_ingest(
        files_written_payload_file=str(files_path),
        raw_files_payload_file=str(raw_path),
    )

    assert result == "ok"
    assert captured == {"files": files, "processed": processed}


def test_ingest_finalization_requires_matching_processed_file(isolated_memory):
    db_store.init_db()
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is False
    db_store.mark_file_processed("raw/test.pdf", "different")
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is False
    db_store.mark_file_processed("raw/test.pdf", "abc123")
    assert _ingest_finalization_proven("raw/test.pdf", "abc123") is False


def test_job_claim_uses_a_lease(isolated_memory):
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", {"filepath": "raw/test.pdf"})

    first = db_store.claim_pending_jobs(limit=1, lease_seconds=60)
    second = db_store.claim_pending_jobs(limit=1, lease_seconds=60)

    assert [job["job_id"] for job in first] == [job_id]
    assert second == []


def test_ingest_job_enqueue_is_idempotent_by_file_hash(isolated_memory):
    db_store.init_db()
    payload = {
        "filepath": "raw/test.pdf",
        "hash": "abc123",
        "canonical_name": "Source_Test.md",
        "instructions": "large prompt",
    }

    first = db_store.enqueue_job("ingest", payload)
    second = db_store.enqueue_job("ingest", {**payload, "instructions": "different prompt"})

    assert second == first
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_ingest_worker_creates_subagent_task_packet(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw = isolated_memory / "raw" / "native.md"
    raw.write_text("Synthetic native source.", encoding="utf-8")
    from vector_lake.tool_ingest import calculate_hash
    payload = {
        "filepath": str(raw),
        "hash": calculate_hash(str(raw)),
        "canonical_name": "Source_Native.md",
        "source_hash": "native-source-version",
        "instructions": "compile this source",
    }
    job_id = db_store.enqueue_job("ingest", payload)

    process_jobs()

    row = db_store.get_connection().execute(
        "SELECT status, error_msg FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "awaiting_subagent"
    task_path = row["error_msg"].split("Subagent task packet: ", 1)[1]
    with open(task_path, "r", encoding="utf-8") as handle:
        task = json.load(handle)
    assert task["task_type"] == "ingest"
    assert task["runtime"] == "current-environment-subagent"
    assert task["metadata"]["job_id"] == job_id
    assert task["metadata"]["processed_data"]["filepath"] == str(raw)
    assert task["metadata"]["processed_data"]["source_hash"] == "", "dispatch must not reuse the obsolete token"
    assert task["metadata"]["processed_data"]["job_id"] == job_id
    assert "CURRENT-ENVIRONMENT SUBAGENT HANDOFF" in task["prompt"]
    listed = list_ingest_tasks(limit=5, include_queued=False)
    assert job_id in listed
    assert "task_packet=" in listed
    claimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    claimed_processed = claimed["task_packet"]["metadata"]["processed_data"]
    assert claimed_processed["lease_owner"] == claimed["lease_owner"]
    assert claimed_processed["lease_token"] == claimed["lease_token"]
    assert claimed_processed["lease_generation"] == claimed["lease_generation"]
    import os
    os.remove(task_path)


def test_ingest_worker_rebuilds_legacy_awaiting_packet_before_dispatch(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw_path = isolated_memory / "raw" / "legacy-awaiting.md"
    raw_path.write_text("Legacy source content.", encoding="utf-8")
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {}}),
        encoding="utf-8",
    )
    payload = {
        "filepath": str(raw_path),
        "hash": __import__("hashlib").md5(raw_path.read_bytes()).hexdigest(),
        "canonical_name": "Source_Legacy-Awaiting.md",
        "instructions": "legacy prompt without integration disposition",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    from vector_lake.native_llm import create_subagent_task

    old_task = create_subagent_task("ingest", "legacy prompt", "legacy output", {"job_id": job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(old_task))

    process_jobs()

    row = db_store.get_connection().execute(
        "SELECT status, payload, task_packet_path FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    rebuilt = json.loads(row["payload"])
    assert row["status"] == "awaiting_subagent"
    assert rebuilt["source_hash"] == ""
    assert "semantic disposition" in rebuilt["instructions"]
    assert "canonical SQLite version tokens" in rebuilt["instructions"]
    assert row["task_packet_path"] != str(old_task)
    assert old_task.exists() is False
    Path(row["task_packet_path"]).unlink(missing_ok=True)


def test_subagent_task_claim_uses_lease_and_can_reclaim_expired_work(isolated_memory):
    db_store.init_db()
    payload = {
        "filepath": "raw/lease.md",
        "hash": "lease-hash",
        "canonical_name": "Source_Lease.md",
        "instructions": "compile this source",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")

    first = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
    second = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))

    assert [item["job_id"] for item in first] == [job_id]
    assert second == []
    assert first[0]["task_packet"] is None
    assert first[0]["lease_owner"]

    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )
    reclaimed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
    assert [item["job_id"] for item in reclaimed] == [job_id]
    assert reclaimed[0]["lease_generation"] == first[0]["lease_generation"] + 1
    assert reclaimed[0]["lease_token"] != first[0]["lease_token"]


def test_finalize_ingest_rejects_mismatched_job_payload(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/expected.md",
            "hash": "expected-hash",
            "canonical_name": "Source_Expected.md",
            "instructions": "compile this source",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [],
        {
            "filepath": "raw/other.md",
            "hash": "expected-hash",
            "canonical_name": "Source_Expected.md",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    row = db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert result.startswith("Error finalizing ingestion")
    assert "filepath does not match" in result
    assert row["status"] == "subagent_processing"
    assert _ingest_finalization_proven("raw/other.md", "expected-hash") is False


def test_finalize_ingest_rejects_source_hash_not_bound_to_job_payload(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/source-version.md",
            "hash": "source-version-hash",
            "canonical_name": "Source_Version.md",
            "source_hash": "queued-version",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [],
        {
            "filepath": "raw/source-version.md",
            "hash": "source-version-hash",
            "canonical_name": "Source_Version.md",
            "source_hash": "substituted-version",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "source_hash does not match" in result


def test_finalize_ingest_marks_subagent_job_finalized(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw = isolated_memory / "raw" / "finalize.md"
    raw.write_text("Synthetic finalize source.", encoding="utf-8")
    file_hash = tool_ingest.calculate_hash(str(raw))
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-finalize")
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/finalize.md",
            "hash": file_hash,
            "canonical_name": "Source_Finalize.md",
            "instructions": "compile this source",
        },
    )
    from vector_lake.native_llm import create_subagent_task

    task_path = create_subagent_task("ingest", "test", "JSON array", {"job_id": job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(task_path))
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Finalize.md", "content": _source_content()}],
        {
            "filepath": "raw/finalize.md",
            "hash": file_hash,
            "canonical_name": "Source_Finalize.md",
            "integration": {
                "disposition": "standalone",
                "reason": "No supported relation to existing Wiki nodes was found.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    row = db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert result.startswith("Successfully finalized ingestion")
    assert row["status"] == "finalized"
    assert _ingest_finalization_proven("raw/finalize.md", file_hash) is True
    assert task_path.exists() is False


def test_finalize_ingest_requires_claimed_job(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])

    result = _finalize(
        [],
        {"filepath": "raw/unbound.md", "hash": "unbound-hash"},
    )

    assert result.startswith("Error finalizing ingestion")
    assert "claimed job_id" in result
    assert _ingest_finalization_proven("raw/unbound.md", "unbound-hash") is False


def test_stale_subagent_lease_cannot_finalize_after_reclaim(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/fenced.md",
            "hash": "fenced-hash",
            "canonical_name": "Source_Fenced.md",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    stale = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )
    current = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    stale_result = _finalize(
        [],
        {
            "filepath": "raw/fenced.md",
            "hash": "fenced-hash",
            "canonical_name": "Source_Fenced.md",
            "job_id": job_id,
            "lease_owner": stale["lease_owner"],
            "lease_token": stale["lease_token"],
            "lease_generation": stale["lease_generation"],
        },
    )

    assert stale_result.startswith("Error finalizing ingestion")
    assert "current lease" in stale_result
    row = db_store.get_connection().execute(
        "SELECT status, lease_token, lease_generation FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["lease_token"] == current["lease_token"]
    assert row["lease_generation"] == current["lease_generation"]
    assert _ingest_finalization_proven("raw/fenced.md", "fenced-hash") is False


def test_final_cas_rolls_back_processed_marker_if_lease_changes_after_validation(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/race.md",
            "hash": "race-hash",
            "canonical_name": "Source_Race.md",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    stale = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    def reclaim_during_payload_validation(files, contract):
        with db_store.transaction():
            db_store.get_connection().execute(
                "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                (job_id,),
            )
        json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))
        return []

    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", reclaim_during_payload_validation)
    result = _finalize(
        [{"filename": "Source_Race.md", "content": _source_content()}],
        {
            "filepath": "raw/race.md",
            "hash": "race-hash",
            "canonical_name": "Source_Race.md",
            "integration": {
                "disposition": "standalone",
                "reason": "No supported relation to existing Wiki nodes was found.",
            },
            "job_id": job_id,
            "lease_owner": stale["lease_owner"],
            "lease_token": stale["lease_token"],
            "lease_generation": stale["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "lease_token does not match the current lease" in result
    assert _ingest_finalization_proven("raw/race.md", "race-hash") is False
    row = db_store.get_connection().execute(
        "SELECT status, lease_generation FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["lease_generation"] == stale["lease_generation"] + 1


def test_relevant_index_context_searches_beyond_first_hundred_nodes(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("国家级主数据治理要求进入持续运营阶段。", encoding="utf-8")
    target_content = _concept_content("主数据治理")
    execute_mutation_plan("Concept_主数据治理.md", content=target_content)
    nodes = {
        f"Concept_Noise-{index}": {
            "id": f"Concept_Noise-{index}",
            "title": f"Noise {index}",
            "type": "concept",
            "summary": "unrelated",
        }
        for index in range(150)
    }
    nodes["Concept_主数据治理"] = {
        "id": "Concept_主数据治理",
        "title": "主数据治理",
        "type": "concept",
        "summary": "跨机构数据质量治理",
    }
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": nodes}, ensure_ascii=False),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path), max_nodes=10)

    assert "Concept_主数据治理.md" in context
    assert governance_store.canonical_page_versions({"Concept_主数据治理"})[
        "Concept_主数据治理"
    ] in context


def test_relevant_index_context_includes_canonical_synthesis_candidates(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "synthesis-candidate.md"
    raw_path.write_text("Agentic clinical systems need a target synthesis.", encoding="utf-8")
    execute_mutation_plan(
        "Synthesis_Agentic-Clinical-Systems.md",
        content=_synthesis_content("Agentic Clinical Systems"),
    )
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {
            "Synthesis_Agentic-Clinical-Systems": {
                "type": "synthesis",
                "title": "Agentic Clinical Systems",
                "summary": "Clinical orchestration synthesis",
            }
        }}),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path))

    assert "Synthesis_Agentic-Clinical-Systems.md" in context
    assert governance_store.canonical_page_versions({"Synthesis_Agentic-Clinical-Systems"})[
        "Synthesis_Agentic-Clinical-Systems"
    ] in context


def test_relevant_index_context_rejects_an_unreadable_index(isolated_memory):
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("source", encoding="utf-8")
    (isolated_memory / "wiki" / "index.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="source-relevant ingest context"):
        _read_relevant_index_context(str(raw_path))


def test_relevant_index_context_excludes_sources_and_ascii_substring_false_positives(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "candidate.md"
    raw_path.write_text("The team will investigate the clinical workflow and platform system. 医疗平台体系。", encoding="utf-8")
    execute_mutation_plan(
        "Concept_GATE.md",
        content=_concept_content("GATE").replace("id: concept_target", "id: concept_gate"),
    )
    execute_mutation_plan(
        "Concept_Noise.md",
        content=_concept_content("Noise").replace("id: concept_target", "id: concept_noise"),
    )
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {
            "Source_Candidate": {"type": "source", "title": "Candidate"},
            "Concept_GATE": {"type": "concept", "title": "GATE", "summary": "execution test"},
            "Concept_Noise": {"type": "concept", "title": "Noise", "aliases": ["体系", "平台"]},
        }}),
        encoding="utf-8",
    )

    context = _read_relevant_index_context(str(raw_path))

    assert "Source_Candidate.md" not in context
    assert "Concept_GATE.md" not in context
    assert "Concept_Noise.md" not in context


def test_finalize_ingest_rejects_missing_semantic_disposition(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/no-disposition.md", "hash": "no-disposition", "canonical_name": "Source_No-Disposition.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [],
        {
            "filepath": "raw/no-disposition.md",
            "hash": "no-disposition",
            "canonical_name": "Source_No-Disposition.md",
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "integration disposition" in result
    assert db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["status"] == "subagent_processing"
    assert _ingest_finalization_proven("raw/no-disposition.md", "no-disposition") is False


def test_finalize_ingest_accepts_audited_standalone_source(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/standalone.md", "hash": "standalone-hash", "canonical_name": "Source_Standalone.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Standalone.md", "content": _source_content()}],
        {
            "filepath": "raw/standalone.md",
            "hash": "standalone-hash",
            "canonical_name": "Source_Standalone.md",
            "integration": {
                "disposition": "standalone",
                "reason": "No existing node has a direct, source-supported semantic relation.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    assert (isolated_memory / "wiki" / "Source_Standalone.md").exists()
    stored_result = db_store.get_connection().execute(
        "SELECT result_json FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["result_json"]
    assert json.loads(stored_result)["integration"]["disposition"] == "standalone"


def test_standalone_ingest_cannot_overwrite_existing_source_without_queued_version(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Standalone.md", content=_source_content())
    payload = {
        "filepath": "raw/standalone-rewrite.md",
        "hash": "standalone-rewrite",
        "canonical_name": "Source_Standalone.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{
            "filename": "Source_Standalone.md",
            "content": _source_content().replace("Primary source content.", "Unauthorized rewrite."),
        }],
        {
            **payload,
            "integration": {
                "disposition": "standalone",
                "reason": "No existing node has a direct, source-supported semantic relation.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "Canonical version conflict" in result
    assert _ingest_finalization_proven("raw/standalone-rewrite.md", "standalone-rewrite") is False
    assert db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["status"] == "subagent_processing"
    assert "Unauthorized rewrite." not in (
        isolated_memory / "wiki" / "Source_Standalone.md"
    ).read_text(encoding="utf-8")


def test_finalize_ingest_integrates_source_and_target_atomically(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target_content = _concept_content()
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=target_content)
    target_content = target_path.read_text(encoding="utf-8")
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    outbox_before = db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0]
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/integrated.md", "hash": "integrated-hash", "canonical_name": "Source_Integrated.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Integrated.md", "content": _source_content()}],
        {
            "filepath": "raw/integrated.md",
            "hash": "integrated-hash",
            "canonical_name": "Source_Integrated.md",
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source directly supports the target mechanism.",
                    "confidence": 0.93,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    source = (isolated_memory / "wiki" / "Source_Integrated.md").read_text(encoding="utf-8")
    target = target_path.read_text(encoding="utf-8")
    assert "[validates:: [[Concept_Target]]]" in source
    assert "(Source: [[Source_Integrated]])" in target
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == outbox_before + 2


def test_integration_uses_canonical_outbox_snapshot_when_markdown_projection_is_stale(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    stale_projection = target_path.read_text(encoding="utf-8")
    canonical_v2 = _concept_content().replace(
        "Target compiled truth.",
        "Canonical V2 content must survive integration.",
    )
    real_materialize = mutation_coordinator.materialize_markdown_projection
    fail_projection = True

    def fail_once_for_target(filename, mutation_type, payload_text=None, validation_mode="full", **kwargs):
        if fail_projection and filename == "Concept_Target.md":
            raise OSError("injected projection failure")
        return real_materialize(filename, mutation_type, payload_text, validation_mode, **kwargs)

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        fail_once_for_target,
    )
    execute_mutation_plan("Concept_Target.md", content=canonical_v2)
    assert target_path.read_text(encoding="utf-8") == stale_projection
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]
    fail_projection = False

    payload = {
        "filepath": "raw/stale-projection.md",
        "hash": "stale-projection",
        "canonical_name": "Source_Stale-Projection.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    result = _finalize(
        [{"filename": "Source_Stale-Projection.md", "content": _source_content()}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source supports the canonical V2 target content.",
                    "confidence": 0.94,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    updated_target = target_path.read_text(encoding="utf-8")
    assert "Canonical V2 content must survive integration." in updated_target
    assert "The source supports the canonical V2 target content." in updated_target


def test_reingest_replaces_relation_evidence_without_duplicate_anchors(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Concept_Target.md", content=_concept_content())

    def finalize_version(filepath, file_hash, source_hash, target_hash, evidence, predicate, event_tag):
        payload = {
            "filepath": filepath,
            "hash": file_hash,
            "canonical_name": "Source_Integrated.md",
            "source_hash": source_hash,
        }
        job_id = db_store.enqueue_job("ingest", payload)
        db_store.mark_job_awaiting_subagent(job_id, "")
        claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
        return _finalize(
            [{"filename": "Source_Integrated.md", "content": _source_content()}],
            {
                **payload,
                "integration": {
                    "disposition": "integrated",
                    "relations": [{
                        "target": "Concept_Target.md",
                        "target_hash": target_hash,
                        "predicate": predicate,
                        "evidence": evidence,
                        "confidence": 0.91,
                        "event_date": "2026-07-15",
                        "event_tag": event_tag,
                    }],
                },
                "job_id": job_id,
                "lease_owner": claim["lease_owner"],
                "lease_token": claim["lease_token"],
                "lease_generation": claim["lease_generation"],
            },
        )

    first = finalize_version(
        "raw/integrated-v1.md",
        "integrated-v1",
        "",
        governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"],
        "The original source evidence supports the target mechanism.",
        "validates",
        "Validation",
    )
    assert first.startswith("Successfully finalized ingestion")

    source_path = isolated_memory / "wiki" / "Source_Integrated.md"
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    legacy_source = re.sub(
        r"\s*<!-- vector-lake-relation:[0-9a-f]+ -->",
        "",
        source_path.read_text(encoding="utf-8"),
    )
    source_legacy_line = next(
        line for line in legacy_source.splitlines() if "[[Concept_Target]]" in line
    )
    execute_mutation_plan(
        "Source_Integrated.md",
        content=f"{legacy_source.rstrip()}\n{source_legacy_line.replace('original source evidence', 'second stale duplicate')}\n",
    )
    legacy_target = re.sub(
        r"\s*<!-- vector-lake-relation:[0-9a-f]+ -->",
        "",
        target_path.read_text(encoding="utf-8"),
    )
    target_legacy_line = next(
        line for line in legacy_target.splitlines() if "(Source: [[Source_Integrated]])" in line
    )
    execute_mutation_plan(
        "Concept_Target.md",
        content=f"{legacy_target.rstrip()}\n{target_legacy_line.replace('original source evidence', 'second stale duplicate')}\n",
    )

    second = finalize_version(
        "raw/integrated-v2.md",
        "integrated-v2",
        governance_store.canonical_page_versions({"Source_Integrated"})["Source_Integrated"],
        governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"],
        "The revised source evidence changes the supported mechanism.",
        "related_to",
        "Observation",
    )
    assert second.startswith("Successfully finalized ingestion")

    source = source_path.read_text(encoding="utf-8")
    target = target_path.read_text(encoding="utf-8")
    assert "The revised source evidence" in source
    assert "The revised source evidence" in target
    assert "The original source evidence" not in source
    assert "The original source evidence" not in target
    assert "second stale duplicate" not in source
    assert "second stale duplicate" not in target
    assert source.count("vector-lake-relation:") == 1
    assert target.count("vector-lake-relation:") == 1
    assert target.count("(Source: [[Source_Integrated]])") == 1


def test_finalize_ingest_integrates_into_synthesis_supporting_topology(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Synthesis_Target.md", content=_synthesis_content())
    target_version = governance_store.canonical_page_versions({"Synthesis_Target"})["Synthesis_Target"]
    payload = {
        "filepath": "raw/synthesis-support.md",
        "hash": "synthesis-support",
        "canonical_name": "Source_Synthesis-Support.md",
        "source_hash": "",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Synthesis-Support.md", "content": _source_content()}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Synthesis_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source supports a critical synthesized claim directly.",
                    "confidence": 0.88,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion")
    target = (isolated_memory / "wiki" / "Synthesis_Target.md").read_text(encoding="utf-8")
    assert "## 支撑拓扑 (Supporting Topology)" in target
    assert "[depends-on:: [[Source_Synthesis-Support]]]" in target
    assert target.count("vector-lake-relation:") == 1


def test_finalize_ingest_rejects_stale_target_hash(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    target_path.write_text(_concept_content(), encoding="utf-8")
    db_store.init_db()
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": "raw/stale-target.md", "hash": "stale-target", "canonical_name": "Source_Stale-Target.md"},
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Stale-Target.md", "content": _source_content()}],
        {
            "filepath": "raw/stale-target.md",
            "hash": "stale-target",
            "canonical_name": "Source_Stale-Target.md",
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": "stale",
                    "predicate": "validates",
                    "evidence": "The source directly supports the target mechanism.",
                    "confidence": 0.9,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion")
    assert "target_hash" in result
    assert not (isolated_memory / "wiki" / "Source_Stale-Target.md").exists()
    assert _ingest_finalization_proven("raw/stale-target.md", "stale-target") is False


def test_init_db_migrates_legacy_jobs_without_changing_payload(isolated_memory):
    path = db_store.get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT, payload TEXT, status TEXT, "
        "retries INTEGER DEFAULT 0, error_msg TEXT, created_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-job", "ingest", '{"filepath":"raw/legacy.md"}', "awaiting_subagent", 0, "", "2026-01-01", "2026-01-01"),
    )
    connection.commit()
    connection.close()

    db_store.init_db()

    columns = {row["name"] for row in db_store.get_connection().execute("PRAGMA table_info(jobs)")}
    assert {"lease_owner", "lease_token", "lease_generation"} <= columns
    row = db_store.get_connection().execute(
        "SELECT payload, status, lease_generation FROM jobs WHERE job_id = 'legacy-job'"
    ).fetchone()
    assert dict(row) == {
        "payload": '{"filepath":"raw/legacy.md"}',
        "status": "awaiting_subagent",
        "lease_generation": 0,
    }


def test_concurrent_subagent_claim_has_single_winner(isolated_memory):
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", {"filepath": "raw/concurrent.md", "hash": "hash"})
    db_store.mark_job_awaiting_subagent(job_id, "")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda owner: db_store.claim_subagent_jobs(limit=1, lease_seconds=60, lease_owner=owner),
            ["owner-a", "owner-b"],
        ))

    claimed = [row for result in results for row in result]
    assert len(claimed) == 1
    assert claimed[0]["job_id"] == job_id


@pytest.mark.parametrize("missing_key", ["lease_owner", "lease_token", "lease_generation"])
def test_finalize_requires_every_lease_credential(isolated_memory, missing_key):
    db_store.init_db()
    payload = {
        "filepath": "raw/credentials.md",
        "hash": "credentials-hash",
        "canonical_name": "Source_Credentials.md",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = db_store.claim_subagent_jobs(limit=1, lease_seconds=60, lease_owner="test-owner")[0]
    processed = {
        **payload,
        "job_id": job_id,
        "lease_owner": claim["lease_owner"],
        "lease_token": claim["lease_token"],
        "lease_generation": claim["lease_generation"],
    }
    processed.pop(missing_key)

    with pytest.raises(ValueError, match=missing_key):
        db_store.validate_ingest_job_finalization(job_id, processed)


def test_finalize_ingest_stamps_the_raw_hash_on_the_source_page(isolated_memory, monkeypatch):
    """The hash the page was compiled from is recorded by finalize, not by the model.

    Nothing else records it: the page carries ``sources: [raw/…]`` and a date, so the
    published-source reconciliation can only *infer* whether the raw file changed since.  With the
    stamp that question becomes a comparison -- and a model-authored hash would be one more thing
    to verify, which is why the stamp is applied on the write path.
    """
    db_store.init_db()
    # The write path validates against the purpose contract read from the memory root.
    _write_purpose_contract(isolated_memory)
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-stamp")
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/stamp.md",
            "hash": "stamp-hash-1234",
            "canonical_name": "Source_Stamp.md",
            "instructions": "compile this source",
        },
    )
    from vector_lake.native_llm import create_subagent_task

    task_path = create_subagent_task("ingest", "test", "JSON array", {"job_id": job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(task_path))
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    page = (
        "---\n"
        'id: "20260919_stamp"\n'
        'title: "Source Stamp"\n'
        "aliases: []\n"
        'type: "source"\n'
        'domain: "Medical_IT"\n'
        'topic_cluster: "General"\n'
        'status: "Active"\n'
        'epistemic-status: "seed"\n'
        "ttl: 365\n"
        'memory_type: "fact"\n'
        'memory_key: "ingest_source_stamp"\n'
        'categories: ["Healthcare_IT"]\n'
        "tags: []\n"
        'strategic_scope: "core"\n'
        'evidence_tier: "primary"\n'
        'updated: "2026-09-19"\n'
        'sources: ["raw/stamp.md"]\n'
        "---\n"
        "Body.\n"
    )
    result = _finalize(
        [{"filename": "Source_Stamp.md", "content": page}],
        {
            "filepath": "raw/stamp.md",
            "hash": "stamp-hash-1234",
            "canonical_name": "Source_Stamp.md",
            "integration": {
                "disposition": "standalone",
                "reason": "Standalone capture; no candidate pages to integrate with.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion"), result
    written = tool_ingest.get_wiki_dir() / "Source_Stamp.md"
    assert written.exists(), "the canonical Source page was not written"
    frontmatter, _ = tool_ingest.split_frontmatter(written.read_text(encoding="utf-8"))
    assert frontmatter.get("source_hash") == "stamp-hash-1234"
    assert frontmatter.get("sources") == ["raw/stamp.md"]


def test_the_dispatch_step_claims_a_short_lease(isolated_memory):
    """Building a packet is local and fast, so a crash must not hide the job for an hour.

    The worker claimed with ``lease_seconds=3600`` for a step that writes one file and flips a
    status, so a restart between the claim and the mark left the job ``dispatched`` until the lease
    expired -- observed 2026-09-19: one job sat that way for 45 minutes and nothing could touch it,
    because ``claim_pending_jobs`` will not reclaim an unexpired ``dispatched`` row.
    """
    db_store.init_db()
    payload = {
        "filepath": "raw/lease.md",
        "hash": "lease-hash",
        "canonical_name": "Source_Lease.md",
        "instructions": "compile this source",
    }
    db_store.enqueue_job("ingest", payload)

    with patch.object(ingest_worker, "claim_pending_jobs", wraps=db_store.claim_pending_jobs) as spy:
        ingest_worker.process_jobs()

    assert spy.call_args.kwargs.get("lease_seconds", 3600) <= 300, (
        "the dispatch step holds a lease far longer than the work it protects"
    )


def _source_page_content(filename: str, sources: list[str]) -> str:
    """A valid Source page body: the ``Source``/``System`` prefix skips the dual-schema rules."""
    return (
        "---\n"
        f'id: "source_{filename[:-3].lower()}"\n'
        f'title: "{filename[:-3]}"\n'
        "type: source\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [System_Architecture]\n"
        "updated: 2026-09-19T00:00:00+00:00\n"
        f"sources: {json.dumps(sources)}\n"
        "---\n"
        "## 1. 编译事实\n\nCompiled truth.\n\n## 2. 证据时间线\n"
    )


def test_a_source_page_named_against_the_mandate_is_refused_outright(isolated_memory, monkeypatch):
    """End to end: the two naming rules cannot diverge for a *new* ingest, and that is the contract.

    ``_apply_integration_disposition`` requires exactly one item named ``canonical_name`` before any
    page is written, so the older model-chosen shape (``Source_<dir>-<stem>-<hash8>``, which 443 of
    the 1 782 live pages use) is refused rather than silently accepted; the 443 predate that gate.
    Pinned because it is what makes the stamp's declaration fallback defence-in-depth rather than a
    repair: for an accepted ingest the mandated name is already the only possibility.
    """
    db_store.init_db()
    monkeypatch.setattr("vector_lake.tool_ingest.load_purpose_contract", lambda: {})
    monkeypatch.setattr("vector_lake.tool_ingest.validate_ingest_payload", lambda files, contract: [])
    job_id = db_store.enqueue_job(
        "ingest",
        {
            "filepath": "raw/news/target.md",
            "hash": "abc123",
            "canonical_name": "Source_Target.md",
            "instructions": "compile this source",
        },
    )
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]
    deviation_name = "Source_news-target-92f53b7a.md"

    result = _finalize(
        [{"filename": deviation_name, "content": _source_page_content(deviation_name, ["raw/news/target.md"])}],
        {
            "filepath": "raw/news/target.md",
            "hash": "abc123",
            "canonical_name": "Source_Target.md",
            "integration": {
                "disposition": "standalone",
                "reason": "Single-source compilation with no cross-page integration.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    from vector_lake.wiki_utils import get_wiki_dir


    wiki_dir = get_wiki_dir()
    assert "requires exactly one canonical source page: Source_Target.md" in result
    assert not (wiki_dir / deviation_name).exists()
    assert not (wiki_dir / "Source_Target.md").exists()


def _graph_section_heading():
    return "## Graph Integration"


def _relation_line(evidence, marker):
    return f"- [validates:: [[Concept_Target]]] {evidence} (confidence: 0.93) {marker}"


def test_the_legacy_relation_dedup_still_replaces_a_markless_generated_line():
    """The narrowed token set must keep doing the job it exists for.

    A relation written before markers existed carries no marker, so the only way a re-ingest
    can avoid appending a duplicate is to recognise the legacy shape.
    """
    heading = _graph_section_heading()
    legacy = "- [validates:: [[Concept_Target]]] 早期生成的关系 (confidence: 0.80)"
    out = tool_ingest._upsert_section_relation(
        f"# Source\n\n{heading}\n\n{legacy}\n",
        heading,
        "<!-- vector-lake-relation:abc123 -->",
        _relation_line("新证据", "<!-- vector-lake-relation:abc123 -->"),
        legacy_tokens=("[[Concept_Target]]", "confidence:"),
    )

    assert "新证据" in out
    assert "早期生成的关系" not in out, "the markless generated line must be replaced, not duplicated"
    assert out.count("[[Concept_Target]]") == 1


def test_a_hand_authored_bullet_that_merely_mentions_the_target_survives():
    """The source page's dedup token used to be the bare wikilink.

    The merge step deletes every line it matches, so any hand-authored bullet that merely
    mentioned the target was replaced by the generated relation and lost.  Requiring the
    legacy ``confidence:`` token as well keeps the dedup on generated lines only.
    """
    heading = _graph_section_heading()
    prose = "- 手工批注：[[Concept_Target]] 的口径与本源的结论有出入，需人工复核。"
    out = tool_ingest._upsert_section_relation(
        f"# Source\n\n{heading}\n\n{prose}\n",
        heading,
        "<!-- vector-lake-relation:abc123 -->",
        _relation_line("新证据", "<!-- vector-lake-relation:abc123 -->"),
        legacy_tokens=("[[Concept_Target]]", "confidence:"),
    )

    assert prose in out, "a hand-authored bullet that only mentions the target must survive"
    assert "新证据" in out
    assert out.count("[[Concept_Target]]") == 2


def test_the_coarse_token_set_is_what_deleted_the_hand_authored_bullet():
    """Differential oracle for the fix, on the exact input above.

    Runs the pre-fix token set over the same content so the regression stays honest: if some
    later change makes the bare wikilink harmless for another reason, this fails instead of
    the suite passing while the documented cause is no longer true.
    """
    heading = _graph_section_heading()
    prose = "- 手工批注：[[Concept_Target]] 的口径与本源的结论有出入，需人工复核。"
    out = tool_ingest._upsert_section_relation(
        f"# Source\n\n{heading}\n\n{prose}\n",
        heading,
        "<!-- vector-lake-relation:abc123 -->",
        _relation_line("新证据", "<!-- vector-lake-relation:abc123 -->"),
        legacy_tokens=("[[Concept_Target]]",),
    )

    assert prose not in out, "the pre-fix token set is expected to destroy this bullet"
    assert out.count("[[Concept_Target]]") == 1


def test_finalize_ingest_keeps_a_hand_authored_bullet_that_mentions_the_target(isolated_memory):
    """End-to-end guard on the production call site that supplies the token set.

    The unit tests above pin the behaviour of the helper; this one pins the fact that
    ``_apply_integration_disposition`` actually passes the narrowed tokens.
    """
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]

    prose = "- 手工批注：[[Concept_Target]] 的口径与本源的结论有出入，需人工复核。"
    legacy = "- [validates:: [[Concept_Target]]] 早期生成的关系 (confidence: 0.80)"
    source = _source_content().replace(
        "Primary source content.",
        f"""Primary source content.

{_graph_section_heading()}

{prose}
{legacy}
""",
    )

    payload = {
        "filepath": "raw/prose-mention.md",
        "hash": "prose-mention",
        "canonical_name": "Source_Prose-Mention.md",
    }
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = _finalize(
        [{"filename": "Source_Prose-Mention.md", "content": source}],
        {
            **payload,
            "integration": {
                "disposition": "integrated",
                "relations": [{
                    "target": "Concept_Target.md",
                    "target_hash": target_version,
                    "predicate": "validates",
                    "evidence": "The source directly supports the target mechanism.",
                    "confidence": 0.93,
                    "event_date": "2026-07-15",
                    "event_tag": "Validation",
                }],
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Successfully finalized ingestion"), result
    written = (isolated_memory / "wiki" / "Source_Prose-Mention.md").read_text(encoding="utf-8")
    assert prose in written, "the hand-authored bullet was destroyed by the integration merge"
    assert legacy not in written, "the markless generated relation line must still be replaced"
    assert "[validates:: [[Concept_Target]]]" in written
    assert written.count("[[Concept_Target]]") == 2
