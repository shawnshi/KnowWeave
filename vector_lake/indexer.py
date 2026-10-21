import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake import db_store
from vector_lake import tokenizer as _tokenizer
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_index_path,
    get_wiki_dir,
    read_markdown_file,
    flush_durable,
    fsync_directory,
    VALID_PREFIXES,
)

from vector_lake.link_resolution import (
    build_link_map,
    core_name_maps,
    declared_names_from_nodes,
    resolve_link_target,
)
from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES
from vector_lake.relation_policy import (
    PRED_WEIGHT_TAXONOMY as PRED_WEIGHT_TAXONOMY,
    PRED_WEIGHT_RELATION as PRED_WEIGHT_RELATION,
    PRED_WEIGHT_MENTION as PRED_WEIGHT_MENTION,
    pair_relation_weight,
    predicate_weight,
)
from vector_lake.schema_validator import validate_schema, SchemaViolationException

try:
    import vector_lake_core
    HAVE_CORE = True
except ImportError:
    vector_lake_core = None
    HAVE_CORE = False

# Community detection moved to Leiden (igraph + leidenalg) and lives in
# scripts/community_clustering_daemon.py; this module never clustered anything.


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-indexer")

DEFAULT_TTL = {
    "source": 365,
    "synthesis": 730,
    "vendor": 1095,
    "product": 1095,
    "person": 1095,
    "event": 1095,
    "concept": 1825,
    "policy": 1095,
    "standard": 1095,
}

RELEVANCE_WEIGHTS = {
    "direct_link": 3.0,
    "source_overlap": 4.0,
    "common_neighbor": 1.5,
    "type_affinity": 1.0,
}

# Degree bound for the page-space edge set.  A full rebuild has always applied
# it; the incremental path did not, which is how the live index grew to 1.6M
# edges (mean degree 487, max 4118) against a documented limit of 15.
MAX_EDGES_PER_NODE = 15

# index.json.lock is shared with the outbox consumer's partial updates.
INDEX_LOCK_TIMEOUT_SECONDS = 120

def get_pred_weight(pred: str) -> float:
    return predicate_weight(pred, RELEVANCE_WEIGHTS["direct_link"])


def _resolved_triple_weights(triples, alias_map, unique_cores, valid_keys):
    weights = {}
    for triple in triples or []:
        target = triple.get("target")
        if not target:
            continue
        target = resolve_link_target(target, alias_map, unique_cores)
        if target not in valid_keys:
            continue
        weight = get_pred_weight(triple.get("predicate", "mentions"))
        weights[target] = max(weights.get(target, weight), weight)
    return weights

TYPE_AFFINITY = {
    "vendor": {"vendor": 0.8, "product": 1.2, "person": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "product": {"vendor": 1.2, "product": 0.8, "person": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "person": {"vendor": 1.0, "product": 1.0, "person": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "event": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 0.8, "concept": 1.2, "source": 1.0, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "concept": {"vendor": 1.2, "product": 1.2, "person": 1.2, "event": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2, "policy": 1.0, "standard": 1.0},
    "source": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 0.5, "synthesis": 1.0, "policy": 1.0, "standard": 1.0},
    "synthesis": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.2, "source": 1.0, "synthesis": 0.8, "policy": 1.0, "standard": 1.0},
    "policy": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 1.0, "synthesis": 1.0, "policy": 0.8, "standard": 1.2},
    "standard": {"vendor": 1.0, "product": 1.0, "person": 1.0, "event": 1.0, "concept": 1.0, "source": 1.0, "synthesis": 1.0, "policy": 1.2, "standard": 0.8},
}

LEGACY_EMBEDDED_INDEX_KEYS = (
    "claim_graph",
    "claim_index",
    "entity_index",
    "source_index",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wiki_dir() -> str:
    return str(get_wiki_dir())


def _empty_index_data() -> dict:
    return {
        "nodes": {},
        "aliases": {},
        "categories": set(),
        "weighted_edges": [],
        "error_log": [],
        "communities": {},
        "community_labels": {},
        "graph_insights": [],
        "graph_state": {
            "dirty": False,
            "clustering_stale": False,
            "reason": "",
            "updated_at": None,
        },
    }


def _load_index_unlocked(output_path: str) -> dict | None:
    if not os.path.exists(output_path):
        return None
    with open(output_path, "r", encoding="utf-8") as handle:
        return json.load(handle)



def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = text.lower()
    tokens = []
    words = re.findall(r'[a-z0-9]+', text)
    tokens.extend(words)
    chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
    tokens.extend(chinese_chars)
    for i in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[i] + chinese_chars[i+1])
    return tokens
def _tokenize_for_fts(text: str) -> str:
    return _tokenizer.tokenize_joined(text)

def _entity_body_text(entity_data: dict) -> str:
    """Full Markdown body of one canonical entity (not stored in index.json)."""
    return str(entity_data.get("raw_text") or "")


def _node_content_digest(node: dict, body_text: str = "") -> str:
    """Hash of every field that feeds the FTS row, used to skip re-tokenizing.

    The active tokenizer backend is part of the digest: rjieba (jieba-rs) and
    pure-Python jieba produce different token streams, so previously cached
    nodes must be re-tokenized instead of silently mixing two segmentations
    in one index.
    """
    aliases = node.get("aliases") or []
    payload = "\x00".join([
        str(node.get("title") or ""),
        str(node.get("summary") or ""),
        "|".join(sorted(str(a) for a in aliases)) if isinstance(aliases, list) else str(aliases),
        str(body_text or ""),
        f"tokenizer={_tokenizer.backend_name()}",
    ])
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _sync_search_index(index_data: dict, bodies: dict, embeddings_map: dict) -> dict:
    """Incrementally reconcile the FTS projection with the canonical node set.

    One transaction per changed node keeps every write-lock hold short instead
    of freezing the whole database for the duration of a full-corpus rebuild.
    Unchanged nodes are skipped entirely, so repeat rebuilds are near-free.
    """
    nodes = index_data.get("nodes") or {}
    state = db_store.search_index_state()
    materialised = db_store.search_index_keys()

    # Reconcile against the real FTS content, not just the hash ledger: legacy or
    # externally inserted rows have no state entry and would otherwise survive.
    stale_keys = sorted(materialised - set(nodes))
    for node_key in stale_keys:
        db_store.delete_search_index(node_key)

    changed = []
    for node_key, node in nodes.items():
        digest = _node_content_digest(node, bodies.get(node_key, ""))
        if node_key not in materialised or state.get(node_key) != digest:
            changed.append((node_key, node, digest))

    stats = {
        "total": len(nodes),
        "reused": len(nodes) - len(changed),
        "tokenized": len(changed),
        "removed": len(stale_keys),
        "materialised_before": len(materialised),
    }
    if not changed:
        log.info(
            "Search index already current: %s nodes reused, %s removed.",
            stats["reused"],
            stats["removed"],
        )
        return stats

    log.info(
        "Tokenizing %s/%s nodes (%s reused, %s removed)...",
        stats["tokenized"], stats["total"], stats["reused"], stats["removed"],
    )
    materialised = set(materialised)
    for index, (node_key, node, digest) in enumerate(changed):
        if index and index % 500 == 0:
            log.info("Tokenized %s/%s changed nodes...", index, len(changed))
        # ``materialised`` is the real FTS key set, read above, so membership answers "does this row
        # exist?" without asking the virtual table -- which is the difference between inserting the
        # missing rows and scanning the whole index before each of them (measured 52.1 ms/row).
        _write_search_row(
            node_key, node, digest, bodies, embeddings_map,
            replace_existing=node_key in materialised,
        )
    return stats


