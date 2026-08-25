# DeepRadio 工程落地

> 日期：2026-08-25
> 读者：`grc/agent`、`grc/gui` 的开发与维护人员
> 范围：模块边界、持久化、GUI 接入、BLE/SDR 运行链路、0825 GUI 实验问题与方案、待办
> 测试方法见 `DeepRadio_Test_and_Experiment_V2.md`，状态迁移语义见 `DeepRadio_Workflow_Algorithm_V2.md`。

---

## 1. 工程基线

DeepRadio 在 GNU Radio Companion（GTK）上提供自然语言到可运行流图的能力。用户文本进入 `ServiceAgent.step()`，由 `WorkflowEngine` 识别任务、编排串行 Stage、处理确认，再通过 `registry.call` 执行工具，结果投影到 `AgentReply`、会话文件和 GUI。

基线包括：

- 七类 Task Catalog，按条件选择串行 Stage；
- `WorkflowEngine` 管理活动任务、状态迁移、重试、Checkpoint 和失效传播；
- `state.json` 保存领域事实，`workflow.yaml` 保存执行状态，`events.jsonl` 保存追加事件；
- 确定性 Stage handler 与可选 LLM Subagent 共用 `TaskCard`、`ResultEnvelope` 和 Completion；
- BLE 广播 PDU、CRC、白化、GFSK 波形、Pluto/B210 发射流图的参数化生成与离线校验；
- 设备能力档案、发现、身份绑定探测；
- 受控 RF：用户确认、`GRC_AGENT_ENABLE_RF=1`、启动健康检查、有限时长、查询、停止和紧急停止；
- `run_id` 贯穿启动、空口确认、停止、Claim 和 Evidence；
- 会话产物使用 session 相对路径；导出 Manifest 只收录本轮显式产物。

自动回归命令与空口实验记录见 `DeepRadio_Test_and_Experiment_V2.md`。

---

## 2. 模块地图

```text
grc/agent/
├── workflow/
│   ├── task_catalog.yaml    七类 Task、条件 Stage、完成条件和转移
│   ├── schema.py            Intent、Workflow、Stage、Checkpoint
│   ├── engine.py            实例化、迁移、Checkpoint、失效、低置信 Intent 补全
│   └── completion.py        Completion 硬门槛
├── service/
│   ├── adapter.py           ServiceAgent：入口、Stage 循环、确定性门、可选 LLM
│   ├── orchestrator.py      Stage 范围内的 Agent/Tool 装配
│   ├── stage_executor.py    确定性执行器与 ResultEnvelope
│   ├── subagents.py         Subagent、Skill、Tool 与 system-prompt
│   ├── tools_lc.py          LangChain 桥，调用 registry.call
│   ├── hardware_runtime.py  有限时长 RF 子进程
│   └── session_store.py     会话文件、事件、归档和导出
├── state/
│   ├── shared_state.py      RadioSpec、Project、Claim、Coordination、路径改写
│   ├── claim_store.py       Evidence 绑定和版本失效
│   ├── policy.py            ALLOW / PROPOSE / DENY / CONFIRM
│   └── snapshot.py          工程快照与恢复
├── tools/
│   ├── registry.py          唯一执行入口
│   ├── knowledge_tools.py   块检索与描述
│   ├── build_tools.py       增块、连线、改参、写出 .grc
│   ├── critic_tools.py      结构校验
│   ├── sim_tools.py         仿真、读指标、绘图
│   ├── ble_tools.py         BLE 协议、波形和发射流图
│   ├── hardware_profiles.py SDR 能力档案与 device_args
│   ├── hardware_tools.py    discover / probe / configure / start / stop
│   ├── state_tools.py       Spec、Project、Claim 操作
│   ├── design_link.py       通用配方建图宏
│   ├── debug_by_metric.py   指标诊断宏
│   └── narrate.py           按专业度组织回复
├── runtime/simulate.py      无头仿真（EVM / 星座 / 频谱）
├── knowledge/recipes.py     通用通信配方
├── memory/profile.py        用户档位
├── skills/                  grc-spec / grc-block-rag / grc-build / grc-critic /
│                            grc-sim / grc-diagnosis / grc-hardware /
│                            grc-ble-advertising
├── env.py · llm.py · schema.py
└── tests/
    ├── test_seven_tasks.py
    ├── test_ble.py
    ├── test_hardware.py
    └── test_workflow.py

grc/gui/
├── AgentPanel.py            对话、档位、重置、撤销、运行状态轮询
├── ClaimsPanel.py           任务 / 运行时 / 规格、确认、执行详情
├── chat_markup.py           对话 Markdown → Pango
└── tests/test_chat_markup.py
```

