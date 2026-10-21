# Relation read model

## Meaning and compatibility

`build_relation_catalog(nodes, weighted_edges=None, *, domain="", cluster="", include_history=False, max_relations=50000, context=None)` is pure. The optional context mapping is keyed by `(source, target, predicate)`; existing positional arguments and result keys are unchanged. `get_relations(entity_name, domain, cluster="", depth=1, limit=200, include_relatedness=False)` retains its frozen interface.

- `declared` is a document's typed declaration, not a factual entity assertion. `subject_scope` remains `document`; neither nearby prose, subject IDs nor a page title establish a business subject.
- `navigation` is a wikilink, not a factual predicate. `relatedness` is an undirected ranking projection, not support or truth. Ranking weights and canonical tables are unchanged.
- Direction is preserved. `predicate_semantics` contains controlled-vocabulary, direction, inverse-predicate and endpoint-type diagnostics. An inverse name is a hint, never an emitted reverse assertion. Unknown predicates, wrong direction and document taxonomy declarations remain visible without rewriting or deleting old assertions. Type compatibility is advisory, never verification; unsupported type rules remain `unknown`.
- `verified` stays false and `epistemic_status` stays `unverified`. Record lifecycle (the page's existing status), epistemic state and product release lifecycle must not be conflated. This model does not infer product release state from a document status or from being seeded.

## Scoped context and failure semantics

Only the read-only SQLite reader enriches already-scoped nodes. Global identity-only fields remain necessary to reject aliases contested by hidden pages. Hidden payloads are not hydrated. Traversal and result caps select declaration sources before context queries, so unreachable/capped pages and depth-zero results do not hydrate claim/evidence payloads. Claim queries require the existing generated page locator column and an exact visible page key; evidence queries also require an exact evidence ID on that same page. No raw files, canonical entity payloads, source records or unrelated pages are opened.

Context requires a unique existing claim with an exact explicit `[predicate:: [[target]]]` token and a positive existing block locator on the declaration's page. Resolution uses the same ambiguity rules as the catalog. This binds a document declaration, not its factual subject. Existing extraction normally removes typed syntax from claim text; those claims cannot safely be reverse-engineered into predicate bindings and therefore remain `binding_unknown`. No backfill or extraction change is implied.

`context.status` is `matched`, `binding_unknown`, `ambiguous` or `schema_unavailable`. Missing schema or binding is not zero evidence. `evidence_count` is null unless every explicitly listed pointer resolves on the same block and points back to that claim; an explicit empty list in an available schema is a genuine zero. Multiple linked evidence records are permitted; multiple matching claims are ambiguous. `provenance_status=linked_unverified` describes pointers only, never source admission, independent support or verified truth. Missing, foreign-block and dangling pointers remain unknown.

Only page key and numeric block index are exposed as locators. Claim/evidence text, headings, IDs, subject names and source references are withheld. Pure-builder context is allow-listed too. Explicit ISO `valid_from`, `valid_to` and `temporal_anchor` fields may be returned, retaining existing year/month/quarter/half-year anchor precision without synthesizing dates; `updated_at`, creation/compilation/ingestion time, headings and prose are never event time. Missing time stays `temporal.status=unknown`.

Missing projections, stale projections, ambiguous identity, valid empty results and read errors retain distinct statuses. Missing optional claim/evidence schema does not turn declarations into errors or invent evidence. Malformed accessed context is a read error, not a fabricated empty result. Both endpoints must satisfy exact domain/cluster/history scope before any relation/context is emitted; hidden nodes cannot bridge traversal.

## Activation and recovery

These source changes have only synthetic validation. They are not production activation. Integrate only lane deltas from the sealed batch2 seed after component host-gate hash verification; then run the composite gate and independent final review. No service reload, rebuild, source admission, raw-binding change or canonical migration is performed by this component.

A production activation, if separately authorized, must load the reviewed code in a fresh host and prove scoped behavior. Existing unbound claims remain unknown without migration; do not rebuild or admit sources to make context appear verified. Recovery restores only these lane deltas after checking integration drift against the sealed seed; preserve unrelated dirty work. No database rollback or format conversion is necessary because this component never writes storage.

Byte/CAS versions remain the authority for canonical compare-and-swap and outbox compatibility. A logical-content diagnostic fingerprint (provided separately by the runtime lane, if integrated) may ignore approved representation differences such as line endings. It is diagnostic only: equality cannot authorize a write or replace byte/CAS versions; unavailable fingerprints remain unavailable. This component changes neither canonical version computation nor raw bindings.

## Verification

`tests/test_relation_semantics.py` covers actual scoped SQLite context, absent/multiple evidence, ambiguous binding, inverse/type/wrong-direction hints, temporal unknown, subject-inference refusal, redaction and source/database protection. Existing catalog/adapter/ranking regressions protect the frozen API, scoping, direction and weights. All test MEMORY roots must be synthetic under worktree scratch and provider credentials removed before imports. Browser validation is outside this lane; no browser retry is authorized.
