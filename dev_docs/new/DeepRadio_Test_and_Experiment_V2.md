# DeepRadio 测试与实验

> 日期：2026-08-27
> 读者：测试、实验、CHI、算法与工程人员
> 范围：自动回归、七类 Task 金标、GUI 验收、Pluto/B210 硬件实验和证据标准
> 算法对照：`DeepRadio_Workflow_Algorithm_V2.md`（控制面流水线、BLE `deploy_stages`、离线验证）
> 工程对照：`DeepRadio_Engineering_V2.md`（P0/P1/P2 已接线；Wave 1/2 拷贝去冗余已落地，剩余见该文档 §5.3）

---

## 1. 测试分层


| 层级                 | 执行者     | 验证目标                                     | 主要证据                |
| ------------------ | ------- | ---------------------------------------- | ------------------- |
| A. 单元与契约           | 自动      | IntentIR、Compiler、工具算法、Completion、Policy | unittest 输出         |
| B. ServiceAgent 集成 | 自动      | catalog 片段、Stage 顺序、产物、Claim、恢复          | 临时会话与断言             |
| C. GUI 人机交互        | 人工      | 文本理解、确认行为、Inspector、画布刷新                 | 截图、会话文件             |
| D. HIL 空口          | 人工＋自动记录 | 真实设备、受控运行、手机/接收机观察、停止                    | runtime、Evidence、截图 |


A/B 层证明控制面和算法契约。手机扫描结果由 D 层证明。七类 Task 是评测标签和 catalog 片段库，不是独立编排器。§4 是主机控制面金标，不是 LLM 智能门。泛化看 `test_workflow` / `test_plan_p12`；空口看 §7。

### 1.1 评测挂在哪一段流水线上

权威流水线（算法文档 §1）：

```text
用户文本 → turn_relation → IntentIR → Catalog 片段
→ Plan Compiler 截断 → 主机确定性 / LLM Subagent
→ Completion → SharedState / Claim / GUI
```


| 流水线环节                       | 主要自动证据                                                           | 不由 Gate 1 证明    |
| --------------------------- | ---------------------------------------------------------------- | --------------- |
| `turn_relation` / Intent 分类 | `test_seven_tasks` 73 变体、`test_workflow` Intent                  | 开放句式是否好懂        |
| Catalog 片段与短视距              | `test_workflow` 截断/批准展开；`test_plan_p12` Compiler                 | —               |
| 主机 Stage（协议、探测、arm、发射、停止）   | `test_ble`、`test_hardware`、七类代表路径                                | 指定 SDR 已打开      |
| 离线协议验证                      | `test_ble`：PDU/CRC/白化/IQ 回环；建图后、发现前必须过 `offline_protocol_verify` | 天线端波形           |
| LLM / Skill                 | 默认不调线上模型；无 LLM 时回落确定性 handler                                    | 智能程度、Skill 是否被读 |
| 空口 Evidence                 | OTA 与 `run_id`/名称绑定的契约                                           | LightBlue 扫描    |


---



## 2. 自动回归

仓库根目录：

```bash
conda run -n gnuradio python -m unittest discover -s grc.agent.tests -t .
conda run -n gnuradio python -m unittest discover -s grc.gui.tests -t .
```

等价写法（已 `conda activate gnuradio`）：

```bash
PYTHONPATH=$PWD python -m unittest discover -s grc/agent/tests -v
PYTHONPATH=$PWD python -m unittest discover -s grc/gui/tests -v
```

2026-08-27（`gnuradio` conda，仓库根目录，`required_permissions` 不受限沙箱）结果：

```text
grc.agent.tests  Ran 156 tests, OK (skipped=1)
grc.gui.tests    Ran 13 tests, OK
```

合计 **169** 项自动测试通过，其中 1 项按设计跳过。跳过项为可选硬件块或 B210 实机 discover/probe（`GRC_AGENT_HIL=1` 且设备在线才跑）。若当前 shell 仍留着 GUI 空口实验的 `GRC_AGENT_ENABLE_RF=1`，HIL 项也会 skip，不会把 Gate 1 判红。自动回归默认不调用线上 Intent LLM。运行中可能出现 GNU Radio 无 Throttle 告警；验收以最后的 `Ran ...`、`OK` 和退出码为准。受限沙箱（禁止 `/var/tmp`）会让真实 GNU Radio 仿真失败，不得改用伪造 metrics。

七类 Task 主检包含在上述 Agent discover 中（23 项全部 ok）。单独运行：

```bash
conda run -n gnuradio python -m unittest grc.agent.tests.test_seven_tasks
```

