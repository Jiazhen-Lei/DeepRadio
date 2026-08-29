# DeepRadio 工程方案 V2

> 更新日期：2026-08-29<br>
> 当前证据：`local/agent_sessions/0827/V3/`、`local/output/0827/V3/`、`local/agent_sessions/0828/V2/plutoble/`、`local/output/0828/V2/plutoble/` 与当前工作区代码<br>
> 状态口径：实验已暴露但尚未由修改后全量回归证明关闭的问题，统一视为活动问题或待回归问题。<br>
> 约束：不替换现有框架，不引入第二套编排器，不把系统改成纯 LLM 执行。

---

## UI 目标对齐与实施记录（2026-08-29）

### 结论

目标界面以 `local/docs/jensen/273b186c647dde8b1425086f7f4724be.png` 和 `local/docs/jensen/deepradio-task-walkthrough-plan.md` 为准。此前方案只做到方向一致，尚未完全对齐：已有 SharedIntent、选择题和 Workflow Inspector，但 Radio Specification 仍是单行摘要，诊断结果没有形成可视步骤，Claims/日志/内部 ID 混在默认面板，而且旧工程的 BLE 配置会污染新诊断任务的规格视图。

本轮修订后采用以下界面结构：

```text
真实 GRC Canvas（左）        DeepRadio 对话与交互卡片（右）
                              ├── Radio Specification 可编辑表格
                              ├── Workflow Stage 框图
                              └── Diagnosis 步骤卡（仅诊断时出现）
──────────────────────────────────────────────────────────
右下角紧凑区：Task/Stage + Runtime controls + Claim summary
```

这仍是 GNU Radio Companion 内嵌界面，不改成 Web dashboard，也不把图中的 BLE 六格故事板写成固定流水线。Phase 由当前 state/checkpoint 推导；不同任务可跳过或回退阶段。

### 默认保留、条件展示与隐藏字段

| 层 | 默认展示 | 条件展示 | 仅保留的审计数据 |
|---|---|---|---|
| 意图 | 对话中的当前任务与等待原因 | 规格是否已对齐 | intent id/revision/hash、capabilities、原始 IntentIR 仅落 session，不进入用户界面 |
| Radio Specification | 在对话内显示 Goal、协议/调制、Device、Channel/Carrier、Sample rate、时长、Success condition、字段来源 | 每个可编辑字段提供候选项与自定义填写；可一次提交整个表格 | 全量 slots、规则命中过程只落 session |
| Workflow | 对话内以 Stage 框图显示 Passed/Failed/Current | 成功条件；验收条件数量只通过 tooltip 解释 | attempt、completion 明细、workflow id 仅落 session，不展示 |
| Diagnosis | 对话内显示用户请求的检查维度、Passed/Failed/Unknown、短证据、修复建议 | 只有诊断任务出现；物理连接必须是 Unknown/人工证据，不能伪造 Passed | 原始 report JSON、厂商 CLI 全输出只落 session |
| Claims | 右下角只显示 Failed/Stale/Not tested/Passed 摘要 | 用户主动展开时显示简短断言 | 完整 Evidence JSON、measurement id 不默认展示 |
| Runtime | 右下角显示 RF 状态、run id、剩余时长、Stop/Emergency Stop | 授权或运行时出现 | PID、return code、原始 `U/O` 日志只落 session，不展示 |

字段来源必须区分 `User`、`Protocol Default`、`Safety Default`、`Derived`、`Canvas` 和 `Unresolved`。默认值可以帮助补全规格，但安全时长和任务成功条件不能静默冒充用户决定。

### 数据与展示边界

保持主链不变：

```text
GUI → ServiceAgent → WorkflowEngine → StageExecutor
    → deterministic handler / LLM subagent → Completion → SharedState
```

新增纯展示投影：

```text
SharedState + Workflow digest
        ↓
workflow_presenter（无 GTK、可单测）
        ↓
Phase / Specification / Diagnosis / Claims / Runtime ViewModel
        ↓
AgentPanel 对话卡 + ClaimsPanel 紧凑运行/证据区
```

GTK 不再自行解释底层 JSON。Subagent 也不能直接写 UI；它只能返回 ResultEnvelope/工具事实，由 host 投影到 SharedState，再生成 ViewModel。

### 同步修复的底层问题

