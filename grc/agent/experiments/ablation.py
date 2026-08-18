"""ablation.py:DeepRadio 交互消融实验(离线可复现)。

围绕两个创新点做可量化对比,全部不依赖 LLM,用确定性 skills/memory 路径:

E1  自适应表达 vs 固定档位(创新 B 核心消融)
    喂同一段"专业度逐渐上升"的用户话语, 分别在 adaptive=True/False 下跑,
    记录每轮画像档位轨迹 + system-prompt 风格段, 量化"是否跟随用户调档"。

E2  三档表达差异化
    对同一 design_link 结果, 在 novice/student/expert 三档各渲染一次 narrative,
    用词袋 Jaccard 距离量化三档表达是否真的不同(差异度越高越好)。

E3  经验复用(长期记忆支线)
    先 design_link 一次并 remember_flowgraph, 再用相似意图 recall,
    验证命中历史配方 -> 可省去重新选型的往返。

用法::

    PYTHONPATH=$PWD python -m grc.agent.experiments.ablation [--out DIR]

产物:每个实验的 Session json + 汇总指标打印到 stdout。
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..memory.profile import UserProfile
from ..memory.session import Session
from ..memory.store import FlowGraphStore

_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _bag(text: str) -> set:
    return set(_TOKEN.findall((text or "").lower()))


def _jaccard_dist(a: str, b: str) -> float:
    """1 - Jaccard 相似度:两段文本越不同, 值越接近 1。"""
    sa, sb = _bag(a), _bag(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return 1.0 - (inter / union if union else 0.0)


@dataclass
class AblationResult:
    """一次消融的结构化结果(便于测试断言 / 画图)。"""

    name: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    session_path: Optional[str] = None
    passed: bool = True
    note: str = ""


# ---------------------------------------------------------------------------
# E1:自适应 vs 固定档位
# ---------------------------------------------------------------------------
#: 一段"专业度逐渐上升"的对话:小白口吻 -> 学原理 -> 上术语 -> 纯专家决策。
#: 末两句连续给强专家信号, 用于展示 EMA 抗抖动下仍能稳步爬升到 expert。
_ESCALATING_DIALOG = [
    "我是新手, 第一次用, 能不能通俗点讲讲这是什么",
    "那这个调制到底是什么意思呀",
    "我想搞懂原理, 为什么要加噪声信道",
    "EVM 和 Eb/N0 是怎么算的, 物理意义是什么",
    "把 RRC 滚降和定时恢复的环路带宽调一下, 看星座收敛",
    "Costas 环带宽和 Gardner 定时恢复的过采样怎么权衡, 直接给参数",
    "匹配滤波后 EVM 还是偏高, 是不是信道估计和均衡该上, 给关键决策",
]


def run_e1_adaptive_vs_fixed(out_dir: str) -> List[AblationResult]:
    results: List[AblationResult] = []
    for adaptive in (True, False):
        tag = "adaptive" if adaptive else "fixed"
        prof = UserProfile()          # 两组都从 student(score=0) 起
        if not adaptive:
            prof.pin("student")        # 固定组钉在 student, observe 不改档
        sess = Session(session_id=f"e1-{tag}")
        sess.meta["adaptive"] = adaptive
        levels: List[str] = []
        for utext in _ESCALATING_DIALOG:
            if adaptive:
                prof.observe(utext)
            level = prof.level
            levels.append(level)
            sess.add_user(utext, level=level)
            sess.add_assistant(
                f"[{level}] " + prof.style_prompt()[:40] + "…", stage="intent")
        path = sess.save(os.path.join(out_dir, f"e1_{tag}.json"))
        # 量化档位轨迹:用 novice/student/expert -> 0/1/2 的序数看单调性。
        # 消融要证明的是"自适应组随专业度上升而单调上移, 且末档 > 首档",
        # 而非硬性要求在有限轮内触达 expert(画像刻意做了 EMA 抗抖动)。
        ord_map = {"novice": 0, "student": 1, "expert": 2}
        ords = [ord_map[l] for l in levels]
        distinct = len(set(levels))
        non_decreasing = all(b >= a for a, b in zip(ords, ords[1:]))
        climbed = ords[-1] > ords[0]
        if adaptive:
            passed = non_decreasing and climbed and distinct > 1
            note = "随专业度上升单调上移(non-decreasing 且末档>首档)"
        else:
            passed = distinct == 1
            note = "固定 student, 全程不跟随"
        results.append(AblationResult(
            name=f"E1/{tag}",
            metrics={"levels": levels, "ords": ords,
                     "distinct_levels": distinct,
                     "non_decreasing": non_decreasing, "climbed": climbed},
            session_path=path,
            passed=passed,
            note=note))
    return results


# ---------------------------------------------------------------------------
# E2:三档表达差异化(对同一 design_link 结果)
# ---------------------------------------------------------------------------
def run_e2_three_tier(platform, out_dir: str) -> AblationResult:
    from ..skills.design_link import design_link
    from ..tools.registry import ToolContext

    narratives: Dict[str, str] = {}
    for level in ("novice", "student", "expert"):
        prof = UserProfile().pin(level)
        ctx = ToolContext(platform=platform,
                          out_dir=os.path.join(out_dir, f"e2_{level}"))
        # 只建图不仿真, 保证快速且确定性(表达差异不依赖仿真数值)。
        r = design_link(ctx, prof, recipe="bpsk_awgn",
                        simulate=False, render=False)
        narratives[level] = r.get("narrative", "")

    d_ns = _jaccard_dist(narratives["novice"], narratives["student"])
    d_se = _jaccard_dist(narratives["student"], narratives["expert"])
    d_ne = _jaccard_dist(narratives["novice"], narratives["expert"])
    avg = round((d_ns + d_se + d_ne) / 3.0, 3)
    # 三档两两都应有实质差异(阈值 0.2 为经验值), 且专家 vs 小白差异最大。
    passed = min(d_ns, d_se, d_ne) > 0.2 and d_ne >= d_ns
    return AblationResult(
        name="E2/three_tier",
        metrics={"dist_novice_student": round(d_ns, 3),
                 "dist_student_expert": round(d_se, 3),
                 "dist_novice_expert": round(d_ne, 3),
                 "avg_distance": avg,
                 "lengths": {k: len(v) for k, v in narratives.items()}},
        passed=passed,
        note="三档 narrative 两两差异度(1-Jaccard), 越大越区分")


# ---------------------------------------------------------------------------
# E3:经验复用
# ---------------------------------------------------------------------------
def run_e3_reuse(platform, out_dir: str) -> AblationResult:
    from ..skills.design_link import design_link
    from ..tools.registry import ToolContext

    store = FlowGraphStore(path=os.path.join(out_dir, "store.jsonl"))
    # 首次:一句 BPSK 意图 -> 真建图 -> 记忆
    ctx = ToolContext(platform=platform, out_dir=os.path.join(out_dir, "e3"))
    intent1 = "用 BPSK 调制过 AWGN 信道, 看星座图和 EVM"
    r = design_link(ctx, UserProfile(), intent=intent1,
                   simulate=False, render=True)
    store.remember_flowgraph(
        intent1, recipe=r["recipe"],
        grc_path=r.get("artifacts", {}).get("grc_path"))

    # 再来一句真实用户"换句话说"的复述(重用 BPSK/AWGN/星座 等关键词)
    # -> 应召回同一 recipe, 省去重新选型。
    intent2 = "帮我再搭一个 BPSK 过 AWGN 信道的链路, 也要看星座图"
    recalled = store.best_recipe(intent2)
    hits = store.recall(intent2, kind="flowgraph", top_k=1)
    top_score = hits[0].score if hits else 0.0
    passed = (recalled == r["recipe"]) and top_score >= 0.15
    return AblationResult(
        name="E3/reuse",
        metrics={"stored_recipe": r["recipe"], "recalled_recipe": recalled,
                 "recall_score": round(top_score, 3), "store_size": len(store)},
        passed=passed,
        note="相似意图召回历史配方 -> 省去重新选型往返")


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def run_ablation(out_dir: Optional[str] = None,
                 with_build: bool = True) -> List[AblationResult]:
    """跑全部消融, 返回结构化结果列表。

    Args:
        out_dir: 产物目录; None 则用临时目录。
        with_build: 是否跑需要 platform 建图的 E2/E3(离线纯 memory 测试可关)。
    """
    out_dir = out_dir or tempfile.mkdtemp(prefix="deepradio_ablation_")
    os.makedirs(out_dir, exist_ok=True)

    results: List[AblationResult] = []
    results.extend(run_e1_adaptive_vs_fixed(out_dir))

    if with_build:
        from .. import env
        platform = env.make_platform()
        results.append(run_e2_three_tier(platform, out_dir))
        results.append(run_e3_reuse(platform, out_dir))

    return results


def _print_results(results: List[AblationResult]) -> int:
    print("\n================ DeepRadio 交互消融汇总 ================")
    all_ok = True
    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        all_ok = all_ok and r.passed
        print(f"\n[{flag}] {r.name}  — {r.note}")
        for k, v in r.metrics.items():
            print(f"    {k}: {v}")
        if r.session_path:
            print(f"    session: {r.session_path}")
    print("\n总结:", "全部 PASS" if all_ok else "存在 FAIL")
    return 0 if all_ok else 1


def _main() -> int:
    import logging

    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="DeepRadio 交互消融实验")
    parser.add_argument("--out", default=None, help="产物输出目录")
    parser.add_argument("--no-build", action="store_true",
                        help="跳过需要 platform 建图的 E2/E3")
    args = parser.parse_args()

    results = run_ablation(out_dir=args.out, with_build=not args.no_build)
    return _print_results(results)


if __name__ == "__main__":
    import sys

    sys.exit(_main())
