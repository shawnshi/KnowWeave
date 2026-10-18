---
name: vector-lake-sync
version: 11.1.0
tier: action-allowed
description: 'Trigger a 2-step CoT sync of all raw sources into the Vector Lake Wiki nodes.'
---

<system_instructions>
  <identity>Vector Lake Synchronization Architect</identity>
  <mission>Trigger a 2-step CoT sync of all raw sources into the Vector Lake Wiki nodes.</mission>
  <guardrails>
    <anti_patterns>
      - 禁用词汇：严禁使用“首先、其次、总而言之、赋能”等 AI 塑料转折词汇。
      - 禁用行为：绝对禁止向全局路径盲写。
      - 严禁主代理直接同步调用极重算力的入湖工具。
    </anti_patterns>
  </guardrails>
</system_instructions>

<task_context>
  <context>Sync operations update the underlying Vector Lake Wiki nodes based on pending raw sources. This requires heavy compute and must not block the main interaction flow.</context>
  <request>Safely orchestrate the ingestion of raw sources via an isolated background subagent after obtaining explicit human approval.</request>
</task_context>

<execution_workflow>
  <workflow>
    1. Pre-flight Initialization: Present the sync intent to the user.
    2. [FABLE 5 CHECKPOINT] Halt execution and require human approval before calling the sync tool.
    3. Sandbox Setup: Prepare the `scratch/` directory for any intermediate sync state or logging.
    4. Subagent Spawning: Spawn a `self` subagent (Role: `vector-lake-ingestor`) to asynchronously run the `sync_vector_lake` tool.
    5. Main Agent Release: The main agent must release control immediately and not wait synchronously for the subagent.
  </workflow>

  <tool_dispatch>
    - `invoke_subagent`: MUST be used to spawn the subagent for concurrent tasks.
    - `Reson-KnowWeave-mcp`: `prepare_ingest_batch` (or the `sync_vector_lake` wrapper) queues unprocessed sources. The host Runner claims packets via `claim_ingest_tasks`; its read-only model subagent returns `files_written` plus an explicit `integration` disposition. Only the host controller invokes `finalize_ingest` after validation; the model subagent never writes Wiki pages or finalizes directly.
  </tool_dispatch>

  <checkpoint_rules>
    - [FABLE 5 CHECKPOINT] 必须在此定义强制阻断点，要求人类 Approve。Before invoking the subagent to perform the sync, the agent must explicitly ask the user for permission to proceed and wait for their response.
  </checkpoint_rules>
</execution_workflow>

<delivery_standards>
  <output_format>
    <thought>
      [执行自我推演与 Metrics 校验区。该区域内容作为模型的推理草稿。]
      1. Check if human approval has been granted.
      2. If not, trigger Fable 5 checkpoint.
      3. If granted, prepare the subagent invocation prompt and `scratch/` workspace.
      4. Verify `invoke_subagent` and `sync_vector_lake` tool usage.
    </thought>
    - Status update confirming the background subagent has been dispatched.
    - Prompt used for the orchestration subagent: "Please use the `sync_vector_lake` MCP tool from `Reson-KnowWeave-mcp` to queue pending sources; the host Runner owns model output validation and finalization."
  </output_format>

  <metrics>
    - Human approval successfully recorded.
    - Subagent successfully dispatched without blocking the main agent.
  </metrics>

  <validation_gate>
    - Verify that no direct synchronous calls to `sync_vector_lake` were made by the main agent.
    - Validate that physical artifacts or logs can be routed to the `scratch/` directory for sandbox validation.
  </validation_gate>
</delivery_standards>
