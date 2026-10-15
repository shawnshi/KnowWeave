# Vector Lake Schema & Governance (Schema V8.0)

## 1. Core Mandate

This file documents the data contract and authoring conventions, not a second executable rule owner.
`vector_lake/schema_validator.py` owns enforced structural rules and closed vocabularies;
`vector_lake/ingest_schema_contract.py` reflects those rules into every ingest backend.
`SCHEMA_CATEGORIES.md` explains classification; `MEMORY/purpose.md` YAML owns user strategy,
evidence-tier definitions and synthesis thresholds. Its current version is supplied at runtime,
not pinned to a minor release in this document. Page skeletons and generator instructions live
in `templates/wiki/` and `templates/prompts/`; examples cannot expand the write gate's vocabulary.

Bounded Vector Lake page candidates are generated or validated against this contract. Candidates have no direct write authority: the host must commit them through the Mutation Coordinator, which atomically records canonical state and durable outbox intent before publishing Markdown and index projections. The target knowledge base is optimized for Medical IT industry intelligence, topology mapping, and compliance tracking.

## 2. File System Architecture
- **`MEMORY/raw/`**: Source documents. READ ONLY except for cascade deletes.
- **`MEMORY/wiki/.meta/vector_lake.db`**: Transactional canonical store for entities, claims, evidence, sources, graph state, change sets and durable outbox intent. Do not edit it outside governed runtime entrypoints.
- **`MEMORY/wiki/`**: Human-audit Markdown projection. Do not hand-edit derived pages unless an explicitly authorized legacy repair path requires it.
- **`MEMORY/wiki/index.json` / `MEMORY/wiki/claim_topology.json`**: Recoverable projections written from committed canonical state after the transaction commits.  `index.json` carries the page nodes (summary only, no body text) and the weighted edges; `claim_topology.json` carries the claim graph built from `claim_graph_edges`.  The read path consumes their SQLite projection (`page_index_*` + FTS5) and falls back to `index.json` when the projection is behind.  Do not hand-edit either.
- **SQLite `operational_memory` table in `MEMORY/wiki/.meta/vector_lake.db`**: Machine-facing Agent read model compiled from canonical claims. It resides in the canonical database for transactional consistency but is not an independent source of truth and must not be hand-edited.
- **`MEMORY/wiki/log.md`**: The append-only chronological log. Append an entry here for every ingest, query-to-page, or lint operation. Format: `## [YYYY-MM-DD HH:MM] <Action> | <Target>`
- **`MEMORY/wiki/overview.md`**: Human-readable bird's-eye summary of ALL wiki topics. Updated after each ingest batch.
- **`MEMORY/purpose.md`**: The versioned strategic-purpose control plane. Its YAML contract drives ingestion directives, retrieval context, autonomous research, operational-memory weighting, SIR review proposals, and synthesis thresholds.

## 3. Markdown Conventions
- **YAML Frontmatter**: Wiki pages must carry frontmatter; exact required fields and artifact exemptions come from the runtime validator contract.
  - `required_frontmatter`, field vocabularies and generated-artifact exemptions are reflected from the validator, not copied into a second YAML example here.
  - `aliases` must be a list when present; `tags` obey the code-owned size/collision rules. `topic_cluster` and `ttl` are optional. `created` is an authoring convention, not an additional required-field gate.
  - `categories` and `domain` are different axes. Use the category values and registered domains/verticals/aliases in the runtime contract; the explanatory catalogue is `SCHEMA_CATEGORIES.md`.
  - `strategic_scope` is `core`, `edge`, or `other` for knowledge nodes. Scope classifies content and may guide retrieval preferences; it does not exclude eligible input. Declare `evidence_tier` only when supported by the source and the current purpose YAML definitions supplied with the task. Otherwise omit the tier and state the evidence limitation; do not invent evidence or reject the source.
  - A provided evidence grade is validated on full-purpose writes; absence alone does not prevent ingestion. Operational memory uses `memory_type`/`sources` for provenance and must not fabricate `derived`. Recovery retains a recorded grade and does not default missing metadata to `primary`. Explicit unsupported grades remain errors, including on existing memory pages.
  - `memory_type` and `memory_key` identify operational-memory records, not mandatory metadata for ordinary knowledge pages; the compiled memory page may mirror its type.
  - `architecture_patterns` is an authoring convention only: no code consumes it.
  - `tension_edges` records target, polarity, intensity and context. User-purpose thresholds decide synthesis proposals; their numeric values are not duplicated here.

