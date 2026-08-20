# DeepRadio
From: jensenlei, cindysha, sihanwang

## 介绍

DeepRadio 在 GNU Radio Companion 之上增加一层「自然语言意图 → 可运行流图」能力。
**主路径不是 LLM 直接写 `.grc`**，而是：

```
用户文本
  → ServiceAgent.step()
      → SharedState（规格 / 工程 / claims / 协调）
      → PolicyGateway（改图 / 换配方准入）
      → registry.call / design_link（单一执行入口）
      → AgentReply（叙述 + artifacts + claims + spec_digest）
  → AgentPanel（对话）+ ClaimsPanel（只读）+ Flowgraph 画布
```

未安装 `deepagents` 或未配置 `GRC_AGENT_*` 时，同一套 SharedState / 工具链会降级走确定性
`design_link`，无 LLM 也能产出 `.grc` 与 Claim-Evidence。GUI 上勾选「一句话直出(baseline)」
才走 LLM 直出 YAML，不经 SharedState，仅作论文对照。

### 包布局（`grc/agent`）

```
grc/agent/
  schema.py                 GUI 契约：AgentReply / ToolInvocation
  env.py · llm.py           Platform 与 GRC_AGENT_* LLM 配置
  knowledge/recipes.py      配方：tone_noise / bpsk_awgn / qpsk_awgn /
                            rx_bpsk_awgn / ofdm_awgn
  runtime/simulate.py       无头仿真、取指标、画图
  memory/profile.py         用户画像
  state/                    共享事实层
    shared_state.py         RadioSpec / ProjectState / Claim / Coordination
    claim_store.py          证据绑定、按版本失效
    policy.py               gate() → ALLOW | PROPOSE | DENY | CONFIRM
    snapshot.py             改图前快照 / 回滚
  tools/                    单一执行入口
    registry.py             @tool 注册 + call()
    design_link.py          选型 → 建图 → 校验 → 仿真 → 验 claim
    state_tools.py          spec_* / verify_claims / apply_grc_diff / configure_sdr
    build / critic / sim / knowledge / debug_by_metric
  skills/                   渐进式披露：grc-spec · grc-block-rag · grc-build
                            · grc-critic · grc-sim · grc-diagnosis · grc-hardware
  service/                  装配与 GUI 守门
    adapter.py              ServiceAgent.step → AgentReply
    orchestrator.py         create_deep_agent
    subagents.py            6 个 Domain Subagent
    tools_lc.py             registry → LangChain @tool
    session_store.py        state.json / snapshots / events.jsonl
```

GUI：`grc/gui/AgentPanel.py` + `grc/gui/ClaimsPanel.py`；画布仍走 `open_flow_graph`。

### 六层结构

```
L6  Workspace     AgentPanel · ClaimsPanel · Flowgraph 画布
L5  MainAgent     ServiceAgent：确认闭环 → 抽规格 → deepagents 或 design_link
                  主 Agent 持有全量工具，也可委派 6 个子代理
L4  Subagents     spec · radio_design · flowgraph · verification · diagnosis · hardware
L3  Shared State  RadioSpec / ProjectState / ClaimStore / Coordination + PolicyGateway
                  落盘 local/agent_sessions/<id>/state.json 与 snapshots/v{N}/
L2  Tools         registry.call / design_link（LLM 与降级走同一套执行）
L1  GNU Radio     env.make_platform · runtime 无头仿真
```
---

## 快速开始

```bash
# 1. 创建并激活 gnuradio 环境
conda env create -f environment.yml
conda activate gnuradio

# 2. 配置 Agent API (从模板复制并填写你的 key)
cp .env.example .env
# 编辑 .env, 填入 GRC_AGENT_BASE_URL / GRC_AGENT_API_KEY / GRC_AGENT_MODEL
# 未配置时 Agent 会降级为确定性建图

# 3. 启动 GRC (GUI)
cd DeepRadio          # 确保在项目根目录
PYTHONPATH=$PWD python -m grc.main --gtk
```

---

## GUI 任务测试

先激活环境并进入仓库根目录，再启动 GRC。右侧 Agent 面板**不要勾选**「一句话直出(baseline)」。
独立任务先点「重置」；Task 4–7 需沿用 Task 1 的会话，不要重置。

```bash
conda activate gnuradio
cd /.../DeepRadio
PYTHONPATH=$PWD python -m grc --gtk
```

| Task | 实现什么 | 对 Agent 的输入 | 期望输出 |
|---|---|---|---|
| 1 端到端仿真 | 建 BPSK+AWGN 基带链路，仿真并验 EVM | `做一个 BPSK 过 AWGN 的基带链路，EVM 要小于 10%，并显示星座图和频谱。` | 打开 `bpsk_awgn.grc`；ClaimsPanel 有 `EVM < 10%`（Passed 或如实 Failed）及 Evidence；有星座图/频谱图 |
| 2 单音发射 | 建单音+噪声并画频谱 | `生成一个 100 kHz 复数单音，加高斯噪声，并画出频谱。` | 选中 `tone_noise`；画布含 signal / noise / add / head / file sink；有频谱图；无 EVM Claim |
| 3 接收机构建 | 建自包含 BPSK 接收机 | `构建一个自包含的 BPSK AWGN 接收机，包含定时恢复和星座接收判决。` | 选中 `rx_bpsk_awgn`；画布含 clock sync 与 constellation receiver；流图可生成、校验、运行。当前无 BER/EVM 接收质量 Claim |
| 4 指标诊断 | 在 Task 1 工程上诊断 EVM（先不要改图） | `诊断当前链路的 EVM，解释主要原因，并给出最小修改建议，先不要修改。` | 回复含实际 EVM；建议指向噪声/频偏/成形；`flowgraph_version` 不变 |
| 5 频谱观测 | 在当前工程上画频谱 | `分析当前接收信号的频谱，给出主峰并显示频谱图。` | 面板内联显示频谱图 |
| 6 修改工程 | 把 Task 1 的 BPSK 改成 QPSK（需确认） | ① `把当前 BPSK 工程改成 QPSK，其余条件不变。` ② `确认` | ① 状态栏 `CONFIRM`，不立即覆盖工程 ② 换成 `qpsk_awgn`，版本 +1，旧 Claim 变为 `NotTested`。输入 `取消修改` 则工程不变 |
| 7 参数调优 | 改噪声电压并重验 EVM | `把 chan.noise_voltage 改为 0.02，重新仿真并验证 EVM < 10%。` | 改前快照；版本 +1；新 Evidence 绑定当前版本 |
| 8 需求澄清 | 信息不足时澄清规格 | `帮我做一个无线通信系统。` | 主路径应追问调制/信道/指标；Spec 摘要出现 `open_questions`，不声称需求已验证 |

产物：用户可见文件在 `local/output/`；会话状态在 `local/agent_sessions/gui-*/state.json`。
