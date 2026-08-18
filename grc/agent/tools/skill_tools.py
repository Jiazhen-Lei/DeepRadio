"""skill_tools:把 skills 层能力包装成可被 LLM function-calling 调度的"宏工具"。

skills(design_link/debug_by_metric)本身是"面向子目标"的编排能力,签名是
``skill(ctx, profile, **kw)``。为了让 LLM 在 BUILD/TUNE 阶段能像调原子工具一样
一步到位地调用它们,这里用统一的 ``@tool`` 装饰器把它们注册进 registry,
归入 ``macro`` 分组。

关键桥接:tools 的统一签名是 ``fn(ctx: ToolContext, **kwargs)``,不含 profile;
而 skills 需要 profile 来分档渲染 narrative。因此约定 profile 通过
``ctx.extra["profile"]`` 传入(Agent 每轮把 ctx.profile 注入 tool_ctx.extra),
宏工具在此取出转交给 skill。这样既不破坏 tools 的无状态契约,又让创新 B 的
分档表达贯穿到 LLM 调度路径。
"""

from __future__ import annotations

from typing import Any, Dict

from .registry import tool


def _profile_of(ctx):
    """从 tool_ctx.extra 取 UserProfile(Agent 注入);无则返回 None。"""
    try:
        return ctx.extra.get("profile")
    except AttributeError:
        return None


@tool(
    name="design_link",
    description=(
        "宏工具:按一句通信意图端到端搭出一张可跑流图(选配方→逐块建图→"
        "critic 自检→可选仿真取指标→存 .grc)。适合 BUILD 阶段一步到位;"
        "给 intent(自然语言)或 recipe(配方名 tone_noise/bpsk_awgn/qpsk_awgn/ofdm_awgn)之一。"),
    parameters={
        "type": "object",
        "properties": {
            "intent": {"type": "string",
                       "description": "自然语言意图,用于离线选配方"},
            "recipe": {"type": "string",
                       "description": "显式配方名,优先于 intent"},
            "simulate": {"type": "boolean",
                         "description": "建好后是否顺带仿真取指标,默认 true"},
            "render": {"type": "boolean",
                       "description": "是否存 .grc,默认 true"},
        },
    },
    group="macro",
)
def design_link_tool(ctx, intent: str = "", recipe: str = "",
                     simulate: bool = True, render: bool = True) -> Dict[str, Any]:
    from ..skills.design_link import design_link
    return design_link(ctx, _profile_of(ctx), intent=intent, recipe=recipe,
                       simulate=simulate, render=render)


@tool(
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
)
def debug_by_metric_tool(ctx, metric: str = "evm", probe_id: str = "",
                         modulation: str = "bpsk", sps: int = 4) -> Dict[str, Any]:
    from ..skills.debug_by_metric import debug_by_metric
    return debug_by_metric(ctx, _profile_of(ctx), metric=metric,
                          probe_id=probe_id, modulation=modulation, sps=sps)
