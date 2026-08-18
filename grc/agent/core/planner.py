"""Planner:分层协商状态机(创新 A 的骨架)。

显式状态机,每个状态是一个**协商检查点**,人可确认/驳回/追问:

    INTENT -> PROPOSE -> BUILD -> SIMULATE -> TUNE -> DONE
       ↑______________ 任一层可回退/重协商 ______________↑

Planner 不含领域逻辑,只负责:
* 记录当前处于哪个阶段;
* 根据"用户是否确认"决定前进/回退/停留;
* 告诉 Agent 当前阶段该调哪一组工具(用 allowed_tool_groups 约束,
  实现"在意图层就只做意图澄清"的可控节奏)。

这个状态机本身就是论文的交互技术 progressive intent alignment,
每个 checkpoint 的 override 都是可测量的实验变量。
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional


class Stage(str, Enum):
    """协商阶段。"""

    INTENT = "intent"        # 意图复述与澄清
    PROPOSE = "propose"      # 给候选方案
    BUILD = "build"          # 增量建图
    SIMULATE = "simulate"    # 无头仿真
    TUNE = "tune"            # 看指标调参
    DONE = "done"            # 完成


#: 阶段推进顺序
_ORDER: List[Stage] = [
    Stage.INTENT, Stage.PROPOSE, Stage.BUILD,
    Stage.SIMULATE, Stage.TUNE, Stage.DONE,
]

#: 每个阶段允许 Agent 调用的工具分组(约束 ReAct 不越权乱调)。
#: macro 组是把 skills 编排能力暴露的"宏工具",在 BUILD/TUNE 放开,
#: 让 LLM 可以一步 design_link 建图、一步 debug_by_metric 诊断。
_ALLOWED_GROUPS = {
    Stage.INTENT: ["knowledge"],
    Stage.PROPOSE: ["knowledge"],
    Stage.BUILD: ["knowledge", "build", "critic", "macro"],
    Stage.SIMULATE: ["sim"],
    Stage.TUNE: ["build", "critic", "sim", "macro"],
    Stage.DONE: [],
}

#: 每个阶段是否为 checkpoint(需要用户确认才前进)
_CHECKPOINT = {
    Stage.INTENT: True,
    Stage.PROPOSE: True,
    Stage.BUILD: True,
    Stage.SIMULATE: True,
    Stage.TUNE: True,
    Stage.DONE: False,
}


class Planner:
    """分层协商状态机。"""

    def __init__(self, start: Stage = Stage.INTENT):
        self.stage: Stage = start

    # -- 查询 ---------------------------------------------------------------
    def allowed_tool_groups(self) -> List[str]:
        """当前阶段允许调用的工具分组。"""
        return list(_ALLOWED_GROUPS.get(self.stage, []))

    def is_checkpoint(self) -> bool:
        """当前阶段是否需要用户确认才能前进。"""
        return _CHECKPOINT.get(self.stage, False)

    def is_done(self) -> bool:
        return self.stage == Stage.DONE

    # -- 迁移 ---------------------------------------------------------------
    def advance(self) -> Stage:
        """确认后前进一个阶段。已在 DONE 则停留。"""
        idx = _ORDER.index(self.stage)
        if idx < len(_ORDER) - 1:
            self.stage = _ORDER[idx + 1]
        return self.stage

    def back(self, to: Optional[Stage] = None) -> Stage:
        """驳回/回退:回到指定阶段,或回退一个阶段。"""
        if to is not None:
            self.stage = to
        else:
            idx = _ORDER.index(self.stage)
            if idx > 0:
                self.stage = _ORDER[idx - 1]
        return self.stage

    def reset(self) -> Stage:
        self.stage = Stage.INTENT
        return self.stage

    # -- 意图判定(把用户回应归类为 确认/驳回/其它) -------------------------
    @staticmethod
    def classify_response(text: str) -> str:
        """粗粒度地把用户回应分类为 'confirm' / 'reject' / 'other'。

        真实系统里可交给 LLM 判定;这里给一个可离线跑的启发式兜底,
        便于骨架先跑通、也作为无 LLM 时的默认行为。
        """
        t = (text or "").strip().lower()
        confirm_kw = ("对", "是", "确认", "可以", "好的", "没问题", "继续",
                      "ok", "yes", "correct", "go", "proceed", "对的")
        reject_kw = ("不对", "不是", "错", "重来", "回退", "改", "换",
                     "no", "wrong", "back", "redo")
        if any(k in t for k in reject_kw):
            return "reject"
        if any(k in t for k in confirm_kw):
            return "confirm"
        return "other"
