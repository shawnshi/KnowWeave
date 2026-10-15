import json
import os
import hashlib
import logging
import re
import time

import yaml
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import get_extension_root
from vector_lake.template_loader import read_template, render_template
from vector_lake.ingest_schema_contract import render_schema_contract, schema_snapshot
from vector_lake.db_store import mark_file_processed
from vector_lake import governance_store
from vector_lake.skeleton_parser import parse_static_skeleton
from vector_lake.wiki_utils import (
    canonical_source_name,
    flush_durable,
    projection_hash,
    read_ingest_item_content,
    resolve_ingest_source_path,
    split_frontmatter,
    dump_yaml,
    get_raw_dir,
    get_wiki_dir,
    get_index_path,
    is_private_raw_source,
    load_config,
    read_frontmatter_only,
    validate_wiki_filename,
)
from vector_lake.schema_validator import INGEST_EVENT_TAGS, INGEST_INTEGRATION_PREDICATES, VALID_PREDICATES
from vector_lake.purpose_contract import (
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    validate_ingest_payload,
)

log = logging.getLogger("vector-lake-ingest")

def list_ingest_tasks(limit: int = 20, include_queued: bool = True) -> str:
    """List ingest jobs that require operator or host-subagent action."""
    from vector_lake.db_store import get_jobs_by_status

    statuses = ["awaiting_subagent"]
    if include_queued:
        statuses.insert(0, "queued")
    rows = get_jobs_by_status(statuses, limit=limit)
    if not rows:
        return "No queued or awaiting-subagent ingest jobs."
    lines = ["=== Ingest Task Queue ==="]
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        lines.append(
            "- "
            f"{row.get('job_id')} "
            f"status={row.get('status')} retries={row.get('retries')} "
            f"file={payload.get('filepath', '<unknown>')} "
            f"task_packet={row.get('task_packet_path') or '<not-created>'}"
        )
    return "\n".join(lines)


def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease task packets to the current host runtime and return structured work."""
    from vector_lake.db_store import claim_subagent_jobs

    claimed = claim_subagent_jobs(limit=limit, lease_seconds=lease_seconds)
    tasks = []
    for row in claimed:
        packet_path = row.get("task_packet_path")
        task_packet = None
        if packet_path:
            try:
                task_packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                task_packet = {"error": f"Unreadable task packet: {type(exc).__name__}: {exc}"}
        if packet_path and not isinstance(task_packet, dict):
            task_packet = {"error": "Task packet must be a JSON object"}
        if isinstance(task_packet, dict) and "error" not in task_packet:
            metadata = task_packet.get("metadata")
            processed = metadata.get("processed_data") if isinstance(metadata, dict) else None
            if not isinstance(processed, dict) or not processed:
                task_packet = {"error": "Task packet requires metadata.processed_data object"}
            else:
                processed.update({
                    "job_id": row.get("job_id"),
                    "lease_owner": row.get("lease_owner"),
                    "lease_token": row.get("lease_token"),
                    "lease_generation": row.get("lease_generation"),
                })
        # Return every acquired lease, including errors. The consumer alone spends its
        # attempt, so cycle caps and healthy work behind a damaged packet remain visible.
        tasks.append({
            "job_id": row.get("job_id"),
            "task_type": row.get("task_type"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "lease_until": row.get("lease_until"),
            "lease_owner": row.get("lease_owner"),
            "lease_token": row.get("lease_token"),
            "lease_generation": row.get("lease_generation"),
            "task_packet_path": packet_path,
            "task_packet": task_packet,
        })
    return json.dumps(tasks, ensure_ascii=False, indent=2)


def is_transient_failure(reason: str) -> bool:
    """Whether redispatch can resolve the failure without spending the source budget."""
    from vector_lake.ingest_errors import failure_is_transient

    return failure_is_transient(reason)


def record_ingest_failure(job_id: str, reason: str, *, claim: dict | None = None) -> str:
    """Consume one attempt of a job's bounded budget; report whether it is now terminal.

    The runner's failure branches used to increment only a local counter.  The job stayed in
    ``subagent_processing`` holding a one-hour lease, ``claim_ingest_tasks`` re-claims the
    oldest job first, and the same deterministically-rejecting task was therefore re-attempted
    every hour forever -- one model call each time, with no attempt cap and nothing visible in
    the queue.  The contract in ``scripts/ingest_runner.py`` already promised "C3 bounded
    attempts; never re-claim a job to try once more"; this is what makes that true.

    A transient failure (:func:`is_transient_failure`) is released for retry without consuming
    the budget, because terminalizing it would abandon a source a later attempt would ingest.
    """
    from vector_lake.db_store import (
        MAX_INGEST_ATTEMPTS,
        _ingest_failure_claim_current,
        get_connection,
        release_job_for_retry,
        transaction,
        update_job_status,
    )
    from datetime import datetime, timezone

    reason = str(reason or "unspecified failure")[:500]
    with transaction():
        row = get_connection().execute(
            "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
        ).fetchone()
        if row is None:
            return f"job {job_id} is not in the jobs table; nothing recorded"
        if not _ingest_failure_claim_current(dict(row), claim, datetime.now(timezone.utc).isoformat()):
            return f"job {job_id} failure ignored: claim is stale, unowned or terminal"
        if is_transient_failure(reason):
            if not release_job_for_retry(str(job_id), reason, claim=claim):
                return f"job {job_id} failure ignored: claim expired before release"
            return f"job {job_id} released for retry (transient: {reason[:80]}); attempt budget untouched"
        attempt = int(row["retries"] or 0) + 1
        if not update_job_status(str(job_id), "failed", reason, claim=claim):
            return f"job {job_id} failure ignored: claim expired before failure update"
        if attempt < MAX_INGEST_ATTEMPTS:
            return f"job {job_id} failed attempt {attempt}/{MAX_INGEST_ATTEMPTS}: will be re-dispatched"

        # Failure and content abandonment share the ownership check and transaction.
        abandoned = 0
        try:
            fields = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            fields = {}
        filepath = str(fields.get("filepath") or "")
        file_hash = str(fields.get("hash") or "")
        if filepath and file_hash:
            from vector_lake.db_store import record_abandoned_source

            abandoned = record_abandoned_source(filepath, file_hash, reason)
        suffix = (
            f"; source abandoned after {abandoned} terminal job(s) on this content "
            "(clear with `cli.py ingest-tasks --clear-abandoned`)"
            if abandoned
            else ""
        )
        return (
            f"job {job_id} failed attempt {attempt}/{MAX_INGEST_ATTEMPTS}: attempt budget "
            f"spent, no further dispatch ({reason}){suffix}"
        )


def reconcile_ingest_in_flight(grace_seconds: float = 120.0) -> dict:
    """Release in-flight markers whose source has no dispatchable job behind it.

    The ledger exists to stop two scans scheduling the same source twice, so a marker is only
    meaningful while a job is actually queued, leased or awaiting a subagent.  A marker left
    without one makes the source invisible: ``prepare_ingest_batch`` skips it, so it never gets
    enqueued, and the marker is refreshed on every later scan that sees it expire.

    Measured on the live corpus 2026-09-19 07:41: 24 sources marked in one batch while only 2
    jobs were created, three of them with no job row at all and no key match either.  The
    producer of that particular batch was not identified, which is exactly why this is written
    as a reconciliation rather than a fix to one caller: any path that leaves a marker behind
    becomes a transient state instead of a stuck one.

    ``grace_seconds`` keeps a marker that a scan is legitimately holding right now (the batch
    is marked before it is enqueued) from being reaped out from under it.
    """
    import time

    from vector_lake.db_store import get_connection

    ledger = _load_ingest_in_flight()
    if not ledger:
        return {"checked": 0, "released": []}

    conn = get_connection()
    # Statuses from which the pipeline will still act on the job.  ``failed`` only counts while
    # its attempt budget is unspent: at the cap ``claim_pending_jobs`` refuses to dispatch it, so
    # a marker pointing at such a job is exactly the orphan this function exists to release.
    # Getting that wrong is not theoretical -- measured on 2026-09-19, 7 of the 11 terminal
    # failures held in-flight markers, and treating them as active kept every one of those sources
    # invisible to the scan that would have re-enqueued them.
    from vector_lake.db_store import MAX_INGEST_ATTEMPTS

    dispatchable = ("queued", "dispatched", "awaiting_subagent", "subagent_processing")
    placeholders = ", ".join("?" for _ in dispatchable)
    active: set[str] = set()
    rows = conn.execute(
        f"SELECT payload FROM jobs WHERE status IN ({placeholders}) "
        f"OR (status = 'failed' AND retries < ?)",
        (*dispatchable, MAX_INGEST_ATTEMPTS),
    )
    for (payload,) in rows:
        try:
            filepath = json.loads(payload or "{}").get("filepath")
        except (TypeError, ValueError):
            continue
        if filepath:
            active.add(_ingest_ref(filepath))

    now = time.time()
    released = [
        ref
        for ref, entry in ledger.items()
        if now - float(entry.get("since", 0) or 0) >= grace_seconds and ref not in active
    ]
    for ref in released:
        _clear_ingest_in_flight(ref)
    return {"checked": len(ledger), "released": released}


def list_terminal_failed_ingest_jobs() -> str:
    """Jobs that spent their attempt budget, with the source each names."""
    from vector_lake.db_store import list_terminal_failed_jobs

    jobs = list_terminal_failed_jobs()
    if not jobs:
        return "No terminal-failed ingest jobs."
    lines = [f"{len(jobs)} terminal-failed ingest job(s):"]
    for job in jobs:
        name = os.path.basename(job["filepath"]) or "(no filepath)"
        lines.append(
            f"- {job['job_id']} retries={job['retries']} at {job['updated_at'][:19]} "
            f"{name}: {job['error_msg'][:150]}"
        )
    lines.append(
        "Close those whose source is now ingested with `cli.py ingest-tasks --close-terminal-failed`."
    )
    return "\n".join(lines)


def close_terminal_failed_ingest_jobs(source_ingested_only: bool = True) -> str:
    """Mark terminal-failed jobs superseded once their source is ingested."""
    from vector_lake.db_store import close_terminal_failed_jobs

    result = close_terminal_failed_jobs(source_ingested_only=source_ingested_only)
    kept = len(result["kept"])
    message = f"Closed {len(result['closed'])} terminal-failed job(s) as superseded."
    if kept:
        message += (
            f" Kept {kept} whose source has no processed_files row: those are still unfinished, "
            "and the abandonment rule decides their fate."
        )
    return message


def list_abandoned_ingest_sources() -> str:
    """Sources withheld from dispatch after repeated deterministic failures."""
    from vector_lake.db_store import list_abandoned_sources

    rows = list_abandoned_sources()
    if not rows:
        return "No abandoned ingest sources."
    lines = [f"{len(rows)} abandoned ingest source(s):"]
    for row in rows:
        lines.append(
            f"- {row['filepath']} (terminal_jobs={row['terminal_jobs']}, "
            f"since {str(row['abandoned_at'])[:19]}): {str(row['reason'])[:160]}"
        )
    lines.append("Re-dispatch with `cli.py ingest-tasks --clear-abandoned [FILE]`, or fix the source.")
    return "\n".join(lines)


def clear_abandoned_ingest_sources(filepath: str | None = None) -> str:
    """Allow abandoned source(s) to be dispatched again."""
    from vector_lake.db_store import clear_abandoned_sources

    cleared = clear_abandoned_sources(filepath)
    scope = f"'{filepath}'" if filepath else "all sources"
    return (
        f"Cleared {cleared} abandoned ingest source record(s) for {scope}. "
        "They are eligible for dispatch on the next scan."
    )


def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Mark stale awaiting-subagent ingest jobs as failed so they can be retried explicitly."""
    from vector_lake.db_store import expire_stale_subagent_jobs

    expired = expire_stale_subagent_jobs(max_age_seconds=max_age_seconds)
    return f"Expired {expired} awaiting-subagent ingest job(s)."

