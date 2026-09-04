"""工具注册表:统一的 ``@tool`` 装饰器 + JSON-Schema + 调度入口。

工具由 :mod:`service.tools_lc` 桥接为 LangChain ``StructuredTool``,再经
deepagents 的 function-calling 协议供 MainAgent 调度。

工具签名统一为 ``fn(ctx, **kwargs) -> dict``:

* ``ctx``: :class:`ToolContext`,携带 platform / flow_graph / 输出目录等
  运行期依赖(工具本身无状态,状态都在 ctx 上)。
* 返回值必须是可 JSON 序列化的 ``dict``(``ok`` / ``error`` / 业务字段),
  以便直接回喂给 LLM 作为 observation。
"""

from __future__ import annotations

import importlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具运行上下文
# ---------------------------------------------------------------------------
@dataclass
class ToolContext:
    """一次会话/一轮建图中,所有工具共享的运行期依赖。

    工具是无状态纯函数,可变状态都挂在这里,由 Agent 持有并在工具间传递。

    Attributes:
        platform: env.make_platform() 得到的平台(块库/存盘)。
        flow_graph: 当前正在增量构建的 FlowGraph(add_block/connect 就地改它)。
        out_dir: 生成脚本 / .grc / 图片的输出目录。
        blocks: 已添加块的 id -> block 对象,便于 connect/set_param 按 id 定位。
        last_sim: 最近一次仿真的 SimResult(供 read_metric/plot_* 复用)。
        extra: 其它临时数据。
    """

    platform: Any = None
    flow_graph: Any = None
    out_dir: Optional[str] = None
    blocks: Dict[str, Any] = field(default_factory=dict)
    last_sim: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def ensure_flow_graph(self):
        """惰性创建 flow_graph(首次 add_block 时调用)。"""
        if self.flow_graph is None:
            if self.platform is None:
                raise RuntimeError("ToolContext 缺少 platform,无法创建 flow_graph")
            self.flow_graph = self.platform.make_flow_graph()
        return self.flow_graph


# ---------------------------------------------------------------------------
# 工具描述与注册表
# ---------------------------------------------------------------------------
@dataclass
class ToolSpec:
    """单个工具的元信息。"""

    name: str
    fn: Callable[..., Dict[str, Any]]
    description: str
    parameters: Dict[str, Any]          # JSON-Schema (type=object)
    group: str = "misc"
    origin: str = ""
    runtime: str = ""
    permission: str = "project.read"
    idempotent: bool = True
    requires: List[str] = field(default_factory=list)


#: 全局注册表: name -> ToolSpec
_REGISTRY: Dict[str, ToolSpec] = {}

#: 已加载过工具模块的标记,避免重复 import
_LOADED = False

#: 当前工具调用链的顶层工具；ContextVar 避免并行调用共享 ToolContext 时互相污染。
_GATEWAY_PARENT_TOOL: ContextVar[Optional[str]] = ContextVar(
    "gateway_parent_tool", default=None
)

#: 各工具模块(相对本包),load_all 时依次 import 触发 @tool 注册
_TOOL_MODULES = (
    "knowledge_tools",
    "build_tools",
    "critic_tools",
    "sim_tools",
    "state_tools",
    "ble_tools",
    "hardware_tools",
    "debug_by_metric",
    "diagnosis_experiment",
    "diagnosis_checks",
)


def tool(name: str, description: str,
         parameters: Optional[Dict[str, Any]] = None,
         group: str = "misc",
         origin: str = "",
         runtime: str = "",
         permission: str = "project.read",
         idempotent: bool = True,
         requires: Optional[List[str]] = None):
    """把一个函数注册为工具。

    Args:
        name: 工具名(LLM 用它来调用,须唯一)。
        description: 给 LLM 看的一句话说明(何时该用)。
        parameters: JSON-Schema(type=object)。None 表示无参数。
        group: 分组标签(knowledge/build/critic/sim)。
        origin: 实现归属。``deepradio_protocol`` 是 DeepRadio 协议算法；
            ``deepradio_compose`` 用 GNU Radio 块组拓扑；``vendor_cli``
            调用 uhd/iio 等主机命令；``deepradio_runtime`` 是受控子进程。
        runtime: 实际执行面，如 ``gnuradio_blocks`` / ``grcc`` / ``iio_info``。

    被装饰函数签名应为 ``fn(ctx: ToolContext, **kwargs) -> dict``。
    """
    schema = parameters or {"type": "object", "properties": {}}

    def _decorator(fn: Callable[..., Dict[str, Any]]):
        if name in _REGISTRY:
            logger.warning("工具 %s 已注册,后者覆盖前者", name)
        _REGISTRY[name] = ToolSpec(
            name=name, fn=fn, description=description,
            parameters=schema, group=group, origin=origin, runtime=runtime,
            permission=str(permission or "project.read"),
            idempotent=bool(idempotent),
            requires=list(requires or []))
        return fn

    return _decorator


def load_all() -> None:
    """import 所有工具模块,触发 @tool 注册。幂等。"""
    global _LOADED
    if _LOADED:
        return
    for mod in _TOOL_MODULES:
        try:
            importlib.import_module(f"{__package__}.{mod}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载工具模块 %s 失败: %s", mod, exc)
    _LOADED = True


def get(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)


def all_specs() -> List[ToolSpec]:
    return list(_REGISTRY.values())