`hardware_runtime.py` 管受控 RF 子进程。`runtime/simulate.py` 管本地无头仿真。只有 `WorkflowEngine` 写入 `workflow.yaml`。

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

GUI 产物目录为 `local/output/`。

### 3.1 `state.json`

保存规格、工程版本、活动流图、设备配置、Claims、Evidence、快照和运行摘要。路径以 session 根为基准相对化，加载时解析回当前根目录。

### 3.2 `workflow.yaml`

JSON 子集。保存 `workflow_id`、Task、Intent、Stage 列表、当前 Stage、状态、attempt、outcome、`completion_result`、Checkpoint、revision 和 `base_project_version`。

### 3.3 `events.jsonl`

每行一个追加事件：单调 `seq`、时间、Workflow/Stage/attempt、用户输入、执行 `mode`、Tool `origin`、Policy、结果。GUI 时间线 Actor 列同时展示 tool、origin 和 mode。

### 3.4 Manifest

会话内 `final/manifest.json` 记录相对路径、大小和 SHA-256。导出调用 `write_export_manifest(..., exported_paths)`，只收录本轮复制出去的文件。导出目标必须是本轮专用空目录。

---

## 4. Workflow 与执行边界

`WorkflowEngine` 是控制面状态机。`SharedState` 提供工程事实。一次 Stage 提交：

1. Engine 选中可运行 Stage；
2. Orchestrator 按 Stage 白名单装配 Subagent 和 Tool；
3. 主机控制面执行安全敏感 Stage，或可选 LLM 执行语义 Stage；
4. Executor 返回 `ResultEnvelope`；
5. CompletionEvaluator 核对产物、Claim、工具结果和版本；
6. Engine 提交转移并追加事件；
7. Adapter 投影到 `state.json` 和 GUI。

执行模式：

- `deterministic`：协议构建、结构校验、设备探测、RF 启停；
- `llm_subagent` / `deepagents`：已配置 `GRC_AGENT_*` 且安装 deepagents 时的语义 Stage。

LLM 未配置或组装失败时，同一套 Catalog、State 和 `registry.call` 由确定性 handler 执行；通用仿真建图走 `design_link`。

主机控制面 Stage 包括：`build_ble_advertiser`、`offline_protocol_verify`、`discover_and_probe_device`、`discover_and_probe_hardware`、`configure_device`、`transmit_bounded`、`run_bounded`、`stop_and_finalize`、`stop_runtime`。

---

## 5. BLE 与 SDR

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

实时观察使用 `runtime_stages`（发现/探测 → RF 确认 → 有限运行 → 观察确认 → 停止）。仅保存配置使用默认 `stages`（预检 → 确认 → 记录配置）。

### 5.1 协议与波形

BLE 工具按输入生成 Advertising PDU、长度、CRC24、白化序列和 Gaussian GFSK 波形，并做解白化、解调和字段回环。协议工具可按广告信道 37 / 38 / 39 单独生成 PDU 与波形。部署路径取 `advertising_channels[0]` 作为当前发射信道：用户写 2.402 / 2.426 / 2.480 GHz 时分别落到 37 / 38 / 39；未指定频率时槽位为 `[37, 38, 39]`，构建使用 37。三信道跳频调度未实现。

