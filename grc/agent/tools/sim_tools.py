"""仿真类工具:run_simulation / read_metric / plot_constellation 等。

把 :mod:`grc.agent.runtime.simulate` 的能力封成可被 LLM 调度的工具,
是创新点 C(仿真在环)的调度入口。仿真结果 SimResult 存到
``ctx.last_sim``,后续 read_metric / plot_* 复用它,避免重复跑。
"""

from __future__ import annotations

import os

from ..runtime import simulate
from .registry import ToolContext, tool


@tool(
    name="run_simulation",
    description="对当前流图跑一次无头仿真:生成脚本->执行->读回指定 probe 的数据。要求流图里已有 blocks_file_sink 落盘。",
    parameters={
        "type": "object",
        "properties": {
            "probes": {
                "type": "object",
                "description": "probe_id -> [文件路径, dtype]。dtype: complex64/float32/int8/uint8。",
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
    result = simulate.run(fg, ctx.platform, probes=norm,
                          out_dir=ctx.out_dir, timeout=timeout)
    ctx.last_sim = result
    if not result.ok:
        return {"ok": False, "error": result.error or "仿真失败",
                "stderr": result.stderr, "summary": result.summary}
    sizes = {pid: int(getattr(arr, "size", 0))
             for pid, arr in result.data.items()}
    return {"ok": True, "summary": result.summary,
            "script_path": result.script_path,
            "out_dir": result.out_dir, "probe_sizes": sizes}


@tool(
    name="read_metric",
    description="从最近一次仿真的数据里计算指标:evm / ber / spectrum_peak。",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "'evm' / 'ber' / 'spectrum_peak'"},
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

    iq = res._pick_complex(probe_id or None)
    if kind in ("evm", "ber") and iq is None:
        return {"ok": False, "error": "找不到复数 probe 数据"}

    if kind == "evm":
        syms = simulate.extract_symbols(iq, sps=sps, skip_symbols=4)
        evm = simulate.evm_from_symbols(
            syms, simulate.ideal_points_for(modulation))
        res.metrics["evm_pct"] = evm
        return {"ok": True, "kind": "evm", "value": evm,
                "unit": "%", "n_symbols": int(syms.size)}

    if kind == "ber":
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
        if iq is None or iq.size == 0:
            return {"ok": False, "error": "无数据"}
        n = min(len(iq), 4096)
        spec = np.abs(np.fft.fft(iq[:n]))
        return {"ok": True, "kind": "spectrum_peak",
                "peak_bin": int(np.argmax(spec)), "peak": float(spec.max())}

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
    res = ctx.last_sim
    if res is None or not res.ok:
        return {"ok": False, "error": "尚无成功的仿真结果"}
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
    res = ctx.last_sim
    if res is None or not res.ok:
        return {"ok": False, "error": "尚无成功的仿真结果"}
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
    res = ctx.last_sim
    if res is None or not res.ok:
        return {"ok": False, "error": "尚无成功的仿真结果"}
    out = path or os.path.join(res.out_dir or ".", "eye.png")
    p = res.plot_eye(out, probe_id=probe_id or None, sps=sps)
    if p is None:
        return {"ok": False, "error": "数据不足以画眼图"}
    return {"ok": True, "path": p}
