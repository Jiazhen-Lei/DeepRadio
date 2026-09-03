"""GRC Agent —— 自动流图设计能力。

本包在 GRC 之上增加一层"意图 -> 流图"的能力，不修改 grc/core。
子模块规划:
    env        环境引导(混搭 conda 运行时时的桥接)
    llm        LLM 后端(function-calling / 文本, 配置来自 GRC_AGENT_*)
    schema     GUI 契约(AgentReply / ToolInvocation, service 层回填给 GUI 渲染)
    tools      动词壳: 原子工具层(可被 LLM function-calling 调度) +
               design_link / debug_by_metric / narrate 领域动作
    skills     喂给 deepagents 的 SKILL markdown 目录(渐进式披露)
    knowledge  名词料: 领域知识层(通信任务配方库 recipes)
    runtime    名词料: 无头仿真执行体(simulate)
    memory     名词料: 用户画像(创新 B, profile)
    service    ★ deepagents 装配层(create_deep_agent: 单 MainAgent + SKILL)

对外高层入口:
    UserProfile    三档用户画像(创新 B 数据核心, 见 grc.agent.memory)
    design_link / debug_by_metric   领域动作(见 grc.agent.tools)
    MainAgentRuntime   MainAgent 的宿主运行环境(见 grc.agent.service);
                   step(text) 返回 AgentReply, GUI 侧渲染逻辑零改动。
"""

from __future__ import annotations

__all__ = [
    "env", "llm", "UserProfile",
    "design_link", "debug_by_metric",
    "MainAgentRuntime", "build_mainagent_runtime",
]

#: 顶层惰性入口名 -> (子模块, 属性名)。避免无 gnuradio 时过早导入依赖链。
_LAZY = {
    "UserProfile": ("memory", "UserProfile"),
    "design_link": ("tools.design_link", "design_link"),
    "debug_by_metric": ("tools.debug_by_metric", "debug_by_metric"),
    "MainAgentRuntime": ("service", "MainAgentRuntime"),
    "build_mainagent_runtime": ("service", "build_mainagent_runtime"),
}


def __getattr__(name):
    """惰性暴露高层入口, 避免在无 gnuradio 运行时的场景下过早导入。"""
    target = _LAZY.get(name)
    if target:
        mod = __import__(f"{__name__}.{target[0]}", fromlist=[target[1]])
        return getattr(mod, target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
