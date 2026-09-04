# DeepRadio硬件连接指南

## 连接 USRP B210

1. 使用支持数据传输的 USB 3.x 线缆连接 B210，尽量直连电脑。发射前，在 `TX/RX` 端接好天线或 50 Ω 负载。

2. 激活 Radioconda 并下载 UHD 镜像：

   ```bash
   source ~/radioconda/bin/activate  # macOS
   uhd_images_downloader
   ```

   Windows 请在 **Radioconda Prompt** 中执行 `uhd_images_downloader`。

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

## 连接 PlutoSDR

PlutoSDR 在不同操作系统下需要使用不同的 USB 网络模式：

| USB 网络模式 | macOS | Windows | Linux |
|---|:---:|:---:|:---:|
| RNDIS（默认） | 不支持 | 支持 | 支持 |
| CDC-NCM | 支持 | 不支持 | 支持 |

PlutoSDR 一次只能启用一种 USB 网络模式。在 macOS 和 Windows 之间切换时，需要编辑 PlutoSDR 虚拟磁盘中的 `config.txt`。`usb_ethernet_mode` 默认可能不在文件中显示；缺少该项即表示使用默认的 RNDIS 模式。

macOS 使用：

```ini
[SYSTEM]
xo_correction =
udc_handle_suspend = 0
usb_ethernet_mode = ncm
```

Windows 使用：

```ini
[SYSTEM]
xo_correction =
udc_handle_suspend = 0
usb_ethernet_mode = rndis
```

应将 `usb_ethernet_mode` 追加到文件中已有的 `[SYSTEM]` 段内，不要重复创建第二个 `[SYSTEM]`。保存后安全弹出 PlutoSDR 虚拟磁盘，并等待设备重启。

GNU Radio 中的 **PlutoSDR Source/Sink** 建议将 `IIO context URI` 留空，使其自动调用 `iio.get_pluto_uri()` 发现设备。这可以适配 IP 地址和 USB 设备地址变化，但不能消除 macOS 与 Windows 对 USB 网络协议的支持差异。

连接后可执行：

```bash
iio_info -S
```

也可以按实际地址测试：

```bash
iio_info -u ip:192.168.2.1
```

如果出现 `Unable to create context`，先检查 USB 网络模式是否与当前操作系统匹配，再检查设备 IP；这通常不是 GNU Radio 采样率或调制参数问题。
