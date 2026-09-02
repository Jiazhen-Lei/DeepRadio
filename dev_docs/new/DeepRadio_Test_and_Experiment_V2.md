# DeepRadio 测试与实验 V2

> 更新日期：2026-08-31<br>
> 当前证据：当前工作区代码全量自动回归（agent tests `250 passed, 1 skipped`；GUI tests `20 passed`）与 2026-08-30 PlutoSDR 真机冒烟；`local/agent_sessions/0827|0828` 历史目录仅作版本基线<br>
> 环境：所有自动测试和 GUI/HIL 实验均使用 `gnuradio` Conda 环境<br>
> 原则：历史实验保留为版本基线；代码发生行为相关修改后，必须在新目录重跑，不能覆盖旧记录。

---

## 0. 2026-08-31 V6 泛化链路验收

新增/更新的自动契约覆盖：

- 等价 TurnIR 不依赖用户原句；活动自然语言轮次只调用一次 semantic LLM，并在同一结果中携带 relation 与参数增量。
- completed preview 后带 operation 的 follow-up 建立 deploy Workflow；无 checkpoint 的纯 confirmation 保持安全拒绝。
- planner proposal 不得改写已有 Stage 的 producer/dependency/completion，不得提高 effect；未知 action、悬空 transition、缺 probe/grant/stop 的 RF plan 必须拒绝或无损回退。
- host preflight 成功但 discovery 失败时，不得产生 detected/probed/configured 物理设备事实；RF start 依赖 discovery + probe + grant，OTA 事实绑定同一 run id。
- presenter 边界的默认可见文本必须为英文，且不包含 `workflow_id/task_type/stage_id/revision/completion` 或日志式 `Intent:/Completed:` 字段。

执行分层：

1. 无头核心：`python -m unittest grc.agent.tests.test_plan_p12 grc.agent.tests.test_workflow`
2. 完整 GNU Radio block library：`grc.agent.tests.test_hardware/test_ble/test_seven_tasks`
3. GTK 环境（需 PyGObject `gi`）：`grc.gui.tests.test_workflow_presenter`
4. HIL：连接目标 SDR 后验证 identity mismatch、probe failure、RF disabled、bounded start/stop 与独立 OTA observation。

## 0. 2026-08-30 V5 增量：硬件意图对齐与探测链路回归

### 0.1 新增契约测试

| 位置 | 测试 | 验证内容 |
|---|---|---|
| `test_workflow.py` | `test_llm_device_alias_merges_onto_hardware` | LLM 用 `device` 键回答时归一到 `hardware`，source=llm，不残留 `device` 键 |
| `test_workflow.py` | `test_seed_hardware_survives_llm_omission_with_literal_evidence` | LLM 漏提取时，有字面证据的 hardware/protocol 种子存活，missing 判定不再追问 |
| `test_workflow.py` | `test_llm_answer_still_wins_over_seed_fallback` | LLM 显式回答始终优先于种子兜底 |
| `test_workflow.py` | `test_specification_merges_device_alias_into_single_hardware_row` | 规格卡对 `device`+`hardware` 只渲染一行 `hardware` |
| `test_hardware.py` | `CrossFamilyDiscoveryTest.test_b210_miss_reports_a_present_pluto` | 期望 B210 未找到时跨家族扫描发现 Pluto，返回 `devices` + `mismatch_hint` |
| `test_hardware.py` | `CrossFamilyDiscoveryTest.test_hit_on_expected_family_skips_cross_scan` | 期望家族命中时不做任何额外扫描（零开销） |
| `test_hardware.py` | `test_repeated_hardware_failure_escalates_to_llm_diagnosis` | 连续两次失败后重试提示附加 `Diagnosis:` 行，LLM 恰好调用一次 |
| `test_hardware.py` | `test_successful_recheck_resets_failure_counter` | 发现设备后 `hw_retry_failures` 清零 |
| `test_hardware.py` | `test_retry_reports_missing_device_and_does_not_rerun`（升级） | 重试预检把 intent 的 `device_type=pluto` 传入 `discover_devices` |

