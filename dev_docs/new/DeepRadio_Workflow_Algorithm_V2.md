# DeepRadio Workflow 算法 V2

> 日期：2026-08-23  
> 读者：工作流设计、控制面语义  
> 本文写 Turn / Intent / Task / Workflow / Stage / Invocation，以及完成契约、确认点、失效与重试。  
> 代码路径、GUI 控件、点击 SOP 不在本文。落地文件见工程文档。
>
> 同族文档：
> - 产品：`DeepRadio_Product_V2.md`
> - 算法：`DeepRadio_Workflow_Algorithm_V2.md`（本文）
> - 工程：`DeepRadio_Engineering_V2.md`
> - 测试：`DeepRadio_Test_and_Experiment_V2.md`

---

## 1. 目标

把 DeepRadio 从「一次对话直接建图」升级为 Task 驱动的 Dynamic Workflow：识别任务类型，从候选库取得建议 Stage 骨架，结合当前工程实例化本次 Workflow，并在每个 Stage 内调度一个或多个领域 Subagent。

约束：

- 七类 Task，不要加第八类。调制方式、信道、频率、采样率是槽位。
- 单一活动 Workflow，串行 Stage。
- 代码控制状态迁移；模型只做受约束的意图补全、Stage 内协作和结果叙述。
- 有 LLM 与确定性降级两条路径，写入同一套 Stage 状态、outcome、Checkpoint 和事件。
- BLE 空口不是新 Task，而是 `HARDWARE_CONFIGURE` 且 `operation=deploy` 的条件 Stage。

---

## 2. 核心概念

```text
User Turn
└── Intent
    └── Task Candidate
        └── Workflow Instance
            └── Stage
                └── Subagent Invocation
                    └── Skill + Tool
```

| 概念 | 含义 |
|---|---|
| User Turn | 本轮用户输入，以及它与当前 Workflow 的关系 |
| Intent | 结构化解释：任务类型、槽位、置信度、待补信息 |
| Task Candidate | 一类用户目标的建议模板：必要上下文、Stage、完成条件、分支、重试 |
| Workflow Instance | 针对本次请求和当前工程实例化后的运行对象 |
| Stage | 两个用户交互点之间的连续执行，以完成契约结束 |
| Subagent Invocation | Stage 内一次委派：TaskCard 描述任务，ResultEnvelope 返回结果 |

### 2.1 User Turn 关系

```text
new_task     创建新任务
answer       回答规格问题
adjustment   调整尚未执行的方案
feedback     对已有产物提出修改
approval     批准待执行操作
rejection    拒绝待执行操作
cancel       终止当前 Workflow
```

存在活动 Workflow 时，先判断本轮属于哪一种关系，再决定继续、回退、失效或归档。低置信续跑保持同一 `workflow_id`；只有口头「新任务」或强 Task 类型切换才覆盖。缺规格补充和失败反馈必须保持 `workflow_id`。

---

## 3. 总体结构

```text
GRC Workspace
  → ServiceAgent（GUI 契约、会话、主/降级路由）
    → WorkflowEngine（Intent、实例化、Stage 迁移、Checkpoint、恢复）
         ├─ Task Catalog（建议 Stage 与分支）
         └─ StageExecutor（TaskCard / ResultEnvelope）
              → Domain Subagents
                   → Skill + Deterministic Tools
                        → GNU Radio Core
```

`WorkflowEngine` 是唯一控制面。不要另造第二套状态机。GUI 只消费摘要，不自己推进 Stage。

单轮逻辑：

```text
ServiceAgent.step(user_text)
  → WorkflowEngine.consume_turn
  → 创建或恢复 Workflow
  → 处理 Checkpoint / 调整 / 反馈
  → 获取 current_stage
  → StageExecutor.execute
  → 校验 ResultEnvelope
  → WorkflowEngine.accept_result
  → 保存控制状态与工程事实
  → ServiceAgent._fold
  → AgentReply
```

一个用户轮次可以连续完成同一 autonomous Stage 内的多个 Subagent 调用。遇到 Checkpoint、外部等待、重试上限或 Workflow 终态时返回 GUI。

---

## 4. Task 候选库

### 4.1 七类 Task

| Task Type | 用户目标 | 主要产物 |
|---|---|---|
| `END_TO_END_SIM` | 构建通信系统并完成仿真验证 | `.grc`、指标、图、Claims |
| `TX_BUILD` | 构建发射链路 | 发射机 `.grc`、结构校验 |
| `RX_BUILD` | 构建接收链路 | 接收机 `.grc`、接收质量结果 |
| `DIAGNOSE` | 定位当前工程问题 | 诊断结论、Evidence、修复建议 |
| `MODIFY_PROJECT` | 修改已有工程 | 新 Flowgraph version、重验结果 |
| `OBSERVE` | 查看结构、频谱、星座或指标 | 测量结果和图片 |
| `HARDWARE_CONFIGURE` | 规划、配置或受控部署 SDR | 硬件配置、预检、或受控发射状态 |

