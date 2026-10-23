"""SQLite projection of ``index.json`` for the read paths.

``index.json`` stays the sovereign, portable artifact: it is what an operator can
inspect, diff and ship.  But it is one document, so every reader had to parse the
whole thing and rebuild derived structures from scratch:

* ``_search_scored_pages`` parsed the file once per process (18 MB / 0.2 s on the
  live 7 178-node corpus, and the parsed dict is a much larger object graph), and
  rebuilt a 30 140-edge undirected adjacency dict on *every* query;
* ``assemble_context`` re-parsed the whole file to render a 50-line summary;
* at 10^5 nodes the file is the dominant read cost and the in-memory dict is the
  dominant memory cost.

This module projects what the read paths consume into SQLite:

``page_index_nodes``
    One row per node: the filter columns as real columns plus ``node_json`` for
    full fidelity, because ``filter_expr`` is a caller-supplied expression over
    arbitrary node fields.  Read paths fetch only the keys they actually need, so
    the 7 178-node dict never has to exist in memory.

``page_index_edges``
    ``weighted_edges`` with an explicit ``sequence`` column.  It is the only database
    projection of that set -- a second table used to mirror it, was removed on 2026-09-18 because
    nothing read it -- and it derives from the published file, which is the source.  The sequence
    preserves ``index.json``'s edge
    order, which the two-step personalised PageRank walk is sensitive to.
    The undirected adjacency is built once per process and reused.

``page_index_state``
    ``(index_mtime, index_size)`` of the ``index.json`` this projection was built
    from, so an out-of-band rewrite is detected with one ``stat`` instead of a
    parse, and the projection repairs itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from types import MappingProxyType

from vector_lake.db_store import get_connection, get_db_path, init_db, read_snapshot, transaction
from vector_lake.link_resolution import build_link_map, declared_names_from_nodes, resolve_link_target
from vector_lake.wiki_utils import get_index_path
from vector_lake.schema_validator import canonical_domain
from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger("vector-lake-page-index")

ADJACENCY_CACHE_LIMIT = 1
SCOPED_ADJACENCY_CACHE_LIMIT = 8
SCOPED_ADJACENCY_REFERENCE_LIMIT = 200_000

ScopedAdjacency = Mapping[str, tuple[tuple[str, float], ...]]

# ``link_target_resolver`` memo.  Keyed on the two index-only numbers below, so a per-page
# caller pays one build per index change, not one per page.
_LINK_RESOLVER_CACHE: dict = {"fingerprint": None, "resolver": None}


def link_target_resolver():
    """``resolve(name) -> page key | None`` over the published node index, or ``None``.

    Who answers "which page does this link name" is ``link_resolution``; this is the adapter that
    hands it the index it resolves against.  It exists because the two producers of edges had two
    rules: the indexer resolved through ``link_resolution``, while ``claim_extractor`` stored the
    link's **literal text** as the edge target -- the "one question, two answers" defect the
    resolution module was created to end, still live on the claim-graph side.  Live measurement of
    where that left the corpus: 232 of 10 487 ``claim_graph_edges`` rows had an endpoint no page
    answers, 230 of which the resolver could answer (``Concept_CoMET`` -> ``Product_CoMET``).

    Memoised against the node projection's row count and highest rowid -- both index-only reads,
    0.2 ms warm on the live corpus -- so a 7 000-page batch pays one 0.20 s build instead of 7 000.

    Returns ``None`` when the projection is empty, absent, or unreadable.  A caller must then keep
    the target as written: no index means no answer, and guessing one would invent a page key.
    """
    # ``get_connection`` *creates* the database file when it is absent, and this is called from
    # the extractor, which runs on dry-run paths too -- ``migrate_existing_wiki(dry_run=True)``
    # asserts that no SQLite file appears, and it did.  A database that does not exist has no
    # index to answer with, so the guard is also the honest answer: keep the target as written.
    if not get_db_path().exists():
        return None
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM page_index_nodes"
        ).fetchone()
    except sqlite3.Error as exc:
        log.debug("No node index to resolve link targets against: %s", exc)
        return None
    count, highest = int(row[0]), int(row[1])
    if not count:
        return None
    # The database identity is part of the key, not only the row numbers: two isolated corpora
    # (tests, a backup being inspected) can both hold one node, and a memo keyed on the numbers
    # alone would answer one with the other's index.  Same reason ``tool_timeline``'s parity
    # fingerprint carries the path.
    fingerprint = (str(get_db_path()), count, highest)
    if _LINK_RESOLVER_CACHE["fingerprint"] == fingerprint:
        return _LINK_RESOLVER_CACHE["resolver"]

    nodes: dict[str, dict] = {}
    for node in conn.execute("SELECT node_key, node_json FROM page_index_nodes"):
        try:
            payload = json.loads(node["node_json"])
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        nodes[str(node["node_key"])] = {
            "title": payload.get("title"),
            "aliases": payload.get("aliases") or [],
        }
    link_map, _core_pages, unique_cores, _contested = build_link_map(
        nodes.keys(), declared_names_from_nodes(nodes)
    )
    resolver = lambda target: resolve_link_target(str(target), link_map, unique_cores)  # noqa: E731
    _LINK_RESOLVER_CACHE["fingerprint"] = fingerprint
    _LINK_RESOLVER_CACHE["resolver"] = resolver
    return resolver

def index_file_stamp() -> tuple[float, int] | None:
    path = get_index_path()
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime, stat.st_size)


# --- writer-reported staleness ----------------------------------------------
#
# ``indexer._write_index`` refreshes this projection right after it rewrites
# ``index.json``.  When that refresh loses the write lock, the failure used to be
# swallowed as a log line, so the next reader silently paid for the rebuild -- and
# was the only party who could report it.  The marker records the failure where
# the reader can see it: it travels with the database, is written with an atomic
# replace, and never blocks, because the situation it describes is precisely the
# one where the database is unavailable to us.

STALE_MARKER_NAME = "page_index_projection.stale.json"


def stale_marker_path():
    return get_meta_dir() / STALE_MARKER_NAME


def projection_stale() -> dict | None:
    """The last writer-reported refresh failure, or ``None`` when there is none."""
    path = stale_marker_path()
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def mark_projection_stale(reason: str) -> None:
    """Record that a writer could not refresh the projection.  Never raises."""
    path = stale_marker_path()
    payload = {"reason": str(reason)[:500], "at": _utc_now()}
    stamp = index_file_stamp()
    if stamp is not None:
        payload["index_stamp"] = [stamp[0], stamp[1]]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        log.warning("Could not record projection staleness at %s: %s", path, exc)


def clear_projection_stale() -> None:
    """Drop the marker once the projection is known to be in step again."""
    path = stale_marker_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear projection staleness at %s: %s", path, exc)


def projection_state() -> dict | None:
    """The recorded stamp row, or ``None`` when the projection is absent.

    Tolerates a missing ``page_index_state`` table: readers run before
    ``init_db`` can be shown to be necessary, and a fresh database has no
    projection yet.  Treating that as "not current" sends the caller down the
    rebuild path; raising here would turn a cold lake into a hard error.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT index_mtime, index_size, node_count, edge_count, updated_at, edge_digest "
            "FROM page_index_state WHERE singleton = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # A database last initialised by an older build has no digest column yet;
        # it arrives with the next ``init_db``.  Until then the refresh path falls
        # back to rewriting edges, which is the behaviour that build had.
        try:
            row = conn.execute(
                "SELECT index_mtime, index_size, node_count, edge_count, updated_at "
                "FROM page_index_state WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return dict(row) if row is not None else None


def projection_is_current() -> bool:
    stamp = index_file_stamp()
    if stamp is None:
        return False
    state = projection_state()
    if state is None:
        return False
    return (float(state["index_mtime"]), int(state["index_size"])) == stamp


def _node_columns(key: str, node: dict) -> tuple:
    """Full column tuple, including ``node_key`` (which is the dict key, not ``node['id']``)."""
    return (
        str(key),
        str(node.get("id") or ""),
        str(node.get("title") or ""),
        str(node.get("type") or ""),
        str(node.get("status") or ""),
        str(node.get("domain") or ""),
        str(node.get("topic_cluster") or ""),
        json.dumps(node, ensure_ascii=False),
    )


def _node_insert() -> str:
    return (
        "INSERT OR REPLACE INTO page_index_nodes "
        "(node_key, node_id, title, type, status, domain, topic_cluster, node_json) "
        "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS ("
        "SELECT 1 FROM page_index_nodes WHERE node_key IS ? AND node_id IS ? "
        "AND title IS ? AND type IS ? AND status IS ? AND domain IS ? "
        "AND topic_cluster IS ? AND node_json IS ?)"
    )


def _weighted_edges_digest(index_data: dict) -> str | None:
    """Content digest of ``weighted_edges``; ``None`` when the key is absent.

    The stamp on ``page_index_state`` is ``(mtime, size)``, which changes on every
    ``index.json`` rewrite -- including the many rewrites that leave the topology
    untouched (a page edit with no link change, a governance pass, a decay tick).
    Without a content check every one of those rewrites re-projected all 1.6 M
    edge rows while holding the write lock.

    ``json.dumps`` then one hash, rather than a Python loop over the edges: on the
    live corpus this measures 1.58 s (1.36 s dump + 0.22 s blake2b) against a
    measured 2.92 s for the bare DELETE+INSERT on an empty database, and the
    rewrite additionally holds ``BEGIN IMMEDIATE`` while updating two indexes on a
    2.3 GB database.  The cost moved off the lock and the locked work is skipped.

    The digest is over the edge order too, so it can only ever answer "identical",
    never "equivalent": the PageRank walk depends on that order.
    """
    if "weighted_edges" not in index_data:
        return None
    edges = index_data.get("weighted_edges") or []
    blob = json.dumps(edges, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def _edge_digest_supported(conn) -> bool:
    """Whether ``page_index_state`` carries the digest column yet.

    Probed per call rather than assumed: ``refresh_page_index_projection`` is also
    reached from the indexer's write path, and a database last initialised by an
    older build has not run the ``ALTER`` that adds the column.
    """
    try:
        return "edge_digest" in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(page_index_state)")
        }
    except sqlite3.Error:
        return False


def _stored_edge_state(conn):
    """``(edge_count, edge_digest)`` as recorded, or ``None`` when unknown."""
    if not _edge_digest_supported(conn):
        return None
    try:
        row = conn.execute(
            "SELECT edge_count, edge_digest FROM page_index_state WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    return (row[0], row[1]) if row is not None else None


def refresh_page_index_projection(index_data: dict, node_keys=None) -> dict:
    """Project ``index_data`` into SQLite and record the index.json stamp.

    ``node_keys`` restricts the upsert to those nodes, which is what the indexer's
    partial-update path needs.  Nodes that disappeared still have to be removed,
    so that branch compares the projection against the authoritative key set
    rather than trusting the partial list.  The edge rows for a partial update are written by
    :func:`refresh_page_index_projection` itself, from the published file.
    """
    conn = get_connection()
    nodes = index_data.get("nodes") or {}
    partial_keys = None if node_keys is None else [str(key) for key in node_keys]
    if partial_keys is not None:
        projected = conn.execute("SELECT COUNT(*) FROM page_index_nodes").fetchone()[0]
        if projected != len(nodes):
            partial_keys = None  # a page was added or removed: refresh everything

    if partial_keys is None:
        rows = [_node_columns(str(key), node) for key, node in nodes.items()]
    else:
        rows = []
        for key in partial_keys:
            node = nodes.get(key)
            if node is None:
                continue
            rows.append(_node_columns(str(key), node))
    # Keep serialization outside the write transaction. Full refresh still
    # reconciles every authoritative column; only identical rows skip REPLACE.
    # Changed rows retain the existing REPLACE/rowid semantics and invalidate
    # the same readers as before, rather than adopting an in-place UPSERT.
    authoritative_keys = json.dumps(sorted(str(key) for key in nodes))
    # Retain the historical count of prepared/processed rows, not SQLite's
    # affected-row count, so callers need no result-schema change.
    written = len(rows)

    stamp = index_file_stamp()
    edge_changed = True
    for_edge_digest: str | None = None
    if "weighted_edges" not in index_data:
        edge_changed = False
    else:
        edges = index_data.get("weighted_edges") or []
        stored = _stored_edge_state(conn)
        # Only pay for the digest when an identical row count makes "unchanged"
        # possible; a different count is proof of change on its own.
        if stored is not None and stored[1] and int(stored[0] or 0) == len(edges):
            for_edge_digest = _weighted_edges_digest(index_data)
            edge_changed = for_edge_digest != stored[1]

    edge_rows = None
    if edge_changed and "weighted_edges" in index_data:
        edge_rows = [
            (position, str(edge.get("source") or ""), str(edge.get("target") or ""),
             float(edge.get("weight", 1.0) or 0.0))
            for position, edge in enumerate(index_data.get("weighted_edges") or [])
        ]
        if for_edge_digest is None:
            for_edge_digest = _weighted_edges_digest(index_data)
    # Scope membership, ordered edges and their generation must become visible
    # together. Separate commits could cache a new domain with the old graph.
    with transaction():
        if rows:
            conn.executemany(_node_insert(), (row + row for row in rows))
        conn.execute(
            "DELETE FROM page_index_nodes "
            "WHERE node_key IS NULL OR node_key NOT IN (SELECT value FROM json_each(?))",
            (authoritative_keys,),
        )
        if edge_rows is not None:
            conn.execute("DELETE FROM page_index_edges")
            if edge_rows:
                for start_idx in range(0, len(edge_rows), 5000):
                    conn.executemany(
                        "INSERT OR REPLACE INTO page_index_edges "
                        "(sequence, source_key, target_key, weight) VALUES (?, ?, ?, ?)",
                        edge_rows[start_idx : start_idx + 5000],
                    )
        if stamp is not None:
            _write_projection_state(conn, stamp, index_data, for_edge_digest)
    _invalidate_adjacency()
    return {"nodes_written": written, "partial": partial_keys is not None, "edges_rewritten": edge_changed}


def _write_projection_state(conn, stamp, index_data, for_edge_digest):
    digest_supported = _edge_digest_supported(conn)
    columns = (
        "(singleton, index_mtime, index_size, node_count, edge_count, updated_at, edge_digest) "
        if digest_supported
        else "(singleton, index_mtime, index_size, node_count, edge_count, updated_at) "
    )
    placeholders = "VALUES (1, ?, ?, ?, ?, ?, ?)" if digest_supported else "VALUES (1, ?, ?, ?, ?, ?)"
    digest_update = "edge_digest = excluded.edge_digest, " if digest_supported else ""
    values = [
        stamp[0], stamp[1],
        conn.execute("SELECT COUNT(*) FROM page_index_nodes").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0],
        _utc_now(),
    ]
    if digest_supported:
        # Committed atomically with the rows it describes.
        values.append(for_edge_digest if for_edge_digest is not None else _weighted_edges_digest(index_data))
    conn.execute(
        "INSERT INTO page_index_state "
        + columns
        + placeholders
        + " ON CONFLICT(singleton) DO UPDATE SET "
        "index_mtime = excluded.index_mtime, index_size = excluded.index_size, "
        "node_count = excluded.node_count, edge_count = excluded.edge_count, "
        + digest_update
        + "updated_at = excluded.updated_at",
        tuple(values),
    )


def ensure_page_index_projection() -> bool:
    """Rebuild the node projection from ``index.json`` when it is behind.

    Returns False when there is no index at all, which callers report as the
    cold-start "lake is drying" state.

    **Writer-side only.**  This takes the write lock, so a reader that called it
    made every query queue behind whichever writer held the lock -- and a reader
    that lost that race returned no answer at all.  Readers use
    :func:`read_catalog` instead, which never writes and falls back to
    ``index.json`` itself when the projection is behind.  The repair below is the
    outbox consumer's job (:func:`heal_projection_if_stale`).
    """
    if projection_is_current():
        return True
    init_db()
    if projection_is_current():
        return True
    stamp = index_file_stamp()
    if stamp is None:
        return False
    path = get_index_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            index_data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - surfaced as a degraded read
        log.error("Failed to read index.json: %s", exc)
        raise
    refresh_page_index_projection(index_data)
    clear_projection_stale()
    log.info(
        "Rebuilt the page index projection from %s (%s nodes).",
        path.name, len(index_data.get("nodes") or {}),
    )
    return True


def heal_projection_if_stale() -> dict:
    """Single-writer repair entry point: bring the projection back in step.

    Called by the outbox consumer, never by a reader.  Kept separate from
    :func:`ensure_page_index_projection` so the intent is explicit at the call
    site and so the write-lock cost lands on the one process that already owns
    the index instead of on whoever happens to query next.

    Returns a small report rather than raising: a contended heal is a normal
    outcome that the next cycle retries.
    """
    from vector_lake.db_store import DatabaseLockTimeout

    marker = projection_stale()
    if projection_is_current():
        if marker is not None:
            clear_projection_stale()
        return {"healed": False, "reason": "current"}
    if index_file_stamp() is None:
        return {"healed": False, "reason": "no index.json"}
    try:
        rebuilt = ensure_page_index_projection()
    except DatabaseLockTimeout as exc:
        return {"healed": False, "reason": f"write lock unavailable: {exc}", "marker": bool(marker)}
    except Exception as exc:  # noqa: BLE001 - the writer reports and retries next cycle
        return {"healed": False, "reason": f"{type(exc).__name__}: {exc}", "marker": bool(marker)}
    return {"healed": bool(rebuilt), "reason": "rebuilt", "marker": bool(marker)}


def _load_index_json() -> dict | None:
    """Parse ``index.json``; ``None`` when it is missing or unreadable."""
    path = get_index_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        log.error("Failed to read index.json: %s", exc)
        return None
    return data if isinstance(data, dict) else None


class _SqliteCatalog:
    """Node and edge accessors over the SQLite projection."""

    source = "projection"

    def nodes_by_key(self, keys) -> dict[str, dict]:
        return nodes_by_key(keys)

    def iter_name_fields(self):
        """Stream the current directory's identity metadata without retaining a second index."""
        cursor = get_connection().execute(
            "SELECT node_key, node_id, title, type, status, domain, topic_cluster, "
            "json_quote(json_extract(node_json, '$.aliases')) FROM page_index_nodes ORDER BY node_key"
        )
        try:
            for key, node_id, title, kind, status, domain, cluster, aliases in cursor:
                yield key, dict(id=node_id, title=title, type=kind, status=status, domain=domain,
                                topic_cluster=cluster, aliases=json.loads(aliases) if aliases else [])
        finally:
            cursor.close()

    def filter_fields(self, keys) -> dict[str, dict]:
        return filter_fields(keys)

    def adjacency(self) -> dict[str, list[tuple[str, float]]]:
        return adjacency()

    def scoped_adjacency(self, domain="", cluster="") -> ScopedAdjacency:
        with _adjacency_lock:
            conn = get_connection()
            # A caller-owned transaction may contain uncommitted projection rows.
            # It must not populate the process cache, even if later rolled back.
            if conn.in_transaction:
                return _freeze_adjacency(self._build_scoped_adjacency(conn, domain, cluster))
            with read_snapshot():
                state = conn.execute("SELECT * FROM page_index_state WHERE singleton=1").fetchone()
                if state is None:
                    return _freeze_adjacency(self._build_scoped_adjacency(conn, domain, cluster))
                key = ("projection", _scope_source_identity(), tuple(state), _normalized_scope(domain, cluster))
                return _scoped_adjacency(key, lambda: self._build_scoped_adjacency(conn, domain, cluster))

    @staticmethod
    def _build_scoped_adjacency(conn, domain, cluster):
        conn.create_function('vl_page_scope_domain', 1, lambda value: canonical_domain(value).lower(), deterministic=True)
        conn.create_function('vl_page_scope_cluster', 1, lambda value: str(value or '').lower(), deterministic=True)
        table: dict[str, list[tuple[str, float]]] = {}
        for source, target, weight in conn.execute(
            "SELECT e.source_key, e.target_key, e.weight FROM page_index_edges e "
            "JOIN page_index_nodes s ON s.node_key=e.source_key "
            "JOIN page_index_nodes t ON t.node_key=e.target_key "
            "WHERE (? = '' OR (vl_page_scope_domain(s.domain) = ? AND vl_page_scope_domain(t.domain) = ?)) "
            "AND (? = '' OR (vl_page_scope_cluster(s.topic_cluster) = ? AND vl_page_scope_cluster(t.topic_cluster) = ?)) ORDER BY e.sequence",
            (domain, canonical_domain(domain).lower(), canonical_domain(domain).lower(), cluster, cluster.lower(), cluster.lower()),
        ):
            table.setdefault(source, []).append((target, float(weight)))
            table.setdefault(target, []).append((source, float(weight)))
        return table

    def node_summary_lines(self, limit: int = 50) -> list[str]:
        return node_summary_lines(limit)


class _FileCatalog:
    """Node and edge accessors built from ``index.json`` itself.

    The file is the sovereign artifact, so it can always answer a read: a reader
    that finds the projection behind does not have to take the write lock to
    rebuild it in order to return results that the file already contains -- and
    contains more up to date.

    Built per request and dropped with it.  The module docstring's objection to
    parsing the whole file stands (169 MB / ~1.9 s on the live corpus), but the
    alternative for a stale projection was no answer at all, and caching the
    object graph in a long-lived server is the cost this projection exists to
    avoid.  The edge walk is lazy: queries whose top hits produce no seeds never
    pay for it.
    """

    source = "index.json"

    def __init__(self, index_data: dict):
        nodes = index_data.get("nodes") or {}
        self._nodes = {str(key): node for key, node in nodes.items()}
        self._weighted_edges = index_data.get("weighted_edges")
        self._adjacency: dict[str, list[tuple[str, float]]] | None = None
        # The parsed catalog owns one file generation. A token (not id(self))
        # avoids recycled identities without retaining the whole catalog in the LRU.
        self._scope_identity = (_scope_source_identity(), object())
        self._scope_fields = {key: (canonical_domain(node.get("domain")).lower(),
                                    str(node.get("topic_cluster") or "").lower())
                              for key, node in self._nodes.items()}

    def nodes_by_key(self, keys) -> dict[str, dict]:
        ordered = [str(key) for key in keys]
        return {key: self._nodes[key] for key in ordered if key in self._nodes}

    def iter_name_fields(self):
        for key in sorted(self._nodes):
            node = self._nodes[key]
            yield key, {field: node.get(field) for field in
                        ('id', 'title', 'type', 'status', 'domain', 'topic_cluster', 'aliases')}

    def filter_fields(self, keys) -> dict[str, dict]:
        ordered = [str(key) for key in keys]
        return {
            key: {name: (self._nodes[key] or {}).get(name) for name in FILTER_FIELDS}
            for key in ordered
            if key in self._nodes
        }

    def adjacency(self) -> dict[str, list[tuple[str, float]]]:
        if self._adjacency is None:
            table: dict[str, list[tuple[str, float]]] = {}
            for edge in self._weighted_edges or []:
                source = str(edge.get("source") or "")
                target = str(edge.get("target") or "")
                weight = float(edge.get("weight", 1.0) or 0.0)
                table.setdefault(source, []).append((target, weight))
                table.setdefault(target, []).append((source, weight))
            self._adjacency = table
        return self._adjacency

    def scoped_adjacency(self, domain="", cluster="") -> ScopedAdjacency:
        scope = _normalized_scope(domain, cluster)
        with _adjacency_lock:
            return _scoped_adjacency((self.source, self._scope_identity, scope),
                                     lambda: self._build_scoped_adjacency(scope))

    def _build_scoped_adjacency(self, scope):
        has_domain, domain, has_cluster, cluster = scope
        visible = {key for key, (node_domain, node_cluster) in self._scope_fields.items()
                   if (not has_domain or node_domain == domain)
                   and (not has_cluster or node_cluster == cluster)}
        table = {}
        for edge in self._weighted_edges or []:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            weight = float(edge.get("weight", 1.0) or 0.0)
            # Preserve file-catalog key order and empty visible adjacency rows,
            # without retaining a second, full adjacency for a scoped request.
            if source in visible:
                table.setdefault(source, [])
            if target in visible:
                table.setdefault(target, [])
            if source in visible and target in visible:
                table[source].append((target, weight))
                table[target].append((source, weight))
        return table

    def node_summary_lines(self, limit: int = 50) -> list[str]:
        return [
            f"[{self._nodes[key].get('type') or '?'}] {self._nodes[key].get('title') or key}"
            for key in sorted(self._nodes)[: int(limit)]
        ]


class ProjectionNote(str):
    """Enriched note string carrying machine-readable projection status."""

    def __new__(cls, text: str, *, is_degraded: bool = True, source: str = "index.json", reason: str | None = None):
        instance = super().__new__(cls, text)
        instance.is_degraded = is_degraded
        instance.source = source
        instance.reason = reason
        return instance


def _fallback_note(served_from_cache_for: float | None = None) -> ProjectionNote:
    note = (
        "page index projection is behind index.json; node and edge reads fell back to "
        "index.json itself (results are current; the outbox consumer rebuilds the projection)"
    )
    if served_from_cache_for is not None:
        # Say which file the answer came from, not just that a fallback is in play: inside the
        # re-parse window this is the parse of an *earlier* stamp.
        note += (f"; served from the cached parse of an earlier stamp ({served_from_cache_for:.0f} s "
                 "old) to keep a busy index.json rewrite from re-parsing the whole file on every "
                 "query")
    marker = projection_stale()
    reason = None
    if marker is not None:
        reason = marker.get("reason")
        note += f"; last writer-reported refresh failure: {reason}"
    return ProjectionNote(note, is_degraded=True, source="index.json", reason=reason)


# One cached fallback catalog, keyed by the ``index.json`` stamp.
#
# A stale projection makes every read parse the whole file (169 MB / 1.9 s on the
# live corpus, plus a 1.6 M-edge adjacency when seeds exist).  The stamp is
# exactly the granularity at which that parse stops being valid, so caching on it
# is enough to make a stale window cheap for repeated queries.  Deliberately a
# single entry rather than an LRU: the module's objection to holding the parsed
# object graph in a long-lived process stands, so the cache is bounded to one and
# dropped the moment the projection can answer again.
#
# The stamp alone is not enough while a writer is active: every page commit rewrites
# ``index.json``, so every stamp move makes the *next* reader pay for the whole parse
# again -- measured 408 ms for the live 12.7 MB file.  ``VECTOR_LAKE_FALLBACK_REPARSE_SECONDS``
# bounds that: set above 0, a re-parse is rate limited and the previous parse keeps
# answering inside the window, with the note saying which stamp answered.  It defaults
# to 0 (re-parse on every stamp move) because that is the contract the suite pins:
# ``test_a_changed_index_json_invalidates_the_cached_catalog`` and
# ``test_stale_projection_is_served_from_index_json`` both state that an edited file wins
# on the next read.  Trading that away is an operator decision, not a side effect of a fix.
_CATALOG_CACHE: dict[str, object] = {"key": None, "catalog": None, "at": 0.0}
_CATALOG_LOCK = threading.Lock()


def _fallback_reparse_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("VECTOR_LAKE_FALLBACK_REPARSE_SECONDS", "0")))
    except ValueError:
        return 0.0


