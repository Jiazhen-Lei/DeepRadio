# DeepRadio 上层任务分类、测试策略与 CHI 2027 研究方案 V3

> 日期：2026-08-26  
> 范围：GNU Radio 上层任务分类、软硬件组合、诊断维度、动态 Workflow 规则、自动/人工测试顺序，以及 CHI 2027 研究定位  
> 性质：目标架构与研究方案。文中使用“当前已实现”“部分实现”“目标能力”区分现状和规划，不能把目标能力直接写成现有产品结论。

---

## 0. 核心结论

当前七类 Task 可以继续作为用户可理解的一级任务名称，但不能继续作为七个互斥且固定的流程模板。

DeepRadio 应采用下面的上层结构：

```text
Workflow
  = 用户目标图
  + 一个或多个 Task
  + 每个 Task 的场景维度
  + 可组合 Capability
  + 安全策略
  + Evidence 验收条件
```

其中：

```text
Task 决定用户最终要得到什么；
Scenario 决定任务在哪种环境中执行；
Stage 决定完成任务必须经过哪些步骤；
Subagent 决定由谁执行步骤；
Skill 决定采用什么方法；
Tool 执行确定性动作；
Completion Contract 决定是否真的完成。
```

七类 Task 应调整为：

1. `END_TO_END_SYSTEM`：端到端系统构建与验证；
2. `TX_BUILD`：发射机设计；
3. `RX_BUILD`：接收机设计；
4. `DIAGNOSE`：诊断；
5. `MODIFY_PROJECT`：修改已有工程；
6. `OBSERVE`：观测与测量；
7. `HARDWARE_OPERATE`：硬件生命周期与受控运行。

其中第 7 类不应再吞掉第 1～3 类的设计目标。硬件是跨 Task 的执行维度，而不是看到 `B210`、`Pluto`、`SDR` 等词就覆盖其他任务类型。

例如：

```text
“用 B210 发射 BLE，让手机扫描到”

= TX_BUILD：构建 BLE 发射机
→ 离线协议与结构验证
→ HARDWARE_OPERATE：发现、绑定、配置、确认、有限发射
→ OBSERVE：记录手机或 Sniffer 空口 Evidence
→ HARDWARE_OPERATE：停止
```

---

## 1. 当前泛化不足的根因

### 1.1 单标签无法表达复合目标

当前 Workflow 主要保存一个 `task_type`。当一句话同时包含“设计、校验、接硬件、运行、观察”时，只能把所有 Stage 拼到一个主 Task 后面。这会带来：

- 主目标被硬件关键词覆盖；
- Task 的完成语义不清；
- 一个步骤失败后可能进入错误类型的恢复路径；
- 无法单独回滚、取消或重试某一个子目标；
- GUI 只能展示一个 Task，无法忠实表达复合任务。

### 1.2 Task 与执行环境混在一起

`TX_BUILD` 当前主要按纯仿真理解；真实设备通常转入 `HARDWARE_CONFIGURE`。但从 GNU Radio 使用者视角，下面三句话的主任务都是 TX 设计：

```text
生成 QPSK IQ 文件。
生成带 UHD Sink 但不要打开设备的 B210 发射流图。
用已经连接的 B210 有限发射 QPSK。
```

它们的区别是 `execution_context`、`hardware_access` 和 `safety_level`，不是主任务不同。

### 1.3 关键词枚举代替了语义结构

当前规则只显式枚举少量调制、指标、硬件和动作。增加文本变体可以提高已有表达的命中率，但不能解决新协议、新设备、新信号源和复合目标的泛化问题。

上层识别必须从“包含哪个词”升级为：

```text
动作 + 对象 + 信号方向 + 执行环境 + 硬件访问 + 安全约束 + 成功条件
```

### 1.4 缺槽规则没有按场景变化

不同接收场景需要的参数不同：

- 自包含 BER 仿真需要 TX reference 和信道条件；
- 文件 IQ 解调需要文件格式、采样率和中心频率元数据；
- 实时 SDR RX 需要设备 identity、频率、采样率、通道、增益；
- 未知信号侦测不一定需要调制类型；
- BLE 扫描需要协议和信道信息，但不能要求通用 BPSK 槽位。

因此 `required_slots` 必须由 Task 的具体 scenario 决定。

### 1.5 诊断覆盖面过窄

GNU Radio 故障可能来自环境、依赖、驱动、设备身份、USB/网络、接线、流图结构、调度器、RF、同步、PHY、协议、测量方法或外部应用。只根据 EVM/频谱阈值给建议无法构成完整诊断。

---

## 2. Task、Stage、Subagent、Skill 的边界

### 2.1 何时切分 Task

满足以下任一条件时，Planner 应考虑切分新 Task：

1. 产生独立产物；
2. 具有独立验收标准；
3. 中间必须由用户表态；
4. 跨越安全边界，例如从离线设计进入真实 RF 发射；
5. 可以独立失败、重试或回滚；
6. 后续步骤可以取消而不影响前一步成果；
7. 需要切换到另一种外部资源或执行环境。

不应因为以下原因切 Task：

- 换了 Subagent；
- 调用了新的 Skill；
- 内部更换了 Tool；
- 仅增加一个普通结构检查；
- 仅改变回复语言风格。

构建后的必要结构校验通常是同一个 Task 的 Stage。如果用户明确要求“先生成，等我确认后再测量”，校验或观测才应成为独立 Task/Checkpoint。

### 2.2 四层职责

| 层级 | 职责 | 是否对用户可见 |
|---|---|---|
| Task | 用户目标、产物、验收和安全边界 | 是 |
| Stage | 规格、构建、校验、运行、恢复步骤 | 是，支持折叠 |
| Subagent | 负责执行某类步骤 | 可显示名称，但不是控制对象 |
| Skill/Tool | 方法知识和确定性动作 | 默认隐藏，专家模式可查看摘要 |

Subagent 与 Skill 必须允许多对多：一个 Subagent 可以按 Stage 调用多个 Skill；同一个 Skill 也可以服务多个 Subagent。

---

## 3. 场景维度模型

每个 Task 必须附带场景维度。七类 Task 只表示 `primary_goal`。

