# DeepRadio 测试与实验 V2

> 更新日期：2026-08-29<br>
> 当前证据：0827 V3 七类 GUI 实验与 0828 V2 PlutoSDR BLE 实验<br>
> 环境：所有自动测试和 GUI/HIL 实验均使用 `gnuradio` Conda 环境<br>
> 原则：历史实验保留为版本基线；代码发生行为相关修改后，必须在新目录重跑，不能覆盖旧记录。

---

## 0. 2026-08-29 增量：意图对齐、共享意图和泛化诊断实验

### 0.1 测试问题分析

旧实验主要使用参数完整、正交的七个单句，能够证明 happy path，但不能证明：短输入能被逐步补齐；回答顺序变化时状态仍正确；执行中改要求会失效旧产物；Subagent 使用的是用户确认版本；诊断不会混淆设备可见、物理连接和 OTA；UI 选择能反向改变 Workflow。

因此测试单位应从“一个 Text 对应一个 Task”升级为“一个 session 中的多轮轨迹”：输入草案、系统问题、用户选择、意图 revision、Workflow revision、工具事实、人工证据和最终结果必须一起验收。

### 0.2 自动/人工分类与顺序

| 类别 | 内容 | 顺序 |
|---|---|---|
| 完全自动 | Intent/RequirementResolver、SharedIntent 序列化、stale response、TaskCard hash、IntentPatch scope、离线建图/仿真/协议校验、Policy/Completion、诊断报告 schema | 每次提交先运行 |
| 先自动后人工 | GUI choices 和状态展示、discover/probe、设备型号一致性、流图编译、bounded runtime、日志与 underflow、OTA Evidence 绑定 | 自动 Gate 通过后再人工 |
| 主要人工 | 语言是否清楚、选择题是否符合预期、天线/端口/衰减器/线缆是否正确、LightBlue 是否看到名称、用户是否能理解和控制回退 | 最后执行并留截图/观察记录 |

固定顺序：

```text
Gate 0 静态/单元
→ Gate 1 Intent 多轮契约
→ Gate 2 GNU Radio 离线集成
→ Gate 3 GUI 人工交互
→ Gate 4 硬件只读 discover/probe
→ Gate 5 生成并编译未 arm 流图
→ Gate 6 独立 RF 授权后的有限时长运行
→ Gate 7 OTA/外部仪器 Evidence
```

任一 Gate 失败不得用后续人工结果覆盖。例如 LightBlue 偶然收到信号不能替代离线协议校验、设备身份匹配和有界停止能力。

### 0.3 人工输入字段清单

人工实验记录以下字段；“条件必填”由 RequirementResolver 决定，不要求用户第一句话全部给出：

| 字段 | 何时需要 | 示例 |
|---|---|---|
| `goal/operation` | 始终 | 生成、接收、观察、诊断、修改、配置、部署 |
| `protocol/modulation` | 协议或波形任务 | BLE、BPSK、QPSK、OFDM |
| `direction` | 收发或硬件任务 | tx / rx |
| `hardware` | DEVICE_READ 以上 | PlutoSDR、USRP B210 |
| `carrier_frequency` | 硬件或频域约束 | 2.402 GHz |
| `sample_rate/bandwidth/symbol_rate` | 构建或硬件配置 | 2 Msps |
| `local_name/payload` | BLE 广播或指定数据 | `manual-align-01` |
| `duration` | 有界 runtime | 10–30 s |
| `gain/attenuation` | RF 计划 | 低功率或 30 dB attenuation |
| `signal_source_scope` | 观察/诊断 | current project / live device / generated fixture |
| `desired_artifacts` | 有明确交付要求时 | `.grc`、频谱、报告、抓包 |
| `success/evidence` | 验收 | LightBlue 显示目标名称并附截图 |
| `safety constraints` | 硬件任务 | 不发射、停在确认、屏蔽箱、最大时长 |

每次人工记录还必须包含：session id、Git/dirty 指纹、Conda/GNU Radio、模型/LLM fallback、硬件序列或脱敏 identity、选择项、自由文本回答、所有 intent/workflow revision、最终证据路径。

