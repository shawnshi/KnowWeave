import json
import logging
import math
import os
import webbrowser
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from vector_lake import get_extension_root
from vector_lake import governance_store
from vector_lake.wiki_utils import get_claim_graph_path, get_index_path, get_memory_dir

try:
    import vector_lake_core
    HAVE_CORE = True
except ModuleNotFoundError as exc:
    if exc.name != "vector_lake_core":
        raise
    vector_lake_core = None
    HAVE_CORE = False


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-graph")

DEFAULT_MAX_PAGE_EDGES = 5000
DEFAULT_MAX_CLAIM_EDGES = 5000

# The lock is held for a whole read-modify-write cycle and a full rebuild can
# hold it for minutes, so 5s (the previous value) was not a real wait: on the
# live 169 MB index the tool timed out on every call and silently fell back to
# an unlocked read.  The fallback is still needed, but it is now long enough to
# be exceptional, and it is reported in the payload when it happens.
DEFAULT_INDEX_LOCK_TIMEOUT_SECONDS = 20.0


def _normalize_backbone_edges(selected_candidates: list[dict]) -> tuple[list[dict], dict[str, int]]:
    # Step 5: Normalize relations and compute backbone degrees
    backbone_degrees: dict[str, int] = {}
    normalized_edges = []
    for edge in selected_candidates:
        s = edge["source"]
        t = edge["target"]
        w = round(float(edge.get("weight", 1.0)), 3)
        rel = edge.get("relation")
        if not rel:
            rel = "strong_association" if w >= 10.0 else ("association" if w >= 5.0 else "related_to")

        normalized_edges.append({
            "source": s,
            "target": t,
            "weight": w,
            "relation": rel,
        })
        backbone_degrees[s] = backbone_degrees.get(s, 0) + 1
        backbone_degrees[t] = backbone_degrees.get(t, 0) + 1

    return normalized_edges, backbone_degrees



def _native_backbone_candidates(raw_edges, valid_node_ids, max_edges, min_weight):
    native = getattr(vector_lake_core, "fast_backbone_indices", None) if HAVE_CORE else None
    if (native is None or not isinstance(max_edges, int)
            or len(raw_edges) > 1_000_000 or len(valid_node_ids) > 100_000
            or not all(isinstance(node, str) for node in valid_node_ids)):
        return None
    node_ids = {node: index for index, node in enumerate(sorted(valid_node_ids))}
    edges = []
    for index, edge in enumerate(raw_edges):
        source, target = edge.get("source"), edge.get("target")
        if not source or not target or source == target:
            continue
        if source not in valid_node_ids or target not in valid_node_ids:
            continue
        weight = float(edge.get("weight", 1.0))
        if weight < min_weight:
            continue
        # Python sorting of NaN has special ordering behavior; do not change it.
        if not math.isfinite(weight):
            return None
        edges.append((node_ids[source], node_ids[target], weight, index))
    # Python accepts arbitrarily large integers; never pass an unbounded budget to usize.
    indices = native(len(node_ids), edges, min(max_edges, len(edges)))
    return [raw_edges[index] for index in indices]