| 维度 | 典型候选值 |
|---|---|
| 主目标 | 系统构建、TX、RX、诊断、修改、观测、硬件操作 |
| 信号方向 | TX、RX、收发一体、半双工、全双工、多通道/MIMO |
| 信号域 | bit、byte/PDU、symbol、complex baseband、IF、RF |
| 数据来源 | 随机源、文件、网络、音频、实时数据、录制 IQ、外部空口 |
| 数据终点 | GUI、文件、网络、解码器、SDR Sink、应用层 |
| 执行环境 | 纯离线、仿真、文件回放、数字环回、线缆环回、屏蔽箱、OTA |
| 硬件关系 | 无硬件、仅面向硬件建图、只读探测、实时 RX、真实 TX |
| 波形/协议 | 通用数字调制、模拟调制、BLE、Wi-Fi、LoRa、用户自定义 |
| 信道 | 无信道、AWGN、多径、频偏、时钟偏差、录制信道、真实空口 |
| 运行模式 | 有限批处理、有限实时、连续、突发、定时、循环 |
| 指标 | BER、SER、PER、EVM、SNR、RSSI、频谱、星座、眼图、CRC、吞吐率 |
| 用户动作 | 新建、修改、观察、诊断、部署、停止、恢复、更换设备 |
| 安全等级 | 纯软件、只读硬件、真实 RX、线缆 TX、屏蔽 TX、OTA TX |
| 验收方式 | 自动指标、结构校验、运行日志、外部仪器、人工 Evidence |
| 禁止条件 | 不改图、不接设备、不运行、不发射、不覆盖、不自动修复 |

协议、调制和硬件型号应由开放 Catalog 扩展，不能通过不断添加 Task 类型扩展。

---

## 4. 七类上层 Task 的完整定义

## 4.1 Task 1：端到端系统构建与验证

建议内部名称由 `END_TO_END_SIM` 调整为 `END_TO_END_SYSTEM`。仿真只是其一个 scenario。

### 场景范围

1. 纯基带端到端仿真；
2. 文件输入到文件输出；
3. 软件 TX 到软件 RX 数字环回；
4. 单 SDR 双通道或自收自发；
5. 两台 SDR 线缆环回；
6. 两台 SDR 屏蔽环境或 OTA 收发；
7. Payload/PDU/PHY/Decode 的协议端到端；
8. Audio、Socket、ZMQ 等实时流式链路；
9. 多载波、多通道或 MIMO 链路；
10. 参数扫描、Monte Carlo 或鲁棒性验证。

### 必须识别的规格

- 输入和输出；
- TX/RX 方向及是否双工；
- 调制或协议；
- symbol rate、sample rate、SPS；
- 信道或物理连接方式；
- 编码、帧、同步要求；
- 单设备还是双设备；
- 运行方式和时长；
- 指标、阈值和统计置信度；
- 是否允许真实硬件访问和 RF 发射。

### 标准 Stage

```text
spec_alignment
→ architecture_plan
→ build_tx_rx
→ structural_validate
→ execute_in_selected_context
→ measure
→ evaluate_claims
→ finalize
```

### 完成条件

- 流图结构有效；
- TX/RX 数据类型、采样率和协议相容；
- 实际执行环境与用户要求一致；
- 指标 Evidence 绑定当前工程版本；
- 硬件场景绑定到唯一设备 identity；
- OTA 场景有独立空口 Evidence；
- 没有把“软件运行成功”写成“端到端空口成功”。

## 4.2 Task 2：发射机设计

Task 2 应覆盖完整 TX 设计空间，而不是只覆盖 File Sink 仿真。

### 场景范围

| Scenario | 含义 |
|---|---|
| `baseband_tx` | 调制后保存或输出 IQ，不涉及 SDR |
| `if_tx` | 生成数字中频信号 |
| `hardware_targeted_tx` | 流图含指定 SDR Sink，但不访问设备、Sink 保持禁用 |
| `connected_tx_config` | 发现并配置真实设备，但不启动 RF |
| `bounded_rf_tx` | 用户确认后有限时长发射 |
| `burst_tx` | Tagged Stream、突发或定时发射 |
| `continuous_tx` | 连续发射，必须有监控和停止机制 |
| `multi_channel_tx` | 双通道/MIMO/多载波发射 |
| `protocol_tx` | BLE、Wi-Fi 等协议帧发射 |

### TX 设计维度

- Payload 或源数据；
- 帧结构、编码、加扰和 CRC；
- 调制与脉冲成形；
- symbol rate、SPS、sample rate；
- 插值、重采样和硬件采样率约束；
- 中心频率、带宽和信道；
- 振幅归一化和削顶风险；
- TX gain 或 attenuation；
- 天线、TX/RX 端口和通道；
- 连续、突发、重复或定时发送；
- 时钟源、时间源、同步或时间戳；
- 停止条件和最大运行时长。

### 关键规则

```text
“生成一个带 B210 Sink 的发射流图，但不要运行”

= TX_BUILD
+ execution_context=hardware_targeted
+ hardware_access=forbidden
+ rf_emit=forbidden
```

出现硬件名称不能自动产生 discover、probe 或 start。

```text
“用连接的 B210 发射”

= TX_BUILD：设计和离线验证
→ HARDWARE_OPERATE：发现、绑定、配置、确认、运行、停止
```

### 必须区分的完成语义

1. 发射流图已生成；
2. 已针对目标设备完成离线配置；
3. 目标设备已发现和绑定；
4. GNU Radio 已开始向 SDR 送样本；
5. 外部接收端已观察到目标空口信号。

这五个结论不能互相推导或替代。

## 4.3 Task 3：接收机设计

### 场景范围

1. 自包含仿真接收机；
2. 录制 IQ 文件接收机；
3. 面向 SDR Source 的离线接收流图；
4. 连接硬件后的实时采集；
5. 实时解调和帧解析；
6. 扫频、信道扫描或能量检测；
7. 单通道、双通道或 MIMO 接收；
8. 未知信号的通用前端；
9. 已知协议的专用解码器；
10. 与 TX reference 对比的 BER/PER 闭环接收机。

### RX 设计维度

- 信号来自仿真、文件还是空口；
- 文件格式、数据类型和元数据；
- 中心频率、采样率和带宽；
- RX gain、AGC、天线和通道；
- 频偏、采样时钟偏差；
- 匹配滤波；
- 定时、载波、相位和帧同步；
- 解调、译码、解扰和 CRC；
- 输出形式；
- 指标是否存在合法参考。

### BER 规则

只有存在可信 TX reference 时才能测 BER：

```text
TX reference bits
+ RX decision bits
+ 明确且有界的对齐算法
+ 比较位数
+ 错误位数
= BER Evidence
```

接收外部未知信号时不能伪造 BER，可以测量：

- RSSI/接收功率；
- SNR；
- CFO；
- EVM；
- CRC 通过率；
- 帧成功数和 PER；
- 解码成功率。

### 硬件组合示例