### 0.4 人工实验教程 A：短输入逐步对齐

1. 在 `gnuradio` 环境启动 GRC，不设置 RF 开关也可完成对齐阶段。
2. 输入：`我要用硬件发射一段 BLE 信号`。
3. 预期系统先问硬件；选择 `PlutoSDR`（也应允许自定义其他受支持型号）。
4. 预期系统再问 `local_name`；输入 `manual-align-01`。
5. 预期显示完整意图摘要；此时仍没有 RF 授权，也不应启动设备。
6. 展开 Inspector：检查同一 `intent_id`、递增 revision、状态从 `awaiting_input` 到 `awaiting_confirmation`。
7. 点击“确认并建立 Workflow”。检查 Workflow 此时才出现，TaskCard 中的 intent id/revision/hash 与 SharedIntent 一致。

正确结果：问题顺序由缺失字段产生，不从固定测试句补答案；协议/安全默认有来源；确认意图不等于批准发射。UI 展示需人工检查，状态/hash/事件可自动检查。

### 0.5 人工实验教程 B：执行中修改与反向控制

1. 使用实验 A 建立 BLE Workflow，并推进到 RF 计划确认前。
2. 不批准 RF，在聊天中输入：`把 local name 改为 manual-align-02，其余不变`。
3. 预期生成新的 intent revision，并显示 `downstream` 影响；旧波形/流图/证据不能直接作为新名称的证明。
4. 确认新意图后，检查重新生成和离线校验包含 `manual-align-02`。
5. 若在受控 runtime 已运行时再改语义参数，预期先出现 `runtime_stopped_for_intent_patch`，`rf_active=false`；新 revision 必须重新走 RF 授权。
6. 在 UI 选择“继续修改”后输入另一个名称，确认 Workflow 跟随 UI/用户操作变化。这是“用户操作 UI 反向控制 Workflow”的验收。

自动检查：event 顺序、revision、hash、旧 grant 未复用、runtime 已停。人工检查：用户能否理解影响范围、按钮是否明确。

### 0.6 人工实验教程 C：泛化硬件诊断

连接 PlutoSDR 后输入：

```text
只读诊断当前连接的 PlutoSDR：检查驱动、设备发现、实际型号是否与我说的一致、exact probe、运行状态和还需要人工检查的连接；不要修改工程，不要发射。
```

预期 `diagnosis_report.json` 至少有 intent、environment、device discovery、identity match、exact probe、parameters/project/runtime/RF path 维度。设备正常时 discover/probe 应为 pass；没有运行时 runtime 可以为 unknown；物理线缆/天线必须为 unknown + requires_human，不能因为 `iio_info -S` 成功而 pass。拔掉设备重跑时 discovery/probe 应 fail 并给 remediation；换成 B210 而仍声称 Pluto 时 identity 不匹配不得静默改写用户意图。

报告 schema、工具结果和工程 hash 可自动检查；端口、天线、线缆和实际换设备需人工操作。

### 0.7 人工实验教程 D：Pluto BLE 端到端

1. 检查 Pluto、天线/衰减和合法实验环境；`conda activate gnuradio`。
2. 运行 `iio_info -S usb`，只把它登记为“主机可发现”，不要登记为物理 RF 路径通过。
3. `export GRC_AGENT_ENABLE_RF=1` 后启动 `PYTHONPATH=$PWD python -m grc --gtk --fresh`。
4. 输入：`用 PlutoSDR 发射 BLE 广播，希望手机看到名称 manual-ota-01`。其余参数让系统提问或展示默认来源。
5. 完成意图确认；检查 PDU/PHY 离线校验、未 arm 流图、discover/probe 和型号匹配。
6. 核对 RF 计划的设备 identity、频率、采样率、带宽、增益/衰减、最大时长后，单独批准有限时长发射。
7. 不手工点击 GRC Run。用 LightBlue 扫描 `manual-ota-01`，上传截图后确认 OTA。
8. 检查进程提前或到时停止，`runtime.status=stopped`、`rf_active=false`；日志若有 underflow，结果应为 passed with warning。