def _extract_backbone_edges(
    raw_edges: list[dict],
    valid_node_ids: set[str],
    max_edges: int = DEFAULT_MAX_PAGE_EDGES,
    min_weight: float = 0.0,
) -> tuple[list[dict], dict[str, int]]:
    """Extract a high-salience topological backbone from dense or explosive edge sets.

    Combines:
      1. Valid endpoint verification (drop dangling/self-loop edges).
      2. Unordered-pair deduplication (a repeated row must not consume quota;
         the live index carried 314,660 duplicate pairs).
      3. Local coverage first: a pairing pass over uncovered nodes, then a pass
         that covers whatever the pairing left over.  A selection in which every
         connected node has at least one incident edge needs only about half as
         many edges as nodes, so a budget below the node count still covers the
         connected graph -- which weight-ordered greedy alone did not do (5,000
         edges over 7,125 pages left 909 orphans).
      4. Global weight-salience descending fill for the remaining quota.

    Coverage of every connected node is therefore guaranteed whenever the budget
    stretches, but a node is not guaranteed its *strongest* edge: strength only
    decides which edge represents a node, not whether it is represented.

    Returns:
      (selected_edges, backbone_degree_map)
    """
    if not raw_edges or max_edges <= 0:
        return [], {}
    native_candidates = _native_backbone_candidates(raw_edges, valid_node_ids, max_edges, min_weight)
    if native_candidates is not None:
        return _normalize_backbone_edges(native_candidates)

    # Step 1: Filter dangling endpoints and self-loops
    clean_edges = []
    for edge in raw_edges:
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target or source == target:
            continue
        if source not in valid_node_ids or target not in valid_node_ids:
            continue
        weight = float(edge.get("weight", 1.0))
        if weight < min_weight:
            continue
        clean_edges.append(edge)

    if not clean_edges:
        return [], {}

    # Step 2: Sort descending by weight, with a deterministic endpoint tie-break
    clean_edges.sort(
        key=lambda item: (
            -float(item.get("weight", 1.0)),
            str(item.get("source")),
            str(item.get("target")),
        )
    )

    unique_edges = []
    seen_pairs: set[tuple[str, str]] = set()
    for edge in clean_edges:
        source = edge["source"]
        target = edge["target"]
        pair = (source, target) if source <= target else (target, source)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        unique_edges.append(edge)

    if len(unique_edges) <= max_edges:
        selected_candidates = unique_edges
    else:
        chosen_indices: set[int] = set()
        covered_nodes: set[str] = set()

        # Step 3a: pair up still-uncovered nodes, strongest edge first.
        for idx, edge in enumerate(unique_edges):
            source = edge["source"]
            target = edge["target"]
            if source in covered_nodes or target in covered_nodes:
                continue
            chosen_indices.add(idx)
            covered_nodes.add(source)
            covered_nodes.add(target)
            if len(chosen_indices) >= max_edges:
                break

        # Step 3b: attach whatever the pairing pass left with no edge at all.
        if len(chosen_indices) < max_edges:
            for idx, edge in enumerate(unique_edges):
                if idx in chosen_indices:
                    continue
                source = edge["source"]
                target = edge["target"]
                if source in covered_nodes and target in covered_nodes:
                    continue
                chosen_indices.add(idx)
                covered_nodes.add(source)
                covered_nodes.add(target)
                if len(chosen_indices) >= max_edges:
                    break

        # Step 4: Fill remaining quota with global top-salience edges
        if len(chosen_indices) < max_edges:
            for idx in range(len(unique_edges)):
                if idx not in chosen_indices:
                    chosen_indices.add(idx)
                    if len(chosen_indices) >= max_edges:
                        break

        selected_candidates = [unique_edges[i] for i in sorted(chosen_indices)]

    return _normalize_backbone_edges(selected_candidates)

def _graph_output_path(memory_dir: str) -> str:
    """Default dashboard location: the ``scratch`` isolation zone of the lake.

    The first candidate used to be ``<parent of memory_dir>/tmp``, i.e. one
    level *above* the memory root, so a call without ``output_dir`` wrote a
    generated artifact into the host's home layout (and fell back to
    ``<extension_root>/data/tmp``).  ``<memory_dir>/scratch`` is the zone the
    graph skill already requires for explicit ``output_dir`` calls, so the
    default and the explicit path now agree; the extension-root candidate stays
    only as a last resort when the scratch tree is not writable.
    """
    extension_root = str(get_extension_root())
    candidates = [
        os.path.join(memory_dir, "scratch", "vector_lake_graph.html"),
        os.path.join(extension_root, "data", "tmp", "vector_lake_graph.html"),
    ]
    for candidate in candidates:
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return candidates[-1]


