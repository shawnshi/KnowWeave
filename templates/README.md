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
| `topology.html` | Existing graph HTML template; retains its own escaping and substitution | `tool_graph` |

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
