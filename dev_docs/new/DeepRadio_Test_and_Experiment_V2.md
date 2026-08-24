# DeepRadio 测试与实验 V2

> 日期：2026-08-24
> 读者：测试、实验、CHI、算法与工程人员
> 范围：自动回归、七类 Task 交互实验、GUI 验收、Pluto/B210 硬件实验和证据标准

---

## 1. 测试分层

DeepRadio 的验收分为四层：

| 层级 | 执行者 | 验证目标 | 主要证据 |
|---|---|---|---|
| A. 单元与契约 | 自动 | Intent、状态迁移、工具算法、Completion、Policy | unittest 输出 |
| B. ServiceAgent 集成 | 自动 | 七类 Task、Stage 顺序、产物、Claim、恢复 | 临时会话与断言 |
| C. GUI 人机交互 | 人工 | 文本理解、确认行为、Inspector、画布刷新 | 截图、会话文件 |
| D. HIL 空口 | 人工＋自动记录 | 真实设备、受控运行、手机/接收机观察、停止 | runtime、Evidence、截图 |

一次能力声明必须落到对应层级。A/B 层通过可证明控制面和算法契约；手机扫描结果由 D 层证明。

---

## 2. 当前自动回归基线

在仓库根目录执行：

```bash
conda activate gnuradio
PYTHONPATH=$PWD:$PWD/scripts/jensen python -m unittest \
  scripts.jensen.test_ble_deploy_contracts \
  scripts.jensen.test_dynamic_workflow_v2_contracts \
  scripts.jensen.test_hardware_runtime_contracts \
  scripts.jensen.test_intent_llm \
  scripts.jensen.test_seven_task_service_agent \
  scripts.jensen.test_seven_task_text_variants \
  scripts.jensen.test_seven_task_texts \
  scripts.jensen.test_usrp_rx_spectrum_contracts \
  scripts.test_ble_protocol_safety \
  scripts.test_chat_markup \
  scripts.test_workflow_capability_composition \
  scripts.test_v3_hardware_workflow_regressions
```

2026-08-24 验证结果：

```text
Ran 79 tests in 9.543s
OK (skipped=1)
```

运行中可能出现 GNU Radio double-mapped buffer 分配告警并回退到其他 buffer 实现。本次命令退出码为 0，所有断言通过。验收时以最后的 `Ran ...`、`OK` 和 shell 退出码为准，同时保存完整日志以便判断告警是否影响特定平台。

### 2.1 自动覆盖范围

- 七类 Task 的代表文本与 7×10 文本变体分类；
- 多轮补槽、低置信续跑、确认/拒绝/取消、新任务切换；
- Workflow revision、Stage attempt、Completion、失效和停止循环；
- TaskCard/ResultEnvelope 协议与 Agent/Tool 范围；
- BLE PDU、CRC、白化、IQ 回环和参数变化；
- BLE Pluto/B210 Flowgraph 结构与安全默认值；
- HardwareProfile、Pluto IIO 扫描/URI probe、B210 UHD 探测；
- RF Policy、环境开关、语义哈希、armed flowgraph；
- 解释器选择、启动健康检查、run_id、运行状态、停止和 crash；
- OTA 确认与活动 runtime、目标名称、run_id 的绑定；
- Manifest 相对路径、会话导出和 GUI markup 回归。

### 2.2 自动测试边界

自动回归不产生真实 RF，无法单独证明：

- 指定 SDR 已经由当前主机打开；
- 天线端存在符合预期的空口波形；
- 手机 LightBlue 或独立 sniffer 已看到目标广播；
- GTK 布局、按钮可用性和用户理解符合要求；
- 移动整个会话目录后所有外部绝对路径均可恢复。

---

## 3. 自动测试的人工复核方法

### 3.1 命令级复核

1. 确认使用 `gnuradio` conda 环境。
2. 从仓库根目录执行 §2 的完整命令。
3. 检查最后一行是 `OK`，并记录测试数量和 skipped 数量。
4. 检查命令退出码：

```bash
echo $?
```

期望为 `0`。

### 3.2 单用例定位

失败时按测试类或测试方法运行：

```bash
PYTHONPATH=$PWD:$PWD/scripts/jensen python -m unittest -v \
  scripts.test_v3_hardware_workflow_regressions
```

保留首次失败日志、复现命令、环境信息和相关临时会话。避免仅以重跑成功覆盖偶发错误。

### 3.3 产物复核

对会话目录检查：

