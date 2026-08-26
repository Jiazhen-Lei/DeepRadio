"""debug_by_metric:以指标为线索定位问题并给出改参建议。

拿最近一次仿真的指标(EVM/BER/频谱峰)对照阈值判断链路健康度,
再结合当前配方的 knobs 给出"调哪个参数、往哪个方向"的可执行建议。
这是创新点 C"仿真在环闭环"的诊断环节:指标 -> 归因 -> 改参。

无 LLM 也能给确定性诊断;有 LLM 时把它当宏工具,让模型据此决定
下一步 set_param。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import registry
from .narrate import narrate_debug

# EVM(%) 判决阈值:< good 优秀,< usable 可用,否则偏高
_EVM_GOOD = 5.0
_EVM_USABLE = 15.0
_EVM_CLAIM_RE = re.compile(r"EVM\s*<\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _evm_claim_threshold(state) -> Optional[float]:
    """Smallest numeric EVM Claim / success condition, if any."""
    if state is None:
        return None
    thresholds: List[float] = []
    for claim in getattr(state, "claims", None) or []:
        match = _EVM_CLAIM_RE.search(str(getattr(claim, "statement", "") or ""))
        if match:
            thresholds.append(float(match.group(1)))
    spec = getattr(state, "spec", None)
    for condition in getattr(spec, "success_conditions", None) or []:
        match = _EVM_CLAIM_RE.search(str(condition or ""))
        if match:
            thresholds.append(float(match.group(1)))
    return min(thresholds) if thresholds else None


def debug_by_metric(ctx, profile=None, metric: str = "evm",
                    probe_id: str = "", modulation: str = "bpsk",
                    sps: int = 4) -> Dict[str, Any]:
    """读指标 -> 判断 -> 给改参建议。

    要求 ``ctx.last_sim`` 已有成功仿真(通常先跑 design_link 或 run_simulation)。
    """
    metric = (metric or "evm").lower().strip()
    if metric in ("spectrum", "spectrum_peak", "psd"):
        metric = "spectrum"

    if metric == "evm":
        m = registry.call("read_metric",
                          {"kind": "evm", "probe_id": probe_id,
                           "modulation": modulation, "sps": sps}, ctx)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "读 EVM 失败")}
        value = m["value"]
        state = None
        try:
            state = ctx.extra.get("state")
        except AttributeError:
            state = None
        verdict, suggestions, meets_claim = _diagnose_evm(
            value, claim_threshold=_evm_claim_threshold(state)
        )
        diag = {
            "ok": True, "metric": "EVM", "value": value, "unit": "%",
            "verdict": verdict, "suggestions": suggestions,
            "meets_claim": meets_claim,
        }
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


def _diagnose_evm(
    evm: float, *, claim_threshold: Optional[float] = None
) -> tuple:
    """按 EVM 值给判断 + 分档改参建议。

    建议项结构统一,便于 narrate 分档渲染:
        {knob, dir, say_novice, say_student}
    """
    meets_claim = (
        claim_threshold is not None and evm < float(claim_threshold)
    )
    if meets_claim:
        verdict = (
            f"EVM 已达标（Claim 要求 < {claim_threshold:g}%，当前 {evm:.2f}%）。"
            "主因是设计噪声（AWGN），不是故障。"
        )
        sugg: List[dict] = [{
            "knob": "chan.noise_voltage", "dir": "↑(可选压测)",
            "say_novice": "已经达标了，不用改。如果想看它变差，可以把杂音调大试试。",
            "say_student": "可选：增大 chan.noise_voltage 做鲁棒性压测。",
        }]
        return verdict, sugg, True
    if evm < _EVM_GOOD:
        verdict = "EVM 正常,链路质量优秀。"
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↑(压测)",
            "say_novice": "信号很干净;如果想看它变差,可以把杂音调大试试。",
            "say_student": "可增大 chan.noise_voltage 做鲁棒性压测。",
        }]
    elif evm < _EVM_USABLE:
        verdict = (
            "EVM 处于设计噪声下的正常范围，主因是 AWGN 设计点，不是故障。"
        )
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↓(可选)",
            "say_novice": "这不是故障。如果想更干净，可以把杂音再调小一点。",
            "say_student": "可选：减小 chan.noise_voltage 进一步压低 EVM。",
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
    return verdict, sugg, False


def _profile_of(ctx):
    try:
        return ctx.extra.get("profile")
    except AttributeError:
        return None


@registry.tool(
    name="debug_by_metric",
    description=(
        "宏工具:以指标为线索定位问题并给改参建议(读 EVM/频谱峰→判决→分档改参)。"
        "适合 TUNE 阶段;需先有一次成功仿真(先调 design_link 或 run_simulation)。"),
    parameters={
        "type": "object",
        "properties": {
            "metric": {"type": "string",
                       "description": "指标:evm 或 spectrum,默认 evm"},
            "probe_id": {"type": "string",
                         "description": "探针块 id(仿真落盘用的 sink id)"},
            "modulation": {"type": "string",
                           "description": "调制方式(算 EVM 用),默认 bpsk"},
            "sps": {"type": "integer",
                    "description": "每符号采样数,默认 4"},
        },
    },
    group="macro",
    origin="deepradio_macro",
    runtime="deepradio",
)
def debug_by_metric_tool(ctx, metric: str = "evm", probe_id: str = "",
                         modulation: str = "bpsk", sps: int = 4) -> Dict[str, Any]:
    return debug_by_metric(ctx, _profile_of(ctx), metric=metric,
                          probe_id=probe_id, modulation=modulation, sps=sps)
