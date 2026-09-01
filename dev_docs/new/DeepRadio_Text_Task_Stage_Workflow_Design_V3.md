# DeepRadio Bounded-Hybrid DeepAgent Workflow Design V3

> 状态：当前唯一架构基线
> 日期：2026-09-01
> 范围：文本交互、Intent、Task、Workflow、Stage、DeepAgent、Tool、Policy、Evidence、GUI 与 PlutoSDR 执行闭环

## 1. 设计结论

V3 保留 DeepAgent 作为 DeepRadio 的核心多智能体架构，不把系统退化为固定脚本。V3 修正的是 DeepAgent 的权力边界和调用时机：

> DeepAgent 负责有歧义、有选择、有跨层推理价值的阶段内协调；确定性 Workflow、Execution Gateway、Policy、Checkpoint 和 Evidence Completion 负责约束、执行与验收。

V2 实测问题来自实现把“动态阶段内协调”扩张成“所有 Stage 默认由 LLM 循环执行”。协议位级验证、硬件探测、设备配置、RF 启停和恢复交互因此承担了不必要的模型往返，并出现重复委派、非法 envelope、假完成、全局硬件能力污染离线重试等问题。V3 不改变论文主心骨，而是让实现回到论文已经表达的“deterministic workflow control + bounded stage-level reasoning”。

## 2. 目标与非目标

目标：

- 保留 Main Agent 与领域 Agent 的动态组合能力。
- 固定操作快速、可复现、可审计。
- LLM 不能绕过权限、声明工具已执行或自证 Stage 通过。
- 每个等待态都有用户可以完成的结构化动作。
- 局部失败只重试局部 Stage，不重新运行无关阶段。
- PlutoSDR 从离线构建到有限时长 RF 的每个安全前置条件均可追溯。
- 删除重复工具路由表、失效兼容分支和永真验收逻辑，控制代码规模。

非目标：

- 不让 LLM 直接控制 Workflow 状态机。
- 不用固定七条任务脚本替代开放文本理解。
- 不把“生成了文件”视为协议、测量、设备或空口验收通过。
- 不把模拟通过表述为真实 PlutoSDR 发射通过。

## 3. 双环架构

```text
User / GUI
   │ text or structured command
   ▼
Intent Alignment ──► confirmed SharedIntent revision
   │
   ▼
Deterministic outer loop: WorkflowEngine
compose Stage → transition → attempt budget → checkpoint → acceptance
   │
   ├─ agentic / hybrid reasoning ─► MainAgent ─► eligible Domain Agents
   │                                      │ proposals / hypotheses
   │                                      ▼
   └─ deterministic execution ─────► Execution Gateway ─► Tool Registry
                                           │ receipts
                                           ▼
                               Evidence + Completion Evaluator
```

外环拥有控制权，内环拥有推理权，Gateway 拥有执行权，Completion 拥有验收权。

## 4. 单一事实源

| 对象 | 唯一写者 | 作用 |
|---|---|---|
| `SharedIntent` | `IntentAlignmentCoordinator` | 用户目标、约束、槽位、来源与 revision |
| `Workflow` / `Stage` | `WorkflowEngine` | 阶段图、状态、尝试次数、Checkpoint、转移 |
| `StageProfile` | Task Catalog | Stage 执行模式与允许工具范围 |
| `ToolSpec` | Tool Registry | 工具参数、effect、幂等性与前置条件 |
| `ExecutionReceipt` | Execution Gateway / Tool | 实际操作、结果、版本、时间与错误 |
| `Claim` / `Evidence` | Host projector | 版本化工程事实和验收证据 |
| GUI view model | Presenter | 只读投影，不反写 JSON |

Agent Registry 只描述领域角色及其能力；Stage Profile 只描述当前阶段允许的范围；Tool Registry 只描述工具自身效果和前置条件。运行时取三者交集，不再维护多份相同的 Stage→Tool 表。

## 5. Stage 执行模式

`Stage.execution_mode` 只能是：

- `agentic`：需要开放语义、跨层诊断或方案权衡。
- `hybrid`：首次使用确定性 fast path；已有失败证据时允许 DeepAgent 进行一次有界协调。
- `deterministic`：固定算法、验证、文件事务或硬件操作。
- `checkpoint`：只等待结构化用户决定。
- `safety_finalizer`：必须可达的停止/清理路径，永不交给 LLM。

推荐映射：