1. `SharedState.spec_digest()` 以当前 `SharedIntent.parameters` 为规格真值，并携带 `parameter_sources` 与 `radio_specification` 行；存在活动意图时不从旧 `project.config` 补入无关协议、Local Name 或设备。
2. BLE 物理部署的 Alignment Gate 除硬件和 Local Name 外，还要求用户显式确认最大时长与成功证据；Carrier、Channel 和 Sample rate 可保留协议默认并显示来源。
3. 诊断按 `diagnosis_dimensions` 或用户文本中的领域维度生成 scope。纯硬件诊断不会因为画布上恰好打开旧 BLE 工程就进入 EVM 离线诊断。
4. 硬件诊断与信号诊断分别使用 `hardware_diagnosis_report` 和 `signal_diagnosis_report`，禁止同名产物互相覆盖。
5. 最新诊断以 `intent_id + intent_revision` 绑定保存；GUI 只展示与当前意图版本一致的 findings。Unknown 是合法诊断结论但会显示 warning，不会被渲染成 Passed。
6. Claim 写入时绑定 `intent_id + intent_revision`。项目修改/重验仍可复用项目级 Claim，但纯硬件诊断等新 scope 的默认视图不会混入旧 BLE/仿真断言；完整历史仍保存在 SharedState。
7. Runtime 卡新增可执行的 `Stop` 与 `Emergency Stop`；命令直接进入 host control plane，停止后撤销 RF grant，不依赖 LLM，也不推进或伪造 Workflow 完成状态。

### 修改落点

| 文件 | 修改职责 |
|---|---|
| `grc/gui/workflow_presenter.py` | 新增纯 ViewModel：Phase、Radio Specification、Diagnosis、Claims、Runtime |
| `grc/gui/ClaimsPanel.py` | 精简为运行控制与 Claim 摘要；移除用户可见的内部 Workflow dump、PID 和原始日志 |
| `grc/gui/AgentPanel.py` | 在对话流中渲染可编辑规格表、Workflow Stage 框图和 Diagnosis 卡；移除输入框上方重复状态字 |
| `grc/agent/state/shared_state.py` | 当前意图优先的规格投影；增加版本绑定的 DiagnosisSnapshot |
| `grc/agent/state/claim_store.py` | Claim 绑定意图版本；支持当前意图视图与完整历史两种投影 |
| `grc/agent/knowledge/specs/requirements.json` | 声明式补充有界 RF 时长和成功证据要求 |
| `grc/agent/knowledge/spec_requirements.py` | 识别“值存在但仍是未确认安全默认”的字段 |
| `grc/agent/workflow/intent_alignment.py` | 处理结构化答案与来源；支持一次提交完整 Radio Specification，并对更新后的意图重新确认 |
| `grc/agent/service/stage_handlers.py` | 诊断 scope 路由、报告角色分离、Unknown quality |
| `grc/agent/service/adapter.py` | 将当前 intent revision 对应的 diagnosis 放入 workflow digest；处理 Stop/Emergency Stop host command |

### 验收口径

- 输入“我要用硬件发射一段 BLE 信号”时，先显示带未决项和来源的完整 Radio Specification，再逐项/成组补齐，不得只显示当前一个问题。
- 对齐完成前不建立可执行 Workflow；用户修改任意规格后，卡片与 intent revision 同步更新。
- 只读 PlutoSDR/B210 接入诊断只展示请求的驱动、发现、身份、probe、runtime、物理连接等步骤，不得自动插入 EVM。
- 新任务不得显示上一任务的 Radio Specification、Claims 或 RF grant；诊断快照必须匹配当前 intent revision。
- 画布被用户修改后，project version 增长，相关 Claims 变 Stale，并返回验证；重新 Passed 必须有新版本证据。
- RF proposal 和 RF authorization 是两个 checkpoint；运行卡必须提供有界时长、Stop/Emergency Stop，并把 task success 与 runtime quality 分开。
- 默认界面不出现 workflow hash、attempt/completion 明细、PID 和 `UUU`；这些信息保存在 session/state 中供离线审计，不在 GUI 保留旧 Developer Inspector。

以上实现对齐的是目标图的信息架构和交互机制，不承诺每个像素与生成图一致；最终论文截图仍需在真实 GRC 中按 `(a)`～`(f)` 六个状态逐帧人工验收。

