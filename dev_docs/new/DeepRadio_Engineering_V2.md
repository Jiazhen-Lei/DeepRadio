# DeepRadio 工程方案 V2

> 更新日期：2026-08-31<br>
> 当前证据：当前工作区代码与全量自动回归（agent tests `250 passed, 1 skipped`；GUI tests `20 passed`）、2026-08-30 PlutoSDR 真机冒烟；`local/agent_sessions/0827|0828` 历史目录仅作版本基线<br>
> 状态口径：实验已暴露但尚未由修改后全量回归证明关闭的问题，统一视为活动问题或待回归问题。<br>
> 约束：不替换现有框架，不引入第二套编排器，不把系统改成纯 LLM 执行。

---

## 2026-08-31 V6 增量：泛化链路修复

本次修改保持 `Alignment → Intent → Plan → Workflow → Evidence → UI` 架构不变，收紧各边界：

1. `WorkflowEngine` 的活动轮次 LLM 一次返回 relation 与 `intent_patch`；`ServiceAgent` 不再把同一 alignment confirmation 二次送入 Workflow。初始 idle intent 仍进入 Alignment，已确认/已完成状态的新目标进入 Workflow。
2. `plan_needs_proposal` 先比较 IntentIR 的具体 operation/artifact 与既有 Stage evidence coverage；无 gap 不调用 planner。proposal 对已有 Stage 只可更新 objective/unbound metadata，不可覆盖 requires/produces/completion。
3. Catalog 的设备链统一为 `hardware_precheck → discover_and_probe_hardware → checkpoint → configure/runtime`。Compiler 对 DEVICE_CONFIG/RF_RUN 执行 evidence/effect/finalizer 不变量校验。
4. `hardware_preflight` 输出 `readiness_scope=host_environment` 与 `physical_device_checked=false`。`result_projector` 只有在 discover 与 probe 同轮成功后写入 physical device claim；新探测失败会移除 stale observed device。
5. GTK 默认界面只渲染 `workflow_presenter.present()` 生成的英文 label/message。Claims 默认详情不再展开 raw evidence JSON；规格卡不显示 revision/status 日志标题；回复不再附加 `Completed:` 工具事件列表。

自动回归以无硬件/无完整 GNU Radio block library 的当前环境执行：Plan/Workflow 契约测试通过；需要完整 GNU Radio runtime、GTK `gi` 或 HIL 的套件按环境条件单独验收。

## 2026-08-30 V5 增量：硬件意图对齐与探测链路修复（P0～P2 已实施）

### V5 实测暴露的问题

V5 会话（用户输入 "I want to use plutosdr to transmit ble signal."，PlutoSDR 实际接入 USB）暴露三个链路缺陷：

1. 意图 LLM 用 `device` 键返回设备型号，系统只认 `hardware` 槽位：规格卡出现 `hardware: pluto (rules)` 与 `device: plutosdr (llm)` 两行 Device，missing 判定仍认为 hardware 缺失，第一轮重复追问设备。
2. 重试预检 `_refresh_hardware_for_retry` 未把 intent 的 hardware 槽位传给 `discover_devices`，空参落入工具 `b210` 默认值：对 PlutoSDR 连续执行 3 次 `uhd_find_devices`（UHD 后端）全部失败，而 `iio_info -S usb` 本可一次发现设备。
3. 失败提示只有通用"未检测到设备"，没有期望/发现的矛盾解释，用户只能盲重试。

### 修复内容

