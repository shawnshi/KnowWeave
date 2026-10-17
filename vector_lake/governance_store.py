import copy
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from vector_lake.claim_extractor import extract_page_objects
from vector_lake.db_store import (
    ensure_operational_memory_index_cheap,
    get_connection,
    init_db,
    transaction,
)
from vector_lake.wiki_utils import (
    get_meta_dir,
    get_wiki_dir,
    read_markdown_file,
    split_frontmatter,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-governance-store")

SCHEMA_VERSION = "8.0"
OPERATIONAL_MEMORY_TYPES = {"fact", "preference", "decision", "task_state"}
MEMORY_TTL_DAYS = {
    "fact": 365,
    "preference": 365,
    "decision": 730,
    "task_state": 45,
}
VALIDITY_FACTORS = {
    "active": 1.0,
    "expiring-soon": 0.82,
    "review-due": 0.72,
    "needs-review": 0.62,
    "provisional": 0.58,
    "unsupported": 0.42,
    "conflicted": 0.18,
    "superseded": 0.08,
    "expired": 0.0,
    "archived": 0.0,
}


_PURPOSE_VECTORS_CACHE = None
_PURPOSE_VECTORS_MTIME = 0

def get_purpose_vectors() -> dict:
    global _PURPOSE_VECTORS_CACHE, _PURPOSE_VECTORS_MTIME
    path = get_meta_dir() / "purpose_vectors.json"
    
    current_mtime = 0
    if path.exists():
        current_mtime = path.stat().st_mtime
        
    if _PURPOSE_VECTORS_CACHE is not None and _PURPOSE_VECTORS_MTIME == current_mtime:
        return _PURPOSE_VECTORS_CACHE
        
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                _PURPOSE_VECTORS_CACHE = json.load(f)
        except Exception:
            _PURPOSE_VECTORS_CACHE = {"keywords": [], "weight_boost": 0.0}
    else:
        _PURPOSE_VECTORS_CACHE = {"keywords": [], "weight_boost": 0.0}
        
    _PURPOSE_VECTORS_MTIME = current_mtime
    return _PURPOSE_VECTORS_CACHE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(path) -> FileLock:
    return FileLock(str(path) + ".lock", timeout=10)


_QUEUE_LOCK_STATE = threading.local()
_QUEUE_LOCK: FileLock | None = None
_QUEUE_LOCK_PATH: str | None = None


def _governance_queue_lock() -> FileLock:
    """One process-wide ``FileLock`` instance per lock path.

    ``filelock`` only re-enters a file it already holds when the *same instance*
    is reused; two instances on one path can deadlock or self-block.  Cache the
    instance so nested callers share it.
    """
    global _QUEUE_LOCK, _QUEUE_LOCK_PATH
    path = str(get_meta_dir() / "governance_queue.lock")
    if _QUEUE_LOCK is None or _QUEUE_LOCK_PATH != path:
        _QUEUE_LOCK = FileLock(path, timeout=10)
        _QUEUE_LOCK_PATH = path
    return _QUEUE_LOCK


@contextmanager
def governance_queue_session():
    """Serialise a whole load -> mutate -> save cycle on the governance queue.

    ``_save_db_queue`` persists the queue by deleting every key that is absent
    from the caller's snapshot.  A writer that saves a snapshot taken before
    another writer appended therefore *deletes* that writer's item, silently
    losing governance work (research directives, merge candidates, publish
    candidates).  Every writer must hold this lock across its entire cycle.

    Re-entrant within a thread, so an already-locked caller can safely call a
    locked helper.  Acquire this before any database transaction so the global
    lock order stays file-lock -> database transaction.
    """
    depth = getattr(_QUEUE_LOCK_STATE, "depth", 0)
    if depth:
        _QUEUE_LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _QUEUE_LOCK_STATE.depth = depth
        return
    lock = _governance_queue_lock()
    with lock:
        _QUEUE_LOCK_STATE.depth = 1
        try:
            yield
        finally:
            _QUEUE_LOCK_STATE.depth = 0


def _default_map_store(key_name: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "items": {},
        "key_name": key_name,
    }


def _default_queue_store() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "items": [],
    }


ALLOWED_TABLES = {
    "entities", "claims", "evidence", "sources", "change_sets",
    "governance_queue", "wiki_search_index", "alias_registry",
    "operational_memory", "claim_graph_edges",
    "timeline_events", "processed_files", "mutation_outbox"
}

def _validate_table_name(table_name: str):
    """🛡️ Sentinel: Prevent SQL injection by validating table names against a strict whitelist."""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Security error: Invalid table name '{table_name}'. Expected one of {ALLOWED_TABLES}.")

def initialize_meta_store():
    init_db()


def _count_wiki_pages() -> int:
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return 0
    return len([
        name for name in os.listdir(wiki_dir)
        if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")
    ])


def _load_db_map(table_name: str, pk_col: str):
    _validate_table_name(table_name)
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store(pk_col)
    rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
    for row in rows:
        store["items"][row[pk_col]] = json.loads(row["data_json"])
    return store


def _load_db_queue(table_name: str, pk_col: str):
    _validate_table_name(table_name)
    initialize_meta_store()
    conn = get_connection()
    store = _default_queue_store()
    rows = conn.execute(f"SELECT data_json FROM {table_name} ORDER BY updated_at ASC").fetchall()
    for row in rows:
        store["items"].append(json.loads(row["data_json"]))
    return store


def _save_db_map(table_name: str, pk_col: str, data: dict, extra_cols: list = None):
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    if extra_cols is None:
        extra_cols = []
    
    with transaction():
        existing_rows = conn.execute(f"SELECT {pk_col}, data_json FROM {table_name}").fetchall()
        existing_map = {row[0]: row["data_json"] for row in existing_rows}
        new_keys = set(data.get("items", {}).keys())
        keys_to_delete = set(existing_map.keys()) - new_keys
        
        if keys_to_delete:
            conn.executemany(f"DELETE FROM {table_name} WHERE {pk_col} = ?", [(k,) for k in keys_to_delete])
            
        if data.get("items"):
            cols = [pk_col] + [c[0] for c in extra_cols] + ["data_json", "updated_at"]
            placeholders = ["?"] * len(cols)
            
            all_vals = []
            for key, item in data.get("items", {}).items():
                new_json = json.dumps(item, ensure_ascii=False)
                # Skip SQLite I/O if the row hasn't changed at all
                if key in existing_map and existing_map[key] == new_json:
                    continue
                    
                params = [key]
                for c_name, c_key, c_type in extra_cols:
                    val = item.get(c_key)
                    if c_type is float:
                        params.append(float(val or 0.0))
                    elif c_type is int:
                        params.append(int(val or 0))
                    else:
                        params.append(str(val or ""))
                params.append(new_json)
                params.append(now)
                all_vals.append(tuple(params))
                
            if all_vals:
                conn.executemany(f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", all_vals)


def _save_db_queue(table_name: str, pk_col: str, data: dict):
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    with transaction():
        # V10.1 Diff-based synchronization (Avoid full table wipe)
        for item in data.get("items", []):
            if not item.get(pk_col):
                item[pk_col] = uuid.uuid4().hex
                
        existing_keys_query = conn.execute(f"SELECT {pk_col} FROM {table_name}").fetchall()
        existing_keys = {row[0] for row in existing_keys_query}
        new_keys = {item[pk_col] for item in data.get("items", [])}
        keys_to_delete = existing_keys - new_keys
        
        if keys_to_delete:
            conn.executemany(f"DELETE FROM {table_name} WHERE {pk_col} = ?", [(k,) for k in keys_to_delete])
            
        if data.get("items"):
            all_vals = []
            for item in data.get("items", []):
                k = item.get(pk_col)
                all_vals.append((k, json.dumps(item, ensure_ascii=False), now))
            conn.executemany(f"INSERT OR REPLACE INTO {table_name} ({pk_col}, data_json, updated_at) VALUES (?, ?, ?)", all_vals)


def load_entities():
    return _load_db_map("entities", "entity_id")


def query_entities(filters: dict = None) -> dict:
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store("entity_id")
    query = "SELECT data_json FROM entities"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            if not re.match(r"^[a-zA-Z0-9_]+(!=)?$", k):
                raise ValueError(f"Security error: Invalid filter key '{k}'.")
            if k.endswith("!="):
                clauses.append(f"{k[:-2]} != ?")
            else:
                clauses.append(f"{k} = ?")
            params.append(v)
        query += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(query, tuple(params)).fetchall()
    for row in rows:
        data = json.loads(row["data_json"])
        store["items"][data["entity_id"]] = data
    return store



def load_claims():
    return _load_db_map("claims", "claim_id")


def load_evidence():
    return _load_db_map("evidence", "evidence_id")


def load_sources():
    return _load_db_map("sources", "source_id")


def load_alias_registry():
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store("alias")
    rows = conn.execute("SELECT key, value FROM alias_registry").fetchall()
    for row in rows:
        store["items"][row["key"]] = row["value"]
    return store


def get_alias(key: str) -> str | None:
    """Return one alias target without loading the complete registry."""
    initialize_meta_store()
    row = (
        get_connection()
        .execute(
            "SELECT value FROM alias_registry WHERE key = ?",
            (key,),
        )
        .fetchone()
    )
    return row["value"] if row else None


def upsert_alias(key: str, value: str) -> None:
    """Persist one alias mapping and participate in any surrounding transaction."""
    now = _utc_now()
    with transaction():
        get_connection().execute(
            "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )


def load_claims_by_ids(claim_ids: list[str]) -> dict[str, dict]:
    """Decode only the named claims, keyed by id.

    ``trace`` returns a handful of claims out of the 101 323 it scans; decoding those rows keeps
    the returned fields complete without paying for the corpus.
    """
    conn = get_connection()
    wanted = [str(claim_id) for claim_id in claim_ids if str(claim_id)]
    if not wanted:
        return {}
    decoded: dict[str, dict] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start : start + 400]
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT claim_id, data_json FROM claims WHERE claim_id IN ({placeholders})",
            chunk,
        ):
            try:
                decoded[str(row["claim_id"])] = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
    return decoded


