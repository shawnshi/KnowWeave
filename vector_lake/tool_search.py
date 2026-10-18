import logging
import os
import re
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone

import functools
import ast
import operator
from html import escape
from pathlib import Path

from vector_lake import governance_store, search_ledger
from vector_lake import author_facet, retrieval_runtime as runtime
from vector_lake.domain_dictionary import PUBLIC_QUERY_PROFILE, QUERY_EXPANSION_DICT
from vector_lake.node_vocabulary import strip_prefix
from vector_lake.schema_validator import canonical_domain
from vector_lake.wiki_utils import get_index_path, get_wiki_dir

try:
    import vector_lake_core
    HAVE_CORE = True
except ModuleNotFoundError as exc:
    if exc.name != "vector_lake_core":
        raise
    vector_lake_core = None
    HAVE_CORE = False


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_PPR_INDEX: dict = {"generation": None, "adj": None, "index": None}


def _ppr_index(adj, generation: int):
    """Cache a graph snapshot by its actual adjacency object and projection generation."""
    global _PPR_INDEX
    cached = _PPR_INDEX
    # VL-S03: file fallback graphs do not advance the SQLite generation. A strong reference
    # distinguishes those snapshots and prevents recycled object IDs; one assignment also
    # keeps concurrent readers from pairing an index with another snapshot's metadata.
    if cached["generation"] != generation or cached.get("adj") is not adj or cached["index"] is None:
        cached = {"generation": generation, "adj": adj, "index": vector_lake_core.PprIndex(adj)}
        _PPR_INDEX = cached
    return cached["index"]
log = logging.getLogger("vector-lake-tool-search")

BUDGET_SHARES = {
    # Character shares.  The constant was called ``TOKEN_BUDGET``, but nothing here counts tokens:
    # ``assemble_context`` compares these shares against ``len(str)`` and ``DEFAULT_MAX_CHARS``.  ``operational_memory`` is the nominal memory share and
    # ``memory_burst`` the ceiling the documented alert burst may raise it to; ``wiki_pages`` is
    # realised as the budget's *remainder* rather than as this share, and ``chat_history`` is not
    # used by this assembler at all.  Three of these five keys were read nowhere, while the memory
    # share was a 0.50 literal that contradicted ``operational_memory``.
    "operational_memory": 0.30,
    "memory_burst": 0.50,
    "wiki_pages": 0.45,
    "chat_history": 0.05,
    "index_summary": 0.05,
    "system_prompt": 0.15,
}
DEFAULT_MAX_CHARS = 200000

# A query embedding runs inside an MCP tool call, so it must not outlive the
# caller's patience.  A healthy provider round trip measured 9.6 s cold and ~0.5 s
# warm on this host; a quota-retry loop used to sleep a flat 60 s per attempt.
# The budget is generous against the healthy path and hard against the loop.
QUERY_EMBEDDING_BUDGET_SECONDS = 20.0

#: Minimum cosine similarity a vector hit must reach to enter the fusion.
#:
#: It is a cosine threshold and not an L2 one because of the conversion above: ``vec_embeddings``
#: stores unit vectors (``db_store.upsert_embedding`` normalises before writing, which is why a
#: sampled norm measures exactly 1.000000), so ``1 - d^2/2`` is the cosine.  If that write-time
#: normalisation were ever removed, this gate and the conversion would both go wrong silently --
#: they are one assumption, stated here rather than implied by a bare literal.
VECTOR_MIN_COSINE = 0.5

#: Reciprocal-rank-fusion constant, used when ``VECTOR_LAKE_FUSION=rrf``.  It is the only knob
#: because it is the only insensitive one: moving k shifts ranks by a few positions, whereas the
#: magnitude sum decides its winner by which source's arbitrary scale happens to be larger for
#: that query (``-bm25`` measured 5.1-47.6 across five queries against a hard vector ceiling of 15).
RRF_K = 60

FUSION_MODES = ("sum", "rrf")


def _fusion_mode() -> str:
    """``sum`` (the long-standing blend) or ``rrf`` (``VECTOR_LAKE_FUSION=rrf``)."""
    requested = os.environ.get("VECTOR_LAKE_FUSION", "sum").strip().lower()
    if requested not in FUSION_MODES:
        log.warning("Unknown VECTOR_LAKE_FUSION=%r; using 'sum'.", requested)
        return "sum"
    return requested


def _fts_backend_name() -> str:
    """Actual path for the last lexical call; default before a call is FTS5."""
    return getattr(_LAST_FTS_ERROR, "backend", "fts5")


def _expansion_quota(pool_size: int) -> int | None:
    """Pool slots guaranteed to graph expansion, or ``None`` for the long-standing behaviour.

    Unset means expansion competes for whatever the fusion stage left over.  Both recall paths are
    asked for ``_candidate_depth()`` candidates and the pool is ``_candidate_pool()``, so the
    leftovers are often nothing: measured on the live lake, three of five sampled queries left zero
    slots, which made graph expansion a documented candidate source that could not supply a
    candidate.
    """
    raw = os.environ.get("VECTOR_LAKE_EXPANSION_QUOTA")
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning("Unknown VECTOR_LAKE_EXPANSION_QUOTA=%r; using the default.", raw)
        return None
    if value < 0:
        log.warning("VECTOR_LAKE_EXPANSION_QUOTA must be >= 0; using the default.")
        return None
    return min(value, pool_size)


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning("Ignoring unparsable %s=%r", name, raw)
        return default
    if value < 1:
        log.warning("%s must be >= 1; using %d", name, default)
        return default
    return value


#: How deep each retrieval stage looks, and how large the pool the reranker sees.
#:
#: These are properties of the *pipeline*, not of the window the caller asked for, and the values
#: are exactly what the pipeline computed at the default ``top_k=5`` before this change
#: (``top_k * 5`` candidates, ``max(40, top_k * 3)`` pool, ``int(pool * 0.6)`` source slots in the
#: pool, ``max(1, int(top_k * 0.6))`` source slots in the answer).
#:
#: They used to scale with ``top_k``, which meant ``top_k`` did not only decide how many answers
#: came back -- it decided *which* pages were eligible.  A larger window drew a deeper candidate
#: list, resized the pool, refilled the source caps and therefore changed the pool-local min-max
#: normalisation inside the reranker, so the top-5 of a top-20 request was not the top-5 of a top-5
#: request.  Measured: of 268 queries whose top-5 pool held no relevant page, 36 (13.4%) had their
#: best page inside the top five of the same ranking asked for twenty, while that page was absent
#: from the top-5 result -- the evaluation read "not found" where the pipeline had capped it out,
#: and D2 had already recorded the same heuristic's non-monotonicity at ``top_k=1``.
#:
#: With the depth fixed, the first *k* of a top-k result are the first *k* of any larger one.  The
#: two knobs exist so a deeper pipeline can be measured as its own change, one variable at a time.
CANDIDATE_DEPTH = 25
CANDIDATE_POOL = 40
#: How much a Source page's score is scaled down for the final ordering, once the pool has been
#: chosen.  A *preference*, not a filter: the ranking still contains every eligible page, and the
#: source type only decides where it sits.
#:
#: This replaces an absolute cap on how many Source pages could appear in the answer, which had two
#: costs that were measured rather than imagined.  It shortened the window: with the cap at 3, a
#: top-20 request came back with fewer than twenty results for 104 of 333 queries (31%), because a
#: source-heavy pool had nothing else to fill the remaining slots with.  And any threshold on the
#: answer's *contents* is a function of the window size, which is the one property restored by
#: keeping the pipeline independent of top_k; a multiplicative penalty is scale-free (it means the
#: same thing under sum, whose scores are 5-50, and under rrf, whose scores are ~0.016), so the
#: ordering is a function of the pool alone and top-k stays a pure window.
SOURCE_RANK_PENALTY = 0.6


def _vector_sim_scale() -> float:
    try:
        return float(os.environ.get("VECTOR_LAKE_VECTOR_SIM_SCALE", "15.0"))
    except ValueError:
        return 15.0


def _candidate_depth() -> int:
    return _env_positive_int("VECTOR_LAKE_CANDIDATE_DEPTH", CANDIDATE_DEPTH)


def _candidate_pool() -> int:
    return _env_positive_int("VECTOR_LAKE_CANDIDATE_POOL", CANDIDATE_POOL)


def _source_rank_penalty() -> float:
    """``SOURCE_RANK_PENALTY``, or a value from ``VECTOR_LAKE_SOURCE_RANK_PENALTY``.

    ``1.0`` turns the preference off (pure score order) and ``0.0`` pushes every Source page to the
    bottom while keeping it in the ranking.  Both are useful for measuring the preference rather
    than assuming it.
    """
    raw = os.environ.get("VECTOR_LAKE_SOURCE_RANK_PENALTY")
    if raw is None or not str(raw).strip():
        return SOURCE_RANK_PENALTY
    try:
        value = float(str(raw).strip())
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_SOURCE_RANK_PENALTY=%r", raw)
        return SOURCE_RANK_PENALTY
    if not 0.0 <= value <= 1.0:
        log.warning("VECTOR_LAKE_SOURCE_RANK_PENALTY must be within 0..1; using the default.")
        return SOURCE_RANK_PENALTY
    return value

CJK_REGEX = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

#: A four-digit year, not the substring the intent guess used to match on.
_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "by", "from", "as", "it", "this", "that",
}

def _passes_filters(node: dict, domain: str | None, cluster: str | None, include_history: bool, filter_expr: str | None) -> bool:
    """Single source of truth for the caller-visible result filters.

    Graph expansion and reranking must not bypass these; previously only the
    first scoring pass applied them, so ``domain=`` could return other domains.
    """
    if domain and canonical_domain(node.get("domain")).lower() != canonical_domain(domain).lower():
        return False
    if cluster and str(node.get("topic_cluster") or "").lower() != cluster.lower():
        return False
    if not include_history and str(node.get("status") or "").lower() in ("deprecated", "archived"):
        return False
    if filter_expr:
        try:
            if not _safe_eval(filter_expr, node):
                return False
        except Exception as exc:
            log.warning("Filter expression evaluation failed: %s", runtime.safe_error(exc))
            return False
    return True


# A query embedding costs one provider round trip and the embedding is a pure
# function of the query text, so repeats (the common case for an agent that
# re-asks with a stable phrasing) can be served locally.  Bounded and LRU so a
# long-lived server cannot grow without limit; vectors are stored as float32
# because that is what the vector projection consumes anyway.
QUERY_EMBEDDING_CACHE_SIZE = 256
_QUERY_EMBEDDING_CACHE: "OrderedDict[tuple[str, int, str], array]" = OrderedDict()
_QUERY_EMBEDDING_CACHE_LOCK = threading.Lock()
QUERY_EMBEDDING_MAX_IN_FLIGHT = 256
_QUERY_EMBEDDING_IN_FLIGHT: "dict[tuple[str, int, str], Future]" = {}


def _embedding_cache_key(query: str) -> tuple[str, int, str]:
    from vector_lake.embedding_scheduler import load_embedding_rate_config

    config = load_embedding_rate_config()
    return config.model, config.dimension, query


def _cached_query_embedding(query: str):
    key = _embedding_cache_key(query)
    with _QUERY_EMBEDDING_CACHE_LOCK:
        cached = _QUERY_EMBEDDING_CACHE.get(key)
        if cached is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(key)
        return cached


def _store_query_embedding_by_key(key, values) -> None:
    vector = array("f", values)
    with _QUERY_EMBEDDING_CACHE_LOCK:
        _QUERY_EMBEDDING_CACHE[key] = vector
        _QUERY_EMBEDDING_CACHE.move_to_end(key)
        while len(_QUERY_EMBEDDING_CACHE) > QUERY_EMBEDDING_CACHE_SIZE:
            _QUERY_EMBEDDING_CACHE.popitem(last=False)


def _store_query_embedding(query: str, values) -> None:
    _store_query_embedding_by_key(_embedding_cache_key(query), values)


