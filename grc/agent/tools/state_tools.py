"""Tools that bridge agent requests to SharedState."""

from __future__ import annotations

import copy
import os
import re
import shutil
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from ..knowledge import recipes
from ..state import (
    Claim,
    ClaimStore,
    Evidence,
    SharedIntent,
    SpecificationField,
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


_EVM_THRESHOLD_RE = re.compile(
    r"(?:evm)\s*(?:要|需|必须|应)?\s*(?:小于|低于|<|≤)\s*(\d+(?:\.\d+)?)\s*%?"
)


def ensure_success_condition_claims(state, conditions: List[str]) -> List[str]:
    """Derive evaluable Claims from aligned success conditions."""
    claim_ids: List[str] = []
    for raw in conditions or []:
        text = str(raw or "").strip()
        if not text:
            continue
        match = _EVM_THRESHOLD_RE.search(text.lower())
        if not match:
            continue
        threshold = float(match.group(1))
        condition = f"EVM < {threshold:g}%"
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
    name="spec_update",
    description=(
        "Merge a SpecAgent-authored Radio Specification patch and return Required "
        "fields that are not aligned. Each field needs key, label, value, group, "
        "source and status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                        "value": {},
                        "group": {"type": "string", "enum": ["required", "added"]},
                        "source": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["aligned", "needs_confirmation", "missing"],
                        },
                    },
                    "required": ["key"],
                },
            },
            "remove_fields": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "object"},
            "assumptions": {"type": "array", "items": {"type": "object"}},
        },
    },
    group="state",
    permission="project.write",
)
def spec_update(
    ctx: ToolContext,
    fields: Optional[List[Dict[str, Any]]] = None,
    remove_fields: Optional[List[str]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    assumptions: Optional[List[Dict[str, Any]]] = None,
):
    state = _state(ctx)
    shared = state.intent
    if shared.status == "idle" or not shared.intent_id:
        workflow = dict(ctx.extra.get("workflow") or {})
        previous_specification = shared.specification
        shared = SharedIntent.new(
            str(ctx.extra.get("user_text") or ""),
            workflow_id=str(workflow.get("workflow_id") or ""),
        )
        shared.specification = previous_specification
        state.intent = shared

    candidate = copy.deepcopy(shared.specification)
    by_key = {item.key: item for item in candidate.fields}
    changed_fields: List[str] = []
    for raw in fields or []:
        if not isinstance(raw, dict) or not str(raw.get("key") or "").strip():
            return {"ok": False, "error": "Every Specification field needs a key"}
        key = str(raw["key"]).strip()
        if key not in by_key:
            missing_keys = [
                name for name in ("label", "value", "group", "source", "status")
                if name not in raw
            ]
            if missing_keys:
                return {
                    "ok": False,
                    "error": f"New field {key} is missing: {', '.join(missing_keys)}",
                }
        if "group" in raw and raw.get("group") not in {"required", "added"}:
            return {"ok": False, "error": f"Invalid group for {key}: {raw.get('group')}"}
        if "status" in raw and raw.get("status") not in {
            "aligned", "needs_confirmation", "missing",
        }:
            return {"ok": False, "error": f"Invalid status for {key}: {raw.get('status')}"}
        base = asdict(by_key[key]) if key in by_key else {}
        item = SpecificationField.from_dict({**base, **raw, "key": key})
        if key not in by_key or asdict(by_key[key]) != asdict(item):
            by_key[key] = item
            changed_fields.append(key)
    for key in remove_fields or []:
        key = str(key or "").strip()
        if key in by_key:
            del by_key[key]
            changed_fields.append(key)
    candidate.fields = list(by_key.values())

    metadata_changed = False
    if constraints is not None and dict(constraints) != candidate.constraints:
        candidate.constraints = dict(constraints)
        metadata_changed = True
    if assumptions is not None and list(assumptions) != candidate.assumptions:
        candidate.assumptions = list(assumptions)
        metadata_changed = True

    errors = candidate.validate()
    if errors:
        return {"ok": False, "error": "Invalid Radio Specification", "errors": errors}
    if changed_fields or metadata_changed or candidate.revision < 1:
        candidate.revision = max(1, candidate.revision + 1)
    candidate.validation_errors = []
    shared.specification = candidate
    unresolved = candidate.unresolved_fields()
    shared.status = "awaiting_input" if unresolved else "draft"
    shared.refresh_hash()
    if changed_fields or metadata_changed:
        shared.record_patch(
            changed_fields=changed_fields,
            scope="radio_specification",
            source="spec_update",
        )
    return {
        "ok": True,
        "status": "awaiting_input" if unresolved else "ready",
        "spec_revision": candidate.revision,
        "changed_fields": list(dict.fromkeys(changed_fields)),
        "unresolved_fields": unresolved,
        "specification": candidate.to_dict(),
    }


@tool(
    name="spec_commit",
    description="Commit the current Radio Specification only when all Required fields are aligned.",
    parameters={"type": "object", "properties": {}},
    group="state",
    permission="project.write",
)
def spec_commit(ctx: ToolContext):
    state = _state(ctx)
    shared = state.intent
    if shared.status == "idle" or not shared.intent_id:
        return {"ok": False, "error": "No Radio Specification is available"}
    specification = shared.specification
    errors = specification.validate()
    unresolved = specification.unresolved_fields()
    specification.validation_errors = list(errors)
    if errors or unresolved:
        shared.status = "awaiting_input"
        shared.refresh_hash()
        return {
            "ok": False,
            "status": "awaiting_input",
            "error": "Radio Specification is not aligned",
            "errors": errors,
            "unresolved_fields": unresolved,
            "spec_revision": specification.revision,
        }
    success = specification.field("success_conditions")
    criteria = success.value if success is not None else []
    if not isinstance(criteria, list):
        criteria = [criteria] if criteria not in (None, "") else []
    claim_ids = ensure_success_condition_claims(
        state, [str(item) for item in criteria]
    )
    shared.status = "confirmed"
    shared.confirmed_at = time.time()
    shared.refresh_hash()
    return {
        "ok": True,
        "status": "aligned",
        "spec_revision": specification.revision,
        "changed_fields": [],
        "unresolved_fields": [],
        "claims": claim_ids,
    }


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
    permission="project.write",
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
    permission="project.write",
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
        "CLI availability, and configuration readiness. This does not discover "
        "or probe a physical device."
    ),
    parameters={
        "type": "object",
        "properties": {"device_type": {"type": "string"}},
    },
    group="hardware",
    origin="deepradio_state",
    runtime="shared_state",
    permission="device.read",
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
        "missing": [name for name, value in checks.items() if not value],
        "note": "Host configuration and driver readiness checks passed.",
    }


