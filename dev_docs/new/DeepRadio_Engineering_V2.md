# DeepRadio 工程问题与修复方案 V2

> 日期：2026-08-26  
> 范围：`0826/V2` 七类 Task 与 PlutoBLE 人工实验暴露的工程问题、根因、P0～P2 修复方案和验收标准  
> 测试步骤见 `DeepRadio_Test_and_Experiment_V2.md`。

---

## 1. 本轮结论

证据范围：

```text
local/agent_sessions/0826/V2/
local/output/0826/V2/
```

不能只依据 `workflow.yaml` 的 `passed/completed` 判定成功。Task 3、6 是假通过，Task 7 明确失败；PlutoBLE 空口链路成功，但 Evidence 归档不完整。

| 用例 | Workflow 状态 | 工程验收 | 结论 |
|---|---|---|---|
| Task 1 `END_TO_END_SIM` | completed | EVM、流图和图片存在，档位与指标一致性有缺陷 | 部分通过 |
| Task 2 `TX_BUILD` | completed | 无硬件 QPSK TX，无 RF 行为 | 通过 |
| Task 3 `RX_BUILD` | completed | BER=0 缺少可信测量契约和 Claim，未补 Eb/N0 | 失败/假通过 |
| Task 4 `DIAGNOSE` | completed | 工程未变，原因判断缺少量化对照 | 部分通过 |
| Task 5 `MODIFY_PROJECT` | completed | 确认和版本更新正确，图、指标和 Claim 生命周期不完整 | 部分通过 |
| Task 6 `OBSERVE` | completed | 留有 open question，规格未知，主峰语义错误 | 失败/假通过 |
| Task 7 `HARDWARE_CONFIGURE` | failed | Pluto Builder 失败后错误回退为 BPSK+AWGN 仿真图 | 失败 |
| PlutoBLE | completed | 手机收到 `Deepradio27`，自动启停成功，截图未绑定 Evidence | 功能通过、证据部分通过 |

当前版本不满足七类 Task Gate，也不满足完整的空口 Evidence Gate。

---

## 2. 当前问题

### 2.1 Task 1：端到端仿真

已实现：正确识别 `END_TO_END_SIM`；生成 BPSK+AWGN 流图、Python、IQ、星座图和频谱图；EVM=5.89%，`evm_lt_10` Claim 为 Passed。

问题：

1. 输入时 `profile_level=student`，最终 reply/completion 变成 `expert`，档位不应自行变化。
2. 星座图、调制类型和 EVM 没有同源样本一致性校验；当前只检查标量是否达标。
3. GUI 显示的是后台静态图片，流图仍为 File Sink，用户容易误以为流图含 QT GUI。
4. Task 1、4、5 共用 session，适合连续编辑，但不能替代三个独立代表用例。

### 2.2 Task 2：发射机构建

主体通过：正确识别 `TX_BUILD`；禁止 deploy/hardware runtime；生成 `qpsk_tx.grc`；没有设备发现、RF 确认或发射事件。

遗留问题：

- `qpsk_tx_rx.bin` 的 `rx` 与实际 TX 输出语义不符；
- 结构正确性主要依赖 Completion，缺少独立结构 Claim。

### 2.3 Task 3：接收机构建与 BER

Workflow 标为 passed，但不应验收：

1. 缺 Eb/N0 时没有询问，也没有记录测试要求的 8 dB。
2. GUI 显示 `BER 0`，但 state 中没有 BER Claim。
3. Completion 仅要求 BER 有限且小于 0.45，并检查 TX/RX probe 名称；未验证错误数、比较位数、捕获期、对齐方法和置信度。
4. BER 在接近整段数据的范围搜索最佳延时，并允许全局反相后取最小值，存在择优偏差，可能产生虚假的低 BER。
5. recipe 中 `max_rate_deviation` 被报告未知后忽略，结构仍判通过。
6. TX reference 使用 0～255 字节再展开 bits，未证明与调制器和接收判决的位序一致。
7. 没有持久化 BER、bit errors、compared bits、delay 和 probe 身份等审计字段。