def _build_page_graph(
    index_data: dict,
    max_edges: int = DEFAULT_MAX_PAGE_EDGES,
    min_weight: float = 0.0,
) -> dict:
    nodes_dict = index_data.get("nodes", {})
    communities = index_data.get("communities", {})
    raw_weighted_edges = index_data.get("weighted_edges", [])
    aliases = index_data.get("aliases", {})
    valid_node_ids = set(nodes_dict.keys())

    # True undirected degree from the weighted edge set.  The previous version
    # incremented the source once per declared link (resolved or not) while
    # counting in-edges only when the alias resolved, so the figure the UI and
    # the search ranking read differed from the real degree for 89% of nodes
    # (Institution_浙大一院 reported 3 against 791 actual edges).
    degree_map: dict[str, int] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for edge in raw_weighted_edges:
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target or source == target:
            continue
        pair = (source, target) if source <= target else (target, source)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        degree_map[source] = degree_map.get(source, 0) + 1
        degree_map[target] = degree_map.get(target, 0) + 1

    # Extract high-salience topological backbone
    backbone_edges, backbone_degrees = _extract_backbone_edges(
        raw_edges=raw_weighted_edges,
        valid_node_ids=valid_node_ids,
        max_edges=max_edges,
        min_weight=min_weight,
    )

    backbone_adjacency: dict[str, set[str]] = {}
    for edge in backbone_edges:
        s = edge.get("source")
        t = edge.get("target")
        if s and t:
            backbone_adjacency.setdefault(s, set()).add(t)
            backbone_adjacency.setdefault(t, set()).add(s)

    graph_nodes = []
    for key, node in nodes_dict.items():
        valid_links = set()
        for target in node.get("links", []):
            target_key = aliases.get(target, target)
            if target_key in nodes_dict:
                valid_links.add(target_key)
        # Combine explicitly declared links with high-salience backbone edges
        valid_links.update(backbone_adjacency.get(key, set()))

        local_degree = backbone_degrees.get(key, 0)
        graph_nodes.append({
            "id": key,
            "nid": node.get("id", ""),
            "name": node.get("title", key),
            "group": (node.get("categories") or ["Uncategorized"])[0] if isinstance(node.get("categories"), list) else (node.get("categories") or "Uncategorized"),
            "raw_type": key.split("_")[0] if "_" in key else str(node.get("type", "unknown")).capitalize(),
            "community": str(communities.get(key) or ""),
            "degree": degree_map.get(key, 0),
            "backbone_degree": local_degree,
            "is_orphan": local_degree == 0,
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("sources", []),
            "semantic_links": sorted(valid_links),
            "node_kind": "page",
            "status": node.get("status", ""),
            "domain": node.get("domain", ""),
            "alignment_score": node.get("alignment_score", 100),
            "decay_weight": node.get("decay_weight", 1.0),
        })

    return {
        "nodes": graph_nodes,
        "edges": backbone_edges,
        "community_labels": index_data.get("community_labels", {}),
        "total_raw_edges": len(raw_weighted_edges),
    }


def _build_claim_graph(
    claim_graph: dict,
    max_edges: int = DEFAULT_MAX_CLAIM_EDGES,
) -> dict:
    claim_graph = claim_graph or {}
    raw_nodes = claim_graph.get("nodes", [])
    raw_edges = claim_graph.get("edges", [])
    valid_claim_ids = {node.get("id") for node in raw_nodes if node.get("id")}

    # Extract backbone / clean edges for claims
    clean_claim_edges, backbone_claim_degrees = _extract_backbone_edges(
        raw_edges=raw_edges,
        valid_node_ids=valid_claim_ids,
        max_edges=max_edges,
        min_weight=0.0,
    )

    adjacency: dict[str, list[str]] = {}
    for edge in clean_claim_edges:
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target:
            continue
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, []).append(source)

    nodes = []
    for node in raw_nodes:
        nid = node.get("id")
        if not nid:
            continue
        nodes.append({
            "id": nid,
            "name": node.get("name", nid),
            "group": node.get("group", "Claim"),
            "degree": node.get("degree", 0),
            "backbone_degree": backbone_claim_degrees.get(nid, 0),
            "updated": node.get("updated", ""),
            "summary": node.get("summary", ""),
            "sources": node.get("source_pages", []),
            "semantic_links": adjacency.get(nid, []),
            "node_kind": "claim",
            "validity_state": node.get("validity_state", "unknown"),
            "claim_type": node.get("claim_type", "claim"),
            "confidence": node.get("confidence"),
            "subject_entities": node.get("subject_entities", []),
        })

    return {
        "nodes": nodes,
        "edges": clean_claim_edges,
        "community_labels": {},
        "total_raw_edges": len(raw_edges),
    }