```text
“用 Pluto 接收 433.92 MHz 的信号并显示实时频谱”

= RX_BUILD：构建 SDR 接收前端
→ HARDWARE_OPERATE：发现、绑定、配置、启动 RX
→ OBSERVE：实时频谱
→ HARDWARE_OPERATE：停止
```

## 4.4 Task 4：诊断

诊断应建立分层诊断树，不应只做指标阈值判断。

### 诊断维度清单

| 层级 | 诊断内容 | 主要 Evidence |
|---|---|---|
| 请求一致性 | 当前工程、信号方向、设备是否与用户表达一致 | intent、工程摘要、版本 |
| 会话上下文 | 当前结果是否来自本轮 session 和当前项目 | session、artifact path、hash |
| 环境 | conda、Python、GNU Radio 版本是否正确 | executable、环境、版本 |
| 依赖 | 模块、OOT Block、动态库是否存在 | import、Block registry、stderr |
| 驱动 | UHD、libiio、Soapy、HackRF、Lime 驱动是否可用 | 命令路径、版本、探测输出 |
| 设备发现 | 电脑是否发现任何设备 | discovery inventory |
| 设备匹配 | 实际设备是否与用户指定型号一致 | expected profile、actual identity |
| 设备绑定 | 是否绑定到确定的 serial、URI 或 IP | identity record |
| 设备变化 | 是否拔插、替换或地址改变 | 当前与历史 fingerprint |
| 传输链路 | USB、以太网、IP、权限、设备占用 | backend/OS 错误和日志 |
| 接线和端口 | TX/RX 端口、天线、衰减器、线缆方向 | 人工确认、环回和功率变化 |
| 静态流图 | 缺失端口、类型不匹配、非法参数 | GRC validate/compiler |
| 速率链 | sample rate、插值、抽取、SPS、带宽一致性 | rate propagation |
| 参数范围 | 频率、增益、带宽、通道是否支持 | hardware profile |
| 调度运行 | 死锁、无 throttle、CPU 和 buffer 问题 | runtime status、日志 |
| 数据流质量 | underrun、overrun、丢包和吞吐不足 | `U/O` 计数、运行统计 |
| RF 前端 | 无信号、过弱、过载、饱和、DC spur、镜像 | PSD、功率和增益扫描 |
| 同步 | CFO、相位、定时和帧同步失败 | constellation、correlation、sync state |
| PHY | 调制、bit order、whitening、CRC、编码错误 | PDU/PHY 校验报告 |
| 协议 | 信道、Access Address、帧、广播间隔不匹配 | 协议解析报告 |
| 应用端 | 手机权限、扫描模式、目标应用是否支持 | 人工或应用 Evidence |
| 测量可信度 | Probe 陈旧、样本不足、单位或算法错误 | metric report、hash、version |
| 回归 | 与上一次正常运行相比改变了什么 | project/device/environment diff |
| 安全合规 | 当前测试方式是否允许真实发射 | Policy、确认和实验环境 |

### 硬件接入诊断必须覆盖的状态

```text
未发现任何设备；
发现了设备，但不是用户指定型号；
发现多个同型号设备，无法唯一绑定；
发现目标设备，但 probe 失败；
设备之前存在，本轮消失；
设备被替换，serial/URI/IP 改变；
设备被其他进程占用；
驱动命令不存在；
驱动存在但权限不足；
USB/网络发现成功，但流图无法打开设备；
设备打开成功，但没有样本；
设备运行过程中断开；
频率、采样率、增益或带宽超出范围；
运行存在 underrun/overrun；
驱动发现的是同族设备，但型号与能力不同。
```

### 设备替换规则

硬件绑定不能只保存 `hardware=pluto`，必须至少保存：

```text
设备族
驱动族
型号
serial/URI/IP
通道
首次发现时间
最近 probe 时间
能力摘要
```

如果设备 fingerprint 改变：

1. 标记旧硬件绑定失效；
2. 标记旧 runtime/OTA Claim 为 stale；
3. 重新读取设备能力；
4. 判断流图 Block 和参数是否需要迁移；
5. 重新生成或修改配置；
6. 重新请求 RF 确认。

禁止静默把 Pluto 换成 B210，或把 B210 降级成未指定型号的 USRP。

### 接线诊断的能力边界

仅凭 `iio_info`、`uhd_find_devices` 或 `uhd_usrp_probe` 不能确认天线和线缆连接正确。

系统只能通过以下方式提高置信度：

- 用户确认接线；
- 展示设备端口和接线检查表；
- 低功率线缆环回并使用合适衰减；
- 改变 TX gain 后观察 RX 功率是否同步变化；
- 使用功率计、频谱仪或第二台 SDR；
- 使用手机、LightBlue 或独立 Sniffer 作为外部 Evidence。

诊断结论需要支持：

```text
confirmed
likely
possible
unverified
ruled_out
```

不能把“软件无法确认接线”表述成“接线正确”。

### 标准诊断流程

```text
冻结当前工程和版本
→ 收集请求、工程、环境、设备和运行清单
→ 对比用户期望与实际对象
→ 静态检查
→ 安全的 discover/probe
→ 必要时执行有限诊断运行
→ 分层定位 RF、同步、PHY 和协议问题
→ 排序候选原因
→ 提供最小区分实验
→ 用户确认是否修复
→ 修复后重新验证
```

诊断报告至少包含：

- 已确认事实；
- 未确认事实；
- 候选原因和置信度；
- 支持/反对 Evidence；
- 下一步区分实验；
- 最小修复方案；
- 修复风险；
- 本轮是否修改了工程。

## 4.5 Task 5：修改已有工程

### 修改类型

1. 单参数修改；
2. 多参数联动修改；
3. Block 替换；
4. 拓扑修改；
5. 调制或协议修改；
6. TX/RX 方向修改；
7. 数据源或终点修改；
8. 加入或移除测量 Probe；
9. 从仿真改成硬件目标；
10. 更换硬件；
11. 从文件回放改成实时运行；
12. GNU Radio/Block 版本迁移；
13. 性能优化；
14. 故障修复。

### 风险分级

| 等级 | 示例 | 交互规则 |
|---|---|---|
| L0 | 布局和显示名称 | 可不确认 |
| L1 | 单个非危险参数 | 可按用户偏好 |
| L2 | 多 Block、速率、调制和拓扑 | 必须确认 |
| L3 | 加入或更换硬件端点 | 必须确认 |
| L4 | 启动 RX 或修改真实设备状态 | 必须确认 |
| L5 | 真实 RF 发射 | RF 专用确认 |

### 影响范围规则

