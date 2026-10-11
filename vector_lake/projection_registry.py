"""One place that answers, for every derived projection: is it healthy, and what repairs it?

Why this exists: on 2026-09-25 five separate incidents landed on the same root cause -- the lake keeps
a *lot* of derived indices (FTS5, the operational-memory gram index, the vector projection, tantivy,
the page projection, the claim index, the timeline index, governance), and each one had its own idea
of what "healthy" means, its own repair entry point, and its own way of degrading.  The symptoms were
all log lines: a search quietly taking the exact projected scan for 13 hours, 501 missing vectors
after a mass edit, an FTS switch that was inert on the path that mattered.

This module does not add a ninth mechanism.  It gives the existing health checks and repair entry
points **one shape**: ``Projection(name, authority, cost, health, repair)``, plus ``status()`` and
``reconcile()`` over them.  A projection's health is a fact with a detail string, not a sentence in a
log; its repair is a callable, not a script name in a comment.

Pilots registered here (2026-09-25): the operational-memory gram index and the vector projection --
the two this session had to repair by hand.  The remaining projections are **not migrated yet**;
until they are, this is a reporting and reconciliation surface for those two, and the honest next step
is one projection per change with its own test.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

log = logging.getLogger("vector-lake-projections")

HEALTHY = "healthy"
DEGRADED = "degraded"


@dataclass(frozen=True)
class Projection:
    """A derived index: what it is derived from, how to tell it is behind, how to fix it."""

    name: str
    #: The sovereign thing it is derived from -- the answer to "what must I trust if they disagree".
    authority: str
    #: What a repair costs, in the units the cost was actually measured in.
    cost: str
    #: ``() -> (state, detail)`` with state in {HEALTHY, DEGRADED}.
    health: Callable[[], tuple[str, str]]
    #: ``() -> str``.  Idempotent and safe to call when nothing is due.  ``None`` means there is no
    #: routine automatic repair and ``manual_entry`` names what a human runs instead -- an entry with
    #: no repair is a statement about the system, not a hole to be filled with a guess.
    repair: Callable[[], str] | None
    #: Which reader degrades when this is unhealthy, so the impact is not a guess.
    degrades: str
    #: What to run when ``repair`` is ``None``.
    manual_entry: str = ""

    @property
    def repairable(self) -> bool:
        return self.repair is not None


def _gram_health() -> tuple[str, str]:
    from vector_lake import memory_gram_index

    try:
        if memory_gram_index.gram_index_usable():
            return HEALTHY, "indexed path serving"
        _total, live, _retired = memory_gram_index.dirty_breakdown()
        return DEGRADED, f"{live} live dirty document(s); memory retrieval on the exact scan"
    except Exception as exc:  # noqa: BLE001 - an unreadable projection is not a healthy one
        return DEGRADED, f"unreadable: {type(exc).__name__}: {exc}"


def _gram_repair() -> str:
    from vector_lake import memory_gram_index

    return memory_gram_index.maybe_rebuild_memory_gram_index()


def _fts_repair() -> str:
    from vector_lake.tool_projection import repair_search_projection

    return repair_search_projection(dry_run=False)


def _claim_index_repair() -> str:
    from vector_lake import db_store

    return f"claim_index reconciled: {db_store.ensure_claim_index(force=False)}"


def _tantivy_repair() -> str:
    from vector_lake import tantivy_index

    try:
        docs = tantivy_index.rebuild_from_sqlite()
    except tantivy_index.MirrorReplayDeferred as exc:
        # The registry's 'repaired' action is reserved for completed repairs.
        raise tantivy_index.MirrorReplayDeferred(
            f"rebuild scheduled, not completed; continue via replay_pending: {exc}"
        ) from exc
    return f"rebuilt {docs} document(s) from the FTS5 table"


def _vector_health() -> tuple[str, str]:
    from vector_lake import db_store

    conn = db_store.get_connection()
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM page_index_nodes) AS nodes,"
        " (SELECT COUNT(*) FROM vec_embeddings) AS vectors,"
        " (SELECT COUNT(*) FROM vec_embeddings e WHERE NOT EXISTS ("
        "     SELECT 1 FROM vec_embedding_inputs i WHERE i.node_key = e.page_key)) AS unstamped"
    ).fetchone()
    nodes, vectors, unstamped = int(row["nodes"]), int(row["vectors"]), int(row["unstamped"])
    missing = max(0, nodes - vectors)
    if missing or unstamped:
        return DEGRADED, (
            f"{vectors}/{nodes} vectors, {missing} missing, {unstamped} unstamped "
            "(the vector half of hybrid retrieval is short on those pages)"
        )
    return HEALTHY, f"{vectors}/{nodes} vectors, 0 missing, 0 unstamped"


def _vector_repair() -> str:
    from vector_lake.periodic_catch_up import embedding_catch_up

    summary = embedding_catch_up()
    return (
        f"embedded {summary.get('embedded', 0)} of {summary.get('candidates', 0)} candidate(s)"
        + (f" (skipped: {summary['skipped']})" if summary.get("skipped") else "")
    )


_COUNT_SCOPE: ContextVar[dict | None] = ContextVar("projection_count_scope", default=None)


def _counts(conn) -> dict:
    """Share successful count reads only within one synchronous status call.

    Retain the connection in the entry to prevent id reuse; return a copy so
    one health callback cannot mutate another's report. Failed reads propagate
    unchanged and are never converted to an empty or healthy result.
    """
    scope = _COUNT_SCOPE.get()
    if scope is None:
        return _read_counts(conn)
    key = id(conn)
    if key not in scope:
        scope[key] = (conn, _read_counts(conn))
    return dict(scope[key][1])


def _read_counts(conn) -> dict:
    """Every signal the count-based pilots need, each one measured cheap (total ~60 ms).

    **What is deliberately not here:** comparing ``wiki_search_index``'s row keys against
    ``page_index_nodes`` as an anti-join.  Measured 2026-09-25 on the live lake: it did not finish in
    90 s, against 46 ms for ``COUNT(*) FROM wiki_search_index`` -- an FTS5 table cannot serve
    ``node_key = ?``, so each of the 7 139 probes scans it.  That anti-join is what made the first
    version of this function take **271 s**, which is not a health probe, it is an outage; FTS
    coverage is therefore reported as a *count shortfall*, and naming the specific pages is left to
    the rebuild path.  The other two anti-joins (claims/claim_index 35 ms, timeline/claims 10 ms) and
    the unstamped-vector one (16 ms) are indexed and kept.
    """
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM page_index_nodes) AS nodes,"
        " (SELECT COUNT(*) FROM page_index_edges) AS edges,"
        " (SELECT COUNT(*) FROM wiki_search_index) AS fts_rows,"
        " (SELECT COUNT(*) FROM claims) AS claims,"
        " (SELECT COUNT(*) FROM claim_index) AS claim_index_rows,"
        " (SELECT COUNT(*) FROM timeline_events) AS timeline_events,"
        " (SELECT COUNT(*) FROM claims c WHERE NOT EXISTS ("
        "     SELECT 1 FROM claim_index i WHERE i.claim_id = c.claim_id)) AS claims_unindexed,"
        " (SELECT COUNT(*) FROM timeline_events e WHERE e.claim_id IS NOT NULL AND NOT EXISTS ("
        "     SELECT 1 FROM claims c WHERE c.claim_id = e.claim_id)) AS timeline_orphans,"
        " (SELECT COUNT(*) FROM governance_queue) AS governance_items,"
        " (SELECT COUNT(*) FROM mutation_outbox WHERE status IN ('pending','processing','retrying'))"
        " AS outbox_lag"
    ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def _page_projection_health() -> tuple[str, str]:
    from vector_lake import db_store

    counts = _counts(db_store.get_connection())
    if counts["outbox_lag"]:
        return DEGRADED, (
            f"{counts['outbox_lag']} durable mutation(s) not yet materialised "
            f"(index.json holds {counts['nodes']} node(s), {counts['edges']} edge(s))"
        )
    return HEALTHY, f"index.json current: {counts['nodes']} node(s), {counts['edges']} edge(s)"


def _page_projection_repair() -> str:
    from vector_lake.watchdog_app import process_mutation_outbox_batch

    stats = process_mutation_outbox_batch(limit=200)
    return f"drained outbox: {stats}"


def _fts_health() -> tuple[str, str]:
    from vector_lake import db_store

    counts = _counts(db_store.get_connection())
    if counts["fts_rows"] < counts["nodes"]:
        return DEGRADED, (
            f"{counts['nodes'] - counts['fts_rows']} row(s) short of {counts['nodes']} page(s) "
            "(the lexical half of hybrid retrieval cannot see them)"
        )
    return HEALTHY, f"{counts['fts_rows']}/{counts['nodes']} page(s) indexed"


def _tantivy_health() -> tuple[str, str]:
    import os

    from vector_lake import db_store, tantivy_index

    if not tantivy_index.enabled():
        if os.environ.get("VECTOR_LAKE_FTS", "fts5").strip().lower() == "tantivy":
            return DEGRADED, "Tantivy requested but unavailable; FTS5 serving"
        return HEALTHY, "not in use (VECTOR_LAKE_FTS=fts5); FTS5 serving"
    try:
        state = db_store.tantivy_mirror_state()
        detail = (
            f"phase={state['phase']}, pending={state['pending']}, blocked={state['blocked']}, "
            f"control_attempts={state['attempt_count']}"
        )
        if state["last_error"]:
            detail += f"; control error: {state['last_error']}"
        queue_error = db_store.get_connection().execute(
            "SELECT last_error FROM tantivy_mirror_queue WHERE last_error IS NOT NULL LIMIT 1"
        ).fetchone()
        if queue_error is not None:
            detail += f"; queue error: {queue_error['last_error']}"
        if tantivy_index.current_token() is None:
            return DEGRADED, detail + "; mirror not validated; FTS5 serving"
        return HEALTHY, detail + "; mirror current (validated token/incarnation)"
    except Exception as exc:  # noqa: BLE001 - unreadable is not healthy or empty
        return DEGRADED, f"mirror error: {type(exc).__name__}: {exc}; FTS5 serving"


def _claim_index_health() -> tuple[str, str]:
    from vector_lake import db_store

    counts = _counts(db_store.get_connection())
    if counts["claims_unindexed"] or counts["claim_index_rows"] < counts["claims"]:
        short = max(counts["claims_unindexed"], counts["claims"] - counts["claim_index_rows"])
        return DEGRADED, (
            f"{short} of {counts['claims']} claim(s) not in claim_index "
            "(claim search cannot find them)"
        )
    return HEALTHY, f"{counts['claims']} claim(s) indexed"


def _timeline_health() -> tuple[str, str]:
    from vector_lake import db_store

    counts = _counts(db_store.get_connection())
    if counts["timeline_orphans"]:
        return DEGRADED, (
            f"{counts['timeline_orphans']} timeline event(s) name a claim that no longer exists "
            f"(of {counts['timeline_events']} event(s))"
        )
    return HEALTHY, f"{counts['timeline_events']} event(s) over {counts['claims']} claim(s)"


def _timeline_repair() -> str:
    from vector_lake import tool_timeline

    return tool_timeline.rebuild_timeline_events_from_claims(dry_run=False)


def _governance_health() -> tuple[str, str]:
    from vector_lake import db_store

    counts = _counts(db_store.get_connection())
    return HEALTHY, (
        f"{counts['governance_items']} item(s) queued for a human; state lives inside item data_json"
    )


PILOTS: tuple[Projection, ...] = (
    Projection(
        name="memory_gram",
        authority="operational_memory_index",
        cost="~77 s for 69 929 documents (measured 2026-09-25: 58.0 stage + 16.5 pack + 2.0 publish)",
        health=_gram_health,
        repair=_gram_repair,
        degrades="search_vector_lake[memory] and assemble_context (1 240 ms instead of 782 ms)",
    ),
    Projection(
        name="vectors",
        authority="page_index_nodes",
        cost="bounded per sweep (the catch-up embedding budget, default 120 s)",
        health=_vector_health,
        repair=_vector_repair,
        degrades="the vector half of hybrid retrieval for the pages that are missing one",
    ),
    Projection(
        name="page_projection",
        authority="SQLite canonical state (page_index_nodes / mutation_outbox)",
        cost="one 200-row drain per reconcile (seconds); a full rebuild is `cli.py projection-rebuild`",
        health=_page_projection_health,
        repair=_page_projection_repair,
        degrades="readers fall back to the raw index.json for anything the outbox has not materialised",
    ),
    Projection(
        name="fts_index",
        authority="page_index_nodes",
        cost="incremental: content-hash ledger, one transaction per changed node (near-free when current)",
        health=_fts_health,
        repair=_fts_repair,
        degrades="the lexical half of hybrid retrieval for the pages that have no row",
    ),
    Projection(
        name="tantivy_mirror",
        authority="wiki_search_index (the FTS5 projection stays authoritative)",
        cost="~8 s to rebuild from the FTS5 table alone (measured 2026-09-25: 7 139 documents)",
        health=_tantivy_health,
        repair=_tantivy_repair,
        degrades="nothing while VECTOR_LAKE_FTS=fts5; otherwise the lexical half",
    ),
    Projection(
        name="claim_index",
        authority="claims",
        cost="incremental gap-fill from claims (`ensure_claim_index`); `force` re-derives every row",
        health=_claim_index_health,
        repair=_claim_index_repair,
        degrades="claim search for the unindexed claims",
    ),
    Projection(
        name="timeline_events",
        authority="claims",
        cost="rebuild from claims, bounded by `limit`",
        health=_timeline_health,
        repair=_timeline_repair,
        degrades="timeline queries that point at removed claims",
    ),
    Projection(
        name="governance_queue",
        authority="(a human queue, not derived from the corpus)",
        cost="no automatic repair by design: resolving an item is a judgement, not a rebuild",
        health=_governance_health,
        repair=None,
        degrades="nothing mechanically; unresolved items are knowledge debt",
        manual_entry="`cli.py debt` to inspect, `cli.py resolve` / the review skills to close items",
    ),
)


def registry() -> tuple[Projection, ...]:
    return PILOTS


def status() -> dict[str, dict]:
    """Every projection's state and detail; read-only, with call-local counts.

    No process-lifetime cache: another status or a repair must read again.
    Nested status calls restore the parent's scope even on failure.
    """
    token = _COUNT_SCOPE.set({})
    try:
        return _status_report()
    finally:
        _COUNT_SCOPE.reset(token)


def _status_report() -> dict[str, dict]:
    report: dict[str, dict] = {}
    for projection in registry():
        try:
            state, detail = projection.health()
        except Exception as exc:  # noqa: BLE001 - a probe must not take the caller down
            state, detail = DEGRADED, f"probe failed: {type(exc).__name__}: {exc}"
        report[projection.name] = {
            "state": state,
            "detail": detail,
            "authority": projection.authority,
            "cost": projection.cost,
            "degrades": projection.degrades,
            "repairable": projection.repairable,
            "manual_entry": projection.manual_entry,
        }
    return report


def reconcile(only: str | None = None, dry_run: bool = False) -> dict[str, dict]:
    """Repair every unhealthy projection (or one named one), containing each failure.

    Containment is the point: one projection failing to repair must not stop the others, which is
    what the ad-hoc halves in ``periodic_catch_up`` each had to implement separately.  A projection
    with ``repair is None`` is reported as ``manual`` with its entry point -- never silently skipped.
    """
    results: dict[str, dict] = {}
    for projection in registry():
        if only and projection.name != only:
            continue
        state, detail = projection.health()
        if state == HEALTHY:
            results[projection.name] = {"action": "none", "state": state, "detail": detail}
            continue
        if projection.repair is None:
            results[projection.name] = {
                "action": "manual", "state": state, "detail": detail,
                "entry": projection.manual_entry or "(no entry point recorded)",
            }
            continue
        if dry_run:
            results[projection.name] = {"action": "would-repair", "state": state, "detail": detail}
            continue
        try:
            results[projection.name] = {
                "action": "repaired", "state": state, "detail": detail,
                "result": str(projection.repair())[:400],
            }
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning("Repair of projection %s failed: %s: %s", projection.name, type(exc).__name__, exc)
            results[projection.name] = {
                "action": "failed", "state": state, "detail": detail,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


def status_line() -> str:
    """One compact line for a status surface, e.g. ``memory_gram=healthy vectors=degraded(0/7139…)``."""
    parts = []
    for name, entry in status().items():
        if entry["state"] == HEALTHY:
            parts.append(f"{name}=healthy")
        else:
            parts.append(f"{name}=degraded({entry['detail'][:60]})")
    return " ".join(parts)
