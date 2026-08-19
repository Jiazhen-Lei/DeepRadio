# 仿真输出契约

simulation_agent 完成后必须满足:

## 产物路径
- 指标:`/session/work/sim/metrics.json`(如 `{"evm_pct": 3.21, "n_symbols": 2048}`)。
- 图片:`/session/work/sim/constellation.png` / `spectrum.png` / `eye.png`(按配方 metrics 出图)。

## 数据来源
- 从 file_sink 落盘的 IQ(complex64)读回,按 sps 抽符号后计算指标。
- probe id 与配方 `probe_block_id` 一致(默认 `sink`)。

## 回报格式(给主 Agent)
一段话:跑了哪些指标、数值、出了哪些图、有无异常(如样本不足)。

## 安全
- 仅本地无头仿真,不驱动任何真实 SDR 硬件。
