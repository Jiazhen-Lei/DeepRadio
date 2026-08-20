"""Tools that bridge agent requests to SharedState."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from ..knowledge import recipes
from ..state import (
    ALLOW,
    Claim,
    ClaimStore,
    Decision,
    Evidence,
    create_snapshot,
    gate,
)
from .registry import ToolContext, tool


def _state(ctx: ToolContext):
    state = ctx.extra.get("state")
    if state is None:
        raise RuntimeError("ToolContext 未挂载 SharedState")
    return state


def resolve_confirmation(ctx: ToolContext, text: str) -> Dict[str, Any]:
    state = _state(ctx)
    normalized = (text or "").strip().lower()
    reject = any(
        phrase in normalized
        for phrase in (
            "取消修改",
            "拒绝修改",
            "不要执行",
            "不要继续",
            "不确认",
            "不同意",
            "cancel",
        )
    )
    affirm = not reject and (
        normalized in ("确认", "同意", "继续", "approve")
        or any(
            phrase in normalized
            for phrase in ("确认执行", "确认修改", "同意修改", "继续执行")
        )
    )
    if not state.coordination.pending_confirmations:
        return {"ok": True, "resolved": False}
    pending = state.coordination.pending_confirmations[-1]
    if affirm:
        pending["approved"] = True
        return {"ok": True, "resolved": True, "approved": True}
    if reject:
        state.coordination.pending_confirmations.pop()
        return {"ok": True, "resolved": True, "approved": False}
    return {"ok": True, "resolved": False}


def commit_intent(ctx: ToolContext, text: str) -> Dict[str, Any]:
    """Deterministically extract the minimum traceable radio specification."""
    state = _state(ctx)
    lowered = text.lower()
    modulation = next(
        (name for name in ("ofdm", "qpsk", "bpsk") if name in lowered), ""
    )
    known_modulation = next(
        (
            str(item.value)
            for item in reversed(state.spec.decisions)
            if item.key == "modulation"
        ),
        "",
    )
    channel = "awgn" if "awgn" in lowered or "噪声" in text else ""
    decisions = []
    if modulation:
        decisions.append(Decision("modulation", modulation, "user"))
    if channel:
        decisions.append(Decision("channel", channel, "user"))
    rejected = []
    for decision in decisions:
        existing = next(
            (d for d in state.spec.decisions if d.key == decision.key), None
        )
        if (
            existing
            and decision.key in state.coordination.locked_constraints
            and existing.value != decision.value
        ):
            rejected.append(decision.key)
            continue
        if existing:
            existing.value = decision.value
            existing.source = decision.source
        else:
            state.spec.decisions.append(decision)
    if text and text not in state.spec.goals:
        state.spec.goals.append(text)

    match = re.search(
        r"(?:evm)\s*(?:要|需|必须|应)?\s*(?:小于|低于|<|≤)\s*(\d+(?:\.\d+)?)\s*%?",
        lowered,
    )
    claim_ids: List[str] = []
    if match:
        threshold = float(match.group(1))
        condition = f"EVM < {threshold:g}%"
        if condition not in state.spec.success_conditions:
            state.spec.success_conditions.append(condition)
        claim_id = f"evm_lt_{threshold:g}".replace(".", "_")
        ClaimStore(state).upsert(
            Claim(
                id=claim_id,
                statement=condition,
                layer="sim",
                project_version=state.project.flowgraph_version,
            )
        )
        claim_ids.append(claim_id)
    state.spec.open_questions = []
    if not (modulation or known_modulation):
        state.spec.open_questions.append("使用哪种调制方式？")
    return {
        "ok": True,
        "decisions": [d.key for d in decisions],
        "rejected_locked": rejected,
        "claims": claim_ids,
        "open_questions": list(state.spec.open_questions),
    }


def verify_state_claims(ctx: ToolContext, metrics: Dict[str, Any]) -> Dict[str, Any]:
    state = _state(ctx)
    store = ClaimStore(state)
    updated = []
    for claim in state.claims:
        if claim.layer != "sim" or not claim.statement.upper().startswith("EVM"):
            continue
        match = re.search(r"<\s*(\d+(?:\.\d+)?)", claim.statement)
        value = metrics.get("evm_pct")
        if not match or value is None:
            continue
        threshold = float(match.group(1))
        evidence = Evidence(
            test="read_metric",
            observation={
                "metric": "evm_pct",
                "value": float(value),
                "operator": "<",
                "threshold": threshold,
            },
            project_version=state.project.flowgraph_version,
            artifact=str(ctx.extra.get("artifacts", {}).get("constellation_png", "")),
        )
        store.add_evidence(claim.id, evidence, passed=float(value) < threshold)
        updated.append(claim.id)
    return {"ok": True, "updated": updated, "claims": store.summary()}


@tool(
    name="spec_clarify",
    description="Inspect missing radio specification fields.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    group="state",
)
def spec_clarify(ctx: ToolContext, text: str = ""):
    result = commit_intent(ctx, text)
    return {
        "ok": True,
        "open_questions": result["open_questions"],
        "complete": not result["open_questions"],
    }


@tool(
    name="spec_commit",
    description="Commit traceable user goals and radio decisions.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    group="state",
)
def spec_commit(ctx: ToolContext, text: str):
    return commit_intent(ctx, text)


@tool(
    name="select_recipe",
    description="Select a deterministic flowgraph recipe for an intent.",
    parameters={
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "recipe": {"type": "string"},
        },
    },
    group="knowledge",
)
def select_recipe(ctx: ToolContext, intent: str = "", recipe: str = ""):
    selected = recipes.get_recipe(recipe) if recipe else recipes.match_recipe(intent)
    if selected is None:
        return {"ok": False, "error": f"未知配方: {recipe}"}
    return {"ok": True, "recipe": selected.name, "title": selected.title}


@tool(
    name="verify_claims",
    description="Bind current simulation metrics to pending claims.",
    parameters={"type": "object", "properties": {}},
    group="state",
)
def verify_claims(ctx: ToolContext):
    return verify_state_claims(ctx, ctx.extra.get("metrics", {}))


@tool(
    name="configure_sdr",
    description="Record an SDR flowgraph configuration without touching hardware.",
    parameters={
        "type": "object",
        "properties": {
            "device_type": {"type": "string"},
            "center_freq": {"type": "number"},
            "sample_rate": {"type": "number"},
        },
        "required": ["device_type"],
    },
    group="hardware",
)
def configure_sdr(
    ctx: ToolContext,
    device_type: str,
    center_freq: Optional[float] = None,
    sample_rate: Optional[float] = None,
):
    state = _state(ctx)
    policy = gate(
        {
            "target": "device",
            "scope": "configuration",
            "domain": "flowgraph",
        },
        state.coordination,
    )
    if policy != ALLOW:
        state.coordination.pending_confirmations.append(
            {
                "action": "configure_sdr",
                "device_type": device_type,
                "policy": policy,
            }
        )
        return {
            "ok": False,
            "policy": policy,
            "requires_confirmation": True,
        }
    state.project.config["device"] = {
        "type": device_type,
        "center_freq": center_freq,
        "sample_rate": sample_rate,
        "mode": "flowgraph_config_only",
    }
    return {"ok": True, "device": state.project.config["device"]}


@tool(
    name="list_devices",
    description="Report hardware discovery availability.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def list_devices(ctx: ToolContext):
    return {
        "ok": False,
        "enabled": False,
        "requires_confirmation": True,
        "error": "一期不枚举真实 SDR；仅支持 flowgraph 配置",
    }


@tool(
    name="apply_grc_diff",
    description="Apply a single-block parameter change through deterministic tools.",
    parameters={
        "type": "object",
        "properties": {
            "block_id": {"type": "string"},
            "parameter": {"type": "string"},
            "value": {},
        },
        "required": ["block_id", "parameter", "value"],
    },
    group="build",
)
def apply_grc_diff(
    ctx: ToolContext, block_id: str, parameter: str, value: Any
):
    from . import registry

    state = _state(ctx)
    policy = gate(
        {"target": parameter, "scope": "single_block_change", "domain": "dsp"},
        state.coordination,
    )
    if policy != ALLOW:
        return {"ok": False, "policy": policy, "error": "策略拒绝修改"}
    if ctx.flow_graph is None:
        return {
            "ok": False,
            "error": "当前 session 没有内存流图；请先构建或加载工程",
        }
    state_path = str(ctx.extra.get("state_path") or "")
    snapshots_dir = str(ctx.extra.get("snapshots_dir") or "")
    if state_path and snapshots_dir:
        create_snapshot(state, snapshots_dir, state_path)
    result = registry.call(
        "set_param",
        {"id": block_id, "name": parameter, "value": value},
        ctx,
    )
    if result.get("ok"):
        rendered = registry.call(
            "render_grc", {"path": state.project.grc_path}, ctx
        )
        if not rendered.get("ok"):
            return rendered
        state.project.flowgraph_version += 1
        ClaimStore(state).invalidate_by_version(state.project.flowgraph_version)
        result["path"] = rendered.get("path")
        result["flowgraph_version"] = state.project.flowgraph_version
    return result


def make_task_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