def _write_search_row(node_key, node, digest, bodies, embeddings_map, replace_existing=None) -> None:
    """Write one page into the FTS projection (one transaction, owned by ``upsert_search_index``).

    **Batching these writes was tried and measured on 2026-09-25**: one transaction per 200 rows
    instead of per row changed the cost from **66.1 ms to 64.8 ms per row** (~472 s to ~463 s for a
    cold rebuild of 7 139 rows), i.e. commit overhead is not what makes this expensive, and the
    batching was reverted rather than kept for 2%.

    What the profile says instead: **87% of a rebuild's samples are inside
    ``upsert_search_index``**, and with commits ruled out the remaining suspect is the
    ``DELETE FROM wiki_search_index WHERE node_key = ?`` in front of the insert -- an FTS5 table
    cannot serve a lookup on a *column*, which is the same mechanism that made the FTS coverage
    anti-join take 271 s.  The structural fix is to delete by ``rowid`` (the state table already
    keys the node) rather than by ``node_key``, and that is a schema change, not a port.
    """
    aliases = node.get("aliases") or []
    aliases_str = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else str(aliases)
    text = f"{aliases_str} {bodies.get(node_key, '')}"
    db_store.upsert_search_index(
        node_key,
        _tokenize_for_fts(node.get("title", "")),
        _tokenize_for_fts(node.get("summary", "")),
        _tokenize_for_fts(text),
        content_hash=digest,
        replace_existing=replace_existing,
    )
    if node_key in embeddings_map:
        db_store.upsert_embedding(node_key, embeddings_map[node_key])




def _strip_legacy_embedded_payloads(index_data: dict) -> list[str]:
    removed = []
    # Strip any heavy artifacts that might have been saved in index.json previously
    for key in ["bm25_index", "governance_queue", "alias_registry", "entities"]:
        if key in index_data:
            del index_data[key]
            removed.append(key)
    return removed


def _strip_system_nodes(index_data: dict) -> list[str]:
    """Keep System_ pages outside the user-facing search projection in warm and cold paths."""
    nodes = index_data.get("nodes") or {}
    removed = [str(key) for key in nodes if str(key).startswith("System_")]
    if not removed:
        return []
    removed_set = set(removed)
    for key in removed:
        nodes.pop(key, None)
    index_data["aliases"] = {
        key: value
        for key, value in (index_data.get("aliases") or {}).items()
        if key not in removed_set and value not in removed_set
    }
    index_data["weighted_edges"] = [
        edge
        for edge in (index_data.get("weighted_edges") or [])
        if edge.get("source") not in removed_set and edge.get("target") not in removed_set
    ]
    return removed


def _write_json_payload(output_path: str, data: dict):
    temp_path = output_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        flush_durable(handle)
    import time
    for attempt in range(5):
        try:
            os.replace(temp_path, output_path)
            fsync_directory(os.path.dirname(output_path) or ".")
            return
        except PermissionError as e:
            if attempt < 4:
                time.sleep(0.1 * (2 ** attempt))
            else:
                log.critical(f"Failed to write {output_path} due to file lock after 5 attempts. Index update aborted.")
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                raise e

def _write_json_stage(stage_path: str, data: dict):
    with open(stage_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        flush_durable(handle)


def _refresh_page_projection(index_data: dict) -> None:
    """Bring the read projection in step with the ``index.json`` just written.

    Done by the writer, not lazily on read: the ingest path rewrites
    ``index.json`` once per page, so a lazy rebuild would cost a full parse per
    search, and readers no longer rebuild at all (see
    ``page_index_projection.read_catalog``).

    A failure is recorded rather than raised: the file is already published and
    stays sovereign, readers fall back to it, and ``heal_projection_if_stale`` --
    the outbox consumer's job -- retries under the write lock it can afford.
    """
    from vector_lake import page_index_projection

    try:
        page_index_projection.refresh_page_index_projection(index_data)
        page_index_projection.clear_projection_stale()
    except Exception as exc:  # noqa: BLE001 - the file stays sovereign; reads self-heal
        # Do not swallow this silently: the next reader inherits the rebuild, and
        # under contention that rebuild is exactly what it cannot get.  Recording
        # the failure lets the reader name the cause instead of reporting a bare
        # lock timeout, and leaves an operator-visible trace of who lost the race.
        log.warning("Page index projection refresh failed: %s: %s", type(exc).__name__, exc)
        try:
            page_index_projection.mark_projection_stale(f"{type(exc).__name__}: {exc}")
        except Exception as marker_exc:  # noqa: BLE001 - the marker is best-effort
            log.warning("Could not record projection staleness: %s", marker_exc)


def _write_index(output_path: str, index_data: dict):
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
    _write_json_payload(output_path, index_data)
    _refresh_page_projection(index_data)


def _write_claim_graph(output_path: str, claim_graph_data: dict):
    _write_json_payload(output_path, claim_graph_data)


def _mark_graph_dirty(index_data: dict, reason: str):
    """Records that both the edge set and the community assignment are stale.

    A node/edge change invalidates the Leiden partition too, so the two flags
    are set together and cleared by different owners: ``dirty`` by the
    topology refresh in this module, ``clustering_stale`` by the clustering
    daemon.
    """
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = True
    graph_state["clustering_stale"] = True
    graph_state["reason"] = reason
    graph_state["updated_at"] = _utc_now()


def _mark_graph_clean(index_data: dict):
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = False
    graph_state["clustering_stale"] = False
    graph_state["reason"] = ""
    graph_state["updated_at"] = _utc_now()


def is_graph_dirty(index_data: dict | None) -> bool:
    if not index_data:
        return True
    graph_state = (index_data.get("graph_state") or {})
    return bool(graph_state.get("dirty"))


def is_clustering_stale(index_data: dict | None) -> bool:
    """True when ``communities`` predates the current node/edge set.

    Readers that render community membership (the graph tool, the naming
    queue) must not present a stale partition as current.  Legacy payloads
    written before the flags were separated have no ``clustering_stale`` key;
    they fall back to ``dirty``, which was the only staleness signal then.
    """
    if not index_data:
        return True
    graph_state = index_data.get("graph_state") or {}
    if "clustering_stale" in graph_state:
        return bool(graph_state["clustering_stale"])
    return bool(graph_state.get("dirty"))


def _parse_wiki_node(filepath: str, node_key: str):
    try:
        fm_data, body, _ = read_markdown_file(filepath)
    except (UnicodeDecodeError, OSError) as e:
        log.warning(f"Cannot read {os.path.basename(filepath)}: {e}")
        return None
    except Exception as e:
        log.warning(f"Failed to parse frontmatter in {os.path.basename(filepath)}: {e}")
        return None

    if not fm_data:
        return None

    try:
        validate_schema(fm_data, body, os.path.basename(filepath))
    except SchemaViolationException as e:
        log.warning(f"Schema violation in {os.path.basename(filepath)}: {e}")
        # Note: In a real system we would append to error_log, but we only have node_key here.
        # We will let generate_index log this as a warning.
        return None

    node_id = fm_data.get("id", "")
    title = fm_data.get("title", node_key)

    raw_type = str(fm_data.get("type", "concept")).lower().strip().replace('"', "").replace("'", "")
    if raw_type in ["vendor", "product", "person", "event", "concept", "source", "synthesis"]:
        node_type = raw_type
    elif raw_type in ["entity", "organization"]:
        node_type = "vendor"
    elif raw_type in ["system", "project"]:
        node_type = "product"
    elif raw_type in ["reference"]:
        node_type = "source"
    elif raw_type in ["comparison", "report"]:
        node_type = "synthesis"
    else:
        node_type = raw_type if raw_type.isalnum() else "concept"

    updated = str(fm_data.get("updated", ""))
    categories = fm_data.get("categories", [])
    domain = fm_data.get("domain")
    topic_cluster = fm_data.get("topic_cluster", "General")
    status = fm_data.get("status")

    ttl = fm_data.get("ttl")
    if not isinstance(ttl, (int, float)):
        ttl = DEFAULT_TTL.get(node_type, 1095)

    decay_weight = 1.0
    try:
        updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - updated_dt).days
        if age_days > 0 and ttl > 0:
            decay_weight = 0.5 ** (age_days / ttl)
    except Exception:
        pass

    if not domain or not status:
        log.warning(f"Schema violation: Missing 'domain' or 'status' in {os.path.basename(filepath)}. Node excluded from index.")
        return None

    raw_aliases = fm_data.get("aliases", [])
    if isinstance(raw_aliases, list):
        aliases = [str(alias).strip() for alias in raw_aliases]
    elif isinstance(raw_aliases, str):
        aliases = [raw_aliases.strip()]
    else:
        aliases = []

    raw_sources = fm_data.get("sources", [])
    if isinstance(raw_sources, list):
        sources = [str(source).strip() for source in raw_sources if source]
    elif isinstance(raw_sources, str):
        sources = [raw_sources.strip()]
    else:
        sources = []

    # STQM: Extract tension edges from frontmatter
    raw_tension_edges = fm_data.get("tension_edges", [])
    tension_edges = []
    if isinstance(raw_tension_edges, list):
        for te in raw_tension_edges:
            if isinstance(te, dict) and te.get("target"):
                tension_edges.append({
                    "target": str(te.get("target")).strip(),
                    "polarity": float(te.get("polarity", 0.0)),
                    "intensity": float(te.get("intensity", 0.0)),
                    "context": str(te.get("context", "")).strip()
                })

    alignment_score = 100.0  # V7.2: Removed manual LLM alignment_score scoring. Handled algorithmically.

    links = set()
    triples = []
    
    # Strip code blocks to prevent parsing AST links inside markdown code sections
    import re
    clean_body = re.sub(r'```.*?```', '', body, flags=re.DOTALL)
    clean_body = re.sub(r'`.*?`', '', clean_body)
    
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        target = match.group(2).split("|")[0].strip().replace(".md", "")
        if target:
            links.add(target)
            triples.append({"predicate": predicate, "target": target})
            
    links.discard("")

    summary_text = re.sub(r"#.*?\n", "", body)
    summary_text = re.sub(r"\[([^\[\]]+?)::\s*\[\[.*?\]\]\]", "", summary_text)
    summary_text = re.sub(r"\[\[([^\]]*?\|)?([^\]]*?)\]\]", r"\2", summary_text)
    summary_text = summary_text.strip().replace("\n", " ")

    from vector_lake.wiki_utils import enforce_entity_dict
    return enforce_entity_dict({
        "id": node_id,
        "title": title,
        "type": node_type,
        "updated": updated,
        "categories": categories,
        "domain": domain,
        "topic_cluster": topic_cluster,
        "status": status,
        "aliases": aliases,
        "sources": sources,
        "tension_edges": tension_edges,
        "links": sorted(links),
        "triples": triples,
        "summary": summary_text[:240],
        "raw_text": body,
        "decay_weight": round(decay_weight, 4),
        "alignment_score": round(alignment_score, 2),
    })


