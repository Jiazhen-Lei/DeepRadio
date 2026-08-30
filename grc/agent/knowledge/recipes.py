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
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re

RECIPE_INDEX_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "grc-build"
    / "references"
    / "recipe_index.md"
)

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
    title="BPSK Baseband + AWGN Channel",
    difficulty="T2",
    summary="Random bits -> BPSK constellation modulation (RRC shaping) -> additive white Gaussian noise -> IQ capture; "
            "suited to observing constellation and EVM degradation with noise.",
    blocks=[
        *_common_vars(),
        ("variable_constellation", "bpsk_const", {
            "type": "bpsk", "const_points": "[-1+0j, 1+0j]",
            "rot_sym": "2", "sym_map": "[0, 1]",
        }),
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
        "chan.noise_voltage": "Noise strength; higher values spread the constellation and increase EVM. Suggested: 0.01–0.5",
        "mod.excess_bw": "RRC roll-off; higher values widen bandwidth and reduce inter-symbol interference. Suggested: 0.2–0.5",
        "sps.value": "Samples per symbol; affects oversampling and eye opening. Suggested: 2–8",
        "chan.freq_offset": "Normalized frequency offset; nonzero values rotate the constellation. Useful for carrier-recovery diagnosis",
    },
    metrics=["evm", "constellation", "spectrum"],
    keywords=["bpsk", "awgn", "星座", "噪声", "调制", "误差", "evm", "基带"],
    probe_block_id="sink",
    sps=4,
)

# ---------------------------------------------------------------------------
# T2':QPSK 过 AWGN(在 BPSK 基础上换星座,验证多进制扩展)
# ---------------------------------------------------------------------------
_RECIPE_QPSK_AWGN = Recipe(
    name="qpsk_awgn",
    title="QPSK Baseband + AWGN Channel",
    difficulty="T2",
    summary="Random bits -> QPSK constellation modulation -> AWGN -> IQ capture. With two bits per symbol, "
            "it is more error-prone than BPSK at the same noise level and supports constellation/EVM comparison.",
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
        "chan.noise_voltage": "Noise strength; QPSK decision boundaries are denser and more noise-sensitive. Suggested: 0.01–0.3",
        "mod.excess_bw": "RRC roll-off. Suggested: 0.2–0.5",
        "chan.freq_offset": "Normalized frequency offset; the QPSK constellation rotates in integer multiples of 45°",
    },
    metrics=["evm", "constellation", "spectrum"],
    keywords=["qpsk", "四相", "awgn", "星座", "噪声", "evm"],
    probe_block_id="sink",
    sps=4,
)

# ---------------------------------------------------------------------------
# TX-only: modulator to file sink, no AWGN / EVM loop
# ---------------------------------------------------------------------------
def _tx_only_recipe(
    name: str,
    title: str,
    constellation: str,
    src_max: str,
    keywords: List[str],
) -> Recipe:
    const_id = f"{constellation}_const"
    return Recipe(
        name=name,
        title=title,
        difficulty="T2",
        summary=f"Random bits -> {constellation.upper()} constellation modulation -> IQ capture, without a channel model.",
        blocks=[
            *_common_vars(),
            ("variable_constellation", const_id, {"type": constellation}),
            (
                "analog_random_source_x",
                "src",
                {
                    "type": "byte",
                    "min": "0",
                    "max": src_max,
                    "num_samps": "1000",
                    "repeat": "True",
                },
            ),
            (
                "digital_constellation_modulator",
                "mod",
                {
                    "constellation": const_id,
                    "differential": "False",
                    "samples_per_symbol": "sps",
                    "excess_bw": "0.35",
                },
            ),
            ("blocks_head", "head", {"type": "complex", "num_items": "8192"}),
            ("blocks_file_sink", "sink", {"type": "complex", "file": "__PROBE__"}),
        ],
        connections=[("src", "mod"), ("mod", "head"), ("head", "sink")],
        knobs={
            "mod.excess_bw": "RRC roll-off. Suggested: 0.2–0.5",
            "sps.value": "Samples per symbol. Suggested: 2–8",
        },
        metrics=[],
        keywords=keywords,
        probe_block_id="sink",
        sps=4,
    )


_RECIPE_BPSK_TX = _tx_only_recipe(
    "bpsk_tx",
    "BPSK Transmitter (No Channel)",
    "bpsk",
    "2",
    ["bpsk", "发射机", "transmitter", "tx", "发射链"],
)
_RECIPE_QPSK_TX = _tx_only_recipe(
    "qpsk_tx",
    "QPSK Transmitter (No Channel)",
    "qpsk",
    "4",
    ["qpsk", "四相", "发射机", "transmitter", "tx", "发射链"],
)

