"""Bounded, recoverable maintenance for memories of deleted canonical pages.

Freeze is read-only against the source database. Apply requires that exact frozen
scope; no memory text, page name, or source identifier is printed. Index maintenance
uses the database's native triggers, including gram-index retirement markers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMNS = ("memory_id", "memory_type", "score", "data_json", "updated_at", "status", "ttl")
SQL_COLUMNS = ", ".join("m." + column for column in COLUMNS)
MAX_SCOPE_ROWS = 2000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
ORPHAN_QUERY = """
WITH memory_scope AS (
  SELECT m.*, CASE WHEN substr(json_extract(m.data_json, '$.source_page'), -3) = '.md'
    THEN substr(json_extract(m.data_json, '$.source_page'), 1,
                length(json_extract(m.data_json, '$.source_page')) - 3)
    ELSE json_extract(m.data_json, '$.source_page') END AS source_page_key
  FROM operational_memory m
)
SELECT """ + SQL_COLUMNS + """ FROM memory_scope m
WHERE m.f_source_claim_id IS NOT NULL AND m.f_source_claim_id != ''
AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id = m.f_source_claim_id)
AND json_type(m.data_json, '$.source_page') = 'text'
AND json_extract(m.data_json, '$.source_page') != ''
AND NOT EXISTS (SELECT 1 FROM page_index_nodes p WHERE p.node_key = m.source_page_key)
AND NOT EXISTS (
  SELECT 1 FROM entities e
  WHERE e.f_page_key = m.source_page_key)
