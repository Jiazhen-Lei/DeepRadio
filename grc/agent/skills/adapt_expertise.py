"""adapt_expertise:根据用户信号调整画像档位并给出重渲染指令(创新 B 执行体)。

它是\"同一后端、分档表达\"闭环里的**控制器**:
    观测用户这一句 -> 更新 UserProfile(EMA 平滑 / 显式钉档)
    -> 若档位发生迁移,产出\"重渲染指令\"(新的 style prompt + 迁移说明)。

agent 拿到重渲染指令后,会把它注入下一次 system prompt 的 STYLE 段,
从而让**同一个 LLM**对同一问题以不同繁简/术语密度作答。无 LLM 时,
skills 层各自的 narrate 也直接吃这个档位。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..memory import profile as _profile


def adapt_expertise(ctx=None, profile=None, text: str = "",
                    pin: str = "") -> Dict[str, Any]:
    """观测一句用户输入并调整画像。

    Args:
        ctx: 兼容 skill 统一签名,可为 None(本 skill 不碰流图)。
        profile: 现有 UserProfile;None 则新建。
        text: 本轮用户输入(用于推断专业度信号)。
        pin: 显式钉档("novice"/"student"/"expert"),优先级最高。

    Returns:
        dict:ok / level(新档) / prev_level / changed / signals /
        style_prompt(注入 system prompt 用) / note(迁移说明)。
    """
    prof = profile or _profile.UserProfile()
    prev = prof.level

    if pin in _profile.LEVELS:
        prof.pin(pin)
    if text:
        prof.observe(text)

    new = prof.level
    sig = _profile.infer_level_signals(text) if text else \
        _profile.ProfileSignals()

    changed = new != prev
    note = _migration_note(prev, new) if changed else ""

    return {
        "ok": True,
        "level": new,
        "prev_level": prev,
        "changed": changed,
        "score": round(prof.score, 3),
        "pinned": prof.pinned,
        "signals": sig.as_dict(),
        "style_prompt": prof.style_prompt(),
        "note": note,
        "_profile": prof,   # 便于调用方拿回更新后的对象
    }


_LEVEL_CN = {"novice": "小白", "student": "学生", "expert": "专家"}


def _migration_note(prev: str, new: str) -> str:
    a, b = _LEVEL_CN.get(prev, prev), _LEVEL_CN.get(new, new)
    order = {"novice": 0, "student": 1, "expert": 2}
    if order.get(new, 1) > order.get(prev, 1):
        return f"检测到更专业的表述,表达档位从「{a}」上调到「{b}」,后续会更精炼、多讲权衡。"
    return f"表达档位从「{a}」下调到「{b}」,后续会更通俗、少用术语、多打比方。"
