# 知序 · KnowWeave

**让知识可追溯，让记忆可延续。**

*Traceable knowledge. Lasting context.*

知序（KnowWeave）是一个本地文件优先的知识编译器：把原始材料持续编译成可审阅、可追溯的 Markdown wiki，并为 Agent 提供可调用的事实、偏好、决策和任务状态记忆。它不是传统向量数据库，也不只是一次性 RAG 检索接口。

## 适用场景与输入输出

适合长期管理研究资料、持续积累项目知识，以及需要让 Agent 跨任务复用上下文的个人或可信本地工作流。它不是开箱即用的聊天网站，不提供单靠安装 Python 依赖就可用的文本模型服务；MCP 载荷路径限制也不等于操作系统、网络或多租户隔离。

|输入 / 动作|输出 / 用途|
|-|-|
|把报告、技术说明等 `.md` / `.txt` 放入已配置的 raw 目录|生成摄取作业；Runner 调用所选模型，经校验后发布 Source 和关联知识页面|
|检索一个主题|返回页面、断言或运行态记忆，并保留来源指针|
|通过 MCP 明确写入偏好、决策或任务状态|先形成受控 Wiki/canonical 记录，再编译为运行态记忆；不直接写记忆表|
|Agent 查询一个问题|先组装 Memory Packet，再按预算补充 Wiki 证据；供宿主继续推理|

来源主张不自动成为已证实事实；资料入库也不自动成为个人偏好或决策。记忆类型依赖显式声明。原始材料保留为输入，模型输出受结构、来源、租约与写入校验约束。

## 目录与阅读路径