### 5.2 设备档案

`hardware_profiles.py` 将用户名称归一化为能力。B210 的 `default_device_args` 为 `type=b200`。

| 设备 | 驱动 | 发现 | 身份探测 | BLE Builder |
|---|---|---|---|---|
| PlutoSDR | IIO | `iio_info -S usb` | `iio_info -u <URI>` | Pluto sink |
| USRP B210 | UHD | `uhd_find_devices` | `uhd_usrp_probe --args type=b200` | UHD sink |

另有 HackRF、LimeSDR、未指定型号 USRP 的频率与驱动档案；它们没有 BLE TX Builder。

Pluto 发现阶段解析 USB URI，探测绑定该 URI。B210 发现与 probe 使用同一 `type=b200` 身份。

### 5.3 RF 运行

启动需同时满足：离线协议校验通过（部署路径）、指定设备发现和身份探测通过、用户批准 RF 计划、流图语义哈希与批准版本一致、`GRC_AGENT_ENABLE_RF=1`、选定解释器能导入所需 GNU Radio 模块。

基础 `.grc` 保持发射禁用。批准后生成 `.armed.grc`。`HardwareRuntime` 用选定解释器执行生成的 Python，启动宽限期后返回 `ready` 与 `startup_health_passed`。每个 session 同时一个硬件进程。stdout/stderr 持续排空。

停止：正常退出 `return_code=0`、`crashed=false`；启动后异常退出记 crash 并使运行 Claim 失败；reset / archive / emergency stop 清除 armed 状态。空口确认必须引用仍在运行且未超过 deadline 的同一 `run_id`。

---

## 6. GUI 接入

`AgentReply.workflow_digest` 由 Workflow 生成。GUI 展示并转发结构化命令。

`AgentPanel`：对话、字体、专业度档位（自适应 / 小白 / 学生 / 专家）、重置、撤销到上一版本。交付时 `open_flow_graph` 原地刷新画布。确认阶段不改画布。运行中每秒 `peek_runtime_digest` 刷新状态。重置触发紧急停止并归档 Workflow。

`ClaimsPanel` 默认三行：任务/阶段、运行时、规格。BLE 规格显示协议摘要，隐藏「改规格」。可折叠「执行详情」含 Stage 列表（含 `completion n/m`）、时间线（Seq / Event / Stage / Actor）。Claims 表默认折叠。

确认按钮随 Checkpoint 变化：

- 普通 Checkpoint：确认 / 取消；
- `rf_plan_confirmation`：批准有限时长发射 / 取消；
- `over_air_verification`：已看到目标名称 / 未看到，可「附加上传截图」；
- 失败且 `can_retry`：受控重试发射。

RF 运行时提示「无需点击 GRC Run」。命令携带 `checkpoint_id`。空口批准时服务端重查 runtime，校验目标名称与 `run_id`，可选把截图复制到 `final/evidence/`。

可选勾选「一句话直出(baseline)」时，该轮调用 `build_flow_graph_from_text`，不进入 Workflow 与 Claims。

---

## 7. 0825 GUI 代表实验：问题、思考与方案

证据：`local/output/0825/`、`local/agent_sessions/0825/`。实验输入即测试文档 §4.1–§4.3。HIL 时间线仍只记在测试文档。

### 7.1 观察到的问题

| 用例 | 会话 | 控制面结果 | 用户可见失败 |
|---|---|---|---|
| Task 1 端到端仿真 | `gui-efefe806` | `END_TO_END_SIM` 完成，EVM 5.892% | 主路径通过。次要：配方多画了眼图；File Sink 写入会话绝对路径 |
| Task 2 发射机构建 | `gui-e0237fd9` | Task 为 `TX_BUILD`，停在 `spec_alignment` | 用户写「只做仿真，不接真实硬件」，系统仍要 SDR / 载频 / 采样率；无 `.grc` |
| Task 3 接收机构建 | `gui-8a9fb43c` | `RX_BUILD`，attempt 1 约 280s 后 `APITimeoutError`，自动进入 attempt 2 且停在 `running` | 界面长期「处理中」、执行详情空、画布不刷新。会话 `final/` 已有 `rx_bpsk_awgn.grc`（含定时恢复与判决），未仿真 BER，未导出 |

