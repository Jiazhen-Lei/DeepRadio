"""session.py:一次多轮对话的运行时容器。

把散落在 core 里的\"历史消息 + 画像 + 工具调用 trace\"收编成一个可
序列化对象,便于:(1) 落盘复现实验;(2) 论文里画协商时序;(3) 断点续聊。

与 :class:`grc.agent.core.context.AgentContext` 的分工:
    - AgentContext 持有\"当前活跃状态\"(平台句柄、正在建的流图、intent/plan);
    - Session 持有\"可持久化的对话叙事\"(逐轮文本 + 画像快照 + trace)。

保持零外部依赖。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Turn:
    """一轮交互(用户一句 + 助手一句 + 本轮工具调用摘要)。"""

    role: str                       # "user" | "assistant" | "system"
    text: str
    stage: Optional[str] = None     # planner 阶段
    level: Optional[str] = None     # 当轮用户画像档位
    tool_calls: List[dict] = field(default_factory=list)  # [{name,args,ok}]
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "stage": self.stage,
                "level": self.level, "tool_calls": self.tool_calls,
                "ts": round(self.ts, 3)}


@dataclass
class Session:
    """多轮对话叙事 + trace。"""

    session_id: str = ""
    turns: List[Turn] = field(default_factory=list)
    meta: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.session_id:
            self.session_id = time.strftime("sess-%Y%m%d-%H%M%S")

    # -- 记录 --------------------------------------------------------------
    def add_user(self, text: str, level: Optional[str] = None) -> Turn:
        t = Turn(role="user", text=text, level=level)
        self.turns.append(t)
        return t

    def add_assistant(self, text: str, stage: Optional[str] = None,
                      tool_calls: Optional[List[dict]] = None) -> Turn:
        t = Turn(role="assistant", text=text, stage=stage,
                 tool_calls=list(tool_calls or []))
        self.turns.append(t)
        return t

    def record_tool(self, name: str, args: dict, ok: bool) -> None:
        """把一次工具调用附到最近一条 assistant turn(没有则新建)。"""
        if not self.turns or self.turns[-1].role != "assistant":
            self.add_assistant("", tool_calls=[])
        self.turns[-1].tool_calls.append(
            {"name": name, "args": _shrink(args), "ok": bool(ok)})

    # -- 视图 --------------------------------------------------------------
    def last_user_text(self) -> str:
        for t in reversed(self.turns):
            if t.role == "user":
                return t.text
        return ""

    def tool_trace(self) -> List[dict]:
        """扁平化所有工具调用,供论文画\"协商-建图-仿真\"时序。"""
        out: List[dict] = []
        for t in self.turns:
            for c in t.tool_calls:
                out.append({"stage": t.stage, **c})
        return out

    def transcript(self, limit: Optional[int] = None) -> List[dict]:
        rows = [t.as_dict() for t in self.turns]
        return rows[-limit:] if limit else rows

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "meta": self.meta,
                "turns": [t.as_dict() for t in self.turns]}

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "Session":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        s = cls(session_id=d.get("session_id", ""), meta=d.get("meta", {}))
        for row in d.get("turns", []):
            s.turns.append(Turn(
                role=row.get("role", "user"), text=row.get("text", ""),
                stage=row.get("stage"), level=row.get("level"),
                tool_calls=row.get("tool_calls", []),
                ts=row.get("ts", time.time())))
        return s


def _shrink(args: dict, maxlen: int = 120) -> dict:
    """截断超长参数值,避免 trace 膨胀。"""
    out = {}
    for k, v in (args or {}).items():
        s = repr(v)
        out[k] = s if len(s) <= maxlen else s[:maxlen] + "…"
    return out