```text
state.json        JSON 可解析，当前工程和 Claim 版本一致
workflow.yaml     当前 Stage、状态、attempt、Completion 合理
events.jsonl      seq 单调，关键 Tool/Checkpoint/run_id 可追踪
final/*.grc       GRC 可打开，结构与用户槽位一致
final/manifest    相对路径存在，size 和 SHA-256 匹配
runtime_status    start/stop/run_id/return_code 与事件一致
runtime.log       有进程输出，异常时包含诊断信息
```

导出检查必须使用独立空目录。当前导出函数会扫描目标目录，复用宽目录会把无关文件加入 Manifest。

---

## 4. 七类 Task 的 GUI 代表实验

通用启动：

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

每个用例使用新 session。记录输入、全部系统回复、按钮选择、画布截图、Claims/Inspector 截图以及 session 路径。

### 4.1 `END_TO_END_SIM`

输入：

```text
做一个 BPSK 过 AWGN 的基带链路，EVM 小于 10%，显示星座图和频谱。
```

交互：规格完整时允许自动执行；若系统询问阈值，回答“10%”。

正确过程与产物：Task 为 `END_TO_END_SIM`；经过规格对齐、构建与验证；生成 `.grc`、EVM、星座图、频谱图和当前版本 Claims。EVM 达标才可完成。

检查方式：结构、文件存在性和 EVM 可自动检查；图像内容和 GUI 刷新人工抽检。

### 4.2 `TX_BUILD`

输入：

```text
构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件。
```

交互：保持“只做仿真”约束；任何 RF 确认均判失败。

正确过程与产物：Task 为 `TX_BUILD`；生成 TX `.grc` 和结构校验；事件中无设备发现、配置或 start 调用。

检查方式：可自动检查；人工打开 GRC 确认画布可读。

### 4.3 `RX_BUILD`

输入：

```text
构建一个自包含的 BPSK AWGN 接收机，包含定时恢复和判决，并测 BER。
```

交互：如缺少 Eb/N0，回答“8 dB”；补充后应保持同一 `workflow_id`。

正确过程与产物：Task 为 `RX_BUILD`；生成接收流图；BER 证据同时引用发送参考和接收判决 probe。

检查方式：结构与双 probe 自动检查；人工确认补槽轮次没有新建 Workflow。

### 4.4 `DIAGNOSE`

前置：打开一个已有工程。

输入：

```text
诊断当前链路的 EVM，解释主要原因并给出最小修改建议，先保持工程不变。
```

交互：诊断后若出现修复询问，选择拒绝。

正确过程与产物：Task 为 `DIAGNOSE`；有诊断与 Evidence；`.grc` 哈希、Project version 和画布保持不变。

检查方式：哈希与版本自动检查；解释质量人工评分。

### 4.5 `MODIFY_PROJECT`

前置：打开 BPSK 工程。

输入：

```text
把当前 BPSK 工程改成 QPSK，其余条件保持一致。
```

交互：先检查方案和待修改范围，再点击确认。

正确过程与产物：Task 为 `MODIFY_PROJECT`；确认前工程不变；确认后 Project version 增加，流图变为 QPSK，受影响 Claim 重新验证。

检查方式：版本、语义差异和 Claim 可自动检查；画布刷新人工检查。

### 4.6 `OBSERVE`

前置：打开可仿真的接收工程。

输入：

```text
查看当前接收信号的频谱和星座图，给出主峰，只观察工程。
```

交互：无需修改确认。

正确过程与产物：Task 为 `OBSERVE`；输出图和指标；工程哈希与版本保持不变。

检查方式：不变性与文件存在性自动检查；主峰、图像和交互自然度人工检查。

### 4.7 `HARDWARE_CONFIGURE`

无 RF 的配置代表输入：

```text
为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。
```

交互：批准配置，拒绝发射。

正确过程与产物：Task 为 `HARDWARE_CONFIGURE`；发现与 probe 精确设备；生成禁用发射的基础 `.grc`；无 `start_flowgraph` 成功事件。

检查方式：设备探测和流图结构可自动检查；真实连接和 GUI 确认人工检查。

---

## 5. Text 数据集实验

七类 Task 每类至少 10 条文本，合计 70 条。每类包含：

1. 标准完整表达；
2. 参数顺序变化；
3. 中文同义表达；
4. 英文或中英混合；
5. 缺槽位输入；
6. 多轮补充；
7. 否定约束；
8. 复合目标；
9. 模糊指代；
10. 容易与相邻 Task 混淆的表达。