def load_claim_scan_rows() -> list[dict] | None:
    """The claim fields ``trace`` and ``debt`` read, from the ``claim_index`` projection.

    ``load_claims`` decodes all 101 323 JSON payloads for these fields (3.2 s of a 4.2 s trace).
    The rows come back in ``source_rowid`` order -- the store's natural ``SELECT *`` order, which is
    the order the previous full scan iterated in and therefore the tie-break of its stable sort --
    with ``source_ids`` parsed, and ``[]`` for the absent case exactly as ``claim.get(..., [])``
    would have produced.

    Returns ``None`` when the projection is unusable (absent, or empty while claims exist), so a
    caller degrades to the decoded path rather than answering from a partial projection.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT claim_id, status, confidence, freshness_tier, valid_to, review_after, "
            "evidence_count, contradicts_count, subject_entity_count, source_page, claim_text, "
            "source_ids, evidence_gap, source_rowid FROM claim_index ORDER BY source_rowid"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        try:
            if conn.execute("SELECT EXISTS (SELECT 1 FROM claims)").fetchone()[0]:
                return None
        except sqlite3.OperationalError:
            return None
    claims = []
    for row in rows:
        raw_ids = row["source_ids"]
        claims.append(
            {
                "claim_id": str(row["claim_id"]),
                "status": str(row["status"]),
                "confidence": float(row["confidence"]),
                "freshness_tier": str(row["freshness_tier"]),
                "evidence_gap": str(row["evidence_gap"] or ""),
                "valid_to": str(row["valid_to"]) or None,
                "review_after": str(row["review_after"]) or None,
                "evidence_ids": [None] * int(row["evidence_count"]),
                "contradicts": [None] * int(row["contradicts_count"]),
                "subject_entity_ids": [None] * int(row["subject_entity_count"]),
                "source_page": str(row["source_page"]),
                "claim_text": str(row["claim_text"]),
                "source_ids": json.loads(raw_ids) if raw_ids is not None else [],
            }
        )
    return claims


def load_memory_objects():
    return _load_db_map("operational_memory", "memory_id")


def query_memory_objects(filters: dict = None) -> dict:
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store("memory_id")
    query = "SELECT data_json FROM operational_memory"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            if not re.match(r"^[a-zA-Z0-9_]+(!=)?$", k):
                raise ValueError(f"Security error: Invalid filter key '{k}'.")
            if k.endswith("!="):
                clauses.append(f"{k[:-2]} != ?")
            else:
                clauses.append(f"{k} = ?")
            params.append(v)
        query += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(query, tuple(params)).fetchall()
    for row in rows:
        data = json.loads(row["data_json"])
        store["items"][data["memory_id"]] = data
    return store


def load_change_sets():
    return _load_db_queue("change_sets", "change_set_id")


def load_governance_queue():
    return _load_db_queue("governance_queue", "item_id")


def save_entities(data):
    _save_db_map("entities", "entity_id", data, [
        ("canonical_name", "canonical_name", str),
        ("type", "type", str),
        ("status", "status", str),
        ("ttl", "ttl", float),
        ("decay_weight", "decay_weight", float)
    ])


def save_claims(data):
    conn = get_connection()
    with transaction():
        old_rows = conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims"
        ).fetchall()
        _save_db_map("claims", "claim_id", data, [("claim_text", "claim_text", str), ("status", "status", str)])
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

        sync_timeline_events_for_claim_delta(old_rows, list(data.get("items", {}).values()))


def save_evidence(data):
    _save_db_map("evidence", "evidence_id", data)


def save_sources(data):
    _save_db_map("sources", "source_id", data)


def save_graph_edges(edges: list[dict]):
    """Persist claim-space edges.  The page-space projection is derived
    owned by the indexer and must never receive claim ids."""
    if not edges: return
    conn = get_connection()
    with transaction():
        for edge in edges:
            conn.execute(
                "INSERT OR REPLACE INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
                (edge["source_id"], edge["target_id"], edge["relation"], edge.get("weight", 1.0), edge.get("updated_at", _utc_now())),
            )


def save_alias_registry(data):
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    with transaction():
        existing_keys = {row["key"] for row in conn.execute("SELECT key FROM alias_registry")}
        new_keys = set(data.get("items", {}))
        stale_keys = existing_keys - new_keys
        if stale_keys:
            conn.executemany("DELETE FROM alias_registry WHERE key = ?", [(key,) for key in stale_keys])
        for k, v in data.get("items", {}).items():
            conn.execute("INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now))


def save_memory_objects(data):
    _save_db_map("operational_memory", "memory_id", data, [
        ("memory_type", "memory_type", str), 
        ("score", "memory_score", float),
        ("status", "status", str),
        ("ttl", "ttl", float)
    ])


def save_change_sets(data):
    _save_db_queue("change_sets", "change_set_id", data)

def save_governance_queue(data):
    _save_db_queue("governance_queue", "item_id", data)

# =============================================================================
# V10.1 TARGETED ATOMIC CRUD (Replaces load_all -> save_all pattern)
# =============================================================================
def get_entity(entity_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT data_json FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    if row:
        return json.loads(row["data_json"])
    return None


def _canonical_entity_rows_version(page_rows: list[tuple[str, str]]) -> str:
    normalized_rows = []
    for entity_id, raw in page_rows:
        data = json.loads(raw)
        # extract_page_objects supplies wall-clock time when legacy pages omit `created`.
        # That fallback is storage metadata, not page state, so it cannot participate in CAS.
        data.pop("created_at", None)
        normalized = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_rows.append((entity_id, normalized))
    serialized = "\x1e".join(
        f"{entity_id}\x1f{raw}" for entity_id, raw in sorted(normalized_rows)
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def canonical_page_version_from_content(filename: str, content: str) -> str:
    """Calculate the canonical entity version that full Markdown would produce."""
    frontmatter, body = split_frontmatter(content)
    extracted = extract_page_objects(filename, frontmatter, body)
    rows = [
        (str(record["entity_id"]), json.dumps(record, ensure_ascii=False))
        for record in extracted.get("entities", [])
    ]
    if not rows:
        return ""
    return _canonical_entity_rows_version(rows)


def canonical_page_versions(page_keys: set[str] | None = None) -> dict[str, str]:
    """Return deterministic version tokens for the current canonical page state."""
    init_db()
    requested = set(page_keys) if page_keys is not None else None
    rows_by_page: dict[str, list[tuple[str, str]]] = {}
    if requested is not None and not requested:
        return {}
    if requested is None:
        rows = get_connection().execute("SELECT entity_id, data_json FROM entities ORDER BY entity_id").fetchall()
    else:
        placeholders = ",".join("?" for _ in requested)
        rows = get_connection().execute(
            f"SELECT entity_id, data_json FROM entities WHERE f_page_key IN ({placeholders}) ORDER BY entity_id",
            tuple(sorted(requested)),
        ).fetchall()
    for row in rows:
        raw = str(row["data_json"])
        try:
            page_key = str(json.loads(raw).get("page_key") or "")
        except (TypeError, ValueError):
            continue
        if not page_key or (requested is not None and page_key not in requested):
            continue
        rows_by_page.setdefault(page_key, []).append((str(row["entity_id"]), raw))

    return {
        page_key: _canonical_entity_rows_version(page_rows)
        for page_key, page_rows in rows_by_page.items()
    }

def _float_or_zero(value) -> float:
    """``float(value)``, or ``0.0`` when the value is not a number.

    The two entity writers have to agree on this, and the rest of the tree already treats an
    unparseable ttl as absent (``indexer``, ``tool_lint`` read it defensively).  The batch path
    converts ``ttl`` inside the change-set transaction, so an exception there would roll back a whole
    batch over one bad frontmatter value; accepting numeric strings keeps ``ttl: "180"`` working.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def upsert_entity(entity_id: str, data: dict):
    conn = get_connection()
    now = _utc_now()
    cols = ["entity_id", "canonical_name", "type", "status", "ttl", "decay_weight", "data_json", "updated_at"]
    placeholders = ["?"] * len(cols)
    params = [
        entity_id,
        str(data.get("canonical_name") or data.get("title") or data.get("page_key") or entity_id),
        str(data.get("type", "")),
        str(data.get("status", "Active")),
        _float_or_zero(data.get("ttl")),
        _float_or_zero(data.get("decay_weight")),
        json.dumps(data, ensure_ascii=False),
        now
    ]
    with transaction():
        conn.execute(f"INSERT OR REPLACE INTO entities ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", params)

def delete_entity(entity_id: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))
# =============================================================================

def _upsert_map_records(store: dict, records: list, key_name: str):
    for record in records:
        key = record[key_name]
        store["items"][key] = record


def _upsert_canonical_records(table_name: str, key_name: str, records: list[dict]):
    """Upsert only the records in one page-scoped canonical delta."""
    if not records:
        return
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    if table_name == "entities":
        conn.executemany(
            # ``ttl`` and ``decay_weight`` are in the column list for the same reason the fields
            # above are: ``INSERT OR REPLACE`` *replaces* the row, so a column left out of this
            # statement is reset to NULL.  That is how 7,919 of 7,924 rows came to have a NULL
            # ``ttl`` while the json carried one, and why ``decay_weight`` (which has no json
            # counterpart at all) was wiped by every batch write.  The values are derived exactly as
            # ``upsert_entity`` derives them, so the two write paths agree by construction.
            "INSERT OR REPLACE INTO entities "
            "(entity_id, canonical_name, type, status, ttl, decay_weight, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    record[key_name],
                    str(record.get("canonical_name") or record.get("title") or record.get("page_key") or record[key_name]),
                    str(record.get("type", "")),
                    str(record.get("status", "Active")),
                    _float_or_zero(record.get("ttl")),
                    _float_or_zero(record.get("decay_weight")),
                    json.dumps(record, ensure_ascii=False),
                    now,
                )
                for record in records
            ],
        )
        return
    if table_name == "claims":
        conn.executemany(
            "INSERT OR REPLACE INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    record[key_name],
                    str(record.get("claim_text", "")),
                    str(record.get("status", "Active")),
                    json.dumps(record, ensure_ascii=False),
                    now,
                )
                for record in records
            ],
        )
        return
    conn.executemany(
        f"INSERT OR REPLACE INTO {table_name} ({key_name}, data_json, updated_at) VALUES (?, ?, ?)",
        [
            (record[key_name], json.dumps(record, ensure_ascii=False), now)
            for record in records
        ],
    )


def _upsert_operational_memory_records(records: list[dict]):
    if not records:
        return
    conn = get_connection()
    now = _utc_now()
    conn.executemany(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record["memory_id"],
                str(record.get("memory_type", "fact")),
                float(record.get("memory_score", 0.0) or 0.0),
                str(record.get("status", "Active")),
                float(record.get("ttl_days", 0.0) or 0.0),
                json.dumps(record, ensure_ascii=False),
                now,
            )
            for record in records
        ],
    )


def _json_id_list(ids) -> str:
    """A sorted JSON array for ``json_each``-based membership tests.

    ``IN (?,?,...)`` costs one SQL variable per id and SQLite caps them at
    32,766 (measured on this build), while a bulk change-set batch touches every
    claim of every affected page -- 14,055 of them for one rewrite of the
    community index pages -- and the ``claim_graph_edges`` statement below binds
    each id twice.  That exceeded the ceiling with
    ``sqlite3.OperationalError: too many SQL variables``, and because the whole
    batch runs in one transaction it aborted the entire run.  A single JSON
    parameter has no such limit.  ``sorted`` keeps the previous deterministic
    parameter order.
    """
    return json.dumps(sorted(ids), ensure_ascii=False)