def names() -> List[str]:
    return list(_REGISTRY.keys())


def origin_of(name: str) -> str:
    spec = _REGISTRY.get(name)
    return spec.origin if spec else ""


def runtime_of(name: str) -> str:
    spec = _REGISTRY.get(name)
    return spec.runtime if spec else ""


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def call(name: str, arguments: Dict[str, Any],
         ctx: ToolContext) -> Dict[str, Any]:
    """按名字调用工具,统一异常包装为 ``{"ok": False, "error": ...}``。

    Args:
        name: 工具名。
        arguments: 参数字典(来自 LLM 的 function_call.arguments,已解析成 dict)。
        ctx: 运行上下文。
    """
    load_all()
    spec = _REGISTRY.get(name)
    if spec is None:
        return {"ok": False, "error": f"未知工具: {name}",
                "available": names()}
    denial = _execution_denial(spec, ctx, arguments or {})
    if denial:
        result = {
            "ok": False,
            "policy": "DENY",
            "error": denial,
            "tool": name,
            "permission": spec.permission,
        }
        if name == "start_flowgraph":
            result.update({"enabled": False, "running": False})
        if name == "arm_hardware_flowgraph":
            result["armed"] = False
        return result
    parent_token = _GATEWAY_PARENT_TOOL.set(spec.name)
    try:
        result = spec.fn(ctx, **(arguments or {}))
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        result.setdefault("ok", True)
        return result
    except TypeError as exc:
        return {"ok": False, "error": f"参数错误: {exc}",
                "expected": spec.parameters.get("properties", {})}
    except Exception as exc:  # noqa: BLE001
        logger.exception("工具 %s 执行异常", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _GATEWAY_PARENT_TOOL.reset(parent_token)


def _execution_denial(
    spec: ToolSpec, ctx: ToolContext, arguments: Dict[str, Any]
) -> str:
    """Central execution gateway shared by all tool callers."""
    extra = getattr(ctx, "extra", {}) or {}
    if extra.get("enforce_stage_tools") and spec.permission != "rf.stop":
        workflow = dict(extra.get("workflow") or {})
        stage_id = str(extra.get("stage_id") or workflow.get("current_stage") or "")
        if not stage_id:
            return "A current Workflow Stage is required before using domain tools"
        try:
            from ..workflow.catalog import allowed_tools_for_stage

            allowed = allowed_tools_for_stage(stage_id)
        except ValueError as exc:
            return str(exc)
        requested_tool = str(_GATEWAY_PARENT_TOOL.get() or spec.name)
        if requested_tool not in allowed:
            return f"Tool {requested_tool} is not allowed in Stage {stage_id}"
    forbidden = set(extra.get("forbidden_permissions") or [])
    if spec.permission in forbidden and spec.permission != "rf.stop":
        return f"Permission {spec.permission} is forbidden for this user request"
    if spec.permission == "project.write" and extra.get("mutation_forbidden"):
        return "The current user request is read-only"
    missing = [
        name
        for name in spec.requires
        if not _requirement_satisfied(name, ctx, arguments)
    ]
    if missing:
        return "Missing execution preconditions: {}".format(", ".join(missing))
    return ""


def _requirement_satisfied(
    name: str, ctx: ToolContext, arguments: Dict[str, Any]
) -> bool:
    extra = getattr(ctx, "extra", {}) or {}
    state = extra.get("state")
    project = getattr(state, "project", None)
    runtime = getattr(state, "runtime", None)
    if name == "user_effect_grant":
        try:
            from .hardware_tools import _rf_approved

            return bool(_rf_approved(ctx))
        except Exception:  # noqa: BLE001
            return bool({"rf.start", "RF_RUN"} & set(
                getattr(runtime, "granted_permissions", None) or []
            ))
    if name == "flowgraph_armed":
        try:
            from .hardware_tools import _rf_armed

            return bool(_rf_armed(ctx, str(arguments.get("grc_path") or "")))
        except Exception:  # noqa: BLE001
            return bool((getattr(project, "config", None) or {}).get("rf_armed"))
    if name == "device_probed":
        observed = dict(
            getattr(getattr(state, "project", None), "config", {}).get(
                "observed_device"
            ) or {}
        )
        if observed.get("identity"):
            return True
        try:
            from .hardware_tools import _completion_satisfied

            return bool(_completion_satisfied(ctx, "device_probed"))
        except Exception:  # noqa: BLE001
            pass
        workflow = dict(extra.get("workflow") or {})
        for stage in workflow.get("stages") or []:
            completion = dict((stage.get("result") or {}).get("completion") or {})
            if completion.get("device_probed") is True:
                return True
        return any(
            event.get("kind") == "probe_device"
            and bool((event.get("payload") or {}).get("device_probed"))
            for event in (extra.get("events") or [])
        )
    # Unknown requirements fail closed instead of silently becoming advisory.
    return False


def action_metadata(name: str) -> Dict[str, Any]:
    """Return planner-facing metadata without exposing the callable."""
    spec = _REGISTRY.get(name)
    if spec is None:
        return {}
    return {
        "name": spec.name,
        "description": spec.description,
        "group": spec.group,
        "permission": spec.permission,
        "idempotent": spec.idempotent,
        "requires": list(spec.requires),
        "parameters": dict(spec.parameters),
    }


# ---------------------------------------------------------------------------
# 导出: OpenAI function-calling schema(主)
# ---------------------------------------------------------------------------
