"""What search answered, recorded so a ranking change can be measured against a baseline.

The A1/A2 work changes which candidates reach ``top_k``, and the lake keeps no record that could
show whether it improved: there is no ``log.md`` and no query table.  This writes one JSON line per
retrieval -- enough to compare two runs that must agree, and to see which of FTS, the vector
projection or graph expansion actually produced each page.

The query text is **not** stored.  It is caller content, it is not this module's to retain, and a
digest answers the two questions the ledger exists for: "is this the same query as last time" and
"is the same query answering differently than it did".  ``query_chars`` is kept because a digest
alone cannot distinguish a short query from a long one when reading the file by eye.

Bounded and fail-open.  The ledger is a measurement: it must never make a search fail, and it must
not grow without limit.  One generation is rotated aside at ``MAX_BYTES``, so the ceiling is 2x.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

from vector_lake.wiki_utils import get_runtime_tmp_dir

log = logging.getLogger("vector-lake-search-ledger")

LEDGER_FILENAME = "search_ledger.jsonl"
#: Rotate one generation aside once the live file passes this.  2 MiB holds roughly 4 000 entries
#: at the observed line size, and the ceiling is twice that.
MAX_BYTES = 2 * 1024 * 1024
_WRITE_LOCK = threading.RLock()
_SKIPPED_WRITES = 0


def observation_status() -> dict:
    return {'skipped_writes': _SKIPPED_WRITES}


def ledger_path() -> Path:
    return get_runtime_tmp_dir() / LEDGER_FILENAME


def enabled() -> bool:
    """On by default; ``VECTOR_LAKE_SEARCH_LEDGER=0`` turns it off."""
    return os.environ.get("VECTOR_LAKE_SEARCH_LEDGER", "1").strip() != "0"


def query_digest(query: str) -> str:
    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:16]


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        # A rotation failure is not allowed to cost the entry we are about to write; the file
        # simply grows past the ceiling until the next attempt.
        pass


def record(
    query: str,
    *,
    mode: str,
    top_k: int,
    returned: list[dict],
    notes: list[str] | None = None,
    elapsed_ms: float | None = None,
    error: str | None = None,
    fusion: str | None = None,
    fts_backend: str | None = None,
    selectors: dict | None = None,
    index_generation: str | None = None,
    configuration: dict | None = None,
    timings: dict | None = None,
    status: str | None = None,
    entry_point: str | None = None,
    request_total_ms: float | None = None,
    process: dict | None = None,
    cache_events: dict | None = None,
) -> None:
    """Append one entry.  Never raises: a measurement must not break the thing it measures.

    ``fusion`` and ``fts_backend`` are recorded because without them a past answer cannot be
    explained: the ledger kept the query digest, the timing and the returned rows, so "this query
    answers differently now" was visible while *which retrieval configuration produced it* was not.
    The eval harness recorded both in its run config; production did not -- measured 2026-09-25:
    903 entries, ``fusion`` null in all of them.
    """
    global _SKIPPED_WRITES
    if not enabled():
        return
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": str(mode),
            "top_k": int(top_k),
            "q_hash": query_digest(query),
            "q_chars": len(str(query)),
            "returned": returned,
        }
        if status is not None:
            if status not in {'complete', 'partial', 'failed'}:
                raise ValueError('Invalid retrieval completion status')
            entry['status'] = status
        if selectors is not None:
            entry["selectors"] = {
                key: selectors[key] for key in ('domain', 'cluster', 'include_history', 'filter_expr_active')
                if key in selectors
            }
        if index_generation is not None:
            import hashlib
            entry["index_generation"] = hashlib.sha256(index_generation.encode()).hexdigest()[:16]
        if configuration is not None:
            entry["configuration"] = {
                key: configuration[key] for key in ('candidate_depth', 'candidate_pool', 'allow_external_embedding',
                                                      'profile', 'use_chunks', 'event_year_requested', 'supplemental_query_count',
                                                      'actual_vector_backends', 'actual_backends', 'source_bytes')
                if key in configuration
            }
        if timings is not None:
            entry["timings_ms"] = {str(key): round(float(value), 2) for key, value in timings.items()}
        if fusion:
            entry["fusion"] = str(fusion)
        if fts_backend:
            entry["fts_backend"] = str(fts_backend)
        if notes:
            entry["notes"] = [str(note) for note in notes]
        if elapsed_ms is not None:
            entry["ms"] = round(float(elapsed_ms), 1)
        if entry_point is not None:
            # Which surface produced this row: ``search`` and ``context`` used to be recorded as
            # ``mode="page"`` alike, so a context assembly could not be told from a page search.
            entry["entry_point"] = str(entry_point)
        if request_total_ms is not None:
            # The whole request scope, beside the stage's own ``ms``.  They differ exactly when the
            # recorded stage ran nested inside a larger one.
            entry["request_ms"] = round(float(request_total_ms), 1)
        if process is not None:
            entry["process"] = {key: process[key] for key in ('pid', 'token', 'uptime_ms') if key in process}
        if cache_events:
            # Structural work this request caused (a graph rebuild, a catalog parse, a cache hit).
            entry["cache_events"] = {str(k): int(v) for k, v in cache_events.items()}
        if error:
            entry["error"] = str(error)

        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _WRITE_LOCK.acquire(timeout=0.25):
            raise TimeoutError('Ledger process writer budget exhausted')
        try:
            with FileLock(str(path) + '.lock', timeout=0.1):
                _rotate(path)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            _WRITE_LOCK.release()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        _SKIPPED_WRITES += 1
        log.debug("search ledger write skipped: %s", type(exc).__name__)


def entries(limit: int = 0) -> list[dict]:
    """The most recent entries, oldest first.  Read-only; used by ``doctor`` and the harness."""
    path = ledger_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def summary() -> dict:
    """What the ledger says about production retrieval, for the doctor line and for readiness.

    Entries are operator traffic only: the evaluation harness turns the ledger off for the duration
    of a replay, and the rows written before that guard existed were moved aside to
    ``search_ledger.eval-legacy.jsonl``.  Measured before that split: 893 of 901
    entries were evaluation traffic, so every reading taken from this file was describing the
    evaluation sets rather than the lake in use.  That harness has since been retired from the
    repository; the split is why this file no longer carries its traffic.

    The query text is not stored (see the module docstring), so this is a count of what happened --
    how often an answer was empty, how long it took, and which route produced the pages -- not a
    list of what was asked.
    """
    rows = entries()
    answered = [row for row in rows if row.get("returned")]
    empty = [row for row in rows if not row.get("returned")]
    latencies = sorted(
        float(row["ms"]) for row in rows if isinstance(row.get("ms"), (int, float))
    )
    origins: dict[str, int] = {}
    for row in rows:
        for entry in row.get("returned") or []:
            origin = str(entry.get("origin") or "unknown")
            origins[origin] = origins.get(origin, 0) + 1

    def _percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        return round(values[min(len(values) - 1, int(len(values) * fraction))], 1)

    # One row per surface.  The headline percentiles mix a page search with a context assembly --
    # they differ by an order of magnitude -- so a single p50 describes neither.  ``entry_point`` is
    # absent on rows written before it existed; those are attributed to their ``mode``, and said so.
    by_surface: dict[str, list[float]] = {}
    for row in rows:
        if not isinstance(row.get("ms"), (int, float)):
            continue
        name = str(row.get("entry_point") or row.get("mode") or "unknown")
        by_surface.setdefault(name, []).append(float(row["ms"]))
    surfaces = {
        name: {
            "entries": len(values),
            "latency_ms_p50": _percentile(sorted(values), 0.5),
            "latency_ms_p90": _percentile(sorted(values), 0.9),
            "latency_ms_p95": _percentile(sorted(values), 0.95),
        }
        for name, values in sorted(by_surface.items())
    }

    return {
        "entries": len(rows),
        "distinct_queries": len({row.get("q_hash") for row in rows}),
        "answered": len(answered),
        "empty": len(empty),
        "empty_rate": (len(empty) / len(rows)) if rows else None,
        "errors": sum(1 for row in rows if row.get("error")),
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p90": _percentile(latencies, 0.9),
        "origins": dict(sorted(origins.items(), key=lambda item: -item[1])),
        "by_surface": surfaces,
        "first_at": rows[0].get("at") if rows else None,
        "last_at": rows[-1].get("at") if rows else None,
        "path": str(ledger_path()),
    }