同时保留 V4 回归守护 `test_llm_omission_drops_nonconflicting_rule_candidates`：无字面证据的 regex 猜测不得因 LLM 漏提取而存活——种子兜底不得破坏该契约。

### 0.2 回归与冒烟结果（2026-08-30，`gnuradio` Conda 环境）

```bash
python -m unittest discover -s grc/agent/tests -p 'test_*.py'
```

- agent tests：`234 passed, 1 skipped`（B210 HIL 条件跳过）。
- 真机冒烟：`discover_devices(device_type="plutosdr")` → 执行 `iio_info -S usb` → `device_found=True, identity=usb:2.4.5`。对应 V5 会话中"3 次 `uhd_find_devices` 失败"的场景现在一次命中。
- GUI 本轮未改动，沿用 `17 passed` 基线；GUI/HIL 人工实验（第 3、4 节）不因无头测试通过而免除。

---

## 1. 测试类型与顺序

| 类型 | 适用内容 | 顺序 |
|---|---|---|
| 完全自动 | schema、Intent 锁定规则、槽位别名归一与种子兜底、跨家族扫描、Plan Compiler、effect、Completion、Manifest、测量算法、无授权副作用 | 每次改代码首先运行 |
| 自动后人工 | 七类 GUI、回复事实一致性、图像可读性、Workflow Inspector、修改 diff、真实设备 mismatch 提示与 LLM 诊断文案 | 自动回归全绿后运行 |
| 必须人工或独立接收端 | 天线/接线、手机 LightBlue、真实空口、GTK 交互观感 | 软件和设备预检均通过后最后运行 |

总顺序：

```text
静态/单元
→ MainAgentRuntime 集成
→ session replay/manifest
→ 无 RF GUI 七类回归
→ 硬件 discover/probe（含跨家族 mismatch 分支）
→ RF 安全和 stop 故障注入
→ 有界 Pluto BLE
→ OTA 人工/独立 Evidence
```

不得为了省时间跳过离线 BLE、device identity、RF authorization 和 stop Gate。

---

## 2. 自动回归教程

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

已落地的自动覆盖（按文件）：

| 测试位置 | 覆盖 |
|---|---|
| `grc/agent/tests/test_workflow.py` | LLM intent/plan trace；未配置与异常 fallback；`signal_source_scope`；checkpoint purpose；未知 predicate 不通过；槽位别名归一；字面证据种子兜底；LLM 优先级；规格卡去重（V5） |
| `grc/agent/tests/test_plan_p12.py` | 工具 effect 上界；LLM 不得删除安全尾部；开放复合文本不被七类标签歪曲 |
| `grc/agent/tests/test_seven_tasks.py` | 七类主路径；事实化回复；GraphPatch 优先；诊断报告；离线/实时来源 |
| `grc/agent/tests/test_hardware.py` | 版本指纹；可复现导出；路径迁移；RF active/ever；underflow quality；三种 checkpoint；重试预检传参、跨家族扫描、LLM 失败诊断、计数复位（V5） |
| `grc/agent/tests/test_ble.py` | 通用 BLE 算法；Evidence grade；无附件不能达到最高 Gate；单信道能力声明 |
| `grc/gui/tests/` | 无 `?` 摘要；warning/Failed Claim；配置交付、RF 授权、OTA 按钮文案 |

### 2.1 必测的反过拟合样例

除七条代表文本外，每类至少准备同义、顺序变化、中英混合、否定、缺槽、多轮和复合目标。特别加入：

- "分析这个工程为什么误码高，但别动我的图"；
- "先做一个可保存的 Pluto 发射预览，今天不要上空口"；
- "我已经有一个手工改过的流图，只把调制阶数换掉"；
- "看一下天线口现在收到的频谱"和"看一下当前仿真文件的频谱"；
- "生成 BLE 波形但不要接设备"；
- "现在停止刚才的发射"；
- "I want to use plutosdr to transmit ble signal."（V5 原句：验证别名归一、种子兜底与探测直达）；
- 期望 B210 但只接 PlutoSDR 的设备不匹配场景（V5：验证 mismatch 提示而非盲重试）；
- 未出现七类关键词但具有同等目标的开放表达。

