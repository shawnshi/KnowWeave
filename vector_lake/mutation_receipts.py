"""Read-only observations of native mutation intents; never repair or complete rows."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from vector_lake import db_store, governance_store
from vector_lake.wiki_utils import get_wiki_dir

MAX_IDS = 100
MAX_HOPS = 32
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024


class ReadBudget:
    def __init__(self):
        self.deadline = time.monotonic() + 10
        self.bytes = 0

    def check(self, size=0):
        self.bytes += size
        if time.monotonic() >= self.deadline:
            raise TimeoutError("receipt_read_deadline_exceeded")
        if self.bytes > MAX_SCAN_BYTES:
            raise ValueError("receipt_read_budget_exceeded")


@contextmanager
def readonly_snapshot(budget=None):
    """An existing database only: no init_db, migrations, writable connection or retries."""
    path = Path(db_store.get_db_path()).resolve()
    if not path.is_file():
        raise FileNotFoundError("canonical_database_missing")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    budget = budget or ReadBudget()
    try:
        conn.set_progress_handler(lambda: int(time.monotonic() >= budget.deadline), 1000)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def canonical_snapshot(conn, *, domain="", cluster="", page_key=None, budget=None):
    """Return native CAS tokens and exact scopes, rejecting malformed or oversized state."""
    budget = budget or ReadBudget()
    cursor = conn.execute(
        "SELECT entity_id, f_page_key, "
        "CASE WHEN length(CAST(data_json AS BLOB))<=4194304 THEN data_json END AS data_json FROM entities"
    ) if page_key is None else conn.execute(
        "SELECT entity_id, f_page_key, "
        "CASE WHEN length(CAST(data_json AS BLOB))<=4194304 THEN data_json END AS data_json "
        "FROM entities WHERE f_page_key=?", (page_key,),
    )
    rows_by_page = {}
    scopes = {}
    total = 0
    for count, row in enumerate(cursor):
        raw = row["data_json"]
        if not isinstance(raw, str):
            raise ValueError("invalid_canonical_record")
        size = len(raw.encode("utf-8"))
        total += size
        budget.check(size)
        if count >= 50000 or total > MAX_SCAN_BYTES:
            raise ValueError("canonical_scan_budget_exceeded")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_canonical_json") from exc
        if not isinstance(data, dict) or data.get("entity_id") != row["entity_id"]:
            raise ValueError("invalid_canonical_entity_binding")
        key = data.get("page_key")
        if not isinstance(key, str) or not key or key != row["f_page_key"]:
            raise ValueError("invalid_canonical_page_binding")
        if any(data.get(field) is not None and not isinstance(data[field], str) for field in ("domain", "topic_cluster")):
            raise ValueError("invalid_canonical_scope")
        scope = (data.get("domain") or "", data.get("topic_cluster") or "")
        if key in scopes and scopes[key] != scope:
            raise ValueError("ambiguous_canonical_scope")
        scopes[key] = scope
        rows_by_page.setdefault(key, []).append((str(row["entity_id"]), raw))
    selected = {key: rows for key, rows in rows_by_page.items()
                if (not domain or scopes[key][0] == domain) and (not cluster or scopes[key][1] == cluster)}
    return {key: governance_store._canonical_entity_rows_version(rows) for key, rows in selected.items()}


def _safe_filename(filename):
    if (not isinstance(filename, str) or not filename.endswith(".md")
            or "/" in filename or "\\" in filename or ":" in filename
            or Path(filename).name != filename or filename in {".md", "..md"}):
        raise ValueError("invalid_outbox_filename")
    return filename


def _intent(conn, outbox_id, budget):
    # CASE bounds payload transfer before materialization in Python.
    row = conn.execute(
        "SELECT id,filename,mutation_type,status,projection_generation,base_version,superseded_by,validation_mode, "
        "CASE WHEN length(CAST(payload_text AS BLOB))<=? THEN payload_text END AS payload_text, "
        "length(CAST(payload_text AS BLOB)) AS payload_size FROM mutation_outbox WHERE id=?",
        (MAX_PAYLOAD_BYTES, outbox_id),
    ).fetchone()
    if row is not None and (row["payload_size"] or 0) > MAX_PAYLOAD_BYTES:
        raise ValueError("outbox_payload_budget_exceeded")
    budget.check((row["payload_size"] or 0) if row is not None else 0)
    return dict(row) if row is not None else None


def _equivalent(left, right):
    fields = ("filename", "mutation_type", "payload_text", "base_version", "validation_mode")
    return all(left[key] == right[key] for key in fields)


def _observe(conn, outbox_id, max_hops, budget):
    report = {"outbox_id": outbox_id, "status": "unknown", "completed": False,
              "canonical": "unknown", "projection": "unknown", "index": "unknown", "successor_chain": []}
    original = _intent(conn, outbox_id, budget)
    if original is None:
        return dict(report, status="missing", reason="outbox_intent_missing")
    report.update(status=original["status"], filename=_safe_filename(original["filename"]),
                  canonical="committed", projection_generation=original["projection_generation"])
    current = original
    seen = {outbox_id}
    while current["status"] == "superseded":
        successor_id = current["superseded_by"]
        if len(report["successor_chain"]) >= max_hops:
            return dict(report, reason="successor_hop_limit")
        if type(successor_id) is not int or successor_id <= 0 or successor_id in seen:
            return dict(report, reason="invalid_successor_chain")
        successor = _intent(conn, successor_id, budget)
        if successor is None:
            return dict(report, reason="successor_missing")
        if not _equivalent(current, successor):
            return dict(report, reason="non_equivalent_successor")
        if (type(successor["projection_generation"]) is not int
                or type(current["projection_generation"]) is not int
                or successor["projection_generation"] <= current["projection_generation"]):
            return dict(report, reason="invalid_successor_generation")
        seen.add(successor_id)
        report["successor_chain"].append(successor_id)
        current = successor
    report.update(effective_outbox_id=current["id"], effective_status=current["status"])
    if type(current["projection_generation"]) is not int or current["projection_generation"] <= 0:
        return dict(report, reason="invalid_current_generation")
    if current["validation_mode"] not in {"full", "schema"}:
        return dict(report, reason="invalid_validation_mode")
    latest = conn.execute(
        "SELECT id,projection_generation FROM mutation_outbox WHERE filename=? "
        "ORDER BY projection_generation DESC,id DESC LIMIT 1", (current["filename"],)
    ).fetchone()
    if latest is None or (latest["id"], latest["projection_generation"]) != (current["id"], current["projection_generation"]):
        return dict(report, reason="not_current_intent")
    key = Path(current["filename"]).stem
    versions = canonical_snapshot(conn, page_key=key, budget=budget)
    version = versions.get(key, "")
    mutation_type, payload = current["mutation_type"], current["payload_text"]
    if current["base_version"] is None:
        return dict(report, reason="unbound_canonical_target")
    if mutation_type not in {"update", "delete"}:
        return dict(report, reason="invalid_mutation_type")
    if mutation_type == "update":
        if not isinstance(payload, str) or not version:
            return dict(report, reason="canonical_target_missing")
        payload_version = governance_store.canonical_page_version_from_content(current["filename"], payload)
        if payload_version != current["base_version"]:
            return dict(report, reason="payload_version_mismatch")
    elif payload is not None:
        return dict(report, reason="invalid_delete_payload")
    if version != current["base_version"] or (mutation_type == "delete" and version):
        return dict(report, reason="canonical_target_changed")
    report["canonical"] = "current_target"
    path = get_wiki_dir() / current["filename"]
    if path.is_symlink():
        return dict(report, reason="projection_symlink_refused")
    if mutation_type == "delete":
        matched = not path.exists()
    else:
        if not path.exists():
            matched = False
        else:
            with path.open("rb") as handle:
                raw = handle.read(MAX_PAYLOAD_BYTES + 1)
            budget.check(len(raw))
            if len(raw) > MAX_PAYLOAD_BYTES:
                return dict(report, reason="projection_budget_exceeded")
            # Mirrors the native consumer's text read (universal newlines).
            matched = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") == payload
    report["projection"] = "materialized" if matched else "deferred"
    if current["status"] == "failed":
        return dict(report, index="failed", reason="native_intent_failed")
    if current["status"] in {"pending", "processing"}:
        return dict(report, index="pending")
    if current["status"] != "completed":
        return dict(report, reason="unrecognized_native_status")
    if not matched:
        return dict(report, reason="completed_projection_drift")
    return dict(report, completed=True, index="completed", reason="native_completed_current_target")


def get_mutation_status(outbox_ids: list[int], *, max_hops: int = MAX_HOPS) -> dict:
    """Observe 1-100 native receipts; superseded remains distinct from equivalent completion.

    Completion covers the native outbox's indexing contract, not remote embedding
    availability. DB state is one snapshot; filesystem observations are not atomic.
    No payload, lease capabilities or raw dependency error text is returned.
    """
    if (not isinstance(outbox_ids, list) or not 1 <= len(outbox_ids) <= MAX_IDS
            or any(type(item) is not int or item <= 0 for item in outbox_ids)):
        raise ValueError("outbox_ids must contain 1-100 positive integers")
    if type(max_hops) is not int or not 1 <= max_hops <= MAX_HOPS:
        raise ValueError("max_hops must be between 1 and 32")
    report = {"read_only": True, "state": "observed", "receipts": [], "completed": False}
    try:
        budget = ReadBudget()
        with readonly_snapshot(budget) as conn:
            for outbox_id in dict.fromkeys(outbox_ids):
                budget.check()
                report["receipts"].append(_observe(conn, outbox_id, max_hops, budget))
        report["completed"] = all(item["completed"] for item in report["receipts"])
    except FileNotFoundError:
        report.update(state="unavailable", reason="required_data_missing")
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        report.update(state="error", reason="receipt_observation_failed", error_type=type(exc).__name__)
    if report["state"] != "observed":
        report["completed"] = False
    return report