自动回归里的短句「构建一个 QPSK 发射机」「构建 BPSK 接收机并测 BER」可以通过：它们不含「硬件」子串，且 unittest 通常不配置 LLM，建图走 `design_link`。GUI 实验踩的是约束语义和大模型执行路径。

### 7.2 根因

**Intent 层（Task 2）**

1. `_detect_capabilities` 用 `_HARDWARE_HINTS`（含「硬件」「sdr」）做子串命中。「不接真实硬件」被加成 `hardware_configure`。
2. 有该能力后，`_missing_slots` 强制硬件三槽，`_compose_stages` 把 `HARDWARE_CONFIGURE` 接到 TX 后面。
3. `classify` 只在 `confidence < 0.9` 时调用 `complete_intent`。Task 2 因已抽出 `modulation=qpsk`、`direction=tx`，置信度 0.95，LLM 没有机会校正。
4. `complete_intent` 的 `_merge` 只允许 LLM **追加** capabilities，不能删除规则误加的能力，也不能把用户约束写成受保护槽位。

**执行层（Task 3）**

1. `rx_build_and_verify` 不在主机控制面。GUI 配置了 `GRC_AGENT_*` 后，整段 Stage 交给 `agent.invoke()`。LLM 的职责混了「识别 / 选型 / 解释」和「亲自建图」。
2. `_run_deep` 只对 `GraphRecursionError` 按已有产物交付；`APITimeoutError` 直接抛出。`tool_called` 要等 `_fold` 才写入 `events.jsonl`，超时后工具轨迹丢失。
3. Engine 对 `errored` 且 `attempt < max_attempts` 立刻把 Stage 标回 `pending`。`step()` 的 `_continue_autonomous` 马上开第二次调用。第一次错误文案被盖掉，GUI 继续显示「处理中」。
4. 长调用期间 GUI 不读 `workflow.yaml`，所以 Inspector 为空、规格显示「尚未提取」。第一次失败也未 `open_flow_graph`，画布保持空白。

**过拟合风险（对初版改法的检讨）**

两套「快修」会把系统钉死在这次三个句子上：

- 只加「不接真实硬件 / simulation only」关键词表：换一种否定（「先别接板子」「不要上射频」）仍会失败。
- 按 Stage id 规定 `build_and_verify` / `tx_build_and_validate` / `rx_build_and_verify` 一律 `design_link`、禁止 LLM：没有配方的新架构无法走智能识别，有配方时 LLM 也不能做选型与解释。

要修的是 **约束与能力冲突**、以及 **识别/解释 vs 可证明构建** 的分工，不是把三个代表用例写成特例。

### 7.3 约束语义：用 LLM 判断，规则作兜底

否定、范围限制、安全边界属于 Intent 语义，应当走结构化补全，而不是只靠词表。

契约：

1. 规则层继续做廉价候选：设备专名、肯定的「配置 / 接 SDR」、协议词、调制与方向。
2. 只要 `raw_text` 含约束语气或规则能力可能互相打架（例如同时出现硬件词与仿真/禁止硬件），**即使规则置信度 ≥ 0.9 也要做一次约束调和**。有 LLM 时调用 Intent 补全；无 LLM 时用约束解析器（可含一小段否定模式作为离线兜底，不能当唯一语义源）。
3. 补全输出增加 `constraints` / `forbidden_capabilities`。`_merge` 允许 LLM **删除**与约束冲突的能力，且不得覆盖 `slot_sources=user` 的槽位。用户明确禁止的能力不得再进入 Catalog 组合。
4. Prompt 写清：同句里的「不要 / 只 / 禁止」约束优先于关键词；不得因为出现「硬件」就打开 `hardware_configure`。
5. 回归用本次 Task 2 原文，以及至少两条未写进词表的否定（例如「先别接板子，只仿真」）。断言：`hardware_configure` 不在 capabilities、无硬件 Stage、能产出 TX `.grc`、事件无 `discover_devices` / `start_flowgraph`。有 LLM 与无 LLM 两条都要过。

