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
import sqlite3
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
        self.now = store._memory_now()

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
    """One retained winner: its exact sort key and the payload it stands for.

    The payload is fetched after every window has been filled.  Holding the decoded document here
    meant the spool had to keep the whole domain's JSON objects alive; holding ``memory_id`` keeps
    the spool to masks and sort keys, which is what makes the single pass affordable.
    """

    key: tuple
    memory_id: str
    rowid: int
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


#: Per-row spool bound.  The spool holds, for one scoped document, its query-term field masks and
#: the four values its sort key needs -- never its payload.  That is what lets the scope be read
#: once: the corpus DF and the per-document relevance both come out of the same pass, so the second
#: read of the whole domain (measured 1.2-2.4 s of the 2.4-5.4 s a scoped search used to cost on the
#: live Medical_IT scope) no longer happens.  These bound the spool, not the payload stream; the
#: scan budgets above still bound the work.
MAX_SPOOL_ROWS = 200000
MAX_SPOOL_BYTES = 96 * 1024 * 1024
_SPOOL_ENTRY_BYTES = 320

# Field bits for one (document, term) pair.  The first three match ``memory_gram_index``'s posting
# mask so the two scorers describe the same thing; bit 3 is the type hit that only the payload
# carries.  The weights below are ``_memory_relevance``'s, in the order it adds them.
_MASK_KEY, _MASK_TEXT, _MASK_PAGE, _MASK_TYPE = 1, 2, 4, 8
_MASK_WEIGHTS = ((_MASK_KEY, 4), (_MASK_TEXT, 3), (_MASK_PAGE, 1), (_MASK_TYPE, 1))


def _field_masks(item: dict, terms) -> tuple[int, ...]:
    """Which fields of one memory carry each query term.

    ``_memory_relevance`` asks the same four questions per term; asking them once here is the whole
    point of the single pass.  ``memory_type`` is included because the scorer includes it (unlike
    the corpus DF, which cannot -- see ``governance_store._memory_term_frequencies``).

    The concatenated haystack decides *presence* first: a term absent from all three text fields is
    absent, and asking the three fields separately costs three checks per absent term.  Measured on
    the live Medical_IT scope (30 613 rows, 17 terms), masking every field unconditionally cost 2.6 s
    for the scan; presence first costs one check per term.  A term that lives only in the type name
    is still found, because the (short) type string is always tested.
    """
    key = str(item.get('memory_key', '')).lower()
    text = str(item.get('text', '')).lower()
    page = str(item.get('source_page', '')).lower()
    memory_type = str(item.get('memory_type', 'fact')).lower()
    haystack = key + '\n' + text + '\n' + page
    masks = []
    for term in terms:
        mask = _MASK_TYPE if term in memory_type else 0
        if term in haystack:
            if term in key:
                mask |= _MASK_KEY
            if term in text:
                mask |= _MASK_TEXT
            if term in page:
                mask |= _MASK_PAGE
        masks.append(mask)
    return tuple(masks)


def _scan_scope(conn, domain, cluster, terms, budget, fact_entity=None):
    """One read of the scope: spool rows (masks + sort key) and return their count."""
    from vector_lake.tool_search import _memory_matches_entity

    budget.check()
    budget.passes += 1
    spool: list[tuple] = []
    spool_bytes = 0
    total = 0
    payloads = store._iter_scoped_memory_payloads(
        conn, domain, cluster, max_payload_bytes=MAX_RETAINED_BYTES, with_rowid=True,
    )
    try:
        for rowid, raw, size in payloads:
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
            item = store._current_memory(item, budget.now)
            total += 1
            spool.append((
                _field_masks(item, terms) if terms else (),
                str(item.get('memory_type', 'fact')).lower(),
                str(item.get('validity_state', 'active')).lower(),
                store._memory_score_value(item.get('memory_score')),
                store._dt_rank(item.get('updated_at')),
                str(item.get('memory_id', '')),
                rowid,
                size,
                _memory_matches_entity(item, fact_entity),
            ))
            spool_bytes += _SPOOL_ENTRY_BYTES
            if total > MAX_SPOOL_ROWS or spool_bytes > MAX_SPOOL_BYTES:
                raise runtime.RetrievalBudgetExceeded(
                    'Scoped memory spool budget exhausted; no truncated ranking returned')
    finally:
        payloads.close()
    return spool, total


def _scope_document_frequencies(spool, terms) -> dict[str, int]:
    """Corpus DF over the scope, from the masks the scan already computed.

    The type bit is excluded: ``_memory_term_frequencies`` counts key/text/page only, because the
    postings carry no type bit and the two paths have to agree on the factor.
    """
    frequencies = dict.fromkeys(terms, 0)
    if not terms:
        return frequencies
    visible = _MASK_KEY | _MASK_TEXT | _MASK_PAGE
    for row in spool:
        masks = row[0]
        for index, mask in enumerate(masks):
            if mask & visible:
                frequencies[terms[index]] += 1
    return frequencies


