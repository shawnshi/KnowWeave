import hashlib
import json
import logging
import math
import time

from vector_lake.link_resolution import build_link_map, declared_names_from_nodes, resolve_link_target
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter

log = logging.getLogger("vector-lake-gc")


def _in_scope(node: dict, domain: str, cluster: str) -> bool:
    return (not domain or node.get("domain") == domain) and (
        not cluster or node.get("topic_cluster") == cluster
    )


def _snapshot():
    """Bind all inputs to the destructive decision, not just its selected victims."""
    from vector_lake import governance_store

    entities = governance_store.load_entities().get("items", {})
    claims = governance_store.load_claims().get("items", {})
    versions = governance_store.canonical_page_versions()
    files = {}
    wiki_dir = get_wiki_dir().resolve()
    for path in sorted(wiki_dir.glob("*.md")):
        if path.is_symlink() or not path.resolve().is_relative_to(wiki_dir):
            raise ValueError("Maintenance refuses symlinked wiki pages.")
        raw = path.read_bytes()
        files[path.stem] = (hashlib.sha256(raw).hexdigest(), path.stat().st_mtime_ns)
    state = json.dumps([entities, claims, versions, files], sort_keys=True, ensure_ascii=False, default=str)
    return entities, claims, versions, files, hashlib.sha256(state.encode("utf-8")).hexdigest()