这项实验必须人工核对实验环境和 LightBlue；离线校验、身份绑定、启动时长、停止和证据 hash 可以自动验收。

### 0.8 本次自动回归记录

2026-08-29 在 `gnuradio` Conda 环境执行：

```bash
PATH=/Users/cindysha/miniforge3/envs/gnuradio/bin:$PATH \
/Users/cindysha/miniforge3/envs/gnuradio/bin/python \
  -m unittest discover -s grc/agent/tests -p 'test_*.py'

PATH=/Users/cindysha/miniforge3/envs/gnuradio/bin:$PATH \
/Users/cindysha/miniforge3/envs/gnuradio/bin/python \
  -m unittest discover -s grc/gui/tests -p 'test_*.py'
```

结果：agent `181 OK, skipped=1`（需要显式 HIL 条件的 Gate 跳过）；GUI `14 OK`。另执行 `compileall` 和 `git diff --check` 均通过。自动结果不替代 0.4～0.7 的 GTK 可用性、真实设备连接和 LightBlue OTA 人工实验。

## 1. 当前证据如何定性

| 证据 | 可以证明 | 不能证明 |
|---|---|---|
| `local/agent_sessions/0827/V3/` | 当时版本七类代表任务的主路径均完成；保留了较完整 session 事实 | 修改后的最新代码仍通过；开放文本泛化；全部回复和 Evidence 完全正确 |
| `local/output/0827/V3/` | 用户可见的流图、图片和截图 | 完整可复现归档；部分 `.py` 和原始 probe 未导出 |
| `local/agent_sessions/0828/V2/plutoble/` | Pluto BLE 离线校验、探测、授权、运行、手机观察和停止的过程 | 流质量 clean；OTA Evidence 附件完整；最新代码七类 Task 均通过 |
| `local/output/0828/V2/plutoble/` | 本轮交付产物与人工截图 | 独立 sniffer 的自动验收 |

实验目录缺少 commit、dirty diff、环境和模型指纹，因此 0827 与 0828 都是有效历史证据，但不能作为当前 HEAD 的发布证明。修改代码后应新建目录，例如：

```text
local/agent_sessions/0828/V3/task1 ... task7
local/output/0828/V3/task1 ... task7
local/agent_sessions/0828/V3/plutoble
local/output/0828/V3/plutoble
```

---

## 2. 0827 V3 七类任务审计结果

| Task | 主路径 | 未满足或证据不足 | 修改后必须新增的断言 |
|---|---|---|---|
| 1 端到端仿真 | Passed；EVM 约 5.89%，满足 `<10%` | Claim/Measurement/图片绑定不完整；probe 路径不可迁移 | 所有测量有 `measurement_id`；Claim 引用报告/图片；路径均可相对解析 |
| 2 TX 构建 | Passed；QPSK 仿真 TX，无硬件操作 | UI 摘要有 `?`；回复声称存在实际没有的星座/频谱；导出不完整 | 回复产物集合等于 Manifest；摘要为角色化 TX；可复现导出含 `.py` 和 TX 数据 |
| 3 RX 构建 | Passed；补充 8 dB 后 BER=0 | 只比较 8163 bit，缺统计限定；回复声称不存在图片；未解释自包含 TX/AWGN fixture | 报告 errors、compared bits、对齐和置信上界；回复明确 fixture；不存在产物不展示 |
| 4 诊断 | 主路径完成 | 计划的诊断报告和根因谓词未被产物/Completion 验收；引用旧 0826 工程路径 | `diagnosis_report.json`；工程 hash/version 不变；根因/建议引用 measurement 或 experiment |
| 5 修改 | 生成 QPSK 并验证 | recipe 重建代替最小 patch；无 diff/保留证明；写工程仍记为 `READ` | 使用 GraphPatch；确认前后 diff；未涉及元素保持；effect 至少 `ARTIFACT_WRITE` |
| 6 观察 | 生成频谱、星座和主峰 | `realtime_observe` 实际走离线重仿真；主峰没有醒目标注排除 DC；引用旧工程路径 | `signal_source_scope=current_project_offline`；报告 DC/非 DC 规则；输入工程进入本轮 artifact |
| 7 硬件配置 | 安全预览、Pluto probe、没有 RF | 配置确认与发射授权语义接近；deferred RF 尾部显示冗余；默认测试音未显式说明 | checkpoint purpose=`config_handoff`；无 `RF_RUN` grant；默认信号来源在 UI 可见；probe warning 可见 |