### 7.4 仿真构建：LLM 做识别与解释，构建按配方覆盖决定

LLM 应当做意图、配方选择和面向用户的解释。它不应当在已有可覆盖配方时，成为建图的唯一执行器。

分工：

```text
raw_text
→ Intent（规则候选 + 约束调和，可含 LLM）
→ 配方是否覆盖剩余 capabilities
    → 覆盖：主机 registry.call(design_link / design_flowgraph)
    → 不覆盖：LLM 用原子工具按块构建，或停在 waiting 说明缺口
→ Completion（结构、仿真、BER/EVM、Claim）
→ LLM / narrate 解释结果
```

这是 **capability 与配方覆盖** 策略，不是「七类 Task 的三个 Stage 写死 design_link」：

- 新调制、新接收结构、无配方的请求，仍然走 LLM + 原子工具或澄清。
- 已有 `rx_bpsk_awgn` 这类覆盖「自包含 + 定时恢复 + 判决 + BER」的配方时，主机建图；LLM 解释 BER、环路带宽、探针含义。
- 主编排可以 `select_recipe`，但 `design_flowgraph` / `run_simulation` 的成功以工具结果为准。超时后若 `final/` 已有 `.grc`，按已有产物评估 Completion，缺 BER 则 `waiting`，并展示流图。

超时与重试：

- 工具调用当时写入 `events.jsonl`。
- 仿真 Stage 的 LLM/API 超时：有产物则交付或等待补测；无产物则 `waiting` 并显示错误。禁止在同一用户回合里静默第二次长调用。
- GUI 在 Stage `running` 时按会话 digest 刷新任务/阶段；交付时再 `open_flow_graph`。

### 7.5 实现顺序

1. Intent 约束调和（LLM 可删能力 + 无 LLM 兜底）与 Task 2 原文回归。
2. 配方覆盖时主机建图；LLM 负责选型说明和结果解释；无覆盖时保留 LLM 按块构建。
3. 超时按已有产物收敛；禁止静默重试；工具事件即时落盘；GUI 运行中刷新 digest。
4. RX 在缺噪声/EbN0 时澄清或写入带来源的默认值；BER 同时绑定发送参考与接收判决探针。

BLE 跳频与 HIL 排在上述四项之后。

---

## 8. 待办

优先（由 §7 实验导出）：

1. Intent 约束调和：LLM 可删除冲突能力；无 LLM 时离线兜底；Task 2 原文及开放否定句回归。
2. 配方覆盖则主机 `design_link`；无覆盖则 LLM/原子工具或等待用户。LLM 始终可解释。
3. 仿真超时与 `errored` 不得在同一回合静默重试；已有 `.grc` 要投影到画布与 Completion。
4. 工具事件即时写入；GUI 在 `running` 刷新 digest。

其后：

5. BLE 广告信道 37/38/39 跳频调度（每信道白化、载频、空口验收与 Catalog Completion）。
6. 真实进程超时、崩溃、重复启动的故障注入钩子，供测试文档故障矩阵使用。

HIL、七类 Task GUI 实验步骤和发布门槛只写在测试文档。多任务排队、并行 Stage、跨工程任务不在当前产品范围。

---

## 9. 启动环境

```bash
conda activate gnuradio
iio_info -S
export GRC_AGENT_ENABLE_RF=1
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

RF 实验必须在合法、低功率、可控环境中进行。`GRC_AGENT_ENABLE_RF` 只开放运行能力。启动还要求设备探测、用户确认、语义哈希和 Completion 全部通过。
