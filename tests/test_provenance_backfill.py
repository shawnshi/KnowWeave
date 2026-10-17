"""Provenance backfill (recovers what two exact ledgers answer) and the legacy acceptance ledger.

The live corpus settled two facts these tests pin: provenance is recovered from the ingest job
ledger and from a unique ``canonical_source_name`` match -- never from similarity, because a
general statement's "closest" raw file would be a fabricated citation -- and the pages left over
are *decided* rather than repaired, which the debt metric must report apart from the open count.
"""

import json
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import compute_debt_metrics
from vector_lake.provenance_backfill import (
    _render_with_sources,
    _rollback_path,
    backfill_provenance,
    plan_provenance_backfill,
    revert_provenance_backfill,
)
from vector_lake.provenance_legacy import (
    accept_unrecorded_provenance,
    accepted_pages,
    ledger_path,
)
from vector_lake.tool_lint import lint_vector_lake
from vector_lake.wiki_utils import get_wiki_dir

from tests.test_mutation_coordinator import _write_purpose_contract

_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n"
    "### 物理机制 (Mechanism)\nA fact.\n\n"
    "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n\n"
    "- [2026-01-01] [Observation] observed.\n"
)


def _page(name: str, sources: str = "[]") -> str:
    # The type has to match the name prefix: ``validate_schema`` refuses a ``Source_`` page
    # declared ``type: concept`` and the extractor then skips it, which is how the first version
    # of this fixture produced pages that carried no claims at all.
    type_ = "source" if name.startswith("Source_") else "concept"
    get_wiki_dir().mkdir(parents=True, exist_ok=True)
    (get_wiki_dir() / f"{name}.md").write_text(
        "---\n"
        f"id: {name.lower()}\n"
        f"title: {name}\n"
        f"type: {type_}\n"
        f"domain: {'Medical_IT' if type_ == 'source' else 'General'}\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        f"categories: [{'Healthcare_IT' if type_ == 'source' else 'System_Architecture'}]\n"
        "strategic_scope: core\n"
        "evidence_tier: primary\n"
        "topic_cluster: Test\n"
        "updated: 2026-01-01\n"
        f"sources: {sources}\n"
        "---\n" + _BODY,
        encoding="utf-8",
    )
    return name


def _raw(relative: str) -> None:
    path = get_wiki_dir().parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raw body\n", encoding="utf-8")


def _job(canonical_name: str, filepath: str) -> None:
    db_store.get_connection().execute(
        "INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at) "
        "VALUES (?, 'ingest', ?, 'finalized', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
        (f"job_{canonical_name}", json.dumps({"canonical_name": canonical_name, "filepath": filepath})),
    )
    db_store.get_connection().commit()


def _materialize(*pages: str) -> None:
    """Run the real extraction so the seeded pages carry their claims and gaps."""
    governance_store.create_change_set(
        [str(get_wiki_dir() / f"{page}.md") for page in pages], origin="probe", auto_approve=True
    )


def _gaps(page_key: str) -> list[str]:
    rows = db_store.get_connection().execute(
        """SELECT json_extract(data_json,'$.evidence_gap') AS gap,
                  json_extract(data_json,'$.evidence_ids') AS evidence
           FROM claims WHERE json_extract(data_json,'$.locator.page_key') = ?""",
        (page_key,),
    ).fetchall()
    return [(row["gap"], row["evidence"]) for row in rows]


def _all_gapped(page_key: str) -> bool:
    """The page's claim(s) exist and every one of them records the no-source gap."""
    gaps = _gaps(page_key)
    return bool(gaps) and all(gap == "no_source" and evidence == "[]" for gap, evidence in gaps)