自动验证结果（`gnuradio` Conda 环境）：agent tests `187 passed, 1 skipped`（跳过项为预期 HIL 条件），GUI tests `16 passed`；新增测试覆盖规格字段来源/未决项、Radio Specification 表格的一次性批量对齐、诊断卡 scope、模糊 BLE 对齐、新意图不继承旧 BLE 工程字段、Claim 的 intent 视图隔离，以及 GUI Emergency Stop 撤销 RF grant。GTK 像素布局、真实画布编辑回流与六帧论文截图仍属于人工 GUI 验收，不以 headless 单测替代。

## 0. 2026-08-29 增量：问题分析与当前实现

### 0.1 工程问题分析

| ID | 问题 | 根因 | 风险 |
|---|---|---|---|
| E-17 | 不完整输入在 Workflow 内才逐步暴露 | 缺少独立 Alignment Gate | Workflow 过早建立，歧义被当作执行事实 |
| E-18 | 意图未形成可共享、可版本化的单一事实源 | SharedState 只有 RadioSpec 投影，TaskCard 无 intent identity | Subagent/skill 偏离后难审计 |
| E-19 | GUI 只有通用确认/取消 | Pending 只表达 Checkpoint，缺少字段、choices 和 revision | 选择题、过期回答和意图确认无法可靠处理 |
| E-20 | 执行中改要求没有统一影响分析 | 槽位合并与 Stage 状态迁移耦合 | 旧产物/旧授权可能被错误复用 |
| E-21 | 硬件诊断结果分散 | discover/probe/运行/人工连接各自返回 | 容易将“设备可见”误报为“物理链路正确” |
| E-22 | 硬件诊断被 `current_project` 阻塞 | Task 标签规则没有区分软件与硬件诊断 | 没有 `.grc` 时无法回答设备接入问题 |

### 0.2 不改主框架的实现方案与落点

保留：

```text
GUI/API → ServiceAgent → WorkflowEngine → StageExecutor
        → deterministic handler / LLM subagent → Completion → SharedState
```

新增/修改：

| 文件 | 当前修改 |
|---|---|
| `grc/agent/state/intent_state.py` | 新增 `SharedIntent`、semantic hash、revision 和 patch history |
| `grc/agent/state/shared_state.py` | SharedState 持久化 SharedIntent；TaskCard/ResultEnvelope 绑定 intent identity |
| `grc/agent/knowledge/specs/requirements.json` | capability/protocol 驱动的字段问题、choices 和来源参考 |
| `grc/agent/knowledge/spec_requirements.py` | 加载并解析 required fields，不依赖七类测试句 |
| `grc/agent/workflow/intent_alignment.py` | Workflow 前的 IntentDraft、逐字段问答、意图确认和结构化 response |
| `grc/agent/workflow/revision.py` | 字段级 patch 影响范围、停止与重新确认条件 |
| `grc/agent/workflow/engine.py` | 确认 intent id 绑定 workflow id；硬件诊断不再强制当前工程 |
| `grc/agent/service/adapter.py` | 接入 Alignment Gate；共享 intent 给 ToolContext；活动 RF 变更先停止 |
| `grc/agent/service/stage_executor.py` | TaskCard/ResultEnvelope 携带 SharedIntent 快照与版本 |
| `grc/agent/tools/diagnosis_checks.py` | 统一、只读、证据分级的硬件/环境/runtime/RF path 诊断报告 |
| `grc/agent/service/stage_handlers.py` | 无 `.grc` 的硬件诊断直接走统一诊断；软件诊断保留原链路 |
| `grc/gui/ClaimsPanel.py` | choices、自定义输入、意图确认按钮、SharedIntent Inspector |
| `grc/gui/AgentPanel.py` | 结构化 `interaction_response` 异步提交 |

没有新增第三方依赖，因此 `environment.yml` 无需更新。

### 0.3 边界与后续工程要求

