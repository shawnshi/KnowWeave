import hashlib
import logging
import os
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from vector_lake import db_store
from vector_lake.defense_hook import verify_asset
from vector_lake.schema_validator import check_placeholder_sources, validate_schema
from vector_lake.wiki_utils import (
    atomic_write_text,
    get_index_path,
    get_outbox_signal_path,
    get_wiki_dir,
    split_frontmatter,
    validate_wiki_filename,
)


log = logging.getLogger("vector-lake-mutation")


def resolve_wiki_mutation_path(
    filename: str,
    allow_existing_legacy_name: bool = False,
    *, validate_new_name: bool = True,
) -> Path:
    """Resolve a single wiki basename and reject every traversal form."""
    if not isinstance(filename, str) or not filename or filename != filename.strip():
        raise ValueError("Mutation filename must be a non-empty trimmed string.")
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise ValueError("Mutation filename must be a basename without path separators.")
    candidate_name = Path(filename)
    if candidate_name.is_absolute() or candidate_name.name != filename or filename in {".", ".."}:
        raise ValueError("Mutation filename must be a relative wiki basename.")
    wiki_root = get_wiki_dir().resolve()
    candidate = (wiki_root / filename).resolve()
    if candidate.parent != wiki_root:
        raise ValueError(f"Mutation path escapes wiki boundary: {filename}")
    if candidate.name != filename:
        raise ValueError(f"Mutation filename aliases a differently named wiki page: {filename}")
    if validate_new_name and not (allow_existing_legacy_name and candidate.exists()):
        validate_wiki_filename(filename)
    return candidate


_PROJECTION_LOCAL = threading.local()
PROJECTION_LOCK_SECONDS = 20.0


class ProjectionVersionConflict(RuntimeError):
    """An intent is not bound to the current canonical page state."""


@contextmanager
def projection_locks(filenames: Iterable[str]):
    """Serialize managed canonical and projection writes in sorted page order."""
    from filelock import FileLock
    from vector_lake.wiki_utils import get_runtime_tmp_dir

    held = getattr(_PROJECTION_LOCAL, "held", None)
    if held is None or getattr(_PROJECTION_LOCAL, "pid", None) != os.getpid():
        held = _PROJECTION_LOCAL.held = set()
        _PROJECTION_LOCAL.pid = os.getpid()
    deadline = time.monotonic() + PROJECTION_LOCK_SECONDS
    seen_keys = set()
    with ExitStack() as stack:
        for filename in sorted(set(filenames)):
            path = resolve_wiki_mutation_path(filename, validate_new_name=False)
            key = os.path.normcase(str(path.resolve()))
            if key in seen_keys:
                raise ValueError("Mutation filenames alias the same physical wiki page")
            seen_keys.add(key)
            if key in held:
                continue
            if db_store.in_write_transaction():
                raise RuntimeError("Page locks must be acquired outside a SQLite transaction")
            root = get_runtime_tmp_dir() / "projection-locks"
            root.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock")))
            stack.enter_context(lock.acquire(timeout=max(0.0, deadline - time.monotonic())))
            if os.path.normcase(str(resolve_wiki_mutation_path(filename, validate_new_name=False))) != key:
                raise ValueError("Wiki path changed while waiting for its page lock")
            held.add(key)
            stack.callback(held.remove, key)
        yield


def require_projection_intent(outbox_id: int, filename: str, mutation_type: str, payload_text: str | None, *, claim: dict | None = None, validation_mode: str = "full"):
    row = db_store.validate_mutation_claim(claim) if claim is not None else db_store.current_mutation_intent(outbox_id)
    if str(row["idempotency_key"] or "").startswith("source-metadata:"):
        raise ProjectionVersionConflict("Automatic source publication disabled: verified physical exclusion is unavailable; prepare a patch")
    if row["id"] != outbox_id or row["filename"] != filename or row["mutation_type"] != mutation_type or row["payload_text"] != payload_text or str(row["validation_mode"] or "full") != validation_mode:
        raise db_store.MutationClaimLost("Materialization does not match its durable intent")
    if row["status"] not in {"pending", "processing", "completed"}:
        raise db_store.MutationClaimLost("Outbox intent is not eligible for materialization")
    from vector_lake import governance_store

    page_key = Path(filename).stem
    current = governance_store.canonical_page_versions({page_key}).get(page_key, "")
    expected = row["base_version"]
    if (expected is None and current) or (expected is not None and expected != current) or (mutation_type == "delete" and current):
        raise ProjectionVersionConflict("Outbox intent is unversioned or no longer matches canonical state; controlled re-enqueue required")
    return row


