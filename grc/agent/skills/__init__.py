"""skills:面向子目标的能力层。

skills 把原子 tools 编排成\"完成一件事\"的完整行为,是 core.agent 之下、
tools 之上的中间层。每个 skill 都遵循同一契约:

    skill(ctx: ToolContext, profile: UserProfile | None, **kwargs) -> dict

其中返回 dict 必带 ``ok``,并尽量给出 ``narrative``(按 profile 档位渲染的
自然语言解说),这样 agent 既可在有 LLM 时把 skill 当\"宏工具\"调度,
也可在无 LLM 时直接跑 skill 得到确定性结果(论文 baseline / 兜底)。

四个能力(对应架构文档):
    design_link      意图 -> 选配方 -> 增量建图 -> critic 自检 -> (可选)仿真
    explain_block    给某个块产出\"它是什么/为什么这么配\"的分档解说
    debug_by_metric  拿指标(EVM/BER/频谱)定位问题并给出改参建议
    adapt_expertise  按用户信号调整画像档位并重渲染表达(创新 B 的执行体)
"""

from __future__ import annotations

_EXPORTS = {
    "design_link": "design_link",
    "explain_block": "explain_block",
    "debug_by_metric": "debug_by_metric",
    "adapt_expertise": "adapt_expertise",
}


def __getattr__(name):
    mod_name = _EXPORTS.get(name)
    if mod_name:
        mod = __import__(f"{__package__}.{mod_name}", fromlist=[name])
        attr = getattr(mod, name)
        # 子模块与其内同名函数重名时, __import__ 可能返回模块本身,
        # 此处确保拿到的是可调用的函数而非模块对象。
        if not callable(attr):
            attr = getattr(mod, name, attr)
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
