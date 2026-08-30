# DeepRadio 工程方案 V2

> 更新日期：2026-08-30<br>
> 当前证据：`local/agent_sessions/0827/V3/`、`local/output/0827/V3/`、`local/agent_sessions/0828/V2/plutoble/`、`local/output/0828/V2/plutoble/` 与当前工作区代码<br>
> 状态口径：实验已暴露但尚未由修改后全量回归证明关闭的问题，统一视为活动问题或待回归问题。<br>
> 约束：不替换现有框架，不引入第二套编排器，不把系统改成纯 LLM 执行。

---

## 自适应 Radio Specification 与常驻 Workflow Monitor 方案（2026-08-29）

### 实施状态（2026-08-30）

本节 P0～P2 已按不替换现有 WorkflowEngine 的方式实现。主要落点为：

- `grc/agent/knowledge/specs/profiles.json` 与 `spec_requirements.py`：组合 general、TX/RX、BLE、Wi-Fi、diagnose profile，分离 required、mentioned、optional 和 derived。
- `intent_state.py`、`shared_state.py`：Radio Specification 已纳入 SharedIntent 权威状态，并原子投影为 session 下的 `radio_specification.json`。
- `intent_alignment.py`：支持一轮多字段自然语言补齐、部分回答后继续追问、默认值显式确认、可选字段介绍和最终 confirm。
- `AgentPanel.py`：对话中只读、可折叠、分组与分色的 Radio Specification；旧 ComboBox/Entry 写规格入口已删除。
- `ClaimsPanel.py` 与 `workflow_presenter.py`：底部常驻 Workflow Monitor，Stage 纵向 stepper 内嵌 Claim，并显示最近正向/回退跳转。
- `adapter.py`：增加只读 progress channel，只推送意图 revision、Stage、checkpoint 和 Workflow 状态事件，不把 tool/routing log 涌入 UI；GUI 可实时刷新，但不获得推进状态机的写权。
- BLE Local/Advertising Name 已改为真正的可选字段；未指定名称也能构建、生成和校验合法广播，空口验收回退为通用“观察到目标信号”。

自动回归结果：GNU Radio `gnuradio` conda 环境下 agent tests `195 passed, 1 skipped`（HIL 条件跳过），GUI tests `17 passed`。GNU Radio 会在 `/var/tmp` 创建双映射缓冲区，因此全量仿真回归必须在允许该路径的环境运行；本次已在非沙箱模式通过。

### 改造前问题与根因（已关闭）

本节取代此前“在 Radio Specification 表格内用下拉框一次性填写”和“Workflow 只作为对话卡片显示”的 UI 方案。目标仍以 `local/docs/jensen/273b186c647dde8b1425086f7f4724be.png` 与 `local/docs/jensen/deepradio-task-walkthrough-plan.md` 为信息架构参考，但图中的 PlutoSDR BLE 只是一个 state-dependent workflow instance，不能反向成为所有任务的固定模板。

以下是本轮改造针对的历史问题及根因，其处置结果以上方“实施状态”为准。

