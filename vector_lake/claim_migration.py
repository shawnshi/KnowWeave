"""Explicit, page-scoped Claim migration; preview is read-only and apply requires a backup."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from vector_lake.claim_extractor import extract_page_objects
from vector_lake.governance_store import (
    CLAIM_RELATION_FIELDS,
    _canonical_entity_rows_version,
    _claim_relation_targets,
    _validate_claim_relation_delta,
)
from vector_lake.wiki_utils import split_frontmatter


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _page_path(root: Path, filename: str) -> Path:
    if Path(filename).name != filename or not filename.endswith(".md"):
        raise ValueError("Migration accepts explicit Markdown basenames only")
    wiki = (root / "wiki").resolve()
    path = (wiki / filename).resolve()
    if path.parent != wiki:
        raise ValueError("Migration page escapes the specified wiki root")
    return path


def _snapshot(conn, keys: list[str]) -> str:
    parameter = json.dumps(keys)
    rows = []
    for table, key in (("entities", "entity_id"), ("claims", "claim_id")):
        rows.extend(
            (table, *tuple(row)) for row in conn.execute(
                f"SELECT {key}, data_json FROM {table} "
                "WHERE f_page_key IN (SELECT value FROM json_each(?)) ORDER BY 1",
                (parameter,),
            )
        )
    return _digest(rows)


def plan_claim_migration(memory_root: str | Path, filenames: list[str]) -> dict:
    """Inspect only explicitly selected pages; never initialize a database or write a plan."""
    root = Path(memory_root).resolve()
    names = sorted(set(filenames))
    if not names:
        raise ValueError("At least one explicit page is required")
    paths = {name: _page_path(root, name) for name in names}
    database = root / "wiki" / ".meta" / "vector_lake.db"
    if not database.is_file():
        raise FileNotFoundError(database)
    keys = [Path(name).stem for name in names]
    pages = []
    proposed = []
    old_ids = set()
    legacy_records = []
    mappings: dict[str, set[str]] = {}
    blockers = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        token = _snapshot(conn, keys)
        for name, path in paths.items():
            content = path.read_text(encoding="utf-8-sig", errors="strict")
            fm, body = split_frontmatter(content)
            extracted = extract_page_objects(name, fm, body, resolve_target=lambda target: target)
            entity_rows = conn.execute(
                "SELECT entity_id, data_json FROM entities WHERE f_page_key = ? ORDER BY entity_id",
                (Path(name).stem,),
            ).fetchall()
            if not entity_rows or not extracted["entities"]:
                blockers.append(f"{name}: missing canonical entity or non-knowledge page")
            if any(json.loads(row["data_json"]).get("raw_text") != body for row in entity_rows):
                blockers.append(f"{name}: wiki content differs from canonical; reconcile it before migration")
            old = [json.loads(row[0]) for row in conn.execute(
                "SELECT data_json FROM claims WHERE f_page_key = ?", (Path(name).stem,),
            )]
            old_ids.update(item["claim_id"] for item in old)
            legacy_records.extend((name, item) for item in old)
            new = extracted["claims"]
            proposed.extend(new)
            for item in new:
                for legacy in [item["claim_id"], *item.get("legacy_claim_ids", [])]:
                    mappings.setdefault(legacy, set()).add(item["claim_id"])
            for item in old:
                candidates = mappings.get(item["claim_id"], set())
                if not candidates:
                    blockers.append(f"{name}: old claim {item['claim_id']} has no successor")
                if len(candidates) > 1:
                    blockers.append(f"{name}: ambiguous truncated identity {item['claim_id']}")
            pages.append({
                "filename": name,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "expected_version": _canonical_entity_rows_version([(row[0], row[1]) for row in entity_rows]),
                "old_claim_count": len(old),
                "new_claim_count": len(new),
            })
        try:
            _validate_claim_relation_delta(proposed, old_ids, set(keys), connection=conn)
        except ValueError as exc:
            blockers.append(str(exc))
        successors = {record["claim_id"]: record for record in proposed}
        def mapped(target):
            candidates = mappings.get(target, {target})
            return next(iter(candidates)) if len(candidates) == 1 else target
        for name, old in legacy_records:
            candidates = mappings.get(old["claim_id"], set())
            if len(candidates) != 1:
                continue
            successor = successors[next(iter(candidates))]
            for field in CLAIM_RELATION_FIELDS:
                previous = {mapped(target) for target in _claim_relation_targets(old, field)}
                declared = {mapped(target) for target in _claim_relation_targets(successor, field)}
                if not previous <= declared:
                    blockers.append(f"{name}: {old['claim_id']} {field} is absent from its successor; declare it in the page first")
    plan = {
        "contract": "claim-migration-v1", "memory_root": str(root),
        "canonical_snapshot": token, "pages": pages,
        "identity_map": {old: sorted(new) for old, new in sorted(mappings.items()) if old in old_ids},
        "blockers": sorted(set(blockers)),
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def apply_claim_migration(plan: dict) -> dict:
    """Apply an unchanged preview through canonical mutations after an integrity-checked backup."""
    from vector_lake import db_store
    from vector_lake.mutation_coordinator import execute_mutation_batch
    from vector_lake.wiki_utils import get_memory_dir

    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("contract") != "claim-migration-v1" or plan.get("plan_sha256") != _digest(unsigned):
        raise ValueError("Invalid or modified migration plan")
    root = Path(plan["memory_root"]).resolve()
    if get_memory_dir().resolve() != root:
        raise ValueError("Explicit migration root must match VECTOR_LAKE_MEMORY_DIR")
    if plan["blockers"]:
        raise ValueError("Migration has unresolved blockers")
    names = [page["filename"] for page in plan["pages"]]
    fresh = plan_claim_migration(root, names)
    if fresh["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("Migration preview is stale")
    keys = [Path(name).stem for name in names]
    backup = root / "wiki" / ".meta" / "backups" / f"claim-migration-{uuid.uuid4().hex}.db.bak"
    db_store.backup_database(backup)
    mutations = []
    for page in plan["pages"]:
        content = _page_path(root, page["filename"]).read_text(encoding="utf-8-sig", errors="strict")
        mutations.append({"filename": page["filename"], "content": content, "expected_version": page["expected_version"]})

    def precondition():
        if _snapshot(db_store.get_connection(), keys) != plan["canonical_snapshot"]:
            raise ValueError("Canonical claims changed since migration preview")
        for mutation, page in zip(mutations, plan["pages"]):
            content = _page_path(root, page["filename"]).read_text(encoding="utf-8-sig", errors="strict")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != page["content_sha256"]:
                raise ValueError("Wiki changed since migration preview")

    ok, message = execute_mutation_batch(mutations, validation_mode="schema", canonical_precondition=precondition)
    if not ok:
        raise RuntimeError(message)
    return {"canonical_committed": True, "backup_path": str(backup), "page_count": len(names), "message": message}
