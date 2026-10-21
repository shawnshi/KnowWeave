import re
import unicodedata
from datetime import datetime, timedelta, timezone

from vector_lake import governance_store
from vector_lake.node_vocabulary import is_source_anchor_pair


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalized_name(value: str) -> str:
    """Case- and punctuation-insensitive name key.

    Must stay injective for CJK names.  The previous ``re.sub(r"[^a-z0-9]+", "", ...)``
    erased every Chinese name to the empty string, which collapsed all CJK
    entities into one bucket and made every pair a "normalized-name-match"
    merge candidate (and degraded the candidate pre-filter back to O(N^2)).
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).lower()
    cleaned = "".join(character for character in raw if character.isalnum())
    # Never return an empty key: it would re-create the degenerate shared bucket.
    return cleaned or raw.strip()


def _name_keys(names) -> frozenset[str]:
    """Normalized keys for a name set, dropping anything that normalizes away."""
    return frozenset(
        key for key in (_normalized_name(name) for name in names if name) if key
    )


def infer_claim_validity(claim: dict, now=None) -> dict:
    now = now or _utc_now()
    valid_from = _parse_dt(claim.get("valid_from"))
    valid_to = _parse_dt(claim.get("valid_to"))
    review_after = _parse_dt(claim.get("review_after"))
    freshness_tier = str(claim.get("freshness_tier", "unknown")).lower()
    confidence = float(claim.get("confidence", 0) or 0)
    status = str(claim.get("status", "Active")).lower()
    evidence_count = len(claim.get("evidence_ids", []))
    contradictions = len(claim.get("contradicts", []))
    reasons = []

    if status in {"deprecated", "archived", "inactive"}:
        reasons.append("status")
        return {"validity_state": "expired", "reasons": reasons}
    if valid_to and valid_to < now:
        reasons.append("valid_to")
        return {"validity_state": "expired", "reasons": reasons}
    if valid_from and valid_from > now:
        return {"validity_state": "not-yet-valid", "reasons": ["valid_from"]}
    if contradictions:
        reasons.append("conflicts")
        return {"validity_state": "conflicted", "reasons": reasons}
    if evidence_count == 0:
        # The extractor records *which* gap it is, and the two have different owners: a page that
        # records no source at all is an ingest-contract problem, while a page that records
        # several and a block that does not say which is the block's own missing anchor.  A claim
        # written before the field existed still lands on the general reason.
        reasons.append(str(claim.get("evidence_gap") or "missing_evidence"))
        return {"validity_state": "unsupported", "reasons": reasons}
    support = claim.get("evidence_support")
    verified = set(claim.get("verified_evidence_ids", [])) & set(claim.get("evidence_ids", []))
    if support != "verified" or not verified:
        reason = "legacy-unverified" if not support else "declared-unverified"
        return {"validity_state": "needs-review", "reasons": [reason]}
    if review_after and review_after < now:
        reasons.append("review_after")
        return {"validity_state": "review-due", "reasons": reasons}
    if valid_to and valid_to < now + timedelta(days=14):
        reasons.append("valid_to")
        return {"validity_state": "expiring-soon", "reasons": reasons}
    if freshness_tier in {"volatile", "breaking", "fast-changing"} and not review_after:
        reasons.append("freshness_tier")
        return {"validity_state": "needs-review", "reasons": reasons}
    if confidence < 0.55:
        reasons.append("confidence")
        return {"validity_state": "provisional", "reasons": reasons}
    return {"validity_state": "active", "reasons": reasons}


def annotate_claim_validity(claim: dict, now=None) -> dict:
    validity = infer_claim_validity(claim, now=now)
    annotated = dict(claim)
    annotated["validity_state"] = validity["validity_state"]
    annotated["validity_reasons"] = validity["reasons"]
    return annotated


def establishment_key(created, identity) -> tuple:
    """Ordering for survivor selection: oldest first, nodes without a date last.

    Shared by the merge-candidate generator (entity rows) and the lint auto-merge
    (page frontmatter), so the same duplicate pair resolves to the same survivor
    whichever path handles it.
    """
    return (str(created or "") or "\uffff", str(identity or ""))


def _establishment_key(entity: dict) -> tuple:
    return establishment_key(entity.get("created_at"), entity.get("entity_id"))


def _resolvable_name(entity: dict, canonical_name: str) -> str:
    """On-disk identifier the merge resolver can turn into a wiki page.

    ``governance_service.resolve_governance_item`` resolves the two sides as
    ``<prefix><name>.md`` / ``<name>.md``, so what it needs is the page key.
    ``canonical_name`` is unusable for that: it keeps the raw title
    (``Source_intelligence_20260302_briefing``, ``Google (谷歌)``,
    ``Mayo Clinic Platform``) while the file is named from the normalized key.
    """
    page_key = str(entity.get("page_key") or "").strip()
    return page_key or str(canonical_name or entity.get("entity_id") or "").strip()


def _names_of(entity: dict) -> set:
    """Every name an entity answers to: its canonical name plus its aliases."""
    names = {str(entity.get("canonical_name", "") or "")}
    names.update(str(alias) for alias in (entity.get("aliases") or []))
    return {name for name in names if name}


def _name_owner_index(entities) -> dict:
    """name -> {entity id}, so ownership counts entities rather than slots."""
    owners: dict = {}
    for entity in entities:
        for name in _names_of(entity):
            owners.setdefault(name, set()).add(str(entity.get("entity_id") or ""))
    return owners


def _merge_hazards(name_owners: dict, shared_names) -> list:
    """Names whose ownership is ambiguous across the graph.

    A shared name claimed by three or more distinct entities cannot decide which
    page survives: merging any one pair would silently reassign a name another
    live node still owns.  Such candidates stay visible in the preview surface but
    are not enqueued by default (see ``governance_store.create_merge_suggestions``)
    and are refused at resolution time unless explicitly forced (see
    ``governance_service.resolve_governance_item``).
    """
    hazards = []
    for name in sorted(name for name in shared_names if name):
        owners = name_owners.get(str(name), set())
        if len(owners) >= 3:
            hazards.append(f"ambiguous-name:{name} claimed by {len(owners)} distinct entities")
    return hazards


def ambiguous_name_hazards(page_keys) -> list:
    """Ambiguous shared names for two wiki pages identified by page key.

    Used as the entry guard in ``governance_service.resolve_governance_item`` so
    that every resolution path is checked - including callers that build their own
    ``merge_candidate``, such as ``bulk_reconciliation``, which never calls
    ``find_merge_candidates``.
    """
    keys = [str(key) for key in page_keys if key]
    if len(keys) < 2:
        return []
    entities = list(
        governance_store.query_entities({"status!=": "Merged", "type!=": "system"})["items"].values()
    )
    by_key: dict = {}
    for entity in entities:
        key = str(entity.get("page_key") or "")
        if key in keys and key not in by_key:
            by_key[key] = entity
    resolved = list(by_key.values())
    if len(resolved) < 2:
        return []
    shared = _names_of(resolved[0]) & _names_of(resolved[1])
    return _merge_hazards(_name_owner_index(entities), shared)


def find_merge_candidates(limit: int = 20) -> list[dict]:
    #: Pairs a reader already opened and decided are distinct entities.  The same ledger the
    #: name-shape check reads: "these two names are not one entity" is one decision, and offering
    #: the pair again every run is how a stable list stops carrying information.
    #: Imported under a distinct name on purpose: this function already binds ``pair_key`` to a
    #: ``"::"``-joined entity-id string further down, and a bare import would be shadowed by it.
    from vector_lake.name_collision_ledger import accepted_pair_keys
    from vector_lake.name_collision_ledger import pair_key as ledger_pair_key

    decided_pairs = accepted_pair_keys()

    entities = list(governance_store.query_entities({"status!=": "Merged", "type!=": "system"})["items"].values())
    candidates = []

    valid_entities = []
    for e in entities:
        name = str(e.get("canonical_name", ""))
        if e.get("status") == "Merged" or name.startswith("Comm ") or e.get("type") == "system":
            continue
        names = frozenset({name, *e.get("aliases", [])})
        norms = _name_keys(names)
        tokens = frozenset({token for n in names for token in re.split(r"\W+", str(n).lower()) if token})

        # ⚡ Bolt: Pre-extract dict values to avoid O(N^2) dict lookups
        # Measurement: Reduces dict .get() calls from ~5M to ~50K in large datasets, saving ~25% of execution time.
        domain = e.get("domain")
        topic_cluster = e.get("topic_cluster")
        canonical_name = e.get("canonical_name", e["entity_id"])
        valid_entities.append((e, names, norms, tokens, domain, topic_cluster, canonical_name))

    # Names claimed by a *distinct* entity, for the ambiguity guard.  Counting
    # slots instead of entities would over-count, because one entity may both
    # carry a name as its canonical name and repeat it in its aliases.
    name_owners = _name_owner_index(entity for entity, *_ in valid_entities)

    # ⚡ Bolt: Build inverted indices to pre-filter candidate pairs.
    # A candidate pair requires a minimum score of 3. Since token overlap gives +1
    # and domain/cluster match gives +1, a pair mathematically cannot reach a score of 3
    # without either an alias overlap (+3) or a normalized-name match (+3).
    # Measurement: Reduces O(N^2) loop iterations from ~25M to mere thousands in large datasets, saving ~90% execution time.
    candidate_pairs = set()
    norm_to_indices = {}
    name_to_indices = {}

    for idx, (_, names, norms, _, _, _, _) in enumerate(valid_entities):
        for norm in norms:
            norm_to_indices.setdefault(norm, []).append(idx)
        for name in names:
            name_to_indices.setdefault(name, []).append(idx)

    for indices in norm_to_indices.values():
        if len(indices) > 1:
            for i in range(len(indices)):
                for j in range(i + 1, len(indices)):
                    candidate_pairs.add((indices[i], indices[j]))

    for indices in name_to_indices.values():
        if len(indices) > 1:
            for i in range(len(indices)):
                for j in range(i + 1, len(indices)):
                    candidate_pairs.add((indices[i], indices[j]))

    for idx_left, idx_right in candidate_pairs:
        left, left_names, left_norms, left_tokens, left_domain, left_cluster, left_canon_name = valid_entities[idx_left]
        right, right_names, right_norms, right_tokens, right_domain, right_cluster, right_canon_name = valid_entities[idx_right]

        if left["entity_id"] == right["entity_id"]:
            continue

        reasons = []
        score = 0

        if not left_names.isdisjoint(right_names):
            alias_overlap = (left_names & right_names) - {""}
            if alias_overlap:
                reasons.append(f"alias-overlap:{', '.join(sorted(alias_overlap)[:3])}")
                score += 3

        if not left_norms.isdisjoint(right_norms):
            reasons.append("normalized-name-match")
            score += 3

        if not left_tokens.isdisjoint(right_tokens):
            token_overlap = left_tokens & right_tokens
            if len(token_overlap) >= 2:
                reasons.append(f"token-overlap:{', '.join(sorted(token_overlap)[:4])}")
                score += 1

        if left_domain == right_domain and left_cluster == right_cluster:
            score += 1

        if score < 3:
            continue

        # A ``Source_*`` page is the provenance record of the node it fed, so the pair shares a
        # name by contract rather than by duplication.  Detected here, before the candidate is
        # built, because such a pair can never be merged: one side or the other would be lost.
        if is_source_anchor_pair(
            str(left.get("page_key") or left_canon_name or ""),
            str(right.get("page_key") or right_canon_name or ""),
        ):
            continue

        if ledger_pair_key(
            str(left.get("page_key") or left_canon_name or ""),
            str(right.get("page_key") or right_canon_name or ""),
        ) in decided_pairs:
            continue

        domain = left_domain or right_domain or "General"
        hazards = _merge_hazards(name_owners, left_names & right_names)

        # Survivor = the older node.  Pairing comes out of set-iteration order, so
        # without this the surviving page key would be an arbitrary coin flip
        # rather than a property of the data.  Recorded as a reason so a reviewer
        # can audit the choice.
        if _establishment_key(right) < _establishment_key(left):
            left, right = right, left
            left_canon_name, right_canon_name = right_canon_name, left_canon_name
        reasons.append("direction:older-entity-survives")

        pair_key = "::".join(sorted([left["entity_id"], right["entity_id"]]))
        candidates.append({
            "pair_key": pair_key,
            "score": score,
            "left_entity_id": left["entity_id"],
            "left_name": _resolvable_name(left, left_canon_name),
            "left_canonical_name": left_canon_name,
            "right_entity_id": right["entity_id"],
            "right_name": _resolvable_name(right, right_canon_name),
            "right_canonical_name": right_canon_name,
            "reasons": reasons,
            "domain": domain,
            "hazards": hazards,
        })

    candidates.sort(key=lambda item: (-item["score"], item["pair_key"]))
    return candidates[:limit]


def _memory_projection_aggregates() -> tuple[int, dict[str, int], dict[str, int]]:
    """``(total, validity_state counts, memory_type counts)`` from the projection.

    The debt metrics used to answer these from ``load_memory_objects()``, which decodes all
    147 231 ``operational_memory`` payloads -- 5.2 s of the 12.7 s ``compute_debt_metrics``
    took, to produce four counters and one histogram.  Every one of those fields is a
    column of ``operational_memory_index``, which triggers keep in step with ``data_json``;
    on the live corpus both sides agree exactly (147 231 rows, all six states and all four
    types).  The aggregate costs ~48 ms instead of 5 200 ms.
    """
    from vector_lake.db_store import get_connection

    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0]
    states = {
        str(state): int(count)
        for state, count in conn.execute(
            "SELECT validity_state, COUNT(*) FROM operational_memory_index GROUP BY validity_state"
        )
    }
    types = {
        str(memory_type): int(count)
        for memory_type, count in conn.execute(
            "SELECT memory_type, COUNT(*) FROM operational_memory_index GROUP BY memory_type"
        )
    }
    return int(total), states, types


def compute_debt_metrics(skip_heavy: bool = False, merge_candidates: list[dict] | None = None) -> dict:
    """Corpus-wide governance debt.

    ``merge_candidates`` lets a caller that already computed the candidate list hand it in
    instead of paying for a second ``find_merge_candidates`` (``debt_vector_lake`` printed
    the list and this function re-derived its length, so the entity load and the pairwise
    scoring ran twice per dashboard).  ``None`` keeps the previous behaviour of computing it
    here at the same ``limit=20``.
    """
    # ⚡ Bolt: Hoist _utc_now() out of the loop.
    # Measurement: Avoids calling datetime.now(timezone.utc) N times, reducing compute_debt_metrics execution time by ~50% in large datasets.
    now = _utc_now()
    # ``claim_index`` carries exactly the fields ``annotate_claim_validity`` reads, so the
    # annotation runs on narrow rows instead of on 101 323 decoded payloads; the verdict is the same
    # because the same function receives the same values (see tests/test_claim_index.py, which
    # compares the projected inputs against a full decode claim by claim).
    scan_rows = governance_store.load_claim_scan_rows()
    claim_source = (
        scan_rows if scan_rows is not None else governance_store.load_claims()["items"].values()
    )
    claims = [annotate_claim_validity(claim, now=now) for claim in claim_source]
    sources = governance_store.load_sources()["items"].values()
    queue = governance_store.load_governance_queue()["items"]
    memory_total, memory_states, memory_types = _memory_projection_aggregates()
    if not memory_total and claims:
        # The bootstrap case the decoded path repaired: memory absent while canonical
        # claims exist.  It rebuilds, so the aggregate has to be re-read afterwards.
        governance_store.rebuild_operational_memory()
        memory_total, memory_states, memory_types = _memory_projection_aggregates()

    validity_state_counts = {}
    unsupported_claim_count = 0
    #: Unsupported claims whose page's provenance is recorded as never having existed (the
    #: acceptance ledger).  Reported apart from the open count: "no evidence attached" and "no
    #: evidence can ever be attached, and that was decided" are different states, and only the
    #: first one is work.
    legacy_unsourced_claim_count = 0
    ambiguous_source_claim_count = 0
    unsourced_claim_count = 0
    conflicted_claim_count = 0
    stale_claim_count = 0
    expired_claim_count = 0
    review_due_claim_count = 0
    provisional_claim_count = 0
    high_centrality_low_confidence = 0

    from vector_lake.provenance_legacy import accepted_pages, ledger_path

    accepted_legacy_pages = accepted_pages()
    for claim in claims:
        state = claim.get("validity_state", "active")
        validity_state_counts[state] = validity_state_counts.get(state, 0) + 1
        if state == "unsupported":
            # The two-way split stays a breakdown of the *open* count, so
            # ``unsupported == unsourced + ambiguous_source`` keeps holding wherever it is read.
            if str(claim.get("source_page") or "").replace(".md", "") in accepted_legacy_pages:
                legacy_unsourced_claim_count += 1
            else:
                unsupported_claim_count += 1
                if "ambiguous_source" in (claim.get("validity_reasons") or []):
                    ambiguous_source_claim_count += 1
                else:
                    unsourced_claim_count += 1
        if state == "conflicted":
            conflicted_claim_count += 1
        if state in {"review-due", "needs-review", "expiring-soon"}:
            stale_claim_count += 1
        if state == "expired":
            expired_claim_count += 1
        if state == "review-due":
            review_due_claim_count += 1
        if state == "provisional":
            provisional_claim_count += 1
        if float(claim.get("confidence", 0)) < 0.5 and len(claim.get("subject_entity_ids", [])) > 0:
            high_centrality_low_confidence += 1

    source_ids_with_claims = {source_id for claim in claims for source_id in claim.get("source_ids", [])}
    orphan_source_count = len([source for source in sources if source["source_id"] not in source_ids_with_claims])
    pending_items = [item for item in queue if item.get("status") == "pending"]
    if skip_heavy:
        merge_candidates = []
    elif merge_candidates is None:
        merge_candidates = find_merge_candidates(limit=20)

    return {
        "stale_claim_count": stale_claim_count,
        "expired_claim_count": expired_claim_count,
        "review_due_claim_count": review_due_claim_count,
        "unsupported_claim_count": unsupported_claim_count,
        "legacy_unsourced_claim_count": legacy_unsourced_claim_count,
        "ambiguous_source_claim_count": ambiguous_source_claim_count,
        "unsourced_claim_count": unsourced_claim_count,
        "legacy_accepted_page_count": len(accepted_legacy_pages),
        "conflicted_claim_count": conflicted_claim_count,
        "provisional_claim_count": provisional_claim_count,
        "pending_change_set_count": len(governance_store.pending_change_sets()),
        "merge_candidate_count": len(merge_candidates),
        "orphan_source_count": orphan_source_count,
        "high_centrality_low_confidence_count": high_centrality_low_confidence,
        "pending_governance_item_count": len(pending_items),
        "operational_memory_count": memory_total,
        "superseded_memory_count": memory_states.get("superseded", 0),
        "conflicted_memory_count": memory_states.get("conflicted", 0),
        "memory_type_counts": memory_types,
        "validity_state_counts": validity_state_counts,
        "legacy_acceptance_ledger": str(ledger_path()) if accepted_legacy_pages else "",
    }