相关代码：

```text
grc/agent/runtime/simulate.py
grc/agent/tools/sim_tools.py
grc/agent/workflow/completion.py
grc/agent/knowledge/recipes.py
```

### 2.4 Task 4：只读诊断

原工程、Project version 和 hash 没有改变，但：

1. “主因是 AWGN”主要由 recipe 推断，没有通过关闭噪声、频偏、定时偏差等对照实验量化贡献。
2. 没有 diagnosis Claim/Evidence，结论只能从回复读取。
3. 没出现修复确认，未覆盖“用户拒绝修复后工程保持不变”的交互分支。

### 2.5 Task 5：修改已有工程

确认前未改图；确认后 BPSK 改为 QPSK，Project version 从 1 增至 2。

问题：

1. QPSK 星座图呈现多个散乱簇，与 EVM=5.52% 的直观表现不一致。
2. 原 Claim 被直接更新到 version 2，缺少 `stale → re-evaluating → passed/failed` 生命周期。
3. 实现是切换预置 `qpsk_awgn` recipe，不是任意工程的通用最小 diff。
4. “其余条件保持一致”缺少结构 diff 和参数保持清单 Evidence。

### 2.6 Task 6：只读观察

这是明确的假通过：

1. state 仍有 `open_questions=["使用哪种调制方式？"]`，但 Intent 为 `missing_slots=[]`，Workflow 又 completed。
2. GUI 规格显示 `? → ? → ?`，canvas context 未投影到 Spec。
3. 主峰约为 `3505.934 @ bin 0`，无频率/幅度单位；bin 未结合采样率、FFT 长度和 shift 转为 Hz。
4. 线性幅值没有转换或声明为 dB/dBFS。
5. 回复没有直接给出用户要求的主峰位置和幅度。
6. 没有 measurement Claim。
7. 观察的是 Task 1 的 BPSK 工程，不是测试文档要求的接收工程，实验前置也不合格。
8. Observe 将副本写入本 session 的 `final/`，需要明确它不是工程修改。

相关代码：

```text
grc/agent/service/adapter.py
grc/agent/workflow/completion.py
grc/agent/tools/sim_tools.py
grc/gui/ClaimsPanel.py
```

### 2.7 Task 7：仅配置 PlutoSDR

实际链路：

```text
Pluto Builder
→ valid=false / Port is not connected
→ DeepAgent 改调用 design_link
→ 生成 bpsk_awgn.grc
→ hardware_endpoint_present=false
→ radio_parameters_match=false
→ 后续 hardware Stage 全部 pending
```

问题：

1. 用户未指定 modulation，规则层却给出 `missing_slots=[]`，由 DeepAgent 临时询问 BPSK，Intent、Spec 和对话状态不一致。
2. Pluto Builder 生成未连接端口；内部 validate 不能替代真实 GRC compile。
3. 硬件 Builder 失败后错误回退到 `bpsk_awgn`，产物歪曲了硬件目标。
4. 错误仿真产物进入 final/Manifest。
5. Task 7 截图与 Task 3 截图 SHA-256 相同，是错误复制，不能作为证据。

相关代码：

```text
grc/agent/service/adapter.py
grc/agent/tools/hardware_tools.py
grc/agent/knowledge/recipes.py
grc/agent/workflow/completion.py
```

### 2.8 PlutoBLE

真实功能已通过：

- 手机收到 `Deepradio27`；
- PDU、离线验证与手机名称一致；
- 发现并 probe `usb:2.4.5`；
- 用户批准后才启动 RF；
- `run_id=run-9bacf77c5a16`；
- 启动返回 `ready=true`、`startup_health_passed=true`；
- OTA 确认时进程仍在 deadline 内；
- 主动停止，`return_code=0`、`crashed=false`；
- 无需点击 GRC Run。

Evidence Gate 未通过：

