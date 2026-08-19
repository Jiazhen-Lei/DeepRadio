"""tools_lc:把 DeepRadio 的确定性建图工具桥接为 LangChain ``@tool``。

``create_deep_agent`` 的工具是 LangChain 工具。DeepRadio 已有一套确定性工具链
(``grc.agent.tools.registry`` + ``skills.design_link`` 宏),它既是无 LLM 降级
底座,也应作为 subagent 在 function-calling 时的真实执行工具——**单一事实源**。

桥接策略(与 deepagents 内部 runtime API 解耦,跨版本稳定):

* 用**闭包工厂** :func:`build_grc_tools` 把一个共享的 :class:`ToolContext`
  (携带 platform / out_dir / flow_graph)绑定进每个 LangChain 工具。
* 工具内部走 ``registry.call`` / ``design_link`` 真实建图,产物路径与摘要
  写回 ``ctx.extra["artifacts"]`` 与 ``ctx.extra["events"]``,由 adapter 统一
  收集、落盘镜像到 ``/session/final``。
* 工具返回值为紧凑 JSON 文本(observation),回喂给模型。

这样模型走的路径与离线降级路径完全一致,保证可复现(论文 baseline)。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from ..tools.registry import ToolContext

logger = logging.getLogger(__name__)


def _rec_event(ctx: ToolContext, kind: str, payload: Dict[str, Any]) -> None:
    """把一次工具事件记进 ctx.extra['events'](供 adapter 折叠为事件流)。"""
    ctx.extra.setdefault("events", []).append({"kind": kind, "payload": payload})


def _merge_artifacts(ctx: ToolContext, artifacts: Dict[str, Any]) -> None:
    """合并产物路径到 ctx.extra['artifacts']。"""
    store = ctx.extra.setdefault("artifacts", {})
    for k, v in (artifacts or {}).items():
        if v:
            store[k] = v


def build_grc_tools(ctx: ToolContext) -> List[Any]:
    """按共享 ctx 生成绑定后的 LangChain 工具列表。

    Args:
        ctx: 共享运行上下文(须已带 platform / out_dir)。

    Returns:
        LangChain 工具对象列表(用 ``@tool`` 装饰)。

    Raises:
        ImportError: 未安装 langchain_core 时向上抛出。
    """
    from langchain_core.tools import tool

    from ..tools import registry
    from ..tools.design_link import design_link as _design_link

    profile = ctx.extra.get("profile")

    @tool
    def design_flowgraph(intent: str = "", recipe: str = "",
                         simulate: bool = True) -> str:
        """按一句通信意图端到端搭出一张可跑流图并自检。

        选配方 -> 逐块建图 -> critic 校验 -> 可选仿真取指标 -> 存 .grc。
        参数: intent(自然语言意图) 或 recipe(配方名 tone_noise/bpsk_awgn/
        qpsk_awgn/ofdm_awgn)择一;simulate 是否顺带仿真取指标。
        """
        result = _design_link(ctx, profile=profile, intent=intent,
                              recipe=recipe, simulate=simulate, render=True)
        _rec_event(ctx, "design_link", {
            "recipe": result.get("recipe"),
            "valid": result.get("valid"),
            "steps": result.get("steps", []),
        })
        _merge_artifacts(ctx, result.get("artifacts", {}))
        if result.get("metrics"):
            ctx.extra.setdefault("metrics", {}).update(result["metrics"])
        if result.get("narrative"):
            ctx.extra["narrative"] = result["narrative"]
        # 环境级失败(缺 platform 等):如实透传,不伪装成建图结果
        return json.dumps({
            "ok": result.get("ok"),
            "recipe": result.get("recipe"),
            "valid": result.get("valid"),
            "num_blocks": result.get("num_blocks"),
            "errors": result.get("errors"),
            "metrics": result.get("metrics"),
            "artifacts": list((result.get("artifacts") or {}).keys()),
            "error": result.get("error"),
        }, ensure_ascii=False)

    @tool
    def validate_flowgraph() -> str:
        """对当前流图跑 critic 校验,返回是否合法与错误列表。"""
        r = registry.call("validate_flowgraph", {}, ctx)
        _rec_event(ctx, "validate", {"valid": r.get("valid"),
                                     "errors": r.get("errors", [])})
        return json.dumps(r, ensure_ascii=False)

    @tool
    def run_simulation(probe_id: str = "sink") -> str:
        """对当前流图跑一次无头仿真,把 probe_id 指定的 sink 落盘取指标。"""
        probe_path = None
        if ctx.out_dir:
            import os
            probe_path = os.path.join(ctx.out_dir, f"{probe_id}_rx.bin")
        probes = {probe_id: [probe_path, "complex64"]} if probe_path else {}
        r = registry.call("run_simulation", {"probes": probes}, ctx)
        _rec_event(ctx, "simulate", {"ok": r.get("ok"),
                                     "out_dir": r.get("out_dir")})
        if r.get("out_dir"):
            _merge_artifacts(ctx, {"out_dir": r["out_dir"]})
        return json.dumps(r, ensure_ascii=False)

    @tool
    def read_metric(kind: str = "evm", probe_id: str = "sink",
                    modulation: str = "bpsk", sps: int = 4) -> str:
        """从最近一次仿真结果读指标(kind: evm / spectrum)。"""
        r = registry.call("read_metric", {
            "kind": kind, "probe_id": probe_id,
            "modulation": modulation, "sps": sps}, ctx)
        if r.get("ok") and r.get("value") is not None:
            ctx.extra.setdefault("metrics", {})[f"{kind}"] = r["value"]
        _rec_event(ctx, "read_metric", {"kind": kind, "ok": r.get("ok"),
                                        "value": r.get("value")})
        return json.dumps(r, ensure_ascii=False)

    return [design_flowgraph, validate_flowgraph, run_simulation, read_metric]
