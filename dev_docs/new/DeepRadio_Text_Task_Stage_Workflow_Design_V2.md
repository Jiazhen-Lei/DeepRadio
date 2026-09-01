# DeepRadio Dynamic Workflow 实施方案 V2

## 1. 文档目标

DeepRadio 面向 GNU Radio Companion（GRC）提供原生智能体能力，使用户能够通过自然语言完成通信系统仿真、发射机与接收机构建、工程修改、故障诊断、结果观测和硬件配置。

本方案将现有 DeepRadio Agent 升级为 Task 驱动的 Dynamic Workflow：MainAgent 识别任务类型，从 Task 候选库取得建议 Stage 骨架，结合当前工程状态生成本次 Workflow，并在每个 Stage 中调度一个或多个领域 Subagent。Workflow 通过 YAML 持久化，工程事实继续由现有 SharedState 管理。

V2 以最小改动、运行可控、状态可恢复和研究可复现为主要目标。

## 2. 设计目标

### 2.1 功能目标

- 识别用户输入所属的无线电任务类型及关键参数；
- 根据任务类型和当前工程状态生成本次专用 Workflow；
- 以用户交互边界划分 Stage；
- 在 Stage 内动态调度领域 Subagent、Skill 和 Tool；
- 支持用户确认、取消、反馈、修改和恢复；
- 支持校验、仿真、诊断、修复、重验闭环；
- 保持有 LLM 与确定性降级路径的状态语义一致；
- 向 GUI 提供当前任务、Stage、Checkpoint、产物和 Claim-Evidence 摘要。

### 2.2 工程目标

- 复用现有六个领域 Subagent；
- 复用 `ServiceAgent.step(text) -> AgentReply` GUI 契约；
- 复用 `SharedState`、`PolicyGateway`、`ClaimStore`、snapshot 和工具注册表；
- 由代码控制 Workflow 状态迁移，模型负责受约束的意图补全、Stage 内协作和结果叙述；
- 所有工程变更继续经过确定性 Tool 和 PolicyGateway；
- 以单活动 Workflow、串行 Stage 作为首版运行模型。

## 3. 核心概念

```text
User Turn
└── Intent
    └── Task Candidate
        └── Workflow Instance
            └── Stage
                └── Subagent Invocation
                    └── Skill + Tool
```

### 3.1 User Turn

用户本轮输入，以及它与当前 Workflow 的关系。推荐类型：

```text
new_task     创建新任务
answer       回答规格问题
adjustment   调整尚未执行的方案
feedback     对已有产物提出修改
approval     批准待执行操作
rejection    拒绝待执行操作
cancel       终止当前 Workflow
```

### 3.2 Intent

对 User Turn 的结构化解释，包含任务类型、领域槽位、置信度和待补信息。

### 3.3 Task Candidate

一类用户目标的建议 Workflow 模板。它定义必要上下文、建议 Stage、完成条件、条件分支和重试边界。

### 3.4 Workflow Instance

Task Candidate 针对某次用户请求和当前工程状态实例化后的运行对象。每个会话首版同时维护一个活动 Workflow。

### 3.5 Stage

两个用户交互点之间的一段连续执行。一个 Stage 可以调用一个或多个 Subagent，并以明确的完成契约结束。

### 3.6 Subagent Invocation

Stage 内一次实际的领域任务委派。调用内容由 TaskCard 描述，结果由 ResultEnvelope 返回；完整历史写入 `events.jsonl`。

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│ GRC Workspace                                                │
│ AgentPanel · ClaimsPanel · Flowgraph Canvas                  │
└──────────────────────────────┬───────────────────────────────┘
                               │ user turn
┌──────────────────────────────▼───────────────────────────────┐
│ ServiceAgent                                                 │
│ GUI 契约 · 会话上下文 · 结果折叠 · 主/降级执行路由             │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ WorkflowEngine                                               │
│ Intent 识别 · Task 实例化 · Stage 迁移 · Checkpoint · 恢复    │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
┌───────────────▼──────────────┐ ┌────────────▼────────────────┐
│ Task Candidate Catalog       │ │ Stage Executor              │
│ 建议 Stage 与分支             │ │ TaskCard · ResultEnvelope   │
└──────────────────────────────┘ └────────────┬────────────────┘
                                              │
