"""Exact scoped memory TopK with a pinned, two-pass streaming read.

Corpus DF includes every row in the requested scope, as in the full-sort oracle.
Only result heaps are retained; scan-work and retained-payload budgets are separate.
Payload byte accounting is not a physical Python/process memory ceiling.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import copy
import heapq
import json
import math
import time

from vector_lake import governance_store as store
from vector_lake import retrieval_runtime as runtime

MAX_RETAINED_ROWS = 10000
MAX_RETAINED_BYTES = 16 * 1024 * 1024
# Two passes of the measured ~74k-row/104-MiB synthetic shape fit this envelope.
# These bound work, not materialization; the public cooperative deadline still applies.
MAX_SCANNED_ROWS = 200000
MAX_SCANNED_BYTES = 256 * 1024 * 1024
MAX_TERMS = 8192


class _Budget:
    def __init__(self):
        self.deadline = time.monotonic() + runtime.remaining_seconds()
        self.rows = self.bytes = self.retained_rows = self.retained_bytes = 0
        self.passes = 0

    def check(self):
        runtime.checkpoint()
        if time.monotonic() >= self.deadline:
            raise runtime.RetrievalBudgetExceeded('Scoped memory streaming deadline exhausted')

    def scan(self, size):
        self.check()
        self.rows += 1
        self.bytes += size
        if (size > MAX_RETAINED_BYTES or self.rows > MAX_SCANNED_ROWS
                or self.bytes > MAX_SCANNED_BYTES):
            raise runtime.RetrievalBudgetExceeded('Scoped memory scan budget exhausted; no truncated ranking returned')

    def retain(self, byte_delta, row_delta):
        if (self.retained_rows + row_delta > MAX_RETAINED_ROWS
                or self.retained_bytes + byte_delta > MAX_RETAINED_BYTES):
            raise runtime.RetrievalBudgetExceeded('Scoped memory result budget exhausted; no truncated ranking returned')
        self.retained_rows += row_delta
        self.retained_bytes += byte_delta

    def observe(self):
        runtime.generation('memory_scope', {
            'scanned_rows': self.rows, 'scanned_bytes': self.bytes, 'passes': self.passes,
            'retained_rows': self.retained_rows, 'retained_bytes': self.retained_bytes,
        })


@dataclass(slots=True)
class _Entry:
    key: tuple
    memory: dict
    size: int

    def __lt__(self, other):
        # heap[0] is the worst retained result; public heapq only needs __lt__.
        return self.key > other.key


@contextmanager
def _read_snapshot(conn):
    """Own only our read transaction/flag; never commit or roll back a caller's work."""
    runtime.track_connection(conn)
    owned = not conn.in_transaction
    query_only = conn.execute('PRAGMA query_only').fetchone()[0]
    changed_flag = not query_only
    if changed_flag:
        conn.execute('PRAGMA query_only=ON')
    try:
        if owned:
            conn.execute('BEGIN')
        try:
            yield
        finally:
            if owned:
                conn.rollback()
    finally:
        if changed_flag:
            conn.execute('PRAGMA query_only=OFF')


def _rows(conn, domain, cluster, budget):
    budget.check()
    budget.passes += 1
    payloads = store._iter_scoped_memory_payloads(
        conn, domain, cluster, max_payload_bytes=MAX_RETAINED_BYTES,
    )
    try:
        for raw, size in payloads:
            if not isinstance(size, int) or size < 0:
                raise ValueError('Invalid scoped operational memory payload length')
            budget.scan(size)
            if not isinstance(raw, (str, bytes)):
                raise ValueError('Invalid scoped operational memory payload')
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError('Invalid scoped operational memory payload') from exc
            if not isinstance(item, dict):
                raise ValueError('Scoped operational memory payload must be an object')
            yield item, size
    finally:
        payloads.close()


def search_views(terms, windows, *, domain, cluster):
    """Return independent (top_k, include_history, allowed_types) result windows."""
    if not domain and not cluster:
        raise ValueError('Scoped memory search requires a scope')
    if any(top_k < 0 for top_k, _, _ in windows):
        raise ValueError('Memory window sizes must not be negative')
    if len(terms) > MAX_TERMS:
        raise runtime.RetrievalBudgetExceeded('Scoped memory query term budget exhausted')
    if not any(top_k for top_k, _, _ in windows):
        return [[] for _ in windows]
    budget = _Budget()
    heaps = [[] for _ in windows]
    conn = store.get_connection()
    runtime.backend('memory_stream')
    try:
        with _read_snapshot(conn):
            total = 0

            def counted():
                nonlocal total
                rows = _rows(conn, domain, cluster, budget)
                try:
                    for memory, _ in rows:
                        total += 1
                        yield memory
                finally:
                    rows.close()

            corpus = counted()
            try:
                if terms:
                    df = store._memory_term_frequencies(corpus, terms)
                else:
                    # The legacy DF helper intentionally doesn't consume an empty query.
                    for _ in corpus:
                        pass
                    df = {}
            finally:
                corpus.close()
            if total:
                rows = _rows(conn, domain, cluster, budget)
                try:
                    for ordinal, (memory, size) in enumerate(rows):
                        memory_type = str(memory.get('memory_type', 'fact')).lower()
                        hidden = str(memory.get('validity_state', 'active')).lower() in store.HIDDEN_MEMORY_STATES
                        eligible = [
                            (include_history or not hidden)
                            and (not allowed_types or memory_type in allowed_types)
                            and top_k > 0
                            for top_k, include_history, allowed_types in windows
                        ]
                        if not any(eligible):
                            continue
                        relevance = store._memory_relevance(memory, terms, df, total)
                        if relevance <= 0 and terms:
                            continue
                        score = round(relevance, 4)
                        # Ordinal preserves stable full-sort ties, including duplicate JSON IDs.
                        stored_score = store._memory_score_value(memory.get('memory_score'))
                        if not math.isfinite(stored_score):
                            raise ValueError('Non-finite scoped memory score has no supported ranking')
                        key = (-score, -stored_score,
                               -store._dt_rank(memory.get('updated_at')),
                               str(memory.get('memory_id', '')), ordinal)
                        for heap, (top_k, _, _), allowed in zip(heaps, windows, eligible):
                            if not allowed:
                                continue
                            if len(heap) < top_k:
                                budget.retain(size, 1)
                                heapq.heappush(heap, _Entry(key, memory, size))
                            elif key < heap[0].key:
                                budget.retain(size - heap[0].size, 0)
                                heapq.heapreplace(heap, _Entry(key, memory, size))
                finally:
                    rows.close()
            results = []
            for heap in heaps:
                view = []
                for entry in sorted(heap, key=lambda item: item.key):
                    budget.check()
                    item = copy.deepcopy(entry.memory)
                    item['retrieval_score'] = -entry.key[0]
                    view.append(item)
                results.append(view)
            budget.check()
            return results
    finally:
        budget.observe()
