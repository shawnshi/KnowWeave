import hashlib
import os
import re
from datetime import datetime, timezone

from vector_lake.wiki_utils import canonical_source_name, enforce_claim_dict, normalize_sources
from vector_lake.node_vocabulary import is_generated_artifact
from vector_lake.schema_validator import validate_schema, SchemaViolationException
import logging

log = logging.getLogger("vector-lake-claim-extractor")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Which implementation produces the claim blocks: ``rust`` (default) or ``python``.
CLAIM_BLOCK_BACKEND = os.environ.get("VECTOR_LAKE_CLAIM_BLOCKS", "rust").strip().lower()


#: The value of ``vector_lake_core.blocks_contract()`` this module's Rust path requires.
BLOCKS_CONTRACT = "claim-blocks-parity-2026-09-25"


def _rust_blocks(body: str):
    """``[(kind, heading, raw_text)]`` from the Rust core, or ``None`` to fall back to mistune.

    Returns ``None`` -- never raises -- for every reason the old path should be used: the backend
    switch, a body with the bytes the two parsers disagree on, or a missing/short core.  A block
    extractor must not turn an optional accelerator into a failure mode.

    The capability check is on ``blocks_contract()``, not on ``hasattr(fast_extract_blocks)``: the
    pre-2026-09-25 build exported that name with different semantics (280-character cleaning, nested
    items emitted, nested headings moving ``current_heading``), so a presence check would accept a
    build that silently changes the claim corpus.  That is not hypothetical -- it was the live state
    on 2026-09-25 until the wheel was replaced.
    """
    if CLAIM_BLOCK_BACKEND != "rust":
        return None
    if "\x00" in body or "\ufffd" in body:
        return None
    try:
        import vector_lake_core
    except ImportError:
        return None
    if getattr(vector_lake_core, "blocks_contract", None) is None:
        return None
    if vector_lake_core.blocks_contract() != BLOCKS_CONTRACT:
        log.warning(
            "vector_lake_core.blocks_contract() is %r, expected %r; using mistune.",
            vector_lake_core.blocks_contract(), BLOCKS_CONTRACT,
        )
        return None
    try:
        return [
            (block.kind, block.heading, block.raw_text)
            for block in vector_lake_core.fast_extract_blocks(body)
        ]
    except Exception as exc:  # noqa: BLE001 - fail open to mistune, loudly enough to be greppable
        log.warning("Rust block extraction failed (%s: %s); using mistune.", type(exc).__name__, exc)
        return None


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=12).hexdigest()
    return f"{prefix}_{digest}"