┌─────────────────────────────────────────────▼────────────────┐
│ Domain Subagents                                             │
│ Spec · RadioDesign · Flowgraph · Verification · Diagnosis    │
│ Hardware                                                     │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Skill + Deterministic Tools                                  │
│ registry · design_link · build · critic · sim · state        │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ GNU Radio Core                                               │
│ FlowGraph · Generator · Runtime · SDR Blocks                 │
└──────────────────────────────────────────────────────────────┘
```

## 5. Task 候选库

### 5.1 首版 Task 类型

| Task Type | 用户目标 | 主要产物 |
|---|---|---|
| `END_TO_END_SIM` | 构建通信系统并完成仿真验证 | `.grc`、指标、图、Claims |
| `TX_BUILD` | 构建发射链路 | 发射机 `.grc`、结构校验 |
| `RX_BUILD` | 构建接收链路 | 接收机 `.grc`、接收质量结果 |
| `DIAGNOSE` | 定位当前工程问题 | 诊断结论、Evidence、修复建议 |
| `MODIFY_PROJECT` | 修改已有工程 | 新 Flowgraph version、重验结果 |
| `OBSERVE` | 查看工程结构、频谱、星座或指标 | 测量结果和图片 |
| `HARDWARE_CONFIGURE` | 规划或配置 SDR 系统 | 硬件配置、预检或阻塞说明 |

Task 类型按最终产物、验收方式和安全边界划分。调制方式、信道类型、中心频率、采样率等作为领域槽位进入 Task 实例。

### 5.2 建议 Stage 编排

| Task Type | 建议 Stage |
|---|---|
| `END_TO_END_SIM` | `spec_alignment? → build_and_verify` |
| `TX_BUILD` | `spec_alignment? → tx_build_and_validate` |
| `RX_BUILD` | `rx_spec_alignment? → rx_build_and_verify` |
| `DIAGNOSE` | `inspect_and_diagnose → repair_confirmation? → repair_and_verify?` |
| `MODIFY_PROJECT` | `inspect_and_plan → change_confirmation? → apply_and_verify` |
| `OBSERVE` | `inspect_and_measure` |
| `HARDWARE_CONFIGURE` | `hardware_precheck → hardware_confirmation → configure_and_check` |

`?` 表示按领域槽位、Policy 或执行结果动态启用的条件 Stage。

### 5.3 Task Catalog 结构

Task 候选库使用单一文件：

```text
grc/agent/workflow/task_catalog.yaml
```

示例：

```yaml
schema_version: 1

task_candidates:
  MODIFY_PROJECT:
    description: 修改已有 GNU Radio 工程

    required_context:
      - current_project
      - requested_change

    stages:
      - id: inspect_and_plan
        interaction: autonomous
        recommended_agents:
          - spec_agent
          - flowgraph_agent
        completion:
          - change_plan_created
        on:
          completed: change_confirmation
          errored: stop

      - id: change_confirmation
        interaction: conditional_checkpoint
        when:
          - modulation_change
          - multi_block_change
          - hardware_change
        on:
          approved: apply_and_verify
          rejected: cancelled
          not_required: apply_and_verify

      - id: apply_and_verify
        interaction: autonomous
        recommended_agents:
          - flowgraph_agent
          - verification_agent
          - diagnosis_agent
        completion:
          - flowgraph_saved
          - structural_validation_completed
          - affected_claims_evaluated
        max_attempts: 2
        on:
          passed: completed
          failed_with_improvement: apply_and_verify
          failed_without_improvement: waiting_user
          errored: stop
