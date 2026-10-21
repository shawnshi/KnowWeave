"""Pure, directed document declarations, separate from pruned relatedness scores."""
from __future__ import annotations

import math
import re
from datetime import date, datetime

from vector_lake.link_resolution import (
    build_link_map,
    declared_names_from_nodes,
    resolve_link_target,
)
from vector_lake.node_vocabulary import strip_prefix
from vector_lake.relation_policy import predicate_diagnostics
from vector_lake.wiki_utils import entity_identity_key

MAX_RELATIONS = 50_000
MAX_DIAGNOSTICS = 200


class RelationInputError(ValueError):
    """Malformed relation query or catalog input, never an empty business result."""

    code = "invalid_input"


def validate_limit(value, name, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise RelationInputError(f"{name} must be an integer within 0..{maximum}")


def node_field(node, field, default=None):
    """Read index fields or canonical entity metadata without interpreting prose."""
    return node.get(field, (node.get("metadata") or {}).get(field, default))


def in_scope(node, domain="", cluster="", include_history=False):
    return (not domain or node_field(node, "domain") == domain) and (
        not cluster or node_field(node, "topic_cluster") == cluster
    ) and (include_history or str(node_field(node, "status", "")).lower() not in ("archived", "deprecated"))


def identity_resolver(nodes):
    identities = {key: {"title": node_field(node, "title"), "aliases": node_field(node, "aliases", [])}
                  for key, node in nodes.items()}
    link_map, _, cores, contested = build_link_map(nodes, declared_names_from_nodes(identities))

    def resolve(name):
        # An exact filename owns its name, but a contested declaration must not
        # escape ambiguity refusal by falling through to a different core spelling.
        if name in link_map:
            return link_map[name], "resolved"
        if name in contested or entity_identity_key(strip_prefix(name)) in contested:
            return None, "ambiguous_target"
        target = resolve_link_target(name, link_map, cores)
        return target, "resolved" if target else "unresolved_target"

    return resolve


def relation_context(value, source):
    """Allow-list read-only context; never return claim text, names or source refs."""
    value = value if isinstance(value, dict) else {}
    status = value.get("status")
    if not isinstance(status, str) or status not in {"matched", "binding_unknown", "ambiguous", "schema_unavailable"}:
        status = "binding_unknown"
    locator = value.get("locator")
    block = locator.get("block_index") if isinstance(locator, dict) else None
    if status == "matched" and (not isinstance(locator, dict) or locator.get("page_key") != source
                                or type(block) is not int or block <= 0):
        status = "binding_unknown"
    count = value.get("evidence_count") if status == "matched" else None
    count = count if type(count) is int and count >= 0 else None
    context = {"status": status, "evidence_count": count}
    if status == "matched":
        context["locator"] = {"page_key": source, "block_index": block}
    temporal = {"status": "unknown"}
    if status == "matched":
        for field in ("valid_from", "valid_to", "temporal_anchor"):
            stamp = value.get(field)
            if not isinstance(stamp, str):
                continue
            if (field == "temporal_anchor" and re.fullmatch(r"[0-9]{4}(?:-(?:0[1-9]|1[0-2]|Q[1-4]|H[12]))?", stamp)
                    and int(stamp[:4]) > 0):
                temporal[field] = stamp
                continue
            try:
                if len(stamp) == 10:
                    date.fromisoformat(stamp)
                else:
                    datetime.fromisoformat(stamp)
            except ValueError:
                continue
            temporal[field] = stamp
        if len(temporal) > 1:
            temporal["status"] = "explicit"
    provenance = "linked_unverified" if count is not None and count > 0 else "absent" if count == 0 else "unknown"
    return {"context": context, "temporal": temporal, "provenance_status": provenance,
            "epistemic_status": "unverified"}


def build_relation_catalog(nodes: dict, weighted_edges=None, *, domain="", cluster="",
                           include_history=False, max_relations=MAX_RELATIONS, context=None) -> dict:
    """Build scoped declarations/navigation independently of weighted pruning.

    Missing targets are diagnosed without echoing their identity. Hidden resolved
    endpoints are omitted entirely. All assertions remain document-scoped/unverified.
    """
    if not isinstance(nodes, dict) or not isinstance(domain, str) or not isinstance(cluster, str):
        raise RelationInputError("nodes must be a mapping and scope values must be strings")
    if type(include_history) is not bool:
        raise RelationInputError("include_history must be boolean")
    validate_limit(max_relations, "max_relations", MAX_RELATIONS)
    for key, node in nodes.items():
        if not isinstance(key, str) or not key or not isinstance(node, dict):
            raise RelationInputError("nodes require nonempty string keys and object values")
        if "metadata" in node and not isinstance(node["metadata"], dict):
            raise RelationInputError("metadata must be an object")
        aliases = node_field(node, "aliases", [])
        if not isinstance(aliases, (list, str)) or (isinstance(aliases, list) and not all(isinstance(a, str) for a in aliases)):
            raise RelationInputError("aliases must be strings")
    if weighted_edges is not None and not isinstance(weighted_edges, list):
        raise RelationInputError("weighted_edges must be a list")
    if context is not None and not isinstance(context, dict):
        raise RelationInputError("context must be a mapping")
    resolve = identity_resolver(nodes)
    visible = {key for key, node in nodes.items() if in_scope(node, domain, cluster, include_history)}
    result = {"relations": [], "diagnostics": [], "scope": {
        "domain": domain, "cluster": cluster, "include_history": include_history}, "truncated": False}
    seen = set()

    def emit(source, target, predicate, layer, weight=None):
        signature = (source, target, predicate, layer)
        if signature in seen:
            return
        seen.add(signature)
        if len(result["relations"]) >= max_relations:
            result["truncated"] = True
            return
        extra = relation_context((context or {}).get(signature[:3]) if layer == "declared" else None, source)
        extra["semantic_kind"] = {"declared": "document_declaration", "navigation": "navigation",
                                  "relatedness": "relatedness"}[layer]
        if layer == "declared":
            semantics, codes = predicate_diagnostics(predicate, node_field(nodes[source], "type"),
                                                    node_field(nodes[target], "type"))
            extra["predicate_semantics"] = {**semantics, "diagnostics": codes}
        result["relations"].append({
            "source": source, "target": target, "predicate": predicate, "layer": layer,
            "declared": layer == "declared", "verified": False, "weight": weight,
            "origin": {"page_key": source} if layer != "relatedness" else {"kind": "weighted_edges"},
            "resolution_status": "resolved", "subject_scope": "document" if layer != "relatedness" else "unknown",
            **extra,
        })

    def declaration(source, name, predicate, layer):
        target, status = resolve(name)
        if target is None:
            if len(result["diagnostics"]) < MAX_DIAGNOSTICS:
                result["diagnostics"].append({"source": source, "code": status, "layer": layer})
            else:
                result["truncated"] = True
        elif target in visible:
            emit(source, target, predicate, layer)

    for key in sorted(visible):
        node = nodes[key]
        triples = node_field(node, "triples", [])
        links = node_field(node, "links", node_field(node, "outbound_links", []))
        if not isinstance(triples, list) or not isinstance(links, list):
            raise RelationInputError("triples and links must be lists")
        for triple in triples:
            if not isinstance(triple, dict) or not all(isinstance(triple.get(f), str) and triple[f].strip()
                                                     for f in ("predicate", "target")):
                raise RelationInputError("each triple requires nonempty predicate and target strings")
        for triple in sorted(triples, key=lambda t: (t["target"], t["predicate"])):
            declaration(key, triple["target"], triple["predicate"], "declared")
        for target in links:
            if not isinstance(target, str) or not target.strip():
                raise RelationInputError("links must contain nonempty strings")
        for target in sorted(set(links)):
            declaration(key, target, "wikilink", "navigation")

    pairs = {}
    for edge in weighted_edges or []:
        if not isinstance(edge, dict) or not all(isinstance(edge.get(f), str) and edge[f] for f in ("source", "target")):
            raise RelationInputError("weighted edges require string endpoints")
        weight = edge.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (float, int)) or weight < 0:
            raise RelationInputError("relatedness weights must be finite nonnegative numbers")
        try:
            finite = math.isfinite(weight)
        except OverflowError as exc:
            raise RelationInputError("relatedness weight exceeds numeric range") from exc
        if not finite:
            raise RelationInputError("relatedness weights must be finite nonnegative numbers")
        source, target = edge["source"], edge["target"]
        if source in visible and target in visible:
            pair = tuple(sorted((source, target)))
            pairs[pair] = max(pairs.get(pair, 0), weight)
    for (source, target), weight in sorted(pairs.items()):
        emit(source, target, "relatedness", "relatedness", weight)
    return result