| ID | 问题 | 修复 | 落点 |
|---|---|---|---|
| P0-1 | 探测未收到意图设备选择 | 重试预检把 `intent.slots["hardware"]` 传入 `discover_devices`；工具的 `b210` 默认值不得屏蔽用户选择 | `grc/agent/service/adapter.py` `_refresh_hardware_for_retry` |
| P0-2 | LLM 返回 `device` 等同义键不被识别；LLM 漏提取时用户已陈述的事实被丢弃 | 意图 prompt 增加规范键名规则（设备型号必须写 `hardware`，禁止 `device/sdr/radio` 同义键）；`_merge` 将别名键自动归一到 `hardware`；新增**字面证据种子兜底**：LLM 漏提取时，仅当规则种子值字面出现在用户原文（如 `pluto` ∈ "use plutosdr…"）才回填，LLM 返回键始终优先；自由文本键（local_name/payload）与安全键（operation/deploy_permission/duration）永不回填 | `grc/agent/workflow/engine.py`（`_PROMPT`、`_merge`、`_SEED_FALLBACK_KEYS`） |
| P1-3 | 规格卡双 Device 行 | 渲染前按别名表归并，同一事实只渲染一行、一个来源 | `grc/agent/knowledge/spec_requirements.py` |
| P1-4 | 期望设备未找到时同命令盲重试 | 未找到时只读扫描其它驱动家族（`iio_info -S usb`、`hackrf_info`、`LimeUtil --find` 等）；发现设备则返回 `devices` + `mismatch_hint`（如"期望 B210 但发现 PlutoSDR，请确认设备选择或物理连接"）；期望家族命中时零额外开销 | `grc/agent/tools/hardware_tools.py`（`_scan_other_families`）、`grc/agent/tools/hardware_profiles.py`（`iter_profiles()`） |
| P2-5 | 连续失败无矛盾解释 | 失败计数 ≥2 触发 LLM 咨询诊断 `_diagnose_hardware_mismatch`：期望类型/命令/输出/发现设备交给 LLM，输出 `Diagnosis: 原因 | 建议` 附加到重试提示；LLM 不可用时静默降级；发现设备后计数清零 | `grc/agent/service/adapter.py` |

设计红线：

- 种子兜底不升格 source（保持 `rules`），并保住 V4 回归契约——"LLM 漏提取时不得把无文本证据的 regex 猜测升格为用户事实"（`test_llm_omission_drops_nonconflicting_rule_candidates` 继续通过）。
- LLM 诊断纯咨询性，不改变确定性重试循环、Policy 与安全门。
- 跨家族扫描是只读探测，不配置、不启动任何设备。

### 回归与冒烟

- agent tests：`234 passed, 1 skipped`（HIL 条件跳过），含本轮新增 8 项契约测试。
- 真机冒烟（2026-08-30）：`discover_devices(device_type="plutosdr")` → 执行 `iio_info -S usb` → `device_found=True, identity=usb:2.4.5`，即 V5 会话中 3 次失败的场景现在一次命中。

---

## 自适应 Radio Specification 与常驻 Workflow Monitor 方案（2026-08-29，已实施）

### 实施状态（2026-08-30）

本节 P0～P2 已按不替换现有 WorkflowEngine 的方式实现。主要落点为：

- `grc/agent/knowledge/specs/profiles.json` 与 `spec_requirements.py`：组合 general、TX/RX、BLE、Wi-Fi、diagnose profile，分离 required、mentioned、optional 和 derived。
- `intent_state.py`、`shared_state.py`：Radio Specification 已纳入 SharedIntent 权威状态，并原子投影为 session 下的 `radio_specification.json`。
- `intent_alignment.py`：支持一轮多字段自然语言补齐、部分回答后继续追问、默认值显式确认、可选字段介绍和最终 confirm。
- `AgentPanel.py`：对话中只读、可折叠、分组与分色的 Radio Specification；旧 ComboBox/Entry 写规格入口已删除。
- `ClaimsPanel.py` 与 `workflow_presenter.py`：底部常驻 Workflow Monitor，Stage 纵向 stepper 内嵌 Claim，并显示最近正向/回退跳转。
- `adapter.py`：增加只读 progress channel，只推送意图 revision、Stage、checkpoint 和 Workflow 状态事件，不把 tool/routing log 涌入 UI；GUI 可实时刷新，但不获得推进状态机的写权。
- BLE Local/Advertising Name 已改为真正的可选字段；未指定名称也能构建、生成和校验合法广播，空口验收回退为通用"观察到目标信号"。