| 问题 | 直接原因 | 架构根因 | 影响 |
|---|---|---|---|
| BLE 规格总出现 Advertising name 等字段 | `SharedState.spec_digest()` 根据 `protocol == ble` 硬编码展示 Channel、Advertising name 等行 | “展示字段”“必填字段”“协议可选字段”没有独立模型，示例字段进入了通用投影 | 无关字段污染任务，扩展 Wi-Fi/通用信号时会继续堆条件分支 |
| 必填和可选字段混在一起 | `requirements.json` 只有少量 required rule，UI 又把已有默认值和 choices 全部当作可编辑项 | 缺少 required/mentioned/optional/derived 四类字段语义 | 用户不知道哪些信息阻塞 Workflow，默认值也可能被误当成用户决定 |
| 交互像填写配置表，不像和 Agent 协作 | GUI 的 ComboBox/Entry 直接提交 `specification_update` | 意图补齐同时由 GUI 控件和 Alignment Coordinator 驱动，存在两套交互入口 | 不利于研究自然语言渐进对齐，也无法检验 LLM 对补充文本的理解 |
| Radio Specification 没有稳定文件身份 | `RadioSpec`、`SharedIntent.parameters` 与 GUI digest 存在重叠 | 规格只是 SharedIntent 的投影，但尚未显式定义唯一真值源和持久化契约 | 修改、回退、跨 Agent 共享时容易产生两个版本 |
| Workflow 到任务结束后才明显出现 | 对话卡只在完整 `AgentReply` 返回时刷新；后台 Stage 执行期间没有 progress event | UI 订阅的是“回复完成”，不是“Workflow 状态变化” | 用户看不到实时运行、跳转、失败回退和等待确认 |
| Stage 框图不容易理解跳转 | 窄侧栏内使用 `FlowBox` 平铺 Stage，仅靠图标表达状态 | 展示模型缺少 transition/current/previous/retry/back-edge 语义 | 回退和重试看起来像静态列表 |
| 对话文字不能可靠随宽度换行 | `_FlowLabel` 只约束普通气泡；规格卡、Grid、badge、Stage label 使用普通 `Gtk.Label`，部分控件会撑大自然宽度；已分配宽度还可能残留旧值 | 缺少统一的 responsive label/card 组件 | 缩窄侧栏后出现截断、横向撑宽或不重新排版 |

### 1. Radio Specification 字段选择规则

规格默认只展示两类字段：

1. **Required fields**：完成当前请求的下一项可执行产物或物理操作所必需的字段。
2. **Mentioned fields**：用户文本明确提到的字段，即使它不是必填字段，也必须回显，防止系统忽略用户约束。

Optional fields 不默认占据表格；用户通过自然语言提出、点击“可选字段”提示，或者要求教学介绍后才加入。Derived fields 仅在其对建图、验证或执行有实际影响时展示，并标记来源，不把所有协议常量都塞进规格表。

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

`ask_user_question` 可以在一条消息中询问多个缺失字段，但回复解析必须允许用户只回答其中一部分。每轮只合并能可靠识别的字段，未回答字段继续保留 `Unresolved`，不得为了尽快建立 Workflow 而猜测用户答案。

Open Questions 分两类：

- `blocking_questions`：缺失 required 字段或验证冲突；未解决时不能建立 Workflow。
- `optional_prompts`：是否添加可选字段、是否需要参数教学；不影响规格完整性，在最终 confirm 中提供入口，不新增 Workflow 阻塞状态。

教学模式只解释字段含义、推荐范围和取舍，不替用户确认字段，也不能将 Claim 或 Completion 改成 Passed。

### 3. Template 设计：组合模板，不复制固定任务表

不建议维护“BLE 发射完整模板”“Wi-Fi 发射完整模板”两份互相独立的大表。应采用声明式组合：

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

这样既满足“一直有文件记录、随对话更新”，又避免双写漂移。用户在 Workflow 建立后修改规格时，继续使用现有 `revision.py` 做影响分析：只使依赖该字段的 Stage/Artifact/Claim 失效；涉及 RF 参数或硬件的变更必须停止运行、撤销授权并重新确认。

### 5. 折叠式 Radio Specification UI

Radio Specification 保留在对话中，每次 revision 替换当前 live card，不重复堆积。默认展开状态取决于当前阶段：

- 有 blocking question：自动展开。
- 等待最终确认：自动展开。
- Workflow 已建立：默认折叠，标题显示 `Radio Specification · confirmed · rev N`。
- 规格被用户修改或校验失败：重新展开。

展开内容为只读表格，按 `Required`、`Mentioned/Added` 分组，Optional catalog 不直接铺满。卡片底部只显示 Open Questions；DeepRadio 随后在对话中询问用户可以“确认、继续修改、添加可选字段或介绍这些参数”。这四项是 LLM 需要识别的回复意图，不是另一组下拉框或必选按钮，最终由同一个 coordinator 生成结构化 interaction event，不能直接修改 JSON。

### 6. Workflow 与 Claims 合并为常驻 Workflow Monitor

