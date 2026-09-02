"""Assemble the MainAgent-owned DeepRadio control plane.

这是本层的核心 —— 严格按 ``local/docs/agent_architecture_deepagents.md``,
用 deepagents **现成库**装配真正的深度代理,不再自研编排器:

* ``model``   —— ``build_chat_model`` 把 ``llm.get_config()`` 封成 ``ChatOpenAI``;
* ``tools``   —— MainAgent-only Workflow control tools;
* ``subagents`` —— :func:`service.subagents.build_grc_subagents` 的 ``SubAgent`` 列表;
* ``skills``  —— ``skills`` 目录(deepagents 渐进式披露 SKILL);
* ``backend`` —— ``build_backend`` 的 ``CompositeBackend``;

未装 deepagents 或未配置 LLM 时返回 ``None``。生产链路不会静默切换到另一套
Workflow；确定性能力只作为 SubAgent 调用的工具存在。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .. import llm
from ..tools.registry import ToolContext
from . import subagents as _subagents

logger = logging.getLogger(__name__)
def deepagents_available() -> bool:
    """探测 deepagents 库是否可用(不抛异常)。"""
    try:
        import deepagents  # noqa: F401
        return True
    except ImportError:
        return False


def build_agent(ctx: ToolContext, *, temperature: float = 0.2) -> Optional[Any]:
    """组装并返回一个 deepagents 深度代理(``CompiledStateGraph``)。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),绑定业务工具。
        temperature: 主 Agent 采样温度。

    Returns:
        编译好的 deepagents 图；若缺 deepagents 或未配置 LLM 则返回 ``None``。
    """
    if not deepagents_available():
        logger.info("未安装 deepagents，MainAgent 不可用。")
        return None
    if not is_available():
        logger.info("未配置 LLM(GRC_AGENT_*)或缺 langchain_openai，MainAgent 不可用。")
        return None

    from deepagents import create_deep_agent

    chat = build_chat_model(temperature=temperature)
    agent_names = _subagents.subagent_names()
    subs = _subagents.build_grc_subagents(ctx)
    workflow_store = ctx.extra.get("workflow_store")
    if workflow_store is None:
        raise ValueError("ToolContext is missing the dynamic Workflow store")
    from .workflow_tools import build_workflow_tools

    # MainAgent never receives domain tools. DeepAgents adds ``task`` for
    # delegation; these two tools are its only host-side mutation authority.
    tools = build_workflow_tools(ctx, workflow_store)
    be = build_backend()
    style_prompt = _resolve_style_prompt(ctx)
    orch_prompt = _subagents.build_orchestrator_prompt(
        agent_names or _subagents.subagent_names(), style_prompt=style_prompt)

    agent: Any = create_deep_agent(
        model=chat,
        tools=tools,
        system_prompt=orch_prompt,
        subagents=subs,
        skills=[SKILLS_MOUNT],
        backend=be,
    )
    logger.info("deepagents 主 Agent 组装完成: %d tools, %d subagents",
        len(tools), len(subs))
    return agent


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

SKILLS_MOUNT = "/workspace/skills/"


def skills_root() -> str:
    """返回 SKILL 包根目录 ``grc/agent/skills`` 的绝对路径。"""
    agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(agent_dir, "skills")


def build_backend():
    """组装 deepagents 的 CompositeBackend。"""
    from deepagents.backends import CompositeBackend, StateBackend
    from deepagents.backends.filesystem import FilesystemBackend

    routes = {}
    root = skills_root()
    if os.path.isdir(root):
        routes[SKILLS_MOUNT] = FilesystemBackend(
            root_dir=root, virtual_mode=True)
    else:
        logger.warning("skills 目录不存在: %s(SKILL 只读挂载被跳过)", root)
    return CompositeBackend(default=StateBackend(), routes=routes)


def build_chat_model(temperature: float = 0.2):
    """按 ``llm.get_config()`` 构造一个 LangChain ``ChatOpenAI``。"""
    cfg = llm.get_config()
    from langchain_openai import ChatOpenAI
    extra_body: dict[str, Any] = {}
    # GLM-5.x: 委派/建图轮次关闭深度思考(单轮约省 45%); env 可切 enabled/auto。
    if cfg.get("thinking") in ("disabled", "enabled"):
        extra_body["thinking"] = {"type": cfg["thinking"]}
    # 单轮输出上限。实测同一个 Task 1 两次运行 115s vs 271s,差异几乎全部来自
    # "长输出轮"(最大单次 41s / 2621 tok);工具调用轮只需要几十到几百 token,
    # 最终答复也应是简短叙述,所以设一个足够宽但能挡住失控长文的上限。
    #
    # 必须走 extra_body: ChatOpenAI 会把 max_tokens(以及 model_kwargs 里的同名
    # 参数)统一转成 OpenAI 新参数名 ``max_completion_tokens``,而 bigmodel(GLM)
    # 只认 ``max_tokens`` —— 实测 max_completion_tokens=200 仍输出 1181 tok
    # (18.5s),max_tokens=200 才真正截断到 200 tok(3.9s)。
    max_tokens = _output_token_cap()
    if max_tokens:
        extra_body["max_tokens"] = max_tokens
    kwargs: dict[str, Any] = {"extra_body": extra_body} if extra_body else {}
    model = ChatOpenAI(
        model=cfg["model"],
        base_url=f"{cfg['base_url']}/",
        api_key=cfg["api_key"],
        temperature=temperature,
        timeout=cfg["timeout"],
        max_retries=1,
        **kwargs,
    )
    logger.info("已构造 ChatOpenAI: model=%s base_url=%s max_tokens=%s",
                cfg["model"], cfg["base_url"], max_tokens or "unset")
    return model


def _output_token_cap() -> int:
    """单轮 completion token 上限(``GRC_AGENT_MAX_OUTPUT_TOKENS``,0=不限)。"""
    try:
        return max(0, int(
            os.environ.get("GRC_AGENT_MAX_OUTPUT_TOKENS", "").strip() or 1200
        ))
    except ValueError:
        return 1200


def is_available() -> bool:
    """探测:是否既配置了 LLM 又装了 langchain_openai。"""
    if not llm.is_configured():
        return False
    try:
        import langchain_openai  # noqa: F401
        return True
    except ImportError:
        return False