def _relevance(masks, weights, terms) -> float:
    """``_memory_relevance`` over precomputed masks, in the same addition order.

    Field order and float order are part of the contract: the two scorers must produce the same
    number, and the rounding to four decimals that follows hides nothing about ordering.
    """
    if not terms:
        return 0.0
    score = 0.0
    for index, mask in enumerate(masks):
        if not mask:
            continue
        weight = weights[terms[index]]
        for bit, factor in _MASK_WEIGHTS:
            if mask & bit:
                score += weight * factor
    return score


def _fetch_payloads(rowids):
    """Decoded payloads by row id, read positionally.

    The row id, not ``data_json``'s ``memory_id``: caller content can repeat an id across rows (and
    one test does exactly that), while the row id names the payload that was scored.  Positional
    indices so this works on a caller's connection whether or not it installed ``sqlite3.Row``.
    """
    by_id: dict[int, dict] = {}
    ordered = sorted({int(rowid) for rowid in rowids})
    if not ordered:
        return by_id
    conn = store.get_connection()
    for start in range(0, len(ordered), 400):
        chunk = ordered[start : start + 400]
        placeholders = ','.join('?' for _ in chunk)
        for row in conn.execute(
            f'SELECT rowid, data_json FROM operational_memory WHERE rowid IN ({placeholders})',
            chunk,
        ):
            by_id[int(row[0])] = json.loads(row[1])
    return by_id


def _fetch_payloads_by_memory_id(memory_ids):
    """Decoded payloads by ``memory_id``, read positionally (PK lookups, so no row-factory need)."""
    by_id: dict[str, dict] = {}
    ordered = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids))
    if not ordered:
        return by_id
    conn = store.get_connection()
    for start in range(0, len(ordered), 400):
        chunk = ordered[start : start + 400]
        placeholders = ','.join('?' for _ in chunk)
        for row in conn.execute(
            f'SELECT memory_id, data_json FROM operational_memory WHERE memory_id IN ({placeholders})',
            chunk,
        ):
            by_id[str(row[0])] = json.loads(row[1])
    return by_id


#: How many scoped candidates the index path is willing to hydrate before it gives up being a
#: candidate generator and defers to the streaming scan.  Above it the per-row mask scan is the
#: cheaper shape; below it, one indexed read of a few hundred rows beats streaming 88 MB of JSON.
MAX_INDEX_CANDIDATES = 4000


def _index_rows(conn, docs):
    """Index columns the exact key needs, for the given document numbers."""
    rows: dict[int, tuple] = {}
    ordered = sorted(int(doc) for doc in docs)
    for start in range(0, len(ordered), 400):
        chunk = ordered[start : start + 400]
        placeholders = ','.join('?' for _ in chunk)
        for row in conn.execute(
            'SELECT g.doc, i.memory_id, i.memory_type, memory_current_state(m.data_json), '
            'memory_current_score(m.data_json), i.updated_rank '
            'FROM operational_memory_gram_docs g JOIN operational_memory_index i '
            'ON i.memory_id = g.memory_id JOIN operational_memory m ON m.memory_id = i.memory_id '
            f'WHERE g.doc IN ({placeholders})',
            chunk,
        ):
            rows[int(row[0])] = (str(row[1]), str(row[2]).lower(), str(row[3]).lower(),
                                 store._memory_score_value(row[4]), float(row[5] or 0.0))
    return rows


def _type_weight_factors(conn, terms, df, total) -> dict[str, float]:
    """Per-type relevance constant, the way ``_gram_memory_candidates`` folds it.

    ``memory_type`` contributes one point per term that is a substring of the type name; that is a
    per-type constant rather than a per-document lookup, so it is computed once per request.
    """
    from vector_lake.memory_gram_index import idf_weight

    factors = {term: idf_weight(df.get(term, 0), total) for term in terms if term}
    weights: dict[str, float] = {}
    for row in conn.execute('SELECT DISTINCT memory_type FROM operational_memory_index'):
        name = str(row[0]).lower()
        weight = sum(factor for term, factor in factors.items() if term in name)
        if weight:
            weights[name] = weight
    return weights


