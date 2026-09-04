"""Render metric diagnoses using the selected reply style.

The same structured result is phrased for beginner, practitioner, or expert
users.  This module is an internal dependency of ``debug_by_metric``.

纯模板 + 规则,零依赖,离线可复现。有 LLM 时 agent 可在此基础上润色;
无 LLM 时这就是最终输出。

注:本模块是 tools 层内部的渲染依赖,不注册为可被 LLM 调度的 tool。
"""

from __future__ import annotations

from typing import Any, Dict


def _level(profile) -> str:
    if profile is None:
        return "practitioner"
    try:
        return profile.level
    except AttributeError:
        return "practitioner"


# ---------------------------------------------------------------------------
# debug_by_metric 的解说
# ---------------------------------------------------------------------------
def narrate_debug(diagnosis: Dict[str, Any], profile) -> str:
    lvl = _level(profile)
    verdict = diagnosis.get("verdict", "")
    suggestions = diagnosis.get("suggestions", [])
    metric = diagnosis.get("metric", "")
    value = diagnosis.get("value")

    val_str = f"{value:.2f}" if isinstance(value, (int, float)) else str(value)

    if lvl == "beginner":
        s = f"I checked the signal quality ({metric}={val_str}). {_plain(verdict)} "
        if suggestions:
            s += "My suggestion: " + suggestions[0]["say_beginner"]
        return s

    if lvl == "expert":
        s = f"{metric}={val_str} -> {verdict}."
        if suggestions:
            tips = "; ".join(x["knob"] + " " + x["dir"] for x in suggestions[:3])
            s += f" Suggestions: {tips}."
        return s

    # practitioner
    s = f"Current {metric}={val_str}; assessment: {verdict}."
    if suggestions:
        s += " Try: "
        s += "; ".join(f"{x['say_practitioner']}" for x in suggestions[:2])
    return s


def _plain(text: str) -> str:
    """把偏术语的短语做一次口语化(小白档)。"""
    repl = {
        "EVM 偏高": "EVM is high; the signal is distorted.",
        "EVM 正常": "EVM is within the expected range; the signal is clean.",
        "误码率": "bit error rate",
        "噪声": "noise",
        "星座": "constellation",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text
