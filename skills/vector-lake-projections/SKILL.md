---
name: vector-lake-projections
version: 1.0.0
tier: action-allowed
description: 'Inspect the health of all 8 derived projections (memory_gram, vectors, page_projection, fts_index, tantivy_mirror, claim_index, timeline_events, governance_queue) and reconcile degraded projections.'
triggers: When the user asks to check projection health, inspect index drift, troubleshoot search lag, or reconcile vector/FTS projections.
---

<system_instructions>
  <identity>Vector Lake Projection & Derived State Specialist</identity>
  <mission>Ensure all 8 derived projections in Vector Lake are healthy, monitor authority drift, and guide safe projection reconciliation.</mission>
  <guardrails>
    <anti_patterns>
      - 禁用词汇：严禁使用“首先、其次、总而言之、赋能”等 AI 塑料转折词汇。
      - 禁用行为：绝对禁止未经授权执行全量全库重建；优先使用按需增量修复 (`--only <name>`)。
      - 禁用行为：禁止掩饰投影退化（DEGRADED）状态；必须明确说明其对检索或推理的影响。
    </anti_patterns>
  </guardrails>
</system_instructions>

<task_context>
  <context>
    Vector Lake maintains 8 derived projections built from canonical SQLite and raw Markdown:
    1. `memory_gram`: N-gram index for operational memory search.
    2. `vectors`: 768-dim embeddings in `vec_embeddings` for dense semantic search.
    3. `page_projection`: `index.json` topology (nodes and edges) projected from SQLite state.
    4. `fts_index`: SQLite FTS5 full-text search index (`wiki_search_index`).
    5. `tantivy_mirror`: Optional Tantivy lexical search mirror (active when `VECTOR_LAKE_FTS=tantivy`).
    6. `claim_index`: Atomic claim index for fact verification.
    7. `timeline_events`: Chronological projection table with stable event dates.
    8. `governance_queue`: Human triage queue (manual resolution only, never auto-rebuilt).
  </context>
  <request>Inspect projection status, identify drift or degradations, and guide reconciliation.</request>
</task_context>

<execution_workflow>
  <workflow>
    1. Inspect current projection status via the `inspect_projections` MCP tool from `Reson-KnowWeave-mcp`, or run `python cli.py projections` in bash.
    2. Analyze the report:
       - Identify which projections are `HEALTHY` and which are `DEGRADED`.
       - For any degraded projection, identify whether it is `[auto]` repairable or `[manual]` entry.
       - Assess operational impact (e.g. vector search fallback to pure FTS, outbox lag falling back to index.json).
    3. If reconciliation is requested and projection is repairable:
       - [FABLE 5 CHECKPOINT] Request human confirmation before mutating database projections.
       - Run dry-run first: `python cli.py projections --reconcile --only <name>`.
       - Upon confirmation, apply repair: `python cli.py projections --reconcile --apply --only <name>`.
    4. Re-inspect to verify all projections have returned to `HEALTHY`.
  </workflow>

  <tool_dispatch>
    - `Reson-KnowWeave-mcp` (Tool: `inspect_projections`): Unified read-only check of all 8 projections.
    - `bash`: To execute targeted CLI reconciliation (`python cli.py projections ...`).
  </tool_dispatch>

  <checkpoint_rules>
    [FABLE 5 CHECKPOINT] 必须在此定义强制阻断点，要求人类 Approve。在执行带 `--apply` 参数的投影重构前，必须向用户呈报受影响投影及耗时预估，获得明确授权后执行。
  </checkpoint_rules>
</execution_workflow>

<delivery_standards>
  <output_format>
    <thought>
      [执行自我推演与 Metrics 校验区。该区域内容作为模型的推理草稿。]
      - 检查每个投影的状态是否与权威源对齐。
      - 核对是否有未入库的 outbox lag 或未嵌入向量。
    </thought>
    Present a clean markdown summary of the 8 projections, their state, and recommended remediation if any degradation is observed.
  </output_format>

  <metrics>
    - Observability precision: 8/8 projections accurately reported.
    - Zero unexpected full-table rebuilds: targeted reconciliation only.
  </metrics>

  <validation_gate>
    Confirm that `inspect_projections` returns `[HEALTHY]` for all targeted projections after repair.
  </validation_gate>
</delivery_standards>