#: Below this many pages, a cluster-only scope is cheaper to answer from the payload scan than from
#: the postings.  Measured on the live corpus: a 127-page cluster (1 201 memory rows) answered in 37 ms
#: by the scan and 107 ms by the indexed path, because for a narrow cluster the accelerator's cluster
#: index resolves the scope almost for free while the indexed path still pays corpus-wide document
#: frequency and relevance.  Domain scopes are not gated: their resolution is the expensive part
#: (285-470 ms) and the indexed path removes it.  A cost heuristic only -- both paths are exact.
SCAN_CHEAPER_PAGE_LIMIT = 400


def _cluster_only_scope_is_small(conn, cluster) -> bool:
    """Whether a cluster-only scope is narrow enough that the payload scan is the cheaper answer."""
    row = conn.execute(
        "SELECT COUNT(*) FROM page_index_nodes WHERE lower(topic_cluster) = ?",
        (str(cluster).lower(),),
    ).fetchone()
    return int(row[0] or 0) < SCAN_CHEAPER_PAGE_LIMIT


def _index_views(terms, windows, domain, cluster, budget):
    """Scoped ranking from the n-gram postings, or ``None`` when the payload scan must answer.

    The scope is enumerated from index columns, document frequency and relevance come from the
    postings restricted to it, and the winners are hydrated by id and re-scored by
    ``store.score_memory_items`` -- the same exact scorer the payload scan uses.  The index is
    therefore a candidate generator, not a second scoring rule: whatever it proposes, the oracle
    decides the order of.
    """
    from vector_lake import memory_gram_index as gram
    from vector_lake import scoped_retrieval_index as indexes

    conn = store.get_connection()
    store._query_memory_functions(conn, budget.now)
    # Cheapest gates first: a stale base costs one COUNT to refuse, while enumerating the scope
    # costs a few hundred milliseconds.  Asking the gram index first keeps that state at "no work"
    # instead of "30 613 rows read, then fall back".
    if not gram.ensure_memory_gram_index() or not gram.gram_index_usable():
        return None
    names = store._scope_domain_aliases(domain) if domain else []
    if not names and cluster and _cluster_only_scope_is_small(conn, cluster):
        return None
    scope_sql = indexes.scoped_memory_scope_sql(conn, names, cluster) if (names or cluster) else None
    if scope_sql is None:
        return None
    sql, params = scope_sql
    scope = {int(row[0]) for row in conn.execute(sql, params)}
    budget.passes = 1
    budget.rows = len(scope)
    if not scope:
        return [[] for _ in windows]
    skip = gram.skip_doc_set(conn)
    total = len(scope)
    with runtime.timed_stage('memory_scope_df'):
        if terms:
            df, relevance = gram.fused_term_frequencies_and_relevance(
                terms, skip_docs=skip, total_docs=total, docs=scope
            )
        else:
            df, relevance = {}, dict.fromkeys(scope, 0.0)
    with runtime.timed_stage('memory_scope_weights'):
        type_weights = _type_weight_factors(conn, terms, df, total)
    with runtime.timed_stage('memory_scope_rank'):
        if not relevance:
            return [[] for _ in windows]
        ranked = sorted(relevance.items(), key=lambda item: (-item[1], item[0]))
        # A relevance-only cut can be filled by hidden/expired rows. Fall back
        # to the bounded exact scan rather than dropping lower eligible rows.
        if len(ranked) > MAX_INDEX_CANDIDATES * 2:
            return None
        index_rows = _index_rows(conn, [doc for doc, _ in ranked])
    runtime.backend('memory_index')
    results = []
    for top_k, include_history, allowed_types in windows:
        view: list[dict] = []
        if top_k > 0 and index_rows:
            candidates = []
            for doc, score in relevance.items():
                row = index_rows.get(doc)
                if row is None:
                    continue
                memory_id, memory_type, validity_state, stored_score, updated_rank = row
                if allowed_types and memory_type not in allowed_types:
                    continue
                if not include_history and validity_state in store.HIDDEN_MEMORY_STATES:
                    continue
                total_score = round(score + type_weights.get(memory_type, 0.0), 4)
                candidates.append((-total_score, -stored_score, -updated_rank, memory_id))
            candidates.sort()
            window = max(top_k * store.MEMORY_SEARCH_RESULT_WINDOW, top_k + 16)
            wanted = [item[3] for item in candidates[:window]]
            payloads = _fetch_payloads_by_memory_id(wanted)
            ordered = [payloads[memory_id] for memory_id in wanted if memory_id in payloads]
            view = store.score_memory_items(
                ordered, terms, top_k,
                allowed_types=allowed_types, include_history=include_history,
                df=df, total=total, now=budget.now,
            )
            budget.retain(sum(len(json.dumps(item)) for item in ordered), len(ordered))
        results.append(view)
    budget.rows = total
    runtime.generation('memory_scope', {
        'scanned_rows': total, 'scanned_bytes': 0, 'passes': 1,
        'retained_rows': budget.retained_rows, 'retained_bytes': budget.retained_bytes,
        'source': 'index',
    })
    return results


