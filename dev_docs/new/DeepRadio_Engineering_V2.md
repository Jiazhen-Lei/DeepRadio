# DeepRadio 工程方案 V2

> 更新日期：2026-08-28<br>
> 当前证据：`local/agent_sessions/0827/V3/`、`local/output/0827/V3/`、`local/agent_sessions/0828/V2/plutoble/`、`local/output/0828/V2/plutoble/` 与当前工作区代码<br>
> 状态口径：实验已暴露但尚未由修改后全量回归证明关闭的问题，统一视为活动问题或待回归问题。<br>
> 约束：不替换现有框架，不引入第二套编排器，不把系统改成纯 LLM 执行。

---

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
