# DeepRadio Dynamic Workflow 代码修改整理

本文按模块分类，以文件为最小单位说明 MainAgent 动态 Workflow 重构涉及的代码修改。

本次共涉及 51 个文件，不包含用户原有的 `note.ipynb` 删除状态。

## 1. 架构方案文档

- `dev_docs/new/【jensen】DeepRadio_MainAgent_Dynamic_Workflow_Refactor.md`（新增）
  - 记录新链路、MainAgent/SubAgent 职责、动态 Workflow 模型、Skill 与宿主约束边界、权限模型、删除项和验收条件。

## 2. 动态 Workflow

- `grc/agent/workflow/dynamic.py`（新增）
  - 实现 `DynamicIntent`、`DynamicStage`、`DynamicWorkflow` 和持久化 Store。
  - 支持 MainAgent 动态创建、替换、增删和重排 Stage。
  - 校验 Workflow revision、工程版本、SubAgent 名称和完成证据。
  - 处理执行中断恢复、用户决策和状态文件损坏。

- `grc/agent/workflow/__init__.py`（修改）
  - 删除旧 `WorkflowEngine` 和固定 Workflow 类型导出。
  - 改为导出动态 Workflow 类型。

### 删除的旧 Workflow 文件

- `grc/agent/workflow/engine.py`（删除）
  - 删除宿主控制的固定状态机、Stage 推进和规则编排主链。

- `grc/agent/workflow/schema.py`（删除）
  - 删除旧 Workflow、Stage、Checkpoint 数据模型。

- `grc/agent/workflow/task_catalog.yaml`（删除）
  - 删除 Task 到固定 Workflow、固定 Stage 顺序和 execution mode 的映射。

- `grc/agent/workflow/completion.py`（删除）
  - 删除与固定 Stage 绑定的完成条件体系。

- `grc/agent/workflow/intent_alignment.py`（删除）
  - 删除宿主规则驱动的 Intent 对齐和确认控制链。

- `grc/agent/workflow/llm_planner.py`（删除）
  - 删除独立于 MainAgent 的二级 LLM Planner。

- `grc/agent/workflow/plan_compiler.py`（删除）
  - 删除从固定任务目录编译 Workflow 的逻辑。

- `grc/agent/workflow/planning.py`（删除）
  - 删除 effect level、Stage 工具范围和旧编排辅助模型。

- `grc/agent/workflow/revision.py`（删除）
  - 删除基于规则分析用户文本并修改计划的路径。

- `grc/agent/workflow/narration.py`（删除）
  - 删除旧 Workflow 状态机绑定的叙述生成逻辑。

## 3. MainAgent 服务链

- `grc/agent/service/mainagent_service.py`（新增）
  - 提供新的 `ServiceAgent` 主实现，保持 GUI 接口不变。
  - 内部只调用 MainAgent，不再运行固定 Stage 执行链。
  - 负责会话状态、Workflow 持久化、Artifact/Claim 汇总、用户确认和 UI digest 投影。
  - 管理 RF 授权绑定、画布同步、工程版本、快照恢复和异常状态。
  - LLM 不可用时返回错误，不再进入规则 Workflow。

- `grc/agent/service/workflow_tools.py`（新增）
  - 提供 MainAgent 专用的 `update_workflow` 和 `request_user_decision`。
  - SubAgent 不持有 Workflow 修改工具。

- `grc/agent/service/adapter.py`（修改）
  - 从完整混合编排器缩减为兼容导出。
  - 转发新的 `ServiceAgent`，保持原有 import 路径。

- `grc/agent/service/orchestrator.py`（修改）
  - `build_agent()` 不再接收固定 Stage。
  - MainAgent 只持有 Workflow 控制工具，领域工具全部交给 SubAgent。
  - 删除 Stage 工具交集、execution mode 和确定性 Workflow 降级逻辑。

- `grc/agent/service/subagents.py`（修改）
  - 精简 MainAgent 和 SubAgent Prompt。
  - 明确 MainAgent 是唯一用户接口。
  - SubAgent 只执行 TaskCard、调用领域工具和返回 Evidence。
  - 删除按 Stage 动态过滤 Agent 和工具的逻辑。

- `grc/agent/service/result_projector.py`（修改）
  - 删除依赖旧 `WorkflowEngine`、effect level 和固定 Stage 的控制状态投影。
  - 保留 Artifact、Tool Result 和 Claim 投影。

- `grc/agent/service/__init__.py`（修改）
  - 删除旧双链路和降级模式说明。
  - 改为新的 MainAgent/SubAgent 服务说明。

### 删除的旧执行链

- `grc/agent/service/stage_executor.py`（删除）
  - 删除 deterministic/hybrid Stage 执行器和旧 ResultEnvelope 路径。

- `grc/agent/service/stage_handlers.py`（删除）
  - 删除按固定 Stage 名称执行规则代码和工具序列的 Handler。

## 4. Orchestration Skill 与 Reference

- `grc/agent/skills/grc-orchestration/SKILL.md`（新增）
  - 定义 MainAgent 如何维护最短 Workflow、生成 TaskCard和委派 SubAgent。
  - 定义 Evidence 验证、重新编排和用户授权流程。

- `grc/agent/skills/grc-orchestration/references/stage_library.yaml`（新增）
  - 提供可选 Stage 能力库。
  - 只描述目标、推荐 SubAgent 和 Evidence，不规定任务流程和转移顺序。

