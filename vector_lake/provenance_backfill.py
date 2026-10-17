"""Preview and export source-only patches from exact source-locator candidates.

No live Wiki, canonical history, admission facts or outbox are changed. Source
matches are not factual verification. Failed preparation raises with already-
exported manifest paths; stale reversal never acknowledges success.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import mutation_coordinator, source_metadata
from vector_lake.wiki_utils import canonical_source_name, get_meta_dir, get_wiki_dir, normalize_sources, split_frontmatter

log = logging.getLogger(__name__)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_root() -> Path:
    return get_wiki_dir().parent


def _no_source_pages() -> dict[str, int]:
    """``page_key -> claim count`` for every page carrying an evidence gap of ``no_source``."""
    with source_metadata.readonly_database() as conn:
        rows = conn.execute(
            """SELECT CASE WHEN length(CAST(json_extract(data_json, '$.locator.page_key') AS BLOB)) <= 4096
                     THEN json_extract(data_json, '$.locator.page_key') END AS page_key, COUNT(*) AS claims
               FROM claims
               WHERE json_extract(data_json, '$.evidence_gap') = 'no_source'
                 AND json_extract(data_json, '$.locator.page_key') IS NOT NULL
               GROUP BY 1 LIMIT ?""", (source_metadata.MAX_RECORDS+1,)
        ).fetchall()
    if len(rows) > source_metadata.MAX_RECORDS or any(row["page_key"] is None for row in rows):
        raise ValueError("Source planning page count/key byte budget exceeded")
    return {str(row["page_key"]): int(row["claims"]) for row in rows}


def declared_raw_paths(page_key: str) -> list[str]:
    """The ``raw/`` entries the page declares today, or an empty list."""
    path = mutation_coordinator.resolve_wiki_mutation_path(f"{page_key}.md", allow_existing_legacy_name=True)
    if not path.is_file():
        return []
    text = _page_snapshot(page_key).decode("utf-8").removeprefix("\ufeff").replace("\r\n", "\n")
    frontmatter, _ = split_frontmatter(text)
    return [ref for ref in normalize_sources(frontmatter.get("sources") or []) if ref.startswith("raw/")]


def _ledger_paths() -> dict[str, str]:
    """Read bounded exact ingest ledger locators, never full unguarded payloads."""
    resolved = {}
    budget = source_metadata._Budget()
    total = 0
    with source_metadata.readonly_database(budget) as conn:
        cursor = conn.execute(
            "SELECT CASE WHEN length(CAST(payload AS BLOB))<=? AND json_valid(payload) THEN json_extract(payload, '$.filepath') END AS filepath, "
            "CASE WHEN length(CAST(payload AS BLOB))<=? AND json_valid(payload) THEN json_extract(payload, '$.canonical_name') END AS canonical_name, "
            "json_valid(payload) AS valid, length(CAST(payload AS BLOB)) AS bytes FROM jobs "
            "WHERE payload LIKE '%\"filepath\"%' AND payload LIKE '%canonical_name%' LIMIT ?",
            (source_metadata.MAX_RECORD_BYTES, source_metadata.MAX_RECORD_BYTES, source_metadata.MAX_RECORDS+1))
        try:
            for index, row in enumerate(cursor):
                budget.check()
                total += row["bytes"]
                if index >= source_metadata.MAX_RECORDS or total > source_metadata.MAX_CANONICAL_BYTES or row["bytes"] > source_metadata.MAX_RECORD_BYTES:
                    raise ValueError("Source planning ledger read budget exceeded")
                if not row["valid"]:
                    raise ValueError("Malformed ingest ledger payload during source planning")
                name = str(row["canonical_name"] or "").strip()
                filepath = str(row["filepath"] or "").strip().replace("\\", "/")
                if not name or not filepath:
                    continue
                if "/raw/" in filepath:
                    filepath = "raw/" + filepath.split("/raw/", 1)[1]
                resolved.setdefault(name.replace(".md", ""), filepath)
        finally:
            cursor.close()
    return resolved


def _canonical_name_paths() -> dict[str, list[str]]:
    """``canonical source page name -> raw paths``, inverted over the whole raw tree."""
    inverted: dict[str, list[str]] = collections.defaultdict(list)
    root = _memory_root()
    retained_bytes = 0
    budget = source_metadata._Budget()
    for count, path in enumerate((root / "raw").rglob("*")):
        budget.check()
        if count >= 200_000:
            raise ValueError("Source planning raw inventory count budget exceeded")
        if path.is_file():
            relative = str(path.relative_to(root)).replace("\\", "/")
            retained_bytes += len(relative.encode("utf-8"))
            if retained_bytes>source_metadata.MAX_CANONICAL_BYTES:
                raise ValueError("Source planning raw inventory byte budget exceeded")
            inverted[canonical_source_name(relative).replace(".md", "")].append(relative)
    return inverted


def plan_provenance_backfill() -> dict:
    """Read-only plan: what can be restored by an exact rule, and what cannot."""
    root = _memory_root()
    pages = _no_source_pages()
    ledger = _ledger_paths()
    canonical = _canonical_name_paths()

    restorable: list[dict] = []
    ambiguous: list[dict] = []
    unmatched: list[dict] = []
    already_declared: list[dict] = []
    for page_key, claims in sorted(pages.items()):
        if declared_raw_paths(page_key):
            # A page that already declares a raw path but still shows no_source is a different
            # defect from an unrecorded provenance, so it is reported apart and not touched.
            already_declared.append({"page_key": page_key, "claim_count": claims})
            continue
        raw_path = ledger.get(page_key)
        rule = "job-ledger"
        if not raw_path or not (root / raw_path).exists():
            candidates = canonical.get(page_key, [])
            rule = "canonical-name"
            raw_path = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                ambiguous.append(
                    {"page_key": page_key, "claim_count": claims, "candidates": candidates[:5]}
                )
                continue
        if raw_path and (root / raw_path).exists():
            restorable.append(
                {"page_key": page_key, "raw_path": raw_path, "rule": rule, "claim_count": claims}
            )
        else:
            unmatched.append({"page_key": page_key, "claim_count": claims})

    return {
        "no_source_pages": len(pages),
        "no_source_claims": sum(pages.values()),
        "restorable": restorable,
        "restorable_claims": sum(entry["claim_count"] for entry in restorable),
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "already_declared": already_declared,
    }


def _rollback_path() -> Path:
    directory = get_meta_dir() / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-provenance-backfill.rollback.jsonl"


def _render_with_sources(text: str, raw_path: str) -> str:
    return source_metadata.render_sources(text, [raw_path])


def _page_snapshot(page_key: str) -> bytes:
    path = mutation_coordinator.resolve_wiki_mutation_path(f"{page_key}.md", allow_existing_legacy_name=True)
    if not path.is_file() or path.stat().st_size > source_metadata.MAX_PAGE_BYTES:
        raise ValueError("Rollback/backfill requires a bounded existing Wiki page")
    with path.open("rb") as handle:
        raw = handle.read(source_metadata.MAX_PAGE_BYTES + 1)
    if len(raw) > source_metadata.MAX_PAGE_BYTES:
        raise ValueError("Page grew beyond the source maintenance byte budget")
    return raw


def _source_update(page_key: str, before: bytes, refs: list[str]) -> dict:
    filename = page_key + ".md"
    version = source_metadata.bounded_page_versions({page_key}).get(page_key)
    if not version:
        raise ValueError("Existing canonical page version is required")
    return {"filename": filename, "sources": refs, "expected_version": version,
            "expected_sha256": hashlib.sha256(before).hexdigest()}


class PatchPreparationError(ValueError):
    """Preparation failed, possibly after earlier non-applied bundles were exported."""
    def __init__(self, reason: str, prepared: int, manifests: list[str]):
        self.patches_prepared = prepared
        self.manifest_paths = list(manifests)
        super().__init__("\n".join([f"{reason}; prepared {prepared} patch(es); applied 0 page(s).",
            *[f"patch manifest: {path}" for path in manifests]]))


def backfill_provenance(dry_run: bool = True, batch: int = 50, limit: int | None = None) -> str:
    """Preview or export non-applied source patches; never restore live pages."""
    if batch < 1 or (limit is not None and limit < 1):
        raise ValueError("batch and limit must be positive")
    plan = plan_provenance_backfill()
    entries = plan["restorable"][:limit]
    lines = ["=== Provenance Backfill ===",
        f"pages with a no_source gap: {plan['no_source_pages']} ({plan['no_source_claims']} claims)",
        f"restorable by an exact rule: {len(plan['restorable'])} pages ({plan['restorable_claims']} claims)",
        f"ambiguous: {len(plan['ambiguous'])} pages | no match: {len(plan['unmatched'])} pages"]
    for entry in entries[:8]:
        lines.append(f"  {entry['page_key']} -> {entry['raw_path']}  [{entry['rule']}]")
    if dry_run:
        return "\n".join([*lines, f"[DRY RUN] {len(entries)} source patch candidate(s); no files or database rows changed."])
    prepared = 0
    manifests = []
    batch = min(batch, source_metadata.MAX_PAGES)
    try:
        for start in range(0, len(entries), batch):
            updates = []
            for entry in entries[start:start+batch]:
                raw = _page_snapshot(entry["page_key"])
                updates.append(_source_update(entry["page_key"], raw, [entry["raw_path"]]))
            result = source_metadata.prepare_source_metadata_batch(updates)
            prepared += result["patches_prepared"]
            manifests.append(result["manifest_path"])
    except Exception as exc:
        # Preserve the real cause; never turn a failure into empty/success.
        raise PatchPreparationError(str(exc), prepared, manifests) from exc
    return "\n".join([*lines,*[f"patch manifest: {path}" for path in manifests],
        f"[PATCH ONLY] prepared {prepared} source patch(es); restored 0 page(s).",
        "Automatic application unavailable: verified physical exclusion is not established."])


def revert_provenance_backfill(rollback_file: str, batch: int = 50, dry_run: bool = False) -> str:
    """Propose source-only reversal; stale inputs fail and disclose prior exports."""
    if batch < 1:
        raise ValueError("batch must be positive")
    path = Path(rollback_file)
    if not path.is_file():
        raise FileNotFoundError(f"No rollback file at {rollback_file}.")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Rollback input byte budget exceeded")
    records = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if len(line.encode("utf-8")) > 6*source_metadata.MAX_PAGE_BYTES+65536:
                raise ValueError("Rollback record byte budget exceeded")
            if line.strip():
                record = json.loads(line)
                required = {"page_key", "before", "after_sha256"}
                if not isinstance(record, dict) or not required.issubset(record) or any(not isinstance(record[key], str) for key in required):
                    raise PatchPreparationError("refused: rollback records require string page_key, before and after_sha256 fields", 0, [])
                if not record["page_key"] or len(record["page_key"]) > 4096 or len(record["before"].encode("utf-8")) > source_metadata.MAX_PAGE_BYTES or len(record["after_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in record["after_sha256"]):
                    raise PatchPreparationError("refused: rollback record fields exceed their shape/byte limits", 0, [])
                records.setdefault(record["page_key"], record)
    if dry_run:
        return f"[DRY RUN] {len(records)} source-reversal patch candidate(s); no application."
    entries = sorted(records.items())
    manifests = []
    prepared = 0
    batch = min(batch, source_metadata.MAX_PAGES)
    try:
        for start in range(0, len(entries), batch):
            updates = []
            for page_key, record in entries[start:start+batch]:
                current = _page_snapshot(page_key)
                if not record.get("after_sha256") or hashlib.sha256(current).hexdigest() != record["after_sha256"]:
                    raise ValueError("refused: legacy reversal requires its exact post-backfill file hash; no live changes")
                fm, _ = split_frontmatter(record["before"].removeprefix("\ufeff").replace("\r\n", "\n"))
                refs = fm.get("sources") or []
                refs = [refs] if isinstance(refs, str) else refs
                updates.append(_source_update(page_key, current, refs))
            result = source_metadata.prepare_source_metadata_batch(updates)
            prepared += result["patches_prepared"]
            manifests.append(result["manifest_path"])
    except Exception as exc:
        # Chaining retains YAML/I/O/programming errors and all earlier exports.
        raise PatchPreparationError(str(exc), prepared, manifests) from exc
    return "\n".join([f"[PATCH ONLY] prepared {prepared} reversal patch(es); reverted 0 page(s).",
        *[f"patch manifest: {path}" for path in manifests], "Automatic application unavailable without verified physical exclusion."])
