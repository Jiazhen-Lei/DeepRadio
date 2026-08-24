# DeepRadio 工程落地 V2

> 日期：2026-08-23（按当前代码回写）  
> 读者：`grc/agent` / `grc/gui` 作者  
> 本文写模块、会话文件、GUI 接入、已落地项和剩余工程债。  
> 70 条用例集、HIL 点击步骤不在本文。算法语义见算法文档。
>
> 同族文档：
> - 产品：`DeepRadio_Product_V2.md`
> - 算法：`DeepRadio_Workflow_Algorithm_V2.md`
> - 工程：`DeepRadio_Engineering_V2.md`（本文）
> - 测试：`DeepRadio_Test_and_Experiment_V2.md`

---

## 1. 当前落地状态

控制面已经可用。V2 未完全对齐原始规范的全部研究门槛，但已经超过「骨架有、契约全缺」。

**已落地（不要当 P0 重做）**

- 七类 Task Catalog、串行 Stage、`WorkflowEngine`、`workflow.yaml`（JSON 子集）持久化；
- `turn_relation` 分流（确认/拒绝/回答/反馈/取消/覆盖新任务）；
- `CompletionEvaluator` 硬门槛；CONFIRM 不会把 Workflow 标成 completed；
- Checkpoint 与 Policy pending 通过 `checkpoint_id` 关联；GUI 走 `step_command`；
- Stage 过滤 Subagent/Tool；确定性 Stage handler 与 LLM 共用完成契约；
- Inspector：`workflow_digest` 含 Stage 列表、revision、attempt、completion 计数；
- 执行时间线读取 `events.jsonl` 最近事件（Seq/Event/Stage/Actor）；
- 领域工具：`inspect_flowgraph`、原子 patch、绘图、RX BER 双 probe；
- BLE 离线 PDU/波形/UHD TX `.grc`、只读 discover/probe、RF 默认关闭；
- B210 RX 实时频谱流图：`uhd_usrp_source` + `qtgui_freq_sink_x`，默认 2 Msps，不启动 RF；
- 低置信续跑保持 `workflow_id`；只有口头「新任务」或强 Task 切换才覆盖；
- 低置信 `classify()` 可走 Intent LLM 结构化补全；未配置或失败时回退规则，且不覆盖 `slot_sources==user`；
- 确定性路径按 recommended Subagent 合成 TaskCard/ResultEnvelope，`protocol_valid` 必须为 True；
- deepagents 路径按每次 `task` 委派绑定 ResultEnvelope；缺委派不得用确定性信封充数；`recommended_agents` 必须全覆盖；
- `invalidate()` 优先匹配 Catalog `depends_on`；相同结果指纹不再空转重试；
- 事件外层含单调 `seq` 以及 `workflow_id` / `stage_id` / `attempt` / `profile_level`。

**仍未完成**

| 项 | 说明 |
|---|---|
| 真实空口 HIL | 路径与 opt-in 测试已有；未接 B210 实跑 LightBlue / QT 频谱 |
| E1/E2 实验 | 70 条变体是分类-only；LLM 重复实验与真人交互仍未做 |
| BLE 三信道 | Intent 可写 37/38/39，构建只取 Channel 37 |
| stop 故障注入 | Tool 已有；真实/假进程异常场景未完整自动验收 |
| GUI 硬件按钮 | 重置会 emergency stop；没有常驻停止按钮 |
| 独立 BLE 向量 | 当前测试主要是实现自洽，不是标准向量对照 |

对应原规范 Phase：

| 阶段 | 当前状态 |
|---|---|
| Phase 1：Workflow 控制面 | **完成**（连续性边角已收：低置信续跑、事件 seq） |
| Phase 2：Stage 范围执行 | **完成契约**；确定性合成信封，deepagents 必须实际 `task` 且全覆盖 recommended agents |
| Phase 3：领域工具补全 | **完成工具+变体分类**；E1/E2 实验未做 |
| Phase 4：真实硬件闭环 | **离线+受控路径已实现，RF/HIL 默认关，实机未跑** |

原则不变：不另造第二套状态机；`WorkflowEngine` 仍是唯一控制面。七类 Task **不要加第八类**；BLE 继续挂在 `HARDWARE_CONFIGURE` 的 `deploy` Stage。

历史「上午缺口快照」已过时，不要再当现状。需要对照旧缺口时，以本节和 §6 为准。

