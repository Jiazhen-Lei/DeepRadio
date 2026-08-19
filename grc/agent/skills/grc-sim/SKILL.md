---
name: grc-sim
description: 对已校验通过的 GRC 流图执行无头仿真,读回 EVM/BER 等指标,画星座/频谱/眼图。当需要"跑一下看指标/看图"时使用。
---

# grc-sim:无头仿真与指标

## 何时使用
- 主 Agent 在 SIMULATE 阶段委派:对**已校验通过**的流图跑无头仿真并出指标/图。

## 使用协议
1. 用 `run_simulation(probes=...)` 执行无头仿真(本地 GNU Radio,安全)。
2. 用 `read_metric(kind, probe_id, ...)` 读指标;指标定义见
   `references/metric_definitions.md`。
3. 用 `plot_constellation / plot_spectrum / plot_eye` 出图。
4. 产物写 `/session/work/sim/`:`metrics.json` + 各 `*.png`。

## 前置条件
- 只对 critic 判定 valid 的流图仿真;输入非法时回报主 Agent,不强行执行。
- file_sink 落盘路径由运行时提供(probe 机制)。

## 输出契约
见 `references/sim_output_contract.md`。
