"""DeepAgents runtime: one MainAgent plans and executes, host persists."""

from __future__ import annotations

__all__ = [
    "MainAgentRuntime",
    "build_mainagent_runtime",
]


def __getattr__(name):
    """惰性暴露高层入口,避免在无 gnuradio / 无 LLM 场景下过早导入。"""
    if name in ("MainAgentRuntime", "build_mainagent_runtime"):
        from .mainagent_runtime import MainAgentRuntime, build_mainagent_runtime
        return {
            "MainAgentRuntime": MainAgentRuntime,
            "build_mainagent_runtime": build_mainagent_runtime,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
