# Vector Lake templates

Runtime Wiki skeletons and model instructions have one owner under this directory.
`vector_lake/template_loader.py` provides UTF-8 loading and single-pass `{{identifier}}` substitution.
No external template engine, discovery registry, recursive includes, or embedded-code fallback is used.

## Ownership

| Directory/file | Responsibility | Callers |
| --- | --- | --- |
| `wiki/entity.md`, `wiki/source.md` | Model-facing page skeletons injected into the ingest brief | `tool_ingest` |
| `wiki/stub.md` | Broken-link Stub body | `stub_creator` |
| `wiki/operational_memory*.md` | Memory page, legacy mechanism fragment and timeline entry | `tool_memory` |
| `wiki/restored*.md` | Canonical-to-Markdown recovery bodies | `tool_projection` |
| `wiki/community*.md`, node-link fragments | Community page and its optional pending/delta blocks | `community_clustering_daemon` |
| `wiki/domain_overview.md`, `wiki/domain_overview/` | Overview page and repeatable/optional sections | `compile_domain_overviews` |
| `wiki/static_skeleton/` | Deterministic Python/JSON/YAML parsing presentation | `skeleton_parser` |
| `prompts/ingest/` | Main brief, handoff, output contract, CLI, relay and repair instructions | Ingest workers/adapters |
| `prompts/query/` | Query synthesis and comparison hint | `tool_query` |
| `prompts/strategy*.md` | Strategic-purpose instructions with supplied contract data | `purpose_contract` |
| `prompts/research*.md`, `prompts/review/`, `prompts/janitor*.md` | Research and governance instructions | Matching tools/scripts |
| `topology.html` | Single-file knowledge map: offline-first UI, pure graph helpers and embedded application; retains legacy placeholder compatibility | `tool_graph` |

Optional or repeated page blocks are separate templates where necessary; code still selects blocks,
iterates records, formats numbers, and serializes YAML/JSON. Parser selectors and API messages are not
page templates. `.pi/agents/` and `skills/` remain framework discovery entries, not relocated assets.

## Rendering contract

- Template names are relative to the repository-owned `templates/` root, never provided by a source or model.
- Missing files, unknown placeholder expressions, missing variables, and unused variables raise errors.
- Inserted data is opaque: `{{...}}` inside a source or candidate is not rendered again.
- Closing `}}` can be ordinary nested JSON. It is not interpreted on its own as a placeholder.
- No cache: a changed template is seen on the next construction of the relevant brief/page.
- Wiki skeletons are supplied by the host; the model is not asked to read template paths.
  Structured inputs carry the exact host-supplied Static Skeleton H2 on the Source page as an explicit
  additional-heading exception; entity pages keep their closed two-H2 structure.
- Angle-bracket examples in entity/source skeletons are instructions, not default facts.
- `schema_validator` owns closed vocabularies; `ingest_schema_contract.schema_snapshot()` reflects fields, categories,
  domains/verticals/aliases, H3 slots, predicates, event tags and metric keys for all ingest backends.
  The main brief, handoff, output contract and relay no longer keep independent vocabulary renderers.
- Purpose YAML stays in the user's MEMORY root. The validated strategy renderer supplies its current version,
  evidence-tier definitions and concrete synthesis thresholds; Markdown body prose is not executable policy.
- Operational memory does not invent a business evidence grade. Recovery preserves a recorded grade without
  filling absent metadata with `primary`. Explicit invalid existing grades are not erased or remapped;
  full-purpose validation rejects supplied empty/whitespace-only grades while preserving absent-field compatibility.
  Schema checks, result schemas, dispatch/version gates, and mutation coordination remain executable code.
- Python renderers and their template variables form one deployment unit. After paired edits, existing resident
  services must reload both through their authorized lifecycle before use; fresh CLI tests do not prove that an
  already-running MCP server or Runner has loaded them. Old cached modules can fail closed on new variables.
- The raw File Hash remains MD5. Canonical version and Markdown projection fingerprints keep their existing meanings.
- `legacy_packet_contract.md` preserves the pre-existing old-packet compatibility branch. It is **not** a fallback
  for a missing template or invalid current packet; template-loading failures still propagate.
- Moving an instruction does not grant permission to search, write, merge, or sync. Runtime authorization
  and validation contracts still govern every action; generating a janitor shard does not execute a merge.

## KnowWeave · 知识经纬

The header inlines the original `knowledge-graph.svg` from the project root, inheriting `currentColor` without an external image request. Keep the template copy synchronized when updating that asset; rendering tests compare both SVG trees. The project README uses `knowweave-app.svg`.

`tool_graph._render_graph_html(payload, memory_dir)` only reads this template and renders HTML. It does not bootstrap canonical state, refresh projections, launch a browser, or read declared source documents. The public visualization tool retains its existing preparation and output contract. `%%GRAPH_DATA%%` and the quoted `%%MEMORY_BASE_PATH%%` remain compatible with a resident pre-change renderer; no new companion runtime asset or Watchdog/Runner restart is required. The fresh renderer substitutes both markers in one pass and JSON-escapes data and the file URI. A resident old module retains its old escaping implementation until its normal reload.

The map explicitly presents a **static snapshot**. It starts with a domain overview, supports type/community grouping, searchable and paginated classification, intersecting filters, one/two-hop exploration, and a full matching-node list. Only drawing is capped at 100/200/400 nodes; overview aggregates at most 12 groups plus an explicitly named remainder. The left sidebar separates classification from filters and pages categories eight at a time. The right inspector has Overview/Sources/Relations/Actions sections, with five source/neighbor entries per page and a 180-character expandable/collapsible summary preview. No list grows cumulatively when paging. Coordinates are a reading layout, not historical or causal evidence. Relation strengths are the existing weight categories, not newly inferred support relations. Empty type selection means no results. Missing confidence remains unknown; the legacy constant alignment score is not presented as a measurement. Claim references resolve declared sources rather than inventing a Markdown file for every claim.