自动回归结果：GNU Radio `gnuradio` conda 环境下 agent tests `234 passed, 1 skipped`（HIL 条件跳过），GUI tests `17 passed`（本轮 V5 未改 GUI，沿用基线）。GNU Radio 会在 `/var/tmp` 创建双映射缓冲区，因此全量仿真回归必须在允许该路径的环境运行。

此前 0829～0830 的 E-01～E-22 系列修复（版本指纹 `run_metadata.json`、LLM 调用 trace、session 相对路径与可复现导出、事实化回复、GraphPatch 优先、`signal_source_scope` 来源域、checkpoint purpose 分离、outcome/quality/evidence_grade 三维结果、`rf_ever_started/rf_active` 终态语义、角色化规格摘要、SharedIntent 单一事实源、泛化硬件诊断）均已落地并由既有回归覆盖，对应"待修复方案"表格已从本文档移除。

### 改造前问题与根因（已关闭）

本节取代此前"在 Radio Specification 表格内用下拉框一次性填写"和"Workflow 只作为对话卡片显示"的 UI 方案。目标仍以 `local/docs/jensen/273b186c647dde8b1425086f7f4724be.png` 与 `local/docs/jensen/deepradio-task-walkthrough-plan.md` 为信息架构参考，但图中的 PlutoSDR BLE 只是一个 state-dependent workflow instance，不能反向成为所有任务的固定模板。

| 问题 | 直接原因 | 架构根因 | 影响 |
|---|---|---|---|
| BLE 规格总出现 Advertising name 等字段 | `SharedState.spec_digest()` 根据 `protocol == ble` 硬编码展示 Channel、Advertising name 等行 | "展示字段""必填字段""协议可选字段"没有独立模型，示例字段进入了通用投影 | 无关字段污染任务，扩展 Wi-Fi/通用信号时会继续堆条件分支 |
| 必填和可选字段混在一起 | `requirements.json` 只有少量 required rule，UI 又把已有默认值和 choices 全部当作可编辑项 | 缺少 required/mentioned/optional/derived 四类字段语义 | 用户不知道哪些信息阻塞 Workflow，默认值也可能被误当成用户决定 |
| 交互像填写配置表，不像和 Agent 协作 | GUI 的 ComboBox/Entry 直接提交 `specification_update` | 意图补齐同时由 GUI 控件和 Alignment Coordinator 驱动，存在两套交互入口 | 不利于研究自然语言渐进对齐，也无法检验 LLM 对补充文本的理解 |
| Radio Specification 没有稳定文件身份 | `RadioSpec`、`SharedIntent.parameters` 与 GUI digest 存在重叠 | 规格只是 SharedIntent 的投影，但尚未显式定义唯一真值源和持久化契约 | 修改、回退、跨 Agent 共享时容易产生两个版本 |
| Workflow 到任务结束后才明显出现 | 对话卡只在完整 `AgentReply` 返回时刷新；后台 Stage 执行期间没有 progress event | UI 订阅的是"回复完成"，不是"Workflow 状态变化" | 用户看不到实时运行、跳转、失败回退和等待确认 |
| Stage 框图不容易理解跳转 | 窄侧栏内使用 `FlowBox` 平铺 Stage，仅靠图标表达状态 | 展示模型缺少 transition/current/previous/retry/back-edge 语义 | 回退和重试看起来像静态列表 |
| 对话文字不能可靠随宽度换行 | `_FlowLabel` 只约束普通气泡；规格卡、Grid、badge、Stage label 使用普通 `Gtk.Label`，部分控件会撑大自然宽度；已分配宽度还可能残留旧值 | 缺少统一的 responsive label/card 组件 | 缩窄侧栏后出现截断、横向撑宽或不重新排版 |

