"""统一数据结构:Agent 与 GUI 之间流转的类型(GUI 契约)。

:class:`AgentReply` / :class:`ToolInvocation` 是 :mod:`grc.agent.service.adapter`
返回给 GUI 的稳定契约;:class:`ExpertiseLevel` 等供画像/渲染分档使用。

保持"贫血"——只装数据、少行为,便于序列化回喂 LLM、也便于 CHI 实验埋点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExpertiseLevel(str, Enum):
    """用户专业度档位(创新 B)。"""

    NOVICE = "novice"     # 小白:重比喻/少术语/多确认
    STUDENT = "student"   # 学生:标准 DSP 术语 + 适度解释
    EXPERT = "expert"     # 专家:精炼、参数直给、少寒暄


@dataclass
class Intent:
    """从用户话语中解析出的结构化意图。"""

    raw_text: str = ""
    task: str = ""                         # 如 "bpsk_awgn" / "fm_audio"
    modulation: Optional[str] = None       # bpsk/qpsk/...
    channel: Optional[str] = None          # awgn/multipath/...
    goal_metric: Optional[str] = None      # ber/evm/...
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0                # 0~1,低于阈值需追问
    missing: List[str] = field(default_factory=list)  # 还需澄清的槽位


@dataclass
class Step:
    """方案里的一步(通常对应一次工具调用或一个块)。"""

    action: str = ""                       # 工具名或动作描述
    args: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""                    # 为什么做这步(explain 用)


@dataclass
class Plan:
    """一条候选链路方案。"""

    name: str = ""
    summary: str = ""
    steps: List[Step] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)  # 期望块清单


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
    """

    text: str = ""
    stage: str = ""
    needs_confirmation: bool = False
    tool_invocations: List[ToolInvocation] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    done: bool = False