| 修改内容 | 必须失效的下游结果 |
|---|---|
| 画布布局 | 不失效信号 Claim |
| 显示 Sink | 对应图像 Evidence |
| 噪声或信道 | EVM、BER、星座和频谱 |
| 调制方式 | 构建、同步、EVM、BER和协议结果 |
| sample rate | 速率校验、频谱轴、硬件配置和运行结果 |
| payload/local name | PDU、CRC、波形和空口 Evidence |
| 硬件型号 | 硬件绑定、配置、运行和 OTA Evidence |
| serial/URI/IP | probe、配置、运行和 OTA Evidence |
| TX gain | RF 计划、功率和 OTA Evidence |
| 仅修改说明文字 | 不影响工程 Evidence |

禁止修改后沿用旧工程版本的 Passed Claim。

## 4.6 Task 6：观测与测量

### 场景范围

1. 静态观察工程结构；
2. 对已有仿真结果测量；
3. 对录制 IQ 文件测量；
4. 运行纯软件流图后测量；
5. 使用 SDR 实时接收并观测；
6. 观察硬件运行质量；
7. 使用外部接收端完成 OTA 验收；
8. 多次运行比较；
9. 参数扫描或趋势分析。

### 指标范围

- 时域：波形、幅度、包络、峰均比；
- 频域：PSD、主峰、占用带宽、杂散、噪声底；
- 调制：星座、EVM、眼图；
- 链路：BER、SER、PER、SNR；
- 协议：CRC、帧数、解码成功率；
- 硬件：RSSI、gain、温度、LO lock；
- 运行：吞吐率、underrun、overrun、丢包；
- 应用：手机是否发现、目标名称是否一致。

### 只读规则

“只观察不修改”意味着不能修改用户原工程。

如果测量必须增加 File Sink 或 Probe，必须：

- 生成临时派生流图；或者
- 转入 `MODIFY_PROJECT` 并要求确认。

不能偷偷修改原工程后仍声称只读观测。

## 4.7 Task 7：硬件生命周期与受控运行

建议将 `HARDWARE_CONFIGURE` 扩展为 `HARDWARE_OPERATE`。它负责真实设备生命周期，不负责覆盖 TX/RX 的设计目标。

### 子能力

1. `inventory`：枚举全部设备；
2. `discover`：发现指定设备族；
3. `bind`：绑定 serial/URI/IP；
4. `probe`：读取能力和状态；
5. `preflight`：检查驱动、参数和 Policy；
6. `configure_offline`：生成硬件配置但不访问设备；
7. `configure_live`：配置真实设备；
8. `arm`：使能硬件端点；
9. `start_rx`：开始接收；
10. `start_tx_bounded`：有限时长发射；
11. `monitor`：监控运行；
12. `stop`：正常停止；
13. `emergency_stop`：立即终止；
14. `reconnect`：断线恢复；
15. `rebind`：设备替换后重新绑定；
16. `collect_evidence`：保存运行和外部 Evidence。

### 建议硬件状态机

```text
unbound
→ discovered
→ bound
→ probed
→ configured
→ armed
→ running
→ stopping
→ stopped
```

异常状态：

```text
disconnected
busy
mismatched
unsupported
faulted
emergency_stopped
```

### 硬件执行等级

| 等级 | 含义 |
|---|---|
| H0 | 只生成面向目标硬件的流图，不访问设备 |
| H1 | 只做 inventory/discover/probe |
| H2 | 配置真实设备，但不启动流图 |
| H3 | 启动真实 RX |
| H4 | 线缆或屏蔽环境 TX |
| H5 | OTA TX |

每一级需要独立 Policy，不能只依赖一个总 RF 开关。

---

## 5. 复合请求的编排规则

### 示例 1：纯软件 TX

```text
用户：生成一个 QPSK 发射机，保存 IQ，不接硬件。

TX_BUILD
  scenario=baseband_tx
  hardware_access=forbidden
  runtime=offline_bounded
```

### 示例 2：目标硬件流图但不访问设备

```text
用户：生成一个 B210 的 QPSK 发射流图，但不要打开硬件。

TX_BUILD
  scenario=hardware_targeted_tx
  hardware=b210
  hardware_access=forbidden
  rf_emit=forbidden
```

只能生成带禁用 UHD Sink 的可校验流图，不执行 discover。

### 示例 3：真实 B210 RX

```text
用户：用已连接的 B210 接收 915 MHz BPSK 并显示频谱。

RX_BUILD
→ HARDWARE_OPERATE(discover/bind/probe/configure/start_rx)
→ OBSERVE(live_spectrum)
→ HARDWARE_OPERATE(stop)
```

### 示例 4：BLE 手机验收

```text
用户：用 Pluto 发射 BLE，local name 为 deepradio，让手机扫描到。

TX_BUILD(protocol_tx)
→ offline_protocol_verify
→ HARDWARE_OPERATE(discover/bind/probe)
→ rf_plan_confirmation
→ HARDWARE_OPERATE(bounded_tx)
→ OBSERVE(ota_external_evidence)
→ HARDWARE_OPERATE(stop)
```

手机截图、扫描记录或独立 Sniffer 报告是空口 Evidence，不能用流图运行日志代替。

### 示例 5：硬件未发现诊断

```text
用户：Pluto 已连接，为什么系统说没有发现？

DIAGNOSE
  capabilities=[hardware_readonly_diagnostics]
```

可以调用安全的 discover/probe，但不进入硬件配置和发射确认。

### 示例 6：更换设备

```text
用户：我把 Pluto 换成 B210，继续刚才的发射。

MODIFY_PROJECT(hardware_endpoint_change)
→ 旧硬件 Claim stale
→ HARDWARE_OPERATE(rebind/probe)
→ 重新生成 RF 计划
→ 再次确认
→ bounded_tx
```

---

## 6. 意图识别和 Workflow 生成规则

### 6.1 意图输出不再只是单标签

建议意图层输出：

```json
{
  "primary_goal": "TX_BUILD",
  "requested_actions": [
    "design",
    "offline_verify",
    "bind_hardware",
    "run_bounded",
    "observe_ota",
    "stop"
  ],
  "direction": "tx",
  "execution_context": "ota",
  "hardware": {
    "required": true,
    "expected_family": "pluto",
    "access": "required"
  },
  "protocol": "ble",
  "runtime_mode": "bounded",
  "safety_level": "H5",
  "forbidden_actions": [],
  "success_conditions": [
    "protocol_packet_valid",
    "device_identity_matched",
    "runtime_started",
    "external_receiver_observed"
  ]
}
```

