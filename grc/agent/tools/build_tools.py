"""建图类工具:增量式 add_block / set_param / connect / render_grc。

这是从"一次性直出 YAML"升级到"逐块可协商"的核心:每个工具就地修改
``ctx.flow_graph``,让 Agent/LLM 能一步步搭图、随时校验、局部改参。

接口全部取自 ``env`` 已验证的链路:
    fg.new_block(key) / block.params[name].set_value(v)
    fg.rewrite() -> fg.connect(src.sources[i], dst.sinks[j]) -> fg.rewrite()
    platform.save_flow_graph(path, fg)
"""

from __future__ import annotations

import os

from .. import env
from .registry import ToolContext, tool


@tool(
    name="init_flow_graph",
    description="新建一张空流图并设置输出方式(no_gui/qt_gui)。建图的第一步。",
    parameters={
        "type": "object",
        "properties": {
            "flowgraph_id": {"type": "string", "description": "流图 id(决定生成文件名),如 'bpsk_awgn'"},
            "generate_options": {"type": "string", "description": "'no_gui'(仿真)或 'qt_gui'(可视),默认 no_gui"},
        },
        "required": ["flowgraph_id"],
    },
    group="build",
)
def init_flow_graph(ctx: ToolContext, flowgraph_id: str,
                    generate_options: str = "no_gui"):
    if ctx.platform is None:
        return {"ok": False, "error": "缺少 platform"}
    fg = ctx.platform.make_flow_graph()
    env.configure_options(fg, "python", generate_options,
                          flowgraph_id=flowgraph_id)
    ctx.flow_graph = fg
    ctx.blocks = {}
    return {"ok": True, "flowgraph_id": flowgraph_id,
            "generate_options": generate_options}


@tool(
    name="add_block",
    description="往当前流图添加一个块并可选地设置参数。id 必须唯一。",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "块类型 key,如 'blocks_throttle'"},
            "id": {"type": "string", "description": "块实例 id(唯一),如 'throttle0'"},
            "params": {
                "type": "object",
                "description": "参数名->值(字符串),如 {'type':'complex','samp_rate':'samp_rate'}",
            },
        },
        "required": ["key", "id"],
    },
    group="build",
)
def add_block(ctx: ToolContext, key: str, id: str, params: dict = None):
    fg = ctx.ensure_flow_graph()
    block = fg.new_block(key)
    if block is None:
        return {"ok": False, "error": f"块不存在: {key}"}
    block.params["id"].set_value(id)
    unknown = []
    for name, value in (params or {}).items():
        if name in block.params:
            block.params[name].set_value(str(value))
        else:
            unknown.append(name)
    # 给个默认坐标,避免画布重叠
    idx = len(ctx.blocks)
    block.states["coordinate"] = (120 + (idx % 4) * 230, 140 + (idx // 4) * 170)
    ctx.blocks[id] = block
    out = {"ok": True, "id": id, "key": key}
    if unknown:
        out["warning"] = f"忽略未知参数: {unknown}; 可用: {list(block.params)}"
    return out


@tool(
    name="set_param",
    description="修改已存在块的某个参数(对话式调参用)。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "块实例 id"},
            "name": {"type": "string", "description": "参数名"},
            "value": {"type": "string", "description": "新值(字符串)"},
        },
        "required": ["id", "name", "value"],
    },
    group="build",
)
def set_param(ctx: ToolContext, id: str, name: str, value: str):
    block = ctx.blocks.get(id)
    if block is None:
        return {"ok": False, "error": f"块 id 不存在: {id}",
                "known": list(ctx.blocks)}
    if name not in block.params:
        return {"ok": False, "error": f"块 {id} 无参数 {name}",
                "available": list(block.params)}
    block.params[name].set_value(str(value))
    return {"ok": True, "id": id, "name": name, "value": value}


@tool(
    name="connect",
    description="连接两个块的端口(源块输出口 -> 目标块输入口)。连接前会自动 rewrite。",
    parameters={
        "type": "object",
        "properties": {
            "src_id": {"type": "string", "description": "源块 id"},
            "src_port": {"type": "integer", "description": "源块输出端口序号,默认 0"},
            "dst_id": {"type": "string", "description": "目标块 id"},
            "dst_port": {"type": "integer", "description": "目标块输入端口序号,默认 0"},
        },
        "required": ["src_id", "dst_id"],
    },
    group="build",
)
def connect(ctx: ToolContext, src_id: str, dst_id: str,
            src_port: int = 0, dst_port: int = 0):
    fg = ctx.flow_graph
    if fg is None:
        return {"ok": False, "error": "流图尚未创建"}
    src = ctx.blocks.get(src_id)
    dst = ctx.blocks.get(dst_id)
    if src is None or dst is None:
        return {"ok": False, "error": "src/dst id 不存在",
                "known": list(ctx.blocks)}
    fg.rewrite()
    try:
        srcp = src.sources[src_port]
        dstp = dst.sinks[dst_port]
    except IndexError as exc:
        return {"ok": False, "error": f"端口序号越界: {exc}",
                "src_sources": len(src.sources), "dst_sinks": len(dst.sinks)}
    try:
        fg.connect(srcp, dstp)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"连接失败: {exc}"}
    fg.rewrite()
    return {"ok": True,
            "connection": f"{src_id}[{src_port}] -> {dst_id}[{dst_port}]"}


@tool(
    name="render_grc",
    description="把当前流图存成 .grc 文件(供人工在 GRC 打开检查或载入画布)。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "可选,输出 .grc 路径;默认放 out_dir 下"},
        },
    },
    group="build",
)
def render_grc(ctx: ToolContext, path: str = ""):
    fg = ctx.flow_graph
    if fg is None:
        return {"ok": False, "error": "流图尚未创建"}
    fg.rewrite()
    if not path:
        fid = fg.get_option("id") or "flow_graph"
        out_dir = ctx.out_dir or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{fid}.grc")
    try:
        ctx.platform.save_flow_graph(path, fg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"存盘失败: {exc}"}
    return {"ok": True, "path": path}
