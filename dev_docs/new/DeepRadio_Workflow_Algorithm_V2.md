# DeepRadio Workflow 算法（V3 架构基线）

> 日期：2026-09-01
> 本文虽保留兼容文件名 `V2`，内容只描述当前 V3 算法；历史增量和旧路由规则已删除。

## 1. 算法总览

```text
UserTurn
→ Turn/Intent semantic parsing
→ IntentAlignmentCoordinator
→ confirmed SharedIntent(revision, hash)
→ capability-driven Stage composition
→ deterministic Plan Compiler
→ Stage mode resolver
   ├─ agentic: bounded DeepAgent coordination
   ├─ hybrid: deterministic fast path; agentic repair on new evidence
   ├─ deterministic: host handler
   ├─ checkpoint: structured user decision
   └─ safety_finalizer: host stop/cleanup
→ Execution Gateway
→ Tool receipts / artifacts / measurements
→ deterministic Completion
→ transition or actionable waiting state
```

Task Type 是评测与 Catalog 组合标签，不是控制脚本。真实控制变量是 capabilities、slots、forbidden effects、Stage dependencies、execution mode、effect 和 evidence contract。

## 2. Intent 与对齐

LLM 负责开放语义提取，Host 负责 schema、来源、依赖和安全校验。输出至少包含：

```text
task_type, capabilities, goals, requested_operations,
desired_artifacts, evidence_requirements, constraints,
slots, slot_sources, forbidden_effects,
decision_boundaries, stop_conditions, execution_effect
```

合并优先级：

```text
用户明确事实/否定
> 安全与当前工程事实
> 已确认的 SharedIntent
> LLM 语义提取
> 协议或安全默认
```

规则解析只产生候选，不能用关键词覆盖 LLM 的整体语义；安全默认和当前工程事实不能被 LLM 改写。所有别名先归一到规范键。缺失必填字段进入 InteractionRequest，不能静默猜测。

Intent 完成后生成 `intent_id + revision + semantic_hash`。任何 Agent 结果、Artifact、Claim、Checkpoint grant 都绑定该版本。影响 RF 参数或运行语义的 patch 必须先停止 runtime、撤销旧 grant，再重新编织受影响 Stage。

## 3. Workflow 组合

Catalog 提供 Task fragments 和 Stage profiles。组合算法：

```text
fragments = select_by(capabilities, task_label, operation, direction)
stages = dependency_order(fragments)
insert alignment when required fields unresolved
insert flowgraph review before first hardware effect
append RF safety finalizer after every RF_RUN tail
truncate materialized horizon at next checkpoint
compile and validate transitions/effects/evidence
```

Compiler 只能绑定 Registry/Catalog 已知动作。LLM plan 可以提出短视野候选，但不能发明工具、提高 effect、删除依赖/completion/checkpoint/finalizer、改写已确认 Intent，或把任意自然语言声明绑定成可执行 predicate。

## 4. Stage Profile 与路由

Catalog 是 `execution_mode` 和 `allowed_tools` 的唯一来源。Stage schema：

```text
id, objective, interaction, execution_mode,
recommended_agents, allowed_tools,
effect_level, depends_on, completion,
attempt, max_attempts, transitions,
idempotent, safety_finalizer
```

路由算法：

```text
if interaction is checkpoint:
    wait for structured decision
elif mode in deterministic/safety_finalizer:
    host_handler()
elif mode == hybrid and no prior failure evidence:
    host_handler()                  # fast path
else:
    bounded_deepagent()
    if unavailable: safe deterministic fallback or waiting
```

实验 override 不能把硬件、Checkpoint 或 finalizer 交给 Agent。

## 5. 有界 DeepAgent

Main Agent 在当前 Stage 的 `recommended_agents` 中选择最小集合。领域 Agent 的完整工具能力与 Stage `allowed_tools` 求交集后绑定。

TaskCard 包含：

```text
workflow/stage/intent identity and revision,
raw instruction and current turn,
slots + sources + forbidden capabilities,
stage mode + allowed tools,
completion status,
prior results + last failure,
claim snapshot
```

继续下一轮必须满足 `information_delta = true`：出现新工具证据、用户修正、环境变化或新的修复候选。相同 `delegation_key` 不重复委派；幂等工具以参数 hash 缓存。