def materialize_markdown_projection(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
    validation_mode: str = "full",
    pre_parsed_frontmatter: dict | None = None,
    *, outbox_id: int, claim: dict | None = None,
) -> Path:
    """Idempotently materialize the Markdown projection for one outbox row."""
    with projection_locks([filename]):
        require_projection_intent(outbox_id, filename, mutation_type, payload_text, claim=claim, validation_mode=validation_mode)
        filepath = resolve_wiki_mutation_path(
            filename,
            # Same reasoning as ``_prepare_mutations``: a delete removes an existing name
            # and cannot introduce a malformed one.
            allow_existing_legacy_name=validation_mode == "schema" or mutation_type == "delete",
            validate_new_name=mutation_type != "delete",
        )
        if mutation_type == "delete":
            if filepath.exists():
                filepath.unlink()
            return filepath
        if mutation_type != "update":
            raise ValueError(f"Unsupported mutation_type: {mutation_type}")
        if payload_text is None:
            if not filepath.exists():
                raise ValueError(f"Outbox update for {filename} has no payload and no existing Markdown projection.")
            payload_text = filepath.read_text(encoding="utf-8")
        atomic_write_text(filepath, payload_text, pre_parsed_frontmatter=pre_parsed_frontmatter, validation_mode=validation_mode)
        return filepath


def _signal_outbox_consumer():
    """Best-effort wake-up hint; correctness never depends on this file.

    The path must match the one the outbox consumer polls, otherwise the hint is
    silently dead and every projection update waits for the next poll tick.
    """
    try:
        atomic_write_text(get_outbox_signal_path(), "1")
    except OSError as exc:
        log.warning(f"Could not write outbox wake-up hint: {exc}")


def _previous_sources(filepath: Path) -> list:
    """The ``sources`` the page carries right now, for the growth-only placeholder rule.

    Read from the Markdown projection rather than from canonical state: this runs before
    the database transaction, and the projection is what the caller is replacing.  A file
    that cannot be read is left to raise -- returning an empty list here would report
    "the page had no sources" for a read error and turn a transient I/O fault into a
    provenance verdict.
    """
    if not filepath.exists():
        return []
    frontmatter, _ = split_frontmatter(filepath.read_text(encoding="utf-8"))
    return list(frontmatter.get("sources") or [])