每条记录：

```json
{
  "case_id": "HW-07",
  "turns": ["用户第一轮", "用户补充或决定"],
  "expected_task": "HARDWARE_CONFIGURE",
  "expected_operation": "deploy",
  "expected_slots": {"hardware": "pluto", "protocol": "ble"},
  "forbidden_events": ["unapproved_rf_start"],
  "manual_checks": ["回复没有歪曲目标"]
}
```

评测指标：Task accuracy、slot exact match、缺槽识别率、同一 Workflow 延续率、受限操作违规率、完成率、平均轮次、平均 Stage 数、端到端时延。

LLM 条件至少重复 5 次，报告均值、标准差和失败样例。确定性条件执行一次全量回归，再对关键边界做重复运行。

---

## 6. GUI 与人机交互验收

### 6.1 Inspector

人工确认以下信息实时更新：

- Task 名称和类型；
- 当前 Stage、序号和总数；
- Stage 状态、attempt 和 Completion；
- 等待原因、确认和取消按钮；
- 执行时间线的 seq、Stage、执行模式、执行者和 Tool；
- runtime 的 `run_id`、状态、剩余时间和 return code。

当前检查重点：确定性执行应显示真实 `mode`；Checkpoint 解决后 Completion 应显示完成；managed runtime 的日志和自动启动状态应足够醒目。

### 6.2 交互行为

至少覆盖：

- 缺参数后补充；
- 确认、拒绝、取消；
- 失败后修改参数并重试；
- 活动 Workflow 中插入无关新任务；
- 用户保存画布导致 Claim stale；
- reset 时硬件进程紧急停止；
- 小白、学生、专家三档语言风格。

技术验收阈值在三个专业度档位保持一致。

---

## 7. PlutoSDR BLE 端到端实验

### 7.1 环境与安全

- PlutoSDR 通过 USB 连接；
- 手机安装 LightBlue；
- 使用合法、低功率、可控实验环境；
- 安装并可调用 GNU Radio IIO blocks 与 `iio_info`。

### 7.2 启动

