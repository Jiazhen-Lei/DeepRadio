"""仿真类工具:run_simulation / read_metric / plot_constellation 等。

把 :mod:`grc.agent.runtime.simulate` 的能力封成可被 LLM 调度的工具,
是创新点 C(仿真在环)的调度入口。仿真结果 SimResult 存到
``ctx.last_sim``,后续 read_metric / plot_* 复用它,避免重复跑。
"""

from __future__ import annotations

import os

from ..runtime import simulate
from .registry import ToolContext, tool

#: file_sink 的 ``type`` 参数 -> read_probe 支持的 numpy dtype 名。
_DTYPE_BY_SINK_TYPE = {
    "complex": "complex64",
    "float": "float32",
    "byte": "uint8",
    "char": "int8",
}

#: 无样本时给模型的可执行提示,避免它去猜产物路径或反复重跑。
_NO_SAMPLE_HINT = (
    "probe 读到 0 个样本。probe 文件路径必须来自流图里 file sink 的 file 参数,"
    "不要按 probe_id 猜文件名;推荐直接调用 design_flowgraph(simulate=True) 走"
    "完整链路。"
)


def _strip_quotes(value) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def derive_probes(ctx: ToolContext) -> dict:
    """从当前流图的 ``blocks_file_sink`` 推导 probe_id -> (路径, dtype)。

    真实落盘路径只写在 file sink 的 ``file`` 参数里。调用方若按 probe_id
    拼文件名(如 ``<probe_id>_rx.bin``),流图仍写自己的路径,读回就是 0 样本。
    """
    probes = {}
    for block_id, block in (getattr(ctx, "blocks", None) or {}).items():
        if getattr(block, "key", "") != "blocks_file_sink":
            continue
        params = getattr(block, "params", None) or {}
        if "file" not in params:
            continue
        path = _strip_quotes(params["file"].get_value())
        if not path:
            continue
        if not os.path.isabs(path):
            path = os.path.join(ctx.out_dir or os.getcwd(), path)
        sink_type = ""
        if "type" in params:
            sink_type = str(params["type"].get_value()).strip()
        probes[block_id] = (path, _DTYPE_BY_SINK_TYPE.get(sink_type,
                                                          "complex64"))
    return probes


def _require_samples(ctx: ToolContext, probe_id: str = ""):
    """取出可用的复数样本;不可用时返回 (None, 错误 dict)。"""
    res = ctx.last_sim
    if res is None or not res.ok:
        return None, {"ok": False, "error": "尚无成功的仿真结果,请先 run_simulation"}
    arr = res._pick_complex(probe_id or None)
    if arr is None:
        probe = res.data.get(probe_id) if probe_id else None
        if probe is None and res.data:
            probe = next(iter(res.data.values()))
        dtype = str(getattr(probe, "dtype", "") or "")
        if probe is not None and (
            "int" in dtype or dtype.startswith("uint")
        ):
            return None, {
                "ok": False,
                "error": (
                    f"probe '{probe_id or 'sink'}' 是 {dtype or '整数'} 比特/字节,"
                    "不能按 IQ 计算 EVM 或频谱。请改用 kind=ber,或换复数 IQ sink。"
                ),
            }
        return None, {"ok": False, "error": "找不到复数 probe 数据",
                      "hint": _NO_SAMPLE_HINT}
    if int(getattr(arr, "size", 0)) == 0:
        return None, {"ok": False, "error": "probe 无样本(0 采样)",
                      "hint": _NO_SAMPLE_HINT}
    return arr, None


def _pick_integer_bits(res, probe_id: str = ""):
    import numpy as np
    if probe_id:
        arr = res.data.get(probe_id)
        if arr is not None and np.issubdtype(getattr(arr, "dtype", np.float32), np.integer):
            return arr
        return None
    for arr in res.data.values():
        if arr is not None and np.issubdtype(getattr(arr, "dtype", np.float32), np.integer):
            return arr
    return None