Planner 再将 `requested_actions` 编译成 Task DAG。

### 6.2 路由顺序

1. 判断本轮是新任务、补参数、反馈、调整、确认、拒绝还是取消；
2. 提取否定和禁止条件；
3. 提取所有动作，而非只找一个动词；
4. 确定动作对象：工程、TX、RX、信号、硬件、指标或协议；
5. 确定执行环境；
6. 确定是否需要真实硬件访问；
7. 确定是否存在 RF 发射；
8. 生成一个或多个 Task 节点；
9. 根据每个 scenario 计算缺失参数；
10. 做设备能力、参数范围和安全校验；
11. 最后才分配 Subagent 和 Skill。

### 6.3 强制约束

- 否定条件优先于普通关键词；
- 出现设备名不等于访问设备；
- 出现“发射流图”不等于实际发射；
- 缺少设备不能自动降级成仿真；
- 硬件 Builder 失败不能换成无关软件配方；
- 用户指定 B210、实际发现 Pluto 时必须阻塞并报告不一致；
- 多设备时必须绑定唯一 identity；
- 真实发射必须独立确认；
- RX 和 TX 使用不同安全策略；
- “运行成功”不能推导出“空口成功”；
- “设备发现”不能推导出“接线正确”；
- 指标值不能脱离测量报告和 Evidence 单独通过验收；
- LLM 只能提出 Task/Slot 候选，确定性 Validator 决定其是否合法。

---

## 7. Dynamic State 与状态模型

### 7.1 多 Task Workflow

目标状态应支持一个 Workflow 包含多个 Task：

```json
{
  "workflow_id": "wf-xxx",
  "goal": "使用 B210 接收 BPSK 并测量",
  "tasks": [
    {
      "task_id": "task-1",
      "kind": "RX_BUILD",
      "scenario": "hardware_targeted_rx",
      "depends_on": [],
      "status": "completed",
      "artifacts": ["receiver.grc"]
    },
    {
      "task_id": "task-2",
      "kind": "HARDWARE_OPERATE",
      "scenario": "live_rx",
      "depends_on": ["task-1"],
      "status": "waiting",
      "waiting_for": "hardware_identity"
    },
    {
      "task_id": "task-3",
      "kind": "OBSERVE",
      "scenario": "live_spectrum",
      "depends_on": ["task-2"],
      "status": "pending"
    }
  ]
}
```

### 7.2 状态保持精简

Task/Stage 执行状态：

```text
pending
running
waiting
completed
failed
cancelled
```

其他概念分开保存：

- `outcome`：passed / failed / inconclusive；
- `decision`：pending / approved / rejected；
- `evidence_validity`：current / stale / invalid；
- `runtime_state`：idle / armed / running / stopping / stopped / faulted。

`confirmed` 不应是 Task 状态；`stale` 也不应与执行状态混在一起。

### 7.3 动态转移公式

```text
下一步
= 当前 Workflow/Task/Stage 状态
+ 用户本轮动作或选择
+ 最新工程与硬件事实
+ 反馈影响范围
+ Policy
+ Completion Evidence
```

例如：

- 用户只改画布布局：不重新测 BER；
- 用户修改 sample rate：重做速率校验、频谱和硬件配置；
- 用户更换设备：重新绑定、重新配置、重新确认 RF；
- 用户上传手机截图：更新 OTA Evidence，不重建波形；
- 用户点“未看到”：不得把 OTA Stage 记为 Passed，应停止发射并进入诊断/等待。

---

## 8. 测试如何分类

测试应分成三种，而不是简单分成“自动”和“人工”。

## 8.1 A 类：完全自动化即可判定

适用于结果有确定性结构、数值或状态契约的场景。

### 可以完全自动化的内容

- Task/Scenario/Capability 分类；
- 多 Task DAG 分解和依赖关系；
- Slot 提取、单位转换和缺槽判断；
- 否定条件和禁止操作保持；
- Workflow/Task/Stage 状态迁移；
- Checkpoint 创建、确认、拒绝、取消；
- 失败、重试、回滚和失效传播；
- `.grc` 是否生成；
- Block 类型、参数、连接和启用状态；
- GNU Radio 静态校验和编译；
- 纯软件有限仿真；
- BER/EVM/频谱等确定性测量协议；
- Claim 与 Evidence 的版本、hash 和来源绑定；
- 禁止副作用，例如“不要硬件”时没有 discover/start；
- 使用伪造驱动输出的设备发现、错误型号、多设备和断线状态机；
- runtime 最大时长、停止、紧急停止；
- underrun/overrun 日志解析；
- GUI Presenter 的文字、按钮状态和 Workflow Digest 映射；
- session 恢复、一致性和旧结果丢弃。

### 自动化通过即可完成的代表场景

- 纯基带 TX/RX；
- AWGN 端到端仿真；
- 文件 IQ 生成和回放；
- 面向 B210/Pluto 的离线流图，且硬件块禁用；
- 不需要视觉质量判断的结构修改；
- 注入已知错误的诊断单元测试；
- 不触碰真实设备的 Policy 测试。

## 8.2 B 类：必须先自动化，再人工检查

这类场景同时包含机器可验证契约和人类体验/物理世界结果。自动测试不通过时，不应进入人工实验。

### 典型内容

| 场景 | 自动检查 | 人工检查 |
|---|---|---|
| GUI Workflow Inspector | Task、Stage、按钮、计数、状态字段 | 是否易懂、信息层级、是否过载 |
| 自动生成流图 | 结构、参数、类型、编译 | 布局、命名和可维护性 |
| 诊断回复 | 原因与 Evidence 字段齐全、没有非法改图 | 建议是否有帮助、是否符合专家心智模型 |
| 修改工程 | 确认前后 hash/version/Claim 正确 | 用户是否理解变更及后果 |
| 真实 SDR RX | discover/probe、参数、运行日志、数据到达 | 接线、天线、实际环境是否正确 |
| 真实 SDR TX | 离线 PHY、Policy、有限时长、stop | 外部仪器/手机是否收到 |
| OTA BLE | PDU/CRC/PHY、流图、runtime | LightBlue 名称、稳定性和距离表现 |
| 设备替换 | fingerprint 改变、旧 Claim stale、重新规划 | 用户是否理解为何需要重新确认 |

### 顺序

```text
自动结构/策略检查
→ 自动离线运行
→ 自动设备 discover/probe
→ 人工接线确认
→ 低风险真实运行
→ 人工/外部设备观察
→ Evidence 归档
```

## 8.3 C 类：主要依赖人工或外部世界

这些项目无法仅靠当前电脑的软件可靠判定：