def _refresh_operational_memory_delta(old_claim_ids: set[str], proposed_claims: list[dict]):
    """Rebuild memory only for changed claims and their direct conflict peers."""
    from vector_lake import governance_metrics

    conn = get_connection()
    proposed_claim_ids = {record["claim_id"] for record in proposed_claims}
    changed_claim_ids = old_claim_ids | proposed_claim_ids
    old_memories = []
    if changed_claim_ids:
        old_memories = [
            json.loads(row["data_json"])
            for row in conn.execute(
                "SELECT data_json FROM operational_memory "
                "WHERE f_source_claim_id IN (SELECT value FROM json_each(?))",
                (_json_id_list(changed_claim_ids),),
            ).fetchall()
        ]
        conn.execute(
            "DELETE FROM operational_memory "
            "WHERE f_source_claim_id IN (SELECT value FROM json_each(?))",
            (_json_id_list(changed_claim_ids),),
        )

    new_memories = [
        _memory_object_from_claim(governance_metrics.annotate_claim_validity(record))
        for record in proposed_claims
    ]
    _upsert_operational_memory_records(new_memories)

    related_claim_ids = set(changed_claim_ids)
    impacted_keys = set()
    for memory in [*old_memories, *new_memories]:
        related_claim_ids.update(memory.get("contradicts_claim_ids", []))
        if memory.get("memory_type") != "fact":
            impacted_keys.add((memory.get("memory_type"), memory.get("memory_key")))

    peer_rows = []
    if related_claim_ids:
        peer_rows.extend(
            conn.execute(
                "SELECT data_json FROM operational_memory "
                "WHERE f_source_claim_id IN (SELECT value FROM json_each(?))",
                (_json_id_list(related_claim_ids),),
            ).fetchall()
        )
    for memory_type, memory_key in sorted(impacted_keys):
        peer_rows.extend(
            conn.execute(
                "SELECT data_json FROM operational_memory "
                "WHERE memory_type = ? AND f_memory_key = ?",
                (memory_type, memory_key),
            ).fetchall()
        )

    peer_store = _default_map_store("memory_id")
    for row in peer_rows:
        memory = json.loads(row["data_json"])
        if (memory.get("conflict_resolution") or {}).get("state") == "superseded":
            memory["validity_state"] = "active"
            memory.pop("superseded_by", None)
            memory.pop("conflict_resolution", None)
            memory.update(score_memory_object(memory))
        peer_store["items"][memory["memory_id"]] = memory
    if peer_store["items"]:
        _resolve_memory_conflicts(peer_store)
        _upsert_operational_memory_records(list(peer_store["items"].values()))


def _refresh_alias_delta(old_entity_ids: set[str], proposed_entities: list[dict]):
    conn = get_connection()
    affected_entity_ids = old_entity_ids | {record["entity_id"] for record in proposed_entities}
    if affected_entity_ids:
        placeholders = ",".join("?" for _ in affected_entity_ids)
        conn.execute(
            f"DELETE FROM alias_registry WHERE value IN ({placeholders})",
            tuple(sorted(affected_entity_ids)),
        )
    now = _utc_now()
    aliases = []
    for entity in proposed_entities:
        entity_id = entity["entity_id"]
        aliases.append((str(entity.get("canonical_name") or entity.get("title") or entity_id), entity_id, now))
        aliases.extend((str(alias), entity_id, now) for alias in entity.get("aliases", []) if alias)
    if aliases:
        conn.executemany(
            "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            aliases,
        )


def rebuild_alias_registry():
    entities = load_entities()
    alias_registry = _default_map_store("alias")
    for entity in entities["items"].values():
        entity_id = entity["entity_id"]
        alias_registry["items"][entity["canonical_name"]] = entity_id
        for alias in entity.get("aliases", []):
            alias_registry["items"][alias] = entity_id
    save_alias_registry(alias_registry)
    return alias_registry


def annotated_claims() -> list[dict]:
    from vector_lake import governance_metrics

    return [
        governance_metrics.annotate_claim_validity(claim)
        for claim in load_claims()["items"].values()
    ]


def _compact_claim_text(text: str, limit: int = 240) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=12).hexdigest()
    return f"{prefix}_{digest}"


def _coerce_float(value, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dt_rank(value) -> float:
    parsed = _parse_dt(value)
    if not parsed:
        return 0.0
    return parsed.timestamp()


def _normalize_memory_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"


def _query_terms(query: str) -> list[str]:
    text = str(query or "").lower()
    terms = {token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text) if token}
    cjk_chars = [char for char in text if "\u4e00" <= char <= "\u9fff"]
    for index in range(len(cjk_chars) - 1):
        terms.add(cjk_chars[index] + cjk_chars[index + 1])
    terms.update(cjk_chars)
    return sorted(terms)


def infer_memory_type(claim: dict) -> str:
    """Which memory slot a claim belongs in, or ``fact`` when the claim does not say.

    Only two things may choose a slot: the claim's own ``memory_type``, or a ``claim_type`` that
    names one.  A keyword scan over the prose used to decide when neither was present, and it
    fabricated both non-fact slots: of the 6,681 records it produced, *every* one of the 4,842
    ``decision`` and 1,338 ``task_state`` records was a page section whose text happened to contain
    方案/采用/状态 -- keys like ``decision_物理机制_mechanism`` (465), ``decision_2_证据时间线`` (331)
    and ``decision_中文摘要`` (112) -- so "Open Decisions" listed compiled-fact sections and "Task
    State" listed timelines.  The 431 inferred preferences were unmatchable by construction: a
    preference is a statement about what someone wants, not a sentence containing 偏好.

    The slots are filled by explicit writes (:mod:`vector_lake.tool_memory` writes
    ``memory_type`` into the page's frontmatter), and the 70 records that had an explicit
    declaration survived the same audit unchanged.
    """
    explicit = str(claim.get("memory_type") or "").strip().lower().replace("-", "_")
    if explicit in OPERATIONAL_MEMORY_TYPES:
        return explicit

    claim_type = str(claim.get("claim_type") or "").lower().replace("-", "_")
    if claim_type in OPERATIONAL_MEMORY_TYPES:
        return claim_type

    return "fact"


def _infer_memory_key(claim: dict, memory_type: str) -> str:
    explicit = claim.get("memory_key") or claim.get("preference_key") or claim.get("decision_key") or claim.get("task_key")
    if explicit:
        return _normalize_memory_key(explicit)

    locator = claim.get("locator") or {}
    heading = locator.get("heading") or claim.get("source_page") or "general"
    text = str(claim.get("claim_text") or "")
    match = re.match(r"^(.{2,80}?)[：:]\s+.+$", text)
    if match:
        heading = match.group(1)

    if memory_type == "fact":
        return _normalize_memory_key(claim.get("claim_id") or text[:96])
    return _normalize_memory_key(f"{memory_type}:{heading}")


def _freshness_score(record: dict, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    valid_to = _parse_dt(record.get("valid_to"))
    if valid_to and valid_to < now:
        return 0.0

    updated_at = _parse_dt(record.get("updated_at")) or _parse_dt(record.get("created_at"))
    if not updated_at:
        return 0.55

    age_days = max(0, (now - updated_at).days)
    ttl_days = record.get("ttl_days") or MEMORY_TTL_DAYS.get(record.get("memory_type", "fact"), 365)
    try:
        ttl_days = max(1.0, float(ttl_days))
    except (TypeError, ValueError):
        ttl_days = MEMORY_TTL_DAYS.get(record.get("memory_type", "fact"), 365)
    return round(0.5 ** (age_days / ttl_days), 4)


def score_memory_object(memory: dict, now=None) -> dict:
    confidence_score = _coerce_float(memory.get("confidence"), 0.72)
    authority_score = _coerce_float(memory.get("authority_score"), 0.65)
    importance_score = _coerce_float(memory.get("importance_score"), 0.55)
    
    purpose_vectors = get_purpose_vectors()
    intent_weight = 0.0
    if purpose_vectors.get("keywords"):
        text = (memory.get("text") or "").lower()
        key = (memory.get("memory_key") or "").lower()
        for kw in purpose_vectors["keywords"]:
            if kw.lower() in text or kw.lower() in key:
                intent_weight = float(purpose_vectors.get("weight_boost", 0.20))
                break
                
    importance_score = min(1.0, importance_score + intent_weight)
    
    freshness_score = _freshness_score(memory, now=now)
    reinforcement_count = int(memory.get("reinforcement_count") or 0)
    reinforcement_score = min(1.0, math.log1p(max(0, reinforcement_count)) / math.log(8))
    validity_factor = VALIDITY_FACTORS.get(str(memory.get("validity_state", "active")).lower(), 0.5)
    memory_score = (
        0.30 * confidence_score
        + 0.25 * freshness_score
        + 0.20 * authority_score
        + 0.15 * importance_score
        + 0.10 * reinforcement_score
    ) * validity_factor
    return {
        "confidence_score": round(confidence_score, 4),
        "freshness_score": round(freshness_score, 4),
        "authority_score": round(authority_score, 4),
        "importance_score": round(importance_score, 4),
        "reinforcement_score": round(reinforcement_score, 4),
        "validity_factor": round(validity_factor, 4),
        "memory_score": round(memory_score, 4),
    }


def _memory_object_from_claim(claim: dict) -> dict:
    memory_type = infer_memory_type(claim)
    memory_key = _infer_memory_key(claim, memory_type)
    memory_id = _stable_id("mem", f"{claim.get('claim_id')}:{memory_type}:{memory_key}")
    source_ids = list(claim.get("source_ids", []))
    evidence_ids = list(claim.get("evidence_ids", []))
    memory = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_key,
        "text": claim.get("claim_text", ""),
        "value": claim.get("memory_value") or claim.get("claim_text", ""),
        "source_claim_id": claim.get("claim_id"),
        "source_page": claim.get("source_page"),
        "locator": claim.get("locator", {}),
        "subject_entity_ids": list(claim.get("subject_entity_ids", [])),
        "evidence_ids": evidence_ids,
        "source_ids": source_ids,
        "source_count": len(source_ids),
        "status": claim.get("status", "Active"),
        "validity_state": claim.get("validity_state", "active"),
        "validity_reasons": claim.get("validity_reasons", []),
        "temporal_anchor": claim.get("temporal_anchor"),
        "valid_from": claim.get("valid_from"),
        "valid_to": claim.get("valid_to"),
        "review_after": claim.get("review_after"),
        "created_at": claim.get("created_at"),
        "updated_at": claim.get("updated_at"),
        "confidence": claim.get("confidence", 0.72),
        "authority_score": claim.get("authority_score", 0.72 if source_ids else 0.48),
        "importance_score": claim.get("importance_score", 0.55),
        "reinforcement_count": claim.get("reinforcement_count", len(evidence_ids)),
        "ttl_days": claim.get("ttl_days") or MEMORY_TTL_DAYS.get(memory_type, 365),
        "contradicts_claim_ids": list(claim.get("contradicts", [])),
    }
    memory.update(score_memory_object(memory))
    return memory


