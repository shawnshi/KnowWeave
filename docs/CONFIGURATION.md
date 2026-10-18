# 配置与环境参考

先按 [快速开始](../README.md#快速开始) 选择全新演示库或审阅已有部署；不得用示例覆盖现有配置。

## 配置来源、生效时机与凭据

- 数据目录：`VECTOR_LAKE_MEMORY_DIR` → 仓库 `config.json` 的 `memory_dir` → 历史默认 `~/.gemini/MEMORY`。空的 `memory_dir` 不是新建库指令。
- 数据库可被 `VECTOR_LAKE_DB_PATH` 单独覆盖。演示与正式环境均应确认它与所选 MEMORY 一致，不能只核对一个路径。
- 文本后端：`--model-cmd` → `VECTOR_LAKE_RUNNER_MODEL_CMD` → `ingest.backend` → Pi；embedding 的 `GEMINI_API_KEY` 不替代文本 CLI 的认证。
- 普通 `cli.py` 非 `ops-status` 分支会在存在时加载 `~/.gemini/.env`，默认不覆盖已设置环境变量；不会因此自动读取仓库 `.env`。`ops-status` 提前进入显式路径的只读分支。
- MCP、Watchdog、直接 Runner 不经过 CLI 的上述加载分支。任务/GUI 的环境和身份可能不同于终端，凭据由各自宿主环境或所选 CLI 的原生认证提供；不要假定它们继承一次 CLI 加载的环境。
- 配置与环境在进程启动时读取，不热更新；修改磁盘不表示驻留进程已采用。不要为切换后端同时启动两个消费者。
- `--check` 只检查命令与参数能力；认证、模型响应、业务完成需分别验证。直接 Runner/手工监督器默认 shadow，Watchdog 自动 Runner 默认真实写页；shadow 仍写队列与状态。


所有命令和源码路径均相对于仓库根目录；先使用 README 核对过的项目虚拟环境。本文是参考，不是可整段执行的安装脚本。

[返回项目首页](../README.md)

## 配置文件与常用环境变量 (Configuration & Environment)

升级时保留已有配置与数据，不直接复制示例覆盖它们；示例初始化只用于新部署。

```powershell
# 从示例创建配置文件
if (Test-Path 'config.json') { throw '已有配置：先审阅合并，不覆盖' }
Copy-Item config.example.json config.json
```

`config.json` 核心字段说明：

* `ingest.backend`：后台摄取的模型执行 CLI，可选 `pi`、`gemini`、`codex`，默认 `pi`。这是嵌套字段，不是顶层 `backend`，也不是模型名称或 MCP 客户端类型；所选 CLI 需安装在 Runner 所在主机并完成其原生认证，Pi 后端还需 subagent 能力。
* `memory_dir`：自定义 `MEMORY` 根目录；优先级为 `VECTOR_LAKE_MEMORY_DIR`、此字段、宿主历史默认路径。新部署建议显式设置绝对路径，避免误用宿主目录。
* `target_directories`：摄入扫描目录；留空时扫描当前 `MEMORY/raw`，非空时替代默认目录（相对路径基于项目根目录）。
* `exclude_paths`：默认排除 `stocks/`、`garmin/`、`personal-insights/`；配置会覆盖默认列表。另有代码级规则拒绝 `privacy/.../Diary/...` 来源，不能通过配置解除。
* `supported_extensions`：扫描允许的原始资料后缀（默认 `[".md", ".txt"]`）；修改配置不会新增解析器，摄取路径仍须满足格式校验。PDF / JSON 不是当前默认编译入口，需要另行授权的格式转换与文本核验，不能把未解析原文件当作摄取成功。

将以下配置合并到 `config.json`；使用 Gemini 或 Codex 时，将 `pi` 改为 `gemini` 或 `codex`，保留其他字段：

```json
{
  "ingest": {
    "backend": "pi"
  }
}
```

后端选择优先级为 `--model-cmd` > `VECTOR_LAKE_RUNNER_MODEL_CMD` > `ingest.backend` > 默认 Pi。非法配置会报错；真实摄取启动时检查 CLI 能力，不可用时明确退出，不自动切换服务。配置在启动时读取，不热切换；修改后需协调重启并核对实际后端。能力检查、锁和安全切换流程见 [Config](CONFIGURATION.md#config)。

启用 embedding 或宿主编译会将相关文本交给模型服务。先核对扫描目录、排除项及宿主的数据边界；本地文件优先不等于零外联。

Outbox 发布协议使用逐页 `projection_generation` 和独立的租约 owner/token/generation。完成与失败回写必须携带当前未过期的认领回执；仅传 outbox ID 不再可用。A→B→A 重放保留幂等行 ID，但分配新的逐页版本，旧重试不能覆盖新投影。canonical 提交、前台物化、后台物化/索引及手工编辑归一化共用按文件名排序的页面锁；等待页面锁时不持有 SQLite 写事务；调用方不得在外层 SQLite 事务中发布页面。锁归属绑定 PID 与线程，文件名别名不能以不同 canonical 名称指向同一物理页面。现有 payload retention 保留 outbox 行及版本，只清空过期 payload。

已有库由 writer 首次使用 outbox 协议时做加列、索引与旧租约迁移；纯 reader 保留旧快照的只读访问。迁移不得与旧版本 writer 混跑：部署前停止全部旧 writer、保存并验证完整数据库备份，在受控环境升级后统一启动新版本。未知版本的历史 intent 若对应现存 canonical 页面会失败关闭，不自动把旧 payload 绑定到当前页面；恢复需通过受控 canonical mutation 重新入队。canonical callback 可以更新关联账本，但不能改变已准备页面，否则整笔事务回滚。绕过协议的原始 SQL/外部文件写者不受合作式页面锁保护。

Watchdog 的 wiki/raw watcher 与后台循环统一接受线程监督，异常退出有界重启；独立的 `watch-wiki` / `watch-raw` 状态不会被主进程心跳清除，正常停机等待当前替代线程结束。

备份回收只处理具有完成回执、SQLite 完整性检查结果及匹配 SHA-256 的备份。生产备份与回收共享目录锁，维护备份先在 `.partial` 目录构建再发布；旧备份、未完成备份、内容变化或包含额外文件的目录均保留并显示为 `PROTECTED`。至少保留最新的验证通过副本；该验证不等同于跨投影恢复演练。历史备份不会被自动补写回执，受保护文件可能使实际磁盘占用超过回收预算。

**常用环境变量（来源须按入口区分，见本指南开头）：**

|环境变量|作用|推荐值|
|-|-|-|
|`PYTHONUTF8`|强制 Python 运行时使用 UTF-8 编码（Windows 强烈推荐）|`1`|
|`GEMINI_API_KEY`|向量嵌入模型 API Key（Gemini Embedding）；代码从进程环境读取，不从 `config.json` 读取凭据|不在仓库填写|
|`VECTOR_LAKE_MEMORY_DIR`|显式指定 MEMORY 根路径（优先级高于 `config.json`）|例如 `C:/path/to/MEMORY`|
|`VECTOR_LAKE_PAYLOAD_ROOT`|额外允许的任务包 sandbox 根，不覆盖内置的仓库 `brain/` 与宿主根；独立代码部署时保留已有任务包原根，不放宽到整个仓库上级|例如 `C:/path/to/KnowWeave/brain`|
|`VECTOR_LAKE_RUNNER_MODEL_CMD`|覆盖 `ingest.backend` 的模型接缝命令；使用配置文件选择后端时应取消此覆盖|通常不设置|
|`VECTOR_LAKE_RUNNER_SHADOW`|Watchdog 拉起 Runner 时，设为 `1` 跳过模型调用和写页，但仍认领任务、处理重复来源并写运行状态；直接 Runner 默认 shadow，需 `--no-shadow` 才写页|默认 `0`（Watchdog 路径）|
|`VECTOR_LAKE_RUNNER_CONCURRENCY`|摄取 Runner 并发模型调用线程数，也可由 `ingest_runner.py --concurrency` 覆盖|默认 `1`；按宿主容量调整|
|`VECTOR_LAKE_RUNNER_HOLD_SHADOW_LEASE`|设为 `1` 时 shadow 轮不释放已认领的任务包（保留租约供人工检查；默认释放以便下一轮重试）|默认 `0`|
|`VECTOR_LAKE_QUERY_CONTEXT_TTL`|Query 上下文临时文件的过期秒数|默认 `7200` (2小时)|
|`VECTOR_LAKE_VECTOR_SIM_SCALE`|Sum 混合检索模式下向量相似度权重乘数|默认 `15.0`|
|`VECTOR_LAKE_NO_BROWSER`|设为 `1` 时生成 3D 拓扑图后静默不自动弹出浏览器窗口（适合无头/CI环境）|默认 `0`|


## Config

`config.json` 与环境变量共同控制运行范围和模型调用。该文件是机器相关配置，**不入 git**：克隆后执行 `cp config.example.json config.json` 再按本机填写。文件缺失时使用代码内置默认值（含默认排除列表 `exclude_paths`），不会退化为“无排除”。

* `ingest.backend`：后台摄取的模型执行 CLI，只接受 `pi` / `gemini` / `codex`；字段缺失或空对象时兼容默认 `pi`，非法值或未知 ingest 字段明确报错。它不等于连接 MCP 的客户端，所选 CLI 须安装在 Runner 所在主机/容器。
* `target_directories`：raw source 扫描路径。
* `exclude_paths`：排除目录。
* `supported_extensions`：当前启用的输入扩展名。
* `memory_dir`：MEMORY 根目录（机器相关，按安装填写）；可用 `VECTOR_LAKE_MEMORY_DIR` 覆盖。

最小后端配置（合并到本机 `config.json`，不要覆盖其他字段）：

```json
{"ingest": {"backend": "codex"}}
```

```bash
python scripts/ingest_runner.py --check
python scripts/ingest_runner_service.py --check
# 直接检查另一后端，不改本机配置：
python scripts/ingest_model_cli.py --backend gemini --check
```

`--check` 只解析配置/可执行文件及本地 CLI 帮助与安全参数，不领取作业、写运行状态或调用模型；成功不证明认证或模型服务可用。自定义接缝只报告所选命令，不执行不明自检。非 shadow 启动在领取任务前检查内置后端能力；CLI 缺失或参数不支持时退出并报错，不改用其他服务、不消耗来源尝试次数。

Codex/Gemini 接缝使用原始素材和 dispatch snapshot，不继承 Pi 的 subagent 机制。适配器将工作目录设为任务临时目录，并配置禁用执行/文件工具、扩展、用户 MCP、hooks 与额外上下文读取；Codex 请求只读 sandbox 与 ephemeral 会话，Gemini 使用临时系统设置、空 core tools 和 deny-all policy。这些是 CLI 级限制，不是 OS 隔离，也不以本地参数检查替代实机效果验证。结果只经现有 `finalize_ingest` 入库；临时输出/设置结束后清理，不复制凭证，认证仍由 CLI 自身完成。处理来源前需确认数据与模型服务授权，配置字段不能替代授权。

Codex 要求 `exec` 的输出 schema/最终消息/ephemeral 参数及工具禁用 feature gates；Gemini 要求 `--output-format` / `--extensions` / `--policy`。不支持这些能力的版本明确拒绝启动。CLI 探针仅核验本地参数，后端权限和供应商模型的实机行为仍需用脱敏样本独立验收。

配置在启动时读取，不热切换。状态记录 `effective_backend` 和 `selection_source`；Runner 另记 `resolved_model_command`，保留旧 `model_command` 布尔字段。`.runner_service.lock` 是所有入口的 root 级所有权门：直接 Runner 持有它，监督器只允许自己的实际子进程通过已占用的门；每个模型消费者还须持有 `.runner_consumer.lock`。冲突方不领取任务、不覆盖赢家 PID/状态，输出 requested/effective backend 和 `configuration_mismatch`；直接 Runner 冲突返回 `4`，监督器保留既有重复启动返回 `0` 的协议。未知/损坏旧记录报告 unknown，不声称新配置生效。仍在运行的旧无锁 Runner 会由其 PID 记录阻止并行启动；不会自动删除 PID 文件或终止旧进程。状态路径在写入时解析，避免导入时缓存跨越 root 隔离边界。

切换时先让在途任务完成，协调停止当前 root 的 Runner/监督器及其自动看护，避免 watchdog 按旧启动计划重新拉起。修改配置后重新启动相应入口，核对新 PID、`effective_backend`、`selection_source` 与心跳；命令行或环境变量覆盖会优先于配置文件。更新 watchdog 的后端选择代码时，还需重启 watchdog；仅重启其子进程不会更新父进程已加载的代码。不会自动杀掉其他客户端的进程，生产切换/重启需独立授权。

* 入账与去重：`processed_files` 记 `(路径, 内容哈希)`，表示来源已被处理，不保证存在 Source 页面。契约升级不会重跑未改字节的历史来源；兜底扫描也不是历史缺页补齐。历史 `integration.disposition=rejected` 记录保留，但不能证明当前内容已经出版，也不能关闭失败重编；v10 不再产生成功拒收。去重要求实际 Source 页的精确原文路径和当前字节摘要，不能只依赖 ledger、名称相似或旧拒收理由；历史 `operator_trust_hash_baseline_upgrade` 的 `completed` 记录只证明操作者接受了哈希基线，不是模型摄入或出版回执。核查缺页时应绑定当前内容摘要及对应的完成 / 拒收回执；证据缺失保留为待核验，不直接清空账本或批量重摄入。
* 出版溯源：实际出版时在**规范 Source 页面**的 frontmatter 写入 `source_hash`，记录编译所用内容。已发布但缺账目行的源由扫描按证据补齐；有 `source_hash` 则比对哈希，无则仅按页面 `created` 与文件 mtime 作历史日期推断，不能证明字节一致。证据显示文件已变时**不补行**，而是让它重新摄入，避免修改被静默丢弃。
* `VECTOR_LAKE_DB_PATH`：覆盖 SQLite 数据库路径（默认 `<MEMORY>/wiki/.meta/vector_lake.db`）。
* `VECTOR_LAKE_PAYLOAD_ROOT` / `VECTOR_LAKE_PAYLOAD_MAX_BYTES`：MCP `payload_file` 沙箱的可读根与单文件字节上限（默认 5 MiB）。
* `VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE=1`：跳过写入前健康门（仅用于受控维护，不建议常开）。
* `VECTOR_LAKE_EMBEDDING_RPM` / `VECTOR_LAKE_EMBEDDING_TPM`：embedding 调度限额，默认分别为 `3000` 和 `1000000`。
* `VECTOR_LAKE_EMBEDDING_UTILIZATION`：安全水位，默认 `0.8`，即按 2400 RPM / 800k TPM 调度。
* `VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS` / `VECTOR_LAKE_EMBEDDING_MAX_BATCH_TOKENS`：单批条数与 token 上限，默认 `100` / `200000`。
* `VECTOR_LAKE_EMBEDDING_TIMEOUT_MS`：单次 embedding HTTP 超时，默认 `30000` 毫秒，三种传输都按此配置。带 caller budget 时，每次请求只使用剩余预算与该配置的较小值，不再将 REST 的小预算抬至 1 秒。SDK 内部重试设为一次尝试，由调度器统一负责配额分类及有界重试。
* `embed_texts(..., budget_seconds=...)` 从输入准备及 client 获取前建立单一 monotonic deadline；限流、请求、重试和响应验证共享它。逾期结果不会返回给调用方／写入向量，过期后也不启动新的 provider attempt。它是**协作式 deadline，不是硬墙钟取消**：同步 SDK 初始化、client／SQLite 锁等待、DNS 和持续传输下的 socket inactivity timeout 仍可能超过总预算，结束后才会检查并拒收；不创建无法终止的后台 timeout worker。backfill 的预算仍按 batch 计算，不包含批次前 setup、批次后的数据库发布及整轮多个 batch；不据此承诺整轮或 MCP 请求的硬上限。
* `VECTOR_LAKE_EMBEDDING_TRANSPORT`：默认 `rest`（urllib），可显式选择 `rest-httpx`（懒导入 HTTPX 的连接池）或 `sdk`（`google-genai`）；错误不会自动换 backend。模型、维度、调度重试与配额算法不变，默认 REST 不导入 SDK。HTTPX 缺包和导入损坏分别报错，不自动安装。
* `rest-httpx` 使用 urllib 的代理发现／旁路决策、显式代理、`trust_env=False` 和 `ssl.create_default_context()`；不兼容代理在发送前报错，不静默直连。每 origin/代理池最多 8 个连接、8 个 keepalive、空闲 5 秒，HTTP/1.1；不跟随重定向、不保留响应 cookie、压缩协商为 `identity`，Key 仅在当前请求头中设置。TLS 信任内容变化需显式 reset；真实代理／TLS／提供方仍需部署环境单独验收。
* SDK 与 HTTPX 自有同步 client 按租约借用，每 backend 最多两代（含构造、退役、关闭及关闭失败）；活跃租约最后归还时才关闭。`reset_client_cache()` 退役旧代，不取消请求、不清空 query cache；SDK factory／基础 timeout 自动失效，其他认证／端点／代理配置需配置所有者显式 reset。fork 后拒绝复用继承 manager，请使用 spawn／全新进程。
* MCP CLI 正常／异常退出均调用 `shutdown_embedding_clients()`：禁止新借用，闲置立即关闭、在途 drain；不承诺 SDK `.aio` 资源所有权。shutdown 后普通 reset／预热不能重新开启；测试或嵌入宿主可在停止旧工作后明确 `reset_client_cache(reopen=True)`。关闭失败仅记录异常类别，不冒充成功释放。
* `VECTOR_LAKE_EMBEDDING_PREWARM=off`：关闭 MCP 的 SDK 构造预热；仅在受限 API-key 模式启动，不预热 Vertex／ADC／enterprise／自定义 factory／端点／debug-replay 配置，但不限制其按需认证路径。不新增 REST 网络预热；HTTPX 冷启动仍有导入／TLS 构造成本，构建池不等于已建立连接。获取资源等待受剩余 budget 或配置 timeout 限制；同步构造／关闭不能硬墙钟取消，超时也不证明远端未执行或配额未消耗。
* `VECTOR_LAKE_TOKENIZER`：目前只有一个合法值 `rjieba`（保留该开关是为了让已有的环境配置显式表达意图）；写别的值会告警并走自动选择。安装了 rjieba 就用它，反之分词为 `unavailable`（CJK 全文匹配下降，doctor 会报出）。
* `VECTOR_LAKE_OUTBOX_MAX_BACKLOG`：outbox 待处理行数阈值，默认 `2000`。**超出后默认只计为降级告警，不阻断写入**（阻断积压等于掉断唯一的自愈路径）；设 `VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING=1` 才升级为阻断。
* `VECTOR_LAKE_OUTBOX_FAILURE_NONBLOCKING=1`：把 `mutation_outbox_failed` 从硬故障降为降级告警。默认关闭，因为失败行会阻塞 canonical 写入；打开等于用可用性换安全性。
* `VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING=1` / `VECTOR_LAKE_TIMELINE_PARITY_BLOCKING=1`：把终态失败作业与时间线 parity 漂移从降级升级为阻断写入的诊断用闸门，默认关闭。
* `VECTOR_LAKE_BACKUP_KEEP` / `VECTOR_LAKE_BACKUP_MAX_BYTES`：`backup-retention` 的默认保留份数（`3`）与字节预算（`12 GiB`）。
* `VECTOR_LAKE_RUNNER_EXPECTED=0`：不再把缺失的摄取 Runner 报为告警；用于不需要摄取的主机。
* `VECTOR_LAKE_RUNNER_AUTOSTART=0`：不让 `watchdog_sync.py` 拉起并看护摄取 Runner（保留 `runner_absent` 告警，用于手工管理 Runner 的主机）。默认开启。
* `VECTOR_LAKE_RUNNER_MODEL_CMD`：显式覆盖模型接缝命令。自动启动、手动监督器和直接 Runner 共用 `--model-cmd` > 此环境变量 > `config.json` 的 `ingest.backend` > 默认 Pi 的解析顺序；空白覆盖值忽略，非法配置仍报错。自定义命令维持原有 shell/stdin/stdout 协议，内置接缝按 Python argv 启动。
* `VECTOR_LAKE_RUNNER_PI_BIN` / `VECTOR_LAKE_RUNNER_GEMINI_BIN` / `VECTOR_LAKE_RUNNER_CODEX_BIN`：对应 CLI 的可执行文件或 PATH 名称，默认 `pi` / `gemini` / `codex`。Pi 保留托管安装路径的后备解析；显式不可用的二进制不回退到其他安装或服务。`VECTOR_LAKE_RUNNER_SUBAGENT_AGENT` 仅适用于 Pi，默认项目级 `vector-lake-ingestor`（`.pi/agents/vector-lake-ingestor.md`，仅 `read` 工具、fresh 上下文，不继承全局/项目材料或技能）；默认 profile 缺失时拒绝替换为 reviewer。显式代理覆盖仍受所选代理自己的权限与输出契约约束。Pi 接缝从仓库根启动，原生任务包/模型结果验证与 `finalize_ingest` 不变。各接缝共用 `VECTOR_LAKE_RUNNER_MODEL_TIMEOUT`，Runner 另读 `VECTOR_LAKE_RUNNER_COOLDOWN`。
* `VECTOR_LAKE_RUNNER_REPAIR_ATTEMPTS`：可修正的输出/格式错误在 `finalize_ingest` 拒绝后最多修正的轮数（默认 `2`；`0` 关闭）。各轮共享同一模型截止时间，不另领超时预算；源变化、过期版本或租约冲突不进入模型格式修正，须重新派发。缺失版本字段与真正的陈旧版本不同，仍可由模型修正。重试不能替代校验。
* `VECTOR_LAKE_RUNNER_SHADOW=1`：让自动拉起的 Runner 不写 Wiki 页面，默认关闭。直接运行 `scripts/ingest_runner.py` 默认 shadow，使用 `--no-shadow` 才写页；shadow 跳过模型调用，但仍会认领任务、处理重复来源并记录运行状态，不是零副作用模式。
* `VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS`：守护进程的周期性兜底间隔，单位是**秒**，不是启用开关。部署示例采用 `600` 秒（10 分钟）；未设置时应用缺省为 `900` 秒，`0` 关闭，`1` 表示每轮结束后仅等待 1 秒。兜底做三件事：按扫描规则准备尚未处理或内容已变更的 raw、把超过时限的陈旧摄取任务作废、以及按批次重建缺失或输入已变的向量。它不把所有“缺少 Source 页面”的已处理记录自动重摄入；历史拒收、信任基线和校验隔离不自动改写；恢复旧拒收使用显式受控重编清单，不清空队列或手改哈希。
* `VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH`：兜底每轮最多重新嵌入多少个节点（默认 `200`；`0` 关闭该半部）。增量索引在页面变更时会作废其向量且按契约不调用 embedding API，兜底是把这个失效补回来的自动对应物；批大小的上限保证循环不被长时间占用。
* `VECTOR_LAKE_CATCHUP_EMBEDDING_BUDGET_SECONDS`：兜底单个 embedding 批次允许等待的上限（默认 `120` 秒，含限流窗口与重试）。超出被记为失败批并留给下一轮，而不是占住 900 秒的节拍——配额错误每次重试固定 sleep 60 秒，不设上限时两个批次就能吃掉整轮。
* `VECTOR_LAKE_STALE_TASK_MAX_AGE_SECONDS`：兜底把多旧的摄取任务视为陈旧（默认 `86400` 秒）。
* `VECTOR_LAKE_RUNNER_STALE_SECONDS`：Runner / 监督器心跳过期阈值，默认 `2400` 秒。
* `VECTOR_LAKE_RUNNER_STRICT=1`：把 Runner 告警从 `warnings` 升入 `degraded`（两者都不阻断写入）。
* `VECTOR_LAKE_FTS`：词法检索后端，默认 `fts5`；`tantivy` 使用派生镜像。两者接收项目分词器的预切词串，并沿用负数 rank 约定。页面检索的提取分两档：先用去掉停用词（`的`/`在`/`与`/`对`/`the`/`of` 等）后的全部限定词做 AND，只要它返回任何命中即为答案；仅当 AND 命中为零时，才用其中词频较低的一半限定词做 OR 补位（bm25 排序），Bm25 仍决定顺序，且不引入只命中单个限定词的页作为“答案”。这样 `电子病历系统应用水平分级评价六级` 这类自然语言长查询从 0 条变为可答，而 `大模型 部署` 仍然不会召回只命中 `部署` 的页。未填满召回深度时，最多再执行 8 条保留其他限定词的词典替换查询，去重后补位（sum 模式的补充 BM25 贡献减半；RRF 中补充项排在原始命中之后）。FTS5 仍是权威投影，dirty 或无效镜像回退 SQL。可用 `python cli.py projections --reconcile --only tantivy_mirror --apply` 请求有界重建；deferred 不代表完成，后续由现有 watchdog 续接。具体持久队列、读门禁、资源界限与回退见[运维指南的 Tantivy 写入约束](OPERATIONS.md#tantivy-写入约束)。切换后端前需验证相关性，不能仅凭吞吐量替换默认值。
* `VECTOR_LAKE_FALLBACK_REPARSE_SECONDS`：投影落后于 `index.json` 时，页面目录回落解析该文件的限频窗口，默认 `0`（每次戳变化都重新解析，即“文件优先”的既有契约）。设为正数后，窗口内继续用上一次解析结果作答并在检索备注里注明是哪一次归档、已过期多久，避免写入活跃期每次重写 `index.json` 都让下一个请求付整文件解析（实测 12.7 MB / 408 ms）。代价是窗口内新建/改名的页最迟一个窗口后才可见，故不默认开启。
* `VECTOR_LAKE_CLAIM_BLOCKS`：claim 块提取器，默认 `rust`（`vector_lake_core.fast_extract_blocks`）；`python` 使用 mistune。含 NUL 或 U+FFFD 的正文走 mistune，避免解析器差异改变 claim 文本与标识。
* `VECTOR_LAKE_RERANK_WEIGHT`：同池重排权重，默认 `0.4`，混合上游归一化分与 Rust BM25 分；`0` 禁用重排。缺少 `fast_bm25_rerank` 时保留上游顺序并告警，没有 Python 重排回退。重排不增加候选，也不等于经过人工相关性验收。
* `VECTOR_LAKE_ENTITY_NAME_PRIORITY`：是否优先提升被查询点名的实体页，默认 `0`；开启后按实体名在查询中的位置分层，层内按混合分排序。点名某实体不一定意味着想要它的主体页，故不默认开启。
* `VECTOR_LAKE_FUSION`：FTS 与向量融合方式，默认 `sum`（词法分与按 `VECTOR_LAKE_VECTOR_SIM_SCALE` 缩放的向量分相加）；`rrf` 按名次融合，并使图扩展使用相同的名次量纲。切换会改变排序，不把代理回放或模型判定等同于人工质量验收。
* `VECTOR_LAKE_VECTOR_INDEX`：默认 `auto`。影子自检一致且 NumPy 可用时优先 RAM float32 全扫描候选＋SQL L2 精排；`ram` 同一路径。缺包、容量/活动视图耗尽、旧代次仍被引用或借用事务时，`auto` / `ram` 回退精确 `vec_embeddings` SQL，不再自动切到二值近似。只有显式 `two_stage` 选择二值预筛；`legacy` 直接使用原始精确臂。并列边界仍不承诺所有实现完全同序。缓存最多一份活矩阵代次（矩阵 128 MiB，另有键/范数/复核缓冲开销）、8 个连接绑定和 4 个活动视图；旧租约继续有效，但旧代次引用释放前不分配新矩阵。未安装显式加速器时仍按源字节复核；安装 `retrieval-indexes` 后用覆盖 float 表原始 INSERT/UPDATE/DELETE 的事务内计数区分向量修改与无关写入。模型/维度或 Schema 变化仍失效，不把 tag、计数或缓存当作授权。
* `VECTOR_LAKE_METADATA_FIRST`：过滤态默认 `1`，RAM/二值候选先过滤、通过者再精排；`0` 恢复先精排后过滤。普通选择器只读取 `domain` / `topic_cluster` / `status`，`filter_expr` 使用完整节点。首次扩容最多预取4096个候选，后续复用查询内前缀与过滤判定；精排仍走原SQL。RAM使用确定性的距离/page_key前缀，二值仅在已验证的 `sqlite-vec v0.1.9` 上预取；前缀不一致时逐轮读取。来源代次、Schema、请求模式或实际候选后端变化会清空复用；借用的事务每轮清空，避免保存点回滚复用未提交判定。当前前缀长度用于耗尽判断，预取池大小不冒充已检查量；触顶时仍报告结果可能不完整。该取值顺序可能与旧臂产生不同候选和排序。
* `VECTOR_LAKE_NUMERIC_THREADS`：MCP、Watchdog 和 Runner 在导入数值库前应用的进程级线程预设，默认 `1`；`0` 关闭预设，允许值为 0–64。已有 `OPENBLAS_NUM_THREADS`、`OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`NUMEXPR_NUM_THREADS` 设置优先。不修改系统或用户环境变量。部署后仍须验证实际检索吞吐，不把提交内存下降等同于常驻 RAM 节省。
* `VECTOR_LAKE_MEMORY_GRAM_MAX_BASE_AGE_SECONDS`：脏 gram 基线的最大年龄，默认 `3600` 秒；`0` 关闭年龄触发。基线存在脏文档且距上次重建达到此值时判为到期，干净索引不会按年龄重建。它是基线年龄上限，不是首个脏文档的精确计时；重建仍由后台维护执行，不在查询路径阻塞重建。
* `VECTOR_LAKE_RUNNER_MODEL_TIMEOUT`：同一任务初次模型调用及全部修正轮次的共享时间预算，默认 `900` 秒、上限 `3300` 秒；必须是有限正数。实际预算还受当前租约剩余时间约束，预留 `120` 秒用于提交；窗口不足时不调用模型，留待租约恢复。预留只是余量，不保证长时间锁竞争下仍能提交。
* `VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC`：宿主传给模型接缝的内部单调时钟截止时间，不手工配置。
* `VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES`：脏 memory n-gram 索引按搜索次数触发重建的阈值。代码默认 `0`（关闭此触发）；N>0 时，索引存在脏文档且重建后搜索次数达到 N，`gram-index --if-due` 判为到期，不改变脏索引不能服务的精确性门。托管启动脚本 `scripts/watchdog_service.ps1` 在未设置时导出 `20`，显式 `0` 或其他值保留；普通 shell 不继承该脚本默认值。到期判定不等于重建已完成，需要守护进程或手工执行维护。
* `VECTOR_LAKE_EXPANSION_QUOTA`：给图扩展预留候选池槽位。不设时只使用融合后剩余槽位；设 N 时预留最多 `min(N, expansion_limit)` 个槽位，`expansion_limit` 为 general 5 / entity 12。预留不保证图中存在足够的合格候选。
* `VECTOR_LAKE_AUTHOR_SOURCES`：作者关联的 raw 路径前缀，逗号分隔，默认 `raw/article`。应按自己的资料目录约定配置；路径前缀不是作者实名证明。关联集合取最新成功摄入台账映射与页面 `sources` 溯源的并集，因此包含符合前缀的 Source 页及关联的 Concept / Event / Synthesis 等编译页。缓存随台账、前缀及 `page_index_state` 变化失效，不从标题或正文提及猜归属。
* `VECTOR_LAKE_AUTHOR_FACET`：代码默认 `off`（不应用关联偏好）；`boost` 为关联页加上 `VECTOR_LAKE_AUTHOR_BOOST` × 池内最高分（系数默认 `0.25`），不是分数倍率；`filter` 只保留关联页。混合来源编译页只要有一个来源命中就会关联，不能据此认定整页原创或所有结论都是作者立场。全局策略用于 `legacy`；非作者 profile 不继承默认作者抬升，显式 `author` profile 启用作者偏好。
仓库的 `.mcp.json` 与 `mcp_config.json` 显式配置 `boost`；自建宿主使用代码默认值时为 `off`。宿主若缓存注册环境，修改文件或仅重连子进程未必生效，应重载宿主并核对实际进程环境。
* `VECTOR_LAKE_AUTHOR_ANNOTATE`：默认 `1`；facet 启用时可在查询包页标题后标注 `[author]`。标注表示符合来源关联规则，不是整页作者认证，也不改变候选集合。
* `VECTOR_LAKE_CANDIDATE_DEPTH`：每条召回路径进入融合的候选深度，默认 `25`，不随 `top_k` 变化；过滤召回可有界扩容至 4096。触顶告警保留，列出 `raw_examined`（实际检查前缀）、`target_depth`、`selected` 及请求的过滤条件；元数据优先臂还列出 `metadata_passed`、`similarity_passed` 与相似度阈值。达到 `top_k` 不代表已达到召回目标，低相似度也可导致触顶，告警不等同于索引损坏。
* 检索 `domain` 按主题精确过滤：医疗 IT 用 `Medical_IT`，政策治理用 `Policy_and_Governance`；`General` 仅是具体类别，不表示全库。`Healthcare_IT` 可映射至 `Medical_IT`，`HIT` / `Clinical` 不是当前别名。任务涉及多个域时分别在获准范围内查询，不通过清空域过滤读取无关私人资料。
* 检索备注分级：作者关联 `boost` / `filter`、正常的过滤零匹配及请求主动关闭外部嵌入为 `[INFO]`；召回触顶、后端失败、投影降级及未分类备注仍保留 `[DEGRADED]`。FTS / 本地重排异常会返回阶段及异常类型，不回显可能含查询内容的异常参数。
* 检索范围与出站：MCP `search_vector_lake`、`preview_query_context`、`query_logic_lake` 以及 CLI `search` / `query` 要求显式 `domain`。范围贯穿页面、记忆及上下文摘要；有域的聚合上下文不自动加载全局策略文本。`allow_external_embedding` 默认为 `False`（CLI：`--allow-external-embedding`），只有当前授权允许发送该查询时才开启。范围参数和许可标志不构成身份认证、ACL 或物理网络隔离。
* 记忆检索：先按明确的域/主题簇过滤，再评分和截断；缺失记忆域时仅通过可解析的 Source 页元数据确定范围，不从路径子串猜测，也不在错误时回退全库。此路径使用有界任务范围内的精确扫描，不声明具有 gram 路径的延迟。记忆视图只在同次上下文组装内复用，作者关联缓存最多保留 8 个代次。
* 向量与证据：查询向量在入缓存前归一化；提供方响应拒绝零向量与非有限值。优势/不足类问题可从同一已召回页面提取两个独立定位窗口，总长度仍受 2500 字符限制。页面嵌入仍为前 15000 字符；可选分块索引已提供，但不自动构建、不默认融合，也不自动重建历史向量。

SQL-backed 词法节点在可选向量工作前完成物化。到期后上下文不再读取新正文，只能使用已取得且标明 `index-summary` 的有界摘要；UTF-8 序列化省略页面时，末态诊断和输出证据键同步缩减。

同名但 DDL 不兼容的托管检索索引或触发器不报可用；显式维护拒绝覆盖并回滚。正常查询不创建或修复它们。
* 检索台账：除查询摘要哈希外，记录选择器、主索引及分块侧库代次摘要、候选深度/池大小、外部嵌入许可与阶段耗时；策略仅记录名称、是否请求年份与改写数量，不记录实体锚点或原始年份。分块命中另记录源文 SHA-256。日志不新增查询原文或过滤表达式正文存储。
* 请求边界：查询长度 1–4096 字符，公开 top_k 为 1–50，上下文上限保留 200000 字符。请求合作式预算为 15 秒，主库及分块 SQL 通过 progress handler 中断；文件系统、原生调用和客户端传输不是物理硬截止。正文单页最多读取 512 KiB、单请求累计 4 MiB，超过预算保留可用摘要/独立结果并标记降级，不把输出字符数当作 I/O 上界。
* 完成状态：台账记录 `complete` / `partial` / `failed`，端点耗时在正文与上下文处理后记录（不含传输序列化及观察器自身追加耗时）。失败的空结果不再宣称 `No matching pages`。上下文字典附 `retrieval_status` / `retrieval_diagnostics`。异常只暴露类型及数值 SQLite 错误码，不转发 SDK/SQL 异常正文。台账按进程锁及跨进程文件锁串行化轮转/追加；锁预算超限仍 fail-open，`observation_status()` 报告跳过条数。
* 显式加速器：CLI `retrieval-indexes --db <explicit-existing-db>` 只读预览，另加 `--apply` 才在事务内创建 scoped-memory 表达式索引与向量代次触发器；查询不创建、不迁移。维护预算默认 60 秒、最高 120 秒；失败回滚。应用前备份，生产应用需单独授权及审查。缺失时继续正确的精确扫描。完整字节验证的旧路径仍为 O(行数×维度)。
* Scoped memory：同一 SQLite 读快照内先流式计算整个请求作用域的 DF，再流式评分；active/history 窗口共享这两遍读取，各自只保留 TopK，不先截断候选或改用全局 DF。合计保留上限仍为 10000 条 / 16 MiB 原始 JSON payload；单条超限通过 SQL 长度投影拒绝传入 Python。两遍合计扫描预算为 200000 条 / 256 MiB，并继承公开端点的 15 秒合作式期限；预算不足明确失败，不返回假完整排名。字节计账不是 Python 对象或进程 RSS 的物理硬上限，原生 SQLite 工作也不承诺硬取消。自有读事务与 `query_only` 标志会恢复，调用方事务不提交、不回滚。非有限记忆分数在此路径显式报数据错误，不改写存量记录或未限定范围的旧评分路径。`memory_scope` 在请求内统计扫描遍数、行数、字节数与保留量；当前台账仅保存代次摘要哈希，不持久化这组原始计数或正文。字段覆盖取决于入口；缺失的配置字段表示未记录，不能据此推断实际配置。
* `profile`：公开页面/上下文接口默认 `auto`。`legacy` 保留原排序；其他策略取消全局 Source 数量限制、降权与作者偏好，`author` 单独启用作者关联 boost，`source` 优先 Source 类型，`entity` 优先完整标题/别名。`analysis` 为同时含正反方面的问题增加有界实体补充查询。显式会议年份约束下推到候选与图节点查找；未知年份被标记，多年份不猜单个年份。所有这些都是检索偏好，不是事实或作者身份认证。
* `use_chunks`：默认 `False`；只读取已有 `.meta/retrieval_chunks.sqlite3`。启用后以 RRF 融合主检索与分块路径，按当前源元数据重新验证选择器，并比对源文哈希；不自动创建或修复侧库。文件缺失、模型不符、过期源文均显式报告。原始页面 15000 字符嵌入不修改，分块是独立补充路径。
* CLI `chunk-index`：必须给出一个域和 1–8 个显式页面。默认只预览元数据，`--apply` 才写侧库，`--allow-external-embedding` 单独许可正文外发。单页最多 512 KiB / 512 块，默认块长 2000 字符、重叠 150 字符，标题/层级上下文分别限制在 512/1024 字符；分块向量限 1–4096 维。每页替换事务原子，整批不原子；预算/后续页失败时早先完整提交保留。总预算默认 60 秒、最大 120 秒，为合作式检查，不是物理硬截止。WAL 仅用于该侧库，不改主数据库。
* CLI `retrieval-eval`：离线读取显式标签和结果文件，单文件最多 8 MiB、数据集最多 1000 条。报告 judgment-relative Recall、MRR、nDCG、已判定比例、方面文档覆盖及有明确答案标记时的拒答正确率，并附输入摘要。随附 fixture 为合成标签；未审核的人工作业拒绝评测。功能测试及合成指标不替代人工相关性标注或生产并发/SLO 验收。
* `VECTOR_LAKE_CANDIDATE_POOL`：重排候选池规模，默认 `40`；仅 `legacy` 的池内来源类上限为 `int(pool * 0.6)`。它与候选深度均不随 `top_k` 缩放，`top_k` 只决定最终窗口大小；候选不足时可能返回少于请求数量。
* `VECTOR_LAKE_SOURCE_RANK_PENALTY`：`legacy` 的来源类页最终得分倍率，默认 `0.6`；其他 profile 不施加这项全局降权。这是排序偏好，不是过滤；`1.0` 关闭偏好，`0.0` 压至零分但仍保留候选。
* `VECTOR_LAKE_LEIDEN_L1_RESOLUTION` / `VECTOR_LAKE_LEIDEN_L0_RESOLUTION`：Leiden 的 Micro / Global 分辨率，默认 `2.0` / `1.0`。分辨率越高社区越小。
* `VECTOR_LAKE_LEIDEN_SEED`：Leiden 随机种子，默认 `42`。**必须固定**才能保证社区划分可复现。
* 所有进程通过 SQLite 滚动窗口共享 RPM/TPM 预算；索引重建和增量索引不调用 embedding API，内容变更后的旧向量先作废、再由周期兜底（`VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH`）按批重建，需要一次性全量重建时用显式 `embedding-backfill`。

其余开关（凡 `vector_lake/` 里出现的 `VECTOR_LAKE_*` 字面量都在此登记；由测试守往）：

* `VECTOR_LAKE_EMBEDDING_MODEL`：embedding 模型名（默认见 `embedding_scheduler.DEFAULT_MODEL`）。
* `VECTOR_LAKE_EMBEDDING_DIMENSION`：向量维度（默认见 `DEFAULT_DIMENSION`）。
* `VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM`：单条目字符上限，默认 `15000`。
* `VECTOR_LAKE_EMBEDDING_MAX_TOKENS_PER_ITEM`：单条目 token 上限，默认 `7500`。
* `VECTOR_LAKE_EMBEDDING_MAX_RETRIES`：单次重试上限，默认 `5`。
* `VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES`：连续失败批次上限，默认 `3`。
* `VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS`：embedding run 的租约过期时间，默认 `3600`（下限 `60`）秒。
* `VECTOR_LAKE_MEMORY_SEARCH`：运行态记忆检索后端，默认 `gram`（精确 n-gram 倒排）；其他取值回退到投影扫描；`legacy` 强制旧路径。受域检索（`domain=`）不枚举整域 payload：域范围由 `operational_memory_index.memory_domain` 与 `page_key`→`page_index_nodes.node_key` 两段合取，语料 DF 与逐文档相关性来自倒排，胜出条目再交给 `score_memory_items` 精确重排，因此与 payload 扫描同解、只差开销（活体实测 Medical_IT 30 613 行：177 ms 对 857 ms）。三个前提任一不成立时自动回退 payload 扫描且不加缓存：加速器索引不存在、gram 基座不可用（有 dirty 文档或未建）、或指定了 `cluster=`（簇的“自有 `$.topic_cluster` 优先”这一半尚无索引列）。
* `VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE`：设值（非空）时忽略已完成的定时 lint 标记，用于强制重跑一次。
* `VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS`：outbox 最老待处理行的年龄阈值，默认 `300` 秒。
* `VECTOR_LAKE_WRITE_LOCK_CONTENTION_WINDOW_SECONDS`：判定写锁争用是否仍属「当前」的滑动窗口，默认 `600` 秒。
* `VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS`：等待中 subagent 作业的条数阈值，默认 `500`。
* `VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS`：等待中 subagent 作业的年龄阈值，默认 `86400` 秒。
* `VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING=1`：把 subagent 积压从降级告警升级为阻断写入。默认关闭。
* `VECTOR_LAKE_SUBAGENT_RUN_ID`：本进程作为 outbox / job 租约持有者的标识；不设置时用 `hostname:pid`。
* `VECTOR_LAKE_SEARCH_LEDGER=0`：关闭检索台账，默认开启。台账保存查询哈希与字符数、返回页键、召回来源、耗时及降级说明，不保存查询原文。位于 `<meta>/runtime/search_ledger.jsonl`，达 2 MiB 时保留一代轮转；并发 append/轮转串行化；写入失败不替代检索结果，跳过写入可通过 `observation_status()` 计数观察。末态在证据和上下文序列化完成后记录 `complete`、`partial` 或 `failed`，不包含传输序列化耗时。
* `VECTOR_LAKE_CORE_VERSION`：指定本进程使用哪个已安装的原生核心构建（不设则用 `vector_lake_core/_active_version.txt`，即 `scripts/install_core.py` 最后激活的那个）。用于灰度/回滚：一个进程可以钉在旧构建上，而其他进程已用新构建。
* `VECTOR_LAKE_OUTBOX_PAYLOAD_KEEP_DAYS`：已完成 outbox 载荷保留天数，默认 `30`，`0` 表示下次维护清理。只清 `payload_text`，不删除用于幂等去重与终态复用的业务行。
* `VECTOR_LAKE_RECLAIM_FREE_SPACE=1`：允许维护执行 `VACUUM`，默认关闭、只报告。未启用自动回收的 SQLite 实例删行后只增加 freelist；`VACUUM` 要重写文件并占用写锁，启用前需预留磁盘空间、恢复点和维护窗口，不从旧实例的可回收字节数推断当前收益。
* Ingest 完成必须提交领取阶段返回的 `job_id`、`lease_owner`、`lease_token` 和 `lease_generation`；过期 Worker 的结果会被最终 CAS 拒绝。


## MCP 客户端接入 (MCP Client Configuration)

知序（KnowWeave）以标准 Model Context Protocol (MCP) 向宿主（Pi、Claude Desktop、Cursor 等）暴露 20 个工具；准确注册面由 `mcp_server.py` 与 `tests/test_command_surface.py` 核验。

**在客户端配置文件（如 `claude_desktop_config.json` 或 `.mcp.json`）中添加：**

```json
{
  "mcpServers": {
    "Reson-KnowWeave-mcp": {
      "command": "C:/path/to/KnowWeave/.venv/Scripts/python.exe",
      "args": [
        "-m",
        "vector_lake.mcp_server"
      ],
      "env": {
        "PYTHONPATH": "C:/path/to/KnowWeave",
        "PYTHONUTF8": "1",
        "VECTOR_LAKE_MEMORY_DIR": "C:/path/to/MEMORY",
        "VECTOR_LAKE_PAYLOAD_ROOT": "C:/path/to/KnowWeave/brain"
      }
    }
  }
}
```

`PYTHONPATH` 使用仓库的绝对路径，`VECTOR_LAKE_MEMORY_DIR` 与 `VECTOR_LAKE_PAYLOAD_ROOT` 分别绑定实际 MEMORY 和额外允许的任务包根目录；按部署位置修改示例路径，避免依赖客户端工作目录或宿主默认值。需要向量嵌入时，由启动环境或客户端的私有配置提供 `GEMINI_API_KEY`，不要把真实密钥写入共享示例。

Pi 的文件配置位于 `~/.pi/agent/mcp.json`；其他客户端使用各自的配置入口。在具备 `tool_search` 的 Pi 会话中，可以在该 server 项增加：

```json
{
  "toolExposure": {
    "query_logic_lake": "deferred",
    "finalize_query_synthesis": "deferred"
  }
}
```

这是 Pi 的工具暴露配置，不应直接复制到其他 MCP 客户端。修改 Pi 的 MCP 配置后，使用 `/reload` 重新加载配置；服务代码变更后，常驻 Python 进程仍需重连或重启，可用 `/mcp reconnect Reson-KnowWeave-mcp`。在实际连接上调用 `runtime_identity`，核对 PID、加载路径与选定函数身份；`doctor` 的新进程检查、配置文件修改或独立临时客户端都不能替代实际宿主连接验收。