@tool(
    name="run_simulation",
    description="对当前流图跑一次无头仿真:生成脚本->执行->读回 probe 数据。probes 省略时自动从流图的 blocks_file_sink 推导。",
    parameters={
        "type": "object",
        "properties": {
            "probes": {
                "type": "object",
                "description": "可选。probe_id -> [文件路径, dtype];省略时按流图里的 file sink 自动推导。",
            },
            "timeout": {"type": "number", "description": "墙钟超时秒数,默认 30"},
        },
    },
    group="sim",
)
def run_simulation(ctx: ToolContext, probes: dict = None, timeout: float = 30.0):
    fg = ctx.flow_graph
    if fg is None:
        return {"ok": False, "error": "流图尚未创建"}
    # 把 [path, dtype] 规整成 (path, dtype) 元组
    norm = None
    if probes:
        norm = {}
        for pid, spec in probes.items():
            if isinstance(spec, (list, tuple)) and len(spec) >= 2:
                norm[pid] = (str(spec[0]), str(spec[1]))
            elif isinstance(spec, dict):
                norm[pid] = (str(spec.get("path")), str(spec.get("dtype", "complex64")))
    if not norm:
        norm = derive_probes(ctx) or None
    result = simulate.run(fg, ctx.platform, probes=norm,
                          out_dir=ctx.out_dir, timeout=timeout)
    ctx.last_sim = result
    if not result.ok:
        return {"ok": False, "error": result.error or "仿真失败",
                "stderr": result.stderr, "summary": result.summary}
    sizes = {pid: int(getattr(arr, "size", 0))
             for pid, arr in result.data.items()}
    payload = {"ok": True, "summary": result.summary,
               "script_path": result.script_path,
               "out_dir": result.out_dir, "probe_sizes": sizes,
               "probes": {pid: path for pid, (path, _) in (norm or {}).items()}}
    if sizes and not any(sizes.values()):
        payload["warning"] = _NO_SAMPLE_HINT
    return payload


@tool(
    name="read_metric",
    description="从最近一次仿真的数据里计算指标:evm / ber / spectrum_peak。",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "'evm' / 'ber' / 'spectrum_peak'(也接受 spectrum)"},
            "probe_id": {"type": "string", "description": "用哪个 probe 的数据;默认取第一个复数 probe"},
            "modulation": {"type": "string", "description": "调制方式(算 evm/ber 用),如 'bpsk'/'qpsk'"},
            "sps": {"type": "integer", "description": "每符号样本数,默认 4"},
            "tx_bits_probe": {"type": "string", "description": "算 ber 时,已知发送比特所在的 probe_id"},
        },
        "required": ["kind"],
    },
    group="sim",
)
def read_metric(ctx: ToolContext, kind: str, probe_id: str = "",
                modulation: str = "bpsk", sps: int = 4,
                tx_bits_probe: str = ""):
    import numpy as np
    res = ctx.last_sim
    if res is None or not res.ok:
        return {"ok": False, "error": "尚无成功的仿真结果,请先 run_simulation"}
    kind = (kind or "").lower().strip()
    if kind in ("spectrum", "psd"):
        kind = "spectrum_peak"

    if kind in ("evm", "spectrum_peak"):
        iq, err = _require_samples(ctx, probe_id)
        if err is not None:
            return err
    else:
        iq = res._pick_complex(probe_id or None)

    if kind == "evm":
        ideal = simulate.ideal_points_for(modulation)
        filtered = simulate.matched_filter_rrc(iq, sps=sps)
        syms, phase = simulate.extract_symbols_best_phase(
            filtered,
            sps=sps,
            ideal_points=ideal,
            skip_symbols=15 if sps > 1 else 4,
        )
        evm = simulate.evm_from_symbols(syms, ideal)
        res.metrics["evm_pct"] = evm
        return {"ok": True, "kind": "evm", "value": evm,
                "unit": "%", "n_symbols": int(syms.size),
                "sample_phase": phase}

    if kind == "ber":
        bits = _pick_integer_bits(res, probe_id)
        if bits is not None:
            tx = res.data.get(tx_bits_probe) if tx_bits_probe else None
            if tx is None:
                return {
                    "ok": False,
                    "kind": "ber",
                    "error": (
                        "probe 是比特/字节,算 BER 还需要 tx_bits_probe "
                        "(已知发送比特)。不要对 byte sink 计算 EVM。"
                    ),
                    "n_bits": int(getattr(bits, "size", 0)),
                }
            ber, delay = simulate.ber_from_bits(np.asarray(tx), np.asarray(bits))
            res.metrics["ber"] = ber
            return {"ok": True, "kind": "ber", "value": ber, "delay": delay}
        if iq is None:
            iq, err = _require_samples(ctx, probe_id)
            if err is not None:
                return err
        syms = simulate.extract_symbols(iq, sps=sps, skip_symbols=4)
        rx_bits = simulate.demod_bits(syms, modulation)
        tx = res.data.get(tx_bits_probe)
        if tx is None:
            return {"ok": False,
                    "error": "算 ber 需要已知发送比特(tx_bits_probe)"}
        ber, delay = simulate.ber_from_bits(np.asarray(tx), rx_bits)
        res.metrics["ber"] = ber
        return {"ok": True, "kind": "ber", "value": ber, "delay": delay}

    if kind == "spectrum_peak":
        n = min(len(iq), 4096)
        spec = np.abs(np.fft.fft(iq[:n]))
        peak = float(spec.max())
        res.metrics["spectrum_peak"] = peak
        return {"ok": True, "kind": "spectrum_peak", "value": peak,
                "peak_bin": int(np.argmax(spec)), "peak": peak}

    return {"ok": False, "error": f"未知指标: {kind}"}