def _entity_to_index_node(entity_data: dict, entity_id: str = "") -> tuple[str, dict]:
    """Project one canonical SQLite entity into the index read model."""
    node_key = str(
        entity_data.get("page_key")
        or os.path.splitext(str(entity_data.get("source_page") or ""))[0]
        or entity_data.get("canonical_name")
        or entity_id
    ).strip()
    if not node_key:
        raise ValueError("Canonical entity is missing page_key and fallback identity fields.")

    node_type = str(entity_data.get("type") or entity_data.get("entity_type") or "concept").lower()
    updated = str(entity_data.get("updated") or entity_data.get("updated_at") or "")
    ttl = entity_data.get("ttl")
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        ttl = DEFAULT_TTL.get(node_type, 1095)

    decay_weight = entity_data.get("decay_weight")
    if not isinstance(decay_weight, (int, float)):
        decay_weight = 1.0
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_days = max(0, (datetime.now(timezone.utc) - updated_dt).days)
            decay_weight = 0.5 ** (age_days / ttl)
        except (TypeError, ValueError):
            pass

    categories = entity_data.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]
    aliases = entity_data.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    sources = entity_data.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    links = entity_data.get("links") or entity_data.get("outbound_links") or []
    if isinstance(links, str):
        links = [links]

    title = str(entity_data.get("title") or entity_data.get("canonical_name") or node_key.replace("_", " "))
    node_data = {
        "id": entity_data.get("id") or entity_id or node_key,
        "title": title,
        "summary": entity_data.get("summary") or "",
        # raw_text is intentionally NOT stored here: index.json used to carry a
        # second full-text copy of every page, which multiplied storage and made
        # every read-model parse O(corpus).  Consumers take the body from
        # canonical SQLite via _entity_body_text().
        "type": node_type,
        "domain": entity_data.get("domain") or "General",
        "topic_cluster": entity_data.get("topic_cluster") or "General",
        "status": entity_data.get("status") or "Active",
        "epistemic_status": entity_data.get("epistemic-status") or entity_data.get("epistemic_status") or "draft",
        "categories": list(categories),
        "tags": entity_data.get("tags") or [],
        "aliases": list(aliases),
        "relations": entity_data.get("relations") or [],
        "sources": list(sources),
        "tension_edges": entity_data.get("tension_edges") or [],
        "links": list(links),
        "outbound_links": list(links),
        "triples": entity_data.get("triples") or [],
        "ttl": ttl,
        "decay_weight": round(float(decay_weight), 4),
        "alignment_score": float(entity_data.get("alignment_score", 100.0)),
        "node_score": round(float(decay_weight), 4),
        "updated": updated,
        "updated_at": updated,
    }
    return node_key, node_data