* **首次使用**：[快速开始](#快速开始) → [首次摄取与验收](#首次摄取与验收) → [接入 MCP](#接入-mcp)
* **了解设计**：[Architecture](#architecture) → [技术与进程职责](#技术与进程职责) → [详细架构](docs/ARCHITECTURE.md)
* **运行与维护**：[Config](#config) → [Commands](#commands) → [运维与恢复](docs/OPERATIONS.md)
* **开发与平台验证**：[开发测试](#开发测试) → [开发指南](docs/DEVELOPMENT.md) → [平台边界](docs/PLATFORM_SUPPORT.md)

## 快速开始

### 1\. 环境与部署路径

* Python **≥ 3.10**；目标系统为 Windows 11+、macOS 26+，以及符合内核要求的指定 Linux 发行版。目标与实际验收范围见 [平台说明](docs/PLATFORM_SUPPORT.md)，不能把 CI 配置当成已实测支持。
* **基础部署**可不安装可选 `vector_lake_core`；相关能力会按各自契约降级或提示不可用。其他依赖如 rjieba 仍含原生二进制，这不是“禁用所有 Rust/C 扩展”。
* **预编译核心部署**仅在已取得可信、匹配 checkout / 平台的包时使用 [免编译安装](docs/NATIVE_WHEELS.md)。当前没有公开 Release/PyPI 核心下载渠道；CI 构建包只在任务临时目录，不可据此下载。普通部署不安装 Rust/Cargo/maturin，缺包不自动源码编译。
* **完整摄取**另需选择并安装 Pi / Gemini / Codex CLI，在运行它的同一用户身份下完成认证。安装 Python SDK、配置 embedding Key 或通过 `--check`，都不能证明文本模型已经可用。

以下主线使用 **Windows PowerShell、仓库根目录、已激活的项目 venv**。不要在未激活的新终端照抄裸 `python`。如果激活脚本受本机策略限制，不修改全局策略；将下文每个 `python` 替换为 `.\.venv\Scripts\python.exe` 的实际路径。macOS/Linux 使用 `python3 -m venv .venv` 与 `source .venv/bin/activate`，其平台构建/验收限制仍适用。

```powershell
git clone https://github.com/shawnshi/KnowWeave.git KnowWeave
if ($LASTEXITCODE -ne 0) { throw '克隆未成功：先定位正确仓库，不继续安装' }
cd KnowWeave
if (Test-Path '.venv') { throw '已有环境：使用新目录或先审阅，不覆盖' }
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，不继续安装' }
.\.venv\Scripts\Activate.ps1
python -c "import pathlib,sys; print(sys.executable); sys.exit(0 if pathlib.Path(sys.prefix).resolve()==pathlib.Path('.venv').resolve() and sys.prefix!=sys.base_prefix else 1)"
if ($LASTEXITCODE -ne 0) { throw '解释器不是当前项目 venv，不继续安装' }
python -m pip install --only-binary=:all: -r requirements.txt
```

后续终端应显示同一个项目解释器。已有部署不要覆盖其环境或配置；创建新环境，验收后再按独立的切换流程启用。

### 2\. 新建演示库与最小配置

下例只用于**没有 `config.json` 的新 checkout 和全新演示目录**。已有配置必须先审阅并合并，不运行下面的初始化片段。环境变量会覆盖文件配置，示例显式绑定当前进程的演示路径，避免误用宿主历史 MEMORY。

```powershell
$MemoryRoot = Join-Path (Get-Location) 'scratch/quickstart-memory'
if (Test-Path 'config.json') { throw '已有配置：请审阅合并，不要覆盖' }
if (Test-Path $MemoryRoot) { throw '演示目录已存在：请换一个新目录' }
New-Item -ItemType Directory -Path (Join-Path $MemoryRoot 'raw') | Out-Null
Copy-Item 'docs/examples/purpose.md' (Join-Path $MemoryRoot 'purpose.md')
$config = Get-Content 'config.example.json' -Raw | ConvertFrom-Json
$config.memory_dir = $MemoryRoot
$config.target_directories = @((Join-Path $MemoryRoot 'raw'))
$config.ingest.backend = 'pi' # 也可选 gemini / codex，须先安装并认证对应 CLI
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path (Get-Location) 'config.json'), ($config | ConvertTo-Json -Depth 8), $utf8)
$env:VECTOR_LAKE_MEMORY_DIR = $MemoryRoot
$env:VECTOR_LAKE_DB_PATH = Join-Path $MemoryRoot 'wiki/.meta/vector_lake.db'
$env:VECTOR_LAKE_RUNNER_AUTOSTART = '0'
```

使用无 BOM 的 UTF-8 写配置，避免 Windows PowerShell 5.1 的默认 UTF-8 BOM 影响 JSON 读取。示例 `purpose.md` 是合成教学合同：正式使用前改成真实研究目的、证据层级和复审日期。主题范围不是文件摄取许可，不能绕过路径与隐私过滤。

```powershell
python scripts/ingest_runner_service.py --check
if ($LASTEXITCODE -ne 0) { throw '后端能力检查未通过，先排查，不启动真实摄取' }
python cli.py doctor
```

`--check` 只检查所选 CLI 与参数能力，不调用模型、不证明认证。按所选 CLI 的官方流程完成安装/登录，并在同一身份下验证一次最小模型请求后再继续。`doctor` 可能初始化或维护所选新库，不是只读探针；缺少可选核心或 embedding 凭据时可以有明确提示，不以“所有项目全绿”作为唯一验收。

## 首次摄取与验收

这一步会把**合成演示材料**发送给已选模型，并写入演示库，可能产生模型费用；如配置了 embedding 凭据，索引阶段还可能请求 embedding。不要换成私人或未经授权的材料。

### 1\. 准备一份材料并登记任务

```powershell
$sample = "# KnowWeave 快速上手`n`n本示例使用合成资料验证知识编译、来源追踪与检索。"
[IO.File]::WriteAllText((Join-Path $MemoryRoot 'raw/quickstart.md'), $sample, $utf8)
python cli.py sync
python cli.py ingest-tasks
```

`sync` 负责发现来源、准备摄取任务及相关索引维护，**不自行调用文本模型完成编译**。看到 queued / awaiting_subagent 只表示作业已准备，不表示已发布。

### 2\. 启动一个守护，再执行一次真实摄取

在**第二个 PowerShell 终端**进入同一仓库并激活相同 venv，然后明确绑定同一演示库、关闭自动 Runner：

```powershell
.\.venv\Scripts\Activate.ps1
$env:VECTOR_LAKE_MEMORY_DIR = Join-Path (Get-Location) 'scratch/quickstart-memory'
$env:VECTOR_LAKE_DB_PATH = Join-Path $env:VECTOR_LAKE_MEMORY_DIR 'wiki/.meta/vector_lake.db'
$env:VECTOR_LAKE_RUNNER_AUTOSTART = '0'
python watchdog_sync.py
```

保留该终端用于 outbox 消费与增量维护。返回第一个已激活的终端执行：

```powershell
python scripts/ingest_runner.py --once --limit 1 --concurrency 1 --no-shadow
```

这里只启动一次手工 Runner；不要再启动监督器或另一个自动 Runner。手工 Runner 默认 shadow，只有这里明确的 `--no-shadow` 才启用模型与真实写页。shadow 仍会认领作业、更新状态，不是零副作用预览。

### 3\. 核对结果，而不是只看退出码

```powershell
python cli.py ingest-tasks
Get-ChildItem (Join-Path $MemoryRoot 'wiki') -Filter 'Source_*.md'
python cli.py search "快速上手" --domain General --top_k 5
python cli.py search "快速上手" --domain General --mode claim --top_k 5
python cli.py query "这份快速上手材料说明了什么？" --domain General --dry-run
```

成功应同时看到：本次任务不再等待处理、Runner 本批没有未处理错误、对应 Source 页面已物化且保留原始来源、检索可以找到该资料。单次 Runner 退出或任务领取不证明完成；canonical 提交也不等于所有投影就绪。`query --dry-run` 输出上下文而不保存 synthesis，不承诺跳过其他初始化/读取行为。

若尚未成功，先检查 [配置与后端检查](docs/CONFIGURATION.md) 和 [日志、暂停、队列与恢复](docs/OPERATIONS.md)，不要删除数据库、清空队列或通过修改原文伪造新事件。示例结束后手工 Runner 已退出，在第二个终端按 Ctrl+C 停止本次前台守护；这不是既有生产计划任务的停机流程。

## Architecture

```mermaid
flowchart LR
    Raw["原始材料 raw（输入只读）"] --> Jobs["准备摄取作业"]
    Jobs --> Runner["Runner 调用宿主模型 CLI"]
    Runner --> Gate["结构 / 来源 / 租约校验"]
    Gate --> Canonical["同一事务：canonical + outbox"]
    Canonical --> Publish["物化 Markdown 与检索投影"]
    Publish --> Retrieve["页面 / 断言 / 运行态记忆检索"]
    Retrieve --> Packet["Memory Packet + Wiki 证据"]
    Packet --> Agent["宿主 Agent 继续推理"]
```

**两条闭环：**材料经摄取成为可追溯知识；Agent 明确写入的新事实/偏好/决策/任务状态先经过受控 mutation，再生成运行态记忆。二者复用 canonical、outbox 与投影机制，不允许模型直接改 SQLite。

### Storage Layout \& Architecture

|数据 / 组件|职责与权威边界|
|-|-|
|`MEMORY/raw/`|原始信源，只读输入|
|`MEMORY/purpose.md`|目的与证据合同；新库必须准备有效合同，不能靠空文件占位|
|`MEMORY/wiki/.meta/vector_lake.db`|canonical 数据、作业/租约、outbox 及派生索引；并非每个表都是 canonical|
|`MEMORY/wiki/.meta/retrieval_chunks.sqlite3`|可选分块检索侧车；须显式构建，普通查询不会创建或修复|
|`MEMORY/wiki/*.md`|人可审阅的发布层；手工修改也须进入校验 mutation 路径|
|`index.json` / `claim_topology.json`、FTS / 向量等|派生投影，不是另一套可直接修改的真相源|
|`brain/`、`scratch/`|仓库侧任务包、日志与验收制品，不是原始知识库|

详见 [架构、受控类型、记忆与模块地图](docs/ARCHITECTURE.md)。文件优先不等于“Markdown 始终是唯一 canonical”；SQL 事务提交与文件/索引发布是不同完成边界。

### 技术与进程职责

|层 / 技术|职责|是否必需|
|-|-|-|
|Python / CLI / MCP（FastMCP）|业务协调、工具暴露、校验与任务生命周期|运行时必需；MCP 用于 Agent 接入|
|SQLite / outbox / 租约与 fencing|canonical 提交、队列认领、过期 Worker 拒绝与可重试发布|必需|
|Markdown / JSON 投影|人可读知识与派生拓扑|由受控流程发布|
|FTS5 / 图检索 / rjieba|全文、CJK 分词与图扩展|核心检索路径；rjieba 是原生绑定，不存在纯 Python jieba 回退|
|Gemini embedding / sqlite-vec、可选 Tantivy|向量检索与搜索镜像|依赖凭据/配置/实际可用性，缺失不能冒充健康|
|Rust `vector_lake_core`|图、解析、重排等可选加速|可选；不同入口的降级契约不同|
|Pi / Gemini / Codex CLI|宿主侧文本模型调用|自动文本摄取需选其一，认证和权限独立于 embedding|

**MCP** 提供 Agent 工具，不替代后台消费；**Watchdog** 消费 outbox、监听和维护，并默认看护真实写页的 Runner；**Runner** 领取作业、调用模型并提交校验结果。只读检索可不启动守护，发生写入后需要消费 outbox。常驻与手工 Runner 不应对同一个 MEMORY 根并跑。

## 当前检索行为

- 公共 MCP/CLI 页面检索默认 `profile=auto`；可显式选择 `legacy`、`balanced`、`entity`、`source`、`author` 或 `analysis`。默认不普遍抬高 Source/作者材料，排序分值不是证据置信度。
- 明确的会议年份参与候选资格与有界扩召回；创建、摄入或核验时间不冒充事件时间。无法核验年份的材料会标注缺口，不承诺已经识别事件身份。
- 分块融合 `use_chunks` 默认关闭；普通查询不自动构建侧车或调用外部 embedding。构建、索引维护与外部 embedding 分别授权。
- `auto`/`ram` 无法取得 RAM 候选时回退精确 SQL；只有显式 `two_stage` 使用二值近似预筛。
- 完成状态区分 `complete`、`partial`、`failed`。可选阶段到期后保留已取得的词法证据；正文不可读时明确标注元数据摘要。请求预算是协作式限制，不是对原生、文件系统或传输的物理硬取消。
- 默认保留 Decayed 页面并降权，排除 Deprecated/Archived；`include_history` 可显式纳入历史状态。域、专题和这些开关都不是认证 ACL。

参数和构建入口见 [配置参考](docs/CONFIGURATION.md)。合成样本与故障路径测试不代表真实相关性提升或生产 SLO 验收。

## 接入 MCP

GUI 客户端不会可靠继承当前终端的 venv。必须把下例解释器、仓库和 MEMORY 路径改成自己的绝对路径；macOS/Linux 的解释器通常为 `<repo>/.venv/bin/python`。

```json
{
  "mcpServers": {
    "Reson-KnowWeave-mcp": {
      "command": "C:/path/to/KnowWeave/.venv/Scripts/python.exe",
      "args": ["-m", "vector_lake.mcp_server"],
      "env": {
        "PYTHONPATH": "C:/path/to/KnowWeave",
        "VECTOR_LAKE_MEMORY_DIR": "C:/path/to/MEMORY",
        "VECTOR_LAKE_DB_PATH": "C:/path/to/MEMORY/wiki/.meta/vector_lake.db",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

不要将真实 Key 写入仓库示例。上例是通用 `mcpServers` 配置，不是 Pi 专用配置；Pi 入口及重连说明见 [配置指南](docs/CONFIGURATION.md)。在实际连接调用 `runtime_identity` 核对解释器、PID 与选定函数的加载指纹；它不代表整模块或原生映像已验收。还需用受控检索确认目标路径，独立 `doctor` 不能代替该连接验收。

## 常驻部署与已知边界

watchdog、runner 和 MCP 是独立进程。重启前两者不会刷新常驻 MCP 的已加载代码；MCP 需在宿主中单独重连，再核对运行身份与实际检索后端。

Windows 常驻链为 `VectorLake-Watchdog → scripts/watchdog_service.ps1 → watchdog_sync.py → Runner`。正式部署必须在任务动作中显式指定 `-Python`、`-MemoryDir` 等参数；包装器的默认宿主 Python 不保证是项目 venv。注册脚本会更新任务配置，不是保留参数的重启命令。

前面的演示通过关闭自动 Runner 避免重复执行；正式部署的默认行为不同。源码、配置或版本指针改变不会热更新驻留进程。升级应先安排空闲窗口、备份、确认实际任务/进程身份，再做受控切换与验收；不按进程名批量终止，也不自动恢复旧数据库覆盖新业务写入。

macOS/Linux 的依赖与前台入口不代表已提供或验收 systemd / launchd 服务安装器。平台、CI、原生二进制、模型可用性、恢复与 SLA 是不同验收维度。参见 [运维与恢复](docs/OPERATIONS.md)、[平台支持范围](docs/PLATFORM_SUPPORT.md) 和 [原生包分发边界](docs/NATIVE_WHEELS.md)。

## Config

所有配置入口集中在此；完整含义、默认值、示例和生效时机见 [配置与环境参考](docs/CONFIGURATION.md)。配置在启动时读取，不热切换。

|首次部署配置|建议 / 行为|
|-|-|
|`memory_dir`|显式绝对路径；实际优先级为 `VECTOR_LAKE_MEMORY_DIR` → `config.json` → 宿主历史默认目录|
|`ingest.backend`|选择 `pi` / `gemini` / `codex`；后端选择为 `--model-cmd` → `VECTOR_LAKE_RUNNER_MODEL_CMD` → 文件配置 → Pi|
|`target_directories`|新演示显式指定 MEMORY/raw 的绝对路径；留空使用当前 MEMORY/raw，相对路径以仓库根目录为基准|
|`exclude_paths` / `supported_extensions`|保留隐私排除；默认处理 `.md` / `.txt`，不自动解析 PDF 等格式|
|`purpose.md`|版本化目的、范围、证据层级和 SIR 复审合同；[演示模板](docs/examples/purpose.md) 不是正式业务政策|
|`GEMINI_API_KEY`|仅用于 embedding；不是 Pi/Gemini/Codex 文本认证的统一替代品|
|`VECTOR_LAKE_RUNNER_AUTOSTART` / `VECTOR_LAKE_RUNNER_SHADOW`|控制自动 Runner；shadow 仍有队列/状态副作用，不能当只读模式|

**凭据来源不能混为一谈：**普通 `cli.py` 的非 `ops-status` 分支在存在时会加载 `~/.gemini/.env`，默认不覆盖已设置的环境变量；它不是自动读取仓库 `.env`。`ops-status` 提前走显式路径的只读分支。MCP、Watchdog 与直接 Runner 不通过 CLI 的该加载分支，凭据应由各自宿主/任务环境或所选 CLI 的原生认证提供。不要把不相关凭据复制进项目配置。

<details>
<summary>高级环境变量登记与参考入口</summary>

下面保留开关的可检索登记；详细行为以对应指南和当前源码为准，避免在多个章节重复维护默认值。

|类别|开关|说明|
|-|-|-|
|Runner 执行与恢复|`VECTOR_LAKE_RUNNER_AUTOSTART`、`VECTOR_LAKE_RUNNER_CODEX_BIN`、`VECTOR_LAKE_RUNNER_CONCURRENCY`、`VECTOR_LAKE_RUNNER_COOLDOWN`、`VECTOR_LAKE_RUNNER_EXPECTED`、`VECTOR_LAKE_RUNNER_GEMINI_BIN`、`VECTOR_LAKE_RUNNER_HOLD_SHADOW_LEASE`、`VECTOR_LAKE_RUNNER_MODEL_CMD`、`VECTOR_LAKE_RUNNER_MODEL_TIMEOUT`、`VECTOR_LAKE_RUNNER_PI_BIN`、`VECTOR_LAKE_RUNNER_REPAIR_ATTEMPTS`、`VECTOR_LAKE_RUNNER_SHADOW`、`VECTOR_LAKE_RUNNER_STALE_SECONDS`、`VECTOR_LAKE_RUNNER_STRICT`、`VECTOR_LAKE_RUNNER_SUBAGENT_AGENT`|[参考](docs/CONFIGURATION.md)|
|原生与图计算|`VECTOR_LAKE_CORE_VERSION`、`VECTOR_LAKE_TOKENIZER`|[参考](docs/CONFIGURATION.md)|
|摄取与补扫|`VECTOR_LAKE_AUTHOR_SOURCES`、`VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH`、`VECTOR_LAKE_CATCHUP_EMBEDDING_BUDGET_SECONDS`、`VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS`、`VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS`、`VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS`、`VECTOR_LAKE_SOURCE_RANK_PENALTY`、`VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING`、`VECTOR_LAKE_SUBAGENT_RUN_ID`|[参考](docs/CONFIGURATION.md)|
|数据路径与载荷|`VECTOR_LAKE_DB_PATH`、`VECTOR_LAKE_MEMORY_DIR`、`VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES`、`VECTOR_LAKE_MEMORY_GRAM_MAX_BASE_AGE_SECONDS`、`VECTOR_LAKE_MEMORY_SEARCH`、`VECTOR_LAKE_OUTBOX_PAYLOAD_KEEP_DAYS`、`VECTOR_LAKE_PAYLOAD_MAX_BYTES`、`VECTOR_LAKE_PAYLOAD_ROOT`|[参考](docs/CONFIGURATION.md)|
|检索、索引与运维|`VECTOR_LAKE_AUTHOR_ANNOTATE`、`VECTOR_LAKE_AUTHOR_BOOST`、`VECTOR_LAKE_AUTHOR_FACET`、`VECTOR_LAKE_BACKUP_KEEP`、`VECTOR_LAKE_BACKUP_MAX_BYTES`、`VECTOR_LAKE_CANDIDATE_DEPTH`、`VECTOR_LAKE_CANDIDATE_POOL`、`VECTOR_LAKE_CLAIM_BLOCKS`、`VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE`、`VECTOR_LAKE_EMBEDDING_DIMENSION`、`VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS`、`VECTOR_LAKE_EMBEDDING_MAX_BATCH_TOKENS`、`VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM`、`VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES`、`VECTOR_LAKE_EMBEDDING_MAX_RETRIES`、`VECTOR_LAKE_EMBEDDING_MAX_TOKENS_PER_ITEM`、`VECTOR_LAKE_EMBEDDING_MODEL`、`VECTOR_LAKE_EMBEDDING_PREWARM`、`VECTOR_LAKE_EMBEDDING_RPM`、`VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS`、`VECTOR_LAKE_EMBEDDING_TIMEOUT_MS`、`VECTOR_LAKE_EMBEDDING_TPM`、`VECTOR_LAKE_EMBEDDING_TRANSPORT`、`VECTOR_LAKE_EMBEDDING_UTILIZATION`、`VECTOR_LAKE_ENTITY_NAME_PRIORITY`、`VECTOR_LAKE_EXPANSION_QUOTA`、`VECTOR_LAKE_FALLBACK_REPARSE_SECONDS`、`VECTOR_LAKE_FTS`、`VECTOR_LAKE_FUSION`、`VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE`、`VECTOR_LAKE_LEIDEN_L0_RESOLUTION`、`VECTOR_LAKE_LEIDEN_L1_RESOLUTION`、`VECTOR_LAKE_LEIDEN_SEED`、`VECTOR_LAKE_METADATA_FIRST`、`VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC`、`VECTOR_LAKE_NO_BROWSER`、`VECTOR_LAKE_NUMERIC_THREADS`、`VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING`、`VECTOR_LAKE_OUTBOX_FAILURE_NONBLOCKING`、`VECTOR_LAKE_OUTBOX_MAX_BACKLOG`、`VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS`、`VECTOR_LAKE_QUERY_CONTEXT_TTL`、`VECTOR_LAKE_RECLAIM_FREE_SPACE`、`VECTOR_LAKE_RERANK_WEIGHT`、`VECTOR_LAKE_SEARCH_LEDGER`、`VECTOR_LAKE_STALE_TASK_MAX_AGE_SECONDS`、`VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING`、`VECTOR_LAKE_TIMELINE_PARITY_BLOCKING`、`VECTOR_LAKE_VECTOR_INDEX`、`VECTOR_LAKE_VECTOR_SIM_SCALE`、`VECTOR_LAKE_WRITE_LOCK_CONTENTION_WINDOW_SECONDS`|[参考](docs/CONFIGURATION.md)|

</details>

## Commands



> **MCP 是主接口**：Agent 直接调用 `vector_lake/mcp_server.py` 注册的工具。本仓库不随附 slash command 兼容层；宿主若打包 skills，应按实际注册的插件命名空间调用。项目保留 `vector-lake-*` 技能标识，不把旧宿主的固定前缀当作跨客户端协议。
>
> 全量恢复、内部调度和重建端点保留在 CLI，避免 Agent 在常规检索中误触重型维护。

|职责分类|工具|说明|
|-|-|-|
|**混合检索与时序**|`search_vector_lake` · `search_timeline` · `trace_vector_lake`|按已记录的模型与维度执行向量、FTS5 和 PPR 混合检索；时间线查询与事实溯源|
|**严格原文证据（可选）**|`get_query_evidence`|Windows 试点：仅为已有 canonical Source/Raw 绑定的记录返回有界逐字片段；缺失绑定明确拒绝，不代表原文不存在，不替代既有原文阅读。见 [证据边界](docs/QUERY_EVIDENCE.md)|
|**推演上下文与记忆**|`preview_query_context` · `query_logic_lake` · `finalize_query_synthesis` · `update_operational_memory`|上下文预览、准备推演任务、核验提案页面与运行态记忆写入|
|**知识治理与审查**|`review_governance_list` · `resolve_governance_item` · `get_governance_debt` · `trigger_audit_graph` · `merge_suggestions_vector_lake` · `check_duplicate_entity`|治理队列审阅与裁决、知识债务度量、拓扑审计、实体查重与候选合并|
|**自愈体检与安全写入**|`lint_vector_lake` · `doctor_vector_lake` · `runtime_identity` · `rename_entity` · `write_wiki_page` · `inspect_projections`|Schema 审计与修复、运行环境体检、当前进程身份观察、实体重命名、单页安全写入和派生投影巡检|
|**拓扑可视化**|`visualize_vector_lake`|3D HTML 知识拓扑交互仪表盘|

<details>
<summary>完整 CLI 与受控维护示例（不是安装脚本）</summary>

以下命令要求已激活项目 venv；涉及删除、修复、恢复或重新派发的命令仅供已核准的维护任务，不作为首次上手步骤。

基础体检：

```powershell
python cli.py doctor
```

登记 raw 摄取任务（此步不调用文本模型）：

```powershell
python cli.py sync
```

启动后台守护进程（增量监听）：

```powershell
python watchdog_sync.py
```

搜索页面层：

```powershell
python cli.py search "Agent memory" --domain System_Architecture --top_k 5
```

搜索运行态记忆：

```powershell
python cli.py search "部署目标" --domain System_Architecture --mode memory --top_k 5
```

搜索 claim-level facts：

```powershell
python cli.py search "Agent memory" --domain System_Architecture --mode claim --top_k 5
```

基于 Memory Packet 和 wiki 证据做 synthesis：

```powershell
python cli.py query "对比 Karpathy LLM Wiki 与 Agent memory 的架构差异" --domain System_Architecture
```

准备查询并查看溯源（仍会创建临时上下文 payload，不写 Wiki 页面）：

```powershell
python cli.py query "总结当前运行态记忆架构" --domain System_Architecture --dry-run
```

只需要内联上下文、不创建提案 payload 时，在 MCP 宿主调用 `preview_query_context`：

```json
{"query_str": "总结当前运行态记忆架构", "domain": "System_Architecture"}
```

预览仍可能记录检索审计事件，不是整个文件系统的零写入模式。

检索接口迁移：MCP 检索/上下文入口及 CLI `search` / `query` 必须显式传入一个 `domain`。`General` 是类别而不是全库；多域任务分别查询。默认不发送查询到外部嵌入服务；只有当前授权允许外发该查询时，才传 `allow_external_embedding: true`（CLI：`--allow-external-embedding`）。这是范围选择与出站许可，不是身份认证或操作系统隔离。未提供外发许可时只使用本地支路，并在结果中说明。底层 Python 函数保留无域的受控内部调用，不应直接作为无约束的外部接口发布。

任务级检索策略（只适用于 page / 上下文，memory / claim 不使用这些排序策略）：

```powershell
python cli.py search "WiNEX的竞争优势及不足" --domain Medical_IT --profile analysis
python cli.py search "示例研究员" --domain Medical_IT --profile entity
python cli.py search "2026年全国卫生健康工作会议要点" --domain Policy_and_Governance --profile auto
```

`auto` 使用本地规则选择 `entity` / `balanced` / `analysis` / `source` / `author`。非 `legacy` 策略取消全局 Source 降权与作者偏好；`author` 显式增加作者关联偏好，`entity` 仅优先完整标题/别名匹配，`source` 优先原件类型。`legacy` 保留原有全局排序开关。年份只按明确会议查询的事件元数据或标题约束，不使用 created/updated；多年份对比不强制选一个年份。年份和类型匹配都不等于已核验会议事实。

稳定性边界：`auto` / `ram` 在容量或并发不足时回退精确 SQL；二值近似仅由显式 `two_stage` 选择。PPR、记忆、分块证据故障保留独立可用结果，并报告部分完成或失败。弱论坛/软件关键词不硬排除 Product 等实体；分块文档资格过滤后会有界扩深。

限定 `domain` / `cluster` 的 memory 检索及 Memory Packet 使用同一 SQLite 读快照：先流式计算完整作用域的词频，再流式评分、各自保留 active/history TopK。不会先截断候选，也不保留整个域的 JSON 列表；扫描工作量与保留结果分别受预算约束，超限明确失败，不返回假完整排名。未限定范围的受控内部旧路径保持不变，具体上限见 [配置指南](docs/CONFIGURATION.md)。

查询最多 4096 字符，top_k 为 1–50。请求采用 15 秒合作式预算，正文单页 512 KiB、累计 4 MiB；这些不是物理封网、权限认证或原生调用的硬截止。上下文/台账返回完成状态，异常正文不进入诊断。

可选 scoped-memory / 向量代次加速器不在检索时自动创建。维护命令默认只读预览，`--apply` 才创建五个 scope 索引；仅当数据库已有 `vec_emb_float` 表时，才同时创建 revision 表及三个触发器。更新代码不会自动安装这些对象。执行前需指定已授权的现有数据库、备份并核对维护边界，参见 [运维说明](docs/OPERATIONS.md#检索-scope--revision-加速器)：

```powershell
python cli.py retrieval-indexes --db "X:/approved/vector_lake.db"
python cli.py retrieval-indexes --db "X:/approved/vector_lake.db" --apply --budget-seconds 60
```

长文分块侧库（仅 CLI 构建，不自动扫描知识库；先将示例页面键替换为已授权且已发布的真实页面键）：

```powershell
python cli.py chunk-index --domain Medical_IT --page Source_ApprovedReport
python cli.py chunk-index --domain Medical_IT --page Source_ApprovedReport --apply
python cli.py search "长文中的关键风险" --domain Medical_IT --use-chunks --profile analysis
```

第一条只检查显式页面元数据。`--apply` 才向 MEMORY 的 `wiki/.meta/retrieval_chunks.sqlite3` 写入独立侧库；默认只建本地词法分块，不调用外部嵌入。需要分块向量时必须另有正文外发授权，再显式加 `--allow-external-embedding`。每次最多 8 页、单页 512 KiB；模型/维度不符时保留原库并拒绝，不自动迁移。侧库采用 WAL，查询不自动创建索引。`--use-chunks` 将页面与分块路径按 RRF 融合，并复查当前域、状态和源文哈希；不保证因此提高真实召回率。

离线评测只读取指定的本地标签与运行结果，不执行检索或调用模型：

```powershell
python cli.py retrieval-eval --dataset tests/fixtures/retrieval_synthetic.json --results your-results.json --k 10
```

结果文件按 case ID 提供页面键列表或 `{"pages": [...], "abstained": true}`。随附标签明确为合成用例，不是人工相关性标注。人工标签须经审核并声明 `label_kind=human`、`status=approved`、`approved_by`；这些字段是来源声明，不是身份认证。指标仅相对已标注相关集，不能当作全库召回率或答案真实性验证。

治理与审计：

```powershell
python cli.py review
python cli.py review resolve "<index|item_id>" --resolution skip
python cli.py audit-graph
python cli.py lint
python cli.py lint --auto-fix
python cli.py research
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
```

图谱与清理：

```powershell
python cli.py graph
# 两者默认仅预览；显式 --apply 才写入，仍须另行授权。
python cli.py gc --days 30
python cli.py delete "<raw-source-path>"
```

投影与 canonical 维护：

```powershell
python cli.py projection-report --limit 20
# 派生投影健康（八条：memory_gram / vectors / page_projection / fts_index / tantivy_mirror /
# claim_index / timeline_events / governance_queue）与它们的修复入口：默认只报告；
# `--reconcile` 预览会修什么，`--reconcile --apply` 才真修；`--only NAME` 限定一条。
python cli.py projections
python cli.py projections --reconcile --only fts_index
python cli.py projections --reconcile --apply
python cli.py canonical-backfill --limit 100
python cli.py canonical-backfill --apply --limit 100
python cli.py timeline-rebuild --apply
python cli.py timeline-repair
python cli.py timeline-repair --apply
python cli.py projection-rebuild-index --apply
# 向量影子投影支持 RAM 精确候选与显式 two_stage 二值近似；失效时不自动重建。
python cli.py vector-index-rebuild
python cli.py vector-index-rebuild --apply --shortlist 256
python cli.py embedding-backfill --limit 200
python cli.py embedding-backfill --apply --limit 200
python cli.py wiki-restore --apply --limit 10
python cli.py gram-index
python cli.py gram-index --if-due --apply
```

备份保留与幂等性键维护：

```powershell
python cli.py backup-retention
python cli.py backup-retention --keep 2 --max-bytes 6442450944 --apply
python cli.py idempotency-status
python cli.py repair-idempotency --table mutation_outbox
python cli.py repair-idempotency --table mutation_outbox --apply
python cli.py claim-pointer-report
python cli.py claim-pointer-repair
python cli.py claim-pointer-repair --apply --edges
python cli.py claim-evidence-queue
python cli.py claim-evidence-queue --group month --batch-pages 50
python cli.py claim-evidence-queue --apply
python cli.py provenance-backfill
python cli.py provenance-backfill --limit 1 --prepare-patch
python cli.py provenance-backfill --revert "<reviewed-rollback.jsonl>" --prepare-patch
python cli.py provenance-accept
python cli.py provenance-accept --apply
python cli.py anchor-draft
python cli.py anchor-backfill
python cli.py anchor-backfill --only 1,2,5,9-14 --apply
```

这些维护命令默认报告或演练，修改必须显式使用对应的 `--apply`。重建、修复与接受遗产缺口是不同动作，不应互相替代。

* `canonical-backfill`：从已有 Wiki Markdown 回填 canonical。
* `projection-rebuild-index`：从 canonical 重建页面索引、FTS 和 claim topology，保留已有向量。
* `embedding-backfill`：按 RPM/TPM 限额断点补齐向量；`wiki-restore` 从 canonical 恢复缺失的 Markdown。
* `timeline-repair`：就地修复事件投影 parity 与日期来源字段，不以入库日期代替事件日期。
* `gram-index`：报告或重建精确 n-gram 倒排。脏基表不服务，检索退回精确扫描；重建分批 staging，最后经内容指纹校验发布。`--if-due` 根据基表可用性、500 个脏文档阈值、脏基线年龄、搜索成本摊销及交互式搜索阈值判断到期。守护进程提供定时维护；手工命令本身不要求守护进程运行。以实际 `due=` 和 `Watchdog Status` 判断维护状态。
* `backup-retention`：只约束 `.meta/backups` 中的数据库副本，默认保留最新 3 份，其余受 12 GiB 预算约束；最新一份不会因超预算被删。`MEMORY/backup/` 中的页面恢复点不在此范围。
* `idempotency-status` / `repair-idempotency`：检查唯一性等级，清理冗余幂等键以恢复完整唯一索引，不删除业务行。
* `claim-pointer-report` / `claim-pointer-repair`：检查及摘除证据中的死 claim 指针，先记录回滚项；`--edges` 只修正可解析的目标页面键，不改边的出处页，不更新证据新鲜度。该入口不清理历史孤儿 operational memory，后者使用下述冻结范围维护脚本。
* `claim-evidence-queue`：按缺口形状与 cohort 分批进入治理队列，默认演练，`--apply` 才入队。出处缺口不自动转成外部研究指令。占位符和运行态 Packet 叙述不作为 claim。
* `provenance-backfill`：仅根据摄入台账或唯一源文件逆匹配提出来源候选，多义与无匹配时弃权。默认只读预览；`--prepare-patch` 只导出原始字节、候选文件、diff 和 manifest，不修改 Wiki、canonical、历史或 outbox。兼容参数 `--apply` 也只导出，成功导出后返回退出码 **3**，不表示已修复。撤回旧回填同样只生成反向补丁；过期、布局不支持或导出失败会报错，并披露此前已生成的补丁。补丁依赖读取时的指纹，生成成功不证明当前仍适用、来源真实或已获写入授权；当前版本没有自动应用分支。边界与预算见[来源维护说明](docs/OPERATIONS.md#来源维护受控窗口与仅生成补丁)。`provenance-accept` 另行记录已接受的遗产缺口，不把它伪装成已找到来源。
* `anchor-draft`：依据候选出处的判别词起草块级归属，并提供原文行供复核；证据不足时弃权。`anchor-backfill --only ... --apply` 只写确认项并保留回滚记录。

锚点写入需保持 claim 清洗后的文本与 ID 不变：出处标记贴紧追加；含括号而不能安全解析的出处跳过；只定位正文，不在标题或 frontmatter 上补锚点。迁移执行统计与旧评测记录不作为当前运维状态，历史版本见 Git 与 CHANGELOG。

历史孤儿 memory-ingest 的分类、scope 审批与小批恢复步骤见 [运维指南](docs/OPERATIONS.md#历史孤儿记忆的定向维护)。这是受控维护流程，不是首次安装或后台自动升级步骤。

</details>



## 只读运维与维护入口

```powershell
python cli.py ops-status --db "<明确的 vector_lake.db 路径>" --json
python cli.py recompile-ingest --plan "<frozen-plan.json>" --plan-sha256 "<digest>" --approval "<read-approval.json>" --approval-sha256 "<digest>"
```

`ops-status` 使用显式 DB 路径，读取有界聚合和公开心跳，不初始化、迁移或修复；它不能证明模型、MCP 或完整业务链可用。普通 `doctor`、shadow、治理“预览”与其他维护命令不可一概视为零副作用。受控重编示例只展示默认校验入口，不构成批准或真实派发。

删除、孤儿清理、投影重建、触发器维护与恢复均不是首次上手步骤。先按 [运维与恢复](docs/OPERATIONS.md) 核对授权、范围和恢复点；不清库来消除告警。

## 开发测试

本地固定测试清单按已验证的 Python 3.13 使用；其他解释器以对应 CI 清单/工作流为准。在已激活的独立开发 venv 中安装测试依赖，不向部署或全局 Python 临时补包：

```powershell
python -m pip install --only-binary=:all: -r requirements-test.txt
python scripts/ci_core.py test --backend fallback --receipt scratch/dev-test-new/core.json -- -q
```

该测试使用合成 bootstrap MEMORY，运行前确认回执路径属于本次任务且没有其他写者。native 构建/探针、测试依赖与 CI 细节见 [开发指南](docs/DEVELOPMENT.md)。实际 CI 平台矩阵以 [工作流](.github/workflows/test.yml) 为准，配置存在不等于远端已执行通过。

核心入口：

|路径|职责|
|-|-|
|`vector_lake/cli_app.py`|CLI 命令与入口分支|
|`vector_lake/mcp_server.py`|MCP 工具注册|
|`vector_lake/tool_ingest.py`|摄取准备、认领与完成协议|
|`vector_lake/mutation_coordinator.py`|受控 canonical mutation 与 outbox|
|`vector_lake/watchdog_app.py`|常驻消费与维护|
|`scripts/ingest_runner.py`|宿主侧模型调用与结果提交|

完整 [模块地图与技术细节](docs/ARCHITECTURE.md#module-map)、[运行限制与恢复](docs/OPERATIONS.md)、[配置参考](docs/CONFIGURATION.md)。本项目不随附 slash command 兼容层，MCP 是 Agent 主接口。