1. 手机截图只在 output，没有进入 `final/evidence/`。
2. OTA Claim 的 `artifact`、`sha256`、`evidence_id` 为空。
3. Manifest 不含手机截图。
4. `runtime.log` 为 `UUU`，存在 underrun；手机收到不等于运行质量完全健康。
5. 当前只发射 CH37，不能宣称三信道调度完成。
6. GUI 需要区分“本次启动曾 ready”和“当前已停止”。

---

## 3. 根因

### 3.1 Completion 只检查存在性

多个 Completion 将“有文件、有字段、有数值”误当成目标已经可信完成：

- BER 低于宽松上限即完成；
- Observe 有图和 peak 数值即完成；
- open question 与 completed 不互斥；
- Claim 不是所有成功条件的硬证据；
- hardware task 在执行阶段可以产生错误种类的流图。

### 3.2 硬件任务缺少不可降级的产物契约

`HARDWARE_CONFIGURE` 只在事后检查 endpoint。硬件 Builder 失败后，LLM/DeepAgent 仍可选择普通基带 recipe，导致 Task 类型没变但产物语义被歪曲。

### 3.3 Intent、Spec、Project、Profile 不是统一事务

表现为：

- `missing_slots=[]` 与 open question 并存；
- canvas 已知 BPSK，但 Spec 未知；
- profile 从 student 跳成 expert；
- Claim、Workflow revision 与 Project version 的失效/重验链不完整。

### 3.4 指标缺少测量契约

BER、频谱峰和 EVM 只是标量，没有统一保存 probe、样本区间、单位、算法、有效性条件、Project version 及其与图像的绑定关系。

### 3.5 Evidence 未闭环

output 中存在截图不等于系统已有 Evidence。附件上传、复制、hash、Evidence、Claim 和 Manifest 没有形成原子流水线。

---

## 4. 推荐修复方案

### 4.1 P0：阻止假通过与错误产物

#### P0-1 Completion 全局不变量

在 Workflow commit 前强制：

```text
open_questions 非空                    => 禁止 completed
missing_slots/validation_errors 非空   => 禁止 autonomous build completed
requested BER                          => 必须有有效 BER report + BER Claim
requested spectrum peak                => 必须有带单位 peak report + Claim
hardware_configure TX                  => 必须有目标硬件 sink
hardware_configure RX                  => 必须有目标硬件 source
Claim.project_version 过期             => 不得支撑当前完成条件
```

Completion 应返回结构化 failure code，而不是只有布尔值：

```json
{
  "passed": false,
  "missing": ["ber_claim"],
  "invalid": ["open_questions_not_empty"],
  "evidence": []
}
```

#### P0-2 硬件 ArtifactContract

构建前形成契约，例如：

```json
{
  "task_type": "HARDWARE_CONFIGURE",
  "direction": "tx",
  "hardware": "pluto",
  "required_blocks": ["iio_pluto_sink"],
  "required_parameters": {
    "center_frequency": 2402000000,
    "sample_rate": 2000000
  },
  "forbidden_success_artifacts": ["baseband_file_sink_only"]
}
```

执行约束：

- 硬件 Builder 失败后进入 retry/waiting_user；
- 可重试同类硬件 Builder，不能以 `bpsk_awgn/qpsk_awgn` 成功降级；
- LLM 只能选择满足 contract 的 Tool；
- 不符合 contract 的文件放入 `work/rejected/`，不得进入 final/Manifest；
- `ResultEnvelope.ok=true` 仍须经过 contract validator。

#### P0-3 修复 Pluto 通用 Builder

1. 输出 block 端口和 connection graph；
2. Builder 返回前运行结构校验；
3. 写出 `.grc` 后调用 GNU Radio compiler 做二次验证；
4. 编译失败禁止 finalize；
5. 对 Pluto/B210 做参数化测试，变化频率、采样率、调制和设备身份，不能写死本次值。

#### P0-4 修正硬件补槽

