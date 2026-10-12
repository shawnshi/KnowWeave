import re
import json
from datetime import datetime
from pathlib import Path

from vector_lake.node_vocabulary import (
    GENERATED_ARTIFACT_PREFIX,
    NODE_TYPE_SET,
    NON_NODE_WIKI_FILES,
    STUB_MARKER_TAG,
    is_generated_artifact,
)

class SchemaViolationException(Exception):
    pass

VALID_H3_SLOTS = {
    "vendor": ["### 组织架构与商业模式 (Business Model)", "### 核心护城河 (Moat)", "### 市场占位与竞争态势 (Market & Competition)", "### 生态位与战略联盟 (Ecosystem & Alliances)", "### 关键产品线 (Key Products)", "### 核心团队与权力拓扑 (Key Personnel)"],
    "institution": ["### 机构定位与核心诉求 (Positioning & Needs)", "### 数字化演进路线 (Digital Roadmap)", "### 核心供应商与生态锚定 (Key Suppliers & Lock-ins)", "### 预算流动与财务状况 (Budget & Financial Health)"],
    "concept": ["### 物理机制 (Mechanism)", "### 适用与失效边界 (Boundaries)", "### 产业落地与代表实例 (Implementations)", "### 演进关联 (Evolution)"],
    "product": ["### 目标客群与应用边界 (Target ICP & Use Cases)", "### 临床与管理价值流 (Clinical & Admin Value)", "### 部署架构与底层依赖 (Architecture & Dependencies)", "### 医疗合规与资质壁垒 (Compliance & Certifications)", "### 商业化与交付模式 (Monetization & Delivery)"],
    "person": ["### 核心权责与控制域 (Mandates & Domain of Control)", "### 关键造物与历史印记 (Key Artifacts & Legacy)", "### 核心主张与商业/技术理念 (Key Stances & Philosophies)", "### 利益纽带与权力拓扑 (Affiliations & Power Topology)"],
    "event": ["### 动因与前置条件 (Catalysts & Preconditions)", "### 核心影响与转折 (Impact)", "### 关键参与方 (Stakeholders)", "### 后续衍生与未决节点 (Fallout & Unresolved Issues)"],
    "policy": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"],
    "standard": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"]
}

VALID_TYPES = NODE_TYPE_SET

# Conditional slot: required on Section 1 exactly when the frontmatter declares
# ``tension_edges``.  Exported so the merge path can reproduce the same allow-list
# instead of duplicating the literal.
TENSION_H3_SLOT = "### 认知张力与未决争议 (Controversies & Tensions)"

# The taxonomy cap on ``tags``.  Exported for the same reason as the slot above: the
# merge path has to respect the bound, and a second literal would let the two drift.
MAX_TAGS = 3

# Source pages that exist only to satisfy the schema for nodes whose real provenance was
# never assigned.  ``Source_Auto_Fixed`` is the live example; its own body says the
# connected nodes "require future manual review to assign their true provenance".  Citing
# one therefore asserts that provenance is *unknown* -- it is a marker, not evidence.
#
# The size of the backlog decides the enforcement: a census on 2026-09-19 found 2 267
# live pages citing it (28.6% of the wiki), so a flat ban would have made more than a
# quarter of the store unwritable and frozen the very pages the anchor was created for.
# A re-census on 2026-09-28 found no live page citing it -- ``Source_Auto_Fixed.md`` is the
# only file that still contains the string, and it does not cite itself -- so what remains
# enforced is *growth*: see ``check_placeholder_sources``.
PLACEHOLDER_SOURCES = frozenset({"source_auto_fixed"})

VALID_CATEGORIES = {
    "Uncategorized",
    "General_Knowledge",
    "Artificial_Intelligence",
    "Healthcare_IT",
    "Strategy_and_Business",
    "System_Architecture",
    "Philosophy_and_Cognitive",
    "Biomedicine",
    "Policy_and_Governance",
    "Entities_and_Actors"
}

VALID_STATUS = {"Active", "Draft", "Superseded", "Deprecated", "Archived", "Contested"}
VALID_EPISTEMIC_STATUS = {"seed", "sprouting", "evergreen"}