def _get_query_embedding(query: str) -> tuple[list[float], str | None]:
    """Share concurrent misses by model/dimension/query; each caller keeps its own deadline."""
    if not os.environ.get("GEMINI_API_KEY"):
        return [], "GEMINI_API_KEY is not set"
    deadline = time.monotonic() + QUERY_EMBEDDING_BUDGET_SECONDS
    state = runtime.current()
    if state is not None:
        deadline = min(deadline, state.deadline)
    key = _embedding_cache_key(query)
    with _QUERY_EMBEDDING_CACHE_LOCK:
        cached = _QUERY_EMBEDDING_CACHE.get(key)
        if cached is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(key)
            return list(cached), None
        flight = _QUERY_EMBEDDING_IN_FLIGHT.get(key)
        owner = flight is None
        if owner:
            if len(_QUERY_EMBEDDING_IN_FLIGHT) >= QUERY_EMBEDDING_MAX_IN_FLIGHT:
                return [], "query embedding in-flight capacity reached"
            flight = Future()
            _QUERY_EMBEDDING_IN_FLIGHT[key] = flight

    if not owner:
        try:
            values, error = flight.result(timeout=max(0.0, deadline - time.monotonic()))
            return list(values), error
        except FutureTimeoutError:
            # A follower leaving must not cancel work still owned by another live request.
            return [], "query embedding in-flight wait budget exceeded"

    try:
        try:
            from vector_lake.embedding_scheduler import embed_texts

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = ((), "query embedding budget exhausted before provider call")
            else:
                embeddings = embed_texts([query], budget_seconds=min(remaining, runtime.remaining_seconds()), durable_reservation=False)
                if not embeddings:
                    result = ((), "embedding provider returned no vector")
                else:
                    import math
                    values = tuple(float(value) for value in embeddings[0])
                    norm = math.hypot(*values)
                    if not values or not all(math.isfinite(value) for value in values) or not math.isfinite(norm) or norm == 0:
                        raise ValueError('Query embedding must be finite and non-zero')
                    values = tuple(value / norm for value in values)
                    _store_query_embedding_by_key(key, values)
                    result = (values, None)
        except Exception as exc:
            message = runtime.failed_stage('Query embedding', exc)
            result = ((), message)
        flight.set_result(result)
        return list(result[0]), result[1]
    except BaseException as exc:
        # Host cancellation/interrupt must release followers rather than leave a pending Future.
        if not flight.done():
            flight.set_exception(exc)
        raise
    finally:
        with _QUERY_EMBEDDING_CACHE_LOCK:
            if _QUERY_EMBEDDING_IN_FLIGHT.get(key) is flight:
                del _QUERY_EMBEDDING_IN_FLIGHT[key]


def _vector_projection_error() -> str | None:
    """Do not compare vectors made by different encoders or dimensionalities."""
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    try:
        from vector_lake import db_store
        from vector_lake.embedding_scheduler import load_embedding_rate_config

        state = db_store.embedding_projection_state()
        if not state:
            return ("embedding projection model is unverified; rebuild vectors"
                    if db_store.count_embeddings() else None)
        config = load_embedding_rate_config()
        if state.get("model") != config.model or state.get("dimension") != config.dimension:
            return (f"embedding projection model/dimension mismatch: stored "
                    f"{state.get('model')}/{state.get('dimension')}, "
                    f"query {config.model}/{config.dimension}; rebuild vectors")
    except Exception as exc:
        return runtime.failed_stage('Embedding projection check', exc)
    return None


def _get_vector_search_results(query_vector: list[float], limit: int = 50) -> tuple[dict[str, float], str | None]:
    """Vector hits plus an explicit degradation reason when the query failed."""
    try:
        from vector_lake.db_store import get_connection
        import sqlite_vec
        conn = get_connection()
        query_blob = sqlite_vec.serialize_float32(query_vector)
        from vector_lake import two_stage_index
        with two_stage_index.query_view(conn) as view:
            if view.state is not None:
                scored = two_stage_index.search(conn, query_blob, limit, state=view.state)
            else:
                if view.requested_mode in ("two_stage", "ram"):
                    log.warning(
                        "VECTOR_LAKE_VECTOR_INDEX=%s but the shadow index is unavailable;"
                        " using the legacy brute-force scan.", view.requested_mode,
                    )
                cursor = conn.execute(
                    "SELECT page_key, distance FROM vec_embeddings WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (query_blob, limit)
                )
                scored = [(row["page_key"], row["distance"]) for row in cursor.fetchall()]

        results = {}
        for page_key, dist in scored:
            # ``vec_embeddings.page_key`` holds a **page key** (the writer passes ``node_key``, and
            # ``db_store`` deletes by the same name), not ``entities.entity_id``.  The column name is
            # a trap: a join to ``entities.entity_id`` returns nothing, silently.  The FTS path keys
            # by ``node_key`` too, so the two remain in one namespace and the fusion cannot mix them.
            # distance is L2. convert to approx sim: 1 - (dist^2)/2
            sim = 1.0 - (dist * dist) / 2.0
            if sim > VECTOR_MIN_COSINE:
                results[page_key] = sim
        return results, None
    except Exception as e:
        return {}, runtime.failed_stage('vec_embeddings query', e)

