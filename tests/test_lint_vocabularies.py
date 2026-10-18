"""``tool_lint`` must not keep private copies of the shared vocabularies.

The lint module used to redeclare ``valid_types``, ``valid_status``,
``valid_epistemic``, ``valid_categories`` and ``valid_prefixes``.  Four were
byte-identical to their owners in ``schema_validator`` / ``wiki_utils``; the
fifth had drifted -- ``valid_status`` was missing ``archived`` and ``contested``,
so 16 live pages carrying the legal status ``Contested`` were reported as
invalid while ``schema_validator`` accepted them.

These tests drive the check through its real entry point with pages that use the
*edges* of each vocabulary, so any future copy that drifts away from the shared
source fails here instead of on the live wiki.
"""

import datetime as _dt

from vector_lake.schema_validator import (
    REQUIRED_FIELDS,
    VALID_CATEGORIES,
    VALID_EPISTEMIC_STATUS,
    VALID_STATUS,
    VALID_TYPES,
    missing_required_fields,
)
from vector_lake.wiki_utils import get_wiki_dir, read_markdown_file

from tests.test_mutation_coordinator import _write_purpose_contract
from vector_lake.tool_lint import lint_vector_lake


_BODY = (
    "## 1. 编译事实 (Compiled Truth - READ MODEL)\n\n- x\n\n"
    "## 2. 证据时间线 (Timeline - EVENT STORE)\n\n- [2026-09-23] [Observation] x\n"
)


def _page(name: str, *, type_: str, status: str, epistemic: str, category: str) -> None:
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text(
        "---\n"
        f"id: test_{name[:-3].lower()}\n"
        f"title: {name[:-3]}\n"
        f"type: {type_}\n"
        "domain: General\n"
        f"status: {status}\n"
        f"epistemic-status: {epistemic}\n"
        f"categories: [{category}]\n"
        # Relative to now: semantic GC archives a page that is >30 days old, near-empty and
        # unlinked, so a hard-coded date silently crossed that threshold as the calendar moved and
        # the assertion below then read a file the GC had already moved.
        f"updated: {_dt.date.today().isoformat()}\n"
        "sources: []\n"
        "strategic_scope: core\n"
        "evidence_tier: derived\n"
        "---\n\nBody text.\n",
        encoding="utf-8",
    )


def test_the_shared_vocabularies_are_the_edges_the_lint_must_accept(isolated_memory):
    """Drive each check with the value that the drifted copy rejected."""
    assert "Contested" in VALID_STATUS  # the value the live wiki actually uses
    assert "Archived" in VALID_STATUS
    assert "system" in VALID_TYPES
    assert "evergreen" in VALID_EPISTEMIC_STATUS
    assert "Entities_and_Actors" in VALID_CATEGORIES

    _page(
        "Concept_Edge-Values.md",
        type_="system",
        status="Contested",
        epistemic="evergreen",
        category="Entities_and_Actors",
    )

    report = lint_vector_lake(auto_fix=False)

    for label in ("Invalid status", "Invalid type", "Invalid epistemic-status", "Invalid category"):
        assert label not in report, f"{label} reported for a legal vocabulary value:\n{report}"


def test_status_matching_stays_case_insensitive(isolated_memory):
    """The value is lowercased for comparison; the vocabulary is not."""
    _page(
        "Concept_Lowercase-Status.md",
        type_="concept",
        status="active",
        epistemic="seed",
        category="Uncategorized",
    )

    report = lint_vector_lake(auto_fix=False)

    assert "Invalid status" not in report
    assert "Invalid epistemic-status" not in report


def test_system_artifacts_may_carry_their_own_category(isolated_memory):
    """``SCHEMA_CATEGORIES.md`` scopes the ontology to knowledge nodes.

    Derived system artifacts are not entities, concepts or synthesis nodes, so the
    daemon's own ``System`` marker is allowed -- and only on a ``System_*`` page.
    """
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "System_Community_L0_deadbeef.md").write_text(
        "---\n"
        "id: gov_deadbeef\n"
        "title: Community index\n"
        "type: system\n"
        "status: Active\n"
        "categories: [System]\n"
        "updated: 2026-09-16\n"
        "---\n\n- [[Concept_Alpha]]\n",
        encoding="utf-8",
    )
    report = lint_vector_lake(auto_fix=False)
    assert "Invalid category" not in report, report

    # The same category on a knowledge node is still rejected.
    _page(
        "Concept_System-Category.md",
        type_="concept",
        status="Active",
        epistemic="seed",
        category="System",
    )
    report = lint_vector_lake(auto_fix=False)
    assert "Invalid category 'System'" in report, report


