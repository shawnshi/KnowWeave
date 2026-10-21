"""Synthetic regression contracts for GC-01/04/05/06/07; never open source files."""
from types import SimpleNamespace

import pytest

from vector_lake import claim_extractor as extractor
from vector_lake.schema_validator import SchemaViolationException


def fm(**updates):
    result = {
        "id": "concept_contract", "title": "Contract", "type": "concept",
        "domain": "General", "status": "Active", "epistemic-status": "seed",
        "categories": ["System_Architecture"], "updated": "2026-10-21T00:00:00Z",
        "sources": [],
    }
    result.update(updates)
    return result


def extract(body, **updates):
    return extractor.extract_page_objects(
        "Concept_Contract.md", fm(**updates),
        "## 1. 编译事实\n\n" + body + "\n\n## 2. 证据时间线\n",
        resolve_target=lambda target: target,
    )


def blocks(result):
    return [c for c in result["claims"] if c["claim_scope"] == "block"]


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_long_identity_uses_full_text_and_reports_legacy_collision(monkeypatch, backend):
    prefix = "A" * 360
    texts = [prefix + " first ending", prefix + " different ending"]
    monkeypatch.setattr(extractor, "CLAIM_BLOCK_BACKEND", backend)
    if backend == "rust":
        class Core:
            blocks_contract = staticmethod(lambda: extractor.BLOCKS_CONTRACT)
            fast_extract_blocks = staticmethod(lambda body: [
                SimpleNamespace(kind="paragraph", heading="1. 编译事实", raw_text=text)
                for text in texts
            ])
        monkeypatch.setitem(__import__("sys").modules, "vector_lake_core", Core())
    claims = blocks(extract("\n\n".join(texts)))
    assert [c["claim_text"] for c in claims] == texts
    assert len({c["claim_id"] for c in claims}) == 2
    for claim in claims:
        assert claim["claim_id"] == extractor._stable_id("claim", "Concept_Contract:" + claim["claim_text"])
        assert claim["identity_version"] == 2
        assert claim["legacy_claim_ids"] == [extractor._stable_id("claim", "Concept_Contract:" + prefix)]
        assert claim["display_text"] == prefix


def test_short_identity_stays_stable_and_custom_identity_is_authoritative():
    text = "A short assertion."
    claim = blocks(extract(text))[0]
    assert claim["claim_id"] == extractor._stable_id("claim", "Concept_Contract:" + text)
    assert claim["legacy_claim_ids"] == []
    custom = blocks(extract("A" * 400, claim_id="claim_explicit"))[0]
    assert custom["claim_id"] == "claim_explicit"
    assert custom["legacy_claim_ids"] == []


def test_repeated_text_preserves_occurrences_and_type_conflicts():
    result = extract("Repeated assertion.\n\n## 2. 证据时间线\n\nRepeated assertion.", sources=["raw/a.pdf"])
    claims = blocks(result)
    assert len(claims) == 1
    claim = claims[0]
    assert [o["locator"]["block_index"] for o in claim["occurrences"]] == [1, 2]
    assert claim["claim_type"] == "compiled-truth"
    assert claim["claim_types"] == ["compiled-truth", "timeline-event"]
    assert "claim_type" in claim["metadata_conflicts"]
    assert len(result["evidence"]) == len({e["evidence_id"] for e in result["evidence"]})
    evidence = next(e for e in result["evidence"] if e["evidence_type"] == "block-paragraph")
    assert [o["locator"]["block_index"] for o in evidence["occurrences"]] == [1, 2]


def test_schema_violation_propagates_instead_of_success_shaped_empty_result():
    with pytest.raises(SchemaViolationException, match="Missing required"):
        extractor.extract_page_objects("Concept_Invalid.md", {}, "Some text.", resolve_target=lambda x: x)


