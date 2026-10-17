"""Export source-only patches; never apply without verified physical exclusion.

No exclusive publication backend is installed. This module deliberately has no
canonical mutation, outbox enqueue, live file replacement or approval-flag bypass.
Source containment, a manifest and hashes are not identity/admission/authorization.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from vector_lake import db_store, governance_store, mutation_coordinator
from vector_lake.schema_validator import check_placeholder_sources, validate_schema
from vector_lake.source_patch_artifacts import ArtifactBundle
from vector_lake.wiki_utils import get_index_path, get_wiki_dir, split_frontmatter
from vector_lake.yaml_utils import SafeLoader

MAX_PAGES = 32
MAX_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 512 * 1024
MAX_RECORDS = 8192
MAX_CANONICAL_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
_FRONT = re.compile(
    r"\A(?P<bom>\ufeff)?---\r?\n(?P<head>.*?)(?P<end>\r?\n---(?:\r?\n|$))", re.S
)


class _Budget:
    def __init__(self):
        self.deadline = time.monotonic() + 120
        self.bytes = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Source patch cooperative deadline exceeded")

    def entities(self, conn, page):
        self.check()
        cursor = conn.execute(
            "SELECT entity_id, CASE WHEN length(CAST(data_json AS BLOB))<=? THEN data_json END, "
            "length(CAST(data_json AS BLOB)) FROM entities WHERE f_page_key=? LIMIT ?",
            (MAX_RECORD_BYTES, page, MAX_RECORDS + 1),
        )
        records = []
        try:
            for index, (identity, raw, size) in enumerate(cursor):
                self.check()
                self.bytes += int(size or 0)
                if (
                    index >= MAX_RECORDS
                    or raw is None
                    or self.bytes > MAX_CANONICAL_BYTES
                ):
                    raise ValueError("Source patch canonical read budget exceeded")
                record = json.loads(raw)
                if record.get("entity_id") != identity:
                    raise ValueError("Canonical entity identity disagrees with storage")
                records.append((str(identity), raw))
        finally:
            cursor.close()
        return records


@contextmanager
def readonly_database(budget: _Budget | None = None):
    """Do not initialize, migrate or create the canonical database."""
    budget = budget or _Budget()
    budget.check()
    path = db_store.get_db_path().resolve()
    if not path.is_file():
        raise FileNotFoundError(
            "Existing canonical database is required for source patch planning"
        )
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.set_progress_handler(
            lambda: int(time.monotonic() >= budget.deadline), 1000
        )
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def bounded_page_versions(
    page_keys: set[str], budget: _Budget | None = None
) -> dict[str, str]:
    if len(page_keys) > MAX_PAGES:
        raise ValueError("Source patch version page budget exceeded")
    budget = budget or _Budget()
    with readonly_database(budget) as conn:
        result = {}
        for page in sorted(page_keys):
            records = budget.entities(conn, page)
            if records:
                result[page] = governance_store._canonical_entity_rows_version(records)
        return result


def _parse(text):
    return split_frontmatter(text.removeprefix("\ufeff").replace("\r\n", "\n"))


def _references(frontmatter):
    refs = frontmatter.get("sources") or []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise ValueError("Sources must be a list of strings")
    return refs


def render_sources(text: str, sources: list[str]) -> str:
    """Replace only a verified single-line YAML value; preserve all other bytes."""
    match = _FRONT.match(text)
    if not match:
        raise ValueError("Source patch requires existing UTF-8 frontmatter")
    mapping = yaml.compose(match["head"], Loader=SafeLoader)
    if not isinstance(mapping, yaml.MappingNode):
        raise ValueError("Source declarations require a YAML mapping")
    keys = [key.value for key, _ in mapping.value]
    if keys.count("sources") != 1 or "<<" in keys:
        raise ValueError(
            "Source patch requires exactly one unmerged sources declaration"
        )
    key, value = next(
        (key, value) for key, value in mapping.value if key.value == "sources"
    )
    supported = (isinstance(value, yaml.SequenceNode) and value.flow_style) or (
        isinstance(value, yaml.ScalarNode)
        and value.tag in {"tag:yaml.org,2002:str", "tag:yaml.org,2002:null"}
    )
    if (
        not supported
        or value.start_mark.line != key.start_mark.line
        or value.end_mark.line != key.start_mark.line
        or value.start_mark.index < key.end_mark.index
        or value.start_mark.index == value.end_mark.index
    ):
        raise ValueError(
            "Unsupported source layout: require a nonempty single-line source value; no patch exported"
        )
    begin = match.start("head") + value.start_mark.index
    finish = match.start("head") + value.end_mark.index
    after = text[:begin] + json.dumps(sources, ensure_ascii=False) + text[finish:]
    if _references(_parse(after)[0]) != sources:
        raise ValueError("Exported source declaration differs from requested sources")
    return after


def _read_page(path):
    if not path.is_file() or path.stat().st_size > MAX_PAGE_BYTES:
        raise ValueError("Source patch page budget exceeded or page missing")
    with path.open("rb") as handle:
        raw = handle.read(MAX_PAGE_BYTES + 1)
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("Source patch page budget exceeded")
    return raw


def prepare_source_metadata_batch(updates: Iterable[dict]) -> dict:
    """Export a non-applied patch bundle, using readonly canonical/file snapshots."""
    budget = _Budget()
    pending = []
    names = set()
    total_before = 0
    total_after = 0
    for item in updates:
        if len(pending) >= MAX_PAGES:
            raise ValueError("Source patch batch exceeds 32 pages")
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "sources",
            "expected_version",
            "expected_sha256",
        }:
            raise ValueError(
                "Source patches require filename, sources and both state tokens only"
            )
        name = item["filename"]
        refs = item["sources"]
        if not isinstance(name, str) or name in names:
            raise ValueError("Invalid or duplicate source patch filename")
        if (
            not isinstance(refs, list)
            or len(refs) > 64
            or any(
                not isinstance(r, str) or not r.strip() or len(r) > 4096 for r in refs
            )
        ):
            raise ValueError("Invalid bounded source list")
        if any(
            not isinstance(item[k], str)
            or re.fullmatch(r"[0-9a-f]{64}", item[k]) is None
            for k in ("expected_sha256", "expected_version")
        ):
            raise ValueError("Full canonical/file state tokens are required")
        path = mutation_coordinator.resolve_wiki_mutation_path(
            name, allow_existing_legacy_name=True
        )
        before = _read_page(path)
        if hashlib.sha256(before).hexdigest() != item["expected_sha256"]:
            raise ValueError("Wiki byte-hash conflict")
        text = before.decode("utf-8")
        fm, body = _parse(text)
        after = render_sources(text, list(refs)).encode("utf-8")
        afm, abody = _parse(after.decode("utf-8"))
        if body != abody or {k: v for k, v in fm.items() if k != "sources"} != {
            k: v for k, v in afm.items() if k != "sources"
        }:
            raise ValueError("Patch changed non-source content")
        old = _references(fm)
        check_placeholder_sources(refs, old)
        root = get_wiki_dir().parent.resolve()
        for ref in refs:
            if ref in old:
                continue
            target = (root / ref).resolve()
            if (
                not ref.startswith("raw/")
                or ".." in Path(ref).parts
                or not target.is_file()
                or not target.is_relative_to(root / "raw")
            ):
                raise ValueError(
                    "Changed source must name an existing contained raw file"
                )
        validate_schema(
            afm,
            after.decode("utf-8").removeprefix("\ufeff"),
            name,
            get_index_path(),
            False,
        )
        total_before += len(before)
        total_after += len(after)
        if (
            len(after) > MAX_PAGE_BYTES
            or total_before > MAX_BYTES
            or total_after > MAX_BYTES
        ):
            raise ValueError("Source patch input/output byte budget exceeded")
        pending.append(
            {**item, "sources": list(refs), "before": before, "after": after}
        )
        names.add(name)
    if not pending:
        raise ValueError("Empty source patch batch")
    versions = bounded_page_versions({Path(name).stem for name in names}, budget)
    if any(
        versions.get(Path(item["filename"]).stem) != item["expected_version"]
        for item in pending
    ):
        raise ValueError("Canonical version conflict")
    budget.check()
    with ArtifactBundle(get_wiki_dir()) as bundle:
        entries = []
        for index, item in enumerate(pending):
            budget.check()
            prefix = f"{index:03d}"
            before_name, after_name, diff_name = (
                prefix + ".before.bin",
                prefix + ".after.md",
                prefix + ".patch",
            )
            bundle.write_new(before_name, item["before"])
            bundle.write_new(after_name, item["after"])
            diff = "".join(
                difflib.unified_diff(
                    item["before"].decode("utf-8").splitlines(keepends=True),
                    item["after"].decode("utf-8").splitlines(keepends=True),
                    fromfile="a/" + item["filename"],
                    tofile="b/" + item["filename"],
                )
            )
            bundle.write_new(diff_name, diff.encode("utf-8"))
            entries.append(
                {
                    "filename": item["filename"],
                    "sources": item["sources"],
                    "expected_version": item["expected_version"],
                    "before_sha256": item["expected_sha256"],
                    "after_sha256": hashlib.sha256(item["after"]).hexdigest(),
                    "before_file": before_name,
                    "after_file": after_name,
                    "patch_file": diff_name,
                    "patch_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                }
            )
        result = {
            "status": "patch_only",
            "committed": False,
            "source_applied": 0,
            "patches_prepared": len(entries),
            "automatic_application_supported": False,
            "exclusion_established": False,
            "requires_controlled_maintenance_window": True,
            "reason": "No verified physical exclusion capability; this operation did not modify live Wiki files or database rows",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        }
        manifest = bundle.path / "manifest.json"
        manifest_raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        bundle.write_new("manifest.json", manifest_raw)
        return {
            **result,
            "manifest_path": str(manifest),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        }


def execute_source_metadata_batch(updates: Iterable[dict]):
    """Refuse the unreleased automatic writer; callers must explicitly prepare patches."""
    raise RuntimeError(
        "Automatic source application is unavailable without verified physical exclusion; use patch-only preparation"
    )