```

### 5.4 Task 候选实例化

MainAgent 与 WorkflowEngine 按以下顺序生成 Workflow：

1. 识别 User Turn 与当前 Workflow 的关系；
2. 提取 Task Type 和领域槽位；
3. 读取对应 Task Candidate；
4. 根据现有工程、缺失槽位和 Policy 裁剪条件 Stage；
5. 填充 Stage 输入、完成契约和最大尝试次数；
6. 校验 Task、Stage 和 Subagent 名称；
7. 原子写入 `workflow.yaml`；
8. 激活第一个 Stage。

MainAgent 可以在候选库允许的范围内激活条件 Stage、填写参数和选择推荐 Subagent。WorkflowEngine 负责状态写入、转移和恢复。

### 5.5 控制候选库规模

新增 Task Type 主要依据：

- 最终产物发生变化；
- 验收标准发生变化；
- 用户确认或安全边界发生变化。

BPSK、QPSK、QAM 和 OFDM 仿真共享 `END_TO_END_SIM`，它们通过 `modulation` 等领域槽位区分。构建、诊断、修改与硬件配置分别具有独立产物和治理边界，因此保留为不同 Task Type。

复合请求首版选择终态范围最大的 Task Candidate，并通过条件 Stage 补齐前置工作。例如“构建 BPSK 并配置到 USRP”可实例化 `HARDWARE_CONFIGURE`，同时启用 `build_and_verify` 前置 Stage。

## 6. Intent 识别

### 6.1 输出契约

```yaml
turn_relation: new_task
task_type: END_TO_END_SIM
confidence: 0.94
slots:
  direction: transceiver
  modulation: bpsk
  channel: awgn
  carrier_frequency: 2400000000
  sample_rate: null
  hardware: null
  requested_metrics:
    - evm
    - spectrum
  success_conditions:
    - EVM < 10%
missing_slots: []
```

### 6.2 识别策略

意图识别采用确定性解析与结构化 LLM 补全相结合的方式：

1. 运行时优先处理确认、拒绝、取消、只读分析和显式换配方；
2. 规则解析调制方式、频率、采样率、带宽、符号率、信道、指标阈值和硬件类型；
3. LLM 对任务类型、输入关系和剩余语义进行结构化补全；
4. 输出经过 Schema 和 Task Catalog 校验；
5. 影响架构或硬件安全的缺失槽位进入 `spec_alignment` Checkpoint；
6. 展示偏好和专业度画像独立于技术意图。

### 6.3 与活动 Workflow 的关系

存在活动 Workflow 时，Intent Router 先判断本轮属于：

- 当前 Checkpoint 的回答；
- 对当前方案的调整；
- 对当前产物的反馈；
- 新任务请求；
- 取消当前任务。

该判断决定继续、回退、失效或归档当前 Workflow。

## 7. Workflow YAML

### 7.1 会话目录

```text
local/agent_sessions/<session_id>/
├── state.json
├── workflow.yaml
├── events.jsonl
├── snapshots/
├── work/
└── final/
```

### 7.2 Workflow 实例示例

```yaml
schema_version: 1
catalog_version: 1

workflow_id: wf-a18f
task_type: MODIFY_PROJECT
execution_status: waiting
revision: 2
base_project_version: 3
current_stage: change_confirmation

intent:
  raw_text: 把当前 BPSK 改成 QPSK
  turn_relation: new_task
  confidence: 0.96
  slots:
    target_modulation: qpsk

stages:
  - id: inspect_and_plan
    execution_status: completed
    attempt: 1
    outcome: passed
    result:
      change_type: modulation_change
      from_recipe: bpsk_awgn
      to_recipe: qpsk_awgn

  - id: change_confirmation
    execution_status: waiting
    attempt: 0
    checkpoint:
      id: cp-01
      decision_status: pending
      reason: modulation_change

  - id: apply_and_verify
    execution_status: pending
    attempt: 0
    max_attempts: 2