def test_generated_artifact_skip_has_a_distinct_status():
    result = extractor.extract_page_objects(
        "System_Community_Test.md", {"type": "system"}, "", resolve_target=lambda x: x,
    )
    assert result["claims"] == []
    assert result["extraction_status"] == "skipped"
    assert result["skip_reason"] == "generated_artifact"


def test_copied_block_is_declared_unverified_not_a_verified_quote():
    result = extract("This assertion only declares a source.", sources=["raw/a.pdf"])
    assert result["extraction_status"] == "extracted"
    for claim in result["claims"]:
        assert claim["evidence_support"] == "declared"
        assert claim["verified_evidence_ids"] == []
    for evidence in result["evidence"]:
        assert evidence["provenance_kind"] == "declared-source"
        assert evidence["support_status"] == "unverified"
    source = result["sources"][0]
    assert source["content_hash"] == ""
    assert source["reference_identity"] == source["source_id"]


def test_unanchored_summary_cannot_claim_all_declared_sources():
    result = extract("Unanchored assertion.", sources=["raw/a.pdf", "raw/b.pdf"], summary="Unanchored summary.")
    assert result["evidence"] == []
    assert all(c["source_ids"] == [] for c in result["claims"])
    assert all(c["evidence_gap"] == "ambiguous_source" for c in result["claims"])


@pytest.mark.parametrize("sources,anchor,expected_ref", [
    (["raw/a.pdf", "raw/b.pdf"], "Source_a", "raw/a.pdf"),
    (["raw/a.pdf", "raw/b.pdf"], "raw/a.pdf", "raw/a.pdf"),
    ([], "Source_a.md", "Source_a"),
    (["Source_a.md"], "Source_a", "Source_a.md"),
])
def test_source_ids_equal_actual_evidence_attribution(sources, anchor, expected_ref):
    result = extract(f"An anchored assertion. (Source: [[{anchor}]])", sources=sources)
    claim = blocks(result)[0]
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    assert claim["source_ids"] == sorted({evidence[eid]["source_id"] for eid in claim["evidence_ids"]})
    assert len(claim["source_ids"]) == 1
    source = next(s for s in result["sources"] if s["source_id"] == claim["source_ids"][0])
    assert source["raw_ref"] == expected_ref
    assert source["canonical_source_page"] == "Source_a.md"
    assert source["content_hash"] == ""


def test_summary_only_attaches_the_source_named_by_its_own_anchor():
    result = extract("A separate block.", sources=["raw/a.pdf", "raw/b.pdf"], summary="Summary. (Source: [[Source_b]])")
    summary = next(c for c in result["claims"] if c["claim_type"] == "summary")
    assert summary["claim_text"] == "Summary. "
    source = next(s for s in result["sources"] if s["source_id"] in summary["source_ids"])
    assert source["raw_ref"] == "raw/b.pdf"
    assert len(summary["evidence_ids"]) == 1


def test_frontmatter_relationships_bind_only_explicit_first_claim():
    result = extract("First assertion.\n\nSecond assertion.", claim_id="claim_first", contradicts_claim_ids=["claim_old"], revision_of="claim_revision", supersedes_claim_ids=["claim_prior"])
    first, second = blocks(result)
    assert first["claim_id"] == "claim_first"
    assert first["contradicts_claim_ids"] == ["claim_old"]
    assert first["revision_of"] == "claim_revision"
    assert first["supersedes_claim_ids"] == ["claim_prior"]
    assert second.get("contradicts_claim_ids", []) == []
    assert not second.get("revision_of")


def test_page_relationship_without_block_locator_is_rejected_when_ambiguous():
    with pytest.raises(SchemaViolationException, match="ambiguous"):
        extract("First assertion.\n\nSecond assertion.", contradicts_claim_ids=["claim_other"])


