"""The ingest task packet's dispatch manifest.

The host retains ``source_projection_hash`` and ``ingest_contract_version`` and gives the model
only bounded relation candidates. These tests pin the producer side: the manifest is emitted,
and the candidate list in it is the *same* list the prompt shows.
"""

import json
import re
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.schema_validator import INGEST_INTEGRATION_PREDICATES, VALID_PREDICATES
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.wiki_utils import projection_hash

from tests.test_ingest_contract import _concept_content
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_projection_hash_normalises_line_endings():
    """A CRLF checkout must not hash a different knowledge than an LF one."""
    assert projection_hash("a\r\nb\r\n") == projection_hash("a\nb\n")
    assert projection_hash("a\rb") == projection_hash("a\nb")


def test_projection_hash_tracks_content():
    assert projection_hash("a") != projection_hash("b")
    assert projection_hash("a") != projection_hash("a\n"), "a trailing newline is content"


def _raw_and_candidate(isolated_memory):
    """A raw source plus one registerable candidate page and its index entry."""
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "news" / "manifest.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        _source_content().replace(
            "Primary source content.",
            "Primary source content about the target concept and its mechanism.",
        ),
        encoding="utf-8",
    )
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({
            "nodes": {
                "Concept_Target": {
                    "type": "concept",
                    "title": "Concept Target",
                    "summary": "Target compiled truth about the target concept.",
                }
            }
        }),
        encoding="utf-8",
    )
    return raw


def test_a_packet_carries_the_dispatch_manifest(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=5)

    payload = json.loads(
        db_store.get_connection().execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest'"
        ).fetchone()[0]
    )

    assert payload["ingest_contract_version"] == tool_ingest.INGEST_CONTRACT_VERSION
    assert payload["source_projection_hash"] == projection_hash(raw.read_text(encoding="utf-8"))
    assert payload["integration_candidates"], "the candidate manifest is empty"


def test_the_prompt_and_the_manifest_come_from_one_calculation(isolated_memory):
    """The model may only name candidates it was shown, so the two must not be computed apart."""
    raw = _raw_and_candidate(isolated_memory)
    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=5)

    payload = json.loads(
        db_store.get_connection().execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest'"
        ).fetchone()[0]
    )
    candidates = payload["integration_candidates"]
    rendered = tool_ingest._render_index_context(candidates)

    assert f"- {json.dumps(candidates[0], ensure_ascii=False)}" in rendered
    assert rendered in payload["instructions"], "the prompt does not show the manifest's candidates"
    assert [c["target"] for c in candidates] == [
        c["target"] for c in tool_ingest.select_ingest_candidates(str(raw))
    ]


def test_candidate_records_carry_the_markdown_projection_hash(isolated_memory):
    """``target_projection_hash`` is the content baseline, derived from the canonical projection."""
    raw = _raw_and_candidate(isolated_memory)
    candidates = tool_ingest.select_ingest_candidates(str(raw))

    assert candidates, "expected at least one candidate"
    candidate = candidates[0]
    canonical = tool_ingest._read_canonical_target_content(
        candidate["target"], candidate["target_hash"]
    )
    assert candidate["target_projection_hash"] == projection_hash(canonical)
    assert candidate["target_projection_hash"] != "", "an empty baseline is not a baseline"