def calculate_relevance(node_a: dict, node_b: dict, all_nodes: dict,
                        links_a=None, links_b=None,
                        sources_a=None, sources_b=None,
                        type_a=None, type_b=None,
                        decay_a=None, decay_b=None,
                        align_a=None, align_b=None,
                        triples_a=None, triples_b=None) -> float:
    """Calculates relevance score between two nodes. O(N^2) hot path."""
    score = 0.0
    key_a = node_a.get("_key", "")
    key_b = node_b.get("_key", "")

    if links_a is None: links_a = frozenset((node_a.get("links") or []))
    if links_b is None: links_b = frozenset((node_b.get("links") or []))
    
    if triples_a is None or triples_b is None:
        alias_map = _link_map_for_nodes(all_nodes)
        _core_pages, unique_cores = core_name_maps(all_nodes.keys())
        if triples_a is None:
            triples_a = _resolved_triple_weights(node_a.get("triples"), alias_map, unique_cores, all_nodes)
        if triples_b is None:
            triples_b = _resolved_triple_weights(node_b.get("triples"), alias_map, unique_cores, all_nodes)
    weight_a, weight_b = triples_a.get(key_b), triples_b.get(key_a)
    # Keep the optional predicate-map calling convention while incremental callers
    # pass already-resolved numeric maxima, just like the native payload.
    if isinstance(weight_a, str):
        weight_a = get_pred_weight(weight_a)
    if isinstance(weight_b, str):
        weight_b = get_pred_weight(weight_b)
    score += pair_relation_weight(
        weight_a, weight_b, linked=key_b in links_a or key_a in links_b,
        direct_link=RELEVANCE_WEIGHTS["direct_link"],
    )

    if sources_a is None: sources_a = frozenset((node_a.get("sources") or []))
    if sources_b is None: sources_b = frozenset((node_b.get("sources") or []))

    # Optimization: Use isdisjoint() guard to prevent expensive set allocations in O(N^2) path
    if not sources_a.isdisjoint(sources_b):
        shared_sources = len(sources_a & sources_b)
        score += shared_sources * RELEVANCE_WEIGHTS["source_overlap"]

    # Optimization: Use isdisjoint() guard to prevent expensive set allocations in O(N^2) path
    if not links_a.isdisjoint(links_b):
        common_neighbors = links_a & links_b
        for neighbor_key in common_neighbors:
            neighbor = all_nodes.get(neighbor_key)
            if neighbor:
                links = neighbor.get("links")
                if links:
                    degree = len(links)
                    if degree > 1:
                        score += (1.0 / math.log(degree)) * RELEVANCE_WEIGHTS["common_neighbor"]

    if type_a is None: type_a = node_a.get("type", "concept").lower()
    if type_b is None: type_b = node_b.get("type", "concept").lower()

    type_a_dict = TYPE_AFFINITY.get(type_a)
    if type_a_dict:
        affinity = type_a_dict.get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
    else:
        affinity = 0.5 * RELEVANCE_WEIGHTS["type_affinity"]
    score += affinity

    if decay_a is None: decay_a = node_a.get("decay_weight", 1.0)
    if decay_b is None: decay_b = node_b.get("decay_weight", 1.0)
    
    if align_a is None:
        align_a = node_a.get("alignment_score", 100.0) / 100.0
        if align_a < 0.1: align_a = 0.1

    if align_b is None:
        align_b = node_b.get("alignment_score", 100.0) / 100.0
        if align_b < 0.1: align_b = 0.1
    
    score *= math.sqrt(decay_a * decay_b)
    score *= math.sqrt(align_a * align_b)

    return round(score, 3)


def dedupe_and_prune_edges(
    edges: list[dict],
    max_edges_per_node: int = MAX_EDGES_PER_NODE,
) -> list[dict]:
    """Normalise, deduplicate and degree-bound a page-space edge set.

    Enforces the two invariants every consumer assumes -- the SQLite
    published edge set (primary key ``source_id, target_id``),
    the renderer, and the clustering daemon that reads ``weighted_edges``:

    1. exactly one row per unordered ``(source, target)`` pair, stored as
       ``min``/``max`` so a pair written by two code paths cannot appear twice;
    2. no node above ``max_edges_per_node`` incident edges.

    Both invariants were previously maintained only on the full-rebuild path.
    ``update_index_items`` appended every qualifying edge for the touched node
    and re-added that node's rows from the projection without re-applying
    either rule, so the published set drifted to 1.6M edges for 7,125 nodes
    (mean degree 487.7, max 4,118, 314,660 duplicate pairs) while the
    projection it feeds held 1,293,183 deduplicated rows -- the file and its
    own projection disagreed.

    Kept as a single function so the two paths cannot drift apart again; it
    is deterministic (weight descending, then key ascending) so a rebuild and
    an incremental update converge on the same set.
    """
    best: dict[tuple[str, str], float] = {}
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target or source == target:
            continue
        key = (source, target) if source <= target else (target, source)
        weight = float(edge.get("weight") or 1.0)
        if weight > best.get(key, float("-inf")):
            best[key] = weight

    ordered = sorted(
        ((source, target, weight) for (source, target), weight in best.items()),
        key=lambda item: (-item[2], item[0], item[1]),
    )

    counts: dict[str, int] = {}
    pruned: list[dict] = []
    for source, target, weight in ordered:
        if counts.get(source, 0) >= max_edges_per_node:
            continue
        if counts.get(target, 0) >= max_edges_per_node:
            continue
        counts[source] = counts.get(source, 0) + 1
        counts[target] = counts.get(target, 0) + 1
        pruned.append({"source": source, "target": target, "weight": weight})
    return pruned


def _zero_evidence_below_threshold(
    max_affinity: float | None,
    multiplier: float,
    max_multiplier: float | None,
    min_relevance: float = 1.5,
) -> bool:
    """Skip zero-only evidence only when even its rounded upper bound loses.

    Nonfinite/negative inputs disable this optimization, never qualification.
    Preserve the scorer's multiplication grouping and rounding at the boundary.
    """
    if max_affinity is None or max_multiplier is None:
        return False
    if not all(math.isfinite(value) and value >= 0 for value in (max_affinity, multiplier, max_multiplier)):
        return False
    if not math.isfinite(min_relevance):
        return False
    bound = max_affinity * (multiplier * max_multiplier)
    return math.isfinite(bound) and round(bound, 3) < min_relevance


