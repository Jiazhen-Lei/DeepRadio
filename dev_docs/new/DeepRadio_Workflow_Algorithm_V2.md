# DeepRadio Workflow 算法 V2

> 日期：2026-08-24
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

算法需要同时保证：

- 用户原始目标和显式参数保持最高优先级；
- Task 按终态产物、验收方式和安全边界选择；
- Stage 对应可判定的中间产物或必须由用户表态的边界；
- Subagent、Skill 和 Tool 由能力约束动态装配；
- 每个通过状态都有 Completion 与 Evidence 支撑；
- 工程修改、设备操作和 RF 发射受 Policy 与 Checkpoint 约束；
- 失败只回退受影响范围，保留仍然有效的上游事实和证据。

---

## 2. 核心对象

### 2.1 Intent

Intent 表达用户本轮的结构化语义：

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
    "duration_seconds": 30
  },
  "slot_sources": {
    "local_name": "user"
  },
  "missing_slots": [],
  "confidence": 0.96
}
```

`slot_sources=user` 的字段受保护，规则补全、LLM 补全和默认值均不得覆盖。低置信输入可由 Intent LLM 输出结构化补充；响应无效或模型不可用时使用规则结果。

### 2.2 Workflow

Workflow 保存一个活动任务的执行状态：

- `workflow_id`、`task_type`、`revision`；
- `base_project_version`；
- Intent、capabilities、missing slots；
- 当前 Stage 和完整 Stage 列表；
- Workflow/Stage 的状态、attempt、outcome；
- Completion 结果；
- Checkpoint 与等待原因。

### 2.3 SharedState

SharedState 保存跨 Stage 共享的领域事实：

- RadioSpec 和用户锁定约束；
- 当前 GRC 工程、版本、语义哈希和快照；
- Tool 产生的工件；
- Claim、Evidence 和验证版本；
- 设备身份、RF armed 状态和 runtime 摘要。

### 2.4 TaskCard 与 ResultEnvelope

TaskCard 是 Main Agent 给执行器的最小任务边界，至少包含：

```text
task_id、workflow_id、workflow_revision、stage_id、attempt
目标、允许能力、推荐 Agent、允许 Tool、输入事实、Completion 条件
```

ResultEnvelope 是执行器返回的严格结果：

```text
task_id、workflow_id、workflow_revision、stage_id
ok、outcome、protocol_valid、tool_calls、artifacts、claims、errors
```

所有执行模式使用相同 Envelope。ID、revision 或 Stage 不匹配时，结果被拒绝提交。

---

## 3. 七类 Task 候选库

| Task | 终态产物 | 核心 Stage |
|---|---|---|
| `END_TO_END_SIM` | 完整流图、测量与 Claims | 规格对齐 → 构建与验证 |
| `TX_BUILD` | 发射链路与结构校验 | 规格对齐 → TX 构建与校验 |
| `RX_BUILD` | 接收链路、BER/质量证据 | RX 规格对齐 → 构建与验证 |
| `DIAGNOSE` | 诊断、证据、可选修复 | 检查诊断 → 修复确认 → 修复验证 |
| `MODIFY_PROJECT` | 新工程版本和重验结果 | 检查计划 → 修改确认 → 应用验证 |
| `OBSERVE` | 图、指标或结构观察 | 检查与测量 |
| `HARDWARE_CONFIGURE` | 配置、运行或空口 Evidence | 按 operation 选择配置、运行或部署 Stage |

分类采用以下顺序：

1. 提取显式操作词、期望产物、验收方式和硬件安全范围；
2. 识别复合请求中的最终目标；
3. 选择覆盖最终目标且能容纳前置能力的 Task；
4. 使用 capabilities 添加条件 Stage；
5. 对低置信或冲突槽位发起澄清。

调制方式、协议、设备型号和频率属于槽位或能力。它们驱动 Stage 参数与 Tool 选择，不扩张顶层 Task 数量。

---

## 4. User Turn 与活动 Workflow

每轮文本先判断和活动 Workflow 的关系：

| 关系 | 处理 |
|---|---|
| `new_task` | 创建 Workflow；明确终止当前任务时先归档和停止 runtime |
| `answer` | 填补当前缺失槽位，保持 `workflow_id` |
| `confirm` | 仅解决当前 `checkpoint_id` |
| `reject` | 执行该 Checkpoint 的拒绝分支 |
| `feedback` | 计算影响范围并失效相关 Stage/Claim |
| `cancel` | 取消 Workflow，停止 runtime，清除 armed 状态 |

简单的“继续”“确认”和参数补充需要结合当前等待点解析。缺少活动 Checkpoint 的裸确认不触发受限操作。低置信关系判断优先保持当前 Workflow，并请求澄清。

---

## 5. 状态模型

### 5.1 Workflow 与 Stage 状态

当前最小状态集合：

```text
pending      尚未进入
running      正在执行
waiting_user 等待输入或 Checkpoint
completed    完成条件全部通过
failed       执行完成且验收失败
cancelled    用户取消或拒绝终止
errored      执行器、工具或协议异常
stale        上游事实变化，需要重验
```

用户确认由 Checkpoint 独立表达：

```text
open → approved | rejected | cancelled
```

执行状态、用户决定和验证结论分开保存。`approved` 只表示允许进入下一阶段；Stage 仍需 CompletionEvaluator 判定。

### 5.2 Runtime 状态

硬件运行作为独立事务管理：

```text
prepared → armed → starting → running → stopped/exited
                              └──────→ crashed