```bash
conda activate gnuradio
iio_info -S
export GRC_AGENT_ENABLE_RF=1
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

`iio_info -S` 应显示 Pluto/ADALM 设备及 USB URI。出现错误时先处理驱动、USB、权限或 IIO 环境。

### 7.3 输入与交互

输入：

```text
用 PlutoSDR 发射一段 2.402 GHz 的 BLE 广播，local name 为 DRTEST24，
目标是用手机 LightBlue 扫描到，最长发射 30 秒。
```

操作：

1. 检查 Workflow 为 `HARDWARE_CONFIGURE`，operation 为 `deploy`。
2. 查看离线协议校验、设备发现和精确 URI probe 通过。
3. 在 RF 计划确认处核对频率、采样率、增益、设备和最长时长。
4. 点击“批准有限时长发射”。
5. Inspector 应出现新的 `run_id`、running/ready 和剩余时长。
6. 在 LightBlue 扫描 `DRTEST24`。
7. 扫描到后点击“已看到目标名称”；未扫描到则点击“未看到”。
8. 确认 Workflow 进入停止阶段，runtime 终止且 return code 合法。

### 7.4 通过标准

- 输入 local name 动态出现在 PDU、离线解码结果和手机扫描中；
- 设备 probe 绑定发现阶段获得的同一 URI；
- RF 启动发生在用户批准之后；
- `start_flowgraph` 返回 `running=true`、`ready=true`、`startup_health_passed=true` 和 `run_id`；
- 空口确认时 runtime 仍在 deadline 内，Evidence 引用同一 `run_id`；
- `stop_flowgraph` 返回 `running=false`、`crashed=false`，return code 合法；
- `runtime_status.json`、`runtime.log`、事件和 Claim 相互一致；
- 截图保存到 `final/evidence/` 并进入 Manifest。

当前 Evidence 文件选择需要人工核对。若截图只留在会话文件夹外，本轮记录为“手机观察通过、Evidence 附件缺失”。

### 7.5 2026-08-24 V6 结果

会话：`local/agent_sessions/0824_V6/gui-9edd1171`<br>
导出：`local/output/0824_V6`

实测结果：LightBlue 扫描到 `loveu`；managed runtime 使用 `run-f646528e87c5`，启动健康检查通过，空口确认时仍在运行，随后以 `return_code=0`、`crashed=false` 停止。

复核结论：

- BLE 动态名称、Pluto 探测、自动启动、活动运行空口确认和停止链路通过；
- 自动 Stage 的高速度来自确定性通用算法与工具链；
- 用户随后手动点击 GRC 运行箭头，说明自动运行状态和日志的 GUI 可见性仍需增强；
- 导出 Manifest 含其他目录条目，导出隔离检查未通过；
- 手机截图未与 Claim 一起归档，Evidence 附件检查未通过；
- Checkpoint Completion 计数、路径可迁移性和 BLE 专用摘要需要继续验证。

---

## 8. B210 硬件实验

### 8.1 只读预检

```bash
conda activate gnuradio
uhd_find_devices
uhd_usrp_probe --args="type=b200"
```

记录 serial、USB 速率、FPGA/Firmware 信息和命令完整输出。Workflow 中发现的设备身份必须与 probe 一致。

### 8.2 BLE TX HIL

使用与 Pluto 相同的 §7 流程，将输入设备改为 USRP B210。检查 Builder 选择 `uhd_usrp_sink`、设备 args 绑定目标 B210、采样率和增益处于批准范围。完成屏蔽/低功率实验并用 LightBlue 或独立 BLE sniffer验收。

B210 BLE TX HIL 当前需要执行并落盘 Evidence，登记状态为待验证。

### 8.3 RX 实时频谱

输入：

```text
用 B210 在 2.402 GHz、2 Msps 查看实时频谱，先生成流图并停在运行确认。
```

通过标准：流图含 `uhd_usrp_source` 与 `qtgui_freq_sink_x`；批准前没有运行；批准后 QT 频谱窗口有实时刷新；停止/重置后设备释放。屏幕截图、runtime 状态和事件一并保存。

---

## 9. 故障注入矩阵

| 场景 | 期望结果 |
|---|---|
| RF 环境变量缺失 | start 被 Policy 拒绝 |
| 设备发现为空 | 停在 waiting_user，无 armed 流图 |
| probe 身份不匹配 | 配置和发射均被阻止 |
| 生成代码导入失败 | startup health 失败，Stage 不通过 |
| 进程立即退出 | crashed，运行 Claim 失败 |
| 同 session 重复 start | 第二次启动被拒绝 |
| 用户取消/reset/archive | emergency stop，armed 清除 |
| 到达 deadline | 自动停止并持久化终态 |
| OTA 确认时进程已停 | over_air_observed 不通过 |
| OTA 名称不匹配 | Evidence 拒绝提交 |
| Flowgraph 语义变化 | 原批准和 armed 状态失效 |
| 导出目录预置其他文件 | Manifest 仅含显式本轮产物 |
| 会话目录移动 | 相对路径仍可解析 |

每个故障必须同时断言 Workflow 状态、runtime、Claim、事件和 GUI 回复，防止错误被后续停止动作覆盖。

---

## 10. 发布门槛

### Gate 1：自动回归

- §2 全量命令退出码为 0；
- flaky 重跑率为 0；
- skipped 项有明确平台原因。

### Gate 2：七类 Task

- 每类代表用例通过；
- 70 条 Text 数据集达到约定阈值；
- 否定约束与只读任务无越权动作。

### Gate 3：GUI

- Workflow Inspector、Checkpoint、画布刷新、运行状态和日志可理解；
- 三种专业度完成任务，技术结论一致；
- 人工可从 session 文件复核每个关键回复。

### Gate 4：硬件安全

- discover/probe/start/status/stop/emergency-stop 故障矩阵通过；
- RF 默认关闭；
- 所有运行有唯一 `run_id` 和 deadline；
- reset/archive 不留存运行进程。

### Gate 5：空口

- Pluto HIL 用新 local name 重复通过；
- B210 HIL 完成并落盘；
- Evidence 附件、Claim、run_id、Manifest 完整一致；
- 三信道用例只在 37/38/39 调度实现后登记通过。

---

## 11. 当前执行顺序

1. 修复导出目录隔离、显式 Artifact Manifest 和相对路径恢复。
2. 补齐 Checkpoint Completion 与 Evidence 附件原子提交。
3. 增强 GUI 的 managed runtime 状态、日志、执行模式和受控重跑。
4. 重跑 79 项自动测试及故障注入矩阵。
5. 用随机新 local name 重复 Pluto HIL，并完整归档截图。
6. 执行 B210 只读预检、BLE TX HIL 和 RX 频谱 HIL。
7. 实现并验证 BLE 37/38/39 三信道调度。