ORDER BY m.memory_id
"""


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fingerprint(rows, check=None):
    digest = hashlib.sha256()
    for row in rows:
        if check is not None:
            check()
        digest.update(canonical_bytes(dict(row)))
        digest.update(b"\n")
    return digest.hexdigest()


class Deadline:
    def __init__(self, seconds):
        if not 0 < seconds <= 145:
            raise ValueError("Write deadline must be positive and at most 145 seconds")
        self.end = time.monotonic() + seconds

    def check(self):
        if time.monotonic() >= self.end:
            raise TimeoutError("Maintenance deadline exceeded; transaction must roll back")

    def interrupt(self):
        return int(time.monotonic() >= self.end)


def clear_authorizer(conn):
    # Passing None only disables the callback on Python >=3.11. On 3.10 it
    # leaves an unusable callback which silently denies subsequent SQL.
    if sys.version_info >= (3, 11):
        conn.set_authorizer(None)
    else:
        conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)


def cleanup_connection(conn, old_timeout):
    errors = []
    actions = (
        ("authorizer", lambda: clear_authorizer(conn)),
        ("progress_handler", lambda: conn.set_progress_handler(None, 0)),
        ("busy_timeout", lambda: conn.execute("PRAGMA busy_timeout=" + str(old_timeout))),
    )
    for name, action in actions:
        try:
            action()
        except sqlite3.Error as exc:
            errors.append({"action": name, "error_type": type(exc).__name__})
    return errors


def candidates(conn, limit=MAX_SCOPE_ROWS + 1):
    return [dict(row) for row in conn.execute(ORPHAN_QUERY + " LIMIT ?", (limit,))]


def require_settled_authority(conn):
    pending = conn.execute(
        "SELECT COUNT(*) FROM mutation_outbox WHERE status IS NULL OR status NOT IN ('completed', 'superseded')"
    ).fetchone()[0]
    if pending:
        raise RuntimeError("Canonical mutations are pending; orphan ownership is not settled")


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def write_json(path, data):
    with Path(path).open("x", encoding="utf-8") as sink:
        sink.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        sink.flush()
        os.fsync(sink.fileno())


def freeze(db_path, directory, expected_count):
    started = time.monotonic()
    budget = Deadline(120)
    if not 0 < expected_count <= MAX_SCOPE_ROWS:
        raise ValueError("Frozen scope must contain between 1 and 2000 rows")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    backup_path = directory / "database.sqlite.bak"
    with readonly(db_path) as source:
        source.execute("BEGIN")
        source.set_progress_handler(budget.interrupt, 1000)
        require_settled_authority(source)
        rows = candidates(source, expected_count + 1)
        if len(rows) != expected_count:
            raise RuntimeError(f"Frozen scope count changed: expected {expected_count}, found {len(rows)}")
        with sqlite3.connect(backup_path) as destination:
            def progress(_status, _remaining, _total):
                budget.check()
            source.backup(destination, pages=4096, progress=progress)
        source.rollback()
    # Verify the actual backup's target records, not merely its existence.
    with readonly(backup_path) as backup:
        backup.set_progress_handler(budget.interrupt, 1000)
        if fingerprint(candidates(backup), budget.check) != fingerprint(rows, budget.check):
            raise RuntimeError("Backup target records differ from the frozen source snapshot")
    budget.check()
    write_json(directory / "records.json", rows)
    manifest = {
        "version": 1, "database": str(Path(db_path).resolve()),
        "count": len(rows), "scope_sha256": fingerprint(rows),
        "database_backup": str(backup_path.resolve()),
        "records": "records.json", "created_at": time.time(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    budget.check()
    write_json(directory / "manifest.json", manifest)
    return {"mode": "freeze", "count": len(rows), "scope_sha256": manifest["scope_sha256"],
            "backup_verified": True, "elapsed_seconds": manifest["elapsed_seconds"]}


def load_archive(directory, db_path):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if Path(manifest["database"]).resolve() != Path(db_path).resolve():
        raise RuntimeError("Archive belongs to a different database")
    if manifest.get("version") != 1 or manifest.get("records") != "records.json":
        raise RuntimeError("Unsupported archive manifest")
    if not isinstance(manifest.get("count"), int) or not 0 < manifest["count"] <= MAX_SCOPE_ROWS:
        raise RuntimeError("Recovery count exceeds the bounded scope")
    if (directory / "records.json").stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Recovery archive exceeds the bounded input size")
    rows = json.loads((directory / "records.json").read_text(encoding="utf-8"))
    if len(rows) != manifest["count"] or fingerprint(rows) != manifest["scope_sha256"]:
        raise RuntimeError("Recovery records failed the frozen-scope check")
    if not rows or len({row["memory_id"] for row in rows}) != len(rows):
        raise RuntimeError("Recovery scope must contain unique, nonempty memory identifiers")
    if any(set(row) != set(COLUMNS) for row in rows):
        raise RuntimeError("Recovery record columns do not match the canonical table")
    return manifest, rows


def recovery_rowids(directory, manifest, rows, budget=None):
    budget = budget or Deadline(120)
    budget.check()
    backup_path = Path(directory) / "database.sqlite.bak"
    if Path(manifest["database_backup"]).resolve() != backup_path.resolve():
        raise RuntimeError("Recovery database must be the frozen archive-local snapshot")
    ids_json = json.dumps([row["memory_id"] for row in rows])
    selected = "memory_id IN (SELECT value FROM json_each(?))"
    with readonly(backup_path) as conn:
        conn.set_progress_handler(budget.interrupt, 1000)
        stored = conn.execute("SELECT " + ", ".join(COLUMNS) + " FROM operational_memory WHERE " + selected + " ORDER BY memory_id", (ids_json,))
        if fingerprint(stored, budget.check) != manifest["scope_sha256"]:
            raise RuntimeError("Frozen backup does not contain the exact recovery records")
        positions = {}
        for row in conn.execute("SELECT memory_id, rowid FROM operational_memory WHERE " + selected, (ids_json,)):
            budget.check()
            positions[row["memory_id"]] = row["rowid"]
    envelope = {"version": 1, "scope_sha256": manifest["scope_sha256"], "canonical_rowids": positions}
    path = Path(directory) / "recovery-rowids.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != envelope:
            raise RuntimeError("Recovery rowid envelope differs from the immutable backup")
    else:
        budget.check()
        write_json(path, envelope)
    budget.check()
    return positions


def check_scope(conn, manifest):
    require_settled_authority(conn)
    current = candidates(conn, manifest["count"] + 1)
    if len(current) != manifest["count"] or fingerprint(current) != manifest["scope_sha256"]:
        raise RuntimeError("Live target scope changed; refusing to broaden or replace the approved scope")
    return current


def table_counts(conn):
    tables = ("claims", "evidence", "entities", "page_index_nodes", "claim_index", "timeline_events")
    return {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}


def maintain(db_path, directory, mode="dry-run", *, approved_count=None,
             approved_sha256=None, timeout_seconds=120):
    started = time.monotonic()
    budget = Deadline(timeout_seconds)
    manifest, rows = load_archive(directory, db_path)
    if mode == "dry-run":
        with readonly(db_path) as conn:
            conn.execute("BEGIN")
            conn.set_progress_handler(budget.interrupt, 1000)
            check_scope(conn, manifest)
        return {"mode": mode, "would_delete": len(rows), "scope_sha256": manifest["scope_sha256"]}
    if mode not in ("apply", "restore"):
        raise ValueError("Unknown maintenance mode")
    if approved_count != manifest["count"] or approved_sha256 != manifest["scope_sha256"]:
        raise RuntimeError("Explicit approved count/hash do not match the immutable recovery scope")
    from vector_lake import db_store
    if Path(db_store.get_db_path()).resolve() != Path(db_path).resolve():
        raise RuntimeError("Native runtime database does not match the frozen database")
    conn = db_store.get_connection()
    if conn.in_transaction or getattr(db_store._LOCAL, "in_transaction", False):
        raise RuntimeError("Maintenance must own its transaction; nested entry is forbidden")
    positions = recovery_rowids(directory, manifest, rows, budget)
    # Older SQLite declares the eponymous table via internal schema actions.
    # Prepare this fixed, empty read before installing the mutation-only guard;
    # never authorize sqlite_master writes during the protected transaction.
    conn.execute("SELECT value FROM json_each('[]')").fetchall()
    old_busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    ids_json = json.dumps([row["memory_id"] for row in rows])
    selected = "memory_id IN (SELECT value FROM json_each(?))"
    unrelated_sql = "SELECT " + ", ".join(COLUMNS) + " FROM operational_memory WHERE NOT (" + selected + ") ORDER BY memory_id"
    index_unrelated_sql = "SELECT * FROM operational_memory_index WHERE NOT (" + selected + ") ORDER BY memory_id"
    writable = {"operational_memory", "operational_memory_index", "operational_memory_gram_dirty",
                # The gram index's identity map: the canonical insert trigger allocates one row
                # for a memory that has never been indexed.  Append-only, and nothing here
                # deletes from it -- a document number is never handed to a second identity.
                "operational_memory_gram_docs"}
    append_only = {"operational_memory_gram_dirty", "operational_memory_gram_docs"}

    def authorizer(action, table, _column, _database, _trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if table not in writable or (table in append_only and action != sqlite3.SQLITE_INSERT):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    operation_id = mode + "-" + str(time.time_ns())
    prepared_path = Path(directory) / (operation_id + ".prepared.json")
    budget.check()
    write_json(prepared_path, {"state": "prepared", "mode": mode, "count": len(rows),
                              "scope_sha256": manifest["scope_sha256"]})
    committed = False
    try:
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(budget.interrupt, 1000)
        budget.check()
        with db_store.transaction():
            conn.execute("PRAGMA busy_timeout=5000")
            budget.check()
            require_settled_authority(conn)
            if mode == "apply":
                check_scope(conn, manifest)
            elif conn.execute("SELECT COUNT(*) FROM operational_memory WHERE " + selected, (ids_json,)).fetchone()[0]:
                raise RuntimeError("Restore would overwrite an existing memory; refusing")
            before_other = fingerprint(conn.execute(unrelated_sql, (ids_json,)), budget.check)
            before_index = fingerprint(conn.execute(index_unrelated_sql, (ids_json,)), budget.check)
            before_markers = {row[0] for row in conn.execute("SELECT doc FROM operational_memory_gram_dirty")}
            before_tables = table_counts(conn)
            projected = [row[0] for row in conn.execute("SELECT rowid FROM operational_memory_index WHERE " + selected, (ids_json,))]
            if mode == "apply":
                live_positions = {row["memory_id"]: row["rowid"] for row in conn.execute(
                    "SELECT memory_id, rowid FROM operational_memory WHERE " + selected, (ids_json,))}
                if live_positions != positions:
                    raise RuntimeError("Canonical rowids changed since the frozen snapshot")
                if len(projected) != len(rows):
                    raise RuntimeError("Target lookup projection is incomplete; refusing deletion")
                deleted = conn.execute("DELETE FROM operational_memory WHERE " + selected, (ids_json,)).rowcount
                if deleted != len(rows):
                    raise RuntimeError("Delete count differs from the frozen recovery scope")
                if conn.execute("SELECT COUNT(*) FROM operational_memory_index WHERE " + selected, (ids_json,)).fetchone()[0]:
                    raise RuntimeError("Native memory-index deletion did not cascade")
            else:
                rowid_json = json.dumps(list(positions.values()))
                if conn.execute("SELECT COUNT(*) FROM operational_memory WHERE rowid IN (SELECT value FROM json_each(?))", (rowid_json,)).fetchone()[0]:
                    raise RuntimeError("Restore would collide with an existing canonical rowid")
                placeholders = ", ".join("?" for _ in range(len(COLUMNS) + 1))
                conn.executemany("INSERT INTO operational_memory (rowid, " + ", ".join(COLUMNS) + ") VALUES (" + placeholders + ")",
                                 [(positions[row["memory_id"]],) + tuple(row[column] for column in COLUMNS) for row in rows])
                restored = conn.execute("SELECT " + ", ".join(COLUMNS) + " FROM operational_memory WHERE " + selected + " ORDER BY memory_id", (ids_json,))
                if fingerprint(restored, budget.check) != manifest["scope_sha256"]:
                    raise RuntimeError("Restored records differ from the frozen original")
                expected = conn.execute("SELECT m.memory_id, " + db_store._om_index_value_expr("m") + " FROM operational_memory m WHERE " + selected + " ORDER BY memory_id", (ids_json,))
                actual = conn.execute("SELECT " + ", ".join(db_store._OM_INDEX_COLUMNS) + " FROM operational_memory_index WHERE " + selected + " ORDER BY memory_id", (ids_json,))
                expected_rows = []
                text_position = db_store._OM_INDEX_COLUMNS.index("source_updated_at")
                for row in expected:
                    budget.check()
                    values = list(row)
                    values[text_position] = conn.execute("SELECT CAST(? AS TEXT)", (values[text_position],)).fetchone()[0]
                    expected_rows.append(tuple(values))
                if [tuple(row) for row in actual] != expected_rows:
                    raise RuntimeError("Restored lookup projection differs from its canonical records")
            # The gram queue is keyed on document identities, not on projection row ids.  Reading it
            # through the projection would compare two namespaces: a memory re-derived by the
            # lookup triggers gets a new row id but keeps the document number its postings name.
            gram_docs = [row[0] for row in conn.execute(
                "SELECT doc FROM operational_memory_gram_docs WHERE memory_id IN (SELECT value FROM json_each(?))",
                (ids_json,))]
            doc_json = json.dumps(gram_docs)
            retired = conn.execute("SELECT COUNT(*) FROM operational_memory_gram_dirty WHERE doc IN (SELECT value FROM json_each(?))", (doc_json,)).fetchone()[0]
            if retired != len(gram_docs):
                raise RuntimeError("Native gram retirement/dirty markers did not cascade")
            after_markers = {row[0] for row in conn.execute("SELECT doc FROM operational_memory_gram_dirty")}
            if after_markers != before_markers | set(gram_docs):
                raise RuntimeError("Unrelated gram markers changed; rolling back")
            if fingerprint(conn.execute(unrelated_sql, (ids_json,)), budget.check) != before_other:
                raise RuntimeError("Unrelated canonical memories changed; rolling back")
            if fingerprint(conn.execute(index_unrelated_sql, (ids_json,)), budget.check) != before_index:
                raise RuntimeError("Unrelated lookup projection changed; rolling back")
            if table_counts(conn) != before_tables:
                raise RuntimeError("Checked authority-table counts changed; rolling back")
            budget.check()
        committed = True
    finally:
        cleanup_errors = cleanup_connection(conn, old_busy_timeout)
    receipt = {"state": "committed" if committed else "not-committed", "mode": mode,
               "connection_cleanup_errors": cleanup_errors, "affected": len(rows), "scope_sha256": manifest["scope_sha256"],
               "native_index_cascade_verified": True, "gram_markers_verified": len(gram_docs),
               "unrelated_gram_markers_preserved": True, "recovery_rowids_verified": True,
               "unrelated_memories_and_lookup_preserved": True, "other_authority_writes_prohibited": True,
               "checked_authority_table_counts_preserved": True,
               "elapsed_seconds": round(time.monotonic() - started, 3), "receipt_published": True}
    try:
        write_json(Path(directory) / (operation_id + ".committed.json"), receipt)
    except OSError as exc:
        receipt.update(receipt_published=False, receipt_error_type=type(exc).__name__,
                       recovery_operation_state=str(prepared_path))
    return receipt


def refresh_triggers(db_path, directory, *, approved_count, approved_sha256):
    """Refresh only the two owned lookup triggers; never rebuild projection rows."""
    started = time.monotonic()
    budget = Deadline(120)
    manifest, _rows = load_archive(directory, db_path)
    if approved_count != manifest["count"] or approved_sha256 != manifest["scope_sha256"]:
        raise RuntimeError("Trigger refresh is not bound to the approved recovery scope")
    from vector_lake import db_store
    if Path(db_store.get_db_path()).resolve() != Path(db_path).resolve():
        raise RuntimeError("Native runtime database does not match the frozen database")
    conn = db_store.get_connection()
    if conn.in_transaction or getattr(db_store._LOCAL, "in_transaction", False):
        raise RuntimeError("Trigger refresh must own its transaction")
    conn.execute("SELECT value FROM json_each('[]')").fetchall()
    names = ("trg_om_index_insert", "trg_om_index_update")
    names_json = json.dumps(names)
    old_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT IN (SELECT value FROM json_each(?)) ORDER BY type,name"
    canonical_sql = "SELECT " + ",".join(COLUMNS) + " FROM operational_memory ORDER BY memory_id"
    index_sql = "SELECT * FROM operational_memory_index ORDER BY memory_id"

    def authorizer(action, name, _column, _database, _trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and name != "sqlite_master":
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER) and name not in names:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    operation = "trigger-refresh-" + str(time.time_ns())
    committed = False
    try:
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(budget.interrupt, 1000)
        budget.check()
        with db_store.transaction():
            conn.execute("PRAGMA busy_timeout=5000")
            check_scope(conn, manifest)
            baseline = {row["name"]: row["sql"] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (SELECT value FROM json_each(?))", (names_json,))}
            if set(baseline) != set(names):
                raise RuntimeError("Owned trigger baseline is incomplete")
            write_json(Path(directory) / (operation + ".baseline.json"), baseline)
            before_schema = fingerprint(conn.execute(schema_sql, (names_json,)), budget.check)
            before_memory = fingerprint(conn.execute(canonical_sql), budget.check)
            before_lookup = fingerprint(conn.execute(index_sql), budget.check)
            present = {row["name"] for row in conn.execute("PRAGMA table_info(operational_memory_index)")}
            if not set(db_store._OM_INDEX_COLUMNS) <= present:
                raise RuntimeError("Lookup table format is incompatible; a table migration is not authorized")
            columns = ", ".join(db_store._OM_INDEX_COLUMNS)
            upsert = ", ".join(f"{name}=excluded.{name}" for name in db_store._OM_INDEX_COLUMNS if name != "memory_id")
            definitions = {}
            for name, event in zip(names, ("AFTER INSERT", "AFTER UPDATE")):
                sql = (f"CREATE TRIGGER {name} {event} ON operational_memory BEGIN "
                       f"INSERT INTO operational_memory_index ({columns}) "
                       f"VALUES (NEW.memory_id,{db_store._om_index_value_expr('NEW')}) "
                       f"ON CONFLICT(memory_id) DO UPDATE SET {upsert}; END")
                conn.execute("DROP TRIGGER " + name)
                conn.execute(sql)
                definitions[name] = sql
            actual = {row["name"]: row["sql"] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE name IN (SELECT value FROM json_each(?))", (names_json,))}
            if actual != definitions:
                raise RuntimeError("Effective trigger definitions differ from the authorized native definitions")
            if fingerprint(conn.execute(schema_sql, (names_json,)), budget.check) != before_schema:
                raise RuntimeError("Unrelated schema changed; rolling back")
            if fingerprint(conn.execute(canonical_sql), budget.check) != before_memory:
                raise RuntimeError("Canonical memories changed during trigger refresh")
            if fingerprint(conn.execute(index_sql), budget.check) != before_lookup:
                raise RuntimeError("Lookup rows changed during trigger refresh")
            budget.check()
        committed = True
    finally:
        cleanup_errors = cleanup_connection(conn, old_timeout)
    receipt = {"state": "committed" if committed else "not-committed", "mode": "refresh-triggers",
               "connection_cleanup_errors": cleanup_errors, "changed_triggers": list(names),
               "scope_sha256": manifest["scope_sha256"], "data_rows_unchanged": True,
               "unrelated_schema_unchanged": True, "whole_corpus_rebuild": False,
               "elapsed_seconds": round(time.monotonic() - started, 3), "receipt_published": True}
    try:
        write_json(Path(directory) / (operation + ".committed.json"), receipt)
    except OSError as exc:
        receipt.update(receipt_published=False, receipt_error_type=type(exc).__name__)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--mode", choices=("freeze", "dry-run", "apply", "restore", "refresh-triggers"), default="dry-run")
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--approved-count", type=int)
    parser.add_argument("--approved-sha256")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.mode == "freeze":
        if args.expect_count is None or args.expect_count <= 0:
            parser.error("freeze requires a positive --expect-count")
        result = freeze(args.database, args.archive, args.expect_count)
    else:
        if args.mode in ("apply", "restore", "refresh-triggers") and (args.approved_count is None or args.approved_sha256 is None):
            parser.error("Mutations require the explicitly approved count and scope hash")
        if args.mode == "refresh-triggers":
            result = refresh_triggers(args.database, args.archive, approved_count=args.approved_count,
                                      approved_sha256=args.approved_sha256)
        else:
            result = maintain(args.database, args.archive, args.mode, approved_count=args.approved_count,
                              approved_sha256=args.approved_sha256, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
