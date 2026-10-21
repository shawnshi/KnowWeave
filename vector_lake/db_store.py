import hashlib
import json
import logging
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from vector_lake.wiki_utils import get_index_path
from vector_lake.wiki_utils import get_meta_dir
import sqlite_vec

log = logging.getLogger("vector-lake-db")

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()


def _job_idempotency_key(task_type: str, payload: dict | None) -> str | None:
    if task_type != "ingest" or not isinstance(payload, dict):
        return None
    filepath = payload.get("filepath")
    file_hash = payload.get("hash")
    canonical_name = payload.get("canonical_name")
    if not filepath or not file_hash:
        return None
    raw = "\0".join(["ingest", str(filepath), str(file_hash), str(canonical_name or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def get_db_path() -> Path:
    import os
    override = os.environ.get("VECTOR_LAKE_DB_PATH")
    if override:
        return Path(override)
    return get_meta_dir() / "vector_lake.db"

def _sqlite_mmap_bytes() -> int:
    """``VECTOR_LAKE_SQLITE_MMAP_BYTES``, default 256 MiB, 0 disables.

    A memory-mapped file lets every connection read through the OS page cache instead of each
    connection maintaining its own, and it is virtual: pages are faulted in on use, so the mapped
    size is not resident.  Measured on the live 2.4 GB lake, the payload scan of a 30 613-row
    scoped read went 1 149 ms -> 884 ms with this alone; raising ``cache_size`` as well helped less
    than the mapping did and duplicates memory per connection.
    """
    raw = os.environ.get("VECTOR_LAKE_SQLITE_MMAP_BYTES")
    if raw is None or not str(raw).strip():
        return 268435456
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return 268435456


def _sqlite_cache_kib() -> int:
    """``VECTOR_LAKE_SQLITE_CACHE_KIB``, default -2000 (2 MiB) as before.

    Negative values are KiB in SQLite's units.  The default is deliberately unchanged: the mapping
    above covers the read path without per-connection duplication.
    """
    raw = os.environ.get("VECTOR_LAKE_SQLITE_CACHE_KIB")
    if raw is None or not str(raw).strip():
        return -2000
    try:
        return int(str(raw).strip())
    except ValueError:
        return -2000


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs that SQLite scopes to a single connection.

    ``init_db`` is memoized per database path, so pragmas applied there were
    skipped for every connection opened after ``close_connection`` -- and the
    outbox consumer recycles its connection on each loop iteration.  Foreign key
    enforcement and synchronous mode are connection-scoped, so they silently
    reverted to the SQLite defaults on every recycled connection.
    """
    for statement in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        f"PRAGMA mmap_size={_sqlite_mmap_bytes()}",
        f"PRAGMA cache_size={_sqlite_cache_kib()}",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # A read-only or otherwise constrained database must still open.
            logging.getLogger("vector-lake-db").warning("Could not apply %s: %s", statement, exc)


def get_connection() -> sqlite3.Connection:
    if getattr(_LOCAL, "conn", None) is None:
        db_path = get_db_path()
        from vector_lake.retrieval_runtime import current, remaining_seconds
        timeout = min(30.0, remaining_seconds()) if current() is not None else 30.0
        conn = sqlite3.connect(str(db_path), timeout=timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # Load sqlite-vec extension
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        _apply_connection_pragmas(conn)
        
        _LOCAL.conn = conn
    from vector_lake.retrieval_runtime import track_connection
    return track_connection(_LOCAL.conn)

def close_connection():
    if hasattr(_LOCAL, "conn") and _LOCAL.conn is not None:
        _LOCAL.conn.close()
        _LOCAL.conn = None
    _LOCAL.in_transaction = False

# --- write-lock budget -------------------------------------------------------
#
# ``connect(timeout=...)`` *is* the SQLite busy timeout, so every
# ``BEGIN IMMEDIATE`` attempt already blocks inside SQLite for up to
# ``DB_BUSY_TIMEOUT_MS`` before it raises ``database is locked``.  The previous
# loop then retried 60 times with a 0.5-1.5 s sleep on top, so one
# ``transaction()`` could park for roughly half an hour while the process looked
# hung: nothing wrote ``.watchdog_status.json`` during the wait, ``runtime_health``
# then reported ``watchdog_stale``, and every writer queued behind it
# (``enqueue_mutation``, ``claim_mutation_outbox``, ingest, GC, timeline rebuild).
#
# The budget below is a hard ceiling on total waiting.  ``PRAGMA busy_timeout``
# is re-armed to the remaining budget before each attempt so the ceiling is real
# rather than nominal, and restored to the default once the lock is taken so
# later one-off statements keep the full patience.
DB_BUSY_TIMEOUT_MS = 30_000

# The MCP client that drives these tools abandons a call at 60 s (the SDK's
# ``DEFAULT_REQUEST_TIMEOUT_MSEC``); the pi adapter passes no per-server override.
# A writer that parks past that point cannot report anything useful -- the caller
# is already gone while the server still holds the request -- so lock contention
# has to surface as a bounded, actionable error well inside the client's patience.
# Keep the two numbers coupled in one place: raising this above
# ``MCP_CALL_TIMEOUT_SECONDS`` silently reintroduces unreportable waits.
MCP_CALL_TIMEOUT_SECONDS = 60.0
BEGIN_LOCK_BUDGET_SECONDS = 20.0
BEGIN_LOCK_MAX_ATTEMPTS = 8


class DatabaseLockTimeout(RuntimeError):
    """The write lock could not be taken inside ``BEGIN_LOCK_BUDGET_SECONDS``.

    Distinct from ``sqlite3.OperationalError`` so callers that already catch
    SQLite errors for a genuinely unusable database do not silently absorb a
    contention timeout, which is a retryable condition with a different remedy.
    """


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _arm_busy_timeout(conn: sqlite3.Connection, remaining_seconds: float) -> None:
    """Point ``PRAGMA busy_timeout`` at the remaining lock budget."""
    budget_ms = max(1, int(min(remaining_seconds, DB_BUSY_TIMEOUT_MS / 1000.0) * 1000))
    try:
        conn.execute(f"PRAGMA busy_timeout={budget_ms:d}")
    except sqlite3.OperationalError as exc:
        log.warning("Could not arm busy_timeout=%s: %s", budget_ms, exc)


def _record_lock_contention(attempts: int, budget_seconds: float, waited_seconds: float, outcome: str) -> None:
    """Persist write-lock contention so a wedge leaves evidence behind.

    SQLite cannot report which connection holds the write lock, so a stalled holder used
    to be indistinguishable from an idle system: every writer silently timed out and the
    only durable trace was a transient stderr line (measured: a held lock wedged all wiki
    writes for hours and needed hand-forensics to attribute).  This writes a bounded
    marker that ``runtime_health`` surfaces, including the caller's function name.
    """
    try:
        import inspect

        from vector_lake.wiki_utils import get_meta_dir

        runtime_dir = get_meta_dir() / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        marker_path = runtime_dir / "write_lock_contention.json"
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        caller = "unknown"
        # Walking live frames does not load source files/linecache like inspect.stack().
        frame = inspect.currentframe()
        try:
            while frame is not None:
                name = frame.f_code.co_name
                if name not in {
                    "_record_lock_contention", "_acquire_write_lock", "transaction",
                    "__enter__", "__exit__",
                }:
                    caller = name
                    break
                frame = frame.f_back
        finally:
            del frame  # Do not retain a reference cycle through the current frame.
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "attempts": int(attempts),
            "budget_seconds": round(float(budget_seconds), 1),
            "waited_seconds": round(float(waited_seconds), 1),
            "caller": caller,
        }
        payload["last"] = entry
        history = list(payload.get("history") or [])
        history.append(entry)
        payload["history"] = history[-20:]
        marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a write
        log.warning("Could not record write-lock contention: %s", exc)


def _acquire_write_lock(conn: sqlite3.Connection) -> tuple[int, float, float, str] | None:
    """Take the write lock, or raise :class:`DatabaseLockTimeout` inside the budget."""
    started = time.monotonic()
    deadline = started + BEGIN_LOCK_BUDGET_SECONDS
    last_error: sqlite3.OperationalError | None = None
    attempts = 0
    acquired = False
    try:
        while attempts < BEGIN_LOCK_MAX_ATTEMPTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts += 1
            _arm_busy_timeout(conn, remaining)
            try:
                conn.execute("BEGIN IMMEDIATE")
                acquired = True
                break
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc):
                    raise
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Jittered exponential backoff, never past the remaining budget.
                time.sleep(min(remaining, 0.1 * (2 ** (attempts - 1)) * (0.5 + random.random())))
    finally:
        # Restore default patience even on a non-contention error or cancellation.
        _arm_busy_timeout(conn, DB_BUSY_TIMEOUT_MS / 1000.0)
    waited = time.monotonic() - started
    if acquired:
        if attempts > 1:
            # The transaction owner emits telemetry only after commit/rollback.
            return (attempts, BEGIN_LOCK_BUDGET_SECONDS, waited, "acquired-after-retry")
        return None
    _record_lock_contention(attempts, BEGIN_LOCK_BUDGET_SECONDS, waited, "timed-out")
    log.error(
        "Could not acquire the Vector Lake write lock within %.0fs (%d attempt(s)); "
        "another writer is holding it. Last error: %s",
        BEGIN_LOCK_BUDGET_SECONDS, attempts, last_error,
    )
    if not acquired:
        raise DatabaseLockTimeout(
            f"Could not acquire the Vector Lake write lock within "
            f"{BEGIN_LOCK_BUDGET_SECONDS:.0f}s ({attempts} attempt(s)); "
            f"another writer is holding it. Last error: {last_error}"
        ) from last_error


@contextmanager
def read_snapshot():
    """Use one WAL read generation without acquiring the database writer lock.

    The snapshot must settle before any later write transaction. Refuse nesting
    rather than committing a caller's transaction, and reject accidental writes.
    """
    conn = get_connection()
    if in_write_transaction():
        raise RuntimeError("Read snapshot must start outside a SQLite transaction")
    query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        conn.execute("BEGIN DEFERRED")
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.execute("PRAGMA query_only = ON" if query_only else "PRAGMA query_only = OFF")


@contextmanager
def transaction():
    conn = get_connection()
    if getattr(_LOCAL, "in_transaction", False):
        yield conn
        return

    contention = _acquire_write_lock(conn)
    _LOCAL.in_transaction = True
    try:
        yield conn
        conn.commit()
    except BaseException:
        # A failed ``commit()`` used to escape without a rollback, leaving the
        # connection inside an open transaction; the next ``BEGIN IMMEDIATE`` on
        # it then raised "cannot start a transaction within a transaction",
        # which reads as a schema bug rather than a stuck lock.
        try:
            conn.rollback()
        except sqlite3.Error as rollback_error:
            log.error("Rollback failed, connection state is unknown: %s", rollback_error)
        raise
    finally:
        _LOCAL.in_transaction = False
        if contention is not None and not conn.in_transaction:
            _record_lock_contention(*contention)

# --- operational memory search projection -----------------------------------
#
# ``search_operational_memory`` used to decode every row of ``operational_memory``
# (142k rows / ~170 MB of JSON on the live corpus) and score them in Python for
# every query, twice per Memory Packet.  The projection below carries the fields
# the scorer actually reads as plain columns, maintained by triggers so every
# writer -- including ones outside this module -- keeps it in step.
#
# Coercion contract (must stay in step with ``governance_store``):
#   memory_type    : lower(COALESCE(json.memory_type, 'fact'))
#   validity_state : lower(COALESCE(json.validity_state, 'active'))
#   memory_score   : CAST(json.memory_score AS REAL) or 0.0
#   updated_rank   : unixepoch(json.updated_at,'subsec') or 0.0 -- millisecond
#                    precision, while the Python oracle uses microsecond floats,
#                    so rows written inside the same millisecond may tie here
#                    and rank apart in Python.  The differential test in
#                    tests/test_operational_memory_index.py pins this down.
#   *_blob         : lower() of the corresponding JSON field, '' when absent

_OM_INDEX_VALUE_COLUMNS = (
    "memory_type",
    "validity_state",
    "memory_score",
    "updated_rank",
    "key_blob",
    "text_blob",
    "page_blob",
    # ``page_key`` is the source page key with ``.md`` stripped and **case preserved**, which is
    # what ``page_index_nodes.node_key`` is; ``page_blob`` is the same value lowercased, so it can
    # never join to that key.  A scoped memory read is "the rows whose page is in this domain", and
    # without this column the only way to answer it was to evaluate the domain expression over
    # every row of ``operational_memory`` (measured 285-310 ms for one domain on the live corpus,
    # before the rows are even read).
    "page_key",
    # ``memory_domain`` is the row's own ``$.domain`` override, lowercased, '' when absent.  It has
    # to be a column as well: the scope predicate is "the row's own domain, else its page's", and
    # the first half is a JSON path the scope query would otherwise have to evaluate per row.
    "memory_domain",
    # Same shape for ``cluster=``: the row's own ``$.topic_cluster``, else its page's.
    "memory_cluster",
    "source_updated_at",
)

_OM_INDEX_COLUMNS = ("memory_id",) + _OM_INDEX_VALUE_COLUMNS + ("source_rowid",)


def _om_index_value_expr(source: str) -> str:
    """Newline-joined projection value expressions bound to ``source`` (``NEW``/``src``)."""
    page = f"json_extract({source}.data_json, '$.source_page')"
    return (
        f"            lower(COALESCE(json_extract({source}.data_json, '$.memory_type'), 'fact')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.validity_state'), 'active')),\n"
        f"            COALESCE(CAST(json_extract({source}.data_json, '$.memory_score') AS REAL), 0.0),\n"
        f"            COALESCE(unixepoch(NULLIF(json_extract({source}.data_json, '$.updated_at'), ''), 'subsec'), 0.0),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.memory_key'), '')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.text'), '')),\n"
        f"            lower(COALESCE({page}, '')),\n"
        f"            COALESCE(CASE WHEN lower(substr({page}, -3)) = '.md' "
        f"THEN substr({page}, 1, length({page}) - 3) ELSE {page} END, ''),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.domain'), '')),\n"
        f"            lower(COALESCE(json_extract({source}.data_json, '$.topic_cluster'), '')),\n"
        f"            COALESCE(json_extract({source}.data_json, '$.updated_at'), ''),\n"
        f"            {source}.rowid"
    )


def _create_operational_memory_index(conn: sqlite3.Connection) -> None:
    """Create the search projection and its maintenance triggers."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_index (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            validity_state TEXT NOT NULL,
            memory_score REAL NOT NULL,
            updated_rank REAL NOT NULL,
            key_blob TEXT NOT NULL,
            text_blob TEXT NOT NULL,
            page_blob TEXT NOT NULL,
            page_key TEXT NOT NULL DEFAULT '',
            memory_domain TEXT NOT NULL DEFAULT '',
            memory_cluster TEXT NOT NULL DEFAULT '',
            source_updated_at TEXT NOT NULL DEFAULT '',
            source_rowid INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # ``source_rowid`` reproduces the store's natural ``SELECT *`` order, which is
    # the final tie-break of the Python scorer.  Adding the column to a database
    # that predates it invalidates every existing row, so the rename succeeds and
    # forces one rebuild.
    try:
        conn.execute(
            "ALTER TABLE operational_memory_index "
            "ADD COLUMN source_rowid INTEGER NOT NULL DEFAULT 0"
        )
        stale_format = True
    except sqlite3.OperationalError:
        stale_format = False
    # ``page_key``/``memory_domain`` serve the scoped read.  A database written before them answers
    # the scope question by evaluating JSON per row; the tolerated ALTER below forces the one rebuild
    # that fills them in, exactly as ``source_rowid`` above does.
    for column, declaration in (("page_key", "TEXT NOT NULL DEFAULT ''"),
                                ("memory_domain", "TEXT NOT NULL DEFAULT ''"),
                                ("memory_cluster", "TEXT NOT NULL DEFAULT ''")):
        try:
            conn.execute(f"ALTER TABLE operational_memory_index ADD COLUMN {column} {declaration}")
            stale_format = True
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_type_state "
        "ON operational_memory_index (memory_type, validity_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_type ON operational_memory_index (memory_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_page_key "
        "ON operational_memory_index (page_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_memory_domain "
        "ON operational_memory_index (memory_domain)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_om_index_memory_cluster "
        "ON operational_memory_index (memory_cluster)"
    )
    columns = ", ".join(_OM_INDEX_COLUMNS)
    upsert = ", ".join(
        f"{column} = excluded.{column}" for column in _OM_INDEX_COLUMNS if column != "memory_id"
    )
    for trigger, event in (
        ("trg_om_index_insert", "AFTER INSERT"),
        ("trg_om_index_update", "AFTER UPDATE"),
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            f"""
            CREATE TRIGGER {trigger}
            {event} ON operational_memory
            BEGIN
                INSERT INTO operational_memory_index ({columns})
                VALUES (
                    NEW.memory_id,
{_om_index_value_expr('NEW')}
                )
                ON CONFLICT(memory_id) DO UPDATE SET {upsert};
            END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_om_index_delete
        AFTER DELETE ON operational_memory
        BEGIN
            DELETE FROM operational_memory_index WHERE memory_id = OLD.memory_id;
        END
        """
    )
    _create_memory_gram_tables(conn)
    _create_page_index_tables(conn)
    _create_claim_index_tables(conn)
    # ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a column
    # added after a schema's first release needs its own tolerated ALTER, exactly
    # as ``operational_memory_index.source_rowid`` does.  ``_table_columns``
    # answers the same question on the read path without a write lock.
    try:
        conn.execute("ALTER TABLE page_index_state ADD COLUMN edge_digest TEXT")
    except sqlite3.OperationalError:
        pass
    # ``force`` also when the recorded source row no longer resolves: a memory-table rebuild moves
    # every rowid and bypasses the trigger, and only a re-derivation rewrites ``source_rowid``.
    ensure_operational_memory_index(conn, force=stale_format or _om_source_rowid_is_stale(conn))


_CLAIM_INDEX_VALUE_COLUMNS = (
    "claim_type",
    "status",
    "confidence",
    "freshness_tier",
    "valid_to",
    "review_after",
    "evidence_count",
    "contradicts_count",
    "subject_entity_count",
    "source_page",
    "claim_text",
    "source_ids",
    # The annotation reads this through the projection: ``infer_claim_validity`` splits "the page
    # records no source" from "the block does not say which one", and the debt metric runs on
    # these narrow rows.  A projection that carried every other field the annotation reads and not
    # this one reported both gaps as the same number.
    "evidence_gap",
)
_CLAIM_INDEX_COLUMNS = ("claim_id",) + _CLAIM_INDEX_VALUE_COLUMNS + ("source_rowid",)


def _claim_count_expr(source: str, field: str) -> str:
    """``len(claim.get(field, []))`` expressed in SQL, type by type.

    ``json_array_length`` raises "malformed JSON" on a scalar and returns NULL for an absent path,
    so each shape is handled explicitly.  ``length()`` on the extracted text reproduces ``len()``
    for a *string* value, which is what the Python oracle would count; a number, a bool or an
    explicit null would make ``len()`` raise there, so 0 is the projection's answer and the
    differential test in ``tests/test_claim_index.py`` pins both behaviours.
    """
    extracted = f"json_extract({source}.data_json, '$.{field}')"
    kind = f"json_type({source}.data_json, '$.{field}')"
    return (
        f"CASE WHEN {kind} = 'array' THEN json_array_length({extracted}) "
        f"WHEN {kind} = 'text' THEN length({extracted}) ELSE 0 END"
    )


def _claim_index_value_expr(source: str) -> str:
    """Projection value expressions bound to ``source`` (``NEW``/``src``), one per column."""
    expressions = (
        f"lower(COALESCE(json_extract({source}.data_json, '$.claim_type'), ''))",
        f"lower(COALESCE(json_extract({source}.data_json, '$.status'), 'Active'))",
        f"COALESCE(CAST(json_extract({source}.data_json, '$.confidence') AS REAL), 0.0)",
        f"lower(COALESCE(json_extract({source}.data_json, '$.freshness_tier'), 'unknown'))",
        f"COALESCE(json_extract({source}.data_json, '$.valid_to'), '')",
        f"COALESCE(json_extract({source}.data_json, '$.review_after'), '')",
        _claim_count_expr(source, "evidence_ids"),
        _claim_count_expr(source, "contradicts"),
        _claim_count_expr(source, "subject_entity_ids"),
        f"COALESCE(json_extract({source}.data_json, '$.source_page'), '')",
        f"lower(COALESCE(json_extract({source}.data_json, '$.claim_text'), ''))",
        f"CASE WHEN json_type({source}.data_json, '$.source_ids') IS NULL THEN NULL "
        f"ELSE json_quote(json_extract({source}.data_json, '$.source_ids')) END",
        f"COALESCE(json_extract({source}.data_json, '$.evidence_gap'), '')",
    )
    separator = "," + chr(10)
    return separator.join("            " + expression for expression in expressions)


def _create_claim_index_tables(conn: sqlite3.Connection) -> None:
    """Schema for the narrow claim projection (``claim_index``).

    Why it exists: ``trace`` scans every claim's text and ``debt`` annotates every claim, and both
    read a fixed handful of fields -- while ``load_claims`` decodes all 101 323 JSON payloads to get
    them (3.2 s of a 4.2 s trace, and the same share of debt).  The projection carries exactly the
    fields those two consume, maintained by triggers the way ``operational_memory_index`` is, so
    the scan and the annotation read columns and only the returned rows are decoded.

    ``source_rowid`` reproduces the store's natural ``SELECT *`` order, which is the order the
    previous full scan iterated in and therefore the tie-break of its stable sort.

    Two columns carry their exact JSON spelling rather than a derived form.  ``source_page`` is
    **not** lowercased, because ``trace`` both lowercases it into a haystack *and* tests it for
    membership in the FTS result set, whose keys are raw; lowercasing here would change that test
    for any page whose key has capitals.  ``source_ids`` is stored as JSON text (not a count) since
    ``debt`` iterates its values, and stays NULL when the key is absent so a reader can apply the
    same ``[]`` default the JSON path did.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_index (
            claim_id TEXT PRIMARY KEY,
            claim_type TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            freshness_tier TEXT NOT NULL,
            valid_to TEXT NOT NULL DEFAULT '',
            review_after TEXT NOT NULL DEFAULT '',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            contradicts_count INTEGER NOT NULL DEFAULT 0,
            subject_entity_count INTEGER NOT NULL DEFAULT 0,
            source_page TEXT NOT NULL DEFAULT '',
            claim_text TEXT NOT NULL DEFAULT '',
            source_ids TEXT,
            source_rowid INTEGER NOT NULL DEFAULT 0,
            evidence_gap TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # ``evidence_gap`` is the one field the annotation needs that a database predating it does not
    # carry, and the triggers are created with ``IF NOT EXISTS`` -- so the column is added, the two
    # projection triggers are dropped to be re-created with it, and the projection is re-derived
    # once.  Without the rebuild every existing row would keep the empty default and the metric
    # would still read one number instead of two.
    try:
        conn.execute("ALTER TABLE claim_index ADD COLUMN evidence_gap TEXT NOT NULL DEFAULT ''")
        stale_claim_projection = True
    except sqlite3.OperationalError:
        stale_claim_projection = False
    if stale_claim_projection:
        for trigger in ("trg_claim_index_insert", "trg_claim_index_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_index_status ON claim_index (status)")
    columns = ", ".join(_CLAIM_INDEX_COLUMNS)
    upsert = ", ".join(
        f"{column} = excluded.{column}" for column in _CLAIM_INDEX_COLUMNS if column != "claim_id"
    )
    for trigger, event in (
        ("trg_claim_index_insert", "AFTER INSERT"),
        ("trg_claim_index_update", "AFTER UPDATE"),
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger}
            {event} ON claims
            BEGIN
                INSERT INTO claim_index ({columns})
                VALUES (
                    NEW.claim_id,
{_claim_index_value_expr("NEW")},
                    NEW.rowid
                )
                ON CONFLICT(claim_id) DO UPDATE SET {upsert};
            END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_claim_index_delete
        AFTER DELETE ON claims
        BEGIN
            DELETE FROM claim_index WHERE claim_id = OLD.claim_id;
        END
        """
    )
    ensure_claim_index(conn, force=stale_claim_projection)


def ensure_claim_index(conn: sqlite3.Connection | None = None, force: bool = False) -> dict:
    """Reconcile ``claim_index`` with ``claims``; ``force`` re-derives every row."""
    conn = conn or get_connection()
    result = {
        "canonical": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
        "projected": conn.execute("SELECT COUNT(*) FROM claim_index").fetchone()[0],
        "deleted": 0,
        "rebuilt": 0,
    }
    if not force and result["canonical"] == result["projected"]:
        return result
    columns = ", ".join(_CLAIM_INDEX_COLUMNS)
    if force:
        result["deleted"] = conn.execute("DELETE FROM claim_index").rowcount
        gap_clause = ""
    else:
        result["deleted"] = conn.execute(
            "DELETE FROM claim_index WHERE claim_id NOT IN (SELECT claim_id FROM claims)"
        ).rowcount
        gap_clause = "WHERE src.claim_id NOT IN (SELECT claim_id FROM claim_index)"
    # ``INSERT OR REPLACE ... SELECT`` rather than an upsert: SQLite requires a WHERE clause to
    # disambiguate an ON CONFLICT attached to a SELECT, so the "fill the gaps" path would have to
    # synthesise one.  This mirrors ``ensure_operational_memory_index``.
    cursor = conn.execute(
        f"""
        INSERT OR REPLACE INTO claim_index ({columns})
        SELECT src.claim_id,
{_claim_index_value_expr("src")},
               src.rowid
        FROM claims AS src
        {gap_clause}
        """
    )
    result["rebuilt"] = int(cursor.rowcount or 0)
    return result


def _create_page_index_tables(conn: sqlite3.Connection) -> None:
    """Schema for the ``index.json`` projection in ``vector_lake.page_index_projection``.

    ``page_index_edges`` keeps the published edge multiset together with its ``sequence``, because its
    reader -- ``page_index_projection.adjacency`` -- needs the order for the two-step personalised
    PageRank walk, which is order-sensitive for zero-mass candidates.

    A second table used to hold the same pairs collapsed to ``(source, target, relation)``.  Nothing
    read it but the check that compared the two against each other, so it was removed on 2026-09-18;
    the check now compares this projection against the published file, which is what the contract was
    always about.  Do not reintroduce a table here: the source of truth for edges is ``index.json``
    ``weighted_edges``, and the database holds one projection of it.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_nodes (
            node_key TEXT PRIMARY KEY,
            node_id TEXT,
            title TEXT,
            type TEXT,
            status TEXT,
            domain TEXT,
            topic_cluster TEXT,
            node_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_index_nodes_domain ON page_index_nodes (domain)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_nodes_cluster ON page_index_nodes (topic_cluster)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_edges (
            sequence INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL,
            target_key TEXT NOT NULL,
            weight REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_edges_source ON page_index_edges (source_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_index_edges_target ON page_index_edges (target_key)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_index_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            index_mtime REAL NOT NULL DEFAULT 0,
            index_size INTEGER NOT NULL DEFAULT 0,
            node_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            edge_digest TEXT
        )
        """
    )


