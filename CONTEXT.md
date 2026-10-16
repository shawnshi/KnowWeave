# CONTEXT: Vector Lake

## 1. Active Position

Vector Lake is a Markdown-first knowledge compiler with a file-backed governance runtime. It should not be treated as a classic vector database or a stateless RAG service.

Current boundary:

- Human-facing memory: `MEMORY/wiki/*.md`
- Page runtime index: `MEMORY/wiki/index.json`
- Claim topology: `MEMORY/wiki/claim_topology.json`
- Strategic intent: `MEMORY/purpose.md` (YAML contract parsed by `purpose_contract.py`)
- Canonical governance store: `MEMORY/wiki/.meta/vector_lake.db` (SQLite)
- Agent runtime memory: `operational_memory` table in that same SQLite store

The durable architecture is:

```text
raw source -> Markdown wiki -> canonical claims/evidence -> operational memory -> Memory Packet -> query context
```

## 2. Runtime Model

Markdown remains the sovereign, inspectable publication layer. Agent memory is compiled, scored, and selectively injected. **(V7.2+ Mandate: All new operational memory MUST be persisted directly into Markdown Wiki nodes via the Dual-Schema layout, specifically under the `## 2. 证据时间线 (Evidence Timeline)` section to prevent index-rebuild data loss.)**

Operational memory types:

- `fact`
- `preference`
- `decision`
- `task_state`

Scoring fields:

- `confidence_score`
- `freshness_score`
- `authority_score`
- `importance_score`
- `reinforcement_score`
- `validity_factor`
- `memory_score`

Conflict rules:

- Explicit contradictions use `authority_score > confidence_score > updated_at`.
- Same-key `preference / decision / task_state` records use `updated_at > authority_score > confidence_score`.
- Superseded losers are hidden from default runtime retrieval.
- Unresolved ties stay `conflicted` and must remain visible in governance surfaces.

## 3. Module Map

| Module | Responsibility |
|---|---|
| `cli.py` | Root CLI shim |
| `vector_lake/cli_app.py` | Argument parsing and command dispatch |
| `vector_lake/tools.py` | Tool facade |
| `vector_lake/tool_ingest.py` | Raw-source scan and Subagent instructions generation |
| `vector_lake/subagent_handoff.py` | Host subagent task packets; no in-process text generation |
| `vector_lake/ingest_schema_contract.py` | Model-facing reflection of the executable ingest schema |
| `vector_lake/ingest_output_contract.py` | Ingest answer contract published with each task packet |
| `vector_lake/indexer.py` | Page index, weighted edges, claim graph refresh, pure-Python BM25 inverted index |
| `vector_lake/claim_extractor.py` | Page-to-entity/claim/evidence/source extraction |
| `vector_lake/tool_memory.py` | Wiki-as-Database operational memory persistence via MCP |
| `vector_lake/governance_store.py` | Canonical store, change sets, operational memory, conflict resolver; indexed operational-memory retrieval (`score_memory_items`, `search_operational_memory`, `search_memory_packet_views`) |
| `vector_lake/governance_metrics.py` | Debt and health metrics |
| `vector_lake/tool_search.py` | Hybrid search (local query expansion + BM25 + Graph Traversal), Memory Packet assembly |
| `vector_lake/tool_query.py` | Query synthesis with Memory Packet first |
| `vector_lake/tool_research.py` | Autonomous deep research and graph insight processing |
| `vector_lake/purpose_contract.py` | Purpose parsing, ingestion admission, SIR review, and synthesis proposal thresholds |
| `vector_lake/tool_review.py` | Unified review surface |
| `vector_lake/tool_doctor.py` | Runtime layout and dependency checks |
| `vector_lake/mcp_server.py` | Standard Model Context Protocol (MCP) server entrypoint |
| `vector_lake/watchdog_app.py` | Real-time ingest watcher, background job orchestration, scheduled auto-lint |
| `vector_lake/watchdog_status.py` | Status JSON telemetry broadcaster for the daemon |
| `vector_lake/wiki_utils.py` | Path resolution, frontmatter, atomic writes, backups |
| `vector_lake/db_store.py` | SQLite connection pooling, schema initialization, WAL settings, and the trigger-maintained `operational_memory_index` / `operational_memory_gram*` / `page_index_*` read projections |
| `vector_lake/defense_hook.py` | Pre-flight constraints and guardrails |
| `vector_lake/skeleton_parser.py` | Parsers for structural validation |
| `vector_lake/provenance.py` | Tracing entities to raw sources |
| `vector_lake/tool_entity_dedup.py` | Cross-type entity duplicate checks and name normalization |
| `vector_lake/tool_bulk_reconciliation.py` | Graph reconciliation |
| `vector_lake/yaml_utils.py` | YAML helpers |
| `vector_lake/memory_gram_index.py` | Exact n-gram inverted index over operational memory (base postings + a dirty set that suppresses stale documents) that makes `search_operational_memory` sub-second without changing its ranking; it answers only while nothing is queued, and a rebuild is what clears the queue |
| `vector_lake/page_index_projection.py` | SQLite projection of `index.json` (node table + ordered edge table + process-wide adjacency cache) so read paths never parse the whole file |
| `scripts/community_clustering_daemon.py` | Optional operator-invoked Louvain analysis; not scheduled by watchdog |
| `schema.md` | Wiki and runtime memory contract |
| `.pi/agents/` | Host subagent contracts (`vector-lake-ingestor.md`) |

