"""debug_by_metric:以指标为线索定位问题并给出改参建议。

拿最近一次仿真的指标(EVM/BER/频谱峰)对照阈值判断链路健康度,
再结合当前配方的 knobs 给出\"调哪个参数、往哪个方向\"的可执行建议。
这是创新点 C\"仿真在环闭环\"的诊断环节:指标 -> 归因 -> 改参。

无 LLM 也能给确定性诊断;有 LLM 时把它当宏工具,让模型据此决定
下一步 set_param。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..tools import registry
from .narrate import narrate_debug

# EVM(%) 判决阈值:< good 优秀,< usable 可用,否则偏高
_EVM_GOOD = 5.0
_EVM_USABLE = 15.0


def debug_by_metric(ctx, profile=None, metric: str = "evm",
                    probe_id: str = "", modulation: str = "bpsk",
                    sps: int = 4) -> Dict[str, Any]:
    """读指标 -> 判断 -> 给改参建议。

    要求 ``ctx.last_sim`` 已有成功仿真(通常先跑 design_link 或 run_simulation)。
    """
    metric = (metric or "evm").lower().strip()

    if metric == "evm":
        m = registry.call("read_metric",
                          {"kind": "evm", "probe_id": probe_id,
                           "modulation": modulation, "sps": sps}, ctx)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "读 EVM 失败")}
        value = m["value"]
        verdict, suggestions = _diagnose_evm(value)
        diag = {"ok": True, "metric": "EVM", "value": value, "unit": "%",
                "verdict": verdict, "suggestions": suggestions}
        diag["narrative"] = narrate_debug(diag, profile)
        return diag

    if metric == "spectrum":
        m = registry.call("read_metric",
                          {"kind": "spectrum_peak", "probe_id": probe_id},
                          ctx)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "读频谱失败")}
        diag = {"ok": True, "metric": "spectrum_peak",
                "value": m.get("peak"), "peak_bin": m.get("peak_bin"),
                "verdict": "已定位频谱主峰,可据此核对载波/单音频率是否符合预期。",
                "suggestions": []}
        diag["narrative"] = narrate_debug(diag, profile)
        return diag

    return {"ok": False, "error": f"暂不支持的指标: {metric}"}


def _diagnose_evm(evm: float) -> tuple:
    """按 EVM 值给判断 + 分档改参建议。

    建议项结构统一,便于 narrate 分档渲染:
        {knob, dir, say_novice, say_student}
    """
    if evm < _EVM_GOOD:
        verdict = "EVM 正常,链路质量优秀。"
        sugg: List[dict] = [{
            "knob": "chan.noise_voltage", "dir": "↑(压测)",
            "say_novice": "信号很干净;如果想看它变差,可以把杂音调大试试。",
            "say_student": "可增大 chan.noise_voltage 做鲁棒性压测。",
        }]
    elif evm < _EVM_USABLE:
        verdict = "EVM 可用但有余量,存在一定噪声/失真。"
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↓",
            "say_novice": "信号还行,想更干净就把杂音(噪声)调小一点。",
            "say_student": "减小 chan.noise_voltage 可直接降低 EVM。",
        }, {
            "knob": "mod.excess_bw", "dir": "调整",
            "say_novice": "也可以微调成形参数让波形更规整。",
            "say_student": "适当调 RRC 滚降 excess_bw 可改善码间串扰。",
        }]
    else:
        verdict = "EVM 偏高,判决容易出错,需排查噪声/频偏/同步。"
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↓",
            "say_novice": "信号有点乱,先把杂音调小很多再看。",
            "say_student": "先大幅降低 chan.noise_voltage 确认是否为噪声主导。",
        }, {
            "knob": "chan.freq_offset", "dir": "→0",
            "say_novice": "如果信号点在转圈,把\"频率偏移\"设回 0。",
            "say_student": "若星座旋转,说明有频偏,应置 freq_offset=0 或加载波恢复。",
        }]
    return verdict, sugg


def apply_suggestion(ctx, block_param: str, value: str) -> Dict[str, Any]:
    """便捷:把 'chan.noise_voltage' 这类建议落成一次 set_param。"""
    if "." not in block_param:
        return {"ok": False, "error": "block_param 应形如 'chan.noise_voltage'"}
    bid, pname = block_param.split(".", 1)
    return registry.call("set_param",
                        {"id": bid, "name": pname, "value": str(value)}, ctx)
