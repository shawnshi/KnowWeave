"""Phase 1 audit regressions covering storage gates, security boundaries, and core links."""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

from vector_lake import (
    claim_extractor,
    cli_app,
    db_store,
    governance_metrics,
    governance_store,
    indexer,
    outbox_retention,
    provenance,
    tool_memory,
    tool_relations,
    wiki_utils,
)
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.schema_validator import SchemaViolationException, validate_schema


def _write_valid_purpose(root) -> None:
    from tests.test_mutation_coordinator import _write_purpose_contract

    _write_purpose_contract(root)


def _source_md(title: str = "Phase1", topic_cluster: str = "General") -> str:
    return (
        "---\n"
        f'id: "source_{title.lower()}"\n'
        f'title: "{title}"\n'
        'type: "source"\n'
        'domain: "General"\n'
        f'topic_cluster: "{topic_cluster}"\n'
        'status: "Active"\n'
        'epistemic-status: "seed"\n'
        'categories: ["System_Architecture"]\n'
        'updated: "2026-11-22"\n'
        'sources: ["raw/phase1.md"]\n'
        'strategic_scope: "core"\n'
        'evidence_tier: "primary"\n'
        "---\n"
        "Source body.\n"
    )


def test_s01_query_only_connection_skips_index_dml(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, data_json, updated_at) VALUES (?, ?, ?, ?)",
            ("c_ro", "text", json.dumps({"claim_id": "c_ro", "claim_text": "text"}), "2026-11-22T00:00:00Z"),
        )
        conn.execute("DELETE FROM claim_index WHERE claim_id = 'c_ro'")
        conn.execute(
            "INSERT INTO operational_memory (memory_id, memory_type, score, status, ttl, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("m_ro", "fact", 0.5, "Active", 365.0, json.dumps({"memory_id": "m_ro", "text": "hello"}), "2026-11-22T00:00:00Z"),
        )
        conn.execute("DELETE FROM operational_memory_index WHERE memory_id = 'm_ro'")

    conn.execute("PRAGMA query_only = ON")
    try:
        claim_res = db_store.ensure_claim_index(conn)
        om_res = db_store.ensure_operational_memory_index(conn)
        assert claim_res["rebuilt"] == 0
        assert om_res["rebuilt"] == 0
    finally:
        conn.execute("PRAGMA query_only = OFF")


def test_s02_failed_outbox_blocks_unrelated_allows_self_heal_and_cli_outbox(isolated_memory, monkeypatch, capsys):
    _write_valid_purpose(isolated_memory)
    execute_mutation_plan("Source_Alpha.md", content=_source_md("Alpha"))
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO mutation_outbox (filename, mutation_type, status, last_error, created_at) "
            "VALUES ('Source_Failed.md', 'update', 'failed', 'disk busy', '2026-01-01T00:00:00Z')"
        )

    with pytest.raises(RuntimeError, match="hard runtime fault"):
        execute_mutation_plan("Source_Unrelated.md", content=_source_md("Unrelated"))

    ok, msg = execute_mutation_plan("Source_Failed.md", content=_source_md("Failed"))
    assert ok is True and "committed" in msg.lower()

    with db_store.transaction():
        conn.execute(
            "INSERT INTO mutation_outbox (filename, mutation_type, status, last_error, created_at) "
            "VALUES ('Source_Retry.md', 'update', 'failed', 'transient err', '2026-01-01T00:00:00Z')"
        )
    monkeypatch.setattr(sys, "argv", ["cli.py", "outbox", "--retry", "--apply"])
    assert cli_app.main() == 0
    out = capsys.readouterr().out
    assert "Re-queued 1 failed outbox row(s)" in out


def test_s05_outbox_retention_preserves_echo_suppression(isolated_memory):
    db_store.init_db()
    row_id = db_store.enqueue_mutation("Concept_Echo.md", "update", "canonical-body")
    claim = db_store.claim_mutation_outbox(limit=1)[0]
    db_store.complete_mutation_outbox(row_id, claim=claim)
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET completed_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (row_id,))

    res = outbox_retention.prune_outbox_payloads(days=1)
    assert res["stripped"] == 1
    assert db_store.is_managed_projection_state("Concept_Echo.md", "update", "canonical-body") is True
    assert db_store.is_managed_projection_state("Concept_Echo.md", "update", "tampered-body") is False