- 仅记录参数可生成明确标记的 `configuration_only` 禁用模板；
- 用户要求发射流图时必须明确基带输入或调制方式；
- 补槽写回 Intent、Spec、slot source 和 workflow revision；
- 禁止 Stage 内私自补出未持久化语义。

### 4.2 P1：可信指标与统一状态

#### P1-1 BER 测量协议

不得写死本次 BER 结果，应修正通用算法：

1. 定义 TX/RX byte 和 bit order；
2. 使用同步字、帧号或相关峰确定 delay；
3. 固定并记录捕获期；
4. 限制 delay 搜索窗口；
5. 反相只能由相位歧义判决触发并记录；
6. 设置最小比较位数；
7. 保存 bit errors 和 compared bits；
8. Eb/N0/SNR 缺失时补槽；
9. 重复运行报告均值和波动。

标准报告：

```json
{
  "metric": "ber",
  "value": 0.001,
  "bit_errors": 100,
  "compared_bits": 100000,
  "alignment_method": "preamble_correlation",
  "delay_bits": 137,
  "discarded_bits": 512,
  "inversion_applied": false,
  "ebn0_db": 8.0,
  "tx_probe": "tx_sink",
  "rx_probe": "sink",
  "project_version": 1,
  "valid": true
}
```

BER Claim 必须引用报告和两个 probe artifact。

#### P1-2 频谱峰测量协议

```json
{
  "metric": "spectrum_peak",
  "frequency_hz": 0.0,
  "magnitude_dbfs": -12.3,
  "fft_bin": 2048,
  "fft_size": 4096,
  "sample_rate": 1000000,
  "window": "hann",
  "fft_shifted": true,
  "dc_excluded": true,
  "valid": true
}
```

bin 必须转换为 Hz；幅值使用 dBFS 或声明单位；记录 DC 处理；GUI 直接回答主峰；Completion 依赖有效报告和 measurement Claim。

#### P1-3 EVM 与图片同源

- 指标和图片使用同一 probe、运行和样本区间；
- 统一丢弃 transient；
- 按调制阶数检查聚类数量和有效点比例；
- 图像元数据记录 project version、probe、sample range；
- 图与指标明显冲突时不得完成。

#### P1-4 状态事务

每轮原子更新：

```text
Intent slots/missing_slots
→ Spec decisions/open_questions
→ Project context
→ Profile
→ Workflow revision
→ Claim invalidation
```

不变量：

- missing slots 与 open questions 对应；
- profile 只能由用户操作或明确自适应事件改变；
- canvas 槽位以 `source=canvas` 投影到 Spec；
- Project version 变化后受影响 Claim 先 stale，再重验；
- reply/events/state/workflow 使用同一快照。

### 4.3 P2：诊断、Evidence 与 GUI

#### P2-1 量化诊断

对可仿真工程运行临时对照：

```text
baseline
→ 降低 AWGN
→ 清零频偏
→ 恢复理想定时
→ 分别重测
→ 按改善量排序原因
```

对照使用临时快照，不写回用户工程；用户确认后才提交修复。

#### P2-2 Evidence ingest

上传截图时原子完成：

1. 验证类型和大小；
2. 复制到 `final/evidence/`；
3. 计算 SHA-256；
4. 生成 `evidence_id`；
5. 绑定 workflow_id、run_id、expected_name、observed_at；
6. 更新 OTA Claim；
7. 写入 Manifest；
8. 记录事件。

仅点击“已看到”而不上传附件时，显示“人工确认通过，附件 Evidence 缺失”，不得宣称 Gate 5 完整通过。

#### P2-3 Runtime 质量

- 解析 GNU Radio `U/O` 为结构化计数；
- 使用可配置阈值，不因单个 `U` 机械失败；
- runtime Claim 保存 startup ready、当前终态、时长和 underrun；
- GUI 分开显示“曾 ready”“当前状态”“质量告警”；
- CH37/38/39 调度完成前只声明单信道能力。

