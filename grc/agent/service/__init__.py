"""service:DeepAgents 编排层(新架构主路径)。

本层在现有 ``grc/agent`` 之上,用 **deepagents 现成库**引入「主 Agent 编排 +
专职 subagent + SKILL 渐进式披露 + 会话落盘镜像」范式(见
``local/docs/agent_architecture_deepagents.md``),同时守住两条底线:

1. **优雅降级**(红线 4):未安装 ``deepagents`` 或未配置 LLM(``GRC_AGENT_*``)
   时,``adapter`` 自动回落到确定性 ``design_link`` 宏建图 —— 无 LLM 也能产出
   ``.grc``(论文 baseline)。
2. **不破坏 GUI 契约**:对 GUI 暴露的仍是 ``AgentReply`` 与磁盘上的 ``.grc``
   路径,新事件只在 ``adapter`` 内部消费。

子模块:
    backend         deepagents 原生 CompositeBackend 装配(State + SKILL 只读挂载)
    model           把 llm.get_config() 封装为 LangChain ChatOpenAI
    tools_lc        把确定性建图工具桥接为 LangChain @tool
    session_store   落盘镜像 + 会话事件流(mirror_files / append_event)
    system_prompt   主 Agent 与各 subagent 的 system-prompt 构造
    subagents       build_grc_subagents():deepagents SubAgent 列表
    orchestrator    build_agent():create_deep_agent 组装主 Agent
    adapter         运行结果 -> AgentReply / .grc 路径(GUI 契约守门人)

对外高层入口:
    ServiceAgent    服务级 Agent(见 adapter)
    build_service_agent(...)   便捷构造入口
"""

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
