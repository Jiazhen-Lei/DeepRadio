"""gen_recipe_index:由 knowledge.recipes 生成 recipe_index.md。

保证 SKILL references 里的配方索引与代码里的确定性配方**单一事实源一致**。
用法(项目根目录,已能 import grc)::

    python -m grc.agent.skills.grc_build.gen_recipe_index   # 见下方 main 用法说明

或直接运行本文件(需保证 grc 包可导入):

    PYTHONPATH=<gnuradio_src> python grc/agent/skills/grc-build/references/gen_recipe_index.py

它会把最新配方索引写回同目录的 recipe_index.md。
"""

from __future__ import annotations

import os
import sys


def _ensure_import_path() -> None:
    # 本文件: grc/agent/skills/grc-build/references/gen_recipe_index.py
    # 到项目根需上溯 6 级(references->grc-build->skills->agent->grc->根)
    here = os.path.abspath(__file__)
    root = here
    for _ in range(6):
        root = os.path.dirname(root)
    if root not in sys.path:
        sys.path.insert(0, root)


def render_index() -> str:
    _ensure_import_path()
    from grc.agent.knowledge import recipes as R

    lines = ["# 配方索引(由 knowledge/recipes.py 自动生成,勿手改)\n",
             "选型:按意图关键词命中数挑最合适配方(match_recipe);全不中回落 bpsk_awgn。\n"]
    for r in R.RECIPES.values():
        lines.append(f"## {r.name}  ({r.difficulty})")
        lines.append(f"- 标题: {r.title}")
        lines.append(f"- 摘要: {r.summary}")
        lines.append(f"- 关键词: {', '.join(r.keywords)}")
        lines.append(f"- 指标: {', '.join(r.metrics)}")
        lines.append(f"- 块数: {len(r.blocks)}")
        if r.knobs:
            lines.append("- 可调旋钮:")
            for k, v in r.knobs.items():
                lines.append(f"  - `{k}`: {v}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    text = render_index()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "recipe_index.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"已写入 {out} ({len(text)} 字符)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
