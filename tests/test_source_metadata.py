"""Confirmed patch-only fallback: no verified exclusion means no live writes."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store
from vector_lake.wiki_utils import get_wiki_dir
from tests.test_provenance_backfill import _page, _raw, _job, _materialize


@pytest.fixture
def seeded(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    db_store.init_db()
    _page("Source_Chosen")
    _raw("raw/docs/thing.md")
    _materialize("Source_Chosen")
    _job("Source_Chosen.md", str(isolated_memory / "raw/docs/thing.md"))
    return isolated_memory


def _api():
    return importlib.import_module("vector_lake.source_metadata")


def _update(sources, name="Source_Chosen.md"):
    path = get_wiki_dir() / name
    return {
        "filename": name,
        "sources": sources,
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_version": governance_store.canonical_page_versions({name[:-3]})[
            name[:-3]
        ],
    }


def _db_snapshot():
    conn = db_store.get_connection()
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%_fts%' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    result = {}
    for table in tables:
        if table.startswith(("vec_", "vector_")):
            continue
        try:
            rows = conn.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"'
            ).fetchall()
        except Exception:
            raise AssertionError("Synthetic table snapshot failed: " + table)
        result[table] = sorted(repr(tuple(r)) for r in rows)
    return result


def _prepare(updates) -> dict:
    method = getattr(_api(), "prepare_source_metadata_batch", None)
    assert callable(method), "Patch-only API is not implemented"
    result = method(updates)
    assert isinstance(result, dict)
    return result


def test_without_exclusion_only_patch_artifacts_are_created(seeded):
    before = _db_snapshot()
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()
    result = _prepare([_update(["raw/docs/thing.md"])])
    assert result["status"] == "patch_only" and result["committed"] is False
    assert result["source_applied"] == 0 and result["patches_prepared"] == 1
    assert result["automatic_application_supported"] is False
    assert path.read_bytes() == raw and _db_snapshot() == before
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "patch_only" and manifest["committed"] is False
    entry = manifest["entries"][0]
    bundle = Path(result["manifest_path"]).parent
    assert (bundle / entry["before_file"]).read_bytes() == raw
    after = (bundle / entry["after_file"]).read_bytes()
    assert after.replace(b'sources: ["raw/docs/thing.md"]', b"sources: []") == raw
    assert hashlib.sha256(after).hexdigest() == entry["after_sha256"]
    assert (bundle / entry["patch_file"]).is_file()


def test_legacy_execute_name_cannot_bypass_patch_only(seeded):
    before = _db_snapshot()
    with pytest.raises(
        (ValueError, RuntimeError), match="exclusion|patch.only|automatic"
    ):
        _api().execute_source_metadata_batch([_update(["raw/docs/thing.md"])])
    assert _db_snapshot() == before


@pytest.mark.parametrize(
    "line_ending,bom", [("\n", ""), ("\r\n", ""), ("\r\n", "\ufeff")]
)
def test_patch_preserves_exact_body_header_and_encoding(seeded, line_ending, bom):
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = (bom + path.read_text(encoding="utf-8").replace("\n", line_ending)).encode()
    path.write_bytes(raw)
    plan = _prepare([_update(["raw/docs/thing.md"])])
    manifest = json.loads(Path(plan["manifest_path"]).read_text(encoding="utf-8"))
    after = (
        Path(plan["manifest_path"]).parent / manifest["entries"][0]["after_file"]
    ).read_bytes()
    assert path.read_bytes() == raw
    assert after.replace(b'sources: ["raw/docs/thing.md"]', b"sources: []") == raw


@pytest.mark.parametrize("key", ["sources", '"sources"', "'sources'"])
def test_duplicate_source_keys_refuse_without_artifacts_or_data_changes(seeded, key):
    path = get_wiki_dir() / "Source_Chosen.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "sources: []", "sources: []\n" + key + ": []"
        ),
        encoding="utf-8",
    )
    before = _db_snapshot()
    with pytest.raises(ValueError, match="source|declaration"):
        _prepare([_update(["raw/docs/thing.md"])])
    assert _db_snapshot() == before


@pytest.mark.parametrize("key", ["expected_sha256", "expected_version"])
def test_stale_preconditions_refuse_without_data_changes(seeded, key):
    update = _update(["raw/docs/thing.md"])
    update[key] = "0" * 64
    before = _db_snapshot()
    with pytest.raises(ValueError, match="conflict|version|hash"):
        _prepare([update])
    assert _db_snapshot() == before


@pytest.mark.parametrize(
    "refs",
    [
        ["../outside.md"],
        ["raw/../outside.md"],
        ["raw/missing.md"],
        ["https://invalid.example/"],
        [None],
    ],
)
def test_bad_source_is_not_exported_or_applied(seeded, refs):
    before = _db_snapshot()
    with pytest.raises((ValueError, TypeError)):
        _prepare([_update(refs)])
    assert _db_snapshot() == before


def test_no_unbounded_version_reader_or_database_initializer(seeded, monkeypatch):
    update = _update(["raw/docs/thing.md"])
    before = _db_snapshot()

    def forbidden(*args, **kwargs):
        raise AssertionError("Mutating initialization or unrestricted read used")

    monkeypatch.setattr(db_store, "init_db", forbidden)
    monkeypatch.setattr(governance_store, "canonical_page_versions", forbidden)
    _prepare([update])
    assert _db_snapshot() == before


def test_output_failure_never_changes_live_state(seeded, monkeypatch):
    update = _update(["raw/docs/thing.md"])
    before = _db_snapshot()
    path = get_wiki_dir() / "Source_Chosen.md"
    raw = path.read_bytes()
    from vector_lake.source_patch_artifacts import ArtifactBundle

    def fail(self, name, raw):
        raise OSError("synthetic export failure")

    monkeypatch.setattr(ArtifactBundle, "write_new", fail)
    with pytest.raises(OSError, match="export failure"):
        _prepare([update])
    assert path.read_bytes() == raw and _db_snapshot() == before


def test_patch_has_no_admission_or_history_side_effect(seeded):
    path = get_wiki_dir() / "Source_Chosen.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "sources: []", "source_hash: unadmitted-disk-stamp\nsources: []"
        ),
        encoding="utf-8",
    )
    before = _db_snapshot()
    _prepare([_update(["raw/docs/thing.md"])])
    assert _db_snapshot() == before


def test_no_boolean_or_extra_argument_establishes_exclusion(seeded):
    update = _update(["raw/docs/thing.md"])
    update["exclusive"] = True
    with pytest.raises(ValueError):
        _prepare([update])
