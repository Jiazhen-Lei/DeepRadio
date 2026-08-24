# DeepRadio 工程落地 V2

> 日期：2026-08-24
> 读者：`grc/agent`、`grc/gui` 的开发与维护人员
> 范围：当前模块边界、持久化、GUI 接入、BLE/SDR 运行链路和工程待办
> 测试方法见 `DeepRadio_Test_and_Experiment_V2.md`，状态迁移语义见 `DeepRadio_Workflow_Algorithm_V2.md`。

---

## 1. 当前工程基线

DeepRadio 在 GRC 中使用一套共享 Workflow 控制面完成意图识别、任务编排、Stage 执行、用户确认、工具调用、证据记录和工程交付。

当前基线包括：

- 七类 Task Catalog，以及按条件选择的串行 Stage；
- `WorkflowEngine` 统一管理活动任务、状态迁移、重试、Checkpoint 和失效传播；
- `state.json` 保存领域事实，`workflow.yaml` 保存执行状态，`events.jsonl` 保存追加式事件；
- 确定性 Stage handler 与 LLM Subagent 共享 `TaskCard`、`ResultEnvelope` 和 Completion 契约；
- GUI 展示 Task、Stage、等待原因、完成计数、尝试次数和执行时间线；
- BLE 广播 PDU、CRC、白化、GFSK 波形、Pluto/B210 发射流图的参数化生成与离线校验；
- Pluto、B210 等设备的声明式能力档案，以及设备发现和身份绑定探测；
- 受控 RF 运行：显式授权、启动健康检查、有限时长、状态查询、停止和紧急停止；
- `run_id` 贯穿启动、空口确认、停止、Claim 和 Evidence；
- 会话产物、SHA-256 Manifest、运行状态与日志持久化；
- 2026-08-24 PlutoSDR BLE 实机链路已完成：手机 LightBlue 扫描到动态名称 `loveu`，运行进程正常停止。

自动回归基线为 79 项通过、1 项跳过。该结果覆盖控制面和契约；Pluto 空口能力由独立 HIL 会话证明。

---

## 2. 模块地图

```text
grc/agent/
├── workflow/
│   ├── task_catalog.yaml    七类 Task、条件 Stage、完成条件和转移
│   ├── schema.py            Intent、Workflow、Stage、Checkpoint 数据结构
│   ├── engine.py            实例化、迁移、Checkpoint、失效与摘要
│   ├── completion.py        Completion 硬门槛
│   └── intent_llm.py        低置信意图的结构化补全
├── service/
│   ├── adapter.py           ServiceAgent 入口和 Workflow 驱动循环
│   ├── orchestrator.py      Stage 范围内的 Agent/Tool 装配
│   ├── stage_executor.py    确定性执行器和 ResultEnvelope
│   ├── subagents.py         Subagent、Skill、Tool 能力声明
│   ├── tools_lc.py          LangChain Tool 适配与范围过滤
│   ├── hardware_runtime.py  有限时长进程、输出排空和停止
│   └── session_store.py     会话文件、事件、归档和导出
├── state/
│   ├── shared_state.py      RadioSpec、Project、Claim、Coordination
│   ├── claim_store.py       Evidence 绑定和版本失效
│   ├── policy.py            ALLOW / PROPOSE / DENY / CONFIRM
│   └── snapshot.py          工程快照与恢复
├── tools/
│   ├── registry.py          Tool 注册中心
│   ├── ble_tools.py         BLE 协议、波形和发射流图
│   ├── hardware_profiles.py SDR 能力档案与探测解析
│   ├── hardware_tools.py    discover/probe/configure/start/stop/status
│   └── state_tools.py       Spec、Project、Claim 操作
└── skills/
    ├── grc-spec · grc-build · grc-critic · grc-sim
    ├── grc-diagnosis · grc-hardware
    └── grc-ble-advertising · grc-ble-phy

grc/gui/
├── AgentPanel.py            对话、结构化决策、运行状态刷新
└── ClaimsPanel.py           Claims、Workflow Inspector、执行时间线
```

`adapter.py` 仍承担较多职责。后续拆分建议保持行为等价：

```text
service/adapter.py
├── service_agent.py        GUI/API 薄入口
├── workflow_driver.py      自动驱动循环
├── deepagent_runner.py     LLM 调用与协议校验
├── result_projector.py     Workflow → SharedState
└── reply_renderer.py       基于事实生成回复
```

拆分期间必须维持 `WorkflowEngine` 的唯一写入入口以及现有会话格式。

---

## 3. 会话与导出文件

```text
local/agent_sessions/<session_id>/
├── state.json
├── workflow.yaml
├── events.jsonl
├── snapshots/
├── work/
└── final/
    ├── *.grc
    ├── *.armed.grc
    ├── manifest.json
    ├── evidence/
    └── hardware_runtime/
        ├── generated_flowgraph.py
        ├── runtime.log
        └── runtime_status.json
```

