"""orchestrator:用 deepagents ``create_deep_agent`` 组装 DeepRadio 主 Agent。

这是本层的核心 —— 严格按 ``local/docs/agent_architecture_deepagents.md``,
用 deepagents **现成库**装配真正的深度代理,不再自研编排器:

* ``model``   —— :func:`service.model.build_chat_model` 封装的 ``ChatOpenAI``;
* ``tools``   —— :func:`service.tools_lc.build_grc_tools` 桥接的确定性建图工具;
* ``subagents`` —— :func:`service.subagents.build_grc_subagents` 的 ``SubAgent`` 列表;
* ``skills``  —— ``skills`` 目录(deepagents 渐进式披露 SKILL);
* ``backend`` —— :func:`service.backend.build_backend` 的 ``CompositeBackend``;
* ``checkpointer`` —— ``InMemorySaver``(会话内断点续跑)。

**降级红线**(文档红线 4):未装 deepagents 或未配置 LLM 时,``build_agent``
返回 ``None``,由 :mod:`service.adapter` 走确定性 ``design_link`` 骨架 —— 保证
无 LLM 也能建图(论文 baseline)。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..tools.registry import ToolContext
from . import backend as _backend
from . import model as _model
from . import subagents as _subagents
from . import system_prompt as _sp

logger = logging.getLogger(__name__)
_CHECKPOINTERS = {}


def deepagents_available() -> bool:
    """探测 deepagents 库是否可用(不抛异常)。"""
    try:
        import deepagents  # noqa: F401
        return True
    except ImportError:
        return False


def build_agent(
    ctx: ToolContext, *, stage: Any = None, temperature: float = 0.2
) -> Optional[Any]:
    """组装并返回一个 deepagents 深度代理(``CompiledStateGraph``)。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),绑定业务工具。
        temperature: 主 Agent 采样温度。

    Returns:
        编译好的 deepagents 图;若缺 deepagents 或未配置 LLM 则返回 ``None``
        (调用方据此降级到确定性骨架)。
    """
    if not deepagents_available():
        logger.info("未安装 deepagents,主 Agent 降级到确定性骨架。")
        return None
    if not _model.is_available():
        logger.info("未配置 LLM(GRC_AGENT_*)或缺 langchain_openai,降级到确定性骨架。")
        return None

    from deepagents import create_deep_agent

    try:
        from langgraph.checkpoint.memory import InMemorySaver
        state = ctx.extra.get("state")
        session_id = getattr(state, "session_id", "") or "default"
        checkpointer = _CHECKPOINTERS.setdefault(session_id, InMemorySaver())
    except ImportError:
        checkpointer = None

    chat = _model.build_chat_model(temperature=temperature)
    agent_names = list(getattr(stage, "recommended_agents", None) or [])
    tool_names = _subagents.tool_names_for_agents(agent_names)
    tools = _import_tools(ctx, tool_names)
    subs = _subagents.build_grc_subagents(ctx, agent_names)
    be = _backend.build_backend()
    style_prompt = _resolve_style_prompt(ctx)
    orch_prompt = _sp.build_orchestrator_prompt(
        agent_names or _subagents.subagent_names(), style_prompt=style_prompt)
    if stage is not None:
        orch_prompt += (
            "\n【当前 Workflow Stage】\n"
            f"stage_id={stage.id}; completion={stage.completion}; "
            f"只允许委派: {', '.join(agent_names)}。\n"
        )

    agent: Any = create_deep_agent(
        model=chat,
        tools=tools,
        system_prompt=orch_prompt,
        subagents=subs,
        skills=[_backend.SKILLS_MOUNT],
        backend=be,
        checkpointer=checkpointer,
    )
    logger.info("deepagents 主 Agent 组装完成: %d tools, %d subagents",
                len(tools), len(subs))
    return agent


def _import_tools(ctx: ToolContext, allowed: list[str] | None = None) -> list:
    """主 Agent 也持有建图工具(可不委派直接建简单图)。"""
    from . import tools_lc
    return tools_lc.build_grc_tools(ctx, allowed=allowed)


def _resolve_style_prompt(ctx: ToolContext) -> str:
    """从 ctx.extra['profile'] 取当前档位的表达风格串(创新 B 注入点)。

    兼容 profile 为 None / 非 UserProfile / 缺 style_prompt 的情况,任何异常
    都返回空串(不注入 STYLE 段),绝不影响主流程。
    """
    profile = ctx.extra.get("profile") if hasattr(ctx, "extra") else None
    if profile is None:
        return ""
    try:
        fn = getattr(profile, "style_prompt", None)
        if callable(fn):
            style = fn()
            return style if isinstance(style, str) else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 profile 风格失败,忽略: %s", exc)
    return ""