def test_the_renderer_and_the_context_builder_agree(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    context, candidates = tool_ingest.ingest_context_and_candidates(str(raw))
    assert context == tool_ingest._render_index_context(candidates)
    assert tool_ingest._read_relevant_index_context(str(raw)) == context


def test_a_cold_knowledge_base_reports_no_candidates(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "news" / "cold.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_source_content(), encoding="utf-8")

    context, candidates = tool_ingest.ingest_context_and_candidates(str(raw))
    assert candidates == []
    assert context == tool_ingest._COLD_KB_CONTEXT


# ---------------------------------------------------------------------------------------------
# Consumer side: the manifest is an allowlist, not documentation.
# ---------------------------------------------------------------------------------------------

_SOURCE_PAGE = "Source_Manifest.md"


def _relation(target, target_hash, *, projection=None, predicate="validates"):
    relation = {
        "target": target,
        "target_hash": target_hash,
        "predicate": predicate,
        "evidence": "The source directly supports the target mechanism.",
        "confidence": 0.9,
        "event_date": "2026-07-15",
        "event_tag": "Validation",
    }
    if projection is not None:
        relation["target_projection_hash"] = projection
    return relation


def _apply(isolated_memory, raw, *, candidates, relation, source_projection_hash=None, filepath=None):
    processed_data = {
        "filepath": filepath if filepath is not None else str(raw),
        "canonical_name": _SOURCE_PAGE,
        "source_hash": "source-version",
        "integration": {"disposition": "integrated", "relations": [relation]},
    }
    if candidates is not _ABSENT:
        processed_data["integration_candidates"] = candidates
    if source_projection_hash is not None:
        processed_data["source_projection_hash"] = source_projection_hash
    return tool_ingest._apply_integration_disposition(
        [{"filename": _SOURCE_PAGE, "content": _source_content()}], processed_data
    )


class _Absent:
    """Sentinel: the packet has no ``integration_candidates`` key at all."""


_ABSENT = _Absent()


def _manifest(raw):
    return tool_ingest.select_ingest_candidates(str(raw))


def test_a_relation_outside_the_manifest_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    manifest = [{**candidate, "target": "Concept_Not-Offered.md"}]

    with pytest.raises(ValueError, match="not in the packet's candidate manifest"):
        _apply(isolated_memory, raw, candidates=manifest, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_manifest_whose_token_disagrees_with_the_relation_is_refused(isolated_memory):
    """A tampered manifest must not widen what the relation may name."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    manifest = [{**candidate, "target_hash": "0" * 64}]

    with pytest.raises(ValueError, match="does not match the candidate manifest"):
        _apply(isolated_memory, raw, candidates=manifest, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_stale_target_projection_hash_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="target_projection_hash is stale"):
        _apply(isolated_memory, raw, candidates=[candidate], relation=_relation(
            candidate["target"], candidate["target_hash"], projection="0" * 64))


def test_an_empty_manifest_permits_no_relation(isolated_memory):
    """``[]`` means "nothing to integrate with", not "no restrictions"."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="not in the packet's candidate manifest"):
        _apply(isolated_memory, raw, candidates=[], relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_packet_without_a_manifest_is_refused_for_integrated(isolated_memory):
    """M1, fail-closed: the migration refuses rather than restoring the old allow-all.

    A pre-manifest packet is rebuilt and re-dispatched by ``requeue_legacy_ingest_jobs``, which
    keys on the contract version -- so refusing here always has a recovery path.
    """
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="predates"):
        _apply(isolated_memory, raw, candidates=_ABSENT, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_packet_without_a_manifest_is_fine_when_it_declares_no_relation(isolated_memory):
    """The manifest bounds relations, so a disposition that declares none needs no manifest."""
    raw = _raw_and_candidate(isolated_memory)
    processed_data = {
        "filepath": str(raw),
        "canonical_name": _SOURCE_PAGE,
        "source_hash": "source-version",
        "integration": {
            "disposition": "standalone",
            "reason": "Single-source compilation with no cross-page integration.",
        },
    }
    files, disposition = tool_ingest._apply_integration_disposition(
        [{"filename": _SOURCE_PAGE, "content": _source_content()}], processed_data
    )
    assert disposition == "standalone"
    assert [item["filename"] for item in files] == [_SOURCE_PAGE]


def test_the_legacy_requeue_rebuilds_a_packet_for_an_older_contract(isolated_memory, monkeypatch):
    """The recovery path M1 depends on: a packet below the contract version is rebuilt.

    Observed on the live queue: 1730 ``jobs`` rows carry a payload written before the manifest,
    so without this the refusal above would strand them instead of migrating them.
    """
    raw = _raw_and_candidate(isolated_memory)
    payload = {
        "filepath": str(raw),
        "hash": "legacy-hash",
        "canonical_name": _SOURCE_PAGE,
        "instructions": "legacy",
    }
    from vector_lake import subagent_handoff

    db_store.enqueue_job("ingest", payload)
    for row in db_store.get_connection().execute("SELECT job_id FROM jobs"):
        db_store.mark_job_awaiting_subagent(row["job_id"], "synthetic-packet.json")
    removed = []
    monkeypatch.setattr(subagent_handoff, "remove_subagent_task", lambda path: removed.append(path))

    assert tool_ingest.requeue_legacy_ingest_jobs() == 1
    row = db_store.get_connection().execute("SELECT payload, status, task_packet_path FROM jobs").fetchone()
    rebuilt = json.loads(row["payload"])
    assert row["status"] == "queued" and row["task_packet_path"] is None
    assert removed == ["synthetic-packet.json"]
    assert rebuilt["ingest_contract_version"] == tool_ingest.INGEST_CONTRACT_VERSION
    assert rebuilt["integration_candidates"], "the rebuilt packet still has no manifest"
    assert rebuilt["source_projection_hash"]


@pytest.mark.parametrize("status", ["queued", "failed"])
def test_legacy_output_prompt_rebuilt_without_resetting_queued_attempts(isolated_memory, monkeypatch, status):
    from vector_lake import subagent_handoff

    raw = _raw_and_candidate(isolated_memory)
    payload = {
        "filepath": str(raw), "hash": "legacy-v2-hash", "canonical_name": _SOURCE_PAGE,
        "ingest_contract_version": 2, "instructions": "Return only a JSON array",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status = ?, retries = 2, task_packet_path = ? WHERE job_id = ?",
                     (status, "legacy-packet.json", job_id))
    removed = []
    monkeypatch.setattr(subagent_handoff, "remove_subagent_task", lambda path: removed.append(path))

    assert tool_ingest.requeue_legacy_ingest_jobs() == 1
    row = conn.execute("SELECT payload, status, retries, task_packet_path FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["task_packet_path"] is None and removed == ["legacy-packet.json"]
    rebuilt = json.loads(row["payload"])
    assert row["status"] == status and row["retries"] == 2
    assert rebuilt["ingest_contract_version"] == tool_ingest.INGEST_CONTRACT_VERSION == 10
    assert "Return one JSON object" in rebuilt["instructions"]
    assert rebuilt["integration_candidates"]
    assert tool_ingest.requeue_legacy_ingest_jobs() == 0


def test_legacy_requeue_does_not_delete_a_packet_claimed_during_rebuild(isolated_memory, monkeypatch):
    from vector_lake import subagent_handoff

    raw = _raw_and_candidate(isolated_memory)
    payload = {
        "filepath": str(raw), "hash": "legacy-v2-hash", "canonical_name": _SOURCE_PAGE,
        "ingest_contract_version": 2, "instructions": "Return only a JSON array",
    }
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "synthetic-packet.json")
    real_render = tool_ingest._build_ingest_instructions
    removed = []

    def claim_while_rebuilding(*args, **kwargs):
        db_store.update_job_status(job_id, "subagent_processing")
        return real_render(*args, **kwargs)

    monkeypatch.setattr(tool_ingest, "_build_ingest_instructions", claim_while_rebuilding)
    monkeypatch.setattr(subagent_handoff, "remove_subagent_task", lambda path: removed.append(path))

    assert tool_ingest.requeue_legacy_ingest_jobs() == 0
    row = db_store.get_connection().execute(
        "SELECT payload, status, task_packet_path FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert row["status"] == "subagent_processing"
    assert row["task_packet_path"] == "synthetic-packet.json"
    assert json.loads(row["payload"])["ingest_contract_version"] == 2
    assert removed == []


def test_a_correct_manifest_relation_is_accepted(isolated_memory):
    """Positive control: the checks above must not refuse a conforming relation."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    files, _ = _apply(
        isolated_memory,
        raw,
        candidates=[candidate],
        relation=_relation(
            candidate["target"],
            candidate["target_hash"],
            projection=candidate["target_projection_hash"],
        ),
        source_projection_hash=projection_hash(raw.read_text(encoding="utf-8")),
    )
    assert any(item["filename"] == candidate["target"] for item in files)


@pytest.mark.parametrize("predicate", sorted(INGEST_INTEGRATION_PREDICATES))
def test_every_integration_predicate_from_the_narrow_set_is_accepted(isolated_memory, predicate):
    """Positive control for the narrow set: membership is what the finalizer acts on."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    files, _ = _apply(
        isolated_memory,
        raw,
        candidates=[candidate],
        relation=_relation(
            candidate["target"], candidate["target_hash"], predicate=predicate,
            projection=candidate["target_projection_hash"],
        ),
        source_projection_hash=projection_hash(raw.read_text(encoding="utf-8")),
    )
    assert any(item["filename"] == candidate["target"] for item in files)


@pytest.mark.parametrize("predicate", ["has_part", "is-a", "part-of", "created"])
def test_a_page_link_predicate_is_refused_on_an_integration_relation(isolated_memory, predicate):
    """Negative control for the same boundary, using predicates the schema does accept.

    ``has_part`` and friends are legal page links, so an integration relation must be refused
    with a message that names the boundary.  Measured 2026-09-28: the ingest prompt carried the
    full page-link vocabulary without naming the narrower integration set, the model reused
    ``has_part``, and this refusal burned an attempt (and an hour of lease) per round trip.
    """
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    assert predicate in VALID_PREDICATES
    assert predicate not in INGEST_INTEGRATION_PREDICATES

    with pytest.raises(ValueError, match=f"unsupported integration predicate: {re.escape(predicate)}"):
        _apply(
            isolated_memory,
            raw,
            candidates=[candidate],
            relation=_relation(
                candidate["target"], candidate["target_hash"], predicate=predicate,
                projection=candidate["target_projection_hash"],
            ),
            source_projection_hash=projection_hash(raw.read_text(encoding="utf-8")),
        )


def test_a_source_edited_after_the_packet_was_built_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    stale = projection_hash(raw.read_text(encoding="utf-8"))
    raw.write_text(raw.read_text(encoding="utf-8") + "\nAn edit after dispatch.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after the ingest packet was built"):
        _apply(
            isolated_memory,
            raw,
            candidates=[candidate],
            relation=_relation(candidate["target"], candidate["target_hash"]),
            source_projection_hash=stale,
        )


# ---------------------------------------------------------------------------------------------
# The runner's fallback must stack under the model's disposition, not replace it.
# ---------------------------------------------------------------------------------------------


def test_runner_merges_model_decision_without_trusting_model_packet_fields():
    from scripts.ingest_runner import _merge_model_integration

    relations = [{"target": "Concept_Target.md", "target_hash": "version-token"}]
    packet = {"filepath": "raw/x.md", "lease_token": "host-token", "source_hash": "host-version"}
    merged = _merge_model_integration(packet, {"disposition": "integrated", "relations": relations})

    assert merged["integration"] == {"disposition": "integrated", "relations": relations}
    assert merged["lease_token"] == "host-token"
    assert merged["source_hash"] == "host-version"
    assert "integration" not in packet


@pytest.mark.parametrize("integration", [None, "integrated", {}, {"reason": "not decided"}])
def test_runner_refuses_a_missing_model_decision(integration):
    from scripts.ingest_runner import _merge_model_integration

    with pytest.raises(ValueError, match="explicit integration disposition"):
        _merge_model_integration({"filepath": "raw/x.md"}, integration)


def test_runner_preserves_standalone_reason_for_finalizer_validation():
    from scripts.ingest_runner import _merge_model_integration

    merged = _merge_model_integration({"filepath": "raw/x.md"}, {"disposition": "standalone", "reason": "short"})
    assert merged["integration"]["reason"] == "short"


def test_dispatch_source_version_update_is_bound_to_job_and_real_finalizer(isolated_memory):
    from vector_lake.ingest_worker import process_jobs
    from vector_lake.tool_ingest import claim_ingest_tasks
    raw = _raw_and_candidate(isolated_memory)
    tool_ingest.prepare_ingest_batch(batch_size=1)
    conn = db_store.get_connection()
    job = conn.execute("SELECT job_id, payload FROM jobs WHERE task_type='ingest'").fetchone()
    payload = json.loads(job["payload"])
    canonical = payload["canonical_name"]
    execute_mutation_plan(canonical, content=_source_content() + "\nSnapshot A.\n")
    version_a = governance_store.canonical_page_versions({canonical[:-3]})[canonical[:-3]]
    payload["source_hash"] = version_a
    with db_store.transaction():
        conn.execute("UPDATE jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job["job_id"]))
    execute_mutation_plan(canonical, content=_source_content() + "\nSnapshot B.\n")
    version_b = governance_store.canonical_page_versions({canonical[:-3]})[canonical[:-3]]
    assert version_a != version_b
    process_jobs()
    stored = json.loads(conn.execute("SELECT payload FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0])
    task = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))[0]
    processed = task["task_packet"]["metadata"]["processed_data"]
    assert processed["source_hash"] == stored["source_hash"] == version_b
    result = tool_ingest.finalize_ingest(
        [{"filename": canonical, "content": _source_content()}],
        {**processed, "integration": {"disposition": "standalone", "reason": "Synthetic independent source with no selected compiled relations."}},
    )
    assert "Successfully finalized" in result, result
    assert conn.execute("SELECT retries FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT file_hash FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()[0] == payload["hash"]


def test_mid_refresh_raw_edit_is_refused_and_real_finalizer_rejects_old_snapshot(isolated_memory, monkeypatch):
    from vector_lake.ingest_worker import process_jobs
    from vector_lake.tool_ingest import claim_ingest_tasks
    raw = _raw_and_candidate(isolated_memory)
    tool_ingest.prepare_ingest_batch(batch_size=1)
    conn = db_store.get_connection()
    payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE task_type='ingest'").fetchone()[0])
    process_jobs()
    processed = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))[0]["task_packet"]["metadata"]["processed_data"]
    original_candidates = tool_ingest.ingest_context_and_candidates
    def edit_between_hash_and_source_read(filepath):
        context = original_candidates(filepath)
        raw.write_text(raw.read_text(encoding="utf-8") + "\nMid-refresh edit B.\n", encoding="utf-8")
        return context
    monkeypatch.setattr(tool_ingest, "ingest_context_and_candidates", edit_between_hash_and_source_read)
    with pytest.raises(ValueError, match="source changed during dispatch refresh"):
        tool_ingest.refresh_ingest_dispatch_payload(payload)
    result = tool_ingest.finalize_ingest(
        [{"filename": payload["canonical_name"], "content": _source_content()}],
        {**processed, "integration": {"disposition": "standalone", "reason": "Synthetic attempted finalization from an outdated input snapshot."}},
    )
    assert "Successfully finalized" not in result
    assert conn.execute("SELECT count(*) FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()[0] == 0
    assert governance_store.canonical_page_versions({payload["canonical_name"][:-3]}).get(payload["canonical_name"][:-3], "") == ""


def test_stale_dispatch_handoff_and_failure_cannot_change_new_owner(isolated_memory, monkeypatch):
    from datetime import datetime, timedelta, timezone
    class Clock(datetime):
        current = None
        @classmethod
        def now(cls, tz=None):
            return cls.current
    db_store.init_db()
    payload = {"filepath": "raw/synthetic.md", "hash": "synthetic", "canonical_name": "Source_synthetic.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    claim = db_store.claim_pending_jobs(limit=1, lease_seconds=120)[0]
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job_id,))
    Clock.current = datetime.fromisoformat(claim["lease_until"]) - timedelta(seconds=119)
    monkeypatch.setattr(db_store, "datetime", Clock)
    new_claim = db_store.claim_pending_jobs(limit=1, lease_seconds=120)[0]
    assert new_claim["lease_until"] != claim["lease_until"]
    with pytest.raises(db_store.DispatchClaimLost):
        db_store.mark_job_awaiting_subagent(job_id, "stale-packet", dispatch_payload=payload, dispatch_claim=claim)
    assert db_store.fail_dispatch_claim(claim, "old worker failed") is False
    current = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    assert current == new_claim


def test_dispatch_expiry_is_checked_after_write_lock_acquisition(isolated_memory, monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    db_store.init_db()
    payload = {"filepath": "raw/synthetic.md", "hash": "synthetic", "canonical_name": "Source_synthetic.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.claim_pending_jobs(limit=1, lease_seconds=120)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", ("2026-10-05T12:02:00+00:00", job_id))
    before = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    class Clock(datetime):
        current = datetime(2026, 10, 5, 12, 1, 59, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current
    actual_transaction = db_store.transaction
    @contextmanager
    def lock_acquisition_crosses_expiry():
        with actual_transaction():
            # Advance only after actual BEGIN IMMEDIATE: deterministic lock-wait simulation.
            Clock.current = datetime(2026, 10, 5, 12, 2, 5, tzinfo=timezone.utc)
            yield
    monkeypatch.setattr(db_store, "datetime", Clock)
    monkeypatch.setattr(db_store, "transaction", lock_acquisition_crosses_expiry)
    with pytest.raises(db_store.DispatchClaimLost):
        db_store.mark_job_awaiting_subagent(job_id, "expired-packet", dispatch_payload=payload, dispatch_claim=before)
    assert dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()) == before


def test_default_runner_integrates_a_source_through_real_finalizer(isolated_memory, monkeypatch):
    """Only the model call is simulated; leases, version gates and Wiki writes use the temp root."""
    import subprocess
    from scripts import ingest_runner as runner
    from vector_lake.ingest_worker import process_jobs
    from vector_lake.tool_ingest import claim_ingest_tasks

    raw = _raw_and_candidate(isolated_memory)
    assert json.loads(tool_ingest.prepare_ingest_batch(batch_size=1))["filepath"] == str(raw)
    process_jobs()
    # Match the resident runner's lease; short claims intentionally reserve no model window.
    task = json.loads(claim_ingest_tasks(limit=1, lease_seconds=3600))[0]
    processed = task["task_packet"]["metadata"]["processed_data"]
    candidate = processed["integration_candidates"][0]
    canonical = processed["canonical_name"]
    model_result = {
        "files_written": [{"filename": canonical, "content": _source_content()}],
        "integration": {"disposition": "integrated", "relations": [
            _relation(candidate["target"], candidate["target_hash"],
                      projection=candidate["target_projection_hash"])
        ]},
    }
    monkeypatch.setattr(
        runner, "run_contained", lambda *args, **kwargs:
        subprocess.CompletedProcess(["model"], 0, json.dumps(model_result), ""),
    )

    stats, error = runner._process_task(task, False, "synthetic-model", runner.raw_publication_index())

    assert not error and stats["finalized"] == 1
    assert "[validates:: [[Concept_Target]]]" in (
        isolated_memory / "wiki" / canonical
    ).read_text(encoding="utf-8")
    assert f"(Source: [[{canonical.removesuffix('.md')}]])" in (
        isolated_memory / "wiki" / candidate["target"]
    ).read_text(encoding="utf-8")
    row = db_store.get_connection().execute(
        "SELECT result_json FROM jobs WHERE job_id = ?", (task["job_id"],)
    ).fetchone()
    assert json.loads(row["result_json"])["integration"]["disposition"] == "integrated"
    assert db_store.get_connection().execute(
        "SELECT file_hash FROM processed_files WHERE filepath = ?", (str(raw),)
    ).fetchone() is not None


# ---------------------------------------------------------------------------------------------
# Prompt and validator are two owners of one rule.
# ---------------------------------------------------------------------------------------------


def test_the_prompt_states_the_manifest_rule_the_finalizer_enforces():
    """The rule the refusals above implement has to be legible to the model, too."""
    template = (REPO_ROOT / "templates" / "prompts" / "ingest" / "main.md").read_text(encoding="utf-8")

    assert "integration_candidates" in template
    assert "empty manifest permits none" in template
    assert "cannot use `integrated`" in template
    assert "target_projection_hash" in template
    assert "source_projection_hash" in template


def test_the_prompt_does_not_promise_a_finalizer_that_never_reads_the_source():
    """It re-reads the claimed source to verify ``source_projection_hash``; saying otherwise is false."""
    template = (REPO_ROOT / "templates" / "prompts" / "ingest" / "main.md").read_text(encoding="utf-8")

    assert "Never return or ask the finalizer to read a `filepath`" not in template
    assert "verify `source_projection_hash`" in template


def test_two_character_chinese_entity_and_title_affinity_boost(isolated_memory):
    """Two-character Chinese entities (e.g. Person, Vendor) and title matches receive priority boost."""
    db_store.init_db()
    _write_purpose_contract(isolated_memory)

    # 1. Author an existing 2-character person node
    content = (
        "---\nid: 20260101_zhou\ntitle: 周炜\ntype: person\ndomain: Medical_IT\n"
        "status: Active\nepistemic-status: seed\ncategories: [Healthcare_IT]\n"
        "strategic_scope: core\nupdated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n"
        "# 周炜\n\n## 1. 编译事实\n*[System Directive]*\n\n### 核心权责与控制域 (Mandates & Domain of Control)\n"
        "- 周炜是医疗IT领军专家。\n\n---\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] 生平记录。\n"
    )
    execute_mutation_plan("Person_周炜.md", content=content)
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({
            "nodes": {
                "Person_周炜": {
                    "type": "person",
                    "title": "周炜",
                    "summary": "周炜是医疗IT领军专家。",
                }
            }
        }),
        encoding="utf-8",
    )

    # 2. Raw source whose filename/stem explicitly mentions 周炜
    raw = isolated_memory / "raw" / "research" / "周炜_历史定位与核心观点20260926.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("周炜在医疗信息化领域拥有重要历史地位与贡献。\n", encoding="utf-8")

    candidates = tool_ingest.select_ingest_candidates(str(raw))
    assert len(candidates) >= 1
    top = candidates[0]
    assert top["target"] == "Person_周炜.md"
    assert top["match_score"] >= 280  # 180 + len(2) + 100 title affinity boost
    assert any("title_affinity" in r for r in top["match_reasons"])
    assert any("exact_chinese" in r for r in top["match_reasons"])


def test_auto_heal_tag_collisions(isolated_memory):
    """A tag colliding with an existing node's title or alias is auto-healed into a semantic link."""
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({
            "nodes": {
                "Policy_电子病历系统功能应用水平分级评价": {
                    "type": "policy",
                    "title": "电子病历系统功能应用水平分级评价",
                    "aliases": ["电子病历评级", "EMR评级"],
                }
            }
        }),
        encoding="utf-8",
    )

    sample_content = (
        "---\nid: 20260101_test\ntitle: 测试节点\ntype: concept\ndomain: Medical_IT\n"
        "status: Active\nepistemic-status: seed\ncategories: [Healthcare_IT]\n"
        "tags: [电子病历评级, 院内系统替换]\n"
        "strategic_scope: core\nupdated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n"
        "# 测试节点\n\n## 1. 编译事实\n*[System Directive]*\n\n### 物理机制 (Mechanism)\n"
        "- 描述。\n\n---\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] 记录。\n"
    )

    files = [{"filename": "Concept_Test.md", "content": sample_content}]
    healed = tool_ingest.auto_heal_tag_collisions(files, index_path=index_path)

    assert len(healed) == 1
    healed_content = healed[0]["content"]
    assert "电子病历评级" not in healed_content.split("---")[1]  # removed from frontmatter tags
    assert "院内系统替换" in healed_content.split("---")[1]      # clean tag preserved
    assert "[[Policy_电子病历系统功能应用水平分级评价]]" in healed_content  # converted to semantic link


def test_stratify_candidates_preserves_type_diversity():
    """Stratified sampling prevents concept domination and reserves floor quotas for entities."""
    from vector_lake.tool_ingest import _stratify_candidates

    scored_items = []
    # 50 concepts with high scores
    for i in range(50):
        scored_items.append((250 - i, f"Concept_{i}", {"type": "concept"}, "hash", ["overlap"]))
    # 5 persons with lower scores
    for i in range(5):
        scored_items.append((170 - i, f"Person_{i}", {"type": "person"}, "hash", ["overlap"]))
    # 5 vendors with lower scores
    for i in range(5):
        scored_items.append((160 - i, f"Vendor_{i}", {"type": "vendor"}, "hash", ["overlap"]))

    scored_items.sort(key=lambda x: -x[0])
    # Without stratification, top 40 would be 100% concepts (Concept_0 to Concept_39).
    result = _stratify_candidates(scored_items, candidate_limit=40)
    assert len(result) == 40
    types = [item[2]["type"] for item in result]
    assert types.count("person") >= 4
    assert types.count("vendor") >= 3
    assert types.count("concept") >= 25
    # Strict score order maintained in output
    scores = [item[0] for item in result]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_candidate_scoring_tolerates_missing_vector_environment(isolated_memory):
    """Hybrid candidate search falls back gracefully without exception when vector env is absent."""
    raw = _raw_and_candidate(isolated_memory)
    # Even when GEMINI_API_KEY is unset or vector DB has no embeddings, candidate selection works smoothly
    candidates = tool_ingest.select_ingest_candidates(str(raw))
    assert isinstance(candidates, list)
    assert len(candidates) >= 1


def test_target_compiled_truth_slot_updated_with_timeline(isolated_memory):
    """Integrated relations update both Section 1 Compiled Truth slot and Section 2 Timeline."""
    _write_purpose_contract(isolated_memory)
    raw = _raw_and_candidate(isolated_memory)

    # Re-author Concept_Target.md to include the mechanism slot
    target_content = (
        "---\nid: concept_target\ntitle: Target Concept\ntype: concept\ndomain: General\n"
        "status: Active\nepistemic-status: seed\ncategories: [System_Architecture]\n"
        "updated: 2026-07-13T00:00:00+00:00\nsources: [raw/original.md]\nstrategic_scope: core\n"
        "evidence_tier: primary\n---\n## 1. 编译事实\n\n### 物理机制 (Mechanism)\n- 初始机制。\n\n"
        "## 2. 证据时间线\n- [2026-01-01] [Observation] 初始记录。\n"
    )
    execute_mutation_plan("Concept_Target.md", content=target_content)

    manifest = tool_ingest.select_ingest_candidates(str(raw))
    target_rec = next(c for c in manifest if c["target"] == "Concept_Target.md")

    files = [
        {
            "filename": "Source_manifest.md",
            "content": "---\nid: 20260101_src\ntitle: Source_manifest\ntype: source\ndomain: General\nstatus: Active\nepistemic-status: seed\ncategories: [Uncategorized]\nstrategic_scope: core\nupdated: 2026-01-01T00:00:00Z\nsources: [raw/news/manifest.md]\n---\n\n# Source\n\n## 1. 编译事实\n*[System Directive]*\n\n- source content.\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] event.\n",
        }
    ]
    processed_data = {
        "filepath": str(raw),
        "hash": "test_hash",
        "canonical_name": "Source_manifest.md",
        "source_hash": "",
        "source_projection_hash": tool_ingest.projection_hash(raw.read_text(encoding="utf-8")),
        "integration_candidates": [target_rec],
        "integration": {
            "disposition": "integrated",
            "relations": [
                {
                    "target": "Concept_Target.md",
                    "target_hash": target_rec["target_hash"],
                    "target_projection_hash": target_rec["target_projection_hash"],
                    "predicate": "validates",
                    "evidence": "详细验证了目标机制的完整性与有效性。",
                    "confidence": 0.95,
                    "event_date": "2026-01-01",
                    "event_tag": "Validation",
                }
            ],
        },
    }

    mutations, disposition = tool_ingest._apply_integration_disposition(files, processed_data)
    target_mut = next(m for m in mutations if m["filename"] == "Concept_Target.md")
    content = target_mut["content"]

    # Both Section 1 slot (物理机制) and Section 2 timeline are populated!
    assert "### 物理机制 (Mechanism)" in content
    assert "- [validates:: [[Source_manifest]]] 详细验证了目标机制的完整性与有效性。" in content
    assert "- [2026-01-01] [Validation] 详细验证了目标机制的完整性与有效性。" in content
