# 原文证据包：Windows 严格绑定试点

提供已定位 Source 的可选严格证据读取，不替代既有 Source-first 原文阅读。离线 helper 与宿主资源清理修复已有定向独立静态复核，历史 BLOCK 与失败证据保留；新增 MCP facade、主树接入及活动宿主验收须按实际 Diff 和回执分别确认。静态 PASS、合成测试、磁盘注册都不等于活动模型可用、完整生产验收或通用质量非劣。

## 能力与宿主责任

用户侧拟定输入仍为查询、1–8 个已索引 `Source_` 键、显式 domain 和可选 cluster，不接受任意 Raw 路径。当前仅支持 Windows 固定本地盘：

- `Artificial_Intelligence`：`raw/人工智能/`；
- `Medical_IT`：`raw/article/`、`raw/数字化转型/`、`raw/医疗信息化/`。

**canonical 连接由可信宿主所有。** 底层 `get_query_evidence` 的 Python `canonical_connection` 是已打开、只读且进入事务的实际快照对象，不是 MCP/模型参数；reader 不打开 DB 文件、不接管连接生命周期。flags 只是就绪检查，不产生授权。

新增 `vector_lake.query_evidence_host.ReadOnlyEvidenceHost(memory_root, config)` 只能由已获授权的宿主代码构造。它固定使用该显式根下的 `wiki/.meta/vector_lake.db`，冻结根与排除/格式配置，不发现资料库、不跟随环境变化，也不借用 `db_store.get_connection/read_snapshot`。非标准 `VECTOR_LAKE_DB_PATH` 位置不受此试点支持；MCP facade 对与固定位置不一致的覆盖返回 `nonstandard_canonical_location_unsupported`，不自动改选另一资料库。

适配器每次请求在已验证的 Windows 祖先/文件句柄仍固定时，打开专用 SQLite `mode=ro&cache=private` 连接；要求普通、单链接 canonical 文件和已有普通 `entities/page_index_nodes` 表，不创建 DB、安装 Schema 或加载扩展。连接设置 `query_only`、`trusted_schema` 只影响这条自有连接；schema 读取先固定事务世代，再将同根的实际私有上下文传给 reader。`_host_context` 不是 JSON 参数或授权旗标。

快照准备、SQL 与 Raw 共用父请求剩余合作式期限和宿主实际 `threading.Event` 取消信号；SQL progress handler 可中断长查询。适配器独占 rollback/close，取消不阻止逐项清理；不修改调用方写连接或其活动事务。连接、canonical/辅助文件描述符与祖先 Win32 句柄共用清理策略：每项关闭均尝试，原生 `CloseHandle` 失败必须报告，不能因先前关闭异常跳过后项。原始异常不能被清理异常替换：中断或非预期编程异常继续以原对象抛出；安全分类的 `_evidence_cleanup_errors` 尽力写入真实异常字典，绕开子类属性/数据描述符。数值元数据读取同样绕过可执行属性；可选诊断失败使用固定回退或省略，不得中断清理或改选原始异常。可处理的原始失败保留主分类，清理故障按阶段单列 `host_errors`。没有原始异常时，非预期清理异常仍继续抛出。已完成证据遇到可处理的清理失败则保留精确内容、降为 partial，全部安全故障保留，终态仅在清理之后记录一次。

SQLite `mode=ro` 约束 canonical 逻辑写入，不代表零物理副作用：WAL 读取可能访问/协调既有 `-wal/-shm` 状态，短期目录固定会限制重命名/元数据操作。适配器还固定已存在的 `-wal/-shm/-journal`，拒绝重解析/多链接并复核身份；但缺失辅助文件被不可信写者在检查后抢先创建的情形，不由这种句柄固定证明物理排除。所有者已明确选择个人可信单用户用途，暂不进行严格 ACL、账户或服务隔离；这项风险接受不证明物理排除。多用户或对抗性文件系统部署仍需独立证明实际写入边界，不能用路径、flags、测试或文档代替。候选不使用 `immutable=1` 忽略活跃 WAL，不宣称机器隔离、硬实时取消或任意介质上的零写。真实部署介质仍需单独验收。