def _metadata_first_enabled() -> bool:
    """Whether a filtered query rejects on metadata before paying for the exact rerank.

    On by default because it is where most of a filtered query's cost was: the rerank stage fell
    from 38.2 ms to 2.6 ms (p50, 36 measured cells) and p95 total from 483.5 ms to 288.9 ms.  It can
    change *which* eligible page fills a slot -- the candidate pool is the binary shortlist rather
    than the exact top-N, so 30 of 36 measured cells kept the legacy order with mean top-5 overlap
    0.97, and the other 6 got better-ranked eligible pages.  The switch exists so an operator who
    wants the old ordering back does not need a code change.
    """
    return os.environ.get("VECTOR_LAKE_METADATA_FIRST", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _get_vector_search_results_filtered(
    query_vector: list[float],
    limit: int,
    catalog,
    domain: str | None,
    cluster: str | None,
    include_history: bool,
    filter_expr: str | None,
    *,
    reuse: dict | None = None,
) -> tuple[dict[str, float] | None, str | None, int]:
    """Vector hits that already satisfy the selectors, plus how many candidates were examined.

    The order of operations is the whole point.  The binary shortlist is cheap (a 2.75 MB Hamming
    index), the metadata verdict is cheap, and the exact L2 rerank is the expensive stage -- so the
    rerank runs only on pages that can actually appear in the answer.  The legacy order reranked
    every candidate and then threw most of them away on metadata, which is why a filtered query cost
    several times an unfiltered one and why the widening loop dominated its latency.

    ``results is None`` means the shadow index is unavailable and the caller must use the plain arm.
    The third value is the **raw shortlist size**, never the number of survivors: a caller that
    widens when "the source ran out" must not read a heavy filter as an exhausted corpus.

    ``reuse`` belongs to one widening loop, never to a process-wide cache.  The first round stays
    demand-sized; on widening it prefetches at most the recall cap and serves subsequent prefixes.
    Metadata verdicts are reused, but exact reranking stays with the original SQL and tie ordering.
    A changed database/query/selector invalidates it; an inconsistent prefix disables prefetching.
    """
    if reuse is not None:
        reuse.pop("diagnostics", None)
    try:
        from vector_lake.db_store import get_connection
        import sqlite_vec
        from vector_lake import two_stage_index

        conn = get_connection()
        query_blob = sqlite_vec.serialize_float32(query_vector)
        hint = reuse.get("status_hint") if reuse is not None else None
        with two_stage_index.query_view(conn, status_hint=hint) as view:
            # Both tokens matter: data_version observes other connections, total_changes our own.
            # Keep the pre-read token, so a write during a round also invalidates the next round.
            if reuse is not None:
                if not view.owned:
                    reuse.clear()  # A caller can roll back a savepoint without changing total_changes.
                stamp = (
                    conn, *view.stamp,
                    query_blob, id(catalog), domain, cluster, include_history, filter_expr,
                )
                if reuse.get("stamp") != stamp:
                    reuse.clear()
                    reuse.update(
                        stamp=stamp, state=view.state, verdicts={},
                        # KNN prefix/tie behaviour was verified on this runtime.  Unknown versions
                        # retain demand-sized scans rather than silently inheriting that assumption.
                        prefix_safe=(view.backend == "ram" or
                                     conn.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9"),
                    )
                reuse["status_hint"] = (view.state_token, view.state) if view.owned else None
                state = reuse["state"]
            else:
                state = view.state
            if state is None:
                return None, None, 0
            # Exactly what the caller asked for, with no floor.  The widening loop *is* the depth
            # mechanism: a floor of the recorded shortlist made its first four steps (25/50/100/200)
            # examine the same 256 candidates four times, and it also returned pages drawn from a deeper
            # pool than the step the loop had asked about.  Both are avoidable, and neither buys recall
            # the loop cannot buy by widening.
            depth = max(1, int(limit))
            if reuse is None or depth > MAX_FILTERED_RECALL_CANDIDATES:
                keys = two_stage_index.shortlist_keys(conn, query_blob, depth, state=state)
                verdicts = {}
            else:
                previous = reuse.get("keys")
                if previous is None or not reuse.get("prefix_safe", True):
                    keys = two_stage_index.shortlist_keys(conn, query_blob, depth, state=state)
                    reuse["keys"] = keys
                elif depth <= len(previous) or reuse.get("pooled") or len(previous) < reuse["depth"]:
                    keys = previous[:depth]
                else:
                    pool = two_stage_index.shortlist_keys(
                        conn, query_blob, MAX_FILTERED_RECALL_CANDIDATES, state=state
                    )
                    if pool[:len(previous)] != previous:
                        # KNN tie behaviour is backend-owned.  Never union incompatible prefixes:
                        # that would admit candidates the original depth did not contain.
                        reuse["prefix_safe"] = False
                        keys = two_stage_index.shortlist_keys(conn, query_blob, depth, state=state)
                        reuse["keys"] = keys
                    else:
                        reuse.update(keys=pool, pooled=True)
                        keys = pool[:depth]
                reuse["depth"] = depth
                if not reuse.get("prefix_safe", True):
                    reuse["verdicts"] = {
                        key: reuse["verdicts"][key] for key in keys if key in reuse["verdicts"]
                    }
                verdicts = reuse["verdicts"]
            unseen = [key for key in keys if key not in verdicts]
            # An expression may read arbitrary fields; ordinary selectors need only three columns.
            if unseen:
                if filter_expr:
                    nodes = catalog.nodes_by_key(unseen)
                else:
                    cheap = getattr(catalog, "filter_fields", None)
                    nodes = cheap(unseen) if cheap is not None else catalog.nodes_by_key(unseen)
                verdicts.update({
                    key: key in nodes and _passes_filters(
                        nodes[key], domain, cluster, include_history, filter_expr
                    )
                    for key in unseen
                })
            eligible = [key for key in keys if verdicts[key]]
            results: dict[str, float] = {}
            for key, dist in two_stage_index.rerank_keys(conn, query_blob, eligible, limit):
                sim = 1.0 - (dist * dist) / 2.0
                if sim > VECTOR_MIN_COSINE:
                    results[key] = sim
            if reuse is not None:
                reuse["diagnostics"] = {
                    "metadata_passed": len(eligible),
                    "similarity_passed": len(results),
                }
            return results, None, len(keys)
    except Exception as e:
        if reuse is not None:
            reuse.clear()
        return None, runtime.failed_stage('Metadata-first vector arm', e), 0


_LAST_FTS_ERROR = threading.local()
_LAST_RERANK_NOTE = threading.local()


RESCUE_TERM_PROBES = 16
#: How many matching documents the rescue is allowed to sort.  FTS5 ranks with bm25, which scores
#: every match, so the disjunction's cost is its match set: 4 764 pages measured 124-535 ms, a few
#: hundred measured 10-20 ms.  Terms are taken rarest first until this budget is spent, so the
#: rescue answers the natural-language shape without ever sorting the corpus.
RESCUE_MATCH_BUDGET = 1500


def _rescue_terms(conn, terms: list[str]) -> list[str]:
    """The rarest qualifiers that fit the rescue's match budget.

    A plain disjunction over every qualifier can match most of the corpus: measured on the live
    lake, "医疗人工智能在医院的落地路径与风险" matched 7 202 of 7 418 pages and the bm25 sort plus
    the domain join cost 240-535 ms to return 25 rows.  Terms are therefore taken rarest first, up
    to half of them, while the sum of their document frequencies stays inside
    ``RESCUE_MATCH_BUDGET`` -- an upper bound on the disjunction's match set, so the sort is
    bounded.  The probe itself costs 7-45 ms for the whole query and at least one term is always
    kept, so a query whose every qualifier is common still gets an answer.
    """
    if len(terms) <= 2 or len(terms) > RESCUE_TERM_PROBES:
        return terms
    counts: dict[str, int] = {}
    try:
        for term in terms:
            row = conn.execute(
                "SELECT COUNT(*) FROM wiki_search_index WHERE wiki_search_index MATCH ?",
                (f'"{term}"',),
            ).fetchone()
            counts[term] = int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return terms
    keep = (len(terms) + 1) // 2
    selected: list[str] = []
    spent = 0
    for term in sorted(terms, key=lambda term: (counts.get(term, 0), terms.index(term))):
        if len(selected) >= keep:
            break
        cost = counts.get(term, 0)
        if selected and spent + cost > RESCUE_MATCH_BUDGET:
            break
        selected.append(term)
        spent += cost
    return [term for term in terms if term in selected]


def _get_fts_search_results(query: str, limit: int = 50) -> list[dict]:
    from vector_lake import tokenizer as _tokenizer

    if not hasattr(_LAST_FTS_ERROR, "backends"):
        _LAST_FTS_ERROR.backends = set()
    _LAST_FTS_ERROR.backend = "none"
    query_tok = _tokenizer.tokenize_joined(query)
        
    # Sanitize query_tok for FTS5 (remove special syntax characters)
    import re
    query_tok = re.sub(r'["*^&|()\-:\[\]{}]', ' ', query_tok)
    # Ensure it's not empty or just spaces
    if not query_tok.strip():
        _LAST_FTS_ERROR.msg = None
        return []
        
    try:
        from vector_lake import db_store, tantivy_index
        conn = db_store.get_connection()

        if tantivy_index.enabled():
            try:
                if not conn.in_transaction:
                    db_store.init_db()
                before = tantivy_index.current_token()
                ranked = tantivy_index.search_if_current(
                    [term.lower() for term in query_tok.split()], limit=limit,
                ) if before is not None else None
                if ranked is not None:
                    keys = [key for key, _score in ranked]
                    ranks = dict(ranked)
                    placeholders = ",".join("?" for _ in keys)
                    metadata = {
                        row["node_key"]: (row["title"], row["summary"])
                        for row in conn.execute(
                            f"SELECT node_key, title, summary FROM wiki_search_index"
                            f" WHERE node_key IN ({placeholders})", keys,
                        )
                    } if keys else {}
                    if tantivy_index.current_token() == before:
                        _LAST_FTS_ERROR.msg = None
                        _LAST_FTS_ERROR.backend = "tantivy"
                        _LAST_FTS_ERROR.backends.add("tantivy")
                        return [
                            {"node_key": key, "title": metadata[key][0],
                             "summary": metadata[key][1], "rank": ranks[key]}
                            for key in keys if key in metadata
                        ]
            except Exception as exc:  # noqa: BLE001 - native fault is not a valid empty result
                log.warning("Tantivy query failed (%s); using FTS5", runtime.safe_error(exc))

        clean_toks = [t.replace('"', '') for t in query_tok.split()]
        terms = [term for term in clean_toks if term.strip()]
        # Every token used to be a mandatory conjunct, function words included.
        # On the live corpus that made "电子病历系统应用水平分级评价六级" an
        # all-token AND with no match at all, while its disjunction matched 4 764
        # pages; 31 of 112 recorded production searches returned nothing for the
        # same reason, because the queries people type carry 在/的/与/对 and
        # the/of/and as if they were evidence.  Recall is tiered now: the
        # stopword-free conjunction first (it is the precise arm and answers
        # whenever it can), then the disjunction ordered by bm25 and appended
        # behind the conjunction's rows.  A single-term query is unchanged.
        recall_terms = [term for term in terms if term.casefold() not in STOP_WORDS] or terms
        if not recall_terms:
            _LAST_FTS_ERROR.msg = None
            return []

        state = runtime.current()
        scope = state.fts_scope if state is not None else None

        def _run(match_expression: str) -> list[dict]:
            clauses, params = ['wiki_search_index MATCH ?'], [match_expression]
            join = ''
            if scope is not None:
                join = ' JOIN page_index_nodes p ON p.node_key=wiki_search_index.node_key'
                if scope['domain']:
                    # Connection-local, read-only UDF: exactly the Python selector's alias,
                    # case and Unicode-whitespace semantics, not a schema installation.
                    conn.create_function('vl_canonical_domain', 1, lambda value: canonical_domain(value).lower(), deterministic=True)
                    clauses.append('vl_canonical_domain(p.domain)=?')
                    params.append(canonical_domain(scope['domain']).lower())
                if scope['cluster']:
                    clauses.append('lower(p.topic_cluster)=?')
                    params.append(scope['cluster'].lower())
                if not scope['include_history']:
                    clauses.append("lower(COALESCE(p.status,'')) NOT IN ('deprecated','archived')")
            cur = conn.execute(
                'SELECT wiki_search_index.node_key, wiki_search_index.title, wiki_search_index.summary, '
                'bm25(wiki_search_index) AS rank FROM wiki_search_index' + join +
                ' WHERE ' + ' AND '.join(clauses) + ' ORDER BY rank LIMIT ?', [*params, limit],
            )
            return [dict(row) for row in cur.fetchall()]

        rows = _run(' '.join(f'"{t}"' for t in recall_terms))
        if not rows and len(recall_terms) > 1:
            # The disjunction is a rescue, not a second opinion.  Qualifiers are still
            # conjunctive -- a page that matches one of them is not an answer to a query that
            # states all of them -- so it runs only when the conjunction matched *nothing at
            # all*, which on the live corpus is exactly the natural-language shape: "电子病历
            # 系统 应用 水平 分级 评价 六级" has no page carrying every qualifier while its
            # disjunction matches 4 764.  It is over the rarer half of the qualifiers, so the
            # sort stays bounded (measured 89-239 ms against 342-535 ms for the full set); bm25
            # ranks a page matching more of them higher.  A conjunction of that rarer half was
            # tried in front of this and removed: for the very shape it was meant to rescue the
            # rare terms do not co-occur, so it only added one more miss before this query ran.
            seen = {row['node_key'] for row in rows}
            for row in _run(' OR '.join(f'"{t}"' for t in _rescue_terms(conn, recall_terms))):
                key = row['node_key']
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) >= limit:
                    break
        _LAST_FTS_ERROR.msg = None
        _LAST_FTS_ERROR.backend = "fts5"
        _LAST_FTS_ERROR.backends.add("fts5")
        return rows[:limit]
    except Exception as e:
        msg = f"FTS5 query failed ({runtime.safe_error(e)})"
        log.warning(msg)
        _LAST_FTS_ERROR.msg = msg
        return []

def _get_fts_recall_results(query: str, limit: int = 50, supplemental_queries=()) -> list[dict]:
    """Original-query hits first; bounded contextual substitutions fill unused slots."""
    primary = _get_fts_search_results(query, limit=limit)
    primary_error = getattr(_LAST_FTS_ERROR, "msg", None)
    if primary_error or len(primary) >= limit:
        return primary

    # VL-S01: conjunction belongs within each query, not between synonyms. Replacing a
    # dictionary term preserves the other qualifiers; unrelated expansions cannot erase originals.
    variants = []
    seen_queries = {query.casefold()}
    for variant in supplemental_queries:
        if variant and variant.casefold() not in seen_queries:
            seen_queries.add(variant.casefold())
            variants.append(variant)
    for term, replacements in QUERY_EXPANSION_DICT.items():
        if term not in query:
            continue
        for replacement in replacements:
            variant = query.replace(term, replacement)
            if variant.casefold() not in seen_queries:
                seen_queries.add(variant.casefold())
                variants.append(variant)

    primary_keys = {row["node_key"] for row in primary}
    supplemental = {}
    errors = []
    for variant in variants[:8]:
        rows = _get_fts_search_results(variant, limit=limit)
        error = getattr(_LAST_FTS_ERROR, "msg", None)
        if error:
            errors.append(error)
            continue
        for row in rows:
            key = row["node_key"]
            if key in primary_keys:
                continue
            previous = supplemental.get(key)
            if previous is None or row["rank"] < previous["rank"]:
                supplemental[key] = row

    # Negative BM25 ranks are shared by FTS5 and Tantivy. Related terms are a weaker signal,
    # not additional votes; max over substitutions avoids rewarding dictionary redundancy.
    secondary = sorted(supplemental.values(), key=lambda row: (row["rank"], row["node_key"]))
    _LAST_FTS_ERROR.msg = "; ".join(dict.fromkeys(errors)) or None
    return primary + [
        {**row, "rank": row["rank"] * 0.5}
        for row in secondary[:max(0, limit - len(primary))]
    ]


@functools.lru_cache(maxsize=128)
def _keyword_intent(query: str) -> str:
    """A keyword guess at the query's shape -- not an intent classifier, and named accordingly.

    Two consumers only: it exempts a decayed page from the relevance penalty when the query looks
    temporal, and it chooses between 12 and 5 graph-expansion candidates.  The year used to be the
    substring ``"202"``, which matched any query containing those digits (an identifier, a version,
    a price) and called it temporal; it is a number pattern now.  ``"公司"``/``"关联"`` stay
    deliberately broad: they are hints, and a wrong ``entity`` guess costs five more expansion
    candidates, not a wrong answer.
    """
    temporal_keywords = {"上周", "去年", "昨天", "最近", "历史", "last week", "yesterday"}
    entity_keywords = {"是谁", "哪里", "谁在", "who is", "where is", "公司", "人员", "关联", "图谱", "网络"}
    lowered = query.lower()
    if _YEAR_PATTERN.search(lowered) or any(kw in lowered for kw in temporal_keywords):
        return "temporal"
    for kw in entity_keywords:
        if kw in lowered:
            return "entity"
    return "general"


@functools.lru_cache(maxsize=128)
def _local_expansions_cached(query: str, tokenizer_identity, expansion_profile, snapshot) -> tuple[str, ...]:
    expanded_terms = set([query])
    for key, expansions in snapshot:
        if key in query:
            expanded_terms.update(expansions)

    tokens = set()
    from vector_lake import tokenizer as _tokenizer

    backend_ready = _tokenizer.backend_name() != "unavailable"

    for term in expanded_terms:
        if backend_ready and CJK_REGEX.search(term):
            for word in _tokenizer.split(term):
                word_lower = word.lower()
                if word_lower not in STOP_WORDS and word_lower.strip():
                    tokens.add(word_lower)
        else:
            for word in term.strip().split():
                word_lower = word.lower()
                if word_lower in STOP_WORDS:
                    continue
                if CJK_REGEX.search(word):
                    chars = list(word)
                    for index in range(len(chars) - 1):
                        tokens.add(chars[index] + chars[index + 1])
                    for char in chars:
                        if CJK_REGEX.match(char):
                            tokens.add(char)
                    tokens.add(word)
                else:
                    tokens.add(word_lower)
    # A tuple, not a list: this value is the cache entry, and a caller that mutated what it got
    # back would corrupt the cache for every later query.
    return tuple(tokens)


class _LocalExpansionCache:
    """Resolve identity before lookup while retaining the existing cache-control API."""

    cache_clear = staticmethod(_local_expansions_cached.cache_clear)
    cache_info = staticmethod(_local_expansions_cached.cache_info)

    def __call__(self, query: str) -> tuple[str, ...]:
        from vector_lake import tokenizer as _tokenizer

        # Compatibility edits must not reuse tokens from different expansions or whitespace.
        snapshot = tuple((key, tuple(values)) for key, values in QUERY_EXPANSION_DICT.items())
        return _local_expansions_cached(query, _tokenizer.cache_identity(), PUBLIC_QUERY_PROFILE, snapshot)


_local_expansions = _LocalExpansionCache()


def _expand_query_locally(query: str) -> list[str]:
    """A caller-owned copy of expansion tokens; never mutate a tokenizer dictionary."""
    return list(_local_expansions(query))


def _format_memory_result(memory: dict, as_xml: bool = False, index: int = 0) -> str:
    state = memory.get("validity_state", "active")
    memory_type = memory.get("memory_type", "fact")
    score = memory.get("retrieval_score", memory.get("memory_score", 0))
    text = " ".join(str(memory.get("text", "")).split())[:420]
    source = memory.get("source_page") or memory.get("source_claim_id") or "operational_memory"
    if as_xml:
        attrs = (
            f"ID='Memory_{index}' Type='{memory_type}' State='{state}' "
            f"Score='{score}' Source='{source}'"
        )
        return f"<Memory_Item {attrs}>{text}</Memory_Item>\n"
    return (
        f"- **{memory_type}:{memory.get('memory_key', memory.get('memory_id'))}** "
        f"(score: {score:.2f}, state: {state})\n"
        f"  {text}\n"
        f"  Source: {source}\n\n"
    )


def format_operational_memory_results(
    query: str,
    top_k: int = 8,
    as_xml: bool = False,
    include_history: bool = False,
    memory_types: list[str] | None = None,
    domain: str | None = None,
    cluster: str | None = None,
) -> str:
    scope = {"domain": domain, "cluster": cluster} if domain or cluster else {}
    memories = governance_store.search_operational_memory(
        query, top_k=top_k, include_history=include_history, memory_types=memory_types, **scope,
    )
    blocks, emitted, used = [], [], 0
    for index, memory in enumerate(memories):
        runtime.checkpoint()
        bounded = {**memory, 'text': str(memory.get('text') or '')[:4000]}
        block = _format_memory_result(bounded, as_xml=as_xml, index=index)
        if used + len(block) > runtime.MAX_CONTEXT_CHARS - 1000:
            state = runtime.current()
            if state is not None:
                state.problem('Memory response character budget exhausted')
            break
        blocks.append(block)
        used += len(block)
        emitted.append({'key': str(memory.get('memory_id') or '')})
    runtime.defer_record({'query': query, 'mode': 'memory', 'top_k': top_k, 'returned': emitted,
                          'notes': [], 'status': 'partial' if len(emitted) < len(memories) else 'complete'})
    return ''.join(blocks) if blocks else 'No operational memory matched the query.'


def build_memory_packet(
    query: str, max_chars: int = 60000, *, domain: str | None = None,
    cluster: str | None = None, _views=None,
) -> dict:
    if max_chars < 0:
        raise ValueError('max_chars must be non-negative')
    scope = {"domain": domain, "cluster": cluster} if domain or cluster else {}
    if _views is not None:
        memories, historical = _views
    elif scope:
        memories, historical = governance_store.search_memory_packet_views(query, domain=domain, cluster=cluster)
    else:
        memories, historical = governance_store.search_memory_packet_views(query)

    stale_or_conflicted = [
        item for item in historical
        if str(item.get("validity_state", "")).lower() in {"conflicted", "review-due", "needs-review", "superseded", "expired"}
    ]

    sections = {
        "Current Preferences": [],
        "Open Decisions": [],
        "Task State": [],
        "Relevant Facts": [],
    }
    type_to_section = {
        "preference": "Current Preferences",
        "decision": "Open Decisions",
        "task_state": "Task State",
        "fact": "Relevant Facts",
    }

    evidence_pointers = []
    for memory in memories:
        section = type_to_section.get(memory.get("memory_type", "fact"), "Relevant Facts")
        text = " ".join(str(memory.get("text", "")).split())
        # ``rel`` is the score this item was ranked by, and it is shown first because it is the one
        # that explains the order; ``mem`` is the stored per-record score, which now only breaks
        # ties.  The old line showed the stored score alone, so a packet ranked by relevance
        # displayed a column that was nearly constant across items (measured: 24 items inside
        # 0.65-0.70 with five distinct values) and the reader could not see why anything was here.
        line = (
            f"- [rel {float(memory.get('retrieval_score', 0) or 0):.1f}"
            f" | mem {memory.get('memory_score', 0):.2f}"
            f" | {memory.get('validity_state', 'active')}] "
            f"{text[:420]}"
        )
        if memory.get("source_page"):
            line += f" ({memory['source_page']})"
        sections[section].append(line)
        if memory.get("source_claim_id"):
            evidence_pointers.append(
                f"- {memory.get('source_claim_id')} -> {memory.get('source_page', 'unknown')}"
            )

    # The packet is built to fit rather than assembled and then cut.  Cutting at ``max_chars``
    # severed lines and could leave the closing tag detached, and it made ``omitted_count``
    # uncomputable: the field reported ``len(memories) - 12``, a number that matched neither the
    # memories dropped nor the 12-pointer display cap.  It is now exactly the number of memory
    # lines the budget could not carry, and the packet stays well-formed.
    #
    # ``warning_count`` is the true count while only six warnings are shown, because the field is
    # read as ``memory_warning_count``, printed to the caller as "warnings", and used as the
    # trigger for the burst re-render -- a capped counter made all three report "6" for any larger
    # set.
    section_order = ("Current Preferences", "Open Decisions", "Task State", "Relevant Facts")
    truncated_marker = "...[memory packet truncated]"

    warnings_block = [
        f"- [{memory.get('validity_state')}] {memory.get('memory_type')}:{memory.get('memory_key')} "
        f"-> {str(memory.get('text', ''))[:260]}"
        for memory in stale_or_conflicted[:6]
    ] or ["- None matched."]

    from vector_lake import memory_gram_index
    gram_usable = True if scope else memory_gram_index.gram_index_usable()
    policy_line = "Policy: Use this packet as the machine-facing runtime memory. If it conflicts with wiki prose, prefer active non-conflicted memory items and surface the conflict."
    if not gram_usable:
        policy_line += " [DEGRADED: Operational memory gram index is behind; served via exact projected scan]"

    head = [
        "<MEMORY_PACKET>",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Query: {query}",
        policy_line,
        "",
    ]
    tail = [
        "",
        "## Conflicts / Stale Warnings",
        *warnings_block,
        "",
        "## Evidence Pointers",
        *(evidence_pointers[:12] or ["- None matched."]),
        "</MEMORY_PACKET>",
    ]

    # The marker is reserved whether or not it is needed, so appending it cannot push the packet
    # past the budget it was fitted to.
    overhead = sum(len(line) + 1 for line in head + tail) + len(truncated_marker) + 1
    body_budget = max(0, max_chars - overhead)

    body: list[str] = []
    used = 0
    truncated = False
    for title in section_order:
        if truncated:
            break
        block = [f"## {title}", *(sections[title] or ["- None matched."]), ""]
        for line in block:
            if used + len(line) + 1 > body_budget:
                truncated = True
                break
            body.append(line)
            used += len(line) + 1
    if truncated:
        body.append(truncated_marker)

    emitted_memories = sum(1 for line in body if line.startswith("- ["))
    omitted = max(0, len(memories) - emitted_memories)

    packet = "\n".join(head + body + tail)
    if len(packet) > max_chars:
        # ``head`` plus ``tail`` alone overran the budget, so there was nothing to fit: no memory
        # line was emitted above, and the packet is cut to keep the ``budget_used <= budget_max``
        # invariant ``assemble_context`` depends on.
        minimal = f"<MEMORY_PACKET>\n{truncated_marker}\n</MEMORY_PACKET>"
        packet = minimal if len(minimal) <= max_chars else ""
        omitted = len(memories)
    return {
        "packet": packet,
        "memory_count": len(memories),
        "warning_count": len(stale_or_conflicted),
        "omitted_count": omitted,
        "is_degraded": not gram_usable,
    }


#: How much the query naming an entity counts when ranking.  A query like “卫宁健康的公司战略、核心
#: 市场及经营风险” contains a span whose presence in a page's *own name* is identity, not lexical
#: similarity -- and blending the two is what let an analysis page (0.600, whose opening sentence
#: carries 市场竞争力) outrank the subject page (0.186).  Measured over nine query shapes on three
#: subjects: bare name -> subject page rank 1; adding framework words -> rank 2, 5, 6 or 13.
#:
#: Off by default (0): the ordering is a function the labelled replays were measured against, and
#: changing it is a decision for the operator, not a side effect of a fix.  Set to 1 to put pages the
#: query names ahead of pages that merely talk about them, preserving the blended order within each
#: tier.
ENTITY_NAME_PRIORITY_DEFAULT = 0.0

#: Shortest declared name that counts as naming an entity.  Two characters, because a Chinese
#: personal name is two characters -- “师成” was refused at three, and the query that exposed it
#: (“师成关于医疗人工智能的观点”) then fell to rank 3 as a field page won the tier by score.  The
#: population was measured before lowering it: 84 two-character names, 86 occurrences, overwhelmingly
#: real entities (东软, 华为, 王涛, 祁典, 陈飞, 腾讯, 德勤...).  A few look generic (架构, 推理, 幻觉),
#: and those pages enter the named tier when a query says the word -- which is the intended reading,
#: not a collision.
ENTITY_NAME_MIN_CHARS = 2

#: Sort sentinel for a candidate the query does not name: larger than any position, so named pages
#: come first and keep their order by earliest mention.
_NO_ENTITY_MATCH = 10 ** 6


def _normalize_entity_name(text: str) -> str:
    """Lower-case, whitespace- and punctuation-free form used to test containment."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def _entity_name_positions(query: str, candidates: list[tuple[float, dict]]) -> dict[str, int]:
    """Keys the query names outright, mapped to where in the query their name starts.

    The node name (``strip_prefix``) is included so a page needs no alias to be found by the name
    its own key carries: ``Product_WiNEX`` is named by “WiNEX” even without the alias the page
    happens to declare.

    The *position* matters because a query can name more than one entity -- “师成关于医疗人工智能
    的观点” names both a person and a field, and a tier that treats them as equal lets the field's
    page push the person's page down by score.  A question puts its subject first (“X 的公司战略…”,
    “X 关于 Y”), so the earliest mention is the one being asked about.
    """
    normalized_query = _normalize_entity_name(query)
    positions: dict[str, int] = {}
    if not normalized_query:
        return positions
    for _, node in candidates:
        key = str(node.get("_key") or "")
        aliases = node.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        names = [node.get("title"), strip_prefix(key), *aliases]
        best = None
        for name in names:
            normalized = _normalize_entity_name(name)
            if len(normalized) < ENTITY_NAME_MIN_CHARS:
                continue
            at = normalized_query.find(normalized)
            if at != -1 and (best is None or at < best):
                best = at
        if best is not None:
            positions[key] = best
    return positions


def _entity_name_priority() -> float:
    try:
        value = float(os.environ.get("VECTOR_LAKE_ENTITY_NAME_PRIORITY", str(ENTITY_NAME_PRIORITY_DEFAULT)))
    except ValueError:
        return ENTITY_NAME_PRIORITY_DEFAULT
    return min(1.0, max(0.0, value))


RERANK_TOKEN_CACHE_MAX_CHARS = 4096


def _rerank_tokens_uncached(text: str, backend: str) -> tuple[str, ...]:
    from vector_lake import tokenizer as _tokenizer

    return tuple(token.lower() for token in _tokenizer.tokenize_joined(text).split())


@functools.lru_cache(maxsize=256)
def _rerank_tokens_cached(text: str, backend: str, tokenizer_identity, expansion_profile) -> tuple[str, ...]:
    return _rerank_tokens_uncached(text, backend)


class _RerankTokenCache:
    """Resolve actual identity before lookup; preserve controls and oversized-token bypass."""

    cache_clear = staticmethod(_rerank_tokens_cached.cache_clear)
    cache_info = staticmethod(_rerank_tokens_cached.cache_info)
    __wrapped__ = staticmethod(_rerank_tokens_uncached)

    def __call__(self, text: str, backend: str) -> tuple[str, ...]:
        from vector_lake import tokenizer as _tokenizer

        return _rerank_tokens_cached(text, backend, _tokenizer.cache_identity(), PUBLIC_QUERY_PROFILE)


_cached_rerank_tokens = _RerankTokenCache()


def _rerank_candidates_locally(query: str, candidates: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """Phase 2: local reranking of the retrieved candidate pool with BM25.

    Candidate *membership* is decided upstream by FTS5 + graph expansion, so
    recall is unchanged; this only re-orders within that pool and replaces the
    raw `-bm25` magnitudes that previously dominated the blend.

    The lexical signal comes from ``vector_lake_core.fast_bm25_rerank`` (the Rust core) over title +
    summary + aliases. The page body is deliberately not read here: it would add per-candidate file
    IO to every query, and ``index.json`` no longer carries it.

    Why the Rust path is **required** rather than preferred: a pure-Python BM25 reranker was kept as
    a fallback for hosts without the extension, but the build path here is ``maturin develop`` and
    ``tool_doctor`` already reports the core as a first-class check, so the fallback's only effect
    was to make the same query pay a second, differently-scored engine on the hosts least able to
    afford it.  Without the symbol the rerank degrades to the documented no-op
    (``VECTOR_LAKE_RERANK_WEIGHT=0`` semantics: keep the upstream order) and says so at WARNING.

    Scores are **pool-relative**, not absolute: each input signal is min-max
    normalised before blending, so the leading blended score need not be 1.0.
    Without lexical signal the blend is a monotone rescaling of upstream scores.
    The scores are ranking signals, not calibrated confidence.

    ``VECTOR_LAKE_RERANK_WEIGHT`` (default 0.4) blends the two normalised score
    sets; set it to 0 to reproduce the previous ordering exactly. The weight
    keeps upstream influence on purpose so graph-expanded candidates, which have
    no lexical overlap by construction, are not buried.
    """
    _LAST_RERANK_NOTE.message = None
    if len(candidates) < 2:
        return candidates
    try:
        weight = float(os.environ.get("VECTOR_LAKE_RERANK_WEIGHT", "0.4"))
    except ValueError:
        weight = 0.4
    weight = min(1.0, max(0.0, weight))
    if weight <= 0.0:
        return candidates

    if not (HAVE_CORE and hasattr(vector_lake_core, "fast_bm25_rerank")):
        # Fail open and loudly: with no reranker the upstream order stands, which is exactly what
        # VECTOR_LAKE_RERANK_WEIGHT=0 means.  Silent here would hide a broken install, because the
        # scores keep looking normalised either way.
        _LAST_RERANK_NOTE.message = 'Local reranking unavailable: required native symbol is missing'
        log.warning(
            "Local reranking unavailable: vector_lake_core.fast_bm25_rerank is missing "
            "(build the Rust core with `maturin develop`); keeping upstream order."
        )
        return candidates

    from vector_lake import tokenizer as _tokenizer

    def _document(node: dict) -> str:
        aliases = node.get("aliases") or []
        alias_text = " ".join(str(a) for a in aliases) if isinstance(aliases, list) else str(aliases)
        return " ".join(
            part for part in (str(node.get("title") or ""), str(node.get("summary") or ""), alias_text) if part
        ).strip()

    # Pre-tokenize with the project tokenizer and hand the core whitespace-split tokens; the Rust
    # side scores them and blends the upstream signal, so no second engine and no Python-side
    # re-normalisation is involved.
    query_tokens = [token.lower() for token in _tokenizer.cut(query)]
    if not query_tokens:
        return candidates
    backend = _tokenizer.backend_name()
    documents = []
    for _, node in candidates:
        text = _document(node)
        # Bound retained metadata as well as entry count; unusually large pages still rerank,
        # but must not pin their token payload in a long-lived server's hot cache.
        tokenize = _cached_rerank_tokens if len(text) <= RERANK_TOKEN_CACHE_MAX_CHARS else _cached_rerank_tokens.__wrapped__
        documents.append(tokenize(text, backend))
    if not any(documents):
        return candidates

    try:
        rerank_indices = vector_lake_core.fast_bm25_rerank(
            list(query_tokens),
            documents,
            [float(score) for score, _ in candidates],
            weight,
        )
    except Exception as exc:
        # Fail open: a reranker fault must never drop results.
        _LAST_RERANK_NOTE.message = f'Local reranking failed: {type(exc).__name__}'
        log.warning("Local reranking failed (%s); keeping upstream order.", type(exc).__name__)
        return candidates

    if len(rerank_indices) != len(candidates):
        _LAST_RERANK_NOTE.message = 'Local reranking failed: candidate count mismatch'
        log.warning(
            "Local reranking returned %s scores for %s candidates; keeping upstream order.",
            len(rerank_indices), len(candidates),
        )
        return candidates
    return [(score, candidates[index][1]) for index, score in rerank_indices]


def _entity_first_order(
    query: str, scored: list[tuple[float, dict]]
) -> list[tuple[float, dict]]:
    """The final ordering: pages the query *names* first, then by score.

    Two tiers, not a bonus.  A bonus large enough to achieve the same thing is this partition
    expressed the hard way, and keeping the order a sort key means the score values keep their
    pool-normalised meaning.
    """
    named = _entity_name_positions(query, scored) if _entity_name_priority() > 0 else {}
    return sorted(
        scored,
        key=lambda item: (named.get(item[1].get("_key"), _NO_ENTITY_MATCH), -item[0]),
    )


def _safe_eval(expr: str, context: dict) -> bool:
    allowed_operators = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Gt: operator.gt,
        ast.Lt: operator.lt,
        ast.GtE: operator.ge,
        ast.LtE: operator.le,
        ast.In: lambda a, b: a in b if b is not None else False,
        ast.NotIn: lambda a, b: a not in b if b is not None else False,
        ast.And: lambda a, b: a and b,
        ast.Or: lambda a, b: a or b,
        ast.Not: operator.not_,
    }

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            # Try to get from node if filter_expr assumes node dict (e.g. node.get)
            # Actually, standard LLM output uses `type == 'vendor'` so node.id in context handles it.
            return None
        elif isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator)
                if type(op) not in allowed_operators:
                    raise ValueError(f"Unsupported operator: {type(op)}")
                if not allowed_operators[type(op)](left, right):
                    return False
                left = right
            return True
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(_eval(v) for v in node.values)
            elif isinstance(node.op, ast.Or):
                return any(_eval(v) for v in node.values)
        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return not _eval(node.operand)
        elif isinstance(node, ast.Call):
            # To support node.get('key') == 'value' or just get('key')
            # The context is actually `node`. So get() refers to node.get.
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'get':
                obj = _eval(node.func.value)
                if isinstance(obj, dict) and node.args:
                    key = _eval(node.args[0])
                    default = _eval(node.args[1]) if len(node.args) > 1 else None
                    return obj.get(key, default)
            elif isinstance(node.func, ast.Name) and node.func.id == 'get':
                if node.args:
                    key = _eval(node.args[0])
                    default = _eval(node.args[1]) if len(node.args) > 1 else None
                    return context.get(key, default)
        raise ValueError(f"Unsupported AST node: {type(node)}")

    try:
        tree = ast.parse(expr, mode='eval')
        return bool(_eval(tree.body))
    except Exception as e:
        log.warning("Filter expression invalid: %s", runtime.safe_error(e))
        return False


MAX_FILTERED_RECALL_CANDIDATES = 4096


def _filtered_recall(fetch, catalog, depth: int, domain, cluster, include_history, filter_expr, *, raw_count=None):
    """Widen a ranked source until its first ``depth`` eligible pages are known.

    Applying the filter to the first N *unfiltered* hits loses valid pages; widening preserves
    ranked order without changing the unfiltered retrieval path.  A fetch may already be
    metadata-aware (see ``_get_vector_search_results_filtered``), in which case ``raw_count``
    reports how many candidates it examined -- the source is only exhausted when the *raw* list came
    back shorter than requested, not when a filter rejected most of it.
    """
    limit = min(depth, MAX_FILTERED_RECALL_CANDIDATES)
    while True:
        runtime.checkpoint()
        rows, error = fetch(limit)
        keys = list(rows) if isinstance(rows, dict) else [row["node_key"] for row in rows]
        nodes = catalog.nodes_by_key(keys)
        eligible = {
            key for key in keys
            if key in nodes and _passes_filters(
                nodes[key], domain, cluster, include_history, filter_expr
            )
        }
        if isinstance(rows, dict):
            selected = {key: value for key, value in rows.items() if key in eligible}
        else:
            selected = [row for row in rows if row["node_key"] in eligible]
        examined = raw_count() if raw_count else None
        if examined is None:
            examined = len(keys)
        complete = len(selected) >= depth or examined < limit
        capped = limit >= MAX_FILTERED_RECALL_CANDIDATES and not complete
        if error or complete or capped:
            if capped:
                note = (f"filtered recall cap ({MAX_FILTERED_RECALL_CANDIDATES}) reached; "
                        f"results may be incomplete (raw_examined={examined}, "
                        f"target_depth={depth}, selected={len(selected)}, "
                        f"domain={domain or '(any)'}, cluster={cluster or '(any)'}, "
                        f"include_history={include_history}, filter_expr_active={bool(filter_expr)})")
            elif not error and not selected and keys:
                # The source did match; the selectors removed every candidate. Reporting this as
                # "no stored vectors matched" sent the reader to ``embedding-backfill``, which
                # cannot repair a filter. Name the stage that actually decided.
                note = (f"{len(keys)} ranked candidate(s) matched but none passed the "
                        "domain/cluster/history/filter selectors "
                        f"(domain={domain or '(any)'}, cluster={cluster or '(any)'}, "
                        f"include_history={include_history}, filter_expr_active={bool(filter_expr)})")
            else:
                note = None
            return (dict(list(selected.items())[:depth]) if isinstance(selected, dict)
                    else selected[:depth]), error, note
        limit = min(limit * 2, MAX_FILTERED_RECALL_CANDIDATES)


def _filtered_graph_expansions(ranked, catalog, limit, domain, cluster, include_history, filter_expr):
    """Select the first eligible graph hits, reading metadata in bounded batches."""
    selected = []
    if limit <= 0:
        return selected
    has_selectors = bool(domain or cluster or not include_history or filter_expr)
    needs_full = not has_selectors or bool(filter_expr) or not hasattr(catalog, "filter_fields")
    # An unfiltered request needs no separate metadata round trip; fetch only its next quota.
    batch_size = 32 if has_selectors else min(32, limit)
    for start in range(0, len(ranked), batch_size):
        batch = ranked[start:start + batch_size]
        keys = [key for key, _weight in batch]
        fields = catalog.nodes_by_key(keys) if needs_full else catalog.filter_fields(keys)
        eligible = [
            (key, weight) for key, weight in batch
            if key in fields and _passes_filters(fields[key], domain, cluster, include_history, filter_expr)
        ]
        # Metadata can change or disappear before its payload read. Count only still-eligible
        # materialised pages toward the quota rather than leaking a changed selector or shortening it.
        while eligible and len(selected) < limit:
            remaining = limit - len(selected)
            shortlist, eligible = eligible[:remaining], eligible[remaining:]
            nodes = fields if needs_full else catalog.nodes_by_key([key for key, _ in shortlist])
            selected.extend(
                (key, weight, nodes[key]) for key, weight in shortlist
                if key in nodes and (needs_full or _passes_filters(nodes[key], domain, cluster, include_history, filter_expr))
            )
        if len(selected) >= limit:
            break
    return selected


def _scoped_chunk_candidates(query, domain, cluster, include_history, query_vector, catalog, plan, *, filter_expr=None):
    from pathlib import Path
    from vector_lake import chunk_index
    if not domain:
        raise ValueError('Chunk retrieval requires an explicit domain')
    model = _embedding_cache_key(query)[0] if query_vector else None
    root = Path(get_wiki_dir()).resolve()
    accepted, stale, excluded, checked = {}, set(), set(), set()
    limit, notes = 25, []
    while True:
        runtime.checkpoint()
        rows, batch_notes = chunk_index.search_chunks(
            query, domain, cluster=cluster, include_history=include_history,
            query_vector=query_vector, model=model, supplemental_queries=plan.supplemental_queries,
            top_k=limit,
        )
        notes.extend(n for n in batch_notes if n not in notes)
        nodes = catalog.nodes_by_key([row['page_key'] for row in rows])
        for row in rows:
            key = row['page_key']
            if key in checked:
                continue
            checked.add(key)
            node = nodes.get(key)
            if node is None or not plan.accepts(node) or not _passes_filters(node, domain, cluster, include_history, filter_expr):
                excluded.add(key)
                continue
            try:
                path = (root / (key + '.md')).resolve()
                if '/' in key or '\\' in key or not path.is_relative_to(root):
                    raise ValueError('Invalid chunk source path')
                if chunk_index.content_hash(chunk_index.read_page(path)) != row['source_hash']:
                    raise ValueError('Stale chunk source')
            except runtime.RetrievalBudgetExceeded as exc:
                notes.append(runtime.failed_stage('Chunk source budget', exc))
                return list(accepted.values())[:25], notes
            except (OSError, UnicodeError, ValueError):
                stale.add(key)
                continue
            accepted[key] = row
        if len(accepted) >= 25 or len(rows) < limit:
            break
        if limit >= chunk_index.MAX_CANDIDATES:
            notes.append('Filtered chunk document budget reached; results may be incomplete')
            break
        limit = min(limit * 2, chunk_index.MAX_CANDIDATES)
    if stale:
        notes.append(f'Chunk source unavailable or changed for {len(stale)} candidate(s); refresh explicitly')
    if excluded:
        notes.append(f'Chunk candidates excluded by current source metadata: {len(excluded)}')
    return list(accepted.values())[:25], notes


def _search_scored_pages(
    query: str,
    top_k: int,
    domain: str | None = None,
    cluster: str | None = None,
    include_history: bool = False,
    filter_expr: str | None = None,
    catalog=None,
    projection_note: str | None = None,
    *,
    timings: dict[str, float] | None = None,
    allow_external_embedding: bool = True,
    profile: str = 'legacy',
    use_chunks: bool = False,
):
    """Ranked pages plus retrieval notes.

    Split out of ``search_vector_lake`` so callers that need the pages can use
    them directly.  ``assemble_context`` used to re-parse the human-readable
    string with a regex, which silently produced an empty wiki context whenever
    the formatting changed or a title contained the delimiter.

    Nodes and the personalised-PageRank adjacency come from the SQLite projection
    (``page_index_projection``) instead of a full ``index.json`` parse per process
    and a full 30 140-edge dict rebuild per query.

    ``timings`` is an optional caller-owned output dictionary of phase milliseconds;
    it is reset per call and does not change result formatting or the persistent ledger.
    """
    from vector_lake import page_index_projection

    from vector_lake.retrieval_plan import plan_query, PlannedCatalog
    plan = plan_query(query, profile)
    if use_chunks and not domain:
        raise ValueError('Chunk retrieval requires an explicit domain')
    started = time.perf_counter()
    phase_started = started
    if top_k <= 0:
        raise ValueError('top_k must be positive')
    timings = {} if timings is None else timings
    timings.clear()

    def _mark(phase):
        nonlocal phase_started
        if timings is not None:
            now = time.perf_counter()
            timings[phase] = (now - phase_started) * 1000.0
            phase_started = now

    # Resolved before ``_record`` is defined: the ledger closure references it, and the early exits
    # (a missing index, unreadable tokens) record through that same closure -- referencing a local
    # assigned further down raised ``NameError: cannot access free variable 'fusion_mode'`` on exactly
    # those paths.
    fusion_mode = 'rrf' if use_chunks else _fusion_mode()
    _LAST_FTS_ERROR.backends = set()

    def _record(returned_rows, notes, error=None):
        generation = page_index_projection.index_file_stamp()
        if use_chunks:
            from vector_lake.chunk_index import generation_token
            try:
                generation = (generation, generation_token())
            except OSError:
                generation = (generation, 'chunk-state-unavailable')
        _, failures = split_retrieval_notes(notes)
        status = 'failed' if error or (failures and not returned_rows) else 'partial' if failures else 'complete'
        payload: dict = dict(
            query=query,
            mode="page",
            top_k=top_k,
            returned=returned_rows,
            notes=notes,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=error,
            fusion=fusion_mode,
            fts_backend="+".join(sorted(_LAST_FTS_ERROR.backends)) or "none",
            selectors={"domain": domain, "cluster": cluster, "include_history": include_history,
                       "filter_expr_active": bool(filter_expr)},
            index_generation=repr(generation),
            configuration={"candidate_depth": _candidate_depth(), "candidate_pool": _candidate_pool(),
                           "allow_external_embedding": allow_external_embedding,
                           "use_chunks": use_chunks, **plan.diagnostics()},
            timings=dict(timings),
            status=status,
        )
        if status == 'failed' and not payload['error']:
            payload['error'] = 'retrieval_failed'
        if not runtime.defer_record(payload):
            search_ledger.record(**payload)
        _mark("ledger")
        if timings is not None:
            timings["total"] = (time.perf_counter() - started) * 1000.0

    # Read-only source selection.  A reader never repairs the projection: the
    # rebuild needs the write lock, and a reader that lost that race used to give
    # up entirely (a 60 s MCP timeout with no answer).  ``index.json`` is the
    # sovereign artifact, so when the projection is behind the file answers
    # instead -- the note records that the answer did not come from the
    # projection, and the outbox consumer rebuilds it out of band.
    #
    # ``catalog`` is injectable so a caller that already resolved it for this
    # request (``assemble_context``) does not parse the file a second time.
    if catalog is None:
        catalog, projection_note = page_index_projection.read_catalog()
    _mark("catalog")
    if catalog is None:
        if page_index_projection.index_file_stamp() is None:
            answer = "Lake is drying. No index.json found, please ingest sources first."
            _record([], [], error=answer)
            return [], [], answer
        answer = "Error reading the knowledge base index. Please ensure the index exists and is not corrupted."
        _record([], [], error=answer)
        return [], [], answer

    if plan.event_query:
        catalog = PlannedCatalog(catalog, plan)
    intent = _keyword_intent(query)
    scored = []
    tokens = _expand_query_locally(query)
    _mark("query_processing")
    if not tokens:
        _record([], [], error="No valid search tokens.")
        return [], [], "No valid search tokens."

    intent = _keyword_intent(query)
    
    # PHASE 2 FTS5 + VECTOR HYBRID QUERY
    hybrid_scores = {}
    vector_notes = []
    if plan.profile != 'legacy':
        vector_notes.append(f'Retrieval profile: {plan.profile}; ranking preference is not evidence confidence')
    if use_chunks:
        vector_notes.append('Chunk fusion: reciprocal rank fusion; sidecar is opt-in')
    if plan.event_query:
        vector_notes.append('Event query constraints: metadata/title years only; event identity requires source verification')
        if plan.year_ambiguous:
            vector_notes.append('Event year unverified: multiple years requested; no single-year restriction applied')
    
    # 1. FTS5 Search
    state = runtime.current()
    if state is not None:
        state.fts_scope = {'domain': domain, 'cluster': cluster, 'include_history': include_history} if getattr(catalog, 'source', '') == 'projection' else None
    fts_ranked: list[str] = []
    lexical_nodes: dict[str, dict] = {}
    try:
        if domain or cluster or filter_expr or not include_history or plan.event_year:
            def fetch_fts(limit):
                rows = (_get_fts_recall_results(query, limit=limit, supplemental_queries=plan.supplemental_queries)
                        if plan.supplemental_queries else _get_fts_recall_results(query, limit=limit))
                return rows, getattr(_LAST_FTS_ERROR, "msg", None)

            fts_results, fts_error, fts_note = _filtered_recall(
                fetch_fts, catalog, _candidate_depth(), domain, cluster,
                include_history, filter_expr,
            )
            if fts_note:
                vector_notes.append(f"FTS: {fts_note}")
        else:
            fts_results = (_get_fts_recall_results(query, limit=_candidate_depth(), supplemental_queries=plan.supplemental_queries)
                           if plan.supplemental_queries else _get_fts_recall_results(query, limit=_candidate_depth()))
            fts_error = getattr(_LAST_FTS_ERROR, "msg", None)
        if fts_error:
            vector_notes.append(fts_error)
            if not fts_results and hasattr(catalog, "_nodes") and isinstance(catalog._nodes, dict):
                fallback_hits = []
                q_lower = query.lower()
                toks_lower = [t.lower() for t in tokens if len(t) >= 2]
                for k, node in catalog._nodes.items():
                    title = str(node.get("title") or "").lower()
                    k_lower = k.lower()
                    score = 0.0
                    if q_lower in title or q_lower in k_lower:
                        score = 10.0
                    elif any(t in title or t in k_lower for t in toks_lower):
                        score = 5.0
                    if score > 0.0:
                        fallback_hits.append({"node_key": k, "title": node.get("title", k), "score": score})
                fallback_hits.sort(key=lambda x: x["score"], reverse=True)
                fts_results = fallback_hits[:_candidate_depth()]
                if fts_results:
                    _LAST_FTS_ERROR.backend = "index.json"
                    _LAST_FTS_ERROR.backends.add("index.json")
                    vector_notes.append(f"FTS5 fallback: matched {len(fts_results)} node(s) from index.json")
        # Finish lexical evidence before starting optional vector work. Keep this
        # request-local snapshot usable even after the SQL progress deadline expires.
        read_nodes = getattr(catalog, 'nodes_by_key', None)
        if not callable(read_nodes):
            raise TypeError('Catalog node reader unavailable')
        loaded_nodes = read_nodes([row['node_key'] for row in fts_results])
        if not isinstance(loaded_nodes, dict):
            raise TypeError('Catalog node reader must return a dictionary')
        lexical_nodes = loaded_nodes
        for position, row in enumerate(fts_results):
            key = row['node_key']
            if key not in lexical_nodes:
                continue
            raw_score = row.get('rank')
            if raw_score is None:
                raw_score = row.get('score', 0)
            if fusion_mode == "rrf":
                # Reciprocal rank: the source's position, not its arbitrary magnitude.
                contribution = 1.0 / (RRF_K + position + 1)
            else:
                contribution = raw_score * -1.0  # SQLite BM25 is negative
            hybrid_scores[key] = hybrid_scores.get(key, 0.0) + contribution
            fts_ranked.append(key)
    except Exception as e:
        log.error("FTS5 Search failed (%s)", type(e).__name__)
        vector_notes.append(f"FTS arm failed: {type(e).__name__}")

    _mark("fts")

    # 2. Vector Search (Hybrid blending)
    vector_ranked: list[str] = []
    query_vector = []
    try:
        projection_error = _vector_projection_error() if allow_external_embedding and not runtime.expired() else None
        if runtime.expired():
            query_vector, embedding_error = [], 'Request deadline budget exhausted'
        elif not allow_external_embedding:
            query_vector, embedding_error = [], "External embedding disabled by request policy"
        else:
            query_vector, embedding_error = ([], projection_error) if projection_error else _get_query_embedding(query)
        _mark("embedding")
        if query_vector:
            vector_note = None
            if domain or cluster or filter_expr or not include_history or plan.event_year:
                # Metadata-first arm: reject on metadata before paying for the exact rerank.  The
                # fallback to the plain arm (shadow index unavailable, or the switch off) keeps the old
                # cost and ordering profile rather than losing filtered recall.
                examined = {"raw": None}
                shortlist_reuse = {}

                def _fetch(limit, _examined=examined):
                    rows, error, raw = (
                        _get_vector_search_results_filtered(
                            query_vector, limit, catalog, domain, cluster, include_history, filter_expr,
                            reuse=shortlist_reuse,
                        )
                        if _metadata_first_enabled()
                        else (None, None, 0)
                    )
                    if rows is None:
                        _examined["raw"] = None
                        return _get_vector_search_results(query_vector, limit=limit)
                    _examined["raw"] = raw
                    return rows, error

                vector_results, vector_error, vector_note = _filtered_recall(
                    _fetch,
                    catalog, _candidate_depth(), domain, cluster, include_history, filter_expr,
                    raw_count=lambda: examined["raw"],
                )
                if vector_note:
                    diagnostics = shortlist_reuse.get("diagnostics")
                    if diagnostics:
                        vector_note += (
                            f"; metadata_passed={diagnostics['metadata_passed']}, "
                            f"similarity_passed={diagnostics['similarity_passed']}, "
                            f"min_cosine>{VECTOR_MIN_COSINE}"
                        )
                    vector_notes.append(f"vector: {vector_note}")
            else:
                vector_results, vector_error = _get_vector_search_results(query_vector, limit=_candidate_depth())
            if vector_error:
                vector_notes.append(vector_error)
            elif not vector_results and not vector_note:
                vector_notes.append(
                    'Page vector path had no eligible matches; chunk path evaluated separately' if use_chunks else
                    "no stored vectors matched; run `embedding-backfill --apply` to (re)build the vector projection"
                )
            # ``_get_vector_search_results`` inserts in ascending distance, so enumeration order is
            # the vector ranking that RRF needs.
            for position, (key, sim) in enumerate(vector_results.items()):
                if fusion_mode == "rrf":
                    contribution = 1.0 / (RRF_K + position + 1)
                else:
                    # scale similarity so it competes/blends with BM25.
                    contribution = (sim ** 2) * _vector_sim_scale()
                hybrid_scores[key] = hybrid_scores.get(key, 0.0) + contribution
                vector_ranked.append(key)
        else:
            vector_notes.append(embedding_error or "query embedding unavailable")
    except runtime.RetrievalBudgetExceeded as exc:
        vector_notes.append(runtime.failed_stage('Vector retrieval', exc) + '; lexical path retained')

    _mark("vector_search")
    chunk_details = {}
    if use_chunks:
        try:
            chunk_rows, chunk_notes = _scoped_chunk_candidates(
                query, domain, cluster, include_history, query_vector, catalog, plan,
                filter_expr=filter_expr,
            )
            vector_notes.extend(chunk_notes)
            for position, row in enumerate(chunk_rows):
                key = row['page_key']
                hybrid_scores[key] = hybrid_scores.get(key, 0) + 1 / (RRF_K + position + 1)
                chunk_details[key] = row
        except (sqlite3.Error, OSError, UnicodeError, ValueError, ImportError, runtime.RetrievalBudgetExceeded) as exc:
            vector_notes.append(runtime.failed_stage('Chunk retrieval', exc) + '; page paths retained')
    _mark('chunks')
    fts_hits = set(fts_ranked)
    vector_hits = set(vector_ranked)

    # Only the keys the hybrid stage actually produced are materialised, so the
    # 7 178-node dict never has to exist in memory.
    nodes = dict(lexical_nodes)
    missing = [key for key in hybrid_scores if key not in nodes]
    if missing and not runtime.expired():
        try:
            nodes.update(catalog.nodes_by_key(missing))
        except (sqlite3.Error, runtime.RetrievalBudgetExceeded) as exc:
            vector_notes.append(runtime.failed_stage('Candidate hydration', exc) + '; completed lexical evidence retained')

    for key, score in hybrid_scores.items():
        node = nodes.get(key)
        if node is None:
            continue
        if key in fts_hits and key in vector_hits:
            origin = "both"
        elif key in fts_hits:
            origin = "fts"
        else:
            origin = "vec"
        if key in chunk_details:
            origin = (origin + '+chunk') if key in fts_hits or key in vector_hits else 'chunk'
        node = {**node, "_key": key, "_origin": origin}
        if key in chunk_details:
            node['_chunk_source_hash'] = chunk_details[key]['source_hash']
            node['_chunk_hits'] = chunk_details[key]['hits']
        if not _passes_filters(node, domain, cluster, include_history, filter_expr):
            continue
        if not include_history and node.get('status', '').lower() == 'decayed' and intent != 'temporal':
            score *= 0.2
        scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)

    _mark("materialize")

    # P2-1: Dynamic Graph Expansion via Multi-hop PPR (Personalized PageRank)
    top_keys = {node["_key"] for _, node in scored[:5]}
    try:
        adj = catalog.adjacency() if top_keys and not runtime.expired() else None
        if top_keys and adj:
            # PPR parameters.  Non-seed nodes must still receive teleportation mass,
            # otherwise the walk collapses to "adjacent to a seed" after two steps.
            seed_keys = set(top_keys)
            existing_keys = {node["_key"] for _, node in scored}
            expansion_limit = 12 if intent == "entity" else 5

            if HAVE_CORE and hasattr(vector_lake_core, "prepared_personalized_pagerank"):
                # The same walk on a prebuilt index.  Measured 31.9 -> 1.5 ms per query (20.7x) and
                # verified bit-identical on 16 edge cases plus the live graph: the legacy call pays for
                # moving the 39 382-edge dict into Rust and cloning ~150k strings per call, which the
                # index does once (27.9 ms) per graph generation.
                from vector_lake import page_index_projection as _pip

                sorted_ppr = vector_lake_core.prepared_personalized_pagerank(
                    _ppr_index(adj, _pip.adjacency_generation()), list(seed_keys), 0.85, 2
                )
            elif HAVE_CORE:
                sorted_ppr = vector_lake_core.fast_personalized_pagerank(adj, list(seed_keys), 0.85, 2)
            else:
                alpha = 0.85
                restart_mass = (1 - alpha) / len(top_keys)
                ppr_scores = {k: 1.0 / len(top_keys) for k in seed_keys}

                for _ in range(2):
                    next_scores = {k: restart_mass if k in seed_keys else 0.0 for k in adj}
                    for node, current_score in ppr_scores.items():
                        neighbors = adj.get(node, [])
                        if neighbors:
                            total_weight = sum(w for _, w in neighbors)
                            if total_weight <= 0:
                                continue
                            for neighbor, w in neighbors:
                                next_scores[neighbor] = next_scores.get(neighbor, 0.0) + alpha * current_score * (w / total_weight)
                    ppr_scores = next_scores

                sorted_expansions = sorted(
                    [(k, v) for k, v in ppr_scores.items() if k not in existing_keys and v > 0.0],
                    key=lambda x: x[1],
                    reverse=True
                )
            if HAVE_CORE:
                # VL-S02: graph-wide zero entries are not reachable candidates. In RRF a zero
                # would otherwise become a positive vote merely because it has a list position.
                sorted_expansions = [(k, v) for k, v in sorted_ppr if k not in existing_keys and v > 0.0]

            expansions = _filtered_graph_expansions(
                sorted_expansions, catalog, expansion_limit, domain, cluster, include_history, filter_expr,
            )
            for expansion_rank, (expanded_key, ppr_weight, expanded_node) in enumerate(expansions):
                if fusion_mode == "rrf":
                    # The expansion ordering is a third *ranked list*, expressed in the same units as
                    # the fused ones.  Scaling it by 15 instead put it in the space the other two no
                    # longer use after RRF: at sum-fusion ``ppr_weight * 15`` is about 0.45 against
                    # BM25 magnitudes, but against RRF's ~0.015 it dominated -- measured 40 queries with
                    # real embeddings, expansion's share of returned pages went 2/200 to 156/200 and
                    # recall@5 fell 0.75 to 0.33.  One list, one vote, or the stages are not comparable.
                    expansion_score = 1.0 / (RRF_K + expansion_rank + 1)
                else:
                    expansion_score = ppr_weight * 15.0
                scored.append((expansion_score, {**expanded_node, "_key": expanded_key, "_origin": "ppr"}))

    except Exception as exc:
        vector_notes.append(runtime.failed_stage('PPR', exc))

    scored.sort(key=lambda item: item[0], reverse=True)

    _mark("ppr")

    # Phase 1: Expand candidate pool for reranking.  Pool size is fixed, so the reranker's
    # pool-local min-max normalisation no longer depends on how many answers the caller asked for.
    pool_size = _candidate_pool()
    max_sources_pool = int(pool_size * 0.6) if plan.profile == 'legacy' else pool_size
    expansion_quota = _expansion_quota(pool_size)
    source_budget = [0]

    def _fill(entries, capacity):
        """Take up to ``capacity`` entries in score order, with the source-type cap applied.

        ``source_budget`` is shared across calls so the cap stays pool-wide, exactly as the single
        loop it replaces applied it.
        """
        taken = []
        for score, node in entries:
            if node.get("type", "").lower() == "source":
                if source_budget[0] >= max_sources_pool:
                    continue
                source_budget[0] += 1
            taken.append((score, node))
            if len(taken) >= capacity:
                break
        return taken

    if expansion_quota:
        primary = [(s, n) for s, n in scored if n.get("_origin") != "ppr"]
        expanded = [(s, n) for s, n in scored if n.get("_origin") == "ppr"]
        candidate_pool = _fill(primary, pool_size - expansion_quota)
        candidate_pool += _fill(expanded, expansion_quota)
        if len(candidate_pool) < pool_size:
            # The reservation is a floor for expansion, not a ceiling for the rest: what expansion
            # did not use goes back to the fusion candidates, still in score order.
            already = {node["_key"] for _, node in candidate_pool}
            leftover = [(s, n) for s, n in scored if n["_key"] not in already]
            candidate_pool += _fill(leftover, pool_size - len(candidate_pool))
        candidate_pool.sort(key=lambda item: item[0], reverse=True)
    else:
        candidate_pool = _fill(scored, pool_size)
    _mark("pool")

    # Phase 2: Local deterministic ranking. Text-model reranking is delegated
    # to the host agent when explicitly requested, not performed by runtime code.
    _LAST_RERANK_NOTE.message = None
    reranked = _rerank_candidates_locally(query, candidate_pool)
    if _LAST_RERANK_NOTE.message:
        vector_notes.append(_LAST_RERANK_NOTE.message)
    _mark("rerank")

    # Phase 3: Final top_k extraction.  Sources are demoted, not counted: a fixed multiplicative
    # penalty keeps the ordering a function of the pool (so top-k remains a pure window) and keeps
    # every eligible page in the ranking, while still making an all-Source answer hard to produce.
    # The absolute cap this replaces shortened the window for 104 of 333 queries at top_k=20.
    penalty = _source_rank_penalty() if plan.profile == 'legacy' else 1.0
    penalised = [
        (score * penalty if node.get("type", "").lower() == "source" else score, node)
        for score, node in reranked
    ]
    # The author facet: off by default, and expressed as a *score* multiplier rather than a tier for
    # the same reason the entity tier had to move -- the sort below reads scores, so anything that
    # only reorders is discarded.  Authorship is not in the text: 131 of the 149 pages written from
    # raw/article never name the author, so "what did he say about X" cannot find them by words.
    author_mode = author_facet.author_facet_mode() if plan.profile == 'legacy' else ('boost' if plan.profile == 'author' else 'off')
    if author_mode != "off":
        author_keys = author_facet.author_page_keys()
        if author_keys:
            if author_mode == "filter":
                narrowed = [(s, n) for s, n in penalised if n.get("_key") in author_keys]
                vector_notes.append(
                    f"VECTOR_LAKE_AUTHOR_FACET=filter: {len(narrowed)} of {len(penalised)} "
                    f"candidates are pages from {', '.join(author_facet.author_source_prefixes())}."
                )
                penalised = narrowed
            else:
                # A *relative* additive lift, not a multiplier: the blended scores are min-max
                # normalised over the pool, so the pool's minimum is exactly 0.0 and no multiplier can
                # lift it (measured: a x60 boost left the bottom candidate at 0.0).  Scaling the lift by
                # the pool's own maximum keeps it dimension-free, which matters because ``rrf`` fusion
                # produces scores around 0.016 rather than 0-1.
                bonus = author_facet.author_boost()
                pool_max = max((score for score, _ in penalised), default=0.0)
                lift = bonus * pool_max
                promoted = sum(1 for _s, n in penalised if n.get("_key") in author_keys)
                vector_notes.append(
                    f"VECTOR_LAKE_AUTHOR_FACET=boost: {promoted} of {len(penalised)} candidates "
                    f"lifted by {lift:.3f} ({bonus}x pool max) as the author's own pages."
                )
                penalised = [
                    (score + lift if node.get("_key") in author_keys else score, node)
                    for score, node in penalised
                ]
    # The entity tier belongs *here*, at the final ordering: the sort below is by score, so a tier
    # expressed inside the reranker (which only reorders, it does not rescore) was overwritten by
    # this very statement -- measured, the switch changed no rank at all.
    final_scored = (_entity_first_order(query, penalised) if plan.profile == 'legacy' else plan.order(penalised))[:top_k]
    if plan.event_year:
        unverified = sum(plan.year_status(node) == 'unverified' for _, node in final_scored)
        if unverified:
            vector_notes.append(f'Event year unverified for {unverified} candidate(s); inspect the source before claiming an exact event match')
    _mark("final_order")

    if projection_note:
        vector_notes.append(projection_note)
    if runtime.expired():
        vector_notes.append('Request deadline budget exhausted; partial paths retained')
    _record(
        [
            {"key": node["_key"], "origin": node.get("_origin", "?"), "score": round(score, 6),
             **({'chunk_source_hash': node['_chunk_source_hash']} if node.get('_chunk_source_hash') else {})}
            for score, node in final_scored
        ],
        vector_notes,
    )
    return final_scored, vector_notes, None


def _relevant_excerpt(content: str, query: str, limit: int = 2500) -> tuple[str, str]:
    """Select a query-bearing section/window and return a stable page-line locator."""
    frontmatter = re.match(r"\A---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    offset = frontmatter.end() if frontmatter else 0
    body = content[offset:]
    if not body:
        return "", "L1"
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+)$", body))
    sections = []
    path: list[str] = []
    for index, heading in enumerate(headings):
        level = len(heading.group(1))
        path = path[:level - 1] + [heading.group(2).strip()[:512]]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections.append((heading.start(), end, " > ".join(path)))
    if not sections or sections[0][0] > 0:
        sections.insert(0, (0, sections[0][0] if sections else len(body), ""))
    terms = set(re.findall(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]{2,}", query.lower()))
    terms.update(
        term[i:i + 2] for term in tuple(terms) if CJK_REGEX.search(term)
        for i in range(len(term) - 1)
    )
    phrase = query.strip().lower()
    best = (-1, 0, 0, "")
    for start, end, heading in sections:
        for position in range(start, end, max(1, limit // 2)):
            segment = body[position:min(end, position + limit)]
            lower = segment.lower()
            score = (10 if phrase and phrase in lower else 0) + sum(
                len(term) * lower.count(term) for term in terms
            )
            if score > best[0]:
                best = (score, position, min(end, position + limit), heading)
    _, start, end, heading = best
    line = len(re.findall(r'\r\n|\r|\n', content[:offset + start])) + 1
    return body[start:end].strip(), f"L{line}" + (f" ({heading[:1024]})" if heading else "")


def _evidence_excerpts(content: str, query: str, limit: int = 2500) -> tuple[str, str]:
    """Keep opposing aspects in separate source windows without changing page membership."""
    lower = query.lower()
    positive = ('优势', '优点', 'strength', 'advantage')
    negative = ('不足', '缺点', '风险', '局限', 'weakness', 'limitation', 'shortcoming')
    if limit < 400 or not (any(t in lower for t in positive) and any(t in lower for t in negative)):
        return _relevant_excerpt(content, query, limit)
    per_window = (limit - 200) // 2
    windows = [_relevant_excerpt(content, ' '.join(aspect), per_window)
               for aspect in (positive, negative)]
    if windows[0][1] == windows[1][1] or windows[0][0] == windows[1][0]:
        return _relevant_excerpt(content, query, limit)
    text = '\n\n'.join(f'[Source excerpt: {locator}]\n{snippet}' for snippet, locator in windows)
    if len(text) > limit:
        return _relevant_excerpt(content, query, limit)
    return text, '; '.join(locator for _, locator in windows)


def _evidence_for_node(content: str, query: str, node: dict):
    readable = content.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')
    snippet, locator = _evidence_excerpts(readable, query)
    expected = node.get('_chunk_source_hash')
    if not expected:
        return snippet, locator, None
    from vector_lake.chunk_index import content_hash
    digest = content_hash(content)
    if digest != expected:
        return '', f'L1 @sha256:{digest}', 'Source changed since chunk recall; chunk evidence withheld'
    hits = node.get('_chunk_hits') or []
    if not hits or '; ' in locator:
        return snippet, locator + f' @sha256:{digest}', None
    # Re-locate each fragment inside its recorded line span in the actual current file.
    # Never quote stored chunk text merely because an index row claims a source hash.
    from bisect import bisect_right
    lines = content.splitlines(keepends=True)
    floor = 0
    if lines and lines[0].lstrip('\ufeff').strip() == '---':
        floor = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == '---'), len(lines))
    breaks = [m.end() for m in re.finditer(r'\r\n|\r|\n', content)]
    windows, locations, seen = [], [], set()
    used = 0
    invalid_span = False
    for hit in hits[:3]:
        try:
            if not isinstance(hit, dict) or type(hit.get('start_line')) is not int or type(hit.get('end_line')) is not int:
                raise ValueError('Invalid chunk span type')
            start, end = hit['start_line'] - 1, hit['end_line']
            fragment = hit['text']
            if not isinstance(fragment, str) or not fragment or start < floor or not start < end <= len(lines):
                raise ValueError('Invalid chunk span range')
        except (KeyError, TypeError, ValueError):
            invalid_span = True
            continue
        area = ''.join(lines[start:end])
        position = area.find(fragment)
        if position < 0:
            invalid_span = True
            continue
        if fragment in seen:
            continue
        seen.add(fragment)
        offset = sum(len(line) for line in lines[:start]) + position
        first_line = bisect_right(breaks, offset) + 1
        last_line = bisect_right(breaks, offset + len(fragment) - 1) + 1
        location = f'L{first_line}-L{last_line}'
        actual = area[position:position + len(fragment)].replace('\r\n', '\n').replace('\r', '\n')
        block = f'[Chunk source: {location}]\n{actual}'
        if used + len(block) + 2 > 2500:
            break
        windows.append(block)
        locations.append(location)
        used += len(block) + 2
    if windows:
        warning = 'Some chunk spans could not be revalidated' if invalid_span else None
        return '\n\n'.join(windows), '; '.join(locations) + f' @sha256:{digest}', warning
    return snippet, locator + f' @sha256:{digest}', 'Chunk span could not be revalidated; current source window used'


def split_retrieval_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """Separate known ranking-policy information; unfamiliar notes remain degradations."""
    information, degradations = [], []
    for note in notes:
        if note.startswith(("VECTOR_LAKE_AUTHOR_FACET=boost:", "VECTOR_LAKE_AUTHOR_FACET=filter:",
                            "External embedding disabled by request policy", "Retrieval profile:",
                            "Chunk fusion:", "Chunk candidates excluded by current source metadata:",
                            "Event year unverified", "Event query constraints:",
                            "Page vector path had no eligible matches; chunk path evaluated separately")) or re.match(
            r"^(?:FTS|vector): \d+ ranked candidate\(s\) matched but none passed the domain/cluster/history/filter selectors ", note
        ):
            information.append(note)
        else:
            degradations.append(note)
    return information, degradations


@runtime.request_entry
def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str | None = None, cluster: str | None = None, include_history: bool = False, mode: str = "page", filter_expr: str | None = None, allow_external_embedding: bool = False, profile: str = 'auto', use_chunks: bool = False):
    if top_k <= 0:
        raise ValueError('top_k must be positive')
    from vector_lake.retrieval_plan import PROFILES
    if profile not in PROFILES:
        raise ValueError('Unknown retrieval profile')
    normalized_mode = str(mode or "page").lower()
    if normalized_mode != 'page' and (use_chunks or profile not in {'auto', 'legacy'}):
        raise ValueError('Profiles and chunk retrieval apply to page mode only')
    if normalized_mode not in {"page", "memory", "operational-memory", "operational_memory", "claim", "claims"}:
        raise ValueError('mode must be page, memory or claim')
    if normalized_mode in {"memory", "operational-memory", "operational_memory"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history, domain=domain, cluster=cluster)
    if normalized_mode in {"claim", "claims"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history, memory_types=["fact"], domain=domain, cluster=cluster)

    wiki_dir = str(get_wiki_dir())
    final_scored, vector_notes, error = _search_scored_pages(
        query,
        top_k=top_k,
        domain=domain,
        cluster=cluster,
        include_history=include_history,
        filter_expr=filter_expr,
        allow_external_embedding=allow_external_embedding,
        profile=profile, use_chunks=use_chunks,
    )
    if error:
        return error

    result = ""
    page_notes = []
    information, degradations = split_retrieval_notes(vector_notes)
    for label, notes in (("DEGRADED", degradations), ("INFO", information)):
        if notes:
            result += f"[{label}] Retrieval notes: " + "; ".join(notes) + "\n\n"
    if not final_scored:
        if degradations:
            return result + '[STATUS] failed\nRetrieval failed; no absence conclusion is available.'
        return result + "No matching pages."
    state = runtime.current()
    if state is not None:
        state.emitted_keys = set()
    for index, (score, node) in enumerate(final_scored):
        filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
        snippet = ""
        locator = "summary"
        if os.path.exists(filepath):
            try:
                content = runtime.read_source(Path(filepath), root=Path(wiki_dir))
                snippet, locator, source_note = _evidence_for_node(content, query, node)
                if source_note:
                    page_notes.append(f"{node['_key']}.md: {source_note}")
                    if state is not None:
                        state.problem(source_note)
            except (OSError, UnicodeError, ValueError, runtime.RetrievalBudgetExceeded) as exc:
                snippet = str(node.get("summary") or "")[:2500]
                locator = "index-summary"
                page_notes.append(f"{node['_key']}.md: {runtime.failed_stage('Source evidence', exc)}; original page text unavailable")
        else:
            snippet = str(node.get("summary") or "")
            
        tension_edges = node.get("tension_edges", [])
        tension_info = ""
        if tension_edges:
            tension_info = "  [Tension Edges]:\n"
            for te in tension_edges[:12]:
                tension_info += f"    -> {str(te.get('target'))[:160]} (Polarity: {te.get('polarity')}, Intensity: {te.get('intensity')}): {str(te.get('context'))[:256]}\n"
                
        if as_xml:
            block = (f"<Evidence_Node ID='Wiki_{index}' "
                       f"Source='{escape(node['_key'], quote=True)}.md' "
                       f"Locator='{escape(locator, quote=True)}'>\n"
                       f"{escape(tension_info + snippet)}\n</Evidence_Node>\n")
        else:
            block = f"- **{str(node.get('title', node['_key']))[:512]}** (score: {score:.3f}) [source: {node['_key']}.md:{locator}]\n{tension_info}  {snippet}...\n\n"
        if len(result) + len(block) > runtime.MAX_CONTEXT_CHARS - 2000:
            if state is not None:
                state.problem('Response character budget exhausted')
            page_notes.append('Response character budget exhausted')
            break
        result += block
        if state is not None and state.emitted_keys is not None:
            state.emitted_keys.add(node['_key'])
    if page_notes:
        result = "[DEGRADED] Source text notes: " + "; ".join(page_notes) + "\n\n" + result
    return result


@runtime.request_entry
def assemble_context(
    query: str, max_chars: int = DEFAULT_MAX_CHARS, *, domain: str | None = None,
    cluster: str | None = None, allow_external_embedding: bool = False,
    profile: str = 'legacy', use_chunks: bool = False,
) -> dict:
    """Assemble a budget-bounded reasoning context for a query.

    Pages come from ``_search_scored_pages`` directly.  This function previously
    re-parsed the formatted search string with a regex, which produced an empty
    wiki context whenever the formatting drifted.  The returned budget is now
    enforced: ``budget_used <= budget_max`` always holds.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    index_budget = int(max_chars * BUDGET_SHARES["index_summary"])

    # The nominal share and the burst ceiling.  Memory used to take a hardcoded 0.50 of *every*
    # budget, so the declared ``operational_memory`` share was decorative and the alert condition was
    # never tested; the two-step shape kept that honest by rebuilding at the burst ceiling when the
    # packet actually reported alerts.
    #
    # The steps run burst-first because the comment this replaces was wrong about which case is rare:
    # measured 2026-09-25, ``assemble_context`` built the packet **1.76 times per query**, so alerts
    # are the common case and the build that was being thrown away was the *nominal* one -- ~0.8 s per
    # query spent on a retrieval whose result was discarded.  Building at the ceiling first and
    # shrinking only when there is nothing to warn about produces the identical packet (the same
    # budget is used in each branch) and pays the second retrieval in the rare case instead.
    nominal_budget = int(max_chars * BUDGET_SHARES["operational_memory"])
    burst_budget = int(max_chars * BUDGET_SHARES["memory_burst"])
    scope = {"domain": domain, "cluster": cluster} if domain or cluster else {}
    memory_notes = []
    try:
        runtime.checkpoint()
        request_views = governance_store.search_memory_packet_views(query, domain=domain, cluster=cluster) if scope else None
        packet_options = {**scope, "_views": request_views} if scope else {}
        memory_packet = build_memory_packet(
            query, max_chars=burst_budget if burst_budget > nominal_budget else nominal_budget,
            **packet_options,
        )
        if not memory_packet["warning_count"] and burst_budget > nominal_budget:
            memory_packet = build_memory_packet(query, max_chars=nominal_budget, **packet_options)
    except Exception as exc:
        message = runtime.failed_stage('Memory', exc)
        memory_notes.append(message)
        packet = '<MEMORY_PACKET>\n[DEGRADED] ' + message + '\n</MEMORY_PACKET>'
        memory_packet = {'packet': packet if len(packet) <= nominal_budget else '', 'memory_count': 0,
                         'warning_count': 0, 'omitted_count': 0, 'is_degraded': True}
    actual_memory_used = len(memory_packet["packet"])

    purpose = ""
    try:
        from vector_lake.purpose_contract import render_strategy_directive

        purpose = render_strategy_directive() if not scope else ""
    except Exception as exc:
        log.warning("Strategy directive unavailable for context assembly: %s", runtime.safe_error(exc))
    purpose_budget = int(max_chars * BUDGET_SHARES["system_prompt"])
    purpose = purpose[:purpose_budget]

    from vector_lake import page_index_projection

    # Resolved once and handed to the scoring pass, so one request parses
    # index.json at most once even when the projection is behind.
    catalog, projection_note = page_index_projection.read_catalog()
    scored_pages, vector_notes, retrieval_error = _search_scored_pages(
        query, top_k=15, catalog=catalog, projection_note=projection_note,
        domain=domain, cluster=cluster, allow_external_embedding=allow_external_embedding,
        profile=profile, use_chunks=use_chunks,
    )
    vector_notes.extend(memory_notes)

    # The index summary is materialised *before* the wiki budget so the wiki gets whatever the
    # summary did not use.  Charging the wiki budget for the index's *allocation* rather than its
    # actual size lost the difference on every request whose catalog was small.
    index_summary = ""
    if os.path.exists(str(get_index_path())):
        try:
            if catalog is None:
                index_summary = "[Index read failed]"
            else:
                if scope:
                    index_summary = "\n".join(
                        f"[{node.get('type') or '?'}] {node.get('title') or node['_key']}"
                        for _, node in scored_pages
                        if _passes_filters(node, domain, cluster, False, None)
                    )[:index_budget]
                else:
                    index_summary = "\n".join(catalog.node_summary_lines(50))[:index_budget]
                # The projection note is not appended here: ``_search_scored_pages`` already
                # returned it in ``vector_notes`` from the same ``projection_note`` this function
                # handed it, and appending it a second time made every degraded answer report the
                # same fallback twice.
        except Exception as exc:
            index_summary = "[Index read failed]"
            log.error("Index summary unavailable: %s", runtime.safe_error(exc))

    # Wiki dynamically eats what memory, the index summary and the purpose left.
    wiki_budget = max(0, max_chars - actual_memory_used - len(index_summary) - len(purpose))

    wiki_dir = str(get_wiki_dir())
    wiki_blocks: list[str] = []
    page_notes = []
    wiki_used = 0
    page_count = 0
    wiki_spans = []
    author_keys = author_facet.author_page_keys() if author_facet.annotate_enabled() and not runtime.expired() else frozenset()
    state = runtime.current()
    if state is not None:
        state.emitted_keys = set()
    for score, node in scored_pages:
        if wiki_used >= wiki_budget:
            break
        key = node["_key"]
        filepath = os.path.join(wiki_dir, f"{key}.md")
        snippet = ""
        locator = "summary"
        if runtime.expired():
            snippet = str(node.get('summary') or '')[:2500]
            locator = 'index-summary'
            page_notes.append(f'{key}.md: request deadline exhausted; retained metadata summary only, no new source read')
        else:
            try:
                content = runtime.read_source(Path(filepath), root=Path(wiki_dir))
                snippet, locator, source_note = _evidence_for_node(content, query, node)
                if source_note:
                    page_notes.append(f'{key}.md: {source_note}')
                    if state is not None:
                        state.problem(source_note)
            except (OSError, UnicodeError, ValueError, runtime.RetrievalBudgetExceeded) as exc:
                snippet = str(node.get("summary") or "")[:2500]
                locator = 'index-summary'
                page_notes.append(f'{key}.md: {runtime.failed_stage("Source evidence", exc)}; original page text unavailable')
        # An authorship marker, never a filter: the reader can tell the author's own writing from
        # writing about him, which no amount of page text says (131 of 149 author pages never name
        # him).  It does not change which pages are retrieved.
        marker = " `[author]`" if key in author_keys else ""
        block = f"- **{str(node.get('title', key))[:512]}**{marker} (score: {score:.3f}; source: {key}.md:{locator})\n  {snippet}\n\n"
        if wiki_used + len(block) > wiki_budget:
            break
        wiki_spans.append((key, wiki_used))
        wiki_blocks.append(block)
        wiki_used += len(block)
        page_count += 1
        if state is not None and state.emitted_keys is not None:
            state.emitted_keys.add(key)
    wiki_context = "".join(wiki_blocks)

    # ``purpose`` is the last claimant on the budget and must not overflow it.  It was already
    # capped at its own share above; this is the invariant's backstop.
    remaining = max_chars - (len(memory_packet["packet"]) + len(wiki_context) + len(index_summary))
    purpose = purpose[:max(0, min(purpose_budget, remaining))]

    budget_used = len(memory_packet["packet"]) + len(wiki_context) + len(index_summary) + len(purpose)
    return {
        "memory_packet": memory_packet["packet"],
        "memory_count": memory_packet["memory_count"],
        "memory_warning_count": memory_packet["warning_count"],
        "memory_omitted_count": memory_packet["omitted_count"],
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "_wiki_spans": wiki_spans,
        "index_summary": index_summary,
        "purpose": purpose,
        "retrieval_notes": list(vector_notes or []) + page_notes + ([retrieval_error] if retrieval_error else []),
        "budget_used": budget_used,
        "budget_max": max_chars,
    }