def _jsonable(value):
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return str(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _collapse_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# Reader-facing markers written by the page-shaping tools (``tool_query``, the
# lint stub builder) and by the page template itself.  They are emitted *inside*
# the compiled-truth and timeline sections, so the heading rule below turns them
# into claims even though they record nothing.
SYSTEM_DIRECTIVE_MARKER = "[System Directive:"
# The template's section caption, written as a paragraph above the ledger.  On the live
# corpus 199 claims are exactly ``(Timeline - EVENT STORE)``: no date, no proposition.
TIMELINE_CAPTION_PREFIXES = ("(timeline", "timeline - event store")
# The scope disclaimer the ingest template puts above the ledger.  On the live corpus 345
# claims carry that same sentence.
PAGE_SCOPE_MARKER = "本页只记录证据"

# The heading markers that decide a block's claim type.  They live here, next to the rule that
# uses them, because a bare ``证据`` used to qualify a block as a ledger entry -- which typed
# every evidence-boundary/limitation section as an event log: on the live corpus 1595 claims
# came from headings like ``证据边界`` and 99% of them carried no date.
COMPILED_TRUTH_HEADING_MARKERS = ("编译事实", "compiled truth", "事实", "truth")
LEDGER_HEADING_MARKERS = ("证据时间线", "时间线", "timeline", "event store")


def claim_type_for_heading(kind: str, heading: str, text: str) -> str:
    """The claim type the heading rule gives one block of a page.

    Named rather than inlined in :func:`extract_page_objects` because the classification is
    also re-derived against already-stored claims (``scripts/reclassify_timeline_claims.py``)
    when the rule changes; two copies of it would drift.
    """
    heading_lower = (heading or "").lower()
    if any(k in heading_lower for k in COMPILED_TRUTH_HEADING_MARKERS):
        return "compiled-truth"
    if (
        any(k in heading_lower for k in LEDGER_HEADING_MARKERS)
        and not _is_page_scope_disclaimer(text)
    ):
        return "timeline-event"
    return _claim_type_for_block(kind)


#: Bookkeeping the maintenance tooling wrote *into* the ledger, prefixed with a date so it became
#: a dated timeline entry: 5 live claims say nothing but ``Node auto-migrated to V11 schema``.  The
#: temporal prefix is deliberately kept in a claim's text (see ``_parse_temporal``), so the check
#: strips it and compares what is left.
BOOKKEEPING_BODIES = ("node auto-migrated to v11 schema",)


#: Whole-block placeholders.  The synthesis skeleton asks a page to declare its sections before the
#: analysis exists, so an unfinished page legitimately says ``待补充``; mined as a claim that becomes
#: an *Active* assertion, which is how ``待补充`` reached the live claim index as ``claim_e09b8236...``.
PLACEHOLDER_BODIES = (
    "待补充",
    "待补齐",
    "待定",
    "待确认",
    "待核实",
    "tbd",
    "todo",
    "to be added",
    "n/a",
)

#: Query/run narration.  A page may describe the run it was written from, but that state belongs to
#: the run: two live claims recorded a *packet's* ``[superseded]`` warning list, and by the time
#: anyone read them the ids they named were gone from ``operational_memory``.
RUN_NARRATION_MARKERS = (
    "operational memory packet",
    "operational-memory packet",
    "query packet",
    "runtime packet",
    "运行记忆 packet",
    "运行记忆packet",
    "查询 packet",
)


def _is_run_state_or_placeholder(text) -> bool:
    """True for a block that carries no proposition: a placeholder or run narration."""
    normalized = _collapse_text(text).lower()
    if not normalized:
        return False
    stripped = re.sub(r"^(?:\s*\[[^\]]*\]\s*)+", "", normalized).strip()
    stripped = stripped.strip(".!。:：")
    if stripped in PLACEHOLDER_BODIES:
        return True
    return any(marker in normalized for marker in RUN_NARRATION_MARKERS)


def _is_page_boilerplate(text) -> bool:
    """True for a block that only carries a template caption or a reader instruction.

    Neither is a claim, but both sit under a ``证据时间线``/``编译事实`` heading.  A
    directive's text is rewritten on every reshape (it embeds a ``Last Reshaped`` date),
    and because the timeline projection id is content-addressed, each reshape moved the id
    and orphaned the previous ``timeline_events`` row -- the one defect that kept
    regenerating timeline-projection drift.  The caption is markup: no date and no
    proposition, yet it reached the timeline as an undated "event" that the projection then
    dated with its *ingestion* time.

    ``BOOKKEEPING_BODIES`` is the same family found later: a migration left its own marker as a
    dated ledger entry, which the current code would happily re-mint because the date prefix makes
    it look like a real observation.
    """
    normalized = _collapse_text(text).lower()
    if not normalized:
        return False
    if SYSTEM_DIRECTIVE_MARKER.lower() in normalized:
        return True
    stripped = re.sub(r"^(?:\s*\[[^\]]*\]\s*)+", "", normalized).strip().strip(".!。")
    if stripped in BOOKKEEPING_BODIES:
        return True
    return any(normalized.startswith(prefix) for prefix in TIMELINE_CAPTION_PREFIXES)


def _is_page_scope_disclaimer(text) -> bool:
    """The scope sentence the ingest template puts above the ledger.

    It says how the page was built, so it stays a claim -- it is just not a ledger entry.
    Reaching the timeline it became an undated "event" on 345 live rows, each dated by the
    projection with its own ingestion time.
    """
    return _collapse_text(text).lower().startswith(PAGE_SCOPE_MARKER)


def _body_summary(body: str, limit: int = 320) -> str:
    """The page's own first words, not its scaffolding.

    The fallback used to be the first ``limit`` characters of the raw body, which on every page
    starts with the template's headings and -- on 3 214 live pages -- the system directive.  The
    resulting ``summary`` claim therefore carried markup and a reader instruction instead of
    knowledge, its ``page-summary`` evidence recorded the directive as ``evidence_text``, and since
    the summary is re-minted from the body on every extraction, re-extracting those pages could not
    remove the row: a 200-page sample showed the marker surviving all 169 pages whose body held no
    other offending block.  Skipping the scaffolding (the same rule the block loop uses) makes the
    summary say something, and turns the old row into one the next extraction replaces.
    """
    for block in _iter_blocks(body):
        text = block.get("text") or ""
        if (
            not text
            or _is_page_boilerplate(text)
            or _is_page_boilerplate(block.get("raw_text"))
            or _is_run_state_or_placeholder(text)
        ):
            continue
        # Cleaned: this string becomes both a claim text and the ``page-summary`` evidence text, so
        # raw markup and inline anchors must not travel into either.
        return _clean_claim_text(text, limit=limit)
    return _collapse_text(body)[:limit]


def _heading_to_text(line: str) -> str:
    return re.sub(r"^#+\s*", "", line).strip()


def _clean_claim_text(text: str, limit: int = 360) -> str:
    cleaned = _collapse_text(text)
    # Strip inline sources completely to reduce RAG noise
    cleaned = re.sub(r"\(Source[s]?:\s*(.*?)\)", "", cleaned, flags=re.IGNORECASE)
    # Strip typed links first: [predicate:: [[Target|Alias]]] -> Alias or [predicate:: [[Target]]] -> Target
    cleaned = re.sub(r"\[([^\[\]]+?)::\s*\[\[([^\]|]+)\|([^\]]+)\]\]\]", r"\3", cleaned)
    cleaned = re.sub(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", r"\2", cleaned)
    # Then strip legacy links
    cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
    return cleaned[:limit]


def _iter_blocks(body: str) -> list[dict]:
    """Blocks claim extraction consumes, from the Rust core when it agrees with mistune.

    The Rust `fast_extract_blocks` is a parity port of the mistune walk below (measured 2026-09-25:
    1495/1500 pages, 20 720 blocks, identical `kind`/`heading`/`raw_text`; 13.6 s -> 0.2 s per
    corpus).  Two carve-outs keep the remainder from changing derived data:

    * **bodies carrying NUL or U+FFFD bytes** stay on mistune.  Those four pages are the only place
      the two parsers were measured to disagree by content (2-6 characters), and the live claim
      corpus contains **none** of those bytes -- so the Rust path would be introducing them, not
      reproducing mistune;
    * **`VECTOR_LAKE_CLAIM_BLOCKS=python`** forces the old path, because a block extractor feeds
      120k claim rows and a switch is cheaper than trust.

    One further known difference is *not* carved out: `Concept_GAIN-矩阵.md`, where mistune folds a
    column-0 `# ` heading into the preceding list while pulldown-cmark ends the list, so one block
    is attributed to that heading.  It is a single block on a single page, recorded in the
    CHANGELOG rather than hidden.
    """
    rust_blocks = _rust_blocks(body)
    if rust_blocks is not None:
        return [
            {
                "kind": kind,
                "heading": heading,
                "text": _clean_claim_text(raw_text),
                "raw_text": raw_text,
            }
            for kind, heading, raw_text in rust_blocks
        ]

    import mistune
    markdown = mistune.create_markdown(renderer='ast')
    ast = markdown(body or "")
    
    blocks = []
    current_heading = None

    def extract_text(node) -> str:
        if isinstance(node, dict):
            if node.get("type") == "block_code":
                return " "
            if node.get("type") in ("softbreak", "hardbreak"):
                return " "
            text = node.get("raw", "")
            for child in node.get("children", []):
                text += extract_text(child)
            return text
        return ""

    def process_node(node):
        nonlocal current_heading
        if node["type"] == "heading":
            current_heading = extract_text(node).strip()
        elif node["type"] == "paragraph":
            raw_text = extract_text(node).strip()
            text = _clean_claim_text(raw_text)
            if text:
                blocks.append({
                    "kind": "paragraph",
                    "heading": current_heading,
                    "text": text,
                    "raw_text": raw_text,
                })
        elif node["type"] == "list":
            for child in node.get("children", []):
                if child["type"] == "list_item":
                    raw_text = extract_text(child).strip()
                    text = _clean_claim_text(raw_text)
                    if text:
                        blocks.append({
                            "kind": "bullet",
                            "heading": current_heading,
                            "text": text,
                            "raw_text": raw_text,
                        })

    for node in ast:
        process_node(node)

    return blocks


def _claim_type_for_block(kind: str) -> str:
    if kind == "bullet":
        return "bullet-claim"
    return "assertion"


def _validity_defaults(frontmatter: dict) -> dict:
    return {
        "valid_from": _jsonable(frontmatter.get("valid_from")),
        "valid_to": _jsonable(frontmatter.get("valid_to")),
        "review_after": _jsonable(frontmatter.get("review_after")),
        "freshness_tier": frontmatter.get("freshness_tier", "unknown"),
        "temporal_anchor": frontmatter.get("temporal_anchor"),
        "memory_type": frontmatter.get("memory_type"),
        "memory_key": frontmatter.get("memory_key"),
        "authority_score": frontmatter.get("authority_score"),
        "importance_score": frontmatter.get("importance_score"),
        "reinforcement_count": frontmatter.get("reinforcement_count"),
        "ttl_days": frontmatter.get("ttl_days") or frontmatter.get("ttl"),
    }


def _link_target_resolver():
    """The node index's ``resolve(name) -> page key | None``, or ``None`` if there is no index.

    Deferred on purpose: the projection module reads the database at call time, and this module is
    imported by the store that writes it, so the edge points up one scope rather than at load.
    """
    try:
        from vector_lake.page_index_projection import link_target_resolver
    except ImportError:  # pragma: no cover - the projection module is always present
        return None
    return link_target_resolver()


def _source_key(value: str) -> str:
    """The form in which two spellings of one source are compared.

    ``sources`` keeps the extension (``normalize_raw_ref`` is what the store records) while
    ``_parse_inline_sources`` strips ``.md``, so comparing the two raw made the multi-source gate
    unsatisfiable: on a page declaring more than one source, no block could ever attach evidence,
    whatever anchor it wrote.  Measured on the live corpus: of 400 pages declaring two or more
    sources, exactly one carried any evidence, and its blocks were anchored to source *pages* (an
    id the append branch invents) rather than to a declared file.  926 claims still sit behind
    that gate.  One question, one comparison.
    """
    return str(value or "").strip().replace(".md", "")


def extract_page_objects(
    page_path: str,
    frontmatter: dict,
    body: str,
    resolve_target=None,
) -> dict:
    """Entity, claim, evidence, source and edge records for one page.

    ``resolve_target`` maps a link target to the page it names (``link_resolution`` is the owner
    of that question; ``page_index_projection.link_target_resolver`` builds it from the index).
    It is only applied to an edge's ``target_id`` -- the field that is a *key* -- and never to
    ``links`` or ``triples``, which record what the page declared.  Without it the target is
    stored as written, which is what every caller did until the claim graph was measured: 232 of
    10 487 live ``claim_graph_edges`` rows pointed at a name no page answers, 230 of them at a
    name the resolver could answer, because the indexer resolved and this extractor did not.

    When it is omitted the index-backed resolver is used, so a caller that has nothing to pass
    still gets one answer for the question instead of two.  A page with no index to resolve
    against keeps the literal target, as before.
    """
    if resolve_target is None:
        resolve_target = _link_target_resolver()
    now = _utc_now()
    page_name = os.path.basename(page_path)
    page_key = os.path.splitext(page_name)[0]
    # A page the wiki generates about itself is not knowledge, and the ingestion and index layers
    # already treat it that way -- ``indexer`` skips the namespace outright.  This extractor did
    # not, so it minted claims from cluster indexes: 29 392 of the 47 636 claims the debt report
    # called unsupported sat on those pages, 99.1% of everything extracted from them, because a
    # generated index records no source by design and its Hubs/Members lines are not assertions.
    if is_generated_artifact(frontmatter, page_name, body):
        log.debug("Skipping generated artifact %s: the wiki's own bookkeeping, not knowledge.", page_name)
        return {
            "entities": [],
            "claims": [],
            "evidence": [],
            "sources": [],
            "edges": [],
            "page_key": page_key,
            "page_type": str(frontmatter.get("type", "system")).lower(),
        }
    title = frontmatter.get("title", page_key)
    page_type = str(frontmatter.get("type", "concept")).lower()
    aliases = frontmatter.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    aliases = [str(alias).strip() for alias in aliases if alias and str(alias).strip()]
    sources = normalize_sources(frontmatter.get("sources") or [])
    summary = frontmatter.get("summary") or _body_summary(body)
    validity_defaults = _validity_defaults(frontmatter)

    try:
        validate_schema(frontmatter, body, page_name)
    except SchemaViolationException as e:
        log.warning(f"Validation failed for {page_name}, skipping extraction: {e}")
        return {
            "entities": [],
            "claims": [],
            "evidence": [],
            "sources": [],
            "edges": [],
            "page_key": page_key,
            "page_type": page_type,
        }

    def _parse_temporal(text: str):
        """The date a ledger entry carries as a ``[...]`` prefix, or ``None``.

        The prefix is deliberately *not* stripped.  This text is the input to ``claim_id``,
        ``evidence_id`` and the embedded ``claim_text``, so removing it would re-mint the
        identity of every dated entry on the next compile, while the projection reads the
        same date off the text either way (``tool_timeline._TEXT_EVENT_DATE``).  It would be
        a corpus-wide identity rewrite for nothing.

        The previous pattern accepted only ``[2026-07]`` / ``[2026-Q1]`` (its ``[H|Q]`` is a
        character class, not an alternation) and silently skipped the ``[YYYY-MM-DD]`` form
        the ledger format in ``README`` actually mandates -- which is why 7246 of 9974 live
        timeline claims carried no ``temporal_anchor`` and every reader had to re-parse the
        date out of free text.
        """
        match = re.match(
            r"^\s*\[(\d{4}-\d{2}-\d{2}|\d{4}-[QH]\d|\d{4}-\d{2}|\d{4})\]",
            str(text or ""),
        )
        return match.group(1) if match else None

    def _parse_inline_sources(raw_text: str):
        found_sources = []
        for match in re.finditer(r"\(Source[s]?:\s*(.*?)\)", raw_text, flags=re.IGNORECASE):
            content = match.group(1)
            for m2 in re.finditer(r"\[\[(.*?)\]\]", content):
                found_sources.append(m2.group(1).split("|")[0].strip().replace(".md", ""))
        return found_sources

    subject_entity_ids = []
    entity_records = []
    source_records = []
    evidence_records = []
    claim_records = []

    clean_body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    clean_body = re.sub(r"`.*?`", "", clean_body)
    triples = []
    links = set()
    page_edges = []
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        target = match.group(2).split("|")[0].strip().replace(".md", "")
        if target:
            links.add(target)
            triples.append({"predicate": predicate, "target": target})
            page_edges.append({
                "source_id": page_key,
                "target_id": (resolve_target(target) or target) if resolve_target else target,
                "relation": predicate,
                "weight": 1.0,
                "updated_at": now,
            })

    entity_id = frontmatter.get("entity_id") or _stable_id("entity", page_key)
    subject_entity_ids.append(entity_id)
    entity_records.append({
        "entity_id": entity_id,
        "id": frontmatter.get("id") or entity_id,
        "page_key": page_key,
        "canonical_name": title,
        "title": title,
        "type": page_type,
        "entity_type": page_type,
        "status": frontmatter.get("status", "Active"),
        "epistemic-status": frontmatter.get("epistemic-status", "draft"),
        "aliases": aliases,
        "domain": frontmatter.get("domain", "General"),
        "topic_cluster": frontmatter.get("topic_cluster", "General"),
        "categories": _jsonable(frontmatter.get("categories", [])),
        "tags": _jsonable(frontmatter.get("tags", [])),
        "sources": sources,
        "tension_edges": _jsonable(frontmatter.get("tension_edges", [])),
        "relations": _jsonable(frontmatter.get("relations", [])),
        "links": sorted(links),
        "outbound_links": sorted(links),
        "triples": triples,
        "summary": summary,
        "raw_text": body,
        "ttl": frontmatter.get("ttl"),
        "created_at": _jsonable(frontmatter.get("created", now)),
        "updated": _jsonable(frontmatter.get("updated", now)),
        "updated_at": _jsonable(frontmatter.get("updated", now)),
        "source_page": page_name,
    })
    if page_type == "source" and "source_hash" in frontmatter:
        # Persist admission facts in the canonical CAS state, including otherwise
        # unparsed metadata. Never backfill an old record from a disk-only stamp.
        entity_records[-1]["source_hash"] = _jsonable(frontmatter["source_hash"])
        entity_records[-1]["source_frontmatter"] = _jsonable(frontmatter)

    source_ids = []
    for raw_ref in sources:
        source_id = _stable_id("source", raw_ref)
        source_ids.append(source_id)
        source_records.append({
            "source_id": source_id,
            "raw_ref": raw_ref,
            "canonical_source_page": (page_name if page_type == "source"
                else canonical_source_name(raw_ref)),
            "source_type": os.path.splitext(raw_ref)[1].lstrip(".").lower() or "md",
            "title": title if page_type == "source" else os.path.basename(raw_ref),
            "ingested_at": now,
            "content_hash": frontmatter.get("id") or _stable_id("hash", raw_ref + page_name),
        })

    blocks = _iter_blocks(body)
    if not blocks and summary:
        blocks = [{
            "kind": "paragraph",
            "heading": title,
            "text": summary,
        }]

    for block_index, block in enumerate(blocks, start=1):
        if _is_page_boilerplate(block["text"]) or _is_page_boilerplate(block.get("raw_text")):
            continue
        if _is_run_state_or_placeholder(block["text"]):
            # A placeholder or a run's own packet state is not knowledge: it must not reach the
            # claim or timeline projection, where it would be re-minted as an Active assertion.
            continue
        block_temporal = _parse_temporal(block["text"])
        final_temporal = block_temporal or validity_defaults.get("temporal_anchor")
        # ``_iter_blocks`` already cleaned this; ``_parse_temporal`` adds an anchor without
        # editing it, so the claim text is the block text verbatim.
        block_text = block["text"]

        raw_text = block.get("raw_text", block["text"])
        inline_sources = _parse_inline_sources(raw_text)

        custom_claim_type = claim_type_for_heading(
            block["kind"], block.get("heading") or title, block_text
        )
            
        combined_sources = list(sources)
        combined_source_ids = list(source_ids)
        for isrc in inline_sources:
            # Compared as keys: an anchor that spells a declared source without its extension is
            # that source, not a second one -- appending it would mint a second id for one file.
            if _source_key(isrc) not in {_source_key(item) for item in combined_sources}:
                combined_sources.append(isrc)
                sid = _stable_id("source", isrc)
                combined_source_ids.append(sid)
                if not any(s["source_id"] == sid for s in source_records):
                    source_records.append({
                        "source_id": sid,
                        "raw_ref": isrc,
                        "canonical_source_page": canonical_source_name(isrc),
                        "source_type": os.path.splitext(isrc)[1].lstrip(".").lower() or "md",
                        "title": os.path.basename(isrc),
                        "ingested_at": now,
                        "content_hash": _stable_id("hash", isrc + page_name),
                    })

        evidence_ids = []
        inline_source_keys = {_source_key(item) for item in inline_sources}
        for raw_ref, source_id in zip(combined_sources, combined_source_ids):
            # Why this gate exists: with several declared sources, a block that names none of them
            # must not be attached to all of them.  It said that, and then asked the question in
            # two different spellings.
            if len(sources) > 1 and page_type != "source" and _source_key(raw_ref) not in inline_source_keys:
                continue
            evidence_id = _stable_id("evidence", f"{page_key}:{raw_ref}:{block_text}")
            evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {
                    "page_key": page_key,
                    "heading": block.get("heading") or title,
                    "block_index": block_index,
                },
                "evidence_text": block_text,
                "evidence_type": f"block-{block['kind']}",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        # Why this block carries no evidence, kept distinct from the other gap: a page that
        # records no source at all is an ingest-contract problem, while a page that records
        # several and a block that does not say which is the block's own missing anchor.
        evidence_gap = ""
        if not evidence_ids:
            evidence_gap = "no_source" if not sources else "ambiguous_source"

        claim_id = frontmatter.get("claim_id") if block_index == 1 else None
        claim_id = claim_id or _stable_id("claim", f"{page_key}:{block_text}")
        claim_record = enforce_claim_dict({
            "claim_id": claim_id,
            "claim_text": block_text,
            "claim_type": custom_claim_type,
            "claim_scope": "block",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.6 if page_type == "synthesis" else 0.8),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": evidence_ids,
            # Why a block carries no evidence, recorded rather than flattened into one number:
            # "the page records no source at all" and "the page records several and this block
            # does not say which" are different problems with different owners -- an ingest
            # contract that let a page land without provenance, against a block that owes its
            # inline anchor.  Both used to read as the same ``unsupported``.
            "evidence_gap": evidence_gap,
            "source_ids": list(source_ids),
            "inline_sources": inline_sources,
            "locator": {
                "page_key": page_key,
                "heading": block.get("heading") or title,
                "block_index": block_index,
            },
            **validity_defaults,
            "temporal_anchor": final_temporal,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })
        claim_records.append(claim_record)
        if evidence_ids:
            for evidence_record in evidence_records[-len(evidence_ids):]:
                evidence_record["supports_claim_ids"].append(claim_id)

    if summary:
        summary_temporal = _parse_temporal(summary)
        final_summary_temporal = summary_temporal or validity_defaults.get("temporal_anchor")

        summary_evidence_ids = []
        for raw_ref, source_id in zip(sources, source_ids):
            evidence_id = _stable_id("evidence", f"{page_key}:summary:{raw_ref}")
            summary_evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {"page_key": page_key, "heading": title, "block_index": 0},
                "evidence_text": summary,
                "evidence_type": "page-summary",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        summary_claim_id = _stable_id("claim", f"{page_key}:summary:{summary}")
        summary_claim = enforce_claim_dict({
            "claim_id": summary_claim_id,
            "claim_text": summary,
            "claim_type": "summary",
            "claim_scope": "page",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.65 if page_type == "synthesis" else 0.82),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": summary_evidence_ids,
            "evidence_gap": "" if summary_evidence_ids else "no_source",
            "source_ids": list(source_ids),
            "locator": {"page_key": page_key, "heading": title, "block_index": 0},
            **validity_defaults,
            "temporal_anchor": final_summary_temporal,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })
        if summary_evidence_ids:
            claim_records.append(summary_claim)
            for evidence_record in evidence_records[-len(summary_evidence_ids):]:
                evidence_record["supports_claim_ids"].append(summary_claim_id)

    return {
        "entities": entity_records,
        "claims": claim_records,
        "evidence": evidence_records,
        "sources": source_records,
        "edges": page_edges,
        "page_key": page_key,
        "page_type": page_type,
    }