| Stage 类型 | 模式 | DeepAgent 价值 |
|---|---|---|
| 规格消歧 | Agentic 或 Checkpoint | 解释目标、提出问题；Host 校验答案 |
| 通用建图/修改 | Hybrid | 首次快速执行，失败后跨层选择修复 |
| BLE PDU/波形/流图构建 | Hybrid | 已绑定参数走确定性构建，异常时协调诊断 |
| 离线协议与结构验证 | Deterministic | 固定位级算法与解析器，无需模型循环 |
| 观察与测量 | Deterministic | 指标来自实际运行和测量工具 |
| 诊断、修改计划 | Agentic | 汇总证据、排序原因、提出候选变化 |
| 设备发现/探测/配置 | Deterministic | 厂商 CLI 与设备事实必须可复现 |
| RF 授权、空口观察 | Checkpoint | 只接受结构化用户决定和证据 |
| RF 启动/查询/停止 | Deterministic / Safety finalizer | 受控子进程、时长上限、可靠停止 |

环境变量只用于实验消融：`deterministic` 可关闭 Agent；`deepagents` 可让 agentic/hybrid 使用 Agent，但不能把 checkpoint、硬件或 safety finalizer 改成 Agent 控制。

## 6. Main Agent 与领域 Agent

Main Agent 只负责：

- 读取不可修改的 TaskCard。
- 在 `recommended_agents` 中选择能覆盖目标的最小集合。
- 汇总领域 Agent 的候选、冲突和未知项。
- 在出现新证据时提出下一项最小动作。
- 生成用户可理解的解释。

领域 Agent 只负责一个领域：Spec、Radio Design、Flowgraph、Verification、Diagnosis、Protocol、Hardware。它们不能直接推进 Workflow、批准 effect 或把自然语言当证据。

每次委派必须绑定：

```text
task_id, workflow_id, stage_id, workflow_revision,
intent_id, intent_revision, intent_hash,
expected_results, allowed_tools, completion_status
```

非法、缺失或错绑的 ResultEnvelope 一律不参与 Stage 通过判定。工具已经执行成功也不能修复无效的 Agent 协议结果；Host 可以保存工具回执供恢复，但本次 Agent 委派仍判失败。

## 7. Execution Gateway

所有 Agent 路径和确定性路径共用一个工具执行入口：

```text
tool exists
→ tool ∈ Stage.allowed_tools
→ Tool.effect ≤ Stage.effect_level
→ Tool.requires 全部满足
→ Policy / checkpoint grant 满足
→ 参数 schema 校验
→ execute
→ immutable receipt + event
```

未知前置条件必须 fail closed。工具内部继续保留硬件底线检查，形成纵深防御；内部检查不是第二套 Policy。

职责边界：

| 问题 | 所有者 |
|---|---|
| 建议做什么 | LLM / DeepAgent |
| 当前 Stage 允许哪些动作 | Catalog |
| 某工具自身会产生什么效果 | ToolSpec |
| 当前是否授权 | Policy / Checkpoint |
| 如何执行 | Tool implementation |
| 是否真的完成 | Completion / Evidence |

## 8. Completion 与证据

Stage 通过条件：

```text
reply_status_ok
AND execution_protocol_ok
AND every registered completion predicate is true
AND evidence belongs to current intent/workflow/project version
```

Completion 只读取：

- 工具回执；
- 当前 artifact 及其 hash/version；
- 当前测量 run；
- 当前 Claim/Evidence；
- 结构化 checkpoint decision。

禁止读取 Agent 叙述中的“已验证”“已完成”。Checkpoint predicate 由 `_checkpoint_result` 记录真实 decision，不使用通用 `True` 占位。Outcome、quality 与 evidence grade 分开：目标通过不代表运行 clean，人工空口陈述也不等同仪器证据。

## 9. 有界 Agent 循环与性能

每个 Agent Stage 必须有：

- Main Agent model-call budget；
- Subagent model/tool-call budget；
- 单次输出 token 上限；
- `delegation_key` 去重；
- 相同幂等工具调用缓存；
- 无效 envelope 最多一次格式修复；
- 无新 evidence delta 时禁止下一轮；
- 超限后回到确定性 fallback 或用户恢复点。

Hybrid 首轮 fast path 避免“Main Agent 调度一次、Subagent 再思考一次、每个固定工具后又思考一次”。DeepAgent 的计算预算集中用于歧义、诊断和修复，而不是 CRC、GRC parse、设备扫描或 stop。

## 10. InteractionRequest

等待态必须具有稳定、结构化、可恢复的交互对象：

```json
{
  "id": "workflow:stage:revision:kind",
  "kind": "approval|input|recovery|capability",
  "status": "pending",
  "reason": "...",
  "allowed_actions": ["retry_stage", "cancel_workflow"]
}
```

强不变量：

