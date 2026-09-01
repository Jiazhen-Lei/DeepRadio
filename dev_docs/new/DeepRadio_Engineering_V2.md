# DeepRadio 工程方案（V3 架构基线）

> 日期：2026-09-01
> 本文保留兼容文件名 `V2`，内容已清除旧版增量日志，只描述当前 V3 目标实现。

## 1. 工程原则

- 保留 DeepAgent、Main Agent、领域 Agent、WorkflowEngine、SharedState、Plan Compiler、Policy 与 deterministic handlers。
- 不新建第二套状态机、第二套工具注册表或第二套 Intent 真值源。
- Catalog 决定 Stage mode/tool scope；Agent Registry 决定角色能力；ToolSpec 决定工具 effect/requires。
- Agent 负责 proposal，Gateway 负责执行，Completion 负责验收。
- 优先删除重复映射和不可达兼容路径，新增代码必须替代旧职责而不是叠加。

## 2. 目标模块图

```text
grc/gui
  AgentPanel / ClaimsPanel
  workflow_presenter          read-only projection + structured commands

grc/agent/service
  adapter                     turn/command boundary and Stage router
  orchestrator                DeepAgent assembly only
  subagents                   domain-agent registry
  stage_handlers              deterministic executors
  stage_executor              TaskCard, invocation binding, StageResult
  tools_lc                    LangChain bridge and idempotent replay

grc/agent/workflow
  engine                      sole workflow writer
  task_catalog.yaml           Stage profiles + task fragments
  plan_compiler               deterministic composition/effect checks
  completion                  evidence-only predicates
  intent_alignment            sole SharedIntent writer

grc/agent/tools
  registry                    Execution Gateway + ToolSpec
  protocol/build/sim/hardware concrete tools

grc/agent/state
  shared_state                versioned facts, claims, runtime and coordination
```

## 3. Stage Profile 单一来源

`task_catalog.yaml.stage_profiles` 必须为每个 Stage id 声明：

```json
{
  "build_ble_advertiser": {
    "execution_mode": "hybrid",
    "allowed_tools": [
      "build_ble_advertising_pdu",
      "generate_ble_1m_waveform",
      "build_ble_pluto_tx_flowgraph",
      "validate_flowgraph"
    ]
  }
}
```

Catalog loader 将 profile 合并到每个 materialized/deferred Stage 并校验 mode。旧 `_STAGE_TOOLS` 删除；Plan Compiler、orchestrator 和 effect floor 统一读取 `Stage.allowed_tools`。

`Stage` 新增：

```python
execution_mode: str
allowed_tools: list[str]
```

不要把 Intent 的 `execution_mode=design/deploy/...` 与 Stage execution mode 混为一项；前者建议后续重命名为 `requested_effect_mode`。

## 4. 路由实现

`adapter._resolved_stage_mode(stage)`：

- checkpoint/deterministic/safety_finalizer 原样返回；
- 全局 deterministic override 只降低 Agent 使用；
- deepagents override 只影响 agentic/hybrid；
- hybrid 首次确定性执行；存在 prior failure/result history 后进入 agentic；
- Agent 构建失败时使用安全 deterministic fallback，否则 waiting。

每次 Stage 上下文必须注入：

```text
stage_id, stage_execution_mode,
stage_allowed_tools, stage_effect_level,
workflow snapshot, task card, state
```

事件同时记录 declared mode、resolved mode 和 actual executor。

## 5. Agent Registry

`subagents.py` 保留领域角色和各角色的完整工具能力。运行时：

```text
bound_tools = Agent.tools ∩ Stage.allowed_tools
```

这里的两份集合职责不同，不属于重复硬编码：Agent 集合表达专业能力，Stage 集合表达当前授权范围。禁止重新增加第三份 adapter route map。

Main Agent 只持有 `task` 委派能力；有 Subagent 时不直接持有业务工具。Agentic Stage 的系统 prompt 必须包含 completion、allowed tools、版本和信息增量停止规则。

## 6. Execution Gateway

`tools.registry.call` 是 Agent 与 Host handler 的统一入口，顺序检查：

1. ToolSpec 存在。
2. Tool 在 `stage_allowed_tools`。
3. Tool effect 不高于 `stage_effect_level`。
4. `requires` 全部满足；未知 requirement fail closed。
5. 工具参数与 JSON schema 一致。
6. Policy / checkpoint grant 满足；allow/deny 写入 `ctx.extra["events"]`。
7. 执行并返回 dict receipt。

当前必须支持的 requirements：`rf_runtime`、`device_probed`、`flowgraph_armed`、`user_effect_grant`。硬件工具内部保留相同底线检查，防止未来旁路 Gateway。

## 7. Result 与 Completion

`stage_executor.make_result_envelope` 必须同时要求：

```text
reply_ok && execution_protocol_ok && completion_ok
```

删除“completion 已满足则 invalid invocation 只 warning”的兼容行为。Host deterministic handler 使用 `synthesize_deterministic_invocations` 生成可信执行记录；Agent invocation 必须通过 identity/shape binding。