## 4. CLI Contract & MCP Interface

**Note (v8.3+)**: Agents interact with the system entirely through the `vector_lake/mcp_server.py` MCP tools (e.g. `search_vector_lake`, `sync_vector_lake`).

**Command surfaces**: there is no bundled slash-command compatibility layer. `commands/`
was removed in the host-convention batch — it held two `commands/*.toml` files that no code
read. Hosts reach every capability through the MCP tool surface, and hosts that package
skills additionally use `$vector-lake:<name>` (for example `$vector-lake:query`,
`$vector-lake:timeline`). `tests/test_command_surface.py` fails if a `commands/` directory
reappears before the documentation is updated.

### 4.1 Host-runtime conventions

Host conventions are data, not literals: `vector_lake/host_env.py` owns the `~/.gemini`
and `~/.codex` fallbacks (env file, MEMORY root, diary hook, payload sandbox roots) and
`tests/test_portability.py` fails if any other module reintroduces one.

| Variable | Effect |
|---|---|
| `VECTOR_LAKE_MEMORY_DIR` | MEMORY root; highest precedence, above `config.json` |
| `VECTOR_LAKE_PAYLOAD_ROOT` | **Extends** the built-in payload sandboxes; it no longer replaces them. A configured root is trusted as named, while the built-in `brain/` roots still require the `<root>/<project>/scratch/...` shape. A value resolving to a filesystem anchor (`C:\`, `/`) is refused with `host_env.PayloadRootError` |
| `VECTOR_LAKE_PAYLOAD_MAX_BYTES` | Payload size ceiling, default 5 MiB |
| `VECTOR_LAKE_DIARY_SYNC_SCRIPT` | Overrides `host_env.legacy_diary_sync_script()` |
| `VECTOR_LAKE_SUBAGENT_RUN_ID` | Namespaces generated subagent task packets |

The following CLI commands remain the ground truth operating surface for *human operators*:

```powershell
python cli.py doctor
python cli.py sync
python cli.py search "query" --domain General --top_k 5
python cli.py search "query" --domain General --mode memory --top_k 5
python cli.py search "query" --domain General --mode claim --top_k 5
python cli.py query "question" --domain General --dry-run
python cli.py review
python cli.py audit-graph
python cli.py research [--dry-run]
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
python cli.py graph
# gc/delete 默认预览；写入须显式 --apply 并取得相应授权。
python cli.py gc --days 30
python cli.py delete "<raw-source-path>"
```

For Windows validation, prefer:

```powershell
$env:PYTHONUTF8='1'; python -m unittest discover -s tests -p 'test_*.py' -v
$env:PYTHONUTF8='1'; python -m compileall vector_lake tests
```

## 5. Validation entry points

Use the test commands in [README.md](README.md#开发测试) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Evidence must identify the current commit/source version and test scope; historical test totals, memory counts and Doctor snapshots are not current acceptance evidence. The core fallback/native manifests are bounded selections, not whole-repository or production-SLO approval.

For a resident MCP connection, verify its interpreter, PID and loaded code with `runtime_identity`. Restarting watchdog/runner alone does not reload MCP.

## 6. Operating Rules

1. Preserve the split: Markdown is for humans; `.meta` is canonical state; `operational_memory` is for Agents.
2. Keep `schema.md`, `README.md`, and `.pi/agents/` aligned when the runtime surface changes.
3. Do not hand-edit derived runtime files unless the task is explicitly data repair. Prefer rebuild paths.
4. Use dry-run first for delete, gc, and any operation that removes assets.
5. Treat lock contention as environmental state, not proof that a code patch failed. Note that `daemon-watchdog` and `sync` operations are protected by cross-process `filelock` to prevent meta and index corruption.
6. Use `PYTHONUTF8=1` when scripts may print Chinese paths.
7. Never silently include unrelated dirty files in a publish or commit scope.

## 7. System Capabilities & Architecture Defenses
The Vector Lake system is designed for high-concurrency ingestion and graph maintenance with several defensive mechanisms:
- **Two-Track Watchdog**: Monitors raw sources for incremental ingestion by creating host-subagent task packets and monitors wiki nodes for O(1) index updates. It hooks `on_deleted` and `on_moved` events to reflect Semantic GC operations and prevent ghost nodes.
- **Write Health Gate**: Ordinary mutations are blocked when watchdog heartbeat, mutation outbox, or Wiki/index/SQLite projection consistency is unhealthy. Bounded repairs can use schema mode or an explicit operator override. Runtime health checks are read-only when the SQLite file already exists, so doctor/write-gate checks do not take a schema-migration write lock during watchdog batches.
- **I/O Debouncing**: The Indexer buffers multiple O(1) memory mutations (BM25 updates, edge recalculations) across batched file events and flushes them in a single write operation to `index.json`. This eliminates O(N) disk thrashing during heavy wiki modifications.
- **Scheduled Read-Only Lint**: At 10:00 and 23:00 the watchdog refreshes dirty graph topology, runs `lint_vector_lake(auto_fix=False)`, and checkpoints the SQLite WAL. It does not merge, archive, or rewrite Wiki pages.
- **Explicit Auxiliary Jobs**: Research, timeline rebuild, semantic deduplication, overview compilation, and community clustering are operator-invoked workflows. The watchdog does not silently launch those scripts.
- **Strategic Intent Engine (V12.0)**: Parses `MEMORY/purpose.md` as the primary source for intent keywords and weighting. The same contract is injected into ingestion, retrieval, and research; it validates `strategic_scope` and `evidence_tier` during ingest, exposes SIR review proposals, and creates de-duplicated `Synthesis-Proposal` queue items only after the configured independent-source and tension thresholds are met.
- **Strict Two-Step CoT Ingestion (V9.0)**: Forces ingestion subagents to output an intermediate `analysis_buffer.json` (parsing tensions, consensus, unknowns) before writing Markdown, eliminating extraction omissions.
- **Strict 7-Type Enforcement (V9.1)**: The PIEA interceptor actively strips nested/invalid prefixes (e.g., `Concept_Synthesis_` or `Entity_`) and forces LLM agents to save files using exact, canonical 7-type filenames (`vendor`, `product`, `person`, `event`, `concept`, `synthesis`, `source`). All backend algorithms are strictly aligned to this matrix.
- **Cross-Type PIEA Deduplication (V9.1)**: `tool_entity_dedup.py` no longer segregates similarity checks by type. If an agent proposes `Vendor_Accenture` when `Concept_Accenture` already exists, it triggers a hard block and forces a timeline append, eradicating "Same Name, Multi-Type" pollution.
- **Error Resilience**: Utilizes a global `global_task_lock` for thread safety and halts on consecutive failures to prevent cascading storms. The watchdog writes an immediate startup status and periodic heartbeat to `MEMORY/wiki/.meta/.watchdog_status.json`; subprocess errors are emitted to the same status surface.
- **AST-Based Parsing (V11.5)**: Replaced brittle Regex and string splitting with `mistune` Abstract Syntax Tree (AST) parsing, making extraction immune to Markdown stylistic variations or Markdown linting format changes.
- **Native Vector Engine (V11.5)**: Integrated `sqlite-vec` extension for FTS5 + Vector hybrid search. Eliminated the `embeddings.pkl` O(N) memory bottleneck, offloading similarity calculation directly into the SQLite C-backend.
- **Rate-Aware Embedding Scheduler**: Gemini embeddings are a resumable projection. All processes reserve requests and tokens through one SQLite rolling window, validate response cardinality and 3072-dimensional vectors, use an explicit HTTP timeout, and record resumable batch progress. Index rebuild and incremental index maintenance never invoke the embedding API.
- **O(V+E) Graph Indexing (V11.5)**: Eliminated catastrophic O(N²) CPU deadlocks during node overlapping frequency calculations by utilizing an inverted-index map. 
- **Chinese Tokenization (V11.5; backend replaced 2026-09-18)**: CJK pre-tokenization runs before SQLite `MATCH` execution, fixing the precision drop caused by `porter unicode61` character splitting.  The backend is `rjieba` (jieba-rs via PyO3) alone; the pure-Python `jieba` fallback is gone, and a host without `rjieba` reports `unavailable` rather than degrading silently.
- **Subagent Text Runtime Boundary (V11.13)**: Search expansion and reranking are deterministic; ingest creates current-environment subagent task packets; semantic dedupe no longer calls a text arbiter. `google-genai` remains only for embedding paths when `GEMINI_API_KEY` is configured.
- **Canonical Transaction Boundary**: SQLite canonical changes and durable outbox intent commit together. Markdown, FTS, `index.json`, and `claim_topology.json` are recoverable projections written after commit.
- **Pure Canonical Architecture & Outbox (V11.11)**: Mutation entrypoints converge on `mutation_coordinator`; the outbox is polled even when the wake-up signal is missing, claims rows with leases, retries transient failures, and records terminal errors. Full and incremental index paths read SQLite entities and identify nodes by `page_key`.
- **Fenced Subagent Completion**: Ingest claims issue owner/token/generation credentials. Finalization validates them before work and repeats a compare-and-set inside the canonical transaction, so expired workers cannot commit late results.
- **Timeline Projection Parity**: Claim deltas update Timeline rows in the same transaction. Queries verify stable event-ID parity and fall back to canonical claims whenever the projection is incomplete; deep Doctor checks report exact missing/extra counts.
