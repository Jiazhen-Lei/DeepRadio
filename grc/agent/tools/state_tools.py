"""Tools that bridge agent requests to SharedState."""

from __future__ import annotations

import copy
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from ..knowledge import recipes
from ..runtime_config import rf_runtime_enabled
from ..state import (
    ALLOW,
    CONFIRM,
    PROPOSE,
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
        raise RuntimeError("ToolContext has no SharedState attached")
    return state


def resolve_confirmation_decision(
    ctx: ToolContext, *, approved: bool
) -> Dict[str, Any]:
    """Synchronize a structured GUI decision with the Policy pending record."""
    state = _state(ctx)
    if not state.coordination.pending_confirmations:
        return {"ok": True, "resolved": False}
    pending = state.coordination.pending_confirmations[-1]
    if approved:
        pending["approved"] = True
    else:
        state.coordination.pending_confirmations.pop()
    return {"ok": True, "resolved": True, "approved": bool(approved)}


def looks_like_task_dump(text: str) -> bool:
    """True when *text* is a TaskCard / USER DECISIONS dump, not a user goal."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("TaskCard") or "当前 Stage TaskCard" in stripped:
        return True
    if "USER DECISIONS" in stripped.upper():
        return True
    return len(stripped) > 240


_EVM_THRESHOLD_RE = re.compile(
    r"(?:evm)\s*(?:要|需|必须|应)?\s*(?:小于|低于|<|≤)\s*(\d+(?:\.\d+)?)\s*%?"
)


def ensure_success_condition_claims(
    state, conditions: List[str], *, keep_verbatim: bool = True
) -> List[str]:
    """Register success conditions and derive evaluable Claims from EVM thresholds.

    Shared by the legacy ``commit_intent`` tool path and the workflow
    intent-sync path so "EVM < x%" always maps to an ``evm_lt_x`` sim-layer
    claim regardless of which path produced the intent. Non-EVM conditions
    are recorded verbatim when *keep_verbatim* is set.
    """
    claim_ids: List[str] = []
    for raw in conditions or []:
        text = str(raw or "").strip()
        if not text:
            continue
        match = _EVM_THRESHOLD_RE.search(text.lower())
        if match:
            threshold = float(match.group(1))
            condition = f"EVM < {threshold:g}%"
        else:
            if not keep_verbatim:
                continue
            condition = text
        if condition not in state.spec.success_conditions:
            state.spec.success_conditions.append(condition)
        if not match:
            continue
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
    return claim_ids


def commit_intent(ctx: ToolContext, text: str) -> Dict[str, Any]:
    """Project the canonical LLM-owned SharedIntent into the legacy Spec view.

    This tool deliberately performs no text interpretation.  Natural-language
    understanding belongs to ``IntentAlignmentCoordinator``; accepting raw
    text here used to create a second rule-derived intent that could disagree
    with the shared intent and mistake confirmation turns for new goals.
    """
    state = _state(ctx)
    shared = state.intent
    if shared.status == "idle" or not shared.intent_id:
        return {
            "ok": False,
            "decisions": [],
            "rejected_locked": [],
            "claims": [],
            "open_questions": list(state.spec.open_questions),
            "error": "No canonical SharedIntent is available; run LLM intent alignment first",
        }
    for goal in shared.goals or ([shared.raw_text] if shared.raw_text else []):
        if goal and goal not in state.spec.goals and not looks_like_task_dump(goal):
            state.spec.goals.append(str(goal))
    changed = []
    for key, value in dict(shared.parameters or {}).items():
        if value in (None, "", []):
            continue
        source = str(shared.parameter_sources.get(key) or "shared_intent")
        existing = next((item for item in state.spec.decisions if item.key == key), None)
        if existing is None:
            state.spec.decisions.append(Decision(key, value, source))
        else:
            existing.value = value
            existing.source = source
        changed.append(key)
    claim_ids = ensure_success_condition_claims(
        state, list(shared.success_criteria or []), keep_verbatim=True
    )
    state.spec.open_questions = list(shared.missing_fields or [])
    return {
        "ok": True,
        "decisions": changed,
        "proposed": [],
        "rejected_locked": [],
        "claims": claim_ids,
        "open_questions": list(state.spec.open_questions),
    }


def apply_proposed_decisions(state, proposed: List[Dict[str, Any]]) -> None:
    for item in proposed or []:
        key = str(item.get("key") or "")
        if not key:
            continue
        existing = next((d for d in state.spec.decisions if d.key == key), None)
        if existing:
            existing.value = item.get("value")
            existing.source = str(item.get("source") or "user")
        else:
            state.spec.decisions.append(
                Decision(
                    key=key,
                    value=item.get("value"),
                    source=str(item.get("source") or "user"),
                )
            )


def verify_state_claims(ctx: ToolContext, metrics: Dict[str, Any]) -> Dict[str, Any]:
    state = _state(ctx)
    store = ClaimStore(state)
    updated = []
    for claim in state.claims:
        if claim.layer != "sim":
            continue
        statement = claim.statement.upper()
        match = re.search(r"<\s*(\d+(?:\.\d+)?)", claim.statement)
        if not match:
            continue
        threshold = float(match.group(1))
        if statement.startswith("EVM"):
            value = metrics.get("evm_pct")
            metric_name = "evm_pct"
            artifact_key = "constellation_png"
        elif statement.startswith("BER"):
            value = metrics.get("ber")
            metric_name = "ber"
            artifact_key = ""
        else:
            continue
        if value is None:
            continue
        report_key = "evm_report" if metric_name == "evm_pct" else "ber_report"
        report = metrics.get(report_key)
        measurement_id = str(
            (report or {}).get("measurement_id")
            if isinstance(report, dict) else ""
        ) or str(metrics.get("measurement_id") or ctx.extra.get("measurement_id") or "")
        evidence = Evidence(
            test="read_metric",
            observation={
                "metric": metric_name,
                "value": float(value),
                "operator": "<",
                "threshold": threshold,
            },
            project_version=state.project.flowgraph_version,
            artifact=str(ctx.extra.get("artifacts", {}).get(artifact_key, "")),
            measurement_id=measurement_id,
            evidence_grade="system_measurement",
        )
        store.add_evidence(claim.id, evidence, passed=float(value) < threshold)
        updated.append(claim.id)
    return {"ok": True, "updated": updated, "claims": store.summary()}


@tool(
    name="spec_clarify",
    description="Inspect missing radio specification fields without writing them.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    group="state",
)
def spec_clarify(ctx: ToolContext, text: str = ""):
    state = _state(ctx)
    shared = state.intent
    open_questions = list(shared.missing_fields or []) if shared.status != "idle" else [
        "Run LLM intent alignment before inspecting missing fields."
    ]
    return {
        "ok": True,
        "open_questions": open_questions,
        "complete": not open_questions,
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
    effect_level="ARTIFACT_WRITE",
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
    """Resolve a catalog recipe from the caller's intent phrase.

    The agent should pass ``intent`` (its own reading of the goal) or an
    explicit ``recipe`` id.  Falling back to the raw user text keeps older
    call sites working, but it is reported through ``intent_source`` so a
    lexical match never looks like an LLM decision in the event stream.
    """
    supplied = (intent or "").strip()
    intent = supplied or str(ctx.extra.get("user_text") or "")
    selected = recipes.resolve_recipe(intent=intent, recipe=recipe)
    if selected is None:
        return {"ok": False, "error": f"Unknown recipe: {recipe}"}
    return {
        "ok": True,
        "recipe": selected.name,
        "title": selected.title,
        "intent_source": (
            "recipe_id" if recipe else ("agent" if supplied else "raw_user_text")
        ),
    }


@tool(
    name="verify_claims",
    description="Bind current simulation metrics to pending claims.",
    parameters={"type": "object", "properties": {}},
    group="state",
    effect_level="ARTIFACT_WRITE",
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
    origin="deepradio_state",
    runtime="shared_state",
    effect_level="ARTIFACT_WRITE",
)
def configure_sdr(
    ctx: ToolContext,
    device_type: str,
    center_freq: Optional[float] = None,
    sample_rate: Optional[float] = None,
):
    if ctx.extra.get("mutation_forbidden"):
        return {
            "ok": False,
            "error": "Flowgraph changes are disabled for this read-only request.",
            "policy": "DENY",
        }
    if center_freq is None or sample_rate is None:
        return {
            "ok": False,
            "outcome": "failed",
            "error": "SDR configuration requires center frequency and sample rate.",
        }
    state = _state(ctx)
    policy = gate(
        {
            "target": "device",
            "scope": "configuration",
            "domain": "hardware",
        },
        state.coordination,
    )
    if policy != ALLOW and not _workflow_checkpoint_approved(
        ctx, "hardware_confirmation", "rf_plan_confirmation"
    ):
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
        "configuration_mode": "recorded",
        "mode": "configuration_recorded",
    }
    return {"ok": True, "device": state.project.config["device"]}


@tool(
    name="hardware_preflight",
    description=(
        "Check host-side SDR prerequisites: requested parameters, local driver "
        "CLI availability, and RF feature enablement. This does not discover "
        "or probe a physical device."
    ),
    parameters={
        "type": "object",
        "properties": {"device_type": {"type": "string"}},
    },
    group="hardware",
    origin="deepradio_state",
    runtime="shared_state",
    effect_level="DEVICE_READ",
)
def hardware_preflight(ctx: ToolContext, device_type: str = ""):
    state = _state(ctx)
    configured = dict(state.project.config.get("device") or {})
    workflow_slots = dict(
        ((ctx.extra.get("workflow") or {}).get("intent") or {}).get("slots") or {}
    )
    requested = (device_type or configured.get("type") or "").lower()
    center_freq = configured.get("center_freq")
    if center_freq is None:
        center_freq = workflow_slots.get("carrier_frequency")
    sample_rate = configured.get("sample_rate")
    if sample_rate is None:
        sample_rate = workflow_slots.get("sample_rate")
    if requested in ("usrp", "b210", "b200") or "b210" in requested or "usrp" in requested:
        driver = "uhd_find_devices"
    elif "pluto" in requested:
        driver = "iio_info"
    else:
        driver = ""
    checks = {
        "device_type_present": bool(requested),
        "driver_command_available": bool(driver and shutil.which(driver)),
        "center_frequency_present": center_freq is not None,
        "sample_rate_present": sample_rate is not None,
        "real_hardware_actions_enabled": (
            rf_runtime_enabled()
        ),
    }
    complete = all(
        checks[name]
        for name in (
            "device_type_present",
            "driver_command_available",
            "center_frequency_present",
            "sample_rate_present",
        )
    )
    return {
        "ok": complete,
        "outcome": "passed" if complete else "failed",
        "device_type": requested,
        "readiness_scope": "host_environment",
        "physical_device_checked": False,
        "checks": checks,
        "system_capabilities": {
            "rf_runtime": {
                "available": checks["real_hardware_actions_enabled"],
                "requires_restart": False,
                "environment_key": "GRC_AGENT_ENABLE_RF",
                "persistent_preference": "rf_runtime_enabled",
            }
        },
        "missing": [name for name, value in checks.items() if not value and name != "real_hardware_actions_enabled"],
        "note": (
            "Host configuration and driver readiness checks passed; RF runtime is enabled."
            if checks["real_hardware_actions_enabled"]
            else "Host configuration and driver readiness checks passed; RF runtime is not enabled."
        ),
    }


@tool(
    name="apply_grc_diff",
    description="Apply a single-block parameter change through deterministic tools. Modulation/constellation changes are rejected; use design_flowgraph with a new recipe instead.",
    parameters={
        "type": "object",
        "properties": {
            "block_id": {"type": "string"},
            "parameter": {"type": "string"},
            "value": {},
            "resimulate": {
                "type": "boolean",
                "description": "改参成功后是否自动仿真并绑定 Claim,默认 true",
            },
        },
        "required": ["block_id", "parameter", "value"],
    },
    group="build",
    effect_level="ARTIFACT_WRITE",
)
def apply_grc_diff(
    ctx: ToolContext,
    block_id: str,
    parameter: str,
    value: Any,
    resimulate: bool = True,
):
    from . import registry

    if ctx.extra.get("mutation_forbidden"):
        return {"ok": False, "error": "Flowgraph changes are disabled for this request."}
    state = _state(ctx)
    policy = gate(
        {
            "target": parameter,
            "block_id": block_id,
            "scope": "single_block_change",
            "domain": "dsp",
        },
        state.coordination,
    )
    if policy != ALLOW:
        return {
            "ok": False,
            "policy": policy,
            "requires_confirmation": policy in (PROPOSE, CONFIRM),
            "error": (
                "调制/星座变更必须走 design_flowgraph 换 recipe,并等待用户确认;"
                "禁止用 apply_grc_diff 改 const_points/type/sym_map。"
            ),
        }
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
        if resimulate:
            result["reverify"] = _resimulate_and_verify(ctx, state)
    return result


@tool(
    name="apply_flowgraph_patch",
    description="Atomically apply add/remove/set/connect/disconnect operations, then validate and save; restore the in-memory graph on any failure.",
    parameters={
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {"type": "object"},
                "maxItems": 100,
            },
            "preconditions": {
                "type": "array",
                "items": {},
            },
            "resimulate": {"type": "boolean"},
        },
        "required": ["operations"],
    },
    group="build",
    effect_level="ARTIFACT_WRITE",
)
def apply_flowgraph_patch(
    ctx: ToolContext,
    operations: List[Dict[str, Any]],
    preconditions: Optional[List[Any]] = None,
    resimulate: bool = True,
):
    from . import registry

    if ctx.extra.get("mutation_forbidden"):
        return {"ok": False, "error": "Flowgraph changes are disabled for this request."}
    if ctx.flow_graph is None:
        return {"ok": False, "error": "当前 session 没有已加载的流图"}
    expanded, expand_error = expand_patch_operations(operations)
    if expand_error:
        return {"ok": False, "error": expand_error}
    operations = expanded
    if not isinstance(operations, list) or not operations or len(operations) > 100:
        return {"ok": False, "error": "operations 必须是 1~100 项的列表"}
    allowed = {"add", "remove", "set", "connect", "disconnect"}
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or operation.get("op") not in allowed:
            return {"ok": False, "error": f"operations[{index}] 的 op 非法"}
    pre_error = check_patch_preconditions(ctx, preconditions or [])
    if pre_error:
        return {"ok": False, "error": pre_error}

    state = _state(ctx)
    scope = (
        "single_block_change"
        if len(operations) == 1 and operations[0].get("op") == "set"
        else "multi_block_change"
    )
    policy = gate(
        {"target": "flowgraph", "scope": scope, "domain": "dsp"},
        state.coordination,
    )
    if policy == "DENY":
        return {"ok": False, "policy": policy, "error": "PolicyGateway 拒绝 patch"}
    if policy != ALLOW and not _workflow_checkpoint_approved(
        ctx, "change_confirmation", "repair_confirmation"
    ):
        return {
            "ok": False,
            "policy": policy,
            "requires_confirmation": True,
            "error": "多块 patch 需要 Workflow Checkpoint 批准",
        }
    backup = copy.deepcopy(ctx.flow_graph.export_data())
    state_path = str(ctx.extra.get("state_path") or "")
    snapshots_dir = str(ctx.extra.get("snapshots_dir") or "")
    if state_path and snapshots_dir:
        create_snapshot(state, snapshots_dir, state_path)

    def restore_graph():
        restored = ctx.platform.make_flow_graph()
        restored.import_data(copy.deepcopy(backup))
        ctx.flow_graph = restored
        ctx.blocks = {
            str(block.name): block
            for block in restored.blocks
            if block is not restored.options_block
        }

    applied = []
    for index, operation in enumerate(operations):
        op = operation["op"]
        if op == "add":
            result = registry.call("add_block", {
                "key": operation.get("key"),
                "id": operation.get("id"),
                "params": operation.get("params") or {},
            }, ctx)
        elif op == "set":
            result = registry.call("set_param", {
                "id": operation.get("id"),
                "name": operation.get("name"),
                "value": operation.get("value"),
            }, ctx)
        elif op == "connect":
            result = registry.call("connect", {
                "src_id": operation.get("src_id"),
                "src_port": int(operation.get("src_port", 0)),
                "dst_id": operation.get("dst_id"),
                "dst_port": int(operation.get("dst_port", 0)),
            }, ctx)
        elif op == "remove":
            block_id = str(operation.get("id") or "")
            block = ctx.blocks.get(block_id)
            if block is None:
                result = {"ok": False, "error": f"块 id 不存在: {block_id}"}
            else:
                ctx.flow_graph.remove_element(block)
                ctx.blocks.pop(block_id, None)
                result = {"ok": True, "id": block_id}
        else:
            result = _disconnect_exact(ctx, operation)
        if not result.get("ok"):
            restore_graph()
            return {
                "ok": False,
                "error": f"Patch item {index + 1} failed: {result.get('error')}",
                "rolled_back": True,
            }
        applied.append({"op": op, "result": result})

    validation = registry.call("validate_flowgraph", {}, ctx)
    if not validation.get("ok") or not validation.get("valid"):
        restore_graph()
        return {
            "ok": False,
            "error": "Structural validation failed after applying the patch.",
            "errors": validation.get("errors") or [],
            "rolled_back": True,
        }

    target = str(state.project.grc_path or "")
    if not target:
        flowgraph_id = ctx.flow_graph.get_option("id") or "flow_graph"
        target = os.path.join(ctx.out_dir or os.getcwd(), f"{flowgraph_id}.grc")
    temp = f"{target}.patch.tmp.grc"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        ctx.platform.save_flow_graph(temp, ctx.flow_graph)
        os.replace(temp, target)
    except Exception as exc:  # noqa: BLE001
        restore_graph()
        if os.path.isfile(temp):
            os.unlink(temp)
        return {"ok": False, "error": f"Failed to save the patch: {exc}", "rolled_back": True}

    state.project.grc_path = target
    state.project.flowgraph_version += 1
    state.project.config["canvas_dirty"] = False
    ClaimStore(state).invalidate_by_version(state.project.flowgraph_version)
    reverify = _resimulate_and_verify(ctx, state) if resimulate else {
        "ok": True, "skipped": True,
    }
    return {
        "ok": True,
        "outcome": "passed",
        "path": target,
        "flowgraph_version": state.project.flowgraph_version,
        "applied": applied,
        "reverify": reverify,
        "affected_claims_evaluated": bool(reverify.get("ok")),
    }


def expand_patch_operations(
    operations: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], str]:
    """Normalize GraphPatch IR aliases into executable graph ops."""
    if not isinstance(operations, list) or not operations:
        return [], "operations 必须是 1~100 项的列表"
    expanded: List[Dict[str, Any]] = []
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            return [], f"operations[{index}] 不是对象"
        operation = dict(raw)
        op = str(operation.get("op") or "")
        if op == "set_param":
            operation["op"] = "set"
            operation.setdefault("id", operation.get("block") or operation.get("block_id"))
            operation.setdefault("name", operation.get("key") or operation.get("parameter"))
        elif op == "replace_block":
            old_id = str(operation.get("old") or operation.get("id") or "")
            new_id = str(operation.get("new") or operation.get("new_id") or old_id)
            key = str(operation.get("key") or operation.get("new_key") or "")
            if not old_id or not key:
                return [], f"operations[{index}] replace_block 需要 old 与 key"
            expanded.append({"op": "remove", "id": old_id})
            expanded.append({
                "op": "add",
                "id": new_id,
                "key": key,
                "params": dict(operation.get("params") or {}),
            })
            continue
        elif op == "connect":
            operation.setdefault("src_id", operation.get("src") or operation.get("source"))
            operation.setdefault("dst_id", operation.get("dst") or operation.get("destination"))
        elif op == "set":
            operation.setdefault("id", operation.get("block") or operation.get("block_id"))
            operation.setdefault("name", operation.get("key") or operation.get("parameter"))
        expanded.append(operation)
        if len(expanded) > 100:
            return [], "operations 必须是 1~100 项的列表"
    return expanded, ""


def check_patch_preconditions(ctx: ToolContext, preconditions: List[Any]) -> str:
    for index, item in enumerate(preconditions or []):
        if item in (None, "", True):
            continue
        if isinstance(item, str):
            if item not in (ctx.blocks or {}):
                return f"preconditions[{index}] 缺少块 {item}"
            continue
        if not isinstance(item, dict):
            return f"preconditions[{index}] 非法"
        block_id = str(item.get("block") or item.get("id") or "")
        param = str(item.get("param") or item.get("name") or item.get("key") or "")
        if block_id and block_id not in (ctx.blocks or {}):
            return f"preconditions[{index}] 缺少块 {block_id}"
        if block_id and param:
            block = (ctx.blocks or {}).get(block_id)
            params = getattr(block, "params", None) or {}
            if param not in params:
                return f"preconditions[{index}] 块 {block_id} 无参数 {param}"
    return ""


def _disconnect_exact(ctx: ToolContext, operation: Dict[str, Any]) -> Dict[str, Any]:
    src_id = str(operation.get("src_id") or "")
    dst_id = str(operation.get("dst_id") or "")
    src_port = str(operation.get("src_port", 0))
    dst_port = str(operation.get("dst_port", 0))
    match = next((
        connection for connection in ctx.flow_graph.connections
        if str(connection.source_block.name) == src_id
        and str(connection.sink_block.name) == dst_id
        and str(connection.source_port.key) == src_port
        and str(connection.sink_port.key) == dst_port
    ), None)
    if match is None:
        return {"ok": False, "error": "指定连接不存在"}
    ctx.flow_graph.remove_element(match)
    return {"ok": True, "connection": f"{src_id}[{src_port}] -> {dst_id}[{dst_port}]"}


def _workflow_checkpoint_approved(ctx: ToolContext, *stage_ids: str) -> bool:
    workflow = ctx.extra.get("workflow") or {}
    return any(
        (stage.get("checkpoint") or {}).get("decision_status") == "approved"
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict) and stage.get("id") in stage_ids
    )


def _resimulate_and_verify(ctx: ToolContext, state) -> Dict[str, Any]:
    """改参后重跑仿真并把新指标绑到当前版本的 Claim。"""
    from . import registry

    sim = registry.call("run_simulation", {}, ctx)
    if not sim.get("ok"):
        return {"ok": False, "error": sim.get("error") or "The verification simulation failed."}
    ctx.extra.setdefault("artifacts", {})
    if sim.get("out_dir"):
        ctx.extra["artifacts"]["out_dir"] = sim["out_dir"]
    recipe_name = str(state.project.config.get("recipe") or "")
    selected = recipes.get_recipe(recipe_name)
    want_evm = selected is None or "evm" in (selected.metrics or [])
    modulation = ""
    for block in (ctx.blocks or {}).values():
        params = getattr(block, "params", None) or {}
        for name in ("type", "constellation"):
            param = params.get(name)
            try:
                raw = str(param.get_value()).lower()
            except AttributeError:
                raw = ""
            found = next(
                (item for item in ("bpsk", "qpsk", "ofdm", "gfsk") if item in raw),
                "",
            )
            if found:
                modulation = found
                break
        if modulation:
            break
    modulation = modulation or str(state.project.config.get("modulation") or "bpsk")
    notes = []
    if want_evm:
        metric = registry.call(
            "read_metric",
            {"kind": "evm", "modulation": modulation, "sps": 4},
            ctx,
        )
        if metric.get("ok") and metric.get("value") is not None:
            ctx.extra.setdefault("metrics", {})["evm_pct"] = metric["value"]
            plot = registry.call("plot_constellation", {"sps": 4}, ctx)
            if plot.get("ok") and plot.get("path"):
                ctx.extra["artifacts"]["constellation_png"] = plot["path"]
        else:
            notes.append(metric.get("error") or "无 EVM")
    bound = verify_state_claims(ctx, ctx.extra.get("metrics", {}))
    out = {
        "ok": True,
        "evm_pct": (ctx.extra.get("metrics") or {}).get("evm_pct"),
        "modulation": modulation,
        "measurement_id": str(ctx.extra.get("measurement_id") or ""),
        "claims": bound.get("updated", []),
    }
    if notes:
        out["note"] = "; ".join(notes)
    return out