---

## 2. 模块地图

```text
grc/agent/workflow/
  task_catalog.yaml     七类 Task 与条件 Stage
  schema.py             Workflow / Stage / Checkpoint / Intent
  engine.py             实例化、迁移、Checkpoint、digest()
  completion.py         Stage 完成硬门槛
  intent_llm.py         低置信 Intent 结构化补全

grc/agent/service/
  adapter.py            ServiceAgent.step() 接入 Engine；archive 触发 emergency stop
  orchestrator.py       Stage 范围装配
  subagents.py          6 个领域 + protocol_agent
  tools_lc.py           按 Subagent/Stage 过滤 Tool
  session_store.py      会话路径、原子保存、recent_events
  stage_executor.py     TaskCard / Envelope / 确定性 handler
  hardware_runtime.py   有限时长进程、stop / emergency stop

grc/agent/state/
  shared_state.py       RadioSpec / Project / Claim / Coordination
  claim_store.py        证据绑定、按版本失效
  policy.py             ALLOW | PROPOSE | DENY | CONFIRM
  snapshot.py           改图前快照 / 回滚

grc/agent/tools/
  registry.py / design_link.py / state_tools.py
  ble_tools.py          PDU / 波形 / UHD TX 流图
  hardware_tools.py     discover / probe / configure / start / stop

grc/agent/skills/
  grc-spec · grc-build · grc-critic · grc-sim
  grc-diagnosis · grc-hardware
  grc-ble-advertising · grc-ble-phy

grc/gui/
  AgentPanel.py         User Turn、digest 刷新、step_command
  ClaimsPanel.py        活动条、Inspector、执行时间线
```

会话目录：

```text
local/agent_sessions/<session_id>/
├── state.json
├── workflow.yaml
├── events.jsonl
├── snapshots/
├── work/
└── final/
```

文件名是 `workflow.yaml` / `task_catalog.yaml`，当前加载器按 JSON 读写（YAML 1.2 子集）。

---

## 3. 三类文件写什么

`state.json`：RadioSpec、ProjectState、当前 `.grc` 路径和 Flowgraph version、Claims 与 Evidence、锁定约束、snapshot 索引、工程配置。硬件配置-only 批准后，`project.config.device` 类似：

```json
{
  "type": "b210",
  "center_freq": 2400000000.0,
  "sample_rate": 1000000.0,
  "mode": "flowgraph_config_only"
}
```

`workflow.yaml`：`workflow_id`、Task Type、execution_status、revision、`base_project_version`、current_stage、Intent 与槽位、各 Stage 状态/attempt/outcome/结果摘要、Checkpoint。

`events.jsonl`：用户输入与决定、Workflow 创建和转移、Stage 生命周期、Subagent 委派、Tool 调用、Policy、产物、stale、恢复。每条含单调 `seq`。

---

## 4. GUI 接入

`AgentReply.workflow_digest` 由 `Workflow.digest()` 生成，ClaimsPanel / AgentPanel 直接消费，不另建状态机。

摘要字段包括：`workflow_id`、`task_type`、`execution_status`、`current_stage`、`stage_index`、`stage_total`、`waiting_reason`、Stage 列表、revision、project version、attempt、completion 计数、`timeline`。

确认/取消走结构化 `step_command` + `checkpoint_id`。确认阶段不把 `.grc` 载入画布；交付后原地刷新当前画布。session 内 Ctrl+S 会 `version+1` 并让 Claim 待重验。「重置」归档活动 Workflow，并触发 emergency stop。

不要把 GUI `Platform` 传到后台 `ServiceAgent` 线程。控制台日志用主线程空闲回调投递。

GTK 面板不能用浏览器代替验收。

---

## 5. 相对原规范的代码范围

原规范要求新增：

```text
grc/agent/workflow/
├── __init__.py
├── schema.py
├── engine.py
└── task_catalog.yaml
```

实际还增加了 `completion.py`、`intent_llm.py`、`stage_executor.py`、`hardware_runtime.py`、`ble_tools.py`、`hardware_tools.py`，以及 BLE Skills。

原规范要求调整、现已接入的文件：