### 1. Radio Specification 字段选择规则

规格默认只展示两类字段：

1. **Required fields**：完成当前请求的下一项可执行产物或物理操作所必需的字段。
2. **Mentioned fields**：用户文本明确提到的字段，即使它不是必填字段，也必须回显，防止系统忽略用户约束。

Optional fields 不默认占据表格；用户通过自然语言提出、点击"可选字段"提示，或者要求教学介绍后才加入。Derived fields 仅在其对建图、验证或执行有实际影响时展示，并标记来源，不把所有协议常量都塞进规格表。

```text
visible_fields = required_fields
               ∪ user_mentioned_fields
               ∪ user_added_optional_fields
               ∪ execution_relevant_derived_fields
```

字段应包含以下元数据，而不能只保存 key/value：

```json
{
  "key": "carrier_frequency",
  "value": 2402000000.0,
  "requirement": "required",
  "source": "protocol_default",
  "locked": true,
  "confirmed": false,
  "reason": "BLE advertising channel 37 determines the carrier",
  "depends_on": ["protocol", "advertising_channels"]
}
```

- `requirement`：`required | mentioned | optional_added | derived`。
- `source`：`user | protocol_default | safety_default | derived | canvas | unresolved`。
- `locked` 只表示不能在表格中直接编辑，不表示永远不可改变。用户仍可在对话中修改上游要求；例如修改 BLE Channel 后重新推导 Carrier。
- Required 字段如果使用普通协议默认值，可以展示建议；涉及设备身份、物理 RF 授权、安全时长和成功证据的值不得静默确认。
- Local/Advertising name 仅在用户提到名称、选择 BLE advertising payload，或成功条件要求接收端按名称观察时出现；它不是所有 BLE 发射任务的必填项。
- 同一事实经别名键进入时（如 LLM 的 `device` 与规则的 `hardware`）必须先归并再渲染：一行、一个来源，禁止重复行（V5 P1-3 契约）。

### 2. 对话式渐进对齐协议

Radio Specification 是**只读的当前解释投影**，不再在表格单元格中放 ComboBox/Entry。所有修改通过自然语言进入同一个 Alignment Coordinator，避免 GUI 与 Agent 各写一套状态。

```text
用户 text1
  ↓ LLM 提取用户明确字段与候选任务方向
TemplateResolver 组合模板并计算 required_fields
  ↓
显示折叠式 Radio Specification（已有值 + Unresolved 必填项）
  ↓
ask_user_question：一次询问本轮全部缺失必填项，并给建议与理由
  ↓
用户 text2
  ↓ LLM 做增量字段提取；Host 做类型、依赖和硬件能力校验
仍缺字段 ──是──→ 更新表格并继续追问剩余字段
  │否
  ↓
显示完整 Radio Specification
  ↓
confirm：确认 / 继续修改 / 添加可选字段 / 介绍这些参数
  ↓确认
建立 Workflow
```

`ask_user_question` 可以在一条消息中询问多个缺失字段，但回复解析必须允许用户只回答其中一部分。每轮只合并能可靠识别的字段，未回答字段继续保留 `Unresolved`，不得为了尽快建立 Workflow 而猜测用户答案。追问前提是"用户确实没有陈述过该事实"：已在原文中字面陈述的硬件/协议等结构化事实，即使 LLM 漏提取也不得重新追问（V5 P0-2 种子兜底契约）。

Open Questions 分两类：

- `blocking_questions`：缺失 required 字段或验证冲突；未解决时不能建立 Workflow。
- `optional_prompts`：是否添加可选字段、是否需要参数教学；不影响规格完整性，在最终 confirm 中提供入口，不新增 Workflow 阻塞状态。

教学模式只解释字段含义、推荐范围和取舍，不替用户确认字段，也不能将 Claim 或 Completion 改成 Passed。

### 3. Template 设计：组合模板，不复制固定任务表