#: The two sections ``schema.md`` requires of a ``Synthesis_`` page.  The gate enforces that they
#: are present; enforcing that they *open* the document would reject every legacy synthesis page
#: that put them last, so the order is reported by lint instead (``synthesis_skeleton_order_report``).
#: Two live pages showed the divergence is real: both carried the headings at the end of the body
#: and passed the gate while ``schema.md`` claimed the document "MUST begin with" them.
SYNTHESIS_SKELETON_HEADINGS = (
    "## 核心合成论点 (Core Synthesized Claims)",
    "## 支撑拓扑 (Supporting Topology)",
)


def synthesis_skeleton_order_report(body: str) -> str | None:
    """Non-blocking report when a synthesis page's required skeleton is not its opening sections."""
    headings = [
        line.strip() for line in (body or "").splitlines() if line.startswith("## ")
    ]
    if len(headings) < 2:
        return "synthesis page has fewer than two H2 sections"
    if tuple(headings[:2]) == SYNTHESIS_SKELETON_HEADINGS:
        return None
    return (
        "synthesis skeleton is present but not the opening sections "
        f"(first H2: {headings[0][:48]!r})"
    )

# ``SCHEMA_CATEGORIES.md`` scopes the category ontology to "entities, concepts, and
# synthesis logic nodes".  Derived system artifacts (the clustering daemon's
# ``System_Community_*`` indexes) are none of those, and the daemon marks them with
# ``categories: [System]``.  This set is the only place a ``System_*`` page may use
# a category outside ``VALID_CATEGORIES``.
SYSTEM_ARTIFACT_CATEGORIES = frozenset({"System"})

# The ``domain`` facet had no controlled vocabulary, and 192 distinct values accumulated by
# 2026-09-23 (67 of them used on exactly one page) while ``categories`` -- the axis that *is*
# governed -- sat 39% empty.  This is the canonical set.  It applies to **new** nodes only:
# refusing a legacy value here would freeze 7 400 pages that have not been migrated yet, and
# the migration is a separate reviewable pass.
VALID_DOMAINS = frozenset({
    "Medical_IT",
    "Artificial_Intelligence",
    "System_Architecture",
    "Enterprise_Software",
    "Strategy_and_Business",
    "Policy_and_Governance",
    "Biomedicine",
    "Cognitive_Science",
    "General",
})

# The second tier of the same facet, and the reason it exists: ``domain`` is read in the corpus as
# a *subject or industry*, not as a second copy of the macro axis.  Measured 2026-09-23, the pages
# left outside ``VALID_DOMAINS`` were not junk -- 54 verticals (Sociology, Startup, Venture_Capital,
# Semiconductor, Defense_Tech, Tobacco, ...) whose ``categories`` already said the macro thing and
# whose tags carried the vertical in exactly 1 page of 118.  Flattening them into the nine macro
# values would have deleted the only record of their subject.
#
# So the facet is **open but curated**: a new node may use a macro domain or one of these
# registered verticals, and anything else is reported -- and refused on a new node -- until it is
# registered here.  Registration is a governance act, not a consequence of a page existing.
DOMAIN_VERTICALS = frozenset({
    # Social science and the humanities: no macro value names them faithfully.
    "Sociology",
    "Academic_Sociology",
    "Science_Epistemology",
    "Science",
    "Scientific_Research",
    "Mathematics",
    "History",
    "Arts",
    "Narratology",
    "Communication",
    "Neuroscience",
    "Study",
    # Media, as an industry and as a subject.
    "Media",
    "New_Media",
    # Industries with no faithful macro home.
    "Tobacco",
    "Space_Technology",
    "Agriculture_Machinery",
    "Consumer_Electronics",
})