def _rank_memory_for_conflict(memory: dict, explicit_contradiction: bool = False) -> tuple:
    if explicit_contradiction:
        return (
            memory.get("authority_score", 0),
            memory.get("confidence_score", 0),
            _dt_rank(memory.get("updated_at")),
            memory.get("memory_score", 0),
        )
    return (
        _dt_rank(memory.get("updated_at")),
        memory.get("authority_score", 0),
        memory.get("confidence_score", 0),
        memory.get("memory_score", 0),
    )


def _mark_superseded(loser: dict, winner: dict, reason: str):
    loser["validity_state"] = "superseded"
    loser["superseded_by"] = winner["memory_id"]
    loser["conflict_resolution"] = {
        "state": "superseded",
        "winner": winner["memory_id"],
        "rule": reason,
        "resolved_at": _utc_now(),
    }
    loser.update(score_memory_object(loser))


def _resolve_memory_conflicts(store: dict) -> dict:
    items = store.get("items", {})
    by_claim_id = {
        memory.get("source_claim_id"): memory
        for memory in items.values()
        if memory.get("source_claim_id")
    }
    conflict_events = []

    for memory in list(items.values()):
        for right_claim_id in memory.get("contradicts_claim_ids", []):
            other = by_claim_id.get(right_claim_id)
            if not other or other["memory_id"] == memory["memory_id"]:
                continue
            left_rank = _rank_memory_for_conflict(memory, explicit_contradiction=True)
            right_rank = _rank_memory_for_conflict(other, explicit_contradiction=True)
            if left_rank == right_rank:
                memory["validity_state"] = "conflicted"
                other["validity_state"] = "conflicted"
                memory.update(score_memory_object(memory))
                other.update(score_memory_object(other))
                conflict_events.append({
                    "type": "unresolved-explicit-contradiction",
                    "memory_ids": sorted([memory["memory_id"], other["memory_id"]]),
                })
            elif left_rank > right_rank:
                _mark_superseded(other, memory, "explicit-contradiction:authority-confidence-recency")
            else:
                _mark_superseded(memory, other, "explicit-contradiction:authority-confidence-recency")

    grouped = {}
    for memory in items.values():
        if memory.get("memory_type") == "fact":
            continue
        if str(memory.get("validity_state", "")).lower() in {"expired", "archived"}:
            continue
        grouped.setdefault((memory.get("memory_type"), memory.get("memory_key")), []).append(memory)

    for (memory_type, memory_key), candidates in grouped.items():
        # Reset validity state so superseded ones can compete again
        for c in candidates:
            if c.get("validity_state") == "superseded":
                c["validity_state"] = "active"
                c.pop("superseded_by", None)
                c.pop("conflict_resolution", None)
                
        if len(candidates) <= 1:
            if len(candidates) == 1 and candidates[0].get("validity_state") != "active":
                candidates[0]["validity_state"] = "active"
                candidates[0].update(score_memory_object(candidates[0]))
            continue
        ordered = sorted(candidates, key=_rank_memory_for_conflict, reverse=True)
        winner = ordered[0]
        winner["conflict_resolution"] = {
            "state": "winner",
            "rule": f"{memory_type}:newer-authority-confidence",
            "resolved_at": _utc_now(),
            "competing_memory_ids": [item["memory_id"] for item in ordered[1:]],
        }
        for loser in ordered[1:]:
            _mark_superseded(loser, winner, f"{memory_type}:newer-authority-confidence")
        conflict_events.append({
            "type": "typed-memory-supersession",
            "memory_type": memory_type,
            "memory_key": memory_key,
            "winner": winner["memory_id"],
            "losers": [item["memory_id"] for item in ordered[1:]],
        })

    store["conflict_events"] = conflict_events
    store["memory_type_counts"] = {}
    for memory in items.values():
        memory_type = memory.get("memory_type", "fact")
        store["memory_type_counts"][memory_type] = store["memory_type_counts"].get(memory_type, 0) + 1
    return store


def rebuild_operational_memory() -> dict:
    claims = annotated_claims()
    store = _default_map_store("memory_id")
    for claim in claims:
        memory = _memory_object_from_claim(claim)
        store["items"][memory["memory_id"]] = memory
    store = _resolve_memory_conflicts(store)
    save_memory_objects(store)
    return store


def _memory_term_frequencies(memories, terms: list[str]) -> dict[str, int]:
    """Documents containing each term, over the fields the gram index indexes.

    ``memory_type`` is excluded on purpose: the postings carry no type bit, so a frequency that
    counted type matches would be one the index cannot reproduce, and the two scorers would drift.
    """
    wanted = [term for term in terms if term]
    frequencies = dict.fromkeys(wanted, 0)
    if not frequencies:
        return frequencies
    for memory in memories:
        haystack = "\n".join(
            (
                str(memory.get("memory_key", "")).lower(),
                str(memory.get("text", "")).lower(),
                str(memory.get("source_page", "")).lower(),
            )
        )
        for term in wanted:
            if term in haystack:
                frequencies[term] += 1
    return frequencies


def _memory_relevance(
    memory: dict, terms: list[str], df: dict[str, int] | None = None, total: int | None = None
) -> float:
    """Field-weighted term overlap, each term scaled by its inverse document frequency.

    ``df`` / ``total`` must be the same numbers the n-gram index uses (see
    :func:`memory_gram_index.term_document_frequencies`) or the two scorers stop agreeing.  Without
    them the weights fall back to 1.0, which is the pre-IDF behaviour -- used by callers that score
    a handful of records outside any corpus.
    """
    if not terms:
        return 0.0
    from vector_lake.memory_gram_index import FIELD_WEIGHTS, idf_weight

    haystacks = {
        "key": str(memory.get("memory_key", "")).lower(),
        "text": str(memory.get("text", "")).lower(),
        "page": str(memory.get("source_page", "")).lower(),
        "type": str(memory.get("memory_type", "")).lower(),
    }
    score = 0.0
    for term in terms:
        weight = idf_weight(df.get(term, 0), total or 0) if df else 1.0
        if term in haystacks["key"]:
            score += weight * FIELD_WEIGHTS[1]
        if term in haystacks["text"]:
            score += weight * FIELD_WEIGHTS[2]
        if term in haystacks["page"]:
            score += weight * FIELD_WEIGHTS[4]
        if term in haystacks["type"]:
            score += weight * 1.0
    return score


HIDDEN_MEMORY_STATES = frozenset({"archived", "expired", "superseded"})


