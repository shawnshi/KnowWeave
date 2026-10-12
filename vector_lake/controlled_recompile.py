"""Opt-in, fingerprint-bound current-version recompilation; never used by sync.

The CLI is a local operator entry, not an authentication boundary. A hash binds
an approval artifact; it does not prove its public-data assertion. Unknown or
private classifications are deliberately unsupported by this entry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from vector_lake.path_safety import is_link_or_junction

TASK_TYPE = "ingest_recompile"
REQUEST_TYPE = "ingest_recompile_request"
VERSION = 2
COUNT = 60
MAX_BYTES = 32 * 1024 * 1024
PRIVATE = {"privacy", "diary", "garmin", "stocks", "personal-insights"}
STATES = {"no_proven_current_publication", "quarantine_without_current_version_proof"}


def _decode_json(data, label: str):
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid {label} JSON") from exc


def _json_file(path: str, digest: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Approval/plan requires its exact SHA-256")
    p = Path(path)
    if is_link_or_junction(p) or p.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Invalid or oversized approval artifact")
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Approval artifact changed")
    result = _decode_json(data, "approval artifact")
    if not isinstance(result, dict):
        raise ValueError("Approval artifact must be an object")
    return result


def raw_path(value: str) -> Path:
    from vector_lake.wiki_utils import get_raw_dir
    from vector_lake.tool_ingest import _load_scan_config

    root = Path(os.path.abspath(get_raw_dir()))
    p = Path(os.path.abspath(value))
    try:
        relative = p.relative_to(root)
    except ValueError as exc:
        raise ValueError("Recompile target escapes raw root") from exc
    if not relative.parts or any(part.startswith((".", "~")) or part.casefold() in PRIVATE for part in relative.parts):
        raise ValueError("Recompile target is excluded")
    # Inspect every lexical ancestor before any resolve/open; don't follow links.
    for candidate in (p, *p.parents):
        if is_link_or_junction(candidate):
            raise ValueError("Recompile target traverses a link/junction")
    config = _load_scan_config()
    if p.suffix.lower() not in config.get("supported_extensions", [".md", ".txt"]):
        raise ValueError("Unsupported recompile source")
    if any(str(excluded).casefold() in p.as_posix().casefold() for excluded in config.get("exclude_paths", [])):
        raise ValueError("Recompile target is excluded by scan policy")
    return p


def fingerprint(value: str) -> dict:
    p = raw_path(value)
    before = p.stat()
    if before.st_size > 8 * 1024 * 1024 or not p.is_file():
        raise ValueError("Recompile source exceeds the byte budget")
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with p.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            sha.update(block)
            md5.update(block)
    after = p.stat()
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
        raise ValueError("Recompile source changed while fingerprinting")
    return {"filepath": str(p), "sha256": sha.hexdigest(), "md5": md5.hexdigest(),
            "mtime_ns": before.st_mtime_ns, "size": before.st_size}


def load_scope(plan_path: str, plan_sha256: str, approval_path: str, approval_sha256: str) -> dict:
    plan = _json_file(plan_path, plan_sha256)
    approval = _json_file(approval_path, approval_sha256)
    request_id = approval.get("request_id")
    if approval.get("version") not in {1, VERSION} or type(approval.get("version")) is not int:
        raise ValueError("Unsupported recompile approval version")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        raise ValueError("Recompile approval requires a bounded request_id")
    if approval.get("plan_sha256") != plan_sha256 or approval.get("read_scope") != "public_only":
        raise ValueError("Recompile approval must bind the plan and a public-only read boundary")
    rows = plan.get("rows")
    entries = approval.get("entries")
    version = approval["version"]
    if (not isinstance(rows, list) or not isinstance(entries, list)
            or not 1 <= len(entries) <= COUNT
            or not all(isinstance(row, dict) for row in rows + entries)):
        raise ValueError("Recompile scope requires a bounded nonempty approval whitelist")
    all_paths = [str(raw_path(str(row.get("filepath") or ""))) for row in rows]
    selected = [row for row in rows if row.get("state") in STATES]
    if len(set(all_paths)) != len(rows) or len(selected) != len(entries):
        raise ValueError("Recompile selection differs from the approved scope")
    if version == 1 and (len(rows) != 78 or len(entries) != COUNT
            or sum(row.get("state") == "no_proven_current_publication" for row in rows) != 39
            or sum(row.get("state") == "quarantine_without_current_version_proof" for row in rows) != 21
            or sum(row.get("state") == "preserve_rejection" for row in rows) != 18):
        raise ValueError("Legacy recompile scope must preserve its frozen 78/60/18 boundary")
    if version == VERSION and len(selected) != len(rows):
        raise ValueError("Every new plan row must belong to the exact approved scope")
    from vector_lake.wiki_utils import canonical_source_name
    if len({canonical_source_name(row["filepath"]).casefold() for row in selected}) != len(selected):
        raise ValueError("Selected sources collide on canonical Source filenames")
    expected = {}
    for row in selected:
        p = raw_path(str(row.get("filepath") or ""))
        digest = str(row.get("current_sha256") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or str(p) in expected:
            raise ValueError("Invalid/duplicate frozen source fingerprint")
        expected[str(p)] = row
    approved = {}
    for entry in entries:
        p = raw_path(str(entry.get("filepath") or ""))
        evidence = entry.get("classification_evidence")
        if (entry.get("classification") != "public" or not isinstance(evidence, str)
                or not 12 <= len(evidence) <= 1000 or str(p) in approved):
            raise ValueError("Every source needs an explicit, evidenced public-data classification; unknown/private is blocked")
        approved[str(p)] = entry
    if set(expected) != set(approved):
        raise ValueError("Approval whitelist differs from the exact selected sources")
    started, total, sources = time.monotonic(), 0, []
    for p, row in expected.items():
        current = fingerprint(p)
        total += current["size"]
        digest = str(row["current_sha256"]).removeprefix("sha256:")
        if (current["sha256"] != digest or approved[p].get("sha256") != digest
                or current["md5"] != row.get("current_md5")
                or current["size"] != row.get("size") or current["mtime_ns"] != row.get("mtime_ns")):
            raise ValueError("A selected current version drifted; rebuild the approved scope")
        if total > MAX_BYTES or time.monotonic() - started > 80:
            raise ValueError("Recompile verification exceeded its bounded budget")
        sources.append({**current, "old_ledger": row.get("old_ledger")})
    return {"version": version, "request_id": request_id, "plan_sha256": plan_sha256,
            "approval_sha256": approval_sha256, "read_scope": "public_only", "sources": sources}


def _check_source_target(filepath: str, payload: dict) -> None:
    from vector_lake.db_store import get_connection
    from vector_lake.governance_store import canonical_page_versions
    from vector_lake.wiki_utils import canonical_source_name, get_memory_dir

    name = canonical_source_name(filepath)
    if payload.get("canonical_name") != name:
        raise ValueError("Recompile canonical Source target differs from its raw source")
    # Metadata only, never private canonical body/candidate context. A shared or
    # unidentified existing Source is not authorized for replacement.
    rows = get_connection().execute(
        "SELECT json_extract(data_json, '$.sources') AS refs FROM entities WHERE f_page_key = ?",
        (name[:-3],),
    ).fetchall()
    if canonical_page_versions({name[:-3]}).get(name[:-3]) and not rows:
        raise ValueError("Existing Source ownership projection is missing")
    for row in rows:
        refs = _decode_json(row["refs"] or "null", "Source ownership")
        if not isinstance(refs, list) or not refs:
            raise ValueError("Existing Source ownership is not proven")
        for ref in refs:
            if not isinstance(ref, str):
                raise ValueError("Existing Source ownership is not proven")
            p = Path(ref)
            if not p.is_absolute():
                p = get_memory_dir() / p
            if os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(filepath)):
                raise ValueError("Existing Source is shared or belongs to another raw source")


def _snapshot_path(value: str) -> Path:
    from vector_lake import get_extension_root
    root = Path(os.path.abspath(get_extension_root() / "scratch" / "controlled-recompile-inputs"))
    p = Path(os.path.abspath(value))
    if not p.is_relative_to(root) or p.parent == root:
        raise ValueError("Recompile snapshot escapes its managed task directory")
    for candidate in (p, *p.parents):
        if is_link_or_junction(candidate):
            raise ValueError("Recompile snapshot traverses a link/junction")
    return p


def _snapshot_hash(value: str) -> str:
    p = _snapshot_path(value)
    if not p.is_file() or p.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Invalid recompile source snapshot")
    sha = hashlib.sha256()
    with p.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            sha.update(block)
    return sha.hexdigest()


def create_source_snapshot(source: dict) -> str:
    from vector_lake import get_extension_root
    p = _snapshot_path(str(get_extension_root() / "scratch" / "controlled-recompile-inputs" / uuid.uuid4().hex / Path(source["filepath"]).name))
    p.parent.mkdir(parents=True)
    sha, total = hashlib.sha256(), 0
    try:
        with raw_path(source["filepath"]).open("rb") as incoming, p.open("xb") as outgoing:
            for block in iter(lambda: incoming.read(65536), b""):
                total += len(block)
                if total > 8 * 1024 * 1024:
                    raise ValueError("Recompile snapshot exceeds its byte budget")
                sha.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        current = fingerprint(source["filepath"])
        if sha.hexdigest() != source["sha256"] or current != {k: source[k] for k in current}:
            raise ValueError("Current source drifted while preparing its approved snapshot")
        p.chmod(stat.S_IREAD)
        return str(p)
    except BaseException:
        if p.exists():
            p.unlink()
        p.parent.rmdir()
        raise


def remove_source_snapshot(value: str) -> None:
    p = _snapshot_path(value)
    if p.exists():
        p.chmod(stat.S_IWRITE | stat.S_IREAD)
        p.unlink()
        p.parent.rmdir()


def current_version_proven(filepath: str, sha256: str, md5: str) -> bool:
    from vector_lake.tool_ingest import current_source_publication_proven

    return current_source_publication_proven(filepath, md5)


def _check_source_state(source: dict, *, check_current_publication: bool = True) -> None:
    from vector_lake.db_store import get_connection
    ledger = get_connection().execute("SELECT * FROM processed_files WHERE filepath = ?", (source["filepath"],)).fetchone()
    if (dict(ledger) if ledger else None) != source["old_ledger"]:
        raise ValueError("Selected source ledger drifted before completion")
    if check_current_publication and current_version_proven(source["filepath"], source["sha256"], source["md5"]):
        raise ValueError("Selected source now has a current-version finalization; reassess the frozen scope")


def validate_marker(payload: dict, *, task_type: str, require_snapshot: bool = True,
                    check_source_version: bool = True) -> dict | None:
    """Check durable request membership and bytes; caller also checks its lease."""
    marker = payload.get("controlled_recompile")
    if task_type != TASK_TYPE:
        if marker is not None:
            raise ValueError("Ordinary ingest cannot carry a recompile exception")
        return None
    if not isinstance(marker, dict) or set(marker) != {"version", "request_job_id", "request_id", "plan_sha256", "approval_sha256", "sha256"}:
        raise ValueError("Controlled ingest requires its complete authorization marker")
    from vector_lake.db_store import get_connection
    row = get_connection().execute("SELECT * FROM jobs WHERE job_id = ?", (marker["request_job_id"],)).fetchone()
    if row is None or row["task_type"] != REQUEST_TYPE or row["status"] != "completed":
        raise ValueError("Recompile request is missing or revoked")
    request = _decode_json(row["payload"], "durable recompile request")
    if (not isinstance(request, dict) or not isinstance(request.get("sources"), list)
            or not 1 <= len(request["sources"]) <= COUNT
            or not all(isinstance(source, dict) and {"filepath", "sha256", "md5", "mtime_ns", "size"} <= set(source) for source in request["sources"])):
        raise ValueError("Invalid durable recompile request")
    for key in ("version", "request_id", "plan_sha256", "approval_sha256"):
        if marker.get(key) != request.get(key):
            raise ValueError("Recompile request binding mismatch")
    if type(marker["version"]) is not int or marker["version"] not in {1, VERSION} or request.get("read_scope") != "public_only":
        raise ValueError("Recompile request boundary mismatch")
    p = raw_path(str(payload.get("filepath") or ""))
    matches = [s for s in request["sources"] if s["filepath"] == str(p)]
    if len(matches) != 1 or (marker["version"] == 1 and len(request["sources"]) != COUNT):
        raise ValueError("Recompile target is outside its approved whitelist")
    approved = matches[0]
    if str(p) in _deferred_sources(get_connection(), row["job_id"], request):
        raise ValueError("Controlled source is explicitly deferred, not authorized for model reading")
    # Post-write callback sees our own publication. Keep raw, authorization and
    # ledger checks, but fence competing publication before mutation only.
    _check_source_state(approved, check_current_publication=check_source_version)
    if marker["sha256"] != approved["sha256"] or payload.get("hash") != approved["md5"]:
        raise ValueError("Recompile content binding mismatch")
    current = fingerprint(str(p))
    if current != {k: approved[k] for k in current}:
        raise ValueError("Recompile current version changed")
    if payload.get("integration_candidates") != []:
        raise ValueError("Controlled recompile cannot read unrelated Wiki candidate context")
    _check_source_target(str(p), payload)
    if check_source_version:
        from vector_lake.governance_store import canonical_page_versions
        name = payload["canonical_name"][:-3]
        current_version = canonical_page_versions({name}).get(name, "")
        if current_version != payload.get("source_hash"):
            raise ValueError("Controlled Source version drifted before completion")
    if require_snapshot:
        if not isinstance(payload.get("source_read_path"), str) or _snapshot_hash(payload["source_read_path"]) != marker["sha256"]:
            raise ValueError("Recompile source snapshot does not match its approved current version")
    return marker


DEFERRAL_TYPE = "ingest_recompile_deferral"


def _defer_target(scope: dict, filepath: str | None, sha256: str | None) -> dict | None:
    if filepath is None and sha256 is None:
        return None
    if not filepath or not sha256:
        raise ValueError("Deferral requires both filepath and approved SHA-256")
    p = str(raw_path(filepath))
    matches = [source for source in scope["sources"] if source["filepath"] == p]
    if len(matches) != 1 or sha256 != matches[0]["sha256"]:
        raise ValueError("Deferral is outside the approved current-version whitelist")
    current = fingerprint(p)
    if current != {k: matches[0][k] for k in current}:
        raise ValueError("Deferred source current version changed")
    return matches[0]


def _deferred_sources(conn, request_job: str, scope: dict) -> set[str]:
    # Locate the durable identity before trusting any mutable fields on that row.
    # Revocation/corruption must not look like absence or be revived by old flags.
    row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?",
                       ("controlled-deferral:" + scope["request_id"],)).fetchone()
    if row is None:
        return set()
    if row["task_type"] != DEFERRAL_TYPE or row["status"] != "completed":
        raise ValueError("Controlled deferral is revoked or its identity is damaged")
    value = _decode_json(row["payload"], "controlled deferral")
    expected_fields = {"version", "request_id", "request_job_id", "plan_sha256",
                       "approval_sha256", "filepath", "sha256", "reason"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("Invalid controlled deferral binding")
    members = [s for s in scope["sources"] if s["filepath"] == value["filepath"]]
    if (type(value["version"]) is not int or value["version"] != scope["version"]
            or value["request_job_id"] != request_job
            or value["request_id"] != scope["request_id"]
            or value["plan_sha256"] != scope["plan_sha256"]
            or value["approval_sha256"] != scope["approval_sha256"]
            or len(members) != 1 or value["sha256"] != members[0]["sha256"]
            or value["reason"] != "operator_deferred_unproven_source_ownership"):
        raise ValueError("Invalid controlled deferral binding")
    return {value["filepath"]}


def dispatch_scope(scope: dict, *, batch_size: int = 1,
                   defer_filepath: str | None = None, defer_sha256: str | None = None) -> dict:
    """One bounded native packet handoff; no model calls or Wiki writes here.

    Separate job kind is invisible to old Runner claims. Failed controlled jobs
    are re-handed off only by this explicit operator entry, never by sync.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= COUNT:
        raise ValueError("Recompile batch must be between 1 and 60")
    from vector_lake import db_store, governance_store
    from vector_lake.output_contract import build_output_contract
    from vector_lake.ingest_worker import _subagent_ingest_prompt
    from vector_lake.native_llm import create_subagent_task, remove_subagent_task
    from vector_lake.tool_ingest import INGEST_CONTRACT_VERSION, _build_ingest_instructions
    from vector_lake.wiki_utils import canonical_source_name, projection_hash

    target = _defer_target(scope, defer_filepath, defer_sha256)
    db_store.init_db()
    conn = db_store.get_connection()
    made, snapshots, job_ids = [], [], []
    started = time.monotonic()
    try:
        with db_store.transaction():
            request_key = "controlled-request:" + scope["request_id"]
            existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (request_key,)).fetchone()
            if existing:
                if existing["payload"] != json.dumps(scope, ensure_ascii=False) or existing["status"] != "completed":
                    raise ValueError("Recompile request identity already bound to another or revoked scope")
                request_job = existing["job_id"]
            else:
                request_job = db_store.enqueue_job(REQUEST_TYPE, scope, idempotency_key=request_key)
                db_store.update_job_status(request_job, "completed")
            deferred = _deferred_sources(conn, request_job, scope)
            if target is not None:
                p = target["filepath"]
                if deferred and p not in deferred:
                    raise ValueError("A different source is already deferred for this request")
                if p not in deferred:
                    _check_source_state(target)
                    if conn.execute("SELECT 1 FROM jobs WHERE task_type IN ('ingest', 'ingest_recompile') "
                                    "AND json_extract(payload, '$.filepath') = ? "
                                    "AND status IN ('queued','failed','dispatched','awaiting_subagent','subagent_processing') LIMIT 1", (p,)).fetchone():
                        raise ValueError("Cannot defer a source with unfinished native work")
                    try:
                        _check_source_target(p, {"canonical_name": canonical_source_name(p)})
                    except ValueError as exc:
                        if str(exc) != "Existing Source ownership is not proven":
                            raise
                    else:
                        raise ValueError("Deferral requires the unproven-ownership gate")
                    record = {k: scope[k] for k in ("version", "request_id", "plan_sha256", "approval_sha256")}
                    record.update(request_job_id=request_job, filepath=p, sha256=target["sha256"],
                                  reason="operator_deferred_unproven_source_ownership")
                    deferral = db_store.enqueue_job(DEFERRAL_TYPE, record,
                        idempotency_key="controlled-deferral:" + scope["request_id"])
                    db_store.update_job_status(deferral, "completed")  # Registration, never a source verdict.
                    deferred = _deferred_sources(conn, request_job, scope)
            for source in scope["sources"]:
                if source["filepath"] in deferred:
                    continue  # Keep ambiguous Source, raw, ledger and historic receipts.
                if len(job_ids) >= batch_size:
                    break
                if time.monotonic() - started > 120:
                    raise ValueError("Recompile handoff exceeded its write budget")
                p = source["filepath"]
                key = "controlled-source:" + scope["request_id"] + ":" + hashlib.sha256(p.encode()).hexdigest()
                existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
                if existing and existing["status"] not in {"failed", "queued"}:
                    continue  # Never reset leases, successful receipts, or active work.
                if existing and int(existing["retries"] or 0) >= db_store.MAX_INGEST_ATTEMPTS:
                    continue
                if existing and str(existing["available_at"] or "") > datetime.now(timezone.utc).isoformat():
                    continue
                active = conn.execute(
                    "SELECT 1 FROM jobs WHERE task_type IN ('ingest', 'ingest_recompile') "
                    "AND (status IN ('dispatched','awaiting_subagent','subagent_processing') "
                    "OR (status IN ('queued','failed') AND retries < ?)) "
                    "AND json_extract(payload, '$.filepath') = ? AND job_id != ? LIMIT 1",
                    (db_store.MAX_INGEST_ATTEMPTS, p, existing["job_id"] if existing else ""),
                ).fetchone()
                if active:
                    raise ValueError("A selected source already has active native work")
                spent = conn.execute(
                    "SELECT COALESCE(SUM(retries), 0) FROM jobs WHERE task_type = 'ingest_recompile' "
                    "AND json_extract(payload, '$.filepath') = ? AND json_extract(payload, '$.hash') = ?",
                    (p, source["md5"]),
                ).fetchone()[0]
                if spent >= db_store.MAX_INGEST_ATTEMPTS:
                    continue  # Changing request identity must not reset the same-content budget.
                _check_source_state(source)  # Every handoff, including same-request retries.
                name = canonical_source_name(p)
                prior_payload = _decode_json(existing["payload"], "prior controlled dispatch") if existing else None
                if prior_payload is not None:
                    # Retrying this request does not authorize a new Source baseline.
                    validate_marker(prior_payload, task_type=TASK_TYPE)
                marker = {k: scope[k] for k in ("version", "request_id", "plan_sha256", "approval_sha256")}
                marker.update(request_job_id=request_job, sha256=source["sha256"])
                payload = {"filepath": p, "hash": source["md5"], "canonical_name": name,
                           "source_hash": prior_payload["source_hash"] if prior_payload is not None else governance_store.canonical_page_versions({name[:-3]}).get(name[:-3], ""),
                           "ingest_contract_version": INGEST_CONTRACT_VERSION,
                           "integration_candidates": [], "controlled_recompile": marker}
                validate_marker(payload, task_type=TASK_TYPE, require_snapshot=False)
                payload["source_read_path"] = create_source_snapshot(source)
                snapshots.append(payload["source_read_path"])
                # Dedicated, hash-checked managed snapshot path: never widen the
                # generic raw-only resolver to permit arbitrary external files.
                text = _snapshot_path(payload["source_read_path"]).read_text(encoding="utf-8")
                payload["source_projection_hash"] = projection_hash(text)
                instructions = _build_ingest_instructions(
                    p, source["md5"], name,
                    index_context="No Wiki candidate context is authorized for this current-version recompilation. Return standalone with a canonical Source page for every eligible source, even empty or outside-focus material; never invent integration targets.",
                    purpose_context="Focus on medical digital transformation, AI-native hospitals, and AI mechanisms with a supported connection to those goals. Use only the supplied source evidence; do not infer private user history.",
                    source_read_path=payload["source_read_path"],
                )
                instructions = (
                    "CONTROLLED SOURCE READ: read only the source_read_path snapshot supplied in metadata. "
                    "processed_data.filepath and the Source template's sources value identify original provenance only; do not open the mutable original raw file.\n"
                    + instructions
                )
                validate_marker(payload, task_type=TASK_TYPE)
                job_id = existing["job_id"] if existing else db_store.enqueue_job(TASK_TYPE, payload, idempotency_key=key)
                if existing:
                    conn.execute("UPDATE jobs SET payload = ? WHERE job_id = ?", (json.dumps(payload, ensure_ascii=False), job_id))
                processed = {**payload, "job_id": job_id}
                task_path = create_subagent_task("ingest", _subagent_ingest_prompt(instructions),
                                                "JSON object with files_written and integration; host calls finalize_ingest",
                                                {"job_id": job_id, "processed_data": processed, "finalize_tool": "finalize_ingest", "output_contract": build_output_contract()})
                made.append(task_path)
                db_store.mark_job_awaiting_subagent(job_id, str(task_path))
                job_ids.append(job_id)
        return {"request_job_id": request_job, "selected": len(scope["sources"]), "deferred": len(deferred), "enqueued": len(job_ids), "job_ids": job_ids, "state": "DISPATCHED_NOT_COMPLETE" if job_ids else "NO_NEW_HANDOFF"}
    except BaseException:
        for path in made:
            remove_subagent_task(path)
        for path in snapshots:
            remove_source_snapshot(path)
        raise


def controlled_recompile(plan_path: str, plan_sha256: str, approval_path: str, approval_sha256: str,
                         *, apply: bool = False, batch_size: int = 1,
                         defer_filepath: str | None = None, defer_sha256: str | None = None) -> dict:
    scope = load_scope(plan_path, plan_sha256, approval_path, approval_sha256)
    _defer_target(scope, defer_filepath, defer_sha256)
    if not apply:
        return {"selected": len(scope["sources"]), "preserved_rejections": 18 if scope["version"] == 1 else 0, "request_id": scope["request_id"],
                "plan_sha256": plan_sha256, "approval_sha256": approval_sha256,
                "state": "VALIDATED_NOT_DISPATCHED", "production_writes": False}
    return dispatch_scope(scope, batch_size=batch_size, defer_filepath=defer_filepath, defer_sha256=defer_sha256)