对话区不再承担主要 Workflow 可视化。底部保留一个常驻、可折叠的 `Workflow Monitor`，将执行状态和 Claims 合并，避免“Workflow 一套状态、Claims 又一套状态”的重复认知负担。

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

### 8. 修改落点与顺序

| 顺序 | 文件/模块 | 修改职责 |
|---|---|---|
| P0-1 | `grc/agent/knowledge/specs/` | 将单一 requirements 文件拆成字段注册表、通用/operation/protocol/hardware profile；声明 required 条件、依赖、默认来源和教学说明 |
| P0-2 | `grc/agent/knowledge/spec_requirements.py` | 实现 TemplateResolver，输出 required/mentioned/optional/derived 和 validation，不依赖 Task 文案 |
| P0-3 | `grc/agent/state/intent_state.py`、`shared_state.py` | 把 Radio Specification 作为 SharedIntent 的版本化子对象；删除硬编码 BLE 行；原子导出只读 `radio_specification.json` |
| P0-4 | `grc/agent/workflow/intent_alignment.py` | 支持多字段自然语言增量补齐、成组 blocking question、可重复追问和最终 confirm；移除 GUI 表格直接写值的主路径 |
| P1-1 | `grc/gui/workflow_presenter.py` | 增加 read-only specification、open questions、transition、stage-bound claim 的纯 ViewModel |
| P1-2 | `grc/gui/AgentPanel.py` | 折叠式只读规格卡、自然语言交互、统一 wrapping widget；移除规格 ComboBox/Entry |
| P1-3 | `grc/gui/ClaimsPanel.py` | 改为常驻可折叠 Workflow Monitor，纵向 Stage stepper 内嵌 Claim；保留 Runtime controls |
| P1-4 | `grc/agent/service/adapter.py` / workflow driver | 增加只读 progress sink，在 Stage/checkpoint/revision 事件后推送 digest |
| P2 | tests | Template 组合、缺失字段循环、规格持久化、revision 失效、实时事件顺序、不同宽度 GTK layout 与人工截图验收 |

不新增第二套状态机，不让 LLM 决定安全门是否通过，不把 BLE/Wi-Fi 示例文本写进分类器，也不允许 GUI、subagent 或 skill 直接编辑 `state.json`/`radio_specification.json`。

### 9. 验收场景

1. 通用 BPSK 仿真不出现 Device、Advertising name 或 BLE Channel；用户提到 roll-off 时，即使它是 optional 也必须回显。
2. “用硬件发射 BLE”只展示当前必填字段与已提及字段；Local name 在未被请求、未被成功条件依赖时不出现。
3. 用户第一轮只给出部分字段时，表格显示全部已知值和剩余 required；第二轮只回答一部分时，只追问剩余项；没有完整规格时不得建立 Workflow。
4. 用户选择“介绍这些参数”只获得解释，不改变 confirmed/source/Claim。
5. BLE、Wi-Fi、通用 TX/RX 由 profile 组合得到不同字段集合；增加新 protocol profile 不修改 alignment 主循环。
6. 每次规格变化同时增加 SharedIntent revision，并能在 session 中读取同 revision/hash 的 `radio_specification.json`；直接修改导出文件不会改变运行状态。
7. Workflow Monitor 在 Stage 开始时即更新，不等待任务结束；失败回退能显示最近跳转原因，Claim 与 project/intent revision 一致。
8. 将侧栏宽度分别调整为 260、340、480 px，普通对话、规格行、Open Questions、Stage/Claim 均自动换行，不出现水平滚动或内容遮挡。

本节改造后的自动回归基线已更新为 agent tests `195 passed, 1 skipped`和 GUI tests `17 passed`。实机宽度 260/340/480 px 的视觉换行、失败回退动画以及运行中 Stop/Emergency Stop 仍属于人工 GUI/HIL 验收，不应由无头单元测试代替。

## 0. 已实现的工程基础（历史问题与处置）

本节记录当前方案所依赖的既有能力。表中“根因”描述的是改造前的历史问题，不代表它们仍全部处于未修复状态；本轮新增待实施项以文档最前面的 P0～P2 为准。

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
