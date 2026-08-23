"""recipes.py:通信任务配方库。

一个 ``Recipe`` 把\"通信意图\"编码成可被 build_tools 逐步执行的结构:

    - ``blocks``:有序的 (key, id, params) —— 对应 add_block 调用序列
    - ``connections``:(src_id, dst_id[, src_port, dst_port]) —— 对应 connect
    - ``knobs``:对话式调参暴露的关键旋钮(id.param -> 直观含义 + 建议区间)
    - ``metrics``:该链路适合看的指标(evm/ber/eye/spectrum)
    - ``keywords``:意图匹配用的关键词(离线 baseline 无需 LLM 也能选型)

所有块 key 与参数名都取自本仓库已验证的链路(与 tools/build_tools 的建图动作一致)。
配方是\"骨架 + 安全默认值\";专家想改的旋钮通过 ``knobs`` 暴露给协商层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# (src_id, dst_id, src_port, dst_port);后两个可省略(默认 0)
Conn = Tuple[str, str]


@dataclass
class Recipe:
    """一条可执行的通信链路配方。"""

    name: str
    title: str
    difficulty: str                       # "T1" | "T2" | "T3"
    summary: str
    blocks: List[Tuple[str, str, Dict[str, str]]]
    connections: List[tuple]
    knobs: Dict[str, str] = field(default_factory=dict)
    metrics: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    #: 仿真时的 file_sink 探针 id(design_link 会据此配 run_simulation)
    probe_block_id: Optional[str] = None
    tx_probe_block_id: Optional[str] = None
    probe_dtype: str = "complex64"
    sps: int = 4

    def score(self, text: str) -> int:
        """意图文本命中关键词的次数(离线选型用)。"""
        low = (text or "").lower()
        return sum(1 for k in self.keywords if k.lower() in low)


# ---------------------------------------------------------------------------
# 公共片段:采样率 / 每符号样本 / BPSK 星座 变量
# ---------------------------------------------------------------------------
def _common_vars(samp_rate: str = "1000000", sps: str = "4"):
    return [
        ("variable", "samp_rate", {"value": samp_rate}),
        ("variable", "sps", {"value": sps}),
    ]


# ---------------------------------------------------------------------------
# T2:BPSK 过 AWGN 看星座/EVM(教学主力,已在 core 自检验证)
# ---------------------------------------------------------------------------
_RECIPE_BPSK_AWGN = Recipe(
    name="bpsk_awgn",
    title="BPSK 基带 + AWGN 信道",
    difficulty="T2",
    summary="随机比特 -> BPSK 星座调制(RRC 成形)-> 加性高斯白噪声 -> 采集 IQ,"
            "适合观察星座/EVM 随噪声退化。",
    blocks=[
        *_common_vars(),
        ("variable_constellation", "bpsk_const", {"type": "bpsk"}),
        ("analog_random_source_x", "src",
         {"type": "byte", "min": "0", "max": "2",
          "num_samps": "1000", "repeat": "True"}),
        ("digital_constellation_modulator", "mod",
         {"constellation": "bpsk_const", "differential": "False",
          "samples_per_symbol": "sps", "excess_bw": "0.35"}),
        ("channels_channel_model", "chan",
         {"noise_voltage": "0.05", "freq_offset": "0.0",
          "epsilon": "1.0", "taps": "1.0", "seed": "0"}),
        ("blocks_head", "head", {"type": "complex", "num_items": "8192"}),
        ("blocks_file_sink", "sink",
         {"type": "complex", "file": "__PROBE__"}),
    ],
    connections=[
        ("src", "mod"), ("mod", "chan"), ("chan", "head"), ("head", "sink"),
    ],
    knobs={
        "chan.noise_voltage": "噪声强度;越大星座越散、EVM 越高。建议 0.01~0.5",
        "mod.excess_bw": "RRC 滚降系数;越大带宽越宽、码间串扰越小。建议 0.2~0.5",
        "sps.value": "每符号样本数;影响过采样与眼图张开度。建议 2~8",
        "chan.freq_offset": "归一化频偏;非零会让星座旋转。诊断载波恢复用",
    },
    metrics=["evm", "constellation", "eye", "spectrum"],
    keywords=["bpsk", "awgn", "星座", "噪声", "调制", "误差", "evm", "基带"],
    probe_block_id="sink",
    sps=4,
)

# ---------------------------------------------------------------------------
# T2':QPSK 过 AWGN(在 BPSK 基础上换星座,验证多进制扩展)
# ---------------------------------------------------------------------------
_RECIPE_QPSK_AWGN = Recipe(
    name="qpsk_awgn",
    title="QPSK 基带 + AWGN 信道",
    difficulty="T2",
    summary="随机比特 -> QPSK 星座调制 -> AWGN -> 采集 IQ。每符号 2 bit,"
            "同噪声下比 BPSK 更易出错,适合对比星座/EVM。",
    blocks=[
        *_common_vars(),
        ("variable_constellation", "qpsk_const", {"type": "qpsk"}),
        ("analog_random_source_x", "src",
         {"type": "byte", "min": "0", "max": "4",
          "num_samps": "1000", "repeat": "True"}),
        ("digital_constellation_modulator", "mod",
         {"constellation": "qpsk_const", "differential": "False",
          "samples_per_symbol": "sps", "excess_bw": "0.35"}),
        ("channels_channel_model", "chan",
         {"noise_voltage": "0.05", "freq_offset": "0.0",
          "epsilon": "1.0", "taps": "1.0", "seed": "0"}),
        ("blocks_head", "head", {"type": "complex", "num_items": "8192"}),
        ("blocks_file_sink", "sink",
         {"type": "complex", "file": "__PROBE__"}),
    ],
    connections=[
        ("src", "mod"), ("mod", "chan"), ("chan", "head"), ("head", "sink"),
    ],
    knobs={
        "chan.noise_voltage": "噪声强度;QPSK 判决边界更密,对噪声更敏感。建议 0.01~0.3",
        "mod.excess_bw": "RRC 滚降系数。建议 0.2~0.5",
        "chan.freq_offset": "归一化频偏;QPSK 星座会整体旋转 45°的整数倍",
    },
    metrics=["evm", "constellation", "eye", "spectrum"],
    keywords=["qpsk", "四相", "awgn", "星座", "噪声", "evm"],
    probe_block_id="sink",
    sps=4,
)

# ---------------------------------------------------------------------------
# T2:Self-contained BPSK receiver chain for receiver construction tasks
# ---------------------------------------------------------------------------
_RECIPE_RX_BPSK_AWGN = Recipe(
    name="rx_bpsk_awgn",
    title="BPSK AWGN 接收机",
    difficulty="T2",
    summary="自包含 BPSK 激励与 AWGN 信道，经星座接收机完成载波跟踪和判决。",
    blocks=[
        *_common_vars(),
        ("variable_constellation", "bpsk_const", {"type": "bpsk"}),
        (
            "analog_random_source_x",
            "src",
            {
                "type": "byte",
                "min": "0",
                "max": "2",
                "num_samps": "1000",
                "repeat": "True",
            },
        ),
        ("blocks_head", "tx_head", {"type": "byte", "num_items": "2048"}),
        ("blocks_file_sink", "tx_sink", {"type": "byte", "file": "__TX_PROBE__"}),
        (
            "digital_constellation_modulator",
            "mod",
            {
                "constellation": "bpsk_const",
                "differential": "False",
                "samples_per_symbol": "sps",
                "excess_bw": "0.35",
            },
        ),
        (
            "channels_channel_model",
            "chan",
            {
                "noise_voltage": "0.05",
                "freq_offset": "0.0",
                "epsilon": "1.0",
                "taps": "1.0",
                "seed": "0",
            },
        ),
        (
            "digital_pfb_clock_sync_xxx",
            "clock_sync",
            {
                "type": "ccf",
                "sps": "sps",
                "loop_bw": "0.0628",
                "taps": (
                    "firdes.root_raised_cosine(32, 32, "
                    "1.0/float(sps), 0.35, 11*sps*32)"
                ),
                "filter_size": "32",
                "init_phase": "16",
                "max_rate_deviation": "1.5",
                "osps": "1",
            },
        ),
        (
            "digital_constellation_receiver_cb",
            "rx",
            {
                "constellation": "bpsk_const",
                "loop_bw": "0.0628",
                "fmin": "-0.25",
                "fmax": "0.25",
            },
        ),
        ("blocks_head", "head", {"type": "byte", "num_items": "2048"}),
        ("blocks_file_sink", "sink", {"type": "byte", "file": "__PROBE__"}),
    ],
    connections=[
        ("src", "tx_head"),
        ("tx_head", "tx_sink"),
        ("src", "mod"),
        ("mod", "chan"),
        ("chan", "clock_sync"),
        ("clock_sync", "rx"),
        ("rx", "head"),
        ("head", "sink"),
    ],
    knobs={
        "rx.loop_bw": "载波环路带宽；跟踪速度与噪声抑制折中。",
        "chan.noise_voltage": "接收机输入噪声强度。",
        "chan.freq_offset": "用于验证接收机载波跟踪范围。",
    },
    metrics=["ber"],
    keywords=[
        "接收机", "receiver", "解调",
        "定时恢复", "时钟同步", "pfb", "判决",
        "constellation_receiver", "自包含",
    ],
    probe_block_id="sink",
    tx_probe_block_id="tx_sink",
    probe_dtype="uint8",
    sps=1,
)

# ---------------------------------------------------------------------------
# T1:纯基带正弦 + 噪声(最简,给零基础用户\"先看到东西\")
# ---------------------------------------------------------------------------
_RECIPE_TONE_NOISE = Recipe(
    name="tone_noise",
    title="单音信号 + 噪声(入门)",
    difficulty="T1",
    summary="一个复正弦音叠加高斯噪声 -> 采集 IQ,用于最直观地演示"
            "\"信号 + 噪声\"与频谱,门槛最低。",
    blocks=[
        *_common_vars(),
        ("analog_sig_source_x", "sig",
         {"type": "complex", "samp_rate": "samp_rate", "waveform": "analog.GR_COS_WAVE",
          "freq": "100000", "amplitude": "1.0"}),
        ("analog_noise_source_x", "noise",
         {"type": "complex", "noise_type": "analog.GR_GAUSSIAN",
          "amplitude": "0.1", "seed": "0"}),
        ("blocks_add_xx", "add", {"type": "complex", "num_inputs": "2"}),
        ("blocks_head", "head", {"type": "complex", "num_items": "8192"}),
        ("blocks_file_sink", "sink",
         {"type": "complex", "file": "__PROBE__"}),
    ],
    connections=[
        ("sig", "add", 0, 0), ("noise", "add", 0, 1),
        ("add", "head"), ("head", "sink"),
    ],
    knobs={
        "noise.amplitude": "噪声幅度;越大频谱本底越高。建议 0.05~0.5",
        "sig.freq": "单音频率(Hz);决定频谱峰位置",
    },
    metrics=["spectrum", "constellation"],
    keywords=["正弦", "单音", "tone", "噪声", "频谱", "入门", "sine", "信号加噪"],
    probe_block_id="sink",
    sps=1,
)

# ---------------------------------------------------------------------------
# T3:OFDM 发射链(占位骨架,预留;参数取 gr-digital ofdm_tx 常见默认)
# ---------------------------------------------------------------------------
_RECIPE_OFDM = Recipe(
    name="ofdm_awgn",
    title="OFDM 发射 + AWGN(进阶,占位)",
    difficulty="T3",
    summary="随机字节 -> OFDM 发射(FFT 64, CP 16)-> AWGN -> 采集 IQ。"
            "预留骨架:多载波抗多径,适合看 PAPR/子载波频谱。",
    blocks=[
        *_common_vars(samp_rate="1000000"),
        ("analog_random_source_x", "src",
         {"type": "byte", "min": "0", "max": "256",
          "num_samps": "10000", "repeat": "True"}),
        ("digital_ofdm_tx", "ofdm",
         {"fft_len": "64", "cp_len": "16", "packet_length_tag_key": '"packet_len"',
          "bps_header": "1", "bps_payload": "2", "rolloff": "0"}),
        ("channels_channel_model", "chan",
         {"noise_voltage": "0.02", "freq_offset": "0.0",
          "epsilon": "1.0", "taps": "1.0", "seed": "0"}),
        ("blocks_head", "head", {"type": "complex", "num_items": "16384"}),
        ("blocks_file_sink", "sink",
         {"type": "complex", "file": "__PROBE__"}),
    ],
    connections=[
        ("src", "ofdm"), ("ofdm", "chan"), ("chan", "head"), ("head", "sink"),
    ],
    knobs={
        "ofdm.fft_len": "子载波数;越大频率分辨率越高、PAPR 风险越大。常见 64/128",
        "ofdm.cp_len": "循环前缀长度;抗多径能力 vs 开销权衡。常取 fft_len/4",
        "chan.noise_voltage": "噪声强度。建议 0.01~0.1",
    },
    metrics=["spectrum", "constellation"],
    keywords=["ofdm", "多载波", "子载波", "fft", "循环前缀", "cp", "papr"],
    probe_block_id="sink",
    sps=1,
)


RECIPES: Dict[str, Recipe] = {
    r.name: r for r in (
        _RECIPE_TONE_NOISE,
        _RECIPE_BPSK_AWGN,
        _RECIPE_QPSK_AWGN,
        _RECIPE_RX_BPSK_AWGN,
        _RECIPE_OFDM,
    )
}


def list_recipes() -> List[dict]:
    """列出所有配方的元信息(供 UI / knowledge 查询)。"""
    return [{"name": r.name, "title": r.title, "difficulty": r.difficulty,
             "summary": r.summary, "metrics": r.metrics} for r in
            RECIPES.values()]


def get_recipe(name: str) -> Optional[Recipe]:
    return RECIPES.get((name or "").lower().strip())


#: 含这些词时优先选 rx_* 接收配方,避免「BPSK/AWGN/星座」把发射 recipe 分打得更高。
_RX_HINTS = (
    "接收机", "receiver", "解调", "定时恢复", "时钟同步",
    "判决", "clock_sync", "constellation_receiver", "pfb_clock",
)
_TX_LINK_RECIPES = frozenset({"bpsk_awgn", "qpsk_awgn", "ofdm_awgn"})


def wants_receiver(intent: str) -> bool:
    low = (intent or "").lower()
    return any(hint.lower() in low for hint in _RX_HINTS)


def match_recipe(intent: str, default: str = "bpsk_awgn") -> Recipe:
    """离线选型:按关键词命中数挑最合适的配方;全不中回落 default。

    用户明确要接收机时,给 ``rx_*`` 加分并压低发射链路配方,避免 Task 3
    那种「BPSK AWGN 接收机」被 ``bpsk_awgn`` 抢走。
    没有接收机动词时压低 ``rx_*``,避免 Task 1 被误建成接收机。
    """
    wants_rx = wants_receiver(intent)
    best, best_score = None, 0
    for recipe in RECIPES.values():
        score = recipe.score(intent)
        if wants_rx:
            if recipe.name.startswith("rx_"):
                score += 10
            elif recipe.name in _TX_LINK_RECIPES:
                score -= 5
        elif recipe.name.startswith("rx_"):
            score -= 20
        if score > best_score:
            best, best_score = recipe, score
    return best if best is not None else RECIPES[default]


def resolve_recipe(intent: str = "", recipe: str = "") -> Recipe:
    """显式 recipe 仍受意图约束:没有接收机动词时不允许落到 rx_*。"""
    selected = get_recipe(recipe) if recipe else None
    wants_rx = wants_receiver(intent)
    if selected is None:
        return match_recipe(intent)
    if selected.name.startswith("rx_") and not wants_rx:
        return match_recipe(intent or "bpsk awgn")
    if wants_rx and selected.name in _TX_LINK_RECIPES:
        return get_recipe("rx_bpsk_awgn") or selected
    return selected
