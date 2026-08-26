# DeepRadio 测试与实验

> 日期：2026-08-26
> 读者：测试、实验、CHI、算法与工程人员
> 范围：自动回归、七类 Task 交互实验、GUI 验收、Pluto/B210 硬件实验和证据标准

---

## 1. 测试分层


| 层级                 | 执行者     | 验证目标                               | 主要证据                |
| ------------------ | ------- | ---------------------------------- | ------------------- |
| A. 单元与契约           | 自动      | Intent、状态迁移、工具算法、Completion、Policy | unittest 输出         |
| B. ServiceAgent 集成 | 自动      | 七类 Task、Stage 顺序、产物、Claim、恢复       | 临时会话与断言             |
| C. GUI 人机交互        | 人工      | 文本理解、确认行为、Inspector、画布刷新           | 截图、会话文件             |
| D. HIL 空口          | 人工＋自动记录 | 真实设备、受控运行、手机/接收机观察、停止              | runtime、Evidence、截图 |


A/B 层证明控制面和算法契约。手机扫描结果由 D 层证明。

---



## 2. 自动回归

仓库根目录：

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m unittest discover -s grc/agent/tests -v
PYTHONPATH=$PWD python -m unittest grc.gui.tests.test_chat_markup -v
```

2026-08-26 结果：

```text
Ran 121 tests in 13.044s
OK (skipped=1)

Ran 6 tests in 0.029s
OK
```

跳过项为 B210 实机 discover/probe（`GRC_AGENT_HIL=1` 且设备在线才跑）。若当前 shell 里仍留着 GUI 空口实验的 `GRC_AGENT_ENABLE_RF=1`，该项也会 skip，不会把 Gate 1 判红。自动回归默认不调用线上 Intent LLM；GUI 不受影响。运行中可能出现 GNU Radio double-mapped buffer 告警；验收以最后的 `Ran ...`、`OK` 和退出码为准。

测试模块：`test_seven_tasks.py`、`test_ble.py`、`test_hardware.py`、`test_workflow.py`、`grc.gui.tests.test_chat_markup`。

### 2.1 覆盖范围

- 七类 Task 代表文本与不少于 70 条变体分类；
- 多轮补槽、低置信 Intent 补全、确认/拒绝/取消；
- 同一 Agent：端到端仿真 → 只读诊断 → 确认后把 BPSK 改成 QPSK（流图、recipe、version）；
- DENY / 失败改图不计入 `flowgraph_saved`、不误加 version；导出 Manifest 按路径去重；
- Workflow revision、Stage attempt、Completion、失效；
- TaskCard / ResultEnvelope 协议；
- BLE PDU、CRC、白化、IQ 回环；
- BLE Pluto/B210 Flowgraph 结构与安全默认值；
- HardwareProfile、`type=b200`、Pluto IIO URI probe；
- RF Policy、环境开关、语义哈希、armed flowgraph；
- 解释器选择、启动健康检查、run_id、停止和 crash；
- OTA 确认与活动 runtime、目标名称、run_id 绑定；
- 相对路径会话、导出 Manifest、GUI markup。



### 2.2 自动测试做不到的

- 指定 SDR 已由当前主机打开；
- 天线端存在符合预期的空口波形；
- 手机 LightBlue 已看到目标广播；
- GTK 布局和按钮可用性。

---



## 3. 人工复核自动测试

1. 使用 `gnuradio` conda 环境。
2. 从仓库根目录执行 §2 命令。
3. 最后一行是 `OK`，记录数量和 skipped。
4. `echo $?` 期望为 `0`。

失败时：

```bash
PYTHONPATH=$PWD python -m unittest -v \
  grc.agent.tests.test_hardware.V3HardwareWorkflowRegressionTest