def _calculate_weighted_edges(index_data: dict, alias_map: dict | None = None) -> list[dict]:
    nodes_dict = index_data["nodes"]
    node_keys = list(nodes_dict.keys())

    for key, node in nodes_dict.items():
        node["_key"] = key

    edges = []

    # Resolution is one rule, shared with the linter (``vector_lake.link_resolution``).  It used to
    # be written out here as filenames + titles + aliases with last-writer-wins, which meant a link
    # by core name built no edge (``[[Concept_CoMET]]`` against ``Product_CoMET.md``) while the
    # linter accepted it, a declaration could overwrite another page's filename, and a page's id
    # resolved here but nowhere else.  Measured on the live wiki: 67 typed links were dropped this
    # way, 23 of which the core route resolves; zero depended on the id route.
    if alias_map is None:
        claims = declared_names_from_nodes(nodes_dict)
        alias_map, _core_pages, unique_cores, _contested = build_link_map(nodes_dict.keys(), claims)
    else:
        _core_pages, unique_cores = core_name_maps(nodes_dict.keys())

    # Pre-compute resolved links and sources sets for O(1) access inside the nested loop
    node_links = {}
    node_types = {}
    node_triples = {}
    node_sources = {}
    node_degrees = {}
    declared_weights = {}

    for key, node in nodes_dict.items():
        resolved_links = set()
        for link in (node.get("links") or []):
            resolved_links.add(resolve_link_target(link, alias_map, unique_cores) or link)
        node_links[key] = frozenset(resolved_links)

        node_types[key] = node.get("type", "concept").lower()
        
        declared_weights[key] = _resolved_triple_weights(
            node.get("triples"), alias_map, unique_cores, nodes_dict,
        )

        node_sources[key] = frozenset((node.get("sources") or []))

        links_len = len(node.get("links") or [])
        if links_len > 1:
            node_degrees[key] = (1.0 / math.log(links_len)) * RELEVANCE_WEIGHTS["common_neighbor"]
        else:
            node_degrees[key] = 0.0

    # Preserve the native target->float ABI without inventing links or reverse
    # facts. One existing link/declaration direction owns the pair's MAX; an
    # existing opposite direction carries zero. Canonical triples stay intact.
    for key in node_keys:
        node_triples[key] = dict.fromkeys(node_links[key] | declared_weights[key].keys(), 0.0)
    for key, targets in node_triples.items():
        for target in targets:
            reverse = node_triples.get(target, {})
            if key in reverse and key > target:
                continue
            targets[target] = pair_relation_weight(
                declared_weights[key].get(target), declared_weights.get(target, {}).get(key),
                linked=target in node_links[key] or key in node_links.get(target, ()),
                direct_link=RELEVANCE_WEIGHTS["direct_link"],
            )

    # Match native candidate retention for declarations not present in links.
    # This does not add candidates: only existing source/neighbor postings count.
    triple_only_endpoints = set()
    for key, targets in node_triples.items():
        for target in targets:
            if target in nodes_dict and target not in node_links[key]:
                triple_only_endpoints.update((key, target))

    # Bolt Optimization: Pre-populate node_degrees with 0.0 for all unresolved links
    # to avoid expensive dictionary .get() fallbacks in the O(N^2) inner loop.
    for links in node_links.values():
        for link in links:
            if link not in node_degrees:
                node_degrees[link] = 0.0

    default_affinity = 0.5 * RELEVANCE_WEIGHTS["type_affinity"]

    # Pre-populate nested dictionary for ALL type combinations to allow fast O(1)
    # direct dictionary lookups instead of expensive .get() fallbacks in the O(N^2) hot loop
    all_types_observed = set(node_types.values())
    all_types_precomp = set(TYPE_AFFINITY.keys()) | all_types_observed

    type_affinity_precomputed = {}
    for type_a in all_types_precomp:
        type_affinity_precomputed[type_a] = {}
        for type_b in all_types_precomp:
            a_dict = TYPE_AFFINITY.get(type_a)
            if a_dict:
                affinity_val = a_dict.get(type_b, 0.5) * RELEVANCE_WEIGHTS["type_affinity"]
            else:
                affinity_val = default_affinity
            type_affinity_precomputed[type_a][type_b] = affinity_val
    overlap_weight = RELEVANCE_WEIGHTS["source_overlap"]

    node_multipliers = {}
    for key, node in nodes_dict.items():
        decay = node.get("decay_weight", 1.0)
        align = node.get("alignment_score", 100.0) / 100.0
        if align < 0.1: align = 0.1
        node_multipliers[key] = math.sqrt(decay * align)

    source_to_nodes = {}
    for key, sources in node_sources.items():
        for source in sources:
            source_to_nodes.setdefault(source, []).append(key)
            
    _temp_reverse_links = {}
    for key, links in node_links.items():
        for link in links:
            _temp_reverse_links.setdefault(link, []).append(key)

    reverse_links = {k: frozenset(v) for k, v in _temp_reverse_links.items()}

    if HAVE_CORE and vector_lake_core is not None and hasattr(vector_lake_core, "fast_calculate_weighted_edges"):
        payload = {}
        for key in node_keys:
            payload[key] = {
                "type": node_types[key],
                "links": list(node_links[key]),
                "sources": list(node_sources[key]),
                "triples": node_triples[key],
                "multiplier": node_multipliers[key],
                "degree_weight": node_degrees.get(key, 0.0),
            }
        for node in nodes_dict.values():
            node.pop("_key", None)
        return vector_lake_core.fast_calculate_weighted_edges(
            payload, type_affinity_precomputed, overlap_weight, 1.5, MAX_EDGES_PER_NODE
        )

    # Include the native API's missing-type fallback (0.5) conservatively.
    affinity_values = [0.5] + [value for row in type_affinity_precomputed.values() for value in row.values()]
    max_affinity = max(affinity_values) if all(math.isfinite(value) for value in affinity_values) else None
    multiplier_values = list(node_multipliers.values())
    max_multiplier = max(multiplier_values, default=0.0) if all(math.isfinite(value) and value >= 0 for value in multiplier_values) else None

    for key_a in node_keys:
        links_a = node_links[key_a]
        sources_a = node_sources[key_a]
        type_a = node_types[key_a]
        triples_a = node_triples[key_a]
        multiplier_a = node_multipliers[key_a]
        affinity_dict_a = type_affinity_precomputed[type_a]

        candidate_source_overlaps = {}
        candidate_neighbor_scores = {}
        skip_zero_postings = key_a not in triple_only_endpoints and _zero_evidence_below_threshold(max_affinity, multiplier_a, max_multiplier)

        if overlap_weight != 0.0 or not skip_zero_postings:
            for source in sources_a:
                for key_b in source_to_nodes.get(source, []):
                    if key_a < key_b:
                        candidate_source_overlaps[key_b] = candidate_source_overlaps.get(key_b, 0) + 1

        for neighbor in links_a:
            degree_weight = node_degrees[neighbor]
            if degree_weight == 0.0 and skip_zero_postings:
                continue
            for key_b in reverse_links.get(neighbor, []):
                if key_a < key_b:
                    candidate_neighbor_scores[key_b] = candidate_neighbor_scores.get(key_b, 0.0) + degree_weight

        candidates = set(candidate_source_overlaps.keys())
        candidates.update(candidate_neighbor_scores.keys())
        
        for key_b in links_a:
            if key_a < key_b:
                candidates.add(key_b)
        
        for key_b in reverse_links.get(key_a, []):
            if key_a < key_b:
                candidates.add(key_b)

        for key_b in candidates:
            if key_b not in node_links:
                continue

            type_b = node_types[key_b]
            multiplier_b = node_multipliers[key_b]

            score = 0.0

            score += triples_a.get(key_b, 0.0)
            score += node_triples[key_b].get(key_a, 0.0)

            if key_b in candidate_source_overlaps:
                score += candidate_source_overlaps[key_b] * overlap_weight

            if key_b in candidate_neighbor_scores:
                score += candidate_neighbor_scores[key_b]

            score += affinity_dict_a[type_b]

            score *= multiplier_a * multiplier_b
            relevance = round(score, 3)

            if relevance >= 1.5:
                edges.append({
                    "source": key_a,
                    "target": key_b,
                    "weight": relevance,
                })

    for node in nodes_dict.values():
        node.pop("_key", None)

    # Page-space edges are derived from canonical links/source overlap only.  The claim-space edge
    # table lives in a different key space and the database holds a single projection of this set
    # (``page_index_edges``), so nothing is merged in here.
    return dedupe_and_prune_edges(edges)


def _link_map_for_nodes(nodes: dict) -> dict[str, str]:
    """The link map for an in-memory node set, built by the shared rule.

    The incremental update path needs the same map the full build publishes; deriving it from the
    nodes it already holds keeps the two paths on one rule instead of two.
    """
    link_map, _core_pages, _unique_cores, _contested = build_link_map(
        nodes.keys(), declared_names_from_nodes(nodes)
    )
    return link_map


def _declared_names_for_nodes(nodes: dict) -> dict[str, list[str]]:
    """The same declaration map, named for the caller that needs it beside the core table."""
    return declared_names_from_nodes(nodes)


