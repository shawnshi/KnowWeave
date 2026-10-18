---
name: vector-lake-query
version: 11.2.2
tier: action-allowed
description: 'Query KnowWeave with source-first evidence retrieval; escalate to memory context or deep synthesis when the question or evidence requires it.'
triggers: When the user asks to query KnowWeave, Vector Lake, or the Logic Lake
---

# KnowWeave 查询

普通查询默认先定位资料与原文，不把“query”解释为必须启动深度推演或准备 proposal。

## 范围与默认值

- 先遵守当前任务的数据源路由、隐私和授权约束。每次调用都必须提供已授权、已确认的非空 `domain`；确有范围歧义时再澄清，不重复索要已有授权。
- `General` 是实际类别，不代表全部域；域选择不是认证 ACL。多域问题在原授权内分别查询，不能在失败后退回无域或全库。
- `allow_external_embedding=false`、`use_chunks=false`。外发 embedding 需要另有明确授权；索引和工具参数不扩大权限。
- 检索不授权新增 Wiki、修改 canonical、自动归档或保存原文。`scratch/` 不是物理隔离。

## 按证据分流

1. **资料、概念、实体、作者公开观点等普通查询**：先调用 `search_vector_lake`，使用 `mode="page"`。根据任务选择 profile；作者线索可用 `author`，定位原件可用 `source`。profile 只是排序偏好，不是作者或事实证明。
2. **历史记录、观点演化、跨期比较、相互冲突的证据，或首批资料不足**：追加所需的 `mode="memory"` / `mode="claim"` 检索，或使用 `preview_query_context`。这不是禁止记忆检索，也不是固定调用次数后强行结束。
3. **明确需要受控 proposal / 持久综合流程**：仅在该落盘动作、目标和范围已有授权时使用 `query_logic_lake`。它会准备 proposal payload；不能从 `dry_run` 名称推断零写入，最终 Wiki 写入仍有单独的授权与验证门。

普通页面检索模板；将占位值替换为任务已确认的内容，不可原样发送：

```json
{
  "query": "<core-concepts-and-necessary-aliases>",
  "domain": "<authorized-domain>",
  "top_k": 5,
  "mode": "page",
  "allow_external_embedding": false,
  "profile": "auto",
  "use_chunks": false
}
```

预览使用 `query_str`、显式 `domain` 和默认 `allow_external_embedding=false`，不把检索模板中的 `query` 参数直接复制给预览工具。

## 查询与原文核验

- 检索字符串保留核心概念、作者和必要的中英文别名；把“区分事实与推断、优先原文”等回答指令留在编排层，不混入检索词。不能机械缩短查询、删掉有效别名或降低 TopK 来制造提速。
- 结果偏题、出处不足或缺少关键证据时，在原范围内有界补查别名、标题和不同来源；必要时升级上下文预览。不要把“未命中”或工具失败解释为不存在。
- 已知参数、互不依赖的检索或定向读取可批量执行；需要先理解规则、授权或上一结果的步骤保持先后依赖。遵守宿主预算，不为并行而并行。
- 确认原文能支持问题的关键结论；核验作者、日期、出处与矛盾。Wiki 编译字段、profile 加权、文件路径或哈希不能单独证明署名、事实真实性、所有权或授权。
- 可选严格工具 `get_query_evidence` 仅接受已定位的 Source 键、查询、显式 domain 与可选 cluster；不是必经调用。只有现存 canonical 绑定成立才返回 verified；`binding_unproven`、不支持或未注册均不等于原文不存在，也不自动修复元数据或降低 verified 含义。继续既有已授权原文阅读路径时明确其绑定/覆盖未知，不把普通读取包装为严格 verified，不为凑轮数盲目调用。
- 核对读取工具的实际返回范围，不把请求的 offset/limit 当作硬边界。宿主可能按 Markdown 节自动扩读；对于有严格日期、字段或行数边界的材料，须用已验证能保证边界的读取能力，否则停在受影响读取前，不能靠扩大请求范围凑证据。提示词不是物理读取控制。
- 只对实际核验的片段作结论；图片未查看时标记覆盖不完整，不把文本摘要或出处命中等同于全文核验。
- 历史分享链接可能带会话、签名或访问参数。不回传、落盘或联网使用这些参数值，不假定它们已过期；必要时只给可核验的稳定主链接或说明缺口。脱敏文本不得标成逐字原文引用。
- 证据足够才综合；区分来源主张、事实、推断和未知。返回资料与可访问的证据定位，不输出隐藏推理草稿。

## 停止与升级

复杂性本身、已有授权内的关键词细化或原文读取，不新增人类 Approve 门。只有实质范围/方向歧义、缺失必要授权、预算耗尽或不可逆操作未获准时，停止受影响分支并给出最小缺口。不能绕过原有隐私、来源和持久化门。

主代理可直接执行。委派仅在用户或适用合同已授权且宿主真实具备执行能力时使用；复杂性不是委派许可。不要把工具、模型或规则读取轮数设为成功标准；交付以证据充分性和问题覆盖为准。
