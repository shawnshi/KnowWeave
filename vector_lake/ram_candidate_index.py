"""Bounded, optional RAM candidate scan. SQLite remains the scorer and source.

Snapshots are immutable and only built inside a caller-owned query-only read
transaction. A connection's data_version is never compared with another's:
first/new bindings validate the entire bounded float projection. Exact key/byte
comparison can reuse an existing matrix; changed content is loaded/fingerprinted. Borrowed write/savepoint
transactions must not call acquire(). No database schema or global BLAS settings
are changed; missing NumPy is a capability fallback, not a swallowed failure.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import importlib
import logging
import os
import sqlite3
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)
MAX_MATRIX_BYTES = 128 * 1024 * 1024
MAX_ROWS = 100_000
MAX_KEY_HEAP_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 16_384
MAX_ACTIVE_VIEWS = 4
MAX_CONNECTION_BINDINGS = 8
MAX_RECHECK_BUFFER_BYTES = 4 * 1024 * 1024

_LOCK = threading.RLock()
_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_VIEWS)
_BINDINGS: OrderedDict[sqlite3.Connection, tuple[tuple, bytes]] = OrderedDict()
_CURRENT: _Snapshot | None = None
_PID = os.getpid()


@dataclass(frozen=True)
class _Snapshot:
    database: tuple
    metadata: tuple
    digest: bytes
    keys: tuple[str, ...]
    matrix: Any
    norms: Any


@dataclass
class Lease:
    snapshot: _Snapshot
    numpy: Any
    _slots: Any
    _released: bool = False

    def close(self) -> None:
        if not self._released:
            self._released = True
            self._slots.release()

    def shortlist(self, query_blob: bytes, k: int) -> list[str]:
        """Full scan; deterministic (squared L2, page_key) prefixes.

        Dot-product cancellation is rechecked by direct L2 in bounded blocks.
        These distances are selection-only: callers must use the old SQL rerank.
        """
        if self._released:
            raise RuntimeError("RAM candidate lease has already been released")
        np = self.numpy
        matrix = self.snapshot.matrix
        dimension = matrix.shape[1]
        query = np.frombuffer(query_blob, dtype=np.float32)
        if len(query) != dimension or not np.isfinite(query).all():
            raise ValueError("RAM query must be finite and match the stored dimension")
        if not len(matrix):
            return []
        query_norm = float(np.einsum("i,i->", query, query, dtype=np.float64))
        dots = np.einsum("ij,j->i", matrix, query, optimize=False)
        scores = self.snapshot.norms + query_norm - 2.0 * dots.astype(np.float64)
        if not np.isfinite(scores).all():
            raise ValueError("RAM candidate arithmetic overflowed")
        # Conservative dot reduction bound; correct near-zero cancellation instead
        # of clipping it and losing identity/near-duplicate nearest neighbours.
        eps = float(np.finfo(np.float32).eps)
        gamma = (dimension + 2) * eps / (1.0 - (dimension + 2) * eps)
        radius = 8.0 * gamma * np.sqrt(self.snapshot.norms * query_norm)
        uncertain = np.flatnonzero(scores <= radius)
        if len(uncertain):
            rows = min(256, max(1, MAX_RECHECK_BUFFER_BYTES // (dimension * 4)))
            buffer = np.empty((rows, dimension), dtype=np.float32)
            for start in range(0, len(uncertain), rows):
                selected = uncertain[start : start + rows]
                view = buffer[: len(selected)]
                np.take(matrix, selected, axis=0, out=view)
                np.subtract(view, query, out=view)
                scores[selected] = np.einsum("ij,ij->i", view, view, dtype=np.float64)
        scores = np.maximum(scores, 0.0)
        take = min(len(matrix), max(1, int(k)))
        boundary = np.partition(scores, take - 1)[take - 1]
        lower = np.flatnonzero(scores < boundary)
        ties = np.flatnonzero(scores == boundary)
        # Rows/keys were loaded in page_key order; do not argpartition arbitrary ties.
        chosen = np.concatenate((lower, ties[: take - len(lower)]))
        ordered = chosen[np.lexsort((chosen, scores[chosen]))]
        return [self.snapshot.keys[int(index)] for index in ordered]


def _numpy():
    try:
        return importlib.import_module("numpy")
    except ModuleNotFoundError as exc:
        if exc.name != "numpy":
            raise  # A broken installation/dependency is not optional absence.
        return None


def clear() -> None:
    """Release the cache/bindings; outstanding immutable leases remain valid."""
    global _CURRENT
    with _LOCK:
        _CURRENT = None
        _BINDINGS.clear()


def _database(conn: sqlite3.Connection) -> tuple:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return ("file", row[2]) if row[2] else ("memory", conn)
    raise ValueError("SQLite connection has no main database")


def _metadata(state: dict) -> tuple:
    return (
        state.get("model"),
        int(state["dimension"]),
        int(state["row_count"]),
        state.get("written_at"),
        int(state.get("shortlist") or 256),
    )


def _load(conn, database, metadata, np) -> _Snapshot | None:
    count, dimension = metadata[2], metadata[1]
    matrix = np.empty((count, dimension), dtype=np.float32)
    digest = hashlib.sha256()
    keys: list[str] = []
    heap = 0
    cursor = conn.execute(
        "SELECT page_key, embedding FROM vec_emb_float ORDER BY page_key"
    )
    for position, row in enumerate(cursor):
        if position >= count:
            raise ValueError("RAM projection membership changed inside read snapshot")
        key, blob = row[0], row[1]
        if (
            not isinstance(key, str)
            or not isinstance(blob, bytes)
            or len(blob) != dimension * 4
        ):
            raise ValueError("Malformed float projection row for RAM candidates")
        heap += sys.getsizeof(key)
        if heap > MAX_KEY_HEAP_BYTES:
            return None  # Explicit resource policy; not a fabricated no-data result.
        encoded = key.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        digest.update(blob)
        matrix[position] = np.frombuffer(blob, dtype=np.float32)
        keys.append(key)
    if len(keys) != count:
        raise ValueError("RAM projection count does not match snapshot metadata")
    if not np.isfinite(matrix).all():
        raise ValueError("Non-finite float projection cannot populate RAM candidates")
    norms = np.einsum("ij,ij->i", matrix, matrix, dtype=np.float64)
    matrix.flags.writeable = False
    norms.flags.writeable = False
    return _Snapshot(database, metadata, digest.digest(), tuple(keys), matrix, norms)


def _matches_current(conn: sqlite3.Connection, snapshot: _Snapshot) -> bool:
    """Verify every source byte without allocating another projection matrix."""
    count, dimension = snapshot.metadata[2], snapshot.metadata[1]
    seen = 0
    heap = 0
    cursor = conn.execute(
        "SELECT page_key, embedding FROM vec_emb_float ORDER BY page_key"
    )
    try:
        for position, row in enumerate(cursor):
            if position >= count:
                return False
            key, blob = row[0], row[1]
            if (
                not isinstance(key, str)
                or not isinstance(blob, bytes)
                or len(blob) != dimension * 4
            ):
                raise ValueError("Malformed float projection row for RAM candidates")
            heap += sys.getsizeof(key)
            if heap > MAX_KEY_HEAP_BYTES:
                return False
            if (
                key != snapshot.keys[position]
                or blob != snapshot.matrix[position].tobytes()
            ):
                return False
            seen += 1
        return seen == count
    finally:
        cursor.close()


def acquire(conn: sqlite3.Connection, state: dict) -> Lease | None:
    """Caller guarantees a pinned, owned read-only transaction around this lease.

    One current matrix, at most four in-flight views, eight small bindings and
    one serial bounded rebuild. Fallback on optional absence/capacity only.
    """
    global _CURRENT, _PID
    np = _numpy()
    if np is None:
        log.debug("RAM vector candidates unavailable: NumPy is not installed")
        return None
    metadata = _metadata(state)
    dimension, count = metadata[1], metadata[2]
    if (
        not 0 < dimension <= MAX_DIMENSION
        or not 0 <= count <= MAX_ROWS
        or count * dimension * 4 > MAX_MATRIX_BYTES
    ):
        log.debug(
            "RAM vector candidates unavailable: projection exceeds resource policy"
        )
        return None
    slots = _SLOTS
    if not slots.acquire(blocking=False):
        log.debug("RAM vector candidates unavailable: active view budget is full")
        return None
    lease = None
    try:
        with _LOCK:
            if _PID != os.getpid():
                clear()
                _PID = os.getpid()
            database = _database(conn)
            stamp = (
                conn.execute("PRAGMA data_version").fetchone()[0],
                conn.total_changes,
                conn.execute("PRAGMA schema_version").fetchone()[0],
                metadata,
            )
            previous = _BINDINGS.get(conn)
            if (
                previous is not None
                and previous[0] == stamp
                and _CURRENT is not None
                and _CURRENT.database == database
                and _CURRENT.metadata == metadata
                and previous[1] == _CURRENT.digest
            ):
                _BINDINGS.move_to_end(conn)
                lease = Lease(_CURRENT, np, slots)
                return lease
            if (
                _CURRENT is not None
                and _CURRENT.database == database
                and _CURRENT.metadata == metadata
                and _matches_current(conn, _CURRENT)
            ):
                rebuilt = _CURRENT
            else:
                rebuilt = _load(conn, database, metadata, np)
            if rebuilt is None:
                log.debug("RAM vector candidates unavailable: key heap budget exceeded")
                return None
            if (
                _CURRENT is None
                or _CURRENT.database != database
                or _CURRENT.metadata != metadata
                or _CURRENT.digest != rebuilt.digest
            ):
                _CURRENT = rebuilt
            _BINDINGS[conn] = (stamp, _CURRENT.digest)
            _BINDINGS.move_to_end(conn)
            while len(_BINDINGS) > MAX_CONNECTION_BINDINGS:
                _BINDINGS.popitem(last=False)
            lease = Lease(_CURRENT, np, slots)
            return lease
    finally:
        if lease is None:
            slots.release()


def _after_fork() -> None:
    global _LOCK, _SLOTS, _BINDINGS, _CURRENT, _PID
    _LOCK = threading.RLock()
    _SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_VIEWS)
    _BINDINGS = OrderedDict()
    _CURRENT = None
    _PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
