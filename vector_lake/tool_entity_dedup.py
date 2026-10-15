import functools
import time
import json
import logging
import math
import re
from collections import Counter
from filelock import FileLock
from vector_lake import get_extension_root
from vector_lake.wiki_utils import get_index_path, normalize_memory_key

log = logging.getLogger("vector-lake-entity-dedup")


#: Tokenisation is a pure function of one string, and the duplicate check tokenises every existing
#: node's summary on every call, so the same 7 157 summaries were re-tokenised each time (1.07 s of
#: a 1.33 s similarity loop under cProfile).  A cache is exact here -- the same input yields the same
#: ``Counter`` -- and bounded at ~3x the node count, so it cannot grow with the corpus.  Callers only
#: read the returned Counter; nothing mutates it.
@functools.lru_cache(maxsize=20_000)
def _token_vector(text: str) -> Counter:
    """Token counts of ``text``: CJK characters, adjacent CJK bigrams, and doubled Latin words."""
    tokens: Counter = Counter()
    text = str(text).lower()
    cjk_chars = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text)
    for char in cjk_chars:
        tokens[char] += 1
    for i in range(len(cjk_chars) - 1):
        tokens[cjk_chars[i] + cjk_chars[i + 1]] += 1
    for word in re.findall(r"[a-z0-9]+", text):
        tokens[word] += 2
    return tokens


@functools.lru_cache(maxsize=20_000)
def _vector_norm_squared(text: str) -> float:
    """Squared norm of ``text``'s token vector, cached for the same reason as the vector itself."""
    vector = _token_vector(text)
    return float(sum(count ** 2 for count in vector.values()))


def _calculate_cosine_similarity(text1: str, text2: str) -> float:
    vec1 = _token_vector(text1)
    vec2 = _token_vector(text2)
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum([vec1[x] * vec2[x] for x in intersection])
    denominator = math.sqrt(_vector_norm_squared(text1)) * math.sqrt(_vector_norm_squared(text2))
    if not denominator:
        return 0.0
    return float(numerator) / denominator


def strip_name(name: str) -> str:
    name = name.replace('.md', '')
    for prefix in ['Concept_', 'Vendor_', 'Institution_', 'Person_', 'Product_', 'Event_', 'Policy_', 'Standard_', 'Synthesis_', 'Source_']:
        if name.startswith(prefix):
            name = name[len(prefix):]
    for w in ['系统', '架构', '模型', '法则', '理论', '平台', 'System', 'Model', 'Theory', 'Platform', '与', '的']:
        name = name.replace(w, '')
    name = re.sub(r'[\s_\-\(\)]+', '', name)
    return name.lower()


def _dedup_node_payloads() -> dict | None:
    """Node payloads for the duplicate check, from the ``page_index_nodes`` projection.

    The check walks every node once reading ``title``, ``type``, ``aliases`` and ``summary``, and
    used to parse the whole 17 MB ``index.json`` aggregate to do it.  The projection carries each
    node's payload verbatim (``node_json``) and was verified equal to the file on the live corpus --
    7 157 nodes, 0 missing, 0 extra, 0 differing payloads -- so this reads 7 157 rows instead.

    Returns ``None`` when the projection cannot be used, which leaves the file as the fallback
    rather than answering from a partial projection.
    """
    try:
        from vector_lake.db_store import get_connection

        rows = get_connection().execute(
            "SELECT node_key, node_json FROM page_index_nodes"
        ).fetchall()
    except Exception:  # noqa: BLE001 - a missing table or column simply means "use the file"
        return None
    if not rows:
        return None
    nodes: dict = {}
    for row in rows:
        try:
            nodes[str(row["node_key"])] = json.loads(row["node_json"])
        except (TypeError, ValueError):
            continue
    return nodes or None