def search_views(terms, windows, *, domain, cluster, now=None,
                 _fact_entity=None, _display_groups=False):
    """Return independent (top_k, include_history, allowed_types) result windows."""
    if not domain and not cluster and _fact_entity is None and not _display_groups:
        raise ValueError('Scoped memory search requires a scope')
    if any(top_k < 0 for top_k, _, _ in windows):
        raise ValueError('Memory window sizes must not be negative')
    if len(terms) > MAX_TERMS:
        raise runtime.RetrievalBudgetExceeded('Scoped memory query term budget exhausted')
    if not any(top_k for top_k, _, _ in windows):
        return [[] for _ in windows]
    result_limits = [top_k for top_k, _, _ in windows]
    if _display_groups:
        windows = [(max(k * store.MEMORY_SEARCH_RESULT_WINDOW, k + 16) if k else 0, history, types)
                   for k, history, types in windows]
    budget = _Budget()
    budget.now = now or store._memory_now()
    heaps = [[] for _ in windows]
    conn = store.get_connection()
    try:
        if (domain or cluster) and _fact_entity is None and not _display_groups:
            # The fast path answers both axes from index columns; the scan stays the reference
            # implementation and the fallback whenever the accelerators or the gram base are missing.
            try:
                with runtime.timed_stage('memory_scope_total'), _read_snapshot(conn):
                    views = _index_views(terms, windows, domain, cluster, budget)
            except (sqlite3.Error, OSError, ValueError, KeyError):
                # Any index-side inconsistency defers to the scan: it is the exact reference
                # implementation, and a candidate generator that cannot answer must not answer.
                views = None
            if views is not None:
                return views
        runtime.backend('memory_stream')
        with runtime.timed_stage('memory_scope_total'), _read_snapshot(conn):
            with runtime.timed_stage('memory_scope_df'):
                spool, total = _scan_scope(conn, domain, cluster, terms, budget, _fact_entity)
                df = _scope_document_frequencies(spool, terms)
            if total:
                with runtime.timed_stage('memory_scope_weights'):
                    from vector_lake.memory_gram_index import idf_weight
                    # Same pinned corpus and term order as the full-sort scorer; never cache
                    # across requests, scopes, caller transactions, or changing corpus DF.
                    idf_weights = {term: idf_weight(df.get(term, 0), total) if df else 1.0
                                   for term in terms}
                with runtime.timed_stage('memory_scope_rank'):
                    for ordinal, (masks, memory_type, validity_state, stored_score,
                                  dt_rank, memory_id, rowid, size, aligned) in enumerate(spool):
                        if not aligned:
                            continue
                        hidden = validity_state in store.HIDDEN_MEMORY_STATES
                        eligible = [
                            (include_history or not hidden)
                            and (not allowed_types or memory_type in allowed_types)
                            and top_k > 0
                            for top_k, include_history, allowed_types in windows
                        ]
                        if not any(eligible):
                            continue
                        relevance = _relevance(masks, idf_weights, terms)
                        if relevance <= 0 and terms:
                            continue
                        score = round(relevance, 4)
                        # Ordinal preserves stable full-sort ties, including duplicate JSON IDs.
                        if not math.isfinite(stored_score):
                            raise ValueError('Non-finite scoped memory score has no supported ranking')
                        key = (-score, -stored_score, -dt_rank, memory_id, ordinal)
                        for heap, (top_k, _, _), allowed in zip(heaps, windows, eligible):
                            if not allowed:
                                continue
                            if len(heap) < top_k:
                                budget.retain(size, 1)
                                heapq.heappush(heap, _Entry(key, memory_id, rowid, size))
                            elif key < heap[0].key:
                                budget.retain(size - heap[0].size, 0)
                                heapq.heapreplace(heap, _Entry(key, memory_id, rowid, size))
            results = []
            wanted = [entry.rowid for heap in heaps for entry in heap]
            payloads = _fetch_payloads(wanted)
            for heap, result_limit in zip(heaps, result_limits):
                view = []
                for entry in sorted(heap, key=lambda item: item.key):
                    budget.check()
                    payload = payloads.get(entry.rowid)
                    if payload is None:
                        # The row went away between the scan and the fetch; a missing winner is a
                        # shorter window, never a fabricated one.
                        continue
                    item = copy.deepcopy(store._current_memory(payload, budget.now))
                    item['retrieval_score'] = -entry.key[0]
                    view.append(item)
                results.append(store._aggregate_memory_display(view, result_limit)
                               if _display_groups else view)
            budget.check()
            return results
    finally:
        budget.observe()
