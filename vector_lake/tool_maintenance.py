"""Operator-facing wrappers for the two bounded repair paths.

``backup_retention.prune_backups`` and ``db_store.repair_idempotency_keys`` both
answer "what would change" before they change anything, and neither acts until it
is told to.  These wrappers keep that contract at the surface: the CLI's
``--apply`` and the MCP tool's ``dry_run`` flag are the only route to the mutating
branch, and the returned text always states whether anything actually changed.

They live here rather than inside ``backup_retention`` / ``db_store`` so the repair
logic stays free of presentation, and rather than inside ``cli_app`` /
``mcp_server`` so both surfaces report identically.
"""

from __future__ import annotations

import json
from pathlib import Path

from vector_lake import db_store
from vector_lake.backup_retention import (
    prune_backups,
)
from vector_lake.db_store import IDEMPOTENCY_TABLES
from vector_lake.link_resolution import (
    build_link_map,
    declared_names_from_nodes,
    resolve_link_target,
)


def _gib(value: int) -> str:
    return f"{value / 1024 ** 3:.2f} GiB"


def _backup_root() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "backups"


def _bound(value: int) -> int | None:
    """``0`` (or less) means "use the configured default" for either bound."""
    return int(value) if value and int(value) > 0 else None


def backup_retention_report(keep: int = 0, max_bytes: int = 0, dry_run: bool = True) -> str:
    """Report, and only if asked enforce, the bound on ``.meta/backups``.

    Args:
        keep: Number of newest copies to retain. 0 uses the configured default.
        max_bytes: Byte budget for the older copies. 0 uses the configured default.
        dry_run: When True (the default) nothing is removed.
    """
    result = prune_backups(
        _backup_root(),
        keep_count=_bound(keep),
        max_bytes=_bound(max_bytes),
        dry_run=dry_run,
    )

    entries = result["keep"] + result["remove"]
    width = max((len(entry["name"]) for entry in entries), default=0)
    lines = [
        "=== Backup Retention ===",
        f"Root: {result['root']}",
        (
            f"Entries: {result['entry_count']}  total {_gib(result['total_bytes'])}  "
            f"retained {_gib(result['retained_bytes'])}  "
            f"removable {_gib(result['removable_bytes'])}"
        ),
        (
            f"Bound: keep <= {result['keep_count']} copy/copies "
            f"and <= {_gib(result['max_bytes'])}"
        ),
    ]
    for entry in result["keep"]:
        lines.append(
            f"  KEEP    {entry['name']:<{width}}  {entry['kind']:<9} {_gib(entry['bytes'])}"
        )
    for entry in result["remove"]:
        lines.append(
            f"  REMOVE  {entry['name']:<{width}}  {entry['kind']:<9} {_gib(entry['bytes'])}"
        )
    if result["unrecognized"]:
        lines.append(
            "  unrecognised, never pruned: " + ", ".join(result["unrecognized"])
        )

    for entry in result["protected"]:
        lines.append(f"  PROTECTED  {entry['name']}: {entry['reason']}")

    if not result["remove"]:
        lines.append("Nothing to remove: no verified backup exceeds the bound.")
    elif result["dry_run"]:
        lines.append(
            f"[DRY RUN] {len(result['remove'])} entr(ies) would be removed, freeing "
            f"{_gib(result['removable_bytes'])}. Re-run with apply to remove them."
        )
    else:
        lines.append(
            f"[APPLIED] Removed {len(result['deleted'])} entr(ies), freeing "
            f"{_gib(result['deleted_bytes'])} in completed units. The newest verified copy is always kept."
        )
        for name in result["deleted"]:
            lines.append(f"  deleted: {name}")
    for failure in result["failures"]:
        lines.append(f"  refused: {failure}")
    return "\n".join(lines)


