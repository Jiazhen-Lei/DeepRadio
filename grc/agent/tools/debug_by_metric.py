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

    要求 ``ctx.last_sim`` 已有成功仿真(通常先跑 run_simulation)。
    """
    metric = (metric or "evm").lower().strip()
    if metric in ("spectrum", "spectrum_peak", "psd"):
        metric = "spectrum"

    if metric == "evm":
        m = registry.call("read_metric",
                          {"kind": "evm", "probe_id": probe_id,
                           "modulation": modulation, "sps": sps}, ctx)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "Failed to read EVM")}
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
            return {"ok": False, "error": m.get("error", "Failed to read the spectrum")}
        diag = {"ok": True, "metric": "spectrum_peak",
                "value": m.get("peak"), "peak_bin": m.get("peak_bin"),
                "verdict": "The spectrum peak was located and can be used to verify the expected carrier or tone frequency.",
                "suggestions": []}
        diag["narrative"] = narrate_debug(diag, profile)
        return diag

    return {"ok": False, "error": f"Unsupported metric: {metric}"}


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
            f"EVM meets the claim (< {claim_threshold:g}%; current value: {evm:.2f}%). "
            "The primary contributor is the designed AWGN, not a fault."
        )
        sugg: List[dict] = [{
            "knob": "chan.noise_voltage", "dir": "↑ (optional stress test)",
            "say_novice": "The target is already met. No change is needed; increase the noise only if you want to observe degradation.",
            "say_student": "Optional: increase chan.noise_voltage for a robustness stress test.",
        }]
        return verdict, sugg, True
    if evm < _EVM_GOOD:
        verdict = "EVM is normal and link quality is excellent."
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↑ (stress test)",
            "say_novice": "The signal is clean; increase the noise only if you want to observe degradation.",
            "say_student": "Increase chan.noise_voltage for a robustness stress test.",
        }]
    elif evm < _EVM_USABLE:
        verdict = (
            "EVM is within the normal range for the designed noise level. The primary contributor is the AWGN design point, not a fault."
        )
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↓ (optional)",
            "say_novice": "This is not a fault. Reduce the noise if you want an even cleaner signal.",
            "say_student": "Optional: reduce chan.noise_voltage to lower EVM further.",
        }]
    else:
        verdict = "EVM is high; symbol decisions may fail. Inspect noise, frequency offset, and synchronization."
        sugg = [{
            "knob": "chan.noise_voltage", "dir": "↓",
            "say_novice": "The signal is distorted. Reduce the noise substantially and check again.",
            "say_student": "Reduce chan.noise_voltage substantially to determine whether noise is dominant.",
        }, {
            "knob": "chan.freq_offset", "dir": "→0",
            "say_novice": "If the signal points rotate, set the frequency offset back to 0.",
            "say_student": "A rotating constellation indicates frequency offset; set freq_offset=0 or add carrier recovery.",
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
        "适合诊断阶段;需先有一次成功仿真(先调 run_simulation)。"),
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