#: Spellings that name a subject a macro domain already names, mapped to that macro value.
#:
#: ``Healthcare_IT`` is the *categories* macro value for exactly what the domain facet calls
#: ``Medical_IT`` -- ``SCHEMA_CATEGORIES.md`` defines it as "Digital health systems, hospital
#: implementations, electronic health records" -- so the two axes offer one subject under two
#: names, and an author reaching for the spelling they just read on the other axis is not inventing
#: a subject, they are reading the wrong field's vocabulary.  Measured 2026-09-26: the Source page
#: for ``raw/research/刘海一先生的历史定位、生平贡献与思想体系深度解析20260926.md`` was refused
#: with ``domain: Healthcare_IT`` while its two siblings in the same batch happened to emit
#: ``Medical_IT`` -- a deterministic schema violation that abandoned the source.
#:
#: These are **aliases, not verticals**.  The registration criterion for ``DOMAIN_VERTICALS`` is
#: that no macro domain can faithfully express the subject, and ``Medical_IT`` does express this
#: one.  Registering it as a vertical would put two values for a single subject into the facet,
#: which is what ``tool_search._passes_filters`` compares by equality to decide a match.
DOMAIN_ALIASES = {
    "Healthcare_IT": "Medical_IT",
}


def canonical_domain(domain: str | None) -> str:
    """The canonical spelling of a ``domain`` value: an alias resolved, anything else unchanged.

    The single owner for the mapping.  Readers that compare ``domain`` for equality call this
    instead of carrying a second copy of the table -- ``tool_search._passes_filters`` is the one
    that decides whether a search returns a page, and a copy there is how a vocabulary splits.
    """
    value = str(domain or "").strip()
    return DOMAIN_ALIASES.get(value, value)


def is_registered_domain(domain: str) -> bool:
    """A macro domain or a registered vertical: the two tiers of the ``domain`` facet.

    An alias of a macro domain passes: it names that macro domain, and its page is accepted under
    the spelling it was authored with.  Rewriting the stored value would mean rewriting page
    content from inside a validator that ``mutation_coordinator`` documents as pure, and the
    legacy-field migration is a separate reviewable pass (see ``VALID_DOMAINS``).
    """
    canonical = canonical_domain(domain)
    return canonical in VALID_DOMAINS or canonical in DOMAIN_VERTICALS


def category_shape_violation(frontmatter: dict, filename: str) -> str | None:
    """The category contract's shape and vocabulary, enforced on **every** write.

    This is the rule's single owner.  It lived in two places -- here for a new node, and in
    ``purpose_contract.validate_ingest_payload`` for whatever that gate happened to see -- which
    is worse than duplication: an update in ``schema`` mode was checked by neither, so a bare
    string could be written back onto an existing page.

    The message names what arrived, not only what was expected.  It is the recorded reason an
    operator reads, and ``DHWB-20260913.md`` failed six days of ingest rounds on this one rule.
    """
    categories = frontmatter.get("categories")
    if isinstance(categories, list):
        received = f"a list of {len(categories)} element(s): {categories!r}"
    elif categories is None:
        received = "no value"
    else:
        received = f"{type(categories).__name__} {categories!r}"

    if not isinstance(categories, list) or len(categories) != 1:
        return f"categories must be a list with exactly one domain. Received {received}."

    category = categories[0]
    if not isinstance(category, str):
        return f"category must be a string. Received {received}."
    artifact = is_generated_artifact(frontmatter, filename)
    if category not in VALID_CATEGORIES and not (
        artifact and category in SYSTEM_ARTIFACT_CATEGORIES
    ):
        return f"Invalid category '{category}'. Allowed: {sorted(VALID_CATEGORIES)}"
    return None


