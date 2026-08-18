"""core 内核抽象包:Agent / Planner / Context / Schema。

这是 DeepAgent 的 Orchestration 层(架构文档第 3 节 ③④)。
* schema   统一数据结构(Intent/Plan/Stage/Step/AgentReply...)
* context  AgentContext:贯穿一次会话的上下文(含 ToolContext / 历史 / profile)
* planner  Planner:分层协商状态机(创新 A 骨架)
* agent    Agent:ReAct 主循环,受 planner 约束地调度 tools

对外主入口:``from grc.agent.core import Agent``。

子模块按需导入,此处不做即时导入,以免
``python -m grc.agent.core.agent`` 触发 runpy 警告。
"""

from __future__ import annotations


def __getattr__(name):
    if name == "Agent":
        from .agent import Agent
        return Agent
    if name in ("Planner", "Stage"):
        from . import planner
        return getattr(planner, name)
    if name in ("AgentReply", "Intent", "Plan", "Step", "ToolInvocation"):
        from . import schema
        return getattr(schema, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