def _catalog_cache_key():
    path = get_index_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime, stat.st_size)


def reset_catalog_cache() -> None:
    """Drop the cached fallback catalog (tests, and explicit memory release)."""
    with _CATALOG_LOCK:
        _CATALOG_CACHE["key"] = None
        _CATALOG_CACHE["catalog"] = None
        _CATALOG_CACHE["at"] = 0.0
    with _adjacency_lock:
        _scoped_adjacencies.clear()


def read_catalog():
    """Read-only node/edge source for a query, plus a note when it is a fallback.

    Never writes and never takes the write lock.  ``(None, note)`` means there was
    nothing to read at all, which callers report as the cold-start state.

    The fallback catalog is cached on the index stamp, so a request that repeats
    during a stale window does not re-parse the file.
    """
    if projection_is_current():
        # The projection can answer again: release the fallback object graph.
        # Checked lock-free first so the steady state (the common case) does not
        # take the cache lock on every query.
        if _CATALOG_CACHE["catalog"] is not None:
            reset_catalog_cache()
        return _SqliteCatalog(), None

    key = _catalog_cache_key()
    if key is None:
        return None, "no index.json to read"
    with _CATALOG_LOCK:
        if _CATALOG_CACHE["key"] == key and _CATALOG_CACHE["catalog"] is not None:
            from vector_lake import retrieval_runtime as _runtime
            _runtime.counter('catalog_cache_hit')
            return _CATALOG_CACHE["catalog"], _fallback_note()
        cached = _CATALOG_CACHE["catalog"]
        age = time.monotonic() - float(_CATALOG_CACHE.get("at") or 0.0)
        cached_key = _CATALOG_CACHE["key"]
        same_root = isinstance(cached_key, tuple) and cached_key[0] == key[0]
        if cached is not None and same_root and age < _fallback_reparse_seconds():
            return cached, _fallback_note(served_from_cache_for=age)

    from vector_lake import retrieval_runtime as _runtime
    _runtime.counter('catalog_parse')
    index_data = _load_index_json()
    if index_data is None:
        return None, "no index.json to read"
    catalog = _FileCatalog(index_data)
    with _CATALOG_LOCK:
        # Stored as a key/catalog pair so a racing rebuild can never pair a
        # catalog with the wrong stamp; the loser simply rebuilds next time.
        _CATALOG_CACHE["key"] = key
        _CATALOG_CACHE["catalog"] = catalog
        _CATALOG_CACHE["at"] = time.monotonic()
    return catalog, _fallback_note()


