"""subagents:用 deepagents 原生 ``SubAgent`` 装配 4 个专职子代理。

对齐 ``local/docs/agent_architecture_deepagents.md``:主 Agent 通过内置 ``task``
工具委派子代理,每个子代理:

* 一个 system-prompt(来自 :mod:`system_prompt`);
* 一组 LangChain 工具(来自 :mod:`tools_lc`,绑定共享 ToolContext);
* 通过 ``skills`` 绑定专属 SKILL 名,子代理用内置文件工具按需读取
  ``/workspace/skills/<skill>/`` 下的 references(渐进式披露)。

本模块产出的是 deepagents 的 :class:`~deepagents.SubAgent`(TypedDict) 列表,
不再自研规格类 —— 直接喂给 ``create_deep_agent(subagents=...)``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..tools.registry import ToolContext
from . import system_prompt as _sp
from . import tools_lc

logger = logging.getLogger(__name__)

#: subagent 名 -> (描述, prompt 构造器, 绑定的 SKILL 名, 工具名子集)
_SUBAGENT_DEFS = [
    (
        "block_knowledge_agent",
        "检索 GRC 块、解释端口/参数、查找示例链路;只提供知识,不建图。",
        _sp.build_block_knowledge_prompt,
        "grc-block-rag",
        [],  # 只读 SKILL references,不需业务工具
    ),
    (
        "flowgraph_builder_agent",
        "按确定性配方选型并建图(design_flowgraph),产物写 build/。",
        _sp.build_builder_prompt,
        "grc-build",
        ["design_flowgraph", "validate_flowgraph"],
    ),
    (
        "flowgraph_critic_agent",
        "校验流图合法性,把报错整理为可执行修复建议;不直接改图。",
        _sp.build_critic_prompt,
        "grc-critic",
        ["validate_flowgraph"],
    ),
    (
        "simulation_agent",
        "对已校验流图无头仿真,读回 EVM/BER 等指标;产物写 sim/。",
        _sp.build_simulation_prompt,
        "grc-sim",
        ["run_simulation", "read_metric"],
    ),
]


def build_grc_subagents(ctx: ToolContext) -> List[Dict[str, Any]]:
    """按共享 ctx 生成 deepagents ``SubAgent`` 列表。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),用于绑定业务工具。

    Returns:
        SubAgent(TypedDict) 列表,可直接传给 ``create_deep_agent``。
    """
    all_tools = {t.name: t for t in tools_lc.build_grc_tools(ctx)}

    subagents: List[Dict[str, Any]] = []
    seen = set()
    for name, desc, prompt_builder, skill, tool_names in _SUBAGENT_DEFS:
        if name in seen:
            raise ValueError(f"subagent 名称重复: {name}")
        seen.add(name)

        sub: Dict[str, Any] = {
            "name": name,
            "description": desc,
            "system_prompt": prompt_builder(),
            "skills": [skill],
        }
        bound = [all_tools[n] for n in tool_names if n in all_tools]
        if bound:
            sub["tools"] = bound
        subagents.append(sub)

    return subagents


def subagent_names() -> List[str]:
    """返回 4 个 subagent 名称(供主 Agent prompt 列举)。"""
    return [d[0] for d in _SUBAGENT_DEFS]
