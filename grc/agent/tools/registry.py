"""工具注册表:统一的 ``@tool`` 装饰器 + JSON-Schema + 调度入口。

设计目标(对应架构文档第 6 节的调度决策):

* **function-calling 为主**: :func:`openai_schemas` 导出 OpenAI ``tools`` 数组,
  每个工具带 JSON-Schema 参数描述,供模型可靠地选工具、填参数。
* **ReAct 文本协议兜底**: :func:`react_tool_descriptions` 导出人类可读的
  ``名称(参数): 说明`` 清单,用于不支持 function-calling 的接口/离线场景,
  由 Agent 主循环解析 ``Action/Action Input``。

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


#: 全局注册表: name -> ToolSpec
_REGISTRY: Dict[str, ToolSpec] = {}

#: 已加载过工具模块的标记,避免重复 import
_LOADED = False

#: 各工具模块(相对本包),load_all 时依次 import 触发 @tool 注册
_TOOL_MODULES = (
    "knowledge_tools",
    "build_tools",
    "critic_tools",
    "sim_tools",
    "skill_tools",     # 宏工具:把 skills 编排能力暴露给 function-calling
)


def tool(name: str, description: str,
         parameters: Optional[Dict[str, Any]] = None,
         group: str = "misc"):
    """把一个函数注册为工具。

    Args:
        name: 工具名(LLM 用它来调用,须唯一)。
        description: 给 LLM 看的一句话说明(何时该用)。
        parameters: JSON-Schema(type=object)。None 表示无参数。
        group: 分组标签(knowledge/build/critic/sim)。

    被装饰函数签名应为 ``fn(ctx: ToolContext, **kwargs) -> dict``。
    """
    schema = parameters or {"type": "object", "properties": {}}

    def _decorator(fn: Callable[..., Dict[str, Any]]):
        if name in _REGISTRY:
            logger.warning("工具 %s 已注册,后者覆盖前者", name)
        _REGISTRY[name] = ToolSpec(
            name=name, fn=fn, description=description,
            parameters=schema, group=group)
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


# ---------------------------------------------------------------------------
# 导出: OpenAI function-calling schema(主)
# ---------------------------------------------------------------------------
def openai_schemas(groups: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """导出 OpenAI ``tools`` 数组,直接放进 chat/completions 请求的 ``tools`` 字段。

    Args:
        groups: 只导出指定分组的工具(如 ["knowledge","build"]);None 导全部。
    """
    load_all()
    out = []
    for spec in _REGISTRY.values():
        if groups and spec.group not in groups:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        })
    return out


# ---------------------------------------------------------------------------
# 导出: ReAct 文本协议(兜底)
# ---------------------------------------------------------------------------
def react_tool_descriptions(groups: Optional[List[str]] = None) -> str:
    """导出人类/模型可读的工具清单,用于 ReAct 文本协议提示词。

    形如::

        - search_blocks(query: string) : 语义检索可用块
        - add_block(key: string, id: string) : 往流图添加一个块
    """
    load_all()
    lines = []
    for spec in _REGISTRY.values():
        if groups and spec.group not in groups:
            continue
        props = spec.parameters.get("properties", {})
        arg_str = ", ".join(
            f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {spec.name}({arg_str}) : {spec.description}")
    return "\n".join(lines)


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


def call_from_llm_toolcall(tool_call: Dict[str, Any],
                           ctx: ToolContext) -> Dict[str, Any]:
    """从 OpenAI 风格的 tool_call 对象直接调用(参数是 JSON 字符串)。

    ``tool_call`` 形如 ``{"function": {"name": ..., "arguments": "{...}"}}``。
    """
    fn = (tool_call or {}).get("function", {})
    name = fn.get("name", "")
    raw = fn.get("arguments", "{}")
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"arguments 不是合法 JSON: {exc}"}
    return call(name, args, ctx)