结论口径：七项均为“happy path 完成”；Task4～6 不应登记为“语义和证据完全通过”，其他 Task 也有轻量证据或展示缺口。

---

## 3. 0828 V2 Pluto BLE 审计结果

已确认：

- 用户文本进入 GUI session，不是直接导入结果；
- BLE 本地名为 `Deepradio27`，频率 2.402 GHz；
- 离线包、波形和结构校验通过；
- Pluto identity 被发现并探测；
- 用户确认后由受控 runtime 发射；
- LightBlue 实际看到目标名称；
- runtime 最终 `return_code=0`、`status=stopped`。

仍存在：

1. 日志出现 GNU Radio `U`，`rf_runtime_underflow` 为 Failed；本轮应记为 `passed_with_warning`，不是 clean pass。
2. OTA 观察只有人工按钮确认，`artifact/sha256/evidence_id` 为空、`evidence_complete=false`；它证明人工看到了，但不满足论文级附件 Evidence。
3. `rf_started=true` 在停止后仍存在，容易被理解成仍在发射；应拆成历史和当前状态。
4. Workflow completed 没有突出 Failed underflow Claim。
5. Session 没有版本和模型指纹。

---

## 4. 测试类型与顺序

| 类型 | 适用内容 | 顺序 |
|---|---|---|
| 完全自动 | schema、Intent 锁定规则、Plan Compiler、effect、Completion、Manifest、测量算法、无授权副作用 | 每次改代码首先运行 |
| 自动后人工 | 七类 GUI、回复事实一致性、图像可读性、Workflow Inspector、修改 diff | 自动回归全绿后运行 |
| 必须人工或独立接收端 | 天线/接线、手机 LightBlue、真实空口、GTK 交互观感 | 软件和设备预检均通过后最后运行 |

总顺序：

```text
静态/单元
→ ServiceAgent 集成
→ session replay/manifest
→ 无 RF GUI 七类回归
→ 硬件 discover/probe
→ RF 安全和 stop 故障注入
→ 有界 Pluto BLE
→ OTA 人工/独立 Evidence
```

不得为了省时间跳过离线 BLE、device identity、RF authorization 和 stop Gate。

---

## 5. 自动回归教程

