# DeepRadio 论文架构修改建议 V3

> 日期：2026-09-01
> 状态：修改建议；本轮不修改 `local/paper/Latex_DeepRadio` 论文源码

## 1. 核心判断

论文不应删除或弱化 DeepAgent。需要修改的是论述精度：DeepAgent 不是全系统状态机、权限系统、工具执行器和验收器的合集，而是确定性边界内的自适应多智能体协调层。

当前论文设计已有正确基础：Workflow control 与 stage-level reasoning 分离；Stage 声明 eligible agents、tools、completion、dependencies 和 attempt budget；固定 verification/hardware preconditions 采用 deterministic paths。因此 V3 主要是澄清、形式化和补充实验，不是推翻论文架构。

建议统一用一句话描述：

> DeepRadio embeds adaptive DeepAgent coordination within a deterministic, evidence-grounded workflow and policy envelope.

## 2. 需要保持不变的论文主线

- Main Agent 根据当前状态选择并协调领域 Agent。
- Workflow 是 state-dependent composition，不是固定任务脚本。
- Radio Specification 是用户目标与约束的结构化共享对象。
- 跨协议、流图、运行时和硬件层的 Evidence/Claim 形成验证闭环。
- Mixed initiative 允许用户在真正有后果的边界保持控制。
- 失败可触发 Diagnosis Agent 与 Repair Agent 的跨层协作。

这些内容仍是系统创新的主体，不应改成“LLM 只做一次 Intent 分类”。

## 3. 建议新增的架构概念

### 3.1 Bounded-Hybrid DeepAgent

将 DeepAgent 的作用域明确为 Stage 内部，并增加 Stage execution mode：

```text
agentic | hybrid | deterministic | checkpoint | safety_finalizer
```

说明动态性存在于两个层面：

1. Workflow 根据 Intent、能力、当前状态和 decision boundary 动态组合 Stage。
2. Agentic/Hybrid Stage 根据证据动态选择领域 Agent、诊断维度和修复候选。

固定 CRC、GRC parse、设备 CLI、RF 启停采用确定性执行，不降低系统的动态多智能体属性；它们为 Agent 推理提供可信 observation。

### 3.2 四权分离

建议在 Design 中加入明确表格：

| Authority | Component |
|---|---|
| semantic proposal | DeepAgent / Domain Agents |
| transition and attempt control | WorkflowEngine |
| authorization and side-effect control | Policy + Execution Gateway |
| acceptance | Completion + Evidence |

强调 Agent output 是 proposal/contribution，不是 completion evidence。

### 3.3 AgentContribution 与 ExecutionReceipt

论文中的 ResultEnvelope 建议拆成概念上的两层：

- `AgentContribution`：proposal、hypothesis、agent selection、requested operations。
- `ExecutionReceipt`：实际工具、校验后参数、effect、artifact version、observation、error。

最终 StageResult 由 Host 将二者与 completion evaluation 合并。这样可以解释为什么 Agent 仍是决策主体，但不能自证成功。

### 3.4 Information-delta retry

把“有修复候选且预算允许才能 retry”强化为：下一轮 Agent 调度必须存在新的 evidence、user patch、environment change 或 repair candidate。相同 delegation/tool arguments 不重复执行。

## 4. 各章节修改建议

### Introduction

保留现有三项贡献，并把 orchestration contribution 表述为“state-bounded multi-agent orchestration”，避免给读者造成 LLM 直接操纵硬件或每个 Stage 都由 LLM 执行的印象。

建议加入问题陈述：开放式 Agent 能处理语义和跨层诊断，但无线系统的验证、设备动作和安全收尾要求确定性、版本化证据；DeepRadio 的贡献是二者的组合机制。

### Design Overview

在总体图增加：

- 外层 deterministic WorkflowEngine；
- 内层 bounded DeepAgent；
- 统一 Execution Gateway；
- Completion/Evidence feedback；
- Stage execution mode。

图中不要把 deterministic path 画成 Agent 失败后的次级 fallback；它是正常主路径的一部分。

### Dynamic Workflow and Multi-Agent Orchestration