```

保留首次失败日志、复现命令、环境信息和临时会话。

会话目录核对：

```text
state.json        JSON 可解析，工程和 Claim 版本一致
workflow.yaml     当前 Stage、状态、attempt、Completion 合理
events.jsonl      seq 单调，Tool / Checkpoint / run_id 可追踪
final/*.grc       GRC 可打开，结构与用户槽位一致
final/manifest.json  相对路径存在，size 和 SHA-256 匹配
runtime_status    start/stop/run_id/return_code 与事件一致
runtime.log       有进程输出
```

导出必须使用独立空目录；Manifest 只含本轮显式产物。

---



## 4. 七类 Task 的 GUI 代表实验

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

每个用例使用新 session（可点「重置」）。不要勾选「一句话直出(baseline)」。`DIAGNOSE` / `MODIFY_PROJECT` / `OBSERVE` 先打开已有工程。记录输入、回复、按钮、画布/状态栏截图和 session 路径。

### 4.1 `END_TO_END_SIM`

```text
做一个 BPSK 过 AWGN 的基带链路，EVM 小于 10%，显示星座图和频谱。
```

规格完整时允许自动执行；若询问阈值，回答“10%”。期望：Task 为端到端仿真；产出 `.grc`、EVM、星座图、频谱图和当前版本 Claims。EVM 达标才完成。

### 4.2 `TX_BUILD`

```text
构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件。
```

出现 RF 确认即失败。期望：TX `.grc` 和结构校验；事件中无设备发现、配置或 start。

### 4.3 `RX_BUILD`

```text
构建一个自包含的 BPSK AWGN 接收机，包含定时恢复和判决，并测 BER。
```

缺 Eb/N0 时答“8 dB”，保持同一 `workflow_id`。期望：接收流图；BER 同时引用发送参考和接收判决 probe。

### 4.4 `DIAGNOSE`

前置：打开已有工程。

```text
诊断当前链路的 EVM，解释主要原因并给出最小修改建议，先保持工程不变。
```

若询问修复，选择拒绝。期望：有诊断与 Evidence；`.grc` 哈希、Project version 和画布不变。

### 4.5 `MODIFY_PROJECT`

前置：打开 BPSK 工程。

```text
把当前 BPSK 工程改成 QPSK，其余条件保持一致。
```

先看方案，再点确认。期望：确认前工程不变；确认后 version 增加，流图变为 QPSK，受影响 Claim 重验。

### 4.6 `OBSERVE`

前置：打开可仿真的接收工程。

```text
查看当前接收信号的频谱和星座图，给出主峰，只观察工程。
```

期望：图和指标；工程哈希与版本不变。

### 4.7 `HARDWARE_CONFIGURE`

```text
为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。
```

批准配置，拒绝发射。期望：发现与 probe 精确设备；生成禁用发射的基础 `.grc`；无 `start_flowgraph` 成功事件。

## 5. Text 数据集实验

七类 Task 每类至少 10 条文本，合计 70 条。覆盖：完整表达、参数顺序、中文同义、英文或中英混合、缺槽、多轮补充、否定约束、复合目标、模糊指代、与相邻 Task 易混的表达。

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

评测：Task accuracy、slot exact match、缺槽识别率、同一 Workflow 延续率、受限操作违规率、完成率、平均轮次、平均 Stage 数、端到端时延。

有 LLM 时至少重复 5 次，报告均值、标准差和失败样例。确定性条件执行一次全量回归，再对关键边界重复运行。

---



## 6. GUI 验收



### 6.1 状态栏与 Inspector

人工确认：

- 任务名称、类型、当前 Stage、序号；
- Stage 状态、attempt、Completion `n/m`；
- 等待原因与确认/取消（RF / 空口专用文案）；
- 时间线 Seq、Event、Stage、Actor（含 origin 与 mode）；
- runtime 的 `run_id`、状态、剩余时间、return code、末行日志；
- BLE 规格摘要；仿真任务的「改规格」。



### 6.2 交互

- 缺参数后补充；
- 确认、拒绝、取消；
- 失败后受控重试发射；
- 活动任务中插入无关新任务；
- 用户保存画布导致 Claim 待重验；
- 重置时硬件进程紧急停止；
- 小白、学生、专家三档语言风格（技术阈值一致）。

---



## 7. PlutoSDR BLE 端到端实验



### 7.1 环境

- PlutoSDR 经 USB 连接；
- 手机安装 LightBlue；
- 合法、低功率、可控实验环境；
- 可调用 GNU Radio IIO 块与 `iio_info`。



### 7.2 启动

```bash
conda activate gnuradio
iio_info -S
export GRC_AGENT_ENABLE_RF=1
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

`iio_info -S` 应显示 Pluto/ADALM 及 USB URI。

### 7.3 输入与操作

```text
用 PlutoSDR 发射一段 2.402 GHz 的 BLE 广播，local name 为 Deepradio27，
目标是用手机 LightBlue 扫描到，最长发射 30 秒。
```

1. Workflow 为 `HARDWARE_CONFIGURE`，operation 为 `deploy`。
2. 离线协议校验、设备发现和精确 URI probe 通过。
3. RF 计划确认处核对频率、采样率、增益、设备和最长时长。
4. 点击「批准有限时长发射」。状态栏提示无需点击 GRC Run。
5. 出现新的 `run_id`、running/ready 和剩余时长。
6. LightBlue 扫描 `DRTEST24`。
7. 扫描到后点「已看到目标名称」，可「附加上传截图」；未扫描到点「未看到」。
8. Workflow 进入停止阶段，runtime 终止且 return code 合法。



### 7.4 通过标准

- 输入 local name 出现在 PDU、离线解码和手机扫描中；
- probe 绑定发现阶段同一 URI；
- RF 启动发生在用户批准之后；
- `start_flowgraph` 返回 `running=true`、`ready=true`、`startup_health_passed=true` 和 `run_id`；
- 空口确认时 runtime 仍在 deadline 内，Evidence 引用同一 `run_id`；
- `stop_flowgraph` 返回 `running=false`、`crashed=false`；
- `runtime_status.json`、`runtime.log`、事件和 Claim 一致；
- 截图在 `final/evidence/` 并进入 Manifest。

未选择截图时，记录为「手机观察通过、Evidence 附件缺失」。

### 7.5 2026-08-24 记录

会话：`local/agent_sessions/0824_V6/gui-9edd1171`  
导出：`local/output/0824_V6`


| 事件                    | 结果                                           |
| --------------------- | -------------------------------------------- |
| 用户提交                  | 21:14:01.533                                 |
| 到达 RF 确认              | 21:14:01.922                                 |
| 用户批准 RF               | 21:14:39.460                                 |
| managed runtime ready | 21:14:40.582                                 |
| 空口确认                  | 21:14:46.477，运行中，elapsed 约 6.65 秒            |
| 停止                    | 21:14:47.049，`return_code=0`，`crashed=false` |
| 运行标识                  | `run-f646528e87c5`                           |
| 手机                    | LightBlue 扫描到 `loveu`                        |


前三个自动 Stage 约 0.39 秒；RF 启动约 1.12 秒。`duration_seconds=30` 为最长窗口，空口确认后工作流主动停止。本轮未把截图写入 `final/evidence/`。

### 7.6 2026-08-25 V2 记录

会话：`local/agent_sessions/0825/V2/plutoble/gui-190d6c70`  
导出：`local/output/0825/V2/plutoble`  
输入 local name：`Mobicom27`（与 §7.3 示例 `DRTEST24` 不同，按随机新名称复测）


| 事件          | 结果                                                              |
| ----------- | --------------------------------------------------------------- |
| 用户提交        | 用 PlutoSDR 发射 2.402 GHz BLE，local name `Mobicom27`              |
| 离线协议 / 发现探测 | 通过；URI `usb:2.4.5`                                              |
| RF 批准后启动    | `run_id=run-ac9a5230c71c`，`pid=74718`                           |
| 空口          | LightBlue 扫到 `Mobicom27`，MAC `DD:C2:2E:E0:5B:D4`，RSSI 约 -83 dBm |
| 停止          | `return_code=0`，`crashed=false`，`reason=stopped`                |
| 信道          | 仅 CH37 / 2.402 GHz                                              |
| runtime 日志  | `UUUU` 欠载                                                       |
| Evidence    | 手机截图在 output 目录；未写入 `final/evidence/`                           |


问题与改法见工程文档 §8。

---



## 8. B210 硬件实验



### 8.1 只读预检

```bash
conda activate gnuradio
uhd_find_devices
uhd_usrp_probe --args="type=b200"
```

记录 serial、USB 速率、FPGA/Firmware。Workflow 中的设备身份须与 probe 一致。

### 8.2 BLE TX HIL

使用 §7 流程，设备改为 USRP B210。检查 Builder 选择 `uhd_usrp_sink`、`device_args` 含 `type=b200`、采样率和增益在批准范围。完成低功率实验并用 LightBlue 或独立 BLE sniffer 验收。登记状态：待验证。

### 8.3 RX 实时频谱

```text
用 B210 在 2.402 GHz、2 Msps 查看实时频谱，先生成流图并停在运行确认。
```

通过标准：流图含 `uhd_usrp_source` 与 `qtgui_freq_sink_x`；批准前没有运行；批准后 QT 频谱窗口实时刷新；停止/重置后设备释放。截图、runtime 和事件一并保存。

---



## 9. 故障注入矩阵


| 场景                 | 期望结果                        |
| ------------------ | --------------------------- |
| RF 环境变量缺失          | start 被 Policy 拒绝           |
| 设备发现为空             | 停在 waiting_user，无 armed 流图  |
| probe 身份不匹配        | 配置和发射均被阻止                   |
| 生成代码导入失败           | startup health 失败，Stage 不通过 |
| 进程立即退出             | crashed，运行 Claim 失败         |
| 同 session 重复 start | 第二次启动被拒绝                    |
| 用户取消/reset/archive | emergency stop，armed 清除     |
| 到达 deadline        | 自动停止并持久化终态                  |
| OTA 确认时进程已停        | over_air_observed 不通过       |
| OTA 名称不匹配          | Evidence 拒绝提交               |
| Flowgraph 语义变化     | 原批准和 armed 状态失效             |
| 导出目录预置其他文件         | Manifest 仅含本轮产物             |
| 会话目录移动             | 相对路径仍可解析                    |


每个故障同时断言 Workflow 状态、runtime、Claim、事件和 GUI 回复。

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

- 状态栏、Checkpoint、画布刷新、运行状态和日志可理解；
- 三种专业度完成任务，技术结论一致；
- 可从 session 文件复核关键回复。



### Gate 4：硬件安全

- discover/probe/start/status/stop/emergency-stop 故障矩阵通过；
- RF 默认关闭；
- 所有运行有唯一 `run_id` 和 deadline；
- reset/archive 不留存运行进程。



### Gate 5：空口

- Pluto HIL 用新 local name 重复通过；
- B210 HIL 完成并落盘；
- Evidence 附件、Claim、run_id、Manifest 完整一致；
- 三信道跳频用例只在 37/38/39 跳频调度实现后登记通过。

---



## 11. 下一步实验

1. 用随机新 local name 重复 Pluto HIL，并完整归档截图。
2. 执行 B210 只读预检、BLE TX HIL 和 RX 频谱 HIL。
3. 实现并验证 BLE 37/38/39 三信道跳频调度。
4. 按 §9 补齐真实进程故障注入。

