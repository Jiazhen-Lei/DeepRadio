"""GRC Agent —— 自动流图设计能力。

本包在 GRC 之上增加一层"意图 -> 流图"的能力，不修改 grc/core。
子模块规划:
    env        环境引导(混搭 conda 运行时时的桥接)
    knowledge  块库索引与检索
    planner    LLM 规划器
    builder    结构化图谱 -> FlowGraph
    critic     DSP 规则检查器
    layout     自动布局
"""

__all__ = ["env"]
