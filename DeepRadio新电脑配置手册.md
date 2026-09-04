# DeepRadio 新电脑配置手册

适用于 Windows 10/11（x86_64）和 macOS 11 及以上版本（Intel 或 Apple Silicon）。Miniforge 同时支持 Windows、macOS 和 Linux。本项目统一使用 Miniforge，并通过仓库内的 `environment.yml` 创建独立环境。

## 1. 安装 Git

### macOS

在终端执行：

```bash
xcode-select --install
git --version
```

### Windows

从 [Git for Windows](https://git-scm.com/download/win) 下载安装，安装选项保持默认。安装完成后重新打开终端并验证：

```bat
git --version
```

## 2. 安装 Miniforge

下载地址：[Miniforge Releases](https://github.com/conda-forge/miniforge/releases/latest)

### macOS

```bash
curl -fsSLo Miniforge3.sh "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-$(uname -m).sh"
bash Miniforge3.sh
```

安装时接受默认路径，并选择初始化 Conda。安装完成后关闭并重新打开终端：

```bash
conda --version
```

### Windows

下载并运行 `Miniforge3-Windows-x86_64.exe`，选择仅为当前用户安装并保持默认路径。随后从开始菜单打开 **Miniforge Prompt**：

```bat
conda --version
```

## 3. 克隆项目并创建环境

以下命令均在 macOS 终端或 Windows 的 **Miniforge Prompt** 中执行：

```bash
git clone --branch jensen-dev-single --single-branch https://github.com/Jiazhen-Lei/DeepRadio.git
cd DeepRadio
conda env create -f environment.yml
conda activate gnuradio
python -c "import gnuradio, numpy, langchain_openai; print('Environment OK')"
```

最后一行输出 `Environment OK` 即表示环境可用。

## 4. 配置模型

### macOS

```bash
cp .env.example .env
open -e .env
```

### Windows

```bat
copy .env.example .env
notepad .env
```

至少填写：

```ini
GRC_AGENT_BASE_URL=接口地址
GRC_AGENT_API_KEY=API密钥
GRC_AGENT_MODEL=模型名称
```

不要提交 `.env`。

## 5. 配置 SDR 硬件

只执行实际使用设备对应的小节。连接发射端前，应先接好适用天线或额定 50 Ω 负载，并从低增益开始测试。

### USRP B210

UHD 已随 Conda 环境安装。先激活环境并下载与当前 UHD 匹配的镜像：

```bash
conda activate gnuradio
uhd_images_downloader
```

macOS 不需要额外 USB 驱动。

Windows 需要 WinUSB 驱动：

1. 连接 B210。
2. 下载并运行 [Zadig](https://zadig.akeo.ie/)。
3. 选择 B210 对应设备；必要时启用 `Options > List All Devices`。设备 USB ID 通常以 `2500` 或 `3923` 开头。
4. 目标驱动选择 `WinUSB`，点击 `Install Driver` 或 `Replace Driver`。

使用 USB 3.x 数据线直连电脑，然后检查：

```bash
uhd_find_devices --args "type=b200"
uhd_usrp_probe --args "type=b200"
```

输出包含 `product: B210` 和设备序列号即正常。

### PlutoSDR

libiio 和 GNU Radio IIO 模块已随 Conda 环境安装。

Windows：先断开 PlutoSDR，再安装 Analog Devices 的 [PlutoSDR Windows USB 驱动](https://wiki.analog.com/university/tools/pluto/drivers/windows)。设备的 `config.txt` 使用：

```ini
usb_ethernet_mode = rndis
```

macOS 不需要额外 USB 驱动。打开 PlutoSDR 虚拟磁盘中的 `config.txt`，在已有的 `[SYSTEM]` 段内加入：

```ini
usb_ethernet_mode = ncm
```

保存后安全弹出 PlutoSDR，等待设备重启。随后在Miniforge Prompt中检查：

```bash
iio_info -S
```

输出包含 `Analog Devices Inc. PlutoSDR` 即正常。GNU Radio 中 PlutoSDR Source/Sink 的 `IIO context URI` 保持为空。

## 6. 启动 DeepRadio

每次启动前先进入项目目录并激活环境。

### macOS

```bash
cd /项目路径/DeepRadio
conda activate gnuradio
PYTHONPATH="$PWD" python -m grc --gtk --fresh
```

### Windows

在 **Miniforge Prompt** 中执行：

```bat
cd /d C:\项目路径\DeepRadio
conda activate gnuradio
set PYTHONPATH=%CD%
python -m grc --gtk --fresh
```

GRC 打开并显示 DeepRadio 面板即启动成功。生成文件和会话记录分别保存在：

```text
local/output/<session_id>/
local/agent_sessions/<session_id>/
```

## 7. 最小验收

1. 在 DeepRadio 面板输入：`创建一个 BPSK 仿真链路并验证输出。`
2. 确认 Workflow 能生成并完成离线验证。
3. 使用硬件时，先确认 `uhd_usrp_probe` 或 `iio_info -S` 能发现设备，再进入真实 RF 阶段。

若设备无法发现，先检查驱动、USB 数据线、接口和设备模式，不要先修改流图参数。