def _prepare_mutations(
    mutations: Iterable[dict],
    validation_mode: str = "full",
) -> list[dict]:
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    prepared = []
    seen_filenames = set()
    for mutation in mutations:
        filename = mutation.get("filename")
        content = mutation.get("content")
        is_delete = bool(mutation.get("is_delete", False))
        has_expected_version = "expected_version" in mutation
        expected_version = mutation.get("expected_version")
        if has_expected_version and not isinstance(expected_version, str):
            raise ValueError("expected_version must be a string when supplied.")
        filepath = resolve_wiki_mutation_path(
            filename,
            # Deleting a page never *creates* a name, so an existing legacy filename
            # must not block the removal.  Without this a page whose name violates
            # the pattern could never be renamed: `rename_vector_lake_entity` starts
            # by deleting the old name, and the validator rejected that same name.
            allow_existing_legacy_name=validation_mode == "schema" or is_delete,
        )
        if filename in seen_filenames:
            raise ValueError(f"A mutation batch cannot contain duplicate filenames: {filename}")
        seen_filenames.add(filename)

        mutation_type = "delete" if is_delete else "update"
        if not is_delete:
            if content is None:
                raise ValueError("Update mutations require full Markdown content.")
            frontmatter, _ = split_frontmatter(content)
            # Growth-only, and checked before the mode branch so both validation modes
            # answer to it: `verify_asset` and `validate_schema` are both pure, and
            # neither can see the page being replaced.
            check_placeholder_sources(frontmatter.get("sources"), _previous_sources(filepath))
            # A page this batch is about to create is held to the new-node rules -- but only in
            # ``full`` mode.  ``schema`` mode is the bounded legacy-maintenance path: it
            # re-materializes nodes that already exist (projections, restores, renames), and a
            # legacy node keeps the classification it was written under.
            is_new = validation_mode == "full" and not filepath.exists()
            if validation_mode == "full":
                verify_asset(content, filename, frontmatter, get_index_path(), is_new)
            else:
                validate_schema(frontmatter, content, filename, get_index_path(), is_new)
        else:
            frontmatter = None

        prepared.append(
            {
                "filename": filename,
                "content": content,
                "frontmatter": frontmatter,
                "filepath": filepath,
                "mutation_type": mutation_type,
                "has_expected_version": has_expected_version,
                "expected_version": expected_version,
                "idempotency_key": hashlib.sha256(
                    (
                        f"{mutation_type}\x00{filename}\x00{content or ''}"
                        + (
                            f"\x00expected_version={expected_version}"
                            if has_expected_version
                            else ""
                        )
                        + (f"\x00validation={validation_mode}" if validation_mode != "full" else "")
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    if not prepared:
        raise ValueError("A mutation batch must contain at least one mutation.")
    return prepared


def execute_mutation_batch(
    mutations: Iterable[dict],
    canonical_callback: Callable[[], None] | None = None,
    validation_mode: str = "full",
    *, canonical_precondition: Callable[[], None] | None = None,
):
    """Commit canonical mutations atomically; schema mode is for bounded legacy maintenance."""
    if db_store.in_write_transaction():
        raise RuntimeError("Managed page mutation must start outside a SQLite transaction")
    from vector_lake.runtime_health import enforce_runtime_write_health

    enforce_runtime_write_health(validation_mode=validation_mode)
    prepared = _prepare_mutations(mutations, validation_mode=validation_mode)
    db_store.init_db()
    outbox_ids = []
    from vector_lake import governance_store

    prepared_change_sets = []
    for mutation in prepared:
        if mutation["mutation_type"] != "update":
            continue
        filename = mutation["filename"]
        change_set = governance_store.prepare_change_set_from_content(
            filename,
            mutation["content"],
            origin="mutation_coordinator",
            auto_approve=True,
            summary=f"Canonical mutation for {filename}",
            pre_parsed_frontmatter=mutation.get("frontmatter"),
        )
        prepared_change_sets.append(change_set)

    with projection_locks([mutation["filename"] for mutation in prepared]):
        for mutation in prepared:
            if mutation["mutation_type"] == "update":
                check_placeholder_sources(mutation["frontmatter"].get("sources"), _previous_sources(mutation["filepath"]))
        with db_store.transaction():
            if canonical_precondition is not None:
                canonical_precondition()
            versioned = [mutation for mutation in prepared if mutation["has_expected_version"]]
            if versioned:
                page_keys = {
                    Path(mutation["filename"]).stem
                    for mutation in versioned
                }
                current_versions = governance_store.canonical_page_versions(page_keys)
                for mutation in versioned:
                    filename = mutation["filename"]
                    page_key = Path(filename).stem
                    expected = mutation["expected_version"]
                    current = current_versions.get(page_key)
                    if (expected == "" and current is not None) or (
                        expected != "" and current != expected
                    ):
                        raise ValueError(
                            f"Canonical version conflict for {filename}: "
                            f"expected {expected or '<absent>'}, current {current or '<absent>'}"
                        )
            for mutation in prepared:
                filename = mutation["filename"]
                content = mutation["content"]
                if mutation["mutation_type"] == "delete":
                    node_key = Path(filename).stem
                    if len(prepared) == 1:
                        db_store.delete_node_cascade(node_key)
                    else:
                        db_store.delete_node_cascade(
                            node_key, affected_page_keys={Path(item["filename"]).stem for item in prepared},
                        )
            governance_store.apply_change_sets_batch(prepared_change_sets)
            published_at = datetime.now(timezone.utc).isoformat()
            for change_set in prepared_change_sets:
                change_set["published_at"] = published_at
            governance_store.record_prepared_change_sets(prepared_change_sets)

            outbox_items = [
                {
                    "filename": mutation["filename"],
                    "mutation_type": mutation["mutation_type"],
                    "payload_text": mutation["content"],
                    "idempotency_key": mutation["idempotency_key"],
                    "validation_mode": validation_mode,
                }
                for mutation in prepared
            ]
            outbox_ids = db_store.enqueue_mutations_batch(outbox_items)

            page_keys = {Path(mutation["filename"]).stem for mutation in prepared}
            versions = governance_store.canonical_page_versions(page_keys)
            db_store.bind_mutation_versions(outbox_ids, versions)
            if canonical_callback is not None:
                canonical_callback()
                if governance_store.canonical_page_versions(page_keys) != versions:
                    raise ProjectionVersionConflict("Canonical callback changed a prepared page; transaction rolled back")

        deferred = []
        import inspect
        sig = inspect.signature(materialize_markdown_projection)
        supports_frontmatter = "pre_parsed_frontmatter" in sig.parameters

        for mutation, outbox_id in zip(prepared, outbox_ids):
            try:
                if supports_frontmatter:
                    materialize_markdown_projection(
                        mutation["filename"],
                        mutation["mutation_type"],
                        mutation["content"],
                        validation_mode=validation_mode,
                        outbox_id=outbox_id,
                        pre_parsed_frontmatter=mutation.get("frontmatter"),
                    )
                else:
                    materialize_markdown_projection(
                        mutation["filename"],
                        mutation["mutation_type"],
                        mutation["content"],
                        validation_mode=validation_mode,
                        outbox_id=outbox_id,
                    )
            except Exception as exc:
                deferred.append(mutation["filename"])
                log.error(
                    "Canonical mutation %s committed but projection failed for %s: %s",
                    outbox_id,
                    mutation["filepath"],
                    exc,
                )

    _signal_outbox_consumer()
    projection_note = (
        f"projections deferred for {', '.join(deferred)}"
        if deferred
        else "all Markdown projections materialized"
    )
    return True, f"Canonical mutation batch committed; outbox ids {outbox_ids}; {projection_note}."


def execute_mutation_plan(filename: str, content: str | None = None, is_delete: bool = False):
    """Commit one canonical mutation and durable intent before updating projections."""
    return execute_mutation_batch(
        [{"filename": filename, "content": content, "is_delete": is_delete}]
    )