从仓库根目录执行：

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m unittest discover -s grc/agent/tests -t . -v
PYTHONPATH=$PWD python -m unittest discover -s grc/gui/tests -t . -v
```

本文件不写死测试数量；以当前代码实际发现数量为准。人工检查：

1. 两条命令退出码均为 0；
2. skip 必须注明缺失的可选硬件能力，不能把失败改成 skip；
3. 不允许在 GNU Radio 运行失败后写入伪造指标；
4. 保存完整控制台日志和 `run_metadata.json`；
5. 修改后连续运行三次关键 Workflow 测试，检查无状态漂移。

应补充或更新的自动测试：

| 测试位置 | 新增覆盖 |
|---|---|
| `grc/agent/tests/test_workflow.py` | LLM intent/plan trace；未配置与异常 fallback；`signal_source_scope`；checkpoint purpose；未知 predicate 不通过 |
| `grc/agent/tests/test_plan_p12.py` | 工具 effect 上界；LLM 不得删除安全尾部；开放复合文本不被七类标签歪曲 |
| `grc/agent/tests/test_seven_tasks.py` | 下表七类主路径；事实化回复；GraphPatch 优先；诊断报告；离线/实时来源 |
| `grc/agent/tests/test_hardware.py` | 版本指纹；可复现导出；路径迁移；RF active/ever；underflow quality；三种 checkpoint |
| `grc/agent/tests/test_ble.py` | 通用 BLE 算法；Evidence grade；无附件不能达到最高 Gate；单信道能力声明 |
| `grc/gui/tests/` | 无 `?` 摘要；warning/Failed Claim；配置交付、RF 授权、OTA 按钮文案 |

### 5.1 必测的反过拟合样例

除七条代表文本外，每类至少准备同义、顺序变化、中英混合、否定、缺槽、多轮和复合目标。特别加入：

- “分析这个工程为什么误码高，但别动我的图”；
- “先做一个可保存的 Pluto 发射预览，今天不要上空口”；
- “我已经有一个手工改过的流图，只把调制阶数换掉”；
- “看一下天线口现在收到的频谱”和“看一下当前仿真文件的频谱”；
- “生成 BLE 波形但不要接设备”；
- “现在停止刚才的发射”；
- 未出现七类关键词但具有同等目标的开放表达。

断言目标、能力、effect、来源域、决策边界和禁止事件，不对完整回复文本做 exact match。

---

## 6. 七类 GUI 回归教程

启动：

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

每个 Task 使用新 session。Task4～6 的输入工程必须复制到该 session 或由同一测试前置步骤生成，不能引用旧实验绝对路径。

### Task1：端到端仿真

- 输入：`构建 BPSK 过 AWGN 并测 EVM，要求 EVM 小于 10%`
- 自动检查：Workflow completed；EVM 有单位和样本量并小于阈值；`.grc`、星座、频谱存在；Claim、图片和 Measurement 共用 ID。
- 人工检查：画布、星座与回复易读；回复没有多报产物。

### Task2：发射机

- 输入：`构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件`
- 自动检查：无设备事件、无 RF checkpoint、无 SDR sink；`.grc/.py/TX data` 在可复现导出中。
- 人工检查：摘要显示 `QPSK 基带 TX → File Sink`，不能有 `?`，不能声称有未生成图片。

### Task3：接收机

- 输入：`构建 BPSK 接收机并测 BER`
- 交互：按提示输入 `Eb/N0 8 dB`。
- 自动检查：同一 workflow 延续；BER 报告有 errors、compared bits、delay、对齐方法和置信上界。
- 人工检查：回复说明 TX/AWGN 是自包含 BER 测试参考，不把有限样本 BER=0 说成绝对无误码。

### Task4：诊断

- 前置：打开一份本轮归档的可运行工程。
- 输入：`诊断当前链路的 EVM，给出最小建议，先保持工程不变`
- 自动检查：前后工程 hash/version 相同；生成结构化诊断报告；报告引用测量/对照。
- 人工检查：建议与证据一致，且明确“只提出建议、没有应用修改”。

### Task5：修改

- 前置：打开本轮 BPSK 工程。
- 输入：`把当前 BPSK 改成 QPSK`
- 交互：先查看 diff，再确认。
- 自动检查：确认前 hash 不变；确认后使用 GraphPatch；effect=`ARTIFACT_WRITE` 或更高；无关节点、连接和参数保持。
- 人工检查：画布是在原工程上修改，而不是无说明打开一张全新的 recipe 图。

### Task6：观察

- 前置：打开本轮离线工程。
- 输入：`查看当前工程的频谱和星座图，给出非 DC 主峰，只观察不修改`
- 自动检查：source scope 为 `current_project_offline`；工程 hash/version 不变；报告有 DC 排除、FFT、窗和分辨率。
- 人工检查：回复不出现“实时接收”或未被数据支持的载频结论。

另做实时分支：`查看当前 PlutoSDR 天线口接收信号的实时频谱`。该分支必须要求设备并走 RX hardware path，不得回退离线 AWGN。

### Task7：硬件配置

- 输入：`为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认`
- 自动检查：生成禁用 RF 的安全预览；发现并 probe 精确设备；checkpoint purpose=`config_handoff`；无 arm/start，未授予 `RF_RUN`。
- 人工检查：默认诊断音清楚标为系统安全默认；按钮是“确认已保存/继续发射”，不是“批准发射”；probe warning 可见。

---

## 7. PlutoSDR BLE 端到端教程

### 7.1 自动与硬件预检

```bash
conda activate gnuradio
iio_info -S
PYTHONPATH=$PWD python -m unittest grc.agent.tests.test_ble -v
PYTHONPATH=$PWD python -m unittest grc.agent.tests.test_hardware -v
```

要求：`iio_info -S` 无 fatal error；系统识别的设备类型和 identity 与用户表达一致；BLE 离线包/波形校验通过；stop/emergency_stop 和 duration cap 测试通过。

### 7.2 启动与输入

在符合当地法规的屏蔽或低功率实验条件下：

```bash
export GRC_AGENT_ENABLE_RF=1
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