def classification_violations(frontmatter: dict, filename: str, body: str | None = None) -> list[str]:
    """The category rules that apply to a **newly authored** node only.

    Shape and vocabulary are deliberately absent: they are ``category_shape_violation``, enforced
    on every write, because a legacy page does not stop needing a well-formed category.  What is
    left here is what a page that already exists may keep and a new one may not.
    """
    violations: list[str] = []
    artifact = is_generated_artifact(frontmatter, filename, body)

    # The legacy marker is the one category value a new node may not declare.
    categories = frontmatter.get("categories")
    if isinstance(categories, list) and len(categories) == 1:
        category = categories[0]
        if category == "Uncategorized" and STUB_MARKER_TAG not in (frontmatter.get("tags") or []):
            violations.append(
                "'Uncategorized' is for imported legacy nodes only. Align the new node to one of "
                "the macro-domains, or propose a schema mutation."
            )

    # domain: the subject facet stops drifting at the point of creation.  It has two tiers -- a
    # macro domain, or a vertical registered in DOMAIN_VERTICALS (see the note there for why a
    # second tier exists at all).  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    domain = str(frontmatter.get("domain") or "").strip()
    if domain and not artifact and not is_registered_domain(domain):
        violations.append(
            f"domain '{domain}' is neither a macro domain nor a registered vertical. "
            f"Use one of {sorted(VALID_DOMAINS)}, or register the vertical in "
            "SCHEMA_CATEGORIES.md and DOMAIN_VERTICALS."
        )

    # The generated namespace is not a knowledge namespace.  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # A page the wiki generates about itself is identified by its markers, not by its filename:
    # 235 community indexes were renamed to their titles and a name-only rule read them as
    # knowledge.
    if str(filename).startswith("System_") and not artifact:
        violations.append(
            "the System_ namespace holds generated artifacts "
            f"({GENERATED_ARTIFACT_PREFIX}*), not knowledge. File this node under a knowledge "
            "prefix (Concept_, Source_, Vendor_, ...) instead."
        )

    return violations

# The single source for "which frontmatter keys must be present".  ``tool_lint``
# used to keep its own shorter list with no system-file exemption, so the linter
# and the write gate disagreed about the same page.
REQUIRED_FIELDS = (
    "id", "title", "type", "domain", "status",
    "epistemic-status", "categories", "updated", "sources",
)
# Derived system artifacts (community indexes and the like) carry no domain, no
# epistemic status and no sources by design, so those keys are not required on one.  The
# exemption is scoped by artifact family, not by the ``System_`` prefix: 235 knowledge pages
# sit under that prefix and have to satisfy the same contract as any other node.
SYSTEM_FILE_EXEMPT_FIELDS = frozenset({"domain", "epistemic-status", "sources"})


def missing_required_fields(frontmatter: dict, filename: str, body: str | None = None) -> list[str]:
    """Required frontmatter keys absent from ``frontmatter``, in contract order.

    Callers raise on ``missing[0]`` so the reported field matches the order the
    contract lists them in.  Key *presence* is what counts: ``sources: []`` is a
    present, satisfiable value, not a missing field.
    """
    exempt = (
        SYSTEM_FILE_EXEMPT_FIELDS
        if is_generated_artifact(frontmatter, filename, body)
        else frozenset()
    )
    return [field for field in REQUIRED_FIELDS if field not in frontmatter and field not in exempt]

# Metric keys double as a physical unit contract.  Keep legacy keys readable,
# but use the unambiguous keys below for all newly compiled SIR evidence.
CONTROLLED_METRICS = {
    "MedIT_Revenue", "Bids_Won", "Market_Share", "EMR_Level", "CHI_Level", "SLA",
    "Bid_Count", "Bid_Value_CNY", "FHIR_OMOP_Center_Count", "IT_Budget_Change_Pct",
    "Public_Cloud_Deployment_Ratio", "Acceptance_Case_Count", "Engineering_Test_RPS",
    "Engineering_Test_P99_MS", "Engineering_Test_Error_Rate_Pct",
    "GPU_Infrastructure_Cost_CNY", "API_Access_Fee_CNY", "Implementation_Duration_Days",
    "Implementation_Cost_CNY", "Project_Cancellation_Rate_Pct", "SaaS_Value_Share_Pct",
}

INLINE_SOURCE_ANCHOR = re.compile(r"\(Source:\s*\[\[Source_[^\]]+\]\](?:[^)]*)\)")

#: The one spelling of a controlled-metric assertion.  ``validate_schema`` and the evidence census
#: in ``tool_lint`` both read the corpus through this, so a change to the syntax cannot leave one
#: of them counting something else.
METRIC_ASSERTION = re.compile(r"\{Metric:\s*([^}]+)\}")


def asserted_metric_keys(body: str) -> list[str]:
    """The controlled-metric keys a page asserts, in document order."""
    return [match.strip() for match in METRIC_ASSERTION.findall(body)]