不建议维护"BLE 发射完整模板""Wi-Fi 发射完整模板"两份互相独立的大表。应采用声明式组合：

```text
General base
  + operation overlay（generate / simulate / transmit / receive / diagnose）
  + direction overlay（TX / RX / transceiver）
  + protocol overlay（BLE / Wi-Fi / generic digital / ...）
  + hardware overlay（PlutoSDR / B210 / file / simulation）
  + user-mentioned optional fields
```

TemplateResolver 的输入是 `IntentIR.requested_operations`、capabilities、direction、protocol、hardware、desired artifacts、success criteria，而不是七类 Task 标签或某句测试文本。LLM 负责从开放文本提取候选值；确定性 resolver 负责必填计算、默认来源、字段依赖和合法性验证。

首批 profile 范围：

| Profile | 典型 Required | 条件 Required / Optional 示例 |
|---|---|---|
| 通用流图生成 | goal、source/signal type、requested output | modulation、channel model、visualization、duration 按请求加入 |
| 通用硬件 TX | hardware、center frequency、sample rate、signal definition、bounded duration、success condition | gain/attenuation、bandwidth、antenna/path、payload |
| 通用硬件 RX | hardware、center frequency、sample rate、expected signal 或 observation goal、capture bound | bandwidth、demodulation、output sink、trigger |
| BLE TX | BLE role/operation、PHY 所需 waveform 参数；物理发射时叠加通用 TX 字段 | advertising channels；payload fields；local name 只在名称相关请求中加入 |
| Wi-Fi TX | Wi-Fi role、standard/band or channel、bandwidth；物理发射时叠加通用 TX 字段 | SSID 只对 beacon/AP 等相关帧加入；MCS、GI、payload 可选或按目标必填 |
| Diagnose | diagnosis target、requested dimensions、available artifact/device context | 期望设备身份、错误日志、物理连接证据按诊断维度加入 |

新增协议应增加 protocol profile、builder/validator 与测试向量，不修改 Alignment Coordinator 的主循环。新增硬件应增加 HardwareProfile，不复制协议模板。

### 4. 与 SharedIntent 的持久化关系

Radio Specification 与 SharedIntent **应合并为一个领域真值源，但保留不同视图职责**：SharedIntent 表达用户目标和约束，Radio Specification 是其中通信参数部分的结构化对象。不能再让 `RadioSpec`、`SharedIntent.parameters` 和 GUI 表格成为三个可独立修改的数据源。

建议在 `SharedIntent` 中增加版本化 `specification` 对象，包含 fields、template/profile refs、blocking questions、optional prompts、validation 和 patch history。唯一写者仍是 Host `IntentAlignmentCoordinator`；main agent、subagent、skill 和 GUI 只读取快照或提交 patch proposal。

持久化采用双层但单一写源：

- 权威状态：`local/agent_sessions/<session_id>/state.json` 中的 `shared_intent.specification`。
- 人类可读投影：每次 revision 后原子生成 `local/agent_sessions/<session_id>/radio_specification.json`。
- `radio_specification.json` 带 `intent_id`、`revision`、`semantic_hash`，只作为导出/审计产物，禁止 GUI 或 Agent 直接写回。

这样既满足"一直有文件记录、随对话更新"，又避免双写漂移。用户在 Workflow 建立后修改规格时，继续使用现有 `revision.py` 做影响分析：只使依赖该字段的 Stage/Artifact/Claim 失效；涉及 RF 参数或硬件的变更必须停止运行、撤销授权并重新确认。

### 5. 折叠式 Radio Specification UI

Radio Specification 保留在对话中，每次 revision 替换当前 live card，不重复堆积。默认展开状态取决于当前阶段：

- 有 blocking question：自动展开。
- 等待最终确认：自动展开。
- Workflow 已建立：默认折叠，标题显示 `Radio Specification · confirmed · rev N`。
- 规格被用户修改或校验失败：重新展开。

