"""Bounded read-only control-plane observations; never a write gate or repair."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid

from . import ops_files

READ_COLUMNS = {
    "jobs": {"status", "created_at", "lease_until"},
    "mutation_outbox": {"status", "created_at", "lease_until"},
    "page_index_nodes": {"node_key"}, "vec_embeddings": {"page_key"},
    "vec_embedding_inputs": {"node_key"}, "claims": {"claim_id"},
    "claim_index": {"claim_id"}, "timeline_events": {"claim_id"},
    "governance_queue": set(), "wiki_search_index": set(),
    "operational_memory_gram_dirty": {"doc"}, "operational_memory_index": {"rowid"},
    "operational_memory_gram_docs": {"doc", "memory_id"},
    "tantivy_mirror_queue": set(),
}

# Age is diagnostic only: it does not establish that an unknown status is terminal.
# Older unknown rows remain unknown; only source-defined terminal states are retired.
UNRECOGNIZED_LEGACY_SECONDS = 86400

PROJECTIONS = ("memory_gram", "vectors", "page_projection", "fts_index", "tantivy_mirror", "claim_index", "timeline_events", "governance_queue")
STATES = frozenset({"idle", "processing", "recovering", "error", "halted", "starting", "stopped", "running", "paused", "disabled", "unknown"})
JOB_STATES = frozenset({"queued", "dispatched", "awaiting_subagent", "processing", "completed", "failed", "finalizing", "finalized", "cancelled", "superseded", "subagent_processing"})
OUTBOX_STATES = frozenset({"pending", "processing", "retrying", "completed", "failed", "superseded"})


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _age(value, now: datetime, *, future_tolerance_seconds: float = 5.0) -> int | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        age = (now - date).total_seconds()
        return None if age < -future_tolerance_seconds else max(0, int(age))
    except ValueError:
        return None


def write_public_heartbeat(meta_dir: Path, kind: str, *, state: str, task_queue_size=0, index_queue_size=0, shadow=None) -> None:
    """Producer-side allowlist. No command, owner, source, error text or private state."""
    if kind not in {"watchdog", "runner"}:
        raise ValueError("unsupported heartbeat kind")
    payload = {"schema_version": 1, "status": state if isinstance(state, str) and state in STATES else "unknown", "updated_at": _utc().isoformat()}
    for key, value in (("task_queue_size", task_queue_size), ("index_queue_size", index_queue_size)):
        payload[key] = value if type(value) is int and value >= 0 else None
    if type(shadow) is bool:
        payload["shadow"] = shadow
    filename = f"{kind}_ops.json"
    with ops_files.directory(meta_dir) as parent:
        ops_files.mkdir_child(parent, "runtime")
        with ops_files.directory(Path(meta_dir) / "runtime") as runtime:
            ops_files.atomic_public(runtime, filename, f".{filename}.{uuid.uuid4().hex}.tmp", json.dumps(payload).encode("utf-8"))


def _heartbeat(path: Path, now: datetime) -> dict:
    # Deliberately do not fall back to legacy runner_status/.watchdog_status.
    try:
        raw = ops_files.read_public(path, 8193)
        if len(raw) > 8192:
            return {"state": "unknown", "reason": "heartbeat_too_large"}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
            return {"state": "unknown", "reason": "unsupported_heartbeat"}
        state, age = data.get("status"), _age(data.get("updated_at"), now)
        if not isinstance(state, str) or state not in STATES or age is None:
            return {"state": "unknown", "reason": "invalid_heartbeat"}
        if any(key in data and data[key] is not None and (type(data[key]) is not int or data[key] < 0) for key in ("task_queue_size", "index_queue_size")) or "shadow" in data and type(data["shadow"]) is not bool:
            return {"state": "unknown", "reason": "invalid_heartbeat"}
        report = {"state": "degraded" if state in {"error", "halted", "stopped", "unknown"} or age > 120 else "healthy", "status": state, "age_seconds": age}
        for key in ("task_queue_size", "index_queue_size"):
            value = data.get(key)
            if type(value) is int and value >= 0:
                report[key] = value
        if type(data.get("shadow")) is bool:
            report["shadow"] = data["shadow"]
        return report
    except FileNotFoundError:
        return {"state": "unknown", "reason": "public_heartbeat_missing"}
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
        return {"state": "unknown", "reason": "public_heartbeat_unreadable"}


def collect_status(db_path: str | Path, *, meta_dir: str | Path | None = None, budget_seconds: float = 2.0) -> dict:
    """Observe a caller-selected DB. No init_db, config, env file, payload or repair.

    SQLite read locks/SHM coordination may occur; no canonical DDL/DML is issued.
    This is not an atomic snapshot of DB, heartbeat files and filesystem projections.
    """
    if not 0 < budget_seconds <= 30:
        raise ValueError("budget_seconds must be in (0, 30]")
    started = time.monotonic()
    deadline = started + budget_seconds
    now = _utc()
    database = Path(db_path).expanduser().absolute()
    metadata = Path(meta_dir).expanduser().absolute() if meta_dir is not None else database.parent
    report = {"schema_version": 1, "observed_at": now.isoformat(), "read_only": True,
              "scope": "aggregate metadata only; no content/provider/physical projection proof",
              "database": {"state": "unknown"}, "jobs": {"state": "unknown"},
              "outbox": {"state": "unknown"}, "projections": {name: {"state": "unknown", "reason": "not_observed"} for name in PROJECTIONS}}
    report["runtime"] = {kind: _heartbeat(metadata / "runtime" / f"{kind}_ops.json", now) for kind in ("watchdog", "runner")}
    conn = None
    cache = {}
    object_types = {}

    def authorize(action, table, column, database_name, trigger):
        if action == sqlite3.SQLITE_READ:
            if table in {"sqlite_master", "sqlite_schema"}:
                return sqlite3.SQLITE_OK if column in {"", "name", "type"} else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if object_types.get(table) == "table" and table in READ_COLUMNS and column in READ_COLUMNS[table] | {""} else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and table == "table_xinfo" and column in READ_COLUMNS:
            return sqlite3.SQLITE_OK
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and column in {"count", "min", "max", "julianday", "ops_age_seconds"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def scalar(sql: str, params=()):
        if conn is None:
            raise sqlite3.ProgrammingError("read-only connection unavailable")
        key = (sql, tuple(params))
        if key not in cache:
            if time.monotonic() >= deadline:
                raise TimeoutError
            try:
                cache[key] = conn.execute(sql, params).fetchone()[0]
            except sqlite3.Error as exc:
                cache[key] = exc
        value = cache[key]
        if isinstance(value, sqlite3.Error):
            raise value
        return value

    def probe(function):
        try:
            return function()
        except TimeoutError:
            return {"state": "unknown", "reason": "budget_exceeded"}
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            reason = "budget_exceeded" if time.monotonic() >= deadline or code == sqlite3.SQLITE_INTERRUPT else "database_query_unavailable"
            return {"state": "unknown", "reason": reason, "error_type": type(exc).__name__}

    def queue(table: str, allowed: frozenset, terminal: frozenset, leased: frozenset):
        if conn is None:
            raise sqlite3.ProgrammingError("read-only connection unavailable")
        if object_types.get(table) != "table":
            raise sqlite3.DatabaseError("unsupported queue object type")
        rows = conn.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status").fetchall()
        counts = {key: 0 for key in sorted(allowed)}
        for state, count in rows:
            key = state if state in allowed else "other"
            counts[key] = counts.get(key, 0) + count
        # Backlog counts recognised non-terminal work only; unrecognised rows are reported
        # separately (see ``unrecognized``) instead of being added to the waiting-work figure.
        backlog = sum(count for state, count in counts.items() if state not in terminal and state != "other")
        work = sorted(allowed - terminal)
        work_predicate = f"status IN ({','.join('?' for _ in work)})"
        age = scalar(f"SELECT MAX(ops_age_seconds(created_at)) FROM {table} WHERE {work_predicate}", work)
        placeholders = ",".join("?" for _ in leased)
        parameters = [*sorted(leased), now.isoformat()]
        expired = scalar(f"SELECT COUNT(*) FROM {table} WHERE status IN ({placeholders}) AND (julianday(lease_until) IS NULL OR julianday(lease_until)<=julianday(?))", parameters)
        active = scalar(f"SELECT COUNT(*) FROM {table} WHERE status IN ({placeholders}) AND julianday(lease_until)>julianday(?)", parameters)
        invalid_times = scalar(f"SELECT COUNT(*) FROM {table} WHERE {work_predicate} AND ops_age_seconds(created_at) IS NULL", work)
        # NULL belongs to the same 'other' set as every unsupported status. Never infer
        # completion from age. Superseded jobs are explicit terminals in db_store.enqueue_job.
        known = ",".join("?" for _ in allowed)
        unknown_predicate = f"(status IS NULL OR status NOT IN ({known}))"
        unrecognized = scalar(f"SELECT COUNT(*) FROM {table} WHERE {unknown_predicate}", sorted(allowed))
        unrecognized_age = scalar(f"SELECT MAX(ops_age_seconds(created_at)) FROM {table} WHERE {unknown_predicate}", sorted(allowed))
        recent_unrecognized = scalar(f"SELECT COUNT(*) FROM {table} WHERE {unknown_predicate} AND (ops_age_seconds(created_at) IS NULL OR ops_age_seconds(created_at)<?)", [*sorted(allowed), UNRECOGNIZED_LEGACY_SECONDS])
        treated_as_legacy = False
        state = ("degraded" if expired or counts.get("failed", 0) or recent_unrecognized
                 else "unknown" if unrecognized or backlog and (age is None or invalid_times) else "healthy")
        result = {"state": state, "counts": counts, "backlog": backlog, "oldest_age_seconds": age,
                  "leases": {"active": active, "expired_or_invalid": expired},
                  "unrecognized": {"count": unrecognized, "oldest_age_seconds": unrecognized_age,
                                   "recent_count": recent_unrecognized, "treated_as_legacy": treated_as_legacy}}
        if backlog and (age is None or invalid_times):
            result["reason"] = "backlog_age_unavailable"
        elif unrecognized:
            result["reason"] = "unrecognized_status_semantics"
        return result

    def count_projection(table, authority, field):
        actual, expected = scalar(f"SELECT COUNT(*) FROM {table}"), scalar(f"SELECT COUNT(*) FROM {authority}")
        return {"state": "degraded" if actual < expected else "unknown", field: actual, "authority_count": expected, "reason": "count_shortfall" if actual < expected else "counts_do_not_prove_key_coverage"}

    try:
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=min(.25, budget_seconds))
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute(f"PRAGMA busy_timeout={min(250, max(1, int(budget_seconds * 1000)))}")
        # Fixed pure timestamp parser, not an arbitrary callback or filesystem capability.
        conn.create_function("ops_age_seconds", 1, lambda value: _age(value, now, future_tolerance_seconds=0), deterministic=True)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("BEGIN")
        object_types.update(dict(conn.execute("SELECT name,type FROM sqlite_master")))
        conn.set_authorizer(authorize)
        for table, columns in READ_COLUMNS.items():
            if object_types.get(table) == "table":
                try:
                    metadata_columns = conn.execute(f"PRAGMA main.table_xinfo('{table}')").fetchall()
                except sqlite3.Error:
                    object_types[table] = "unsupported_schema"
                    continue
                if any(row[1] in columns and row[6] in (2, 3) for row in metadata_columns):
                    object_types[table] = "unsupported_generated_columns"
        scalar("SELECT COUNT(*) FROM sqlite_master")
        report["database"] = {"state": "healthy", "scope": "readable snapshot; not integrity_check"}
        report["jobs"] = probe(lambda: queue("jobs", JOB_STATES, frozenset({"completed", "failed", "finalized", "cancelled", "superseded"}), frozenset({"dispatched", "processing", "awaiting_subagent", "subagent_processing"})))
        report["outbox"] = probe(lambda: queue("mutation_outbox", OUTBOX_STATES, frozenset({"completed", "failed", "superseded"}), frozenset({"processing"})))
        p = report["projections"]
        p["fts_index"] = probe(lambda: count_projection("wiki_search_index", "page_index_nodes", "indexed_rows"))
        p["claim_index"] = probe(lambda: {"state": "degraded" if (missing := scalar("SELECT COUNT(*) FROM claims c WHERE NOT EXISTS (SELECT 1 FROM claim_index i WHERE i.claim_id=c.claim_id)")) else "healthy", "missing": missing, "scope": "key coverage only"})
        p["timeline_events"] = probe(lambda: {"state": "degraded" if (orphans := scalar("SELECT COUNT(*) FROM timeline_events e WHERE e.claim_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id=e.claim_id)")) else "healthy", "orphaned": orphans, "scope": "claim reference coverage only"})
        p["governance_queue"] = probe(lambda: {"state": "unknown", "items": scalar("SELECT COUNT(*) FROM governance_queue"), "reason": "count_not_topology_proof"})
        p["memory_gram"] = probe(lambda: {"state": "degraded" if (dirty := scalar("SELECT COUNT(*) FROM operational_memory_gram_dirty d WHERE EXISTS (SELECT 1 FROM operational_memory_gram_docs g JOIN operational_memory_index i ON i.memory_id=g.memory_id WHERE g.doc=d.doc)")) else "unknown", "live_dirty": dirty, "reason": "index_format_not_verified"})
        p["page_projection"] = {"state": "degraded" if report["outbox"].get("backlog", 0) else "unknown", "outbox_backlog": report["outbox"].get("backlog"), "reason": "filesystem_generation_not_verified"}
        p["tantivy_mirror"] = probe(lambda: {"state": "unknown", "queued": scalar("SELECT COUNT(*) FROM tantivy_mirror_queue"), "reason": "mirror_token_not_verified"})

        def vectors():
            # Load only the packaged runtime extension; never an operator-supplied path.
            import sqlite_vec
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            missing = scalar("SELECT COUNT(*) FROM page_index_nodes n WHERE NOT EXISTS (SELECT 1 FROM vec_embeddings e WHERE e.page_key=n.node_key)")
            orphaned = scalar("SELECT COUNT(*) FROM vec_embeddings e WHERE NOT EXISTS (SELECT 1 FROM page_index_nodes n WHERE n.node_key=e.page_key)")
            unstamped = scalar("SELECT COUNT(*) FROM vec_embeddings e WHERE NOT EXISTS (SELECT 1 FROM vec_embedding_inputs i WHERE i.node_key=e.page_key)")
            return {"state": "degraded" if missing or orphaned or unstamped else "healthy", "missing": missing, "orphaned": orphaned, "unstamped": unstamped, "scope": "key/stamp coverage, not content freshness"}

        try:
            p["vectors"] = probe(vectors)
        except ImportError:
            p["vectors"] = {"state": "unknown", "reason": "vector_extension_unavailable"}
    except (sqlite3.Error, TimeoutError) as exc:
        report["database"] = {"state": "unknown", "reason": "database_unavailable", "error_type": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()
    states = [report["database"]["state"], report["jobs"]["state"], report["outbox"]["state"], *(value["state"] for value in report["projections"].values()), *(value["state"] for value in report["runtime"].values())]
    report["state"] = "degraded" if "degraded" in states else "unknown" if "unknown" in states else "healthy"
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    report["budget_exhausted"] = time.monotonic() >= deadline
    return report


def render_status(report: dict) -> str:
    lines = [f"Operational status: {report['state']} (read-only aggregate observations)"]
    for name in ("jobs", "outbox"):
        item = report[name]
        lines.append(f"{name}: {item['state']} backlog={item.get('backlog', 'unknown')} oldest_age_s={item.get('oldest_age_seconds', 'unknown')} leases={item.get('leases', 'unknown')}")
    for name, item in report["runtime"].items():
        lines.append(f"{name}: {item['state']} status={item.get('status', 'unknown')} age_s={item.get('age_seconds', 'unknown')}")
    for name, item in report["projections"].items():
        lines.append(f"{name}: {item['state']} {item.get('reason', item.get('scope', ''))}")
    return "\n".join(lines)