def _apply_graph_topology(index_data: dict):
    """Marks the edge topology as current and the community assignment as stale.

    This runs after ``weighted_edges`` is (re)computed, so ``dirty`` is
    cleared here -- previously this function set ``dirty = True`` again, so the
    flag could never be cleared, ``refresh_graph_topology_if_dirty`` reported
    work on every pass, and the centrality placeholders below were the only
    scores ever written (live index: 4,645 nodes at ``centrality_score = 1.0``,
    2,478 with no key at all).  Clustering is a separate, operator-invoked step
    and stays marked stale until the daemon runs.
    """
    graph_state = index_data.setdefault("graph_state", {})
    graph_state["dirty"] = False
    graph_state["clustering_stale"] = True
    graph_state["reason"] = "Edge topology current; community clustering pending"
    graph_state["updated_at"] = _utc_now()
    
    # Initialize basic node scores so BM25 doesn't crash
    node_keys = list(index_data["nodes"].keys())
    for node_key in node_keys:
        node = index_data["nodes"][node_key]
        node["centrality_score"] = 1.0
        node["node_score"] = round(node.get("decay_weight", 1.0), 4)


def generate_index(skip_embeddings: bool = True):
    """Full rebuild of the index projection under the shared index lock.

    The lock is what makes a rebuild safe against the outbox consumer's partial
    updates: without it a long rebuild clobbered every write that landed while
    it was running, and those outbox rows were already marked complete.
    """
    if db_store.in_write_transaction():
        raise RuntimeError("Index publication must start outside a SQLite transaction")
    output_path = str(get_index_path())
    try:
        with FileLock(output_path + ".lock", timeout=INDEX_LOCK_TIMEOUT_SECONDS):
            return _generate_index_locked(skip_embeddings=skip_embeddings)
    except Timeout as exc:
        raise TimeoutError(
            f"Could not acquire the index lock for {output_path} within "
            f"{INDEX_LOCK_TIMEOUT_SECONDS}s; another rebuild is in progress."
        ) from exc


def _generate_index_locked(skip_embeddings: bool = True):
    index_data = _empty_index_data()
    from vector_lake.db_store import get_connection, init_db

    # The projection reads ``entities.f_page_key``; a database predating that column must converge
    # before the SELECT runs, or the whole rebuild dies on a missing column.
    init_db()
    conn = get_connection()

    # Read from canonical SQLite instead of Markdown files
    rows = conn.execute("SELECT entity_id, data_json FROM entities").fetchall()
    node_bodies: dict[str, str] = {}
    #: Declared names (titles and aliases) -> the pages claiming them, collected as the nodes are
    #: projected so the alias index can be built with the same rules the edges use.
    claims: dict[str, list[str]] = {}

    for row in rows:
        try:
            entity_data = json.loads(row["data_json"])
            node_key, node_data = _entity_to_index_node(entity_data, row["entity_id"])
        except Exception as e:
            log.warning(f"Failed to project canonical entity {row['entity_id']}: {e}")
            continue
        if node_key.startswith("System_") or node_data.get("type") == "system":
            continue

        node_bodies[node_key] = _entity_body_text(entity_data)
        index_data["nodes"][node_key] = node_data
        if node_data.get("title"):
            claims.setdefault(str(node_data["title"]).strip(), []).append(node_key)
        for alias in (node_data.get("aliases") or []):
            claims.setdefault(str(alias).strip(), []).append(node_key)
        if isinstance(node_data.get("categories"), list):
            for category in node_data["categories"]:
                index_data["categories"].add(category)

    # One map for the edges and for the alias index the topology layer reads, so the two cannot
    # disagree about what a link names.  A page's ``id`` is deliberately absent: it is an
    # identifier, not a name (measured: no link on the live wiki depends on it).
    alias_map, _core_pages, _unique_cores, _contested = build_link_map(
        index_data["nodes"].keys(), claims
    )
    index_data["aliases"] = dict(alias_map)

    index_data["weighted_edges"] = _calculate_weighted_edges(index_data, alias_map)
    _apply_graph_topology(index_data)
    
    index_data["categories"] = list(index_data["categories"])
    index_data["governance_metrics"] = governance_metrics.compute_debt_metrics(skip_heavy=True)
    index_data["schema_version"] = "8.0"

    output_path = str(get_index_path())
    claim_graph_path = str(get_claim_graph_path())
    stage_suffix = f".{uuid.uuid4().hex}.tmp"
    tmp_output = output_path + stage_suffix
    tmp_claim = claim_graph_path + stage_suffix
    
    _write_json_stage(tmp_claim, governance_store.build_claim_graph_projection())
    removed = _strip_legacy_embedded_payloads(index_data)
    if removed:
        log.info(f"Stripped legacy embedded payloads before writing index: {', '.join(removed)}")
    _write_json_stage(tmp_output, index_data)

    # Embeddings are a separate resumable projection. Index rebuilds never call an external API.
    embeddings_map = {}
    db_store.delete_stale_embeddings(set(index_data["nodes"]))
    # Each projection below commits per unit of work.  Holding one BEGIN IMMEDIATE
    # across a full-corpus tokenization froze every other writer for minutes.
    search_stats = _sync_search_index(index_data, node_bodies, embeddings_map)

    for staged_path in (tmp_claim, tmp_output):
        if not os.path.exists(staged_path):
            raise FileNotFoundError(f"Missing staged projection file before publish: {staged_path}")
    os.replace(tmp_claim, claim_graph_path)
    os.replace(tmp_output, output_path)
    fsync_directory(os.path.dirname(output_path) or ".")

    # The published file is now the source of truth this projection mirrors, so
    # the writer that published it also refreshes it.  Without this, a full
    # rebuild left the projection stale until some reader rebuilt it -- which is
    # precisely the coupling that made every query compete for the write lock.
    _refresh_page_projection(index_data)

    log.info(
        f"Generated index.json with {len(index_data['nodes'])} nodes | "
        f"{len(index_data['weighted_edges'])} weighted edges | "
        f"{len((index_data.get('error_log') or []))} errors | "
        f"search index tokenized={search_stats['tokenized']} reused={search_stats['reused']}."
    )
    return output_path


