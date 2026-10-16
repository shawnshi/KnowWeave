---
name: vector-lake-watchdog
version: 11.2.1
tier: action-allowed
description: 'Start, inspect, or explicitly restart the persistent KnowWeave watchdog and its ingest runner.'
triggers: 'When the user requests watchdog or runner startup, status inspection, or a controlled restart.'
---

# Watchdog 与 runner 运维

## 边界

- 启动、注册或重启必须有覆盖动作、目标和副作用的明确授权；已有授权足够时不重复确认。
- 不用一次性 `sync` 代替常驻服务，不把守护进程依附于会话 shell。
- 不按进程名批量结束 Python/Node，不删除锁文件伪造可用状态，不自动清空任务或恢复数据库。
- 不擅自终止活跃模型调用。明确授权的重启须先检查在途 job、lease 和 outbox；状态未知不当作零。
- 启动服务不等于同意扩展知识库，不为服务启动另写 operational memory。
- `scratch/` 仅用于任务制品和日志，不是物理安全沙盒；不得放入凭据或知识正文。

## 确定实际安装

以当前仓库、宿主配置和既有计划任务的 executable、arguments、working directory 核验活动安装；不要硬编码个人路径，也不要从旧插件缓存或临时快照启动。

Windows 的常驻入口为项目 [watchdog_service.ps1](../../scripts/watchdog_service.ps1)；缺少任务时，在已授权范围内使用 [register_watchdog_task.ps1](../../scripts/register_watchdog_task.ps1) 注册。不要从 `scratch/` 依赖临时注册脚本，也不要手写替代 XML。

## 启动或受控重启

1. 读取现有任务设置和 PID 归属，核对工作目录、解释器、进程父链及创建时间。
2. 已有健康实例时不重复启动；watchdog 可接管已有 runner supervisor，不因此另起第二个 consumer。
3. 重启前保存可恢复的代码与配置记录；必要的数据库恢复点保留在原数据管理边界，不复制正文到会话或源代码制品。确认没有活跃任务后，按精确任务/进程归属停止和启动，保留原参数。
4. 常用任务名为 `VectorLake-Watchdog`。保留实际启用的触发器、单实例设置和登录方式；注册脚本先尝试 S4U，受限时可能使用 Interactive，不能把 session 0 当作通用验收条件。
5. 若身份、授权或队列状态无法验证，停止该分支并报告。失败按既有重试预算单维度诊断，不盲目重复启动。

## 验收

- 计划任务处于预期状态，进程由它拥有，而非会话 shell；PID/创建时间确实更新，旧进程树退出，无重复 consumer。
- 元数据目录为实际 `MEMORY/wiki/.meta`。`.watchdog_status.json` 提供组件状态；公开心跳为 `runtime/watchdog_ops.json`、`runtime/runner_ops.json`。PID 文件是 `runtime/.watchdog.pid`、`runtime/runner.pid`，不能假设心跳 JSON 必含 PID。
- 结合 `runtime/runner_supervisor.json`、`runtime/runner_status.json` 检查心跳推进、cycles、连续失败和重启次数；观察窗口应覆盖实际配置周期。
- 使用 `python cli.py ops-status --db "<已确认的数据库>" --json` 交叉核验，不把历史失败、unknown 或陈旧计数报告为健康。
- watchdog、runner、MCP 是独立进程。重启前两者不刷新常驻 MCP；MCP 重连是独立操作，需核对该连接的 `runtime_identity`。
- 心跳和空队列循环不证明远端模型已回答，也不代替真实摄取或生产 SLO 验收。

交付仅说明实际动作、进程身份、验证结果、恢复点和残留问题；不输出推理草稿。详细维护边界见 [运维指南](../../docs/OPERATIONS.md)。