The compact Overview section labels record `status` and `epistemic_status` independently. Products additionally show explicit `lifecycle_status`; absent values display `unknown`. Record `seed` is not a lifecycle and never implies `unreleased`. Claim `validity_state` retains its own label and is not replaced by either record or epistemic state. Page projection passes through explicit top-level or metadata epistemic/lifecycle fields without deriving them from prose or timestamps.

Page payloads retain `declared_links` and directed `declared_relations` separately from the budgeted relevance backbone. The UI merges declarations into browsable adjacency without fabricating a relevance score or treating a declaration as verified evidence. The indexer's 15-edge pruning and the exported backbone budget remain unchanged. Older payloads can recover missing pairs from `semantic_links`, but existing backbone pairs are not relabeled as declarations because that legacy field mixed both. Relation details prefer the existing additive catalog `relations`, retaining navigation-vs-assertion labels, direction/predicate, null declaration weight and the literal `verified` value (including `false`). Optional `subject_scope`, `resolution_status`, `context`, `origin`, `provenance_status`, `temporal`, `epistemic_status` and `lifecycle_status` are shown as recorded text/JSON within the existing paged neighbor entries. No evidence pointer, time field or provenance status establishes verification; absent verification stays `unknown`. Catalog relatedness entries do not bypass the backbone drawing budget. Legacy node-level declarations remain a fallback without overwriting catalog context. Declaration details remain separate from numeric retrieval weights; self-links, unresolved/ambiguous targets and malformed records cannot mint nodes. An optional `meta.scoped_refresh` explicitly labels a targeted refresh while retaining the original global snapshot time. `read_consistency=scoped` is disclosed as a mixed-time snapshot, not an unlocked read or an all-library refresh; unrelated cached records and the claim graph can remain untouched.

The canvas uses a light `#f7f9fc` surface with dark nodes, text and links, matching the reading panels. Default label candidates are limited to four aggregate labels or three local labels, with selected/hovered nodes always eligible; collision suppression still applies. Key/on-demand/more label settings affect presentation only, not filtering or data. 3D uses the same light background and hover labels; the 2D density selector is disabled there.

Fonts are local-only: Segoe UI Variable/Segoe UI with Microsoft YaHei UI/PingFang SC for body text, Segoe UI Variable Display with DengXian/PingFang SC fallback for headings, and Cascadia Mono/Consolas for numbers. No font download or OS installation occurs. Canvas uses the computed font family, not an unresolved CSS variable. Responsive layouts, focus indicators, named search controls, equivalent node lists and reduced-motion handling are part of the UI contract; source assertions and color-token tests do not substitute for actual rendered accessibility acceptance.

No script or font is requested externally on initial load. Optional 3D requires an explicit network confirmation and uses `3d-force-graph@1.79.0` (MIT) from unpkg, with SHA-384 `I8xUqUlkgn1YFxE2iHhBohLpcR9rKT9MSaKUFK/+opyNDd5UX5j98GymiICW9V76`. This pins the existing optional dependency, does not install an npm dependency, and is not a complete transitive vulnerability audit. Third-party code runs in the page and can access its data; the dialog discloses that boundary. CSP restricts connections and font downloads but is not claimed as full data isolation. Load/WebGL failure retains 2D. 3D receives exactly the current projected nodes/edges and supports actual pause/resume. The default 2D map has no continuous physics loop or unused temporal slider.

Copy actions only copy text; they do not execute shell commands or submit queue entries. Local links reject schemes, absolute paths and traversal, encode path segments, and identify source locations as declared rather than verified. The template never injects untrusted labels or summaries as HTML.

Verification:

```text
python -m pytest tests/test_tool_graph.py tests/test_graph_algo_contract.py tests/test_tool_graph_import_boundary.py tests/test_topology_render.py -q
node --test tests/topology_ui.test.cjs
```

The Node tests execute the actual inline pure functions and application transitions with a synthetic DOM facade. They are not browser rendering, WebGL, clipboard permission or assistive-technology tests. Rendered acceptance should cover 390/768/1440/1920 widths, 200% zoom, keyboard/search, pointer cancellation, blocked clipboard, empty/malformed data, stale metadata, graph switching, source links, and optional-engine failure. Benchmark output is labeled synthetic core timing, not browser latency.

## Migration and verification

Old internal paths `Concept.md`, `Source.md`, `ingest_prompt.md`, and `query_prompt.md` were replaced by
`wiki/entity.md`, `wiki/source.md`, `prompts/ingest/main.md`, and `prompts/query/main.md` respectively.
Current callers, schema references and path-dependent tests use the new paths; no duplicate legacy copy remains.
The former reference skeletons mixed historical corpus counts and fixed sample IDs/dates with the shape.
They now present source-grounded placeholders; past census values are not generated facts or runtime defaults.
No historical Wiki rewrite or stricter historical Source heading gate was introduced.

`tests/test_template_loader.py` checks loading, strict rendering and skeleton injection.
`tests/test_template_outputs.py` compares 36 synthetic pre-migration outputs with frozen SHA-256 fixtures:
most comparisons are byte-exact; handoff whitespace is normalized, and memory frontmatter is compared as
parsed YAML because host serialization preserves the timestamp type while changing its presentation.
Contract-alignment changes intentionally revise the four memory-grade fixtures, handoff vocabulary wording,
and strategy/relay fixtures (seven changed cases; the other 29 remain frozen);
`tests/test_contract_alignment.py` separately verifies their exact new semantics and unchanged refusal gates.
The golden fixtures contain no real vault content. Keep unrelated user changes when updating them.
