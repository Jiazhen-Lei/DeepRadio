"""Assemble the six DeepRadio domain subagents.

The main agent delegates TaskCards through deepagents' built-in task tool.
Each domain agent receives:

* 一个 system-prompt(来自 :mod:`system_prompt`);
* 一组 LangChain 工具(来自 :mod:`tools_lc`,绑定共享 ToolContext);
* 通过 ``skills`` 绑定专属 SKILL 名,子代理用内置文件工具按需读取
  ``/workspace/skills/<skill>/`` 下的 references(渐进式披露)。

The returned dictionaries are passed directly to ``create_deep_agent``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..tools.registry import ToolContext
from . import system_prompt as _sp
from . import tools_lc

logger = logging.getLogger(__name__)

#: subagent 名 -> (描述, prompt 构造器, SKILL 列表, 工具名子集)
_SUBAGENT_DEFS = [
    (
        "spec_agent",
        "维护可追溯 RadioSpec 与成功条件。",
        _sp.build_spec_prompt,
        ["grc-spec"],
        ["spec_clarify", "spec_commit"],
    ),
    (
        "radio_design_agent",
        "检索块知识并选择确定性通信配方。",
        _sp.build_radio_design_prompt,
        ["grc-block-rag"],
        ["select_recipe", "search_blocks", "describe_block", "list_examples"],
    ),
    (
        "flowgraph_agent",
        "构建或增量修改 GNU Radio 流图。",
        _sp.build_flowgraph_prompt,
        ["grc-build", "grc-block-rag"],
        ["design_flowgraph", "inspect_flowgraph", "apply_grc_diff", "apply_flowgraph_patch"],
    ),
    (
        "verification_agent",
        "校验、仿真、绘图并将证据绑定到 Claim。",
        _sp.build_verification_prompt,
        ["grc-critic", "grc-sim"],
        [
            "validate_flowgraph",
            "run_simulation",
            "read_metric",
            "plot_spectrum",
            "plot_constellation",
            "plot_eye",
            "explain_error",
            "verify_claims",
        ],
    ),
    (
        "diagnosis_agent",
        "根据指标诊断并提出最小修复。",
        _sp.build_diagnosis_prompt,
        ["grc-diagnosis", "grc-critic"],
        ["diagnose_by_metric", "explain_error", "suggest_fix"],
    ),
    (
        "hardware_agent",
        "管理 SDR flowgraph 配置；真实硬件操作保持禁用。",
        _sp.build_hardware_prompt,
        ["grc-hardware", "grc-build"],
        ["hardware_preflight", "configure_sdr", "list_devices", "inspect_flowgraph"],
    ),
]


def build_grc_subagents(
    ctx: ToolContext, allowed_agents: List[str] | None = None
) -> List[Dict[str, Any]]:
    """按共享 ctx 生成 deepagents ``SubAgent`` 列表。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),用于绑定业务工具。

    Returns:
        SubAgent(TypedDict) 列表,可直接传给 ``create_deep_agent``。
    """
    subagents: List[Dict[str, Any]] = []
    seen = set()
    allowed = set(allowed_agents or ())
    for name, desc, prompt_builder, skills, tool_names in _SUBAGENT_DEFS:
        if allowed and name not in allowed:
            continue
        if name in seen:
            raise ValueError(f"subagent 名称重复: {name}")
        seen.add(name)

        sub: Dict[str, Any] = {
            "name": name,
            "description": desc,
            "system_prompt": prompt_builder(),
            "skills": [f"/workspace/skills/{skill}/" for skill in skills],
        }
        bound = tools_lc.build_grc_tools(ctx, allowed=tool_names)
        if bound:
            sub["tools"] = bound
        subagents.append(sub)

    return subagents


def subagent_names() -> List[str]:
    """返回 6 个 subagent 名称(供主 Agent prompt 列举)。"""
    return [d[0] for d in _SUBAGENT_DEFS]


def tool_names_for_agents(agent_names: List[str]) -> List[str]:
    wanted = set(agent_names or [])
    return sorted({
        tool_name
        for name, _desc, _prompt, _skills, tool_names in _SUBAGENT_DEFS
        if not wanted or name in wanted
        for tool_name in tool_names
    })