def metric_evidence_violation(frontmatter: dict, body: str) -> str | None:
    """A page that asserts a controlled metric has to say what supports the number.

    ``evidence_tier`` is the machine-readable form of "the vendor said so" against "independently
    verified" -- the distinction that decides whether a figure may be quoted as established.
    Requiring it of *every* page was the wrong shape: 89.6% of the corpus left it empty because
    nothing read it, and it is a judgement, not a default.

    Requiring it exactly where a number is asserted is a claim about that number's support, and it
    costs one field on the handful of pages that make a quantitative claim.
    """
    keys = asserted_metric_keys(body)
    if not keys:
        return None
    if str(frontmatter.get("evidence_tier") or "").strip():
        return None
    return (
        f"this page asserts a controlled metric ({', '.join(sorted(set(keys)))}) without an "
        "evidence_tier. State what supports the number."
    )


def source_key(entry) -> str:
    """A ``sources`` entry reduced to a comparable key.

    Three shapes have to collapse to one key.  ``sources`` holds both ``raw/...`` paths
    and ``[[Source_X|display]]`` links; the link is written with and without the ``.md``
    suffix; and an unquoted ``- [[Source_Auto_Fixed]]`` is valid YAML *flow sequence*
    syntax, so a hand-written entry arrives here as a nested list rather than a string.
    A string-only comparison silently misses that last form, which is the one a writer is
    most likely to produce by hand.
    """
    if isinstance(entry, (list, tuple)):
        return " ".join(source_key(item) for item in entry)
    text = str(entry).strip()
    match = re.match(r"^\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]$", text)
    if match:
        text = match.group(1).strip()
    if text.lower().endswith(".md"):
        text = text[:-3]
    return text.lower()


def check_placeholder_sources(new_sources, previous_sources=()) -> None:
    """Raise when a placeholder source appears that the page did not already carry.

    Deliberately not a blanket ban.  Every page that already cites a placeholder keeps
    working, so the gate stops the marker from spreading without freezing the thousands of
    pages it was invented for, and a repair write that removes it is never blocked.

    ``previous_sources`` is the page's current ``sources`` list, or empty when the page
    does not exist yet -- which is exactly the case that must be caught.
    """
    previous = {source_key(entry) for entry in (previous_sources or [])}
    for entry in new_sources or []:
        key = source_key(entry)
        if key in PLACEHOLDER_SOURCES and key not in previous:
            raise SchemaViolationException(
                f"Provenance Violation: '{entry}' is a placeholder for pages whose real "
                "provenance is unassigned, and this page did not already carry it. Cite "
                "the actual source, or leave 'sources' empty -- an empty list is honest, "
                "the placeholder is not."
            )


#: The controlled vocabulary for ``[predicate:: [[Target]]]`` links, from ``schema.md``.
#: Kept backward compatible with the shapes that were already in the corpus.
VALID_PREDICATES = frozenset({
    "is-a", "part-of", "evolved-from", "created", "founded", "authored", "architected",
    "competes-with", "supplies-to", "supplied-by", "blocks", "controls", "manages", "invested-in", "allied-with",
    "integrates-with", "runs-on", "deployed-at", "complies-with", "certified-by",
    "validates", "falsifies", "depends-on", "instantiated-by", "mentions", "related_to", "has_part",
    "属于", "核心构件", "关联", "提及", "引用", "类似",
    "parent", "belongs_to", "instance_of", "peer", "see_also", "conflicts-with",
})


#: The controlled event vocabulary.  One owner: the timeline check below writes it as ``[Tag]``
#: and ``tool_ingest.INTEGRATION_EVENT_TAGS`` uses it bare, and both derive from this tuple, so a
#: tag can never be legal in one place and unknown in the other.  It is also what
#: ``vector_lake.output_contract`` publishes to the model, which is the point -- a vocabulary the
#: validator enforces has to reach the prompt, or the model can only guess at it.
INGEST_EVENT_TAGS = (
    "Release", "Pivot", "Conflict", "Validation",
    "Observation", "Decision", "Execution", "Outcome",
)
if not INGEST_EVENT_TAGS:
    raise RuntimeError("INGEST_EVENT_TAGS must not be empty")


