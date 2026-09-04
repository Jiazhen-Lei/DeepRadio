"""统一数据结构:Agent 与 GUI 之间流转的类型(GUI 契约)。

:class:`AgentReply` / :class:`ToolInvocation` 是 MainAgentRuntime 返回给 GUI
的稳定契约;:class:`ExpertiseLevel` 等供画像/渲染分档使用。

保持"贫血"——只装数据、少行为,便于序列化回喂 LLM、也便于 CHI 实验埋点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ExpertiseLevel(str, Enum):
    """User-selected reply style."""

    BEGINNER = "beginner"
    PRACTITIONER = "practitioner"
    EXPERT = "expert"


@dataclass
class ToolInvocation:
    """一次工具调用的记录(ReAct 的 Action+Observation)。"""

    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass
class AgentReply:
    """Agent 一次 step 的返回。

    Attributes:
        text: 给用户看的自然语言回复(已按 profile 渲染)。
        stage: 当前所处的协商阶段名。
        needs_confirmation: 是否是一个 checkpoint(等用户确认/驳回)。
        tool_invocations: 本轮执行过的工具调用记录(CHI 埋点/调试用)。
        artifacts: 产物(如 grc 路径、图片路径、指标)。
        done: 会话是否结束。
        pending: 待用户确认的 Policy 项(若有)。
    """

    text: str = ""
    stage: str = ""
    needs_confirmation: bool = False
    tool_invocations: List[ToolInvocation] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    done: bool = False
    claims: List[Dict[str, Any]] = field(default_factory=list)
    spec_digest: Dict[str, Any] = field(default_factory=dict)
    pending: Dict[str, Any] = field(default_factory=dict)
    workflow_digest: Dict[str, Any] = field(default_factory=dict)