新增 Task Type 的依据是：最终产物变化、验收标准变化、或用户确认/安全边界变化。BPSK/QPSK/QAM/OFDM 共享 `END_TO_END_SIM`。

### 4.2 建议 Stage

| Task Type | 建议 Stage |
|---|---|
| `END_TO_END_SIM` | `spec_alignment? → build_and_verify` |
| `TX_BUILD` | `spec_alignment? → tx_build_and_validate` |
| `RX_BUILD` | `rx_spec_alignment? → rx_build_and_verify` |
| `DIAGNOSE` | `inspect_and_diagnose → repair_confirmation? → repair_and_verify?` |
| `MODIFY_PROJECT` | `inspect_and_plan → change_confirmation? → apply_and_verify` |
| `OBSERVE` | `inspect_and_measure` |
| `HARDWARE_CONFIGURE`（configure） | `hardware_precheck → hardware_confirmation → configure_and_check` |
| `HARDWARE_CONFIGURE`（deploy） | 见 §12 |

`?` 表示按领域槽位、Policy 或执行结果动态启用的条件 Stage。

实例化顺序：

1. 识别 User Turn 与当前 Workflow 的关系；
2. 提取 Task Type 和领域槽位；
3. 读取对应 Task Candidate；
4. 按现有工程、缺失槽位和 Policy 裁剪条件 Stage；
5. 填充 Stage 输入、完成契约和最大尝试次数；
6. 校验 Task、Stage 和 Subagent 名称；
7. 原子写入 Workflow 实例；
8. 激活第一个 Stage。

MainAgent 可在候选库允许的范围内激活条件 Stage、填写参数和选择推荐 Subagent。WorkflowEngine 负责状态写入、转移和恢复。

Catalog 示例（`MODIFY_PROJECT`）：

```yaml
stages:
  - id: inspect_and_plan
    interaction: autonomous
    recommended_agents: [spec_agent, flowgraph_agent]
    completion: [change_plan_created]
    on:
      completed: change_confirmation
      errored: stop

  - id: change_confirmation
    interaction: conditional_checkpoint
    when: [modulation_change, multi_block_change, hardware_change]
    on:
      approved: apply_and_verify
      rejected: cancelled
      not_required: apply_and_verify

  - id: apply_and_verify
    interaction: autonomous
    recommended_agents: [flowgraph_agent, verification_agent, diagnosis_agent]
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

---

## 5. Intent

### 5.1 输出契约

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
  requested_metrics: [evm, spectrum]
  success_conditions: ["EVM < 10%"]
missing_slots: []
```

### 5.2 识别策略

1. 运行时优先处理确认、拒绝、取消、只读分析和显式换配方；
2. 规则解析调制、频率、采样率、带宽、符号率、信道、指标阈值和硬件类型；
3. 低置信时可用结构化 LLM 补全任务类型、关系和剩余语义；未配置或失败时回退规则；
4. 不覆盖 `slot_sources == user` 的槽位；
5. 输出经过 Schema 和 Task Catalog 校验；
6. 影响架构或硬件安全的缺失槽位进入 `spec_alignment` Checkpoint；
7. 展示偏好和专业度画像独立于技术意图。

### 5.3 与活动 Workflow 的关系

先判断本轮是：当前 Checkpoint 的回答、对当前方案的调整、对当前产物的反馈、新任务、或取消。该判断决定继续、回退、失效或归档。

---

## 6. 状态模型

### 6.1 执行状态

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

首版采用单一 `current_stage`。调度器直接判断当前 Stage 是否可执行，因此无需持久化 `ready`。

CONFIRM 不会把 Workflow 标成 `completed`。所有 Stage completion 为 true 后才能 `completed`。空 Invocation 不得 vacuously pass。

### 6.2 用户确认状态

Checkpoint 使用 `pending | approved | rejected`。

| Checkpoint | Stage execution_status | 含义 |
|---|---|---|
| `pending` | `waiting` | 等待用户决定 |
| `approved` | `completed` | 确认完成，进入批准分支 |
| `rejected` | `completed` | 确认完成，进入拒绝或取消分支 |

同一次 Checkpoint 只有一个 `checkpoint_id`。GUI 确认/取消提交结构化决定，不走自然语言猜测。

### 6.3 验证结论

产生领域判断的 Stage 使用 `outcome`: `passed | failed | inconclusive`。