Main Agent、Subagent model calls、Subagent tool calls、输出 token 和 Stage attempts 分开限制。预算耗尽不算完成，转入 recovery。

## 6. Execution Gateway 算法

```text
authorize(stage, tool, arguments):
    require tool in Registry
    require tool in stage.allowed_tools
    require rank(tool.effect) <= rank(stage.effect)
    require all tool.requires
    require policy grant bound to current revision/plan when consequential
    validate argument schema
    return ALLOW or DENY(reason)
```

Agent 和确定性 handler 必须调用同一 Gateway。未知 requirement fail closed。RF stop/emergency stop 是始终可达的宿主控制，但仍记录 receipt。

## 7. Completion

```text
protocol_ok = every agent invocation has a valid, correctly bound envelope
completion_ok = every registered predicate resolves true from current evidence
stage_ok = reply_ok and protocol_ok and completion_ok
```

确定性 handler 由 Host 生成执行 receipt；Agent 路径必须保留真实 invocation。无效 envelope 不能因 completion 成立而降级为 warning。

Predicate resolver 只读工具结果、Artifact、MeasurementRun、Claim/Evidence、Project/Intent version 和 checkpoint decision。自然语言 narrative 永不作为完成证据。

Checkpoint predicate 在用户决定时写入批准/拒绝、run id、evidence id/hash、时间与 purpose，不能使用通用 `True` 占位模拟“已记录”。

## 8. Transition、Retry 与恢复

```text
passed → declared success edge
failed + new repair candidate + budget → local retry/repair edge
failed without delta → waiting recovery
external precondition missing → waiting capability/recovery
errored → safe stop or recovery
```

Retry 只检查当前 Stage 的 effect：当前 Stage 达到 `DEVICE_READ` 才重新发现/探测硬件。完整 Workflow 以后要使用 PlutoSDR，不得污染当前 offline verification。

等待态投影为 `InteractionRequest(id, kind, status, reason, allowed_actions)`。不变量是 `waiting ⇒ allowed_actions != []`。恢复对象由 Workflow 状态重建，不依赖聊天历史里的 action 字段。

## 9. Effect 与 RF 安全

```text
READ < ARTIFACT_WRITE < DEVICE_READ < DEVICE_CONFIG < RF_RUN
```

Stage effect 至少等于其 allowed tools 的最高 effect。RF_RUN 前必须存在 Host readiness、真实 discovery、exact probe、当前 artifact 的 offline verification、明确且绑定的 user grant 和 duration bound。TX flowgraph 还必须先 arm。任何非 finalizer RF_RUN 后必须可达 stop finalizer。

## 10. Pluto BLE 算法

```text
build PDU → generate BLE 1M waveform → build disabled Pluto preview
→ verify packet bits/CRC/whitening → validate GRC
→ user reviews flowgraph
→ host preflight → discover Pluto → exact IIO probe
→ user grants bound RF plan
→ arm sink → grcc compile → bounded start
→ collect OTA evidence → stop/finalize
```

设备未连接时正确结果是 `waiting(device_not_found)`。软件测试只能证明 builder、validator、compiler 和门禁逻辑；实机 RF passed 必须有设备和空口证据。

## 11. 可审计事件

至少记录：User turn、Intent draft/confirm/patch、Workflow compiled、Stage routed/started/completed、Agent delegated、Tool authorized/denied/called、Artifact versioned、Completion evaluated、Checkpoint requested/resolved、Retry preflight、Runtime started/stopped、Transition。

每项包含 Workflow/Stage/Intent revision、executor mode、耗时和相关 hash；用户 UI 仅显示清晰摘要。

## 12. 算法验收

- Catalog 中每个 Stage 都有 profile，所有 tool 名存在于 Registry。
- Stage effect 不低于 allowed tools 的 effect。
- Checkpoint/RF tail/finalizer 的成功路径无环且 finalizer 可达。
- Fully Agentic、Fully Deterministic、Bounded Hybrid 三种实验模式可复现。
- Invalid envelope、stale evidence、tool scope violation、missing grant 均 fail closed。
- 相同输入的 deterministic Stage 结果可复现；Agent retry 具有 evidence delta。