def test_sec01_controlled_memory_privilege_and_supersession_guard():
    base_fm = {
        "id": "evil_1",
        "title": "Uncontrolled Page",
        "type": "concept",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "updated": "2026-11-22",
        "sources": ["raw/x.md"],
        "memory_type": "preference",
    }
    body = "## 1. 编译事实\n- A.\n## 2. 证据时间线\n"
    with pytest.raises(SchemaViolationException, match="restricted"):
        validate_schema(base_fm, body, "Concept_Uncontrolled.md")

    reserved_fm = dict(base_fm)
    reserved_fm.pop("memory_type")
    reserved_fm["authority_score"] = 0.99
    with pytest.raises(SchemaViolationException, match="authority_score"):
        validate_schema(reserved_fm, body, "Concept_Uncontrolled.md")

    unverified_high = governance_store._memory_object_from_claim({
        "claim_id": "c_high",
        "claim_text": "Claimed authority",
        "source_page": "Concept_Uncontrolled.md",
        "evidence_ids": ["ev_1"],
        "evidence_support": "declared",
        "authority_score": 0.99,
        "confidence": None,
        "importance_score": None,
        "reinforcement_count": None,
    })
    assert unverified_high["authority_score"] <= 0.80
    assert unverified_high["confidence"] == 0.72

    verified_old = governance_store._memory_object_from_claim(governance_metrics.annotate_claim_validity({
        "claim_id": "c_old",
        "claim_text": "Established fact",
        "evidence_ids": ["ev_old"],
        "evidence_support": "verified",
        "verified_evidence_ids": ["ev_old"],
        "status": "Active",
        "confidence": 0.8,
        "updated_at": "2026-01-01T00:00:00Z",
    }))
    unverified_superseder = governance_store._memory_object_from_claim(governance_metrics.annotate_claim_validity({
        "claim_id": "c_evil_sup",
        "claim_text": "Fake override",
        "source_page": "Concept_Uncontrolled.md",
        "evidence_ids": ["ev_fake"],
        "evidence_support": "declared",
        "supersedes": ["c_old"],
        "authority_score": 0.99,
        "updated_at": "2026-11-22T00:00:00Z",
    }))
    unverified_superseder["validity_state"] = "active"
    resolved = governance_store._resolve_memory_conflicts({"items": {
        verified_old["memory_id"]: verified_old,
        unverified_superseder["memory_id"]: unverified_superseder,
    }})
    assert resolved["items"][verified_old["memory_id"]]["validity_state"] == "active"


def test_sec03_hardlink_and_segment_exclude_paths(isolated_memory, monkeypatch):
    raw_dir = isolated_memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    secret = raw_dir / "notes.md"
    secret.write_text("original", encoding="utf-8")
    alias = raw_dir / "hardlink.md"
    os.link(secret, alias)
    with pytest.raises(ValueError, match="hardlinked"):
        wiki_utils.validate_raw_ingest_source_path(str(alias))

    stocks_upper = raw_dir / "Stocks" / "holdings.md"
    stocks_upper.parent.mkdir(parents=True, exist_ok=True)
    stocks_upper.write_text("ticker", encoding="utf-8")
    with pytest.raises(ValueError, match="excluded"):
        wiki_utils.validate_raw_ingest_source_path(str(stocks_upper))

    stocks_sibling = raw_dir / "stocks-public" / "note.md"
    stocks_sibling.parent.mkdir(parents=True, exist_ok=True)
    stocks_sibling.write_text("public research", encoding="utf-8")
    assert wiki_utils.validate_raw_ingest_source_path(str(stocks_sibling)) == stocks_sibling.resolve()


def test_sec05_tool_memory_propagates_failure_and_collapses_newlines(isolated_memory, monkeypatch):
    _write_valid_purpose(isolated_memory)
    res = tool_memory.update_operational_memory("fact", "line1\nline2\r\nline3")
    assert "Successfully persisted" in res
    written = (isolated_memory / "wiki" / "Concept_OperationalFacts.md").read_text(encoding="utf-8")
    assert "line1 line2 line3" in written

    monkeypatch.setattr(
        "vector_lake.mutation_coordinator.execute_mutation_plan",
        lambda *a, **k: (False, "simulated write gate block"),
    )
    err = tool_memory.update_operational_memory("fact", "should fail")
    assert err.startswith("Error: Failed to persist")
    assert "simulated write gate block" in err


def test_ret01_extractor_binds_get_relations_and_provenance_handles_cjk_md(isolated_memory):
    _write_valid_purpose(isolated_memory)
    source_raw = isolated_memory / "raw" / "phase1.md"
    source_raw.parent.mkdir(parents=True, exist_ok=True)
    source_raw.write_text("Primary evidence.", encoding="utf-8")
    source_page = _source_md("Primary", topic_cluster="C")
    concept_page = (
        "---\n"
        'id: "concept_alpha"\n'
        'title: "Alpha System"\n'
        'type: "concept"\n'
        'domain: "General"\n'
        'topic_cluster: "C"\n'
        'status: "Active"\n'
        'epistemic-status: "seed"\n'
        'categories: ["System_Architecture"]\n'
        'updated: "2026-11-22"\n'
        'sources: ["raw/phase1.md"]\n'
        'strategic_scope: "core"\n'
        'evidence_tier: "primary"\n'
        "---\n"
        "## 1. 编译事实\n"
        "### 物理机制 (Mechanism)\n"
        "- Alpha [depends-on:: [[Source_Primary]]] in production (Source: [[Source_Primary]]).\n"
        "\n"
        "## 2. 证据时间线\n"
        "- [2026-11-22] [Observation] 医疗系统互联互通正式验证 Alpha System.\n"
    )
    execute_mutation_plan("Source_Primary.md", content=source_page)
    execute_mutation_plan("Concept_Alpha.md", content=concept_page)
    indexer.generate_index()

    assert claim_extractor.claim_type_for_heading("bullet", "## 2. 证据时间线 (Compiled Truth)", "event") == "timeline-event"

    rels = tool_relations.get_relations("Concept_Alpha", "General", "C")
    assert rels["status"] == "ok"
    declared_rows = [r for r in rels["relations"] if r["layer"] == "declared"]
    assert declared_rows
    assert declared_rows[0]["context"]["status"] == "matched"
    assert declared_rows[0]["context"]["evidence_count"] == 2

    trace = provenance.build_trace_for_query("医疗系统互联互通 Alpha")
    assert trace["items"]
    assert trace["items"][0]["claim_text"].startswith("[2026-11-22]")