def _memory_score_value(value) -> float:
    """Coerce a stored ``memory_score`` to a sortable float.

    ``None`` must not reach the sort key: the previous inline
    ``memory.get("memory_score", 0)`` returns ``None`` for an explicitly null
    field and then raised ``TypeError`` inside ``list.sort``.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_memory_types(memory_types: list[str] | None) -> set[str] | None:
    if not memory_types:
        return None
    return {str(item).strip().lower().replace("-", "_") for item in memory_types}


def _load_memory_items() -> list[dict]:
    """Load the operational-memory store, rebuilding it when it is empty."""
    store = load_memory_objects()
    if not store.get("items") and load_claims().get("items"):
        store = rebuild_operational_memory()
    return list(store.get("items", {}).values())


def _memory_corpus_frequencies(terms: list[str]) -> tuple[dict[str, int] | None, int | None]:
    """Corpus document frequencies for ``terms``, from the postings that own them.

    Returns ``(None, None)`` when there is no usable index at all -- the scorers then fall back to
    equal weights, which is the pre-IDF behaviour, rather than inventing frequencies from one
    caller's candidate window.
    """
    if not terms:
        return None, None
    try:
        from vector_lake import memory_gram_index

        if not memory_gram_index.ensure_memory_gram_index():
            return None, None
        conn = get_connection()
        df = memory_gram_index.term_document_frequencies(
            terms, skip_docs=memory_gram_index.skip_doc_set(conn)
        )
        total = int(memory_gram_index.gram_index_state().get("base_docs") or 0)
    except sqlite3.Error as exc:
        log.warning("Memory gram index unreadable (%s: %s); scoring without idf.", type(exc).__name__, exc)
        return None, None
    return (df, total) if total else (None, None)


def score_memory_items(
    memories,
    terms: list[str],
    top_k: int,
    allowed_types: set[str] | None = None,
    include_history: bool = False,
    df: dict[str, int] | None = None,
    total: int | None = None,
) -> list[dict]:
    """Rank an already-loaded memory collection by relevance, then memory score.

    Split out of :func:`search_operational_memory` so a caller that needs both
    the active and the history-inclusive view (the Memory Packet) scores one
    loaded collection twice instead of loading the whole store twice.
    """
    ranked = []
    for memory in memories:
        memory_type = str(memory.get("memory_type", "fact")).lower()
        if allowed_types and memory_type not in allowed_types:
            continue
        state = str(memory.get("validity_state", "active")).lower()
        if not include_history and state in HIDDEN_MEMORY_STATES:
            continue
        relevance = _memory_relevance(memory, terms, df, total)
        if relevance <= 0 and terms:
            continue
        # ``memory_score`` is a stored per-record value, not a query signal, so it breaks ties
        # instead of leading: with the old ``+ 5 * memory_score`` a nearly constant ~3.5 outweighed
        # a relevance of a few integer points, and the ranking was mostly this static number.
        score = relevance
        # ⚡ Bolt: Store the score values as sort keys with a reference to the un-copied memory object.
        # This delays expensive deepcopies until *after* top_k elements are selected.
        ranked.append((
            round(score, 4),
            _memory_score_value(memory.get("memory_score")),
            _dt_rank(memory.get("updated_at")),
            str(memory.get("memory_id", "")),
            memory,
        ))

    # Descending relevance, then the stored score, then recency -- and ``memory_id`` *ascending*
    # as the last key.  It is the one tie-break both scoring paths can produce: the index orders
    # its window by ``source_rowid``, which this path has no equivalent of, so once relevance
    # (rather than ``relevance + 5 * score``) decided the order, genuine ties began resolving
    # differently on the two paths.
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))

    results = []
    for score, memory_score, dt_rank, memory_id, memory in ranked[:top_k]:
        item = copy.deepcopy(memory)
        item["retrieval_score"] = score
        results.append(item)

    return results


MEMORY_SEARCH_BACKEND_ENV = "VECTOR_LAKE_MEMORY_SEARCH"
MEMORY_SEARCH_RESULT_WINDOW = 4
MEMORY_SEARCH_BACKENDS = ("gram", "index", "legacy")


def _memory_search_backend() -> str:
    """``gram`` (default) uses the n-gram index, ``index`` the projected scan.

    ``legacy`` forces the original full-table JSON scan; it is the differential
    oracle the other two backends are tested against, and an operator escape hatch.
    """
    backend = str(os.environ.get(MEMORY_SEARCH_BACKEND_ENV, "gram") or "gram").strip().lower()
    return backend if backend in MEMORY_SEARCH_BACKENDS else "gram"


def _gram_memory_candidates(
    terms: list[str],
    allowed_types: set[str] | None,
    include_history: bool,
    window: int,
) -> list[str] | None:
    """Exact relevance from the n-gram index, ordered by the documented key.

    Returns ``None`` when the index cannot answer: absent, written by an older format
    version, or *any* live dirty document over it -- a stale base is refused at every
    corpus size, because a postings blob that still holds a document's dropped grams
    cannot be told apart from an exact one, and no read-side drain restores that.
    Settling the debt is maintenance's job, not a search's.
    """
    from vector_lake import memory_gram_index

    if not memory_gram_index.ensure_memory_gram_index():
        return None
    conn = get_connection()
    if not terms:
        # No terms means "everything, ordered by memory score" for the legacy
        # scorer; skipping the index is both correct and far cheaper.
        sql = (
            "SELECT memory_id FROM operational_memory_index"
        )
        params: list = []
        where = []
        if allowed_types:
            where.append("memory_type IN (%s)" % ",".join("?" for _ in allowed_types))
            params.extend(sorted(allowed_types))
        if not include_history:
            where.append("validity_state NOT IN (%s)" % ",".join("?" for _ in HIDDEN_MEMORY_STATES))
            params.extend(sorted(HIDDEN_MEMORY_STATES))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY memory_score DESC, updated_rank DESC, memory_id ASC"
            f" LIMIT {int(window)}"
        )
        return [str(row[0]) for row in conn.execute(sql, params)]

    skip_docs = memory_gram_index.skip_doc_set(conn)
    df = memory_gram_index.term_document_frequencies(terms, skip_docs=skip_docs)
    relevance = memory_gram_index.accumulate_relevance(
        terms, skip_docs=skip_docs, df=df
    )
    if not relevance:
        return []

    # ``memory_type`` contributes one point per term that is a substring of the
    # type name; that is a per-type constant rather than a per-document lookup.
    # It carries the same idf factor as a field match, so a term common enough to
    # weigh nothing in the body does not re-enter through the type name.
    from vector_lake.memory_gram_index import idf_weight

    total_docs = int(memory_gram_index.gram_index_state().get("base_docs") or 0)
    type_factors = {term: idf_weight(df.get(term, 0), total_docs) for term in terms if term}
    type_weights: dict[str, float] = {}
    for memory_type in _distinct_memory_types():
        weight = sum(factor for term, factor in type_factors.items() if term in memory_type)
        if weight:
            type_weights[memory_type] = weight

    rows: list[tuple] = []
    ordered = sorted(relevance.items())
    for start in range(0, len(ordered), 4000):
        chunk = ordered[start : start + 4000]
        by_doc = dict(chunk)
        placeholders = ",".join("?" for _ in chunk)
        params: list = [doc for doc, _ in chunk]
        sql = (
            "SELECT rowid, memory_id, memory_type, memory_score, updated_rank, source_rowid "
            f"FROM operational_memory_index WHERE rowid IN ({placeholders})"
        )
        if allowed_types:
            sql += " AND memory_type IN (%s)" % ",".join("?" for _ in allowed_types)
            params.extend(sorted(allowed_types))
        if not include_history:
            sql += " AND validity_state NOT IN (%s)" % ",".join("?" for _ in HIDDEN_MEMORY_STATES)
            params.extend(sorted(HIDDEN_MEMORY_STATES))
        for row in get_connection().execute(sql, params):
            doc = int(row["rowid"])
            memory_score = _memory_score_value(row["memory_score"])
            total = by_doc[doc] + type_weights.get(str(row["memory_type"]), 0)
            rows.append((
                round(total, 4),
                memory_score,
                float(row["updated_rank"] or 0.0),
                str(row["memory_id"]),
            ))
    # ``memory_id`` ascending as the last key, matching the full-scan scorer's tie-break.
    rows.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [row[3] for row in rows[:window]]


def _distinct_memory_types() -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row[0])
            for row in get_connection().execute(
                "SELECT DISTINCT memory_type FROM operational_memory_index"
            )
        )
    )


def _indexed_memory_candidates(
    terms: list[str],
    allowed_types: set[str] | None,
    include_history: bool,
    limit: int,
    df: dict[str, int] | None = None,
    total: int | None = None,
) -> list[str]:
    """Top ``limit`` memory ids by the projected relevance score.

    The predicate is an exact restatement of :func:`_memory_relevance` over the
    columns maintained by ``operational_memory_index``: a row is excluded only
    when its relevance would be 0, which the Python scorer drops as well.  The
    per-term contribution of ``memory_type`` is hoisted into a single ``CASE``
    because the type is one of a handful of literals, which removes one
    ``instr()`` per row per term -- the dominant cost of a plain scan.
    """
    conn = get_connection()
    from vector_lake.memory_gram_index import idf_weight

    factor_of = {
        term: idf_weight((df or {}).get(term, 0), total or 0) for term in terms if term
    }
    params: list = []

    def bound(value) -> str:
        params.append(value)
        return f"?{len(params)}"

    relevance_parts = []
    candidate_parts = []
    for term in terms:
        if not term:
            continue
        # Every matching field is scaled by the term's inverse document frequency, which the
        # caller computed once from the postings -- the same number the other two backends use, so
        # the window this selects is the window their scorer would have chosen.
        factor_param = bound(float(factor_of.get(term, 1.0)))
        key_param = bound(term)
        text_param = bound(term)
        page_param = bound(term)
        relevance_parts.append(
            f"{factor_param} * (4 * (instr(key_blob, {key_param}) > 0)"
            f" + 3 * (instr(text_blob, {text_param}) > 0)"
            f" + (instr(page_blob, {page_param}) > 0))"
        )
        candidate_parts.append(
            f"(instr(key_blob, {key_param}) > 0"
            f" OR instr(text_blob, {text_param}) > 0"
            f" OR instr(page_blob, {page_param}) > 0)"
        )

    type_weight_cases = []
    type_hits = []
    for memory_type in _distinct_memory_types():
        weight = sum(factor for term, factor in factor_of.items() if term in memory_type)
        if not weight:
            continue
        type_hits.append(memory_type)
        type_weight_cases.append(f"WHEN {bound(memory_type)} THEN {bound(float(weight))}")
    if type_weight_cases:
        relevance_parts.append(f"(CASE memory_type {' '.join(type_weight_cases)} ELSE 0 END)")
        candidate_parts.append(
            "memory_type IN (%s)" % ", ".join(bound(item) for item in type_hits)
        )
    relevance = " + ".join(relevance_parts) if relevance_parts else "0"

    where = []
    if allowed_types:
        where.append("memory_type IN (%s)" % ", ".join(bound(item) for item in sorted(allowed_types)))
    if not include_history:
        where.append(
            "validity_state NOT IN (%s)"
            % ", ".join(bound(item) for item in sorted(HIDDEN_MEMORY_STATES))
        )
    if candidate_parts:
        where.append("(" + " OR ".join(candidate_parts) + ")")

    sql = (
        f"SELECT memory_id, ({relevance}) AS relevance FROM operational_memory_index"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY relevance DESC, memory_score DESC, updated_rank DESC, memory_id ASC"
        f" LIMIT {int(limit)}"
    )
    return [str(row["memory_id"]) for row in conn.execute(sql, params)]


def _memory_payloads(memory_ids: list[str]) -> list[dict]:
    """Decode only the payloads the exact scorer still needs to see.

    Rows are returned in ``memory_ids`` order: the downstream scorer is a stable
    sort, so for a tie group the retained order is exactly the one the SQL
    pre-selection chose.  Returning the rows in plan order instead would silently
    reshuffle ties relative to the full-scan oracle.
    """
    if not memory_ids:
        return []
    conn = get_connection()
    by_id: dict[str, dict] = {}
    for start in range(0, len(memory_ids), 400):
        chunk = memory_ids[start : start + 400]
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT memory_id, data_json FROM operational_memory WHERE memory_id IN ({placeholders})",
            chunk,
        ):
            by_id[str(row["memory_id"])] = json.loads(row["data_json"])
    return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]


def _iter_scoped_memory_payloads(conn, domain: str | None, cluster: str | None, *, max_payload_bytes):
    """Stream (bounded JSON, byte length) in row order on the caller's read snapshot."""
    from vector_lake.schema_validator import DOMAIN_ALIASES, canonical_domain

    source = "json_extract(m.data_json, '$.source_page')"
    source_key = (
        f"CASE WHEN lower(substr({source}, -3)) = '.md' "
        f"THEN substr({source}, 1, length({source}) - 3) ELSE {source} END"
    )
    predicates, params, names = [], [], []
    if domain:
        canonical = canonical_domain(domain).lower()
        names = sorted({canonical} | {
            alias.lower() for alias, target in DOMAIN_ALIASES.items()
            if target.lower() == canonical
        })
        placeholders = ','.join('?' for _ in names)
        predicates.append(
            "lower(COALESCE(NULLIF(json_extract(m.data_json, '$.domain'), ''), p.domain, '')) "
            f"IN ({placeholders})"
        )
        params.extend(names)
    if cluster:
        predicates.append(
            "lower(COALESCE(NULLIF(json_extract(m.data_json, '$.topic_cluster'), ''), "
            "p.topic_cluster, '')) = ?"
        )
        params.append(cluster.lower())
    sql = (
        "SELECT CASE WHEN length(CAST(m.data_json AS BLOB)) <= ? THEN m.data_json END, "
        "length(CAST(m.data_json AS BLOB)) FROM operational_memory m LEFT JOIN page_index_nodes p "
        f"ON p.node_key = {source_key} WHERE " + " AND ".join(predicates)
        + " ORDER BY m.rowid"
    )
    params.insert(0, max_payload_bytes)
    from vector_lake import scoped_retrieval_index, retrieval_runtime
    retrieval_runtime.track_connection(conn)
    accelerated = scoped_retrieval_index.scoped_sql(
        conn, names if domain else None, cluster, max_payload_bytes=max_payload_bytes,
    )
    if accelerated is not None:
        sql, params = accelerated
    retrieval_runtime.backend('memory_index' if accelerated else 'memory_scan')
    retrieval_runtime.track_connection(conn)
    cursor = conn.execute(sql, params)
    try:
        for row in cursor:
            retrieval_runtime.checkpoint()
            yield row[0], row[1]
    finally:
        cursor.close()


