"""校验类工具:validate_flowgraph / explain_error。

落到现有接口:fg.rewrite() / fg.validate() / fg.is_valid() /
fg.get_error_messages()。explain_error 把原始报错整理成可读要点,
供 debug skill 或直接回喂 LLM。
"""

from __future__ import annotations

from .registry import ToolContext, tool


def _collect_errors(fg):
    """跑一遍校验,返回 (is_valid, [错误要点])。"""
    fg.rewrite()
    fg.validate()
    valid = fg.is_valid()
    msgs = []
    if not valid:
        try:
            for m in fg.get_error_messages():
                line = m.strip().splitlines()[-1].strip()
                if line:
                    msgs.append(line)
        except Exception:  # noqa: BLE001
            pass
    return valid, msgs


@tool(
    name="validate_flowgraph",
    description="校验当前流图是否合法(类型一致/端口连通等),返回是否通过与错误列表。",
    parameters={"type": "object", "properties": {}},
    group="critic",
)
def validate_flowgraph(ctx: ToolContext):
    fg = ctx.flow_graph
    if fg is None:
        return {"ok": False, "error": "流图尚未创建"}
    valid, msgs = _collect_errors(fg)
    return {"ok": True, "valid": valid, "errors": msgs,
            "num_blocks": len(fg.blocks)}


#: 常见错误模式 -> 可读解释与修复建议(供 explain_error 快速匹配)
_ERROR_HINTS = (
    ("type", "端口数据类型不一致:检查相连块的 type 参数(complex/float/byte)是否一致。"),
    ("port", "端口连接问题:确认源块有输出口、目标块有输入口,且端口序号未越界。"),
    ("not connected", "存在未连接的端口:每个块的输入/输出应被正确连线。"),
    ("throttle", "缺少限速:纯软件仿真链路通常需要 blocks_throttle 或 blocks_head 限速。"),
    ("param", "参数取值非法:检查该参数是否引用了未定义的变量或类型不匹配。"),
    ("id", "块 id 重复或非法:每个块 id 必须唯一且为合法标识符。"),
)


@tool(
    name="explain_error",
    description="把流图校验的原始报错整理成可读的原因与修复建议。",
    parameters={
        "type": "object",
        "properties": {
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选,错误文本列表;不传则自动重新校验当前流图取错误。",
            },
        },
    },
    group="critic",
)
def explain_error(ctx: ToolContext, errors: list = None):
    if errors is None:
        fg = ctx.flow_graph
        if fg is None:
            return {"ok": False, "error": "流图尚未创建且未提供 errors"}
        _valid, errors = _collect_errors(fg)
    explained = []
    for err in errors or []:
        low = err.lower()
        hint = next((h for kw, h in _ERROR_HINTS if kw in low),
                    "请对照块文档检查参数与连接。")
        explained.append({"error": err, "hint": hint})
    return {"ok": True, "count": len(explained), "explanations": explained}