@tool(
    name="plot_constellation",
    description="把最近仿真的 IQ 数据画成星座图,返回图片路径。",
    parameters={
        "type": "object",
        "properties": {
            "probe_id": {"type": "string", "description": "用哪个 probe;默认第一个复数 probe"},
            "sps": {"type": "integer", "description": "每符号样本数,默认 1"},
            "path": {"type": "string", "description": "可选,输出图片路径"},
        },
    },
    group="sim",
)
def plot_constellation(ctx: ToolContext, probe_id: str = "",
                       sps: int = 1, path: str = ""):
    _, err = _require_samples(ctx, probe_id)
    if err is not None:
        return err
    res = ctx.last_sim
    out = path or os.path.join(res.out_dir or ".", "constellation.png")
    p = res.plot_constellation(out, probe_id=probe_id or None, sps=sps)
    if p is None:
        return {"ok": False, "error": "无可用复数数据"}
    return {"ok": True, "path": p}


@tool(
    name="plot_spectrum",
    description="把最近仿真的 IQ 数据画成频谱图,返回图片路径。",
    parameters={
        "type": "object",
        "properties": {
            "probe_id": {"type": "string"},
            "samp_rate": {"type": "number", "description": "采样率(Hz),用于频率轴"},
            "path": {"type": "string"},
        },
    },
    group="sim",
)
def plot_spectrum(ctx: ToolContext, probe_id: str = "",
                  samp_rate: float = 1.0, path: str = ""):
    _, err = _require_samples(ctx, probe_id)
    if err is not None:
        return err
    res = ctx.last_sim
    out = path or os.path.join(res.out_dir or ".", "spectrum.png")
    p = res.plot_spectrum(out, probe_id=probe_id or None, samp_rate=samp_rate)
    if p is None:
        return {"ok": False, "error": "无可用复数数据"}
    return {"ok": True, "path": p}


@tool(
    name="plot_eye",
    description="把最近仿真的 IQ 数据画成眼图,返回图片路径。",
    parameters={
        "type": "object",
        "properties": {
            "probe_id": {"type": "string"},
            "sps": {"type": "integer", "description": "每符号样本数,默认 4"},
            "path": {"type": "string"},
        },
    },
    group="sim",
)
def plot_eye(ctx: ToolContext, probe_id: str = "", sps: int = 4, path: str = ""):
    _, err = _require_samples(ctx, probe_id)
    if err is not None:
        return err
    res = ctx.last_sim
    out = path or os.path.join(res.out_dir or ".", "eye.png")
    p = res.plot_eye(out, probe_id=probe_id or None, sps=sps)
    if p is None:
        return {"ok": False, "error": "数据不足以画眼图"}
    return {"ok": True, "path": p}