def _create_memory_gram_tables(conn: sqlite3.Connection) -> None:
    """Schema for the exact n-gram index in ``vector_lake.memory_gram_index``.

    The dirty queue is filled by triggers on ``operational_memory`` -- the canonical table,
    not the projection -- so every writer marks its own documents stale *and* the queue
    survives the projection being rebuilt.  Nothing here computes grams: that needs Python,
    and replacing the base is a maintenance step the operator or the scheduled block
    triggers (see ``vector_lake.memory_gram_index``).  The overlay table an older release
    filled is neither created nor read any more, and the prune below drops it where it
    survives.

    ``operational_memory_gram_docs`` is what makes the queue survive that rebuild.  A
    posting names its document by the integer ``doc``, and until version 3 that integer was
    ``operational_memory_index.rowid`` -- a physical address.  The projection is rewritten
    wholesale whenever it is re-derived (``ensure_operational_memory_index(force=True)``
    deletes every row and inserts it again), which renumbers the addresses while the base
    keeps the old ones, so after one such rebuild every posting silently names a different
    memory: the index still answers, and its answers are wrong.  Suffix arrays and inverted
    indexes generally need a stable document number for the same reason; this table is
    that number.  Rows are allocated once and never deleted, so an integer is never reused
    for a second identity -- a retired document keeps naming the memory it described, whose
    postings the read path then drops for having no projection row.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram (
            gram TEXT PRIMARY KEY,
            postings BLOB NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_abandoned_sources (
            filepath TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            reason TEXT,
            terminal_jobs INTEGER NOT NULL DEFAULT 0,
            abandoned_at TEXT NOT NULL,
            PRIMARY KEY (filepath, file_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram_docs (
            doc INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram_dirty (
            doc INTEGER PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory_gram_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            format_version INTEGER NOT NULL DEFAULT 1,
            doc_count INTEGER NOT NULL DEFAULT 0,
            gram_count INTEGER NOT NULL DEFAULT 0,
            postings_count INTEGER NOT NULL DEFAULT 0,
            base_docs INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """
    )
    for trigger, event, memory_id, when, allocate in (
        ("trg_om_gram_dirty_insert", "AFTER INSERT", "NEW.memory_id", "", True),
        # Only a change to a gram-bearing field can make the base postings stale.  Firing on
        # every update marked the index unusable for bookkeeping edits alone -- ``validity_state``,
        # ``memory_score``, ``updated_rank`` and ``source_updated_at`` churn constantly, and a
        # sample of 200 queued documents found 200 of them with postings that already matched
        # their content byte for byte.  The read path refuses the whole index while any live
        # document is queued, so a false marker is not merely wasted maintenance: it takes the
        # indexed path away from every search until the next rebuild, which the next bookkeeping
        # write then undoes again.
        #
        # The comparison is on the *blob* semantics, not on the raw JSON: the base is built from
        # ``operational_memory_index.key_blob``/``text_blob``/``page_blob``, which are the same
        # fields lowercased, so a change that only alters case cannot make a posting stale and
        # must not queue the document.
        (
            "trg_om_gram_dirty_update",
            "AFTER UPDATE",
            "NEW.memory_id",
            "WHEN lower(COALESCE(json_extract(OLD.data_json, '$.memory_key'), '')) "
            "IS NOT lower(COALESCE(json_extract(NEW.data_json, '$.memory_key'), '')) "
            "OR lower(COALESCE(json_extract(OLD.data_json, '$.text'), '')) "
            "IS NOT lower(COALESCE(json_extract(NEW.data_json, '$.text'), '')) "
            "OR lower(COALESCE(json_extract(OLD.data_json, '$.source_page'), '')) "
            "IS NOT lower(COALESCE(json_extract(NEW.data_json, '$.source_page'), ''))",
            False,
        ),
        # A delete never needs to allocate: every document the base can name already has its
        # identity row, and a memory that never had one has no postings to retire.
        ("trg_om_gram_dirty_delete", "AFTER DELETE", "OLD.memory_id", "", False),
    ):
        # ``INSERT OR IGNORE``/``OR REPLACE`` are rejected here: a trigger body is a nested
        # statement, and SQLite raises the UNIQUE violation instead of applying the nested
        # conflict resolution (verified with a minimal repro).  An explicit NOT EXISTS guard
        # cannot conflict.
        #
        # Dropped rather than guarded with IF NOT EXISTS because a trigger body is
        # code, and an upgraded database would otherwise keep the previous, buggy
        # body forever -- including the version that was attached to the projection and
        # therefore derived the document number from a physical row id.
        allocate_doc = (
            f"""
                INSERT INTO operational_memory_gram_docs (memory_id)
                SELECT {memory_id} WHERE NOT EXISTS (
                    SELECT 1 FROM operational_memory_gram_docs WHERE memory_id = {memory_id}
                );"""
            if allocate
            else ""
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            f"""
            CREATE TRIGGER {trigger}
            {event} ON operational_memory
            {when}
            BEGIN{allocate_doc}
                INSERT INTO operational_memory_gram_dirty (doc)
                SELECT g.doc FROM operational_memory_gram_docs AS g
                WHERE g.memory_id = {memory_id} AND NOT EXISTS (
                    SELECT 1 FROM operational_memory_gram_dirty AS d WHERE d.doc = g.doc
                );
            END
            """
        )


def ensure_operational_memory_index(
    conn: sqlite3.Connection | None = None, force: bool = False
) -> dict:
    """Reconcile ``operational_memory_index`` with ``operational_memory``.

    ``force`` re-derives every row (content-drift check); the default only fills
    gaps, which is the cheap path taken on first use and after an out-of-band
    schema upgrade.  Returns row counts so callers can report the work done.
    """
    conn = conn or get_connection()
    result = {
        "canonical": conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0],
        "projected": conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0],
        "deleted": 0,
        "rebuilt": 0,
    }
    if not force and result["canonical"] == result["projected"]:
        return result

    columns = ", ".join(_OM_INDEX_COLUMNS)
    if force:
        result["deleted"] = conn.execute("DELETE FROM operational_memory_index").rowcount
        gap_clause = ""
    else:
        result["deleted"] = conn.execute(
            "DELETE FROM operational_memory_index "
            "WHERE memory_id NOT IN (SELECT memory_id FROM operational_memory)"
        ).rowcount
        gap_clause = (
            "WHERE src.memory_id NOT IN (SELECT memory_id FROM operational_memory_index)"
        )
    result["rebuilt"] = conn.execute(
        f"""
        INSERT OR REPLACE INTO operational_memory_index ({columns})
        SELECT src.memory_id,
{_om_index_value_expr('src')}
        FROM operational_memory AS src
        {gap_clause}
        """
    ).rowcount
    result["projected"] = conn.execute(
        "SELECT COUNT(*) FROM operational_memory_index"
    ).fetchone()[0]
    return result


def operational_memory_index_drift(conn: sqlite3.Connection | None = None) -> dict:
    """Report projection rows that are missing or stale relative to the source."""
    conn = conn or get_connection()
    missing, stale = conn.execute(
        """
        SELECT
            SUM(CASE WHEN idx.memory_id IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN idx.memory_id IS NOT NULL
                      AND idx.source_updated_at <> COALESCE(json_extract(src.data_json, '$.updated_at'), '')
                     THEN 1 ELSE 0 END)
        FROM operational_memory AS src
        LEFT JOIN operational_memory_index AS idx ON idx.memory_id = src.memory_id
        """
    ).fetchone()
    return {"missing": int(missing or 0), "stale": int(stale or 0)}


# Reconciling the projection with the store needs two ``COUNT(*)`` index scans
# (~11 ms on the live 142k-row corpus), which is too much to pay on every query.
# ``PRAGMA data_version`` moves when another connection commits and
# ``Connection.total_changes`` moves for this connection's own writes, so an
# untouched database is cleared by two constant-time reads instead.
_OM_INDEX_GUARD: dict = {"fingerprint": None}


def operational_memory_index_fingerprint(conn: sqlite3.Connection | None = None) -> tuple:
    conn = conn or get_connection()
    return (
        str(get_db_path()),
        conn.execute("PRAGMA data_version").fetchone()[0],
        conn.total_changes,
    )


def ensure_operational_memory_index_cheap(conn: sqlite3.Connection | None = None) -> dict | None:
    """Reconcile only when a write could have invalidated the projection.

    Returns the reconciliation result, or ``None`` when the guard short-circuited.
    """
    conn = conn or get_connection()
    if _OM_INDEX_GUARD["fingerprint"] == operational_memory_index_fingerprint(conn):
        return None
    result = ensure_operational_memory_index(conn)
    _OM_INDEX_GUARD["fingerprint"] = operational_memory_index_fingerprint(conn)
    return result

# --- idempotency index recovery ---------------------------------------------
#
# ``enqueue_mutation`` and ``enqueue_job`` both SELECT by ``idempotency_key`` and
# then INSERT, inside one ``BEGIN IMMEDIATE``.  The write lock is what actually
# serialises them; the unique index is defence-in-depth on top of it, and it is
# the only thing that still holds if a caller forgets the lock.
#
# A database written by an earlier release can already hold duplicate keys.
# Aborting ``init_db`` on such a database would make every command unusable, so
# the full index is skipped -- but skipping it used to be the end of the story,
# which left these tables with *no* uniqueness at all.  The partial index below
# restores exactly the guarantee a concurrent enqueue can violate, and it does so
# without deleting any history.
#
# The predicate is written as "not a terminal state" rather than a list of active
# states, so a status added later is protected by default instead of silently
# falling outside the index.
_TERMINAL_IDEMPOTENCY_STATES = ("completed", "finalized", "cancelled", "superseded")
_ACTIVE_IDEMPOTENCY_PREDICATE = (
    "idempotency_key IS NOT NULL AND COALESCE(status, '') NOT IN ("
    + ", ".join(f"'{state}'" for state in _TERMINAL_IDEMPOTENCY_STATES)
    + ")"
)

# table -> (unique index name, primary-key column).  The primary key differs per
# table: ``mutation_outbox`` uses ``id``, ``jobs`` uses ``job_id``.  The repair has
# to know which, because "the canonical row is the lowest one" is expressed against
# it and hardcoding ``id`` made the ``jobs`` repair fail with "no such column: id".
IDEMPOTENCY_TABLES: tuple[str, ...] = ("mutation_outbox", "jobs")
_IDEMPOTENCY_INDEXES: dict[str, tuple[str, str]] = {
    "mutation_outbox": ("idx_mutation_outbox_idempotency", "id"),
    "jobs": ("idx_jobs_idempotency", "job_id"),
}


def _duplicate_idempotency_groups(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM (SELECT idempotency_key FROM {table} "
            f"WHERE idempotency_key IS NOT NULL "
            f"GROUP BY idempotency_key HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    )


def _ensure_idempotency_index(
    conn: sqlite3.Connection, table: str, index_name: str
) -> str:
    """Create the strongest idempotency index the current data allows.

    Returns ``full`` (every key unique forever), ``active`` (unique among
    in-flight rows only, because the table already holds duplicate history) or
    ``absent`` (uniqueness is solely the surrounding write lock).
    """
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {table}(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        return "full"
    except sqlite3.IntegrityError:
        pass

    duplicates = _duplicate_idempotency_groups(conn, table)
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name}_active "
            f"ON {table}(idempotency_key) WHERE {_ACTIVE_IDEMPOTENCY_PREDICATE}"
        )
    except sqlite3.IntegrityError:
        log.error(
            "%s holds %s duplicated idempotency_key group(s), and at least one duplicate "
            "is still in a non-terminal status, so no unique index could be created. "
            "Concurrent enqueues are protected only by the BEGIN IMMEDIATE write lock. "
            "Resolve the in-flight duplicates, then re-run init_db().",
            table,
            duplicates,
        )
        return "absent"

    log.warning(
        "%s holds %s duplicated idempotency_key group(s) written by an earlier release, "
        "so the full unique index cannot be created. Created %s_active over non-terminal "
        "rows instead, which is the population a concurrent enqueue can collide in. "
        "Call repair_idempotency_keys(%r, dry_run=False) to clear the redundant keys and "
        "reclaim the full index.",
        table,
        duplicates,
        index_name,
        table,
    )
    return "active"


def idempotency_index_state() -> dict[str, dict]:
    """Report the uniqueness level actually in force for each idempotency table."""
    init_db()
    conn = get_connection()
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    state: dict[str, dict] = {}
    for table, (index_name, _primary_key) in _IDEMPOTENCY_INDEXES.items():
        if index_name in existing:
            uniqueness = "full"
        elif f"{index_name}_active" in existing:
            uniqueness = "active"
        else:
            uniqueness = "absent"
        state[table] = {
            "uniqueness": uniqueness,
            "duplicate_groups": _duplicate_idempotency_groups(conn, table),
        }
    return state


def repair_idempotency_keys(table: str, dry_run: bool = True) -> dict:
    """Clear the *redundant* idempotency keys so the full unique index is available.

    The canonical row for a key is the one ``enqueue_mutation`` / ``enqueue_job``
    already returns for it: the lowest ``id``.  Every other row keeps its status,
    timestamps, error text and ``superseded_by`` link -- only the duplicate key is
    cleared, which is what makes the rows collide in the first place.  No audit
    history is deleted, and ``dry_run`` (the default) reports the exact ids first.
    """
    if table not in _IDEMPOTENCY_INDEXES:
        raise ValueError(f"Unsupported idempotency table: {table}")
    index_name, primary_key = _IDEMPOTENCY_INDEXES[table]
    init_db()
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            f"SELECT {primary_key} FROM {table} "
            f"WHERE idempotency_key IS NOT NULL AND {primary_key} NOT IN ("
            f"SELECT MIN({primary_key}) FROM {table} WHERE idempotency_key IS NOT NULL "
            f"GROUP BY idempotency_key)"
        ).fetchall()
        # The primary key is not always an integer: ``mutation_outbox`` uses an
        # INTEGER ``id`` while ``jobs`` uses a hex ``job_id``, so the values are
        # carried through untyped rather than coerced.
        redundant_keys = [row[primary_key] for row in rows]
        result = {
            "table": table,
            "dry_run": bool(dry_run),
            "redundant_rows": len(redundant_keys),
            "redundant_keys": redundant_keys,
            "duplicate_groups_before": _duplicate_idempotency_groups(conn, table),
            "uniqueness_before": idempotency_index_state()[table]["uniqueness"],
        }
        if dry_run or not redundant_keys:
            result["uniqueness_after"] = result["uniqueness_before"]
            return result
        conn.executemany(
            f"UPDATE {table} SET idempotency_key = NULL WHERE {primary_key} = ?",
            [(row_id,) for row_id in redundant_keys],
        )
        result["uniqueness_after"] = _ensure_idempotency_index(conn, table, index_name)
        result["duplicate_groups_after"] = _duplicate_idempotency_groups(conn, table)
        return result


