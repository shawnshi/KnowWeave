"""Read-only relatedness policy; scoring never rewrites canonical assertions."""

PRED_WEIGHT_TAXONOMY = frozenset({"属于", "parent", "is-a", "belongs_to", "instance_of", "has_part", "part-of", "核心构件"})
PRED_WEIGHT_RELATION = frozenset({"类似", "related_to", "see_also", "peer", "关联", "competes-with"})
PRED_WEIGHT_MENTION = frozenset({"mentions", "提及", "引用"})


# Advisory only: these profiles never reject or rewrite historical declarations.
PREDICATE_INVERSES = {"part-of": "has_part", "has_part": "part-of",
                      "supplies-to": "supplied-by", "supplied-by": "supplies-to"}
SYMMETRIC_PREDICATES = frozenset({"competes-with", "allied-with", "integrates-with",
                                "conflicts-with", "related_to", "peer", "类似", "关联"})
PREDICATE_TYPES = {
    "authored": ({"person", "institution", "vendor"}, {"source", "synthesis"}),
    "complies-with": (None, {"policy", "standard"}),
    "certified-by": (None, {"institution"}),
    "deployed-at": ({"product"}, {"institution"}),
    "runs-on": ({"product"}, {"product"}),
}


def predicate_diagnostics(predicate, source_type, target_type):
    """Controlled direction/type hints, not factual validation or inverse facts."""
    from vector_lake.node_vocabulary import NODE_TYPE_SET
    from vector_lake.schema_validator import VALID_PREDICATES

    codes = []
    known = predicate in VALID_PREDICATES
    if not known:
        codes.append("unknown_predicate")
    type_status = "unknown"
    if (not isinstance(source_type, str) or not isinstance(target_type, str)
            or source_type not in NODE_TYPE_SET or target_type not in NODE_TYPE_SET):
        codes.append("endpoint_type_unknown")
    elif predicate in PREDICATE_TYPES:
        sources, targets = PREDICATE_TYPES[predicate]
        fits = (sources is None or source_type in sources) and target_type in targets
        type_status = "compatible" if fits else "mismatch"
        if not fits:
            reverse_fits = (sources is None or target_type in sources) and source_type in targets
            codes.append("wrong_direction" if reverse_fits else "predicate_type_mismatch")
    if isinstance(source_type, str) and source_type in {"source", "synthesis"} and predicate in PRED_WEIGHT_TAXONOMY:
        codes.append("document_taxonomy_declaration")
    return {"vocabulary_status": "controlled" if known else "unknown",
            "direction": "symmetric" if predicate in SYMMETRIC_PREDICATES else "source_to_target",
            "inverse_predicate": PREDICATE_INVERSES.get(predicate),
            "type_status": type_status}, codes


def predicate_weight(pred: str, direct_link: float = 3.0) -> float:
    """Weight of a declaration for ranking, not its truth or canonical identity."""
    pred_lower = pred.lower()
    if pred_lower in PRED_WEIGHT_TAXONOMY:
        return direct_link * 3.0
    if pred_lower in PRED_WEIGHT_RELATION:
        return direct_link * 1.5
    if pred_lower in PRED_WEIGHT_MENTION:
        return direct_link * 0.4
    return direct_link


def pair_relation_weight(weight_a: float | None, weight_b: float | None, *, linked: bool,
                         direct_link: float = 3.0) -> float:
    """Strongest declaration across a pair; navigation is a pair-level fallback.

    MAX makes repetitions, inverse-equivalent statements and distinct predicates
    non-additive without conflating or deleting any of those canonical facts.
    """
    weights = [weight for weight in (weight_a, weight_b) if weight is not None]
    if weights:
        return max(weights)
    return predicate_weight("mentions", direct_link) if linked else 0.0
