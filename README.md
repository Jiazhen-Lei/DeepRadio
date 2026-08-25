# DeepRadio

From: jensenlei, cindysha, sihanwang

## 介绍

DeepRadio 在 GNU Radio Companion 之上增加一层「自然语言意图 → 可运行流图」能力。
**主路径不是 LLM 直接写** `.grc`，而是：

```
用户文本
  → ServiceAgent.step()
      → WorkflowEngine（任务类型 / Stage / Checkpoint）
      → SharedState（规格 / 工程 / claims / 协调）
      → PolicyGateway（改图 / 换配方准入）
      → registry.call（单一执行入口；BLE/硬件走确定性 Stage handler）
      → AgentReply（叙述 + artifacts + claims + spec_digest + pending）
  → AgentPanel（对话）+ ClaimsPanel（状态 / 运行时 / 规格）+ Flowgraph 画布
```

未安装 `deepagents` 或未配置 `GRC_AGENT_*` 时，同一套 Workflow / SharedState / 工具链走确定性 Stage handler；通用配方建图仍可降级到 `design_link`。GUI 上勾选「一句话直出(baseline)」才走 `build_flow_graph_from_text`（LLM 直出 YAML，不经 Workflow），仅作论文对照。

### 包布局（`grc/agent`）

活路径（产品默认）：

```
grc/agent/
  workflow/                 控制面：任务目录、Stage 状态机、Completion、Checkpoint
    task_catalog.yaml · schema.py · engine.py · completion.py
  state/                    领域事实
    shared_state.py · claim_store.py · policy.py · snapshot.py
  tools/                    唯一执行入口 registry.call
    registry.py · ble_tools.py · hardware_tools.py · hardware_profiles.py
    state_tools.py · build / critic / sim / knowledge · design_link.py
  service/                  GUI 守门与装配
    adapter.py              ServiceAgent（step / 画布 / 确定性 Stage / 可选 LLM）
    hardware_runtime.py     受控 RF 子进程（与 runtime/simulate 不是同一层）
    session_store.py · orchestrator.py · tools_lc.py · subagents.py
    stage_executor.py
  runtime/simulate.py       无头仿真（EVM / 星座 / 频谱），不是 RF
  skills/                   给 LLM 的渐进式说明书，不是第二套执行器
  knowledge/recipes.py      通用来通信配方（tone_noise / bpsk_awgn / …）
  memory/profile.py         用户画像
  schema.py · env.py · llm.py
  tests/                    契约与回归（进仓）
```

不是活路径：

```
dev_docs/regression/        手工回归脚本（原 grc/agent/examples）
GUI「一句话直出」           build_flow_graph_from_text，论文 baseline
```

GUI：`grc/gui/AgentPanel.py` + `ClaimsPanel.py` + `chat_markup.py`。交付原地刷新当前画布，CONFIRM 不上图；session 内 Ctrl+S 会 `version+1` 并让 Claim 待重验。

### 六层结构

```
L6  Workspace     DeepRadio 对话 · ClaimsPanel（任务/运行时/规格）· 画布
L5  Control plane ServiceAgent + WorkflowEngine：Stage 循环、确认、确定性 handler；
                  可选 deepagents 委派 6 个子代理
L4  Shared State  RadioSpec / ProjectState / ClaimStore / Coordination + PolicyGateway
                  落盘 local/agent_sessions/<id>/state.json · workflow.yaml · events.jsonl
L3  Tools         registry.call（LLM 与确定性路径同一套执行）
L2  Execution     runtime.simulate（无头仿真）· hardware_runtime（受控 RF）
L1  GNU Radio     env.make_platform · 块库 / grcc
```

---



## 快速开始
### 初次使用
```bash
# 1. 创建并激活 gnuradio 环境
conda env create -f environment.yml
conda activate gnuradio

# 2. 配置 Agent API (从模板复制并填写你的 key)
cp .env.example .env
# 编辑 .env, 填入 GRC_AGENT_BASE_URL / GRC_AGENT_API_KEY / GRC_AGENT_MODEL
# 未配置时 Agent 会降级为确定性建图

# 3. 启动 DeepRadio (GTK)
cd DeepRadio          # 确保在项目根目录
PYTHONPATH=$PWD python -m grc.main --gtk --fresh
# 也可用环境变量：GRC_DEEPRADIO_FRESH=1 PYTHONPATH=$PWD python -m grc --gtk
# 不加 --fresh 时仍会按 GRC 偏好恢复上次打开的文件
```
### 后续使用（内部测试，开源要删）
即已有 `gnuradio` 环境时：