def nodes_by_key(keys) -> dict[str, dict]:
    """Full node dicts for ``keys``, preserving the caller's key order."""
    ordered = [str(key) for key in keys]
    if not ordered:
        return {}
    conn = get_connection()
    found: dict[str, dict] = {}
    for start in range(0, len(ordered), 900):
        chunk = ordered[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT node_key, node_json FROM page_index_nodes WHERE node_key IN ({placeholders})",
            chunk,
        ):
            found[str(row["node_key"])] = json.loads(row["node_json"])
    return {key: found[key] for key in ordered if key in found}


#: The node fields the caller-visible selectors read, and the only ones a filter needs.  Keeping
#: the list next to the query is what makes "filter without parsing the node" safe: a fourth field
#: added to ``_passes_filters`` without being added here would otherwise show up as a filter that
#: silently sees ``None``, so the parity test compares this projection against the full node dicts.
FILTER_FIELDS = ("domain", "topic_cluster", "status")


def filter_fields(keys) -> dict[str, dict]:
    """The selector inputs for ``keys``: three columns, no JSON parse.

    ``nodes_by_key`` deserialises the whole node payload, which measured as the largest single
    cost of a filtered query (p95 145 ms of a 299 ms p95, 2026-09-29) even though the selectors
    read only these columns.  Returning exactly those keys keeps the two paths equal by
    construction: ``_passes_filters`` sees the same values it would have read from the JSON.
    """
    ordered = [str(key) for key in keys]
    if not ordered:
        return {}
    columns = ", ".join(FILTER_FIELDS)
    conn = get_connection()
    found: dict[str, dict] = {}
    for start in range(0, len(ordered), 900):
        chunk = ordered[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT node_key, {columns} FROM page_index_nodes WHERE node_key IN ({placeholders})",
            chunk,
        ):
            found[str(row["node_key"])] = {name: row[name] for name in FILTER_FIELDS}
    return {key: found[key] for key in ordered if key in found}


