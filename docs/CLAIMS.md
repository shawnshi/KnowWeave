# Claim 身份、证据与迁移契约

本页对应 GC-01 至 GC-08。Claim 是从页面抽取的断言片段，不是自动核验后的事实；`compiled-truth`、`timeline-event` 表示页面结构。系统不提供自动语义矛盾检测或原始材料蕴含验证。

## 生成与更新

正常入口为 `mutation_coordinator.execute_mutation_batch`。页面校验、显式期望版本检查、canonical 更新及 Outbox 入队组成受保护写入链路。页面更新会重编译该页 claims、evidence 和相关派生记忆；Markdown／索引投影可以延迟，不能以 SQL 提交代替投影完成。

Schema 校验异常必须向上抛出，不能变成成功的空抽取结果。生成物的有意跳过与抽取失败不同。直接调用底层 apply 也必须保持事务与关系引用完整性。

## 身份与多次出现

- 完整规范化正文用于 Claim 身份；正文不能为显示而截断。
- 已有不超过 360 字符的块保持原有内容寻址 ID；长块重新计算完整身份。
- `legacy_claim_ids` 记录历史截断身份。它是迁移线索，不代表该旧 ID 必然只有一个后继。
- 同页重复正文保存多次出现位置，不再依赖数据库最后写入覆盖定位。不同出现的元数据冲突必须显式处理。
- 内容寻址 ID 不能单独承担稳定业务身份。全文变化意味着不同内容身份；需要修订关系时必须显式声明，不自动猜测。

新旧 ID 的一对多关系必须阻塞迁移。截断前缀相同但结尾相反的两句话，不允许通过任意选择一个后继来消除歧义。

## 来源声明不是已核验证据

新抽取的 Evidence 标为 `provenance_kind: declared-source`、`support_status: unverified`。其文本和定位指向派生页面片段，而不是经核对的原文摘录。Claim 的 `evidence_support: declared` 与空的 `verified_evidence_ids` 明确表达该边界。

- `evidence_ids` 保存归属记录，不能仅凭非空判定事实成立。
- Claim 的 `source_ids` 应来自其实际 Evidence 归属，包含行内来源，不应机械复制整页来源清单。
- 多来源摘要不能绕过块级归属要求并自动获得所有来源的支持。
- 未读取并验证原始字节时，不得以文件名、页面名或 frontmatter ID 冒充来源内容哈希。
- 旧 Evidence 缺少核验状态时，应暴露 legacy-unverified 语义，不静默认证为已核验。

本变更没有实现将声明证据提升为已核验证据的写入工具。不得手工设置一个标志就对外宣称完成核验；后续核验能力需要独立的来源、摘录、定位、审核与版本合同。

## 有效性与竞争关系

基础有效性由证据、状态、生效时间、到期时间和复核时间决定；supersession 只表达竞争／替代关系。没有竞争者不构成补足证据或消除冲突的理由。

`valid_from` 尚未来到的记忆不能进入当前结果；`valid_to` 已过期的记忆也不能因索引快照仍为 active 而返回为当前记忆。检索必须在候选与排序阶段使用一致的当前时间语义，避免最后一轮剔除导致过期候选占满 TopK。

TTL 用于新鲜度半衰减，不等于统一的强制到期。`include_history` 可以展示历史对象，但不能把历史状态重新解释为当前有效。

## 显式 Claim 关系

支持的 canonical 引用字段为 `contradicts`、`supersedes`（Claim ID 列表）及 `revision_of`（单个 Claim ID）。页面声明也接受 `contradicts_claim_ids`、`supersedes_claim_ids`，抽取时统一为 canonical 字段。

页面通过 `claim_relations` 明确绑定 `block_index`（正数、从 1 开始）或 `claim_id`，不能将整页关系盲目传播到所有块。顶层关系只绑定 `frontmatter.claim_id` 对应首块；没有该 ID 时仅在单一有效出现位置上无歧义地使用，否则拒绝。

```yaml
claim_relations:
  - scope: block
    block_index: 1
    contradicts: [claim_target_id]
    revision_of: claim_previous_id
```

目标存在性在 canonical 事务中校验；纯抽取器不读取数据库来猜测目标。`supersedes` 只在替代者的基础状态为 active／expiring-soon 时执行，循环替代和循环修订被拒绝；`revision_of` 仅记录修订链，不自动推导替代或矛盾。