Completion 清理要求：

- 删除通用 `repair_decision_recorded=True`、`change_decision_recorded=True`、`flowgraph_decision_recorded=True`、`runtime_observation_recorded=True`。
- Checkpoint completion 只由 `WorkflowEngine._checkpoint_result` 写入。
- `flowgraph_saved` 必须关联本 Stage 的写工具或当前版本 receipt，不能只看任意旧文件存在。
- 所有 measurement/claim 必须校验 project/intent revision。

## 8. Interaction 与恢复

Workflow digest 输出稳定 InteractionRequest：`id/kind/status/reason/allowed_actions`。Presenter 必须先根据 `wait_kind` 补全 recovery/capability request，再判断 `action`，避免真实 digest 因 action 为空被隐藏。

GUI 只发送：

```text
interaction_response
checkpoint_decision
retry_stage
cancel_workflow
stop_runtime
emergency_stop
```

命令处理校验 interaction/checkpoint id 和 revision。任何 waiting state 的 presenter 单测都断言 `visible=True` 且 `allowed_actions` 非空。

## 9. Retry 与局部 effect

`_retry_waiting_stage` 不再通过 Workflow capabilities 判断是否扫描硬件。只在当前 Stage `effect_level >= DEVICE_READ` 时执行 fresh discovery/probe。Offline BLE verify、GRC validate、build repair 不触碰 SDR。

Hybrid Stage 首次失败后保留 last failure、missing completion、tool receipts 与 artifact refs，DeepAgent 只能针对缺口提出新操作。无新 evidence delta 时直接恢复给用户。

## 10. 性能工程

- 固定 Stage 绕过 LLM。
- 相同 Stage/arguments 的幂等工具结果缓存。
- Main/Subagent 独立调用预算和 token cap。
- TaskCard 只携带结构化摘要，不携带完整历史日志。
- DeepAgent checkpointer 按 session LRU；thread 按 Workflow revision + Stage 隔离。
- 独立 producer/validator 可并行，但具有 artifact dependency 的步骤保持顺序。
- 事件实时推送 GUI，避免长调用期间无反馈。

性能日志必须能拆分：Intent LLM、Agent LLM、Tool、Compile、Simulation、Hardware wait、User wait。

## 11. PlutoSDR 实现契约

软件构建：`build_ble_advertising_pdu`、`generate_ble_1m_waveform`、`build_ble_pluto_tx_flowgraph`（初始 sink disabled）、`verify_ble_packet_bits`、`validate_flowgraph` 和 gnuradio 环境中的 `grcc`。

硬件前置：`hardware_preflight` 只证明 Host/driver readiness；`discover_devices(device_type=pluto)` 使用 IIO 路径；`probe_device` 建立 exact identity；新失败探测清除旧 observed-device 投影。

RF：`rf_plan_confirmation` 建立绑定当前 Intent/Artifact/Device/Parameters/Duration 的 grant；`arm_hardware_flowgraph` 只 arm；`start_flowgraph` 校验编译、启动健康和时长；Stop/Emergency Stop 清除 grant/armed 状态。

测试报告使用以下准确等级：

```text
software_passed
hil_ready
device_detected
rf_runtime_passed
ota_verified
```

## 12. 测试矩阵

单元测试：Catalog profile、路由模式、Agent/tool 交集、Gateway scope/effect/requires、strict envelope、completion version、InteractionRequest、Stage-local retry。

集成测试：七类任务、多轮 alignment、修改/诊断/观察、BLE build/verify、GRC compile、session recovery、RF-disabled denial。

HIL：Pluto absent、driver absent、wrong device、probe failure、valid device、grant missing、duration exceeded、runtime crash、normal stop、emergency stop、OTA observed/not observed。

所有测试在 `gnuradio` 环境执行。若 `pytest` 未安装，使用 `python -m unittest discover`。GNU Radio 双映射 buffer 需要可写临时目录；受限环境失败不能误判为算法失败。

## 13. 代码清理清单

- 删除 `_STAGE_TOOLS` 和所有仅服务于它的导入/兼容 helper。
- 删除“全部 Stage 默认 deepagents”的路由和对应旧测试。
- 删除 invalid envelope warning-pass 分支。
- 删除 completion 的永真占位。
- 删除 Workflow-level hardware retry 判定。
- 删除 Presenter 对 recovery `action` 的隐式依赖。
- 检查未使用 imports、旧版本注释、重复设备/task 分支和生成的 `__pycache__`；不提交缓存文件。

## 14. 完成定义

- 文档、Catalog、Schema、router、Gateway、Completion 与 GUI 使用同一 V3 契约。
- 全量单测无失败。
- Pluto BLE 软件链生成、验证和 `grcc` 编译通过。
- 无 Pluto 时正确停在可恢复等待；有 Pluto 时按 HIL 矩阵完成 bounded run 与 stop。
- 论文源码未修改；论文建议单独记录，待实验数据齐备后实施。