保留 eligible agent set `A_i`、tool set `T_i`、completion `Γ_i`、dependencies `D_i`、attempt budget `m_i`，增加 mode `μ_i`：

```text
S_i = (A_i, T_i, Γ_i, D_i, m_i, μ_i)
```

Stage 接受条件建议写为：

```text
Accept(S_i) = ProtocolValid
            ∧ PolicyValid
            ∧ VersionCurrent
            ∧ ∀γ ∈ Γ_i : Resolve(γ, receipts, evidence) = true
```

### Policy and Checkpoints

增加“Task capability 不等于当前 Stage effect”。离线 Stage 不因未来包含硬件而获取 DEVICE/RF 权限。授权绑定 intent revision、device identity、参数、artifact hash 和 duration；任何语义变更撤销旧 RF grant。

### Evidence and Claims

明确自然语言 Agent response 不是 evidence source。补充工具 receipt、artifact version、measurement run 和 human evidence grade 的差异。

### Interaction Design

增加 InteractionRequest 及不变量：任何 waiting 状态必须具有非空 allowed actions。解释文本可以来自 Agent，动作 schema 必须由 Host 产生。

### Implementation

说明 Catalog 是 Stage execution mode 与 tool scope 的唯一来源；Agent Registry 和 Tool Registry 分别表达角色能力与工具效果，运行时求交集。给出 deterministic fast path、agentic diagnosis path 和 safety finalizer。

### Limitations

明确区分：软件验证、设备发现、设备探测、有限 RF runtime 和独立空口证据。没有接入真实设备时不能把 HIL-ready 报告为 RF passed。

## 5. 新的研究问题

- RQ1：Bounded-Hybrid 是否比 Fully Agentic 降低时延、无效调用和未完成交互？
- RQ2：与 Fully Deterministic 相比，DeepAgent 是否提高开放需求理解、跨层诊断和非模板修复成功率？
- RQ3：Evidence contract 与 Execution Gateway 是否减少错误完成和越权动作？
- RQ4：InteractionRequest 是否提高用户在恢复、修改和 RF 授权处的控制感与完成率？

## 6. 消融实验

| 条件 | Workflow | Stage reasoning | 固定工具/硬件 | 用途 |
|---|---|---|---|---|
| Fully Agentic | deterministic graph | all DeepAgent | Agent calls tools | 测量轮次爆炸和稳定性 |
| Fully Deterministic | deterministic graph | none | host | 测量开放任务适应性上限 |
| Bounded Hybrid | deterministic graph | selected stages | host gateway | V3 主系统 |

主要指标：任务完成率、交互完成率、端到端时延、LLM 调用数、重复委派率、无效 envelope 率、false completion、诊断准确率、修复成功率、未授权 effect、可靠停止率。

任务集应同时包含：已知 happy path、自然语言变体、缺参多轮对话、组合任务、非模板修改、跨层故障、设备缺失/错配、模型错误输出、GUI 恢复和真实 PlutoSDR 有限时长实验。

## 7. 应避免的论文表述

- “All stages are autonomously executed by DeepAgent.”
- “The LLM verifies the protocol/hardware result.”
- “A generated flowgraph proves successful RF transmission.”
- “Deterministic execution is merely a fallback when the Agent is unavailable.”
- “The seven task labels define all workflows.”

## 8. 推荐术语

- `bounded-hybrid DeepAgent architecture`
- `state-bounded orchestration`
- `evidence-grounded completion`
- `host-enforced execution gateway`
- `stage-local effect authority`
- `information-delta retry`
- `mixed-initiative recovery contract`

## 9. 修改顺序

1. 先更新 Design 的总体图和职责边界。
2. 再统一 Introduction、Abstract 和 Contributions 的一句话主张。
3. 增加 execution mode 与接受公式。
4. 更新 Implementation，确保与 V3 代码一致。
5. 完成三组消融和 PlutoSDR HIL 后再更新 Evaluation/Discussion。

在 HIL 数据和消融结果完成前，不应提前写具体性能提升百分比。本文件只规定论文修改方向，不修改或替代论文源码。