当前模块与用例数：


| 模块                                         | 用例数     | 重点                                                                                           |
| ------------------------------------------ | ------- | -------------------------------------------------------------------------------------------- |
| `grc/agent/tests/test_seven_tasks.py`      | 23      | 七类标签金标：分类、73 条文本变体、ServiceAgent 产物/Claim/禁止副作用                                               |
| `grc/agent/tests/test_ble.py`              | 17      | `deploy_stages`、离线验证、Pluto/B210 未武装流图、单信道能力与 RF 默认关                                          |
| `grc/agent/tests/test_hardware.py`         | 45      | 设备探测、未 arm 流图回写 identity、硬件规格摘要、运行事务、capability blocker 与 B210 HIL gate                      |
| `grc/agent/tests/test_workflow.py`         | 60      | IntentIR、能力组合、短视距/批准展开、Completion、Mutation、Export、开放式复合                                      |
| `grc/agent/tests/test_plan_p12.py`         | 11      | Plan Compiler、尾部重规划、DiagnosisExperiment、GraphPatch、MeasurementRun、Profile pin、BLE capability |
| `grc/gui/tests/test_chat_markup.py`        | 6       | GTK 对话 Markdown/Pango 转换                                                                     |
| `grc/gui/tests/test_workflow_presenter.py` | 7       | Task/Stage、runtime、Inspector、RF/OTA（含「人工确认、附件缺失」）/结束后清 pending                               |
| **合计**                                     | **169** | Agent 156 项，GUI 13 项                                                                         |




### 2.1 覆盖范围

- 七类标签代表文本与 73 条变体分类（标签选 catalog 片段，不测 LLM 是否调用 Skill）；
- 多轮补槽、低置信 Intent 补全、确认/拒绝/取消；
- 主机控制面 Stage 走确定性 handler（`stage_handlers` 分派），不读 `SKILL.md`；无覆盖 recipe 的开放建图才可能进 LLM；
- RX 构建缺少 `Eb/N0` 时进入 input wait，补充「8 dB」后保持同一 Workflow，recipe 为 `rx_bpsk_awgn`，并写入 `ebn0_db`；
- 同一 Agent：端到端仿真 → 只读诊断 → 观察主峰 → 确认后把 BPSK 改成 QPSK（流图、recipe、version、Claim 重验）；
- 独立打开已有工程后再 `DIAGNOSE` / `OBSERVE`：version 与 GRC 哈希不变；
- `MODIFY_PROJECT` 拒绝确认后工程、recipe 与哈希不变；
- TX 仿真图不得含 SDR sink，探针文件不得命名为 `*_rx.bin`；
- Pluto 配置流图必须含禁用的 Pluto sink 与目标频率/采样率，不得回退 `bpsk_awgn`；Builder 失败同样禁止仿真降级；未 arm 图 naive critic 失败、`arm_disabled_rf` 拓扑重验通过；
- 无 recipe 的硬件规格摘要不得写成 `? → ? → ?`；
- Inspector `outcome=failed` 不被 completion 4/4 显示成 passed；`wait_kind=recovery` 有可操作按钮；
- OBSERVE 完成时必须有带 Hz/dBFS 的主峰报告，并写入回复与 measurement Claim；`open_questions` 必须为空；
- DENY / 失败改图不计入 `flowgraph_saved`、不误加 version；导出 Manifest 按路径去重；
- Workflow revision、Stage attempt、Completion、失效；
- TaskCard / ResultEnvelope 协议；
- BLE `deploy_stages` 顺序：`build_ble_advertiser`（sink 仍 disabled）→ `offline_protocol_verify` → `discover_and_probe_device` → 停在 `rf_plan_confirmation`；
- 离线验证完成 = `verify_ble_packet_bits.valid`（独立解析 preamble/AA/CRC/名字 + IQ 回环）且 `validate_flowgraph.valid`；未过不得发现设备；随后 `arm`/`start` 仍再查 `ble_packet_valid`；
- BLE Pluto/B210 Flowgraph 结构与安全默认值；RF 默认关（无 `GRC_AGENT_ENABLE_RF=1`、sink disabled）；
- HardwareProfile、`type=b200`、Pluto IIO URI probe；
- RF Policy、环境开关、语义哈希、armed flowgraph；
- 解释器选择、启动健康检查、run_id、停止和 crash；
- runtime 日志中的 `U`/`O` 调度器标记统计，以及 underrun/overrun 警告；
- BER 必须包含有效 report、比较比特数、对齐方法、TX/RX probe 和绑定 Claim；仅提交低 BER 数值无法通过 Completion；
- OTA 确认与活动 runtime、目标名称、run_id 绑定；
- 相对路径会话、导出 Manifest、GUI markup；
- 未知 PlanNode 被 Compiler 拒绝；无 LLM 时尾部重规划保持 deferred catalog 片段；
- DiagnosisExperiment 无因素时 `ranked=[]` 且 `ok`；对照不得改原工程哈希 / `flowgraph_version`；
- GraphPatch `set_param`/`replace_block`/`connect` 别名与 precondition 失败拒绝；
- 图像与标量写入同一 `measurement_id`；Claim stale 记录 `stale_reason`；
- Profile pin 后 observe 不改 score，也不改 Intent；
- BLE PDU/波形声明 `ble_advertising_single_channel`，并列出三信道/sniffer 为 unsupported；
- 不属于 `VARIANTS` 的开放式复合请求不得展开 RF Stage。
- Wave 1/2 去冗余后仍须守住：handler 分派、`compact_invocations`、Registry 生成的 LC 包装、`debug_by_metric`（无 `diagnose_by_metric` 别名）。



