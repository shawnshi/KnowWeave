"""Physical regressions for the first patch-only independent review."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_source_metadata import (
    seeded as _shared_seed,
    _prepare,
    _update,
    _db_snapshot,
)
from vector_lake import cli_app, provenance_backfill
from vector_lake.wiki_utils import get_wiki_dir

seeded = _shared_seed


@pytest.mark.parametrize("ending,bom", [("\n", ""), ("\r\n", "\ufeff")])
def test_PATCH_R1_RENDER_non_source_comments_and_whitespace_survive(
    seeded, ending, bom
):
    path = get_wiki_dir() / "Source_Chosen.md"
    text = path.read_text(encoding="utf-8").replace(
        "sources: []",
        "sources: []  # source audit\n  # unrelated retained audit\n   \n",
    )
    before = (bom + text.replace("\n", ending)).encode("utf-8")
    path.write_bytes(before)
    result = _prepare([_update(["raw/docs/thing.md"])])
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    after = (
        Path(result["manifest_path"]).parent / manifest["entries"][0]["after_file"]
    ).read_bytes()
    assert after.replace(b'["raw/docs/thing.md"]', b"[]") == before
    assert path.read_bytes() == before


def _stale_rollback(seeded):
    path = get_wiki_dir() / "Source_Chosen.md"
    rollback = seeded / "stale-rollback.jsonl"
    rollback.write_text(
        json.dumps(
            {
                "page_key": "Source_Chosen",
                "before": path.read_text(encoding="utf-8"),
                "after_sha256": "0" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return rollback


def test_PATCH_R1_EXIT_stale_revert_is_nonzero_cli_failure(seeded, monkeypatch):
    import sys

    rollback = _stale_rollback(seeded)
    before = _db_snapshot()
    monkeypatch.setattr(cli_app, "_configure_stdout", lambda: None)
    monkeypatch.setattr(cli_app, "_load_env", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "provenance-backfill", "--revert", str(rollback), "--prepare-patch"],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = cli_app.main()
    assert result != 0
    assert "refus" in output.getvalue().lower() or "stale" in output.getvalue().lower()
    assert _db_snapshot() == before


def test_PATCH_R1_EXIT_partial_reversal_exports_remain_disclosed(seeded):
    from tests.test_provenance_backfill import _page, _materialize

    _page("Source_Zlate")
    _materialize("Source_Zlate")
    current = (get_wiki_dir() / "Source_Chosen.md").read_bytes()
    later = (get_wiki_dir() / "Source_Zlate.md").read_bytes()
    rollback = seeded / "partial-rollback.jsonl"
    rollback.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "page_key": "Source_Chosen",
                    "before": current.decode(),
                    "after_sha256": hashlib.sha256(current).hexdigest(),
                },
                {
                    "page_key": "Source_Zlate",
                    "before": later.decode(),
                    "after_sha256": "0" * 64,
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        provenance_backfill.revert_provenance_backfill(str(rollback), batch=1)
    manifests = list(
        (get_wiki_dir() / ".meta/source_metadata_patches").glob("patch-*/manifest.json")
    )
    assert len(manifests) == 1
    assert str(manifests[0]) in str(failure.value)
    assert "prepared 1" in str(failure.value)
    assert (get_wiki_dir() / "Source_Zlate.md").read_bytes() == later


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory-pinning race")
def test_PATCH_R1_PATH_directory_swap_cannot_redirect_private_artifacts(
    seeded, monkeypatch
):
    base = get_wiki_dir() / ".meta/source_metadata_patches"
    base.mkdir()
    displaced = base.with_name("displaced-private-artifacts")
    outside = seeded / "outside-artifact-boundary"
    outside.mkdir()
    original = Path.mkdir
    attempts = []
    denied = []

    def replace_directory(path, *args, **kwargs):
        if path.parent == base and path.name.startswith("patch-") and not attempts:
            attempts.append(True)
            try:
                os.rename(base, displaced)
            except PermissionError as exc:
                denied.append(exc.winerror)
            else:
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(base), str(outside)],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode:
                    raise RuntimeError("Synthetic junction fixture creation failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", replace_directory)
    before = _db_snapshot()
    _prepare([_update(["raw/docs/thing.md"])])
    assert attempts and denied == [32], (
        "Expected a kernel sharing violation, not a pathname/ACL check"
    )
    assert not list(outside.iterdir())
    assert _db_snapshot() == before


def test_artifact_capability_expires_and_leaves_are_exclusive(seeded):
    from vector_lake.source_patch_artifacts import ArtifactBundle

    bundle = ArtifactBundle(get_wiki_dir())
    with bundle:
        bundle.write_new("retained.bin", b"synthetic original")
        with pytest.raises(OSError):
            bundle.write_new("retained.bin", b"must not overwrite")
    assert (bundle.path / "retained.bin").read_bytes() == b"synthetic original"
    with pytest.raises(RuntimeError, match="not active"):
        bundle.write_new("after-close.bin", b"forbidden")


def test_unsupported_artifact_capability_fails_closed(seeded, monkeypatch):
    from vector_lake.source_patch_artifacts import ArtifactBundle

    bundle = ArtifactBundle(get_wiki_dir())
    with monkeypatch.context() as scope:
        scope.setattr(os, "name", "unsupported")
        with pytest.raises(RuntimeError, match="unsupported"):
            with bundle:
                raise AssertionError("Unsupported backend was admitted")
    assert not bundle.path.exists()


def test_multiline_sources_are_refused_without_export_or_changes(seeded):
    path = get_wiki_dir() / "Source_Chosen.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "sources: []",
            "sources:\n- raw/docs/thing.md\n  # preserve unsupported block",
        ),
        encoding="utf-8",
    )
    raw = path.read_bytes()
    before = _db_snapshot()
    with pytest.raises(ValueError, match="Unsupported source layout"):
        _prepare([_update([])])
    assert path.read_bytes() == raw and _db_snapshot() == before
    assert not list((get_wiki_dir() / ".meta").glob("source_metadata_patches/patch-*"))


def _two_rollback_records(seeded):
    from tests.test_provenance_backfill import _page, _materialize

    _page("Source_Zlate")
    _materialize("Source_Zlate")
    result = []
    for key in ("Source_Chosen", "Source_Zlate"):
        raw = (get_wiki_dir() / (key + ".md")).read_bytes()
        result.append(
            {
                "page_key": key,
                "before": raw.decode("utf-8"),
                "after_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return result


def _run_reversal_cli(rollback, monkeypatch):
    import sys

    monkeypatch.setattr(cli_app, "_configure_stdout", lambda: None)
    monkeypatch.setattr(cli_app, "_load_env", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli.py",
            "provenance-backfill",
            "--revert",
            str(rollback),
            "--batch",
            "1",
            "--prepare-patch",
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = cli_app.main()
    return code, output.getvalue()


@pytest.mark.parametrize(
    "invalid",
    ["missing-before", "wrong-before-type", "not-a-record", "wrong-hash-type"],
)
def test_PATCH_R2_structure_validation_precedes_every_export(
    seeded, monkeypatch, invalid
):
    records: list[Any] = _two_rollback_records(seeded)
    if invalid == "missing-before":
        records[1].pop("before")
    elif invalid == "wrong-before-type":
        records[1]["before"] = None
    elif invalid == "wrong-hash-type":
        records[1]["after_sha256"] = 1
    else:
        records[1] = []
    rollback = seeded / "malformed-rollback.jsonl"
    rollback.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    before = _db_snapshot()
    code, output = _run_reversal_cli(rollback, monkeypatch)
    assert code == 1 and "prepared 0" in output and "applied 0" in output
    assert not list((get_wiki_dir() / ".meta").glob("source_metadata_patches/patch-*"))
    assert _db_snapshot() == before


def test_PATCH_R2_yaml_failure_discloses_first_bundle_in_cli(seeded, monkeypatch):
    records = _two_rollback_records(seeded)
    records[1]["before"] = "---\nsources: [unterminated\n---\n"
    rollback = seeded / "bad-yaml-rollback.jsonl"
    rollback.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    before = _db_snapshot()
    code, output = _run_reversal_cli(rollback, monkeypatch)
    manifests = list(
        (get_wiki_dir() / ".meta").glob("source_metadata_patches/patch-*/manifest.json")
    )
    assert code == 1 and len(manifests) == 1
    assert (
        "prepared 1" in output and "applied 0" in output and str(manifests[0]) in output
    )
    assert _db_snapshot() == before


@pytest.mark.parametrize("error", [KeyError, AttributeError, RuntimeError])
def test_PATCH_R2_nonfatal_batch_error_retains_receipt_and_cause(
    seeded, monkeypatch, error
):
    records = _two_rollback_records(seeded)
    rollback = seeded / "second-batch-failure.jsonl"
    rollback.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    original = provenance_backfill.source_metadata.prepare_source_metadata_batch
    calls = []

    def fail_second(updates):
        calls.append(True)
        if len(calls) == 2:
            raise error("synthetic second batch error")
        return original(updates)

    monkeypatch.setattr(
        provenance_backfill.source_metadata,
        "prepare_source_metadata_batch",
        fail_second,
    )
    before = _db_snapshot()
    with pytest.raises(provenance_backfill.PatchPreparationError) as failure:
        provenance_backfill.revert_provenance_backfill(str(rollback), batch=1)
    manifests = list(
        (get_wiki_dir() / ".meta").glob("source_metadata_patches/patch-*/manifest.json")
    )
    assert len(manifests) == 1 and str(manifests[0]) in str(failure.value)
    assert failure.value.patches_prepared == 1
    assert isinstance(failure.value.__cause__, error)
    assert _db_snapshot() == before