```

关键字段为：

```text
run_id、pid、program、interpreter、started_at、deadline
ready、startup_health_passed、running、return_code、crashed
```

一次空口 Evidence、停止结果和 runtime Claim 必须引用同一 `run_id`。

---

## 6. Stage 规划与能力装配

Stage 的建立准则：

1. 存在明确中间产物及可判定 Completion；
2. 存在用户决策、风险授权或硬件连接边界；
3. 失败需要形成独立回退范围；
4. 所需能力、权限或运行环境发生变化。

每个 Stage 通过 `recommended_agents` 描述能力责任，通过 Tool 白名单限制动作范围。一个 Subagent 可以绑定多个 Skill，一个 Skill 也可被多个 Subagent 复用。装配关系为：

```text
Stage.required_capabilities
→ Subagent capability match
→ Skill instruction set
→ Tool allowlist
→ TaskCard
```

当前主要 Subagent：

- `spec_agent`：意图、规格、缺失槽位；
- `radio_design_agent`：通信链路和参数设计；
- `flowgraph_agent`：GRC 结构和原子修改；
- `verification_agent`：结构、仿真、协议和测量验收；
- `diagnosis_agent`：失败解释与修复建议；
- `hardware_agent`：设备、配置、运行和停止；
- `protocol_agent`：BLE PDU、PHY 和协议验证。

Task 候选库无需穷举所有文本。Text 数据集覆盖同义表达、参数顺序、否定约束、补充轮次和复合目标；Intent 将它们归一化为稳定槽位。

---

## 7. 执行模式选择

```text
if Stage 具有注册的确定性 handler:
    mode = deterministic
elif Stage 需要语义推理且模型可用:
    mode = llm_subagent
else:
    返回 waiting_user 或 errored
