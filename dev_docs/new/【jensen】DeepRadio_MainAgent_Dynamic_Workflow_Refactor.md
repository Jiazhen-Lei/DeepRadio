# DeepRadio MainAgent Dynamic Workflow Refactor

## 目标

将 Workflow 的创建、阶段编排、状态推进和失败恢复统一交给 MainAgent。MainAgent 是唯一用户接口，SubAgent 只执行领域任务。UI 继续使用现有 `ServiceAgent.step()`、`step_command()` 和 `AgentReply`，不改变界面契约。

## 新链路

```text
AgentPanel
  -> ServiceAgent（UI 兼容层）
  -> MainAgent（Intent、Workflow、用户交互）
  -> Domain SubAgent（执行 TaskCard）
  -> Tool Permission Guard
  -> GNU Radio / SDR Tools
  -> Evidence Validator
  -> MainAgent
```

MainAgent 决定执行什么以及下一步是什么。宿主代码只判断动作是否允许、工具结果是否可信、状态是否仍属于当前 Workflow 和工程版本。

## Workflow 模型

Workflow 只保留：

- `workflow_id`
- `revision`
- `intent`
- `stages`
- `current_stage`
- `status`
- `base_project_version`

Stage 只保留：

- `id`
- `objective`
- `target_agent`
- `inputs`
- `expected_evidence`
- `status`
- `result_refs`

删除固定转移、执行模式、Stage effect ceiling、Stage 工具白名单、固定重试次数和预编译计划。MainAgent 可以从 Stage Library 选择能力，但 Stage Library 不定义完整任务流程。

## Skill 与宿主约束的边界

以下内容迁入 Skill/Reference：

- Intent 澄清、假设和缺失信息处理；
- Stage 选择和最短 Workflow 编排原则；
- GNU Radio 建图、连接、验证、仿真和诊断流程；
- BLE 与 SDR 领域操作知识；
- 失败后重试、重新编排或询问用户的策略。

以下内容必须由宿主代码执行：

- 工具参数 Schema 和文件路径范围；
- 用户授权、锁定约束和工具权限；
- SDR 设备探测、Arm、RF 运行时限和紧急停止；
- Evidence、Workflow revision 和 project version 校验；
- 快照、持久化、异常恢复和调用预算。

Skill 说明正确操作方法，宿主约束决定动作是否能够真正执行。

## MainAgent

MainAgent 的系统指令保持简短：

```text
你是 DeepRadio 的唯一用户接口和 Workflow 负责人。
理解用户目标，读取 grc-orchestration Skill，创建并维护最短 Workflow。
领域任务必须委派给 SubAgent，不直接调用领域工具。
根据已验证 Evidence 决定继续、重试、重新编排或结束。
缺少必要信息或涉及物理 RF 执行时，向用户确认。
使用用户当前语言简洁回复。
```

MainAgent 只持有 Workflow 控制工具和 DeepAgents 的 `task` 委派工具。领域工具只绑定到对应 SubAgent。

## SubAgent

SubAgent 的公共约束简化为：

```text
你是领域执行 Agent，不与用户交互。
执行 TaskCard，遵循指定 Skill，调用已绑定工具。
返回结构化结果和 Evidence，不修改 Workflow，不扩大任务范围。
```

领域细节只写入对应 Skill/Reference，不在 Prompt、Catalog 和代码中重复维护。

## 工具权限

删除有序的 effect level 和 Stage effect ceiling，改用显式权限：

- `project.read`
- `project.write`
- `device.read`
- `device.configure`
- `rf.start`
- `rf.stop`

`rf.start` 必须满足用户授权、RF feature flag、设备探测和 Flowgraph Arm。`rf.stop` 与 `emergency_stop` 始终允许。

## 删除的生产路径

- 固定 Task 到 Workflow 的映射；
- 固定 Stage 转移；
- `deterministic/hybrid/agentic` Stage 分流；
- deterministic Stage handler；
- LLM 不可用时静默进入 deterministic workflow；
- 独立于 MainAgent 的 Workflow Planner 和 Intent 控制链。

确定性的 GNU Radio、协议验证和硬件工具继续保留。它们是可靠的执行能力，不再作为 Workflow 架构模式。

## UI 兼容

以下接口保持不变：

- `AgentPanel`；
- `ServiceAgent.step()`；
- `ServiceAgent.step_command()`；
- `AgentReply`；
- UI 所需的进度事件、artifact 和 workflow digest。

内部 Workflow 状态通过兼容投影提供给现有 UI，界面不获得编排控制权。

## 验收条件

1. MainAgent 是唯一面向用户的 Agent。
2. MainAgent 可以按 Intent 动态创建、插入、删除和重排 Stage。
3. SubAgent 不能修改 Workflow，也不能扩大工具权限。
4. 生产代码中不存在 Stage execution mode 和固定 Catalog 转移。
5. RF 未授权、未探测或未 Arm 时无法启动，停止操作始终可用。
6. 过期 Evidence 和错误工程版本不能完成 Stage。
7. UI 公共接口和现有界面行为保持兼容。
8. 会话中断后可以恢复 Workflow 和工程状态。