相关平台依据：[SQLite URI 参数](https://www.sqlite.org/uri.html)、[只读 WAL 条件](https://www.sqlite.org/wal.html#read_only_databases)、[打开 flags](https://www.sqlite.org/c3ref/open.html)。未知 URI 参数可被忽略，不能靠添加未经支持的 `nofollow` 参数制造安全保证。

所有者已授权剩余接入与真实来源测试；授权不由测试结果产生。`mcp_server.get_query_evidence(query, node_keys, domain, cluster='')` 仅把这四项公开为 MCP 参数；`tools.get_query_evidence` 委托配置宿主 facade，根来自可信宿主配置，不暴露构造器、路径、连接、取消对象或私有上下文。配置、构造和实际读取共用不延长的合作式期限。登记和重连/活动模型可见性分别验收；不得为重连绕过所属宿主的生命周期接口。

真实样本初测五条新 Source 均明确拒绝；已检查的四条缺少 canonical `source_hash/source_frontmatter`。已完整绑定的 Pi Durable 控制样本可返回证据。这是严格能力覆盖缺口，不是原文不存在或已完成质量非劣验收。普通原文阅读可以保持在已有授权路径内，但不得将其包装为严格 verified，或把磁盘字段、processed ledger/path/hash 名称当成缺失的版本绑定。

这是两域公开/专业资料的受限试点，不是通用 ACL 或目录授权。私人域、隐藏/凭证类文件、历史-only Source、未证明绑定、多 Raw 引用、试点外目录会明确拒绝，不归为“没有资料”。严格日期/字段/行数读取不受支持；不能拿整份有界文档读取替代那种权限。

## 当前版本的受控准入收据

[Source 绑定准入接口](SOURCE_BINDING_ADMISSION.md) 是独立的所有者维护能力，不由查询触发。它只写现有 canonical DB 中的类型化控制收据及私有恢复文件，不改变 Source entity、旧 hash/frontmatter、正文、知识表或完整 publication 状态。

旧 hash/frontmatter 均缺失时，reader 可在同一只读快照内核对该收据：确定性主键、任务类型、完成的 lease、精确 Source 记录摘要、域/node key 和批准 Raw 摘要必须一致。没有收据仍明确拒绝；部分或冲突的旧 stamp 不自动改选此路径。普通 completed job、磁盘字段、状态旗标或摘要格式不能替代受控准入。收据 payload/result 有界并计入原输入预算，不打开 writer 或扫描任务历史。

新证据的 `binding_admission` 只标明当前收据，不倒填历史。绑定后仍现场读取 Raw、核摘要并扣留敏感段落；`verified` 的范围不变，不能升级为全文、真实世界事实或完整发布证明。实现、部署及真实来源调用须分别验收，不能据此段文档声称生产已经准入。

## 一次请求的数据链路

1. 敏感赋值形态的节点键在任何响应、ledger 或 I/O 前拒绝；canonical Raw 标识同样检查。不将可识别参数值当作文件名回传。
2. 非 Windows 在配置或路径访问前拒绝。宿主路径来自构造时显式冻结的词法绝对根；借入快照的独立 helper 保留一次配置/环境读取。两者都不调用全局 `get_memory_dir().resolve()`、不扫描 legacy root；固定本地盘检查后仍要经过实际句柄边界。
3. 使用宿主 canonical 快照，先核对索引和 Source 的域/cluster；只接受唯一 Active/Decayed Source。原有路径要求 structured `sources`、`source_frontmatter` 和内容摘要；两项旧绑定字段均缺失时，只接受上述受控当前版本收据。绝不把磁盘字段抄入 canonical，也不弱化部分/冲突旧绑定的拒绝。
4. canonical 先读取有界 rowid/长度元数据，再逐行按**当前剩余额度**读取 payload，不使用一条 SELECT 开始时的陈旧 CASE 限额。行数有界，不安装 Schema。
5. 复用排除配置、格式规则与私密目录分类。Windows 逐级 OPEN_REPARSE_POINT 打开并固定祖先和文件，目录不共享 WRITE/DELETE，复核句柄最终路径。非普通文件、多硬链接、路径替换、共享冲突与能力不足拒绝。POSIX 分支已经移除，不宣称支持。
6. 读取已授权的整份 Raw 文本：每源512KiB、请求合计4MiB、合作式15秒；canonical payload 与准入控制 payload/result 同样计入输入额度，父请求已有消耗不重置。Raw 每次实际传输立即计费，后续身份、摘要、读取或期限失败不退费；按 fstat 的有界大小读取，不用额外越限字节探针。
7. 对同一份字节核对已有 bare MD5、bare SHA256 或 `sha256:<64hex>`；返回 SHA256 及精确 UTF-8/字符/行位置，保持 BOM/CRLF。摘要不证明授权、事实真实性或未来不可变性。
8. 最多三个片段、合计2500字符，直接来自原文切片；大段窗口围绕可见词定位，不拼接伪引文。仅验证 canonical Source 快照绑定与返还字节，不声称完整当前 publication、Wiki projection 或所有 claim 已验证。

Windows 的短期目录句柄会限制并发重命名/元数据写入，不是零副作用或机器隔离。可信宿主的数据权限、配置位置及进程/数据库安全不是可由输入 flags 替代的机制。

## 敏感内容、归因与覆盖

- URL 的 query/fragment/userinfo、可识别凭证赋值所在**整段**保守扣留；同段作者/日期候选也不能复活。该规则可能扣留其他可用上下文，不宣称一般质量非劣。
- 空作者/日期值不会跨 LF/CRLF 吞掉下一行。归因仅为带定位、未裁决的显式候选；created/updated 是记录元数据，不冒充发布日期；多日期不自动裁决。
- 图片不读取/OCR，coverage 为 `text_only_partial`，批次 partial。只对返还片段负责，不把 Source 命中等同于全文阅读。
- 不联网，不回传/落盘敏感参数值，不认定历史参数失效；扣留文本不伪装为逐字引文。这仍不是完备的秘密检测器或不可信原文认证。

## 失败与观测

配置冻结或只读取一次；读不到配置时先于节点处理返回安全的类型化失败，不回显 RuntimeError 文本。单节点的拒绝、IO/解码/SQL/递归解析/预算错误保持显式状态和安全数字 native code，已完成节点不丢失，不归一成 no_data。bootstrap 错误明确失败；自有连接/句柄清理错误附 `host_errors`，不能误报 complete。

独立调用不创建通用 runtime scope、不开 ledger 路径。只有已有、已授权宿主 request scope 时，才通过该宿主记录安全状态/计数/节点指针；外层 scope 的授权与落点仍归其所有者。宿主适配只在自有清理结束后记录终态，不让 reader 提前写 complete。`evidence_binding`、`evidence_source_read`、`evidence_extract`、`evidence_snapshot_sql_setup`、`evidence_host_total` 是粗粒度且可能包含重叠的时间，不是逐行或纯CPU计时。

## 接入与证据分层

- 独立静态复核只覆盖其明确版本；MCP facade 的新增 Diff、参数 schema 和回归须另行核验。
- 本人的受限接入授权已具备；严格物理隔离已按个人使用决定延后，WAL/缺失辅助文件竞态仍是残余风险。非标准 DB 位置保持拒绝。
- 真实 Source 的严格拒绝/成功、原文引文与归因/图像覆盖、留出题及合成答案分别记录。AI 标注的支持点不是人工盲评或真实世界事实裁定。
- 主树磁盘接入、实际 MCP 客户端可调用、当前 Pi 工具可见以及同模型/思考档位的完整链路对比是不同验收面；任何未做的面不报告通过。

真实库只按授权进行有界读取，不修复 canonical/Raw、重建索引或改变知识内容。全局模型/提供者配置、提交/推送、无关服务重启与自动删除工作树均不在本试点动作内。