- 天线是否拧紧；
- TX/RX 端口是否接反；
- 线缆和衰减器是否真实存在且规格正确；
- 空口环境是否存在外部干扰；
- 手机是否实际显示目标 BLE 名称；
- 外部仪器读数是否可信；
- UI 是否让人感到可理解和可控；
- 诊断解释是否帮助用户形成正确心智模型；
- 用户是否能预测确认按钮的后果；
- 用户在失败后是否知道如何恢复。

这类测试仍应先运行所有可自动化的前置 Gate，但最终结论依赖人工、手机、Sniffer、功率计或其他外部 Evidence。

---

## 9. 自动化与人工测试的总顺序

### Gate 0：静态契约

- Schema 和 Catalog 合法；
- Stage 转移目标存在；
- Completion 名称存在；
- Hardware/Protocol Profile 合法；
- 无非法依赖和重复 ID。

失败则停止。

### Gate 1：单元测试

- Intent、Slot、单位、否定；
- Task DAG；
- 状态迁移；
- 影响范围；
- Error Taxonomy；
- Completion Contract；
- Policy。

失败则停止。

### Gate 2：ServiceAgent 集成测试

- 七类 Task 的全部 scenario；
- 多轮补参数；
- 复合目标；
- 确认、拒绝、修改和恢复；
- 产物、Claim、Evidence；
- 不允许的副作用。

失败则停止。

### Gate 3：GNU Radio 离线执行

- `.grc` 加载；
- 静态校验；
- 编译；
- 有限无头运行；
- 文件输出和指标；
- 随机参数、边界参数和故障注入。

失败则停止。

### Gate 4：GUI 自动契约

- Workflow Inspector 字段；
- Checkpoint 按钮；
- Canvas 保存后的逆同步；
- Claim stale 展示；
- Undo、Reset 和 Evidence 上传；
- runtime 状态展示。

失败则停止。

### Gate 5：GUI 人工交互

- 七类 Task 代表路径；
- 新手、学生、专家三个表达档位；
- 信息是否容易定位；
- Checkpoint 的后果是否明确；
- 用户修改画布后是否理解“需要重验”；
- 错误和恢复提示是否可行动。

### Gate 6：硬件只读测试

- 真机 inventory/discover/probe；
- 正确设备、错误设备、多设备；
- 拔插和更换设备；
- 参数能力读取；
- 不启动 RF。

### Gate 7：真实 RX 和线缆/屏蔽 TX

- 先做 RX；
- 再做带衰减器的线缆环回或屏蔽环境 TX；
- 验证 stop/emergency stop；
- 检查 underrun/overrun 和设备断开恢复。

### Gate 8：受控 OTA

- 有限时长、低功率、明确频率；
- 手机、独立 Sniffer 或频谱仪验收；
- 保存运行日志和外部 Evidence；
- 实验结束后确认 runtime 已停止。

### Gate 9：CHI 用户实验

只有工程 Gate 通过后，才测试透明度、控制感、工作负担、信任、恢复能力和用户体验。不能让基础功能失败污染 HCI 结论。

---

## 10. 七类 Task 的测试分配

| Task | 完全自动 | 自动后人工 | 主要人工/外部 |
|---|---|---|---|
| Task 1 端到端 | 纯仿真、文件环回、指标、Claim | GUI 解释、硬件环回 | OTA 双端真实链路 |
| Task 2 TX | 基带/IF、硬件目标离线图、结构校验 | 真机配置、有限 TX | 空口功率与外部接收 |
| Task 3 RX | 自包含 BER、文件 IQ、解码向量 | 实时 SDR RX、GUI 图 | 天线、真实未知信号质量 |
| Task 4 诊断 | 软件故障注入、模拟设备错误 | 真机发现、断线、设备替换 | 接线原因、建议可用性 |
| Task 5 修改 | diff、确认、版本、Claim 失效 | GUI 预览、撤销和理解 | 专家判断方案合理性 |
| Task 6 观测 | 数值、频率轴、报告、artifact | 图像可读性、实时显示 | 手机/仪器空口观察 |
| Task 7 硬件 | Mock 状态机、Policy、stop | 真机 probe、RX、受控 TX | 接线、安全环境和 OTA |

---

## 11. 测试集如何真正覆盖泛化

当前每类十条左右的文本同义改写，只能测试表达鲁棒性。V3 测试集需要覆盖“语义等价类、场景组合和边界”。

### 11.1 每类 Task 必测维度

- 新工程与已有工程；
- 纯软件与硬件目标；
- 无硬件访问与真实硬件访问；
- 参数完整与参数缺失；
- 单轮和多轮；
- 肯定、否定和部分否定；
- 单目标与复合目标；
- 正常、可恢复失败和不可恢复失败；
- 确认、拒绝、改规格和取消；
- 当前、陈旧和缺失 Evidence；
- 中文、英文和中英混合；
- 明确设备、模糊设备、错误设备和多设备；
- 离线、文件、环回、实时 RX、真实 TX 和 OTA。

### 11.2 诊断专项覆盖

- 驱动不存在；
- 没有设备；
- 错误型号；
- 多设备冲突；
- serial/URI 改变；
- 设备占用；
- 参数超范围；
- Source/Sink 端口错误；
- sample rate 不一致；
- 无 throttle；
- underrun/overrun；
- 无 RF 信号；
- 过载饱和；
- 频偏；
- 定时同步失败；
- CRC、whitening 或 bit order 错误；
- Probe 陈旧；
- 样本不足；
- 用户更换设备后继续旧 Workflow；
- 接线无法由软件确认；
- 手机没发现信号但 SDR 正在送样本。

### 11.3 评价指标

- Primary Goal accuracy；
- 多 Task 分解准确率；
- Slot exact/semantic match；
- 否定约束保持率；
- 硬件 identity 一致率；
- 缺槽追问正确率；
- 同一 Workflow 延续率；
- 不相关回退率；
- 未授权硬件访问率；
- Evidence 完整率；
- 错误完成率；
- 设备替换后的失效传播正确率；
- 失败恢复率；
- 用户目标语义保持率。

关键安全指标应要求：

```text
未授权 RF 操作率 = 0
错误设备静默替换率 = 0
无 Evidence 的成功声明率 = 0
```

其他组合不必穷举笛卡尔积，可以使用 pairwise 生成，但上述关键边界必须全部显式覆盖。

---

## 12. CHI 2027：DeepRadio 的研究优势是什么

## 12.1 不应只把贡献写成“用了 Dynamic State”