# Objects that prove ``_init_db_once`` ran to completion.  Its DDL is one
# transaction, so a partial schema cannot exist: either the whole tail is there
# or nothing was committed.  The list deliberately mixes early and late objects
# so a database created by an older, narrower version still takes the full path.
_SCHEMA_SENTINELS: tuple[str, ...] = (
    "entities",
    "claims",
    "operational_memory",
    "operational_memory_index",
    "wiki_search_index",
    "tantivy_mirror_state",
    "tantivy_mirror_queue",
    "page_index_nodes",
    "page_index_edges",
    "page_index_state",
    # A new table has to be a sentinel, or ``_schema_is_complete`` reports an existing
    # database as complete and the DDL that creates it never runs.
    "ingest_abandoned_sources",
    "claim_index",
    "vec_embedding_projection",
    "vec_embedding_inputs",
    # Added with document identities (gram format version 3).  Without it here, an existing
    # database looks complete and keeps the triggers that numbered a document by the projection's
    # row id -- while the new read path resolves those numbers through this table.
    "operational_memory_gram_docs",
)


# ---------------------------------------------------------------------------
# One-time schema prunes
# ---------------------------------------------------------------------------
# Objects that no release in this tree can create, read, write or clean.  Each
# one was left behind by a release whose modules were removed wholesale: the
# live database still carries it because ``CREATE ... IF NOT EXISTS`` wrote it
# once and nothing ever issued a DROP.  Being invisible from inside the tree is
# exactly why they survived several audits.
#
# 2026-09-16, 526df6a ("Local tree becomes the authoritative main line") deleted
# 23 856 lines including the whole v6 retention layer and its test file.  What
# it could not delete is the schema it had already written into live databases:
#
#   ``ingest_jobs`` + its two indexes
#       No SQL in this tree names the table.  The token survives only inside the
#       function name ``requeue_legacy_ingest_jobs``, which queries ``jobs``.
#   ``idx_jobs_retention_v6``, ``idx_mutation_outbox_retention_v6``
#       Built to serve retention DELETEs that no longer exist -- which is why
#       ``mutation_outbox`` (32 280 terminal rows, oldest 2026-07-12) and
#       ``jobs`` have grown unbounded since.
#   ``idx_mutation_outbox_idempotency_lookup``
#       ``_ensure_idempotency_index`` creates ``{name}`` and ``{name}_active``,
#       never ``_lookup``.
#   ``claim_graph_nodes``
#       Zero rows.  The tree creates it and DELETEs from it in the canonical
#       cascade, but never INSERTs into it and never reads it.
#   ``change_sets.change_id``
#       NULL in all 26 774 rows, and the string ``change_id`` appears in no
#       Python file in the repository.
#
# Recovery point: the pre-prune DDL for every object above was captured before
# the first run.  Recreating them is pure DDL and no row data is at risk in
# either direction -- the two tables hold zero rows and the dropped column is
# NULL everywhere.
def _prune_orphaned_legacy_schema(conn: sqlite3.Connection) -> None:
    """Drop the objects a removed release created but never dropped."""
    for statement in (
        "DROP TABLE IF EXISTS ingest_jobs",
        "DROP INDEX IF EXISTS idx_jobs_retention_v6",
        "DROP INDEX IF EXISTS idx_mutation_outbox_retention_v6",
        "DROP INDEX IF EXISTS idx_mutation_outbox_idempotency_lookup",
        "DROP TABLE IF EXISTS claim_graph_nodes",
    ):
        conn.execute(statement)


#: The prune that removes the gram overlay table.  Named once because the read path checks
#: for it: a database whose schema has not converged must not serve from that index.
GRAM_OVERLAY_DROP = "2026-09-18-drop-gram-overlay"


def _prune_gram_overlay(conn: sqlite3.Connection) -> None:
    """Drop the overlay table a released incremental path used to write.

    Nothing creates it, writes it or reads it any more (``memory_gram_index`` explains why
    it was wrong, not merely unused), so a database that still has it converges here.  The
    index on it is dropped with the table.

    Convergence is one-shot per database: the ledger skips a migration it has recorded, so a
    table re-created *afterwards* by a process of a removed release is not dropped again, and
    no surface reports it.  Re-running the DROP on every ``init_db()`` would take the write
    lock on every start, which is the cost the ledger exists to avoid.
    """
    conn.execute("DROP TABLE IF EXISTS operational_memory_gram_overlay")


def _prune_entities_json_indexes(conn: sqlite3.Connection) -> None:
    """Drop the three indexes the ``entities`` json queries used to need.

    ``$.page_key`` is now a generated column with its own index, and ``$.type``/``$.status`` are
    read through the real ``type``/``status`` columns wherever they are read at all -- no statement
    in the tree filters ``entities`` by either one (checked by grep, not by a query trace, which
    would only say the tests never issued that shape).  They cost a write on every entity insert.
    Recreatable from the DDL if a future query needs them.
    """
    for statement in (
        "DROP INDEX IF EXISTS idx_entities_type",
        "DROP INDEX IF EXISTS idx_entities_status",
        "DROP INDEX IF EXISTS idx_entities_page_key",
    ):
        conn.execute(statement)


def _prune_entities_type_status_index(conn: sqlite3.Connection) -> None:
    """Drop the last unused index on ``entities``, under its own migration name.

    ``archive/migrate_v9_to_v10.py`` created ``idx_entities_type_status(type, status)`` and the
    current DDL does not, so a database that ran that migration still carries it.  No statement in
    the tree filters ``entities`` by type or status (checked by grep), so it is a write on every
    entity insert and serves nothing.

    A separate name rather than a step added to the json-index prune: the ledger skips a migration
    it has already recorded, so adding a step there would never run on the databases that matter.
    """
    conn.execute("DROP INDEX IF EXISTS idx_entities_type_status")


def _prune_om_json_indexes(conn: sqlite3.Connection) -> None:
    """Retire the four json indexes on ``operational_memory``.

    Two are replaced by indexes on generated columns of the same data
    (``idx_om_f_memory_key``, ``idx_om_f_source_claim``, created by the DDL that ran before this
    prune).  The other two index ``$.memory_type`` and ``$.status``, and *no statement in the tree
    filters on those expressions* -- the real ``memory_type``/``status`` columns are what statements
    name, and an expression index cannot serve a bare-column predicate, so those two were
    unreachable rather than merely unused.

    The real-column indexes (``idx_om_type``, ``idx_om_status``, ``idx_memory_type_status``) are
    deliberately left alone: no statement in the tree filters *this table* by those columns today
    (the four ``memory_type IN (...)`` sites filter ``operational_memory_index``), but removing an
    index was measured harmful in general (9.68 -> 632.74 ms for a status filter once removed), and
    ``idx_om_type`` is now a strict prefix of ``idx_om_f_memory_key``.  Whether they earn their
    write cost is a question about the search path, not about this migration.
    """
    # The two unreachable json indexes go unconditionally: no statement can use them whatever else
    # exists.  The two that are *replaced* go only when their replacement is present -- the DDL that
    # creates it sits inside a warning-only ``except OperationalError`` and the fast path never
    # re-runs it, so dropping first would leave the claim/delete paths scanning 146k rows with no way
    # back.  Probing is one read of ``sqlite_master``.
    present = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for statement in ("DROP INDEX IF EXISTS idx_memory_type", "DROP INDEX IF EXISTS idx_memory_status"):
        conn.execute(statement)
    for old_index, replacement in (
        ("idx_memory_key", "idx_om_f_memory_key"),
        ("idx_memory_source_claim", "idx_om_f_source_claim"),
    ):
        if replacement in present:
            conn.execute(f"DROP INDEX IF EXISTS {old_index}")


def _backfill_entities_ttl(conn: sqlite3.Connection) -> None:
    """Fill ``entities.ttl`` from the json it was supposed to mirror, once.

    ``upsert_entity`` and ``save_entities`` have always written the column from the record's
    ``ttl``, but the batch path omitted it from its ``INSERT OR REPLACE``, which resets any column
    the statement does not name.  The json is the source the indexer reads, so where it holds a
    ``ttl`` and the column is NULL, the column is the one that is wrong.  Idempotent: after the
    first run the ``WHERE`` matches nothing.

    ``decay_weight`` has no json counterpart at all, so there is nothing to backfill it from and
    this deliberately does not invent a value; both write paths now write it consistently instead.
    """
    conn.execute(
        "UPDATE entities SET ttl = CAST(json_extract(data_json, '$.ttl') AS REAL) "
        "WHERE ttl IS NULL AND json_extract(data_json, '$.ttl') IS NOT NULL"
    )


def _prune_claim_evidence_json_indexes(conn: sqlite3.Connection) -> None:
    """Retire the three expression indexes those tables' columns replace.

    Each is replaced by an index on the generated column holding the same expression, created by the
    DDL that ran before this prune; as with ``operational_memory`` the drop is conditional on the
    replacement existing, because the DDL is best-effort and the fast path never re-runs it.
    """
    present = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for old_index, replacement in (
        ("idx_claims_page_key", "idx_claims_f_page_key"),
        ("idx_claims_claim_type", "idx_claims_f_claim_type"),
        ("idx_evidence_page_key", "idx_evidence_f_page_key"),
    ):
        if replacement in present:
            conn.execute(f"DROP INDEX IF EXISTS {old_index}")


def _prune_governance_queue_index(conn: sqlite3.Connection) -> None:
    """Drop the expression index on ``governance_queue`` -- no statement can use it.

    The table is read through the generic ``_load_db_queue`` helper, which selects every row and
    filters nothing, so the tree contains no predicate on either expression this index is built from
    (``$.change_set_id``, ``$.status``).  Unreachable, not merely unused -- the structural form of the
    argument the earlier index-slimming attempt lacked.  The current DDL does not create it (an older
    release did), so a prune is the only way it converges.
    """
    conn.execute("DROP INDEX IF EXISTS idx_governance_queue_change_set_status")


def _prune_page_graph_edges(conn: sqlite3.Connection) -> None:
    """Drop the second SQLite copy of the published edge set.

    Nothing read it: the search projection is ``page_index_edges``, and the only consumer of this
    table was the consistency check that compared the two against each other, now rewired to compare
    ``page_index_edges`` against the published file instead.  Its indexes go with it.
    """
    conn.execute("DROP TABLE IF EXISTS page_graph_edges")


def _normalise_entities_ttl_encoding(conn: sqlite3.Connection) -> None:
    """Give "absent" one encoding in ``entities.ttl``/``decay_weight``: ``0.0``.

    Both writers store ``0.0`` when a record carries no value, so a NULL in these columns means the
    row has simply not been written since they were added as real columns -- two spellings of one
    state, which is how a column ends up meaning different things depending on its history.  The
    json is the source the indexer reads, and it is untouched here; ``decay_weight`` has no json
    counterpart, so ``0.0`` is the writers' own value for "not set".

    Idempotent: after the first run the ``WHERE`` matches nothing.
    """
    conn.execute(
        "UPDATE entities SET ttl = 0.0 WHERE ttl IS NULL AND json_extract(data_json, '$.ttl') IS NULL"
    )
    conn.execute("UPDATE entities SET decay_weight = 0.0 WHERE decay_weight IS NULL")


def _prune_change_sets_change_id(conn: sqlite3.Connection) -> None:
    """Drop ``change_sets.change_id`` while a pre-prune database still has it.

    Guarded by a read-only probe, deliberately not by catching
    ``OperationalError``.  A ``DROP COLUMN`` that fails because the column is
    referenced by an index, trigger or view reports
    ``error in index ... after drop column: no such column: ...`` -- a substring
    test for ``no such column`` swallows exactly that and then records the
    migration as applied while the column is still there.  With the probe there
    is nothing to tolerate: the statement is issued only when the column exists,
    so any failure from it is real and must propagate.
    """
    if "change_id" not in _table_columns(conn, "change_sets"):
        return
    conn.execute("ALTER TABLE change_sets DROP COLUMN change_id")


# Each migration is a name plus ordered steps.  Steps are callables rather than
# SQL strings so a step can make a decision (probe a column) instead of relying
# on an error message to tell "already done" apart from "failed".
def _restore_processed_files_observation_snapshot(conn: sqlite3.Connection) -> None:
    """Restore the ledger's observation snapshot columns.

    ``processed_files`` carried ``observed_mtime_ns``/``observed_size`` until the 2026-09-17
    refactor dropped the writer.  The columns stayed in the live database, this DDL reverted
    to three columns, and no migration recorded either the addition or the abandonment -- so
    a fresh database has three columns while the operator's database had five.

    What replaced them was unsound: the scan gate compared the file's mtime against the
    row's wall-clock ``processed_at``.  A file edited after finalize can still carry an mtime
    a few milliseconds *earlier* than that row (NTFS clock granularity is ~15.6 ms), and the
    gate then skipped the edit permanently -- measured 2026-09-20: a 3.9 ms inversion hid a
    real edit, and the same gate silently ignores any change restored with an older mtime
    (``cp -p``, ``git checkout``).  The snapshot makes "unchanged" an observation rather than
    a clock comparison, and the content hash stays the authority whenever it disagrees.
    """
    present = _table_columns(conn, "processed_files")
    if not present:
        return
    for column in ("observed_mtime_ns", "observed_size"):
        if column not in present:
            conn.execute(f"ALTER TABLE processed_files ADD COLUMN {column} INTEGER")


def _prune_timeline_redundant_indexes(conn: sqlite3.Connection) -> None:
    """Drop legacy redundant single-column indexes on timeline_events subsumed by composite indexes."""
    for statement in (
        "DROP INDEX IF EXISTS idx_action",
        "DROP INDEX IF EXISTS idx_sentiment",
    ):
        conn.execute(statement)


_LEGACY_SCHEMA_PRUNES: tuple[
    tuple[str, tuple[Callable[[sqlite3.Connection], None], ...]], ...
] = (
    ("2026-09-17-prune-orphaned-legacy-schema", (_prune_orphaned_legacy_schema,)),
    ("2026-09-17-drop-change-sets-change-id", (_prune_change_sets_change_id,)),
    (GRAM_OVERLAY_DROP, (_prune_gram_overlay,)),
    ("2026-09-18-entities-json-indexes", (_prune_entities_json_indexes,)),
    ("2026-09-18-entities-type-status-index", (_prune_entities_type_status_index,)),
    ("2026-09-18-om-json-indexes", (_prune_om_json_indexes,)),
    ("2026-09-18-backfill-entities-ttl", (_backfill_entities_ttl,)),
    ("2026-09-18-claim-evidence-json-indexes", (_prune_claim_evidence_json_indexes,)),
    ("2026-09-18-governance-queue-index", (_prune_governance_queue_index,)),
    ("2026-09-18-normalise-entities-ttl-encoding", (_normalise_entities_ttl_encoding,)),
    ("2026-09-18-drop-page-graph-edges", (_prune_page_graph_edges,)),
    (
        "2026-09-20-restore-processed-files-observation-snapshot",
        (_restore_processed_files_observation_snapshot,),
    ),
    (
        "2026-10-18-drop-timeline-redundant-indexes",
        (_prune_timeline_redundant_indexes,),
    ),
)

_SCHEMA_MIGRATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
"""


def _ledger_exists(conn: sqlite3.Connection) -> bool:
    """Read-only: is the prune ledger table there at all?"""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        is not None
    )


def _recorded_prunes(conn: sqlite3.Connection) -> set[str]:
    """Read-only: the migration names in the ledger.

    Empty when the ledger table does not exist, which is the state of every
    database written before the first prune ran.  Takes no write lock, so it is
    safe to call before deciding whether a transaction is needed at all.

    Table absence is probed through ``sqlite_master`` rather than caught as an
    error.  Swallowing the read and returning "empty" would turn any transient
    failure -- a lock, a schema change in flight -- into "nothing has been
    applied yet", and the caller would then re-run finished migrations and
    overwrite the ledger timestamps with new ones.
    """
    if not _ledger_exists(conn):
        return set()
    return {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}


def apply_legacy_schema_prunes() -> list[str]:
    """Drop schema left behind by removed releases.  Idempotent and recorded.

    Reads the ledger before deciding anything, so a converged database costs one
    SELECT and takes no write lock at all.  Each migration owns its own
    transaction, so an earlier success survives a later failure.  A migration is
    recorded only after every one of its steps succeeded, so a failure leaves it
    pending for the next attempt instead of reporting success.

    Returns the migration names this call actually applied.
    """
    conn = get_connection()
    pending = [
        (name, steps)
        for name, steps in _LEGACY_SCHEMA_PRUNES
        if name not in _recorded_prunes(conn)
    ]
    completed: list[str] = []
    for name, steps in pending:
        with transaction():
            conn = get_connection()
            conn.execute(_SCHEMA_MIGRATIONS_DDL)
            if name in _recorded_prunes(conn):
                continue
            for step in steps:
                step(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            completed.append(name)
    return completed


def legacy_schema_prune_names() -> tuple[str, ...]:
    """Names of the recorded prunes, in application order."""
    return tuple(name for name, _ in _LEGACY_SCHEMA_PRUNES)


def applied_schema_prunes(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """The prune ledger, for the doctor surface.  Empty when never applied.

    Deliberately does not call ``init_db()``: this is a diagnostic, and a
    diagnostic that can write is not a diagnostic.
    """
    if conn is None:
        conn = get_connection()
    if not _ledger_exists(conn):
        return {}
    rows = conn.execute("SELECT name, applied_at FROM schema_migrations").fetchall()
    return {row["name"]: row["applied_at"] for row in rows}


def _apply_prunes_best_effort() -> None:
    """Run the prunes without making them a precondition for using the database.

    ``init_db()`` never needed the write lock when the schema was already
    complete, and every command goes through it.  A read-only snapshot, or a
    database whose writer is busy elsewhere, must stay usable: an un-applied
    cleanup degrades to a warning, and ``doctor`` reports the schema as not
    converged.  ``DatabaseLockTimeout`` is deliberately not caught -- it is
    raised only after this module's own lock budget, and its class docstring
    records that callers must not absorb it.
    """
    try:
        apply_legacy_schema_prunes()
    except sqlite3.OperationalError as exc:
        log.warning("Legacy schema prune deferred, schema not converged: %s", exc)


def _schema_is_complete(db_path: Path) -> bool:
    """Read-only probe: every sentinel object already exists.

    Opens a bare connection and reads ``sqlite_master`` only.  No PRAGMAs are
    applied and no statement is issued that could take the write lock, so this is
    safe to call on the read path while another process is writing.  The caller
    uses it to skip the DDL transaction that would otherwise make every read wait
    behind the writer holding ``BEGIN IMMEDIATE``.

    The prune ledger is deliberately *not* part of this contract.  Requiring it
    here would make ``init_db()`` fail outright on a read-only snapshot of a
    pre-prune database, when all that is missing is a cleanup.  The prunes are
    run separately by ``_apply_prunes_best_effort``, which is how a database
    whose sentinels were already present still converges on its next
    ``init_db()`` instead of never.
    """
    if not db_path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        present = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        }
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return set(_SCHEMA_SENTINELS).issubset(present)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _table_xcolumns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table`` *including* generated ones; empty when it does not exist.

    ``PRAGMA table_info`` omits a ``GENERATED`` column -- only ``table_xinfo`` lists it -- so a
    probe built on :func:`_table_columns` cannot see one and would report it missing forever,
    re-running the ``ALTER`` on every start until it failed with "duplicate column name".
    """
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_xinfo({table})")}
    except sqlite3.Error:
        return set()


