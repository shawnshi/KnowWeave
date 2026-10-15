"""Regression guards for the runtime coordination surfaces repaired in phase 3.

Each case here reproduces a defect that was invisible to the existing suite:
a wake-up signal written to a path nothing polls, in-flight ingest state shared
across knowledge bases in the OS temp directory, and a cascade delete that
matched raw-source references by substring.
"""
from pathlib import Path

from vector_lake import db_store, tool_delete
from vector_lake.wiki_utils import (
    get_ingest_processing_path,
    get_memory_dir,
    get_meta_dir,
    get_outbox_signal_path,
    get_runtime_tmp_dir,
)

from tests.test_mutation_coordinator import _write_purpose_contract


def _concept(name: str, sources: list[str], body: str = "A fact.") -> str:
    source_lines = "\n".join(f"  - {source}" for source in sources)
    return f"""---
id: {name.lower()}
title: {name}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
strategic_scope: core
evidence_tier: primary
topic_cluster: Test
updated: 2026-01-01
sources:
{source_lines}
---
## 1. 编译事实 (Compiled Truth - READ MODEL)
### 物理机制 (Mechanism)
{body}

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
- [2026-01-01] [Observation] An observation. (Source: [[Source_Original]])
"""


def _raw(memory_dir: Path, relative: str, text: str = "raw submission\n") -> Path:
    path = memory_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Wake-up signal path agreement
# --------------------------------------------------------------------------- #

def test_outbox_signal_lives_under_the_memory_root_not_the_os_temp_dir(isolated_memory):
    import tempfile

    signal = get_outbox_signal_path()
    assert signal.is_relative_to(get_meta_dir())
    assert not signal.is_relative_to(Path(tempfile.gettempdir()) / "vector_lake_tmp")


def test_outbox_producer_and_consumer_agree_on_the_signal_path(isolated_memory):
    from vector_lake.mutation_coordinator import _signal_outbox_consumer
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    _signal_outbox_consumer()
    assert get_outbox_signal_path().exists(), "producer did not write the consumer's path"
    # The consumer clears the hint it finds; a drained batch is the observable result.
    stats = process_mutation_outbox_batch(limit=5)
    assert stats["claimed"] == 0


def test_runtime_tmp_dir_is_created_lazily_and_scoped(isolated_memory):
    tmp_dir = get_runtime_tmp_dir()
    assert tmp_dir.is_dir()
    assert tmp_dir.is_relative_to(get_meta_dir())


# --------------------------------------------------------------------------- #
# In-flight ingest bookkeeping
# --------------------------------------------------------------------------- #

def test_ingest_in_flight_state_lives_under_the_memory_root(isolated_memory):
    import tempfile

    assert get_ingest_processing_path().is_relative_to(get_meta_dir())
    assert not get_ingest_processing_path().is_relative_to(Path(tempfile.gettempdir()) / "vector_lake_tmp")


def test_identical_content_in_two_sources_is_enqueued_twice(isolated_memory, monkeypatch):
    """Content-hash keying silently dropped the second source."""
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    from vector_lake import tool_ingest

    _write_purpose_contract(isolated_memory)

    identical = "identical raw content about a vendor\n"
    _raw(isolated_memory, "raw/alpha.md", identical)
    _raw(isolated_memory, "raw/beta.md", identical)

    result = tool_ingest.prepare_ingest_batch(batch_size=5)
    jobs = db_store.get_connection().execute(
        "SELECT payload FROM jobs WHERE task_type = 'ingest'"
    ).fetchall()
    assert len(jobs) == 2, f"expected one job per source, got {len(jobs)}: {result}"


def test_failed_enqueue_releases_the_in_flight_claim(isolated_memory, monkeypatch):
    """A failed enqueue used to block the source for the whole TTL window."""
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    from vector_lake import tool_ingest

    _write_purpose_contract(isolated_memory)
    _raw(isolated_memory, "raw/one.md")

    original = tool_ingest._build_ingest_instructions

    def boom(*args, **kwargs):
        raise RuntimeError("simulated instruction build failure")

    tool_ingest._build_ingest_instructions = boom
    try:
        try:
            tool_ingest.prepare_ingest_batch(batch_size=5)
        except RuntimeError:
            pass
        else:  # pragma: no cover - must propagate
            raise AssertionError("the enqueue failure was swallowed")
    finally:
        tool_ingest._build_ingest_instructions = original

    assert tool_ingest._load_ingest_in_flight() == {}, "in-flight claim was not released"

    tool_ingest.prepare_ingest_batch(batch_size=5)
    count = db_store.get_connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1, "retry after a failed enqueue did not re-enqueue the source"