### 2.2 自动测试做不到的

- 指定 SDR 已由当前主机打开；
- 天线端存在符合预期的空口波形；
- 手机 LightBlue 已看到目标广播；
- GTK 布局和按钮可用性；
- LLM 是否选用 Skill、开放句式的语义质量。

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

导出必须使用独立空目录。Manifest 消费累积 ArtifactIndex（相对路径、大小、SHA-256），不得用本轮显式产物集合覆盖历史条目，也不得收录导出目录里预置的无关文件。

---



## 4. 七类 Task：自动主检与人工抽检

七类名称用于选 catalog 片段和评测对照，运行时不是七套编排器。本节金标守住主机控制面：分类、产物、Claim、禁止副作用。不要用本节判断「Agent 够不够聪明」。泛化与 Compiler 在 `test_workflow` / `test_plan_p12`；BLE 部署与空口在 `test_ble` 和 §7。

迭代顺序：先跑本节命令。红了只修控制面，不要开 GUI。主检全绿后再做人工抽检。主检绿不等于产品过关；GTK 观感、回复是否好懂、真设备仍靠抽检和 §7。

```bash
conda run -n gnuradio python -m unittest grc.agent.tests.test_seven_tasks -v
```

2026-08-27：`test_seven_tasks` 23 项 OK。覆盖七类 Task 分类、73 条文本变体、ServiceAgent 执行、结构化确认/拒绝、产物、Claim 和禁止副作用。代表路径必须真正完成；测量失败不得标成 `completed`。只读诊断必须保持工程哈希不变（对照实验在内存中进行并恢复）。

人工 GUI 抽检使用：

