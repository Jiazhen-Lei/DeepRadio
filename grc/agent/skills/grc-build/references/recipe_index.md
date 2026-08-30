# 配方索引(由 knowledge/recipes.py 生成,勿手改)

选型:`match_recipe` 按关键词命中;全不中回落 `bpsk_awgn`。
仅 `build_tx` 且意图不含信道/EVM/BER/眼图时,`covering_recipe` 改用 `bpsk_tx` / `qpsk_tx`。
含 `hardware_configure` / `diagnose` / `modify_project` 时 covering 返回空,不套基带配方。

## tone_noise  (T1)
- 标题: Tone + Noise (Introductory)
- 摘要: A complex sinusoidal tone plus Gaussian noise -> IQ capture, providing an accessible demonstration of signal, noise, and spectrum.
- 关键词: 正弦, 单音, tone, 噪声, 频谱, 入门, sine, 信号加噪
- 指标: spectrum, constellation
- 块数: 7
- 可调旋钮:
  - `noise.amplitude`: Noise amplitude; higher values raise the spectral floor. Suggested: 0.05–0.5
  - `sig.freq`: Tone frequency (Hz); determines the spectrum-peak position

## bpsk_tx  (T2)
- 标题: BPSK Transmitter (No Channel)
- 摘要: Random bits -> BPSK constellation modulation -> IQ capture, without a channel model.
- 关键词: bpsk, 发射机, transmitter, tx, 发射链
- 指标: (无默认指标)
- 块数: 7
- 可调旋钮:
  - `mod.excess_bw`: RRC roll-off. Suggested: 0.2–0.5
  - `sps.value`: Samples per symbol. Suggested: 2–8

## qpsk_tx  (T2)
- 标题: QPSK Transmitter (No Channel)
- 摘要: Random bits -> QPSK constellation modulation -> IQ capture, without a channel model.
- 关键词: qpsk, 四相, 发射机, transmitter, tx, 发射链
- 指标: (无默认指标)
- 块数: 7
- 可调旋钮:
  - `mod.excess_bw`: RRC roll-off. Suggested: 0.2–0.5
  - `sps.value`: Samples per symbol. Suggested: 2–8

## bpsk_awgn  (T2)
- 标题: BPSK Baseband + AWGN Channel
- 摘要: Random bits -> BPSK constellation modulation (RRC shaping) -> additive white Gaussian noise -> IQ capture; suited to observing constellation and EVM degradation with noise.
- 关键词: bpsk, awgn, 星座, 噪声, 调制, 误差, evm, 基带
- 指标: evm, constellation, spectrum
- 块数: 8
- 可调旋钮:
  - `chan.noise_voltage`: Noise strength; higher values spread the constellation and increase EVM. Suggested: 0.01–0.5
  - `mod.excess_bw`: RRC roll-off; higher values widen bandwidth and reduce inter-symbol interference. Suggested: 0.2–0.5
  - `sps.value`: Samples per symbol; affects oversampling and eye opening. Suggested: 2–8
  - `chan.freq_offset`: Normalized frequency offset; nonzero values rotate the constellation. Useful for carrier-recovery diagnosis

## qpsk_awgn  (T2)
- 标题: QPSK Baseband + AWGN Channel
- 摘要: Random bits -> QPSK constellation modulation -> AWGN -> IQ capture. With two bits per symbol, it is more error-prone than BPSK at the same noise level and supports constellation/EVM comparison.
- 关键词: qpsk, 四相, awgn, 星座, 噪声, evm
- 指标: evm, constellation, spectrum
- 块数: 8
- 可调旋钮:
  - `chan.noise_voltage`: Noise strength; QPSK decision boundaries are denser and more noise-sensitive. Suggested: 0.01–0.3
  - `mod.excess_bw`: RRC roll-off. Suggested: 0.2–0.5
  - `chan.freq_offset`: Normalized frequency offset; the QPSK constellation rotates in integer multiples of 45°

## rx_bpsk_awgn  (T2)
- 标题: BPSK AWGN Receiver
- 摘要: Self-contained BPSK stimulus and AWGN channel with carrier tracking and symbol decisions through a constellation receiver.
- 关键词: 接收机, receiver, 解调, 定时恢复, 时钟同步, pfb, 判决, constellation_receiver, 自包含
- 指标: ber
- 块数: 12
- 可调旋钮:
  - `rx.loop_bw`: Carrier-loop bandwidth; trades tracking speed against noise suppression.
  - `chan.noise_voltage`: Receiver input-noise strength.
  - `chan.freq_offset`: Used to verify the receiver's carrier-tracking range.

## ofdm_awgn  (T3)
- 标题: OFDM Transmitter + AWGN (Advanced Placeholder)
- 摘要: Random bytes -> OFDM transmitter (FFT 64, CP 16) -> AWGN -> IQ capture. Placeholder structure for multicarrier multipath resistance and PAPR/subcarrier-spectrum observation.
- 关键词: ofdm, 多载波, 子载波, fft, 循环前缀, cp, papr
- 指标: spectrum, constellation
- 块数: 7
- 可调旋钮:
  - `ofdm.fft_len`: Subcarrier count; larger values improve frequency resolution and increase PAPR risk. Common values: 64/128
  - `ofdm.cp_len`: Cyclic-prefix length; trades multipath tolerance against overhead. Often fft_len/4
  - `chan.noise_voltage`: Noise strength. Suggested: 0.01–0.1
