"""Read-only scoped relationship queries over the existing page-index projection."""
from __future__ import annotations

import json
import re
import sqlite3

from vector_lake.db_store import get_db_path
from vector_lake.relation_catalog import (
    RelationInputError,
    build_relation_catalog,
    identity_resolver,
    in_scope,
    validate_limit,
)
from vector_lake.wiki_utils import get_index_path

MAX_DEPTH = 5
MAX_RESULTS = 2000


def _scoped_context(conn, nodes, selected):
    """Bind only explicit typed tokens with existing same-page block locators.

    Normal extracted prose has lost its predicate binding. It deliberately remains
    unknown: target-name similarity cannot recover that information safely.
    """
    by_source = {}
    for relation in selected:
        if relation["layer"] == "declared":
            signature = (relation["source"], relation["target"], relation["predicate"])
            by_source.setdefault(relation["source"], set()).add(signature)
    if not by_source:
        return {}
    schemas = {table: {row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")}
               for table in ("claims", "evidence")}
    claims_ready = {"claim_id", "claim_text", "data_json", "f_page_key"} <= schemas["claims"]
    evidence_ready = {"evidence_id", "data_json", "f_page_key"} <= schemas["evidence"]
    resolve = identity_resolver(nodes)
    context = {}
    for source, signatures in sorted(by_source.items()):
        if not claims_ready:
            context.update({key: {"status": "schema_unavailable"} for key in signatures})
            continue
        matches = {key: [] for key in signatures}
        for row in conn.execute("SELECT claim_id, claim_text, data_json FROM claims WHERE f_page_key = ?", (source,)):
            claim = json.loads(row["data_json"])
            if not isinstance(claim, dict):
                raise RelationInputError("claim context must be an object")
            locator = claim.get("locator")
            if (not isinstance(locator, dict) or locator.get("page_key") != source
                    or type(locator.get("block_index")) is not int or locator["block_index"] <= 0):
                continue
            bound = set()
            for token in re.finditer(r"\[([^\[\]]+?)::\s*\[\[([^\[\]]+?)\]\]\]", row["claim_text"] or ""):
                target, _ = resolve(token[2].split("|")[0].strip().removesuffix(".md"))
                key = (source, target, token[1].strip())
                if key in matches:
                    bound.add(key)
            for key in bound:
                matches[key].append((row["claim_id"], claim))
        for key, candidates in matches.items():
            if len(candidates) != 1:
                context[key] = {"status": "ambiguous" if candidates else "binding_unknown"}
                continue
            claim_id, claim = candidates[0]
            value = {"status": "matched", "locator": claim["locator"],
                     **{field: claim.get(field) for field in ("valid_from", "valid_to", "temporal_anchor")}}
            ids = claim.get("evidence_ids")
            if evidence_ready and isinstance(ids, list) and all(isinstance(item, str) for item in ids) and len(set(ids)) == len(ids):
                linked = 0
                for evidence_id in ids:
                    row = conn.execute("SELECT data_json FROM evidence WHERE f_page_key = ? AND evidence_id = ?",
                                       (source, evidence_id)).fetchone()
                    if row is None:
                        break
                    evidence = json.loads(row[0])
                    if not isinstance(evidence, dict):
                        raise RelationInputError("evidence context must be an object")
                    locator = evidence.get("locator")
                    supports = evidence.get("supports_claim_ids")
                    if (not isinstance(locator, dict) or locator.get("page_key") != source
                            or type(locator.get("block_index")) is not int
                            or locator.get("block_index") != claim["locator"]["block_index"]
                            or not isinstance(supports, list) or claim_id not in supports):
                        break
                    linked += 1
                if linked == len(ids):
                    value["evidence_count"] = linked
            context[key] = value
    return context


def get_relations(entity_name: str, domain: str, cluster: str = "", depth: int = 1,
                  limit: int = 200, include_relatedness: bool = False) -> dict:
    """Traverse declarations in either direction without manufacturing reverse facts.

    Domain is a required exact literal (including General). Unavailable projections,
    read errors, ambiguity and valid empty results have separate status values.
    """
    if not isinstance(entity_name, str) or not entity_name.strip():
        raise RelationInputError("entity_name must be a nonempty string")
    if not isinstance(domain, str) or not domain.strip() or not isinstance(cluster, str):
        raise RelationInputError("domain must be nonempty and cluster must be a string")
    validate_limit(depth, "depth", MAX_DEPTH)
    validate_limit(limit, "limit", MAX_RESULTS)
    if type(include_relatedness) is not bool:
        raise RelationInputError("include_relatedness must be boolean")
    response = {"relations": [], "diagnostics": [], "scope": {
        "domain": domain, "cluster": cluster, "include_history": False}, "truncated": False,
        "status": "ok", "source": "page_index_projection"}
    conn = None
    try:
        path = get_db_path()
        if not path.is_file():
            return {**response, "status": "unavailable", "error": {"code": "projection_unavailable"}}
        # db_store.get_connection() creates files, loads extensions and sets writer
        # pragmas. A relation reader owns only this short-lived mode=ro connection.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        index = get_index_path()
        if index.exists():
            stamp = index.stat()
            state = conn.execute("SELECT index_mtime, index_size FROM page_index_state WHERE singleton=1").fetchone()
            if state is None or (float(state[0]), int(state[1])) != (stamp.st_mtime, stamp.st_size):
                return {**response, "status": "unavailable", "error": {"code": "projection_stale"}}
        # Global *identity only* is necessary: a hidden claimant must not turn an
        # ambiguous alias into a unique visible match. Never hydrate hidden payloads.
        nodes = {}
        for row in conn.execute(
            "SELECT node_key, title, domain, topic_cluster, status, "
            "json_quote(COALESCE(json_extract(node_json, '$.aliases'), "
            "json_extract(node_json, '$.metadata.aliases'), json('[]'))) AS aliases "
            "FROM page_index_nodes"
        ):
            aliases = json.loads(row["aliases"])
            nodes[row["node_key"]] = {field: row[field] for field in ("title", "domain", "topic_cluster", "status")}
            nodes[row["node_key"]]["aliases"] = aliases
        resolved, resolution = identity_resolver(nodes)(entity_name)
        if resolved is None:
            return {**response, "status": "ambiguous" if resolution == "ambiguous_target" else "not_found"}
        if not in_scope(nodes[resolved], domain, cluster):
            return {**response, "status": "not_found"}
        response["entity"] = resolved
        for row in conn.execute(
            "SELECT node_key, node_json FROM page_index_nodes WHERE domain = ? "
            "AND (? = '' OR topic_cluster = ?) "
            "AND lower(COALESCE(status, '')) NOT IN ('archived', 'deprecated')",
            (domain, cluster, cluster),
        ):
            payload = json.loads(row["node_json"])
            if not isinstance(payload, dict):
                raise RelationInputError("projected node must be an object")
            nodes[row["node_key"]] = payload
        edges = []
        if include_relatedness:
            for row in conn.execute(
                "SELECT e.source_key, e.target_key, e.weight FROM page_index_edges e "
                "JOIN page_index_nodes s ON s.node_key=e.source_key "
                "JOIN page_index_nodes t ON t.node_key=e.target_key "
                "WHERE s.domain = ? AND t.domain = ? "
                "AND (? = '' OR (s.topic_cluster = ? AND t.topic_cluster = ?)) ORDER BY e.sequence",
                (domain, domain, cluster, cluster, cluster),
            ):
                edges.append({"source": row[0], "target": row[1], "weight": row[2]})
        catalog = build_relation_catalog(nodes, edges, domain=domain, cluster=cluster)
        relations = catalog["relations"]
        neighbors = {}
        for position, relation in enumerate(relations):
            for endpoint in (relation["source"], relation["target"]):
                neighbors.setdefault(endpoint, []).append(position)
        visited, frontier, selected = {resolved}, {resolved}, set()
        for _ in range(depth):
            following = set()
            for key in frontier:
                for position in neighbors.get(key, []):
                    selected.add(position)
                    relation = relations[position]
                    following.update((relation["source"], relation["target"]))
            frontier = following - visited
            visited.update(frontier)
            if not frontier:
                break
        ordered = sorted(selected)
        context = _scoped_context(conn, nodes, [relations[i] for i in ordered[:limit]])
        if context:
            # Select topology before reading context, so unreachable or capped pages
            # cannot force unrelated claim/evidence hydration in this transaction.
            enriched = build_relation_catalog(nodes, edges, domain=domain, cluster=cluster, context=context)
            relations = enriched["relations"]
        response.update(relations=[relations[i] for i in ordered[:limit]],
                        diagnostics=[d for d in catalog["diagnostics"] if d["source"] in visited],
                        truncated=catalog["truncated"] or len(ordered) > limit)
        return response
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        return {**response, "status": "error", "error": {"code": "read_error", "type": type(exc).__name__}}
    finally:
        if conn is not None:
            conn.close()
