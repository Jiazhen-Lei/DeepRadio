from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .. import llm
from ..tools.registry import ToolContext
from . import tools_lc

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
    workflow_store = ctx.extra.get("workflow_store")
    if workflow_store is None:
        raise ValueError("ToolContext is missing the dynamic Workflow store")
    from .workflow_tools import build_workflow_tools

    tools = build_workflow_tools(ctx, workflow_store) + tools_lc.build_grc_tools(ctx)
    be = build_backend()
    style_prompt = _resolve_style_prompt(ctx)
    orch_prompt = build_mainagent_prompt(style_prompt)
    _disable_default_subagent(str(llm.get_config()["model"]))

    agent: Any = create_deep_agent(
        model=chat,
        tools=tools,
        system_prompt=orch_prompt,
        subagents=[],
        skills=[SKILLS_MOUNT],
        backend=be,
    )
    logger.info("deepagents 单 MainAgent 组装完成: %d tools", len(tools))
    return agent


def _disable_default_subagent(model_name: str) -> None:
    """Disable DeepAgents' auto-added general-purpose subagent when supported."""
    try:
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )
    except ImportError:
        # Older DeepAgents releases did not auto-add this subagent.
        return

    profile_key = model_name if model_name.count(":") == 1 else f"openai:{model_name}"
    register_harness_profile(
        profile_key,
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
        ),
    )


def build_mainagent_prompt(style_prompt: str = "") -> str:
    style_section = f"\n【STYLE】{style_prompt}\n" if style_prompt else ""
    return """你是 DeepRadio 唯一的 MainAgent，也是唯一与用户交互的 Agent。

你负责理解用户意图、维护动态 Workflow，并亲自执行当前 Stage。处理涉及 Workflow 的请求时，必须先读取并遵循 grc-orchestration Skill 及 Stage 候选库，再读取当前 Stage 声明的领域 Skill。

每次只处理 current_stage。领域工具虽然可见，但只能调用当前 Stage 的 allowed_tools；宿主机会拒绝跨 Stage 调用。Stage 完成或失败后立即向用户报告并结束本轮，不自动开始下一 Stage。

工具结果、Artifact、Measurement 和 Evidence 必须绑定当前 Workflow、Stage 和工程版本。不能用叙述代替 Evidence，也不能绕过用户确认、工程写入或 RF 安全检查。

严格遵循 STYLE 中指定的回复语言和表达风格。保持简洁明确，不展示内部 JSON、Stage Context、工具日志或状态字段。""".strip() + style_section


def _resolve_style_prompt(ctx: ToolContext) -> str:
    """Read the UI-selected reply style and language from the tool context.

    Missing or invalid presentation settings never affect the control flow.
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
    thinking = cfg.get("thinking")
    if thinking in ("disabled", "enabled"):
        extra_body["thinking"] = {"type": thinking}
    elif thinking in ("low", "high", "max"):
        extra_body["thinking"] = {"type": "enabled"}
        extra_body["reasoning_effort"] = thinking
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
