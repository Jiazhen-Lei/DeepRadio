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
from typing import Any, Dict, List, Optional

from ..tools.registry import ToolContext

logger = logging.getLogger(__name__)

#: 产物落在宿主机磁盘上,而内置文件工具(read_file/ls/glob)看到的是 deepagents
#: 的会话虚拟文件系统。不说清这点,模型会拿真实路径去 read_file,失败后反复
#: ls/glob 找文件直到撞 recursion_limit。
_ARTIFACTS_NOTE = (
    "artifacts 里的路径位于宿主机磁盘,不在你的虚拟文件系统里。"
    "GUI 会自动展示这些产物,禁止用 read_file / ls / glob 去确认它们是否存在。"
)


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
            "policy": result.get("policy"),
            "requires_confirmation": result.get("requires_confirmation"),
            "error": result.get("error"),
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
            "artifacts_note": _ARTIFACTS_NOTE,
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
        """对当前流图跑一次无头仿真并读回落盘数据。

        probe 文件路径自动取自流图里 file sink 的 file 参数,不要自己指定;
        probe_id 仅用于事件记录。
        """
        r = registry.call("run_simulation", {}, ctx)
        _rec_event(ctx, "simulate", {"ok": r.get("ok"),
                                     "probe_sizes": r.get("probe_sizes"),
                                     "out_dir": r.get("out_dir")})
        if r.get("out_dir"):
            _merge_artifacts(ctx, {"out_dir": r["out_dir"]})
        return json.dumps(r, ensure_ascii=False)

    @tool
    def read_metric(kind: str = "evm", probe_id: str = "sink",
                    modulation: str = "bpsk", sps: int = 4) -> str:
        """从最近一次仿真结果读指标(kind: evm / ber / spectrum 或 spectrum_peak)。"""
        kind_norm = (kind or "").lower().strip()
        if kind_norm in ("spectrum", "psd"):
            kind_norm = "spectrum_peak"
        r = registry.call("read_metric", {
            "kind": kind_norm, "probe_id": probe_id,
            "modulation": modulation, "sps": sps}, ctx)
        if r.get("ok"):
            metrics = ctx.extra.setdefault("metrics", {})
            if kind_norm == "evm" and r.get("value") is not None:
                metrics["evm_pct"] = r["value"]
            elif kind_norm == "ber" and r.get("value") is not None:
                metrics["ber"] = r["value"]
            elif kind_norm == "spectrum_peak":
                if r.get("value") is not None:
                    metrics["spectrum_peak"] = r["value"]
                elif r.get("peak") is not None:
                    metrics["spectrum_peak"] = r["peak"]
                if r.get("peak_bin") is not None:
                    metrics["spectrum_peak_bin"] = r["peak_bin"]
        _rec_event(ctx, "read_metric", {
            "kind": kind_norm, "ok": r.get("ok"),
            "value": r.get("value"), "error": r.get("error"),
        })
        return json.dumps(r, ensure_ascii=False)

    def _call(name: str, arguments: Dict[str, Any]) -> str:
        result = registry.call(name, arguments, ctx)
        _rec_event(ctx, name, result)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def spec_clarify(text: str = "") -> str:
        """Extract specification gaps and return questions that need user input."""
        return _call("spec_clarify", {"text": text})

    @tool
    def spec_commit(text: str) -> str:
        """Commit user goals, decisions, and success claims to SharedState."""
        return _call("spec_commit", {"text": text})

    @tool
    def select_recipe(intent: str = "", recipe: str = "") -> str:
        """Select a deterministic GNU Radio recipe without building it."""
        return _call("select_recipe", {"intent": intent, "recipe": recipe})

    @tool
    def search_blocks(query: str, limit: int = 15) -> str:
        """Search installed GNU Radio blocks by keyword."""
        return _call("search_blocks", {"query": query, "limit": limit})

    @tool
    def describe_block(key: str) -> str:
        """Describe the parameters and ports of one installed block."""
        return _call("describe_block", {"key": key})

    @tool
    def verify_claims() -> str:
        """Bind current validation and simulation observations to pending claims."""
        return _call("verify_claims", {})

    @tool
    def plot_spectrum(probe_id: str = "sink", samp_rate: float = 1e6) -> str:
        """Render a spectrum artifact from the latest simulation probe."""
        result = registry.call(
            "plot_spectrum",
            {"probe_id": probe_id, "samp_rate": samp_rate},
            ctx,
        )
        if result.get("path"):
            _merge_artifacts(ctx, {"spectrum_png": result["path"]})
        _rec_event(ctx, "plot_spectrum", result)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def diagnose_by_metric(
        metric: str = "evm",
        probe_id: str = "",
        modulation: str = "bpsk",
        sps: int = 4,
    ) -> str:
        """Diagnose a flowgraph from EVM or spectrum observations."""
        return _call(
            "debug_by_metric",
            {
                "metric": metric,
                "probe_id": probe_id,
                "modulation": modulation,
                "sps": sps,
            },
        )

    @tool
    def suggest_fix(block_param: str, value: str) -> str:
        """Apply one diagnosis suggestion to the in-memory flowgraph."""
        if "." not in block_param:
            return json.dumps(
                {"ok": False, "error": "block_param 格式应为 block.parameter"},
                ensure_ascii=False,
            )
        block_id, parameter = block_param.split(".", 1)
        return _call(
            "apply_grc_diff",
            {"block_id": block_id, "parameter": parameter, "value": value},
        )

    @tool
    def apply_grc_diff(block_id: str, parameter: str, value: str) -> str:
        """Apply one recoverable parameter change. Modulation/constellation
        changes are rejected; use design_flowgraph with a new recipe instead.
        Successful changes resimulate and rebind claims by default.
        """
        return _call(
            "apply_grc_diff",
            {"block_id": block_id, "parameter": parameter, "value": value},
        )

    @tool
    def configure_sdr(
        device_type: str,
        center_freq: Optional[float] = None,
        sample_rate: Optional[float] = None,
    ) -> str:
        """Record SDR block configuration; this never touches real hardware."""
        return _call(
            "configure_sdr",
            {
                "device_type": device_type,
                "center_freq": center_freq,
                "sample_rate": sample_rate,
            },
        )

    @tool
    def list_devices() -> str:
        """Report whether real SDR discovery is enabled."""
        return _call("list_devices", {})

    return [
        design_flowgraph,
        validate_flowgraph,
        run_simulation,
        read_metric,
        spec_clarify,
        spec_commit,
        select_recipe,
        search_blocks,
        describe_block,
        verify_claims,
        plot_spectrum,
        diagnose_by_metric,
        suggest_fix,
        apply_grc_diff,
        configure_sdr,
        list_devices,
    ]
