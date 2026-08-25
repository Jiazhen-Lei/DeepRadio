# DeepRadio Workflow 算法

> 日期：2026-08-25
> 读者：算法、Agent、Workflow 与评测开发人员
> 范围：文本到 Workflow、状态模型、执行选择、完成判定、反馈传播和硬件闭环

---

## 1. 算法目标

DeepRadio 将用户自然语言转换成可执行、可确认、可恢复、可审计的通信工程流程：

```text
用户文本
→ User Turn 关系判断
→ Intent 结构化
→ Task 选择与 Stage 实例化
→ Stage 执行器和能力装配
→ Completion 判定
→ Workflow 转移
→ SharedState、Claims、Evidence 与 GUI 同步
```

下一步由三类输入共同决定：

```text
Next = F(当前 Workflow/工程状态，用户本轮决定，反馈影响范围)
```

约束：

- 用户原始目标和显式参数保持最高优先级；
- Task 按终态产物、验收方式和安全边界选择；
- Stage 对应可判定的中间产物或必须由用户表态的边界；
- Subagent、Skill 和 Tool 由能力约束装配；
- 每个通过状态都有 Completion 与 Evidence；
- 工程修改、设备操作和 RF 发射受 Policy 与 Checkpoint 约束；
- 失败只回退受影响范围，保留仍然有效的上游事实和证据。

---

## 2. 核心对象

### 2.1 Intent

```json
{
  "turn_relation": "new_task | answer | confirm | reject | feedback | cancel",
  "task_type": "HARDWARE_CONFIGURE",
  "operation": "deploy",
  "capabilities": ["protocol", "hardware_runtime", "deploy"],
  "slots": {
    "protocol": "ble",
    "hardware": "pluto",
    "center_freq": 2402000000,
    "local_name": "loveu",
    "advertising_channels": [37],
    "duration_seconds": 30
  },
  "slot_sources": {
    "local_name": "user"
  },
  "missing_slots": [],
  "confidence": 0.96
}
```

`slot_sources=user` 的字段受保护。低置信输入可由 Intent 补全器输出结构化补充；响应无效或模型不可用时使用规则结果。补全实现位于 `workflow/engine.py`。

### 2.2 Workflow

- `workflow_id`、`task_type`、`revision`；
- `base_project_version`；
- Intent、capabilities、missing slots；
- 当前 Stage 和完整 Stage 列表；
- Workflow/Stage 的状态、attempt、outcome；
- `completion_result`；
- Checkpoint 与等待原因。

### 2.3 SharedState

- RadioSpec 和用户锁定约束；
- 当前 GRC 工程、版本、语义哈希和快照；
- Tool 产生的工件；
- Claim、Evidence 和验证版本；
- 设备身份、RF armed 状态和 runtime 摘要。

路径以 session 根为基准相对化存储。

### 2.4 TaskCard 与 ResultEnvelope

TaskCard：

```text
task_id、workflow_id、workflow_revision、stage_id、attempt
目标、允许能力、推荐 Agent、允许 Tool、输入事实、Completion 条件
```

ResultEnvelope：

```text
task_id、workflow_id、workflow_revision、stage_id
ok、outcome、protocol_valid、tool_calls、artifacts、claims、errors、completion
```

ID、revision 或 Stage 不匹配时，结果被拒绝提交。

---

## 3. 七类 Task

| Task | 终态产物 | 核心 Stage |
|---|---|---|
| `END_TO_END_SIM` | 完整流图、测量与 Claims | 规格对齐 → 构建与验证 |
| `TX_BUILD` | 发射链路与结构校验 | 规格对齐 → TX 构建与校验 |
| `RX_BUILD` | 接收链路、BER/质量证据 | RX 规格对齐 → 构建与验证 |
| `DIAGNOSE` | 诊断、证据、可选修复 | 检查诊断 → 修复确认 → 修复验证 |
| `MODIFY_PROJECT` | 新工程版本和重验结果 | 检查计划 → 修改确认 → 应用验证 |
| `OBSERVE` | 图、指标或结构观察 | 检查与测量 |
| `HARDWARE_CONFIGURE` | 配置、运行或空口 Evidence | 按 operation 选择配置、运行或部署 Stage |

分类顺序：

1. 提取显式操作词、期望产物、验收方式和硬件安全范围；
2. 识别复合请求中的最终目标；
3. 选择覆盖最终目标且能容纳前置能力的 Task；
4. 使用 capabilities 添加条件 Stage；
5. 对低置信或冲突槽位发起澄清。

调制、协议、设备型号和频率属于槽位或能力，驱动 Stage 参数与 Tool 选择。