#: Integration binds a source *document* to an existing page, so it may only use predicates that
#: describe how a document stands to a page it did not write: evidence (``validates``,
#: ``falsifies``, ``depends-on``), provenance of the document itself (``evolved-from``), a standard
#: it follows (``complies-with``), a standing it contests (``conflicts-with``), and plain reference
#: (``mentions``, ``related_to``, and their Chinese synonyms).  It deliberately excludes the
#: *entity taxonomy* predicates -- ``is-a``, ``part-of``, ``has_part``, ``instance-of``,
#: ``belongs_to``, ``parent``, ``核心构件``, ``属于`` -- because a document is not an instance of a
#: concept or a part of one; when the closest reading is structural, the honest answer is
#: ``related_to``.  That distinction is the whole point of the narrower set, and both prompt
#: surfaces state it.
#: Measured 2026-09-28: the first cut held only the five evidence/association predicates, and a
#: "体系研究报告" failed every retry with ``unsupported integration predicate: evolved-from`` --
#: its true relation (a report evolving out of a standard) had no legal expression, so the model
#: picked the predicate it meant each time and the ingest was refused each time until the source
#: was abandoned.  The gap was expressive, not misbehaviour.  Deriving the set from
#: ``VALID_PREDICATES`` behind a subset guard keeps one owner for the vocabulary: a drift fails
#: at import, not at finalize.
INGEST_INTEGRATION_PREDICATES = frozenset({
    "validates", "falsifies", "depends-on", "mentions", "related_to",
    "evolved-from", "complies-with", "conflicts-with",
    "引用", "提及", "关联", "类似",
})
if not INGEST_INTEGRATION_PREDICATES <= VALID_PREDICATES:
    raise RuntimeError(
        "Ingest integration predicates are missing from VALID_PREDICATES: "
        f"{sorted(INGEST_INTEGRATION_PREDICATES - VALID_PREDICATES)}. "
        "The page-link vocabulary and the ingest integration contract must agree."
    )