### 3.1 `state.json`

保存当前会话的领域事实：用户规格、工程版本、活动流图、锁定约束、设备配置、Claims、Evidence、快照和运行摘要。工具通过统一服务接口修改，Subagent 通过 TaskCard 获取任务范围，不直接任意写文件。

### 3.2 `workflow.yaml`

文件内容采用 JSON 子集，保存 `workflow_id`、Task、Intent、Stage 列表、当前 Stage、状态、attempt、outcome、Completion 结果、Checkpoint、revision 和 `base_project_version`。

### 3.3 `events.jsonl`

每行一个追加事件，包含单调 `seq`、时间、Workflow/Stage/attempt、用户输入与决定、执行模式、Agent/Tool、Policy、结果和异常。它是问题复盘与实验计时的主要来源。

### 3.4 Manifest

会话内 Manifest 使用相对路径、文件大小和 SHA-256。导出目录的 Manifest 当前按目标目录扫描生成。目标目录范围过大时会把其他会话文件纳入清单，因此导出必须使用独立会话目录。工程修复项是让导出 API 接收本轮显式产物集合，再据此生成 Manifest 并逐项验证存在性和哈希。

---

## 4. Workflow 与执行边界

`WorkflowEngine` 是控制面的唯一状态机。`SharedState` 提供工程事实，二者在 Stage 提交事务中同步：

1. Engine 选中可运行 Stage；
2. Orchestrator 根据 Stage 白名单装配 Subagent 和 Tool；
3. Executor 返回严格的 `ResultEnvelope`；
4. CompletionEvaluator 核对必需产物、Claim、工具结果和版本；
5. Engine 提交状态转移并追加事件；
6. Adapter 将摘要投影到 `state.json` 和 GUI。

执行模式有两种：

- `deterministic`：协议构建、结构校验、设备探测、RF 启停等可复现且安全敏感的 Stage；
- `llm_subagent`：需要语义理解、解释、方案生成或多工具推理的 Stage。

确定性执行速度通常在毫秒到秒级。它调用通用参数化算法和工具链，用户输入中的设备、频率、名称、时长等参数仍在运行时解析并写入产物。

事件中的执行者应展示真实模式。当前部分事件同时出现推荐 Subagent 与 `deterministic_stage_handler`，GUI 需要增加明确的“执行模式”字段，避免把能力归属误解为一次真实 LLM 调用。

---

## 5. BLE 与 SDR 工程链路

BLE 部署使用 `HARDWARE_CONFIGURE` 的 `deploy_stages`：

```text
protocol_spec_alignment（按需）
→ build_ble_advertiser
→ offline_protocol_verify
→ discover_and_probe_device
→ rf_plan_confirmation
→ configure_device
→ transmit_bounded
→ over_air_verification
→ stop_and_finalize
```

### 5.1 协议与波形

BLE 工具根据输入动态生成 Advertising PDU、长度字段、CRC24、白化序列和 Gaussian GFSK 波形，并进行解白化、解调和协议字段回环校验。校验依据算法关系和解析结果，局部名称与 CRC 值均由输入决定。

当前 BLE 发射主路径使用广告信道 37（2.402 GHz）。37/38/39 三信道轮询属于后续能力。

### 5.2 设备档案

`hardware_profiles.py` 将用户名称归一化为设备能力：

| 设备 | 驱动 | 发现 | 身份探测 | BLE Builder |
|---|---|---|---|---|
| PlutoSDR | IIO | `iio_info -S usb` | `iio_info -u <URI>` | Pluto sink |
| USRP B210 | UHD | `uhd_find_devices` | `uhd_usrp_probe --args ...` | UHD sink |

Pluto 发现阶段解析稳定 URI，探测阶段绑定该 URI。设备存在性的判断依赖结构化输出特征，命令退出码与探测内容共同决定结果。

### 5.3 RF 安全与运行事务

RF 启动需要同时满足：

- 离线协议校验通过；
- 指定设备发现和身份探测通过；
- 用户批准 RF 计划；
- 当前流图的语义哈希与批准版本一致；
- `GRC_AGENT_ENABLE_RF=1`；
- 可用 Python 解释器成功导入生成流图需要的 GNU Radio 模块。

基础 `.grc` 保持发射禁用。批准后生成 `.armed.grc`，写入精确设备身份和受控发射参数。运行器使用选定解释器执行生成的 Python，经过启动宽限期后返回 `ready` 和 `startup_health_passed`。

每次运行生成唯一 `run_id`，状态至少包含 PID、解释器、程序路径、开始时间、deadline、运行状态和 return code。后台线程持续排空 stdout/stderr，防止管道阻塞。每个 session 同时只允许一个硬件进程。