def node_summary_lines(limit: int = 50) -> list[str]:
    conn = get_connection()
    return [
        f"[{row['type'] or '?'}] {row['title'] or row['node_key']}"
        for row in conn.execute(
            "SELECT node_key, title, type FROM page_index_nodes ORDER BY node_key LIMIT ?",
            (int(limit),),
        )
    ]


# --- adjacency --------------------------------------------------------------

_adjacency_lock = threading.RLock()
_adjacency: dict = {"stamp": None, "data": None, "generation": 0}
_scoped_adjacencies: OrderedDict = OrderedDict()


def _scope_source_identity():
    path = get_db_path().resolve()
    try:
        stat = path.stat()
        database_identity = (str(path), stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        database_identity = (str(path), None, None)
    return (str(get_index_path().resolve()), database_identity)


def _normalized_scope(domain, cluster):
    # An explicitly blank/whitespace domain is not the unfiltered scope.
    return (bool(domain), canonical_domain(domain).lower(), bool(cluster), str(cluster or "").lower())


def _freeze_adjacency(table) -> ScopedAdjacency:
    return MappingProxyType({key: tuple(neighbors) for key, neighbors in table.items()})


def _scoped_adjacency(key, build) -> ScopedAdjacency:
    """Called under the adjacency lock; cached snapshots never escape mutable."""
    if key in _scoped_adjacencies:
        _scoped_adjacencies.move_to_end(key)
        return _scoped_adjacencies[key][0]
    from vector_lake import retrieval_runtime as _runtime
    _runtime.counter('adjacency_build')
    table = _freeze_adjacency(build())
    references = sum(len(neighbors) for neighbors in table.values())
    if references <= SCOPED_ADJACENCY_REFERENCE_LIMIT:
        while _scoped_adjacencies and (
            len(_scoped_adjacencies) >= SCOPED_ADJACENCY_CACHE_LIMIT
            or references + sum(entry[1] for entry in _scoped_adjacencies.values()) > SCOPED_ADJACENCY_REFERENCE_LIMIT
        ):
            _scoped_adjacencies.popitem(last=False)
        _scoped_adjacencies[key] = (table, references)
    return table


def scoped_adjacency_is_cached(table) -> bool:
    """Whether a native prepared index may retain this bounded snapshot."""
    with _adjacency_lock:
        return any(entry[0] is table for entry in _scoped_adjacencies.values())


def _invalidate_adjacency() -> None:
    with _adjacency_lock:
        _adjacency["stamp"] = None
        _adjacency["data"] = None
        _adjacency["generation"] += 1
        _scoped_adjacencies.clear()


def adjacency_generation() -> int:
    """Identity of the current adjacency object, for callers keying their own derived caches.

    Bumped on invalidation and again whenever the table is rebuilt, so a derived structure (the
    prepared PPR index) is dropped exactly when the graph changes instead of on a timer.  A timer
    would either rebuild a 6 095-node index for nothing or serve one built from a stale graph.
    """
    with _adjacency_lock:
        return int(_adjacency["generation"])


def adjacency() -> dict[str, list[tuple[str, float]]]:
    """Undirected weighted adjacency, identical to the ``weighted_edges`` build.

    ``ORDER BY rowid`` reproduces ``index.json``'s edge order (verified against
    the live corpus), so the two-step personalised PageRank walk -- which is
    order-sensitive for zero-mass candidates -- stays bit-identical.
    """
    conn = get_connection()
    # Keyed on the recorded edge-content digest, not on the ``index.json`` stamp.  The stamp is
    # ``(mtime, size)`` and moves on every rewrite, including the ones that leave the topology
    # untouched (a page edit with no link change, a governance pass, a decay tick).  Measured on the
    # live 20 146-edge graph: rebuilding this table costs 84 ms and rebuilding the prepared PPR index
    # on top of it another 89 ms, and the walk that follows is 5 ms -- so an unrelated rewrite used
    # to add ~173 ms to the next query and to drop a structure that had not gone stale.  The digest
    # is written only after the edge rows are committed (see ``refresh_page_index_projection``), and
    # it covers edge order, so it answers "is this the same graph" rather than "was this the same
    # file".  A database without the column keeps the file stamp.
    stored = _stored_edge_state(conn)
    if stored is not None and stored[1]:
        stamp = ("edge_digest", str(stored[1]), int(stored[0] or 0))
    else:
        edge_count = conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0]
        stamp = (str(get_index_path()), index_file_stamp(), int(edge_count or 0))
    with _adjacency_lock:
        if _adjacency["stamp"] == stamp and _adjacency["data"] is not None:
            return _adjacency["data"]
    from vector_lake import retrieval_runtime as _runtime
    _runtime.counter('adjacency_build')
    table: dict[str, list[tuple[str, float]]] = {}
    for source, target, weight in conn.execute(
        "SELECT source_key, target_key, weight FROM page_index_edges ORDER BY sequence"
    ):
        source = str(source)
        target = str(target)
        weight = float(weight if weight is not None else 1.0)
        table.setdefault(source, []).append((target, weight))
        table.setdefault(target, []).append((source, weight))
    with _adjacency_lock:
        _adjacency["stamp"] = stamp
        _adjacency["data"] = table
        _adjacency["generation"] = int(_adjacency.get("generation", 0)) + 1
    return table


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