def test_explicit_block_relationship_and_claim_id_scope():
    first_id = extractor._stable_id("claim", "Concept_Contract:First assertion.")
    result = extract("First assertion.\n\nSecond assertion.", claim_relations=[
        {"block_index": 2, "contradicts_claim_ids": ["claim_other"]},
        {"claim_id": first_id, "supersedes_claim_ids": ["claim_old"]},
    ])
    first, second = blocks(result)
    assert first["supersedes_claim_ids"] == ["claim_old"]
    assert second["contradicts_claim_ids"] == ["claim_other"]


@pytest.mark.parametrize("declaration", [
    {"block_index": 99, "contradicts_claim_ids": ["claim_other"]},
    {"scope": "page", "contradicts_claim_ids": ["claim_other"]},
    {"block_index": True, "contradicts_claim_ids": ["claim_other"]},
    {"block_index": 1, "contradicts_claim_ids": {"bad": "shape"}},
    {"block_index": 1, "scope": "claim", "contradicts": ["claim_other"]},
    {"block_index": 1, "scope": [], "contradicts": ["claim_other"]},
])
def test_malformed_or_unresolved_relationship_scope_fails_closed(declaration):
    with pytest.raises(SchemaViolationException):
        extract("First assertion.", claim_relations=[declaration])


def test_long_summary_retains_full_text_and_old_summary_identity():
    text = "S" * 420
    result = extract(text, sources=["raw/a.pdf"])
    summary = next(c for c in result["claims"] if c["claim_scope"] == "page")
    assert summary["claim_text"] == text
    assert summary["claim_id"] == extractor._stable_id("claim", "Concept_Contract:summary:" + text)
    assert summary["legacy_claim_ids"] == [extractor._stable_id("claim", "Concept_Contract:summary:" + text[:320])]


def test_same_assertion_with_different_anchors_keeps_both_attributions():
    result = extract(
        "Same assertion.(Source: [[Source_a]])\n\nSame assertion.(Source: [[Source_b]])",
        sources=["raw/a.pdf", "raw/b.pdf"],
    )
    claim = blocks(result)[0]
    assert len(blocks(result)) == 1
    assert len(claim["evidence_ids"]) == 2
    assert len(claim["source_ids"]) == 2
    assert all(len(o["source_ids"]) == 1 for o in claim["occurrences"])
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    for occurrence in claim["occurrences"]:
        assert occurrence["source_ids"] == [evidence[occurrence["evidence_ids"][0]]["source_id"]]
    for record in evidence.values():
        assert set(record["supports_claim_ids"]) <= {c["claim_id"] for c in result["claims"]}


def test_ambiguous_canonical_source_alias_fails_instead_of_choosing_one():
    with pytest.raises(SchemaViolationException, match="ambiguous"):
        extract("Assertion.(Source: [[Source_a]])", sources=["raw/a.pdf", "raw/a.docx"])


def test_raw_file_starting_with_source_is_not_a_source_page_reference():
    result = extract("Assertion.", sources=["raw/Source_report.pdf"])
    assert result["sources"][0]["canonical_source_page"] == "Source_Source-report.md"


def test_explicit_relations_aliases_stay_directed_and_have_no_inferred_targets():
    result = extract("Only assertion.", contradicts="claim_old", supersedes="claim_prior")
    claim = blocks(result)[0]
    assert claim["contradicts"] == claim["contradicts_claim_ids"] == ["claim_old"]
    assert claim["supersedes"] == claim["supersedes_claim_ids"] == ["claim_prior"]
    assert not claim.get("revision_of")
    assert result["edges"] == []
    assert not blocks(extract("An assertion without a relation."))[0].get("contradicts")


def test_empty_relationship_lists_do_not_create_ambiguous_scope():
    assert len(blocks(extract("First assertion.\n\nSecond assertion.", contradicts=[], supersedes=[]))) == 2


def test_revision_conflict_is_not_silently_overwritten():
    with pytest.raises(SchemaViolationException, match="non-conflicting"):
        extract("Only assertion.", revision_of="claim_a", claim_relations=[
            {"block_index": 1, "revision_of": "claim_b"},
        ])
