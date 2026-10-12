# Entity Page Skeleton

Use this shape for concept, vendor, institution, product, person, event, policy and standard pages.
Angle-bracket values below are instructions to fill from evidence, never default facts.
Use only the current validator's fields and vocabularies supplied with this task.

```markdown
---
id: <source-grounded stable id>
title: <entity name>
aliases: []
type: <entity type>
domain: <allowed domain>
status: Active
epistemic-status: seed
categories: [<one allowed category>]
tags: []
sources: [<source provenance>]
strategic_scope: <core, edge, or other>
evidence_tier: <include this field only when a permitted tier is justified; otherwise omit>
created: '<actual creation date>'
updated: '<actual update date>'
---

# <entity name>

## 1. 编译事实 (Compiled Truth - READ MODEL)

*[System Directive: This section represents the LATEST consensus. NO historical narrative here. NO marketing fluff.]*

<Concise definition grounded in the source.> (Last Reshaped: <actual date>)

### <one H3 slot allowed for this type by the supplied schema>

- <entity name and fact, no pronouns> (Source: [[Source_X]], <actual locator when available>)

## 2. 证据时间线 (Timeline - EVENT STORE)

- [<actual event date YYYY-MM-DD>] [<allowed event tag>] <event and entity name> [validates:: [[Target_Entity]]] (Source: [[Source_X]])
```

Section 1 is overwritten in place; section 2 is append-only. NEVER edit, summarize, or delete old timeline entries.
Every factual line requires an inline source anchor. H3 slots are closed per type; do not invent them.
The host-supplied Static Skeleton belongs on the canonical Source page, not an additional entity H2.
Write the reshape date as text, not a Wiki link. Do not invent an event date to satisfy the format.
If tension_edges are declared, include `### 认知张力与未决争议 (Controversies & Tensions)` in section 1.
Use exactly one allowed category and a registered domain; new unclassified pages are refused.
