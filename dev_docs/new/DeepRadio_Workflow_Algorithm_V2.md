# DeepRadio Workflow 算法

> 日期：2026-08-27
> 读者：算法、Agent、Workflow 与评测开发人员
> 范围：当前 `grc/agent` 控制面。七类 Task 是评测标签与 catalog 片段库，不是独立编排器。

---

## 1. 算法目标

DeepRadio 把用户自然语言变成可确认、可截断、可恢复、可审计的通信工程流程。权威流水线：

```text
用户文本
→ turn_relation
→ 规则 Intent + 可选 LLM 补全 → IntentIR
→ Catalog 片段组成 Stage
→ Plan Compiler（截断、校验、RF 时长）
→ 执行器（主机确定性 / LLM Subagent）
→ Completion
→ Workflow 转移（批准后只展开下一视距）
→ SharedState / Claim / ArtifactIndex / GUI
```

下一步由三类输入共同决定：

```text
Next = F(当前 Workflow 与工程，用户本轮决定，反馈影响范围)
```

约束：

- `raw_text` 与 `slot_sources=user` 的字段不被覆盖；确认只追加 Decision，不回写 Intent。
- Catalog 提供可执行 Stage 片段；`task_type` 只选片段并作为评测标签。
- 初始计划只到下一用户决策边界；批准后只重规划未执行尾部。
- Effect 为 `DEVICE_CONFIG` / `RF_RUN` 时，确认前检查进程能力；RF 必须有时长上限与 stop。
- Completion 只认工具事实、工程版本和 `run_id`，不认叙述。
- 失败只失效受影响范围。诊断对照实验不得 bump 工程版本、不得写回原图。

---

## 2. 核心对象

### 2.1 IntentIR

`WorkflowIntent` 是规则槽位加上开放约束层：

```json
{
  "turn_relation": "new_task",
  "task_type": "HARDWARE_CONFIGURE",
  "confidence": 0.96,
  "capabilities": ["protocol", "hardware_configure", "hardware_runtime", "deploy"],
  "slots": {
    "operation": "deploy",
    "protocol": "ble",
    "hardware": "pluto",
    "center_freq": 2402000000,
    "local_name": "loveu",
    "advertising_channels": [37],
    "duration_seconds": 30
  },
  "slot_sources": {"local_name": "user"},
  "missing_slots": [],
  "goals": ["在 Pluto 上广播 BLE 名称 loveu"],
  "requested_operations": ["protocol", "deploy"],
  "entities": {"hardware": "pluto", "protocol": "ble", "local_name": "loveu"},
  "constraints": {"duration_seconds": 30},
  "forbidden_effects": [],
  "stop_conditions": [],
  "decision_boundaries": ["rf_plan_confirmation"]
}
```

规则分类在 `workflow/engine.py`；有模型时 `complete_intent` 按用户目标校正 `operation`（prepare / deploy），不得覆盖 `slot_sources=user` 或 safety 默认时长。无模型时为 no-op。`operation=prepare` 会加上 `stop_at_decision_boundary`；`deploy` 会去掉它。Inspector `wait_kind=approval` 仅在 Workflow 仍 `waiting` 且 Checkpoint 为 pending。

### 2.2 Workflow

- 标识：`workflow_id`、`task_type`、`revision`、`base_project_version`。
- IntentIR。
- **视距内** Stage 列表（不是一次展开的完整尾部）。
- `deferred_plan`：决策点之后尚未物化的 Stage。
- `compiled_plan`：视距 + 延迟项的 PlanNode 摘要。
- `decisions` / `granted_effects`：用户批准记录；不写回 Intent。
- 当前 Stage 的 `execution_status`、`attempt`、`resume_from`、`completion_result`、Checkpoint。

### 2.3 PlanNode

Compiler 挂在 Stage 上的通用计划元数据：`id`、`objective`、`requires`、`produces`、`effect_level`、`success_predicates`、`needs_user_decision`、`tools`、`stage_id`。未知 action 或未知工具名被丢弃，不得发明 Registry 外能力。

### 2.4 SharedState