@tool(
    name="apply_grc_diff",
    description="Apply and save one deterministic block-parameter change without simulation or verification.",
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
    permission="project.write",
)
def apply_grc_diff(
    ctx: ToolContext,
    block_id: str,
    parameter: str,
    value: Any,
):
    from . import registry

    if ctx.extra.get("mutation_forbidden"):
        return {"ok": False, "error": "Flowgraph changes are disabled for this request."}
    state = _state(ctx)
    if ctx.flow_graph is None:
        return {
            "ok": False,
            "error": "当前 session 没有内存流图；请先构建或加载工程",
        }
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
        result["grc_path"] = rendered.get("path")
        result["flowgraph_version"] = state.project.flowgraph_version
        ctx.extra.setdefault("artifacts", {})["grc_path"] = rendered.get("path")
    return result


@tool(
    name="apply_flowgraph_patch",
    description="Atomically apply and save Flowgraph operations without simulation or Stage verification; restore the graph on an invalid patch.",
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
        },
        "required": ["operations"],
    },
    group="build",
    permission="project.write",
)
def apply_flowgraph_patch(
    ctx: ToolContext,
    operations: List[Dict[str, Any]],
    preconditions: Optional[List[Any]] = None,
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
    backup = copy.deepcopy(ctx.flow_graph.export_data())

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
    ctx.extra.setdefault("artifacts", {})["grc_path"] = target
    return {
        "ok": True,
        "outcome": "passed",
        "path": target,
        "grc_path": target,
        "flowgraph_version": state.project.flowgraph_version,
        "applied": applied,
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