def search_operational_memory(
    query: str,
    top_k: int = 12,
    memory_types: list[str] | None = None,
    include_history: bool = False,
    domain: str | None = None,
    cluster: str | None = None,
) -> list[dict]:
    if top_k <= 0:
        raise ValueError('top_k must be positive')
    allowed_types = _normalize_memory_types(memory_types)
    terms = _query_terms(query)
    if domain or cluster:
        # Scope and full scoped DF precede TopK; only the winners are retained.
        from vector_lake.scoped_memory_search import search_views
        return search_views(
            terms, [(top_k, include_history, allowed_types)], domain=domain, cluster=cluster,
        )[0]
    backend = _memory_search_backend()

    if backend == "legacy":
        memories = _load_memory_items()
        df, total = _memory_term_frequencies(memories, terms), len(memories)
        return score_memory_items(
            memories, terms, top_k,
            allowed_types=allowed_types, include_history=include_history, df=df, total=total,
        )

    # Creating the projection, its triggers and the initial backfill is memoised
    # per database path inside db_store.  The reconciliation that follows is
    # short-circuited by a data_version/total_changes guard, so in steady state
    # the self-healing check costs two constant-time reads -- and it lives inside
    # the guard because a projection that has been dropped outright must degrade
    # to the full scan rather than raise from the repair attempt.
    window = max(top_k * MEMORY_SEARCH_RESULT_WINDOW, top_k + 16)
    # The corpus frequencies come from the postings, once, and are handed to every scorer below.
    # Deriving them from whatever collection a caller happens to pass is how a *window* of
    # candidates became the corpus: each candidate was then weighted against the other candidates
    # in its own window, and the returned order stopped being the index's order.
    df, total = _memory_corpus_frequencies(terms)
    try:
        initialize_meta_store()
        ensure_operational_memory_index_cheap()
        if backend == "gram":
            memory_ids = _gram_memory_candidates(terms, allowed_types, include_history, window)
            if memory_ids is not None:
                if not memory_ids:
                    return []
                return score_memory_items(
                    _memory_payloads(memory_ids), terms, top_k,
                    allowed_types=allowed_types, include_history=include_history, df=df, total=total,
                )
            log.info(
                "Memory gram index is not usable; answering from the projected scan "
                "(run rebuild_memory_gram_index to restore the indexed path)."
            )
        memory_ids = _indexed_memory_candidates(
            terms, allowed_types, include_history, window, df=df, total=total
        )
    except sqlite3.Error as exc:
        log.warning(
            "Operational memory index unavailable (%s: %s); falling back to the full scan.",
            type(exc).__name__, exc,
        )
        memories = _load_memory_items()
        return score_memory_items(
            memories, terms, top_k,
            allowed_types=allowed_types, include_history=include_history,
            df=_memory_term_frequencies(memories, terms), total=len(memories),
        )

    if not memory_ids:
        # An empty result is legitimate (nothing matched).  An empty memory store
        # with canonical claims present is not: that is the bootstrap case the
        # legacy path repaired by rebuilding memory from claims.
        if not get_connection().execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0]:
            refreshed = rebuild_operational_memory() if load_claims().get("items") else None
            if refreshed and refreshed.get("items"):
                items = list(refreshed["items"].values())
                return score_memory_items(
                    items, terms, top_k,
                    allowed_types=allowed_types, include_history=include_history,
                    df=_memory_term_frequencies(items, terms), total=len(items),
                )
        return []

    # Re-score the SQL window with the oracle so the returned ordering is the one
    # the previous implementation produced, not SQL's rounding of it.
    return score_memory_items(
        _memory_payloads(memory_ids), terms, top_k,
        allowed_types=allowed_types, include_history=include_history, df=df, total=total,
    )