def test_a_missing_judgement_field_is_reported_not_invented(isolated_memory):
    """The repair may fill in what the filename determines, not what someone must decide.

    This block used to write ``Active``, ``seed``, ``edge``, ``General`` and
    ``["Uncategorized"]`` for whatever it found missing -- the origin of 3 000 unclassified
    pages and of a status axis that read 99.5% ``Active``.  A missing judgement is a governance
    event, so it is reported and left alone.
    """
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Bare.md").write_text(
        "---\nid: bare\ntitle: Bare\ntype: concept\n---\n\n" + _BODY,
        encoding="utf-8",
    )

    report = lint_vector_lake(auto_fix=True)

    frontmatter, _, _ = read_markdown_file(wiki / "Concept_Bare.md")
    assert "Missing fields" in report, report
    for field in (
        "domain",
        "topic_cluster",
        "status",
        "epistemic-status",
        "categories",
        "strategic_scope",
        "evidence_tier",
    ):
        assert field not in frontmatter, f"{field} was invented: {frontmatter.get(field)!r}"


def test_a_missing_mechanical_field_is_still_repaired(isolated_memory):
    """Positive control: a filename-derived field and a timestamp are not judgements."""
    _write_purpose_contract(isolated_memory)
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Repairable.md").write_text(
        "---\n"
        "title: Repairable\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [System_Architecture]\n"
        "strategic_scope: core\n"
        "---\n\n" + _BODY,
        encoding="utf-8",
    )

    lint_vector_lake(auto_fix=True)

    frontmatter, _, _ = read_markdown_file(wiki / "Concept_Repairable.md")
    assert frontmatter.get("id"), "the id is derivable from the name"
    assert frontmatter.get("updated"), "the timestamp is a fact about this write"
    assert frontmatter.get("sources") == [], "an absent sources key means nothing recorded"


def test_an_invalid_type_is_corrected_to_what_the_name_says(isolated_memory):
    """``concept`` for every page renamed a Vendor_ page's type to a value its name contradicts."""
    _write_purpose_contract(isolated_memory)
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Vendor_Wrong-Type.md").write_text(
        "---\n"
        "id: vendor_wrong_type\n"
        "title: Wrong Type\n"
        "type: nonsense\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [System_Architecture]\n"
        "strategic_scope: core\n"
        "updated: 2026-09-23T00:00:00Z\n"
        "sources: []\n"
        "---\n\n" + _BODY,
        encoding="utf-8",
    )

    lint_vector_lake(auto_fix=True)

    frontmatter, _, _ = read_markdown_file(wiki / "Vendor_Wrong-Type.md")
    assert frontmatter["type"] == "vendor", frontmatter["type"]


def test_an_invalid_status_is_reported_and_not_rewritten(isolated_memory):
    """``Contested`` and ``Archived`` are legal; a typo is a report, not an overwrite to Active."""
    _page("Concept_Odd-Status.md", type_="concept", status="Retired", epistemic="seed", category="System_Architecture")

    report = lint_vector_lake(auto_fix=True)

    assert "Invalid status 'retired'" in report, report
    frontmatter, _, _ = read_markdown_file(get_wiki_dir() / "Concept_Odd-Status.md")
    assert frontmatter["status"] == "Retired", frontmatter["status"]


def test_the_metric_evidence_census_reports_coverage_and_gaps(isolated_memory):
    """The reader that makes ``evidence_tier`` worth writing.

    Not every page is asked for a tier -- that was the shape that left 89.6% of the corpus empty.
    A page that puts a number into its compiled truth is, and this census is where the answer is
    read.
    """
    _write_purpose_contract(isolated_memory)
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)

    def body(metric: bool) -> str:
        line = "- [[Concept_X]] 的 {Metric: Market_Share} 为 12% (Source: [[Source_X]])\n"
        head, sep, tail = _BODY.partition("## 2. 证据时间线")
        return f"{head}{line if metric else ''}\n{sep}{tail}"

    def page(name: str, tier: str, metric: bool) -> None:
        evidence_line = f"evidence_tier: {tier}\n" if tier else ""
        (wiki / name).write_text(
            "---\n"
            f"id: {name[:-3].lower()}\ntitle: {name[:-3]}\ntype: concept\ndomain: Medical_IT\n"
            "status: Active\nepistemic-status: seed\ncategories: [System_Architecture]\n"
            f"strategic_scope: core\n{evidence_line}"
            "updated: 2026-09-23T00:00:00Z\nsources: []\n---\n\n" + body(metric),
            encoding="utf-8",
        )

    page("Concept_Metric-No-Tier.md", "", metric=True)
    page("Concept_Metric-With-Tier.md", "primary", metric=True)
    page("Concept_No-Metric.md", "", metric=False)

    report = lint_vector_lake(auto_fix=False)

    assert "asserts Market_Share without an evidence_tier" in report, report
    assert "coverage: 1 of 2 metric-asserting page(s)" in report, report
    assert "Concept_No-Metric.md: asserts" not in report, "a page with no number was counted"