def idempotency_index_report() -> str:
    """Report the uniqueness guarantee each idempotency table actually has."""
    state = db_store.idempotency_index_state()
    lines = ["=== Idempotency Index ==="]
    for table, info in sorted(state.items()):
        lines.append(
            f"  {table}: uniqueness={info['uniqueness']} "
            f"duplicate_groups={info['duplicate_groups']}"
        )

    degraded = {
        table: info for table, info in state.items() if info["uniqueness"] != "full"
    }
    if not degraded:
        lines.append(
            "Every key is unique forever on both tables; a key can never be reused "
            "after its row reaches a terminal status."
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("full   = a key may never repeat.")
    lines.append(
        "active = the table already held duplicate history, so uniqueness holds over "
        "non-terminal rows only -- exactly the population a concurrent enqueue collides in."
    )
    lines.append(
        "absent = no unique index could be created; only the BEGIN IMMEDIATE write lock "
        "protects enqueue."
    )
    lines.append("")
    for table, info in sorted(degraded.items()):
        lines.append(
            f"{table}: {info['duplicate_groups']} duplicated idempotency_key group(s) "
            f"written by an earlier release."
        )
        lines.append(
            f"  inspect: cli.py repair-idempotency --table {table}   "
            f"(then add --apply to reclaim the full index)"
        )
    return "\n".join(lines)


def repair_idempotency_keys(table: str = "mutation_outbox", dry_run: bool = True) -> str:
    """Clear the redundant idempotency keys so the full unique index can be created.

    Only the duplicate *key* is cleared.  The row, its status, timestamps, error text
    and ``superseded_by`` link are all kept, so no audit history is lost.  The
    canonical row for a key is the one ``enqueue_mutation`` / ``enqueue_job`` already
    returns for it: the lowest ``id``.

    Args:
        table: ``mutation_outbox`` or ``jobs``.
        dry_run: When True (the default) no row is changed.
    """
    if table not in IDEMPOTENCY_TABLES:
        return (
            f"Error: unknown idempotency table '{table}'. "
            f"Expected one of: {', '.join(IDEMPOTENCY_TABLES)}."
        )
    result = db_store.repair_idempotency_keys(table, dry_run=dry_run)

    lines = [f"=== Idempotency Repair: {result['table']} ==="]
    lines.append(
        f"Duplicate groups: {result['duplicate_groups_before']}  "
        f"redundant rows: {result['redundant_rows']}  "
        f"uniqueness: {result['uniqueness_before']}"
    )
    if result["redundant_keys"]:
        lines.append(
            "Each group keeps the key on its canonical (lowest) row; the other "
            f"{result['redundant_rows']} row(s) lose only the key and are not deleted."
        )
        shown = ", ".join(str(key) for key in result["redundant_keys"][:50])
        lines.append(f"  redundant rows: {shown}")
        if len(result["redundant_keys"]) > 50:
            lines.append(f"  ... and {len(result['redundant_keys']) - 50} more.")

    if result["dry_run"]:
        if result["redundant_rows"]:
            lines.append(
                "[DRY RUN] No row was changed. Re-run with apply to clear the redundant "
                "keys and reclaim the full unique index."
            )
        else:
            lines.append("Nothing to repair: no duplicated idempotency_key groups.")
        return "\n".join(lines)

    lines.append("[APPLIED] No rows deleted.")
    lines.append(
        f"Duplicate groups: {result['duplicate_groups_before']} -> "
        f"{result.get('duplicate_groups_after', 0)}  "
        f"uniqueness: {result['uniqueness_before']} -> "
        f"{result.get('uniqueness_after', result['uniqueness_before'])}"
    )
    if result.get("uniqueness_after") != "full":
        lines.append(
            "The full unique index is still unavailable: a duplicate is in a non-terminal "
            "status. Resolve that row, then re-run cli.py idempotency-status."
        )
    return "\n".join(lines)


# --- claim pointer reconciliation -------------------------------------------
#
# A claim can be retired outside the delta path -- a bulk pass that deletes rows, or an
# operator's own SQL -- and the tables that *point at* claims then keep the dead pointer.
# Two such surfaces exist and neither had a reader that would notice:
#
#   * ``evidence.supports_claim_ids`` / ``contradicts_claim_ids`` name claim ids.  Measured on
#     the live corpus: 25 220 of 84 606 pointers name a claim that no longer exists, spread over
#     25 217 rows, and 25 214 of them were created in one bulk event on 2026-07-14 -- the count
#     has not moved since (25 221 at the 2026-09-22 snapshot).
#   * ``claim_graph_edges`` stores page keys.  The delta's delete filtered this table by *claim*
#     ids, which matched nothing there, so every rewritten page kept its retired links.
#
# The timeline projection has had a parity gate and a repair since it broke; these two did not.
# Both entry points are read-only until ``--apply``.

_ROLLBACK_TAG = "2026-09-24-claim-pointer-prune"


def _rollback_path() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    directory = get_meta_dir() / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_ROLLBACK_TAG}.rollback.jsonl"