- RadioSpec 与用户锁定约束。
- 当前 GRC 工程、版本、语义哈希、快照。
- 累积 ArtifactIndex（Stage 不得整表覆盖）。
- Claim / Evidence（图像、标量、Claim 共用 `measurement_id`）。
- 设备身份、RF armed、runtime 摘要（同一 `run_id`）。

路径以 session 根为基准相对化。

### 2.5 TaskCard 与 ResultEnvelope

TaskCard：`task_id`、`workflow_id`、`workflow_revision`、`stage_id`、`attempt`，以及目标、允许能力、推荐 Agent、允许 Tool、输入事实、Completion。

ResultEnvelope：对应身份字段 + `ok`、`outcome`、`protocol_valid`、`tool_calls`、`artifacts`、`claims`、`errors`、`completion`。

ID、revision 或 Stage 不匹配时结果被拒绝。落盘时压缩 invocations，原始工具输出留在 Evidence。

---

## 3. Catalog 片段与七类标签

七类名称仍用于选片段和评测对照，**不是**运行时硬路由表。Composer 按 `task_type`、`capabilities`、`slots.operation` 从 `task_catalog.yaml` 取片段，再交给 Compiler 截断。

| 标签 | 终态产物 | 默认片段 |
|---|---|---|
| `END_TO_END_SIM` | 流图、测量、Claims | 规格对齐 → 构建与验证 |
| `TX_BUILD` | 发射链路与结构校验 | 规格对齐 → TX 构建 |
| `RX_BUILD` | 接收链路与质量证据 | RX 规格对齐 → 构建与验证 |
| `DIAGNOSE` | 诊断与可选修复 | 检查诊断；只读则跳过 repair；对照实验不改原图 |
| `MODIFY_PROJECT` | 新工程版本与重验 | 检查计划 → 确认 → GraphPatch 或 recipe 应用 |
| `OBSERVE` | 图、指标或结构 | 检查与测量 |
| `HARDWARE_CONFIGURE` | 配置、运行或空口 Evidence | 见三套片段 |

`HARDWARE_CONFIGURE` 三套片段：

- 默认 `stages`：预检 → 确认 → 记录配置。
- `runtime_stages`：预检 → 发现探测 → 确认（`DEVICE_READ` 显示「配置确认」，`RF_RUN`/`DEVICE_CONFIG` 显示「RF 计划确认」）→ 配置 → 有限运行 → 观察确认 → 停止。
- `deploy_stages`：BLE 协议构建与空口闭环（§10）。

分类顺序：显式操作与否定约束 → 能力集合 → 用 `task_type` 选覆盖终态的片段 → 条件拼接硬件/构建组 → 缺槽则插入 alignment。调制、协议、设备、频率是槽位，不是 Task。

---

## 4. User Turn

`turn_relation`：`new_task`、`answer`、`adjustment`、`feedback`、`approval`、`rejection`、`cancel`。GUI 确认/取消走 `ServiceAgent.step_command`，携带 `checkpoint_id` 与 `approved` / `rejected`。

| 关系 | 处理 |
|---|---|
| `new_task` | 创建 Workflow；明确终止当前任务时先归档并停止 runtime |
| `answer` | 填补 alignment 缺失槽位，保持 `workflow_id` |
| `adjustment` | 等待确认或待执行时合并槽位，递增 revision |
| `approval` | 只解决当前 `checkpoint_id`；追加 `decisions`；物化下一视距 |
| `rejection` | 执行该 Checkpoint 的拒绝分支 |
| `feedback` | 计算影响范围并失效相关 Stage/Claim |
| `cancel` | `outcome=cancelled`，停止 runtime，清除 armed |

缺少活动 Checkpoint 的裸确认不触发受限操作。低置信关系保持当前 Workflow 并澄清。

---

## 5. 状态模型

### 5.1 Workflow 与 Stage

```text
pending → running → waiting | completed | errored
                ↘ invalidated（上游事实变化后待重验）
```

Catalog 的 `waiting_user` 写入 `execution_status=waiting`。用户取消：Workflow `outcome=cancelled` 且 `execution_status=completed`。验收失败走 Catalog 转移，通常回到 `waiting`。

Checkpoint：`pending → approved | rejected`。`approved` 只表示允许进入下一视距；Stage 仍须 Completion。Checkpoint 带 `requested_effect` 与可选 `blocker`（例如 RF 进程能力缺失且 `retryable=false`）。

