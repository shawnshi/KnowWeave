from vector_lake import governance_store


def test_claim_graph_declares_cooccurrence_not_evidential_support(monkeypatch):
    monkeypatch.setattr(governance_store, "load_entities", lambda: {"items": {"entity_a": {"canonical_name": "A"}}})
    monkeypatch.setattr(governance_store, "load_sources", lambda: {"items": {"source_a": {"canonical_source_page": "Source_A.md"}}})
    monkeypatch.setattr(governance_store, "annotated_claims", lambda: [
        {"claim_id": f"claim_{index}", "claim_text": f"Statement {index}", "subject_entity_ids": ["entity_a"], "source_ids": ["source_a"], "validity_state": "unsupported"}
        for index in range(3)
    ])
    graph = governance_store.build_claim_graph_projection(limit_nodes=2)
    assert graph["graph_kind"] == "claim-cooccurrence"
    assert graph["is_evidence_graph"] is False
    assert len(graph["nodes"]) == 2
    assert graph["sparsification"]["node_limit"] == 2
    assert graph["sparsification"]["contradictions_bypass_degree_limit"]
    assert "not evidential support" in graph["edge_semantics"]["shared-entity"]
    assert graph["edges"][0]["relation"] == "shared-entity"
