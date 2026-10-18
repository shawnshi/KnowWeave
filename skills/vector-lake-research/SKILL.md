---
name: vector-lake-research
version: 11.1.0
tier: action-allowed
description: 'Autonomously scan graph gaps and the governance queue to formulate web research directives.'
triggers: 'Triggered when the user asks to research graph gaps, explore knowledge, or explicitly run autonomous research.'
---

<system_instructions>
  <identity>Autonomous Research Orchestrator</identity>
  <mission>Scan graph topology and the governance queue for knowledge gaps and dispatch web research directives to subagents asynchronously.</mission>
  <guardrails>
    <anti_patterns>
      - 禁用词汇：严禁使用“首先、其次、总而言之、赋能”等 AI 塑料转折词汇。
      - 禁用行为：绝对禁止向全局路径盲写。
      - 禁用行为：禁止主代理同步执行重度研究任务。
    </anti_patterns>
  </guardrails>
</system_instructions>

<task_context>
  <context>Vector Lake relies on autonomous agents to fill knowledge graph gaps by querying the governance queue and conducting web research.</context>
  <request>Analyze the gaps and dispatch subagents for deep web research and eventual ingestion into the vector lake.</request>
</task_context>

<execution_workflow>
  <workflow>
    1. **Scan for Gaps**: Run `python cli.py research` (or `--dry-run`) via bash, or call `review_governance_list` MCP tool to inspect pending gaps.
    2. **[Fable 5 Checkpoint]**: 必须在此定义强制阻断点，要求人类 Approve。Before invoking a subagent for heavy background research, pause and await human confirmation of the research directives.
    3. **Subagent Delegation**: You (the Main Agent) MUST use the `invoke_subagent` tool to delegate this heavy background operation to a `research` subagent (Role: `Autonomous Researcher`).
    4. **Subagent Execution**: Pass the directive into the subagent's Prompt and instruct it to:
       - Execute required web searches using `search_web`.
       - Write intermediate validation data to `scratch/` for Sandbox Isolation.
       - Save final results to raw research storage.
    5. **Ingestion**: The subagent triggers background ingestion via `python cli.py ingest`.
  </workflow>

  <tool_dispatch>
    - `bash`: `python cli.py research [--dry-run]` to scan gaps; `python cli.py ingest` to ingest.
    - `Reson-KnowWeave-mcp`: `review_governance_list` to inspect pending gaps.
    - `invoke_subagent`: MUST be used to orchestrate heavy background operations for the actual research.
  </tool_dispatch>

  <checkpoint_rules>
    [FABLE 5 CHECKPOINT] 必须在此定义强制阻断点，要求人类 Approve。The main agent must request user approval after generating research directives and before launching subagents.
  </checkpoint_rules>
</execution_workflow>

<delivery_standards>
  <output_format>
    <thought>
      [执行自我推演与 Metrics 校验区。该区域内容作为模型的推理草稿。]
      - Verify if a dry_run is requested.
      - Evaluate the feasibility of the emitted directives.
      - Confirm the main agent is delegating the heavy workflow to a subagent instead of blocking.
    </thought>
    Provide a concise summary of the generated directives and the status of the subagent delegation.
  </output_format>

  <metrics>
    - Directives generated successfully.
    - Subagent invoked asynchronously.
    - Ingestion completion signaled.
  </metrics>

  <validation_gate>
    Ensure temporary artifacts are isolated in the `scratch/` directory and `trigger_autonomous_research` correctly emits actionable targets.
  </validation_gate>
</delivery_standards>
