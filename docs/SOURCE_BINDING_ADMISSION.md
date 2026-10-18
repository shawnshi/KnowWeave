# 当前 Source 版本的受控绑定准入

这是独立的所有者维护 API，不是查询工具的回退，也不是自动 Source 元数据 writer。它把**当前 canonical Source 记录的精确摘要**与经授权读取的 Raw 字节摘要建立新绑定；不倒填历史、不宣称原有编译过程或完整发布已获证明。

## 写入与不变量

- 仅在现有 canonical DB 的 `jobs` 控制账本中创建 `source_binding_admission` 类型收据，以及 `.meta/source-binding-before-*.json` 私有恢复文件。
- 不修改 Source entity、`raw_text`、旧 `source_hash/source_frontmatter`、Wiki、Raw、sources registry、claims、evidence、edges、processed ledger 或其他知识表。正常 canonical page version 与 Markdown projection 因而不发生变化。
- 不调用按页替换、Markdown mutation outbox、Schema 初始化、索引重建、embedding 或模型。正常完整 publication 的证明函数不因这种收据而升级。
- 收据身份由 schema version、node key、domain 与 canonical Source 记录摘要确定。一次精确 Source 版本只接受一个绑定；同一 Raw 的重放不创建第二条收据，不隐式改绑不同 Raw。
- 已有完整、部分或冲突的旧 binding 字段不走此接口。旧字段都缺失时，仍需显式的当前所有者映射；同名文件、Active、registry ID、磁盘 hash 均不产生映射权限。

现有 API 的按页替换会重建关联知识，不能靠清空 proposed claims 等数组把它改成元数据更新。该接口不修改那条业务路径。

## 可信宿主 API

`vector_lake.source_binding_admission` 提供：

- `BindingTarget(node_key, domain, raw_reference, raw_sha256, cluster=None)`。
- `SourceBindingAdmissionHost(memory_root, config, targets, owner_scope_sha256=...)`。
- `prepare(node_key, domain)`：只读取得当前 Source 记录 CAS，并核对已批准的 Raw 摘要。
- `admit(prepared)`：在精确版本不变时写入绑定收据。
- `revoke(prepared, expected_receipt_sha256=...)`：显式的单收据 CAS 补偿，保留取消历史，不恢复整个数据库或覆盖 Source 文件。

构造器只由已获授权的所有者代码调用。根、配置、目标及所有者范围摘要不是 MCP 参数；Python 对象或摘要本身不是身份认证或许可。实际动作、目标、分类和副作用的批准仍由上游宿主管理。`targets` 为冻结 tuple，最多八条；实际运行只能包含当次明确获准的目标。本次任务只覆盖指定五条 Source，缺少准确 Raw 映射的条目不得填入猜测路径。

`owner_scope_sha256` 记录不可变范围的来源，不是 `approved=true`，也不单独授权写入。API 不扫描其他资料库，不发现或补下载原件，不开放公共写 MCP 方法；查询仍只有原来的四个参数和只读宿主。

## 事务与恢复

API 仅支持 Windows 固定本地盘，固定使用可信根下的 `wiki/.meta/vector_lake.db`。自有 `mode=rw&cache=private` 连接只打开已有数据库；既不借用全局 writer，也不加载扩展或安装 Schema。缺表、未知 jobs 触发器、jobs 外键、位置或能力不符均拒绝。

1. 保留已有 canonical/辅助文件和祖先句柄边界，开启 `BEGIN IMMEDIATE`。
2. 先核对当前索引、域/cluster、唯一 Source 和当前记录摘要，再读取批准的 Raw。
3. 核对 UTF-8 与完整 SHA256，再读一次检测读取期间变化。不把允许 WRITE sharing 的句柄说成物理写排除。
4. 在已固定 `.meta` 祖先下独占创建并 flush 恢复文件。它仅保存这条控制记录的前态或“不存在”，不导出 Source 正文或整个私有数据库。
5. 同一事务内创建实际 lease token/generation/期限，完成条件更新，清空 lease 并提交类型化收据。过期、源版本变化、Raw 变化或恢复写入失败不能提交半条有效收据。

提交后的清理失败可能发生在收据已持久化之后，必须上报；不能说“抛错所以未提交”。按确定性收据 ID 和新只读快照检查现状，不盲目重放。撤销需要收据摘要 CAS；已取消版本不会自动重开。修正或重开这种不可变槽位需要另行定义的所有者动作或新 canonical 版本。

恢复文件与控制账本都是本地受保护制品，不发送到公开服务。保留 `source_binding_admission` 收据是绑定可用性的条件；任何未来 jobs 清理或协议升级必须明确处理该类型，不能把它当临时 ingest 日志清空。

## 严格读取的信任范围

`source_binding_record` 是不含 writer、配置发现或 DB factory 的纯读取模块。严格 reader 仍先验证 Source 的当前域和状态，只对同时缺少旧 hash/frontmatter 的记录查找新的确定性控制 ID：

- 在同一只读 canonical 快照内，要求正确任务类型、已完成真实 lease、精确 Source 版本、域和 node key，且 payload/result 一致。
- PK 精确查找，无 jobs 全表扫描；控制内容按长度限制和当前剩余输入预算读取。普通 completed job、JSON 权限旗标、部分/冲突的旧 stamp 或不匹配版本均不是证明。
- 随后仍执行原 Raw 目录、格式、重解析/硬链接、排除路径、字节摘要、敏感段落、期限和引文边界检查。
- `binding_admission` 仅说明当前收据身份与准入时间。`verified` 仍只覆盖 canonical Source 版本绑定及返还的精确 Raw 字节，不证明作者、日期、事实、历史所有权、全文/图片或完整 publication。

Source 或 Raw 后续变化时，新查询会拒绝旧收据。收据不冻结未来文件；合作式期限不是 OS 抢占隔离。个人可信单用户用途的既有残余风险仍在，不增加严格 ACL/账户/服务工程，也不声称零物理副作用。

## 预算与验收

保持查询 4096 字符、1–8 个 Source、每节点 64 条 canonical rows、每源 512 KiB Raw、请求 4 MiB 输入、每源三个片段/2500 字符及共享合作式 15 秒。控制 payload/result 的实际传输也计入输入，单项至多 16 KiB。不提高预算来通过测试。

生产调用须先完成合成与真实 Schema 集成验证、独立审查、恢复点检查及准确映射核验。测试通过不授权生产写入。要区分工作树实现、主树接入、新宿主加载、当前 MCP 可用和模型答案质量；没有做的面不能报告通过。
