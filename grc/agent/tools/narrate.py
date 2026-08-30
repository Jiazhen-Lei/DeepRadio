"""narrate.py:按用户画像档位渲染自然语言解说(创新 B 的表达执行体)。

同一份结构化结果(建图/指标/诊断),对 novice/student/expert 渲染出
繁简、术语密度、是否给公式都不同的文本。这是"同一后端、分档表达"
的落地点,被 design_link / debug_by_metric 复用。

纯模板 + 规则,零依赖,离线可复现。有 LLM 时 agent 可在此基础上润色;
无 LLM 时这就是最终输出。

注:本模块是 tools 层内部的渲染依赖,不注册为可被 LLM 调度的 tool。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _level(profile) -> str:
    if profile is None:
        return "student"
    try:
        return profile.level
    except AttributeError:
        return "student"


# ---------------------------------------------------------------------------
# design_link 的解说
# ---------------------------------------------------------------------------
_OFFLINE_NOTE = "No hardware was accessed and no RF transmission was started."


def narrate_design(recipe, result: Dict[str, Any], profile) -> str:
    lvl = _level(profile)
    ok = result.get("valid")
    evm = result.get("metrics", {}).get("evm_pct")
    nb = result.get("num_blocks")
    title = recipe.title

    if not ok:
        errs = result.get("errors", [])
        head = f"⚠️ The '{title}' chain was created, but validation found an issue. "
        if lvl == "novice":
            return head + "Let's inspect the connections one step at a time."
        return head + f"Key errors: {_fmt_errors(errs)}"

    if lvl == "novice":
        s = (f"✅ Your '{title}' signal chain is ready. I checked all {nb} blocks "
             f"and the chain passed validation. ")
        if evm is not None:
            s += (f"Its EVM signal-quality score is {evm:.1f}%; "
                  f"a smaller number means a cleaner signal. ")
        s += "You can ask me to increase the noise next and see how the plots change."
        return s + " " + _OFFLINE_NOTE

    if lvl == "expert":
        s = f"✅ Generated '{title}' ({recipe.difficulty}), {nb} blocks; validation passed."
        if evm is not None:
            s += f" EVM={evm:.2f}%."
        knob_keys = ", ".join(list(recipe.knobs)[:3])
        s += f" Adjustable parameters: {knob_keys}."
        return s + " " + _OFFLINE_NOTE

    # student
    s = f"✅ Your '{title}' chain is ready and passed validation."
    if evm is not None:
        s += f" EVM is {evm:.2f}%."
    return s + " " + _OFFLINE_NOTE


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

    if lvl == "novice":
        s = f"I checked the signal quality ({metric}={val_str}). {_plain(verdict)} "
        if suggestions:
            s += "My suggestion: " + suggestions[0]["say_novice"]
        return s

    if lvl == "expert":
        s = f"{metric}={val_str} -> {verdict}."
        if suggestions:
            tips = "; ".join(x["knob"] + " " + x["dir"] for x in suggestions[:3])
            s += f" Suggestions: {tips}."
        return s

    # student
    s = f"Current {metric}={val_str}; assessment: {verdict}."
    if suggestions:
        s += " Try: "
        s += "; ".join(f"{x['say_student']}" for x in suggestions[:2])
    return s


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _fmt_errors(errs) -> str:
    if not errs:
        return "(No details available)"
    out = []
    for e in errs[:3]:
        if isinstance(e, dict):
            out.append(e.get("hint") or e.get("error", ""))
        else:
            out.append(str(e))
    return " / ".join(out)


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