def update_index_items(filenames: list[str]):
    if not filenames:
        return

    # Filter valid files
    valid_filenames = []
    # Same precondition as the full build: the canonical lookup below names ``f_page_key``.
    db_store.init_db()
    for filename in filenames:
        if not filename.endswith(".md") or filename in NON_NODE_WIKI_FILES or filename.startswith("System_"):
            continue
        valid_filenames.append(filename)
        
    if not valid_filenames:
        return
    if db_store.in_write_transaction():
        raise RuntimeError("Index publication must start outside a SQLite transaction")

    # Pre-parse canonical nodes. Embedding refresh is handled by the explicit backfill scheduler.
    pre_parsed_data = {}
    pre_parsed_bodies = {}
    canonical_load_errors = {}
    import os

    conn = db_store.get_connection()
    for filename in valid_filenames:
        node_key = filename[:-3]
        try:
            row = conn.execute(
                "SELECT entity_id, data_json FROM entities "
                "WHERE f_page_key = ? LIMIT 1",
                (node_key,),
            ).fetchone()
            if row:
                entity_data = json.loads(row["data_json"])
                pre_parsed_bodies[node_key] = _entity_body_text(entity_data)
                projected_key, node_data = _entity_to_index_node(entity_data, row["entity_id"])
                if projected_key != node_key:
                    raise ValueError(f"Canonical page_key mismatch: expected {node_key}, got {projected_key}")
            else:
                node_data = None
        except Exception as exc:
            log.error(f"Failed to load canonical entity for {filename}: {exc}")
            canonical_load_errors[filename] = str(exc)
            continue
        if node_data:
            pre_parsed_data[node_key] = node_data

    if canonical_load_errors:
        detail = "; ".join(f"{name}: {error}" for name, error in sorted(canonical_load_errors.items()))
        raise RuntimeError(f"Canonical index batch aborted; source rows could not be loaded: {detail}")

    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        return generate_index()

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    try:
        with FileLock(lock_path, timeout=15):
            from vector_lake.db_store import transaction
            sql_actions = []
            try:
                index_data = _load_index_unlocked(output_path)
            except json.JSONDecodeError:
                needs_full_rebuild = True
                index_data = None

            if index_data is None:
                needs_full_rebuild = True
            else:
                removed_system_keys = _strip_system_nodes(index_data)
                for system_key in removed_system_keys:
                    sql_actions.append((db_store.delete_search_index, (system_key,), {}))
                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads in index.json "
                        f"({', '.join(removed_legacy_keys)}). Triggering full rebuild."
                    )
                    needs_full_rebuild = True
                    index_data = None

            if index_data is not None:
                if isinstance(index_data.get("categories"), list):
                    index_data["categories"] = set(index_data["categories"])

                # The maps the per-node derivation resolves against, built once for the batch
                # exactly as the full build builds them: ``_link_map_for_nodes`` uses the shared
                # declaration rule and ``core_name_maps`` the shared core rule, so the pairs and
                # the weights this touches cannot drift from the ones a rebuild would produce.
                #
                # ``pre_parsed_data`` is merged in first: it holds the pages this very batch is
                # about, so a link to a name the batch introduces (or whose title/alias it
                # changes) resolves now rather than one batch later.  Building the maps from the
                # post-batch set is what a rebuild would do, and this is that set for the pages
                # the batch touches.
                nodes_for_maps = {**(index_data.get("nodes") or {}), **pre_parsed_data}
                alias_map = _link_map_for_nodes(nodes_for_maps)
                _core_pages, unique_cores = core_name_maps(
                    nodes_for_maps.keys(), _declared_names_for_nodes(nodes_for_maps)
                )

                # Every node's triples, with their targets resolved: ``calculate_relevance`` reads
                # the *other* node's predicate weight from this map, so leaving the targets raw
                # meant a typed link written by name (title, alias or core name) was scored as a
                # plain mention.  The difference is large enough that a pair with low decay or
                # alignment fell under the gate and vanished -- which the read-back used to hide.
                # ``pre_parsed_data`` holds the pages this batch is about, so a page introduced by
                # it is present before its own pass runs.
                all_nodes_triples = {}
                for k, v in nodes_for_maps.items():
                    all_nodes_triples[k] = _resolved_triple_weights(
                        (v or {}).get("triples"), alias_map, unique_cores, nodes_for_maps,
                    )

                for filename in valid_filenames:
                    node_key = filename[:-3]

                    if not filename.startswith(VALID_PREFIXES) and filename not in ("index.md", "log.md"):
                        index_data.setdefault("error_log", [])
                        index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]
                        index_data["error_log"].append({"file": filename, "error": "Schema violation: Missing valid entity prefix."})
                        log.warning(f"Schema violation in {filename} during partial update.")
                        old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                        if old_node:
                            sql_actions.append((db_store.delete_search_index, (node_key,), {}))
                        index_data["weighted_edges"] = [
                            edge for edge in (index_data.get("weighted_edges") or [])
                            if edge["source"] != node_key and edge["target"] != node_key
                        ]
                    else:
                        # The alias map is rebuilt from every node below, through the one
                        # shared rule; the old code patched entries here and re-added them
                        # (including the page's ``id``) with plain assignment, which is how the
                        # incremental path came to reuse a different resolution rule than the
                        # full build -- the difference the read-back used to paper over.
                        index_data.setdefault("error_log", [])
                        index_data["error_log"] = [item for item in index_data["error_log"] if item.get("file") != filename]

                        node_data = pre_parsed_data.get(node_key)
                        if node_data is None:
                            old_node = (index_data.get("nodes") or {}).pop(node_key, None)
                            if old_node:
                                sql_actions.append((db_store.delete_search_index, (node_key,), {}))
                            index_data["weighted_edges"] = [
                                edge for edge in (index_data.get("weighted_edges") or [])
                                if edge["source"] != node_key and edge["target"] != node_key
                            ]
                        else:
                            if node_data is not None:
                                index_data["nodes"][node_key] = node_data
                                aliases_str = " ".join((node_data.get("aliases") or [])) if isinstance(node_data.get("aliases"), list) else ""
                                body_text = pre_parsed_bodies.get(node_key, "")
                                text = f"{aliases_str} {body_text}"
                                t_title = _tokenize_for_fts(node_data.get('title', ''))
                                t_summary = _tokenize_for_fts(node_data.get('summary', ''))
                                t_text = _tokenize_for_fts(text)
                                sql_actions.append((
                                    db_store.upsert_search_index,
                                    (node_key, t_title, t_summary, t_text),
                                    {"content_hash": _node_content_digest(node_data, body_text)},
                                ))
                                # The stale vector must not outlive the rewrite, and this path
                                # must not call an embedding provider (pinned by
                                # ``test_incremental_index_invalidates_stale_vector_without_api``),
                                # so invalidation here is deliberate.  The counterpart that puts
                                # the vector back is the periodic sweep
                                # (``periodic_catch_up._embedding_catch_up``); the old comment
                                # promised "explicit backfill", which only ever ran when an
                                # operator remembered, and the projection shrank to 2 602 of
                                # 7 175 nodes before that was noticed on 2026-09-21.
                                sql_actions.append((db_store.delete_embedding, (node_key,), {}))
                                

                                if isinstance(node_data["categories"], list):
                                    categories = set((index_data.get("categories") or []))
                                    for category in node_data["categories"]:
                                        categories.add(category)
                                    index_data["categories"] = categories

                                index_data["weighted_edges"] = [
                                    edge for edge in (index_data.get("weighted_edges") or [])
                                    if edge["source"] != node_key and edge["target"] != node_key
                                ]
                                node_data["_key"] = node_key
                                all_nodes = index_data["nodes"]

                                # Resolution happens here, not only in the full build.  This loop
                                # used to compare *raw* link strings, so a link by core name or
                                # alias produced no pair at all -- the difference the read-back
                                # that used to follow it had been compensating for, which is why a
                                # link deleted from a page kept its edge (the projection was the
                                # source of that edge rather than the derivation).
                                def _resolved(names):
                                    return {
                                        resolve_link_target(name, alias_map, unique_cores) or name
                                        for name in names
                                    }

                                triples_a = all_nodes_triples[node_key]
                                
                                node_links = _resolved(node_data.get("links") or [])
                                node_sources = set((node_data.get("sources") or []))
                                for other_key, other_node in all_nodes.items():
                                    if other_key == node_key:
                                        continue
                                        
                                    other_links = _resolved(other_node.get("links") or [])
                                    other_sources = set((other_node.get("sources") or []))
                                    triples_b = all_nodes_triples.get(other_key)
                                    
                                    has_direct = other_key in node_links or node_key in other_links
                                    # Optimization: Replace bool(set1 & set2) with not isdisjoint() to prevent allocating a new set just to check for overlap
                                    has_source_overlap = not node_sources.isdisjoint(other_sources)
                                    has_common_neighbor = not node_links.isdisjoint(other_links)
                                    
                                    if not (has_direct or has_source_overlap or has_common_neighbor):
                                        continue

                                    # The pair is scored in the orientation the full build uses:
                                    # "a" is the lexicographically smaller key, because
                                    # TYPE_AFFINITY is asymmetric and the full build only ever asks
                                    # for key_a < key_b.  Scoring from the updated node instead gave
                                    # the same pair a different weight.
                                    if node_key < other_key:
                                        first, second = node_data, other_node
                                        first_key, second_key = node_key, other_key
                                        first_links, second_links = node_links, other_links
                                        first_sources, second_sources = node_sources, other_sources
                                        first_triples, second_triples = triples_a, triples_b
                                    else:
                                        first, second = other_node, node_data
                                        first_key, second_key = other_key, node_key
                                        first_links, second_links = other_links, node_links
                                        first_sources, second_sources = other_sources, node_sources
                                        first_triples, second_triples = triples_b, triples_a
                                    first["_key"] = first_key
                                    second["_key"] = second_key
                                    relevance = calculate_relevance(
                                        first, second, all_nodes,
                                        links_a=first_links, links_b=second_links,
                                        sources_a=first_sources, sources_b=second_sources,
                                        triples_a=first_triples, triples_b=second_triples
                                    )
                                    if relevance >= 1.5:
                                        index_data["weighted_edges"].append({
                                            "source": first_key,
                                            "target": second_key,
                                            "weight": relevance,
                                        })
                                    other_node.pop("_key", None)
                                node_data.pop("_key", None)

                # Once per batch, after every branch: rebuilding inside the loop cost
                # O(batch x corpus) under the index lock, and the deletion branch (which pops
                # the node) never rebuilt at all, leaving a removed page's names resolvable.
                index_data["aliases"] = _link_map_for_nodes(index_data["nodes"])
                _mark_graph_dirty(index_data, f"Partial batch update for {len(valid_filenames)} items")
                # Re-apply the shared cap and pair-level dedup after the batch: the loop above
                # appends every qualifying edge for each touched node, so the published set has
                # to be normalised back to weighted_edges' documented contract (one row per
                # unordered pair, min/max orientation, degree cap).
                index_data["weighted_edges"] = dedupe_and_prune_edges(
                    index_data.get("weighted_edges") or []
                )
                index_data["categories"] = list((index_data.get("categories") or []))
                # Do not recompute heavy debt metrics on partial update
                index_data["governance_metrics"] = (index_data.get("governance_metrics") or {})
                index_data["schema_version"] = "8.0"
                # Independent canonical writers may commit while planning holds only
                # the index lock. Read all graph collections from one WAL generation.
                with db_store.read_snapshot():
                    claim_graph = governance_store.build_claim_graph_projection()

            # Only the batch's SQL changes share a write transaction. The index file
            # lock still serializes planning/publication; slow graph and JSON work
            # must not monopolize SQLite's database-wide writer lock.
            if sql_actions:
                with transaction():
                    for operation, args, kwargs in sql_actions:
                        operation(*args, **kwargs)
            if index_data is not None:
                _write_index(output_path, index_data)
                _write_claim_graph(str(get_claim_graph_path()), claim_graph)
    except Timeout:
        raise TimeoutError(f"Timeout while acquiring lock for {output_path}")

    if needs_full_rebuild:
        return generate_index()