```

适合确定性执行的任务包括：

- PDU/CRC/白化/GFSK 生成与回环校验；
- GRC 原子构建和结构检查；
- 设备发现、身份探测和能力范围检查；
- 解释器解析、Flowgraph 启动、状态查询和停止；
- Manifest、哈希和 Claim 事务。

LLM Subagent 负责语义不确定性、方案解释和开放式诊断。安全关键 Stage 的顺序和 Completion 由 Catalog 与 Engine 固定约束。

确定性 Stage 的快速完成表示通用算法在当前输入参数上执行成功。事件应记录 `mode`、实际 `executor_id`、Tool 调用和耗时，使实验能够区分 LLM 推理与代码执行。

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

关键硬件门槛：

- `transmit_started`：`ok && running && ready && startup_health_passed && run_id`；
- `over_air_observed`：runtime 仍在运行、未超 deadline、目标名称精确匹配、Evidence 绑定同一 `run_id`；
- `transmit_stopped`：同一运行已终止、return code 合法、`crashed=false`。

Checkpoint 解决路径当前已记录 decision 和 Evidence 字段。Inspector 的 Completion 计数仍需由结构化 Completion 结果补齐，这是当前算法接口的 P0 项。

---

## 9. 反馈、失效与重试

反馈影响范围由字段依赖图和 Stage 的 `depends_on` 计算：

```text
changed_fields
→ direct dependent stages
→ downstream stages
→ affected claims/evidence
```

处理规则：

1. 更新 Intent/SharedState 并递增 revision；
2. 将直接依赖和下游 Stage 标为 `stale` 或 `pending`；
3. 失效绑定相应工程版本、设备身份或运行标识的 Claim；
4. 保留无依赖关系的 Stage、产物和 Evidence；
5. 从最早受影响 Stage 恢复执行。

自动重试受 `max_attempts` 限制。只有检测到改进或输入发生有效变化时继续；相同结果指纹停止循环并进入 `waiting_user`。工程修改使用 snapshot 支持回滚。

---

## 10. BLE 部署算法

BLE 部署属于 `HARDWARE_CONFIGURE`，由 `operation=deploy` 和 `protocol=ble` 选择以下 Stage：

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

### 10.1 离线协议

1. 从 local name、地址、广告类型等槽位构造 Advertising PDU；
2. 根据 PDU Header 和 Payload 计算 BLE CRC24；
3. 根据广告信道生成白化序列；
4. 组装 preamble、access address、白化后的 PDU+CRC；
5. 以 Gaussian filter 和调制指数参数生成 GFSK IQ；
6. 独立解析、解白化、CRC 重算、IQ 解调进行回环验收；
7. 把协议字段、波形参数和校验结果写入 Artifact/Claim。

这套算法对 local name、地址和信道参数化，验收条件来自协议关系与解码结果。

### 10.2 设备与受控发射

设备名称先映射到声明式 HardwareProfile。Pluto 使用 USB IIO 扫描并提取 URI，再对精确 URI 执行 probe；B210 使用 UHD 发现和 probe。流图 Builder 由 Profile 选择。

批准 RF 计划后，系统根据当前语义哈希创建 `.armed.grc`。Runtime 解析可导入所需 GNU Radio 模块的 Python 解释器，生成代码并有限时长启动。启动宽限期内退出会直接返回失败。

### 10.3 空口 Evidence

人工在 LightBlue 中观察目标 local name。点击“已看到目标名称”时，服务端执行：

```text
query_runtime_status
→ 校验 running、deadline、run_id
→ 校验 expected_name == observed_name
→ 记录 observed_at 和 evidence_kind
→ 可选复制截图到 final/evidence
→ 创建 over_air_observed Claim
```

随后 `stop_and_finalize` 停止同一运行并验证干净退出。`duration_seconds` 是最大运行窗口，空口确认可提前结束运行。

---

## 11. 持久化与恢复

三类文件的语义分工：

| 文件 | 语义 |
|---|---|
| `state.json` | 当前可用的领域事实与工程状态 |
| `workflow.yaml` | 活动任务的执行控制状态 |
| `events.jsonl` | 可追加、可计时、可审计的事件轨迹 |

恢复时按以下顺序：

1. 载入 SharedState；
2. 载入 Workflow 并校验 schema/revision；
3. 对比 `base_project_version` 和当前工程版本；
4. 查询硬件 runtime；
5. 将中断的 `running` Stage 收敛为可重试、等待用户或失败；
6. 刷新 Claims 和 GUI digest。

路径应以 session 根目录为基准持久化。导出 Manifest 应根据本轮显式产物集合生成，逐项写入相对路径、大小和 SHA-256。

---

## 12. 当前算法完善项

### P0

1. Checkpoint 决策形成完整 ResultEnvelope 和 Completion 结果。
2. 导出 Artifact 集合由执行事务显式维护，Manifest 只消费该集合。
3. Evidence 附件与 Claim、`run_id`、观察时间原子提交。
4. GUI/Event 中分别展示 capability owner、execution mode 和 executor。

### P1

1. BLE 广告信道 37/38/39 调度、每信道白化和空口验收。
2. runtime 超时、进程崩溃、重复启动和 emergency-stop 的模型化故障注入。
3. 多轮反馈对 Stage 依赖图的覆盖率度量。
4. Intent 数据集加入否定约束、模糊硬件名、复合操作和对抗性表达。

### P2

1. 多 Task 排队和跨 Workflow 依赖；
2. 可并行 Stage 的资源锁与合并规则；
3. 依据事件轨迹学习 Task/Stage 规划策略，同时保留 Catalog 安全约束。
