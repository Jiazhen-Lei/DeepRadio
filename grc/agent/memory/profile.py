"""profile.py:用户专业度画像(创新 B 的数据核心)。

DeepRadio 用同一 LLM 后端对三档用户自适应表达:

    - ``novice``   小白:重"是什么/为什么",少术语,给类比,回避参数细节
    - ``student``  学生:重"原理+参数含义",适度术语,给公式与直觉
    - ``expert``   专家:重"权衡/边界",高密度术语,只讲关键决策与陷阱

档位不是硬开关,而是可被单轮对话"信号"上下调的软状态:模型据此
渲染 system prompt 的 STYLE 段(见 ``UserProfile.style_prompt``),
并影响 planner 的协商话术粒度。

设计取向:纯启发式 + 可持久化,零外部依赖,离线可复现,便于论文消融。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

Level = str  # "novice" | "student" | "expert"

LEVELS: Tuple[Level, ...] = ("novice", "student", "expert")

#: 每档的表达风格约束,注入 system prompt。key 与 LEVELS 对齐。
STYLE_GUIDE: Dict[Level, str] = {
    "novice": (
        "面向零基础用户。用生活化类比解释概念,避免堆术语;"
        "凡涉及参数,先说它\"直观上控制什么\",再给一个安全默认值,"
        "不要求用户理解公式。每次只推进一小步,多确认。"
    ),
    "student": (
        "面向通信/DSP 在学学生。讲清\"原理 + 参数物理含义 + 直觉\","
        "可用适度术语与关键公式(如 EVM、Eb/N0),并解释参数如何影响指标;"
        "鼓励其自己推断取值,再给建议区间。"
    ),
    "expert": (
        "面向资深工程师。高信息密度,直接讨论设计权衡、边界条件与常见陷阱;"
        "省略基础解释,只点关键决策(如成形滚降、同步环路带宽、信道模型选择);"
        "默认用户能读 GRC/YAML 与指标曲线。"
    ),
}

# ---------------------------------------------------------------------------
# 专业度信号:从单轮用户话语里抽取"上调/下调"证据
# ---------------------------------------------------------------------------

#: 命中即视为"更专业"的关键词(术语密度高)。
_EXPERT_TERMS = [
    "evm", "ber", "eb/n0", "ebn0", "es/n0", "snr", "papr", "rrc",
    "根升余弦", "升余弦", "滚降", "成形", "同步", "载波恢复", "定时恢复",
    "环路带宽", "costas", "gardner", "muller", "均衡", "信道估计",
    "floquet", "星座", "相位噪声", "iq 不平衡", "群时延", "眼图",
    "过采样", "抽取", "内插", "matched filter", "匹配滤波",
]

#: 命中即视为"更小白"的关键词(求最基础解释)。
_NOVICE_TERMS = [
    "是什么", "什么意思", "不懂", "不太懂", "没学过", "小白", "入门",
    "第一次", "怎么开始", "能不能简单", "通俗", "别太专业", "听不懂",
    "新手", "扫盲", "科普",
]

#: 学生档的中间信号(想懂原理但非纯小白)。
_STUDENT_TERMS = [
    "原理", "为什么", "推导", "公式", "作业", "课程", "老师", "考试",
    "怎么算", "如何影响", "物理意义", "含义",
]


@dataclass
class ProfileSignals:
    """单轮话语的专业度证据(用于解释/调试,可入 trace)。"""

    expert_hits: List[str] = field(default_factory=list)
    student_hits: List[str] = field(default_factory=list)
    novice_hits: List[str] = field(default_factory=list)

    @property
    def net(self) -> int:
        """净分:>0 偏专家,<0 偏小白,=0 中性。"""
        return len(self.expert_hits) - len(self.novice_hits)

    def as_dict(self) -> dict:
        return asdict(self)


def infer_level_signals(text: str) -> ProfileSignals:
    """从一句用户输入里抽取专业度信号(不改状态,纯函数)。"""
    low = (text or "").lower()
    sig = ProfileSignals()
    for t in _EXPERT_TERMS:
        if t in low:
            sig.expert_hits.append(t)
    for t in _STUDENT_TERMS:
        if t in low:
            sig.student_hits.append(t)
    for t in _NOVICE_TERMS:
        if t in low:
            sig.novice_hits.append(t)
    return sig


# ---------------------------------------------------------------------------
# 用户画像:软状态 + 平滑更新 + 持久化
# ---------------------------------------------------------------------------
_LEVEL_SCORE: Dict[Level, float] = {"novice": -1.0, "student": 0.0, "expert": 1.0}


def _score_to_level(score: float) -> Level:
    if score <= -0.34:
        return "novice"
    if score >= 0.34:
        return "expert"
    return "student"


@dataclass
class UserProfile:
    """三档软画像。

    ``score`` 是 [-1, 1] 连续量,``level`` 是它的离散读数。每轮用
    :meth:`observe` 喂入用户话语,以 EMA 平滑更新,避免单句抖动导致档位跳变。
    """

    score: float = 0.0
    turns: int = 0
    #: EMA 平滑系数;越大越"跟手"(听信最新一句),越小越稳。
    alpha: float = 0.35
    #: 用户显式设定的档位(优先级最高,observe 不再覆盖)。
    pinned: Optional[Level] = None
    history: List[dict] = field(default_factory=list)

    # -- 读数 --------------------------------------------------------------
    @property
    def level(self) -> Level:
        if self.pinned in LEVELS:
            return self.pinned  # type: ignore[return-value]
        return _score_to_level(self.score)

    def style_prompt(self) -> str:
        """当前档位的表达风格约束(注入 system prompt 的 STYLE 段)。"""
        return STYLE_GUIDE[self.level]

    # -- 写入 --------------------------------------------------------------
    def pin(self, level: Level) -> "UserProfile":
        """显式钉住档位(如用户说\"我是专家\"/\"请通俗点\")。"""
        if level in LEVELS:
            self.pinned = level
        return self

    def unpin(self) -> "UserProfile":
        self.pinned = None
        return self

    def observe(self, text: str) -> "UserProfile":
        """根据一句用户输入平滑更新档位。返回 self 便于链式调用。"""
        self.turns += 1
        # 显式指令优先:直接钉档
        explicit = _detect_explicit_level(text)
        if explicit:
            self.pin(explicit)
        sig = infer_level_signals(text)
        if self.pinned is None and (sig.net != 0 or sig.student_hits):
            # 把净分归一到 [-1,1]:每 2 个净命中拉满
            target = max(-1.0, min(1.0, sig.net / 2.0))
            if sig.net == 0 and sig.student_hits:
                target = 0.0  # 明确的"学生向"信号把画像往中间拉
            self.score = (1 - self.alpha) * self.score + self.alpha * target
        self.history.append(
            {"text": (text or "")[:80], "signals": sig.as_dict(),
             "score": round(self.score, 3), "level": self.level,
             "pinned": self.pinned})
        return self

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"score": self.score, "turns": self.turns,
                "alpha": self.alpha, "pinned": self.pinned}

    @classmethod
    def from_dict(cls, d: dict) -> "UserProfile":
        return cls(score=float(d.get("score", 0.0)),
                   turns=int(d.get("turns", 0)),
                   alpha=float(d.get("alpha", 0.35)),
                   pinned=d.get("pinned"))

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "UserProfile":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return cls()


#: 显式档位指令的正则(命中优先级高于统计信号)。
_EXPLICIT_PATTERNS: List[Tuple[Level, re.Pattern]] = [
    ("expert", re.compile(
        r"我是(专家|工程师|老手|资深)|别(太)?啰嗦|直接(说|讲)重点|"
        r"专业(点|一点)|高阶|advanced|expert")),
    ("novice", re.compile(
        r"我是(小白|新手|零基础)|通俗(点|一点)|简单(点|一点)|别太专业|"
        r"讲(基础|基本)|科普|入门|beginner|novice|听不懂")),
    ("student", re.compile(
        r"我是(学生|在学|本科|研究生)|讲(讲)?原理|想(搞)?懂原理|student")),
]


def _detect_explicit_level(text: str) -> Optional[Level]:
    low = (text or "").lower()
    for level, pat in _EXPLICIT_PATTERNS:
        if pat.search(low):
            return level
    return None
