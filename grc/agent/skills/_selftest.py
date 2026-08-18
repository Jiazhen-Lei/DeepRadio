"""skills 层离线自检:不依赖 LLM,验证 memory/knowledge/skills 端到端。

运行::

    PYTHONPATH=$PWD python -m grc.agent.skills._selftest

覆盖:
    A. profile 三档推断(显式钉档 + 信号平滑)
    B. recipes 选型 + 元信息
    C. design_link 真实建图 + critic 校验 + 仿真取 EVM + 画星座(走真实 tools)
    D. debug_by_metric 用 EVM 给分档诊断与改参建议
    E. explain_block 分档解说
    F. adapt_expertise 三档重渲染(同一诊断结果,三种表达)
"""

from __future__ import annotations

import os
import tempfile


def _selftest() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING)

    from .. import env
    from ..memory.profile import UserProfile
    from ..knowledge import recipes
    from ..tools.registry import ToolContext
    from .adapt_expertise import adapt_expertise
    from .debug_by_metric import debug_by_metric
    from .design_link import design_link
    from .explain_block import explain_block

    ok_all = True

    # ---- A. profile 三档推断 -------------------------------------------
    print("=== A. profile 专业度推断 ===")
    p = UserProfile()
    p.observe("这是什么啊,我是小白完全不懂")
    print(f"  小白话术 -> {p.level} (score={p.score:.2f})")
    a1 = p.level == "novice"

    p2 = UserProfile()
    for t in ["帮我看下 EVM 和 RRC 滚降", "同步环路带宽怎么定", "IQ 不平衡影响大吗"]:
        p2.observe(t)
    print(f"  专家话术 -> {p2.level} (score={p2.score:.2f})")
    a2 = p2.level == "expert"

    p3 = UserProfile().pin("student")
    print(f"  显式钉档 -> {p3.level}")
    a3 = p3.level == "student"
    print("  A:", "PASS" if (a1 and a2 and a3) else "FAIL")
    ok_all &= a1 and a2 and a3

    # ---- B. recipes 选型 -----------------------------------------------
    print("\n=== B. recipes 选型 ===")
    r_bpsk = recipes.match_recipe("用 BPSK 过 AWGN 看星座")
    r_ofdm = recipes.match_recipe("我要做 OFDM 多载波")
    r_tone = recipes.match_recipe("最简单的正弦加噪声入门")
    print(f"  'BPSK/AWGN' -> {r_bpsk.name}")
    print(f"  'OFDM'      -> {r_ofdm.name}")
    print(f"  '正弦入门'  -> {r_tone.name}")
    b = (r_bpsk.name == "bpsk_awgn" and r_ofdm.name == "ofdm_awgn"
         and r_tone.name == "tone_noise")
    print(f"  可用配方 {len(recipes.RECIPES)} 个;B:", "PASS" if b else "FAIL")
    ok_all &= b

    # ---- C. design_link 端到端建图 + 仿真 ------------------------------
    print("\n=== C. design_link 建图 + 仿真闭环 ===")
    platform = env.make_platform()
    out_dir = tempfile.mkdtemp(prefix="skills_")
    ctx = ToolContext(platform=platform, out_dir=out_dir)
    prof_student = UserProfile().pin("student")

    d = design_link(ctx, profile=prof_student,
                    intent="用 BPSK 过 AWGN 看星座图", simulate=True)
    print(f"  配方={d['recipe']} valid={d['valid']} blocks={d['num_blocks']}")
    print(f"  EVM={d['metrics'].get('evm_pct')}")
    print(f"  产物: {list(d['artifacts'])}")
    print(f"  解说: {d['narrative'][:60]}...")
    c_ok = (d["ok"] and d["valid"]
            and d["metrics"].get("evm_pct") is not None
            and "grc_path" in d["artifacts"])
    print("  C:", "PASS" if c_ok else "FAIL")
    ok_all &= c_ok

    # ---- D. debug_by_metric 诊断 ---------------------------------------
    print("\n=== D. debug_by_metric 指标诊断 ===")
    diag = debug_by_metric(ctx, profile=prof_student, metric="evm",
                           modulation="bpsk", sps=4)
    print(f"  {diag.get('metric')}={diag.get('value'):.2f}% "
          f"-> {diag.get('verdict')}")
    print(f"  建议数={len(diag.get('suggestions', []))}")
    print(f"  解说: {diag.get('narrative', '')[:60]}...")
    d_ok = diag.get("ok") and diag.get("value") is not None
    print("  D:", "PASS" if d_ok else "FAIL")
    ok_all &= d_ok

    # ---- E. explain_block 分档解说 -------------------------------------
    print("\n=== E. explain_block 块解说 ===")
    eb = explain_block(ctx, profile=UserProfile().pin("novice"),
                       key="channels_channel_model")
    print(f"  小白: {eb.get('narrative', '')[:70]}")
    eb2 = explain_block(ctx, profile=UserProfile().pin("expert"),
                        key="channels_channel_model")
    print(f"  专家: {eb2.get('narrative', '')[:70]}")
    e_ok = eb.get("ok") and eb2.get("ok") and eb["narrative"] != eb2["narrative"]
    print("  E:", "PASS" if e_ok else "FAIL")
    ok_all &= e_ok

    # ---- F. adapt_expertise 三档重渲染 ---------------------------------
    print("\n=== F. adapt_expertise 三档表达 ===")
    diag_for_render = {"metric": "EVM", "value": 22.0,
                       "verdict": "EVM 偏高,判决容易出错,需排查噪声/频偏/同步。",
                       "suggestions": [
                           {"knob": "chan.noise_voltage", "dir": "↓",
                            "say_novice": "信号有点乱,先把杂音调小很多。",
                            "say_student": "先大幅降低 chan.noise_voltage。"}]}
    from .narrate import narrate_debug
    outs = {}
    for lvl in ("novice", "student", "expert"):
        outs[lvl] = narrate_debug(diag_for_render, UserProfile().pin(lvl))
        print(f"  [{lvl:7}] {outs[lvl][:64]}")
    f_ok = len(set(outs.values())) == 3  # 三档表达互不相同
    # 顺带验证 adapt_expertise 迁移检测
    ad = adapt_expertise(text="我是专家,别啰嗦直接给参数",
                         profile=UserProfile())
    print(f"  迁移: changed={ad['changed']} -> {ad['level']} "
          f"note={ad['note'][:30]}")
    f_ok = f_ok and ad["ok"] and ad["level"] == "expert"
    print("  F:", "PASS" if f_ok else "FAIL")
    ok_all &= f_ok

    print("\n总自检:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