`HARDWARE_CONFIGURE` 三套 Stage：

- 默认 `stages`：预检 → 确认 → 记录配置；
- `runtime_stages`：预检 → 发现探测 → RF 确认 → 配置 → 有限运行 → 观察确认 → 停止；
- `deploy_stages`：BLE 协议构建与空口闭环（§10）。

---

## 4. User Turn 与活动 Workflow

`turn_relation` 取值：`new_task`、`answer`、`adjustment`、`feedback`、`approval`、`rejection`、`cancel`。GUI 确认/取消按钮走 `ServiceAgent.step_command`，携带 `checkpoint_id` 与 `approved` / `rejected`。

| 关系 | 处理 |
|---|---|
| `new_task` | 创建 Workflow；明确终止当前任务时先归档和停止 runtime |
| `answer` | 填补对齐 Stage 的缺失槽位，保持 `workflow_id` |
| `adjustment` | 在等待确认或待执行时合并槽位，递增 revision |
| `approval` | 仅解决当前 `checkpoint_id` 的批准分支 |
| `rejection` | 执行该 Checkpoint 的拒绝分支 |
| `feedback` | 计算影响范围并失效相关 Stage/Claim |
| `cancel` | 取消 Workflow（`outcome=cancelled`），停止 runtime，清除 armed 状态 |

「继续」和参数补充结合当前等待点解析。缺少活动 Checkpoint 的裸确认不触发受限操作。低置信关系判断保持当前 Workflow，并请求澄清。

---

## 5. 状态模型

### 5.1 Workflow 与 Stage

`execution_status`：

```text
pending      尚未进入或待重跑
running      正在执行
waiting      等待输入或 Checkpoint
completed    完成条件全部通过
errored      执行器、工具或协议异常
invalidated  上游事实变化，需要重验
```

Catalog 的转移目标 `waiting_user` 写入 `execution_status=waiting`。用户取消把 Workflow `outcome` 记为 `cancelled`，`execution_status` 为 `completed`。验收失败走 `failed` / `failed_without_improvement` 等 Catalog 转移，通常回到 `waiting`。

Checkpoint `decision_status`：

```text
pending → approved | rejected
```

`approved` 只表示允许进入下一阶段；Stage 还要过 CompletionEvaluator。Checkpoint 解决后写入 `completion_result`，Inspector 显示 `completion n/m`。

### 5.2 Runtime

```text
prepared → armed → starting → running → stopped/exited
                              └──────→ crashed
```

字段：`run_id`、pid、program、interpreter、started_at、deadline、ready、startup_health_passed、running、return_code、crashed。空口 Evidence、停止结果和 runtime Claim 引用同一 `run_id`。

---

## 6. Stage 规划与能力装配

Stage 建立准则：有明确中间产物及 Completion；有用户决策、风险授权或硬件连接边界；失败需要独立回退范围；所需能力或运行环境变化。

```text
Stage.required_capabilities
→ Subagent capability match
→ Skill instruction set
→ Tool allowlist
→ TaskCard
```

Subagent：

- `spec_agent`：意图、规格、缺失槽位；
- `radio_design_agent`：通信链路和配方选择；
- `flowgraph_agent`：GRC 结构与参数修改；
- `verification_agent`：结构、仿真、协议和测量验收；
- `diagnosis_agent`：失败解释与修复建议；
- `protocol_agent`：BLE PDU、PHY 和协议验证；
- `hardware_agent`：设备、配置、运行和停止。

一个 Subagent 可绑定多个 Skill。Skill 是给 LLM 的说明书；确定性路径直接 `registry.call`。

---

## 7. 执行模式选择

```text
if Stage 属于主机控制面 或 已注册确定性 handler:
    mode = deterministic
elif Stage 需要语义推理且模型可用:
    mode = llm_subagent
else:
    返回 waiting_user 或 errored
```

主机控制面 Stage：`build_ble_advertiser`、`offline_protocol_verify`、`discover_and_probe_device`、`discover_and_probe_hardware`、`configure_device`、`transmit_bounded`、`run_bounded`、`stop_and_finalize`、`stop_runtime`。

适合确定性执行：PDU/CRC/白化/GFSK 与回环；GRC 原子构建和结构检查；设备发现与身份探测；解释器解析、启动、查询和停止；Manifest、哈希和 Claim 事务。

LLM Subagent 负责语义不确定性、方案解释和开放式诊断。安全关键 Stage 的顺序和 Completion 由 Catalog 与 Engine 固定。事件记录 `mode`、`origin`、实际 executor、Tool 调用和耗时。

---

## 8. Completion 硬门槛