断言目标、能力、effect、来源域、决策边界和禁止事件，不对完整回复文本做 exact match。

---

## 3. 七类 GUI 回归教程

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
- 人工检查：建议与证据一致，且明确"只提出建议、没有应用修改"。

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
- 人工检查：回复不出现"实时接收"或未被数据支持的载频结论。

另做实时分支：`查看当前 PlutoSDR 天线口接收信号的实时频谱`。该分支必须要求设备并走 RX hardware path，不得回退离线 AWGN。

### Task7：硬件配置

- 输入：`为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认`
- 自动检查：生成禁用 RF 的安全预览；发现并 probe 精确设备；checkpoint purpose=`config_handoff`；无 arm/start，未授予 `RF_RUN`。
- 人工检查：默认诊断音清楚标为系统安全默认；按钮是"确认已保存/继续发射"，不是"批准发射"；probe warning 可见。
- V5 追加：拔掉设备后连续 Retry 两次——第一次提示为通用/矛盾陈述，第二次附加 `Diagnosis:` 诊断行；重新插入设备后一次 Retry 即恢复且计数清零。

---

## 4. PlutoSDR BLE 端到端教程

### 4.1 自动与硬件预检

```bash
conda activate gnuradio
iio_info -S
PYTHONPATH=$PWD python -m unittest grc.agent.tests.test_ble -v
PYTHONPATH=$PWD python -m unittest grc.agent.tests.test_hardware -v
```

要求：`iio_info -S` 无 fatal error；系统识别的设备类型和 identity 与用户表达一致；BLE 离线包/波形校验通过；stop/emergency_stop 和 duration cap 测试通过。

### 4.2 启动与输入

在符合当地法规的屏蔽或低功率实验条件下：

```bash
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
3. 点击"确认有限时长发射"；
4. 用 LightBlue 扫描 `DeepRadioTest`；
5. 在 OTA checkpoint 选择看到/未看到，并上传截图；
6. 确认状态显示 `rf_active=false`、`runtime.status=stopped`。

### 4.3 通过分级

| 等级 | 条件 |
|---|---|
| 控制面通过 | 离线校验、设备 identity、授权、bounded start、stop、return code 均正确 |
| 产品目标通过 | LightBlue 实际看到目标 local name |
| 论文 Evidence 通过 | 截图或独立接收端证据有 artifact、hash、run_id 和目标名称绑定 |
| 流质量 clean | 无 underrun/overrun；否则只能 `passed_with_warning` |

手机收到信号不能抵消 underflow；underflow 也不能反向否定手机确实收到。二者必须分别报告。

---

## 5. 人如何检查自动测试确实成功

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
7. RF session 的 `run_id` 在授权、启动、OTA 和停止记录中一致；
8. 硬件重试场景中，探测命令与意图设备家族一致（选 PlutoSDR 不得出现 `uhd_find_devices`），失败 ≥2 次时重试提示含 `Diagnosis:`（V5）。

---

## 6. 故障注入与发布 Gate

必须自动覆盖：LLM 未配置、LLM 超时/非法 JSON、未知 PlanNode、工具失败、Manifest 文件缺失、工程被外部修改、设备未连接、连接设备与请求不一致（应产生跨家族 mismatch 提示而非盲重试，V5）、RF 开关关闭、arm 失败、start 崩溃、underflow、用户拒绝、OTA 未看到、停止超时和 emergency stop。

发布 Gate：

1. 当前版本全部自动测试退出码为 0；
2. 七类 GUI 回归在同一 run metadata 基线上通过；
3. 回复、状态、Artifact 和 Claim 一致；
4. 修改任务默认使用 patch，诊断与观察不改变工程；
5. RF 未授权绝不 start，停止能力在故障注入中通过；
6. Pluto BLE 在同一版本上完成 HIL；
7. 论文空口声明具有附件或独立接收端 Evidence；
8. 所有 warning、Failed Claim 和 Evidence 不完整均在 GUI 可见。

只有八个 Gate 同时满足，才能用"最新版本已通过七类任务与 Pluto BLE 端到端实验"的表述。