```bash
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

每个 GUI 用例使用新 session，不勾选「一句话直出(baseline)」。`DIAGNOSE`、`MODIFY_PROJECT`、`OBSERVE` 先打开已有工程。


| Task                 | 代表输入                                               | 自动检测（2026-08-27 已通过）                                                                                                                   | 人工检测                                                       |
| -------------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `END_TO_END_SIM`     | `构建 BPSK 过 AWGN 并测 EVM，要求 EVM 小于 10%`              | 必须 completed；`.grc`；EVM<10%；星座图与频谱图文件；`evm_lt_10` Claim Passed；recipe=`bpsk_awgn`                                                      | 画布、星座图和频谱图可读性；回复是否准确                                       |
| `TX_BUILD`           | `构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件`                     | QPSK File Sink；无 SDR 块；探针不是 `*_rx.bin`；无 hardware capability、discover 和 start                                                          | 画布布局；界面中没有 RF 误导                                           |
| `RX_BUILD`           | `构建 BPSK 接收机并测 BER`，随后输入 `Eb/N0 8 dB`              | 缺槽 input wait；同一 `workflow_id`；`ebn0_db=8`；recipe=`rx_bpsk_awgn`；有效 BER report、TX/RX probe 与 `ber_measured` Claim                      | 补槽提示是否易懂；接收链路布局                                            |
| `DIAGNOSE`           | `诊断当前链路的 EVM，给出最小建议，先保持工程不变`                       | 独立打开工程后 version/hash 不变；禁止 modify；达标不得写「偏高」                                                                                            | 原因和建议是否合理、易懂                                               |
| `MODIFY_PROJECT`     | `把当前 BPSK 改成 QPSK`                                 | 确认前 hash/recipe 不变；批准后 QPSK 且 Claim 绑定新 version；拒绝后工程不变                                                                                | 按钮语义、确认前后画布刷新                                              |
| `OBSERVE`            | `查看当前接收信号的频谱和星座图，给出主峰，只观察工程`                       | 独立打开工程后 version/hash 不变；`open_questions=[]`；主峰报告含 Hz/dBFS；回复含「主峰」；measurement Claim                                                    | 图像与主峰解释是否符合观察目标                                            |
| `HARDWARE_CONFIGURE` | `为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认` | Task 保持硬件配置；GRC 含禁用 Pluto sink 与目标频率/采样率；不是 `bpsk_awgn`；无 start。配置确认 `requested_effect=DEVICE_READ`，阶段名「配置确认」，`rf_plan` 不写 30 秒；不是发射授权 | 画布加载预览图；灰块=未 arm；确认文案不是「批准发射」；结束后「现在发射」为新一轮 deploy。真发射见 §7 |


自动测试负责可确定判定的状态、数据和副作用；人工测试负责 GTK 视觉、语言质量和真实外部设备。人工记录输入、回复、按钮选择、画布/状态栏截图和 session 路径。

## 5. Text 数据集实验

七类 Task 每类至少 10 条文本，当前变体表合计 **73** 条（`test_seven_tasks.VARIANTS`）。这是标签分类数据集，不是「每类必须走 LLM」。覆盖：完整表达、参数顺序、中文同义、英文或中英混合、缺槽、多轮补充、否定约束、复合目标、模糊指代、与相邻标签易混的表达。分类由 `test_each_variant_classifies_to_expected_task` 自动断言。不属于该表的开放复合句由 `test_workflow` / `test_plan_p12` 约束（例如不得展开 RF Stage）。

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



## 6. GUI 自动契约与人工验收



### 6.1 自动契约

```bash
PYTHONPATH=$PWD python -m unittest discover -s grc/gui/tests -v
```

自动检查 Markdown/Pango 转换，以及以下 Workflow 展示契约：

- Task、Stage、序号和等待状态；
- Completion `n/m` 与 runtime 终态；
- `run_id`、剩余时间、最大时长、return code 和末行日志；
- 时间线 Actor；
- RF 计划确认 / 配置确认和 OTA 验收按钮文案、Evidence 按钮状态；
- OTA 无附件时文案含「人工确认、附件缺失」。



### 6.2 人工视觉检查

人工确认：

- 任务名称、类型、当前 Stage、序号；
- Stage 状态、attempt、Completion `n/m`；
- 等待原因与确认/取消（RF / 空口专用文案）；
- 时间线 Seq、Event、Stage、Actor（含 origin 与 mode）；
- runtime 的 `run_id`、状态、剩余时间、return code、末行日志；
- BLE 规格摘要；仿真任务的「改规格」。



### 6.3 人工交互

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

1. Workflow 为 `HARDWARE_CONFIGURE`，`operation=deploy`，选用 `deploy_stages`（算法文档 §10）。
2. `build_ble_advertiser`：按 local name 与单信道生成 PDU/波形和 TX 流图，**sink 保持 disabled**。
3. `offline_protocol_verify`（主机 Stage，碰设备之前）：`verify_ble_packet_bits` 与 `validate_flowgraph` 均须 `valid`。独立重解析 preamble/AA/长度/CRC/Complete Local Name，并用 IQ 文件解调回环。不过这一格不得进入发现。
4. `discover_and_probe_device`：发现 Pluto 并对同一 USB URI 精确 probe。
5. 停在 `rf_plan_confirmation`。确认文案和阶段名跟 `requested_effect`：`RF_RUN` 才是「RF 计划确认 / 批准有限时长发射」；`DEVICE_READ` 是「配置确认」，不启动射频、不写默认 30 秒。状态栏提示无需点击 GRC Run。画布应加载当前预览 `.grc`；灰色 sink 表示未 arm。
6. 批准后才 `configure_device`（arm，写出 `.armed.grc`）与 `transmit_bounded`。出现新的 `run_id`、running/ready 和剩余时长。
7. LightBlue 扫描 `Deepradio27`。扫描到后点「已看到目标名称」，可「附加上传截图」；未扫描到点「未看到」。
8. `stop_and_finalize`：runtime 终止且 return code 合法。



### 7.4 通过标准

- 输入 local name 出现在 PDU、离线解码（`offline_protocol_verify` 完成）和手机扫描中；
- 离线验证发生在发现设备之前；未 arm、未 `GRC_AGENT_ENABLE_RF` 时不得 start；
- probe 绑定发现阶段同一 URI；
- RF 启动发生在用户批准之后；`arm` 仍再查 `ble_packet_valid`；
- `start_flowgraph` 返回 `running=true`、`ready=true`、`startup_health_passed=true` 和 `run_id`；
- 空口确认时 runtime 仍在 deadline 内，Evidence 引用同一 `run_id`；
- `stop_flowgraph` 返回 `running=false`、`crashed=false`；
- `runtime_status.json`、`runtime.log`、事件和 Claim 一致；
- 截图在 `final/evidence/` 并进入 Manifest；
- 能力声明不超过单信道 BLE 广播；回复与 Claim 不得写成三信道调度或独立 sniffer 已验收。

未选择截图时，记录为「人工确认、附件缺失」，不得宣称完整 Evidence Gate 通过。

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
输入 local name：`Mobicom27`（与 §7.3 示例 `Deepradio27` 不同，按随机新名称复测）


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


本轮后工程侧：Wave 1/2 拷贝与 LC 包装去冗余已落地。仍待：V5/本轮 Manifest 是否漏列设备报告的人工核验；Wave 2 剩余（handler 去 `self` 走私、`_fold` 抽出、engine 内拆）；Wave 3（catalog 退役，需 Session 迁移测试）。三信道跳频仍未实现，不得扩大 Claim。

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


| 场景                 | 期望结果                                         |
| ------------------ | -------------------------------------------- |
| RF 环境变量缺失          | start 被 Policy 拒绝；只读发现与离线建图仍可完成              |
| 离线协议校验失败           | 停在 `offline_protocol_verify`，不得 discover/arm |
| 设备发现为空             | 停在 waiting_user，无 armed 流图                   |
| probe 身份不匹配        | 配置和发射均被阻止                                    |
| 生成代码导入失败           | startup health 失败，Stage 不通过                  |
| 进程立即退出             | crashed，运行 Claim 失败                          |
| 同 session 重复 start | 第二次启动被拒绝                                     |
| 用户取消/reset/archive | emergency stop，armed 清除                      |
| 到达 deadline        | 自动停止并持久化终态                                   |
| OTA 确认时进程已停        | over_air_observed 不通过                        |
| OTA 名称不匹配          | Evidence 拒绝提交                                |
| Flowgraph 语义变化     | 原批准和 armed 状态失效                              |
| 导出目录预置其他文件         | Manifest 不收录预置文件；会话累积 ArtifactIndex 仍列出      |
| 会话目录移动             | 相对路径仍可解析                                     |


每个故障同时断言 Workflow 状态、runtime、Claim、事件和 GUI 回复。

---



## 10. 发布门槛



### Gate 1：自动回归

- §2 全量命令退出码为 0；当前记录为 Agent 156（skipped=1）+ GUI 13；
- flaky 重跑率为 0；
- skipped 项有明确平台原因。



### Gate 2：控制面金标（七类标签 + Compiler）

- §4 命令 `python -m unittest grc.agent.tests.test_seven_tasks -v` 退出码为 0（当前 23 项）；本节不是 LLM 智能门；
- 每类代表用例的自动硬条件通过，见 §4 表「自动检测」列；
- 73 条 Text 变体分类全部命中期望 Task；
- `test_plan_p12` 11 项通过（Compiler / 对照实验 / GraphPatch / MeasurementRun / pin / 开放式复合）；
- `test_ble` 覆盖 `offline_protocol_verify` 在发现设备之前完成，且 RF 默认拒绝 start；
- 否定约束与只读任务无越权动作；
- 上述自动主检绿了之后，再做 GUI 抽检（画布、文案、确认按钮）。



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

1. 在 §4 自动主检保持全绿的前提下，对七类 Task 做一轮 GUI 抽检，记录截图与 session。
2. 用随机新 local name 重复 Pluto HIL，并把手机截图写入 `final/evidence/` 与 Manifest。
3. 执行 B210 只读预检、BLE TX HIL 和 RX 频谱 HIL。
4. Wave 1/2 拷贝去冗余已落地；Wave 2 剩余与 Wave 3 完成后，重跑本节全量命令三次，确认无 `waiting/completed` 漂移（须非受限沙箱，禁止伪造 metrics）。
5. 人工核验导出 Manifest 是否仍漏列设备报告 / 验证报告。
6. 按 §9 补齐真实进程故障注入。
7. BLE 37/38/39 三信道跳频与独立 sniffer 仍是未实现能力；实现前 Gate 5 该项不登记通过。

