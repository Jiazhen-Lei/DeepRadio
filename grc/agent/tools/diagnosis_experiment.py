"""Counterfactual DiagnosisExperiment: one factor at a time, same metric.

Factors come from the current graph.  Temporary edits are restored and must
not bump ``flowgraph_version``.  The original project is unchanged until the
user confirms a later GraphPatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
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
        return {"ok": False, "error": "The flowgraph has not been created"}
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
        return {"ok": False, "error": simulated.error or "Simulation failed"}
    from . import registry

    payload = dict(args)
    payload["kind"] = metric
    measured = registry.call("read_metric", payload, ctx)
    if not measured.get("ok") or measured.get("value") is None:
        return {
            "ok": False,
            "error": measured.get("error") or "The metric could not be read",
        }
    return measured


def _file_hash(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(
    ctx: ToolContext,
    *,
    metric: str,
    baseline: Any,
    factors: List[Dict[str, Any]],
    trials: List[Dict[str, Any]],
    ranked: List[Dict[str, Any]],
    version_before: int,
    project_hash_before: str,
    baseline_error: str = "",
) -> Dict[str, Any]:
    state = (getattr(ctx, "extra", None) or {}).get("state")
    project = getattr(state, "project", None)
    path = str(getattr(project, "grc_path", "") or "")
    version_after = int(getattr(project, "flowgraph_version", 0) or 0)
    project_hash_after = _file_hash(path)
    unchanged = bool(
        version_after == version_before
        and project_hash_after == project_hash_before
    )
    experiments = [
        {
            "experiment_id": f"factor-{index + 1}",
            "factor": {
                "block": item.get("block"),
                "parameter": item.get("param"),
            },
            "baseline": item.get("baseline"),
            "intervention": item.get("trial_value"),
            "measurement": item.get("metric_value"),
            "delta": item.get("delta"),
            "ok": bool(item.get("ok")),
            "restored": bool(item.get("restored")),
            "error": item.get("error"),
        }
        for index, item in enumerate(trials)
    ]
    ranked_causes = [
        {
            "rank": index + 1,
            "factor": {
                "block": item.get("block"),
                "parameter": item.get("param"),
            },
            "absolute_metric_delta": abs(float(item.get("delta") or 0.0)),
            "experiment_id": "factor-{}".format(
                trials.index(item) + 1
            ) if item in trials else "",
        }
        for index, item in enumerate(ranked)
    ]
    recommendations = [
        {
            "action": "review_factor_before_editing",
            "block": item["factor"]["block"],
            "parameter": item["factor"]["parameter"],
            "because": item["experiment_id"],
        }
        for item in ranked_causes[:3]
    ]
    report = {
        "schema_version": 1,
        "created_at": time.time(),
        "metric": metric,
        "observations": [{
            "observation_id": "baseline",
            "value": baseline,
            "error": baseline_error,
        }],
        "hypotheses": [
            {
                "hypothesis_id": f"hypothesis-{index + 1}",
                "factor": {
                    "block": item.get("block"),
                    "parameter": item.get("param"),
                },
            }
            for index, item in enumerate(factors)
        ],
        "experiments": experiments,
        "ranked_causes": ranked_causes,
        "recommendations": recommendations,
        "project": {
            "path": path,
            "version_before": version_before,
            "version_after": version_after,
            "sha256_before": project_hash_before,
            "sha256_after": project_hash_after,
        },
        "project_unchanged": unchanged,
    }
    out_dir = ctx.out_dir or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, "diagnosis_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return {
        "report": report,
        "report_path": report_path,
        "recommendations": recommendations,
        "project_unchanged": unchanged,
    }


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
    permission="project.write",
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
    project_path = str(getattr(getattr(state, "project", None), "grc_path", "") or "")
    project_hash_before = _file_hash(project_path)
    baseline_sim = getattr(ctx, "last_sim", None)
    factors = discover_factors(ctx)
    if not factors:
        report = _write_report(
            ctx, metric=metric, baseline=None, factors=[], trials=[], ranked=[],
            version_before=version_before,
            project_hash_before=project_hash_before,
        )
        return {
            "ok": True,
            "ranked": [],
            "trials": [],
            "flowgraph_version": version_before,
            "restored": True,
            **report,
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
            report = _write_report(
                ctx, metric=metric, baseline=None, factors=factors,
                trials=[], ranked=[], version_before=version_before,
                project_hash_before=project_hash_before,
                baseline_error=str(baseline.get("error") or ""),
            )
            return {
                "ok": True,
                "ranked": [],
                "trials": [],
                "baseline_error": baseline.get("error"),
                "flowgraph_version": version_before,
                "restored": True,
                **report,
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
    report = _write_report(
        ctx, metric=metric, baseline=baseline_value, factors=factors,
        trials=trials, ranked=ranked, version_before=version_before,
        project_hash_before=project_hash_before,
    )
    return {
        "ok": True,
        "metric": metric,
        "baseline": baseline_value,
        "ranked": ranked,
        "trials": trials,
        "flowgraph_version": version_after,
        "restored": True,
        **report,
    }