def _build_graph_payload(
    index_data: dict,
    claim_graph_data: dict | None = None,
    max_page_edges: int = DEFAULT_MAX_PAGE_EDGES,
    max_claim_edges: int = DEFAULT_MAX_CLAIM_EDGES,
    min_edge_weight: float = 0.0,
    read_consistency: str = "locked",
) -> dict:
    page_graph = _build_page_graph(index_data, max_edges=max_page_edges, min_weight=min_edge_weight)
    claim_graph = _build_claim_graph(claim_graph_data or {}, max_edges=max_claim_edges)
    
    total_raw_page_edges = page_graph.pop("total_raw_edges", len(page_graph["edges"]))
    total_raw_claim_edges = claim_graph.pop("total_raw_edges", len(claim_graph["edges"]))

    from vector_lake.indexer import is_clustering_stale

    meta = {
        "total_page_nodes": len(page_graph["nodes"]),
        "total_page_edges": total_raw_page_edges,
        "rendered_page_edges": len(page_graph["edges"]),
        "orphan_page_nodes": sum(1 for node in page_graph["nodes"] if node.get("is_orphan")),
        "total_claim_nodes": len(claim_graph["nodes"]),
        "total_claim_edges": total_raw_claim_edges,
        "rendered_claim_edges": len(claim_graph["edges"]),
        "pruned": total_raw_page_edges > len(page_graph["edges"]),
        "max_page_edges": max_page_edges,
        # Community membership is a separate, operator-invoked step; a dirty
        # graph_state means the partition shown here predates the node set.
        "communities_stale": is_clustering_stale(index_data),
        "community_labels_available": bool(page_graph["community_labels"]),
        "read_consistency": read_consistency,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # ``nodes``/``edges``/``community_labels`` used to be repeated at the top
    # level next to ``pageGraph``, which serialised all 7,124 page nodes -- with
    # their page summaries -- twice into the HTML artifact.  The bundled
    # template reads ``pageGraph``/``claimGraph``/``meta`` only.
    return {
        "pageGraph": page_graph,
        "claimGraph": claim_graph,
        "governanceMetrics": index_data.get("governance_metrics", {}),
        "meta": meta,
    }


def visualize_vector_lake(
    output_dir: str = None,
    max_edges: int = DEFAULT_MAX_PAGE_EDGES,
    min_edge_weight: float = 0.0,
    lock_timeout_seconds: float = DEFAULT_INDEX_LOCK_TIMEOUT_SECONDS,
    open_browser: bool = True,
):
    bootstrap = governance_store.ensure_canonical_store_populated()
    if bootstrap.get("bootstrapped"):
        from vector_lake import indexer

        indexer.generate_index()

    extension_root = get_extension_root()
    memory_dir = str(get_memory_dir())
    index_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    lock_path = index_path + ".lock"
    template_path = str(extension_root / "templates" / "topology.html")
    
    if output_dir:
        output_path = os.path.join(output_dir, "vector_lake_graph.html")
    else:
        output_path = _graph_output_path(memory_dir)


    if not os.path.exists(index_path):
        return "Error: Lake is drying. index.json not found. Please ingest sources first."
    if not os.path.exists(template_path):
        return "Error: template not found."

    read_consistency = "locked"
    try:
        with FileLock(lock_path, timeout=lock_timeout_seconds):
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
    except Timeout:
        log.warning(
            "Timeout acquiring lock for index.json during graph generation "
            f"({lock_timeout_seconds:g}s). Falling back to read-only mode."
        )
        read_consistency = "read_only_fallback"
        try:
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return "Error: System is busy generating the index. Please try again later."
    except json.JSONDecodeError:
        return "Error: Failed to parse index.json."

    try:
        with open(claim_graph_path, "r", encoding="utf-8") as handle:
            claim_graph = json.load(handle)
    except (OSError, json.JSONDecodeError):
        claim_graph = governance_store.build_claim_graph_projection()

    graph_data = _build_graph_payload(
        index_data=index_data,
        claim_graph_data=claim_graph,
        max_page_edges=max_edges,
        min_edge_weight=min_edge_weight,
        read_consistency=read_consistency,
    )

    with open(template_path, "r", encoding="utf-8") as handle:
        html = handle.read()

    # 🛡️ Sentinel: Escape HTML characters to prevent XSS when injecting JSON into a <script> block
    safe_graph_data = json.dumps(graph_data, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    html = html.replace("%%GRAPH_DATA%%", safe_graph_data)
    html = html.replace("%%MEMORY_BASE_PATH%%", f"file:///{memory_dir.replace(os.sep, '/')}/")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    should_open = (
        open_browser
        and not os.environ.get("VECTOR_LAKE_NO_BROWSER")
        and not os.environ.get("HEADLESS")
        and not os.environ.get("CI")
    )
    if should_open:
        try:
            webbrowser.open(f"file:///{output_path.replace(os.sep, '/')}")
        except Exception as e:
            log.warning(f"Failed to open browser automatically: {e}")
    meta = graph_data.get("meta", {})
    page_edges_str = f"{meta.get('rendered_page_edges', len(graph_data['pageGraph']['edges']))} backbone edges"
    if meta.get("pruned"):
        page_edges_str += f" (pruned from {meta.get('total_page_edges', 0)})"

    # Both conditions change what the picture means, so neither may stay
    # implicit: a stale partition renders community colours for a node set it
    # was not computed on, and an unlocked read can pair two projections from
    # different generations.
    warnings = []
    if meta.get("communities_stale"):
        warnings.append(
            "community assignment is stale; rerun scripts/community_clustering_daemon.py"
        )
    if read_consistency != "locked":
        warnings.append("index.json was read without its lock (index busy, degraded consistency)")
    warning_str = f" WARNING: {'; '.join(warnings)}." if warnings else ""

    return (
        f"Visualized {len(graph_data['pageGraph']['nodes'])} page nodes ({page_edges_str}) / "
        f"{len(graph_data['claimGraph']['nodes'])} claim nodes. Opened graph in browser: {output_path}"
        f"{warning_str}"
    )


def audit_graph() -> str:
    # Removed synchronous refresh_graph_topology_if_dirty()


    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return "Error: Lake is drying. index.json not found. Please ingest sources first."

    with open(index_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    insights = data.get("graph_insights", [])
    if not insights:
        return "No graph insights found. Please ensure 'sync' has been run recently."

    import uuid
    from datetime import datetime, timezone

    items = []
    for insight in insights:
        search_queries = [insight.get("node", "")] if insight.get("node") else []
        affected_pages = [f"wiki/{insight.get('node', '')}.md"] if insight.get("node") else []
        items.append({
            "item_id": f"gov_{uuid.uuid4().hex[:12]}",
            "type": "suggestion",
            # The node belongs in the title: audit_graph de-duplicates pending
            # items by title, so a node-less title collapsed every insight of a
            # given type onto a single queue entry.
            "title": f"Topology Insight: {insight['type'].replace('_', ' ').title()}: {insight.get('node', 'unknown')}",
            "description": insight.get("description", "A topological insight was detected."),
            "search_queries": search_queries,
            "affected_pages": affected_pages,
            "source": "audit-graph",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    if items:
        from vector_lake import governance_store
        with governance_store.governance_queue_session():
            queue = governance_store.load_governance_queue()
            existing_titles = {item.get("title") for item in queue.get("items", [])}
            new_items = [item for item in items if item["title"] not in existing_titles]
            if new_items:
                queue.setdefault("items", []).extend(new_items)
                governance_store.save_governance_queue(queue)
        if new_items:
            return f"Audit complete. Pushed {len(new_items)} new graph topology insights into the async review queue ({len(items) - len(new_items)} duplicates skipped)."
        else:
            return f"Audit complete. No new actionable insights found ({len(items)} existing insights already in queue)."
    return "Audit complete. No actionable insights found."