- `grc/agent/skills/grc-build/references/build_output_contract.md`（修改）
  - 将旧 ResultEnvelope 字段改为 Workflow/Stage/工程版本、outcome、artifacts 和 evidence。

- `grc/agent/skills/grc-hardware/SKILL.md`（修改）
  - 将固定 Workflow Checkpoint 授权改为 MainAgent 显式请求用户授权。

## 5. Shared State 与策略

- `grc/agent/state/shared_state.py`（修改）
  - 将 `effect_level` 改为显式 `permission`。
  - 将 Runtime 授权字段改为 `requested_permission` 和 `granted_permissions`。
  - 保留旧状态文件的兼容迁移。
  - 删除不再使用的宿主 `TaskCard`、`ResultEnvelope` 和 `active_task`。

- `grc/agent/state/policy.py`（修改）
  - 删除调制方式、多块修改和硬件动作的语义规则。
  - 只保留用户锁定约束的保护。

- `grc/agent/state/intent_state.py`（修改）
  - 将 Intent 的语义所有者从宿主 Alignment Coordinator 改为 MainAgent。

- `grc/agent/state/__init__.py`（修改）
  - 删除旧 `TaskCard`、`ResultEnvelope` 导出。

## 6. 工具权限与硬件安全

- `grc/agent/tools/registry.py`（修改）
  - 用显式权限替换有序 effect level。
  - 删除 Stage 工具白名单和权限等级上限。
  - 保留只读约束、禁止权限、工具前置条件和 `rf.stop` 始终可用。

- `grc/agent/tools/hardware_tools.py`（修改）
  - RF 授权必须同时匹配当前 Workflow ID 和工程版本。
  - Arm/start 使用 `rf.start`，停止使用 `rf.stop`。
  - 设备探测和 BLE 验证支持持久证据与当前调用证据。
  - 保留设备范围、运行时限、Arm 和紧急停止检查。

- `grc/agent/tools/ble_tools.py`（修改）
  - BLE PDU、波形和流图生成工具从 `ARTIFACT_WRITE` 改为 `project.write`。

- `grc/agent/tools/build_tools.py`（修改）
  - 建图、加块、设参和连接工具统一改为 `project.write`。

- `grc/agent/tools/design_link.py`（修改）
  - `design_flowgraph` 改用 `project.write` 权限。

- `grc/agent/tools/diagnosis_checks.py`（修改）
  - 设备诊断检查改用 `device.read`。

- `grc/agent/tools/diagnosis_experiment.py`（修改）
  - 诊断实验及报告输出改用 `project.write`。

- `grc/agent/tools/sim_tools.py`（修改）
  - 仿真、指标读取和绘图工具统一改用 `project.write`。

- `grc/agent/tools/state_tools.py`（修改）
  - 更新 Intent 所有权说明。
  - 规格、配方、Claim、SDR 配置和流图修改工具改用显式权限。

## 7. Knowledge

- `grc/agent/knowledge/spec_requirements.py`（修改）
  - 将槽位别名移到本文件，解除对已删除 `WorkflowEngine` 的依赖。

- `grc/agent/knowledge/__init__.py`（修改）
  - 删除旧 WorkflowEngine 相关说明。
  - 保留 Recipe 和块知识的按需导出。

## 8. UI 兼容

- `grc/gui/AgentPanel.py`（修改）
  - RF 确认识别同时兼容旧 `RF_RUN` 和新 `rf.start`。

- `grc/gui/workflow_presenter.py`（修改）
  - 将 `project.write`、`rf.start` 映射到现有确认卡片和按钮。
  - 不改变 UI 布局。

- `grc/gui/tests/test_workflow_presenter.py`（修改）
  - 增加动态 `rf.start` 权限仍能使用原有 RF 确认界面的回归测试。

## 9. 测试重构

- `grc/agent/tests/test_dynamic_architecture.py`（新增）
  - 覆盖动态 Workflow 重排、revision 和防伪 Evidence。
  - 覆盖用户决策、显式权限、RF 安全和 BLE 验证。
  - 覆盖 UI 服务契约、无规则降级和旧状态迁移。
  - 覆盖执行中断恢复和损坏状态保护。

### 删除的旧架构测试

- `grc/agent/tests/test_workflow.py`（删除）
  - 删除依赖固定 WorkflowEngine、Catalog 和 Stage 转移的大型测试集。

- `grc/agent/tests/test_execution_routing.py`（删除）
  - 删除 deterministic/hybrid/agentic 分流测试。

- `grc/agent/tests/test_plan_p12.py`（删除）
  - 删除固定计划编译和 P1/P2 编排测试。

- `grc/agent/tests/test_seven_tasks.py`（删除）
  - 删除七类任务到固定 Workflow 的映射测试。

- `grc/agent/tests/test_hardware.py`（删除）
  - 删除依赖旧 Checkpoint、effect level 和 Stage Handler 的硬件测试。

- `grc/agent/tests/test_ble.py`（删除）
  - 删除依赖旧固定 BLE Workflow 的集成测试。
  - 必要的协议与安全验证已迁移到新测试。

## 10. 验证结果

- Agent 架构与安全测试：17 项通过。
- GUI 回归测试：25 项通过。
- Python 编译检查通过。
- `git diff --check` 通过。
- 尚未执行真实 LLM 和 SDR 硬件联调。

