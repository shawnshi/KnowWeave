{{instructions}}

[CURRENT-ENVIRONMENT SUBAGENT HANDOFF]
You are the host environment subagent completing this Vector Lake ingest task.
Do not use external model APIs from Vector Lake library code.
Return ONLY one JSON object with exactly two keys:
- files_written: array of filename/content objects (complete Markdown with YAML frontmatter)
- integration: explicit disposition with auditable reason or validated relations
Frontmatter rules the validator enforces (violating one refuses the whole ingest):
- categories: a YAML list with EXACTLY one element, one of the supplied runtime contract's category values (not domain values) (e.g. categories: ["Healthcare_IT"]); never a bare string, never two elements
- strategic_scope: exactly `core`, `edge`, or `other`; aliases: a list; epistemic-status is from the supplied runtime contract's epistemic-status vocabulary; and id/title/type/domain/status/updated/sources present -- the field list the schema gate itself requires. `topic_cluster` is optional (the gate defaults it to `General`), and `ttl`, `memory_type` and `memory_key` are not fields the gate requires of a wiki page
- tags: at most {{max_tags}}, and none may equal an existing node's `title` or any of its `aliases` (compared lowercased). The gate calls that `Tag Collision` and refuses the whole ingest, so do not tag a term that already has a page -- including under an alias rather than the page's own title: `电子病历评级` and `EMR评级` are both refused because `Policy_电子病历系统功能应用水平分级评价` declares them as aliases. Express the relation as a typed link instead, e.g. `[related_to:: [[Standard_电子病历评级]]]` in Section 1.
- every typed link must be [predicate:: [[Target]]] with a predicate from this closed vocabulary: {{valid_predicates}}
- an integration relation's `predicate` is NOT drawn from that list: it binds a document to an existing page, so only this narrower set is accepted -- {{integration_predicates}}. `has_part` and the other structural predicates are refused here
- every relation `target` must be copied verbatim from the packet's integration_candidates manifest, `.md` suffix included; a rephrased or suffix-less name is refused
- a bullet under `## 2. 证据时间线` must be `- [YYYY-MM-DD] [Event_Tag] <event>` with a tag from exactly: {{timeline_tags}}; an undated item (an open question, a 未决点) does not belong in that list at all, and no date may be invented to satisfy the shape
Do not echo or alter processed_data; the host retains the task packet's lease and source_hash.
Integrated relations must use candidate canonical target_hash values; standalone requires an auditable reason; content-based rejection is not allowed.
Each integrated relation is a complete checked record: target (verbatim from the manifest,
`.md` included), target_hash + target_projection_hash (copied from that entry, not computed),
predicate (from the narrower set above), evidence (>= 12 chars), confidence (a number in
[0,1]), event_date (YYYY-MM-DD), event_tag (one bare tag, brackets omitted, from {{integration_tags}}). One bad field
refuses the whole ingest.
Do not call finalize_ingest: the host validates and submits the returned object.