停止规则：

- 正常停止记录 `return_code=0`、`crashed=false`；
- 启动后异常退出记录 crash，并使运行 Claim 失败；
- reset、archive 和 emergency stop 清除 RF armed 状态；
- 空口确认必须引用仍在运行且未超过 deadline 的同一 `run_id`。

---

## 6. GUI 接入

`AgentReply.workflow_digest` 由领域 Workflow 生成，GUI 仅做展示和结构化命令转发。

当前展示内容：

- Task 名称、Task Type、Stage 名称和序号；
- Workflow/Stage 状态、attempt 和 Completion 计数；
- 等待原因与确认/取消按钮；
- Claims、测量、规格摘要和事件时间线；
- 硬件运行状态、`run_id`、剩余时长和 return code；
- 空口验收按钮“已看到目标名称”和“未看到”。

GUI 的决策命令必须携带 `checkpoint_id`。空口批准时服务端重新查询运行状态并校验目标名称与 `run_id`，随后生成 Evidence Claim。可选截图通过服务接口复制到 `final/evidence/`。

当前用户仍可能在自动运行完成后点击 GRC 的运行箭头，因为 runtime 输出只保存在日志中，画布控制台缺少同等反馈。GUI 工程项包括：

1. 在对话区持续显示 managed runtime 的启动、运行、停止和末尾日志；
2. 提供受控“重新运行”入口，并复用 RF Policy、设备身份和运行时限；
3. 将流图摘要改成 BLE 专用字段，显示 PDU、PHY、信道、频率、采样率和设备；
4. Checkpoint 解决后写入结构化 Completion 结果，使 Inspector 显示 `1/1`；
5. 为 Evidence 提供截图选择和预览。

---

## 7. 2026-08-24 Pluto HIL 证据

会话：`local/agent_sessions/0824_V6/gui-9edd1171`<br>
导出：`local/output/0824_V6`

| 事件 | 结果 |
|---|---|
| 用户提交 | 21:14:01.533 |
| 到达 RF 确认 | 21:14:01.922 |
| 用户批准 RF | 21:14:39.460 |
| managed runtime ready | 21:14:40.582 |
| 空口确认 | 21:14:46.477，运行中，elapsed 约 6.65 秒 |
| 停止 | 21:14:47.049，`return_code=0`，`crashed=false` |
| 运行标识 | `run-f646528e87c5` |
| 手机结果 | LightBlue 扫描到 `loveu` |

前三个自动 Stage 约 0.39 秒完成，原因是采用确定性协议和设备工具；RF 启动约 1.12 秒完成。`duration_seconds=30` 表示最长运行时长，空口确认完成后工作流主动停止，因此实际运行约 7.2 秒。

本次 HIL 同时暴露出以下工程项：

- 导出 Manifest 纳入了宽目录中的其他会话文件；
- 自动运行日志与 GRC 画布运行状态之间的可见性不足；
- HIL 截图没有随空口 Claim 一起进入 Evidence；
- Checkpoint Stage 的 Completion 计数显示不完整；
- 会话目录移动后，部分绝对路径失效；
- 通用流图摘要对 BLE PHY 的表达不完整。

---

## 8. 工程优先级

### P0：产物与运行可信度

1. 导出目录按 session 隔离，Manifest 从显式产物集合生成并校验。
2. GUI 展示 managed runtime 的实时状态和日志，提供受控重跑入口。
3. Checkpoint 写入完整 ResultEnvelope 与 Completion 结果。
4. Evidence 文件与空口 Claim、目标名称、时间和 `run_id` 原子绑定。
5. 会话持久化统一使用 session 相对路径，加载时解析为当前根目录。

### P1：能力完整度

1. 实现 BLE 37/38/39 信道调度和对应离线/空口验证。
2. 扩充真实进程的超时、崩溃、重复启动和 emergency-stop 故障注入。
3. 完成 B210 BLE 发射 HIL 和 B210 QT 频谱 HIL。
4. 在 Inspector 中明确显示 `deterministic` 与 `llm_subagent`。

### P2：结构治理

1. 拆分 `service/adapter.py` 和 `workflow/engine.py`，保持公开契约稳定。
2. 将 `state_tools.py` 按 Spec、Project、Claim 职责拆分。
3. 根据 Tool registry 元数据生成常规 LangChain 包装器。
4. 将 JSON 子集文件迁移到 `.json` 扩展名，并提供一次性兼容加载。

---

## 9. 启动环境

```bash
conda activate gnuradio
iio_info -S
export GRC_AGENT_ENABLE_RF=1
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

RF 实验必须在合法、低功率、可控环境中进行。`GRC_AGENT_ENABLE_RF` 只开放运行能力，Workflow 中的设备探测、用户确认、语义哈希和 Completion 仍需全部通过。
