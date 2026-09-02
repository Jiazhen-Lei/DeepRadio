"""LangChain bridge over Registry tools.

Custom wrappers stay only where the LLM-facing name or observation folding
differs from ``registry.call``.  Everything else is generated from ToolSpec.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..tools.registry import ToolContext

logger = logging.getLogger(__name__)

_ARTIFACTS_NOTE = (
    "artifacts 里的路径位于宿主机磁盘,不在你的虚拟文件系统里。"
    "GUI 会自动展示这些产物,禁止用 read_file / ls / glob 去确认它们是否存在。"
)
_EVENT_KIND = {
    "validate_flowgraph": "validate",
    "run_simulation": "simulate",
}
_PLOT_ARTIFACTS = {
    "plot_spectrum": "spectrum_png",
    "plot_constellation": "constellation_png",
    "plot_eye": "eye_png",
}
_SKIP_AUTOWRAP = frozenset({"design_link", "read_metric"})
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def record_tool_event(
    ctx: ToolContext,
    kind: str,
    payload: Dict[str, Any],
    args: Dict[str, Any] | None = None,
) -> None:
    """Record a tool event and flush it to the session log immediately."""
    from ..tools import registry
    from . import session_store as store

    event = {
        "kind": kind,
        "origin": registry.origin_of(kind),
        "runtime": registry.runtime_of(kind),
        "args": dict(args or {}),
        "payload": payload,
    }
    ctx.extra.setdefault("events", []).append(event)
    session_id = ctx.extra.get("session_id")
    if not session_id:
        return
    try:
        store.append_session_event(session_id, "tool_called", {
            "tool": kind,
            "origin": event["origin"],
            "runtime": event["runtime"],
            "args": event["args"],
            "result": payload,
        })
        event["logged"] = True
    except Exception:  # noqa: BLE001
        logger.debug("tool_called 即时落盘失败", exc_info=True)


def _merge_artifacts(ctx: ToolContext, artifacts: Dict[str, Any]) -> None:
    store = ctx.extra.setdefault("artifacts", {})
    for key, value in (artifacts or {}).items():
        if value:
            store[key] = value


def _record_diagnosis_artifact(
    ctx: ToolContext, name: str, result: Dict[str, Any]
) -> None:
    if name not in {"debug_by_metric", "explain_error", "run_diagnosis_checks"}:
        return
    report_path = str(result.get("report_path") or "")
    if not report_path and result.get("ok") is not False:
        path = Path(ctx.out_dir or ".") / "diagnosis" / "diagnosis_report.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"tool": name, "result": result},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            report_path = str(path)
        except OSError:
            return
    if report_path:
        _merge_artifacts(ctx, {"diagnosis_report": report_path})


#: 幂等只读/纯计算工具:同一 Stage 内相同入参重复调用必然得到相同结果,
#: 直接回放首次结果并附提示,省掉一整轮"工具执行 + 大上下文 LLM"。
#: 实测(local/agent_sessions/gui-f8262d88)一次 BLE 部署里
#: ``build_ble_advertising_pdu`` 被连调 3 次、``generate_ble_1m_waveform`` 3 次,
#: 每次之间夹一轮 LLM,白烧上百秒。写操作(部署/启动/打补丁)不在此列。
_IDEMPOTENT_TOOLS = frozenset({
    "build_ble_advertising_pdu",
    "generate_ble_1m_waveform",
    "verify_ble_packet_bits",
    "validate_flowgraph",
    "inspect_flowgraph",
    "select_recipe",
    "search_blocks",
    "describe_block",
    "list_examples",
    "read_metric",
    "hardware_preflight",
    "discover_devices",
})


def _repeat_cache_key(name: str, arguments: Dict[str, Any]) -> str:
    try:
        args = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
    except TypeError:
        args = repr(sorted((arguments or {}).items()))
    return f"{name}|{args}"


def _call_registry(ctx: ToolContext, name: str, arguments: Dict[str, Any]) -> str:
    from ..tools import registry

    cache = ctx.extra.setdefault("_idempotent_results", {})
    cache_key = _repeat_cache_key(name, arguments)
    if name in _IDEMPOTENT_TOOLS and cache_key in cache:
        cached = dict(cache[cache_key])
        cached["repeated_call"] = True
        cached["note"] = (
            "Identical call already completed in this stage; the previous "
            "result is returned unchanged. Move on to the next step."
        )
        return json.dumps(cached, ensure_ascii=False)

    result = registry.call(name, arguments, ctx)
    kind = _EVENT_KIND.get(name, name)
    record_tool_event(ctx, kind, result, arguments)
    if name == "render_grc" and result.get("path"):
        _merge_artifacts(ctx, {"grc_path": result["path"]})
    if result.get("grc_path"):
        _merge_artifacts(ctx, {"grc_path": result["grc_path"]})
    if result.get("out_dir"):
        _merge_artifacts(ctx, {"out_dir": result["out_dir"]})
    plot_key = _PLOT_ARTIFACTS.get(name)
    if plot_key and result.get("path"):
        _merge_artifacts(ctx, {plot_key: result["path"]})
    _record_diagnosis_artifact(ctx, name, result)
    if name in _IDEMPOTENT_TOOLS and result.get("ok"):
        cache[cache_key] = dict(result)
    return json.dumps(result, ensure_ascii=False)


def _py_type(schema: Dict[str, Any]) -> Any:
    if not isinstance(schema, dict):
        return Any
    json_type = schema.get("type")
    if isinstance(json_type, list):
        json_type = next((item for item in json_type if item != "null"), "string")
    return _JSON_TYPES.get(str(json_type or ""), Any)


def _wrap_spec(spec: Any, ctx: ToolContext) -> Any:
    from langchain_core.tools import StructuredTool

    props = dict((spec.parameters or {}).get("properties") or {})
    required = set((spec.parameters or {}).get("required") or [])
    parameters = []
    annotations: Dict[str, Any] = {}
    # Parameters without a default must precede defaulted ones, otherwise
    # inspect.Signature raises "non-default argument follows default
    # argument" whenever a tool spec lists an optional property first.
    ordered_props = sorted(
        props.items(), key=lambda kv: 0 if kv[0] in required else 1
    )
    for name, schema in ordered_props:
        schema = schema if isinstance(schema, dict) else {}
        annotation = _py_type(schema)
        if name not in required:
            annotation = Optional[annotation]
        annotations[name] = annotation
        default = (
            inspect.Parameter.empty
            if name in required
            else schema.get("default", None)
        )
        parameters.append(inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=default,
            annotation=annotation,
        ))

    def _run(**kwargs: Any) -> str:
        payload = {key: value for key, value in kwargs.items() if value is not None}
        return _call_registry(ctx, spec.name, payload)

    _run.__name__ = str(spec.name).replace("-", "_")
    _run.__doc__ = spec.description
    _run.__signature__ = inspect.Signature(parameters, return_annotation=str)
    _run.__annotations__ = {**annotations, "return": str}
    return StructuredTool.from_function(
        func=_run,
        name=spec.name,
        description=spec.description or spec.name,
    )


def build_grc_tools(
    ctx: ToolContext, allowed: Optional[Iterable[str]] = None
) -> List[Any]:
    """Bind Registry tools (plus design_flowgraph) as LangChain tools."""
    from langchain_core.tools import tool

    from ..tools import registry
    from ..tools.design_link import design_link as _design_link

    profile = ctx.extra.get("profile")

    @tool
    def design_flowgraph(intent: str = "", recipe: str = "",
                         simulate: bool = True) -> str:
        """按一句通信意图端到端搭出一张可跑流图并自检。

        选配方 -> 逐块建图 -> critic 校验 -> 可选仿真取指标 -> 存 .grc。
        参数: intent(自然语言意图) 或 recipe(配方名)择一;simulate 是否顺带仿真。
        """
        result = _design_link(ctx, profile=profile, intent=intent,
                              recipe=recipe, simulate=simulate, render=True)
        record_tool_event(ctx, "design_link", {
            "ok": result.get("ok"),
            "recipe": result.get("recipe"),
            "valid": result.get("valid"),
            "steps": result.get("steps", []),
            "policy": result.get("policy"),
            "requires_confirmation": result.get("requires_confirmation"),
            "error": result.get("error"),
        })
        _merge_artifacts(ctx, result.get("artifacts", {}))
        if result.get("metrics"):
            ctx.extra.setdefault("metrics", {}).update(result["metrics"])
        if result.get("narrative"):
            ctx.extra["narrative"] = result["narrative"]
        return json.dumps({
            "ok": result.get("ok"),
            "recipe": result.get("recipe"),
            "valid": result.get("valid"),
            "num_blocks": result.get("num_blocks"),
            "errors": result.get("errors"),
            "metrics": result.get("metrics"),
            "artifacts": list((result.get("artifacts") or {}).keys()),
            "error": result.get("error"),
            "artifacts_note": _ARTIFACTS_NOTE,
        }, ensure_ascii=False)

    @tool
    def read_metric(kind: str = "evm", probe_id: str = "sink",
                    modulation: str = "bpsk", sps: int = 4) -> str:
        """从最近一次仿真结果读指标(kind: evm / ber / spectrum 或 spectrum_peak)。"""
        kind_norm = (kind or "").lower().strip()
        if kind_norm in ("spectrum", "psd"):
            kind_norm = "spectrum_peak"
        result = registry.call("read_metric", {
            "kind": kind_norm, "probe_id": probe_id,
            "modulation": modulation, "sps": sps}, ctx)
        if result.get("ok"):
            metrics = ctx.extra.setdefault("metrics", {})
            if kind_norm == "evm" and result.get("value") is not None:
                metrics["evm_pct"] = result["value"]
            elif kind_norm == "ber" and result.get("value") is not None:
                metrics["ber"] = result["value"]
            elif kind_norm == "spectrum_peak":
                if result.get("value") is not None:
                    metrics["spectrum_peak"] = result["value"]
                elif result.get("peak") is not None:
                    metrics["spectrum_peak"] = result["peak"]
                if result.get("peak_bin") is not None:
                    metrics["spectrum_peak_bin"] = result["peak_bin"]
        record_tool_event(ctx, "read_metric", {
            "kind": kind_norm, "ok": result.get("ok"),
            "value": result.get("value"), "error": result.get("error"),
        })
        return json.dumps(result, ensure_ascii=False)

    tools = [design_flowgraph, read_metric]
    existing = {item.name for item in tools}
    registry.load_all()
    for spec in registry.all_specs():
        if spec.name in existing or spec.name in _SKIP_AUTOWRAP:
            continue
        tools.append(_wrap_spec(spec, ctx))
    allowed_set = set(allowed or ())
    return [item for item in tools if not allowed_set or item.name in allowed_set]