def _om_index_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only equivalent of ``_init_db_once``'s ``ALTER TABLE`` format probe.

    That probe learned whether ``source_rowid`` was missing by attempting the
    ALTER, which requires the write lock.  ``PRAGMA table_info`` answers the same
    question without it, so the staleness decision can be made before deciding
    whether the write transaction is needed at all.  A missing table reports
    stale, which sends the caller down the full initialisation path.
    """
    columns = _table_columns(conn, "operational_memory_index")
    return not {"source_rowid", "page_key", "memory_domain", "memory_cluster"} <= columns


def _om_source_rowid_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: does ``operational_memory_index.source_rowid`` still name its source row?

    The column is ``NEW.rowid`` of ``operational_memory``, and a rowid is a *physical* identity: a
    table rebuild (``CREATE new`` / ``INSERT`` / ``DROP`` / ``RENAME``) moves every row and bypasses
    the triggers that would have rewritten it.  Found in the live lake by comparing the two tables:
    only 4 306 of 74 358 index rows named an existing source, because the memory table had been
    rebuilt after those index rows were written.  Nothing read it at the time, so no answer was
    wrong, and nothing reads it for an answer now either: the gram index bridges a posting to its
    payload through ``operational_memory_gram_docs``, precisely because a physical address cannot
    carry an identity across a rebuild.  ``source_rowid`` survives as a tie-break for the
    projection's natural order, and any future consumer that treats it as identity would score the
    wrong row for 94% of the corpus.

    Probed on a bounded sample rather than a full join: after a rebuild the drift is uniform, and
    200 primary-key lookups cost well under a millisecond.  ``source_rowid = 0`` is *unknown*, not
    stale -- rows written by the legacy trigger never had the column -- so it is excluded, which also
    keeps the legacy-upgrade path in ``tests/test_orphan_memory_repair.py`` intact.
    """
    probe = conn.execute(
        "SELECT COUNT(*) AS checked, "
        "SUM(CASE WHEN m.memory_id = i.memory_id THEN 1 ELSE 0 END) AS found "
        "FROM (SELECT memory_id, source_rowid FROM operational_memory_index "
        "WHERE source_rowid > 0 LIMIT 200) i "
        "LEFT JOIN operational_memory m ON m.rowid = i.source_rowid"
    ).fetchone()
    checked = int(probe[0] or 0)
    found = int(probe[1] or 0)
    return checked > 0 and found < checked


def _entities_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: is the generated column the ``page_key`` queries use missing?

    Columns added by ``_init_db_once`` need this, or a database that already has every sentinel
    takes the fast path and never runs the ``ALTER`` -- which is how the rewritten queries found
    themselves asking for a column that did not exist yet.  Same shape as
    :func:`_om_index_format_is_stale`.
    """
    if "f_page_key" not in _table_xcolumns(conn, "entities"):
        return True
    return "idx_entities_f_page_key" not in _index_names(conn)


def _timeline_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: is ``timeline_events`` missing a column or index the writer names?

    Columns added by ``_init_db_once`` need this, or a database that already has every
    sentinel takes the fast path and never runs the ``ALTER`` -- which is how
    ``repair_timeline_projection`` came to ask for a column that did not exist yet on a
    corpus whose schema was otherwise complete.  Same shape as
    :func:`_entities_format_is_stale`.

    ``claim_id`` is the second such column: the event id is content-addressed
    (``sha256(claim_id, event_date, text)``), so a row could say *what* it held but not
    *which claim* it came from, and a claim retired by anything other than the delta path
    left a row nobody could attribute.  The index is required with the column because the
    sync deletes by it on every claim delta.
    """
    columns = _table_xcolumns(conn, "timeline_events")
    if "event_date_source" not in columns or "claim_id" not in columns:
        return True
    return "idx_timeline_claim" not in _index_names(conn)


def _index_names(conn: sqlite3.Connection) -> set[str]:
    """Read-only: every index name in the database."""
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    except sqlite3.Error:
        return set()


def _om_probability_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: are the generated columns the memory queries name missing?

    The indexes are checked with the columns, not only the columns.  The ``CREATE INDEX`` statements
    live in one ``try/except OperationalError`` that only logs, so a failure there -- a full disk, a
    limit -- skips the rest of the block; the fast path never re-runs it, and the conditional prunes
    then *keep* the retired expression indexes, which cannot serve `f_page_key = ?`.  The result
    would be correct answers at full-scan cost, permanently and with no operator-visible signal.
    Requiring the replacement index makes the DDL run again instead.
    """
    present = _table_xcolumns(conn, "operational_memory")
    if not {"f_memory_key", "f_source_claim_id"} <= present:
        return True
    return not {"idx_om_f_memory_key", "idx_om_f_source_claim"} <= _index_names(conn)


def _claims_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Read-only: are the generated columns the claim/evidence/change-set queries name missing?"""
    expected = {
        "claims": ({"f_claim_type", "f_page_key", "f_source_page"},
                   {"idx_claims_f_claim_type", "idx_claims_f_page_key", "idx_claims_f_source_page"}),
        "evidence": ({"f_page_key"}, {"idx_evidence_f_page_key"}),
        "change_sets": ({"f_status", "f_idempotency_key"},
                        {"idx_change_sets_f_status", "idx_change_sets_f_idempotency"}),
    }
    indexes = _index_names(conn)
    for table, (columns, index_names) in expected.items():
        if not columns <= _table_xcolumns(conn, table):
            return True
        if not index_names <= indexes:
            return True
    return False


def _page_index_state_format_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether ``page_index_state`` still lacks the edge digest column."""
    return "edge_digest" not in _table_columns(conn, "page_index_state")


def _jobs_ledger_index_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether the jobs table lacks ``idx_jobs_updated_at``.

    Same reason :func:`_om_probability_format_is_stale` exists: ``CREATE INDEX`` lives inside
    ``_init_db_once``, and ``init_db()`` short-circuits whenever the schema already looks complete,
    so a new index added there would only ever reach databases created after the change.  On this
    host that was measurable -- the index was created by hand at 151 ms and the author facet's
    invalidation read went from 146 ms to 0.2 ms, while a fresh ``init_db()`` kept skipping it.
    Requiring the index here makes the DDL run exactly once on an existing database.
    """
    return not {"idx_jobs_updated_at"} <= _index_names(conn)


def _alias_registry_value_index_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether ``alias_registry`` lacks an index on ``value``.

    The table's only index was its primary-key autoindex on ``key``, while the lookup that matters
    filters on ``value`` (``_registered_page_path``, the alias-registry fallback a merge resolution
    needs).  On 12 005 rows that is a full scan per call -- measured 2026-09-25: **4.386 ms each**,
    2 805 calls in one lint pass.  Same trap as the jobs index above: ``init_db`` short-circuits on a
    complete schema, so the DDL only reaches an existing database if a predicate says it is missing.
    """
    return not {"idx_alias_registry_value"} <= _index_names(conn)


def _search_state_rowid_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether ``wiki_search_index_state`` lacks ``fts_rowid``, or is not backfilled yet.

    Why the rowid exists: ``wiki_search_index`` is an FTS5 table, and FTS5 cannot serve a lookup on a
    *column* -- so ``DELETE FROM wiki_search_index WHERE node_key = ?`` scans the whole index per
    call.  Measured 2026-09-25 on the live lake, per row: that delete **52.4 ms**, the ``INSERT``
    0.1 ms, and ``DELETE ... WHERE rowid = ?`` **0.33 ms** (158x).  The profile had already put 87% of
    a rebuild inside that function, on the delete's line.

    Checking for NULL as well as absence matters: a crash between the ``ALTER`` and the backfill
    would otherwise leave the column present and empty forever, because ``init_db`` short-circuits.
    """
    if "fts_rowid" not in _table_columns(conn, "wiki_search_index_state"):
        return True
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM wiki_search_index_state WHERE fts_rowid IS NULL LIMIT 1"
            ).fetchone()
        )
    except sqlite3.OperationalError:
        return True


def init_db():
    db_path = get_db_path()
    db_key = str(db_path.resolve())
    if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
        return
    with _INIT_LOCK:
        if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
            return
        if (
            _schema_is_complete(db_path)
            and not _om_index_format_is_stale(get_connection())
            and not _om_source_rowid_is_stale(get_connection())
            and not _page_index_state_format_is_stale(get_connection())
            and not _entities_format_is_stale(get_connection())
            and not _om_probability_format_is_stale(get_connection())
            and not _claims_format_is_stale(get_connection())
            and not _timeline_format_is_stale(get_connection())
            and not _jobs_ledger_index_is_stale(get_connection())
            and not _alias_registry_value_index_is_stale(get_connection())
            and not _search_state_rowid_is_stale(get_connection())
        ):
            # The DDL would be a no-op, and running it would block every reader
            # behind the writer's lock.  Keep the cheap gap-fill so a writer that
            # bypassed the triggers still gets reconciled, then memoise.
            _INITIALIZED_DB_PATHS.add(db_key)
            ensure_operational_memory_index(get_connection())
            _apply_prunes_best_effort()
            return
        _INITIALIZED_DB_PATHS.discard(db_key)
        _init_db_once(db_key)
        _apply_prunes_best_effort()