```text
Workflow.waiting
⇒ pending InteractionRequest exists
⇒ allowed_actions is non-empty
⇒ Presenter renders at least one executable action
```

GUI 发送结构化命令，不解析自由文本按钮语义。LLM 可以解释原因，但不能生成授权 token 或直接修改状态文件。

## 11. 局部 Effect 与 Retry

Task capability 描述完整目标；Stage effect 描述当前操作。二者不能混用。

- 离线验证 Stage 即使属于最终含硬件的 Workflow，也仍是 `READ/ARTIFACT_WRITE`。
- 只有当前 Stage effect 达到 `DEVICE_READ`，retry 才重新扫描 SDR。
- 只有当前 Stage effect 达到 `DEVICE_CONFIG/RF_RUN`，Gateway 才检查相应 grant。
- Retry 只恢复当前失败 Stage；已经通过且版本未失效的 Stage 不重跑。
- 再次尝试必须具有新证据、用户修正、环境变化或修复候选。

## 12. PlutoSDR BLE 闭环

```text
confirmed BLE/Pluto intent
→ build_ble_advertiser [hybrid fast path]
   PDU → waveform → disabled Pluto sink preview → saved .grc
→ offline_protocol_verify [deterministic]
   packet bits/CRC/whitening + GRC structural validation
→ flowgraph_confirmation [checkpoint; no RF grant]
→ hardware_precheck [deterministic DEVICE_READ]
→ discover_and_probe_hardware [deterministic DEVICE_READ]
→ rf_plan_confirmation [checkpoint; bound RF_RUN grant]
→ configure_device [deterministic DEVICE_CONFIG; arm only]
→ transmit_bounded [deterministic RF_RUN; duration cap]
→ over_air_verification [human/instrument evidence checkpoint]
→ stop_and_finalize [safety_finalizer]
```

任何一步失败都不得越过后续门。没有真实设备时，系统应停在可重试的 device discovery/probe，而不是声称 HIL 通过。

## 13. 状态、事件与可观测性

必须记录：Intent revision、Stage mode、实际 executor、tool effect、前置条件判定、调用耗时、artifact hash、completion map、transition、checkpoint decision、runtime run id 和 stop reason。

GUI 默认只展示用户能理解的阶段、结果、风险和可执行动作；内部 ID、JSON 和工具日志保留在 Inspector/session export。Stage 开始、完成、失败和等待时通过 progress channel 刷新，不等待整轮 LLM 返回。

## 14. 失败处理

| 失败 | 行为 |
|---|---|
| LLM 不可用 | Agentic Stage 使用确定性 fallback；无法安全替代则等待 |
| 无效 envelope | 一次格式修复；仍无效则 Stage failed/recovery |
| 固定工具失败 | 保存 receipt；不让 LLM 重复盲调 |
| Completion 缺失 | 仅重跑缺失 producer；不得口头补齐 |
| 设备未发现 | waiting + Retry/Cancel；retry 时仅硬件 Stage 重扫 |
| RF 启动失败 | fail closed，并保持 Stop/Emergency Stop 可用 |
| GUI 恢复 | 从 Workflow/InteractionRequest 重建，不依赖旧聊天文本 |

## 15. 验收标准

架构验收：

- Catalog 是 Stage mode 和 allowed tools 的唯一来源。
- Main/Domain Agent 仅看到 Catalog 与自身能力的交集。
- 固定验证、硬件和安全 Stage 不调用 LLM。
- 无效 envelope 永不通过。
- 任意 waiting digest 均投影为可操作 UI。
- 离线 Stage retry 不探测硬件。
- Tool effect 超出 Stage ceiling 时统一拒绝。

软件链路验收：

- GNU Radio 环境全部单测通过。
- Pluto BLE PDU、波形和 `.grc` 生成通过。
- `.grc` 能由 gnuradio 环境的 `grcc` 编译。
- RF disabled 时无法 arm/start；无设备时停在 discovery/probe。
- 带匹配设备、显式 RF grant 和时长上限时才可启动，并必达 stop finalizer。

实机验收必须记录设备 identity、驱动输出、编译 Python、run id、启动健康、有限时长停止结果和独立接收证据。没有这些证据只能标记“软件/HIL-ready”，不能标记“PlutoSDR RF passed”。

## 16. V3 最终原则

DeepAgent 不是被删除，而是被放回最有研究价值的位置：在可验证边界内完成自适应的多智能体理解、诊断和修复。确定性代码不是 DeepAgent 的竞争者，而是它的执行与证据基础。V3 的创新表述是：

> state-bounded, evidence-grounded multi-agent orchestration for mixed-initiative radio engineering.
