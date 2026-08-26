# 常用 GRC 块目录(节选)

面向 DeepRadio 教学链路的高频块。完整信息以 `describe_block(key)` 的实时结果为准,
本表仅作快速定位。

## 信号源
| key | 用途 | 关键参数 | 输出类型 |
|-----|------|----------|----------|
| `variable` | 定义变量(如 samp_rate/sps) | value | — |
| `variable_constellation` | 定义星座(bpsk/qpsk) | type | — |
| `analog_sig_source_x` | 正弦/方波等信号源 | waveform/freq/amplitude/samp_rate | complex/float |
| `analog_noise_source_x` | 高斯/均匀噪声源 | noise_type/amplitude/seed | complex/float |
| `analog_random_source_x` | 随机字节源 | min/max/num_samps/repeat | byte |

## 调制 / 处理
| key | 用途 | 关键参数 | 端口 |
|-----|------|----------|------|
| `digital_constellation_modulator` | 星座调制(含 RRC 成形) | constellation/samples_per_symbol/excess_bw | in:byte out:complex |
| `digital_pfb_clock_sync_xxx` | 多相时钟同步 | sps/taps/loop_bw | in:complex out:complex |
| `digital_constellation_receiver_cb` | 载波恢复与判决 | constellation/loop_bw | in:complex out:byte |
| `digital_ofdm_tx` | OFDM 发射链 | fft_len/cp_len/bps_payload | in:byte out:complex |
| `blocks_add_xx` | 多路相加 | type/num_inputs | in:N out:1 |
| `blocks_throttle` | 节流(实时演示用) | type/samp_rate | 1:1 |
| `blocks_head` | 取前 N 个样本(仿真截断) | type/num_items | 1:1 |

## 信道
| key | 用途 | 关键参数 |
|-----|------|----------|
| `channels_channel_model` | AWGN/频偏/多径综合信道 | noise_voltage/freq_offset/epsilon/taps |

## 汇 / 显示
| key | 用途 | 关键参数 |
|-----|------|----------|
| `blocks_file_sink` | 落盘 IQ(供离线读指标/画图) | type/file |
| `blocks_null_sink` | 丢弃 | type |
| `qtgui_time_sink_x` | 时域波形显示 | — |
| `qtgui_freq_sink_x` | 频谱显示 | — |

## 端口类型速记
- `complex` = complex64(基带 IQ 主类型);`float` = float32;`byte` = 无符号字节(比特流)。
- 连接两端类型必须相同;调制器把 byte 变 complex,是链路里的类型转换点。