```

### 7.3 Workflow 保存内容

`workflow.yaml` 保存：

- Workflow 标识、Task Type 和版本；
- 原始 Intent 与结构化槽位；
- 当前 Stage；
- Stage 执行状态、尝试次数和结果摘要；
- Checkpoint 状态；
- Workflow 与工程的版本关联。

Skill 内容、Tool Schema 和完整调用历史分别由 Skill 目录、Subagent Registry 与 `events.jsonl` 管理。

## 8. 状态模型

### 8.1 执行状态

Workflow 和 Stage 使用：

```text
pending | running | waiting | completed | errored | invalidated
```

| 状态 | 含义 |
|---|---|
| `pending` | 已创建，等待成为当前 Stage |
| `running` | 正在执行 Subagent 或 Tool |
| `waiting` | 等待用户、Policy 或外部条件 |
| `completed` | Stage 已正常产生符合契约的结果 |
| `errored` | 工具异常、超时或结果协议校验失败 |
| `invalidated` | 上游输入或工程版本变化，已有结果需要更新 |

首版采用单一 `current_stage`，调度器直接判断当前 Stage 是否可执行，因此无需持久化 `ready`。

### 8.2 用户确认状态

Checkpoint 使用：

```text
pending | approved | rejected
```

对应关系：

| Checkpoint | Stage execution_status | 含义 |
|---|---|---|
| `pending` | `waiting` | 等待用户决定 |
| `approved` | `completed` | 确认完成，进入批准分支 |
| `rejected` | `completed` | 确认完成，进入拒绝或取消分支 |

### 8.3 验证结论

产生领域判断的 Stage 使用：

```text
passed | failed | inconclusive
```

字段名为 `outcome`。例如结构校验正常运行并发现端口类型错误：

```yaml
execution_status: completed
outcome: failed
```

校验进程超时：

```yaml
execution_status: errored
outcome: inconclusive
```

三类字段分别表达执行生命周期、用户决定和领域结论。

## 9. WorkflowEngine

### 9.1 对外接口

```python
class WorkflowEngine:
    def consume_turn(self, user_text, shared_state): ...
    def instantiate(self, intent, shared_state): ...
    def current_stage(self): ...
    def start_stage(self): ...
    def accept_result(self, result_envelope): ...
    def resolve_checkpoint(self, decision): ...
    def invalidate(self, cause, project_version): ...
    def save(self): ...
```

### 9.2 单轮执行逻辑

```text
ServiceAgent.step(user_text)
  → WorkflowEngine.consume_turn
  → 创建或恢复 Workflow
  → 处理 Checkpoint / 调整 / 反馈
  → 获取 current_stage
  → StageExecutor.execute
  → 校验 ResultEnvelope
  → WorkflowEngine.accept_result
  → 保存 workflow.yaml 与 state.json
  → ServiceAgent._fold
  → AgentReply
```

一个用户轮次可以连续完成同一 autonomous Stage 内的多个 Subagent 调用。遇到 Checkpoint、外部等待、重试上限或 Workflow 终态时返回 GUI。

### 9.3 Stage 转移

WorkflowEngine 根据 Task Catalog 的 `on` 规则和 ResultEnvelope 转移：

```text
execution completed + outcome passed
→ 下一 Stage 或 Workflow completed

execution completed + outcome failed + 有改进空间
→ 当前 Stage attempt + 1

execution completed + outcome failed + 无改进
→ waiting，向用户报告证据和选择

execution errored + attempt 未达上限
→ 重试当前 Stage

execution errored + attempt 达上限
→ Workflow errored
```

### 9.4 版本检查

TaskCard 携带：

```yaml
workflow_id: wf-a18f
stage_id: apply_and_verify
workflow_revision: 2
base_project_version: 3
```

ResultEnvelope 返回相同字段。WorkflowEngine 接收结果时比较 Workflow revision 和 Flowgraph version；版本变化时将结果记为 stale event，并重新计算当前 Stage。

## 10. TaskCard 与 ResultEnvelope

### 10.1 TaskCard

```yaml
task_id: task-12ab
workflow_id: wf-a18f
stage_id: apply_and_verify
workflow_revision: 2
base_project_version: 3
target_agent: flowgraph_agent
instruction: 将当前工程切换为 qpsk_awgn，并保留快照
inputs:
  current_grc: /path/current.grc
  target_recipe: qpsk_awgn
expected_results:
  - flowgraph_saved
  - project_version_incremented
```

### 10.2 ResultEnvelope

```yaml
task_id: task-12ab
workflow_id: wf-a18f
stage_id: apply_and_verify
workflow_revision: 2
base_project_version: 3
ok: true
outcome: passed
produced_claims: []
proposed_changes: []
artifacts:
  grc_path: /path/qpsk_awgn.grc