输入：

```text
用 PlutoSDR 发射一段 2.402 GHz 的 BLE 广播，
local name 为 DeepRadioTest，目标是用手机 LightBlue 扫描到，最长发射 30 秒。
```

操作：

1. 核对离线校验和设备 identity；
2. 核对频率、采样率、衰减和最长时长；
3. 点击“确认有限时长发射”；
4. 用 LightBlue 扫描 `DeepRadioTest`；
5. 在 OTA checkpoint 选择看到/未看到，并上传截图；
6. 确认状态显示 `rf_active=false`、`runtime.status=stopped`。

### 7.3 通过分级

| 等级 | 条件 |
|---|---|
| 控制面通过 | 离线校验、设备 identity、授权、bounded start、stop、return code 均正确 |
| 产品目标通过 | LightBlue 实际看到目标 local name |
| 论文 Evidence 通过 | 截图或独立接收端证据有 artifact、hash、run_id 和目标名称绑定 |
| 流质量 clean | 无 underrun/overrun；否则只能 `passed_with_warning` |

手机收到信号不能抵消 underflow；underflow 也不能反向否定手机确实收到。二者必须分别报告。

---

## 8. 人如何检查自动测试确实成功

每轮实验打开以下文件交叉核对：

```text
run_metadata.json  代码/环境/模型/配置指纹
events.jsonl       用户输入、LLM/回退、Stage、Tool、Checkpoint、序号
workflow.yaml      当前计划、effect、执行状态、Completion、决定
state.json         工程、runtime、Measurement、Claims、Evidence
final/manifest.json  相对路径、大小、SHA-256、role、producer
```

检查顺序：

1. `events.jsonl` 第一轮文本等于实际输入；seq 单调；
2. Intent 事件说明 LLM 是否调用和回退；
3. Workflow 的每个 completed Stage 都有 Completion 事实；
4. State 中 Claim 引用当前工程版本和真实 artifact/measurement；
5. Manifest 中每个文件存在且 hash 匹配；
6. GUI 截图的状态、回复和文件事实一致；
7. RF session 的 `run_id` 在授权、启动、OTA 和停止记录中一致。

---

## 9. 故障注入与发布 Gate

必须自动覆盖：LLM 未配置、LLM 超时/非法 JSON、未知 PlanNode、工具失败、Manifest 文件缺失、工程被外部修改、设备未连接、连接设备与请求不一致、RF 开关关闭、arm 失败、start 崩溃、underflow、用户拒绝、OTA 未看到、停止超时和 emergency stop。

发布 Gate：

1. 当前版本全部自动测试退出码为 0；
2. 七类 GUI 回归在同一 run metadata 基线上通过；
3. 回复、状态、Artifact 和 Claim 一致；
4. 修改任务默认使用 patch，诊断与观察不改变工程；
5. RF 未授权绝不 start，停止能力在故障注入中通过；
6. Pluto BLE 在同一版本上完成 HIL；
7. 论文空口声明具有附件或独立接收端 Evidence；
8. 所有 warning、Failed Claim 和 Evidence 不完整均在 GUI 可见。

只有八个 Gate 同时满足，才能用“最新版本已通过七类任务与 Pluto BLE 端到端实验”的表述。