Semantic Bidirectional Linking (SSOT Rule): ALL topological relations MUST be 100% and uniquely carried by Markdown semantic links. DO NOT use YAML arrays (like parents) for relationships. You MUST use strict relation-typed links from the Controlled Vocabulary: [predicate:: [[Target_Entity_Name]]].
Ontological & Creation: [is-a::], [part-of::], [evolved-from::], [created::], [founded::], [authored::], [architected::]
Strategic & Power: [competes-with::], [supplies-to::], [supplied-by::], [blocks::], [conflicts-with::], [controls::], [manages::], [invested-in::], [allied-with::]
Integration & Deployment (Med IT Spec): [integrates-with::], [runs-on::], [deployed-at::], [complies-with::], [certified-by::]
Epistemic: [validates::], [falsifies::], [depends-on::]
Instantiation: [instantiated-by::]
Inline Provenance & Controlled Metrics (RAG Chunking Friendly):
When summarizing or extracting claims, use inline source anchors rather than bottom footnotes. Example: The model achieves SOTA performance (Source: [[Source_Report_A]]).
Controlled Metrics System: To enable exact cross-entity graph computations, metric assertions MUST use the syntax: `- {Metric: [Controlled_Key]} [[Entity_Name]] [Value] (Source: [[Source_X]])`. A metric without an inline `Source_*` anchor is invalid.

Core keys: `MedIT_Revenue` (CNY), `Market_Share` (ratio), `EMR_Level` (0-8), `CHI_Level`, `SLA` (ratio). `Bids_Won` is a legacy ambiguous key; use `Bid_Count` (count) or `Bid_Value_CNY` (CNY) for new facts.

Medical IT evidence keys: `FHIR_OMOP_Center_Count` (count), `IT_Budget_Change_Pct` (percent), `Public_Cloud_Deployment_Ratio` (ratio), `Acceptance_Case_Count` (count), `Engineering_Test_RPS` (requests/s), `Engineering_Test_P99_MS` (ms), `Engineering_Test_Error_Rate_Pct` (percent), `GPU_Infrastructure_Cost_CNY` (CNY), `API_Access_Fee_CNY` (CNY), `Implementation_Duration_Days` (days), `Implementation_Cost_CNY` (CNY), `Project_Cancellation_Rate_Pct` (percent), `SaaS_Value_Share_Pct` (percent).

Example: `- {Metric: EMR_Level} [[Institution_协和医院]] 7 (Source: [[Source_2025评级公告]])`.
Semantic Tension Quantification Model (STQM): When synthesizing documents that contradict or explicitly support existing knowledge, generate a tension_edges array in the YAML frontmatter. (Polarity: -1.0 to 1.0, Intensity: 0.0 to 1.0, Context: 1-sentence reason).
Contradictions & Synthesis: If new information contradicts existing wiki content, DO NOT just overwrite it silently. Explicitly document the contradiction in the text AND map the physical collision using tension_edges (or embed inline: [falsifies:: [[Target]] {intensity: 0.85}]).
Temporal Rot Defense: Anchor claims to a specific time frame using inline brackets at the start of a bullet/paragraph (e.g., [2024] The market is...).
Epistemic Decay (TTL): Actively assign a shorter ttl for time-sensitive nodes. The indexer reads an explicit `ttl` from the node and honours it only when it is a valid positive number; otherwise it falls back to `DEFAULT_TTL`, keyed by `type` (`source` 365, `synthesis` 730, `concept` 1825, and 1095 for `vendor` / `product` / `person` / `event` / `policy` / `standard`). No code derives a ttl from `epistemic-status`, and the `seed` / `sprouting` / `evergreen` numbers this line used to name are implemented nowhere. Decay is `0.5 ** (age_days / ttl)` against `updated`. `DEFAULT_TTL` has two copies (`indexer.py` and `tool_lint.py`) that must be kept in step.
Operational Memory Split: Agent runtime state is stored in SQLite `operational_memory`. Use the governed memory-update tool to register durable state; do not edit derived rows directly.
File Naming Policy & Ontology Lock:
Strict Naming Protocol ([ControlledType]_[MainName]-[SubName].md).
Allowed prefixes: Concept_, Vendor_ (Supply Side ONLY), Institution_ (Demand/Regulator ONLY), Product_, Person_, Event_, Policy_, Standard_, Source_, Synthesis_. Legacy Entity_ is forbidden.
Absolute Physical Character Set: Alphanumeric, Chinese, hyphens -, and exactly one underscore _. No spaces allowed. Total filename limit: 120 chars.

## 4. wikiFormat Contract (Dual-Schema)
To prevent history noise and AST parsing failures during RAG/search, wiki files are strictly divided into two layout types based on their `type`.

A. Entity & Concept Files (Dual-Schema Mandate)
Target Files: Concept_*.md, Vendor_*.md, Institution_*.md, Product_*.md, Person_*.md, Event_*.md, Policy_*.md, Standard_*.md.
Design Pattern: CQRS (Command Query Responsibility Segregation) & Event Sourcing.
Format Constraint: These files MUST adhere to the "Compiled Truth | Timeline" physical structure.