#### P2-4 GUI 与实验证据

- 静态图标记“离线测量”，QT block 标记“实时 GUI”；
- open question 存在时显示 waiting；
- 截图带 case id、session id 和时间；
- 导出前检测重复图片 hash；
- 连续场景 Task 1→4→5 与七类独立代表场景分别记录。

---

## 5. 实施顺序

### 第一批 P0

1. 收紧 Completion；
2. 引入 ArtifactContract；
3. 禁止硬件任务降级为 File-Sink-only 流图；
4. 修复 Pluto Builder 端口；
5. 失败产物不进入 final/Manifest；
6. 修复硬件补槽。

主要文件：

```text
grc/agent/workflow/completion.py
grc/agent/workflow/engine.py
grc/agent/service/adapter.py
grc/agent/tools/hardware_tools.py
grc/agent/knowledge/recipes.py
grc/agent/tools/registry.py
```

### 第二批 P1

1. 重写 BER 报告；
2. 修正频谱峰频率轴和单位；
3. 统一 EVM/图片样本窗口；
4. 引入状态原子事务；
5. 补齐 Claim stale/revalidate。

主要文件：

```text
grc/agent/runtime/simulate.py
grc/agent/tools/sim_tools.py
grc/agent/tools/design_link.py
grc/agent/state/claim_store.py
grc/agent/state/shared_state.py
grc/agent/service/adapter.py
grc/gui/ClaimsPanel.py
```

### 第三批 P2

1. 量化诊断；
2. Evidence ingest 与 Manifest；
3. runtime underrun 统计；
4. GUI 状态和证据检查；
5. BLE 三信道调度另立能力项。

---

## 6. 必增自动测试

### Completion 负向测试

- open question 非空不得 completed；
- 请求 BER 但无 BER Claim 不得通过；
- BER 缺 compared bits 或 TX/RX probe 不得通过；
- peak 缺单位或 FFT 参数不得通过；
- Pluto/B210 流图缺目标硬件 endpoint 不得通过；
- stale Claim 不得支撑当前版本。

### BER 参数化测试

覆盖随机 payload、delay、捕获期、bit order、相位反转、Eb/N0、长度和 BPSK/QPSK。根据注入错误数计算期望 BER，禁止写死一次实验结果。

### 硬件 Builder 测试

覆盖 Pluto/B210、TX/RX、多组频率与采样率、不同基带 source、validate+compile；断言 Builder 失败后无基带 recipe fallback，rejected artifact 不进 Manifest。

### 状态一致性测试

- profile 稳定；
- 多轮补槽同步更新 Intent/Spec/revision；
- canvas context 补充 Observe/Diagnose；
- 修改后旧 Claim stale、新 Claim 绑定新版本；
- session reload 后一致。

### Evidence 测试

- 截图进入 `final/evidence/`；
- hash、Evidence、Claim、Manifest 一致；
- run_id/名称不匹配时拒绝；
- 仅人工确认时标记附件缺失；
- 导出后相对路径有效。

---

## 7. 修复后验收标准

- Task 1：profile 不跳变，EVM 与图片同源；
- Task 2：无硬件、无 RF，有结构 Claim；
- Task 3：缺 Eb/N0 时补槽，BER 含错误数、比较位数、同步方法和 Claim；
- Task 4：诊断有对照指标，拒绝修复后 hash/version 不变；
- Task 5：确认前不改图，确认后 version 增加，旧 Claim stale、新 Claim 重验；
- Task 6：无悬空 open question，明确回答主峰 Hz 与 dBFS，原工程不变；
- Task 7：生成真实 Pluto Sink 流图并停在确认，File-Sink-only fallback 必须失败；
- PlutoBLE：截图进入 Evidence，Claim 与 Manifest 路径/hash 一致，underrun 被记录，单信道能力不冒充三信道。

只有自动回归、七类独立代表用例、硬件安全链和 Evidence 链全部一致通过，才可登记当前版本发布 Gate 通过。