- `SharedIntent` 只能由 host coordinator 写；不要给 subagent 暴露文件写工具。
- Reference 只能描述规范、候选项和条件，不得写某个测试答案或固定 CRC/结果。
- GUI 必须通过 command API 修改状态，不能直接改 `state.json`。
- 已实现中途 patch 的停止和重新确认；更细粒度的 Stage/Artifact 失效应继续复用现有 Completion、project version 和 dependency，不新增平行状态机。
- 新硬件只应新增 `HardwareProfile`/诊断适配器；新协议只应新增 protocol reference/builder/validator；不在 Adapter 中堆设备或任务专用分支。
- 运行中的 RF 变更必须重新经过设备事实、离线校验和新的 `rf_authorization`，旧 grant 不可跨 intent revision。

## 1. 当前工程结论

0827 V3 的七类代表任务均完成用户可见主路径；0828 V2 的 PlutoSDR BLE 广播被手机 LightBlue 实际接收。当前系统已经具备可用的 GRC 建图、仿真、硬件探测、射频确认、有界运行、停止和动态状态闭环。

这些结果仍不能登记为“最新代码完整通过”，原因是实验目录没有保存 Git commit、dirty diff、环境和模型指纹；0828 的 BLE 也只覆盖一条硬件路径。当前代码修改完成后必须重新跑七类任务和 BLE。

工程上保留以下主链：

```text
GUI / API
→ ServiceAgent
→ WorkflowEngine（Workflow + checkpoint + transition）
→ IntentIR / LLM planner / Plan Compiler
→ deterministic stage handler 或 LLM subagent
→ Completion
→ SharedState / Claim / ArtifactIndex
→ GUI Inspector / session export
```

本轮方案只修正数据契约、执行语义、证据链、回复渲染和测试，不改变上述对象或调用方向。

---

## 2. 当前活动问题与不改框架的修复方案

