"""orchestrator:用 deepagents ``create_deep_agent`` 组装 DeepRadio 主 Agent。

这是本层的核心 —— 严格按 ``local/docs/agent_architecture_deepagents.md``,
用 deepagents **现成库**装配真正的深度代理,不再自研编排器:

* ``model``   —— ``build_chat_model`` 把 ``llm.get_config()`` 封成 ``ChatOpenAI``;
* ``tools``   —— :func:`service.tools_lc.build_grc_tools` 桥接的确定性建图工具;
* ``subagents`` —— :func:`service.subagents.build_grc_subagents` 的 ``SubAgent`` 列表;
* ``skills``  —— ``skills`` 目录(deepagents 渐进式披露 SKILL);
* ``backend`` —— ``build_backend`` 的 ``CompositeBackend``;
* ``checkpointer`` —— ``InMemorySaver``(会话内断点续跑)。

**降级红线**(文档红线 4):未装 deepagents 或未配置 LLM 时,``build_agent``
返回 ``None``,由 :mod:`service.adapter` 走确定性 ``design_link`` 骨架 —— 保证
无 LLM 也能建图(论文 baseline)。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .. import llm
from ..tools.registry import ToolContext
from . import subagents as _subagents

logger = logging.getLogger(__name__)
_CHECKPOINTERS = {}
_STAGE_TOOLS = {
    "spec_alignment": {"spec_clarify", "spec_commit"},
    "rx_spec_alignment": {"spec_clarify", "spec_commit"},
    "inspect_and_plan": {"inspect_flowgraph", "spec_clarify", "spec_commit"},
    "inspect_and_diagnose": {
        "inspect_flowgraph", "validate_flowgraph", "run_simulation", "read_metric",
        "plot_spectrum", "plot_constellation", "diagnose_by_metric",
        "explain_error",
    },
    "inspect_and_measure": {
        "inspect_flowgraph", "validate_flowgraph", "run_simulation", "read_metric",
        "plot_spectrum", "plot_constellation", "plot_eye", "verify_claims",
    },
    "repair_and_verify": {
        "apply_grc_diff", "apply_flowgraph_patch", "validate_flowgraph",
        "run_simulation", "read_metric", "verify_claims", "explain_error",
    },
    "rx_build_and_verify": {
        "select_recipe", "design_flowgraph", "build_usrp_rx_spectrum_flowgraph",
        "validate_flowgraph", "inspect_flowgraph",
    },
    "tx_build_and_validate": {
        "select_recipe", "design_flowgraph", "validate_flowgraph",
        "build_sdr_tx_flowgraph",
    },
    "build_and_verify": {
        "select_recipe", "design_flowgraph", "validate_flowgraph",
        "run_simulation", "read_metric", "plot_spectrum",
        "build_sdr_tx_flowgraph",
    },
    "apply_and_verify": {
        "design_flowgraph", "apply_grc_diff", "apply_flowgraph_patch",
        "validate_flowgraph", "run_simulation", "read_metric", "plot_spectrum",
        "plot_constellation", "plot_eye", "verify_claims", "explain_error",
    },
    "hardware_precheck": {"hardware_preflight", "discover_devices", "inspect_flowgraph"},
    "configure_and_check": {"configure_sdr", "hardware_preflight", "inspect_flowgraph"},
    "protocol_spec_alignment": {"spec_clarify", "spec_commit"},
    "build_ble_advertiser": {
        "build_ble_advertising_pdu", "generate_ble_1m_waveform",
        "build_ble_uhd_tx_flowgraph", "build_ble_pluto_tx_flowgraph",
        "validate_flowgraph",
    },
    "offline_protocol_verify": {"verify_ble_packet_bits", "validate_flowgraph"},
    "discover_and_probe_device": {"discover_devices", "probe_device"},
    "discover_and_probe_hardware": {"discover_devices", "probe_device"},
    "configure_device": {
        "configure_sdr", "hardware_preflight", "arm_hardware_flowgraph",
    },
    "transmit_bounded": {"start_flowgraph", "query_runtime_status", "emergency_stop"},
    "stop_and_finalize": {"stop_flowgraph", "emergency_stop", "query_runtime_status"},
    "run_bounded": {"start_flowgraph", "query_runtime_status", "emergency_stop"},
    "stop_runtime": {"stop_flowgraph", "emergency_stop", "query_runtime_status"},
}


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
    if not is_available():
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

    chat = build_chat_model(temperature=temperature)
    agent_names = list(getattr(stage, "recommended_agents", None) or [])
    tool_names = _subagents.tool_names_for_agents(agent_names)
    allowed_stage_tools = _STAGE_TOOLS.get(getattr(stage, "id", ""))
    if allowed_stage_tools is not None:
        tool_names = [name for name in tool_names if name in allowed_stage_tools]
    tools = _import_tools(ctx, tool_names)
    subs = _subagents.build_grc_subagents(ctx, agent_names, tool_names)
    be = build_backend()
    style_prompt = _resolve_style_prompt(ctx)
    orch_prompt = _subagents.build_orchestrator_prompt(
        agent_names or _subagents.subagent_names(), style_prompt=style_prompt)
    if stage is not None:
        workflow_data = dict(ctx.extra.get("workflow") or {})
        intent_data = dict(workflow_data.get("intent") or {})
        orch_prompt += (
            "\n【当前 Workflow Stage】\n"
            f"stage_id={stage.id}; completion={stage.completion}; "
            f"只允许委派: {', '.join(agent_names)}。\n"
            f"capabilities={intent_data.get('capabilities') or []}; "
            f"slot_sources={intent_data.get('slot_sources') or {}}。"
            "raw_text 是目标原文；context 只能作为待验证背景，不能覆盖用户本轮参数。\n"
        )

    agent: Any = create_deep_agent(
        model=chat,
        tools=tools,
        system_prompt=orch_prompt,
        subagents=subs,
        skills=[SKILLS_MOUNT],
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
    model = ChatOpenAI(
        model=cfg["model"],
        base_url=f"{cfg['base_url']}/",
        api_key=cfg["api_key"],
        temperature=temperature,
        timeout=cfg["timeout"],
        max_retries=1,
    )
    logger.info("已构造 ChatOpenAI: model=%s base_url=%s",
                cfg["model"], cfg["base_url"])
    return model


def is_available() -> bool:
    """探测:是否既配置了 LLM 又装了 langchain_openai。"""
    if not llm.is_configured():
        return False
    try:
        import langchain_openai  # noqa: F401
        return True
    except ImportError:
        return False