def update_index_item(filename: str):
    """Legacy single file entrypoint."""
    return update_index_items([filename])


def refresh_graph_topology_if_dirty() -> bool:
    """Refresh graph topology, releasing every lock before a full rebuild.

    A full rebuild must not run while this function still holds the index lock
    or an open transaction; it acquires both itself.
    """
    if db_store.in_write_transaction():
        raise RuntimeError("Index publication must start outside a SQLite transaction")
    output_path = str(get_index_path())
    if not os.path.exists(output_path):
        generate_index()
        return True

    lock_path = output_path + ".lock"
    needs_full_rebuild = False
    changed = False

    # Decide *outside* any transaction.  This used to open the write transaction first, so every
    # occurrence took the database write lock -- and held it across a 17 MB parse -- even when the
    # answer was "nothing to do".  Measured: complete under ``PRAGMA query_only=ON`` it raised
    # "attempt to write a readonly database" immediately, i.e. the write lock was taken before the
    # decision.  The probe below is a pure read, and the writing path re-loads inside the
    # transaction exactly as before, so nothing about the write path changes.
    #
    # The probe can only skip work, never perform it: a writer that sets ``dirty`` just after this
    # read is caught by the next refresh (and leaves its flag set in the meantime), whereas the
    # previous arrangement could overwrite such a flag while holding the lock.
    try:
        probe = _load_index_unlocked(output_path)
    except json.JSONDecodeError:
        probe = None
    if probe is not None:
        stripped_system = _strip_system_nodes(probe)
        stripped_legacy = _strip_legacy_embedded_payloads(probe)
        if not stripped_system and not stripped_legacy and not is_graph_dirty(probe):
            return False

    try:
        with FileLock(lock_path, timeout=INDEX_LOCK_TIMEOUT_SECONDS):
            from vector_lake.db_store import transaction

            try:
                index_data = _load_index_unlocked(output_path)
            except json.JSONDecodeError:
                index_data = None

            if index_data is None:
                needs_full_rebuild = True
            else:
                removed_system_keys = _strip_system_nodes(index_data)
                if removed_system_keys:
                    with transaction():
                        for system_key in removed_system_keys:
                            db_store.delete_search_index(system_key)

                removed_legacy_keys = _strip_legacy_embedded_payloads(index_data)
                if removed_legacy_keys:
                    log.info(
                        "Detected legacy embedded governance payloads during graph refresh "
                        f"({', '.join(removed_legacy_keys)}). Triggering full rebuild."
                    )
                    needs_full_rebuild = True
                elif is_graph_dirty(index_data):
                    _apply_graph_topology(index_data)
                    _write_index(output_path, index_data)
                    log.info("Graph topology partially refreshed and saved.")
                    changed = True
    except Timeout:
        log.error(f"Timeout while acquiring lock for {output_path}")
        return False

    if needs_full_rebuild:
        generate_index()
        return True
    return changed


def _replace_with_retry(temp_path: str, output_path: str) -> None:
    # The staged file is synced before the swap so a caller cannot publish a torn
    # projection by forgetting to flush its own handle.
    try:
        with open(temp_path, "rb") as staged:
            os.fsync(staged.fileno())
    except OSError as exc:
        log.warning(f"Could not fsync staged {temp_path}: {exc}")
    for attempt in range(5):
        try:
            os.replace(temp_path, output_path)
            fsync_directory(os.path.dirname(output_path) or ".")
            return
        except PermissionError as exc:
            if attempt < 4:
                time.sleep(0.1 * (2 ** attempt))
                continue
            log.error(f"Failed to write {output_path} due to file lock after 5 attempts.")
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise exc


if __name__ == "__main__":
    generate_index()

