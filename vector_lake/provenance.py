import re

from vector_lake import governance_metrics
from vector_lake import governance_store


def _tokenize(query: str) -> list[str]:
    from vector_lake import tokenizer as _tokenizer

    tokens = [str(tok).strip().lower() for tok in _tokenizer.cut(query or "") if str(tok).strip()]
    if tokens:
        return list(dict.fromkeys(tokens))
    return [token.lower() for token in re.split(r"\W+", query or "") if token.strip()]


def build_trace_for_query(query: str, top_k: int = 5) -> dict:
    tokens = _tokenize(query)
    
    # 1. Use FTS5 to find relevant source pages instead of O(N) full claim scan
    from vector_lake.db_store import search_wiki
    search_results = search_wiki(query, limit=10)
    relevant_pages = {res["node_key"] for res in search_results}
    
    # The scan reads two fields per claim, so it runs over the narrow ``claim_index`` projection
    # instead of decoding all 101 549 payloads (3.2 s of this function's 8.0 s).  The haystack is
    # rebuilt exactly as before -- separator, lowercasing, and the *raw* source page used for the FTS
    # membership test are unchanged -- and only the returned rows are decoded, so the projection
    # cannot narrow what a caller sees, only how many rows were read to find it.
    scan_rows = governance_store.load_claim_scan_rows()
    if scan_rows is None:
        scan_rows = [
            {
                **claim,
                "claim_id": str(claim.get("claim_id", "")),
                "source_page": claim.get("source_page", ""),
                "claim_text": str(claim.get("claim_text", "")).lower(),
            }
            for claim in governance_store.load_claims()["items"].values()
        ]
    entities = governance_store.load_entities()["items"]
    sources = governance_store.load_sources()["items"]

    matches = []
    for claim in scan_rows:
        # Boost score if the claim comes from a top FTS match
        source_page = str(claim.get('source_page', '') or '')
        page_key = source_page.removesuffix(".md")
        base_score = 5 if (source_page in relevant_pages or page_key in relevant_pages) else 0
        
        haystack = f"{claim.get('claim_text', '')} {source_page}".lower()
        match_score = sum(1 for token in tokens if token in haystack)
        score = base_score + match_score
        
        if score > 0:
            matches.append((score, claim))
    matches.sort(key=lambda item: item[0], reverse=True)

    top_claims = governance_store.load_claims_by_ids(
        [str(claim.get("claim_id", "")) for _, claim in matches[:top_k]]
    )

    trace_items = []
    for _, claim in matches[:top_k]:
        claim = top_claims.get(str(claim.get("claim_id", "")), claim)
        annotated = governance_metrics.annotate_claim_validity(claim)
        trace_items.append({
            "claim_id": annotated["claim_id"],
            "claim_text": annotated.get("claim_text", ""),
            "subject_entities": [entities[entity_id]["canonical_name"] for entity_id in annotated.get("subject_entity_ids", []) if entity_id in entities],
            "source_pages": [sources[source_id]["canonical_source_page"] for source_id in annotated.get("source_ids", []) if source_id in sources],
            "confidence": annotated.get("confidence"),
            "valid_to": annotated.get("valid_to"),
            "review_after": annotated.get("review_after"),
            "validity_state": annotated.get("validity_state"),
            "evidence_count": len(annotated.get("evidence_ids", [])),
            "locator": annotated.get("locator", {}),
        })

    return {"query": query, "items": trace_items}


def format_trace(trace: dict) -> str:
    if not trace.get("items"):
        return "No provenance trace found."
    lines = ["=== Provenance Trace ===", f"Query: {trace.get('query', '')}", ""]
    for index, item in enumerate(trace["items"], start=1):
        lines.append(f"[{index}] {item['claim_id']}")
        lines.append(f"  Claim: {item['claim_text']}")
        if item["subject_entities"]:
            lines.append(f"  Entities: {', '.join(item['subject_entities'])}")
        if item["source_pages"]:
            lines.append(f"  Source Pages: {', '.join(item['source_pages'])}")
        lines.append(f"  Confidence: {item.get('confidence')}")
        lines.append(f"  Validity: {item.get('validity_state')}")
        lines.append(f"  Evidence Count: {item.get('evidence_count')}")
        locator = item.get("locator") or {}
        if locator:
            lines.append(f"  Locator: {locator.get('page_key', '')}#{locator.get('heading', '')}:{locator.get('block_index', '')}")
        if item.get("review_after"):
            lines.append(f"  Review After: {item['review_after']}")
        lines.append("")
    return "\n".join(lines).strip()

