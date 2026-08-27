# DeepRadio 工程问题与修复方案 V2

> 更新日期：2026-08-27<br>
> 当前证据：`local/agent_sessions/0826/V5/`、`local/output/0826/V5/` 及当前代码<br>
> 文档原则：只保留仍存在或尚未被最新实验闭环验证的问题；已解决项只留结论，不再当作活动修复清单。

---

## 1. 当前工程结论

V5 Task7 的 PlutoSDR 连接、只读发现、精确 probe、安全预览流图生成、结构校验和 GNU Radio 编译均成功。当时失败不是“没有发现 PlutoSDR”，而是 RF 系统能力检查晚于用户确认：precheck 通过后仍开放确认，`arm_hardware_flowgraph` 才拒绝 `GRC_AGENT_ENABLE_RF`。

P0/P1/P2 与 2026-08-27 去冗余之后，handler 近拷贝和 LC 手写包装已去掉。剩下的编排成本主要是 catalog 七类片段库、`adapter`/`engine` 门面体量。功能上：

- 确认前按 effect 做系统能力门禁；配置-only 在 RF 关闭时仍可生成安全预览；
- 初始计划截断到下一决策边界；批准后重规划未执行尾部，不回写 Intent；
- IntentIR、PlanNode、Plan Compiler、可选 LLM 短期计划已接线；无模型时沿用能力片段；
- DiagnosisExperiment、GraphPatch 别名、MeasurementRun、Profile pin/snapshot、BLE 单信道能力声明已落地；
- 七类 Task 仍是评测标签和 catalog 片段库，`task_catalog.yaml` 尚未删除。

### 1.1 V5 已正确完成的部分

- `pluto_tx.grc` 是可编译的安全预览流图；
- PlutoSDR 被识别为 IIO 设备并完成 probe；
- Pluto Sink 处于禁用状态，没有发生未授权 RF 发射；
- 用户确认前没有执行 arm/start；
- RF 系统开关缺失时，底层工具最终拒绝 arm；
- Session 事件序号连续，没有发现重复事件。

### 1.2 当前自动回归状态

在 `gnuradio` Conda 环境运行：

```bash
conda run -n gnuradio python -m unittest discover -s grc.agent.tests -t .
conda run -n gnuradio python -m unittest discover -s grc.gui.tests -t .
```

2026-08-27 P1/P2 后的结果：

```text
grc/agent: Ran 156 tests, OK (1 skipped)
grc/gui:   Ran 13 tests, OK
```

此前三个 `waiting` 失败来自受限沙箱不允许 GNU Radio 在 `/var/tmp` 创建 `vmcircbuf`。相同代码在 `gnuradio` Conda 的真实权限下通过；没有伪造 metrics。跳过项来自当前 GNU Radio 构建缺少可选硬件块。

---

## 2. 已闭环项（不再进入活动清单）

旧 V2/V5 问题中，下列项已有实现和回归，不再展开问题陈述：

| 主题 | 结论 |
|---|---|
| TX 探针 `_rx.bin`、BER 契约、Eb/N0 补槽、频谱 Hz/dBFS | 已修 |
| `open_questions` 非空仍 completed、version 后 Claim 不失效 | 已修 |
| Pluto Builder 失败回退 AWGN、runtime 不统计 `U/O`、配置流图无安全预览 | 已修 |
| RF 能力检查晚于确认、restart-required blocker | P0：确认前按 `requested_effect` 门禁 |
| “停在发射确认”被展开成 RF 尾 | P0：`stop_at_decision_boundary` + deferred materialize |
| `configure_device` 部分成功与无效重试 | P0：`resume_from=arm_flowgraph` |
| Intent 与 Decision 混写 | P0：确认只追加 `decisions` / `granted_effects` |
| Manifest 被 Stage 覆盖 | P0：累积 ArtifactIndex |
| GNU Radio 环境失败当业务失败 | P0：真实 runtime，无假成功 |
| 七类 Task 硬路由 / 短视距计划 | P1 兼容迁移：catalog 作片段库；Compiler 截断与校验；无 LLM 时行为与现测试兼容 |
| 诊断无对照实验 | P2：`run_diagnosis_experiment` 单因素对照后恢复原图 |
| 改图只靠 recipe | P2 部分：GraphPatch `set_param`/`replace_block`/`connect`；新建工程仍可用 recipe |
| Profile 隐式漂移 | P2：pin 后不改 score；`profile_changed`；同轮 `profile_snapshot` |
| 图像/标量/Claim 不同源 | P2：`MeasurementRun.measurement_id` |
| BLE 扩大为三信道/sniffer | P2：声明 `ble_advertising_single_channel`；unsupported 列出三信道与独立 sniffer |
| 确定性 Stage 全堆在 adapter | P2 部分：已抽出 `stage_handlers.py`；`tools_lc` 对其余 Registry 工具自动包装 |