展开内容为只读表格，按 `Required`、`Mentioned/Added` 分组，Optional catalog 不直接铺满。卡片底部只显示 Open Questions；DeepRadio 随后在对话中询问用户可以"确认、继续修改、添加可选字段或介绍这些参数"。这四项是 LLM 需要识别的回复意图，不是另一组下拉框或必选按钮，最终由同一个 coordinator 生成结构化 interaction event，不能直接修改 JSON。

### 6. Workflow 与 Claims 合并为常驻 Workflow Monitor

对话区不再承担主要 Workflow 可视化。底部保留一个常驻、可折叠的 `Workflow Monitor`，将执行状态和 Claims 合并，避免"Workflow 一套状态、Claims 又一套状态"的重复认知负担。

折叠态始终显示：

```text
Task · 当前 Stage i/n · Running/Waiting/Failed/Passed · 关键 Claim 状态
```

展开态使用适合窄面板的**纵向 stepper**，而不是横向 FlowBox：

```text
✓ Build flowgraph          Passed
│  Claim: structure valid  Passed
↓
▶ Verify waveform          Running
│  Claim: BLE PHY valid    Checking
↓
○ Configure hardware       Pending
↩ 最近跳转：Verify → Build（修复后重新验证）
```

规则：

- Stage 是一级导航；与该 Stage 直接相关的 Claim/Evidence 摘要嵌在 Stage 下。
- Failed、Stale、Not tested 优先展示；完整 Evidence JSON 继续只落 session 或按需展开。
- `current_stage` 使用明显高亮；Passed/Failed/Pending/Waiting 使用稳定颜色与图标。
- 正向跳转显示连接箭头；retry/back transition 显示最近一次 `from → to + reason`，不把动态状态机伪装成固定线性流水线。
- Runtime 的 Stop/Emergency Stop 保留在同一 Monitor，但 task result 与 runtime quality 分开。

当前 UI 只在 `AgentReply` 完成后刷新，所以应增加 progress channel，而不是等待整个任务返回。最小改动是在现有 ServiceAgent/driver 中注入 `progress_sink(event, workflow_digest)`：每次 Stage started/completed/failed、checkpoint created/resolved、state revision changed 后发事件；GUI 用 `GLib.idle_add` 更新 ViewModel。它是观察接口，不改变 WorkflowEngine 的状态迁移，也不允许 GUI 直接推进 Stage。轮询仅作为断线后的兜底。

### 7. 对话宽度与自动换行修复

统一建立 responsive widget，而不是只修普通聊天气泡：

1. 普通消息、规格字段值、来源标签、Open Questions、Stage/Claim 文本统一使用 `_WrappingLabel` 工厂，并标记可重新约束的 role。
2. `_on_chat_size_allocate` 将 viewport 的实际 content width 减去卡片 margin 后下发给所有 live cards；取消残留的固定 `width_chars`/旧 `size_request`。
3. Radio Specification 从三列 `Gtk.Grid` 改为响应式 `Gtk.ListBox`：宽屏时 label/value/source 同行，窄屏时 source 换到 value 下方。移除会撑宽表格的 ComboBox。
4. Workflow 使用纵向 stepper；Stage label 设置 `WORD_CHAR` 换行和 `xalign=0`。
5. 路径、设备 identity 等长字符串插入安全的断行机会或中部省略；内部 hash 不进入用户 UI。

### 8. 硬件链路的工程契约（V5 汇总）

- 意图槽位使用规范键名；`device/sdr/radio` 等别名在 `_merge` 内归一到 `hardware`，归一不改变来源语义（LLM 返回记 `llm`，规则种子记 `rules`）。
- 种子兜底必须满足三个条件：键在结构化白名单（hardware/protocol/direction/modulation/carrier_frequency/sample_rate/bandwidth/ble_mode/advertising_channels）、LLM 未返回该键、种子值字面出现在用户原文（忽略大小写与 `-_/空格`）。不满足则丢弃，宁可追问也不把猜测当事实。
- 探测请求必须携带意图的硬件选择；跨家族扫描只在期望家族未命中后发生，且严格只读。
- 失败计数与诊断是提示层能力：不阻塞 Retry 按钮、不改变 Stage 状态机、不触发任何设备写操作。
- `hw_retry_failures` 计数持久化在 `intent.context`，发现设备即清零。

