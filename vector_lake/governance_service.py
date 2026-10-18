import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import governance_metrics, governance_store
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import get_wiki_dir, VALID_PREFIXES

log = logging.getLogger("governance_service")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _mark_resolved(item: dict, resolution: str) -> None:
    item["status"] = "resolved"
    item["resolution"] = resolution
    item["resolved_at"] = _utc_now()


def _page_name_index(wiki_dir: Path) -> dict[str, Path]:
    """``filename -> absolute path`` for the pages directly under ``wiki_dir``, one directory read.

    ``_find_page_file`` used to ask the filesystem per candidate -- ``(wiki_dir / name).resolve()``
    then ``.exists()`` -- and a *miss* costs exactly as much as a hit, so a lookup tries up to
    ``len(VALID_PREFIXES) + 1`` realpath walks.  Measured 2026-09-25: 2 798 lookups cost **17.8 s,
    51% of the lint wall**, with the leaf frames inside ntpath's ``realpath``/
    ``_getfinalpathname_nonstrict``.  A listing answers the same question (does this exact filename
    exist directly under this directory?) without touching the filesystem per candidate; the root is
    resolved once so the returned paths stay canonical, as they were.
    """
    try:
        root = Path(wiki_dir).resolve()
        names = os.listdir(root)
    except OSError:
        return {}
    return {name: root / name for name in names}


def _find_page_file(wiki_dir: Path, name, index: dict[str, Path] | None = None):
    """The page file a name refers to, tried under every valid prefix.

    ``index`` lets a caller that resolves many names -- the lint walks a whole governance queue --
    pay for the directory read once.  Omitting it is still correct, just slower.
    """
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    files = _page_name_index(wiki_dir) if index is None else index
    for candidate_name in [
        *(f"{prefix}{name}.md" for prefix in VALID_PREFIXES),
        f"{name}.md",
    ]:
        path = files.get(candidate_name)
        if path is not None:
            return path
    return None


def _registered_page_path(wiki_dir: Path, entity_id, find) -> Path | None:
    """The page file an entity id is registered under, via the alias registry.

    A governance item records *declared* names, and a declared name can disagree with the
    filename about ``_``/``-`` -- the WiNEX pair recorded ``Synthesis_WiNEX_Concurrency_Model``
    while the file is ``Synthesis_WiNEX-Concurrency-Model.md``.  The registry already maps an
    entity id onto the keys that name it, so asking it is the difference between "this pair
    cannot be resolved" and a merge that actually runs.
    """
    if not entity_id:
        return None
    try:
        rows = governance_store.get_connection().execute(
            "select key from alias_registry where value = ? order by key", (entity_id,)
        ).fetchall()
    except Exception as exc:  # pragma: no cover - registry faults must not merge blindly
        log.warning("Alias registry lookup failed for %s: %s", entity_id, exc)
        return None
    for row in rows:
        path = find(wiki_dir, row[0])
        if path:
            return path
    return None


def unapplied_merge_items(queue: dict | None = None) -> list[str]:
    """Ids of merge items recorded as resolved while both of their pages are still live.

    A merge is durable when the consumed page is gone.  The WiNEX pair sat in this state from
    2026-06-24 to 2026-09-25 because nothing asked the question, and it is not alone: the corpus
    holds a large backlog of resolutions written before ``resolve_governance_item`` could apply a
    merge at all (``resolved_at`` clustering in June 2026 is the shape of it).  Most of those are
    similarity candidates for pages that are *near* neighbours rather than duplicates, so the
    answer is a decision per pair -- not an automated merge.  ``lint`` reports the count.
    """
    queue = queue if queue is not None else governance_store.load_governance_queue()
    wiki_dir = get_wiki_dir().resolve()
    page_index = _page_name_index(wiki_dir)
    outstanding: list[str] = []
    for item in queue.get("items", []):
        if item.get("type") != "merge" or item.get("status") != "resolved":
            continue
        # Only an item that *claims* a merge is an unapplied merge.  Reading status alone
        # reported a ~1,000-item backlog that had already been triaged: the June-2026 bulk pass
        # wrote ``resolution="skip"`` for the neighbours it did not merge, and every one of them
        # counted as outstanding because ``resolution`` was never read.
        if item.get("resolution") != "merge" or item.get("merge_applied"):
            continue
        candidate = item.get("merge_candidate") or {}
        left_path = _find_page_file(wiki_dir, candidate.get("left_name"), page_index)
        right_path = _find_page_file(wiki_dir, candidate.get("right_name"), page_index)
        left_path = left_path or _registered_page_path(
            wiki_dir, candidate.get("left_entity_id"),
            lambda d, n: _find_page_file(d, n, page_index),
        )
        right_path = right_path or _registered_page_path(
            wiki_dir, candidate.get("right_entity_id"),
            lambda d, n: _find_page_file(d, n, page_index),
        )
        if left_path and right_path and left_path != right_path:
            outstanding.append(str(item.get("item_id") or ""))
    return outstanding