def search_memory_packet_views(
    query: str,
    active_top_k: int = 24,
    history_top_k: int = 12,
    domain: str | None = None,
    cluster: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return ``(active, history_inclusive)`` slices for the Memory Packet.

    Scoped calls share one read snapshot, one complete scoped DF pass and one
    scoring pass with separate bounded active/history TopK heaps. Hidden rows
    cannot consume the active window. No whole-domain JSON list is retained.

    Unscoped ``legacy`` calls share a loaded collection. Unscoped ``gram`` and
    ``index`` calls retain their independent projected searches and filters.
    """
    terms = _query_terms(query)
    if domain or cluster:
        from vector_lake.scoped_memory_search import search_views
        active, history = search_views(
            terms, [(active_top_k, False, None), (history_top_k, True, None)],
            domain=domain, cluster=cluster,
        )
        return active, history
    if _memory_search_backend() == "legacy":
        memories = _load_memory_items()
        # One frequency pass serves both views: it is a full scan of the loaded collection, and
        # the two calls differ only in the row filter and the window size.
        df = _memory_term_frequencies(memories, terms) if terms else None
        total = len(memories)
        return (
            score_memory_items(
                memories, terms, active_top_k, include_history=False, df=df, total=total
            ),
            score_memory_items(
                memories, terms, history_top_k, include_history=True, df=df, total=total
            ),
        )
    return (
        search_operational_memory(query, top_k=active_top_k, include_history=False),
        search_operational_memory(query, top_k=history_top_k, include_history=True),
    )


def build_claim_graph_projection(limit_nodes: int | None = None) -> dict:
    max_degree = 12
    entity_window = 6
    source_window = 4
    entities = load_entities()["items"]
    sources = load_sources()["items"]
    claims = annotated_claims()
    
    # Sort claims by update time to ensure we keep the most recent/relevant if limiting
    claims.sort(key=lambda c: _dt_rank(c.get("updated_at")), reverse=True)

    if limit_nodes is None:
        limit_nodes = 2500  # Hard cap to prevent 3D-force-graph from freezing the browser

    if limit_nodes is not None:
        claims = claims[:limit_nodes]

    nodes = []
    claim_ids = {claim["claim_id"] for claim in claims}
    node_lookup = {}
    degree_map = {}

    for claim in claims:
        subject_names = [
            entities[entity_id]["canonical_name"]
            for entity_id in claim.get("subject_entity_ids", [])
            if entity_id in entities
        ]
        source_pages = [
            sources[source_id]["canonical_source_page"]
            for source_id in claim.get("source_ids", [])
            if source_id in sources
        ]
        compact_text = _compact_claim_text(claim.get("claim_text", ""))
        nodes.append({
            "id": claim["claim_id"],
            "name": claim.get("claim_text", "")[:96] or claim["claim_id"],
            "group": "Claim",
            "validity_state": claim.get("validity_state", "unknown"),
            "claim_type": claim.get("claim_type", "claim"),
            "confidence": claim.get("confidence"),
            "summary": compact_text,
            "subject_entities": subject_names,
            "source_pages": source_pages,
            "degree": 0,
            "updated": claim.get("updated_at", ""),
        })
        node_lookup[claim["claim_id"]] = nodes[-1]
        degree_map[claim["claim_id"]] = 0

    edge_records = {}

    def _record_edge(left_id: str, right_id: str, relation: str, weight: float, force: bool = False):
        if left_id == right_id or left_id not in claim_ids or right_id not in claim_ids:
            return
        source_id, target_id = sorted((left_id, right_id))
        edge_key = (source_id, target_id)

        existing = edge_records.get(edge_key)
        if existing:
            if weight > existing["weight"]:
                existing["weight"] = round(weight, 3)
                existing["relation"] = relation
            return

        if not force and (degree_map[source_id] >= max_degree or degree_map[target_id] >= max_degree):
            return

        edge_records[edge_key] = {
            "source": source_id,
            "target": target_id,
            "weight": round(weight, 3),
            "relation": relation,
        }
        degree_map[source_id] += 1
        degree_map[target_id] += 1

    contradiction_pairs = set()
    entity_buckets = {}
    source_buckets = {}

    for claim in claims:
        claim_id = claim["claim_id"]
        for right_id in claim.get("contradicts", []):
            if right_id in claim_ids:
                contradiction_pairs.add(tuple(sorted((claim_id, right_id))))
        for entity_id in claim.get("subject_entity_ids", []):
            entity_buckets.setdefault(entity_id, []).append(claim_id)
        for source_id in claim.get("source_ids", []):
            source_buckets.setdefault(source_id, []).append(claim_id)

    for source_id, target_id in sorted(contradiction_pairs):
        _record_edge(source_id, target_id, "contradiction", 4.0, force=True)

    entity_pair_counts = {}
    for claim_ids_for_entity in entity_buckets.values():
        ordered_ids = sorted(set(claim_ids_for_entity))
        for index, left_id in enumerate(ordered_ids):
            for right_id in ordered_ids[index + 1 : index + 1 + entity_window]:
                edge_key = tuple(sorted((left_id, right_id)))
                entity_pair_counts[edge_key] = entity_pair_counts.get(edge_key, 0) + 1

    for (source_id, target_id), shared_count in sorted(entity_pair_counts.items(), key=lambda item: (-item[1], item[0])):
        weight = 2.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-entity", weight)

    source_pair_counts = {}
    for claim_ids_for_source in source_buckets.values():
        ordered_ids = sorted(set(claim_ids_for_source))
        for index, left_id in enumerate(ordered_ids):
            for right_id in ordered_ids[index + 1 : index + 1 + source_window]:
                edge_key = tuple(sorted((left_id, right_id)))
                source_pair_counts[edge_key] = source_pair_counts.get(edge_key, 0) + 1

    for (source_id, target_id), shared_count in sorted(source_pair_counts.items(), key=lambda item: (-item[1], item[0])):
        if (source_id, target_id) in edge_records:
            continue
        weight = 1.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-source", weight)

    edges = sorted(edge_records.values(), key=lambda edge: (-edge["weight"], edge["source"], edge["target"]))
    for claim_id, degree in degree_map.items():
        if claim_id in node_lookup:
            node_lookup[claim_id]["degree"] = degree
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "nodes": nodes,
        "edges": edges,
    }


def create_merge_suggestions(limit: int = 20, enqueue: bool = True, include_hazardous: bool = False) -> dict:
    """Queue detected duplicate pairs for human resolution.

    Candidates flagged in ``hazards`` (a shared name claimed by three or more
    distinct entities) are not enqueued by default: resolving one means deciding
    which of three or more live nodes keeps the name, which is not a choice the
    detector can make.  Those candidates stay visible in ``find_merge_candidates``
    and in the returned payload, and can be enqueued with ``include_hazardous``.
    """
    from vector_lake import governance_metrics

    suggestions = governance_metrics.find_merge_candidates(limit=limit)
    hazardous = [s for s in suggestions if s.get("hazards")]
    enqueueable = suggestions if include_hazardous else [s for s in suggestions if not s.get("hazards")]
    if not enqueue:
        return {
            "created": 0,
            "suggestions": enqueueable,
            "skipped_hazardous": len(suggestions) - len(enqueueable),
            "hazardous_pairs": [s["pair_key"] for s in hazardous],
        }

    created = 0
    with governance_queue_session():
        queue = load_governance_queue()
        existing_pairs = {
            item.get("pair_key")
            for item in queue["items"]
            if item.get("type") == "merge"
        }
        for suggestion in enqueueable:
            if suggestion["pair_key"] in existing_pairs:
                continue
            queue["items"].append({
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "merge",
                "title": f"Merge candidate: {suggestion['left_name']} <> {suggestion['right_name']}",
                "description": "; ".join(suggestion["reasons"]),
                "created_at": _utc_now(),
                "status": "pending",
                "source": "merge-suggestions",
                "pair_key": suggestion["pair_key"],
                "affected_ids": [suggestion["left_entity_id"], suggestion["right_entity_id"]],
                "search_queries": [suggestion["left_name"], suggestion["right_name"]],
                "affected_pages": [],
                "merge_candidate": suggestion,
            })
            existing_pairs.add(suggestion["pair_key"])
            created += 1
        save_governance_queue(queue)
    return {"created": created, "suggestions": suggestions}


def create_change_set(
    page_paths: list[str],
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    initialize_meta_store()

    proposed_entities = []
    proposed_claims = []
    proposed_evidence = []
    proposed_source_updates = []
    proposed_edges = []
    affected_ids = []
    page_summaries = []
    page_fingerprints = []

    # One resolution map for the whole batch.  ``claim_extractor`` asks the index which page a link
    # target names (that question has one owner, ``link_resolution``); the memo behind this call
    # costs 0.2 ms, and without hoisting it a 7 000-page rebuild would build the map per page.
    from vector_lake.page_index_projection import link_target_resolver

    resolve_target = link_target_resolver()
    for page_path in page_paths:
        if not os.path.exists(page_path):
            continue
        frontmatter, body, raw_content = read_markdown_file(page_path)
        page_fingerprints.append(hashlib.sha1(raw_content.encode("utf-8")).hexdigest())
        extracted = extract_page_objects(
            page_path, frontmatter, body, resolve_target=resolve_target
        )
        proposed_entities.extend(extracted["entities"])
        proposed_claims.extend(extracted["claims"])
        proposed_evidence.extend(extracted["evidence"])
        proposed_source_updates.extend(extracted["sources"])
        proposed_edges.extend(extracted.get("edges", []))
        affected_ids.extend([record["entity_id"] for record in extracted["entities"]])
        affected_ids.extend([record["claim_id"] for record in extracted["claims"]])
        page_summaries.append(extracted["page_key"])

    idempotency_key = _stable_id(
        "changeset_idem",
        "|".join([origin, *sorted(page_summaries), *sorted(page_fingerprints)]),
    )
    if not force:
        from vector_lake.db_store import get_connection
        conn = get_connection()
        row = conn.execute("SELECT data_json FROM change_sets WHERE f_idempotency_key = ?", (idempotency_key,)).fetchone()
        if row:
            duplicate = json.loads(row[0])
            duplicate["deduplicated"] = True
            return duplicate

    change_set = {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "published" if auto_approve else "pending",
        "summary": summary or f"Sync pages: {', '.join(page_summaries[:5])}",
        "risk_level": "medium" if len(page_paths) > 3 else "low",
        "requires_human_review": not auto_approve,
        "affected_ids": sorted(set(affected_ids)),
        "affected_pages": [os.path.basename(path) for path in page_paths],
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
        "proposed_evidence": proposed_evidence,
        "proposed_source_updates": proposed_source_updates,
        "proposed_edges": proposed_edges,
        "write_contract": {
            "transactional": True,
            "idempotent": True,
            "canonical_targets": ["entities", "claims", "evidence", "sources", "operational_memory"],
        },
    }

    if dry_run:
        # ``dry_run`` was accepted and ignored, so a caller asking for a preview got
        # a fully applied and persisted change set instead.
        preview = copy.deepcopy(change_set)
        preview["dry_run"] = True
        preview["status"] = "dry-run"
        return preview

    with governance_queue_session():
        with transaction():
            if auto_approve:
                apply_change_set(change_set)
                change_set["published_at"] = _utc_now()
            else:
                queue = load_governance_queue()
                queue["items"].append({
                    "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                    "type": "publish-candidate",
                    "title": change_set["summary"],
                    "description": f"Pending publish candidate from {origin}",
                    "created_at": change_set["created_at"],
                    "status": "pending",
                    "source": origin,
                    "affected_ids": change_set["affected_ids"],
                    "change_set_id": change_set["change_set_id"],
                    "search_queries": [],
                    "affected_pages": [os.path.basename(path) for path in page_paths],
                })
                save_governance_queue(queue)

            record_prepared_change_sets([change_set])
    return change_set


def prepare_change_set_from_content(
    filename: str,
    content: str,
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
    pre_parsed_frontmatter: dict | None = None,
) -> dict:
    """Build one canonical change set without applying or persisting it."""
    initialize_meta_store()
    if pre_parsed_frontmatter is not None:
        frontmatter = pre_parsed_frontmatter
        _, body = split_frontmatter(content)
    else:
        frontmatter, body = split_frontmatter(content)
    extracted = extract_page_objects(filename, frontmatter, body)
    if not extracted.get("entities") and not filename.startswith("System_"):
        raise ValueError(f"No canonical entity could be extracted from {filename}.")

    page_key = extracted["page_key"]
    fingerprint = hashlib.sha1(content.encode("utf-8")).hexdigest()
    idempotency_key = _stable_id("changeset_idem", "|".join([origin, page_key, fingerprint]))

    proposed_entities = extracted.get("entities", [])
    proposed_claims = extracted.get("claims", [])
    return {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "published" if auto_approve else "pending",
        "summary": summary or f"Sync page: {page_key}",
        "risk_level": "low",
        "requires_human_review": not auto_approve,
        "affected_ids": sorted({
            *[record["entity_id"] for record in proposed_entities],
            *[record["claim_id"] for record in proposed_claims],
        }),
        "affected_pages": [filename],
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
        "proposed_evidence": extracted.get("evidence", []),
        "proposed_source_updates": extracted.get("sources", []),
        "proposed_edges": extracted.get("edges", []),
        "write_contract": {
            "transactional": True,
            "idempotent": True,
            "canonical_targets": ["entities", "claims", "evidence", "sources", "operational_memory"],
        },
    }


def record_prepared_change_sets(change_sets: list[dict]) -> int:
    """Persist prepared change sets once without scanning the JSON history."""
    if not change_sets:
        return 0
    conn = get_connection()
    now = _utc_now()
    added = 0
    with transaction():
        for change_set in change_sets:
            idempotency_key = str(change_set.get("idempotency_key") or change_set["change_set_id"])
            reserved = conn.execute(
                "INSERT OR IGNORE INTO change_set_idempotency "
                "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
                (idempotency_key, change_set["change_set_id"], now),
            )
            if not reserved.rowcount:
                continue
            conn.execute(
                "INSERT INTO change_sets (change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
                (change_set["change_set_id"], json.dumps(change_set, ensure_ascii=False), now),
            )
            added += 1
    return added


def create_change_set_from_content(
    filename: str,
    content: str,
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
) -> dict:
    """Create a canonical change set without requiring Markdown to be written first."""
    change_set = prepare_change_set_from_content(
        filename,
        content,
        origin,
        summary=summary,
        auto_approve=auto_approve,
    )
    existing_change_sets = load_change_sets()
    for existing in existing_change_sets["items"]:
        if existing.get("idempotency_key") == change_set["idempotency_key"]:
            duplicate = copy.deepcopy(existing)
            duplicate["deduplicated"] = True
            return duplicate

    with governance_queue_session():
        with transaction():
            if auto_approve:
                apply_change_sets_batch([change_set])
                change_set["published_at"] = _utc_now()
            else:
                queue = load_governance_queue()
                queue["items"].append({
                    "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                    "type": "publish-candidate",
                    "title": change_set["summary"],
                    "description": f"Pending publish candidate from {origin}",
                    "created_at": change_set["created_at"],
                    "status": "pending",
                    "source": origin,
                    "affected_ids": change_set["affected_ids"],
                    "change_set_id": change_set["change_set_id"],
                    "search_queries": [],
                    "affected_pages": [filename],
                })
                save_governance_queue(queue)

            record_prepared_change_sets([change_set])
    return change_set


def _apply_change_sets_batch_unchecked(change_sets: list[dict]) -> list[dict]:
    """Apply a page-scoped canonical delta inside an existing transaction."""
    if not change_sets:
        return []
    affected_pages = {
        page
        for change_set in change_sets
        for page in change_set.get("affected_pages", [])
    }
    affected_page_keys = {
        page[:-3] if page.endswith(".md") else page
        for page in affected_pages
    }
    proposed_entities = [record for item in change_sets for record in item.get("proposed_entities", [])]
    proposed_claims = [record for item in change_sets for record in item.get("proposed_claims", [])]
    proposed_evidence = [record for item in change_sets for record in item.get("proposed_evidence", [])]
    proposed_sources = [record for item in change_sets for record in item.get("proposed_source_updates", [])]
    proposed_edges = [record for item in change_sets for record in item.get("proposed_edges", [])]

    conn = get_connection()
    old_entity_ids: set[str] = set()
    old_claim_ids: set[str] = set()
    old_claim_rows = []
    if affected_page_keys:
        affected_page_params = tuple(sorted(affected_page_keys))
        placeholders = ",".join("?" for _ in affected_page_params)
        old_entity_ids = {
            row["entity_id"]
            for row in conn.execute(
                f"SELECT entity_id FROM entities WHERE f_page_key IN ({placeholders})",
                affected_page_params,
            )
        }
        old_claim_rows = conn.execute(
            f"SELECT claim_id, claim_text, data_json, updated_at FROM claims "
            f"WHERE f_page_key IN ({placeholders})",
            affected_page_params,
        ).fetchall()
        old_claim_ids = {row["claim_id"] for row in old_claim_rows}
        # ``claim_graph_edges`` carries two key spaces and the previous predicate used one for
        # the other.  Its rows are page keys on the live corpus (``Concept_...``/``Source_...``,
        # plus the ``entity_<hex>`` ids ``delete_node_cascade`` removes by), while
        # ``save_graph_edges`` is also handed claim ids for cross-page edges.  Filtering the
        # table only by claim ids matched nothing in the page-key space: a rewritten page kept
        # one stale edge set per rewrite (232 rows on the live corpus still point at a page key
        # that no longer exists).  Both arms are therefore needed and scoped differently:
        #   * page keys -- the outgoing edges of the pages this delta owns, re-addable from the
        #     new content by ``save_graph_edges``;
        #   * the claims this delta touches, both directions -- the documented contract for the
        #     claim-id space (``test_change_set_apply_deletes_both_edge_directions``), where an
        #     edge owned by another page names a claim of this one.
        edge_keys = sorted(set(affected_page_keys) | old_entity_ids)
        touched_claim_ids = sorted(
            old_claim_ids | {record["claim_id"] for record in proposed_claims if record.get("claim_id")}
        )
        if edge_keys:
            # One JSON parameter instead of a placeholder list: see ``_json_id_list`` for why
            # the placeholder form cannot hold a bulk batch's key set.
            conn.execute(
                "DELETE FROM claim_graph_edges WHERE source_id IN (SELECT value FROM json_each(?))",
                (_json_id_list(edge_keys),),
            )
        if touched_claim_ids:
            touched_claim_json = _json_id_list(touched_claim_ids)
            conn.execute(
                "DELETE FROM claim_graph_edges "
                "WHERE source_id IN (SELECT value FROM json_each(?)) "
                "OR target_id IN (SELECT value FROM json_each(?))",
                (touched_claim_json, touched_claim_json),
            )
        conn.execute(
            f"DELETE FROM entities WHERE f_page_key IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM claims WHERE f_page_key IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM evidence WHERE f_page_key IN ({placeholders})",
            affected_page_params,
        )
        # The page-space edge projection belongs to the indexer and is derived from the published
        # file; this path writes no edge table.

    _upsert_canonical_records("entities", "entity_id", proposed_entities)
    _upsert_canonical_records("claims", "claim_id", proposed_claims)
    _upsert_canonical_records("evidence", "evidence_id", proposed_evidence)
    _upsert_canonical_records("sources", "source_id", proposed_sources)
    _refresh_alias_delta(old_entity_ids, proposed_entities)
    _refresh_operational_memory_delta(old_claim_ids, proposed_claims)
    from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

    sync_timeline_events_for_claim_delta(old_claim_rows, proposed_claims)
    save_graph_edges(proposed_edges)
    memory_count = conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0]
    for change_set in change_sets:
        change_set["operational_memory_count"] = int(memory_count)
    # Obsolete view_builder block removed to prevent ImportError warnings
    # try:
    #     from vector_lake import view_builder
    # 
    #     change_set["view_rebuild"] = view_builder.rebuild_views_for_change_set(change_set)
    # except Exception as exc:
    #     log.warning(f"View rebuild failed for {change_set.get('change_set_id')}: {exc}")
    return change_sets


def apply_change_sets_batch(change_sets: list[dict]) -> list[dict]:
    """Atomically apply page-scoped canonical and derived projection deltas."""
    with transaction():
        return _apply_change_sets_batch_unchecked(change_sets)


def apply_change_set(change_set: dict) -> dict:
    apply_change_sets_batch([change_set])
    return change_set


def publish_change_sets(limit: int | None = None) -> dict:
    # This path reaches ``_refresh_operational_memory_delta``, which names the generated columns on
    # ``operational_memory``; a database predating them has to converge first (the protocol the
    # ``entities`` batch had to learn).  No caller in the tree today, so this is defensive, not a fix
    # for an observed failure.
    initialize_meta_store()
    from vector_lake.db_store import get_connection, transaction
    conn = get_connection()
    rows = conn.execute("SELECT change_set_id, data_json FROM change_sets WHERE f_status = 'pending'").fetchall()
    
    published = 0
    published_ids = []
    
    for row in rows:
        change_set = json.loads(row[1])
        apply_change_set(change_set)
        change_set["status"] = "published"
        change_set["published_at"] = _utc_now()
        
        with transaction():
            conn.execute("UPDATE change_sets SET data_json = ?, updated_at = ? WHERE change_set_id = ?", 
                         (json.dumps(change_set, ensure_ascii=False), _utc_now(), change_set["change_set_id"]))
            
        published += 1
        published_ids.append(change_set["change_set_id"])
        if limit is not None and published >= limit:
            break

    with governance_queue_session():
        queue = load_governance_queue()
        for item in queue["items"]:
            if item.get("change_set_id") in published_ids:
                item["status"] = "published"
                item["resolved_at"] = _utc_now()
        save_governance_queue(queue)
    return {"published": published, "change_set_ids": published_ids}

def pending_change_sets() -> list:
    from vector_lake.db_store import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT data_json FROM change_sets WHERE f_status = 'pending'").fetchall()
    return [json.loads(r[0]) for r in rows]


def pending_governance_items() -> list:
    return [item for item in load_governance_queue()["items"] if item.get("status") == "pending"]


def _resolve_page_source(path: str) -> Path | None:
    """Resolve a caller-supplied page reference to an on-disk Markdown path.

    Callers legitimately pass either an absolute path or a bare wiki basename.
    Testing ``os.path.exists`` on the raw string treated a relative basename as
    a deleted page and deleted its canonical entity while the Markdown file was
    still on disk, which desynchronised the canonical store from the wiki.
    """
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = get_wiki_dir() / candidate.name
    return candidate if candidate.exists() else None


def sync_pages_to_canonical(page_paths: list[str], origin: str, auto_approve: bool = True, summary: str | None = None) -> dict | None:
    existing_paths = []
    deleted_paths = []
    for path in page_paths:
        if not path:
            continue
        resolved = _resolve_page_source(path)
        if resolved is not None:
            existing_paths.append(str(resolved))
        else:
            deleted_paths.append(str(path))
                
    # V10.1 Delete orphaned entities in SQLite when Markdown file is deleted/renamed
    if deleted_paths:
        for path in deleted_paths:
            basename = os.path.basename(path)
            if basename.endswith(".md"):
                page_key = basename[:-3]
                entity_id = _stable_id("entity", page_key)
                delete_entity(entity_id)
                import logging
                logging.getLogger("governance").info(f"Deleted orphan entity {entity_id} ({page_key}) from SQLite due to missing markdown file.")

    if not existing_paths:
        return None
    return create_change_set(existing_paths, origin=origin, summary=summary, auto_approve=auto_approve)


def migrate_existing_wiki(dry_run: bool = False) -> dict:
    wiki_dir = get_wiki_dir()
    page_paths = []
    for name in os.listdir(wiki_dir):
        if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md"):
            page_paths.append(str(wiki_dir / name))

    if dry_run:
        counts = {"entities": 0, "claims": 0, "evidence": 0, "sources": 0, "valid_pages": 0}
        for page_path in page_paths:
            frontmatter, body, _ = read_markdown_file(page_path)
            extracted = extract_page_objects(page_path, frontmatter, body)
            if extracted.get("entities") or os.path.basename(page_path).startswith("System_"):
                counts["valid_pages"] += 1
            counts["entities"] += len(extracted.get("entities", []))
            counts["claims"] += len(extracted.get("claims", []))
            counts["evidence"] += len(extracted.get("evidence", []))
            counts["sources"] += len(extracted.get("sources", []))
        return {
            "dry_run": True,
            "pages_scanned": len(page_paths),
            **counts,
        }

    initialize_meta_store()
    change_set = create_change_set(page_paths, origin="migrate-v8", summary="V8 migration", auto_approve=True, force=True)
    migrated_page_keys = {item.get("page_key") for item in change_set.get("proposed_entities", []) if item.get("page_key")}
    canonical_page_keys = {item.get("page_key") for item in load_entities()["items"].values() if item.get("page_key")}
    stale_entities = canonical_page_keys - migrated_page_keys

    return {
        "dry_run": False,
        "change_set_id": change_set["change_set_id"],
        "pages_scanned": len(page_paths),
        "entities": len(load_entities()["items"]),
        "claims": len(load_claims()["items"]),
        "evidence": len(load_evidence()["items"]),
        "sources": len(load_sources()["items"]),
        "stale_entities_preserved": len(stale_entities),
    }


def ensure_canonical_store_populated() -> dict:
    """Report the canonical-store bootstrap, performing it only when the store is empty.

    The emptiness question used to be answered by decoding every row of ``entities``,
    ``claims`` and ``sources`` -- 102 117 + 7 924 + 4 107 JSON payloads on the live corpus,
    3.5 s of a 8.1 s ``trace`` -- because the same call also returned the decoded lengths.
    ``COUNT(*)`` answers both the question and the lengths with no decode, because each map
    store is keyed by the table's primary key, so ``len(store["items"])`` is the row count
    (verified against the live corpus: 7 924 / 102 117 / 4 107).
    """
    initialize_meta_store()
    conn = get_connection()
    counts = {}
    for table in ("entities", "claims", "sources"):
        _validate_table_name(table)
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    wiki_pages = _count_wiki_pages()

    if counts["claims"] or counts["entities"] or counts["sources"] or wiki_pages == 0:
        return {
            "bootstrapped": False,
            "entities": counts["entities"],
            "claims": counts["claims"],
            "sources": counts["sources"],
            "pages_scanned": wiki_pages,
        }

    log.info("Canonical store is empty; bootstrapping V8 objects from existing wiki pages.")
    result = migrate_existing_wiki(dry_run=False)
    result["bootstrapped"] = True
    return result


def governance_projection() -> dict:
    ensure_canonical_store_populated()
    entities = load_entities()
    sources = load_sources()
    memory_objects = load_memory_objects()
    if not memory_objects.get("items") and load_claims().get("items"):
        memory_objects = rebuild_operational_memory()
    queue = load_governance_queue()
    annotated = annotated_claims()
    claim_index = {claim["claim_id"]: copy.deepcopy(claim) for claim in annotated}
    return {
        "entity_index": copy.deepcopy(entities["items"]),
        "claim_index": claim_index,
        "memory_index": copy.deepcopy(memory_objects["items"]),
        "memory_type_counts": copy.deepcopy(memory_objects.get("memory_type_counts", {})),
        "source_index": copy.deepcopy(sources["items"]),
        "pending_change_set_count": len([item for item in queue["items"] if item.get("status") == "pending"]),
        "claim_graph": build_claim_graph_projection(),
    }


def enqueue_governance_items(items: list[dict]) -> int:
    """Append governance items under one queue lock (single load/save cycle)."""
    if not items:
        return 0
    with governance_queue_session():
        queue = load_governance_queue()
        queue.setdefault("items", []).extend(items)
        save_governance_queue(queue)
    return len(items)


def enqueue_governance_item(item_type: str, title: str, description: str, source: str, search_queries: list, affected_pages: list):
    import uuid
    item = {
        "item_id": f"gov_{uuid.uuid4().hex[:12]}",
        "type": item_type,
        "title": title,
        "description": description,
        "created_at": _utc_now(),
        "status": "pending",
        "source": source,
        "search_queries": search_queries,
        "affected_pages": affected_pages,
    }
    enqueue_governance_items([item])
    return item["item_id"]