```yaml
execution_status: completed
outcome: failed          # 校验进程正常，发现端口类型错误
```

```yaml
execution_status: errored
outcome: inconclusive    # 校验进程超时
```

三类字段分别表达执行生命周期、用户决定和领域结论。

---

## 7. WorkflowEngine

对外接口：

```text
consume_turn(user_text, shared_state)
instantiate(intent, shared_state)
current_stage()
start_stage()
accept_result(result_envelope)
resolve_checkpoint(decision)
invalidate(cause, project_version)
save()
```

职责：保存执行状态、执行状态迁移、管理 Checkpoint、校验 TaskCard 与 ResultEnvelope、处理版本/重试/失效、维护当前 Stage。

MainAgent 职责：解释 Intent、选择候选、为当前 Stage 组织 TaskCard、在推荐范围内选择 Subagent、汇总冲突/证据/产物、生成面向用户的说明。

StageExecutor 职责：按 Stage 范围装配 MainAgent 和候选 Subagent、注入共享 ToolContext、记录委派和 Tool 调用、将模型输出收敛为 ResultEnvelope、无 LLM 时调用确定性 Stage handler。

主 Agent 的工具权限按当前 Stage 收敛。领域写操作由 Subagent 工具白名单和 PolicyGateway 共同约束。

### 7.1 Stage 转移

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

### 7.2 版本检查

TaskCard 与 ResultEnvelope 携带 `workflow_id`、`stage_id`、`workflow_revision`、`base_project_version`。接收结果时比较 Workflow revision 和 Flowgraph version；版本变化时将结果记为 stale event，并重新计算当前 Stage。

---

## 8. TaskCard 与 ResultEnvelope

TaskCard 描述一次委派：目标 Subagent、指令、输入、期望结果、以及 Workflow/Stage/版本。

ResultEnvelope 返回：`ok`、`outcome`、产物、Claims、拟议修改、说明，以及相同的版本字段。写入 SharedState 和 Workflow 前执行结构校验、版本校验和 Policy 检查。`protocol_valid` 必须为 True。

确定性路径按 `recommended_agents` 合成 TaskCard/ResultEnvelope。deepagents 路径按每次 `task` 委派绑定信封；缺委派不得用确定性信封充数，且必须覆盖全部 `recommended_agents`。

---

## 9. Completion 硬门槛

每个 Stage 声明 completion 列表。Evaluator 按契约检查产物、校验、Claims 版本和安全条件。不满足时不得 `passed`，Workflow 不得正常 `completed`。

例如 `apply_and_verify` 需要流图已保存、结构校验完成、受影响 Claims 已评估。硬件 deploy 还需要停止得到确认：没有 `transmit_stopped=true` 时不得正常完成；无法确认停止时必须 `errored` 并触发紧急停止。

---

## 10. Checkpoint 与 Policy

默认生成用户 Checkpoint 的情况：

- 缺少会改变链路结构的关键槽位；
- 修改已有工程的调制方式或 recipe；
- 多块结构修改；
- 硬件配置和真实设备操作；
- 诊断后准备实施较大范围修复；
- 连续尝试未改善结果。

Policy 决策：

```text
ALLOW    允许当前操作
PROPOSE  生成修改方案并创建 Checkpoint
CONFIRM  等待明确批准
DENY     终止该操作并记录原因
```

WorkflowEngine 将 Policy 结果转换为 Stage 状态和 Checkpoint。Checkpoint 与 Policy pending 通过 `checkpoint_id` 关联。

---

## 11. 失效、重试与恢复

### 11.1 失效传播

`invalidate()` 优先匹配 Catalog `depends_on`。

| 变化 | 影响范围 |
|---|---|
| 规格架构字段变化 | 设计、建图、验证 Stage |
| recipe 或调制变化 | 建图、验证 Stage 和相关 Claims |
| Flowgraph 保存 | 验证 Stage 和工程 Claims |
| 成功条件变化 | 对应 Claim Evaluation |
| 表达档位变化 | 用户叙述和 GUI 展示 |

失效时保留旧 Stage 结果与事件，并提升 Workflow revision。

### 11.2 循环控制

```text
build_and_verify
  ├─ passed → completed
  ├─ failed + 有明确改进 → attempt + 1
  └─ failed + 无改进或达到上限 → waiting_user
```

默认 `max_attempts: 2`。每次尝试需产生新的参数、结构或 Evidence。相同结果指纹不再空转重试；连续结果无变化时提前停止。`errored` 在 `max_attempts` 内可 retry。

### 11.3 会话恢复

1. 加载工程事实；
2. 加载并校验 Workflow；
3. 比较 `base_project_version`；
4. 对 `running` Stage 执行中断恢复；
5. 恢复 Checkpoint 或当前 Stage；
6. 写入恢复事件。

