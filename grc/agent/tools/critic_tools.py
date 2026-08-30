"""校验类工具:validate_flowgraph / explain_error。

落到现有接口:fg.rewrite() / fg.validate() / fg.is_valid() /
fg.get_error_messages()。explain_error 把原始报错整理成可读要点,
供 debug skill 或直接回喂 LLM。
"""

from __future__ import annotations

from .registry import ToolContext, tool

# Disabled RF sinks look unconnected to GNU Radio. Topology checks may
# temporarily re-enable them; the saved graph stays unarmed.
_RF_IO = (
    "usrp_sink", "usrp_source", "pluto_sink", "pluto_source",
    "fmcomms", "osmosdr", "limesdr",
)


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


def _enable_disabled_rf(ctx: ToolContext):
    restored = []
    fg = ctx.flow_graph
    blocks = list(getattr(fg, "blocks", []) or []) if fg is not None else []
    if not blocks:
        blocks = list((ctx.blocks or {}).values())
    for block in blocks:
        key = str(getattr(block, "key", "") or "").lower()
        if getattr(block, "state", None) != "disabled":
            continue
        if any(hint in key for hint in _RF_IO):
            restored.append(block)
            block.state = "enabled"
    return restored


@tool(
    name="validate_flowgraph",
    description="Validate the current flowgraph (type consistency, port connectivity, and related checks) and return errors.",
    parameters={
        "type": "object",
        "properties": {
            "arm_disabled_rf": {
                "type": "boolean",
                "description": "Temporarily enable disabled RF endpoints for topology validation without changing saved state",
            },
        },
    },
    group="critic",
)
def validate_flowgraph(ctx: ToolContext, arm_disabled_rf: bool = False):
    fg = ctx.flow_graph
    if fg is None:
        return {"ok": False, "error": "The flowgraph has not been created"}
    restored = _enable_disabled_rf(ctx) if arm_disabled_rf else []
    try:
        valid, msgs = _collect_errors(fg)
    finally:
        for block in restored:
            block.state = "disabled"
    return {"ok": True, "valid": valid, "errors": msgs,
            "num_blocks": len(fg.blocks)}


#: 常见错误模式 -> 可读解释与修复建议(供 explain_error 快速匹配)
_ERROR_HINTS = (
    ("type", "Port data types do not match: verify that connected blocks use matching type parameters (complex/float/byte)."),
    ("port", "Port connection problem: verify source outputs, destination inputs, and port indices."),
    ("not connected", "An unconnected port exists: connect every required block input and output correctly."),
    ("throttle", "Rate limiting is missing: software-only simulation chains typically need blocks_throttle2, blocks_throttle, or blocks_head."),
    ("param", "Invalid parameter value: check for undefined variables or type mismatches."),
    ("id", "A block ID is duplicate or invalid: each block ID must be unique and syntactically valid."),
)


@tool(
    name="explain_error",
    description="Convert raw flowgraph validation errors into readable causes and repair suggestions.",
    parameters={
        "type": "object",
        "properties": {
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional error list; when omitted, rerun flowgraph validation to collect errors.",
            },
        },
    },
    group="critic",
)
def explain_error(ctx: ToolContext, errors: list = None):
    if errors is None:
        fg = ctx.flow_graph
        if fg is None:
            return {"ok": False, "error": "The flowgraph has not been created and no errors were provided"}
        _valid, errors = _collect_errors(fg)
    explained = []
    for err in errors or []:
        low = err.lower()
        hint = next((h for kw, h in _ERROR_HINTS if kw in low),
                    "Check parameters and connections against the block documentation.")
        explained.append({"error": err, "hint": hint})
    return {"ok": True, "count": len(explained), "explanations": explained}
