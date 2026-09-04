---
name: grc-hardware
description: 准备 SDR 硬件，并在用户确认后执行有限时长的真实 RF 任务。
---

# DeepRadio Hardware

根据 TaskCard 的 `stage_id` 执行对应任务。

## Hardware Preparation

- 使用 `configure_sdr` 记录设备参数。
- 使用 `discover_devices` 和 `probe_device` 读取设备状态。
- 使用 `arm_hardware_flowgraph` 准备当前会话的 Flowgraph。
- 不调用 `start_flowgraph`，也不请求 RF 执行确认。

## Physical RF Execution

- 仅在 MainAgent 已记录当前 Workflow 和 Flowgraph 版本的本次确认后调用 `start_flowgraph`。
- 运行时长必须有限。
- 运行结束后调用 `stop_flowgraph` 或确认运行已经停止。
- `stop_flowgraph` 和 `emergency_stop` 始终可以调用。

构建或准备 Flowgraph 不代表已经发射。没有独立观测时，不声明空口结果成功。
