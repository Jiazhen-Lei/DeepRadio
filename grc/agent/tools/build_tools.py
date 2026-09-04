"""Deterministic GNU Radio flowgraph construction tools."""

from __future__ import annotations

import ast
import os
from typing import Optional

from .. import env
from .registry import ToolContext, tool


def _missing_literal_file_source(key: str, params: dict) -> str:
    if key != "blocks_file_source" or "file" not in params:
        return ""
    raw = str(params.get("file") or "").strip()
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        value = raw if os.path.isabs(raw) else ""
    path = str(value) if isinstance(value, str) else ""
    return path if path and os.path.isabs(path) and not os.path.isfile(path) else ""


@tool(
    name="init_flow_graph",
    description="Create an empty GNU Radio flowgraph.",
    parameters={
        "type": "object",
        "properties": {
            "flowgraph_id": {"type": "string"},
            "generate_options": {"type": "string"},
        },
        "required": ["flowgraph_id"],
    },
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    permission="project.write",
)
def init_flow_graph(
    ctx: ToolContext, flowgraph_id: str, generate_options: str = "no_gui"
):
    if ctx.platform is None:
        return {"ok": False, "error": "Platform is missing"}
    flow_graph = ctx.platform.make_flow_graph()
    env.configure_options(
        flow_graph, "python", generate_options, flowgraph_id=flowgraph_id
    )
    ctx.flow_graph = flow_graph
    ctx.blocks = {}
    return {
        "ok": True,
        "flowgraph_id": flowgraph_id,
        "generate_options": generate_options,
    }


@tool(
    name="add_block",
    description="Add one block to the current flowgraph.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "id": {"type": "string"},
            "params": {"type": "object"},
        },
        "required": ["key", "id"],
    },
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    permission="project.write",
)
def add_block(
    ctx: ToolContext, key: str, id: str, params: Optional[dict] = None
):
    missing = _missing_literal_file_source(key, params or {})
    if missing:
        return {"ok": False, "error": f"File Source input does not exist: {missing}"}
    if id in ctx.blocks:
        return {"ok": False, "error": f"Block ID already exists: {id}"}
    flow_graph = ctx.ensure_flow_graph()
    block = flow_graph.new_block(key)
    if block is None:
        return {"ok": False, "error": f"Block does not exist: {key}"}
    block.params["id"].set_value(id)
    unknown = []
    for name, value in (params or {}).items():
        if name in block.params:
            block.params[name].set_value(str(value))
        else:
            unknown.append(name)
    index = len(ctx.blocks)
    block.states["coordinate"] = (
        120 + (index % 4) * 230,
        140 + (index // 4) * 170,
    )
    ctx.blocks[id] = block
    result = {"ok": True, "id": id, "key": key}
    if unknown:
        result["warning"] = f"Ignored unknown parameters: {unknown}"
    return result


@tool(
    name="set_param",
    description="Update one parameter on an existing block.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "value": {},
        },
        "required": ["id", "name", "value"],
    },
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    permission="project.write",
)
def set_param(ctx: ToolContext, id: str, name: str, value):
    block = ctx.blocks.get(id)
    if block is None:
        return {
            "ok": False,
            "error": f"Block ID does not exist: {id}",
            "known": list(ctx.blocks),
        }
    if name not in block.params:
        return {
            "ok": False,
            "error": f"Block {id} has no parameter named {name}",
            "available": list(block.params),
        }
    missing = _missing_literal_file_source(
        str(getattr(block, "key", "") or ""), {name: value}
    )
    if missing:
        return {"ok": False, "error": f"File Source input does not exist: {missing}"}
    block.params[name].set_value(str(value))
    return {"ok": True, "id": id, "name": name, "value": value}


@tool(
    name="connect",
    description="Connect a source port to a destination port.",
    parameters={
        "type": "object",
        "properties": {
            "src_id": {"type": "string"},
            "src_port": {"type": "integer"},
            "dst_id": {"type": "string"},
            "dst_port": {"type": "integer"},
        },
        "required": ["src_id", "dst_id"],
    },
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    permission="project.write",
)
def connect(
    ctx: ToolContext,
    src_id: str,
    dst_id: str,
    src_port: int = 0,
    dst_port: int = 0,
):
    flow_graph = ctx.flow_graph
    if flow_graph is None:
        return {"ok": False, "error": "The flowgraph has not been created"}
    source = ctx.blocks.get(src_id)
    destination = ctx.blocks.get(dst_id)
    if source is None or destination is None:
        return {
            "ok": False,
            "error": "The source or destination ID does not exist",
            "known": list(ctx.blocks),
        }
    flow_graph.rewrite()
    try:
        source_endpoint = source.sources[src_port]
        destination_endpoint = destination.sinks[dst_port]
    except IndexError as exc:
        return {
            "ok": False,
            "error": f"Port index out of range: {exc}",
            "src_sources": len(source.sources),
            "dst_sinks": len(destination.sinks),
        }
    try:
        flow_graph.connect(source_endpoint, destination_endpoint)
        flow_graph.rewrite()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Connection failed: {exc}"}
    return {
        "ok": True,
        "connection": f"{src_id}[{src_port}] -> {dst_id}[{dst_port}]",
    }


@tool(
    name="render_grc",
    description="Save the current flowgraph as a .grc file.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
    },
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    permission="project.write",
)
def render_grc(ctx: ToolContext, path: str = ""):
    flow_graph = ctx.flow_graph
    if flow_graph is None:
        return {"ok": False, "error": "The flowgraph has not been created"}
    flow_graph.rewrite()
    if not path:
        flowgraph_id = flow_graph.get_option("id") or "flow_graph"
        out_dir = ctx.out_dir or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{flowgraph_id}.grc")
    try:
        ctx.platform.save_flow_graph(path, flow_graph)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to save the flowgraph: {exc}"}
    return {"ok": True, "path": path}


@tool(
    name="inspect_flowgraph",
    description="Return a compact, read-only summary of the current flowgraph, project path, parameters, ports, connections, and version.",
    parameters={"type": "object", "properties": {}},
    group="build",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
)
def inspect_flowgraph(ctx: ToolContext):
    flow_graph = ctx.flow_graph
    if flow_graph is None:
        return {"ok": False, "error": "The current session has no loaded flowgraph"}
    flow_graph.rewrite()
    blocks = []
    for block in flow_graph.blocks:
        if block is flow_graph.options_block:
            continue
        params = {
            name: str(param.get_value())
            for name, param in (getattr(block, "params", None) or {}).items()
        }
        blocks.append({
            "id": str(getattr(block, "name", "") or params.get("id") or ""),
            "key": str(getattr(block, "key", "") or ""),
            "params": params,
            "sources": len(getattr(block, "sources", []) or []),
            "sinks": len(getattr(block, "sinks", []) or []),
        })
    connections = [
        {
            "src_id": str(connection.source_block.name),
            "src_port": str(connection.source_port.key),
            "dst_id": str(connection.sink_block.name),
            "dst_port": str(connection.sink_port.key),
        }
        for connection in flow_graph.connections
    ]
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    return {
        "ok": True,
        "path": str(getattr(project, "grc_path", "") or ""),
        "project_version": int(getattr(project, "flowgraph_version", 0)),
        "blocks": blocks,
        "connections": connections,
    }