#: The rule itself lives in ``wiki_utils`` beside the other naming functions, where
#: ``claim_extractor`` and ``tool_delete`` can reach it without importing this module.



_NAME_KEY = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _name_key(value: str) -> str:
    """Alphanumeric-only comparison key, so ``a_b`` matches ``a-b``."""
    return _NAME_KEY.sub("", str(value)).lower()


def _declares_raw_source(item: dict, raw_key: str) -> bool:
    """Whether this item's ``sources:`` names the raw file with this :func:`_raw_key`."""
    if not raw_key:
        return False
    try:
        frontmatter, _body = split_frontmatter(read_ingest_item_content(item))
    except Exception:  # noqa: BLE001 - unparsable frontmatter is the validator's business
        return False
    sources = frontmatter.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    return any(_raw_key(str(source)) == raw_key for source in sources)


def _source_item_to_stamp(
    files: list, canonical_name: str, raw_filepath: str
) -> tuple[dict | None, str]:
    """``(item, deviation)``: the Source page this packet's raw file was written to.

    Two rules name a Source page.  ``canonical_source_name`` is the one the packet mandates and the
    only one the code builds now, and for an *accepted* ingest
    :func:`_apply_integration_disposition` has already required exactly one item under that name --
    so the first branch is what normally fires.  The second covers the pages that predate that gate:
    443 of the 1 782 live Source pages carry the model-chosen ``Source_<dir>-<stem>-<hash8>`` shape,
    and for 1 406 of the 1 874 ledgered raw sources the two rules disagreed on the name.

    Identity is the *declaration*: a page that lists this raw file in ``sources:`` is the page this
    source was compiled into, whatever it is called.  That is the rule :func:`_declared_raw_sources`
    already reconciles on, so this keys the stamp on the same fact instead of on a naming
    convention.  The deviation is returned so the caller can report it rather than let the two rules
    diverge quietly.
    """
    for item in files:
        if os.path.basename(str(item.get("filename") or "")) == canonical_name:
            return item, ""
    raw_key = _raw_key(raw_filepath)
    for item in files:
        filename = os.path.basename(str(item.get("filename") or ""))
        if not filename.startswith("Source_"):
            continue
        if _declares_raw_source(item, raw_key):
            return item, filename
    return None, ""


def _raw_key(raw_path: str) -> str:
    """Comparison key for a raw source: its stem, alphanumerics only."""
    return _name_key(Path(str(raw_path)).stem)


def _declared_raw_sources() -> dict[str, dict]:
    """``{stem_key: {"page", "created", "source_hash"}}`` for every declared raw source.

    Same scan as :func:`_declared_raw_keys`, carrying the three facts the reconciliation below
    needs: which page declares the source, when that page was written, and -- for pages written
    since :func:`_stamp_source_hash` exists -- the raw content hash the page was compiled from.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return {}
    declared: dict[str, dict] = {}
    for entry in os.scandir(wiki_dir):
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        try:
            frontmatter = read_frontmatter_only(entry.path)
        except Exception:
            continue
        record = {
            "page": entry.name,
            "created": str(frontmatter.get("created") or ""),
            "source_hash": str(frontmatter.get("source_hash") or ""),
        }
        for source in frontmatter.get("sources") or []:
            text = str(source).replace("\\", "/")
            if "raw/" not in text:
                continue
            key = _raw_key(text)
            if key and (key not in declared or (entry.name.startswith("Source_")
                    and not declared[key]["page"].startswith("Source_"))):
                declared[key] = record
    return declared


def _declared_raw_keys() -> set:
    """Stems of every raw source that some wiki page declares in its ``sources:``.

    This is the authoritative "already ingested" signal, and it is path-form tolerant
    once normalised (``a_b`` == ``a-b``, spaces and punctuation dropped).  Matching on
    published Source *page names* cannot work for short stems (``AI.md``, ``品牌.md``):
    the page's name cannot be trusted to locate it: pages are named by
    ``canonical_source_name(raw)`` = ``Source_<sanitised stem>`` from one rule, while 443 of the
    1 782 live Source pages carry the model-chosen ``Source_<dir>-<stem>-<hash8>`` of an earlier
    convention, so a short stem never lands at the start and substring matching over-matches
    unrelated pages.

    Built from frontmatter only (one cheap head-read per page) and only when the caller
    actually needs it, so an idle scan pays nothing.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return set()
    keys = set()
    for entry in os.scandir(wiki_dir):
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        try:
            frontmatter = read_frontmatter_only(entry.path)
        except Exception:
            continue
        for source in frontmatter.get("sources") or []:
            text = str(source).replace("\\", "/")
            if "raw/" not in text:
                continue
            key = _raw_key(text)
            if key:
                keys.add(key)
    return keys


