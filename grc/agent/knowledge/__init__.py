"""Legacy Flowgraph recipe helpers; SpecAgent knowledge lives in its Skill."""

from __future__ import annotations


def __getattr__(name):
    if name in (
        "Recipe", "RECIPES", "match_recipe", "list_recipes", "get_recipe",
        "covering_recipe", "resolve_recipe", "guess_modulation",
        "render_recipe_index", "RECIPE_INDEX_PATH",
    ):
        from . import recipes

        return getattr(recipes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