The rendered body skeleton has one owner: `templates/wiki/entity.md`.
Normative constraints (not a second page template):
- Two H2 sections distinguish current compiled facts from dated evidence. Entity pages do not receive the Source-only Static Skeleton section.
- Compiled facts describe the current source-grounded consensus: concise definition, no marketing or historical narrative. Each bullet restates the entity name, not a pronoun, and carries an inline Source anchor.
- Numeric assertions use the controlled metric syntax below; provenance cannot be deferred to bottom footnotes.
- H3 slots are closed per type: use `h3_slots` from the runtime validator contract. A declared `tension_edges` requires `### 认知张力与未决争议 (Controversies & Tensions)` in section 1.
- A reshape date is plain text, never a Wiki link. The generator may replace compiled facts, but does not rewrite existing timeline entries.
- Timeline bullets begin `- [YYYY-MM-DD] [Event_Tag]`, use the validator's event-tag vocabulary, and end with a source anchor. Never invent a date for an undated question.
- Evidence timelines are governed knowledge projections, not business Event Stores. Entries normally append; a correction or supersession requires a separately authorized, auditable maintenance operation. This does not grant the generator permission to edit/delete old entries.

B. Exempted & Synthesis Files (Semi-Structured)
Target Files: Source_*.md, Synthesis_*.md.
Constraint: DO NOT apply the Dual-Schema timeline format to these files.

For Source_*.md: the recommended skeleton has one owner, `templates/wiki/source.md`.
Existing free-form summaries remain readable; the three-section recommendation is not a new historical heading gate.
For supported structured inputs, the host supplies a Static Skeleton block that is copied exactly to the Source page as an explicit additional H2 exception. Graph Integration is host-managed. Neither exception adds an Entity H2.

For Synthesis_*.md: MUST instantiate a lightweight semantic skeleton. The document MUST contain:
## 核心合成论点 (Core Synthesized Claims) (No-Pronoun Constraint enforced).
## 支撑拓扑 (Supporting Topology) (Listing critical [predicate:: [[Target]]] vectors driving the synthesis).
Free-form markdown analysis follows. What `schema_validator.validate_schema` enforces is that these
two sections are *present*. Opening the document with them is the recommended shape, not an enforced
one: enforcing the order would reject legacy synthesis pages that carry the skeleton last, so
`lint` reports the position instead (`synthesis_skeleton_order_report`) and the difference between the
documented rule and the enforced rule stays visible. Current corpus compliance must be measured
with lint, not inferred from a historical page count.

C. Generated Artifacts (Not Authored)
Target Files: `System_Community_*.md` (the clustering daemon's community indexes).
Constraint: these are pages the wiki writes **about itself**, not knowledge nodes. They are exempt
from `domain`, `epistemic-status` and `sources`, carry `categories: [System]`, and every other
layer (indexer, link resolution, governance extraction) skips them.

The namespace is not a knowledge namespace. Knowledge nodes misfiled under `System_` can be skipped
by indexing and linking and evade knowledge-node metadata requirements. The authoring gate therefore
refuses a **new** node whose filename starts with `System_` unless it is an artifact; existing ones
require a separately authorized rename pass. Use a knowledge prefix (`Concept_`, `Source_`, `Vendor_`, ...)
instead.

## 5. Workflows
(Standard workflows for Ingestion, Query-to-Page, and Linting remain intact. Trigger MCP tools enqueue_governance_item for conflicts, and resolve_governance_item for node merges.)

6. Entity Linking Contract (图谱硬连接规范)
All topological relations MUST be explicitly declared using semantic brackets [predicate:: [[Entity_Name]]]. Natural language surrounding verbs are NOT parsed by the AST engine.
Predicate-Slot Alignment: Semantic links must be placed within their business-intent H3 slot. For example:
[controls::], [manages::] → Person's 核心权责与控制域
[supplied-by::] → Institution's 核心供应商与生态锚定
[complies-with::], [certified-by::] → Product's 医疗合规与资质壁垒
[integrates-with::], [runs-on::] → Product's 部署架构与底层依赖
[deployed-at::] → Vendor or Product slots mapping to implementation
Merge Constraint: ABSOLUTELY NO manual file merging. Use resolve_governance_item.

7. Metadata Decay and Taxonomy Tyranny (防腐与分类学暴政)
Metadata Decay Mechanism: Handled by AST daemon TTL expiration, which recomputes the decay weight from `updated` and the node's ttl (§7). No marker text bypasses RAG context: the ``[⏳ 过期警告]`` label this line used to name appears nowhere in the code, so nothing sets it and nothing reads it.
Taxonomy Tyranny:
Rule 1: NEVER use an existing entity name as a tag.
Rule 2: Tags are exclusively reserved for marking cross-entity macro strategic states (e.g., #亏损暴雷, #院内系统替换). Use architecture_patterns in YAML for technical jargon (an authoring convention: nothing in the code reads this key).
Rule 3: An entity's tag count must not exceed the runtime contract's `max_tags`, reflected from `schema_validator.MAX_TAGS`.
***
*(Schema V8.0. User strategic-policy versions and evidence tiers come from purpose YAML. Controlled metrics are unit-specific, metric claims require Source anchors, and configured thresholds create auditable Synthesis-Proposals.)*