def _dangling_pointer_rows(conn) -> list[dict]:
    """Evidence rows naming a claim id that does not exist, with the dead ids they name."""
    claim_ids = {str(row[0]) for row in conn.execute("SELECT claim_id FROM claims")}
    found: list[dict] = []
    for row in conn.execute("SELECT evidence_id, data_json FROM evidence"):
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            # A row whose payload does not decode is a different defect; naming its pointers
            # dead would be a guess, so it is reported by the count and left alone.
            continue
        supports = _pointer_id_list(data, "supports_claim_ids")
        contradicts = _pointer_id_list(data, "contradicts_claim_ids")
        dead_supports = [item for item in supports if item not in claim_ids]
        dead_contradicts = [item for item in contradicts if item not in claim_ids]
        if dead_supports or dead_contradicts:
            found.append({
                "evidence_id": str(row["evidence_id"]),
                "dead_supports": dead_supports,
                "dead_contradicts": dead_contradicts,
            })
    return found


def _edge_key_resolver(conn):
    """``key -> page key`` using the one owner for link resolution (``link_resolution``).

    The same maps the indexer and the linter build, from the same inputs: the page keys the
    node projection holds, and the names those pages declare.  Building a second answer here
    would recreate the defect the module exists to remove.
    """
    nodes = {}
    for row in conn.execute("SELECT node_key, node_json FROM page_index_nodes"):
        try:
            payload = json.loads(row["node_json"])
        except (TypeError, ValueError):
            payload = {}
        nodes[str(row["node_key"])] = {
            "title": payload.get("title"),
            "aliases": payload.get("aliases") or [],
        }
    declared = declared_names_from_nodes(nodes)
    link_map, _core_pages, unique_cores, _contested = build_link_map(nodes.keys(), declared)

    def resolve(key: str) -> str | None:
        return resolve_link_target(str(key), link_map, unique_cores)

    return nodes, resolve


def _dead_edge_rows(conn, resolve, pages: set[str] | None = None) -> list[dict]:
    """Edge rows with an endpoint that neither is a page key nor resolves to one."""
    if pages is None:
        pages = {str(row[0]) for row in conn.execute("SELECT DISTINCT node_key FROM page_index_nodes")}
    found: list[dict] = []
    for row in conn.execute("SELECT source_id, target_id, relation FROM claim_graph_edges"):
        source, target = str(row["source_id"]), str(row["target_id"])
        if source in pages and target in pages:
            continue
        found.append({
            "source_id": source,
            "target_id": target,
            "relation": str(row["relation"]),
            "resolved_source": None if source in pages else resolve(source),
            "resolved_target": None if target in pages else resolve(target),
        })
    return found