| ID | 当前问题 | 原因 | 修复方案 | 完成标准 |
|---|---|---|---|---|
| E-01 | Session 无法证明对应的代码、环境和模型版本 | 创建会话时没有冻结运行指纹 | 在 session 根生成 `run_metadata.json`；记录 commit、dirty diff hash、Conda/Python/GNU Radio、catalog/prompt/schema hash、模型名和 RF 开关；Manifest 收录该文件 | 任意 session 可唯一定位运行版本；dirty worktree 也能区分 |
| E-02 | 无法逐轮审计 LLM 是否被调用、是否回退 | 只记录最终 `intent_classified`，没有 LLM 调用事件 | 增加 `intent_rule_seeded`、`intent_llm_started/succeeded/fallback`、`plan_llm_started/succeeded/fallback`；只落模型与请求/响应 hash、耗时和错误类型，不落密钥 | 每轮能回答“是否调用 LLM、用了什么模型、是否采用其结果” |
| E-03 | `final`、`output` 和状态中的路径不完全一致，存在绝对路径和旧工程引用 | 工具结果、工程上下文和导出采用了不同路径口径 | 写入 Workflow/State 前统一归一为 session 相对路径；外部打开工程先复制或登记为只读输入 artifact；导出 Manifest 标记 `source_path` 与 `export_path` | 移动整个 session 后仍能打开流图、图片和证据；Manifest 校验通过 |
| E-04 | `output` 是裁剪副本，却容易被当成完整实验记录；session 内出现 `__pycache__` | 导出策略和实验归档策略没有显式区分 | 增加 `export_mode=display/reproducible`；论文实验使用 `reproducible`，导出 `.grc/.py/raw data/report/evidence/metadata`；运行 Python 时禁止或清理 session 内 bytecode | `output` Manifest 明确导出模式；可复现导出不缺关键输入和中间数据 |
| E-05 | 回复会声称不存在的星座图、频谱图或报告 | 回复使用通用模板，而不是实际 ArtifactIndex | 回复只从本轮 Stage 的 ArtifactIndex、Measurement 和 Claim 渲染；缺失产物不展示；planned artifact 与 produced artifact 分开 | 回复中每个产物都能点击并通过 Manifest 校验 |
| E-06 | Stage 显示通过，但 LLM 写入的自然语言 success predicates 未真正验收 | 可执行 `completion` 与展示型 `success_predicates` 混在一起 | 保留现有 Completion 框架；只执行已注册的 predicate ID；自然语言谓词只能作为说明。Compiler 将未知谓词标为 `unbound`，不得据此判定通过 | `completed` 只由工具事实、注册谓词和用户证据决定 |
| E-07 | Measurement、图片和 Claim 的关联不完整 | 有的测量没有稳定 `measurement_id`，Evidence 未引用产物 | 所有测量先创建 MeasurementRun，再生成图和 Claim；三者共用 `measurement_id`；Claim Evidence 必须引用报告或图 | 从 Claim 可反查原始样本、测量参数、图片和工程版本 |
| E-08 | Task5 把“修改当前工程”实现成 recipe 重建，且副作用被记为 `READ` | handler 优先调用 `design_link`；effect 未从实际工具推导 | 当前工程存在时优先生成 GraphPatch；展示 diff 后走现有 checkpoint；批准后调用 `apply_flowgraph_patch`。只有用户明确重建或图不可兼容时才回退 recipe；工具 effect 对 Stage effect 取上界 | 修改前后有 diff；未涉及块保持不变；应用阶段至少为 `ARTIFACT_WRITE` |
| E-09 | Task4 诊断计划有报告目标，但没有独立报告和可验收根因 | 诊断叙述与产物/Completion 脱节 | 用现有诊断工具输出结构化 `diagnosis_report.json`：观察、假设、对照、结果、建议、不修改证明；只读对照在临时副本执行并恢复 | 报告存在；工程 hash/version 不变；建议能追溯到测量或对照 |
| E-10 | Task6 把离线工程观察标成 `realtime_observe` | “当前接收信号”缺少来源域，分类只看观察关键词 | Intent 增加 `signal_source_scope` 槽位：`current_project_offline/live_device/generated_fixture`；文本或上下文不能确定时询问，不新增 Task 类型 | UI、回复和工具路径使用同一来源域；离线结果不再声称实时 |
| E-11 | BER=0、非 DC 主峰等数值被过强解释 | 数值渲染缺少统计和测量限定 | BER 同时报比较 bit 数和置信上界；频谱报告明确 DC 是否排除、窗、FFT、分辨率和“非 DC 主峰”；不给不存在的物理含义 | 回复可以由测量报告逐字段复核，不把有限样本外推为绝对结论 |
| E-12 | “停在发射确认”和“批准发射”共用近似语义 | checkpoint 只有通用 approved/rejected | 保留 Checkpoint 类，只增加 `purpose=config_handoff/rf_authorization/ota_observation`。配置交付按钮使用“确认已保存/继续发射”，只有 `rf_authorization` 才授予 `RF_RUN` | Task7 停止点不会产生发射授权；BLE 必须有独立 RF 授权 |
| E-13 | BLE 已被手机接收，但 runtime 有 `U` underflow，Workflow 仍显示普通 passed | 业务目标、进程终态、流质量共用一个结果口径 | 保留 Workflow 状态；增加结果摘要 `quality=clean/warning/failed`。`return_code=0` 只证明进程正常停止；underflow 单独降级为 warning。优化 buffer/调度属于后续性能修复 | 手机接收成功可为 `passed_with_warning`；界面显式展示 underrun 数量 |
| E-14 | OTA Claim 为 Passed，但附件、hash 和 evidence ID 为空 | 人工按钮确认直接等价于完整 Evidence | 在现有 Evidence 上增加 `evidence_grade=human_statement/attached_capture/independent_receiver`；无附件时允许记录观察，但不得写成“证据完整” | Claim 状态和证据等级同时显示；论文 Gate 要求附件或独立接收端 |
| E-15 | 停止后仍保留 `rf_started=true`，终态含义模糊 | 同一字段同时表示“曾启动”和“当前在发射” | 不删除历史事实；改为 `rf_ever_started=true`、`rf_active=false`、`runtime.status=stopped`；兼容读取旧字段但新写入使用新语义 | 停止后 UI 不显示正在发射，历史启动事实仍可审计 |
| E-16 | GUI 规格摘要出现 `?`，失败 Claim 和 warning 不突出 | 摘要模板按固定三段拼接，Claims 只突出 Passed | 按方向、来源和协议生成角色化摘要；Inspector 增加 `quality`、Evidence grade、Failed/warning Claims；不改变 GUI 主布局 | TX、RX、Observe、BLE 摘要不出现无意义 `?`；告警无需展开 JSON 才能看到 |

---

## 3. 各代表任务的工程修复落点