| 文件 | 调整 |
|---|---|
| `service/adapter.py` | `step()` 接入 WorkflowEngine；统一新建、恢复和执行 |
| `service/orchestrator.py` | Stage 范围装配与执行入口 |
| `service/subagents.py` | Skill 改为列表；按 Stage 过滤；增加 `protocol_agent` |
| `service/tools_lc.py` | 按 Subagent/Stage 过滤 Tool |
| `service/session_store.py` | workflow 路径、原子保存、归档、recent_events |
| `state/shared_state.py` | TaskCard/ResultEnvelope 的 Workflow 字段 |
| `schema.py` | AgentReply 增加 `workflow_digest` |
| `gui/AgentPanel.py` | User Turn、digest、结构化 Checkpoint |
| `gui/ClaimsPanel.py` | Task/Stage/进度、Inspector、时间线 |

现有 `design_link`、registry、SharedState、Policy、ClaimStore、snapshot 继续复用。

---

## 6. 对照原规范 Phase 的剩余工程

原规范 Phase 1～3 的控制面、Stage 范围和领域工具已在代码中落地。剩余主要是实验和硬件实机，不是再搭一套引擎：

1. E1：LLM 重复运行与 C1/C2 对照；Gate 4 GUI 抽检；E2 真人。
2. 用独立标准向量验证 BLE PDU/CRC/whitening/GFSK；调制指数、频偏、频谱模板仍缺。
3. 用无 RF 假进程完成 stop / emergency-stop / 超时 / 崩溃测试后，才连接 B210 做只读 discover/probe。
4. 屏蔽或合规低功率环境验证有限时长 start/stop；实现 Channel 37/38/39 调度。
5. LightBlue 截图 Evidence 或独立 BLE sniffer；GUI 常驻停止按钮。

在 stop/Policy 故障注入完成前，不要把 `GRC_AGENT_ENABLE_RF=1` 当作默认可验收配置。

---

## 7. BLE / 硬件实现要点

Intent 扩展（不新增 Task Type）：

```json
{
  "operation": "deploy",
  "protocol": "ble",
  "ble_mode": "advertising",
  "local_name": "deepradio",
  "hardware": "b210",
  "sample_rate": 2000000,
  "advertising_channels": [37, 38, 39],
  "duration_seconds": 30,
  "gain": "explicit-safe-value"
}
```

`protocol_agent` 绑定 `grc-ble-advertising`、`grc-ble-phy`、`grc-build`、`grc-critic`。确定性 Tool：

```text
build_ble_advertising_pdu
generate_ble_1m_waveform
verify_ble_packet_bits
build_ble_uhd_tx_flowgraph
```

TX 流图：`blocks_file_source → blocks_head → uhd_usrp_sink`。构建后 `not_started=true`。Runtime 最长 60 秒；流图内置 60 秒 `head` 硬停止。增益首期 0～10 dB。

硬件 Tool 分级：

```text
discover_devices       只读，无需发射授权
probe_device           只读
configure_device       写配置，需要 Checkpoint
start_flowgraph        有限时长，需要 RF Checkpoint + GRC_AGENT_ENABLE_RF=1
stop_flowgraph         始终允许
emergency_stop         始终允许且最高优先级
query_runtime_status   只读
```

不允许 Agent 任意执行 Shell。只有允许列表中的 `.grc` / 生成代码可执行。stdout/stderr 由 Runtime 收集，完整事件化仍需补测试。

关键文件：

```text
grc/agent/tools/ble_tools.py
grc/agent/tools/hardware_tools.py
grc/agent/service/hardware_runtime.py
grc/agent/workflow/task_catalog.yaml
grc/agent/workflow/engine.py
grc/agent/workflow/completion.py
grc/agent/service/adapter.py
grc/agent/service/orchestrator.py
grc/agent/service/subagents.py
grc/agent/skills/grc-ble-advertising/SKILL.md
grc/agent/skills/grc-ble-phy/SKILL.md
grc/agent/skills/grc-hardware/SKILL.md
```

B210 RX 频谱：离线生成含 `uhd_usrp_source` 与 `qtgui_freq_sink_x` 的 `.grc`，停在 `hardware_precheck` / RF Checkpoint，不调用 `start_flowgraph`。

---

## 8. 启动与环境

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

`--fresh` / `GRC_DEEPRADIO_FRESH` 开新会话。未安装 `deepagents` 或未配置 `GRC_AGENT_*` 时走确定性 `design_link`。GUI 勾选「一句话直出 baseline」绕过 SharedState，不是 V2 主路径。