# The pointer counts a *report* needs are expressible in SQL over ``json_each``, which keeps
# the per-row JSON decode in SQLite's C instead of Python: measured on the live corpus
# (84 556 evidence rows), 0.68 s of Python ``json.loads`` becomes 0.44 s of SQL.  The repair
# still reads the rows, because it needs the ids, not just the count.  Both sides apply the
# same two guards -- a payload that is not valid JSON, and a field that is not an array -- so
# a malformed row can never make the report and the repair disagree.
_POINTER_COUNT_SQL = """
WITH dangling AS (
    SELECT
        json_type(json_extract(e.data_json, '$.supports_claim_ids')) AS supports_type,
        json_type(json_extract(e.data_json, '$.contradicts_claim_ids')) AS contradicts_type,
        e.data_json
    FROM evidence e
    WHERE json_valid(e.data_json)
)
SELECT
    COALESCE(SUM((SELECT COUNT(*) FROM json_each(d.data_json, '$.supports_claim_ids') v
                  WHERE v.value NOT IN (SELECT claim_id FROM claims))), 0),
    COALESCE(SUM((SELECT COUNT(*) FROM json_each(d.data_json, '$.contradicts_claim_ids') v
                  WHERE v.value NOT IN (SELECT claim_id FROM claims))), 0),
    COALESCE(SUM(CASE WHEN EXISTS (
                  SELECT 1 FROM json_each(d.data_json, '$.supports_claim_ids') v
                  WHERE v.value NOT IN (SELECT claim_id FROM claims))
             OR EXISTS (
                  SELECT 1 FROM json_each(d.data_json, '$.contradicts_claim_ids') v
                  WHERE v.value NOT IN (SELECT claim_id FROM claims))
             THEN 1 ELSE 0 END), 0)
FROM dangling d
WHERE d.supports_type IN ('array', 'null') AND d.contradicts_type IN ('array', 'null')
"""


# Measured on the live corpus (84 556 evidence rows, warm): the Python row scan costs 0.72 s and
# the ``json_each`` SQL form 0.86-1.00 s, so the scan stays in Python.  The count does not
# short-circuit the way a bare ``EXISTS`` does -- it visits every pointer -- and SQLite pays a
# correlated lookup per pointer that the Python set membership does not.  A ``NOT IN`` -> the
# ``claims`` primary key costs 0.71 s on its own over ``operational_memory``'s 69 716 rows, which
# is the other half of the probe; both halves are why this sits behind ``deep_projection_checks``.


def _pointer_id_list(data: dict, key: str) -> list[str]:
    """The claim ids a payload lists under ``key``; anything that is not a list is not a list.

    ``json_each`` on a scalar yields one row and iterating a Python string yields characters,
    so without this a payload holding a bare string would be counted two different ways.
    """
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def claim_projection_drift(prepare: bool = True) -> dict:
    """Read-only counts for every surface that points at a claim.

    ``prepare=False`` skips ``init_db`` so a health probe can call it without taking a write
    lock; the caller's process has already migrated the database it is inspecting.

    The link resolver is built only when an edge row actually has an endpoint outside the page
    keys: it has to parse every ``node_json`` (0.17 s live), and a corpus whose edges all point
    at real pages -- which is the state this repair converges to -- never needs it.
    """
    if prepare:
        db_store.init_db()
    conn = db_store.get_connection()
    pointers = _dangling_pointer_rows(conn)
    counts = {
        "evidence_rows": len(pointers),
        "dead_pointers": sum(len(row["dead_supports"]) + len(row["dead_contradicts"]) for row in pointers),
    }
    pages = {str(row[0]) for row in conn.execute("SELECT DISTINCT node_key FROM page_index_nodes")}
    candidates = [
        (str(row["source_id"]), str(row["target_id"]))
        for row in conn.execute("SELECT source_id, target_id FROM claim_graph_edges")
        if str(row["source_id"]) not in pages or str(row["target_id"]) not in pages
    ]
    if candidates:
        _nodes, resolve = _edge_key_resolver(conn)
        edges = _dead_edge_rows(conn, resolve, pages=pages)
    else:
        edges = []
    # Only the *target* is re-keyable.  A source is the page the edge was extracted from, so a
    # source that is not a page key is a retired page, not a link written by core name --
    # re-pointing it through the core rule would move an edge to a different page's provenance.
    rekeyable = [
        row for row in edges
        if row["resolved_target"] and row["resolved_target"] != row["target_id"]
    ]
    memory_rows = conn.execute(
        "SELECT COUNT(*) FROM operational_memory WHERE f_source_claim_id IS NOT NULL "
        "AND f_source_claim_id != '' AND f_source_claim_id NOT IN (SELECT claim_id FROM claims)"
    ).fetchone()[0]
    return {
        "evidence_rows": counts["evidence_rows"],
        "dead_pointers": counts["dead_pointers"],
        "edge_rows": len(edges),
        "rekeyable_edges": len(rekeyable),
        "memory_rows": int(memory_rows),
        "sample_edges": edges[:10],
    }