| Task | 当前保留的正确结果 | 需要补齐 |
|---|---|---|
| 1 端到端仿真 | 流图、EVM、星座、频谱 | MeasurementRun 与 Claim/图片完整绑定；相对路径 |
| 2 TX 构建 | QPSK 仿真 TX、无硬件副作用 | 修复规格摘要；回复不再声称不存在图片；可复现导出 TX 数据 |
| 3 RX 构建 | 补 Eb/N0、BER 测量 | 明确 TX/AWGN 是测试夹具；BER 样本量与置信上界；回复产物事实化 |
| 4 诊断 | 只读观察和建议 | 独立诊断报告、对照证据、工程不变证明 |
| 5 修改 | 确认后得到 QPSK | GraphPatch 优先、diff、effect 修正、保留性验证 |
| 6 观察 | 频谱、星座、主峰 | 离线/实时来源域；非 DC 主峰限定；旧工程输入归档 |
| 7 硬件配置 | Pluto 发现、probe、安全预览、不发射 | 配置交付与 RF 授权分离；安全默认信号显式展示；probe warning 可见 |
| Pluto BLE | 离线 BLE 校验、探测、授权、有界发射、手机接收、停止 | underflow 警告、完整 OTA Evidence、终态 RF 字段、版本指纹 |

---

## 4. 代码修改位置

保持模块边界，只在现有职责内修改：

| 文件 | 修改内容 |
|---|---|
| `grc/agent/service/session_store.py` | `run_metadata.json`；路径归一；可复现导出；Manifest role/source/export 关系；排除 bytecode |
| `grc/agent/workflow/engine.py` | 规则 Intent 与 LLM Intent/回退事件；`signal_source_scope`；checkpoint purpose；保留用户事实 |
| `grc/agent/workflow/llm_planner.py` | planner 调用元数据与 hash；不放宽 allowed actions |
| `grc/agent/workflow/plan_compiler.py` | 未绑定谓词标记；Stage effect 不低于所用工具 effect；决策边界语义 |
| `grc/agent/workflow/completion.py` | 注册谓词验收；报告/Measurement/Evidence grade Gate；warning 与 failure 分离 |
| `grc/agent/service/stage_handlers.py` | 修改任务 GraphPatch 优先；诊断报告；观察来源路由；事实化产物集合 |
| `grc/agent/service/adapter.py` | 调用事件串联；最终 outcome/quality 汇总；回复只消费事实；不新增另一套 driver |
| `grc/agent/service/result_projector.py` | Measurement/Claim/Artifact 绑定；`rf_ever_started/rf_active`；underflow quality |
| `grc/agent/service/hardware_runtime.py` | underrun/overrun 计数与终态质量，不把 `return_code=0` 当作流质量 clean |
| `grc/agent/state/shared_state.py` | 新字段兼容读写；相对路径校验；旧 `rf_started` 迁移 |
| `grc/gui/ClaimsPanel.py` | 角色化规格摘要；warning、Failed Claim、Evidence grade |
| `grc/gui/AgentPanel.py` | 配置交付/RF 授权/OTA 三种按钮文案；Evidence 附件状态 |

---

## 5. 实施顺序

1. 先补 E-01～E-04：版本指纹、LLM trace、路径和可复现导出。这些改动风险最低，并让后续测试可审计。
2. 再补 E-05～E-07：事实驱动回复、可执行 Completion、Measurement/Evidence 绑定。
3. 修 Task4～6 的语义：诊断报告、GraphPatch 优先、离线/实时来源域。
4. 修硬件状态语义：checkpoint purpose、`quality`、Evidence grade、`rf_active`。
5. 最后改 GUI 展示，不改变核心状态真值。
6. 在 `gnuradio` 环境跑自动回归，再按测试文档重跑七类 GUI 和 Pluto BLE；产生带版本指纹的新实验目录。

---

## 6. 禁止项

- 不为七条代表文本写专用判断、固定结果、固定 CRC 或固定频谱值。
- 不用纯 LLM 替代 Policy、Completion、设备控制和停止能力。
- 不删除 Workflow、SharedState、Plan Compiler、Catalog 或 deterministic handler。
- 不用“生成了文件”代替协议、测量、硬件或空口验收。
- 不把历史 session 改写为新版本结果，不覆盖 0827 V3 和 0828 V2。

完成标志是：最新版本的七类任务回归与 Pluto BLE HIL 都产生同一套版本指纹，状态、回复、产物和人工证据能够互相反查。