# --------------------------------------------------------------------------- #
# Cascade delete
# --------------------------------------------------------------------------- #

def test_delete_source_does_not_match_page_names_by_prefix(isolated_memory):
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    (wiki / "Source_Annual.md").write_text(_concept("Source_Annual", ["raw/Annual.md"]), encoding="utf-8")
    (wiki / "Source_AnnualReport.md").write_text(
        _concept("Source_AnnualReport", ["raw/AnnualReport.md"]), encoding="utf-8"
    )
    (wiki / "Concept_Unrelated.md").write_text(
        _concept("Concept_Unrelated", ["raw/Other.md"]), encoding="utf-8"
    )
    _raw(isolated_memory, "raw/Annual.md")

    report = tool_delete.delete_source(str(isolated_memory / "raw" / "Annual.md"), dry_run=True)

    assert "Source_Annual.md" in report
    assert "Source_AnnualReport.md" not in report
    assert "Concept_Unrelated.md" not in report


def test_delete_source_does_not_match_references_by_substring(isolated_memory):
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    # `Annual.md` is a substring of `AnnualReport.md` and of the nested path.
    (wiki / "Concept_Contains.md").write_text(
        _concept("Concept_Contains", ["raw/archive/AnnualReport.md"]), encoding="utf-8"
    )
    _raw(isolated_memory, "raw/Annual.md")

    report = tool_delete.delete_source(str(isolated_memory / "raw" / "Annual.md"), dry_run=True)

    assert "Concept_Contains.md" not in report
    assert "No related wiki pages found" in report


def test_delete_source_creates_a_recovery_point_and_forgets_processed_rows(isolated_memory):
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    (wiki / "Source_Target.md").write_text(_concept("Source_Target", ["raw/target.md"]), encoding="utf-8")
    raw_path = _raw(isolated_memory, "raw/target.md")

    db_store.init_db()
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT OR REPLACE INTO processed_files (filepath, file_hash, processed_at) VALUES (?, ?, ?)",
            (str(raw_path), "deadbeef", "2026-01-01T00:00:00+00:00"),
        )

    report = tool_delete.delete_source(str(raw_path), dry_run=False)

    assert "raw_deleted=True" in report
    assert not raw_path.exists()
    assert not (wiki / "Source_Target.md").exists()

    backups = sorted((get_memory_dir() / "backup" / "delete-source").glob("*/Source_Target.md"))
    assert backups, "cascade delete produced no recovery point"
    assert backups[-1].read_text(encoding="utf-8").startswith("---")

    remaining = db_store.get_connection().execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
    assert remaining == 0, "processed_files row survived the raw source deletion"


# --------------------------------------------------------------------------- #
# Subagent task-tree hygiene
# --------------------------------------------------------------------------- #

def test_stale_empty_runtime_dirs_are_pruned_but_task_packets_survive(tmp_path):
    """Every process start used to leave an unremovable `brain/runtime-*` tree."""
    import os
    import time

    from vector_lake import subagent_handoff

    brain_root = tmp_path / "brain"
    keep = brain_root / "runtime-current"
    stale_empty = brain_root / "runtime-stale-empty"
    stale_with_packet = brain_root / "runtime-stale-packet"
    fresh_empty = brain_root / "runtime-fresh-empty"
    unrelated = brain_root / "not-a-runtime-dir"

    packet = stale_with_packet / "scratch" / "subagent_tasks" / "task.json"
    packet.parent.mkdir(parents=True)
    packet.write_text("{}", encoding="utf-8")
    for path in (keep, stale_empty, fresh_empty, unrelated):
        path.mkdir(parents=True, exist_ok=True)

    old = time.time() - subagent_handoff._STALE_RUNTIME_TTL_SECONDS - 60
    for path in (stale_empty, stale_with_packet, unrelated):
        os.utime(path, (old, old))

    subagent_handoff._prune_stale_runtime_dirs(brain_root, keep=keep)

    assert not stale_empty.exists(), "an empty stale runtime dir was not pruned"
    assert stale_with_packet.exists(), "a runtime dir holding a task packet was pruned"
    assert packet.exists()
    assert keep.exists()
    assert fresh_empty.exists(), "a fresh runtime dir was pruned"
    assert unrelated.exists(), "a non-runtime directory was pruned"