单独使用 JSON 状态机不构成充分的 CHI 贡献。更有说服力的说法是：

> DeepRadio 将动态任务状态外化为一个可观察、可干预、可逆同步、与可执行 GNU Radio 工程及物理硬件 Evidence 绑定的共享交互对象，从而支持复杂技术任务中的 mixed-initiative human-agent collaboration。

真正的研究贡献是以下组合：

```text
Inspectable Dynamic Workflow
+ Human Checkpoints
+ Bidirectional Artifact–Workflow Synchronization
+ Evidence-linked Claims
+ Reversible and Safety-aware Execution
```

## 12.2 当前 Workflow 已有的优势

### 1. 过程可观测

当前 GUI 已经能够展示：

- 当前 Task；
- 当前 Stage；
- Stage 进度；
- Completion 数量；
- Workflow 时间线；
- Actor；
- 等待原因；
- runtime 摘要；
- Claim 和 Evidence。

这比“用户发一句话，Agent 黑盒式返回一个流图”更适合复杂工程任务。

### 2. 用户可以在关键节点控制流程

当前已实现：

- Checkpoint 确认和拒绝；
- RF 计划确认；
- OTA“看到/未看到”反馈；
- 失败后的受控重试；
- 取消任务；
- 撤销到上一版本；
- 重置工作区；
- 上传外部 Evidence。

这说明系统已经具有基础的 mixed-initiative control，而不是完全自治 Agent。

### 3. Claim 与工程版本绑定

当工程变化后，旧验证结果会失效。它能向用户表达：

```text
“这个结论曾经成立，但不一定适用于你现在修改后的流图。”
```

这对于工程系统中的可信度和可追溯性比单纯自然语言解释更重要。

### 4. 有明确的物理世界安全边界

离线设计、设备探测、配置、使能、发射和空口验收被拆成不同阶段。人可以在进入真实 RF 之前阻止系统继续执行。

### 5. 支持失败后的局部恢复

Stage 具有尝试次数、失败结果和恢复入口。理想状态下只重新执行受影响的 Stage，而不是每次重建整个流程。

## 12.3 当前是否可以做到“人控制 Workflow”

结论：已经部分做到，但还不是完整的可编辑 Workflow。

### 当前已实现的控制

| 用户操作 | 当前影响 |
|---|---|
| 点击确认/拒绝 | 解析当前 Checkpoint，改变 Stage 转移 |
| 点击重试/取消 | 重启当前 Stage 或结束 Workflow |
| 修改并保存画布 | 工程 version 增加、Claim stale、Workflow invalidated |
| 打开另一个 `.grc` | 更新 current project |
| 撤销 | 恢复快照并使 Workflow 重新对齐 |
| 重置 | 归档 Workflow，并触发硬件紧急停止 |
| 上传截图 | 作为 OTA Evidence 进入验收 |

因此当前已经存在一条真实的反向路径：

```text
用户编辑 GRC Canvas
→ 保存
→ Agent 检测语义 hash 变化
→ flowgraph_version 增加
→ 旧 Claim 失效
→ Workflow Stage invalidated
```

这可以称为初步的 `artifact-to-workflow reverse synchronization`。

### 当前尚未实现完整的控制

- 用户不能在 UI 中直接查看和编辑完整 Task DAG；
- 用户不能拖动、重排、跳过或插入 Task/Stage；
- Canvas 保存后主要触发 invalidation，尚未普遍生成结构化 semantic diff；
- 系统不能稳定判断用户手工改图对应哪个 Intent Slot；
- 不能自动根据人工改图重算受影响 Task DAG；
- 用户直接点击 GRC 原生运行箭头时，与 Agent runtime/Policy 的同步仍需完整审计；
- Workflow Inspector 主要是只读展示，不是 Workflow Editor；
- 用户不能逐项批准参数、设备、运行时长和验收条件。

因此论文目前可以主张“可观察和有限可控”，不能直接主张“用户能够任意编辑 Workflow”。

## 12.4 目标：真正的双向控制

目标交互闭环应为：

```text
用户 Text
→ 更新 Goal/Task/Slot

用户点击 Checkpoint
→ 更新 Decision 和 Stage 转移

用户编辑 Canvas 并保存
→ 生成 Semantic Diff
→ 更新工程事实和 Intent Slot
→ 计算影响范围
→ 局部重规划

用户启动/停止运行
→ 更新 Runtime State
→ 触发监控和 Evidence 收集

用户上传或否定 Evidence
→ 更新 Claim
→ 完成、诊断或回退
```

建议把人对 Workflow 的控制分为五级：

| 级别 | 能力 | 当前状态 |
|---|---|---|
| C0 | 只能观看状态 | 已实现 |
| C1 | 确认、拒绝、取消、重试 | 已实现 |
| C2 | 编辑工程导致 Claim/Stage 失效 | 已实现 |
| C3 | 从工程语义 diff 自动更新 Slot 并局部重规划 | 部分实现 |
| C4 | 在 UI 中编辑、插入、删除或重排 Task/Stage | 未实现 |
| C5 | UI、Canvas、Runtime、Evidence 全部事件化双向同步 | 目标能力 |

CHI 论文可以把 C0～C2 作为现有原型，把 C3～C5 中选定的关键能力作为下一阶段设计贡献，但必须在投稿前实际实现并评测。

## 12.5 最有潜力的 CHI 贡献点

### Contribution A：外化的动态任务状态

Agent 不仅给最终答案，还把任务拆分、当前阶段、等待原因、失败和证据状态展示给用户。

价值：降低黑盒感，让用户形成对复杂 Agent 行为的正确预期。

### Contribution B：人机混合控制点

系统只在会改变目标、工程结构或真实物理环境的关键边界请求确认，普通确定性步骤自动执行。

价值：在人类控制感和交互负担之间寻找平衡。

### Contribution C：Artifact-grounded Bidirectional Workflow

用户不必只通过聊天纠正 Agent；用户可以直接修改 GRC 画布。工程修改反向更新状态、使旧证据失效并推动局部重规划。

价值：尊重工程师已有工作方式，把可执行 artifact 变成与 Agent 沟通的界面。

### Contribution D：Evidence-linked Claims

“通过”“已发射”“手机已收到”等结论必须绑定当前版本的可核对 Evidence。

价值：减少自动化系统的错误成功声明，并支持审计和协作。

### Contribution E：软件到物理世界的渐进安全边界

Workflow 从离线构建逐步进入 discover、configure、arm、run 和 OTA，每一步有不同的安全等级和用户控制。

价值：研究 Agent 如何在 cyber-physical creative tools 中安全地共享控制权。