def _init_db_once(db_key: str):
    conn = get_connection()
    with transaction():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            -- ``page_key`` holds a **page key** (``Concept_...``).  Until 2026-09-22 this column was
            -- named ``entity_id``, and that name was wrong in a way that stayed quiet: it is *not*
            -- ``entities.entity_id``, a different identifier, so a join between the two returned
            -- nothing and returned it silently.  The FTS table keys by ``node_key``, so vector hits
            -- and lexical hits share one namespace.  vec0 refuses both RENAME and ADD COLUMN, so the
            -- rename was a staged rebuild: rows out to a plain table, drop, recreate under the same
            -- name, rows back -- all inside one transaction.
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                page_key TEXT PRIMARY KEY,
                embedding float[3072]
            )
        """)
        conn.execute("""
            -- Which model produced the projection currently in ``vec_embeddings``.
            --
            -- ``embedding_runs`` records the model per *run*, so once more than one run exists it
            -- cannot answer "which model produced the vectors now stored", and that is the question
            -- that matters: changing ``VECTOR_LAKE_EMBEDDING_MODEL`` moves the query encoder while
            -- the stored vectors stay where they are, so every similarity quietly becomes
            -- meaningless with nothing to compare against.  vec0 refuses extra columns, hence a
            -- separate single-row marker rather than per-row provenance.
            CREATE TABLE IF NOT EXISTS vec_embedding_projection (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                model TEXT,
                dimension INTEGER,
                written_at TEXT
            )
        """)
        conn.execute("""
            -- The embedding *input* each stored vector was built from, per page.
            --
            -- A vector can be present and still wrong: nothing deletes it when a page is rewritten
            -- by a path that bypasses the incremental index, or when a full rebuild changes the
            -- aliases and summary that ``embedding_text_for_node`` reads.  Measured 2026-09-21 by
            -- re-embedding all 7 175 nodes and comparing bit for bit: 7 172 came back identical
            -- and 3 did not -- and the provider is bit-deterministic (same text twice gives
            -- max|delta| = 0), so those 3 were stale inputs, not noise.  Without this table that
            -- class is only findable by paying for a full re-embed and diffing.
            --
            -- ``content_digest`` is a digest of the inputs, not of the derived text: the text is a
            -- pure function of them, and digesting the inputs avoids re-running the whitespace
            -- collapse and the token estimate over the corpus on every check.
            CREATE TABLE IF NOT EXISTS vec_embedding_inputs (
                node_key TEXT PRIMARY KEY,
                content_digest TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        for col, col_type in [("type", "TEXT"), ("status", "TEXT"), ("ttl", "INTEGER"), ("decay_weight", "REAL")]:
            try:
                conn.execute(f"ALTER TABLE entities ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
        # ``page_key`` is named by nine statements across six modules (thirteen source lines) and
        # was only ever reachable through
        # ``json_extract``, so it gets a *generated* column: unlike the four above (which a writer
        # has to keep in step, and which nothing but a full rewrite can backfill), a virtual
        # column is by construction always equal to the json it derives from -- there is no drift
        # to compare, no trigger, and no separate projection table to keep consistent.
        if "f_page_key" not in _table_xcolumns(conn, "entities"):
            conn.execute(
                "ALTER TABLE entities ADD COLUMN f_page_key TEXT "
                "GENERATED ALWAYS AS (json_extract(data_json, '$.page_key')) VIRTUAL"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        # The three json paths this table is queried by, as generated columns: equal to the json by
        # construction.  ``status`` is deliberately absent -- ``claims.status`` is already a real
        # column and nothing filters the json spelling.
        for column, path in (("f_claim_type", "$.claim_type"), ("f_page_key", "$.locator.page_key"),
                             ("f_source_page", "$.source_page")):
            if column not in _table_xcolumns(conn, "claims"):
                conn.execute(
                    f"ALTER TABLE claims ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        if "f_page_key" not in _table_xcolumns(conn, "evidence"):
            conn.execute(
                "ALTER TABLE evidence ADD COLUMN f_page_key TEXT "
                "GENERATED ALWAYS AS (json_extract(data_json, '$.locator.page_key')) VIRTUAL"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_sets (
                change_set_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        for column, path in (("f_status", "$.status"), ("f_idempotency_key", "$.idempotency_key")):
            if column not in _table_xcolumns(conn, "change_sets"):
                conn.execute(
                    f"ALTER TABLE change_sets ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_set_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_queue (
                item_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mutation_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                mutation_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        outbox_columns = [
            ("payload_text", "TEXT"),
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT"),
            ("available_at", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("idempotency_key", "TEXT"),
            ("validation_mode", "TEXT DEFAULT 'full'"),
        ]
        for column_name, column_type in outbox_columns:
            try:
                conn.execute(f"ALTER TABLE mutation_outbox ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "UPDATE mutation_outbox SET available_at = created_at "
            "WHERE available_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_ready "
            "ON mutation_outbox(status, available_at, lease_until, id)"
        )
        _migrate_outbox_fencing(conn)
        # The unique idempotency index is defence-in-depth on top of the SELECT
        # performed by enqueue_mutation().  A database written by an earlier
        # release can already hold duplicate keys, and failing here would make
        # init_db() - and therefore every command - unusable on that database.
        # ``_ensure_idempotency_index`` degrades to a partial index over the
        # non-terminal rows rather than giving up the guarantee entirely.
        _ensure_idempotency_index(conn, "mutation_outbox", "idx_mutation_outbox_idempotency")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_index USING fts5(
                node_key, title, summary, text,
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_search_index_state (
                node_key TEXT PRIMARY KEY,
                content_hash TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tantivy_mirror_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                phase TEXT NOT NULL CHECK (phase IN ('needs_clear', 'seeding', 'replay', 'ready')),
                seed_after_rowid INTEGER NOT NULL,
                seed_through_rowid INTEGER NOT NULL,
                incarnation TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                last_error TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO tantivy_mirror_state
                (singleton, revision, epoch, phase, seed_after_rowid, seed_through_rowid)
            SELECT 1, 0, 0, 'needs_clear', 0, COALESCE(MAX(rowid), 0)
            FROM wiki_search_index
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tantivy_mirror_queue (
                node_key TEXT PRIMARY KEY NOT NULL,
                epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
                fts_rowid INTEGER,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                CHECK ((operation = 'upsert' AND fts_rowid IS NOT NULL)
                    OR (operation = 'delete' AND fts_rowid IS NULL))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tantivy_mirror_eligible "
                     "ON tantivy_mirror_queue(epoch, available_at, generation)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alias_registry (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operational_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT,
                score REAL,
                status TEXT,
                ttl REAL,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE operational_memory ADD COLUMN status TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE operational_memory ADD COLUMN ttl REAL")
        except sqlite3.OperationalError:
            pass
        # ``memory_key`` and ``source_claim_id`` have no real column and are queried (the composite
        # one by ``WHERE memory_type = ? AND memory_key = ?``), so they get *generated* columns:
        # equal to the json by construction, so there is no drift to compare.  The
        # ``memory_type``/``status`` json indexes are a different case -- every statement names the
        # real columns, which an expression index cannot serve -- so those are dropped rather than
        # joined by a third spelling.
        for column, path in (("f_memory_key", "$.memory_key"), ("f_source_claim_id", "$.source_claim_id")):
            if column not in _table_xcolumns(conn, "operational_memory"):
                conn.execute(
                    f"ALTER TABLE operational_memory ADD COLUMN {column} TEXT "
                    f"GENERATED ALWAYS AS (json_extract(data_json, '{path}')) VIRTUAL"
                )
        _create_operational_memory_index(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_graph_edges (
                source_id TEXT,
                target_id TEXT,
                relation TEXT,
                weight REAL,
                updated_at TEXT,
                PRIMARY KEY (source_id, target_id, relation)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id TEXT PRIMARY KEY,
                claim_id TEXT,
                event_date TEXT,
                event_date_source TEXT,
                action TEXT,
                sentiment TEXT,
                description TEXT,
                entity_id TEXT,
                entity_title TEXT,
                source_file TEXT,
                extracted_at TEXT
            )
        """)
        # ``event_date_source`` (``day``/``coarse``/``unknown``) records what ``event_date``
        # is: before it existed, a claim that states no date stored its *ingestion* timestamp
        # in ``event_date`` and no reader could tell that apart from a real event date.  The
        # column is additive, but rows written earlier keep the old meaning until
        # ``repair_timeline_projection`` (or ``rebuild_timeline_events_from_claims``) rewrites
        # them; ``init_db`` only guarantees the column is there to write to.
        if "event_date_source" not in _table_xcolumns(conn, "timeline_events"):
            conn.execute("ALTER TABLE timeline_events ADD COLUMN event_date_source TEXT")
        # ``claim_id`` says which canonical claim a row was projected from.  The event id is
        # a hash, so without this column an orphan row cannot be traced back to the claim
        # that produced it -- and the delta's delete recomputes that hash from the *stored*
        # claim, which stops matching as soon as anything rewrites the claim outside the
        # delta path.  With the column the delete is exact and identity-independent; rows
        # written earlier carry NULL and are converged by ``repair_timeline_projection``.
        if "claim_id" not in _table_xcolumns(conn, "timeline_events"):
            conn.execute("ALTER TABLE timeline_events ADD COLUMN claim_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(event_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_entity ON timeline_events(entity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_claim ON timeline_events(claim_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filepath TEXT PRIMARY KEY,
                file_hash TEXT,
                processed_at TEXT,
                observed_mtime_ns INTEGER,
                observed_size INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT NOT NULL,
                candidates INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                failed_batches INTEGER DEFAULT 0,
                last_error TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_runs_status "
            "ON embedding_runs(status, updated_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_rate_reservations (
                reservation_id TEXT PRIMARY KEY,
                reserved_at REAL NOT NULL,
                token_count INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_rate_window "
            "ON embedding_rate_reservations(reserved_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                task_type TEXT,
                payload TEXT,
                status TEXT,
                retries INTEGER DEFAULT 0,
                error_msg TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        for column_name, column_type in [
            ("available_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_generation", "INTEGER DEFAULT 0"),
            ("idempotency_key", "TEXT"),
            ("task_packet_path", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute("UPDATE jobs SET available_at = created_at WHERE available_at IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_ready "
            "ON jobs(status, available_at, lease_until, created_at)"
        )
        # Same defence-in-depth trade as idx_mutation_outbox_idempotency above:
        # a database written by a release that predates this index can already
        # hold duplicate keys, and failing here would make init_db() - and
        # therefore every command in a fresh process - unusable.
        _ensure_idempotency_index(conn, "jobs", "idx_jobs_idempotency")

        # The author facet invalidates on ``(COUNT(*), MAX(updated_at))`` of this table, and
        # without an index that key read is a full scan that has to walk every row -- measured
        # 259 ms on 2026-09-25 against 3.8k rows whose payloads hold ~157 MB of ingest prompts.
        # The index makes both halves an index-only lookup, so the facet can cheaply notice a
        # ledger change instead of being recomputed on every query.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)")

        # Alias lookups filter on ``value``; without this the 12k-row registry is a full scan per
        # call (4.386 ms measured, 2 805 calls in one lint pass).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alias_registry_value ON alias_registry(value)"
        )

        # The FTS projection's deletion key, and its backfill.  Additive: a database that never runs
        # this still works, because every delete site keeps its ``node_key`` fallback.
        try:
            conn.execute("ALTER TABLE wiki_search_index_state ADD COLUMN fts_rowid INTEGER")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        if conn.execute(
            "SELECT 1 FROM wiki_search_index_state WHERE fts_rowid IS NULL LIMIT 1"
        ).fetchone():
            # One pass over the FTS table, not a correlated subquery per row: the latter would re-scan
            # the virtual table once per row, which is the 271 s mistake this change is about.
            pairs = list(conn.execute("SELECT rowid, node_key FROM wiki_search_index"))
            conn.executemany(
                "UPDATE wiki_search_index_state SET fts_rowid = ? WHERE node_key = ?",
                [(int(row[0]), str(row[1])) for row in pairs],
            )
            log.info("Backfilled fts_rowid for %d FTS row(s).", len(pairs))
        
        # Add expression-based indexes for performance
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_f_page_key ON entities (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_page_key ON claims (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_source_page ON claims (f_source_page)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_f_page_key ON evidence (f_page_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_change_sets_f_status ON change_sets (f_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_change_sets_f_idempotency ON change_sets (f_idempotency_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_om_f_memory_key ON operational_memory (memory_type, f_memory_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_om_f_source_claim ON operational_memory (f_source_claim_id)")
            # ``timeline_projection_parity`` runs on every timeline query; the index that serves the
            # claim_type predicate is on the generated column now, so the predicate no longer has to
            # match the expression text verbatim (its predecessor was an expression index and did).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_f_claim_type ON claims (f_claim_type)")
        except sqlite3.OperationalError as e:
            # Older SQLite versions might not support expression indexes
            import logging
            logging.getLogger("vector-lake-db").warning(f"Could not create JSON expression indexes: {e}")
    _INITIALIZED_DB_PATHS.add(db_key)



@contextmanager
def _fts_mutation():
    """Atomic FTS + mirror intent, without taking ownership of a caller transaction.

    Schema adoption must happen before BEGIN/SAVEPOINT. Missing derived schema
    inside a caller transaction is an error, never a reason to commit that caller.
    """
    conn = get_connection()
    if not conn.in_transaction:
        init_db()
        with transaction():
            yield conn
        return
    for table in ("tantivy_mirror_state", "tantivy_mirror_queue"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
            raise RuntimeError("Initialize Tantivy mirror schema before opening a transaction")
    if not conn.execute("SELECT 1 FROM tantivy_mirror_state WHERE singleton = 1").fetchone():
        raise RuntimeError("Missing Tantivy mirror control row")
    name = "fts_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
        conn.execute(f"RELEASE {name}")
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise


def _enqueue_tantivy_intent(conn, node_key: str, operation: str, rowid: int | None):
    changed = conn.execute("UPDATE tantivy_mirror_state SET revision = revision + 1, "
                           "phase = CASE WHEN phase = 'ready' THEN 'replay' ELSE phase END WHERE singleton = 1")
    if changed.rowcount != 1:
        raise RuntimeError("Missing Tantivy mirror control row")
    queued = conn.execute("""
        INSERT INTO tantivy_mirror_queue (node_key, epoch, generation, operation, fts_rowid)
        SELECT ?, epoch, revision, ?, ? FROM tantivy_mirror_state WHERE singleton = 1
        ON CONFLICT(node_key) DO UPDATE SET epoch = excluded.epoch,
            generation = excluded.generation, operation = excluded.operation,
            fts_rowid = excluded.fts_rowid, attempt_count = 0, available_at = 0, last_error = NULL
    """, (node_key, operation, rowid))
    if queued.rowcount != 1:
        raise RuntimeError("Tantivy mirror intent was not enqueued")


def _reset_tantivy_epoch(conn):
    changed = conn.execute("""
        UPDATE tantivy_mirror_state SET revision = revision + 1, epoch = epoch + 1,
            phase = 'needs_clear', seed_after_rowid = 0,
            seed_through_rowid = (SELECT COALESCE(MAX(rowid), 0) FROM wiki_search_index),
            incarnation = NULL, attempt_count = 0, available_at = 0, last_error = NULL
        WHERE singleton = 1
    """)
    if changed.rowcount != 1:
        raise RuntimeError("Missing Tantivy mirror control row")
    conn.execute("DELETE FROM tantivy_mirror_queue")


@dataclass(frozen=True)
class TantivyMirrorToken:
    """Fresh autocommit readiness identity; native incarnation must also be validated."""

    revision: int
    epoch: int
    incarnation: str


@dataclass(frozen=True)
class TantivyMirrorIntent:
    """Snapshot identity and admitted payload (None for delete or rejected upsert)."""

    node_key: str
    epoch: int
    generation: int
    operation: str
    fts_rowid: int | None
    payload_bytes: int
    title: str | None = None
    summary: str | None = None
    text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class TantivyMirrorBatch:
    """At most max_rows candidates and max_bytes materialized UTF-8 input."""

    epoch: int
    intents: tuple[TantivyMirrorIntent, ...]
    rejected: tuple[TantivyMirrorIntent, ...]
    payload_bytes: int


def _tantivy_limits(max_rows=100, max_bytes=4 * 1024 * 1024, max_attempts=3):
    for name, value, cap in (("max_rows", max_rows, 100), ("max_bytes", max_bytes, 4 * 1024 * 1024),
                             ("max_attempts", max_attempts, 3)):
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError(f"{name} must be an integer in [1, {cap}]")


def _tantivy_autocommit():
    conn = get_connection()
    if conn.in_transaction or getattr(_LOCAL, "in_transaction", False):
        raise RuntimeError("Tantivy consumer requires SQLite autocommit")
    init_db()
    return conn


def tantivy_mirror_state(max_attempts: int = 3) -> dict:
    """Control fields plus pending/blocked counts; no native availability claim."""
    _tantivy_limits(max_attempts=max_attempts)
    conn = _tantivy_autocommit()
    with read_snapshot():
        state = dict(conn.execute("SELECT * FROM tantivy_mirror_state WHERE singleton = 1").fetchone())
        counts = conn.execute("SELECT COUNT(*), COALESCE(SUM(attempt_count >= ?), 0) "
                              "FROM tantivy_mirror_queue", (max_attempts,)).fetchone()
    state.update(pending=counts[0], blocked=counts[1])
    return state


def request_tantivy_rebuild() -> int:
    """Invalidate readiness, capture seed high-water rowid, discard obsolete intents; return epoch."""
    conn = _tantivy_autocommit()
    with transaction():
        if conn.execute("SELECT 1 FROM wiki_search_index GROUP BY node_key HAVING COUNT(*) > 1 LIMIT 1").fetchone():
            raise RuntimeError("Duplicate FTS node_key prevents Tantivy rebuild")
        _reset_tantivy_epoch(conn)
        return conn.execute("SELECT epoch FROM tantivy_mirror_state WHERE singleton = 1").fetchone()[0]


def tantivy_mirror_admit_clear(epoch: int, max_attempts: int = 3) -> bool:
    """Persist one clear attempt before native work, including crash attempts."""
    _tantivy_limits(max_attempts=max_attempts)
    conn = _tantivy_autocommit()
    with transaction():
        return conn.execute("UPDATE tantivy_mirror_state SET attempt_count = attempt_count + 1 "
                            "WHERE singleton = 1 AND epoch = ? AND phase = 'needs_clear' "
                            "AND attempt_count < ? AND available_at <= ?",
                            (epoch, max_attempts, time.time())).rowcount == 1


def tantivy_mirror_ack_clear(epoch: int, incarnation: str) -> bool:
    """After successful native clear/release, conditionally start seeding this epoch."""
    if not isinstance(incarnation, str) or not incarnation or len(incarnation) > 128:
        raise ValueError("incarnation must be a nonempty string of at most 128 characters")
    conn = _tantivy_autocommit()
    with transaction():
        return conn.execute("UPDATE tantivy_mirror_state SET phase = 'seeding', incarnation = ?, "
                            "attempt_count = 0, available_at = 0, last_error = NULL "
                            "WHERE singleton = 1 AND epoch = ? AND phase = 'needs_clear' AND attempt_count > 0",
                            (incarnation, epoch)).rowcount == 1


def _tantivy_retry_delay(retry_delay: float):
    if not isinstance(retry_delay, (int, float)) or not 0 <= retry_delay <= 3600:
        raise ValueError("retry_delay must be in [0, 3600] seconds")


def tantivy_mirror_fail_clear(epoch: int, error: str, retry_delay: float = 1) -> bool:
    """Retain bounded control error; no internal retry and no attempt increment."""
    _tantivy_retry_delay(retry_delay)
    conn = _tantivy_autocommit()
    with transaction():
        return conn.execute("UPDATE tantivy_mirror_state SET last_error = ?, available_at = ? "
                            "WHERE singleton = 1 AND epoch = ? AND phase = 'needs_clear'",
                            (str(error)[:2000], time.time() + retry_delay, epoch)).rowcount == 1


def tantivy_mirror_seed(epoch: int, max_rows: int = 100) -> dict:
    """One metadata-only keyset page; existing newer intents always win. No native replay until done."""
    _tantivy_limits(max_rows=max_rows)
    conn = _tantivy_autocommit()
    with transaction():
        state = conn.execute("SELECT * FROM tantivy_mirror_state WHERE singleton = 1").fetchone()
        if state["epoch"] != epoch or state["phase"] != "seeding":
            return {"scanned": 0, "enqueued": 0, "phase": state["phase"], "conflict": True}
        if state["seed_after_rowid"] == 0 and conn.execute(
            "SELECT 1 FROM wiki_search_index GROUP BY node_key HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone():
            raise RuntimeError("Duplicate FTS node_key prevents Tantivy seeding")
        rows = conn.execute("SELECT rowid, node_key FROM wiki_search_index "
                            "WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                            (state["seed_after_rowid"], state["seed_through_rowid"], max_rows)).fetchall()
        added = 0
        for row in rows:
            added += conn.execute("INSERT OR IGNORE INTO tantivy_mirror_queue "
                                  "(node_key, epoch, generation, operation, fts_rowid) VALUES (?, ?, ?, 'upsert', ?)",
                                  (row["node_key"], epoch, state["revision"], row["rowid"])).rowcount
        after = rows[-1]["rowid"] if rows else state["seed_through_rowid"]
        done = len(rows) < max_rows or after >= state["seed_through_rowid"]
        phase = "replay" if done else "seeding"
        conn.execute("UPDATE tantivy_mirror_state SET seed_after_rowid = ?, phase = ? WHERE singleton = 1",
                     (after, phase))
        return {"scanned": len(rows), "enqueued": added, "phase": phase, "conflict": False}


def tantivy_mirror_select(max_rows: int = 100, max_bytes: int = 4 * 1024 * 1024,
                         max_attempts: int = 3) -> TantivyMirrorBatch:
    """Short snapshot: sizes first, text only for fitting rows. Caller must hold native ownership.

    At most max_rows eligible candidates are inspected. Oversized/missing rows
    are returned as rejected metadata, never text. Byte-budget deferred rows
    remain eligible for later steps. Selection does not consume attempts.
    """
    _tantivy_limits(max_rows, max_bytes, max_attempts)
    conn = _tantivy_autocommit()
    selected, rejected = [], []
    total = 0
    with read_snapshot():
        state = conn.execute("SELECT epoch, phase FROM tantivy_mirror_state WHERE singleton = 1").fetchone()
        epoch = state["epoch"]
        if state["phase"] != "replay":
            return TantivyMirrorBatch(epoch, (), (), 0)
        rows = conn.execute("""
            SELECT q.*, f.rowid AS found_rowid,
                length(CAST(q.node_key AS BLOB)) +
                COALESCE(length(CAST(f.title AS BLOB)), 0) +
                COALESCE(length(CAST(f.summary AS BLOB)), 0) +
                COALESCE(length(CAST(f.text AS BLOB)), 0) AS payload_bytes
            FROM tantivy_mirror_queue q LEFT JOIN wiki_search_index f
                ON q.operation = 'upsert' AND f.rowid = q.fts_rowid AND f.node_key = q.node_key
            WHERE q.epoch = ? AND q.attempt_count < ? AND q.available_at <= ?
            ORDER BY q.generation, q.node_key LIMIT ?
        """, (epoch, max_attempts, time.time(), max_rows)).fetchall()
        for row in rows:
            size = row["payload_bytes"]
            error = None
            if row["operation"] == "upsert" and row["found_rowid"] is None:
                error = "FTS rowid/node_key missing; rebuild required"
            elif size > max_bytes:
                error = "FTS payload exceeds max_bytes; not materialized"
            identity = {"node_key": row["node_key"], "epoch": epoch, "generation": row["generation"],
                        "operation": row["operation"], "fts_rowid": row["fts_rowid"], "payload_bytes": size}
            if error:
                rejected.append(TantivyMirrorIntent(**identity, error=error))
                continue
            if total + size > max_bytes:
                continue
            payload = {}
            if row["operation"] == "upsert":
                payload = dict(conn.execute("SELECT title, summary, text FROM wiki_search_index "
                                            "WHERE rowid = ? AND node_key = ?",
                                            (row["fts_rowid"], row["node_key"])).fetchone())
            selected.append(TantivyMirrorIntent(**identity, **payload))
            total += size
    return TantivyMirrorBatch(epoch, tuple(selected), tuple(rejected), total)


def tantivy_mirror_admit(batch: TantivyMirrorBatch, max_attempts: int = 3) -> tuple[TantivyMirrorIntent, ...]:
    """Persist attempts; return only still-current intents to send to native. Retain rejected rows."""
    _tantivy_limits(max_attempts=max_attempts)
    if len(batch.intents) + len(batch.rejected) > 100 or batch.payload_bytes > 4 * 1024 * 1024:
        raise ValueError("Batch exceeds admission limits")
    conn = _tantivy_autocommit()
    admitted = []
    with transaction():
        state = conn.execute("SELECT epoch, phase FROM tantivy_mirror_state WHERE singleton = 1").fetchone()
        if state["epoch"] != batch.epoch or state["phase"] != "replay":
            return ()
        for item in batch.rejected:
            conn.execute("UPDATE tantivy_mirror_queue SET attempt_count = ?, last_error = ? "
                         "WHERE node_key = ? AND epoch = ? AND generation = ?",
                         (max_attempts, str(item.error)[:2000], item.node_key, item.epoch, item.generation))
        for item in batch.intents:
            changed = conn.execute("UPDATE tantivy_mirror_queue SET attempt_count = attempt_count + 1 "
                                   "WHERE node_key = ? AND epoch = ? AND generation = ? "
                                   "AND attempt_count < ? AND available_at <= ?",
                                   (item.node_key, item.epoch, item.generation, max_attempts, time.time())).rowcount
            if changed:
                admitted.append(item)
    return tuple(admitted)


def tantivy_mirror_ack(intents: tuple[TantivyMirrorIntent, ...]) -> int:
    """Native commit AND release precede this conditional ACK; newer generations survive."""
    if len(intents) > 100:
        raise ValueError("At most 100 ACKs per step")
    conn = _tantivy_autocommit()
    with transaction():
        return sum(conn.execute("DELETE FROM tantivy_mirror_queue WHERE node_key = ? AND epoch = ? "
                                "AND generation = ?", (item.node_key, item.epoch, item.generation)).rowcount
                   for item in intents)


def tantivy_mirror_fail(intents: tuple[TantivyMirrorIntent, ...], error: str, retry_delay: float = 1) -> int:
    """Retain conditional errors after admission; never immediately retry or overwrite newer intent."""
    _tantivy_retry_delay(retry_delay)
    if len(intents) > 100:
        raise ValueError("At most 100 failures per step")
    conn = _tantivy_autocommit()
    with transaction():
        return sum(conn.execute("UPDATE tantivy_mirror_queue SET last_error = ?, available_at = ? "
                                "WHERE node_key = ? AND epoch = ? AND generation = ?",
                                (str(error)[:2000], time.time() + retry_delay,
                                 item.node_key, item.epoch, item.generation)).rowcount for item in intents)


def tantivy_mirror_mark_ready(epoch: int, incarnation: str) -> bool:
    """Only after native success/release; require replay drained in the same write transaction."""
    conn = _tantivy_autocommit()
    with transaction():
        if conn.execute("SELECT 1 FROM wiki_search_index GROUP BY node_key HAVING COUNT(*) > 1 LIMIT 1").fetchone():
            raise RuntimeError("Duplicate FTS node_key prevents Tantivy readiness")
        return conn.execute("UPDATE tantivy_mirror_state SET phase = 'ready' WHERE singleton = 1 "
                            "AND epoch = ? AND incarnation = ? AND phase = 'replay' "
                            "AND NOT EXISTS (SELECT 1 FROM tantivy_mirror_queue)", (epoch, incarnation)).rowcount == 1


def tantivy_mirror_read_token() -> TantivyMirrorToken | None:
    """Single fresh autocommit view; None inside caller transactions or when not ready.

    Compare tokens before/after native + metadata reads. Native owner must
    separately validate incarnation/schema; this helper performs no native IO.
    """
    conn = get_connection()
    if conn.in_transaction or getattr(_LOCAL, "in_transaction", False):
        return None
    row = conn.execute("SELECT revision, epoch, incarnation FROM tantivy_mirror_state "
                       "WHERE singleton = 1 AND phase = 'ready' AND incarnation IS NOT NULL "
                       "AND NOT EXISTS (SELECT 1 FROM tantivy_mirror_queue)").fetchone()
    return TantivyMirrorToken(*row) if row else None


def _tantivy():
    """The optional tantivy mirror for the FTS projection, or ``None`` when it is off.

    Switch-gated by ``VECTOR_LAKE_FTS`` (default ``fts5``).  Every mirror call is wrapped by the
    caller so an auxiliary index can never lose a page write; the FTS5 tables stay authoritative
    and ``tantivy_index.rebuild_from_sqlite`` can rebuild the mirror from them at any time.
    """
    from vector_lake import tantivy_index

    return tantivy_index if tantivy_index.enabled() else None


def _tan_quiet(action, *args, **kwargs):
    """Run a mirror operation, logging rather than raising: a mirror must not fail a write."""
    try:
        return action(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberate: the authoritative write already landed
        logging.getLogger("vector-lake-tantivy").warning(
            "tantivy mirror %s failed: %s: %s (rebuild with tantivy_index.rebuild_from_sqlite)",
            getattr(action, "__name__", action), type(exc).__name__, exc,
        )
        return None


def search_index_keys() -> set[str]:
    """Every node_key currently materialised in the FTS projection."""
    init_db()
    return {
        row["node_key"]
        for row in get_connection().execute("SELECT DISTINCT node_key FROM wiki_search_index")
    }


def upsert_search_index(
    node_key: str,
    title: str,
    summary: str,
    text: str,
    content_hash: str | None = None,
    replace_existing: bool | None = None,
):
    """Write one pre-tokenized node into the FTS projection.

    Callers must pass text that is already tokenized; re-tokenizing here doubled
    the jieba cost of every index rebuild.

    ``replace_existing`` is the difference between a 4-second and a 6-minute rebuild, and it is the
    caller's knowledge, not this function's: ``False`` means "this key is known to have no row yet"
    (a caller that just read the FTS key set knows this), so the delete is skipped.  Measured
    2026-09-25 on the live lake: a cold rebuild inserted 7 039 missing rows in **367 s = 52.1 ms per
    row**, which is exactly the cost of the ``DELETE ... WHERE node_key = ?`` fallback running on
    rows that do not exist -- an FTS5 table cannot serve a lookup on a column, so each delete scans
    the whole virtual index.  ``None`` keeps the previous behaviour for callers that cannot say.
    """
    with _fts_mutation() as conn:
        # FTS5 does not support ON CONFLICT REPLACE, so this deletes then inserts -- and the delete
        # must go through the rowid: ``WHERE node_key = ?`` scans the whole virtual table (52.4 ms
        # per row measured, against 0.33 ms by rowid).  The node_key form is kept as well so a row
        # whose position was never recorded is still removed.
        if replace_existing is not False:
            rowid = _search_index_rowid(conn, node_key)
            if rowid is not None:
                # Verified by node_key as well: a rowid is a position, and a position can be
                # reassigned if the index is ever rebuilt, so deleting on it alone could remove
                # another page's row.  The pair is still a point delete -- the row is located by
                # rowid and only its node_key column is checked -- where the node_key alone is a
                # full scan of the virtual table.
                conn.execute(
                    "DELETE FROM wiki_search_index WHERE rowid = ? AND node_key = ?",
                    (rowid, node_key),
                )
            else:
                conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        cursor = conn.execute("""
            INSERT INTO wiki_search_index (node_key, title, summary, text)
            VALUES (?, ?, ?, ?)
        """, (node_key, title, summary, text))
        if content_hash is not None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_search_index_state"
                " (node_key, content_hash, updated_at, fts_rowid) VALUES (?, ?, ?, ?)",
                (node_key, content_hash, datetime.now(timezone.utc).isoformat(), cursor.lastrowid),
            )
        _enqueue_tantivy_intent(conn, node_key, "upsert", cursor.lastrowid)


def clear_search_index():
    """Reset the FTS projection and its content-hash state."""
    with _fts_mutation() as conn:
        conn.execute("DELETE FROM wiki_search_index")
        conn.execute("DELETE FROM wiki_search_index_state")
        _reset_tantivy_epoch(conn)


def _search_index_rowid(conn: sqlite3.Connection, node_key: str) -> int | None:
    """The FTS rowid recorded for a node, or ``None`` when none was recorded.

    Read through the state table, which is keyed by ``node_key`` -- asking the FTS table itself would
    be the full scan this function exists to avoid.
    """
    try:
        row = conn.execute(
            "SELECT fts_rowid FROM wiki_search_index_state WHERE node_key = ?", (node_key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def search_index_state() -> dict[str, str]:
    """node_key -> content hash of the last tokenized content."""
    init_db()
    return {
        row["node_key"]: row["content_hash"]
        for row in get_connection().execute("SELECT node_key, content_hash FROM wiki_search_index_state")
    }

def _store_vector(conn, page_key, blob) -> None:
    """The only place that writes ``vec_embeddings``.

    Keeping the mirror call inside the same helper is the point: a second writer elsewhere would
    keep the row counts equal while serving stale vectors, which no count check can detect.  A
    guard test asserts no other write to this table exists in the module.
    """
    from vector_lake import two_stage_index
    conn.execute("DELETE FROM vec_embeddings WHERE page_key = ?", (str(page_key),))
    conn.execute(
        "INSERT INTO vec_embeddings (page_key, embedding) VALUES (?, ?)", (str(page_key), blob)
    )
    two_stage_index.mirror_write(conn, page_key, blob)


def _delete_vectors(conn, page_keys) -> None:
    """The only place that deletes from ``vec_embeddings``, mirrored in the same transaction."""
    from vector_lake import two_stage_index
    keys = [(str(key),) for key in page_keys]
    conn.executemany("DELETE FROM vec_embeddings WHERE page_key = ?", keys)
    two_stage_index.mirror_delete(conn, page_keys)


def upsert_embedding(page_key: str, embedding: list[float], *, content_digest: str | None = None):
    """Store one unit vector under a page key.

    The normalisation is load-bearing, not cosmetic: ``tool_search`` converts sqlite-vec's L2
    distance with ``1 - d^2/2`` and gates on a cosine threshold, and both are only correct for unit
    vectors.  A sampled norm measures exactly 1.000000 because of this function -- if it were
    removed, the conversion and the gate would go wrong together and quietly.
    """
    if not embedding:
        return
    import math
    embedding = [float(value) for value in embedding]
    norm = math.hypot(*embedding)
    if not all(math.isfinite(value) for value in embedding) or not math.isfinite(norm) or norm == 0:
        raise ValueError('Embedding must be finite and non-zero')
    embedding = [value / norm for value in embedding]
    conn = get_connection()
    import sqlite_vec
    query_blob = sqlite_vec.serialize_float32(embedding)
    with transaction():
        _store_vector(conn, page_key, query_blob)
        # A digest is written only when the caller actually has one.  Storing a NULL or guessed
        # digest would make an unverified row look verified, and the reader treats "no row" as
        # "needs stamping", so the no-digest path converges instead of lying.
        if content_digest is not None:
            conn.execute(
                "INSERT INTO vec_embedding_inputs (node_key, content_digest, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(node_key) DO UPDATE SET "
                "content_digest = excluded.content_digest, updated_at = excluded.updated_at",
                (str(page_key), str(content_digest), datetime.now(timezone.utc).isoformat()),
            )


def embedding_input_digests() -> dict[str, str]:
    """The recorded embedding-input digest per page key.

    A key absent from this mapping has a vector that was never stamped, which the staleness
    check treats as unverified rather than current.
    """
    conn = get_connection()
    return {
        row["node_key"]: row["content_digest"]
        for row in conn.execute("SELECT node_key, content_digest FROM vec_embedding_inputs")
    }


def delete_embedding(page_key: str):
    conn = get_connection()
    with transaction():
        _delete_vectors(conn, [page_key])
        conn.execute("DELETE FROM vec_embedding_inputs WHERE node_key = ?", (str(page_key),))


def delete_stale_embeddings(valid_page_keys: set[str]) -> int:
    conn = get_connection()
    valid = {str(item) for item in valid_page_keys if item}
    rows = conn.execute("SELECT page_key FROM vec_embeddings").fetchall()
    stale = [row["page_key"] for row in rows if row["page_key"] not in valid]
    if not stale:
        return 0
    with transaction():
        _delete_vectors(conn, stale)
        # The input ledger is keyed the same way and would otherwise keep rows for pages that no
        # longer exist, which reads as "every orphan is stale" on the next sweep.
        conn.executemany("DELETE FROM vec_embedding_inputs WHERE node_key = ?", [(page_key,) for page_key in stale])
    return len(stale)

def count_embeddings() -> int:
    conn = get_connection()
    return int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0])


def record_embedding_projection(model: str, dimension: int) -> None:
    """Record which model produced the vectors currently in ``vec_embeddings``.

    Called by the backfill once it has written rows, because that is the only moment the
    table's content and this marker agree.  See the DDL for why the model cannot live on the
    vector rows themselves.
    """
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn = get_connection()
        previous = conn.execute(
            "SELECT model, dimension FROM vec_embedding_projection WHERE id = 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO vec_embedding_projection (id, model, dimension, written_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET model = excluded.model, "
            "dimension = excluded.dimension, written_at = excluded.written_at",
            (str(model), int(dimension), now),
        )
        # A new projection means the vectors were produced by a different encoder than the shadow
        # was built from.  Invalidating it here makes the read path fall back to the exact scan
        # until a rebuild, instead of mixing encoders under equal row counts.
        if previous and (previous["model"], int(previous["dimension"])) != (str(model), int(dimension)):
            from vector_lake import two_stage_index
            conn.execute(f"DELETE FROM {two_stage_index.META_TABLE} WHERE id = 1")


def embedding_projection_state() -> dict:
    """The recorded model/dimension of the stored projection, or ``{}`` when unrecorded."""
    row = get_connection().execute(
        "SELECT model, dimension, written_at FROM vec_embedding_projection WHERE id = 1"
    ).fetchone()
    return dict(row) if row else {}


def start_embedding_run(run_id: str, model: str, candidates: int):
    import os

    now = datetime.now(timezone.utc).isoformat()
    stale_after = max(60, int(os.environ.get("VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS", "3600")))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    with transaction():
        conn = get_connection()
        conn.execute(
            "UPDATE embedding_runs SET status = 'abandoned', completed_at = ?, updated_at = ?, "
            "last_error = 'Previous embedding process stopped without finalizing the run' "
            "WHERE status = 'running' AND updated_at < ?",
            (now, now, cutoff),
        )
        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, candidates, processed, failed_batches, started_at, updated_at) "
            "VALUES (?, 'running', ?, ?, 0, 0, ?, ?)",
            (run_id, model, int(candidates), now, now),
        )


def update_embedding_run(run_id: str, processed: int, failed_batches: int = 0, last_error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET processed = ?, failed_batches = ?, last_error = ?, updated_at = ? "
            "WHERE run_id = ?",
            (int(processed), int(failed_batches), str(last_error)[:2000], now, run_id),
        )


def finish_embedding_run(run_id: str, status: str, processed: int, failed_batches: int = 0, last_error: str = ""):
    if status not in {"completed", "failed", "partial"}:
        raise ValueError(f"Unsupported embedding run status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET status = ?, processed = ?, failed_batches = ?, "
            "last_error = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
            (status, int(processed), int(failed_batches), str(last_error)[:2000], now, now, run_id),
        )

def delete_search_index(node_key: str):
    with _fts_mutation() as conn:
        # By rowid when it was recorded: `WHERE node_key = ?` scans the whole FTS index
        # (52.4 ms per row measured against 0.33 ms), and the node_key form stays as the
        # fallback for rows whose position predates the backfill.
        rowid = _search_index_rowid(conn, node_key)
        if rowid is not None:
            conn.execute(
                "DELETE FROM wiki_search_index WHERE rowid = ? AND node_key = ?", (rowid, node_key)
            )
        else:
            conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM wiki_search_index_state WHERE node_key = ?", (node_key,))
        _delete_vectors(conn, [node_key])
        conn.execute("DELETE FROM vec_embedding_inputs WHERE node_key = ?", (node_key,))
        _enqueue_tantivy_intent(conn, node_key, "delete", None)

def delete_node_cascade(node_key: str, *, affected_page_keys: set[str] | None = None):
    with _fts_mutation() as conn:
        rows = conn.execute(
            "SELECT entity_id FROM entities "
            "WHERE entity_id = ? OR canonical_name = ? "
            "OR f_page_key = ?",
            (node_key, node_key, node_key),
        ).fetchall()
        entity_ids = {row["entity_id"] for row in rows}
        related_ids = sorted(entity_ids | {node_key})
        placeholders = ",".join("?" for _ in related_ids)
        old_claim_rows = conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE "
            "f_page_key = ? OR "
            "f_source_page IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        ).fetchall()
        from vector_lake.governance_store import _refresh_operational_memory_delta, _validate_claim_relation_delta

        old_claim_ids = {row["claim_id"] for row in old_claim_rows}
        scope = set(affected_page_keys or {node_key})
        if node_key not in scope:
            raise ValueError("Deleted page is outside the canonical batch scope")
        scoped_ids = old_claim_ids | {
            row[0] for row in conn.execute(
                "SELECT claim_id FROM claims WHERE f_page_key IN (SELECT value FROM json_each(?)) "
                "OR f_source_page IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(scope)), json.dumps(sorted(scope | {key + '.md' for key in scope}))),
            )
        }
        _validate_claim_relation_delta([], scoped_ids, scope)
        # Preserve the old supersession targets before removing their winner's memory.
        _refresh_operational_memory_delta(old_claim_ids, [])

        # By rowid when it was recorded: `WHERE node_key = ?` scans the whole FTS index
        # (52.4 ms per row measured against 0.33 ms), and the node_key form stays as the
        # fallback for rows whose position predates the backfill.
        rowid = _search_index_rowid(conn, node_key)
        if rowid is not None:
            conn.execute(
                "DELETE FROM wiki_search_index WHERE rowid = ? AND node_key = ?", (rowid, node_key)
            )
        else:
            conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM wiki_search_index_state WHERE node_key = ?", (node_key,))
        _delete_vectors(conn, related_ids)
        conn.execute(f"DELETE FROM vec_embedding_inputs WHERE node_key IN ({placeholders})", related_ids)
        _enqueue_tantivy_intent(conn, node_key, "delete", None)
        conn.execute(
            "DELETE FROM claims WHERE "
            "f_page_key = ? OR "
            "f_source_page IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        )
        conn.execute(
            "DELETE FROM evidence WHERE f_page_key = ?",
            (node_key,),
        )
        conn.execute(
            "DELETE FROM sources WHERE source_id = ? OR "
            "json_extract(data_json, '$.canonical_source_page') = ?",
            (node_key, node_key + ".md"),
        )
        conn.execute(f"DELETE FROM alias_registry WHERE key = ? OR value IN ({placeholders})", [node_key, *related_ids])
        conn.execute(
            f"DELETE FROM claim_graph_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            [*related_ids, *related_ids],
        )
        # Deferred on purpose, and load-order-bearing: ``tool_timeline`` imports this
        # module at module scope.  The deferred binding is also what lets
        # ``tests/test_timeline_projection.py`` inject a projection failure by patching
        # ``tool_timeline``'s name.  See ``tests/test_import_layering.py``.
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

        sync_timeline_events_for_claim_delta(old_claim_rows, [])
        conn.execute(f"DELETE FROM entities WHERE entity_id IN ({placeholders})", related_ids)

    return {"page_key": node_key, "entity_ids": sorted(entity_ids)}


def published_edge_projection_drift(max_examples: int = 3) -> dict[str, object]:
    """Whether the search projection still holds the published edge set.

    The published ``weighted_edges`` in ``index.json`` is the source; ``page_index_edges`` is its read
    projection, which ``page_index_projection.adjacency`` walks.  This check used to compare that
    projection against ``page_graph_edges`` -- a *second* SQLite table holding the same pairs, whose
    only reader was this check, so it compared two projections of one source instead of the projection
    against the source.  That table is gone (nothing read it) and the comparison is now with the file.

    The published set collapses duplicate pairs to their maximum weight, so both sides are compared as
    distinct pairs.  Read-only, and exact: both sides are tens of thousands of pairs, where the
    LIMIT-probe machinery this replaced existed for a 1.29M-row legacy state that no longer occurs.
    """
    published: set[tuple[str, str]] = set()
    file_rows = 0
    read_error = ""
    index_path = get_index_path()
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Named rather than treated as an empty published set: "the projection has pairs the file
            # does not" and "the file could not be read" are different findings, and reporting the
            # second as the first sends the operator looking in the wrong place.
            data = {}
            read_error = f"{type(exc).__name__}: {exc}"
        for edge in data.get("weighted_edges") or []:
            source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
            if source and target:
                published.add((source, target))
                file_rows += 1
    conn = get_connection()
    projection = {
        (str(row[0]), str(row[1]))
        for row in conn.execute("SELECT DISTINCT source_key, target_key FROM page_index_edges")
    }
    extra = sorted(projection - published)
    missing = sorted(published - projection)
    # Multiplicity is reported separately because the comparison is over *distinct* pairs: the
    # published set collapses duplicates to their maximum weight and the projection is re-inserted
    # from that set, so a repeated row means something wrote the table outside that path.
    projection_rows_raw = int(conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0])
    return {
        "projection_rows": len(projection),
        "published_rows": len(published),
        "difference": len(projection) - len(published),
        "extra_examples": extra[:max_examples],
        "extra": bool(extra),
        "missing": bool(missing),
        "missing_example": missing[0] if missing else None,
        "duplicate_pairs": projection_rows_raw != len(projection) or file_rows != len(published),
        "published_read_error": read_error,
    }


def enqueue_mutations_batch(items: list[dict]) -> list[int]:
    """Batch-enqueue mutations into the durable outbox within the caller's transaction."""
    return [
        enqueue_mutation(
            item["filename"],
            item["mutation_type"],
            payload_text=item.get("payload_text"),
            idempotency_key=item.get("idempotency_key"),
            validation_mode=item.get("validation_mode", "full"),
        )
        for item in items
    ]


def in_write_transaction() -> bool:
    conn = getattr(_LOCAL, "conn", None)
    return bool(getattr(_LOCAL, "in_transaction", False) or (conn is not None and conn.in_transaction))


_OUTBOX_FENCING_COLUMNS = {
    "lease_owner": "TEXT", "lease_token": "TEXT", "lease_generation": "INTEGER DEFAULT 0",
    "projection_generation": "INTEGER DEFAULT 0", "base_version": "TEXT", "superseded_by": "INTEGER",
}
_OUTBOX_REVISION_INDEX = "idx_mutation_outbox_filename_generation"


class MutationClaimLost(RuntimeError):
    """The supplied outbox capability no longer owns a live processing lease."""


class MutationSuperseded(MutationClaimLost):
    """A newer per-page revision has replaced this intent."""


def _migrate_outbox_fencing(conn):
    columns = _table_columns(conn, "mutation_outbox")
    for name, declaration in _OUTBOX_FENCING_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE mutation_outbox ADD COLUMN {name} {declaration}")
    conn.execute("UPDATE mutation_outbox SET projection_generation=id WHERE COALESCE(projection_generation,0)=0")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_OUTBOX_REVISION_INDEX} ON mutation_outbox(filename, projection_generation DESC, id DESC)")
    conn.execute("UPDATE mutation_outbox SET status='pending', lease_until=NULL, lease_owner=NULL, lease_token=NULL "
                 "WHERE status='processing' AND (lease_owner IS NULL OR lease_token IS NULL OR COALESCE(lease_generation,0)<=0)")
    conn.execute("UPDATE mutation_outbox AS old SET status='superseded', lease_until=NULL, lease_owner=NULL, lease_token=NULL, "
                 "superseded_by=(SELECT newer.id FROM mutation_outbox newer WHERE newer.filename IS old.filename "
                 "ORDER BY newer.projection_generation DESC, newer.id DESC LIMIT 1) "
                 "WHERE old.status IN ('pending','processing','failed') AND EXISTS "
                 "(SELECT 1 FROM mutation_outbox newer WHERE newer.filename IS old.filename AND "
                 "(newer.projection_generation>old.projection_generation OR "
                 "(newer.projection_generation=old.projection_generation AND newer.id>old.id)))")


def ensure_mutation_outbox_fencing():
    """Upgrade writers only; a read-only snapshot never needs this protocol."""
    init_db()
    conn = get_connection()
    if set(_OUTBOX_FENCING_COLUMNS) <= _table_columns(conn, "mutation_outbox") and _OUTBOX_REVISION_INDEX in _index_names(conn):
        return
    with transaction():
        _migrate_outbox_fencing(conn)


def mutation_outbox_revision_order() -> str:
    """Read-only compatibility for archived databases predating the writer protocol."""
    return "projection_generation DESC, id DESC" if "projection_generation" in _table_columns(get_connection(), "mutation_outbox") else "id DESC"


def current_mutation_intent(outbox_id: int) -> dict:
    row = get_connection().execute("SELECT * FROM mutation_outbox WHERE id=?", (outbox_id,)).fetchone()
    if row is None:
        raise MutationClaimLost("Outbox intent no longer exists")
    latest = get_connection().execute("SELECT id,projection_generation FROM mutation_outbox WHERE filename IS ? "
                                     "ORDER BY projection_generation DESC,id DESC LIMIT 1", (row["filename"],)).fetchone()
    if row["status"] == "superseded" or latest["id"] != row["id"] or latest["projection_generation"] != row["projection_generation"]:
        raise MutationSuperseded("Outbox intent has a newer per-page revision")
    return dict(row)


def validate_mutation_claim(claim: dict) -> dict:
    try:
        if any(isinstance(claim[name], (bool, float)) for name in ("id", "lease_generation", "projection_generation")):
            raise ValueError("outbox identity fields must be integral")
        outbox_id = int(claim["id"])
        lease_generation = int(claim["lease_generation"])
        revision = int(claim["projection_generation"])
        owner, token = claim["lease_owner"], claim["lease_token"]
        if min(outbox_id, lease_generation, revision) <= 0 or not isinstance(owner, str) or not owner or not isinstance(token, str) or not token:
            raise ValueError("invalid capability")
    except (KeyError, TypeError, ValueError) as exc:
        raise MutationClaimLost("Incomplete outbox lease capability") from exc
    row = current_mutation_intent(outbox_id)
    if row["status"] != "processing" or row["lease_owner"] != owner or row["lease_token"] != token or row["lease_generation"] != lease_generation or row["projection_generation"] != revision:
        raise MutationClaimLost("Outbox processing lease changed")
    try:
        deadline = datetime.fromisoformat(row["lease_until"])
        if deadline <= datetime.now(timezone.utc):
            raise MutationClaimLost("Outbox processing lease expired")
    except (ValueError, TypeError) as exc:
        raise MutationClaimLost("Outbox processing lease has an invalid deadline") from exc
    return row


def enqueue_mutation(filename: str, mutation_type: str, payload_text: str | None = None,
                     idempotency_key: str | None = None, validation_mode: str = "full") -> int:
    if not isinstance(filename, str) or not filename:
        raise ValueError("Outbox filename must be a nonempty string")
    if mutation_type not in {"update", "delete"}:
        raise ValueError(f"Unsupported mutation_type: {mutation_type}")
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    ensure_mutation_outbox_fencing()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        existing = conn.execute("SELECT * FROM mutation_outbox WHERE idempotency_key=?", (idempotency_key,)).fetchone() if idempotency_key else None
        if existing and (existing["filename"] != filename or existing["mutation_type"] != mutation_type):
            raise ValueError("Idempotency key belongs to a different page or mutation type")
        latest = conn.execute("SELECT id,projection_generation FROM mutation_outbox WHERE filename=? ORDER BY projection_generation DESC,id DESC LIMIT 1", (filename,)).fetchone()
        if existing and existing["status"] in {"pending", "processing"} and latest["id"] == existing["id"]:
            if existing["payload_text"] != payload_text or existing["validation_mode"] != validation_mode:
                raise ValueError("Idempotency key has a different active payload or validation mode")
            return int(existing["id"])
        revision = conn.execute("SELECT COALESCE(MAX(projection_generation),0)+1 FROM mutation_outbox WHERE filename=?", (filename,)).fetchone()[0]
        if existing:
            outbox_id = int(existing["id"])
            conn.execute("UPDATE mutation_outbox SET status='pending', attempt_count=0, last_error=NULL, available_at=?, "
                         "started_at=NULL,completed_at=NULL,lease_until=NULL,lease_owner=NULL,lease_token=NULL, "
                         "payload_text=?,validation_mode=?,projection_generation=?,base_version=NULL,superseded_by=NULL WHERE id=?",
                         (now, payload_text, validation_mode, revision, outbox_id))
        else:
            cursor = conn.execute("INSERT INTO mutation_outbox (filename,mutation_type,payload_text,status,attempt_count,created_at,available_at,idempotency_key,validation_mode,projection_generation) "
                                  "VALUES (?,?,?,'pending',0,?,?,?,?,?)", (filename, mutation_type, payload_text, now, now, idempotency_key, validation_mode, revision))
            outbox_id = int(cursor.lastrowid)
        conn.execute("UPDATE mutation_outbox SET status='superseded',superseded_by=?,lease_until=NULL,lease_owner=NULL,lease_token=NULL "
                     "WHERE filename=? AND id<>? AND status IN ('pending','processing','failed') AND projection_generation<?",
                     (outbox_id, filename, outbox_id, revision))
        return outbox_id


def bind_mutation_versions(outbox_ids: list[int], versions: dict[str, str]):
    """Bind inside the canonical transaction, after its callback has settled."""
    conn = get_connection()
    for outbox_id in outbox_ids:
        row = conn.execute("SELECT filename,mutation_type FROM mutation_outbox WHERE id=?", (outbox_id,)).fetchone()
        page_key = Path(row["filename"]).stem
        version = versions.get(page_key, "")
        if row["mutation_type"] == "delete" and version:
            raise ValueError("Delete intent still has canonical page state")
        conn.execute("UPDATE mutation_outbox SET base_version=?, lease_generation=COALESCE(lease_generation,0)+CASE WHEN status='processing' THEN 1 ELSE 0 END, "
                     "status='pending',attempt_count=0,lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?", (version, outbox_id))


def is_managed_projection_state(filename: str, mutation_type: str, payload_text: str | None = None) -> bool:
    init_db()
    row = get_connection().execute("SELECT mutation_type,payload_text FROM mutation_outbox WHERE filename=? ORDER BY "
                                  + mutation_outbox_revision_order() + " LIMIT 1", (str(filename),)).fetchone()
    return row is not None and row["mutation_type"] == mutation_type and (mutation_type == "delete" or row["payload_text"] == payload_text)


def claim_mutation_outbox(limit: int = 50, lease_seconds: int = 120, *, lease_owner: str | None = None) -> list[dict]:
    ensure_mutation_outbox_fencing()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    owner = lease_owner or f"{os.getpid()}:{uuid.uuid4().hex}"
    with transaction():
        rows = conn.execute("SELECT old.id FROM mutation_outbox old WHERE ((status='pending' AND COALESCE(available_at,created_at,'')<=?) "
                            "OR (status='processing' AND (julianday(lease_until) IS NULL OR julianday(lease_until)<=julianday(?)))) "
                            "AND NOT EXISTS (SELECT 1 FROM mutation_outbox newer WHERE newer.filename IS old.filename AND "
                            "(newer.projection_generation>old.projection_generation OR (newer.projection_generation=old.projection_generation AND newer.id>old.id))) "
                            "ORDER BY old.id LIMIT ?", (now, now, max(1, int(limit)))).fetchall()
        claimed = []
        for row in rows:
            conn.execute("UPDATE mutation_outbox SET status='processing',attempt_count=COALESCE(attempt_count,0)+1,started_at=?,lease_until=?, "
                         "lease_owner=?,lease_token=?,lease_generation=COALESCE(lease_generation,0)+1 WHERE id=?",
                         (now, lease_until, owner, uuid.uuid4().hex, row["id"]))
            claimed.append(dict(conn.execute("SELECT * FROM mutation_outbox WHERE id=?", (row["id"],)).fetchone()))
        return claimed


def complete_mutation_outbox(outbox_id: int, *, claim: dict):
    conn = get_connection()
    with transaction():
        row = validate_mutation_claim(claim)
        if row["id"] != outbox_id:
            raise MutationClaimLost("Claim addresses a different outbox intent")
        now = datetime.now(timezone.utc).isoformat()
        changed = conn.execute("UPDATE mutation_outbox SET status='completed',completed_at=?,lease_until=NULL,last_error=NULL "
                               "WHERE id=? AND status='processing' AND lease_owner=? AND lease_token=? AND lease_generation=? "
                               "AND projection_generation=? AND lease_until>?",
                               (now, outbox_id, row["lease_owner"], row["lease_token"], row["lease_generation"], row["projection_generation"], now)).rowcount
        if changed != 1:
            raise MutationClaimLost("Outbox completion lost its processing lease")


def fail_mutation_outbox(outbox_id: int, error: str, max_attempts: int = 3, backoff_base: float = 2.0, *, claim: dict) -> str:
    conn = get_connection()
    with transaction():
        row = validate_mutation_claim(claim)
        if row["id"] != outbox_id:
            raise MutationClaimLost("Claim addresses a different outbox intent")
        attempts = int(row["attempt_count"] or 0)
        now_dt = datetime.now(timezone.utc)
        terminal = attempts >= max(1, int(max_attempts))
        status = "failed" if terminal else "pending"
        delay = 0.0 if terminal else max(0.0, float(backoff_base)) * (2 ** max(0, attempts - 1))
        available_at = (now_dt + timedelta(seconds=delay)).isoformat()
        changed = conn.execute("UPDATE mutation_outbox SET status=?,last_error=?,available_at=?,lease_until=NULL,lease_owner=NULL,lease_token=NULL "
                               "WHERE id=? AND status='processing' AND lease_owner=? AND lease_token=? AND lease_generation=? "
                               "AND projection_generation=? AND lease_until>?",
                               (status, str(error)[:4000], available_at, outbox_id, row["lease_owner"], row["lease_token"], row["lease_generation"], row["projection_generation"], now_dt.isoformat())).rowcount
        if changed != 1:
            raise MutationClaimLost("Outbox failure report lost its processing lease")
    return status

def search_wiki(query: str, limit: int = 50) -> list[dict]:
    import re
    from vector_lake import tokenizer as _tokenizer

    query = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', query) if query else ""
    query_tok = _tokenizer.tokenize_joined(query)

    query_tok = query_tok.strip()
    if not query_tok:
        return []

    conn = get_connection()
    mirror = _tantivy()
    if mirror is not None:
        try:
            if not conn.in_transaction:
                init_db()
            before = mirror.current_token()
            ranked = mirror.search_if_current(query_tok.split(), limit) if before is not None else None
            if ranked is not None:
                keys = [key for key, _ in ranked]
                ranks = dict(ranked)
                placeholders = ",".join("?" for _ in keys)
                metadata = {
                    row["node_key"]: (row["title"], row["summary"])
                    for row in conn.execute(
                        f"SELECT node_key, title, summary FROM wiki_search_index WHERE node_key IN ({placeholders})",
                        keys,
                    )
                } if keys else {}
                # A fresh token after metadata rejects even a dirty->ready race.
                if mirror.current_token() == before:
                    return [
                        {"node_key": key, "title": metadata[key][0],
                         "summary": metadata[key][1], "rank": ranks[key]}
                        for key in keys if key in metadata
                    ]
        except Exception as exc:  # noqa: BLE001 - mirror faults must fall back to authoritative SQL
            logging.getLogger("vector-lake-tantivy").warning(
                "tantivy read failed: %s: %s; using FTS5", type(exc).__name__, exc,
            )

    query_esc = " ".join(f'"{t}"' for t in query_tok.split())

    cur = conn.execute("""
        SELECT node_key, title, summary, bm25(wiki_search_index) as rank 
        FROM wiki_search_index 
        WHERE wiki_search_index MATCH ? 
        ORDER BY rank LIMIT ?
    """, (query_esc, limit))
    return [dict(row) for row in cur.fetchall()]

def free_space_report(conn: sqlite3.Connection | None = None) -> dict:
    """Read-only: how much of the database file is free pages rather than data.

    Measured 2026-09-25 on the live lake: 2 301 MB file, 151 760 free pages = **593 MB** that a
    ``VACUUM`` would return.  ``auto_vacuum=0`` here, so every row deletion (a merge, a repair, a
    superseded job, a stripped payload) moves pages to the freelist and the file never shrinks.
    """
    conn = conn or get_connection()
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_size": page_size,
        "page_count": page_count,
        "file_bytes": page_count * page_size,
        "free_pages": free_pages,
        "free_bytes": free_pages * page_size,
        "auto_vacuum": int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]),
    }


def reclaim_free_space(
    conn: sqlite3.Connection | None = None,
    min_free_mb: float = 256.0,
    allow: bool | None = None,
    dry_run: bool = False,
) -> dict:
    """Return freelist pages to the filesystem with ``VACUUM`` -- when it is warranted *and* allowed.

    Deliberately not automatic.  ``VACUUM`` rewrites the whole file (2.3 GB here), needs free disk of
    roughly that size, cannot run inside a transaction, and holds an exclusive lock for its duration:
    a timer that fired it inside the daemon would turn routine storage maintenance into an outage for
    every writer.  So the daemon only *reports* (:func:`free_space_report`), and the reclaim runs in
    an operator window or with ``VECTOR_LAKE_RECLAIM_FREE_SPACE=1``.  ``busy_timeout`` is widened for
    the duration so it waits for a writer rather than failing halfway.
    """
    conn = conn or get_connection()
    before = free_space_report(conn)
    threshold_bytes = max(0.0, float(min_free_mb)) * 1024 * 1024
    permitted = allow
    if permitted is None:
        permitted = str(os.environ.get("VECTOR_LAKE_RECLAIM_FREE_SPACE", "")).strip().lower() in {
            "1", "true", "yes", "on",
        }
    result = {**before, "vacuumed": False, "freed_bytes": 0, "seconds": 0.0, "reason": ""}
    if before["free_bytes"] < threshold_bytes:
        result["reason"] = (
            f"freelist is {before['free_bytes'] / 1e6:.0f} MB, below the {min_free_mb:.0f} MB threshold"
        )
        return result
    if not permitted:
        result["reason"] = (
            f"{before['free_bytes'] / 1e6:.0f} MB reclaimable; not permitted "
            "(set VECTOR_LAKE_RECLAIM_FREE_SPACE=1, or run it in a maintenance window)"
        )
        return result
    if dry_run:
        result["reason"] = f"dry run: would reclaim {before['free_bytes'] / 1e6:.0f} MB"
        return result

    started = time.monotonic()
    _arm_busy_timeout(conn, 600.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        _arm_busy_timeout(conn, float(BEGIN_LOCK_BUDGET_SECONDS))
    after = free_space_report(conn)
    result.update(
        {
            "vacuumed": True,
            "file_bytes": after["file_bytes"],
            "free_pages": after["free_pages"],
            "free_bytes": after["free_bytes"],
            "freed_bytes": max(0, before["file_bytes"] - after["file_bytes"]),
            "seconds": round(time.monotonic() - started, 1),
            "reason": "vacuumed",
        }
    )
    log.info(
        "Database reclaim: %.0f MB -> %.0f MB (%.0f MB freed in %.1fs).",
        before["file_bytes"] / 1e6, after["file_bytes"] / 1e6, result["freed_bytes"] / 1e6,
        result["seconds"],
    )
    return result


def get_processed_files() -> dict[str, str]:
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash FROM processed_files")
    return {row["filepath"]: row["file_hash"] for row in cur.fetchall()}

def processed_checksum_anomalies(conn=None) -> int:
    """Read-only count; never bless a legacy digest or expose source contents."""
    conn = conn or get_connection()
    return int(conn.execute(
        "SELECT count(*) FROM processed_files WHERE "
        "observed_size > 0 AND lower(file_hash) = 'd41d8cd98f00b204e9800998ecf8427e'"
    ).fetchone()[0])


def mark_file_processed(
    filepath: str,
    file_hash: str,
    *,
    mtime_ns: int | None = None,
    size: int | None = None,
):
    """Record a raw source as processed, with the observation snapshot when supplied.

    The snapshot is what lets the scan skip an unchanged file without hashing it.  It is
    optional so the callers that legitimately cannot stat the file (and the contract tests)
    keep working; ``COALESCE`` then keeps any snapshot already on the row instead of clearing
    it on a write that carries none.
    """
    from datetime import datetime, timezone
    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute("""
            INSERT INTO processed_files (
                filepath, file_hash, processed_at, observed_mtime_ns, observed_size
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                file_hash = excluded.file_hash,
                processed_at = excluded.processed_at,
                observed_mtime_ns = COALESCE(excluded.observed_mtime_ns, processed_files.observed_mtime_ns),
                observed_size = COALESCE(excluded.observed_size, processed_files.observed_size)
        """, (filepath, file_hash, now_str, mtime_ns, size))

#: Statuses from which a job's work is genuinely finished.
#:
#: They are the ones ``enqueue_job`` may deduplicate against.  A job that ended ``failed`` or
#: ``cancelled`` did *not* do the work, and deduplicating against it is a dead end: the raw
#: source is still un-ingested, but every later scan gets the old terminal job id back instead of
#: a dispatchable one.  Measured on 2026-09-19, this is what left 11 sources (8 of them transient
#: "Canonical version conflict" failures) permanently un-ingestable.
INGEST_JOB_TERMINAL_SUCCESS = ("finalized", "completed")


#: Statuses that mean the job gave up rather than finished or being in progress.
#:
#: This is what ``replace_terminal`` may supersede.  The first version asked "is it *not*
#: finished", which is also true of ``queued``, ``awaiting_subagent`` and
#: ``subagent_processing`` -- so a scan superseded jobs that were still in flight.  Measured on
#: the live corpus 2026-09-19 10:50: six sources ended up with **three to four concurrent jobs
#: each**, i.e. three to four model calls for one file.
INGEST_JOB_TERMINAL_FAILURE = ("failed", "cancelled")


def _job_is_terminal_unsuccessful(conn, job_id: str) -> bool:
    row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return False
    return str(row["status"]) in INGEST_JOB_TERMINAL_FAILURE


def enqueue_job(
    task_type: str,
    payload: dict,
    idempotency_key: str | None = None,
    replace_terminal: bool = False,
    supersede_finished: bool = False,
) -> str:
    """Enqueue a job, deduplicating against an *unfinished* job with the same key.

    ``replace_terminal`` (used by the ingest scan) lets a new job supersede one that ended
    ``failed`` or ``cancelled``: the source still needs ingesting, and returning the dead job's id
    would leave it that way forever.  The superseded row is marked so the history stays readable
    instead of being deleted.

    ``supersede_finished`` extends that to a ``finalized``/``completed`` job, which is only safe
    for a caller that has *established the source is not published* -- the ingest scan does, via
    ``raw_is_published``, before it gets here.  It exists because a source can hold a finished job
    while having neither a page nor a ``processed_files`` row (measured:
    ``DHWB-20260913.md``, finalized 2026-09-17, no page anywhere in the wiki).  Without this the
    scan's enqueue was a silent no-op against the finished job's key: the file was reported
    "enqueued", marked in-flight, never dispatched, and released again by the next sweep -- a
    12-minute loop that produced nothing.  The collision is logged, because it means the page and
    the ledger disagree and an operator should know.
    """
    if supersede_finished and not replace_terminal:
        # A finished job is also "not unfinished"; keep the two switches from being used as if
        # they were independent when they are nested.
        replace_terminal = True
    import uuid
    from datetime import datetime, timezone
    init_db()
    conn = get_connection()
    job_id = uuid.uuid4().hex
    now_str = datetime.now(timezone.utc).isoformat()
    key = idempotency_key or _job_idempotency_key(task_type, payload)
    with transaction():
        if key:
            existing = conn.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing:
                existing_id = str(existing["job_id"])
                status_row = conn.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (existing_id,)
                ).fetchone()
                status = str(status_row["status"]) if status_row else ""
                unfinished_mismatch = replace_terminal and _job_is_terminal_unsuccessful(conn, existing_id)
                finished_collision = supersede_finished and status in INGEST_JOB_TERMINAL_SUCCESS
                if not (unfinished_mismatch or finished_collision):
                    return existing_id
                if finished_collision:
                    logging.getLogger("vector-lake-db").warning(
                        "Superseding %s job %s for %s: the source is not published but the job "
                        "table says the work finished, so the page and the ledger disagree.",
                        status, existing_id, (payload or {}).get("filepath"),
                    )
                # Keep the old row as history, but take its key so the new row can hold it.
                #
                # ``unfinished_mismatch`` also has to *stop the old row being dispatched*:
                # clearing the key alone left it ``failed`` with its retries unspent, and
                # ``claim_pending_jobs`` claims ``failed`` while ``retries < MAX_INGEST_ATTEMPTS``
                # -- so the superseded job and its replacement were both handed out, i.e. two
                # model calls for one source.  That is the defect this supersede path was added to
                # remove (independent review, 2026-09-19).  A *finished* row is already
                # unclaimable, so its status is left as the record of the work.
                conn.execute(
                    "UPDATE jobs SET idempotency_key = NULL, "
                    "status = CASE WHEN ? THEN 'superseded' ELSE status END, "
                    "error_msg = COALESCE(NULLIF(error_msg, ''), '') || "
                    "' [superseded by a fresh dispatch]', updated_at = ? WHERE job_id = ?",
                    (1 if unfinished_mismatch else 0, now_str, existing_id),
                )
        conn.execute("""
            INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, task_type, json.dumps(payload, ensure_ascii=False), "queued", now_str, now_str, now_str, key))
    return job_id


#: Attempts a single ingest job may consume before it becomes terminal.
#:
#: One owner for the number: ``claim_pending_jobs`` refuses to dispatch a job at the
#: cap, and ``tool_ingest.record_ingest_failure`` reports against the same value.  The
#: two disagreed before -- the runner's failure branches never consumed the budget at
#: all, so a deterministically rejecting task was re-claimed every hour forever.
MAX_INGEST_ATTEMPTS = 3


def claim_pending_jobs(limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE task_type != 'ingest_recompile' AND "
            "((status IN ('queued', 'failed') AND retries < ? AND COALESCE(available_at, created_at, '') <= ?) "
            "OR (status = 'dispatched' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (MAX_INGEST_ATTEMPTS, now, now, max(1, int(limit))),
        ).fetchall()
        job_ids = [row["job_id"] for row in rows]
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(
            f"UPDATE jobs SET status = 'dispatched', lease_until = ?, updated_at = ? "
            f"WHERE job_id IN ({placeholders})",
            [lease_until, now, *job_ids],
        )
        claimed = conn.execute(
            f"SELECT * FROM jobs WHERE job_id IN ({placeholders}) ORDER BY created_at ASC",
            job_ids,
        ).fetchall()
        return [dict(row) for row in claimed]

def get_pending_jobs(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cur = conn.execute("""
        SELECT * FROM jobs 
        WHERE status = 'queued' OR (status = 'failed' AND retries < 3)
        ORDER BY created_at ASC LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]

def get_jobs_by_status(statuses: list[str], limit: int = 20) -> list[dict]:
    if not statuses:
        return []
    init_db()
    conn = get_connection()
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
        [*statuses, max(1, int(limit))],
    ).fetchall()
    return [dict(row) for row in rows]

class DispatchClaimLost(RuntimeError):
    """A short dispatch claim was superseded or expired; never mutate its new owner."""


def _dispatch_claim_current(conn, claim: dict, now: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE job_id = ? AND status = 'dispatched' "
        "AND lease_until = ? AND lease_until > ? AND updated_at = ? AND payload = ?",
        (claim["job_id"], claim["lease_until"], now, claim["updated_at"], claim["payload"]),
    ).fetchone() is not None


def fail_dispatch_claim(claim: dict, reason: str) -> bool:
    """Spend an attempt only if this exact dispatch claim still owns the job."""
    conn = get_connection()
    with transaction():
        if not _dispatch_claim_current(conn, claim, datetime.now(timezone.utc).isoformat()):
            return False
        update_job_status(claim["job_id"], "failed", reason)
        return True


def mark_job_awaiting_subagent(job_id: str, task_packet_path: str, *,
                              dispatch_payload: dict | None = None,
                              dispatch_claim: dict | None = None):
    conn = get_connection()
    with transaction():
        # BEGIN IMMEDIATE may wait past expiry. Validate using time after acquisition.
        now_str = datetime.now(timezone.utc).isoformat()
        if dispatch_payload is not None:
            if not dispatch_claim or str(dispatch_claim["job_id"]) != str(job_id):
                raise ValueError("Refreshed handoff requires its exact dispatch claim")
            if not _dispatch_claim_current(conn, dispatch_claim, now_str):
                raise DispatchClaimLost("Dispatch claim expired or was superseded before handoff")
            original = json.loads(dispatch_claim["payload"])
            for key in ("filepath", "hash", "canonical_name"):
                if dispatch_payload.get(key) != original.get(key):
                    raise ValueError(f"Dispatch refresh cannot change queued {key}")
            # Payload binding and handoff publish in one transaction. No retry/identity reset.
            conn.execute("UPDATE jobs SET payload = ? WHERE job_id = ?",
                         (json.dumps(dispatch_payload, ensure_ascii=False), job_id))
        conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent', task_packet_path = ?, "
            "error_msg = ?, updated_at = ?, lease_until = NULL, lease_owner = NULL, "
            "lease_token = NULL WHERE job_id = ?",
            (task_packet_path, f"Subagent task packet: {task_packet_path}", now_str, job_id),
        )


def claim_subagent_jobs(
    limit: int = 10,
    lease_seconds: int = 3600,
    lease_owner: str | None = None,
) -> list[dict]:
    """Lease ingest tasks to one host subagent consumer at a time."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    owner = str(
        lease_owner
        or os.environ.get("VECTOR_LAKE_SUBAGENT_RUN_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE task_type IN ('ingest', 'ingest_recompile') AND "
            "(status = 'awaiting_subagent' OR "
            "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (now, max(1, int(limit))),
        ).fetchall()
        job_ids = [str(row["job_id"]) for row in rows]
        if not job_ids:
            return []
        claimed = []
        for job_id in job_ids:
            lease_token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                "UPDATE jobs SET status = 'subagent_processing', lease_until = ?, "
                "lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1, updated_at = ? "
                "WHERE job_id = ? AND (status = 'awaiting_subagent' OR "
                "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?))",
                (lease_until, owner, lease_token, now, job_id, now),
            )
            if cursor.rowcount != 1:
                continue
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is not None:
                claimed.append(dict(row))
        return claimed


def ingest_tasks_ready() -> bool:
    """Cheap read-only availability probe; never claim, refresh or bypass a lease."""
    now = datetime.now(timezone.utc).isoformat()
    return get_connection().execute(
        "SELECT 1 FROM jobs WHERE task_type IN ('ingest', 'ingest_recompile') AND "
        "(status = 'awaiting_subagent' OR (status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?)) LIMIT 1",
        (now,),
    ).fetchone() is not None


def validate_ingest_job_finalization(job_id: str, processed_data: dict, *, check_source_version: bool = True) -> dict:
    """Bind finalization to the exact leased job payload."""
    init_db()
    row = get_connection().execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (str(job_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown ingest job: {job_id}")
    if row["task_type"] not in {"ingest", "ingest_recompile"}:
        raise ValueError(f"Job {job_id} is not an ingest job")
    if row["status"] != "subagent_processing":
        raise ValueError(f"Job {job_id} cannot be finalized from status {row['status']}")
    lease_token = str(processed_data.get("lease_token") or "")
    expected_token = str(row["lease_token"] or "")
    if not lease_token or lease_token != expected_token:
        raise ValueError(f"Job {job_id} lease_token does not match the current lease")
    lease_owner = str(processed_data.get("lease_owner") or "")
    expected_owner = str(row["lease_owner"] or "")
    if not lease_owner or lease_owner != expected_owner:
        raise ValueError(f"Job {job_id} lease_owner does not match the current lease")
    try:
        lease_generation = int(processed_data.get("lease_generation"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Job {job_id} requires a valid lease_generation") from exc
    if lease_generation != int(row["lease_generation"] or 0):
        raise ValueError(f"Job {job_id} lease_generation does not match the current lease")
    lease_until = datetime.fromisoformat(str(row["lease_until"] or "").replace("Z", "+00:00"))
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until <= datetime.now(timezone.utc):
        raise ValueError(f"Job {job_id} lease has expired")
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Job {job_id} has an invalid payload") from exc
    from vector_lake.controlled_recompile import validate_marker
    validate_marker(payload, task_type=row["task_type"], check_source_version=check_source_version)
    if processed_data.get("controlled_recompile") != payload.get("controlled_recompile"):
        raise ValueError(f"Job {job_id} recompile marker does not match its queued payload")
    for key in ("integration_candidates", "source_projection_hash", "ingest_contract_version", "source_read_path"):
        supplied, expected = processed_data.get(key), payload.get(key)
        if type(supplied) is not type(expected) or supplied != expected:
            raise ValueError(f"Job {job_id} {key} differs from its durable dispatch")
    for key in ("filepath", "hash"):
        if str(processed_data.get(key) or "") != str(payload.get(key) or ""):
            raise ValueError(f"Job {job_id} {key} does not match its queued payload")
    expected_name = str(payload.get("canonical_name") or "")
    supplied_name = str(processed_data.get("canonical_name") or "")
    if expected_name and supplied_name != expected_name:
        raise ValueError(f"Job {job_id} canonical_name does not match its queued payload")
    expected_source_hash = str(payload.get("source_hash") or "")
    supplied_source_hash = str(processed_data.get("source_hash") or "")
    if supplied_source_hash != expected_source_hash:
        raise ValueError(f"Job {job_id} source_hash does not match its queued payload")
    result = dict(row)
    result["parsed_payload"] = payload
    return result


def finalize_ingest_job(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    result_data: dict | None = None,
):
    """Mark a validated subagent job complete inside the caller's transaction."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    result_json = json.dumps(result_data or {}, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "UPDATE jobs SET status = 'finalized', completed_at = ?, updated_at = ?, "
        "lease_until = NULL, lease_owner = NULL, lease_token = NULL, error_msg = '', result_json = ? "
        "WHERE job_id = ? AND status = 'subagent_processing' "
        "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? AND lease_until > ?",
        (
            now,
            now,
            result_json,
            str(job_id),
            str(lease_owner),
            str(lease_token),
            int(lease_generation),
            now,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Ingest job {job_id} is no longer finalizable")

def expire_stale_subagent_jobs(max_age_seconds: int = 86400) -> int:
    init_db()
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(max_age_seconds)))).isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', retries = retries + 1, "
            "error_msg = 'Subagent task packet expired before finalization', updated_at = ?, "
            "available_at = ?, lease_until = NULL, lease_owner = NULL, lease_token = NULL "
            "WHERE status = 'awaiting_subagent' AND updated_at < ?",
            (now_str, now_str, cutoff),
        )
        return int(cursor.rowcount or 0)

def record_abandoned_source(filepath: str, file_hash: str, reason: str) -> int:
    """Stop dispatching this exact source content after it kept failing deterministically.

    Keyed on ``(filepath, file_hash)`` on purpose: editing the source changes its hash, so a
    corrected file is dispatchable again without any operator action, while the exact bytes that
    keep failing stop consuming model calls.  Only deterministic failures land here -- a
    transient one is released for retry instead (see ``release_job_for_retry``).

    Returns the number of terminal jobs recorded for this content.
    """
    from datetime import datetime, timezone

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        conn.execute(
            "INSERT INTO ingest_abandoned_sources (filepath, file_hash, reason, terminal_jobs, abandoned_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(filepath, file_hash) DO UPDATE SET "
            "reason = excluded.reason, terminal_jobs = terminal_jobs + 1, abandoned_at = excluded.abandoned_at",
            (str(filepath), str(file_hash), str(reason)[:500], now),
        )
        row = conn.execute(
            "SELECT terminal_jobs FROM ingest_abandoned_sources WHERE filepath = ? AND file_hash = ?",
            (str(filepath), str(file_hash)),
        ).fetchone()
    return int(row["terminal_jobs"]) if row else 1


def abandoned_source_keys() -> set[tuple[str, str]]:
    """``{(filepath, file_hash), ...}`` of sources that must not be re-dispatched."""
    try:
        return {
            (str(row[0]), str(row[1]))
            for row in get_connection().execute(
                "SELECT filepath, file_hash FROM ingest_abandoned_sources"
            )
        }
    except sqlite3.OperationalError:
        # A database predating the table simply has no abandoned sources.
        return set()


def list_abandoned_sources() -> list[dict]:
    """Abandoned sources, newest first, for an operator to inspect or clear."""
    try:
        return [
            dict(row)
            for row in get_connection().execute(
                "SELECT filepath, file_hash, reason, terminal_jobs, abandoned_at "
                "FROM ingest_abandoned_sources ORDER BY abandoned_at DESC"
            )
        ]
    except sqlite3.OperationalError:
        return []


def list_terminal_failed_jobs() -> list[dict]:
    """Jobs that reached the attempt budget, newest first, with the source each names."""
    try:
        rows = get_connection().execute(
            "SELECT job_id, task_type, retries, updated_at, error_msg, payload FROM jobs "
            "WHERE status = 'failed' AND retries >= ? ORDER BY updated_at DESC",
            (MAX_INGEST_ATTEMPTS,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    jobs = []
    for row in rows:
        try:
            filepath = json.loads(row["payload"] or "{}").get("filepath", "")
        except (TypeError, ValueError):
            filepath = ""
        jobs.append(
            {
                "job_id": str(row["job_id"]),
                "task_type": str(row["task_type"]),
                "retries": int(row["retries"] or 0),
                "updated_at": str(row["updated_at"]),
                "error_msg": str(row["error_msg"] or ""),
                "filepath": str(filepath),
            }
        )
    return jobs


def close_terminal_failed_jobs(source_ingested_only: bool = True) -> dict:
    """Mark terminal-failed jobs ``superseded`` once their source is genuinely ingested.

    These are the jobs that spent their attempt budget, and they are what makes ``doctor`` report
    ``[FAIL] Ingest Jobs: terminal_failed:N``.  A job whose source has since been ingested (a
    ledger row) is finished business recorded twice: the *work* is done and the failure is
    history.  Leaving them ``failed`` keeps a FAIL on the health surface that no longer describes
    anything actionable -- measured on 2026-09-19: 11 jobs, every one of whose sources was in the
    ledger.

    ``source_ingested_only`` refuses to close a job whose source has no ledger row, because that
    one really is unfinished work and the abandonment mechanism is what decides its fate.
    """
    conn = get_connection()
    ledger = {str(row[0]) for row in conn.execute("SELECT filepath FROM processed_files")}
    closed: list[str] = []
    kept: list[str] = []
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        for job in list_terminal_failed_jobs():
            filepath = job["filepath"]
            if job["task_type"] == "ingest_recompile":
                from vector_lake.controlled_recompile import current_version_proven, fingerprint
                payload_row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone()
                try:
                    payload = json.loads(payload_row["payload"])
                    marker = payload.get("controlled_recompile") or {}
                    current = fingerprint(filepath)
                    proven = (current["sha256"] == marker.get("sha256")
                              and current_version_proven(filepath, current["sha256"], current["md5"]))
                except (OSError, TypeError, ValueError):
                    proven = False  # Explicitly keep the failed job, not a no-data conclusion.
                if not proven:
                    kept.append(job["job_id"])
                    continue
            if source_ingested_only and (not filepath or filepath not in ledger):
                from pathlib import Path
                if filepath and not Path(filepath).exists():
                    pass  # The source was renamed or removed on disk; close stale job
                else:
                    kept.append(job["job_id"])
                    continue
            conn.execute(
                "UPDATE jobs SET status = 'superseded', updated_at = ?, "
                "error_msg = error_msg || ' [closed: the source was ingested by a later job]' "
                "WHERE job_id = ? AND status = 'failed'",
                (now, job["job_id"]),
            )
            closed.append(job["job_id"])
    return {"closed": closed, "kept": kept}


def clear_abandoned_sources(filepath: str | None = None) -> int:
    """Allow abandoned source(s) to be dispatched again; returns how many were cleared."""
    conn = get_connection()
    with transaction():
        if filepath:
            cursor = conn.execute(
                "DELETE FROM ingest_abandoned_sources WHERE filepath = ?", (str(filepath),)
            )
        else:
            cursor = conn.execute("DELETE FROM ingest_abandoned_sources")
    return int(cursor.rowcount or 0)


def _ingest_failure_claim_current(row: dict, claim: dict | None, now: str) -> bool:
    """Failure/release must hold the same ownership fence as successful finalization."""
    if row["task_type"] not in {"ingest", "ingest_recompile"}:
        return False
    try:
        retries = int(row["retries"] or 0)
        current_generation = int(row["lease_generation"] or 0)
    except (TypeError, ValueError):
        return False
    if claim is None:
        # Preserve unleased administrative callers, never infer a live owner's token.
        return (
            row["status"] in {"queued", "failed"} and retries < MAX_INGEST_ATTEMPTS
            and not any(row[key] for key in ("lease_owner", "lease_token", "lease_until"))
        )
    if str(claim.get("job_id") or "") != str(row["job_id"]):
        return False
    if row["status"] == "dispatched":
        return all(row[key] == claim.get(key) for key in ("lease_until", "updated_at", "payload")) and bool(
            row["lease_until"] and row["lease_until"] > now
        )
    if row["status"] != "subagent_processing":
        return False
    if not all(row[key] and row[key] == claim.get(key) for key in ("lease_owner", "lease_token")):
        return False
    generation_token = claim.get("lease_generation")
    if isinstance(generation_token, bool) or not isinstance(generation_token, (int, str)):
        return False
    try:
        generation = int(generation_token)
        expiry = datetime.fromisoformat(str(row["lease_until"] or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return generation == current_generation and expiry > datetime.fromisoformat(now)


def release_job_for_retry(job_id: str, reason: str, *, claim: dict | None = None, delay_seconds: float = 5.0) -> bool:
    """Make a job dispatchable again *without* spending its terminal attempt budget.

    The budget exists to stop a deterministic rejection being retried forever (a schema
    violation, a name that no gate will ever accept).  A transient rejection is a different
    thing: ``Canonical version conflict`` means the page moved between the model reading it and
    ``finalize_ingest`` writing it, and the next attempt reads the new version.  Counting those
    against the cap turned 8 transient conflicts on the live corpus into permanently
    un-ingested sources -- terminal, and (before ``enqueue_job`` grew ``replace_terminal``)
    impossible to re-enqueue because the dead job held the idempotency key.

    ``retries`` is left alone, so ``claim_pending_jobs`` keeps dispatching while the budget
    lasts; only the lease is released.
    """
    from datetime import datetime, timezone

    conn = get_connection()
    import math

    if not math.isfinite(delay_seconds) or not 1 <= delay_seconds <= 3600:
        raise ValueError("Retry delay must be finite and between 1 and 3600 seconds")
    with transaction():
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        immediate = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or not _ingest_failure_claim_current(dict(row), claim, now_str):
            return False
        conn.execute(
            "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = ?, available_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL WHERE job_id = ?",
            (f"{str(reason)[:460]} [transient, retry does not spend the attempt budget]", now_str, immediate, job_id),
        )
        return True


def update_job_status(job_id: str, status: str, error_msg: str = "", *, claim: dict | None = None):
    from datetime import datetime, timezone
    conn = get_connection()
    with transaction():
        now_str = datetime.now(timezone.utc).isoformat()
        if claim is not None:
            current = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if current is None or not _ingest_failure_claim_current(dict(current), claim, now_str):
                return False
        if status == "failed":
            row = conn.execute("SELECT retries FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            next_retry = int(row["retries"] or 0) + 1 if row else 1
            available_at = (datetime.now(timezone.utc) + timedelta(seconds=5 * (2 ** max(0, next_retry - 1)))).isoformat()
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, retries = retries + 1,
                    available_at = ?, lease_until = NULL, lease_owner = NULL, lease_token = NULL
                WHERE job_id = ?
            """, (status, error_msg, now_str, available_at, job_id))
        else:
            completed_at = now_str if status in {"finalized", "completed"} else None
            conn.execute("""
                UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, lease_until = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    completed_at = COALESCE(?, completed_at)
                WHERE job_id = ?
            """, (status, error_msg, now_str, completed_at, job_id))
        return True



def backup_database(destination_path: str | Path | None = None):
    """Atomically publish an integrity-checked SQLite snapshot and content receipt."""
    from vector_lake.backup_retention import backup_lock, seal_backup

    if not get_db_path().exists():
        init_db()
    if destination_path is None:
        backup_dir = get_meta_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"vector_lake_{time.time_ns()}_{uuid.uuid4().hex[:8]}.db.bak"
    else:
        backup_path = Path(destination_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.partial")
    with backup_lock(backup_path.parent):
        try:
            destination = sqlite3.connect(str(temporary))
            try:
                get_connection().backup(destination)
                integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
            finally:
                destination.close()
            temporary.replace(backup_path)
            seal_backup(backup_path, [backup_path], sqlite_integrity=integrity, database_member=backup_path.name)
        finally:
            temporary.unlink(missing_ok=True)
    return str(backup_path)
