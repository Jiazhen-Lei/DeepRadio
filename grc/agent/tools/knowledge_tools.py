"""知识类工具:检索/描述块、列举示例。

落到现有接口:遍历 ``platform.blocks``(env 已加载 580+ 块)读元数据。
"""

from __future__ import annotations

import os

from .registry import ToolContext, tool


def _block_text(block) -> str:
    """把一个块的可搜索文本拼起来(label + key + 分类 + 文档)。"""
    parts = [
        getattr(block, "label", "") or "",
        getattr(block, "key", "") or "",
        " ".join(getattr(block, "category", []) or []),
        getattr(block, "documentation", {}).get("", "")
        if isinstance(getattr(block, "documentation", None), dict) else "",
    ]
    return " ".join(p for p in parts if p).lower()


@tool(
    name="search_blocks",
    description="按关键词/语义检索可用的 GNU Radio 块,返回匹配的块 key 与标签。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词,如 'bpsk 调制' / 'awgn 信道'"},
            "limit": {"type": "integer", "description": "返回条数上限,默认 15"},
        },
        "required": ["query"],
    },
    group="knowledge",
)
def search_blocks(ctx: ToolContext, query: str, limit: int = 15):
    if ctx.platform is None:
        return {"ok": False, "error": "缺少 platform"}
    q = (query or "").lower().strip()
    terms = [t for t in q.replace("/", " ").split() if t]
    scored = []
    for key, block in ctx.platform.blocks.items():
        text = _block_text(block)
        if not text:
            continue
        score = sum(text.count(t) for t in terms)
        # key 直接命中给高权重
        if q and q in key.lower():
            score += 10
        if score > 0:
            scored.append((score, key, getattr(block, "label", key)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    hits = [{"key": k, "label": lab} for _s, k, lab in scored[:limit]]
    return {"ok": True, "count": len(hits), "blocks": hits}


@tool(
    name="describe_block",
    description="返回某个块的参数列表、输入/输出端口与用途,用于决定如何配置它。",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "块的类型 key,如 'digital_constellation_modulator'"},
        },
        "required": ["key"],
    },
    group="knowledge",
)
def describe_block(ctx: ToolContext, key: str):
    if ctx.platform is None:
        return {"ok": False, "error": "缺少 platform"}
    block_cls = ctx.platform.blocks.get(key)
    if block_cls is None:
        return {"ok": False, "error": f"块不存在: {key}"}
    # 需要实例化到 flow_graph 才能读到 params;用临时 fg 探测
    fg = ctx.platform.make_flow_graph()
    inst = fg.new_block(key)
    if inst is None:
        return {"ok": False, "error": f"无法实例化块: {key}"}
    params = []
    for pname, p in inst.params.items():
        params.append({
            "name": pname,
            "label": getattr(p, "name", pname),
            "default": str(getattr(p, "default", "")),
            "dtype": getattr(p, "dtype", ""),
        })
    return {
        "ok": True,
        "key": key,
        "label": getattr(inst, "label", key),
        "category": getattr(inst, "category", []),
        "params": params,
        "num_sinks_hint": len(getattr(inst, "sinks", [])),
        "num_sources_hint": len(getattr(inst, "sources", [])),
    }


@tool(
    name="list_examples",
    description="列举可参考的 .grc 样例/配方,用于借鉴一条完整链路的搭法。",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "可选,按文件名过滤,如 'bpsk'"},
        },
    },
    group="knowledge",
)
def list_examples(ctx: ToolContext, keyword: str = ""):
    # Manual regressions live under dev_docs/regression, not a live agent layer.
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, os.pardir, os.pardir, os.pardir))
    ex_dir = os.path.join(repo, "dev_docs", "regression")
    kw = (keyword or "").lower().strip()
    files = []
    if os.path.isdir(ex_dir):
        for fn in sorted(os.listdir(ex_dir)):
            if fn.endswith((".grc", ".py")) and (not kw or kw in fn.lower()):
                files.append(fn)
    from ..knowledge import recipes as _recipes

    for recipe in _recipes.list_recipes():
        name = str(recipe.get("name") or "")
        if name and (not kw or kw in name.lower()):
            files.append(f"recipe:{name}")
    return {"ok": True, "count": len(files), "dir": ex_dir, "examples": files}