def test_a_source_page_beside_its_own_node_is_not_a_collision(isolated_memory):
    """The provenance record and the node it fed share a name by design.

    ``Source_哲学家的工具箱`` and ``Concept_哲学家的工具箱`` are one document and one concept, not
    one entity recorded twice: merging them deletes either the provenance record or the knowledge.
    Nine of the 42 identity pairs were this shape, and nine of the merge detector's twenty
    candidates for the same reason (it matches on alias overlap).
    """
    _page("Source_Alpha.md", type_="source", status="Active", epistemic="seed", category="System_Architecture")
    _page("Concept_Alpha.md", type_="concept", status="Active", epistemic="seed", category="System_Architecture")

    report = lint_vector_lake(auto_fix=False)

    assert "Concept_Alpha.md <-> Source_Alpha.md" not in report, report
    assert "excluded as a source page beside its own node: 1 pairs" in report, report


def test_one_name_under_two_knowledge_types_is_still_a_collision(isolated_memory):
    """Positive control: the exclusion is about ``Source_``, not about cross-type pairs."""
    _page("Concept_Beta.md", type_="concept", status="Active", epistemic="seed", category="System_Architecture")
    _page("Product_Beta.md", type_="product", status="Active", epistemic="seed", category="System_Architecture")

    report = lint_vector_lake(auto_fix=False)

    assert "Concept_Beta.md <-> Product_Beta.md" in report, report


def test_a_generated_index_is_not_an_orphan(isolated_memory):
    """98% of the 812 orphans this report carried were the wiki's own machinery.

    Nothing links to a cluster index and the indexer skips the namespace, so a check that
    exempted only ``Source_*`` was reporting generated pages as unlinked knowledge.  The
    knowledge page beside it, with no inbound links, is still reported.
    """
    _write_purpose_contract(isolated_memory)
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "System_Topic.md").write_text(
        "---\n"
        "id: gov_topic\ntitle: System_Topic\ntype: system\nstatus: Active\n"
        "categories: [Uncategorized]\nupdated: 2026-09-23T00:00:00Z\n---\n\n"
        "# L0 Comm: A cluster\n\n## 核心节点 (Hubs)\n\n## 社区成员 (Members)\n",
        encoding="utf-8",
    )
    _page(
        "Concept_Lonely.md",
        type_="concept",
        status="Active",
        epistemic="seed",
        category="System_Architecture",
    )

    report = lint_vector_lake(auto_fix=False)
    orphan_lines = [line for line in report.splitlines() if "No inbound links" in line]

    assert any("Concept_Lonely.md" in line for line in orphan_lines), orphan_lines
    assert not any("System_Topic.md" in line for line in orphan_lines), orphan_lines


def test_missing_required_fields_is_the_single_source():
    """The linter and the write gate must agree on which keys a page needs."""
    system = {
        "id": "x",
        "title": "t",
        "type": "system",
        "status": "Active",
        "categories": ["Uncategorized"],
        "updated": "2026-01-01",
    }
    assert missing_required_fields(dict(system), "System_Community_L0_deadbeef.md") == []
    assert missing_required_fields(dict(system), "Concept_X.md") == [
        "domain",
        "epistemic-status",
        "sources",
    ]
    assert "sources" in REQUIRED_FIELDS


def test_system_artifacts_are_not_flagged_for_exempt_fields(isolated_memory):
    """A ``System_*`` page missing domain/epistemic-status/sources is legal.

    ``validate_schema`` exempts those keys for system artifacts; the linter used
    to ignore the exemption and flagged all 572 community indexes.
    """
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "System_Community_L0_deadbeef.md").write_text(
        "---\n"
        "id: gov_deadbeef\n"
        "title: Community index\n"
        "type: system\n"
        "status: Active\n"
        "categories: [Uncategorized]\n"
        "updated: 2026-09-16\n"
        "---\n\n- [[Concept_Alpha]]\n",
        encoding="utf-8",
    )

    report = lint_vector_lake(auto_fix=False)

    assert "Missing fields" not in report, report