```bash
conda activate gnuradio
conda env update -f environment.yml --prune
```

这会按 yml **增量安装/升级**缺失依赖（本次主要是 pip 包 `markdown`），不会重装 GNU Radio。`--prune` 会卸掉 yml 里已删除的包；只想补新包、不想动现有包时可以去掉 `--prune`。

如果暂时拉不到新 yml、只缺某一个 pip 包，激活环境后直接装也可以，例如：

```bash
conda activate gnuradio
pip install markdown
```
---

### 连接 USRP B210

1. 使用支持数据传输的 USB 3.x 线缆连接 B210，尽量直连电脑。发射前，在 `TX/RX` 端接好天线或 50 Ω 负载。

2. 激活 gnuradio 后下载 UHD 镜像：

   ```bash
   conda activate gnuradio
   uhd_images_downloader
   ```

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


### PlutoSDR BLE 硬件测试

1. 接入 PlutoSDR BLE 硬件。

2. 激活 GNU Radio 环境：

   ```bash
   conda activate gnuradio
   ```

3. 检查设备连接：

   ```bash
   iio_info -S
   ```

   没有报错则继续；有报错请阅读《DeepRadio硬件连接指南.md》。

4. 启用受控 RF 并启动 DeepRadio：

   ```bash
   export GRC_AGENT_ENABLE_RF=1
   PYTHONPATH=$PWD python -m grc --gtk --fresh
   ```

5. 点击 DeepRadio 交互栏，输入：

   ```text
   用plutosdr发射一段2.402GHz的ble信号，local name为xxx，目标实现是人工可以用手机软件接收到
   ```

   其中 `xxx` 可以替换为任意广播名称。

6. 等待交互。交互过程中会提供确认或取消选项，确认实验条件无误后点击确认。最后输出的流图如果没有自动发射，请点击 GRC 中的运行箭头；看到左下方进度中出现 `UUU…` 字样后，使用手机端 LightBlue 抓取 BLE 信号，应能发现 local name 为 `xxx` 的信号。


## 自动契约测试

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m unittest discover -s grc/agent/tests -v
```

不启动真实 RF。GUI 人机用例见下；空口 HIL 见 `dev_docs/new/DeepRadio_Test_and_Experiment_V2.md`。

## GUI 任务测试

启动后不要勾选「一句话直出(baseline)」。独立用例先点「重置」。`DIAGNOSE` / `MODIFY_PROJECT` / `OBSERVE` 需先打开已有工程。回退改图用「撤销到上一版本」。

```bash
conda activate gnuradio
PYTHONPATH=$PWD python -m grc --gtk --fresh
```

| Task | 输入 | 期望 |
| --- | --- | --- |
| `END_TO_END_SIM` | `做一个 BPSK 过 AWGN 的基带链路，EVM 小于 10%，显示星座图和频谱。` | Task 为端到端仿真；产出 `.grc`、EVM、星座图、频谱图；EVM 达标才完成。阈值若被追问，答 `10%`。 |
| `TX_BUILD` | `构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件。` | 只建 TX `.grc` 并做结构校验；无设备发现/配置/start。出现 RF 确认即失败。 |
| `RX_BUILD` | `构建一个自包含的 BPSK AWGN 接收机，包含定时恢复和判决，并测 BER。` | 生成接收流图；BER 同时引用发送参考和接收判决 probe。缺 Eb/N0 时答 `8 dB`，并保持同一 `workflow_id`。 |
| `DIAGNOSE` | `诊断当前链路的 EVM，解释主要原因并给出最小修改建议，先保持工程不变。` | 有诊断与 Evidence；`.grc` 哈希、版本和画布不变。若询问修复，选择拒绝。 |
| `MODIFY_PROJECT` | `把当前 BPSK 工程改成 QPSK，其余条件保持一致。` | 确认前工程不变；确认后 version +1，流图变为 QPSK，受影响 Claim 重验。 |
| `OBSERVE` | `查看当前接收信号的频谱和星座图，给出主峰，只观察工程。` | 输出图和指标；工程哈希与版本不变。无需修改确认。 |
| `HARDWARE_CONFIGURE` | `为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。` | 发现并 probe 设备；生成禁用发射的 `.grc`；批准配置、拒绝发射；无 `start_flowgraph` 成功事件。 |

产物：`local/output/`；会话：`local/agent_sessions/gui-*/state.json`。