仍未完全闭环、需要后续核验或清理的：

| 主题 | 状态 | 判断 |
|---|---|---|
| Evidence / Manifest 原子闭环 | 部分解决 | OTA 无附件显示「人工确认、附件缺失」；V5 导出漏列仍待人工核验 |
| BLE 三信道跳频 | 未实现 | 不得扩大 Claim；实现前 Gate 5 不登记通过 |
| 调制/拓扑大改仍可能走 recipe | 部分解决 | GraphPatch 可改任意已加载图；新建/整链替换仍可用 recipe |
| 静态 catalog 与七类 compose | 兼容保留 | 删除前必须有 Session 迁移测试 |
| `adapter.py` / `engine.py` 体量 | 部分完成 | Wave 1/2 去拷贝已落地；`_fold` 与 engine 内拆仍见 §6 |

---

## 3. 仍有效的正确性约束（已实现，回归必须守住）

这些不再是待修缺陷，而是后续精简时的**禁区**：

1. `READ/ARTIFACT_WRITE/DEVICE_READ` 不因 RF 开关缺失失败；下一节点进入 `DEVICE_CONFIG/RF_RUN` 时，确认前检查；blocker 不可普通 retry。
2. 初始计划只到下一用户决策边界；批准后只重规划未执行尾部；`raw_text` 不被覆盖。
3. RF_RUN 必须有时长上限和 stop/emergency_stop；未授权不得 arm/start。
4. DiagnosisExperiment 不得 bump `flowgraph_version`、不得把对照结果写回原工程。
5. 图像、标量、Claim 必须引用同一 `measurement_id`。
6. BLE 结果只声明单信道广播能力。
7. 测试必须跑真实 GNU Radio；禁止环境失败后伪成功。

---

## 4. P1/P2 实现边界（避免误读为“已完全去 Task”）

已落地：

- IntentIR 字段（含 `entities`）、PlanNode metadata、`llm_planner.propose_plan`（未配置则为 no-op）、`plan_compiler`（未知 action 丢弃、RF bounds、`replan_tail`、`compact_workflow_payload`）；
- `task_type` 作为兼容标签选择 catalog 片段；Compiler 不发明 Registry 外能力；
- Workflow 落盘压缩 invocations；Inspector 短视距仍由 deferred + 决策截断保证。

尚未落地：

- LLM 完全生成并由 Compiler 执行任意未在 catalog/Registry 中的 PlanNode；
- 删除 `task_catalog.yaml` 与 `_compose_stages` 的七类分支；
- `engine.py` 拆成 intent / policy / repository 独立模块。

---

## 5. 编排层现状与精简方案

当前规模（2026-08-27 去冗余后）：

```text
grc/agent/service/adapter.py        2380
grc/agent/workflow/engine.py        2090
grc/agent/tests/test_workflow.py    1397
grc/agent/tools/hardware_tools.py   1096
grc/agent/tests/test_hardware.py    1023
grc/agent/tools/state_tools.py       912
grc/agent/service/stage_handlers.py  807
grc/agent/workflow/plan_compiler.py  318
grc/agent/service/result_projector.py 284
grc/agent/service/tools_lc.py        232
```

