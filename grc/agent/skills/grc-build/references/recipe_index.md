# 配方索引(由 knowledge/recipes.py 自动生成,勿手改)

选型:按意图关键词命中数挑最合适配方(match_recipe);全不中回落 bpsk_awgn。

## tone_noise  (T1)
- 标题: 单音信号 + 噪声(入门)
- 摘要: 一个复正弦音叠加高斯噪声 -> 采集 IQ,用于最直观地演示"信号 + 噪声"与频谱,门槛最低。
- 关键词: 正弦, 单音, tone, 噪声, 频谱, 入门, sine, 信号加噪
- 指标: spectrum, constellation
- 块数: 7
- 可调旋钮:
  - `noise.amplitude`: 噪声幅度;越大频谱本底越高。建议 0.05~0.5
  - `sig.freq`: 单音频率(Hz);决定频谱峰位置

## bpsk_awgn  (T2)
- 标题: BPSK 基带 + AWGN 信道
- 摘要: 随机比特 -> BPSK 星座调制(RRC 成形)-> 加性高斯白噪声 -> 采集 IQ,适合观察星座/EVM 随噪声退化。
- 关键词: bpsk, awgn, 星座, 噪声, 调制, 误差, evm, 基带
- 指标: evm, constellation, eye, spectrum
- 块数: 8
- 可调旋钮:
  - `chan.noise_voltage`: 噪声强度;越大星座越散、EVM 越高。建议 0.01~0.5
  - `mod.excess_bw`: RRC 滚降系数;越大带宽越宽、码间串扰越小。建议 0.2~0.5
  - `sps.value`: 每符号样本数;影响过采样与眼图张开度。建议 2~8
  - `chan.freq_offset`: 归一化频偏;非零会让星座旋转。诊断载波恢复用

## qpsk_awgn  (T2)
- 标题: QPSK 基带 + AWGN 信道
- 摘要: 随机比特 -> QPSK 星座调制 -> AWGN -> 采集 IQ。每符号 2 bit,同噪声下比 BPSK 更易出错,适合对比星座/EVM。
- 关键词: qpsk, 四相, awgn, 星座, 噪声, evm
- 指标: evm, constellation, eye, spectrum
- 块数: 8
- 可调旋钮:
  - `chan.noise_voltage`: 噪声强度;QPSK 判决边界更密,对噪声更敏感。建议 0.01~0.3
  - `mod.excess_bw`: RRC 滚降系数。建议 0.2~0.5
  - `chan.freq_offset`: 归一化频偏;QPSK 星座会整体旋转 45°的整数倍

## ofdm_awgn  (T3)
- 标题: OFDM 发射 + AWGN(进阶,占位)
- 摘要: 随机字节 -> OFDM 发射(FFT 64, CP 16)-> AWGN -> 采集 IQ。预留骨架:多载波抗多径,适合看 PAPR/子载波频谱。
- 关键词: ofdm, 多载波, 子载波, fft, 循环前缀, cp, papr
- 指标: spectrum, constellation
- 块数: 7
- 可调旋钮:
  - `ofdm.fft_len`: 子载波数;越大频率分辨率越高、PAPR 风险越大。常见 64/128
  - `ofdm.cp_len`: 循环前缀长度;抗多径能力 vs 开销权衡。常取 fft_len/4
  - `chan.noise_voltage`: 噪声强度。建议 0.01~0.1