新编译的替代记录保存不含正文的有效性依据。未来生效的显式替代保存待生效依据，五个检索后端在无写入的时间推进中也会启用它；尚未生效时不隐藏前任。查询时替代者到期／待复核，会重新判断前任的基础状态；多个直接替代者中仍有有效者时不会提前恢复前任。增量更新会查找反向矛盾／替代邻居，胜出者归档或失去证据后不能靠旧快照继续隐藏相关断言。真实删除路径也更新相关记忆，不只删除赢家。旧库中缺少该依据的历史 superseded 记录需要受控重编译，不能假装已完成迁移；其隐藏状态不会被无依据地提升为 active。

canonical apply 会拒绝：

- 缺失或歧义目标；
- 自引用；
- 同一 delta 中重复的 Claim ID；
- 用一个页面的变更覆盖另一个页面的 Claim ID；
- 移除目标时遗留未包含在本次明确范围内的入向引用。

同一 canonical 批次可以明确删除引用双方，或在删除目标的同时更新引用方以移除关系。单页删除不能静默扩展到其他页面。

唯一 legacy 别名可以解析到当前 ID。一对多映射不能自动消歧。需要增加同批次页面时，由操作者明确选择，不由迁移器自动扩展范围。

## 图的语义

系统中有不同图面：

1. 页面／实体关系：以页面关系声明为基础，保存在页面拓扑及相关关系记录中。
2. Claim 共现展示图：由 `build_claim_graph_projection` 建立，以共同主体、共同归属来源及显式矛盾连边。
3. 经核验的 Claim–Evidence 支持／反驳图：本实现不宣称已经具备。

共现图声明 `graph_kind: claim-cooccurrence`、`is_evidence_graph: false`。默认最多 2,500 个节点；实体桶窗口为 6、来源桶窗口为 4、普通边度数上限为 12，显式矛盾可突破度数上限。这些是稀疏化策略，不是事实相似度、独立佐证数量或可信概率。

## 分页迁移：默认只预览

迁移器必须显式指定 MEMORY 根目录和页面名称，不扫描全库、不读取 raw。下面使用的是示例隔离目录，不能直接替换为真实库并跳过审批。

```powershell
python scripts/migrate_claims.py --memory-root C:/isolated/claim-fixture --page Concept_Example.md --plan-out C:/isolated/claim-preview.json
```

预览只读 canonical 和明确选中的 Wiki 页面，不初始化数据库、不写 canonical，也不自动保存计划；`--plan-out` 才创建新计划文件，已有文件不覆盖。SQLite 可能使用正常的只读事务锁／WAL 读者协调；这不等于执行数据迁移。

计划包含页面内容哈希、canonical 快照令牌、期望版本、身份映射及阻塞项，不包含正文。若存在以下情况，先解决再重新预览：

- Wiki 与 canonical 正文不一致；
- 旧身份缺少后继或有多个后继；
- 某个旧 Claim 的关系未完整声明在其唯一后继中；另一块出现同名关系不能掩盖该缺口；
- 更新会遗留范围外的入向引用；
- 页面或关系校验失败。

应用需要显式选择 `--apply` 和未修改的计划：

```powershell
python scripts/migrate_claims.py --memory-root C:/isolated/claim-fixture --apply --plan C:/isolated/claim-preview.json
```

应用先重新核验计划，再创建 integrity-checked SQLite 备份，随后使用原有 canonical mutation、期望版本和事务内快照检查。并发更新或计划过时会终止，不重新猜测身份；备份路径与 canonical／投影状态由回执返回。预览通过不是生产迁移授权。

### 回滚边界

本次代码交付不自动迁移或回滚真实库。应用前保留备份与回执；需要恢复时：

1. 停止该库所有写者与 Outbox 消费者，关闭相关 SQLite 连接；检查备份之后是否存在其他需保留的写入。
2. 将备份恢复到新的隔离数据库，执行 `PRAGMA integrity_check`，核对原 Claim ID、引用、行数及相关派生表。
3. 恢复到原位置属于单独的明确操作，不可在活跃 WAL／连接存在时直接覆盖 `.db` 文件；不能覆盖其他人的后续写入。
4. Markdown 本次使用原有内容重新物化；恢复后按原有恢复流程核对 Outbox、索引和图投影，不能以数据库文件存在作为完成证据。

测试使用合成库验证备份可恢复到独立 SQLite 实例；这不代表真实库已执行过迁移或回滚演练。
