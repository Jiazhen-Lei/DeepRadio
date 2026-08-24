# macOS GNU Radio BLE Beacon 发射（PlutoSDR）

本示例用 PlutoSDR 循环播放预先生成的 BLE 复基带波形。

流图和 Python 使用 **PlutoSDR Sink**（`iio_pluto_sink` / `iio.fmcomms2_sink_fc32`），不是 UHD/B210。不要运行 `uhd_find_devices`。

USB 网络模式、`config.txt` 和 `iio_info` 检查见 `DeepRadio硬件连接指南.md` 的「连接 PlutoSDR」。macOS 上 Pluto 必须使用 `usb_ethernet_mode = ncm`。

## 本目录文件

| 文件 | 作用 |
|---|---|
| `ble_beacon_bin_tx_plutosdr.grc` | GNU Radio 流图 |
| `ble_beacon_bin_tx_plutosdr.py` | 由流图生成的 Python |
| `ble_beacon_localname_radiomaster.bin` | BLE IQ 波形（与射频前端无关，文件名表示 Complete Local Name 为 radiomaster） |

## 信号参数

- GNU Radio: 3.10.12.0（conda 环境 `gnuradio`）
- 输入格式：小端交错 `float32` I/Q（`complex64`）
- 波形文件（与本 README 同目录）：`ble_beacon_localname_radiomaster.bin`
- 复采样点数：200,000
- 采样率：2 MS/s
- 文件/重复周期：100 ms
- 波形对应的 BLE PHY：LE 1M GFSK
- BLE 广告信道：38
- 射频中心频率：2426 MHz
- Pluto IIO context URI：留空（自动调用 `iio.get_pluto_uri()`）
- CPU 格式：`fc32`
- 发射衰减：30 dB（Pluto 的 attenuation；数值越大，射频功率越低）

波形含 464 个非零复采样，其后为零填充。以 2 MS/s 完整重复这 200,000 点，即可保持内嵌的 100 ms beacon 间隔。

## 运行

接好 PlutoSDR，在 TX 口接天线或额定 50 Ω 负载，先确认 IIO：

```bash
conda activate gnuradio
iio_info -S
```

正常设备会显示 `Analog Devices Inc. PlutoSDR` 以及 `usb:...` URI。`iio_info -u ip:192.168.2.1` 可选；macOS 上常常走 USB 后端，而不是 `192.168.2.1`。

在本目录打开并运行流图：

```bash
cd /.../example_macos_ble_becon_plutosdr
conda activate gnuradio
gnuradio-companion ble_beacon_bin_tx_plutosdr.grc
```

也可以在本目录直接运行生成的 Python：

```bash
conda activate gnuradio
python ble_beacon_bin_tx_plutosdr.py
```

PlutoSDR Sink 的 URI 保持为空。近距离或传导测试时应把 `tx_attenuation` 调到大于 30 dB。本示例会在 2426 MHz 上连续发射，仅在授权实验环境中使用。
