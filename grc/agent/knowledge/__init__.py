"""knowledge:领域知识层——配方库 / 块语义(与 tools/knowledge_tools 分工)。

- ``tools/knowledge_tools.py`` 是\"可被 LLM 调用的查询动作\"(search_blocks 等);
- ``knowledge/recipes.py`` 是\"结构化的通信任务模板\",供 design_link /
  WorkflowEngine 离线编排;``skills/grc-build/references/recipe_index.md``
  是它的只读副本,给 LLM skill 读。

子模块按需导入。
"""

from __future__ import annotations


def __getattr__(name):
    if name in ("Recipe", "RECIPES", "match_recipe", "list_recipes",
                "get_recipe", "covering_recipe", "resolve_recipe",
                "guess_modulation", "render_recipe_index", "RECIPE_INDEX_PATH"):
        from . import recipes
        return getattr(recipes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