def check_duplicate_entity(candidate_title: str, candidate_type: str, candidate_summary: str = "") -> str:
    """PIEA Hook: Check if an entity or concept already exists in the graph using hard normalization and cosine similarity.
    
    Args:
        candidate_title: The title of the entity to create.
        candidate_type: The type of the entity (e.g. 'entity', 'concept').
        candidate_summary: A brief summary of the entity to use for similarity matching.
    """
    # 1. Clean nested prefixes from candidate_title (e.g., "Concept_Person_XYZ" -> "XYZ")
    candidate_title = re.sub(r'^(Concept|Vendor|Institution|Product|Person|Event|Source|Synthesis)[_-]+', '', candidate_title, flags=re.IGNORECASE).strip()
    
    # 2. Normalize candidate_type (extract core type if nested, e.g., "concept_synthesis" -> "synthesis")
    candidate_type = candidate_type.strip().lower()
    if "synthesis" in candidate_type: candidate_type = "synthesis"
    elif "person" in candidate_type: candidate_type = "person"
    elif "event" in candidate_type: candidate_type = "event"
    elif "vendor" in candidate_type: candidate_type = "vendor"
    elif "institution" in candidate_type: candidate_type = "institution"
    elif "product" in candidate_type: candidate_type = "product"
    elif "concept" in candidate_type: candidate_type = "concept"
    
    if candidate_type not in ("vendor", "institution", "product", "person", "event", "concept", "synthesis"):
        return json.dumps({"is_duplicate": False, "reason": f"Type '{candidate_type}' does not require deduplication check."})

    index_path = get_index_path()
    if not index_path.exists():
        return json.dumps({"is_duplicate": False, "reason": "No index exists yet."})

    nodes = _dedup_node_payloads()
    if nodes is None:
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception as e:
            log.warning(f"Could not load index for PIEA check: {e}")
            return json.dumps({"is_duplicate": False, "reason": "Could not read index."})
        nodes = index_data.get("nodes", {})

    CONFIG_PATH = get_extension_root() / "config.json"
    threshold = 0.92
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            threshold = config.get("piea", {}).get("threshold", 0.92)
    except Exception:
        pass

    candidate_norm = normalize_memory_key(candidate_title)
    if not candidate_summary:
        candidate_summary = candidate_title

    for key, node in nodes.items():
        existing_title = node.get("title", "").strip('"').strip("'")
        existing_norm = normalize_memory_key(existing_title)
        existing_type = node.get("type", "unknown")

        # 1. Hard Normalization Match (Title or Aliases)
        aliases = node.get("aliases", [])
        if isinstance(aliases, list):
            alias_norms = [normalize_memory_key(a) for a in aliases]
            alias_stripped = [strip_name(a) for a in aliases]
        else:
            alias_norms = []
            alias_stripped = []
            
        candidate_stripped = strip_name(candidate_title)
        existing_stripped = strip_name(existing_title)
            
        is_hard_match = False
        if candidate_norm != "general" and (candidate_norm == existing_norm or candidate_norm in alias_norms):
            is_hard_match = True
        elif len(candidate_stripped) > 2 and (candidate_stripped == existing_stripped or candidate_stripped in alias_stripped):
            is_hard_match = True
            
        if is_hard_match:
            instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            if existing_type != candidate_type:
                instruction += f" Note: This entity is already registered as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": 1.0,
                "match_type": "hard_normalization_cross_type" if existing_type != candidate_type else "hard_normalization",
                "instruction": instruction
            })

        # 2. Fallback to Cosine Similarity
        existing_summary = node.get("summary") or existing_title
        sim = _calculate_cosine_similarity(candidate_summary, existing_summary)

        if sim >= threshold:
            instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            if existing_type != candidate_type:
                instruction += f" Note: This entity is already registered as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": sim,
                "match_type": "cosine_similarity_cross_type" if existing_type != candidate_type else "cosine_similarity",
                "instruction": instruction
            })

    # --- NEW CONCURRENCY LOGIC (Pending Entities Registry) ---
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pending_path = tmp_dir / "pending_entities.json"
    lock_path = tmp_dir / "pending_entities.lock"
    
    try:
        with FileLock(lock_path, timeout=15):
            pending_data = {}
            if pending_path.exists():
                try:
                    with open(pending_path, "r", encoding="utf-8") as f:
                        raw_data = json.load(f)
                        # Filter out entries older than 5 minutes (300s)
                        current_time = time.time()
                        pending_data = {
                            k: v for k, v in raw_data.items() 
                            if current_time - v.get("timestamp", 0) < 300
                        }
                except Exception:
                    pending_data = {}
            
            # Check against pending entities being created by other concurrent workers
            for key, node in pending_data.items():
                existing_title = node.get("title", "")
                existing_norm = normalize_memory_key(existing_title)
                existing_type = node.get("type", "unknown")
                
                # 1. Hard Normalization Match (Pending)
                candidate_stripped = strip_name(candidate_title)
                existing_stripped = strip_name(existing_title)
                
                is_hard_match = False
                if candidate_norm == existing_norm and candidate_norm != "general":
                    is_hard_match = True
                elif len(candidate_stripped) > 2 and candidate_stripped == existing_stripped:
                    is_hard_match = True
                    
                if is_hard_match:
                    instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead (currently being built by another worker)."
                    if existing_type != candidate_type:
                        instruction += f" Note: This entity is being created as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
                    return json.dumps({
                        "is_duplicate": True,
                        "existing_key": key,
                        "existing_title": existing_title,
                        "similarity": 1.0,
                        "match_type": "hard_normalization_pending",
                        "instruction": instruction
                    })
                
                # 2. Fallback to Cosine Similarity (Pending)
                existing_summary = node.get("summary") or existing_title
                sim = _calculate_cosine_similarity(candidate_summary, existing_summary)
                if sim >= threshold:
                    instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead (currently being built by another worker)."
                    if existing_type != candidate_type:
                        instruction += f" Note: This entity is being created as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
                    return json.dumps({
                        "is_duplicate": True,
                        "existing_key": key,
                        "existing_title": existing_title,
                        "similarity": sim,
                        "match_type": "cosine_similarity_pending",
                        "instruction": instruction
                    })

            # If not found in index OR pending, register it as pending for other workers
            from vector_lake.wiki_utils import normalize_entity_name
            new_key = normalize_entity_name(f"{candidate_type.capitalize()}_{candidate_title}")
            
            pending_data[new_key] = {
                "title": candidate_title,
                "type": candidate_type,
                "summary": candidate_summary,
                "timestamp": time.time()
            }
            
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump(pending_data, f, ensure_ascii=False)
                
            return json.dumps({
                "is_duplicate": False, 
                "instruction": f"Safe to create new entity. You MUST use the exact filename: {new_key}.md"
            })
                
    except Exception as e:
        log.error(f"Error accessing pending entities lock: {e}")

    # Fallback return if lock fails
    from vector_lake.wiki_utils import normalize_entity_name
    new_key = normalize_entity_name(f"{candidate_type.capitalize()}_{candidate_title}")
    return json.dumps({
        "is_duplicate": False, 
        "instruction": f"Safe to create new entity. You MUST use the exact filename: {new_key}.md"
    })