@pytest.fixture
def prepared(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    return isolated_memory


def test_the_renderer_replaces_inline_value_and_refuses_multiline_layout():
    assert (
        _render_with_sources("---\nsources: []\n---\nbody\n", "raw/a.md")
        == '---\nsources: ["raw/a.md"]\n---\nbody\n'
    )
    block = "---\nsources:\n- raw/old.md\n- raw/other.md\ntype: concept\n---\nbody\n"
    with pytest.raises(ValueError, match="Unsupported source layout"):
        _render_with_sources(block, "raw/new.md")


def test_the_plan_separates_recoverable_ambiguous_and_unmatched(prepared):
    _page("Source_Chosen")          # resolved by the ingest job ledger
    _page("Source_intelligence-1")  # resolved by a unique canonical-name match
    _page("Source_x-y")             # two raw files sanitise to this canonical name
    _page("Concept_Nothing")        # nothing to match
    _raw("raw/docs/thing.md")
    _raw("raw/news/x-y.md")
    _raw("raw/other/x_y.md")
    _raw("raw/briefings/intelligence-1.md")
    for page in ("Source_Chosen", "Source_intelligence-1", "Source_x-y", "Concept_Nothing"):
        _materialize(page)
    _job("Source_Chosen.md", "C:\\fake\\MEMORY\\raw\\docs\\thing.md")
    # The ledger points at a path that is gone, so the unique canonical-name match answers.
    _job("Source_intelligence-1.md", "C:\\fake\\MEMORY\\raw\\gone\\intelligence-1.md")

    plan = plan_provenance_backfill()

    assert plan["no_source_pages"] == 4, plan
    resolved = {entry["page_key"]: entry for entry in plan["restorable"]}
    assert resolved["Source_Chosen"]["raw_path"] == "raw/docs/thing.md"
    assert resolved["Source_Chosen"]["rule"] == "job-ledger"
    assert resolved["Source_intelligence-1"]["raw_path"] == "raw/briefings/intelligence-1.md"
    assert resolved["Source_intelligence-1"]["rule"] == "canonical-name"
    assert [entry["page_key"] for entry in plan["ambiguous"]] == ["Source_x-y"]
    assert sorted(plan["ambiguous"][0]["candidates"]) == ["raw/news/x-y.md", "raw/other/x_y.md"]
    assert [entry["page_key"] for entry in plan["unmatched"]] == ["Concept_Nothing"]


def test_a_dry_run_changes_nothing(prepared):
    _page("Source_Chosen")
    _materialize("Source_Chosen")
    _raw("raw/docs/thing.md")
    _job("Source_Chosen.md", "C:\\fake\\MEMORY\\raw\\docs\\thing.md")
    before = (get_wiki_dir() / "Source_Chosen.md").read_text(encoding="utf-8")

    report = backfill_provenance(dry_run=True)

    assert "[DRY RUN]" in report
    assert (get_wiki_dir() / "Source_Chosen.md").read_text(encoding="utf-8") == before
    assert not _rollback_path().exists()
    assert _all_gapped("Source_Chosen")


def test_the_backfill_exports_patch_without_restoring_or_clearing_gap(prepared):
    _page("Source_Chosen")
    _materialize("Source_Chosen")
    _raw("raw/docs/thing.md")
    _job("Source_Chosen.md", "C:\\fake\\MEMORY\\raw\\docs\\thing.md")
    path = get_wiki_dir() / "Source_Chosen.md"
    before = path.read_bytes()
    report = backfill_provenance(dry_run=False, batch=10)
    assert "[PATCH ONLY]" in report and "restored 0 page(s)" in report
    assert path.read_bytes() == before and _all_gapped("Source_Chosen")
    manifest_path = Path(next(line.removeprefix("patch manifest: ") for line in report.splitlines() if line.startswith("patch manifest: ")))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_applied"] == 0 and manifest["patches_prepared"] == 1
    assert (manifest_path.parent / manifest["entries"][0]["before_file"]).read_bytes() == before


def test_the_revert_only_exports_a_hash_matching_legacy_reversal(prepared):
    import hashlib
    _page("Source_Chosen")
    _materialize("Source_Chosen")
    path = get_wiki_dir() / "Source_Chosen.md"
    before = path.read_text(encoding="utf-8")
    path.write_text(_render_with_sources(before, "raw/docs/thing.md"), encoding="utf-8")
    current = path.read_bytes()
    rollback = _rollback_path()
    rollback.write_text(json.dumps({"page_key": "Source_Chosen", "before": before,
        "after_sha256": hashlib.sha256(current).hexdigest()})+"\n", encoding="utf-8")
    report = revert_provenance_backfill(str(rollback), batch=10)
    assert "[PATCH ONLY]" in report and "reverted 0 page(s)" in report
    assert path.read_bytes() == current and _all_gapped("Source_Chosen")


def test_acceptance_records_the_pages_and_only_on_apply(prepared):
    _page("Concept_Nothing")
    _materialize("Concept_Nothing")

    assert "[DRY RUN]" in accept_unrecorded_provenance(dry_run=True)
    assert not ledger_path().exists()
    assert accepted_pages() == set()

    report = accept_unrecorded_provenance(dry_run=False)

    assert "recorded 1 page(s)" in report
    ledger = json.loads(ledger_path().read_text(encoding="utf-8"))
    assert set(ledger["pages"]) == {"Concept_Nothing"}
    assert ledger["decision"] == "accept-unrecorded-provenance-as-legacy"
    assert ledger["claim_count"] == sum(ledger["pages"].values()) > 0
    assert ledger["revisit_candidates"] == {"Source_": 0, "Event_": 0}
    assert accepted_pages() == {"Concept_Nothing"}


def test_the_debt_metric_reports_decided_debt_apart_from_open_debt(prepared):
    # One page nothing can resolve (decided) and one the job ledger can (still open work), so the
    # split has to keep the second one visible.
    _page("Concept_Nothing")
    _page("Source_Chosen")
    _raw("raw/docs/thing.md")
    _job("Source_Chosen.md", "C:\\fake\\MEMORY\\raw\\docs\\thing.md")
    _materialize("Concept_Nothing", "Source_Chosen")

    before = compute_debt_metrics(skip_heavy=True)
    assert before["unsupported_claim_count"] > 0
    assert before["legacy_unsourced_claim_count"] == 0

    accept_unrecorded_provenance(dry_run=False)

    after = compute_debt_metrics(skip_heavy=True)

    assert after["legacy_accepted_page_count"] == 1
    assert after["legacy_unsourced_claim_count"] == len(_gaps("Concept_Nothing"))
    assert after["unsupported_claim_count"] == len(_gaps("Source_Chosen")) > 0
    # The two-way split stays a breakdown of the *open* count.
    assert after["unsupported_claim_count"] == (
        after["unsourced_claim_count"] + after["ambiguous_source_claim_count"]
    )


def test_the_lint_shows_decided_debt_as_a_census_not_a_finding(prepared):
    _page("Concept_Nothing")
    _materialize("Concept_Nothing")
    accept_unrecorded_provenance(dry_run=False)

    report = lint_vector_lake()

    assert "accepted as legacy" in report
    assert "provenance_legacy_accepted.json" in report
    # Nothing is left open, so the section itself must not read FAIL.
    assert "12. Governance Debt: [PASS]" in report, report.split("12. Governance Debt")[1][:200]
