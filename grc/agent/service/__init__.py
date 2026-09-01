"""DeepAgents service: MainAgent plans, SubAgents execute, host verifies."""

from __future__ import annotations

__all__ = [
    "ServiceAgent",
    "build_service_agent",
    "build_grc_subagents",
]


def __getattr__(name):
    """惰性暴露高层入口,避免在无 gnuradio / 无 LLM 场景下过早导入。"""
    if name in ("ServiceAgent", "build_service_agent"):
        from .adapter import ServiceAgent, build_service_agent
        return {"ServiceAgent": ServiceAgent,
                "build_service_agent": build_service_agent}[name]
    if name == "build_grc_subagents":
        from .subagents import build_grc_subagents
        return build_grc_subagents
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