def claim_projection_drift_report() -> str:
    """Read-only: how many pointers each claim-derived surface still holds onto dead claims."""
    drift = claim_projection_drift()

    lines = ["=== Claim Pointer Drift ==="]
    lines.append(
        f"evidence rows naming a missing claim: {drift['evidence_rows']}  "
        f"dead pointer(s): {drift['dead_pointers']}"
    )
    lines.append(
        f"claim_graph_edges rows with an unresolved endpoint: {drift['edge_rows']}  "
        f"of which re-keyable by target: {drift['rekeyable_edges']}"
    )
    lines.append(f"operational_memory rows naming a missing claim: {drift['memory_rows']}")
    lines.append(
        "Repair: cli.py claim-pointer-repair --apply (prunes the evidence pointers), "
        "cli.py claim-pointer-repair --apply --edges (re-keys the resolvable edge endpoints)."
    )
    for row in drift["sample_edges"]:
        lines.append(
            f"  edge {row['source_id']} -> {row['target_id']} [{row['relation']}] "
            f"resolves to {row['resolved_source'] or row['source_id']} -> "
            f"{row['resolved_target'] or row['target_id']}"
        )
    return "\n".join(lines)


def repair_claim_pointers(dry_run: bool = True, batch: int = 2000, edges: bool = False) -> str:
    """Prune pointers that name a claim nobody holds; optionally re-key resolvable edges.

    The evidence prune only removes ids from ``supports_claim_ids`` /
    ``contradicts_claim_ids``.  The row, its text, its locator, its source and its
    ``updated_at`` are untouched: dropping a pointer is not new evidence, so it must not move
    the freshness clock.  Every removed id is written to a rollback file first, and the write
    is batched so a long corpus cannot hold the write lock for one long transaction.

    ``edges=True`` additionally re-keys the ``claim_graph_edges`` rows whose endpoint the
    resolver can answer (``[[Concept_CoMET]]`` where ``Product_CoMET`` is the page) and leaves
    the rest: an endpoint no page answers is the shape the edge writer keeps on purpose, and
    deleting it would discard a link the page really declares.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    pointers = _dangling_pointer_rows(conn)
    dead_pointers = sum(len(row["dead_supports"]) + len(row["dead_contradicts"]) for row in pointers)
    _nodes, resolve = _edge_key_resolver(conn)
    edge_rows = _dead_edge_rows(conn, resolve) if edges else []
    rekeyable = [
        row for row in edge_rows
        if row["resolved_target"] and row["resolved_target"] != row["target_id"]
    ]

    if not pointers and not rekeyable:
        return "Nothing to repair: no pointer on a claim-derived surface names a missing claim."
    if dry_run:
        return (
            f"[DRY RUN] Would prune {dead_pointers} dead claim pointer(s) from "
            f"{len(pointers)} evidence row(s)"
            + (f" and re-key {len(rekeyable)} edge row(s)" if edges else "")
            + f". Rollback file: {_rollback_path()}"
        )

    from datetime import datetime, timezone

    rollback = _rollback_path()
    written = 0
    with rollback.open("a", encoding="utf-8") as sink:
        for start in range(0, len(pointers), batch):
            chunk = pointers[start : start + batch]
            with db_store.transaction():
                for row in chunk:
                    stored = conn.execute(
                        "SELECT data_json FROM evidence WHERE evidence_id = ?",
                        (row["evidence_id"],),
                    ).fetchone()
                    if stored is None:
                        continue
                    payload = json.loads(stored["data_json"])
                    dead_supports = set(row["dead_supports"])
                    dead_contradicts = set(row["dead_contradicts"])
                    payload["supports_claim_ids"] = [
                        item for item in (payload.get("supports_claim_ids") or [])
                        if str(item) not in dead_supports
                    ]
                    payload["contradicts_claim_ids"] = [
                        item for item in (payload.get("contradicts_claim_ids") or [])
                        if str(item) not in dead_contradicts
                    ]
                    conn.execute(
                        "UPDATE evidence SET data_json = ? WHERE evidence_id = ?",
                        (json.dumps(payload, ensure_ascii=False), row["evidence_id"]),
                    )
                    sink.write(json.dumps({
                        "evidence_id": row["evidence_id"],
                        "supports_claim_ids": sorted(dead_supports),
                        "contradicts_claim_ids": sorted(dead_contradicts),
                    }, ensure_ascii=False) + "\n")
                    written += 1

    rekeyed = 0
    if rekeyable:
        now = datetime.now(timezone.utc).isoformat()
        with rollback.open("a", encoding="utf-8") as sink, db_store.transaction():
            for row in rekeyable:
                source = row["source_id"]
                target = row["resolved_target"]
                conn.execute(
                    "DELETE FROM claim_graph_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
                    (row["source_id"], row["target_id"], row["relation"]),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO claim_graph_edges "
                    "(source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (source, target, row["relation"], 1.0, now),
                )
                sink.write(json.dumps({
                    "edge": [row["source_id"], row["target_id"], row["relation"]],
                    "rekeyed_to": [source, target, row["relation"]],
                }, ensure_ascii=False) + "\n")
                rekeyed += 1

    return (
        f"Pruned {dead_pointers} dead claim pointer(s) from {written} evidence row(s)"
        + (f"; re-keyed {rekeyed} edge row(s)" if edges else "")
        + f". Rollback file: {rollback}"
    )


def outbox_recovery(
    *,
    retry: bool = False,
    clear_failed: bool = False,
    filename: str = "",
    dry_run: bool = True,
) -> str:
    """Inspect, retry, or cancel failed mutation_outbox rows."""
    if retry and clear_failed:
        raise ValueError("Specify at most one of --retry or --clear-failed.")
    target = filename.strip() or None
    rows = db_store.list_failed_outbox_rows(target)
    if not rows:
        return "No failed mutation_outbox rows found."
    lines = [f"Failed mutation_outbox rows ({len(rows)}):"]
    for row in rows[:50]:
        err = (row.get("last_error") or "").replace("\n", " ")[:120]
        lines.append(f"  id={row['id']} file={row['filename']} type={row['mutation_type']} attempts={row['attempt_count']} error={err}")
    if len(rows) > 50:
        lines.append(f"  ... ({len(rows) - 50} more)")
    if dry_run or (not retry and not clear_failed):
        lines.append("(Dry run — pass --retry --apply or --clear-failed --apply to mutate.)")
        return "\n".join(lines)
    if retry:
        changed = db_store.retry_failed_outbox_rows(target)
        return "\n".join([*lines, f"Re-queued {changed} failed outbox row(s) to pending."])
    changed = db_store.clear_failed_outbox_rows(target)
    return "\n".join([*lines, f"Cancelled {changed} failed outbox row(s)."])
