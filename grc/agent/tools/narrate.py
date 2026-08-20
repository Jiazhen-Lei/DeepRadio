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
def narrate_design(recipe, result: Dict[str, Any], profile) -> str:
    lvl = _level(profile)
    ok = result.get("valid")
    evm = result.get("metrics", {}).get("evm_pct")
    nb = result.get("num_blocks")
    title = recipe.title

    if not ok:
        errs = result.get("errors", [])
        head = f"链路「{title}」搭建后校验未通过。"
        if lvl == "novice":
            return head + "别担心,我们一步步来,先看看是哪里没接好。"
        return head + f"错误要点:{_fmt_errors(errs)}"

    if lvl == "novice":
        s = (f"已经帮你搭好一条「{title}」的信号链路,共 {nb} 个模块,"
             f"并且检查过可以正常运行。")
        if evm is not None:
            s += (f"其中有个叫 EVM 的\"信号质量分\"是 {evm:.1f}%,"
                  f"数字越小说明信号越干净。")
        s += "接下来你可以让我把噪声调大一点,看看图会怎么变。"
        return s

    if lvl == "expert":
        s = f"已生成「{title}」({recipe.difficulty}),{nb} 块,校验通过。"
        if evm is not None:
            s += f" EVM={evm:.2f}%。"
        knob_keys = ", ".join(list(recipe.knobs)[:3])
        s += f" 可调旋钮:{knob_keys}。"
        return s

    # student
    s = (f"已按配方「{title}」搭好链路(难度 {recipe.difficulty},{nb} 块)"
         f"并通过校验。")
    if evm is not None:
        s += (f" 星座点相对理想位置的均方误差(EVM)为 {evm:.2f}%,"
              f"它综合反映了噪声/失真对判决的影响。")
    if recipe.knobs:
        first = next(iter(recipe.knobs.items()))
        s += f" 想调质量可从「{first[0]}」入手:{first[1]}。"
    return s


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
        s = f"我看了下信号质量({metric}={val_str})。{_plain(verdict)} "
        if suggestions:
            s += "我的建议是:" + suggestions[0]["say_novice"]
        return s

    if lvl == "expert":
        s = f"{metric}={val_str} -> {verdict}."
        if suggestions:
            tips = "; ".join(x["knob"] + " " + x["dir"] for x in suggestions[:3])
            s += f" 建议:{tips}."
        return s

    # student
    s = f"当前 {metric}={val_str},判断:{verdict}。"
    if suggestions:
        s += " 可尝试:"
        s += "; ".join(f"{x['say_student']}" for x in suggestions[:2])
    return s


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _fmt_errors(errs) -> str:
    if not errs:
        return "(无详细信息)"
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
        "EVM 偏高": "信号有点乱",
        "EVM 正常": "信号挺干净",
        "误码率": "出错的比例",
        "噪声": "杂音",
        "星座": "信号点分布图",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text
