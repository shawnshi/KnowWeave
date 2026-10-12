OUTPUT CONTRACT -- validated field by field; ONE violation rejects the whole ingest.

{
  "files_written": [ {"filename": "<basename.md>", "content": "<complete Markdown incl. YAML frontmatter>"} ],
  "integration": {
    "disposition": "integrated" | "standalone",
    "reason": "<required for standalone>",
    "relations": [
      {
        "target": "<copy VERBATIM from integration_candidates[].target -- .md included>",
        "target_hash": "<copy VERBATIM from that same candidate entry>",
        "target_projection_hash": "<copy VERBATIM from that same candidate entry>",
        "predicate": "<one of: {{predicates}}>",
        "evidence": "<at least 12 characters of prose>",
        "confidence": 0.0,
        "event_date": "YYYY-MM-DD",
        "event_tag": "<one of: {{tags}}>"
      }
    ]
  }
}

Checked, and NOT guessable from the page-link vocabulary in the schema above:
1. `predicate` is not the page-link vocabulary. Only the list shown above is accepted; a
   structural predicate (`is-a`, `part-of`, `has_part`, `instance_of`, `created`) is refused.
   When the honest reading is structural, use `related_to`.
2. `event_tag` is ONE bare word from the list above -- no brackets, no invented value. Both
   `[Release]` and `paper-analysis` are refused.
3. `confidence` is a JSON NUMBER in [0, 1]. Never a word, never a quoted string.
4. `event_date` is exactly `YYYY-MM-DD`: the date of the event the relation records.
5. `target`, `target_hash` and `target_projection_hash` are copied, never computed or reformatted.
6. `relations` is required and non-empty whenever `disposition` is `integrated`.
7. Every bullet under `## 2. 证据时间线` reads `- [YYYY-MM-DD] [Event_Tag] <event>`. An undated
   item -- an open question, a pending decision, a 未决点 -- does not belong in that list at all.