def resolve_governance_item(item_id: str, resolution: str = "skip", change_manifest: dict | None = None) -> dict | None:
    """Resolve one governance item, committing the item and its effect together.

    The queue lock is taken *before* the database transaction: the documented lock
    order is file-lock -> database transaction, and the merge path below writes the
    queue row inside the mutation's own transaction.  That is what stops a merge
    from committing while its item stays ``pending`` - a client or process that
    dies in between used to leave an item whose consumed page no longer exists, so
    re-running it could never succeed.
    """
    with governance_store.governance_queue_session():
        return _resolve_locked(item_id, resolution, change_manifest)


def _resolve_locked(item_id: str, resolution: str = "skip", change_manifest: dict | None = None) -> dict | None:
    queue = governance_store.load_governance_queue()
    for item in queue["items"]:
        if item.get("item_id") != item_id:
            continue
        # We allow re-resolving if it's already resolved but we need to re-apply the merge
        if item.get("status") != "pending" and not (item.get("status") == "resolved" and resolution == "merge"):
            continue
        
        # ACTUALLY PERFORM THE MERGE LOGIC HERE IF RESOLUTION IS MERGE
        if resolution == "merge":
            # Fail closed.  The old shape fell through to ``_mark_resolved`` whenever the item's
            # type or ids did not match, which is how a resolved merge can name no page at all;
            # a merge may only be recorded as resolved once it has been applied.
            if item.get("type") != "merge":
                raise RuntimeError("merge resolution requires a merge governance item.")
            candidate = item.get("merge_candidate") or {}
            left_id = candidate.get("left_entity_id")
            right_id = candidate.get("right_entity_id")
            if not (left_id and right_id):
                raise RuntimeError("merge resolution requires both entity ids.")
            left_name = candidate.get("left_name")
            right_name = candidate.get("right_name")
            left_path, right_path = None, None
            old_left_entity = governance_store.get_entity(left_id)
            wiki_dir = get_wiki_dir().resolve()

            left_path = _find_page_file(wiki_dir, left_name)
            right_path = _find_page_file(wiki_dir, right_name)
            # Declared names and filenames can disagree about ``_``/``-``; ask the registry before
            # calling the pair unresolvable.
            left_path = left_path or _registered_page_path(
                wiki_dir, left_id, _find_page_file
            )
            right_path = right_path or _registered_page_path(
                wiki_dir, right_id, _find_page_file
            )

            # Validate every manifest constraint before canonical state changes.
            if change_manifest:
                try:
                    if old_left_entity and old_left_entity.get("merged_into") == right_id:
                        raise ValueError("Cycle detected: Target is already merged into Source.")
                    if change_manifest.get("allow_cycles", False) is False:
                        visited = set()
                        current = right_id
                        while current:
                            if current in visited:
                                raise ValueError("AHE Contract Failed: Alias cycle detected.")
                            visited.add(current)
                            current = governance_store.get_alias(current)
                except Exception as e:
                    log.error(f"AHE Contract Failed before mutation: {e}.")
                    raise RuntimeError(f"Manifest validation failed: {e}") from e

            if not left_path or not right_path or left_path == right_path:
                raise RuntimeError("Semantic merge requires two distinct existing wiki pages.")

            # Entry guard.  A shared name claimed by three or more live
            # entities cannot be resolved by merging one pair, and callers
            # other than find_merge_candidates (bulk_reconciliation) build
            # their own merge_candidate, so the check has to sit here.
            hazards = governance_metrics.ambiguous_name_hazards(
                [Path(left_path).stem, Path(right_path).stem]
            )
            if hazards and not (change_manifest or {}).get("allow_ambiguous_names", False):
                raise RuntimeError(
                    "Manifest validation failed: ambiguous name claim; "
                    + "; ".join(hazards)
                    + '. Re-issue with {"allow_ambiguous_names": true} to force.'
                )

            left_content = Path(left_path).read_text(encoding="utf-8")
            right_content = Path(right_path).read_text(encoding="utf-8")
            # The consumed page's filename is not derivable from its frontmatter,
            # and inbound ``[[ConsumedKey]]`` links depend on it surviving as an
            # alias.  Passing it here is what stops a merge from breaking them.
            merged_content = merge_markdown_content(
                left_content,
                right_content,
                consumed_page_key=Path(right_path).stem,
            )

            def commit_resolution():
                # One transaction with the canonical mutation: the registry
                # alias and the queue row either both land or neither does.
                governance_store.upsert_alias(right_id, left_id)
                _mark_resolved(item, resolution)
                governance_store.save_governance_queue(queue)

            execute_mutation_batch(
                [
                    {"filename": Path(left_path).name, "content": merged_content},
                    {"filename": Path(right_path).name, "is_delete": True},
                ],
                canonical_callback=commit_resolution,
            )
            return item

        # Not a merge (or an unknown resolution): record the decision alone, but never for a
        # merge -- that branch above fails closed instead of falling through to here.
        _mark_resolved(item, resolution)
        governance_store.save_governance_queue(queue)
        return item
    return None
