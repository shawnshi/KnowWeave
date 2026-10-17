"""Review invariants under the owner-confirmed patch-only fallback contract."""

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_source_metadata import (
    seeded as _shared_seed,
    _prepare,
    _update,
    _db_snapshot,
)
from vector_lake import db_store, mutation_coordinator, source_metadata
from vector_lake.provenance_backfill import (
    backfill_provenance,
    revert_provenance_backfill,
)
from vector_lake.wiki_utils import get_wiki_dir

seeded = _shared_seed


def test_SMR001_no_live_atomic_writer_exists_in_patch_preparation(seeded, monkeypatch):
    before = _db_snapshot()
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Live publication or canonical enqueue attempted")

    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr(mutation_coordinator, "execute_mutation_batch", forbidden)
    monkeypatch.setattr(
        mutation_coordinator, "materialize_markdown_projection", forbidden
    )
    monkeypatch.setattr(db_store, "enqueue_mutations_batch", forbidden)
    _prepare([_update(["raw/docs/thing.md"])])
    assert path.read_bytes() == raw and _db_snapshot() == before


def test_SMR001_direct_editor_change_during_export_survives(seeded, monkeypatch):
    update = _update(["raw/docs/thing.md"])
    path = get_wiki_dir() / "Source_Chosen.md"
    marker = b"\nLATER DIRECT USER EDIT\n"
    original = source_metadata.ArtifactBundle.write_new
    injected = []

    def export(self, name, raw):
        result = original(self, name, raw)
        if not injected:
            path.write_bytes(path.read_bytes() + marker)
            injected.append(True)
        return result

    monkeypatch.setattr(source_metadata.ArtifactBundle, "write_new", export)
    before = _db_snapshot()
    result = _prepare([update])
    assert injected and marker in path.read_bytes() and _db_snapshot() == before
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["committed"] is False
    assert (
        manifest["entries"][0]["before_sha256"]
        != hashlib.sha256(path.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "key",
    ["source-metadata:legacy", "source-metadata:v2:" + ("0" * 64) + ":" + ("1" * 64)],
)
def test_SMR001_old_source_intent_is_refused_by_publication_and_consumer(
    seeded, monkeypatch, key
):
    from vector_lake import watchdog_app, indexer

    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()
    before = _db_snapshot()
    oid = db_store.enqueue_mutation(
        "Source_Chosen.md",
        "update",
        payload_text=raw.decode("utf-8"),
        validation_mode="schema",
        idempotency_key=key,
    )
    with pytest.raises(
        mutation_coordinator.ProjectionVersionConflict, match="exclusion|disabled"
    ):
        mutation_coordinator.materialize_markdown_projection(
            "Source_Chosen.md",
            "update",
            raw.decode("utf-8"),
            validation_mode="schema",
            outbox_id=oid,
        )
    indexed = []
    monkeypatch.setattr(
        indexer, "update_index_items", lambda names, **kw: indexed.extend(names)
    )
    result = watchdog_app.process_mutation_outbox_batch(limit=10)
    assert result["completed"] == 0 and not indexed and path.read_bytes() == raw
    assert _db_snapshot()["claims"] == before["claims"]


def test_SMR002_and_004_evidence_and_inline_anchors_are_never_rebound(seeded):
    path = get_wiki_dir() / "Source_Chosen.md"
    before = _db_snapshot()
    _prepare([_update(["raw/docs/thing.md"])])
    assert _db_snapshot() == before
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM mutation_outbox")
        .fetchone()[0]
        == 0
    )
    assert b"sources: []" in path.read_bytes()


@pytest.mark.parametrize("key", ['"sources"', "'sources'"])
def test_SMR003_single_quoted_key_supported_in_export_only(seeded, key):
    path = get_wiki_dir() / "Source_Chosen.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("sources: []", key + ": []"),
        encoding="utf-8",
    )
    raw = path.read_bytes()
    result = _prepare([_update(["raw/docs/thing.md"])])
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert path.read_bytes() == raw and manifest["source_applied"] == 0


@pytest.mark.parametrize(
    "cap,value",
    [
        ("MAX_RECORD_BYTES", 1),
        ("MAX_RECORDS", 0),
        ("MAX_CANONICAL_BYTES", 1),
        ("MAX_PAGE_BYTES", 1),
    ],
)
def test_SMR005_resource_caps_are_enforced_without_writes(
    seeded, monkeypatch, cap, value
):
    update = _update(["raw/docs/thing.md"])
    before = _db_snapshot()
    monkeypatch.setattr(source_metadata, cap, value)
    with pytest.raises(ValueError, match="budget"):
        _prepare([update])
    assert _db_snapshot() == before


def test_default_preview_and_explicit_patch_never_restore(seeded):
    before = _db_snapshot()
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()
    assert "[DRY RUN]" in backfill_provenance(dry_run=True)
    assert not list((get_wiki_dir() / ".meta").glob("source_metadata_patches/patch-*"))
    report = backfill_provenance(dry_run=False)
    assert "[PATCH ONLY]" in report and "restored 0 page(s)" in report
    assert path.read_bytes() == raw and _db_snapshot() == before


def test_stale_legacy_revert_is_not_a_body_overwrite(seeded):
    rollback = seeded / "rollback.jsonl"
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()
    rollback.write_text(
        json.dumps(
            {
                "page_key": "Source_Chosen",
                "before": raw.decode(),
                "after_sha256": "0" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="refused"):
        revert_provenance_backfill(str(rollback))
    assert path.read_bytes() == raw


def test_missing_database_is_not_created_or_reported_as_empty(seeded, monkeypatch):
    update = _update(["raw/docs/thing.md"])
    missing = seeded / "nonexistent.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(missing))
    with pytest.raises(FileNotFoundError):
        _prepare([update])
    assert not missing.exists()


def test_artifact_symlink_cannot_escape_metadata_boundary(seeded):
    base = get_wiki_dir() / ".meta/source_metadata_patches"
    outside = seeded / "outside"
    outside.mkdir()
    try:
        base.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Native symlink privilege unavailable")
    with pytest.raises((ValueError, OSError)) as refusal:
        _prepare([_update(["raw/docs/thing.md"])])
    if isinstance(refusal.value, ValueError):
        assert "boundary" in str(refusal.value)
    else:
        assert refusal.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert list(outside.iterdir()) == []
