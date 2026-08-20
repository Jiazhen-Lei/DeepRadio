"""knowledge:领域知识层——配方库 / 块语义(与 tools/knowledge_tools 分工)。

- ``tools/knowledge_tools.py`` 是\"可被 LLM 调用的查询动作\"(search_blocks 等);
- ``knowledge/recipes.py`` 是\"结构化的通信任务模板\",供 skills 层离线编排,
  即使无 LLM 也能把一句意图落成一张可跑的流图(论文 baseline / 兜底路径)。

子模块按需导入。
"""

from __future__ import annotations


def __getattr__(name):
    if name in ("Recipe", "RECIPES", "match_recipe", "list_recipes",
                "get_recipe"):
        from . import recipes
        return getattr(recipes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
