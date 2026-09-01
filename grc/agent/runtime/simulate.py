"""无头仿真闭环:生成 -> 执行 -> 读数据 -> 算指标。

这是 DeepRadio-Agent 的"反馈引擎"(架构文档创新点 C 的硬核)。
它把一张 ``FlowGraph`` 或一个 ``.grc`` 文件跑成真实的仿真结果
(星座图 / EVM / BER / 频谱 / 眼图),再封装成 :class:`SimResult`
同时回喂给用户(图)和 Agent(文本摘要)。

设计要点
--------
* **无头**: 强制 ``generate_options = no_gui``,不弹 Qt 窗口
  (依赖 ``env.configure_options`` 已填平的 workflow 顺序坑)。
* **可回读**: 结果不只显示在窗口,而是通过 ``blocks_file_sink``
  落到磁盘,跑完用 numpy 读回。
* **限长自停**: 靠 ``blocks_head`` 限制样本数,流图自然结束;
  再叠加墙钟超时兜底,防止无 head 的图跑不完。
* **复用已验证链路**: 接口取自
  ``dev_docs/regression/bpsk_2g4_regression.py`` 的回归实践。

典型用法::

    from grc.agent import env
    from grc.agent.runtime import simulate

    platform = env.make_platform()
    fg = build_my_flow_graph(platform)          # 任意方式建图
    result = simulate.run(fg, platform,
                          probe_block_id="sink") # sink 处已有 file_sink
    print(result.summary)
    result.plot_constellation("/tmp/const.png")
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 默认的仿真墙钟超时(秒)。head 限长通常几百毫秒就跑完,此为兜底。
DEFAULT_TIMEOUT = 30.0

#: 默认采样上限,当图里没有 head 时自动插桩用。
DEFAULT_MAX_ITEMS = 65536


def _is_complex_array(arr) -> bool:
    if arr is None:
        return False
    import numpy as np
    dtype = getattr(arr, "dtype", None)
    if dtype is None:
        return False
    return np.issubdtype(dtype, np.complexfloating)


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------
@dataclass
class SimResult:
    """一次仿真的完整结果。

    Attributes:
        ok: 是否成功跑完并读到数据。
        script_path: 生成的 python 脚本路径。
        out_dir: 输出目录(含脚本、数据文件、图)。
        data: probe_id -> numpy 数组(IQ complex64 或 bit/byte)。
        metrics: 计算出的标量指标(evm/ber/mean_re/...)。
        stderr: 执行脚本时的错误输出(失败诊断用)。
        error: 顶层错误信息(None 表示成功)。
        summary: 供 Agent/LLM 阅读的一句话文本摘要。
    """

    ok: bool = False
    script_path: Optional[str] = None
    out_dir: Optional[str] = None
    data: Dict[str, "object"] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    stderr: str = ""
    error: Optional[str] = None
    summary: str = ""
    aligned_symbols: Optional[object] = None
    symbol_phase: int = 0

    # -- 可视化(惰性导入 matplotlib,无显示后端) ---------------------------
    def plot_constellation(self, path: str, probe_id: Optional[str] = None,
                           sps: int = 1, modulation: str = "") -> Optional[str]:
        """把某个 IQ probe 画成星座散点图,存到 path。返回图路径。"""
        arr = self._pick_complex(probe_id)
        if arr is None:
            return None
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if self.aligned_symbols is not None:
            syms = self.aligned_symbols
        elif int(sps or 1) > 1 and modulation:
            ideal = ideal_points_for(modulation)
            filtered = matched_filter_rrc(arr, sps=sps)
            syms, phase = extract_symbols_best_phase(
                filtered, sps=sps, ideal_points=ideal,
                skip_symbols=15,
            )
            self.aligned_symbols = syms
            self.symbol_phase = phase
        else:
            syms = arr[sps * 4::sps] if sps > 1 else arr
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.scatter(np.real(syms), np.imag(syms), s=6, alpha=0.4)
        ax.axhline(0, color="gray", lw=0.5)
        ax.axvline(0, color="gray", lw=0.5)
        ax.set_aspect("equal")
        ax.set_title("Constellation")
        ax.set_xlabel("In-phase")
        ax.set_ylabel("Quadrature")
        fig.tight_layout()
        fig.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def plot_spectrum(self, path: str, probe_id: Optional[str] = None,
                      samp_rate: float = 1.0) -> Optional[str]:
        """把某个 IQ probe 画成频谱(dBFS),存到 path。返回图路径。"""
        arr = self._pick_complex(probe_id)
        if arr is None:
            return None
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        freqs, spec_db, _n = spectrum_trace(arr, samp_rate)
        fig, ax = plt.subplots(figsize=(5, 3))
        ax.plot(freqs, spec_db, lw=0.8)
        ax.axvline(0, color="gray", lw=0.6, ls="--")
        ax.set_title("Spectrum")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Magnitude (dBFS)")
        fig.tight_layout()
        fig.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def plot_eye(self, path: str, probe_id: Optional[str] = None,
                 sps: int = 4, n_traces: int = 200,
                 span: int = 2) -> Optional[str]:
        """把某个 IQ probe 画成眼图(实部折叠),存到 path。返回图路径。

        Args:
            sps: 每符号样本数,决定折叠周期。
            n_traces: 最多叠加的迹线条数。
            span: 每条迹线横跨几个符号周期。
        """
        arr = self._pick_complex(probe_id)
        if arr is None:
            return None
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        seg = int(sps) * int(span)
        if seg <= 1 or len(arr) < seg * 2:
            return None
        real = arr.real.astype(float)
        # 跳过前若干符号的成形/信道瞬态
        start = int(sps) * 4
        usable = real[start:]
        n = (len(usable) // seg) * seg
        if n < seg:
            return None
        traces = usable[:n].reshape(-1, seg)
        if traces.shape[0] > n_traces:
            traces = traces[:n_traces]
        t = np.linspace(0, span, seg, endpoint=False)
        fig, ax = plt.subplots(figsize=(5, 3))
        for row in traces:
            ax.plot(t, row, color="steelblue", lw=0.4, alpha=0.3)
        ax.set_title("Eye Diagram (I)")
        ax.set_xlabel("Symbol periods")
        ax.set_ylabel("Amplitude")
        ax.grid(True, lw=0.3, alpha=0.5)
        fig.tight_layout()
        fig.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def _pick_complex(self, probe_id: Optional[str]):
        if probe_id is None:
            for v in self.data.values():
                if _is_complex_array(v):
                    return v
            return None
        arr = self.data.get(probe_id)
        return arr if _is_complex_array(arr) else None


# ---------------------------------------------------------------------------
# 星座定义(为 QPSK/OFDM 扩展预留统一入口)
# ---------------------------------------------------------------------------
#: 常见调制的理想星座点(单位平均能量)。key 全小写。
CONSTELLATIONS: Dict[str, List[complex]] = {
    "bpsk": [-1.0 + 0j, 1.0 + 0j],
    "qpsk": [
        (1 + 1j) / math.sqrt(2), (-1 + 1j) / math.sqrt(2),
        (-1 - 1j) / math.sqrt(2), (1 - 1j) / math.sqrt(2),
    ],
    "8psk": [complex(math.cos(2 * math.pi * k / 8),
                     math.sin(2 * math.pi * k / 8)) for k in range(8)],
    "qam16": [complex(i, q) / math.sqrt(10)
              for i in (-3, -1, 1, 3) for q in (-3, -1, 1, 3)],
}


def ideal_points_for(modulation: str) -> List[complex]:
    """按调制名取理想星座点;未知则回落 BPSK。"""
    return CONSTELLATIONS.get((modulation or "").lower().strip(),
                              CONSTELLATIONS["bpsk"])


def extract_symbols(iq, sps: int, skip_symbols: int = 4):
    """从过采样 IQ 里按符号率抽取符号点。

    Args:
        iq: 复数样本序列。
        sps: 每符号样本数。
        skip_symbols: 跳过前若干符号(成形滤波/信道建立瞬态)。
    """
    import numpy as np
    arr = np.asarray(iq, dtype=np.complex64)
    if sps <= 1:
        return arr[skip_symbols:]
    start = int(sps) * int(skip_symbols)
    return arr[start::int(sps)]


def extract_symbols_best_phase(
    iq, sps: int, ideal_points, skip_symbols: int = 4
):
    """Choose the symbol sampling phase with the lowest decision-directed EVM."""
    import numpy as np

    arr = np.asarray(iq, dtype=np.complex64)
    if sps <= 1:
        return arr[skip_symbols:], 0
    base = int(sps) * int(skip_symbols)
    best_symbols = arr[base::int(sps)]
    best_phase = 0
    best_evm = evm_from_symbols(best_symbols, ideal_points)
    for phase in range(1, int(sps)):
        symbols = arr[base + phase::int(sps)]
        candidate = evm_from_symbols(symbols, ideal_points)
        if candidate < best_evm:
            best_symbols = symbols
            best_phase = phase
            best_evm = candidate
    return best_symbols, best_phase


def spectrum_trace(iq, samp_rate: float, fft_size: int = 4096):
    """Hann-windowed, fftshifted spectrum in dBFS. Returns freqs, dbfs, n."""
    import numpy as np

    arr = np.asarray(iq)
    n = min(len(arr), max(32, int(fft_size)))
    window = np.hanning(n)
    spectrum = np.fft.fftshift(np.fft.fft(arr[:n] * window, n=n))
    magnitude = np.abs(spectrum) / max(float(window.sum()), 1e-12)
    magnitude_dbfs = 20.0 * np.log10(np.maximum(magnitude, 1e-12))
    freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / float(samp_rate)))
    return freqs, magnitude_dbfs, n


def spectrum_peak_report(iq, samp_rate: float, fft_size: int = 4096) -> dict:
    """Peak frequency excluding a DC spike, if one is present."""
    import numpy as np

    freqs, magnitude_dbfs, n = spectrum_trace(iq, samp_rate, fft_size)
    dc_bin = int(n // 2)
    peak_bin = int(np.argmax(magnitude_dbfs))
    dc_excluded = False
    if 2 < dc_bin < n - 2:
        neighbors = np.concatenate((
            magnitude_dbfs[dc_bin - 3:dc_bin],
            magnitude_dbfs[dc_bin + 1:dc_bin + 4],
        ))
        if neighbors.size and magnitude_dbfs[dc_bin] > float(np.median(neighbors)) + 6.0:
            masked = np.array(magnitude_dbfs, copy=True)
            masked[dc_bin] = -np.inf
            peak_bin = int(np.argmax(masked))
            dc_excluded = True
    return {
        "valid": True, "metric": "spectrum_peak",
        "frequency_hz": float(freqs[peak_bin]),
        "magnitude_dbfs": float(magnitude_dbfs[peak_bin]),
        "fft_bin": peak_bin, "peak_bin": peak_bin, "fft_size": n,
        "sample_rate": float(samp_rate), "window": "hann",
        "fft_shifted": True, "dc_excluded": dc_excluded,
        "bin_resolution_hz": float(samp_rate) / float(n),
        "peak_interpretation": (
            "strongest_non_dc_bin_after_dc_spike_exclusion"
            if dc_excluded else "strongest_fft_bin"
        ),
        "dc_exclusion_policy": "exclude_center_bin_only_when_6db_above_neighbors",
        "dc_dbfs": float(magnitude_dbfs[dc_bin]) if 0 <= dc_bin < n else None,
    }


def matched_filter_rrc(
    iq, sps: int, excess_bw: float = 0.35, span_symbols: int = 11
):
    """Apply a unit-energy root-raised-cosine receive matched filter."""
    import numpy as np

    arr = np.asarray(iq, dtype=np.complex64)
    if sps <= 1:
        return arr
    beta = float(excess_bw)
    time_axis = np.arange(
        -span_symbols * sps, span_symbols * sps + 1, dtype=float
    ) / float(sps)
    taps = np.empty_like(time_axis)
    for index, value in enumerate(time_axis):
        if abs(value) < 1e-12:
            taps[index] = 1 + beta * (4 / math.pi - 1)
        elif beta and abs(abs(value) - 1 / (4 * beta)) < 1e-9:
            taps[index] = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta))
            )
        else:
            numerator = math.sin(math.pi * value * (1 - beta))
            numerator += (
                4
                * beta
                * value
                * math.cos(math.pi * value * (1 + beta))
            )
            denominator = (
                math.pi * value * (1 - (4 * beta * value) ** 2)
            )
            taps[index] = numerator / denominator
    taps /= max(float(np.sqrt(np.sum(taps * taps))), 1e-12)
    return np.convolve(arr, taps, mode="same")


# ---------------------------------------------------------------------------
# 指标提取器
# ---------------------------------------------------------------------------
def evm_from_symbols(symbols, ideal_points) -> float:
    """按最近理想星座点计算 EVM(%)。

    Args:
        symbols: 复数符号数组(已按符号率抽取、幅度归一化前)。
        ideal_points: 理想星座点列表(复数),如 BPSK=[-1,1]。
    """
    import numpy as np
    syms = np.asarray(symbols, dtype=np.complex64)
    if len(syms) == 0:
        return float("nan")
    # 幅度归一化到理想点的平均能量
    ideal = np.asarray(ideal_points, dtype=np.complex64)
    scale = np.sqrt(np.mean(np.abs(ideal) ** 2)) / \
        max(np.sqrt(np.mean(np.abs(syms) ** 2)), 1e-12)
    syms = syms * scale
    # 每个符号找最近理想点
    dist = np.abs(syms[:, None] - ideal[None, :])
    nearest = ideal[np.argmin(dist, axis=1)]
    err = syms - nearest
    evm = np.sqrt(np.mean(np.abs(err) ** 2)) / \
        max(np.sqrt(np.mean(np.abs(ideal) ** 2)), 1e-12)
    return float(evm * 100.0)


def bits_from_probe(data):
    """把探针字节变成 0/1 比特。打包字节(0–255)按 MSB 解开。"""
    import numpy as np
    arr = np.asarray(data).ravel()
    if arr.size == 0:
        return np.array([], dtype=np.int8)
    ints = np.asarray(np.clip(arr, 0, 255), dtype=np.uint8).ravel()
    if int(ints.max()) > 1:
        return np.unpackbits(np.ascontiguousarray(ints), bitorder="big").astype(np.int8)
    return ints.astype(np.int8)


def ber_report(tx_bits, rx_bits, *, max_delay: int = 512,
               min_compare_bits: int = 256,
               allow_inversion: bool = False) -> dict:
    """Return an auditable BER measurement with a bounded alignment search.

    The old implementation searched almost the whole capture and always tried
    an inverted stream.  Selecting the best of that many hypotheses can make a
    short/random capture look unrealistically good.  Alignment is now bounded;
    inversion is an explicit modulation ambiguity decision, never implicit.
    """
    import numpy as np
    tx = bits_from_probe(tx_bits)
    rx = bits_from_probe(rx_bits)
    if len(tx) == 0 or len(rx) == 0:
        return {"valid": False, "value": float("nan"), "delay_bits": 0,
                "error": "empty_probe", "compared_bits": 0,
                "bit_errors": 0, "inversion_applied": False}
    n = min(len(tx), len(rx))
    minimum = max(32, int(min_compare_bits))
    if n < minimum:
        return {"valid": False, "value": float("nan"), "delay_bits": 0,
                "error": "insufficient_bits", "compared_bits": n,
                "minimum_bits": minimum, "bit_errors": 0,
                "inversion_applied": False}
    search = min(max(0, int(max_delay)), n - minimum)
    best = None
    for delay in range(search + 1):
        for direction, a, b in (
            ("rx_lags_tx", tx[: n - delay], rx[delay:n]),
            ("tx_lags_rx", tx[delay:n], rx[: n - delay]),
        ):
            compared = min(len(a), len(b))
            if compared < minimum:
                continue
            left, right = a[:compared], b[:compared]
            candidates = [(False, right)]
            if allow_inversion:
                candidates.append((True, np.int8(1) - right))
            for inverted, candidate in candidates:
                errors = int(np.count_nonzero(left != candidate))
                item = (errors / compared, -compared, delay, direction,
                        inverted, errors, compared)
                if best is None or item < best:
                    best = item
    if best is None:
        return {"valid": False, "value": float("nan"), "delay_bits": 0,
                "error": "alignment_failed", "compared_bits": 0,
                "bit_errors": 0, "inversion_applied": False}
    value, _, delay, direction, inverted, errors, compared = best
    # Wilson one-sided 95% upper bound.  A measured BER of zero therefore
    # remains a finite-sample statement rather than an assertion of zero
    # population error rate.
    z = 1.6448536269514722
    proportion = errors / compared
    denominator = 1.0 + (z * z) / compared
    center = proportion + (z * z) / (2.0 * compared)
    radius = z * math.sqrt(
        (proportion * (1.0 - proportion) / compared)
        + (z * z) / (4.0 * compared * compared)
    )
    confidence_upper_bound = min(1.0, (center + radius) / denominator)
    return {
        "valid": True,
        "metric": "ber",
        "value": float(value),
        "bit_errors": errors,
        "compared_bits": compared,
        "delay_bits": delay,
        "alignment_direction": direction,
        "alignment_method": "bounded_delay_search",
        "max_delay_bits": search,
        "inversion_applied": inverted,
        "confidence_level": 0.95,
        "confidence_method": "wilson_one_sided",
        "confidence_upper_bound": confidence_upper_bound,
    }


def demod_bits(symbols, modulation: str = "bpsk"):
    """对符号点做硬判解调,返回比特序列(int8, 每符号 log2(M) 位)。

    仅用于仿真侧算 BER(与发送端已知比特对齐)。目前支持 bpsk/qpsk。
    """
    import numpy as np
    syms = np.asarray(symbols, dtype=np.complex64)
    mod = (modulation or "bpsk").lower().strip()
    if syms.size == 0:
        return np.array([], dtype=np.int8)
    if mod == "bpsk":
        return (syms.real >= 0).astype(np.int8)
    if mod == "qpsk":
        # 格雷映射的简化硬判:按象限取 2 bit
        b0 = (syms.real >= 0).astype(np.int8)
        b1 = (syms.imag >= 0).astype(np.int8)
        return np.column_stack([b0, b1]).ravel().astype(np.int8)
    # 未知调制:回落 BPSK 判实部
    return (syms.real >= 0).astype(np.int8)


# ---------------------------------------------------------------------------
# 核心:生成 + 执行
# ---------------------------------------------------------------------------
def generate_script(flow_graph, out_dir: str) -> str:
    """把 FlowGraph 生成为 no_gui python 脚本,返回脚本路径。

    要求 flow_graph 已 configure 为 no_gui、已 rewrite/validate。
    """
    from grc.core.generator import Generator

    generator = Generator(flow_graph, out_dir)
    generator.write()
    return generator.file_path


def _load_top_block_class(script_path: str):
    """Compile the current generated source without consulting ``.pyc``.

    A flowgraph can be regenerated at the same path, with the same byte size,
    within one filesystem timestamp tick.  Import loaders may then accept the
    previous bytecode cache and execute a stale graph.  Simulation is a
    verification boundary, so it must compile the bytes just generated.
    """
    module = types.ModuleType("_grc_sim_gen")
    module.__file__ = script_path
    with open(script_path, "rb") as handle:
        source = handle.read()
    exec(compile(source, script_path, "exec"), module.__dict__)
    for _name, obj in vars(module).items():
        if (isinstance(obj, type) and hasattr(obj, "start")
                and getattr(obj, "__module__", None) == module.__name__):
            return obj
    raise RuntimeError(f"脚本 {script_path} 中未找到 top_block 类")


def execute_script(script_path: str,
                   timeout: float = DEFAULT_TIMEOUT,
                   cwd: str = "") -> Tuple[bool, str]:
    """在本进程动态载入并执行 top_block,带墙钟超时兜底。

    File Sink 在 .grc 里使用 session 相对路径时，必须在输出目录下执行，
    否则样本会写到进程 cwd，读回就是空探针。
    """
    previous = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        try:
            top_cls = _load_top_block_class(script_path)
        except Exception as exc:  # noqa: BLE001
            return False, f"载入脚本失败: {exc}"

        tb = top_cls()
        err_holder: List[str] = []

        def _run():
            try:
                tb.start()
                tb.wait()
            except Exception as exc:  # noqa: BLE001
                err_holder.append(str(exc))

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            try:
                tb.stop()
            except Exception:  # noqa: BLE001
                pass
            th.join(5.0)
            if th.is_alive():
                return False, f"仿真超时(>{timeout}s)且无法停止"
        if err_holder:
            return False, "执行异常: " + "; ".join(err_holder)
        return True, ""
    finally:
        if cwd:
            try:
                os.chdir(previous)
            except OSError:
                pass


def read_probe(path: str, dtype: str = "complex64"):
    """读回 file_sink 落盘的数据。dtype: complex64/float32/int8/uint8。"""
    import numpy as np
    _MAP = {
        "complex64": np.complex64,
        "float32": np.float32,
        "int8": np.int8,
        "uint8": np.uint8,
        "byte": np.uint8,
    }
    np_dtype = _MAP.get(dtype, np.complex64)
    if not os.path.exists(path):
        return np.array([], dtype=np_dtype)
    return np.fromfile(path, dtype=np_dtype)


def run(flow_graph, platform, *,
        probes: Optional[Dict[str, Tuple[str, str]]] = None,
        out_dir: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        save_grc: bool = True) -> SimResult:
    """跑一次无头仿真并读回数据。

    Args:
        flow_graph: 已 configure(no_gui) + validate 的 FlowGraph。
            **约定**: 图里需已有 blocks_file_sink 把数据落盘,
            其 ``file`` 参数指向 out_dir 下的文件(见 probes)。
        platform: env.make_platform() 得到的平台(用于存 .grc)。
        probes: probe_id -> (文件路径, dtype)。跑完后按此读回数据。
            若为 None,则不读回(只验证能否跑通)。
        out_dir: 输出目录。None 则用 tempfile 新建。
        timeout: 墙钟超时秒数。
        save_grc: 是否顺带存一份 .grc(便于人工在 GRC 里打开检查)。

    Returns:
        SimResult。
    """
    result = SimResult()
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="grc_sim_")
    result.out_dir = out_dir
    os.makedirs(out_dir, exist_ok=True)

    # 1. 校验
    flow_graph.rewrite()
    flow_graph.validate()
    if not flow_graph.is_valid():
        msgs = []
        try:
            for m in flow_graph.get_error_messages():
                msgs.append(m.strip().splitlines()[-1].strip())
        except Exception:  # noqa: BLE001
            pass
        result.error = "流图无效: " + "; ".join(msgs) if msgs else "流图无效"
        result.summary = result.error
        return result

    # 2. 存 .grc(可选,便于人工检查)
    if save_grc:
        try:
            grc_path = os.path.join(
                out_dir, (flow_graph.get_option("id") or "flow_graph") + ".grc")
            platform.save_flow_graph(grc_path, flow_graph)
        except Exception as exc:  # noqa: BLE001
            logger.warning("存 .grc 失败(不影响仿真): %s", exc)

    # 3. 生成脚本
    try:
        result.script_path = generate_script(flow_graph, out_dir)
    except Exception as exc:  # noqa: BLE001
        result.error = f"生成脚本失败: {exc}"
        result.summary = result.error
        return result

    # 4. 执行
    ok, err = execute_script(result.script_path, timeout=timeout, cwd=out_dir)
    if not ok:
        result.error = err
        result.stderr = err
        result.summary = f"仿真执行失败: {err}"
        return result

    # 5. 读回数据
    if probes:
        import numpy as np
        for pid, (fpath, dtype) in probes.items():
            read_path = (
                fpath if os.path.isabs(fpath) else os.path.join(out_dir, fpath)
            )
            arr = read_probe(read_path, dtype)
            result.data[pid] = arr
            if arr.size == 0:
                logger.warning("probe %s 输出为空: %s", pid, read_path)

    result.ok = True
    result.summary = _build_summary(result)
    return result


def _build_summary(result: SimResult) -> str:
    """根据已读回的数据/指标生成一句话文本摘要,供 Agent 阅读。"""
    parts = ["仿真成功"]
    for pid, arr in result.data.items():
        n = getattr(arr, "size", 0)
        parts.append(f"{pid}:{n}样本")
    if result.metrics:
        for k, v in result.metrics.items():
            parts.append(f"{k}={v:.3g}")
    return ", ".join(parts)