def _published_source_keys() -> list:
    """Normalised names of every existing ``Source_*`` page.

    Fallback signal for pages that declare no ``sources:`` path at all.  Used together
    with :func:`_declared_raw_keys` - the skip decision is their *union*, because for
    duplicate prevention an over-eager skip only delays an ingest while an under-eager
    one writes a second copy of a page that already exists.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return []
    return [
        _name_key(name[:-3])
        for name in os.listdir(wiki_dir)
        if name.startswith("Source_") and name.endswith(".md")
    ]


def _already_published(filepath: str, name_keys: list, declared_keys: set, min_key_len: int = 4) -> bool:
    """True when this raw source is already published, by either signal.

    A raw file that has been published must not be prepared again: the
    ``processed_files`` row is written on the finalize path, so a source ingested while
    the ingest pipeline was stalled has a page but no row, and the ledger check below
    therefore keeps treating it as new - the queue refilled with such files indefinitely.
    Short stems are only matched by prefix against page names (a stem like ``AI`` would
    otherwise match unrelated pages) but are matched exactly against declared sources,
    which is why the declaration signal is needed in the union.
    """
    key = _raw_key(filepath)
    if not key:
        return False
    if key in declared_keys:
        return True
    if len(key) < min_key_len:
        return any(page.startswith(key) for page in name_keys)
    return any(key in page for page in name_keys)

def raw_publication_index() -> dict:
    """Public: both signals used to decide whether a raw source is already published.

    Returns ``{"name_keys": [...], "declared_keys": set()}`` so a caller can build the
    index once and reuse it across a batch.  ``prepare_ingest_batch`` and the host-side
    ingest runner must share this single implementation - when they did not, their
    verdicts drifted (a name-only predicate called 88 raw files un-ingested where the
    union calls 73, and would have re-ingested 15 that were already published).
    """
    return {
        "name_keys": _published_source_keys(),
        "declared_keys": _declared_raw_keys(),
        "declared_pages": _declared_raw_sources(),
    }


def raw_is_published(filepath: str, index: dict | None = None) -> bool:
    """Public: is this raw source already published?  Union of both signals."""
    if index is None:
        index = raw_publication_index()
    return _already_published(filepath, index["name_keys"], index["declared_keys"])


def current_source_publication_evidence(filepath: str, expected_hash: str, index: dict | None = None) -> dict:
    """Bind actual canonical state, its complete projection, and exact raw ownership/bytes."""
    from vector_lake.wiki_utils import canonical_source_name, resolve_ingest_source_path

    raw = resolve_ingest_source_path(filepath)
    if raw is None or not expected_hash or not raw.is_file():
        return {}
    before = raw.stat()
    hashers = {"md5": hashlib.md5(), "sha256": hashlib.sha256()}
    with raw.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            for hasher in hashers.values():
                hasher.update(block)
    after = raw.stat()
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
        raise ValueError("raw source changed while verifying publication")
    expected_algorithm = "sha256" if expected_hash.startswith("sha256:") or len(expected_hash) == 64 else "md5"
    if hashers[expected_algorithm].hexdigest() != expected_hash.removeprefix(expected_algorithm + ":"):
        return {}
    if index is None:
        index = {}
    declared = index.get("declared_pages", {}).get(_raw_key(filepath), {})
    candidates = {canonical_source_name(filepath), str(declared.get("page") or "")}
    from vector_lake.db_store import get_connection

    rows = get_connection().execute(
        "SELECT json_extract(payload, '$.canonical_name') AS name FROM jobs "
        "WHERE task_type IN ('ingest', 'ingest_recompile') "
        "AND json_extract(payload, '$.filepath') IN (?, ?)", (filepath, str(raw))
    ).fetchall()
    candidates.update(str(row["name"] or "") for row in rows)
    for name in candidates:
        if not name.startswith("Source_") or os.path.basename(name) != name:
            continue
        page = get_wiki_dir() / name
        if not page.is_file():
            continue
        content = page.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(content)
        if not body.strip():
            continue
        key = name[:-3]
        version = governance_store.canonical_page_versions({key}).get(key)
        if not version or governance_store.canonical_page_version_from_content(name, content) != version:
            continue
        if str(frontmatter.get("type") or "").lower() != "source":
            continue
        refs = frontmatter.get("sources") or []
        if isinstance(refs, str):
            refs = [refs]
        if not any(resolve_ingest_source_path(str(ref)) == raw for ref in refs):
            continue
        rows = governance_store.get_connection().execute(
            "SELECT data_json FROM entities WHERE f_page_key = ?", (key,)
        ).fetchall()
        records = [json.loads(row["data_json"]) for row in rows]
        canonical_sources = [record for record in records
                             if str(record.get("type") or "").lower() == "source"]
        if len(canonical_sources) != 1:
            continue
        canonical_source = canonical_sources[0]
        stamp = str(canonical_source.get("source_hash") or "")
        if not stamp or not isinstance(canonical_source.get("source_frontmatter"), dict):
            continue
        algorithm = "sha256" if stamp.startswith("sha256:") or len(stamp) == 64 else "md5"
        digest = stamp.removeprefix(algorithm + ":")
        if digest and hashers[algorithm].hexdigest() == digest:
            return {"filename": name, "canonical_version": version, "projection_hash": projection_hash(content)}
    return {}


def current_source_publication_proven(filepath: str, expected_hash: str, index: dict | None = None) -> bool:
    """A ledger or a disk-only Source fragment is never evidence of canonical publication."""
    return bool(current_source_publication_evidence(filepath, expected_hash, index))


def _stamp_source_hash(content: str, raw_hash: str, raw_ref: str = "") -> str:
    """Record the raw content hash in the Source page's frontmatter.

    This records which raw bytes the Source was compiled from. Canonical Source
    extraction persists both this stamp and the complete admission frontmatter;
    publication proof must bind them to canonical state, not trust the disk stamp.
    Stamping the hash at finalize -- where the packet already carries it -- turns that inference
    into a comparison.

    Best effort by contract: content without frontmatter, or unparsable frontmatter, is returned
    unchanged rather than made to fail.  The page's own validation is the authority on whether it
    may be written.
    """
    if not raw_hash:
        return content
    try:
        frontmatter, body = split_frontmatter(content)
    except Exception:  # noqa: BLE001 - a page whose frontmatter will not parse is not ours to fix
        return content
    if not frontmatter:
        return content
    if frontmatter.get("source_hash") == raw_hash and not raw_ref:
        return content
    frontmatter["source_hash"] = raw_hash
    if raw_ref:
        frontmatter["sources"] = [raw_ref]
    try:
        rendered = yaml.safe_dump(
            frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
    except Exception:  # noqa: BLE001 - same
        return content
    return "---\n" + rendered + "---\n" + body


def _published_source_verdict(filepath: str, index: dict) -> str:
    """What to do with a source the publication check calls already published.

    ``"record"`` -- a page **declares** this raw path and the file cannot have changed since that
    page was written, so the missing ``processed_files`` row is written and the source is skipped.
    ``"stale"`` -- declared, but the raw file is newer than the page, so the page predates the
    current content and the source must stay pending: re-ingesting is the only way the edit is ever
    seen.  ``"skip"`` -- only the loose name signal matched (over-eager by design, so it may not
    retire a source permanently), or the page records no usable date.

    The state this exists for: a source ingested while the pipeline was stalled has a page but no
    ledger row, and the published branch runs *before* the hash comparison a row would enable.  So
    without this the ledger is permanently wrong **and** a later edit to that source is silently
    ignored -- measured on 2026-09-19, one source in exactly that state
    (``raw/医疗信息化/推动健康中国建设取得决定性进展有关情况.md``).  No raw hash is stored on the
    page, so the evidence is its ``created`` date against the file's mtime, and the ambiguous
    direction resolves to re-ingesting rather than retiring.
    """
    declared = index.get("declared_pages", {}).get(_raw_key(filepath))
    if not declared:
        return "skip"

    # A stamped hash is proof; prefer it over any inference.
    recorded_hash = str(declared.get("source_hash") or "").strip()
    if recorded_hash:
        current_hash = calculate_hash(filepath)
        if not current_hash:
            return "skip"
        return "record" if current_hash == recorded_hash else "stale"

    # Legacy pages carry no hash, so fall back to dates: compared as YYYY-MM-DD strings because
    # this module rebinds ``datetime`` to the class on the ``from datetime import datetime``
    # line, which makes ``datetime.date`` a method descriptor here.
    created_date = str(declared.get("created") or "")[:10]
    if len(created_date) != 10 or created_date[4] != "-" or created_date[7] != "-":
        return "skip"
    try:
        raw_date = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(filepath)))
    except OSError:
        return "skip"
    return "record" if raw_date <= created_date else "stale"


def calculate_hash(filepath: str, *, algorithm: str = "md5") -> str:
    hasher = hashlib.new(algorithm)
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log.error(f"Error calculating hash for {filepath}: {e}")
        return ""

def _read_purpose() -> str:
    try:
        return render_strategy_directive()
    except PurposeContractError as exc:
        log.error("Strategic purpose contract is unavailable: %s", exc)
        return "[STRATEGIC PURPOSE CONTRACT UNAVAILABLE: halt and repair purpose.md before ingesting.]"

# Bumped when the task-packet manifest or model-output protocol changes. The version forces
# queued/awaiting packets to rebuild their rendered prompt before the host claims model work.
INGEST_CONTRACT_VERSION = 10

INTEGRATION_DISPOSITIONS = {"integrated", "standalone"}
#: The narrower vocabulary an integration relation may use.  Owned by ``schema_validator`` next to
#: ``VALID_PREDICATES`` so the page-link list and this one cannot drift apart.
INTEGRATION_PREDICATES = INGEST_INTEGRATION_PREDICATES

DEFAULT_PREDICATE_SLOTS: dict[str, dict[str, str]] = {
    "concept": {
        "validates": "### 物理机制 (Mechanism)",
        "depends-on": "### 物理机制 (Mechanism)",
        "related_to": "### 产业落地与代表实例 (Implementations)",
    },
    "person": {
        "validates": "### 关键造物与历史印记 (Key Artifacts & Legacy)",
        "related_to": "### 关键造物与历史印记 (Key Artifacts & Legacy)",
        "mentions": "### 利益纽带与权力拓扑 (Affiliations & Power Topology)",
    },
    "vendor": {
        "validates": "### 核心护城河 (Moat)",
        "related_to": "### 市场占位与竞争态势 (Market & Competition)",
        "depends-on": "### 核心护城河 (Moat)",
    },
    "institution": {
        "validates": "### 机构定位与核心诉求 (Positioning & Needs)",
        "related_to": "### 机构定位与核心诉求 (Positioning & Needs)",
    },
    "product": {
        "validates": "### 部署架构与底层依赖 (Architecture & Dependencies)",
        "depends-on": "### 部署架构与底层依赖 (Architecture & Dependencies)",
        "related_to": "### 临床与管理价值流 (Clinical & Admin Value)",
    },
    "policy": {
        "validates": "### 核心约束与合规要求 (Compliance Mandates)",
        "related_to": "### 核心约束与合规要求 (Compliance Mandates)",
        "depends-on": "### 管辖范围与适用对象 (Jurisdiction & Applicability)",
    },
    "standard": {
        "validates": "### 核心约束与合规要求 (Compliance Mandates)",
        "related_to": "### 核心约束与合规要求 (Compliance Mandates)",
        "depends-on": "### 管辖范围与适用对象 (Jurisdiction & Applicability)",
    },
}
INTEGRATION_EVENT_TAGS = frozenset(INGEST_EVENT_TAGS)
INGEST_CANDIDATE_TYPES = {
    "concept", "vendor", "institution", "product", "person", "event", "policy", "standard",
    "synthesis",
}
INGEST_SEARCH_STOP_WORDS = {
    "about", "after", "ai", "also", "an", "and", "api", "are", "as", "at", "based", "be", "been", "before",
    "between", "by", "can", "compiled", "consensus", "directive", "for", "from", "generated", "has",
    "have", "here", "if", "in", "into", "is", "it", "its", "latest", "model", "more", "no", "not",
    "of", "on", "only", "or", "other", "our", "read", "should", "source", "sources", "system", "than",
    "that", "the", "their", "then", "there", "this", "through", "to", "truth", "using", "was", "were",
    "which", "while", "who", "will", "with",
}
INGEST_CHINESE_STOP_TERMS = {
    "体系", "机制", "医疗", "系统", "平台", "医院", "数据", "管理", "治理", "国家", "智能", "人工智能",
    "评估", "实验", "资本", "架构", "推理", "物理", "生成式", "临床", "模型", "技术", "应用", "方案",
    "服务", "项目", "流程", "中国", "建设", "发展", "工作", "推进", "研究", "分析", "行业", "领域",
    "报告", "实践", "探索", "创新", "现状", "未来", "全国", "卫生", "健康", "信息", "信息化", "深化",
    "提升", "规范", "关于", "通知", "意见", "标准", "中心", "方法", "指标", "评价", "能力", "问题",
    "协同", "模式", "要求", "实施", "重点", "关键", "全面", "持续",
}


def _normalise_search_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


_COLD_KB_CONTEXT = "- (no existing nodes: cold knowledge base)"


def ingest_context_and_candidates(filepath: str, max_nodes: int = 40, _cache: dict | None = None) -> tuple[str, list[dict]]:
    """The prompt's index context and the dispatch manifest, from one computation.

    They must come from the same calculation: the model is told to use only relations from the
    packet's ``integration_candidates`` manifest, so a manifest computed separately from the list
    the prompt shows would validate the model against a set it never saw.

    A cold knowledge base (no index yet, no wiki pages) has nothing to link to, so an empty
    candidate list is the correct answer.  A knowledge base whose wiki is populated but whose
    index is missing is a real projection fault and stays a hard error, because ingesting without
    the existing-node context is how duplicate entities are created.
    """
    index_path = get_index_path()
    if not index_path.exists():
        wiki_pages = [
            name
            for name in os.listdir(get_wiki_dir())
            if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")
        ] if get_wiki_dir().exists() else []
        if wiki_pages:
            raise RuntimeError(
                f"Cannot build ingest context for {filepath}: {index_path.name} is missing while "
                f"{len(wiki_pages)} wiki page(s) exist. Rebuild the index projection "
                "(`projection-rebuild-index`) before ingesting new sources."
            )
        return _COLD_KB_CONTEXT, []
    candidates = select_ingest_candidates(filepath, max_nodes, _cache=_cache)
    return _render_index_context(candidates), candidates


def _render_index_context(candidates: list[dict]) -> str:
    return "\n".join(f"- {json.dumps(candidate, ensure_ascii=False)}" for candidate in candidates)


def _read_relevant_index_context(filepath: str, max_nodes: int = 40) -> str:
    """The rendered candidate context, for callers that only need the prompt text."""
    return ingest_context_and_candidates(filepath, max_nodes)[0]


def _stratify_candidates(scored_items: list[tuple], candidate_limit: int = 40) -> list[tuple]:
    """Balance candidate selection across core entity types to prevent concept domination."""
    if len(scored_items) <= candidate_limit or candidate_limit <= 10:
        return scored_items[:candidate_limit]

    bucket_map = {
        "person": "person",
        "institution": "institution",
        "vendor": "vendor",
        "product": "product",
        "policy": "norm",
        "standard": "norm",
        "concept": "concept",
        "synthesis": "concept",
        "event": "concept",
    }
    quotas = {
        "person": 4,
        "institution": 4,
        "vendor": 3,
        "product": 3,
        "norm": 4,
        "concept": 6,
    }

    selected = []
    selected_keys = set()
    bucket_counts: dict[str, int] = defaultdict(int)

    def add_item(item):
        key = item[1]
        if key not in selected_keys:
            selected.append(item)
            selected_keys.add(key)
            ntype = str(item[2].get("type") or "").lower()
            b = bucket_map.get(ntype, "other")
            bucket_counts[b] += 1
            return True
        return False

    # Pass 1: Top N // 3 by absolute score (guarantee highest-scoring entities enter)
    top_n = max(5, candidate_limit // 3)
    for item in scored_items:
        add_item(item)
        if len(selected) >= top_n:
            break

    # Pass 2: Fulfill bucket minimum quotas
    by_bucket = defaultdict(list)
    for item in scored_items:
        if item[1] not in selected_keys:
            ntype = str(item[2].get("type") or "").lower()
            b = bucket_map.get(ntype, "other")
            by_bucket[b].append(item)

    for b, target_quota in quotas.items():
        needed = target_quota - bucket_counts[b]
        if needed > 0 and b in by_bucket:
            for item in by_bucket[b][:needed]:
                add_item(item)
                if len(selected) >= candidate_limit:
                    break
        if len(selected) >= candidate_limit:
            break

    # Pass 3: Fill remaining capacity with best overall
    for item in scored_items:
        if len(selected) >= candidate_limit:
            break
        add_item(item)

    selected.sort(key=lambda item: (-item[0], item[1]))
    return selected[:candidate_limit]


def select_ingest_candidates(filepath: str, max_nodes: int = 40, _cache: dict | None = None) -> list[dict]:
    """Source-relevant candidates from the complete index, as dispatch-manifest records."""
    index_path = get_index_path()
    try:
        # The path comes from the ingest packet, so it resolves through the same root check the
        # content readers use.  This text is scored into the prompt the model then compiles from,
        # which makes it one more way an unconstrained path could reach a published page.
        source_path = resolve_ingest_source_path(filepath)
        source_text = (
            source_path.read_text(encoding="utf-8", errors="replace")[:200_000]
            if source_path is not None
            else ""
        )
        source_stem = source_path.stem if source_path is not None else ""
        source_stem_norm = _normalise_search_text(source_stem)
        source_norm = _normalise_search_text(
            f"{source_stem} {source_text}"
        )
        source_words = Counter(
            word for word in re.findall(r"\b[a-z0-9]{2,}\b", source_text.lower())
            if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
        )
        source_acronyms = {
            token.lower() for token in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", source_text)
        }

        # Hybrid pass: pre-fetch FTS and dense vector search hits for the source stem/title
        vector_sim_map: dict[str, float] = {}
        fts_match_set: set[str] = set()
        if source_stem:
            try:
                from vector_lake.tool_search import _get_fts_search_results, _get_vector_search_results, _get_query_embedding
                fts_hits = _get_fts_search_results(source_stem, limit=30)
                for h in fts_hits:
                    if h.get("node_key"):
                        fts_match_set.add(h["node_key"])

                qvec, _ = _get_query_embedding(source_stem)
                if qvec:
                    vhits, _ = _get_vector_search_results(qvec, limit=50)
                    vector_sim_map = vhits
            except Exception as exc:
                log.debug("Hybrid candidate search pass skipped: %s", exc)

        if _cache is not None and "nodes" in _cache and "canonical_versions" in _cache:
            nodes = _cache["nodes"]
            canonical_versions = _cache["canonical_versions"]
        else:
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
            nodes = index_data.get("nodes", {})
            if not nodes:
                return []
            canonical_versions = governance_store.canonical_page_versions(set(nodes))
            if _cache is not None:
                _cache["nodes"] = nodes
                _cache["canonical_versions"] = canonical_versions

        scored = []
        wiki_dir = get_wiki_dir()
        for key, node in nodes.items():
            if str(node.get("type") or "").lower() not in INGEST_CANDIDATE_TYPES:
                continue
            filename = f"{key}.md"
            try:
                validate_wiki_filename(filename)
            except ValueError:
                continue
            target_path = wiki_dir / filename
            target_hash = canonical_versions.get(key)
            if not target_path.exists() or not target_hash:
                continue
            aliases = node.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            node_type = str(node.get("type") or "").lower()
            labels = [key.split("_", 1)[-1], node.get("title", ""), *aliases]
            score = 0
            match_reasons = set()
            title_boosted = False
            for label in labels:
                chinese_label = "".join(re.findall(r"[\u4e00-\u9fff]", str(label)))
                if chinese_label and chinese_label not in INGEST_CHINESE_STOP_TERMS:
                    is_valid_len = len(chinese_label) >= 3 or (
                        len(chinese_label) == 2 and (
                            node_type in {"person", "vendor", "institution", "product", "standard", "policy"}
                            or (source_stem_norm and chinese_label in source_stem_norm)
                        )
                    )
                    if is_valid_len and chinese_label in source_norm:
                        base_score = 180 + len(chinese_label)
                        if not title_boosted and source_stem_norm and chinese_label in source_stem_norm:
                            base_score += 100
                            match_reasons.add(f"title_affinity:{chinese_label}")
                            title_boosted = True
                        score = max(score, base_score)
                        match_reasons.add(f"exact_chinese:{chinese_label}")
                label_words = [
                    word for word in re.findall(r"\b[a-z0-9]{2,}\b", str(label).lower())
                    if word not in INGEST_SEARCH_STOP_WORDS and word not in {"concept", "vendor", "institution"}
                ]
                if not chinese_label and label_words and all(word in source_words for word in label_words):
                    term_score = (
                        160 + sum(min(len(word), 12) for word in label_words)
                        + sum(min(source_words[word], 5) for word in label_words)
                    )
                    if not title_boosted and source_stem_norm and any(w in source_stem_norm for w in label_words):
                        term_score += 100
                        match_reasons.add(f"title_affinity:{'+'.join(label_words)}")
                        title_boosted = True
                    score = max(score, term_score)
                    match_reasons.add(f"exact_terms:{'+'.join(label_words)}")
            candidate_words = {
                word for word in re.findall(
                    r"\b[a-z0-9]{2,}\b",
                    f"{node.get('title', '')} {(node.get('summary', '') or '')[:500]}".lower(),
                )
                if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
            }
            for word in source_words.keys() & candidate_words:
                score += 45 if word in source_acronyms else min(len(word), 12)
                match_reasons.add(f"overlap:{word}")
            if key in fts_match_set:
                score += 35
                match_reasons.add("fts_match")
            if key in vector_sim_map:
                sim = vector_sim_map[key]
                if sim > 0.50:
                    score += int(sim * 100)
                    match_reasons.add(f"vector_sim:{sim:.2f}")
            if score <= 0:
                continue
            scored.append((score, key, node, target_hash, sorted(match_reasons)))
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidate_limit = max(1, int(max_nodes))
        scored = _stratify_candidates(scored, candidate_limit=candidate_limit)
        candidates = []
        scan_limit = max(100, candidate_limit * 5)
        for score, key, node, target_hash, match_reasons in scored[:scan_limit]:
            try:
                projection = _read_canonical_target_content(f"{key}.md", target_hash)
            except ValueError:
                continue
            candidates.append({
                "target": f"{key}.md",
                "target_hash": target_hash,
                # The exact Markdown baseline, so the model names the *content* it saw and not
                # only the SQLite version token.  Derived from the same canonical projection the
                # finalize side re-reads, through the one :func:`projection_hash` implementation.
                "target_projection_hash": projection_hash(projection),
                "type": node.get("type", "unknown"),
                "title": node.get("title", key),
                "summary": (node.get("summary", "") or "")[:160],
                "match_score": score,
                "match_reasons": match_reasons[:8],
            })
            if len(candidates) >= candidate_limit:
                break
        return candidates
    except Exception as exc:
        raise RuntimeError(
            f"Could not build source-relevant ingest context for {filepath}: {exc}"
        ) from exc


def _updated_now(content: str) -> str:
    updated = datetime.now(timezone.utc).isoformat()
    return re.sub(r"(?m)^updated:\s*.*$", f"updated: {updated}", content, count=1)


def _read_canonical_target_content(filename: str, expected_version: str) -> str:
    """Read Markdown whose extracted entity state matches the canonical version."""
    from vector_lake.db_store import get_connection, init_db, mutation_outbox_revision_order

    candidates = []
    target_path = get_wiki_dir() / filename
    if target_path.exists():
        candidates.append(("markdown projection", target_path.read_text(encoding="utf-8")))
    init_db()
    rows = get_connection().execute(
        "SELECT payload_text FROM mutation_outbox "
        "WHERE filename = ? AND mutation_type = 'update' AND payload_text IS NOT NULL "
        "ORDER BY " + mutation_outbox_revision_order() + " LIMIT 20",
        (filename,),
    ).fetchall()
    candidates.extend(("mutation outbox", str(row["payload_text"])) for row in rows)
    seen = set()
    for _origin, content in candidates:
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            content_version = governance_store.canonical_page_version_from_content(filename, content)
        except Exception:
            continue
        if content_version == expected_version:
            return content
    raise ValueError(
        f"No canonical-aligned Markdown snapshot is available for {filename}; "
        "replay or repair its latest mutation outbox projection before integrating"
    )


def _upsert_section_relation(
    content: str,
    heading: str,
    marker: str,
    line: str,
    legacy_tokens: tuple[str, ...] = (),
) -> str:
    """Add ``line`` to the section under ``heading``, replacing what it supersedes.

    A line is superseded when it carries this relation's ``marker``.  ``legacy_tokens`` covers
    the lines written *before* markers existed: it matches only when **every** token is present,
    because the merge step deletes every match.  A caller that passes a single token as loose as
    a wikilink therefore lets the merge eat unrelated prose -- the source page did exactly that
    and replaced any hand-authored bullet that merely mentioned the target.
    """
    start = content.find(heading)
    if start < 0:
        raise ValueError(f"Integration target is missing the required section: {heading}")
    section_start = start + len(heading)
    next_pattern = r"(?m)^(?:##|###|---)\s*" if heading.startswith("###") else r"(?m)^##\s+"
    next_heading = re.search(next_pattern, content[section_start:])
    section_end = section_start + (next_heading.start() if next_heading else len(content[section_start:]))
    section = content[section_start:section_end]
    matches = []
    for relation_match in re.finditer(r"(?m)^-[^\n]*$", section):
        relation_line = relation_match.group(0)
        if marker in relation_line or (
            legacy_tokens and all(token in relation_line for token in legacy_tokens)
        ):
            matches.append(relation_match)
    if matches:
        chunks = [section[:matches[0].start()], line]
        cursor = matches[0].end()
        for duplicate in matches[1:]:
            chunks.append(section[cursor:duplicate.start()])
            cursor = duplicate.end()
        chunks.append(section[cursor:])
        merged = "".join(chunks)
        return _updated_now(content[:section_start] + merged + content[section_end:])
    insert_at = section_end
    prefix = content[:insert_at].rstrip()
    suffix = content[insert_at:].lstrip("\n")
    merged = f"{prefix}\n\n{line}\n"
    if suffix:
        merged += f"\n{suffix}"
    return _updated_now(merged)


def _candidate_manifest(processed_data: dict) -> dict[str, dict] | None:
    """The packet's candidate allowlist, or ``None`` for a pre-manifest packet.

    The two states are deliberately distinct.  An absent key is a packet built before the
    manifest existed and is tolerated while the migration runs; a present but empty list is a
    packet that says there was nothing to integrate with, and must therefore permit no
    relation at all rather than fall back to "any page".
    """
    candidates = processed_data.get("integration_candidates")
    if not isinstance(candidates, list):
        return None
    return {
        str(candidate.get("target")): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("target")
    }


def _verify_source_projection(processed_data: dict) -> None:
    """Fail when the raw source changed after the packet was built.

    ``source_projection_hash`` baselines the source the model actually read.  A raw file edited
    between dispatch and finalize means the compiled page describes text that no longer exists,
    and the ledger's reconciliation only notices on a later pass.  A packet from before the
    manifest carries no baseline and is tolerated while the migration runs.
    """
    declared = str(processed_data.get("source_projection_hash") or "")
    filepath = str(processed_data.get("filepath") or "")
    if int(processed_data.get("ingest_contract_version") or 0) >= 10:
        from vector_lake.wiki_utils import validate_raw_ingest_source_path

        raw = validate_raw_ingest_source_path(filepath)
        if not declared or calculate_hash(str(raw)) != str(processed_data.get("hash") or ""):
            raise ValueError("current ingest requires its actual raw hash and source projection baseline")
    if not declared:
        return
    text = read_ingest_item_content({"filepath": filepath}, required=True)
    actual = projection_hash(text)
    if actual != declared:
        raise ValueError(
            f"the raw source changed after the ingest packet was built for {filepath}: "
            f"source_projection_hash {declared[:12]} != {actual[:12]}"
        )


def _apply_integration_disposition(files_written: list, processed_data: dict) -> tuple[list, str]:
    """Validate semantic completion and materialize bounded source/target updates."""
    integration = processed_data.get("integration")
    if not isinstance(integration, dict):
        raise ValueError("finalize_ingest requires an integration disposition")
    disposition = str(integration.get("disposition") or "").strip().lower()
    if disposition == "duplicate":
        if files_written or not current_source_publication_proven(
            str(processed_data.get("filepath") or ""), str(processed_data.get("hash") or "")
        ):
            raise ValueError("duplicate completion requires a matching current Source publication")
        _verify_source_projection(processed_data)
        return [], disposition
    if disposition not in INTEGRATION_DISPOSITIONS:
        raise ValueError(f"integration disposition must be one of {sorted(INTEGRATION_DISPOSITIONS)}")

    files = []
    for item in files_written:
        record = dict(item)
        if "filepath" in record and not record.get("content"):
            record["content"] = read_ingest_item_content(record, required=True)
        files.append(record)

    reason = str(integration.get("reason") or "").strip()
    _verify_source_projection(processed_data)

    canonical_name = str(processed_data.get("canonical_name") or "").strip()
    source_items = [item for item in files if os.path.basename(str(item.get("filename", ""))) == canonical_name]
    if len(source_items) != 1:
        raise ValueError(f"{disposition} ingest disposition requires exactly one canonical source page: {canonical_name}")
    source_item = source_items[0]
    source_frontmatter, _ = split_frontmatter(str(source_item.get("content") or ""))
    if str(source_frontmatter.get("type") or "").lower() != "source":
        raise ValueError("canonical Source page must declare type: source")
    source_item["expected_version"] = str(processed_data.get("source_hash") or "")
    for item in files:
        if item is source_item:
            continue
        # Forced, not defaulted.  ``expected_version`` is a *write permission*: the coordinator
        # reads a supplied non-empty value as "overwrite the page at this version"
        # (``mutation_coordinator`` only version-checks the mutations that carry the key).
        # Defaulting therefore left a payload free to name any existing page, quote its version,
        # and have the ingest rewrite it with no relation, no predicate and no ``target_hash``
        # check -- the entire integration contract bypassed, on the disposition the host runner
        # hardcodes.  A submitted item is create-only unless it is the canonical source page
        # (version forced on the line above) or a validated relation target (which carries its
        # own checked version and is appended after this loop).
        if item.get("expected_version"):
            log.warning(
                "Ignoring a caller-supplied expected_version for %s: only the canonical source "
                "page and validated relation targets may name an existing version.",
                item.get("filename"),
            )
        item["expected_version"] = ""
    relations = integration.get("relations") or []
    if disposition == "standalone":
        if relations:
            raise ValueError("standalone ingest disposition cannot include integration relations")
        if len(reason) < 12:
            raise ValueError("standalone ingest disposition requires an auditable reason")
        return files, disposition
    if not isinstance(relations, list) or not relations:
        raise ValueError("integrated ingest disposition requires at least one relation")

    submitted_names = {os.path.basename(str(item.get("filename", ""))) for item in files}
    # The packet's dispatch manifest is the allowlist.  The prompt tells the model that only
    # these relations are permitted, so the check has to exist here or the sentence is a promise
    # the code does not keep -- every candidate used to be any page that merely existed.
    manifest = _candidate_manifest(processed_data)
    if manifest is None:
        # M1, fail-closed.  A packet with no manifest is one built before the contract existed,
        # and the manifest is what bounds which pages a relation may name -- accepting it would
        # restore the "any existing page" behaviour the manifest was introduced to remove.
        # ``standalone`` is unaffected: it declares no relations at all.
        # ``requeue_legacy_ingest_jobs`` rebuilds such a packet and re-dispatches it.
        raise ValueError(
            "integrated ingest requires the packet's dispatch manifest; this packet predates "
            "``integration_candidates``. Requeue it so the packet is rebuilt."
        )
    source_content = str(source_item.get("content") or "").rstrip()
    source_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
    graph_heading = "## Graph Integration"
    if graph_heading not in source_content:
        source_content += f"\n\n{graph_heading}\n"
    target_mutations = []
    seen_targets = set()
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("integration relations must be objects")
        target = os.path.basename(str(relation.get("target") or ""))
        if target != str(relation.get("target") or ""):
            raise ValueError("integration relation target must be a wiki basename")
        validate_wiki_filename(target)
        if target not in manifest:
            raise ValueError(
                f"integration target is not in the packet's candidate manifest: {target}"
            )
        if target == canonical_name or target in submitted_names or target in seen_targets:
            raise ValueError(f"integration target is duplicated or conflicts with submitted files: {target}")
        seen_targets.add(target)
        target_path = get_wiki_dir() / target
        if not target_path.exists():
            raise ValueError(f"integration target does not exist: {target}")
        target_key = target[:-3]
        actual_hash = governance_store.canonical_page_versions({target_key}).get(target_key)
        expected_hash = str(relation.get("target_hash") or "")
        if not expected_hash:
            raise ValueError(f"integration relation requires target_hash for {target}")
        if expected_hash != actual_hash:
            raise ValueError(f"integration target_hash is stale for {target}")
        if expected_hash != str(manifest[target].get("target_hash") or ""):
            raise ValueError(
                f"integration target_hash does not match the candidate manifest for {target}"
            )
        target_content = _read_canonical_target_content(target, expected_hash)
        # The content baseline the model was given, and now required: a version token cannot see
        # a projection that changed without one, which is the case this field exists for.
        declared_projection = str(relation.get("target_projection_hash") or "")
        if not declared_projection:
            raise ValueError(f"integration relation requires target_projection_hash for {target}")
        if declared_projection != projection_hash(target_content):
            raise ValueError(f"integration target_projection_hash is stale for {target}")
        predicate = str(relation.get("predicate") or "").strip()
        if predicate not in INTEGRATION_PREDICATES:
            raise ValueError(f"unsupported integration predicate: {predicate}")
        evidence = " ".join(str(relation.get("evidence") or "").split())
        if len(evidence) < 12:
            raise ValueError(f"integration evidence is too short for {target}")
        try:
            confidence = float(relation.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"integration confidence must be numeric for {target}") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"integration confidence must be in [0, 1] for {target}")
        event_date = str(relation.get("event_date") or "")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"integration event_date must be YYYY-MM-DD for {target}") from exc
        event_tag = str(relation.get("event_tag") or "").strip().strip("[]")
        if event_tag not in INTEGRATION_EVENT_TAGS:
            raise ValueError(f"unsupported integration event_tag for {target}: {event_tag}")
        relation_id = hashlib.sha256(f"{source_key}\x00{target_key}".encode("utf-8")).hexdigest()[:16]
        marker = f"<!-- vector-lake-relation:{relation_id} -->"
        source_content = _upsert_section_relation(
            source_content,
            graph_heading,
            marker,
            f"- [{predicate}:: [[{target_key}]]] {evidence} "
            f"(confidence: {confidence:.2f}) {marker}",
            # Both tokens are required: ``[[target]]`` alone also matches a hand-authored
            # line that merely mentions the target, and the merge deletes what it matches.
            # ``confidence:`` is what the pre-marker generated relation always carried, so the
            # AND keeps the dedup working on legacy lines and leaves prose alone.  The target
            # side stays on ``(Source: [[source]])``: that anchor *is* the generated shape, and
            # the bare wikilink is contained in it, so it cannot be narrowed further.
            legacy_tokens=(f"[[{target_key}]]", "confidence:"),
        )
        source_anchor = f"(Source: [[{source_key}]])"
        target_heading = (
            "## 支撑拓扑 (Supporting Topology)"
            if target.startswith("Synthesis_")
            else "## 2. 证据时间线"
        )
        target_line = (
            f"- [depends-on:: [[{source_key}]]] {evidence} {source_anchor} {marker}"
            if target.startswith("Synthesis_")
            else f"- [{event_date}] [{event_tag}] {evidence} {source_anchor} {marker}"
        )

        # Section 1 Compiled Truth slot integration (P2)
        target_type = str(manifest[target].get("type") or "").lower()
        requested_slot = str(relation.get("target_slot") or "").strip()
        from vector_lake.schema_validator import VALID_H3_SLOTS
        if requested_slot and requested_slot in VALID_H3_SLOTS.get(target_type, []):
            slot = requested_slot
        else:
            slot = DEFAULT_PREDICATE_SLOTS.get(target_type, {}).get(predicate)

        if slot and slot in target_content:
            slot_line = f"- [{predicate}:: [[{source_key}]]] {evidence} {source_anchor} {marker}"
            target_content = _upsert_section_relation(
                target_content,
                slot,
                marker,
                slot_line,
                legacy_tokens=(source_anchor,),
            )

        target_content = _upsert_section_relation(
            target_content,
            target_heading,
            marker,
            target_line,
            legacy_tokens=(source_anchor,),
        )
        target_mutations.append({
            "filename": target,
            "content": target_content,
            "expected_version": expected_hash,
        })

    source_item["content"] = _updated_now(source_content)
    return files + target_mutations, disposition


def _build_ingest_instructions(
    filepath: str,
    file_hash: str,
    canonical_name: str,
    index_context: str | None = None,
    *, purpose_context: str | None = None, source_read_path: str | None = None,
) -> str:
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
        category_path = get_extension_root() / "SCHEMA_CATEGORIES.md"
        if category_path.exists():
            schema_content += "\n\n" + category_path.read_text(encoding="utf-8")
    except OSError:
        pass
    root = get_extension_root()
    runtime = schema_snapshot()
    return render_template(
        "prompts/ingest/main.md", root=root, runtime_schema=render_schema_contract(runtime, root=root),
        max_tags=runtime["max_tags"],
        filepath=str(source_read_path or filepath), file_hash=file_hash, canonical_name=canonical_name,
        skeleton_block=parse_static_skeleton(source_read_path or filepath), schema_content=schema_content,
        index_summary=_read_relevant_index_context(filepath) if index_context is None else index_context,
        purpose_content=_read_purpose() if purpose_context is None else purpose_context,
        valid_predicates=", ".join(runtime["page_predicates"]),
        integration_predicates=", ".join(runtime["integration_predicates"]),
        event_tags=", ".join(runtime["event_tags"]),
        timeline_tags=", ".join(f"[{tag}]" for tag in runtime["event_tags"]),
        entity_template=read_template("wiki/entity.md", root=root),
        source_template=render_template(
            "wiki/source.md", root=root, filepath=str(filepath),
            file_hash=file_hash, canonical_name=canonical_name,
        ),
    )


def refresh_ingest_dispatch_payload(payload: dict) -> dict:
    """Take a fresh proposal baseline at dispatch without resetting attempt history."""
    filepath = str(payload.get("filepath") or "")
    if not filepath or calculate_hash(filepath) != str(payload.get("hash") or ""):
        raise ValueError("source changed before dispatch; refusing the stale raw-input job")
    refreshed = dict(payload)
    canonical_name = str(payload["canonical_name"])
    index_context, candidates = ingest_context_and_candidates(filepath)
    source_text = read_ingest_item_content({"filepath": filepath})
    refreshed.update(
        source_hash=governance_store.canonical_page_versions({canonical_name[:-3]}).get(canonical_name[:-3], ""),
        source_projection_hash=projection_hash(source_text),
        ingest_contract_version=INGEST_CONTRACT_VERSION,
        integration_candidates=candidates,
        instructions=_build_ingest_instructions(filepath, payload["hash"], canonical_name,
                                                index_context=index_context),
    )
    # Candidate/prompt construction reads files too. Bind every refreshed snapshot to
    # the queued raw checksum; never stamp H(A) with observations/projection from B.
    if calculate_hash(filepath) != str(payload["hash"]):
        raise ValueError("source changed during dispatch refresh; stale snapshot refused")
    queued_projection = str(payload.get("source_projection_hash") or "")
    if queued_projection and queued_projection != refreshed["source_projection_hash"]:
        raise ValueError("source projection changed during dispatch refresh; stale snapshot refused")
    return refreshed


def requeue_legacy_ingest_jobs() -> int:
    """Rebuild old prompts before claim; never rewrite a leased or finalized job."""
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    rows = conn.execute(
        "SELECT job_id, payload, task_packet_path, status FROM jobs "
        "WHERE task_type = 'ingest' AND status IN ('queued', 'failed', 'awaiting_subagent')"
    ).fetchall()
    migrations = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("ingest_contract_version") == INGEST_CONTRACT_VERSION:
            continue
        filepath = str(payload.get("filepath") or "")
        file_hash = str(payload.get("hash") or "")
        canonical_name = str(payload.get("canonical_name") or "")
        if not filepath or not file_hash or not canonical_name or not Path(filepath).exists():
            continue
        canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
        payload["source_hash"] = governance_store.canonical_page_versions({canonical_key}).get(
            canonical_key,
            "",
        )
        index_context, candidates = ingest_context_and_candidates(filepath)
        payload["instructions"] = _build_ingest_instructions(
            filepath, file_hash, canonical_name, index_context=index_context
        )
        source_text = read_ingest_item_content({"filepath": filepath})
        payload["ingest_contract_version"] = INGEST_CONTRACT_VERSION
        payload["source_projection_hash"] = projection_hash(source_text)
        payload["integration_candidates"] = candidates
        migrations.append((str(row["job_id"]), payload, str(row["task_packet_path"] or ""), row["status"]))

    if not migrations:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    applied = []
    with db_store.transaction():
        for job_id, payload, packet_path, status in migrations:
            # Awaiting work resets its handoff lease. Queued/failed keeps its state and attempts,
            # but an old packet path can survive a previous release or failure and must be retired.
            if status == "awaiting_subagent":
                cursor = conn.execute(
                    "UPDATE jobs SET payload = ?, status = 'queued', retries = 0, "
                    "error_msg = 'Legacy ingest packet rebuilt for the integration contract', "
                    "available_at = ?, updated_at = ?, task_packet_path = NULL, "
                    "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                    "WHERE job_id = ? AND status = 'awaiting_subagent'",
                    (json.dumps(payload, ensure_ascii=False), now, now, job_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE jobs SET payload = ?, updated_at = ?, task_packet_path = NULL "
                    "WHERE job_id = ? AND status = ?",
                    (json.dumps(payload, ensure_ascii=False), now, job_id, status),
                )
            if cursor.rowcount == 1:
                applied.append((packet_path, status))
    from vector_lake.subagent_handoff import remove_subagent_task

    for packet_path, _status in applied:
        if packet_path:
            try:
                remove_subagent_task(packet_path)
            except OSError:
                log.warning("Could not remove superseded ingest packet: %s", packet_path)
    return len(applied)

INGEST_IN_FLIGHT_TTL_SECONDS = 3600


def _ingest_processing_path() -> Path:
    from vector_lake.wiki_utils import get_ingest_processing_path

    return get_ingest_processing_path()


def _ingest_ref(filepath) -> str:
    """Stable per-source key: the resolved path, not the content hash.

    Keying in-flight state by content hash silently dropped a second source whose
    content happened to match an earlier file, and reported "fully synced" while
    that source was never compiled.
    """
    try:
        return str(Path(filepath).resolve())
    except OSError:
        return str(filepath)


def _read_ingest_in_flight_unlocked() -> dict:
    processing_file = _ingest_processing_path()
    try:
        with open(processing_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Discarding unreadable ingest in-flight state %s: %s", processing_file, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    now_ts = datetime.now(timezone.utc).timestamp()
    return {
        key: entry
        for key, entry in data.items()
        if isinstance(entry, dict) and now_ts - float(entry.get("since", 0)) < INGEST_IN_FLIGHT_TTL_SECONDS
    }


def _write_ingest_in_flight_unlocked(data: dict) -> None:
    processing_file = _ingest_processing_path()
    tmp_path = processing_file.with_name(processing_file.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        flush_durable(handle)
    os.replace(tmp_path, processing_file)


def _load_ingest_in_flight() -> dict:
    from filelock import FileLock

    processing_file = _ingest_processing_path()
    with FileLock(str(processing_file) + ".lock", timeout=10):
        return _read_ingest_in_flight_unlocked()


def _mark_ingest_in_flight(filepaths: list) -> None:
    from filelock import FileLock

    if not filepaths:
        return
    processing_file = _ingest_processing_path()
    now_ts = datetime.now(timezone.utc).timestamp()
    with FileLock(str(processing_file) + ".lock", timeout=10):
        # ``_read_ingest_in_flight_unlocked`` already drops TTL-expired entries.
        state = _read_ingest_in_flight_unlocked()
        for path in filepaths:
            state[_ingest_ref(path)] = {"since": now_ts, "filepath": str(path)}
        _write_ingest_in_flight_unlocked(state)


def _clear_ingest_in_flight(filepath) -> None:
    """Release the in-flight claim for one source (best effort, never fatal)."""
    from filelock import FileLock

    processing_file = _ingest_processing_path()
    try:
        with FileLock(str(processing_file) + ".lock", timeout=10):
            state = _read_ingest_in_flight_unlocked()
            if state.pop(_ingest_ref(filepath), None) is not None:
                _write_ingest_in_flight_unlocked(state)
    except Exception as exc:
        # Only affects duplicate-scheduling suppression, never correctness.
        log.warning("Could not release ingest in-flight claim for %s: %s", filepath, exc)


def _load_scan_config() -> dict:
    """Load the extension config without silently dropping the exclusion list.

    This used to swallow every failure into ``{}``, which quietly disabled
    ``exclude_paths`` and therefore re-ingested privacy-excluded raw sources.
    ``config.json`` is per-machine and untracked, so a missing file is a
    supported state -- but it must still yield the shipped defaults rather than
    an empty exclusion list.  An unreadable or malformed file stays a hard
    error (see ``wiki_utils.load_config``).
    """
    return load_config()


def prepare_ingest_batch(batch_size: int = 5, *, _progress: dict | None = None) -> str:
    """Prepare one batch; optional internal progress supports bounded event draining."""
    if _progress is not None:
        _progress.update(enqueued=0, remaining=0)
    from vector_lake.db_store import init_db

    config = _load_scan_config()

    target_dirs = [str((get_extension_root() / d).resolve()) for d in config.get("target_directories", [])]
    if not target_dirs:
        # An empty or missing list means "the active MEMORY root's raw/ directory".
        target_dirs = [str(get_raw_dir())]
    exclude_paths = config.get("exclude_paths", [])
    supported_exts = set(config.get("supported_extensions", [".md", ".txt"]))
    
    files_to_process = []
    for target_dir in target_dirs:
        folder = Path(target_dir)
        if not folder.exists(): continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.startswith('~') or file.startswith('.'): continue
                
                filepath = os.path.join(root, file)
                path_str = filepath.replace("\\", "/")
                if any(exclude in path_str for exclude in exclude_paths):
                    continue
                # The same privacy decision the raw event handler makes.  Without it this
                # scan enqueued the diary text that handler refused to trigger on.
                if is_private_raw_source(path_str):
                    continue

                if os.path.splitext(file)[1].lower() in supported_exts:
                    files_to_process.append(filepath)
                    
    from vector_lake.db_store import get_connection
    init_db()
    conn = get_connection()
    cur = conn.execute(
        "SELECT filepath, file_hash, processed_at, observed_mtime_ns, observed_size"
        " FROM processed_files"
    )
    processed = {
        row["filepath"]: {
            "hash": row["file_hash"],
            "processed_at": row["processed_at"],
            "mtime_ns": row["observed_mtime_ns"],
            "size": row["observed_size"],
        }
        for row in cur.fetchall()
    }
    
    in_flight = _load_ingest_in_flight()
    from vector_lake.db_store import abandoned_source_keys

    abandoned = abandoned_source_keys()
    pending_files = []
    skipped_abandoned: list[str] = []
    quarantined_checksums: list[str] = []
    reconciled: list[str] = []
    skipped_name_only: list[str] = []
    skipped_undated: list[str] = []
    publication_index = None  # built lazily: one frontmatter scan per call, only when needed

    for filepath in files_to_process:
        try:
            stat = os.stat(filepath)
            # A file already carrying an in-flight ingest is skipped so two workers
            # cannot compile the same source concurrently.
            if _ingest_ref(filepath) in in_flight:
                continue

            if filepath in processed:
                # The observation snapshot is the only sound "unchanged" signal.  Comparing the
                # file mtime against the row's wall-clock ``processed_at`` skipped edits whose
                # mtime fell in the same clock tick as the row (NTFS is ~15.6 ms; measured
                # 2026-09-20, a 3.9 ms inversion hid a real edit), and it silently ignored any
                # change restored with an older mtime (``cp -p``, ``git checkout``).  The content
                # hash stays the authority whenever the snapshot disagrees.
                row = processed[filepath]
                digest = str(row.get("hash") or "")
                # Published-source reconciliation also stores non-MD5 marker values.
                # Quarantine only the evidenced impossible empty-MD5/nonempty case.
                if (row.get("size") is not None and row["size"] > 0
                        and digest.lower() == "d41d8cd98f00b204e9800998ecf8427e"):
                    # Unknown provenance is neither unchanged nor a new model task. Preserve
                    # the historical row and withhold automatic recompilation until verified.
                    quarantined_checksums.append(filepath)
                    continue
                if (
                    row.get("mtime_ns") is not None
                    and row.get("size") is not None
                    and row["mtime_ns"] == stat.st_mtime_ns
                    and row["size"] == stat.st_size
                ):
                    continue

                file_hash = calculate_hash(filepath)
                matches_processed_content = file_hash == row["hash"]
                if digest.startswith("sha256:"):
                    # A trusted SHA-256 baseline must not be compared against an MD5 digest.
                    # New ingest packets still use file_hash under the existing MD5 contract.
                    sha256 = calculate_hash(filepath, algorithm="sha256")
                    if not sha256:
                        continue
                    matches_processed_content = digest == f"sha256:{sha256}"
                if matches_processed_content:
                    # Content is unchanged.  Backfill a missing snapshot so later scans can skip
                    # without hashing; rows kept by the pre-2026-09-17 writer already carry one.
                    if row.get("mtime_ns") is None or row.get("size") is None:
                        mark_file_processed(
                            filepath,
                            digest,
                            mtime_ns=stat.st_mtime_ns,
                            size=stat.st_size,
                        )
                    continue
            else:
                # No ledger row.  Normally that means a genuinely new file - but a source
                # already published under a different raw path (or ingested on a path that
                # never recorded a row) would only be duplicated by preparing it again.
                if publication_index is None:
                    publication_index = raw_publication_index()
                if raw_is_published(filepath, publication_index):
                    verdict = _published_source_verdict(filepath, publication_index)
                    if verdict == "record":
                        # The page exists, declares this raw path, and the file has not been
                        # touched since: record the missing ledger row so the accounting is
                        # accurate and a *future* edit is detected as a changed hash.
                        mark_file_processed(
                            filepath,
                            calculate_hash(filepath),
                            mtime_ns=stat.st_mtime_ns,
                            size=stat.st_size,
                        )
                        reconciled.append(filepath)
                        continue
                    if verdict == "skip":
                        # Two very different reasons share this branch, so they are counted apart:
                        # a page that *declares* the source but records no usable date, and a
                        # match on the loose name signal only.  The second is the over-eager
                        # duplicate-prevention trade from ``_already_published`` -- deliberate,
                        # but it skips the source on every scan, so it must not be silent
                        # (independent review, 2026-09-19).
                        if _raw_key(filepath) not in publication_index.get("declared_pages", {}):
                            skipped_name_only.append(filepath)
                        else:
                            skipped_undated.append(filepath)
                        log.info("Skipping raw source already published: %s", filepath)
                        continue
                    # "stale": the page predates the file's current content.  Fall through and
                    # treat the source as pending, so the edit is ingested rather than ignored.
                    log.info(
                        "Raw source changed since it was published; re-ingesting: %s", filepath
                    )
                file_hash = calculate_hash(filepath)

            if file_hash:
                # Keyed on content: a corrected source has a new hash and is dispatchable again
                # without an operator clearing anything.
                if (str(filepath), str(file_hash)) in abandoned:
                    skipped_abandoned.append(filepath)
                    continue
                pending_files.append((filepath, file_hash))
        except OSError:
            log.warning("Skipping unreadable raw source: %s", filepath)

    if quarantined_checksums:
        log.warning("Withheld %d source(s) with invalid historical checksums; ledger unchanged",
                    len(quarantined_checksums))

    if not pending_files:
        notes: list[str] = []
        if quarantined_checksums:
            notes.append(f"{len(quarantined_checksums)} source(s) quarantined for invalid historical checksums; "
                         "verify original publication provenance before repairing or redispatching.")
        if skipped_name_only:
            log.info(
                "Skipped %d source(s) matched only by the loose page-name signal: %s",
                len(skipped_name_only),
                ", ".join(sorted(os.path.basename(p) for p in skipped_name_only)[:3]),
            )
            notes.append(
                f"{len(skipped_name_only)} source(s) skipped on the page-name signal alone "
                "(no page declares them); see `vector_lake.tool_ingest._already_published`."
            )
        if skipped_undated:
            notes.append(
                f"{len(skipped_undated)} declared source(s) whose page records no usable date."
            )
        if reconciled:
            log.info(
                "Recorded %d published source(s) that had a page but no processed_files row: %s",
                len(reconciled), ", ".join(sorted(os.path.basename(p) for p in reconciled)[:3]),
            )
            notes.append(
                f"Recorded {len(reconciled)} already-published source(s) that had no "
                "processed_files row."
            )
        if skipped_abandoned:
            log.info(
                "Not dispatching %d abandoned source(s) whose content keeps failing: %s",
                len(skipped_abandoned), ", ".join(sorted(os.path.basename(p) for p in skipped_abandoned)[:3]),
            )
            notes.append(
                f"{len(skipped_abandoned)} source(s) abandoned after repeated deterministic "
                "failures (inspect with `cli.py ingest-tasks --abandoned`, re-dispatch with "
                "`--clear-abandoned`)."
            )
        if notes:
            return "No new files to ingest. " + " ".join(notes)
        return "No new files to ingest. System is fully synced."

    pending_count = len(pending_files)
    pending_files = pending_files[:batch_size]
    if _progress is not None:
        _progress["remaining"] = pending_count - len(pending_files)
    _mark_ingest_in_flight([filepath for filepath, _ in pending_files])

    from vector_lake.db_store import enqueue_job
    enqueued_count = 0
    last_payload = None
    batch_cache: dict = {}
    # The batch is marked in-flight up front so a concurrent scan cannot schedule the same
    # source twice.  That leaves a window: if one file's enqueue raises, the loop aborts and
    # every file marked *after* it keeps a marker with no job behind it, so the source is
    # skipped for the whole TTL while looking scheduled.  Measured on 2026-09-19: 5 sources in
    # that state.  Release the markers of everything that never got enqueued.
    enqueued_paths: set = set()

    try:
        for filepath, file_hash in pending_files:
            try:
                canonical_name = canonical_source_name(filepath)
                canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
                source_hash = governance_store.canonical_page_versions({canonical_key}).get(canonical_key, "")
                # One computation supplies both the prompt's candidate list and the dispatch
                # manifest, so the model can only ever name a candidate it was shown.
                index_context, candidates = ingest_context_and_candidates(str(filepath), _cache=batch_cache)
                instructions = _build_ingest_instructions(
                    filepath, file_hash, canonical_name, index_context=index_context
                )
                source_text = read_ingest_item_content({"filepath": str(filepath)})

                payload = {
                    "filepath": str(filepath),
                    "hash": file_hash,
                    "canonical_name": canonical_name,
                    "source_hash": source_hash,
                    "instructions": instructions,
                    "ingest_contract_version": INGEST_CONTRACT_VERSION,
                    "source_projection_hash": projection_hash(source_text),
                    "integration_candidates": candidates,
                }

                # ``supersede_finished`` is safe here and only here: this point in the scan is
                # reached after ``raw_is_published`` said the source has no page, so a finished
                # job for it is a contradiction (page and ledger disagree) rather than work to
                # preserve.  Without it such a source loops forever: enqueue returns the finished
                # job's id, the file is marked in-flight, nothing is dispatched, and the next
                # sweep releases the marker and tries again.
                enqueue_job("ingest", payload, replace_terminal=True, supersede_finished=True)
                enqueued_paths.add(_ingest_ref(filepath))
                enqueued_count += 1
                last_payload = payload
            except Exception:
                _clear_ingest_in_flight(filepath)
                log.exception("Failed to enqueue ingest work for %s", filepath)
                raise
    finally:
        unenqueued = [
            _ingest_ref(filepath)
            for filepath, _ in pending_files
            if _ingest_ref(filepath) not in enqueued_paths
        ]
        for ref in unenqueued:
            _clear_ingest_in_flight(ref)

    if _progress is not None:
        _progress["enqueued"] = enqueued_count
    if batch_size == 1 and enqueued_count == 1:
        return json.dumps(last_payload)

    abandoned_note = (
        f" {len(skipped_abandoned)} abandoned source(s) were not dispatched."
        if skipped_abandoned else ""
    )
    reconciled_note = (
        f" Recorded {len(reconciled)} already-published source(s) that had no processed_files row."
        if reconciled else ""
    )
    return (
        f"Successfully enqueued {enqueued_count} files for ingestion."
        f"{abandoned_note}{reconciled_note}"
    )

def auto_heal_tag_collisions(files: list[dict], index_path: Path | None = None) -> list[dict]:
    """Auto-heal tag collisions in submitted ingest files by converting colliding tags into semantic links.

    When an author or subagent model emits a tag that matches an existing entity's title or alias
    (e.g. `tags: [电子病历评级]` when `Policy_电子病历系统功能应用水平分级评价` declares that alias),
    the schema validator's tag collision gate would normally fail the ingest.
    Instead of hard-failing the job, this step removes the colliding tag from frontmatter
    and appends `[related_to:: [[Target_Key]]]` to Section 1 if not already present.
    """
    index_path = index_path or get_index_path()
    if not index_path.exists():
        return files

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        nodes = index_data.get("nodes", {})
        if not nodes:
            return files

        collision_map: dict[str, str] = {}
        for node_id, node_data in nodes.items():
            title = str(node_data.get("title") or "").strip().lower()
            if title:
                collision_map[title] = node_id
            for alias in node_data.get("aliases") or []:
                al = str(alias).strip().lower()
                if al:
                    collision_map[al] = node_id
    except Exception as exc:
        log.warning("Could not build tag collision map for auto-healing: %s", exc)
        return files

    healed_files = []
    for item in files:
        record = dict(item)
        content = record.get("content")
        if not content and "filepath" in record:
            content = read_ingest_item_content(record)
        if not content:
            healed_files.append(record)
            continue

        try:
            frontmatter, body = split_frontmatter(content)
        except Exception:
            healed_files.append(record)
            continue

        tags = frontmatter.get("tags")
        if not isinstance(tags, list) or not tags:
            healed_files.append(record)
            continue

        colliding_entries = []
        clean_tags = []
        for tag in tags:
            clean_tag = str(tag).strip().lstrip("#").lower()
            if clean_tag in collision_map:
                colliding_entries.append((tag, collision_map[clean_tag]))
            else:
                clean_tags.append(tag)

        if not colliding_entries:
            healed_files.append(record)
            continue

        frontmatter["tags"] = clean_tags
        # Append semantic links to Section 1 if not already present
        for tag_str, target_key in colliding_entries:
            target_link = f"[[{target_key}]]"
            if target_link not in body:
                sec1_match = re.search(r"(## 1\. 编译事实.*?\n)(### |\Z|---)", body, re.DOTALL)
                if sec1_match:
                    link_line = f"- [related_to:: {target_link}] (converted from tag #{str(tag_str).strip().lstrip('#')})\n"
                    insert_pos = sec1_match.start(2)
                    body = body[:insert_pos].rstrip() + f"\n{link_line}\n" + body[insert_pos:]
                else:
                    body = body.rstrip() + f"\n\n- [related_to:: {target_link}]\n"

            log.info(
                "Auto-healed tag collision in %s: converted tag [%s] into semantic link to %s",
                record.get("filename"),
                tag_str,
                target_key,
            )

        yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
        record["content"] = f"---\n{yaml_block}---\n{body.lstrip()}"
        healed_files.append(record)

    return healed_files


def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalizes an ingest operation from a subagent using direct data."""
    from vector_lake.wiki_utils import SafeWriteError
    from vector_lake.db_store import finalize_ingest_job, validate_ingest_job_finalization
    from vector_lake.schema_validator import SchemaViolationException
    from vector_lake.defense_hook import DefenseHookException

    try:
        # Defense: parse string inputs if passed as JSON from CLI or loose MCP clients
        if isinstance(files_written, str):
            try:
                files_written = json.loads(files_written)
            except json.JSONDecodeError as exc:
                raise ValueError(f"files_written string is not valid JSON: {exc}")
        if not isinstance(files_written, list):
            raise ValueError(f"files_written must be a list, got {type(files_written).__name__}")

        if isinstance(processed_data, str):
            try:
                processed_data = json.loads(processed_data)
            except json.JSONDecodeError as exc:
                raise ValueError(f"processed_data string is not valid JSON: {exc}")
        if not isinstance(processed_data, dict):
            raise ValueError(f"processed_data must be a dict, got {type(processed_data).__name__}")

        # Defense: unwrap nested {"metadata": {"processed_data": ...}} or {"processed_data": ...}
        if "processed_data" in processed_data and isinstance(processed_data["processed_data"], dict):
            processed_data = processed_data["processed_data"]
        elif "metadata" in processed_data and isinstance(processed_data["metadata"], dict):
            inner = processed_data["metadata"].get("processed_data")
            if isinstance(inner, dict):
                processed_data = inner

        files = auto_heal_tag_collisions(files_written)
        job_id = processed_data.get("job_id")
        if not job_id:
            raise ValueError("finalize_ingest requires a claimed job_id")
        job_row = validate_ingest_job_finalization(str(job_id), processed_data)
        lease_owner = str(processed_data.get("lease_owner") or "")
        lease_token = str(processed_data.get("lease_token") or "")
        lease_generation = int(processed_data.get("lease_generation"))
        duplicate_evidence = None
        if str((processed_data.get("integration") or {}).get("disposition") or "").lower() == "duplicate":
            duplicate_evidence = current_source_publication_evidence(
                str(processed_data.get("filepath") or ""), str(processed_data.get("hash") or "")
            )
            if not duplicate_evidence:
                raise ValueError("duplicate completion requires current canonical Source publication")
        files, integration_disposition = _apply_integration_disposition(files, processed_data)
        recompile = (job_row.get("parsed_payload") or {}).get("controlled_recompile")
        contract = load_purpose_contract()
        node_records = validate_ingest_payload(files, contract)
                
        wiki_dir = get_wiki_dir()

        # Stamp the raw content hash onto the canonical Source page.  Done here rather than by
        # the model: the value has to be the packet's own hash, and a model-authored hash would
        # be one more thing to verify.
        canonical_name = str(processed_data.get("canonical_name") or "")
        raw_hash = str(processed_data.get("hash") or "")
        if canonical_name and raw_hash:
            item, deviation = _source_item_to_stamp(
                files, canonical_name, str(processed_data.get("filepath") or "")
            )
            if item is not None:
                content = read_ingest_item_content(item)
                if content:
                    from vector_lake.wiki_utils import get_memory_dir

                    raw_path = resolve_ingest_source_path(str(processed_data.get("filepath") or ""))
                    if raw_path is None:
                        raise ValueError("canonical Source provenance escaped the raw root")
                    raw_ref = raw_path.relative_to(get_memory_dir().resolve()).as_posix()
                    item["content"] = _stamp_source_hash(content, raw_hash, raw_ref)
                if deviation:
                    # Reported, not corrected: renaming a published page would break every link and
                    # ``sources:`` reference pointing at it, and the reconciliation is keyed on the
                    # declaration, so a deviation is a naming-convention slip and not a lost source.
                    log.warning(
                        "Source page %s was written instead of the mandated %s for %s; the source "
                        "hash is stamped on the page that declares the raw file",
                        deviation,
                        canonical_name,
                        processed_data.get("filepath"),
                    )

        written_paths = []
        mutations = []
        for item in files:
            fname = os.path.basename(item["filename"])
            if "filepath" in item and not item.get("content"):
                # ``required``: an unreadable source must fail the finalize rather than be
                # published as an empty canonical page.
                item["content"] = read_ingest_item_content(item, required=True)
            fcontent = item["content"]
            
            if "Concept_Decision_" in fname:
                lower_content = fcontent.lower()
                if not all(k in lower_content for k in ["context", "alternatives", "justification"]):
                    raise SafeWriteError(f"Decision nodes like {fname} MUST contain 'context', 'alternatives', and 'justification'.")

            mutation = {"filename": fname, "content": fcontent}
            if "expected_version" in item:
                mutation["expected_version"] = item["expected_version"]
            mutations.append(mutation)
            written_paths.append(str(wiki_dir / fname))

        filepath = processed_data["filepath"]
        file_hash = processed_data["hash"]
        # The observation snapshot travels with the ledger row so the next scan can skip an
        # unchanged file without hashing.  A source that has since been removed reports no
        # observation and the row keeps whatever it already had.
        try:
            _stat = os.stat(filepath)
            snapshot = {"mtime_ns": _stat.st_mtime_ns, "size": _stat.st_size}
        except OSError:
            snapshot = {}
        if mutations:
            from vector_lake.mutation_coordinator import execute_mutation_batch

            def mark_ingest_processed():
                # Own Source mutation changed its version; recheck the lease/dispatch and raw
                # bytes without comparing that new Source against its pre-write version.
                validate_ingest_job_finalization(str(job_id), processed_data, check_source_version=False)
                _verify_source_projection(processed_data)
                mark_file_processed(filepath, file_hash, **snapshot)
                finalize_ingest_job(
                    str(job_id),
                    lease_owner,
                    lease_token,
                    lease_generation,
                    result_data={"integration": processed_data.get("integration"),
                                 **({"controlled_recompile": recompile} if recompile else {})},
                )

            def verify_commit_boundary():
                validate_ingest_job_finalization(str(job_id), processed_data)
                _verify_source_projection(processed_data)

            execute_mutation_batch(
                mutations,
                canonical_callback=mark_ingest_processed,
                canonical_precondition=verify_commit_boundary,
            )
        else:
            from vector_lake.db_store import transaction
            from vector_lake.mutation_coordinator import projection_locks

            if integration_disposition != "duplicate" or not duplicate_evidence:
                raise ValueError("successful ingest requires a canonical Source mutation")
            with projection_locks([duplicate_evidence["filename"]]), transaction():
                validate_ingest_job_finalization(str(job_id), processed_data)
                _verify_source_projection(processed_data)
                current = current_source_publication_evidence(filepath, file_hash)
                if current != duplicate_evidence:
                    raise ValueError("Canonical Source version conflict during duplicate completion")
                mark_file_processed(filepath, file_hash, **snapshot)
                finalize_ingest_job(
                    str(job_id), lease_owner, lease_token, lease_generation,
                    result_data={"integration": processed_data.get("integration"),
                                 "publication": duplicate_evidence},
                )

        if recompile:
            from vector_lake.controlled_recompile import remove_source_snapshot
            try:
                remove_source_snapshot(job_row["parsed_payload"]["source_read_path"])
            except (OSError, ValueError) as exc:
                log.warning("Ingest finalized, but source snapshot cleanup failed: %s", type(exc).__name__)
        task_packet_path = job_row.get("task_packet_path") if job_row else None
        if task_packet_path:
            try:
                from vector_lake.subagent_handoff import remove_subagent_task

                remove_subagent_task(task_packet_path)
            except Exception as exc:
                log.warning("Ingest finalized, but task packet cleanup failed: %s", exc)
        
        _clear_ingest_in_flight(filepath)
        
        proposal_count = 0
        if written_paths:
            try:
                # Include existing nodes sharing the newly observed tension target,
                # so independently ingested sources can converge on one proposal.
                candidate_records = list(node_records)
                target_names = {
                    str(edge.get("target", "")).strip()
                    for record in node_records
                    for edge in record.get("tension_edges", [])
                    if isinstance(edge, dict) and str(edge.get("target", "")).strip()
                }
                if target_names:
                    with open(get_index_path(), "r", encoding="utf-8") as handle:
                        index_nodes = json.load(handle).get("nodes", {}).values()
                    for node in index_nodes:
                        edges = node.get("tension_edges", [])
                        if any(isinstance(edge, dict) and edge.get("target") in target_names for edge in edges):
                            candidate_records.append({
                                "filename": node.get("id", ""),
                                "sources": node.get("sources", []),
                                "tension_edges": edges,
                            })
                for proposal in build_synthesis_proposals(candidate_records, contract):
                    governance_store.enqueue_governance_item(
                        proposal["type"], proposal["title"], proposal["description"],
                        ", ".join(proposal["sources"]), proposal["search_queries"], proposal["affected_pages"],
                    )
                    proposal_count += 1
            except Exception as exc:
                log.warning("Ingest completed, but Synthesis-Proposal evaluation failed: %s", exc)

        suffix = f" Queued {proposal_count} Synthesis-Proposal(s)." if proposal_count else ""
        return (
            f"Successfully finalized ingestion for {filepath}. "
            f"Integration disposition: {integration_disposition}.{suffix}"
        )
    except (ValueError, SafeWriteError, PurposeContractError, SchemaViolationException, DefenseHookException) as e:
        # Caller-actionable rejection: the payload is wrong and can be resubmitted.
        return f"Error finalizing ingestion: {e}"
    except Exception:
        # Infrastructure fault (database, outbox, projection, health gate).  These
        # used to be flattened into the same "Error finalizing ingestion" string,
        # so a broken store looked like a rejected payload.  Propagate instead.
        log.exception("finalize_ingest infrastructure failure")
        raise