def validate_schema(
    frontmatter: dict, body: str, filename: str, index_path: Path = None, is_new: bool | None = None
):
    """
    Validates a Vector Lake Wiki node against the strict constraints of schema.md.
    Raises SchemaViolationException on any failure.
    """
    if not filename.endswith(".md"):
        return

    # Skip system meta files
    if filename in NON_NODE_WIKI_FILES:
        return

    # --- 1. FRONTMATTER VALIDATION ---
    if not isinstance(frontmatter, dict):
        raise SchemaViolationException("Schema Violation: Frontmatter must be a valid YAML object.")

    # 1.1 Required Fields
    # strategic_scope and evidence_tier are highly recommended but we allow legacy files without them
    missing = missing_required_fields(frontmatter, filename, body)
    if missing:
        raise SchemaViolationException(f"Schema Violation: Missing required frontmatter field '{missing[0]}'.")
    # 1.2 Type Validation
    doc_type = frontmatter.get("type", "").lower()
    if doc_type not in VALID_TYPES:
        raise SchemaViolationException(f"Schema Violation: Invalid type '{doc_type}'. Must be one of {VALID_TYPES}.")

    # 1.2b The classification contract.  Shape and vocabulary are checked on every write, new
    # node or not; the rules that follow apply only when this write *creates* one, because a
    # legacy page keeps the classification it was written under until the migration runs.
    shape = category_shape_violation(frontmatter, filename)
    if shape:
        raise SchemaViolationException(f"Schema Violation: {shape}")
    if is_new:
        violations = classification_violations(frontmatter, filename, body)
        if violations:
            raise SchemaViolationException(f"Schema Violation: {violations[0]}")
        evidence = metric_evidence_violation(frontmatter, body)
        if evidence:
            raise SchemaViolationException(f"Schema Violation: {evidence}")

    # 1.3 Epistemic Status
    epistemic_status = str(frontmatter.get("epistemic-status", "")).lower()
    if epistemic_status and epistemic_status not in VALID_EPISTEMIC_STATUS:
        raise SchemaViolationException(f"Schema Violation: epistemic-status '{epistemic_status}' is invalid. Allowed: {VALID_EPISTEMIC_STATUS}.")

    # 1.4 Status
    status = str(frontmatter.get("status", "")).title()
    if status and status not in VALID_STATUS:
        raise SchemaViolationException(f"Schema Violation: status '{status}' is invalid. Allowed: {VALID_STATUS}.")

    # 1.5 Tags Constraints
    tags = frontmatter.get("tags", [])
    if isinstance(tags, list) and len(tags) > MAX_TAGS:
        raise SchemaViolationException(f"Taxonomy Violation: Maximum {MAX_TAGS} tags allowed, but found {len(tags)}.")
        
    # 1.5b Alias Constraints
    aliases = frontmatter.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        for alias in aliases:
            text = str(alias).strip()
            if text.startswith("#"):
                raise SchemaViolationException(
                    f"Schema Violation: alias '{text}' starts with '#', which is tag syntax. "
                    "Aliases name the entity itself, so a '#'-prefixed entry puts a tag in the "
                    "entity namespace and would let a tag resolve as a link target. Put it in tags:."
                )

    # 1.6 Dates
    try:
        if "created" in frontmatter:
            datetime.fromisoformat(str(frontmatter["created"]).replace("Z", "+00:00"))
        datetime.fromisoformat(str(frontmatter["updated"]).replace("Z", "+00:00"))
    except ValueError:
        raise SchemaViolationException("Schema Violation: 'created' and 'updated' must be valid ISO8601 timestamps.")
        
    # 1.7 Tension Edges & STQM
    tension_edges = frontmatter.get("tension_edges", [])
    if tension_edges:
        if not isinstance(tension_edges, list):
            raise SchemaViolationException("Schema Violation: 'tension_edges' must be a list.")
        for te in tension_edges:
            if not isinstance(te, dict) or 'target' not in te or 'polarity' not in te or 'intensity' not in te:
                raise SchemaViolationException("Schema Violation: 'tension_edges' items must contain target, polarity, and intensity.")
            try:
                polarity = float(te['polarity'])
                intensity = float(te['intensity'])
            except ValueError:
                raise SchemaViolationException("Schema Violation: tension_edges polarity and intensity must be floats.")
            if not -1.0 <= polarity <= 1.0:
                raise SchemaViolationException(f"Schema Violation: tension_edges polarity {polarity} out of bounds [-1.0, 1.0].")
            if not 0.0 <= intensity <= 1.0:
                raise SchemaViolationException(f"Schema Violation: tension_edges intensity {intensity} out of bounds [0.0, 1.0].")

    # 1.8 YAML Tyranny
    for forbidden_key in ["parents", "children", "competes_with"]:
        if forbidden_key in frontmatter:
            raise SchemaViolationException(f"SSOT Violation: Topological edge '{forbidden_key}' must not exist in YAML. Use Markdown semantic links instead.")

    # --- 2. FILE NAMING ---
    prefix = filename.split("_")[0]
    if prefix.lower() != doc_type and doc_type != "system":
        raise SchemaViolationException(f"Schema Violation: Filename prefix '{prefix}' does not match frontmatter type '{doc_type}'.")

    # --- 3. BODY SYNTAX VALIDATION ---
    
    # 3.1 Exempted Files (Source, Synthesis, System)
    if prefix in {"Source", "System"}:
        pass
    elif prefix == "Synthesis":
        if any(heading not in body for heading in SYNTHESIS_SKELETON_HEADINGS):
            raise SchemaViolationException("Schema Violation: Synthesis files must contain '## 核心合成论点 (Core Synthesized Claims)' and '## 支撑拓扑 (Supporting Topology)'.")
    # 3.2 Dual-Schema Entities
    else:
        # Check Dual-Schema Split
        section_1_match = re.search(r'## 1\. 编译事实.*?(?=## 2\. 证据时间线|---|\Z)', body, re.DOTALL)
        if not section_1_match:
            raise SchemaViolationException("Schema Violation: Missing '## 1. 编译事实' section.")
        if "## 2. 证据时间线" not in body:
            raise SchemaViolationException("Schema Violation: Missing '## 2. 证据时间线' section.")

        section_1_text = section_1_match.group(0)

        # Pronoun constraints are deliberately not enforced: a standalone-pronoun
        # heuristic produced false positives on Chinese prose, so only structural
        # checks run here.
        
        # H3 Slots
        h3_headers = re.findall(r'^###\s+(.*)$', section_1_text, re.MULTILINE)
        allowed_slots = list(VALID_H3_SLOTS.get(doc_type, []))
        if tension_edges:
            allowed_slots.append(TENSION_H3_SLOT)
            
        for header in h3_headers:
            header_cleaned = f"### {header.strip()}"
            if header_cleaned not in allowed_slots:
                raise SchemaViolationException(f"Schema Violation: Invalid H3 header '{header_cleaned}' for type '{doc_type}'. Allowed slots: {allowed_slots}.")
        
        if tension_edges and TENSION_H3_SLOT not in section_1_text:
            raise SchemaViolationException(f"Schema Violation: 'tension_edges' defined in YAML but missing '{TENSION_H3_SLOT}' slot.")

        # Metric Constraint
        metric_matches = asserted_metric_keys(section_1_text)
        for m in metric_matches:
            if m.strip() not in CONTROLLED_METRICS:
                raise SchemaViolationException(f"Schema Violation: Invalid Metric key '{m.strip()}'. Allowed keys: {sorted(CONTROLLED_METRICS)}.")

        # Metrics are hard evidence.  A number without an auditable primary
        # source is not allowed to alter the Compiled Truth read model.
        for line in section_1_text.splitlines():
            if "{Metric:" in line and not INLINE_SOURCE_ANCHOR.search(line):
                raise SchemaViolationException(
                    "Schema Violation: Metric assertions require an inline "
                    "'(Source: [[Source_*]])' anchor."
                )

        # Event Store (Timeline) validation
        section_2_match = re.search(r'## 2\. 证据时间线.*', body, re.DOTALL)
        if section_2_match:
            section_2_text = section_2_match.group(0)
            # Find all bullets
            bullets = re.findall(r'^\s*-\s+(.*)$', section_2_text, re.MULTILINE)
            valid_tags = {f"[{tag}]" for tag in INGEST_EVENT_TAGS}
            for bullet in bullets:
                # Bypass pure text instructions or quotes
                if bullet.startswith("[YYYY-MM-DD]") or "Event_Tag" in bullet: continue
                # Match strict timeline start
                if not re.match(r'^\[\d{4}-\d{2}-\d{2}\]', bullet):
                    raise SchemaViolationException(f"Schema Violation: Timeline entry '{bullet[:20]}...' must start with [YYYY-MM-DD].")
                
                # Check for tag
                tag_match = re.search(r'^\[\d{4}-\d{2}-\d{2}\]\s+(\[.*?\])', bullet)
                if not tag_match or tag_match.group(1) not in valid_tags:
                    raise SchemaViolationException(f"Schema Violation: Timeline entry must have a valid Event_Tag {valid_tags}. Found in: {bullet[:30]}")

    # --- 4. SEMANTIC LINKS (PREDICATES) ---
    clean_body = re.sub(r'```.*?```', '', body, flags=re.DOTALL)
    clean_body = re.sub(r'`.*?`', '', clean_body)
    
    # Check for invalid predicates in typed links.
    #
    # The vocabulary is a module constant so it has one owner and the ingest prompt can state it
    # instead of the model inventing one: ``Invalid predicate 'derived_from'`` was a live failure
    # on 2026-09-19, alongside the ``categories`` shape.
    valid_predicates = VALID_PREDICATES
    
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        if predicate not in valid_predicates:
            # We can be strict and block them
            raise SchemaViolationException(f"Schema Violation: Invalid predicate '{predicate}'.")
            
    # Naked links check: schema says ALL topological relations must use [predicate:: [[Target]]].
    # But RAG sources often use naked links for sources (e.g. (Source: [[Source_X]])).
    # We shouldn't raise exception for (Source: [[Source_X]]).
    
    # Check tag collision if index is passed
    if tags and index_path and index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
            
            entities_in_index = set()
            for node_id, node_data in index_data.get("nodes", {}).items():
                entities_in_index.add(node_data.get("title", "").lower())
                for alias in node_data.get("aliases", []):
                    entities_in_index.add(alias.lower())
                    
            for tag in tags:
                if str(tag).lower() in entities_in_index:
                    raise SchemaViolationException(f"Tag Collision: [{tag}] is already an entity and cannot be used as a tag. Use semantic links instead.")
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
