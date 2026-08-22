# Radioconda 配置指南 及 DeepRadio硬件连接指南

## macOS

1. 查看CPU架构：

   ```bash
   uname -m
   ```

2. 下载对应安装器：

   - Apple Silicon：`MacOSX-arm64.sh`
   - Intel：`MacOSX-x86_64.sh`

3. 安装：

   ```bash
   bash ~/Downloads/radioconda-*.sh
   ```

4. 若已有Miniforge或Anaconda，不启用自动初始化；手动进入Radioconda：

   ```bash
   source ~/radioconda/bin/activate
   ```

5. 验证：

   ```bash
   python -c "import numpy, gnuradio; print('OK')"
   ```

6. 启动GNU Radio：

   ```bash
   gnuradio-companion
   ```

## Windows

1. 下载 `radioconda-Windows-x86_64.exe`。
2. 双击安装，选择“仅为当前用户”，保持默认路径。
3. 从开始菜单打开 **Radioconda Prompt**。
4. 验证：

   ```bat
   python -c "import numpy, gnuradio; print('OK')"
   ```

5. 启动GNU Radio：

   ```bat
   gnuradio-companion
   ```

   也可以直接从开始菜单打开 **GNU Radio Companion**。

## 注意事项

- 安装包从 [Radioconda Releases](https://github.com/radioconda/radioconda-installer/releases) 下载。
- 不要把Radioconda与Miniforge、Anaconda等发行版混装到同一个base环境。
- macOS存在多个Conda发行版时，建议仅手动激活Radioconda，避免改变默认base环境。