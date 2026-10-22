# 运行、维护与恢复

所有命令和源码路径均相对于仓库根目录；先使用 README 核对过的项目虚拟环境。本文是参考，不是可整段执行的安装脚本。

[返回项目首页](../README.md)

首次上手只使用合成演示库；[CLI 完整目录](../README.md#commands) 中的删除、修复、重建和恢复不是初始化步骤。进程、模型可用性、业务发布与投影健康必须分别核验。

## 运行身份与隔离业务验收

MCP `runtime_identity` 返回**实际调用进程**的 PID、Python 版本、磁盘 checkout、选定 Python 函数的已加载代码与当前源文件比较、已加载 core 的版本／路径和磁盘二进制 SHA。它不打开数据库、不导入未加载的业务／core 模块、不重载或重启服务。

`matches_function_code` 仅比较明确列出的解包函数代码，不涵盖装饰器包装、默认参数、globals、导入时状态或整个模块。Git HEAD 与磁盘 SHA 不代表已加载版本；core 磁盘 SHA 也不证明内存映像相同。`not_loaded`、路径不符、代码不符、不可核验分别报告，不能替代健康门或发布绑定。

隔离业务入口复用 CI 后端门，以新目录保存 synthetic MEMORY、后端回执、JUnit 和成功回执；拒绝重用输出目录，单个 lane 130 秒超时后终止其进程树，不生成成功回执：

```bash
python -B scripts/business_acceptance.py --backend native --native-dir /path/to/fresh-ci-core-build --output /path/to/new-native-artifacts
python -B scripts/business_acceptance.py --backend fallback --output /path/to/new-fallback-artifacts
```

覆盖真实 synthetic canonical→outbox→Markdown／索引／FTS 的创建、更新、旧发布拒绝和删除，以及 in-process MCP 的工具发现／调用和定向故障回归。不是生产语料、模型网络、远端协议服务或迁移验收；成功回执仍写 `production_acceptance: not_established`。实际部署仍须单独授权、停止旧 writers、验证完整备份、绑定期望发布构件，并在真实服务进程执行身份和业务验收。远端 CI 与实际生产验收仍须绑定具体发布提交及运行进程；原生剪枝合同为每节点最多 15 条 incident 边、同权按端点字典序，不能据此宣称所有评分严格等价。


## 已知限制与运维要求 (Known Limits & Operational Requirements)

以下约束由当前实现决定，部署前必须满足，否则会出现与预期不符的行为。

|约束|事实|规避|
|-|-|-|
|必须常驻守护进程|outbox 消费、增量索引、定时 lint、**到期时的 gram 索引重建**、WAL checkpoint、备份保留、兜底扫描与 Loop 线程监督均在 `watchdog_sync.py` 内，**它同时拉起并看护摄取 Runner**；摄取任务包的**模型调用**在宿主侧 `scripts/ingest_runner.py`（自动启动默认写入页面；shadow 跳过模型调用，但仍认领任务并记录状态），由 `scripts/ingest_runner_service.py` 负责重启，后者自身持有单实例锁|只读检索才可省略守护进程。**没有守护进程时没有任何定时维护会触发**（写入也会在 5 分钟后进入 outbox 积压告警），gram 索引需人工按 `doctor` 的 `due=` 执行 `python cli.py gram-index --if-due --apply`。常驻形态用计划任务 `VectorLake-Watchdog`（见“日常运行入口”），不依赖临时 shell 保持常驻。实例锁可拒绝重复启动，但不能代替进程托管或证明子进程已退出|
|摄取是一条中继流水线，各段职责不重叠|`ingest_worker`（守护进程内）只认领 `queued` / `failed`（预算未用尽）/ 租约过期的 `dispatched`，产出任务包并转入 `awaiting_subagent`；宿主侧 `ingest_runner.py` 只认领 `awaiting_subagent` 与租约过期的 `subagent_processing`，因此两段不会争抢同一作业。作业租约、`lease_token` 与 `lease_generation` 保证同一个作业不会被并发提交；被顶替或终态失败的作业不会再被派发（前者的状态被标为 `superseded`）|先区分来源预算、后端暂停与历史缺页；缺页按当前字节和明确清单走受控重编。**不要**为“多跑一点”而改原文伪造变更、清空队列、改台账哈希或绕开租约手工改 `jobs`|
|Runner 健康默认只告警|`runner_absent` / `runner_stalled` / `runner_failing` 默认进 `warnings`，不翻转 `ok`；“从未跑过”与“跑挂了”由 `.meta/runtime/runner_supervisor.json` 区分|需要把这几项并入 `degraded` 列表（依然不阻断写入）时设 `VECTOR_LAKE_RUNNER_STRICT=1`|
|文本编译需要模型执行后端|`cli.py sync` 只准备任务包；统一 Runner 选择 Pi / Gemini / Codex 执行模型调用，`subagent_handoff.generate_text` 仍抛 `SubagentTaskRequired`|在 Runner 主机安装并认证所选 CLI；通过 `config.json` 的 `ingest.backend` 选择后端，Pi 才要求 subagent 能力|
|向量投影需要维护|页面变更会使对应向量失效；常驻守护进程的周期兜底会按批次补齐缺失或过时向量，受嵌入模型与凭据可用性约束|未启用兜底或需立即恢复时执行 `python cli.py embedding-backfill --apply`；缺少嵌入凭据时检索降级，不以向量缺失冒充库中无资料|
|GC 的孤儿判据是拓扑度数 ≤ 1|度数来自 canonical 的 `links` / 共享来源 / claim 共现；**不是**可视化边集|先 `python cli.py gc` 做 dry-run，它会在同一调用中打印每个页面的实际度数。单次删除超过候选页 50% 时会自动中止，需 `--force` 才继续|
|删除类命令默认演练|`gc` / `delete` 默认 dry-run，必须 `--apply` 才落盘|保持默认；仅在确认 dry-run 输出后追加 `--apply`|
|修复类工具需要后置重建|`wiki-restore` 会恢复 Markdown，但索引投影需单独重建|按其输出末尾提示运行 `projection-rebuild-index --apply`|
|全量重建成本取决于语料与后端|全量重建需要读取与分词正文；增量路径可跳过未变更节点，不使用旧语料耗时外推当前实例|仅在必要时全量重建；日常依赖增量更新，并记录当前工作负载下的耗时|
|过滤态扩容有界|查询内复用候选池前缀与过滤判定；候选池最多 4096 条，宽泛过滤仍可能触顶或超过延迟预算|触顶时保留不完整告警，不把有界候选检索写成精确全量检索。性能需按当前语料和 provider 网络重新测量，历史结果见 [CHANGELOG](../CHANGELOG.md)|
|人工编辑会经过校验|未通过 schema / 目的契约校验的手改页面会被拒绝并保留原文件，日志给出原因|修复页面后重新保存，或查看守护进程状态文件的 `current_action`|
|增量 wiki 监听只识别小写 `.md`|现有大写 `.MD` 页面的路径兼容不代表增量监听会接收其事件|新页面使用小写 `.md`；已有大写扩展名页面需核对全量重建结果，不以主进程心跳证明其增量索引已更新|

### Tantivy 写入约束

启用 Tantivy 时，原生 writer 仅由一次写入操作持有：进程内互斥、同一 MEMORY 的跨进程文件锁，每层准入等待最多 3 秒；原生提交及 merge 收尾时间不包含在这两个准入期限内。成功后释放原生 writer，失败则回滚未提交内容并释放，防止坏批次进入后续提交。已有 reader 可在写入期间读取上一次提交。

生产 FTS 变更与专用 `tantivy_mirror_queue` 意图在同一 SQLite 事务提交；不再直接写 native。现有 watchdog 的 outbox worker 每 tick 独立推进一个 lexical 步骤，最多 100 行、4 MiB UTF-8 输入、一次 native commit，同 generation 最多 3 次持久 admission。native commit 与 writer release 均成功后才按 epoch/generation ACK；崩溃可幂等续接，超大或耗尽的任务保留并显示 blocked。Markdown 与 lexical 失败分别记录，不增加线程。队列按 key 合并，但没有硬磁盘 backlog 配额，也没有 native commit/merge 硬超时。

生产读只接受查前、native 查询和 SQLite metadata 完成后的完整 token 相同，且 incarnation/schema/目录身份有效的镜像。未追平、借用事务、缺目录或 binding、native 错误均使用 FTS5；合法 native `[]` 不触发 SQL 回退。`search_index_keys` 始终来自 SQLite。检索 ledger 记录实际成功路径 `fts5`、`tantivy` 或 `fts5+tantivy`，而非开关意图。Health 使用 durable phase/pending/blocked/error 与 validated token，不以文档数相等证明同步。

重建复用持久状态机，同步最多 32 步；`MirrorReplayDeferred` 表示已安排但尚未完成。现有 reconcile 错误接口将它报告为 `action=failed` 并注明 deferred，不报告 `repaired`；后续由 watchdog 或 `replay_pending()` 续接，不能反复调用 `rebuild_from_sqlite()` 重置 epoch。低层 `upsert_many` 整批提交仅保留 fixture/底层能力，生产不得与 durable worker 混用。操作级独占不代表单个常驻写入进程；切换前需协调停止旧 producer/consumer，禁止旧协议与新协议共同写索引。

安全运行回退是关闭 Tantivy、使用 FTS5，保留派生队列和控制状态；不要删除 canonical/FTS 数据。回退旧源码或恢复 SQLite 备份后，再次启用新协议必须完整 rebuild，不能沿用旧 ready。源码恢复应使用变更前备份，仅撤回对应变更、保留其他已有工作。合成 fixture 的测试覆盖不能替代目标实例的协议切换、恢复和读写验收；SQLite WAL/NORMAL 的断电持久性承诺未改变。


## 检索 scope / revision 加速器

以下命令只读预览显式指定的现有数据库；不是初始化、全量回填或分块构建：

```bash
python cli.py retrieval-indexes --db "<existing-db-path>"
```

生产应用须有当前授权、独立复核和可用恢复点。先确认写入作业空闲，在原知识库边界内保留 SQLite 一致备份及 Schema 对象清单，再另加 `--apply` 执行既有事务维护；默认预算 60 秒、最高 120 秒，锁冲突或不兼容对象失败时不覆盖已有对象。安装五个 scope 索引；仅当现有数据库包含 `vec_emb_float` 表时，才同时创建向量 revision 计数表及三个触发器。不重写 memory / 页面 / 向量正文，也不启用外部 embedding 或 chunks。

安装后核验 `memory_acceleration` / `vector_revision`、对象定义和实际查询计划，而不只看命令退出码。scope 索引不能替代大域 memory 的流式 TopK 修复；后者保留完整 scoped DF、独立 active/history 窗口，工作超预算明确失败。新连接与当前工作负载还须验证，合成时延不作为生产 SLO。代码更新后，驻留 MCP 需要单独重连；数据库索引安装不等于驻留函数已更新。

回退优先只撤回本次清单中的派生加速对象。恢复整库会影响备份后的合法写入，必须另行核准并协调 writers，不能因有备份就直接覆盖活动数据库。


## 域内记忆检索计时

检索 ledger 的 `timings_ms` 增加粗粒度阶段：`memory_scope_df`（完整域内 DF 扫描）、`memory_scope_weights`（请求内 IDF 权重准备）、`memory_scope_rank`（第二遍扫描、评分及窗口维护）、`memory_scope_total`（整个域内读取快照及结果整理）。DF 和 rank 均包含各自的 SQL 读取与 JSON 解码，**不是纯 CPU 评分或独立解码耗时**。total 包含这些子阶段，不能再与子阶段相加；同一请求内多次域内检索会累计同名阶段。

这些阶段在成功或异常退出时记录，不吞掉原始失败；不做逐行计时或开启 cProfile。现有 `index_generation` 中的 `memory_scope` 保留扫描行数/字节、遍数与保留量。IDF 只在同一请求、同一固定快照的 DF 上复用，不跨域或跨请求缓存，不截断语料、改变排名规则、扩大预算或由 reader 安装 Schema。未执行的阶段可以缺省。

代码验收、原生只读探针、驻留 MCP 重连和 Pi 当前会话 `/reload` 是不同层次。磁盘更新或合成测试通过不证明当前驻留查询函数、模型查询策略已生效；MCP ledger 仍可能来自旧进程。


## Query 载荷副作用与计时边界

`preview_query_context` 内联返回预算内上下文，不写 proposal envelope 或 Wiki 页面，但检索仍可能记录正常的 audit ledger。`query_logic_lake` 的两个 `dry_run` 值都会在扩展根目录的 `tmp/query_context_*.md` 写入上下文载荷，并可能清理该目录内已过期的 query 载荷；`dry_run=true` 只是写完后返回 trace，而非后续 synthesis prompt。知识库路径由 `VECTOR_LAKE_MEMORY_DIR` 重定向，不意味着扩展根的 `tmp` 也随之移动。两者均不执行生成模型推理，载荷生成不等于 canonical Wiki 写入完成。

ledger 的 `timings_ms` 区分 `query_context_assembly`（包含 memory packet 和 page search）、`query_context_render`（序列化与字数预算）、`query_payload_write`（实际载荷写入）。assembly 包含内部检索阶段，不能将其与子阶段相加；模型推理、代理编排和人工原文核验不在这些接口计时内。`cache_events` 沿用现有结构计数器格式，只归属当前请求的结构事件；进程聚合计数独立保留，不能将并发请求的差值当作本请求副作用。新增 `query_payload_write` 次数和 `query_payload_bytes` 实际文件字节数；不记录载荷正文或额外复制查询内容。

purpose 配置在同一请求和知识库范围内固定一次，下一请求重新检查原有文件更新时间；跨根切换不会复用前一根的评分结果。SQL 当前状态与当前分数共享有界的请求内计算，仍在 LIMIT 前计算，异常或请求结束后清理。`purpose_snapshot_load/hit`、`memory_evaluation_compute/hit/eviction` 计数用于识别重复工作，不代表事实或来源核验通过。

性能探针只能对明确授权的样本运行。domain 和 profile 不是认证或隐私边界；应先排除不允许读取的对象，使用任务专用知识库与扩展根副本，关闭外部 embedding/prewarm，再分别采样进程首请求、热态往返和引擎子阶段。公开副本不自动代表生产数据、purpose 配置或服务负载，计时结果不得直接作为生产 SLO。功能测试中需要 ledger 的用例显式启用日志，不能继承性能进程的全局关闭开关。


## 可选严格 Source 证据读取

`get_query_evidence(query, node_keys, domain, cluster='')` 为 Windows 试点的可选 MCP 门面，仅支持已有 canonical Source 版本/Raw 摘要绑定的记录，拒绝任意路径、连接和上下文参数。域仍限 `Artificial_Intelligence`／`Medical_IT`，不存在全域 fallback；缺少绑定明确拒绝，不解释为原文不存在，不从磁盘补字段。普通 Source-first 原文阅读继续走已有授权路径，不能标成严格 verified。细节见 [QUERY_EVIDENCE.md](QUERY_EVIDENCE.md)。

宿主使用固定 canonical 位置和每请求专用只读快照；非标准 DB 覆盖拒绝。配置阶段计入同一不延长的合作式期限。个人用户已选择暂不实施严格权限隔离，仍需承认 WAL/辅助文件竞态与非零物理协调风险。新工具的磁盘登记、实际客户端调用和活动 Pi 模型可见性分别核验；不得为加载新工具重启无关 Watchdog/runner、改变模型或写知识内容。

## 核心机制与运行时防护 (Core Runtime & Defense Systems)

> **运行前提**：完整摄取需要 **① 单机数据根 ② 常驻 `python watchdog_sync.py`（Windows 可由计划任务 `VectorLake-Watchdog` 托管）③ 已安装、已认证的 Pi / Gemini / Codex CLI 后端**。`cli.py sync` 只准备任务包，统一 Runner 执行模型调用并经 `finalize_ingest` 发布。守护进程自动拉起并看护 `scripts/ingest_runner_service.py`，可用 `VECTOR_LAKE_RUNNER_AUTOSTART=0` 关闭；通常无需另开手工监督器。MCP 客户端身份不决定摄取后端，Pi 的 subagent 要求也不适用于 Gemini/Codex。只启动 MCP server 而不启动守护进程时，写入会在 5 分钟后进入 outbox 积压告警状态。

* **双轨看门狗 (Two-Track Watchdog)**：除增量文件外还捕获 `on_deleted` / `on_moved`，因此重命名或删除页面不会在图谱里留下幽灵节点。
* **写入健康门 (Write Health Gate)**：写入只在**硬故障**下被阻断（数据库不可用、存在 hard-failed 的 `mutation_outbox` 行）。outbox 积压超过 `VECTOR_LAKE_OUTBOX_MAX_BACKLOG`、投影漂移、心跳过期、终态失败作业、时间线 parity 漂移都属于**可修复降级**，只记录告警并继续写入——阻断它们会同时阻断唯一的修复通道。需要严格模式的运维方可分别用 `VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING` / `VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING` / `VECTOR_LAKE_TIMELINE_PARITY_BLOCKING` 把这些降级提升为阻断。
* **I/O 批处理防抖 (I/O Debouncing)**：同批次修改合并为一次 `index.json` 写盘；`index.json` 不再保存完整正文（每个节点保留至多 320 字符的摘要，摘要与 `weighted_edges` 各占文件的一部分——具体比例取决于实例），增量 FTS 与旧向量作废使用短事务；全量投影刷新仍可能持有写锁，不能保证重建期间没有锁竞争。
* **语义张力量化模型 (STQM)**：图谱原生支持 `tension_edges`，把争议与矛盾结构化为冲突边，Query 时可直接展示领域盲区。
* **跨类型本体拦截 (PIEA)**：入口级跨类型查重，避免同一名称多态共存；内置正则清洗违规嵌套前缀（如 `Concept_Synthesis_`），并由 schema gate 校验受控前缀与类型。
* **持久化增量索引与稀疏图遍历 (Sparse Graph Traversal)**：前台变更先写 durable outbox，Watchdog 合并批次后更新索引；`_calculate_weighted_edges` 使用稀疏遍历并限制每节点投影边数。
* **跨平台进程防护**：后台子脚本使用 UTF-8；模型调用与被看护的 Runner 使用 `process_control`。Windows 子进程在启动门闩放行前加入带 `KILL_ON_JOB_CLOSE` 的 Job Object；POSIX 使用独立进程组。超时后收束受控进程族，但 POSIX 不覆盖主动 `setsid()` 逃离的进程。
* **派发与版本绑定**：派发时刷新候选与版本快照，再在同一事务中更新任务载荷和交接状态；获写锁后重新检查租约与所有权。保留原作业身份、源路径、内容哈希和重试历史；刷新前后源校验或投影不一致时拒绝旧快照，不向旧提案补写新版本令牌。
* **内容校验与历史异常隔离**：新摄入任务和 Source 的 File Hash 仍使用 MD5；已有 `sha256:<digest>` 处理账本在观察快照缺失或变化时，按 SHA-256 验证内容，不因仅修改时间变化而重复入队。补齐缺失观察字段时保留原摘要格式；SHA-256 校验不可用时不派发，内容不匹配仍按变更入队。账本记录的大小非零、却对应空文件 MD5 时，扫描跳过该源并由 `doctor` 报告；不自动改写历史哈希或批量重派。历史大小为零的文件后来新增内容，仍按正常变更检测入队。
* **定时确定性维护 (Scheduled Deterministic Maintenance)**：每天 10:00 与 23:00 刷新脏图拓扑、执行只读 lint、在索引落后时重建 gram 倒排、做 SQLite WAL checkpoint 并执行备份保留；各维护阶段分别执行并汇总失败，lint 失败不妨碍后续阶段；checkpoint 会核对 SQLite 返回的 busy/帧数结果，不把“调用未抛异常”当作完成。另有一条独立节拍的兜底扫描（`VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS`，部署示例设置为 600 秒；未设置时应用缺省值为 900 秒）按扫描规则准备新源或内容已变更的源、作废陈旧任务、释放失去 job 的在途标记，并按批次重建缺失或**输入已变**的向量。研究、去重、聚类等独立脚本不会被该循环隐式启动。
* **向量投影存于 SQLite (vec\_embeddings)**：向量由 `sqlite-vec` 存放于 `vector_lake.db` 的 `vec_embeddings` 表，不再依赖模型侧的 JSON 载荷；语义去重守护进程只读该表，读取失败时退回**词法/拓扑去重**（不是旧缓存）。向量的**存在不等于有效**：页面被绕过增量索引的路径改写、或全量重建改动了别名与摘要时，旧向量不会被删除，只会默默继续用已经不存在的正文答题。因此每个节点在写入向量的同时记录其嵌入输入的摘要（`vec_embedding_inputs`），周期兜底每轮比对当前节点，把缺失、输入已变、以及未打标的节点一并按批重建；需要一次性全量重建时用显式 `embedding-backfill`。
* **本体免疫型排重 (Ontology-Immune Deduplication)**：去重守护进程豁免 `Source_*` 等时序不可变信源，避免“相似度过高即合并”把不同日期的研报强行合流。
* **合并的可回放性 (Merge Durability)**：`resolution=merge` 只能在合并**已落盘**时写下。类型/ID 不匹配不再静默落到 `_mark_resolved`（fail-closed），声明的名字与文件名不一致（`_`/`-`）时回退查别名注册表，落盘时同写 `merge_applied`/`applied_at`；`lint` 按 `unapplied_merge_items()` 报出“已 resolved 但两页俱在”的条数——只看 `type`/`status` 会把早先已判定为 `skip` 的近邻算成待办。**被消费页的键与标题必须进入幸存页的 `aliases`**（`semantic_merge._union_frontmatter` 的既有规则）：链接解析只认文件名、标题与 frontmatter `aliases`，不读 SQLite 别名表。
* **统一 SQLite 数据底座 (Unified SQLite Engine)**：实体、断言、证据、信源、图拓扑、变更集、治理队列与运行态记忆统一落在 SQLite，启用 WAL。
* **差分垃圾回收机制 (Diff-based GC)**：Markdown 层面重命名 / 删除或断言被移除时，同步层按页面增量清理对应的实体、断言与证据，不再只增不减。
* **夜间拾荒者集群 (Janitor Swarm)**：语义去重的**分片准备器**。`python scripts/launch_janitor_swarm.py` 读取治理队列中的 pending merge 项，按 `SHARD_SIZE` 切分为子代理任务包并写出 `janitor_manifest.json`。**它不会自行合并或启动任何外部进程**；实际合并由宿主子代理调用 `resolve_governance_item` 或 `bulk_reconciliation` 完成。
* **MCP 载荷沙箱 (Payload Sandbox)**：`write_wiki_page` 等文件载荷入口要求 `payload_file`；`update_operational_memory` 也接受直接 `content`，治理与摄取提交入口另支持内联参数。文件读取受沙箱路径与 `VECTOR_LAKE_PAYLOAD_MAX_BYTES` 限制；`VECTOR_LAKE_PAYLOAD_ROOT` 扩展允许根，不替代内置 `brain/<run>/scratch/` 沙箱。这是文件读取约束，不是模型、网络或进程隔离。

### 日常运行入口

1. **常驻守护**：`python watchdog_sync.py`（outbox 消费、增量索引、定时 lint 与 WAL checkpoint 都在这里；只跑 MCP server 会让写入持续积压）。自动拉起的 Runner 按 Config 节的优先级选择后端；未配置时使用 Pi，默认真实写页。`VECTOR_LAKE_RUNNER_AUTOSTART=0` 关闭自动看护；`VECTOR_LAKE_RUNNER_SHADOW=1` 跳过模型调用和写页，但仍认领任务、处理重复来源及记录状态，不是只读预览。
Windows 常驻入口为计划任务 **`VectorLake-Watchdog`**，执行 `scripts/watchdog_service.ps1`，固定仓库工作目录与 UTF-8，日志写入 `scratch/watchdog_service-*-{out,err}.log`，每个流保留最新 10 份。注册脚本 `scripts/register_watchdog_task.ps1` 先预检路径，再用 `-Force` 更新同名任务，不预先注销旧任务；更新仍会改变配置，不能把它当作无损重启命令。脚本默认配置为登录 + 开机 + 每 5 分钟、`MultipleInstances=IgnoreNew`、`ExecutionTimeLimit=PT0S`、失败重启 3 次且间隔 1 分钟；主体优先 `S4U`，失败后尝试 `Interactive`。现有部署的频率和身份可能不同，以实际任务配置为准。

正式运行链为 `VectorLake-Watchdog → scripts/watchdog_service.ps1 → watchdog_sync.py`；`scratch/` 只承载日志、备份和临时验收制品，不应作为常驻程序入口。包装器支持 `-Python`、`-MemoryDir`、`-PayloadRoot`、`-RunnerAutostart` 和 `-CatchupIntervalSeconds`，这些参数只影响所启动的进程，不修改全局环境。部署时可在计划任务的既有 `-File scripts/watchdog_service.ps1` 动作后追加下列参数，实际目录与解释器由部署者填写，不另起第二个服务：

```text
-Python "C:/path/to/python.exe" -MemoryDir "C:/path/to/MEMORY" -PayloadRoot "C:/path/to/KnowWeave/brain" -RunnerAutostart 1 -CatchupIntervalSeconds 600
```

只启动文件监听不会自动为启动前已经存在、之后未修改的 raw 文件补队。关闭周期补扫时，这些文件可能继续没有摄取任务；需要启用周期补扫，或在明确范围授权后手动准备任务。不要通过修改原始文件来伪造新事件，也不要把重新注册任务当作保留现有参数的重启。

**更新常驻代码时先安排空闲窗口**：确认摄取作业和 outbox 无在途处理，保留代码与数据库恢复点；控制计划任务的重复触发，再按已核验的 PID、创建时间和父子关系停止受影响的守护/Runner 进程树。`Stop-ScheduledTask` 不会取消后续触发，也不能单独证明 Python 子进程已退出。新守护可能接管已有 Runner，但接管不等于加载新代码。恢复后核对新 PID、心跳、队列与索引；MCP 连接须另行重连。不要按进程名批量终止其他宿主会话。
2. **摄取 Runner（可选的手工形式）**：`python scripts/ingest_runner_service.py --limit 2 --interval 120`，不指定 `--model-cmd` 才会按环境变量 / `ingest.backend` 选择内置后端。手工监督器及直接 Runner 默认 shadow（报告 `needs-model`，仍有队列与状态副作用）；真实写入需 `--no-shadow`。自动启动则默认真实写页。Runner 用 `--concurrency / -c`（或 `VECTOR_LAKE_RUNNER_CONCURRENCY`）控制模型调用并发；root 所有权锁与消费者锁阻止不同入口并行认领，细节见 Config 节。

   自定义 `--model-cmd` 保留 shell/stdin/stdout 协议，stdout 必须只返回 `{"files_written": [{"filename": "Source_*.md", "content": "..."}], "integration": {"disposition": "integrated|standalone", "relations": [...]}}`；standalone 用 `reason`，每次成功都必须返回 canonical Source 页。纯数组或缺少集成判断会被拒绝；租约、源哈希和候选清单由宿主提供，不能由模型改写。内置后端共用结果验证、修复预算和 `finalize_ingest`，不另建发布路径。

   当前摄取契约版本为 `10`（`tool_ingest.INGEST_CONTRACT_VERSION`），会在认领前重建旧版 queued / failed / awaiting\_subagent 提示词，保留 queued / failed 的原有尝试次数，不改动已领取的 subagent\_processing 作业。升级前先等待在途任务完成并保留恢复点；磁盘改动不会热更新驻留进程。
3. **检索**：`python cli.py search "<keyword>" --domain General`，或 `python cli.py query "<question>" --domain General --dry-run` 走预算受控的上下文组装。这里的 `General` 是实际类别，不是全域；执行前须按任务授权选择正确域，不能省略。`query --dry-run` 仍会准备临时 payload，不能据此推断零写入。
4. **摄取队列**：`python cli.py ingest-tasks` 查看 queued / awaiting\_subagent 作业；模型或 subagent 只返回包含 `files_written` 和 `integration` 的对象，宿主控制器验证后调用 `finalize_ingest` 入湖。
5. **来源预算与历史缺页**：同一 `(路径, 内容哈希)` 反复确定性校验失败，来源预算耗尽后停止派发；模型进程退出、超时或无效交付属于后端故障，不是内容拒收，按后端暂停门处理。`python cli.py ingest-tasks --abandoned` / `--terminal-failed` 查看来源或作业预算，`--clear-abandoned [FILE]` / `--close-terminal-failed` 仅用于已授权的队列维护，不是出版凭据。已处理但缺少当前 Source 的来源，应使用 [受控重编](controlled-recompile.md)：v2 每次批准 1–60 份精确公共来源，先冻结当前哈希、大小、mtime 与旧 ledger，再校验并小批派发；私人或归属不明材料不套用公共批准。保留历史拒收与失败记录，不修改原文或台账来绕过预算。
6. **周期治理**：`python cli.py review` 处理冲突与候选队列，`python cli.py doctor` 检查运行健康度。

### 显式当前版本重评（有界批准清单）

`recompile-ingest` v2 接受 1–60 份当前指纹绑定的批准清单，复用原生租约与写入校验；v1 的历史 78/60/18 清单边界仍保留。它不是普通 `sync` 的替代入口。批准制品、公开性证明、租约、失败预算、延后与完成回执的完整约束见 [专用受控重编说明](controlled-recompile.md)。仅校验、不登记或派发：

```bash
python cli.py recompile-ingest --plan <frozen-plan.json> --plan-sha256 <digest> --approval <read-approval.json> --approval-sha256 <digest>
```

历史版本的逐项特性说明不再在本文件维护；版本变更请查 `CHANGELOG.md`，运行契约以本节与上方“已知限制”表为准。


## 只读运维视图

```powershell
python cli.py ops-status --db "<明确的 vector_lake.db 路径>" --json
python cli.py ops-status --db "<明确的 vector_lake.db 路径>" --meta-dir "<明确的 .meta 目录>" --budget-seconds 2
```

该入口在加载 `.env` 和业务 tools 前分流，不调用初始化、迁移、修复或模型。只观察 SQLite 元数据、队列聚合、租约数量、八投影的可证明元数据及公开心跳；不读取正文、任务 payload、旧私密状态文件，不输出来源、owner、命令或原始错误。数据库使用 `mode=ro`、`query_only` 和底层表/列读取白名单；异常 view、generated column、缺表、锁竞争和超时报告 unknown，不能当作空队列或健康。

SQL 默认共享 2 秒预算，最高 30 秒，锁等待最多 250 毫秒；不是文件系统或网络盘的端到端时限保证。SQLite 仍可能进行只读锁和 WAL/SHM 协调，不能承诺所有文件字节不变。SQL 观察在单一读取事务内，数据库、文件投影和心跳不是共同快照；行数和文件存在不能证明投影 freshness/coverage 或生产 SLA。向量、FTS 等对象无法安全读取时保留 unknown，不绕过白名单。命令退出码只说明数据库是否可读，自动化健康判断应读取 JSON `state`。

Watchdog 和 Runner 更新既有遥测时附带白名单侧栏 `<meta>/runtime/{watchdog,runner}_ops.json`。reader 不回退旧状态；当前进程尚未采用新 producer、文件缺失/损坏/过期均须单独解释。侧栏拒绝链接/junction 目录链：Windows 持有不可共享删除的目录句柄，POSIX 使用 no-follow dirfd；该局部 IO 措施不是对本机恶意账号、硬链接、文件系统或整个进程的安全隔离保证。

上线仍须单独核验依赖风险、不可变原生版本、代码/任务/数据库恢复点、托管进程生命周期、真实业务探针和回退。灾备以 canonical SQLite/outbox 为恢复权威；顺序复制的投影文件不能视为共同快照，应隔离旧消费者与租约后重建并验证。计划任务中的 RestartOnFailure 配置不等于实际退出码触发重启的验收证据。


## 历史孤儿记忆的定向维护

正常页面删除会在同一 native SQLite 事务中，先删除引用所选 claim 的运行态记忆，再删除 claim；lookup 行和 gram dirty / retirement 标记由数据库触发器同步维护。初始化会刷新原生 INSERT / UPDATE lookup 触发器，避免旧定义遗漏 `source_rowid`。

遗留孤儿使用 `scripts/repair_orphan_memory.py`，不需要全库重建。仅选择 claim、页面投影和显式 canonical `page_key` 都已不存在的记忆；实体显示名或 ID 相同不算该页面仍存在。`freeze` / `apply` 要求 canonical mutation outbox 已结算，冻结最多 2000 条。以下为 PowerShell 示例，路径与批准值须按本机实际范围填写：

```powershell
$env:VECTOR_LAKE_MEMORY_DIR = "C:/data/MEMORY"
$Database = "$env:VECTOR_LAKE_MEMORY_DIR/wiki/.meta/vector_lake.db"
$Archive = "$env:VECTOR_LAKE_MEMORY_DIR/wiki/.meta/backups/orphan-memory-recovery"
$ApprovedCount = [int](Read-Host "输入已核对的冻结数量")

python scripts/repair_orphan_memory.py --database $Database --archive $Archive --mode freeze --expect-count $ApprovedCount
python scripts/repair_orphan_memory.py --database $Database --archive $Archive --mode dry-run
# 核对 manifest 与 dry-run 的数量和 scope hash 后，才输入批准 hash 并执行写入。
$ApprovedScopeHash = Read-Host "输入已批准的 scope SHA256"
python scripts/repair_orphan_memory.py --database $Database --archive $Archive --mode refresh-triggers --approved-count $ApprovedCount --approved-sha256 $ApprovedScopeHash
python scripts/repair_orphan_memory.py --database $Database --archive $Archive --mode apply --approved-count $ApprovedCount --approved-sha256 $ApprovedScopeHash
```

只有需要撤回清理时，才使用同一 archive、批准数量及 hash 执行恢复；不要把它与清理流程连续运行：

```powershell
python scripts/repair_orphan_memory.py --database $Database --archive $Archive --mode restore --approved-count $ApprovedCount --approved-sha256 $ApprovedScopeHash
```

* 默认模式为 `dry-run`；`apply` / `restore` / `refresh-triggers` 都必须显式绑定批准数量与 scope hash。不要把有写入作用的 mode 当作预览。
* `refresh-triggers` 只替换 `trg_om_index_insert` / `trg_om_index_update`，验证行内容与其他 schema 不变；不迁移表结构，也不回填全库历史行。触发器更新需单独纳入授权。
* `apply` 在独占 native 事务内重验实时范围、原始 payload 与 canonical 行号；范围变化、未结算 outbox、嵌套事务或索引验证失败均拒绝提交。无关记忆、lookup 和已有 gram 标记保持不变。
* `restore` 恢复原始 canonical 行号与字段，重新生成正确的 lookup；已有 ID 或行号冲突时拒绝覆盖。旧版本错误的 lookup 排序标记不是恢复目标。
* SQL 与迭代采用合作式 deadline，不能承诺操作系统 I/O 永不阻塞。提交后连接清理或回执出版失败，返回明确的 `state=committed` 及错误元数据；不能当作未执行直接重试。
* archive 包含完整 SQLite 快照和原始记忆 payload，应保留在本机私有备份目录，不进入 Git、模型审查包或对外报告。恢复演练使用隔离数据库，不能在生产库上随意往返执行。

执行后检查 `claim-pointer-report`、`projections` 与 `doctor`。gram 索引可能因其他摄取产生 live dirty backlog 而回退精确扫描；这不等于清理失败，也不代表应强制重建。是否维护以实际 `due=`、写入门和用户授权为准。

## 来源维护：受控窗口与仅生成补丁

普通协作锁及“哈希检查后再替换”不能排除直接编辑器的并发写入。当前没有经验证的物理排他发布能力，因此来源维护**不自动修改 Wiki、canonical／派生数据、历史或 outbox**。受控维护窗口须由真实控制建立；确认文字、布尔参数、哈希和审批记录都不能充当排他能力。

`provenance-backfill` 默认只读预览。显式准备补丁使用：

```bash
python cli.py provenance-backfill --limit 1
python cli.py provenance-backfill --limit 1 --prepare-patch
```

补丁包位于同一知识库的 `wiki/.meta/source_metadata_patches/patch-<id>/`，包含原始字节、候选文件、diff 和 manifest。Windows 在固定本地卷上持有所有祖先目录的列举／属性句柄、拒绝 reparse point，并禁止目录删除共享；POSIX 通过 no-follow 目录描述符创建和写入。能力不受支持时拒绝导出，不退回路径检查。manifest 明确标注 `patch_only`、`committed=false`、`source_applied=0`、`automatic_application_supported=false`。当前代码没有自动应用分支；只提供 `prepare_source_metadata_batch`。未发布原型的执行入口拒绝调用，旧 `source-metadata:` outbox 意图也会在消费者发布前拒绝，不能借普通整页写入绕过。

兼容的 `--apply` 请求只生成补丁并返回 **退出码3**，表示没有应用；准备成功不算修复完成。`--prepare-patch` 准备成功返回0；过期撤回或导出失败返回非零，并披露已经生成的补丁数量和 manifest 路径。已审核的旧 rollback 文件只可生成来源反向补丁，必须匹配保存的回填后字节指纹；不会覆盖后续正文编辑：

```bash
python cli.py provenance-backfill --revert "<reviewed-rollback.jsonl>" --prepare-patch
```

单批最多32页；每页512 KiB；原始与候选输入各最多4 MiB。canonical 版本经只读 SQLite 读取，每次最多8192行、单行2 MiB、累计32 MiB，并有合作式120秒检查，不承诺原生调用硬取消或进程 RSS 上限。数据库缺失或读取失败会报错，不会初始化数据库或解释成无数据。重复、合并式或无法唯一编辑的 YAML 来源键会拒绝；有效单引号／双引号来源键可生成补丁。只替换经解析验证的单行来源值；多行／块式等未支持布局拒绝，不吞并相邻注释或空白。UTF-8 BOM、正文及非来源头部保持原样。

补丁以读取时的文件／canonical 指纹为前提，生成期间或之后的手改会使其过期；生成完成不证明补丁仍适用于当前文件。不要直接把这些文件复制进 Wiki，也不要用普通整页重写绕过 canonical 历史／绑定保护。后续应用必须在另行核验的受控维护流程下处理；当前版本不提供自动应用能力。

原文身份、事实正确性、来源所有权和写入授权仍由调用方核验。临时补丁保留在原知识库边界内，不进入 Git；不合并节点、不补造关系，也不把磁盘来源戳提升成 canonical admission。

## Notes

* Windows 控制台建议设置 `PYTHONUTF8=1`，避免中文路径或中文输出触发编码问题。
* 长任务由 `filelock` 串行化（`index.json.lock`、`.meta/governance_queue.lock`、`.watchdog.instance.lock`、`<meta>/runtime/ingest_processing.json.lock`、`<meta>/runtime/.runner_service.lock`、`<meta>/runtime/.runner_consumer.lock`）。遇到占用时先确认没有残留的 watchdog / MCP / ingest Runner 进程，再重试，不要直接删锁文件。
* `.gitignore` 排除任务包、临时文件、`scratch/`、构建产物和本机数据。发布前检查暂存清单，不提交数据库、模型 payload、wheel、原生二进制或凭据。
* 摄取核验要区分未派发、排队、模型处理中、处理结论、最终发布和投影完成；入队成功、子代理返回或 `doctor` 的基础依赖通过均不能代替发布验收。旧 `rejected` 回执只记录历史处理结论，保留原记录但不再当作出版或拒收合理性的证明。v10 的完成须核验实际 Source 及其原文绑定；模型返回 rejected、读取失败、格式错误和版本冲突均不属于成功。`standalone` 表示仅发布 Source；页面与投影仍须单独核验。非空来源若记录空内容 MD5（`d41d8cd98f00b204e9800998ecf8427e`），先核验实际字节、UTF-8、来源路径和当前发布内容；无法证明对应当前内容时重新摄取，不直接改台账哈希冒充完成。保留历史失败记录，按当前来源版本去重，避免重置整库队列或重复模型调用。