### 9. 验收场景

1. 通用 BPSK 仿真不出现 Device、Advertising name 或 BLE Channel；用户提到 roll-off 时，即使它是 optional 也必须回显。
2. "用硬件发射 BLE"只展示当前必填字段与已提及字段；Local name 在未被请求、未被成功条件依赖时不出现。
3. 用户第一轮只给出部分字段时，表格显示全部已知值和剩余 required；第二轮只回答一部分时，只追问剩余项；没有完整规格时不得建立 Workflow。
4. 用户已在原文陈述设备型号（如 "use plutosdr…"）时，任何一轮都不得再追问设备，无论 LLM 是否漏提取、用没用别名键（V5）。
5. BLE、Wi-Fi、通用 TX/RX 由 profile 组合得到不同字段集合；增加新 protocol profile 不修改 alignment 主循环。
6. 每次规格变化同时增加 SharedIntent revision，并能在 session 中读取同 revision/hash 的 `radio_specification.json`；直接修改导出文件不会改变运行状态。
7. Workflow Monitor 在 Stage 开始时即更新，不等待任务结束；失败回退能显示最近跳转原因，Claim 与 project/intent revision 一致。
8. 将侧栏宽度分别调整为 260、340、480 px，普通对话、规格行、Open Questions、Stage/Claim 均自动换行，不出现水平滚动或内容遮挡。
9. 期望设备未接入但另一受支持设备在位时，重试提示必须指出"期望 X 但发现 Y"；连续两次失败后提示附 LLM 诊断；设备恢复后一次探测即通过且计数清零（V5）。

本节改造后的自动回归基线为 agent tests `234 passed, 1 skipped`、GUI tests `17 passed`。实机宽度 260/340/480 px 的视觉换行、失败回退动画、真实设备 mismatch 提示与运行中 Stop/Emergency Stop 仍属于人工 GUI/HIL 验收，不应由无头单元测试代替。

---

## 工程主链与长期约束

### 主链（保持不变）

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

### 边界与后续工程要求

- `SharedIntent` 只能由 host coordinator 写；不要给 subagent 暴露文件写工具。
- Reference 只能描述规范、候选项和条件，不得写某个测试答案或固定 CRC/结果。
- GUI 必须通过 command API 修改状态，不能直接改 `state.json`。
- 中途 patch 已实现停止和重新确认；更细粒度的 Stage/Artifact 失效继续复用现有 Completion、project version 和 dependency，不新增平行状态机。
- 新硬件只应新增 `HardwareProfile`/诊断适配器；新协议只应新增 protocol reference/builder/validator；不在 Adapter 中堆设备或任务专用分支。
- 运行中的 RF 变更必须重新经过设备事实、离线校验和新的 `rf_authorization`，旧 grant 不可跨 intent revision。

### 禁止项

- 不为七条代表文本写专用判断、固定结果、固定 CRC 或固定频谱值。
- 不用纯 LLM 替代 Policy、Completion、设备控制和停止能力。
- 不删除 Workflow、SharedState、Plan Compiler、Catalog 或 deterministic handler。
- 不用"生成了文件"代替协议、测量、硬件或空口验收。
- 不把历史 session 改写为新版本结果，不覆盖 0827 V3 和 0828 V2。
- 不让 LLM 诊断、种子兜底或跨家族扫描获得任何写权限或安全门豁免。

完成标志是：最新版本的七类任务回归与 Pluto BLE HIL 都产生同一套版本指纹，状态、回复、产物和人工证据能够互相反查。
