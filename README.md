# DeepRadio

DeepRadio 在 GNU Radio Companion（GRC）中加入智能体能力。用户通过自然语言描述无线通信任务，系统负责规划 Workflow、调用 SubAgent、生成并验证流图，并在需要时执行真实 RF。

## 核心架构

```text
用户请求 → MainAgent → 当前 Stage → SubAgent Tasks → Evidence → 下一 Stage
                         ├─ 缺少参数：停留并等待用户补充
                         └─ 需求变化：回到受影响的 Stage 重新执行
```

- **Workflow**：根据当前目标动态生成的完整执行链路。
- **Stage**：用户可见的工作阶段，例如需求确认、离线构建与验证、真实 RF 执行。
- **Task**：Stage 内部交给 SubAgent 完成的技术任务。一个 Stage 可以包含多个 Task。
- **Evidence**：工具执行后产生的可验证结果，用于更新 Workflow 状态。

MainAgent 每轮只推进当前 Stage。Stage 完成后，系统等待用户继续。用户可以补充参数、修改已完成阶段的需求，或要求加入新的阶段，例如先进行 simulation。系统会更新 Workflow，并从最早受影响的 Stage 继续。

## 安装

```bash
cd deepradio_dev
conda env create -f environment.yml
conda activate gnuradio
cp .env.example .env
```

在 `.env` 中配置模型：

```bash
GRC_AGENT_BASE_URL=...
GRC_AGENT_API_KEY=...
GRC_AGENT_MODEL=...
```

这三项未配置时，MainAgent 无法运行。

## 启动

```bash
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

运行数据保存在：

- `local/output/<session_id>/`：生成的流图及构建产物。
- `local/agent_sessions/<session_id>/`：Workflow、事件和证据记录。

## 使用

在 GRC 的 DeepRadio 面板中直接描述目标，例如：

```text
创建一个 BPSK 仿真链路并验证输出。
```

```text
使用 PlutoSDR 构建 BLE 发射流程，local name 设置为 DeepRadio，最长运行 30 秒。
```

如果参数不足，系统会停留在当前 Stage，用户补充后继续。需求发生变化时，可以直接说明：

```text
把 BLE local name 改为 DeepRadio-Demo。
```

```text
先增加一个 simulation 验证，再执行后续流程。
```

## 真实 RF

真实 RF 没有全局开关。只有 Workflow 执行到对应 Stage 时，界面才会展示设备、频率、功率和时长，并请求用户确认当前 Workflow 与流图版本。

确认后系统仍会检查设备状态和离线验证结果。单次运行最长 60 秒，运行期间可以随时停止。真实 RF 应通过 DeepRadio Workflow 启动，不应绕过流程手动运行流图。

### 连接 USRP B210

1. 使用支持数据传输的 USB 3.x 线缆连接 B210，尽量直连电脑。发射前，在 `TX/RX` 端接好天线或 50 Ω 负载。

2. 激活 gnuradio 后下载 UHD 镜像：

   ```bash
   conda activate gnuradio
   uhd_images_downloader
   ```

3. 检查设备：

   ```bash
   uhd_find_devices --args "type=b200"
   uhd_usrp_probe --args "type=b200"
   ```

   正常情况下应显示 `product: B210`、序列号和 `type: b200`。

4. GNU Radio 的 **UHD: USRP Sink/Source** 使用：

   - Device Address：`type=b200`
   - Antenna：`TX/RX`
   - Sample Rate：与基带信号一致
   - Center Frequency：按实验频率设置
   - Gain：从较低值开始

如果出现 `No devices found`，优先检查 USB 3.x 线缆、接口和 UHD 镜像，不需要修改流图的调制参数。


### PlutoSDR BLE 硬件测试

1. 接入 PlutoSDR BLE 硬件。

2. 激活 GNU Radio 环境：

   ```bash
   conda activate gnuradio
   ```

3. 检查设备连接：

   ```bash
   iio_info -S
   ```

   没有报错则继续；有报错请阅读《DeepRadio硬件连接指南.md》。

4. 启动 DeepRadio：

   ```bash
   PYTHONPATH=$PWD python -m grc --gtk --fresh
   ```

### 常用设备检查：

```bash
# USRP B210
uhd_find_devices --args "type=b200"

# PlutoSDR
iio_info -S
```

## 主要代码

- `grc/agent/service/mainagent_runtime.py`：MainAgent 宿主运行环境。
- `grc/agent/service/subagents.py`：SubAgent 定义。
- `grc/agent/workflow/dynamic.py`：动态 Workflow 状态。
- `grc/agent/tools/`：GRC、验证和 RF 工具。
- `grc/gui/AgentPanel.py`：交互界面。