note: QPSK 流图已生成并通过结构校验
```

ResultEnvelope 在写入 SharedState 和 Workflow 前执行结构校验、版本校验和 Policy 结果检查。

## 11. StageExecutor 与 MainAgent

### 11.1 MainAgent 职责

- 解释 Intent；
- 从 Task Catalog 选择候选；
- 为当前 Stage 组织 TaskCard；
- 在 Stage 推荐范围内选择 Subagent；
- 汇总冲突、证据和产物；
- 生成面向用户的最终说明。

### 11.2 WorkflowEngine 职责

- 保存执行状态；
- 执行状态迁移；
- 管理 Checkpoint；
- 校验 TaskCard 与 ResultEnvelope；
- 处理版本、重试和失效；
- 维护当前 Stage。

### 11.3 StageExecutor 职责

- 按 Stage 范围装配 MainAgent 和候选 Subagent；
- 把共享 ToolContext 注入确定性工具；
- 记录 Subagent 委派和 Tool 调用；
- 将模型输出收敛为 ResultEnvelope；
- 在无 LLM 环境调用对应的确定性 Stage handler。

主 Agent 的工具权限按当前 Stage 收敛，领域写操作由 Subagent 的工具白名单和 PolicyGateway共同约束。

## 12. Subagent、Skill 与 Tool

### 12.1 六个领域 Subagent

| Subagent | 主要职责 | Skills | 主要 Tools |
|---|---|---|---|
| `spec_agent` | 提取规格、缺失槽位和成功条件 | `grc-spec` | `spec_clarify`、`spec_commit` |
| `radio_design_agent` | 块检索、链路设计和 recipe 选择 | `grc-block-rag` | `select_recipe`、`search_blocks`、`describe_block`、`list_examples` |
| `flowgraph_agent` | 构建、检查和修改 Flowgraph | `grc-build`、`grc-block-rag` | `design_flowgraph`、`inspect_flowgraph`、`apply_flowgraph_patch` |
| `verification_agent` | 结构校验、仿真、测量和 Claims | `grc-critic`、`grc-sim` | validate、simulate、metric、plot、verify |
| `diagnosis_agent` | 依据 Evidence 诊断并提出修复 | `grc-diagnosis`、`grc-critic` | `diagnose_by_metric`、`explain_error` |
| `hardware_agent` | SDR 配置、安全预检和硬件状态 | `grc-hardware`、`grc-build` | configure、preflight、device status |

六个角色覆盖首版 Task Candidate，V2 沿用现有角色集合。

### 12.2 Skill 配置调整

Subagent 注册从单个 skill 字符串调整为 `skills[]`，Stage 通过 Subagent 间接获得 Skill。Skill 负责领域规则、步骤、输入输出契约和边界，Tool 白名单负责实际执行权限。

首版重点补充：

- `grc-spec`：按 Task Type 定义必要槽位和高影响问题；
- `grc-build`：补充工程检查与原子 patch 协议；
- `grc-critic`：统一结构错误与修复建议格式；
- `grc-sim`：明确不同信号类型对应的指标和 probe；
- `grc-diagnosis`：增加 BER、同步、运行错误和参数异常诊断路径；
- `grc-hardware`：增加预检、确认、启停与风险记录契约。

### 12.3 Tool 能力评估

当前工具链能够支撑：

- tone、BPSK、QPSK 和 OFDM 骨架 recipe 建图；
- Flowgraph 结构校验；
- 无头仿真；
- EVM、频谱主峰和基础绘图；
- 单块参数修改；
- Claim-Evidence、快照和版本失效；
- 硬件配置占位。

Dynamic Workflow MVP 优先补充：

1. `inspect_flowgraph`：返回块、连接、参数、路径和版本摘要；
2. `apply_flowgraph_patch`：原子执行 add/remove/set/connect/disconnect，并集成快照、校验和回滚；
3. LangChain 桥接：`list_examples`、`explain_error`、`plot_constellation`、`plot_eye`；
4. RX 双 probe：提供发送比特与接收比特，形成可信 BER Evidence；
5. Intent 槽位解析：频率、采样率、带宽、符号率、方向、设备和指标阈值；
6. TaskCard/ResultEnvelope 运行时校验。

硬件阶段后续扩展：

```text
discover_devices
hardware_preflight
configure_device
start_flowgraph
stop_flowgraph
emergency_stop
```

真实发射能力以设备检测、安全检查、用户确认和停止能力为启用条件。

## 13. `state.json` 与 `workflow.yaml`

### 13.1 `state.json`：领域事实

保存：

- RadioSpec；
- ProjectState；
- 当前 `.grc` 路径和 Flowgraph version；
- Claims 与 Evidence；
- 锁定约束；
- snapshot 索引；
- 工程配置。

它回答“当前无线电工程是什么”。

### 13.2 `workflow.yaml`：执行控制

保存：

- 当前 Task Type；
- Workflow 和 Stage 状态；
- Intent 和槽位；
- Checkpoint；
- attempt、outcome 和结果摘要；
- 当前工程版本引用。

它回答“系统为了完成当前目标正在做什么”。

### 13.3 `events.jsonl`：追加式历史

保存：

- 用户输入与决定；
- Workflow 创建和转移；
- Stage 开始、完成、等待、错误和失效；
- Subagent 委派；
- Tool 调用；
- Policy 决策；
- 产物发布；
- stale result 和恢复事件。

三个文件形成事实、控制和历史的明确分工。

## 14. Checkpoint 与 Policy

### 14.1 默认 Checkpoint

以下情况生成用户 Checkpoint：

- 缺少会改变链路结构的关键槽位；
- 修改已有工程的调制方式或 recipe；
- 多块结构修改；
- 硬件配置和真实设备操作；
- 诊断后准备实施较大范围修复；
- 连续尝试未改善结果。

新建纯仿真工程在规格充分时可以直接进入 autonomous Stage。

### 14.2 Policy 决策

```text
ALLOW    允许当前操作
PROPOSE  生成修改方案并创建 Checkpoint
CONFIRM  等待明确批准
DENY     终止该操作并记录原因
```

WorkflowEngine 将 Policy 结果转换为 Stage 状态和 Checkpoint。GUI 的确认/取消按钮继续通过 `ServiceAgent.step()` 提交决定。

## 15. 失效、回退与恢复

### 15.1 失效传播

| 变化 | 影响范围 |
|---|---|
| 规格架构字段变化 | 设计、建图、验证 Stage |
| recipe 或调制变化 | 建图、验证 Stage 和相关 Claims |
| Flowgraph 保存 | 验证 Stage 和工程 Claims |
| 成功条件变化 | 对应 Claim Evaluation |
| 表达档位变化 | 用户叙述和 GUI 展示 |

失效时保留旧 Stage 结果与事件，并提升 Workflow revision。

### 15.2 循环控制

```text
build_and_verify
  ├─ passed → completed
  ├─ failed + 有明确改进 → attempt + 1
  └─ failed + 无改进或达到上限 → waiting_user