### Contribution F：专业度自适应但验收标准不变

系统可以针对小白、学生和专家改变解释深度及术语，但不改变安全规则和完成条件。

价值：个性化解释与工程正确性解耦。

## 12.6 潜在研究问题

### RQ1：可观测性

展示动态 Task/Stage/Evidence 是否能提高用户对 Agent 当前行为、失败原因和下一步的理解？

### RQ2：控制感

关键 Checkpoint、取消、撤销和局部重试是否能提高 perceived control，并减少不安全或不期望的操作？

### RQ3：反向控制

允许用户直接修改 GRC Canvas，并让修改反向更新 Workflow，是否比纯聊天纠错更快、更准确、更符合工程师习惯？

### RQ4：信任校准

Claim–Evidence 和 stale 状态能否降低用户对错误成功声明的过度信任，同时避免对正确结果产生不必要怀疑？

### RQ5：专业度差异

新手、学生和专家是否需要不同的信息密度、控制粒度和 Workflow 展示方式？

## 12.7 实验条件建议

为了识别真正贡献，建议至少比较：

1. `One-shot Baseline`：一句话直接生成，没有 Workflow；
2. `Opaque Agent`：内部多步执行，但只展示聊天结果；
3. `Inspectable Workflow`：展示 Task、Stage、Checkpoint、Claim/Evidence；
4. `Bidirectional Workflow`：在条件 3 上增加 Canvas 逆同步和局部重规划。

如果参与人数或研究资源有限，可保留 1、3、4 三个条件。

### 代表实验任务

- 纯软件端到端 BPSK/QPSK 构建；
- 缺参数的 RX/BER 任务；
- 修改调制方式并处理旧 Claim；
- 诊断一个由 sample rate 或连接错误导致的失败；
- 用户手工在 Canvas 改参数后继续任务；
- 更换 Pluto/B210 后恢复 Workflow；
- 受控 BLE 发射并使用手机验收。

### 客观指标

- Task success；
- 首次方案正确率；
- 用户目标语义保持率；
- 错误完成率；
- 未授权操作数；
- 恢复成功率；
- 完成时间；
- 对话轮次；
- 用户干预次数；
- 撤销次数；
- Canvas 手工修改次数；
- 旧 Claim 被正确识别的比例；
- 用户对当前 Stage 和下一步的预测准确率。

### 主观和质性指标

- perceived control；
- transparency/understandability；
- trust calibration；
- workload；
- usability；
- 对确认点是否必要的评价；
- 对 Workflow 信息量的评价；
- 访谈中的故障恢复策略；
- 新手与专家的差异。

### 避免混淆因素

- 工程功能测试必须先通过，不能把普通 Bug 当成人机交互效果；
- 不同条件使用相同任务和相同硬件条件；
- 记录模型版本和随机性；
- 明确区分系统建议、用户决定和真实工具结果；
- 不暴露模型私有推理，只展示可验证的任务状态、理由摘要和 Evidence；
- 硬件实验统一频率、功率、时长和环境；
- 对任务顺序做 counterbalancing；
- 对专业度进行分层或作为协变量分析。

---

## 13. CHI 论文中应避免的过度主张

在相应功能和实验完成前，不应写：

- “系统能够诊断所有 GNU Radio 故障”；
- “系统能够自动判断接线正确”；
- “用户可以完全编辑 Workflow”；
- “程序运行成功等于 OTA 成功”；
- “Dynamic State 本身就是核心算法创新”；
- “用户确认说明系统天然安全”；
- “一个 BLE/Pluto 成功案例说明系统对所有协议和硬件泛化”；
- “73 条文本分类通过说明自然语言泛化问题已经解决”。

更准确的表述是：

> DeepRadio 探索了一种用于 GNU Radio 复杂软硬件任务的可观察、证据驱动、有限可逆的人机协同 Workflow。当前原型支持关键 Checkpoint、工程版本与 Claim 失效、Canvas 保存后的逆同步，以及受控硬件阶段；进一步的语义反向规划和 Workflow 直接编辑作为后续实现与评测目标。

---

## 14. 推荐实施顺序

### P0：修正上层任务模型

1. 将单个 `task_type` 扩展成 `primary_goal + requested_actions`；
2. 允许 Workflow 保存多个 Task 节点；
3. 增加 `scenario`、`execution_context`、`hardware_access` 和 `safety_level`；
4. 把否定约束作为一等字段；
5. 将硬件名称与真实硬件访问彻底分离。

### P1：建立场景化 Task Catalog

每个 Task variant 声明：

```text
required_slots
optional_slots
preconditions
capabilities
stages
completion_contract
forbidden_actions
failure_policy
```

优先补齐：

- TX/RX 的硬件目标离线模式；
- 真实 RX；
- 真实受控 TX；
- 文件 IQ；
- 设备替换；
- 诊断只读硬件能力。

### P1：重做诊断框架

1. 建立分层 Error Taxonomy；
2. 建立 Hardware Inventory 和 Identity Binding；
3. 保存环境、工程、设备和运行 fingerprint；
4. 诊断结果采用“事实—假设—证据—区分实验—修复”；
5. 对接线等问题支持 `unverified`，禁止伪确认。

### P2：增强反向控制

1. Canvas 保存时生成语义 diff；
2. 将 Block/参数变化映射回 Intent Slot；
3. 计算 Claim 和 Task 影响范围；
4. 局部重规划而不是整体重启；
5. 在 Workflow Inspector 增加可控操作；
6. 审计原生 GRC Run/Stop 与 Agent runtime 的状态一致性。

### P2：建立组合测试集

1. Task × Scenario × 硬件状态 × 用户交互；
2. 关键边界全部显式覆盖；
3. 其他组合使用 pairwise；
4. 自动 Gate 全绿后再做人工和真机测试；
5. 单独统计错误完成、目标歪曲和未授权硬件访问。

### P3：CHI 原型和实验

1. 完成 Inspectable Workflow；
2. 选择并完成关键 Bidirectional Workflow 能力；
3. 冻结实验任务和版本；
4. 先做 pilot；
5. 再开展正式用户研究；
6. 将工程正确性结果与 HCI 结果分开报告。

---

## 15. 最终设计原则

```text
不要让关键词替用户决定目标；
不要让硬件覆盖 TX/RX 设计语义；
不要让执行成功替代外部验收；
不要让旧 Evidence 验证新工程；
不要让不可观测的物理事实被伪装成确定结论；
不要让 Dynamic Workflow 只是后台状态机；
要让它成为人能够理解、干预并通过工程操作反向影响的共享协作对象。
```

