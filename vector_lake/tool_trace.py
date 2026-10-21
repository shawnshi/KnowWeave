import json

from vector_lake import provenance


def trace_vector_lake(query_or_id: str, *, domain: str = "", cluster: str = "") -> str:
    """Legacy provenance, or exact-scoped document relations without claim reinterpretation."""
    if domain or cluster:
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError("Scoped relation trace requires a nonempty exact domain")
        from vector_lake.tool_relations import get_relations

        relations = get_relations(entity_name=query_or_id, domain=domain, cluster=cluster)
        return "=== Document Relations ===\n" + json.dumps(relations, ensure_ascii=False)
    trace = provenance.build_trace_for_query(query_or_id)
    return provenance.format_trace(trace)