`configure_device` 在 configure 已成功、arm 失败时设 `resume_from=arm_flowgraph`，重试跳过 configure。

### 5.2 Runtime

```text
prepared → armed → starting → running → stopped/exited
                              └──────→ crashed
```

字段：`run_id`、pid、program、interpreter、started_at、deadline、ready、startup_health_passed、running、return_code、crashed。空口 Evidence、停止结果和 runtime Claim 引用同一 `run_id`。

### 5.3 Effect

```text
READ < ARTIFACT_WRITE < DEVICE_READ < DEVICE_CONFIG < RF_RUN
```

`DEVICE_CONFIG` / `RF_RUN` 在确认前检查 `GRC_AGENT_ENABLE_RF=1`。只读发现、离线建图、安全预览不因 RF 关闭而失败。

---

## 6. 计划编译

```text
Catalog 片段
→ 可选 LLM propose_plan（无模型则为空）
→ validate_proposal（未知 id/工具丢弃）
→ attach PlanNode
→ ensure_rf_bounds（RF_RUN 必有 duration）
→ split_at_decision_boundary
→ horizon 进入 Workflow.stages；其余进入 deferred_plan
```

Inspector 只显示到下一 checkpoint。`safety_finalizer`（停止发射）不是业务决策点。批准后 `replan_tail` 只改未执行尾部；空提案或非法提案保持原 deferred。

能力装配：

```text
Stage.recommended_agents + completion
→ Subagent 工具子集
→ Skill
→ Registry allowlist
→ TaskCard
```

Subagent：`spec_agent`、`radio_design_agent`、`flowgraph_agent`、`verification_agent`、`diagnosis_agent`、`protocol_agent`、`hardware_agent`。Skill 是给 LLM 的说明书；确定性路径直接 `registry.call`。

改图两条工具，语义不同，不合并：`apply_grc_diff`（单参，调制/星座 DENY）与 `apply_flowgraph_patch`（多 op 原子回滚，含 GraphPatch 别名 `set_param` / `replace_block` / `connect`）。诊断两条工具不合并：`debug_by_metric`（判决与叙述）与 `run_diagnosis_experiment`（单因素对照，恢复原图）。

---

## 7. 执行模式

```text
if Stage 属于主机控制面 或 确定性 handler 优先:
    mode = deterministic
elif 需要语义推理且模型可用:
    mode = llm_subagent
else:
    回落到确定性 handler；仍无法执行则 waiting 或 errored
```

主机控制面（LLM 不能省略、重排或用别的工具顶替）：

```text
hardware_precheck
configure_and_check
build_ble_advertiser
offline_protocol_verify
discover_and_probe_device
discover_and_probe_hardware
configure_device
transmit_bounded
run_bounded
stop_and_finalize
stop_runtime
```

另外强制走确定性 handler：`inspect_and_plan` / `inspect_and_diagnose` / `inspect_and_measure`；以及带 `hardware_configure` 的 `build_and_verify`、`tx_build_and_validate`、`rx_build_and_verify`、`apply_and_verify`。

确定性适合：PDU/CRC/白化/GFSK 回环、原子建图与结构检查、设备发现与精确 probe、解释器启动/查询/停止、Manifest 与 Claim 事务。LLM 只处理语义不确定与开放解释。事件记录 `mode`、`origin`、executor、工具名和耗时。

---

## 8. Completion

Stage 进入 `completed` 前逐项验证 Catalog Completion：

```text
passed = all(
    产物存在且可解析，
    Tool 返回 ok 且非 DENY，
    Claim 绑定当前工程版本，
    ResultEnvelope 协议有效，
    本 Stage 的安全谓词为真
)
```

`open_questions` 或缺失槽位非空时，自治 Completion 全部失败。

硬件谓词：

- `transmit_started` / `runtime_started`：`ok && running && ready && startup_health_passed && run_id`。
- `over_air_observed`：Intent 槽位已记录观察，且 `ota_observation.run_id` 等于当前 runtime `run_id`。名称比对发生在写入观察时（§10.3），不是 Completion 再算一遍。
- `transmit_stopped` / `runtime_stopped`：同一 `run_id` 已终止、未 crashed。

---

