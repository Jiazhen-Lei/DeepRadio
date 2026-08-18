"""experiments:论文实验脚手架(消融 / 量化)。

目前一个入口:

    ablation  "自适应表达 vs 固定档位" + "经验复用" 两个消融, 用 memory.Session
              埋点、FlowGraphStore 验证复用, 全离线可复现(不需 LLM)。

放在 grc.agent 下, 与 skills/memory 同层; 产物(session json / trace)默认落到
临时目录, 也可用 --out 指定, 供后续画图。
"""

from __future__ import annotations


def __getattr__(name):
    if name in ("run_ablation", "AblationResult"):
        from . import ablation
        return getattr(ablation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