# ---------------------------------------------------------------------------
# T2:Self-contained BPSK receiver chain for receiver construction tasks
# ---------------------------------------------------------------------------
_RECIPE_RX_BPSK_AWGN = Recipe(
    name="rx_bpsk_awgn",
    title="BPSK AWGN Receiver",
    difficulty="T2",
    summary="Self-contained BPSK stimulus and AWGN channel with carrier tracking and symbol decisions through a constellation receiver.",
    blocks=[
        *_common_vars(),
        ("variable_constellation", "bpsk_const", {
            "type": "bpsk", "const_points": "[-1+0j, 1+0j]",
            "rot_sym": "2", "sym_map": "[0, 1]",
        }),
        (
            "analog_random_source_x",
            "src",
            {
                "type": "byte",
                "min": "0",
                "max": "256",
                "num_samps": "1000",
                "repeat": "True",
            },
        ),
        ("blocks_head", "tx_head", {"type": "byte", "num_items": "8192"}),
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
                    "firdes.root_raised_cosine(32, 32 * sps, "
                    "1.0, 0.35, 11 * sps * 32)"
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
        ("blocks_head", "head", {"type": "byte", "num_items": "8192"}),
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
        "rx.loop_bw": "Carrier-loop bandwidth; trades tracking speed against noise suppression.",
        "chan.noise_voltage": "Receiver input-noise strength.",
        "chan.freq_offset": "Used to verify the receiver's carrier-tracking range.",
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
    sps=4,
)

# ---------------------------------------------------------------------------
# T1:纯基带正弦 + 噪声(最简,给零基础用户\"先看到东西\")
# ---------------------------------------------------------------------------
_RECIPE_TONE_NOISE = Recipe(
    name="tone_noise",
    title="Tone + Noise (Introductory)",
    difficulty="T1",
    summary="A complex sinusoidal tone plus Gaussian noise -> IQ capture, providing an accessible demonstration "
            "of signal, noise, and spectrum.",
    blocks=[
        *_common_vars(),
        ("analog_sig_source_x", "sig",
         {"type": "complex", "samp_rate": "samp_rate", "waveform": "analog.GR_COS_WAVE",
          "freq": "100000", "amp": "1.0"}),
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
        "noise.amplitude": "Noise amplitude; higher values raise the spectral floor. Suggested: 0.05–0.5",
        "sig.freq": "Tone frequency (Hz); determines the spectrum-peak position",
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
    title="OFDM Transmitter + AWGN (Advanced Placeholder)",
    difficulty="T3",
    summary="Random bytes -> OFDM transmitter (FFT 64, CP 16) -> AWGN -> IQ capture. "
            "Placeholder structure for multicarrier multipath resistance and PAPR/subcarrier-spectrum observation.",
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
        "ofdm.fft_len": "Subcarrier count; larger values improve frequency resolution and increase PAPR risk. Common values: 64/128",
        "ofdm.cp_len": "Cyclic-prefix length; trades multipath tolerance against overhead. Often fft_len/4",
        "chan.noise_voltage": "Noise strength. Suggested: 0.01–0.1",
    },
    metrics=["spectrum", "constellation"],
    keywords=["ofdm", "多载波", "子载波", "fft", "循环前缀", "cp", "papr"],
    probe_block_id="sink",
    sps=1,
)


RECIPES: Dict[str, Recipe] = {
    r.name: r for r in (
        _RECIPE_TONE_NOISE,
        _RECIPE_BPSK_TX,
        _RECIPE_QPSK_TX,
        _RECIPE_BPSK_AWGN,
        _RECIPE_QPSK_AWGN,
        _RECIPE_RX_BPSK_AWGN,
        _RECIPE_OFDM,
    )
}


def guess_modulation(recipe_name: str) -> str:
    """Map a recipe name to a modulation token used by EVM/BER and spec."""
    n = (recipe_name or "").lower()
    if "qpsk" in n:
        return "qpsk"
    if "bpsk" in n:
        return "bpsk"
    if "ofdm" in n:
        return "ofdm"
    return ""


def render_recipe_index() -> str:
    """Markdown index consumed by grc-build skill; keep in lockstep with RECIPES."""
    lines = [
        "# 配方索引(由 knowledge/recipes.py 生成,勿手改)",
        "",
        "选型:`match_recipe` 按关键词命中;全不中回落 `bpsk_awgn`。",
        "仅 `build_tx` 且意图不含信道/EVM/BER/眼图时,`covering_recipe` 改用 `bpsk_tx` / `qpsk_tx`。",
        "含 `hardware_configure` / `diagnose` / `modify_project` 时 covering 返回空,不套基带配方。",
        "",
    ]
    for recipe in RECIPES.values():
        metrics = ", ".join(recipe.metrics) if recipe.metrics else "(无默认指标)"
        lines.extend(
            [
                f"## {recipe.name}  ({recipe.difficulty})",
                f"- 标题: {recipe.title}",
                f"- 摘要: {recipe.summary}",
                f"- 关键词: {', '.join(recipe.keywords)}",
                f"- 指标: {metrics}",
                f"- 块数: {len(recipe.blocks)}",
            ]
        )
        if recipe.knobs:
            lines.append("- 可调旋钮:")
            for key, desc in recipe.knobs.items():
                lines.append(f"  - `{key}`: {desc}")
        lines.append("")
    return "\n".join(lines)


def list_recipes() -> List[dict]:
    """列出所有配方的元信息(供 UI / knowledge 查询)。"""
    return [{"name": r.name, "title": r.title, "difficulty": r.difficulty,
             "summary": r.summary, "metrics": r.metrics} for r in
            RECIPES.values()]


def get_recipe(name: str) -> Optional[Recipe]:
    return RECIPES.get((name or "").lower().strip())


_RX_HINTS = (
    "接收机", "receiver", "解调", "定时恢复", "时钟同步",
    "判决", "clock_sync", "constellation_receiver", "pfb_clock",
)
_TX_LINK_RECIPES = frozenset({"bpsk_awgn", "qpsk_awgn", "ofdm_awgn"})
_TX_ONLY_RECIPES = frozenset({"bpsk_tx", "qpsk_tx"})
_BASEBAND_EXCLUSIVE = frozenset({
    "diagnose", "modify_project", "protocol", "hardware_configure",
})


def wants_receiver(intent: str) -> bool:
    low = (intent or "").lower()
    return any(hint.lower() in low for hint in _RX_HINTS)


def _intent_score(recipe: Recipe, intent: str) -> int:
    score = recipe.score(intent)
    stem = recipe.name.split("_")[0]
    if stem and re.search(
        rf"(?<![a-z0-9_]){re.escape(stem)}(?![a-z0-9_])",
        (intent or "").lower(),
    ):
        score += 5
    if wants_receiver(intent):
        if recipe.name.startswith("rx_"):
            score += 10
        elif recipe.name in _TX_LINK_RECIPES:
            score -= 5
    elif recipe.name.startswith("rx_"):
        score -= 20
    return score


def match_recipe(intent: str, default: str = "bpsk_awgn") -> Recipe:
    """按关键词与调制名选型；接收机动词抬升 rx_*。全不中回落 default。"""
    best, best_score = None, 0
    for recipe in RECIPES.values():
        score = _intent_score(recipe, intent)
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


def covering_recipe(
    intent: str = "",
    capabilities: list | None = None,
    recipe: str = "",
) -> Optional[Recipe]:
    """Return a baseband recipe only when it covers remaining capabilities."""
    caps = set(capabilities or [])
    if caps & _BASEBAND_EXCLUSIVE:
        return None
    selected = resolve_recipe(intent, recipe)
    if selected is None:
        return None
    if _intent_score(selected, intent) <= 0:
        return None
    if "build_rx" in caps and not selected.name.startswith("rx_"):
        return None
    if "build_tx" in caps and selected.name.startswith("rx_"):
        return None
    if (
        "build_tx" in caps
        and "build_rx" not in caps
        and not _wants_channel_or_quality(intent, caps)
    ):
        if selected.name in _TX_LINK_RECIPES:
            selected = get_recipe(selected.name.replace("_awgn", "_tx"))
            if selected is None:
                return None
        elif selected.name not in _TX_ONLY_RECIPES:
            return None
    return selected


def _wants_channel_or_quality(intent: str, capabilities: set) -> bool:
    if "observe" in capabilities:
        return True
    low = (intent or "").lower()
    return any(
        word in low
        for word in (
            "awgn", "噪声", "高斯", "evm", "ber", "眼图", "误码", "信道",
        )
    )