Stage 进入 `completed` 前逐项验证 Catalog 的 Completion：

```text
passed = all(
    产物存在且可解析，
    Tool 返回 ok，
    Claim 绑定当前工程版本，
    ResultEnvelope 协议有效，
    运行状态满足本 Stage 的安全条件
)
```

硬件门槛：

- `transmit_started`：`ok && running && ready && startup_health_passed && run_id`；
- `over_air_observed`：runtime 仍在运行、未超 deadline、目标名称精确匹配、Evidence 绑定同一 `run_id`；
- `transmit_stopped`：同一运行已终止、return code 合法、`crashed=false`。

---

## 9. 反馈、失效与重试

```text
changed_fields
→ direct dependent stages
→ downstream stages
→ affected claims/evidence
```

1. 更新 Intent/SharedState 并递增 revision；
2. 将直接依赖和下游 Stage 标为 `invalidated` 或 `pending`；
3. 失效绑定相应工程版本、设备身份或运行标识的 Claim；
4. 保留无依赖关系的 Stage、产物和 Evidence；
5. 从最早受影响 Stage 恢复执行。

自动重试受 `max_attempts` 限制。相同结果指纹停止循环并进入 `waiting`。工程修改使用 snapshot 回滚。

---

## 10. BLE 部署

`HARDWARE_CONFIGURE` 在 `operation=deploy` 且 `protocol=ble` 时选择：

```text
build_ble_advertiser
→ offline_protocol_verify
→ discover_and_probe_device
→ rf_plan_confirmation
→ configure_device
→ transmit_bounded
→ over_air_verification
→ stop_and_finalize
```

缺槽时前面插入 `protocol_spec_alignment`。

### 10.1 离线协议

1. 从 local name、地址、广告类型等槽位构造 Advertising PDU；
2. 根据 PDU Header 和 Payload 计算 BLE CRC24；
3. 根据广告信道生成白化序列；
4. 组装 preamble、access address、白化后的 PDU+CRC；
5. 以 Gaussian filter 和调制指数生成 GFSK IQ；
6. 独立解析、解白化、CRC 重算、IQ 解调回环；
7. 把协议字段、波形参数和校验结果写入 Artifact/Claim。

协议工具可按信道 37 / 38 / 39 单独生成。部署构建取 `advertising_channels[0]`：载频 2.402 / 2.426 / 2.480 GHz 分别对应 37 / 38 / 39；未指定载频时槽位为 `[37, 38, 39]`，构建使用 37。三信道跳频调度未实现。

### 10.2 设备与受控发射

设备名称映射到 `HardwareProfile`。Pluto 使用 USB IIO 扫描并提取 URI，再对精确 URI probe；B210 使用 UHD 发现和 `type=b200` probe。流图 Builder 由 Profile 选择。

批准 RF 计划后，按当前语义哈希创建 `.armed.grc`。Runtime 解析可导入所需 GNU Radio 模块的 Python 解释器，生成代码并有限时长启动。启动宽限期内退出直接失败。

### 10.3 空口 Evidence

```text
query_runtime_status
→ 校验 running、deadline、run_id
→ 校验 expected_name == observed_name
→ 记录 observed_at 和 evidence_kind
→ 可选复制截图到 final/evidence
→ 创建 over_air_observed Claim
```

随后 `stop_and_finalize` 停止同一运行。`duration_seconds` 是最大运行窗口，空口确认可提前结束。

---

## 11. 持久化与恢复

| 文件 | 语义 |
|---|---|
| `state.json` | 当前领域事实与工程状态 |
| `workflow.yaml` | 活动任务的执行控制状态 |
| `events.jsonl` | 可追加、可计时、可审计的事件轨迹 |

恢复顺序：

1. 载入 SharedState（相对路径解析到当前 session 根）；
2. 载入 Workflow 并校验 schema/revision；
3. 对比 `base_project_version` 和当前工程版本；
4. 查询硬件 runtime；
5. 将中断的 `running` Stage 收敛为可重试、等待用户或失败；
6. 刷新 Claims 和 GUI digest。

导出 Manifest 消费本轮显式产物集合，逐项写入相对路径、大小和 SHA-256。

---

## 12. 算法待办

1. BLE 广告信道 37/38/39 跳频调度、每信道白化和空口验收。
2. runtime 超时、进程崩溃、重复启动的模型化故障注入覆盖率。
3. 多轮反馈对 Stage 依赖图的覆盖率度量。
4. Intent 数据集加入否定约束、模糊硬件名、复合操作和对抗性表达。
5. 多 Task 排队与可并行 Stage 的资源锁（超出当前单任务产品范围）。