`grc/agent` 仍约 68 个文件。Wave 1 与 Wave 2 的拷贝/包装项已落地；`adapter.py` / `engine.py` 仍是门面，尚未拆 classify / persist。

### 5.1 必须保留的权威来源

| 模块 | 职责 |
|---|---|
| `planning.py` | Effect、截断、capability blocker |
| `plan_compiler.py` | schema、未知 action、RF bounds、replan、落盘压缩 |
| `completion.py` | Stage 成功谓词 |
| `claim_store.py` | Claim CRUD 与 version stale |
| `hardware_tools` + `HardwareRuntime` | RF 副作用 |
| `apply_flowgraph_patch` / `apply_grc_diff` | 多 op 原子补丁 vs 单参策略门 |
| `diagnosis_experiment` + `debug_by_metric` | 对照实验 vs 阈值叙述 |
| `task_catalog.yaml` | 片段库；Wave 3 前不删 |

`debug_by_metric` 与 DiagnosisExperiment 不是重复：一个给 verdict/叙事，一个做单因素对照。`apply_grc_diff` 与 GraphPatch 也不是重复：单参 DENY/确认策略与多 op 回滚语义不同。

### 5.2 已完成的去冗余

| 项 | 结果 |
|---|---|
| probe / start / stop handler 近拷贝 | 已合并函数体；两个 discover Stage id 保留 |
| `engine` / `plan_compiler` 两份 compact | 只留 `plan_compiler.compact_invocations` |
| LLM JSON 解析 | `llm.parse_json_object` 共用 |
| `tools_lc` 手写 `_call` 包装 | 仅保留 `design_flowgraph` / `read_metric`；其余从 Registry schema 生成 |
| `diagnose_by_metric` / `suggest_fix` | 技能与 subagent 改为 `debug_by_metric`；LC 别名删除 |
| `make_task_id` | 已删 |
| `inspect_measure` / Claim 投影 | 分别在 `stage_handlers` 与 `result_projector` |
| Stage if 链 | `_HANDLERS` 字典分派 |

### 5.3 仍待做（不改架构）

**Wave 2 剩余**

1. 确定性 handler 显式传入 host 依赖，不再把 adapter 当 `self` 走私；
2. `_fold` 抽到 reply renderer；
3. `WorkflowEngine` 内部拆 classify / compose / persist，门面保留。

**Wave 3 — 兼容退役（有迁移测试才做）**

1. catalog 改为按 capability 索引的片段；`task_type` 只做评测标签；
2. 会话迁移后再考虑合并 `discover_and_probe_*` 的 Stage id；
3. `VARIANTS` 句式表收敛为每类能力 1–2 条金标。

硬停止条件：Session reload / event replay 一致；RF 确认前门禁回归绿；BLE 单信道声明不变。

---

## 6. 剩余实施顺序

1. Wave 2 剩余：handler 去 `self` 走私、`_fold` 抽出、engine 内部分模块；
2. 人工核验 V5 Manifest 是否仍漏列设备报告；
3. Wave 3 片段化 catalog，删除七类 compose 分支（需 Session 迁移测试）；
4. 三信道跳频与独立 sniffer 仍是**新能力**，不是精简项；未实现前不得扩大 Claim。

---

## 7. 回归 Gate

1. `grc.agent.tests` 156 项（1 skip）与 `grc.gui.tests` 13 项通过；硬件 HIL 允许显式 skip；
2. 连续三次运行无 `waiting/completed` 漂移；
3. V5 同类请求在 RF 未启用时停在 capability blocker，不接受无效确认；
4. RF 已启用时，用户确认后才生成有限时长执行计划；
5. Inspector 初始只显示到下一决策点；
6. Manifest 含设备报告、验证报告和流图；OTA 无附件不得宣称 Evidence Gate 完整通过；
7. 至少一组不属于七类固定表述的开放式复合任务回归（已有 `test_plan_p12` / `test_open_compound_*`）。

只有自动回归、Dynamic State 一致性、安全 Policy、Artifact/Evidence 闭环和真实硬件人工验证共同通过，才可以登记当前工程版本 Gate 通过。
