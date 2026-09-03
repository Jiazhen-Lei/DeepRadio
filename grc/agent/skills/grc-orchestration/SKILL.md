---
name: grc-orchestration
description: 根据 Stage 候选库规划、执行和调整由用户驱动的 DeepRadio Workflow，并将每个 Stage 委派给固定的 SubAgent。
---

# DeepRadio 工作流编排

本 Skill 规定 MainAgent 如何规划、执行和维护 DeepRadio Workflow。

## Stage 候选库

创建或调整 Workflow 前，必须读取 `references/stage_library.yaml`。

该文件是 DeepRadio 的 Stage 候选库：

- 只能选择候选库中定义的 Stage。
- 保持每个 Stage 的职责和 `target_agent` 不变。
- 一个 Stage 对应一个 SubAgent 和一个 TaskCard。
- SubAgent 可以在一个 TaskCard 内调用多个工具。
- Task 输入根据当前 Radio Specification 和已有 Stage 产物生成。
- 如果候选库无法覆盖用户需求，应向用户说明，不得自行创建新的 Stage 类型。

## Workflow 规划

用户提出新的可执行无线电任务时：

1. 理解用户的完整意图。
2. 从 Stage 候选库中选择当前任务需要的全部 Stage。
3. 生成完整且有序的 Workflow。
4. 默认将 `radio_specification_alignment` 作为第一个 Stage。
5. 将第一个 Stage 设置为 `running`，其余 Stage 设置为 `pending`。
6. 在委派任何 Task 前调用 `update_workflow`。

`update_workflow` 只接收 Stage 的 `id`、`objective`、`inputs`、`status` 和
`result_refs`。Task、`target_agent` 和预期 Evidence 由 Stage 候选库确定，
MainAgent 不重复生成这些固定字段。

所有当前可确定的 Stage 应在 Workflow 初次创建时完成规划，不能等当前 Stage 完成后再逐个规划。

如果用户只是提问、请求解释或查看状态，直接回答，不创建或推进 Workflow。

## 当前 Stage 执行

每次只处理 `current_stage`。

执行当前 Stage 时：

1. 根据对应的 Stage 候选定义生成一个 TaskCard。
2. 使用 `task` 将其委派给固定的 `target_agent`。
3. 收集 SubAgent 返回的结果、Artifact、Measurement 和 Evidence。
4. Stage 状态或结果发生变化后，调用 `update_current_stage`。
5. 只有声明的 Evidence 已经存在时，才能将 Stage 标记为 `completed`。

MainAgent 不直接执行 SubAgent 负责的领域任务。

TaskCard 必须包含：

- `workflow_id`
- `revision`
- `base_project_version`
- `stage_id`
- 任务目标
- 任务输入
- 预期 Evidence

如果当前 Stage 依赖已有 Artifact，Task 输入必须使用 `CURRENT_ARTIFACTS` 中对应的真实路径。不得猜测路径或把 `/session/work/...` 写入需要由宿主机读取的 Flowgraph 参数。

## Stage 推进

Stage 完成不代表下一 Stage 自动开始。

当前 Stage 完成后：

- `current_stage` 继续指向刚完成的 Stage。
- 后续 Stage 保持 `pending`。
- Workflow 等待用户下一步指令。
- MainAgent 向用户反馈结果并结束当前回复。

只有当用户明确表示“继续”“下一步”“开始下一阶段”或同等意图时，才能将下一 Stage 设置为 `running` 并开始执行。

推进到已规划的下一个 Stage 时调用 `update_current_stage`，不要重新提交完整 Workflow。

用户提出问题、发表评论或者仅确认结果时，不推进 Workflow。

普通 Stage 之间的推进不使用 `request_user_decision`。

## Stage 完成反馈

每个 Stage 完成后，MainAgent 应向用户说明：

1. 刚完成的是哪个 Stage。
2. 该 Stage 完成了什么工作。
3. 产生了哪些主要结果、Artifact、Measurement 或 Evidence。
4. 下一个 Stage 准备完成什么。
5. 用户是否有疑问、需要修改，或者希望继续。

不要向用户展示内部 JSON、TaskCard、工具日志或状态字段。

## 缺少用户输入

如果当前 Stage 缺少只能由用户提供的信息：

- 保持当前 Stage 不变。
- 调用 `request_user_decision(kind='input')`。
- 将当前 Stage 设置为 `waiting`。
- 用户回答后继续执行同一个 Stage。

多轮参数补充始终属于同一个 Stage，不创建新的 Stage。

## Workflow 调整

只有 Workflow 结构发生变化或需要回溯已完成 Stage 时，才调用 `update_workflow`。

如果用户修改当前 Stage 的内容，更新并重新执行当前 Stage。

如果用户修改已经完成的早期决策：

1. 回到最早受到影响的 Stage。
2. 更新该 Stage 的 Task 输入。
3. 重置该 Stage 以及依赖它的后续 Stage。
4. 保留未受影响的已有结果。
5. 根据用户当前指令决定是否开始执行，不自动推进其他 Stage。

用户可以要求插入、删除或重新排列后续 Stage。MainAgent 应更新完整 Workflow，但不能因此自动执行新的 Stage。

## 失败与诊断

如果当前 Stage 执行失败：

- 保存已有的失败 Evidence。
- 将该 Stage 标记为 `failed`，不得标记为 `completed`。
- 向用户说明失败结果。
- 当前轮立即结束，不得再次委派同一个 Stage。
- 不得自动进入 Diagnosis，也不得自动修改 Flowgraph。

Diagnosis、Flowgraph Build 和 Flowgraph Verification 是相互独立的 Stage，必须遵守 Stage 候选库中的职责边界。

## 真实 RF 执行

只有当 `physical_rf_execution` 是当前 Stage，并且 Flowgraph 已准备完成时，才向用户请求本次执行确认。

调用 `request_user_decision(kind='approval', permission='rf.start')`。

用户确认仅适用于当前 Workflow、当前 Flowgraph 版本和本次执行。

确认后执行限定时长的 RF 任务，并记录任务已经停止。