```

首版默认 `max_attempts: 2`。每次尝试需产生新的参数、结构或 Evidence；连续结果无变化时提前停止。

### 15.3 会话恢复

启动或恢复 ServiceAgent 时：

1. 加载 `state.json`；
2. 加载并校验 `workflow.yaml`；
3. 比较 `base_project_version`；
4. 对 `running` Stage 执行中断恢复处理；
5. 恢复 Checkpoint 或当前 Stage；
6. 将恢复事件写入 `events.jsonl`。

进程中断时的 `running` Stage 恢复为 `pending`，attempt 保留并重新执行。

## 16. GUI 接入

### 16.1 AgentReply 扩展

新增紧凑字段：

```python
workflow_digest = {
    "workflow_id": "wf-a18f",
    "task_type": "MODIFY_PROJECT",
    "execution_status": "waiting",
    "current_stage": "change_confirmation",
    "stage_index": 2,
    "stage_total": 3,
    "waiting_reason": "modulation_change",
}
```

### 16.2 工作区显示

复用 ClaimsPanel 现有活动条：

```text
任务：修改已有工程
阶段：变更确认 2/3
状态：等待用户确认
BPSK → QPSK    [确认] [取消]
```

对话区继续展示用户与 DeepRadio 的自然语言交流；测量区展示 EVM、BER、频谱等指标；Claims 区展示验证状态和 Evidence。

### 16.3 GUI 命令入口

- “确认”和“取消”提交结构化 Checkpoint 决定；
- “改规格”转换为 adjustment User Turn；
- 画布保存触发 `flowgraph_changed`，由 WorkflowEngine 失效验证结果；
- “撤销到上一版本”恢复 snapshot，并同步 Workflow revision；
- “重置”归档活动 Workflow，创建新会话状态。

## 17. 主路径与降级路径

### 17.1 Deepagents 路径

StageExecutor 根据当前 Stage 过滤 Subagent，并传入 TaskCard。MainAgent 在推荐 Agent 范围内完成协作，Subagent 通过确定性工具操作同一 ToolContext。

### 17.2 确定性降级路径

每个首版 autonomous Stage 配置一个确定性 handler，例如：

| Stage | Deterministic Handler |
|---|---|
| `build_and_verify` | `design_link` |
| `inspect_and_measure` | validate + simulate + metric + plot |
| `inspect_and_diagnose` | validate + `debug_by_metric` |
| `apply_and_verify` | recipe switch 或 `apply_grc_diff` + verify |
| `hardware_precheck` | 当前配置检查与能力报告 |

两条路径均通过 WorkflowEngine 写入相同的 Stage 状态、outcome、Checkpoint 和事件。

## 18. 最小代码改动范围

### 18.1 新增

```text
grc/agent/workflow/
├── __init__.py
├── schema.py
├── engine.py
└── task_catalog.yaml
```

- `schema.py`：Workflow、Stage、Checkpoint、Intent 的数据结构与校验；
- `engine.py`：候选实例化、Stage 迁移、Checkpoint、重试、失效和 YAML 保存；
- `task_catalog.yaml`：Task 候选和建议 Stage；
- `__init__.py`：稳定入口。

### 18.2 调整

| 文件 | 调整内容 |
|---|---|
| `service/adapter.py` | `step()` 接入 WorkflowEngine，统一新建、恢复和执行 |
| `service/orchestrator.py` | 增加 Stage 范围装配与执行入口 |
| `service/subagents.py` | Skill 改为列表，支持按 Stage 过滤 Subagent |
| `service/tools_lc.py` | 按 Subagent/Stage 过滤 Tool，并补必要桥接 |
| `service/session_store.py` | 增加 workflow 路径、原子保存和归档 |
| `state/shared_state.py` | 扩展 TaskCard/ResultEnvelope 的 Workflow 字段 |
| `schema.py` | AgentReply 增加 `workflow_digest` |
| `gui/AgentPanel.py` | User Turn 与 Workflow 状态接入 |
| `gui/ClaimsPanel.py` | 展示 Task、Stage、进度和等待原因 |

现有 `design_link`、registry、SharedState、Policy、ClaimStore、snapshot 和 GUI 主体继续复用。

## 19. 实施阶段

### Phase 1：Workflow 控制面

- 建立 Task Catalog；
- 实现 Workflow Schema 和 YAML 原子读写；
- 实现七类 Task 的实例化；
- 实现串行 Stage 状态迁移；
- 接入 Checkpoint、重试、失效和恢复；
- 扩展 AgentReply 和活动条；
- 为 WorkflowEngine 编写纯单元测试。

此阶段沿用现有确定性工具，首先验证 Dynamic Workflow 机制。

### Phase 2：Stage 范围执行

- orchestrator 按 Stage 过滤 Subagent；
- Subagent 注册表统一 Skills 和 Tools；
- TaskCard/ResultEnvelope 运行时校验；
- deepagents 与确定性 handler 输出相同 Stage 语义；
- 补充 Workflow 事件。

### Phase 3：领域工具补全

- `inspect_flowgraph`；
- `apply_flowgraph_patch`；
- RX BER 双 probe；
- Verification 绘图与错误解释工具；
- Diagnosis 指标和错误类型扩展；
- Tx/Rx recipe 覆盖扩展。

### Phase 4：硬件闭环

- 设备发现；
- 连接与能力预检；
- 频率、采样率、增益和功率检查；
- 用户确认；
- 启动、停止与紧急停止；
- 部署后测量和故障恢复。

## 20. 验收场景

### 20.1 Workflow

1. 完整 BPSK 仿真请求直接进入 `build_and_verify` 并完成；
2. 缺少关键调制参数时进入 `spec_alignment`；
3. BPSK 改 QPSK 停在 `change_confirmation`，批准后继续；
4. 用户拒绝修改后 Workflow 进入 `cancelled` 终态；
5. 只诊断请求完成测量和诊断，工程版本保持不变；
6. 校验失败进入重试，达到上限后等待用户；
7. 重启 ServiceAgent 后恢复当前 Stage 和 Checkpoint；
8. 画布保存后验证 Stage 与相关 Claims 失效；
9. 旧版本 ResultEnvelope 被识别为 stale；
10. deepagents 与确定性路径产生一致的状态事件。

### 20.2 领域能力

1. tone_noise 输出频谱；
2. BPSK/QPSK 输出 `.grc`、EVM、星座、频谱和眼图；
3. RX BPSK 输出发送/接收比特关联的 BER；
4. 单参数修改产生 snapshot、新版本和重验 Evidence；
5. recipe 修改经过确认并使旧 Claims 失效；
6. 结构错误生成具体到块、端口或参数的诊断；
7. 硬件请求展示配置、预检状态和确认点。

### 20.3 GUI

1. 活动条显示 Task、Stage、序号和状态；
2. Checkpoint 按钮与 Workflow 状态一致；
3. CONFIRM 阶段保持当前画布；
4. 交付后刷新 `.grc`；
5. 指标和 Claims 对应当前 Flowgraph version；
6. 重置、撤销和画布保存与 WorkflowEngine 同步。

## 21. CHI 研究数据

Workflow 事件为后续实验提供统一数据源：

```text
intent_classified
workflow_created
stage_started
subagent_invoked
tool_called
checkpoint_opened
checkpoint_resolved
stage_completed
stage_errored
stage_invalidated
workflow_completed
workflow_cancelled
stale_result_discarded
```

可量化指标包括：

- Task 完成率；
- 首次 Workflow 命中率；
- 用户确认次数；
- Stage 和 Tool 调用数；
- 无效工具调用率；
- 失败恢复成功率；
- Flowgraph 版本数；
- Claim 通过率；
- 总时延与用户等待时间；
- 不同专业度用户的交互轮次与完成质量。

建议保留当前“一句话直出 baseline”，并增加“自由 prompt 路由”与“Task Catalog Dynamic Workflow”两种实验条件。

## 22. 首版交付边界

首版范围：

- 单会话、单活动 Workflow；
- 单一主要 Task Type；
- 串行 Stage；
- Stage 内顺序调用多个 Subagent；
- 条件 Stage、Checkpoint、重试和失效恢复；
- 仿真优先；
- session 工程的构建、观测、诊断和修改；
- 硬件配置与预检状态展示。

后续根据真实使用数据扩展多 Workflow 排队、并行只读 Stage、跨工程任务和真实 SDR 生命周期管理。

## 23. 最终方案

DeepRadio V2 的核心实现方式为：

```text
一个活动 Workflow
+ 一个主要 Task Type
+ 一个受控 Task Candidate Library
+ 串行、可恢复的 Stage 状态机
+ Stage 内动态 Subagent 协作
+ Subagent 级 Skill/Tool 权限
+ SharedState / Workflow / Event 三类持久化
+ Policy / Checkpoint / Claim-Evidence 闭环
```

Task Candidate 提供建议 Stage 骨架，MainAgent 根据 Intent、当前工程和执行结果完成受控定制，WorkflowEngine 维护唯一执行状态，现有六个领域 Subagent 和确定性工具继续承担具体无线电任务。该结构能够以较小改动形成可演示、可验证、可恢复和可用于 CHI 实验的 Dynamic Workflow。