进程中断时的 `running` Stage 恢复为 `pending`，attempt 保留并重新执行。

---

## 12. 主路径与确定性路径

两条路径均通过 WorkflowEngine 写入相同语义。

| Stage | 确定性 handler 语义 |
|---|---|
| `build_and_verify` | 选型建图并验证 |
| `inspect_and_measure` | 校验 + 仿真 + 指标 + 绘图 |
| `inspect_and_diagnose` | 校验 + 按指标诊断 |
| `apply_and_verify` | recipe 切换或原子 patch + 重验 |
| `hardware_precheck` | 当前配置检查与能力报告 |

deepagents 路径：StageExecutor 按当前 Stage 过滤 Subagent，传入 TaskCard；MainAgent 在推荐范围内协作；Subagent 通过确定性工具操作同一 ToolContext。

六个领域角色：`spec_agent`、`radio_design_agent`、`flowgraph_agent`、`verification_agent`、`diagnosis_agent`、`hardware_agent`。实现可增加 `protocol_agent`，只承担 BLE 离线协议与 TX 流图，不成为第八类 Task。

Skill 负责领域规则、步骤、输入输出契约和边界。Tool 白名单负责实际执行权限。Stage 通过 Subagent 间接获得 Skill。

---

## 13. BLE deploy 作为条件 Stage

`HARDWARE_CONFIGURE` 在 `operation=configure` 时保持三 Stage。`operation=deploy` 且 `protocol=ble` 时动态插入：

```text
protocol_spec_alignment
→ build_ble_advertiser
→ offline_protocol_verify
→ discover_and_probe_device
→ rf_plan_confirmation
→ configure_device
→ transmit_bounded
→ over_air_verification
→ stop_and_finalize
```

仍保持单 Workflow、串行 Stage、单一 Checkpoint 控制面。Intent 可保留 37/38/39 目标；当前构建语义只取列表中的第一个信道 37。三信道轮询是尚未落地的算法扩展，不能把单信道结果登记为三信道通过。

LLM 只提取 `local_name` 等意图。PDU、CRC、whitening、GFSK 参数和 Flowgraph 必须由确定性代码生成。

deploy Completion 包括：`ble_packet_valid`、`ble_waveform_generated`、`flowgraph_saved`、`structural_validation_completed`、`device_discovered`、`device_probed`、`rf_plan_approved`、`transmit_started`、`over_air_observed`、`transmit_stopped`。空口观察必须来自人工或独立接收，不得由发射端自证。

只读发现/probe 不需要发射授权。`start_flowgraph` 需要 RF Checkpoint。`stop_flowgraph` 与 `emergency_stop` 始终允许，后者最高优先级。

---

## 14. 三类持久化的语义分工

| 存储 | 回答的问题 |
|---|---|
| 工程事实（`state.json`） | 当前无线电工程是什么 |
| 执行控制（`workflow.yaml`） | 系统为了完成当前目标正在做什么 |
| 追加历史（`events.jsonl`） | 本轮发生过哪些 Turn、委派、工具和转移 |

工程事实保存 RadioSpec、ProjectState、`.grc` 路径和 version、Claims/Evidence、锁定约束、snapshot 索引、工程配置。

执行控制保存 Task Type、Workflow/Stage 状态、Intent 和槽位、Checkpoint、attempt/outcome/结果摘要、当前工程版本引用。

事件保存用户输入与决定、Workflow 创建和转移、Stage 开始/完成/等待/错误/失效、Subagent 委派、Tool 调用、Policy 决策、产物发布、stale 和恢复。事件外层含单调 `seq` 以及 `workflow_id` / `stage_id` / `attempt` / `profile_level`。

Skill 内容、Tool Schema 和完整调用历史不写入 Workflow 实例正文。

---

## 15. 工作区摘要契约

返回 GUI 的紧凑摘要至少包括：`workflow_id`、`task_type`、`execution_status`、`current_stage`、`stage_index`、`stage_total`、`waiting_reason`，以及 Stage 列表、revision、attempt、completion 计数和时间线。GUI 用该摘要显示进度，用结构化 `checkpoint_id` 提交确认，不另建状态机。

---

## 16. 首版边界与可量化事件

首版：单会话、单活动 Workflow、单一主要 Task Type、串行 Stage、Stage 内顺序调用多个 Subagent、条件 Stage、Checkpoint、重试和失效恢复、仿真优先、硬件配置与预检。

后续才考虑：多 Workflow 排队、并行只读 Stage、跨工程任务、完整 SDR 生命周期管理。仍不需要通用 DAG。

统一事件名可用于后续实验：

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
