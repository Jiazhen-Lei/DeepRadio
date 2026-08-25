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

import importlib.util
import logging
import math
import os
import tempfile
import threading
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

    # -- 可视化(惰性导入 matplotlib,无显示后端) ---------------------------
    def plot_constellation(self, path: str, probe_id: Optional[str] = None,
                           sps: int = 1) -> Optional[str]:
        """把某个 IQ probe 画成星座散点图,存到 path。返回图路径。"""
        arr = self._pick_complex(probe_id)
        if arr is None:
            return None
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        syms = arr[sps * 4::sps] if sps > 1 else arr
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.scatter(syms.real, syms.imag, s=6, alpha=0.4)
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
        """把某个 IQ probe 画成频谱(dB),存到 path。返回图路径。"""
        arr = self._pick_complex(probe_id)
        if arr is None:
            return None
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        n = min(len(arr), 4096)
        spec = np.fft.fftshift(np.abs(np.fft.fft(arr[:n])))
        spec_db = 20 * np.log10(spec + 1e-12)
        freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / samp_rate))
        fig, ax = plt.subplots(figsize=(5, 3))
        ax.plot(freqs, spec_db, lw=0.8)
        ax.set_title("Spectrum")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Magnitude (dB)")
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


def ber_from_bits(tx_bits, rx_bits) -> Tuple[float, int]:
    """比对收发比特算 BER,自动搜索最佳对齐时延。

    Returns:
        (ber, best_delay)。若无法对齐返回 (nan, 0)。
    """
    import numpy as np
    tx = np.asarray(tx_bits, dtype=np.int8).ravel()
    rx = np.asarray(rx_bits, dtype=np.int8).ravel()
    if len(tx) == 0 or len(rx) == 0:
        return float("nan"), 0
    n = min(len(tx), len(rx))
    # 在小范围内搜索最佳循环时延(成形滤波/同步会引入固定偏移)
    best_ber, best_delay = 1.0, 0
    search = min(64, n // 2)
    for d in range(search):
        a = tx[:n - d]
        b = rx[d:n]
        m = min(len(a), len(b))
        if m == 0:
            continue
        ber = float(np.mean(a[:m] != b[:m]))
        if ber < best_ber:
            best_ber, best_delay = ber, d
    return best_ber, best_delay


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
    """动态载入生成的脚本,取出 top_block 类(有 start 方法者)。"""
    spec = importlib.util.spec_from_file_location("_grc_sim_gen", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _name, obj in vars(module).items():
        if (isinstance(obj, type) and hasattr(obj, "start")
                and getattr(obj, "__module__", None) == module.__name__):
            return obj
    raise RuntimeError(f"脚本 {script_path} 中未找到 top_block 类")


def execute_script(script_path: str,
                   timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """在本进程动态载入并执行 top_block,带墙钟超时兜底。

    用线程跑 start()+wait(),超时则尝试 stop()+wait()。
    返回 (是否正常结束, 错误信息)。
    """
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
        # 超时:请求停止(head 通常已让它自停,这里是兜底)
        try:
            tb.stop()
            tb.wait()
        except Exception:  # noqa: BLE001
            pass
        th.join(5.0)
        if th.is_alive():
            return False, f"仿真超时(>{timeout}s)且无法停止"
    if err_holder:
        return False, "执行异常: " + "; ".join(err_holder)
    return True, ""


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
    ok, err = execute_script(result.script_path, timeout=timeout)
    if not ok:
        result.error = err
        result.stderr = err
        result.summary = f"仿真执行失败: {err}"
        return result

    # 5. 读回数据
    if probes:
        import numpy as np
        for pid, (fpath, dtype) in probes.items():
            arr = read_probe(fpath, dtype)
            result.data[pid] = arr
            if arr.size == 0:
                logger.warning("probe %s 输出为空: %s", pid, fpath)

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


# ---------------------------------------------------------------------------
# 自检: T2 BPSK + AWGN 端到端
# ---------------------------------------------------------------------------
def _selftest_bpsk_awgn() -> int:
    """构建 BPSK+AWGN 链路,跑无头仿真,算 EVM,验证闭环。

    运行::

        PYTHONPATH=$PWD python -m grc.agent.runtime.simulate
    """
    import numpy as np
    from grc.agent import env

    logging.basicConfig(level=logging.WARNING)
    out_dir = tempfile.mkdtemp(prefix="bpsk_awgn_")
    iq_file = os.path.join(out_dir, "rx_iq.bin")

    samp_rate = 1_000_000
    sps = 4
    n_samples = 8192

    platform = env.make_platform()
    print(f"[1] 块库: {len(platform.blocks)} 块 / "
          f"{len(platform.workflow_manager.workflows)} workflow")

    fg = platform.make_flow_graph()
    env.configure_options(fg, "python", "no_gui", flowgraph_id="bpsk_awgn_sim")

    def nb(key, bid, **kw):
        block = fg.new_block(key)
        if block is None:
            raise AssertionError(f"块不存在: {key}")
        block.params["id"].set_value(bid)
        for name, value in kw.items():
            if name not in block.params:
                raise AssertionError(
                    f"{key} 无参数 {name!r}; 实际: {list(block.params)}")
            block.params[name].set_value(value)
        return block

    nb("variable", "samp_rate", value=str(samp_rate))
    nb("variable", "sps", value=str(sps))
    nb("variable_constellation", "bpsk_const", type="bpsk")

    src = nb("analog_random_source_x", "src", type="byte",
             min="0", max="2", num_samps="1000", repeat="True")
    mod = nb("digital_constellation_modulator", "mod",
             constellation="bpsk_const", differential="False",
             samples_per_symbol="sps", excess_bw="0.35")
    # AWGN 信道: 加噪声,制造非理想星座
    chan = nb("channels_channel_model", "chan",
              noise_voltage="0.05", freq_offset="0.0",
              epsilon="1.0", taps="1.0", seed="0")
    head = nb("blocks_head", "head", type="complex", num_items=str(n_samples))
    sink = nb("blocks_file_sink", "sink", type="complex", file=repr(iq_file))

    for i, block in enumerate(fg.blocks):
        block.states["coordinate"] = (120 + (i % 4) * 230, 140 + (i // 4) * 170)

    fg.rewrite()
    fg.connect(src.sources[0], mod.sinks[0])
    fg.connect(mod.sources[0], chan.sinks[0])
    fg.connect(chan.sources[0], head.sinks[0])
    fg.connect(head.sources[0], sink.sinks[0])
    fg.rewrite()
    fg.validate()
    print(f"[2] is_valid(): {fg.is_valid()}")
    if not fg.is_valid():
        for msg in fg.get_error_messages():
            print("    !", msg.strip().splitlines()[-1].strip())
        return 1

    result = run(fg, platform,
                 probes={"rx": (iq_file, "complex64")},
                 out_dir=out_dir)
    print(f"[3] 脚本: {result.script_path}")
    print(f"[4] {result.summary}")
    if not result.ok:
        print("    ! 错误:", result.error)
        return 1

    # 算 EVM
    iq = result.data["rx"]
    if iq.size == 0:
        print("    ! 无数据")
        return 1
    syms = extract_symbols(iq, sps=sps, skip_symbols=4)
    evm = evm_from_symbols(syms, ideal_points=ideal_points_for("bpsk"))
    result.metrics["evm_pct"] = evm
    print(f"[5] EVM = {evm:.2f}%  (样本 {iq.size}, 抽取符号 {syms.size})")

    # 硬判解调 -> 演示 BER 通路(自洽:解调结果与自身比对为 0,验证通路)
    rx_bits = demod_bits(syms, "bpsk")
    ber, delay = ber_from_bits(rx_bits, rx_bits)
    result.metrics["ber_selfcheck"] = ber
    print(f"[6] BER 通路自检 = {ber:.3g} (delay={delay}), 解调 {rx_bits.size} bit")

    # 画图: 星座 / 频谱 / 眼图
    const_png = os.path.join(out_dir, "constellation.png")
    result.plot_constellation(const_png, probe_id="rx", sps=sps)
    spec_png = os.path.join(out_dir, "spectrum.png")
    result.plot_spectrum(spec_png, probe_id="rx", samp_rate=samp_rate)
    eye_png = os.path.join(out_dir, "eye.png")
    result.plot_eye(eye_png, probe_id="rx", sps=sps)
    print(f"[7] 星座图: {const_png}")
    print(f"    频谱图: {spec_png}")
    print(f"    眼  图: {eye_png}")

    # AWGN(噪声 0.05)下 EVM 应在合理范围(几个百分点),且非 0(有噪声)
    ok = 0.1 < evm < 40.0
    print("\n自检结果:", "PASS" if ok else "FAIL",
          f"(EVM {evm:.2f}% 应落在 0.1~40)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest_bpsk_awgn())
