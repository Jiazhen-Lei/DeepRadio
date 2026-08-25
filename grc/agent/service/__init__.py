"""service:DeepAgents 编排层(新架构主路径)。

本层在现有 ``grc/agent`` 之上,用 **deepagents 现成库**引入「主 Agent 编排 +
专职 subagent + SKILL 渐进式披露 + 会话落盘镜像」范式(见
``local/docs/agent_architecture_deepagents.md``),同时守住两条底线:

1. **优雅降级**(红线 4):未安装 ``deepagents`` 或未配置 LLM(``GRC_AGENT_*``)
   时,``adapter`` 自动回落到确定性 ``design_link`` 宏建图 —— 无 LLM 也能产出
   ``.grc``(论文 baseline)。
2. **不破坏 GUI 契约**:对 GUI 暴露的仍是 ``AgentReply`` 与磁盘上的 ``.grc``
   路径,新事件只在 ``adapter`` 内部消费。

Live vs baseline (do not delete either):
    Live: ``ServiceAgent`` + ``WorkflowEngine`` with both
    ``deterministic_stage_handler`` and optional LLM/deepagents.
    Baseline: ``build_flow_graph_from_text`` / ``design_link``.
    Hardware identity lives in ``HardwareProfile.default_device_args``;
    ``tools_lc`` is a LangChain bridge over ``registry.call``.

子模块:
    adapter           ServiceAgent（step / 画布 / 确定性 Stage / 可选 LLM）
    orchestrator      build_agent()、ChatOpenAI、CompositeBackend
    tools_lc          确定性工具桥为 LangChain @tool
    session_store     落盘镜像 + 会话事件流
    subagents         SubAgent 列表与 system-prompt
    stage_executor    确定性执行器 / ResultEnvelope
    hardware_runtime  受控 RF 子进程

对外高层入口:
    ServiceAgent    服务级 Agent
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