def gc_vector_lake(days: int = 30, dry_run: bool = True, force: bool = False, *,
                   domain: str = "", cluster: str = "", expected_plan=None) -> str:
    """Preview returns ``Expected plan: <JSON>``; execution requires that unchanged plan."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days must be a nonnegative integer")
    if not isinstance(domain, str) or not isinstance(cluster, str):
        raise ValueError("domain and cluster must be strings")
    if not dry_run and expected_plan is None:
        return "GC aborted: expected_plan from a dry-run preview is required."
    if isinstance(expected_plan, str):
        try:
            expected_plan = json.loads(expected_plan)
        except json.JSONDecodeError as exc:
            raise ValueError("expected_plan must contain valid preview JSON") from exc
    if expected_plan is not None and not isinstance(expected_plan, dict):
        raise ValueError("expected_plan must be a preview object or its JSON string")
    cutoff = time.time() - days * 86400
    if expected_plan is not None:
        cutoff = expected_plan.get("cutoff")
        if isinstance(cutoff, bool) or not isinstance(cutoff, (float, int)) or not math.isfinite(cutoff):
            raise ValueError("expected_plan.cutoff must be finite")
        if cutoff > time.time() - days * 86400:
            return "GC aborted: stale or invalid preview plan."

    entities, claims, versions, files, state = _snapshot()
    nodes = {str(node.get("page_key") or entity_id): node for entity_id, node in entities.items()}
    identity = {str(entity_id): str(node.get("page_key") or entity_id) for entity_id, node in entities.items()}
    canonical_id_of = {key: entity_id for entity_id, key in identity.items()}
    # Connectivity is global and conservative; a scope must not manufacture an orphan
    # by hiding a real neighbour. Only candidate selection is scoped.
    degrees = _topological_degrees(nodes, identity, claims)
    wiki_dir = get_wiki_dir()
    orphans = []
    typed_pages = 0
    for key, node in sorted(nodes.items()):
        if not _in_scope(node, domain, cluster):
            continue
        if str(node.get("type") or "").lower() not in {"vendor", "product", "person", "event"}:
            continue
        typed_pages += 1
        if key not in files or key not in versions or degrees.get(key, 0) > 1:
            continue
        frontmatter, _ = split_frontmatter((wiki_dir / f"{key}.md").read_text(encoding="utf-8"))
        # Absence of an active marker is not disposal authority. Only explicit retired
        # records qualify; frontmatter additionally protects legacy core classification.
        if str(node.get("status", "")).lower() not in {"archived", "deprecated", "retracted"}:
            continue
        if str(frontmatter.get("status", "")).lower() == "active":
            continue
        if any(str(record.get("strategic_scope", "")).lower() == "core" for record in (node, frontmatter)):
            continue
        if files[key][1] / 1e9 < cutoff:
            orphans.append(key)

    plan = {"version": 1, "days": days, "domain": domain, "cluster": cluster,
            "cutoff": cutoff, "state": state, "pages": orphans}
    if expected_plan is not None and plan != expected_plan:
        return "GC aborted: stale preview plan; run dry-run again."
    if dry_run:
        if not orphans:
            lines = [f"[DRY-RUN] No orphan entities older than {days} days found."]
        else:
            lines = [f"[DRY-RUN] Found {len(orphans)} retired orphan entities older than {days} days "
                     f"(page-edge degree <= 1) out of {typed_pages} typed pages. "
                     "Re-run with dry_run=False and expected_plan to execute:"]
            for key in orphans[:20]:
                lines.append(f"  - {key}.md (ID: {canonical_id_of[key]}, degree: {degrees.get(key, 0)})")
        lines.append("Expected plan: " + json.dumps(plan, sort_keys=True, ensure_ascii=False))
        return "\n".join(lines)
    if not orphans:
        return f"GC complete. No orphan entities older than {days} days found."
    if not force and len(orphans) >= 5 and len(orphans) > typed_pages * 0.5:
        return f"GC aborted: {len(orphans)} of {typed_pages} typed pages exceeds the 50% safety threshold; inspect then use force=True."

    import shutil
    from vector_lake.mutation_coordinator import execute_mutation_batch
    backup_dir = wiki_dir.parent / "backup" / "gc" / _utc_stamp()

    def precondition():
        if _snapshot()[-1] != state:
            raise ValueError("stale preview plan; no pages deleted")
        # Native CAS runs in this same write transaction; backup is after every stale
        # check and before the first canonical delete. A failed backup aborts the batch.
        backup_dir.mkdir(parents=True, exist_ok=False)
        for key in orphans:
            shutil.copy2(wiki_dir / f"{key}.md", backup_dir / f"{key}.md")

    try:
        _, receipt = execute_mutation_batch(
            [{"filename": f"{key}.md", "is_delete": True, "expected_version": versions[key]} for key in orphans],
            canonical_precondition=precondition,
        )
    except (ValueError, OSError) as exc:
        return f"GC aborted: {exc}"
    # Ledger retention is not part of a page preview, and must not silently delete
    # unrelated audit rows (especially during scoped maintenance).
    return f"GC canonical commit. Deleted {len(orphans)} orphan pages (backed up to {backup_dir}). {receipt}"


def _topological_degrees(nodes: dict, identity: dict, claims: dict) -> dict:
    """Undirected neighbour count per page, from canonical relations.

    Sources of connectivity, all biased toward *not* declaring an orphan:
      * explicit ``links`` / ``outbound_links`` between pages,
      * pages sharing at least one raw source,
      * pages whose entities co-occur in the same claim.
    """
    keys = set(nodes)
    link_map, _, unique_cores, _ = build_link_map(nodes, declared_names_from_nodes(nodes))

    adjacency = {key: set() for key in keys}

    def _connect(left: str, right: str):
        if left != right and left in keys and right in keys:
            adjacency[left].add(right)
            adjacency[right].add(left)

    source_to_pages = {}
    for key, node in nodes.items():
        for link in [*(node.get("links") or []), *(node.get("outbound_links") or [])]:
            target = str(link).split("|", 1)[0].split("#", 1)[0].removesuffix(".md").strip()
            resolved = resolve_link_target(target, link_map, unique_cores)
            if resolved:
                _connect(key, resolved)
        for source in node.get("sources") or []:
            source_to_pages.setdefault(str(source), []).append(key)
    for pages in source_to_pages.values():
        for index, left in enumerate(pages):
            for right in pages[index + 1:]:
                _connect(left, right)

    claim_to_pages = {}
    for claim in claims.values():
        pages = {identity[entity_id] for entity_id in claim.get("subject_entity_ids") or [] if entity_id in identity}
        for page in pages:
            claim_to_pages.setdefault(page, set()).update(pages - {page})
    for page, peers in claim_to_pages.items():
        for peer in peers:
            _connect(page, peer)

    return {key: len(neighbours) for key, neighbours in adjacency.items()}


def _utc_stamp() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _prune_change_sets(cutoff: float, backup_dir) -> str:
    """Prune the change-set ledger after rotating a copy of the affected rows."""
    import datetime

    from vector_lake.db_store import get_connection, transaction

    cutoff_dt = datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc).isoformat()
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            "SELECT change_set_id, data_json FROM change_sets WHERE updated_at < ?", (cutoff_dt,)
        ).fetchall()
        if not rows:
            return ""
        ledger_backup = backup_dir / f"change_sets_before_{cutoff_dt[:10]}.jsonl"
        try:
            with open(ledger_backup, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            {"change_set_id": row["change_set_id"], "data_json": row["data_json"]},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError as exc:
            log.error(f"Change-set prune aborted; could not rotate ledger backup: {exc}")
            return f" Change-set prune skipped (backup failed: {exc})."
        conn.execute("DELETE FROM change_sets WHERE updated_at < ?", (cutoff_dt,))
    log.info(f"Database GC: pruned {len(rows)} change_sets older than {cutoff_dt}; backup={ledger_backup}")
    return f" Pruned {len(rows)} change_sets older than {cutoff_dt[:10]} (ledger backup: {ledger_backup.name})."