## 9. 反馈、失效与重试

```text
changed_fields
→ 直接依赖 Stage
→ 下游 Stage
→ 受影响 Claim / Evidence
```

1. 更新 SharedState 并递增 revision；不覆盖用户 Intent。
2. 直接依赖与下游标为 `invalidated` 或 `pending`。
3. 失效绑定该工程版本、设备身份或 `run_id` 的 Claim。
4. 保留无依赖的 Stage、产物和 Evidence。
5. 从最早受影响 Stage 恢复。

自动重试受 `max_attempts` 限制。相同结果指纹停止循环并 `waiting`。工程修改用 snapshot 回滚。Profile 在 pin 之后 observe 不改 score；同轮带 `profile_snapshot`。

---

## 10. BLE 部署

`operation=deploy` 且 `protocol=ble` 时选用 `deploy_stages`：

```text
[protocol_spec_alignment 若缺槽]
build_ble_advertiser
→ offline_protocol_verify
→ discover_and_probe_device
→ rf_plan_confirmation
→ configure_device
→ transmit_bounded
→ over_air_verification
→ stop_and_finalize
```

能力声明仅为 `ble_advertising_single_channel`。不得声称三信道跳频或独立 sniffer。

### 10.1 离线协议

1. 由 local name 等槽位构造 Advertising PDU。
2. 计算 BLE CRC24。
3. 按**单一**广告信道生成白化序列。
4. 组装 preamble、access address、白化后的 PDU+CRC。
5. Gaussian filter 与调制指数生成 GFSK IQ。
6. 独立解析、解白化、CRC 重算、IQ 解调回环。
7. 协议字段、波形参数和校验写入 Artifact/Claim。

协议工具可按信道 37 / 38 / 39 **单独**生成。部署构建取 `advertising_channels[0]`。载频 2.402 / 2.426 / 2.480 GHz 对应 37 / 38 / 39。未指定载频时槽位默认为 `[37]`，与 2.402 GHz 对齐。三信道跳频调度未实现。

### 10.2 设备与受控发射

设备名映射到 `HardwareProfile`。Pluto：USB IIO 扫描提取 URI 后再精确 probe。B210：UHD 发现与 `type=b200` probe。Builder 由 Profile 选择，失败不得回退到其它 SDR 或 AWGN。

RF 计划批准后，按当前语义哈希生成 `.armed.grc`。Runtime 解析可导入所需模块的 Python 解释器，有限时长启动。启动宽限期内退出直接失败。`duration_seconds` 是最大窗口，空口确认或取消可提前 stop。

### 10.3 空口 Evidence

写入观察时：

```text
query_runtime_status
→ running && ready && run_id && 未超 deadline
→ expected_name == observed_name
→ 可选复制截图到 final/evidence
→ 记录 ota_observation（含 run_id）与 over_air_observed 槽位
→ Claim ota_ble_local_name_observed
```

无附件时 `evidence_complete=false`，GUI 标明人工确认、附件缺失；不得把 Evidence Gate 记为完整通过。随后 `stop_and_finalize` 停止同一 `run_id`，并要求观察已记录且 `run_id` 一致。

---

## 11. 持久化与恢复

| 文件 | 语义 |
|---|---|
| `state.json` | 领域事实、工程版本、ArtifactIndex、runtime |
| `workflow.yaml` | 控制面（JSON 内容）；invocations 已压缩 |
| `events.jsonl` | 可追加审计轨迹 |

恢复：载入 SharedState → 载入 Workflow 并校验 schema/revision → 对比 `base_project_version` → 查询 runtime → 把中断的 `running` Stage 收敛为可重试、等待或失败 → 刷新 Claim 与 GUI digest。

导出 Manifest 消费**累积** ArtifactIndex，逐项写相对路径、大小和 SHA-256。不得用本轮显式产物集合覆盖历史条目。

---

## 12. 未实现（不是当前算法缺口）

1. BLE 37/38/39 跳频调度与每信道空口验收。实现前不得扩大 Claim。
2. 删除 `task_catalog.yaml` 与七类 compose 分支（需 Session 迁移测试）。
3. LLM 生成并执行未在 catalog/Registry 中的 PlanNode。
4. 多 Task 排队与可并行 Stage 的资源锁。
