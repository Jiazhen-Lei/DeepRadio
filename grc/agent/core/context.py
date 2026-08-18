"""AgentContext:贯穿一次会话的上下文对象。

聚合 Agent 运行所需的一切可变状态:
* tool_ctx   工具运行上下文(platform / flow_graph / last_sim ...)
* history    多轮对话历史(role, content)
* intent     当前已解析的意图
* plan       当前选定的方案
* profile    用户专业度画像(创新 B 的数据核心, memory.UserProfile)

工具/技能都从这里取依赖,Agent 是它的唯一持有者。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from ..memory.profile import UserProfile
from ..tools.registry import ToolContext
from .schema import ExpertiseLevel, Intent, Plan


@dataclass
class AgentContext:
    """一次会话的完整上下文。"""

    tool_ctx: ToolContext = field(default_factory=ToolContext)
    history: List[Tuple[str, str]] = field(default_factory=list)
    intent: Optional[Intent] = None
    plan: Optional[Plan] = None
    #: 用户专业度画像(软状态, 每轮由 adapt_expertise 平滑更新)
    profile: UserProfile = field(default_factory=UserProfile)
    #: 是否开启自适应表达(实验里"自适应 vs 固定"的总开关)
    adaptive: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def profile_level(self) -> ExpertiseLevel:
        """兼容旧接口:把画像的当前档位读成 ExpertiseLevel 枚举。"""
        try:
            return ExpertiseLevel(self.profile.level)
        except ValueError:
            return ExpertiseLevel.STUDENT

    # -- 便捷方法 -----------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.history.append(("user", text))

    def add_assistant(self, text: str) -> None:
        self.history.append(("assistant", text))

    @property
    def platform(self) -> Any:
        return self.tool_ctx.platform

    @platform.setter
    def platform(self, value: Any) -> None:
        self.tool_ctx.platform = value
