"""What an ingest payload is allowed to overwrite.

``expected_version`` is a write permission, not bookkeeping: the coordinator treats a supplied
non-empty value as "overwrite the page at this version".  The disposition step used to *default*
it for submitted items rather than force it, so a payload could name any existing page, quote
its current version, and have the ingest rewrite it with no relation, no predicate and no
``target_hash`` check -- the entire integration contract bypassed, on the disposition the host
runner hardcodes.
"""

import json

from vector_lake import db_store, governance_store, mcp_server, tool_ingest
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.tool_ingest import claim_ingest_tasks

from tests.test_ingest_contract import _concept_content, _source_content
from tests.test_mutation_coordinator import _write_purpose_contract


def _standalone(files, canonical_name="Source_X.md", source_hash="packet-hash"):
    return tool_ingest._apply_integration_disposition(
        files,
        {
            "canonical_name": canonical_name,
            "source_hash": source_hash,
            "integration": {
                "disposition": "standalone",
                "reason": "Single-source compilation with no cross-page integration.",
            },
        },
    )


def test_submitted_items_are_create_only_even_when_the_payload_names_a_version():
    files = [
        {"filename": "Source_X.md", "content": "---\ntype: source\n---\nsource"},
        {"filename": "Other_Page.md", "content": "other", "expected_version": "a-real-version"},
    ]
    out, disposition = _standalone(files)

    assert disposition == "standalone"
    assert out[1]["expected_version"] == "", "a payload must not be able to name a version"


def test_the_canonical_source_page_keeps_the_version_the_packet_supplied(isolated_memory):
    """The one submitted page that *may* name a version keeps the packet's value, not the payload's."""
    files = [
        {"filename": "Source_X.md", "content": "---\ntype: source\n---\nsource", "expected_version": "payload-value"},
        {"filename": "Other_Page.md", "content": "other"},
    ]
    out, _ = _standalone(files, source_hash="packet-hash")
    assert out[0]["expected_version"] == "packet-hash"


def test_finalize_ingest_refuses_a_payload_that_targets_an_existing_page(isolated_memory):
    """The exploit this exists for: rewriting a page the ingest was never authorized to touch."""
    _write_purpose_contract(isolated_memory)
    target_path = isolated_memory / "wiki" / "Concept_Target.md"
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    before = target_path.read_text(encoding="utf-8")
    target_version = governance_store.canonical_page_versions({"Concept_Target"})["Concept_Target"]

    payload = {
        "filepath": "raw/write-scope.md",
        "hash": "write-scope-hash",
        "canonical_name": "Source_Write-Scope.md",
    }
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [
            {"filename": "Source_Write-Scope.md", "content": _source_content()},
            {
                "filename": "Concept_Target.md",
                "content": _concept_content().replace(
                    "Target compiled truth.", "OVERWRITTEN BY PAYLOAD."
                ),
                "expected_version": target_version,
            },
        ],
        {
            **payload,
            "integration": {
                "disposition": "standalone",
                "reason": "Single-source compilation with no cross-page integration.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert result.startswith("Error finalizing ingestion"), result
    assert "Canonical version conflict for Concept_Target.md" in result
    assert target_path.read_text(encoding="utf-8") == before, "the existing page was rewritten"
    assert not (isolated_memory / "wiki" / "Source_Write-Scope.md").exists(), "the batch was partial"
