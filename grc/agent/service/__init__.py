"""DeepAgents runtime: MainAgent plans, SubAgents execute, host persists."""

from __future__ import annotations

__all__ = [
    "MainAgentRuntime",
    "build_mainagent_runtime",
    "build_grc_subagents",
]


def __getattr__(name):
    """惰性暴露高层入口,避免在无 gnuradio / 无 LLM 场景下过早导入。"""
    if name in ("MainAgentRuntime", "build_mainagent_runtime"):
        from .mainagent_runtime import MainAgentRuntime, build_mainagent_runtime
        return {
            "MainAgentRuntime": MainAgentRuntime,
            "build_mainagent_runtime": build_mainagent_runtime,
        }[name]
    if name == "build_grc_subagents":
        from .subagents import build_grc_subagents
        return build_grc_subagents
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
