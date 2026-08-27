"""Counterfactual DiagnosisExperiment: one factor at a time, same metric.

Factors come from the current graph.  Temporary edits are restored and must
not bump ``flowgraph_version``.  The original project is unchanged until the
user confirms a later GraphPatch.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .registry import ToolContext, tool


_FACTOR_TOKENS = (
    "noise",
    "freq_offset",
    "frequency_offset",
    "gain",
    "amplitude",
    "amp",
)

_SKIP_TOKENS = (
    "epsilon",
    "samp_rate",
    "sample_rate",
)


def _numeric_value(raw: Any) -> float | None:
    try:
        return float(str(raw).strip().replace("'", "").replace('"', ""))
    except (TypeError, ValueError, AttributeError):
        return None


def discover_factors(ctx: ToolContext) -> List[Dict[str, Any]]:
    """Inspect current blocks for independently intervenable numeric params."""
    factors: List[Dict[str, Any]] = []
    seen = set()
    for block_id, block in (getattr(ctx, "blocks", None) or {}).items():
        params = getattr(block, "params", None) or {}
        for name, param in params.items():
            key = str(name).lower()
            if any(token in key for token in _SKIP_TOKENS):
                continue
            if not any(token in key for token in _FACTOR_TOKENS):
                continue
            getter = getattr(param, "get_value", None)
            raw = getter() if callable(getter) else param
            baseline = _numeric_value(raw)
            if baseline is None:
                continue
            marker = (str(block_id), str(name))
            if marker in seen:
                continue
            seen.add(marker)
            factors.append({
                "block": str(block_id),
                "param": str(name),
                "baseline": baseline,
                "baseline_raw": raw,
            })
    return factors


def trial_value(param: str, baseline: float) -> float:
    key = str(param).lower()
    if "offset" in key or "epsilon" in key:
        return 0.0
    if "noise" in key:
        return baseline * 0.5
    if "gain" in key or "amp" in key:
        return baseline * 1.5 if baseline else 1.0
    return baseline * 0.5


def _set_param(ctx: ToolContext, block_id: str, name: str, value: Any) -> Dict[str, Any]:
    from . import registry

    return registry.call(
        "set_param",
        {"id": block_id, "name": name, "value": value},
        ctx,
    )


def _restore_factor(ctx: ToolContext, factor: Dict[str, Any]) -> None:
    _set_param(
        ctx,
        factor["block"],
        factor["param"],
        factor.get("baseline_raw", factor["baseline"]),
    )


def _measure(ctx: ToolContext, metric: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from ..runtime import simulate
    from .sim_tools import derive_probes

    fg = getattr(ctx, "flow_graph", None)
    if fg is None:
        return {"ok": False, "error": "流图尚未创建"}
    simulated = simulate.run(
        fg,
        ctx.platform,
        probes=derive_probes(ctx) or None,
        out_dir=ctx.out_dir,
        timeout=30.0,
        save_grc=False,
    )
    ctx.last_sim = simulated
    if not simulated.ok:
        return {"ok": False, "error": simulated.error or "仿真失败"}
    from . import registry

    payload = dict(args)
    payload["kind"] = metric
    measured = registry.call("read_metric", payload, ctx)
    if not measured.get("ok") or measured.get("value") is None:
        return {
            "ok": False,
            "error": measured.get("error") or "指标不可读",
        }
    return measured


@tool(
    name="run_diagnosis_experiment",
    description=(
        "Freeze the current graph, change one discovered factor at a time, "
        "re-measure the same metric, rank contribution, and restore the graph. "
        "Does not modify the saved project."
    ),
    parameters={
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "modulation": {"type": "string"},
            "sps": {"type": "integer"},
            "probe_id": {"type": "string"},
            "samp_rate": {"type": "number"},
        },
    },
    group="sim",
    origin="deepradio_runtime",
    runtime="gnuradio",
    effect_level="READ",
    idempotent=True,
)
def run_diagnosis_experiment(
    ctx: ToolContext,
    metric: str = "evm",
    modulation: str = "bpsk",
    sps: int = 4,
    probe_id: str = "",
    samp_rate: float = 1e6,
) -> Dict[str, Any]:
    state = (getattr(ctx, "extra", None) or {}).get("state")
    version_before = int(getattr(getattr(state, "project", None), "flowgraph_version", 0) or 0)
    baseline_sim = getattr(ctx, "last_sim", None)
    factors = discover_factors(ctx)
    if not factors:
        return {
            "ok": True,
            "ranked": [],
            "trials": [],
            "flowgraph_version": version_before,
            "restored": True,
        }

    measure_args = {
        "modulation": modulation,
        "sps": sps,
        "probe_id": probe_id,
        "samp_rate": samp_rate,
    }
    trials: List[Dict[str, Any]] = []
    baseline_value = None
    try:
        baseline = None
        if baseline_sim is not None and getattr(baseline_sim, "ok", False):
            from . import registry

            payload = dict(measure_args)
            payload["kind"] = metric
            existing = registry.call("read_metric", payload, ctx)
            if existing.get("ok") and existing.get("value") is not None:
                baseline = existing
        if baseline is None:
            baseline = _measure(ctx, metric, measure_args)
            ctx.last_sim = baseline_sim
        if not baseline.get("ok"):
            return {
                "ok": True,
                "ranked": [],
                "trials": [],
                "baseline_error": baseline.get("error"),
                "flowgraph_version": version_before,
                "restored": True,
            }
        baseline_value = float(baseline["value"])

        for factor in factors:
            proposed = trial_value(factor["param"], factor["baseline"])
            if proposed == factor["baseline"]:
                continue
            applied = _set_param(ctx, factor["block"], factor["param"], proposed)
            if not applied.get("ok"):
                trials.append({**factor, "ok": False, "error": applied.get("error")})
                _restore_factor(ctx, factor)
                continue
            measured = _measure(ctx, metric, measure_args)
            _restore_factor(ctx, factor)
            delta = None
            if measured.get("ok") and measured.get("value") is not None:
                delta = float(measured["value"]) - baseline_value
            trials.append({
                **factor,
                "trial_value": proposed,
                "metric_value": measured.get("value"),
                "delta": delta,
                "ok": bool(measured.get("ok")),
                "error": measured.get("error"),
                "restored": True,
            })
    finally:
        for factor in factors:
            _restore_factor(ctx, factor)
        ctx.last_sim = baseline_sim

    ranked = sorted(
        [item for item in trials if item.get("delta") is not None],
        key=lambda item: abs(float(item["delta"])),
        reverse=True,
    )
    version_after = int(getattr(getattr(state, "project", None), "flowgraph_version", 0) or 0)
    if state is not None and version_after != version_before:
        state.project.flowgraph_version = version_before
        version_after = version_before
    return {
        "ok": True,
        "metric": metric,
        "baseline": baseline_value,
        "ranked": ranked,
        "trials": trials,
        "flowgraph_version": version_after,
        "restored": True,
    }
