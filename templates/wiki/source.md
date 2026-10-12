# Canonical Source Page Skeleton

The filename is exactly `{{canonical_name}}`; exactly one such page is required for every successful ingest.
Angle-bracket values below must be grounded in the source or trusted dispatch metadata, never copied as facts.
Use the validator's current frontmatter vocabulary supplied with this task.

```markdown
---
id: <source-grounded stable id>
title: <source title>
aliases: []
type: source
domain: <allowed domain>
status: Active
epistemic-status: seed
categories: [<one allowed category>]
tags: []
sources: []
strategic_scope: <core, edge, or other>
evidence_tier: <include this field only when a permitted tier is justified; otherwise omit>
created: '<actual creation date>'
updated: '<actual update date>'
---

## 来源核验

- Raw 路径：`{{filepath}}`
- Raw MD5（宿主提供的 File Hash）：`{{file_hash}}`
- 以下内容仅由该 raw 文件确定性抽取；未补写 raw 中不存在的事实。

## 概要摘录

<原文摘要，保留作者主张与限定条件；区分事实、来源主张与推断。>

## 结构化摘录

<按主题分节摘录，必要时保留原文章节标题。>

## Graph Integration

<Only the validated host finalizer applies integration links; do not invent targets or version tokens.>
```

Keep the three declared content H2 sections; Graph Integration is the host-managed exception.
Host-supplied Static Skeleton H2 is explicitly allowed on this Source page for structured inputs:
when the brief supplies a `## 确定性结构 (Static Skeleton)` block, copy that complete block exactly
into this Source page, after 来源核验. This is an additional H2 exception, not an entity-page heading.
Do not add any other content H2. Put independently useful facts on the appropriate entity page instead.
The File Hash above is the raw-file MD5, not a canonical version or Markdown projection SHA-256.
Preserve all other supplied hash meanings; do not compute or reformat candidate version tokens.
