import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import (
    canonical_source_name,
    get_memory_dir,
    get_wiki_dir,
    normalize_sources,
    read_markdown_file,
)
from vector_lake.yaml_utils import dump_yaml


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-delete")


def delete_source(raw_path: str, dry_run: bool = True) -> str:
    wiki_dir = Path(get_wiki_dir()).resolve()
    memory_dir = Path(get_memory_dir()).resolve()
    raw_memory_dir = memory_dir / "raw"
    
    raw_path_obj = Path(raw_path).resolve()
    if not raw_path_obj.is_relative_to(raw_memory_dir):
        return f"[Security Error] The target '{raw_path}' is not within {raw_memory_dir}. Only raw sources can be deleted this way."

    raw_basename = raw_path_obj.name
    try:
        raw_ref = str(raw_path_obj.relative_to(memory_dir)).replace("\\", "/")
    except ValueError:
        raw_ref = str(raw_path_obj).replace("\\", "/")

    raw_ref_lower = raw_ref.lower()
    raw_basename_lower = raw_basename.lower()
    # Exact source-reference matching.  Substring matching removed the reference
    # from unrelated pages whose source list merely *contained* the basename, and
    # a prefix match on the page name also caught Source_<stem><suffix>.md.
    # The shared rule, lowered because the comparison below is case-insensitive.  Hand-rolling it
    # as ``source_<stem>.md`` missed every page whose name the sanitiser had to change (a stem with
    # a dot or a space), so the guard fell back to the ``sources:`` declaration for those.
    canonical_source_page = canonical_source_name(raw_path_obj).lower()

    def _references_raw_source(source: str) -> bool:
        normalized = normalize_sources([source])
        if not normalized:
            return False
        candidate = normalized[0].lower()
        return candidate == raw_ref_lower

    if not wiki_dir.exists():
        return "Wiki directory not found."

    actions = []
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            continue

        filepath = os.path.join(wiki_dir, filename)
        try:
            frontmatter, body, _ = read_markdown_file(filepath)
        except Exception:
            continue

        sources = normalize_sources(frontmatter.get("sources", []))
        is_source_page = filename.lower() == canonical_source_page
        has_source_ref = any(_references_raw_source(source) for source in sources)
        if not (is_source_page or has_source_ref):
            continue

        if len(sources) <= 1 or is_source_page:
            actions.append(("DELETE", filepath, filename, None, None))
        else:
            new_sources = [source for source in sources if not _references_raw_source(source)]
            frontmatter["sources"] = new_sources
            actions.append(("REMOVE_REF", filepath, f"{filename}: {len(sources)}→{len(new_sources)} sources", frontmatter, body))

    lines = [f"[CASCADE DELETE] Requested raw source: {raw_path}"]
    if os.path.exists(raw_path):
        lines.append(f"  [DELETE_RAW] {raw_path}")
    else:
        lines.append(f"  [MISSING_RAW] {raw_path}")

    if actions:
        lines.append(f"  [WIKI] {len(actions)} affected wiki page(s):")
        for action, _, detail, _, _ in actions:
            lines.append(f"    [{action}] {detail}")
    else:
        lines.append("  [WIKI] No related wiki pages found.")

    if dry_run:
        lines.append("")
        lines.append("(Dry run — no changes made. Re-run with dry_run=False to execute.)")
        return "\n".join(lines)

    deleted = 0
    updated = 0
    failures = []
    backup_dir = None

    # A cascade delete unlinks Markdown with no undo.  Keep a recovery point for
    # every page this run removes, as `tool_gc` already does for its deletes.
    deletions = [filepath for action, filepath, _, _, _ in actions if action == "DELETE"]
    if deletions:
        import shutil

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup_dir = memory_dir / "backup" / "delete-source" / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        for filepath in deletions:
            try:
                shutil.copy2(filepath, backup_dir / os.path.basename(filepath))
            except OSError as exc:
                return (
                    f"Aborted: could not create a recovery copy of {os.path.basename(filepath)} "
                    f"in {backup_dir} ({exc}). No changes were made."
                )

    from vector_lake.mutation_coordinator import execute_mutation_batch

    mutations = []
    for action, filepath, _, frontmatter, body in actions:
        filename = os.path.basename(filepath)
        if action == "DELETE":
            mutations.append({"filename": filename, "is_delete": True})
            deleted += 1
        elif action == "REMOVE_REF":
            fm_str = dump_yaml(frontmatter, allow_unicode=True, sort_keys=False)
            mutations.append({"filename": filename, "content": f"---\n{fm_str}---\n{body}"})
            updated += 1

    if mutations:
        try:
            execute_mutation_batch(mutations)
        except Exception as exc:
            failures.append(f"ATOMIC_WIKI_CLEANUP: {exc}")
            deleted = 0
            updated = 0
            log.warning("Atomic wiki cleanup failed: %s", exc)

    raw_deleted = False
    if failures:
        log.warning("Skipping raw source deletion because wiki cleanup had failures.")
    elif os.path.exists(raw_path):
        try:
            os.remove(raw_path)
            raw_deleted = True
            log.info(f"Deleted raw source: {raw_path}")
        except Exception as e:
            log.warning(f"Failed to delete raw source {raw_path}: {e}")

    if raw_deleted:
        _forget_processed_file(raw_path_obj)

    lines.append("")
    lines.append(f"Executed: raw_deleted={raw_deleted}, wiki_deleted={deleted}, wiki_updated={updated}. Projection updates queued transactionally.")
    if backup_dir is not None:
        lines.append(f"Recovery point: {backup_dir}")
    if failures:
        lines.append("Warnings:")
        for failure in failures:
            lines.append(f"  {failure}")
        lines.append("Raw source was preserved because wiki cleanup did not complete successfully.")
    return "\n".join(lines)


def _forget_processed_file(raw_path_obj: Path) -> None:
    """Drop processed_files rows for a deleted raw source.

    Leaving the row behind makes a later re-added source with the same content
    hash look already processed, so it is never ingested again.
    """
    from vector_lake.db_store import get_connection, init_db, transaction

    init_db()
    conn = get_connection()
    target = str(raw_path_obj.resolve())
    with transaction():
        rows = conn.execute("SELECT filepath FROM processed_files").fetchall()
        stale = [row["filepath"] for row in rows if _same_file(row["filepath"], target)]
        if stale:
            conn.executemany("DELETE FROM processed_files WHERE filepath = ?", [(path,) for path in stale])
    if stale:
        log.info("Removed %d processed_files row(s) for deleted source %s", len(stale), raw_path_obj.name)


def _same_file(candidate: str, target: str) -> bool:
    try:
        return str(Path(candidate).resolve()) == target
    except OSError:
        return str(candidate) == target

