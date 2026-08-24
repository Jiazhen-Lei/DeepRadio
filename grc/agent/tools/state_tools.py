"""Tools that bridge agent requests to SharedState."""

from __future__ import annotations

import copy
import os
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional

from ..knowledge import recipes
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

_CONFIRM_TEXTS = frozenset({"确认", "同意", "继续", "approve", "确认执行", "确认修改", "同意修改", "继续执行"})
_CANCEL_HINTS = (
    "取消修改", "拒绝修改", "不要执行", "不要继续", "不确认", "不同意", "cancel",
)
_READ_ONLY_HINTS = (
    "先不要修改", "先不要改", "不要修改", "不要改图", "只诊断", "只分析", "先别改",
)


def _state(ctx: ToolContext):
    state = ctx.extra.get("state")
    if state is None:
        raise RuntimeError("ToolContext 未挂载 SharedState")
    return state


def is_confirmation_utterance(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in _CONFIRM_TEXTS or any(
        hint in (text or "") for hint in _CANCEL_HINTS
    )


def is_read_only_request(text: str) -> bool:
    raw = text or ""
    return any(hint in raw for hint in _READ_ONLY_HINTS)


def _guess_modulation(recipe_name: str) -> str:
    n = (recipe_name or "").lower()
    if "qpsk" in n:
        return "qpsk"
    if "bpsk" in n:
        return "bpsk"
    if "ofdm" in n:
        return "ofdm"
    return ""


_SWITCH_RE = re.compile(
    r"(?:改成|换成|改为|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s*"
    r"([A-Za-z0-9_\u4e00-\u9fff]+)",
    flags=re.IGNORECASE,
)
_MOD_TO_RECIPE = {
    "qpsk": "qpsk_awgn",
    "bpsk": "bpsk_awgn",
    "ofdm": "ofdm_awgn",
}


def _parse_switch_target(text: str) -> Optional[str]:
    """从「改成/换成 …」里解析目标 recipe；解析不到则返回 None。"""
    match = _SWITCH_RE.search(text or "")
    if not match:
        return None
    token = match.group(1).lower().strip("，。,. ")
    if token in recipes.RECIPES:
        return token
    for key, name in _MOD_TO_RECIPE.items():
        if key in token:
            return name
    return None


def detect_recipe_switch(state, text: str) -> Optional[str]:
    """若用户明确要求把当前工程换成另一配方，返回新 recipe 名。"""
    if not (state.project.grc_path or state.project.config.get("recipe")):
        return None
    target = _parse_switch_target(text)
    if not target:
        return None
    current = str(state.project.config.get("recipe") or "")
    current_mod = str(
        state.project.config.get("modulation") or _guess_modulation(current)
    )
    target_mod = _guess_modulation(target)
    if target == current:
        return None
    if target_mod and current_mod and target_mod == current_mod:
        return None
    return target


def redundant_recipe_switch(state, text: str) -> Optional[str]:
    """用户要换成的调制/配方已是当前工程时，返回给用户看的名称。"""
    target = _parse_switch_target(text)
    if not target:
        return None
    current = str(state.project.config.get("recipe") or "")
    current_mod = str(
        state.project.config.get("modulation") or _guess_modulation(current)
    )
    target_mod = _guess_modulation(target)
    if target == current or (
        target_mod and current_mod and target_mod == current_mod
    ):
        return (current_mod or target).upper()
    return None


def resolve_confirmation(ctx: ToolContext, text: str) -> Dict[str, Any]:
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
    if affirm:
        return resolve_confirmation_decision(ctx, approved=True)
    if reject:
        return resolve_confirmation_decision(ctx, approved=False)
    return {"ok": True, "resolved": False}


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


def commit_intent(ctx: ToolContext, text: str) -> Dict[str, Any]:
    """Deterministically extract the minimum traceable radio specification."""
    state = _state(ctx)
    if is_confirmation_utterance(text):
        return {
            "ok": True,
            "decisions": [],
            "rejected_locked": [],
            "claims": [],
            "open_questions": list(state.spec.open_questions),
            "skipped": "confirmation",
        }
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
    current_mod = str(state.project.config.get("modulation") or known_modulation or "")
    channel = "awgn" if "awgn" in lowered or "噪声" in text else ""
    decisions = []
    proposed = []
    if modulation:
        decision = Decision("modulation", modulation, "user")
        if current_mod and current_mod != modulation:
            proposed.append(decision)
        else:
            decisions.append(decision)
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
    if proposed:
        ctx.extra["proposed_decisions"] = [
            {"key": d.key, "value": d.value, "source": d.source}
            for d in proposed
        ]

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
        "proposed": [d.key for d in proposed],
        "rejected_locked": rejected,
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
    intent = (intent or "").strip() or str(ctx.extra.get("user_text") or "")
    selected = recipes.resolve_recipe(intent=intent, recipe=recipe)
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
    if ctx.extra.get("mutation_forbidden"):
        return {
            "ok": False,
            "error": "本轮禁止改图（用户要求只诊断/先不要修改）",
            "policy": "DENY",
        }
    if center_freq is None or sample_rate is None:
        return {
            "ok": False,
            "outcome": "failed",
            "error": "SDR 配置需要中心频率和采样率",
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
    name="hardware_preflight",
    description="Read-only SDR configuration and local-driver precheck. Never starts a flowgraph or transmits RF.",
    parameters={
        "type": "object",
        "properties": {"device_type": {"type": "string"}},
    },
    group="hardware",
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
            os.environ.get("GRC_AGENT_ENABLE_RF") == "1"
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
        "checks": checks,
        "missing": [name for name, value in checks.items() if not value and name != "real_hardware_actions_enabled"],
        "note": (
            "配置与驱动只读预检完成；RF 运行功能已由系统管理员启用。"
            if checks["real_hardware_actions_enabled"]
            else "配置与驱动只读预检完成；RF 运行功能尚未启用。"
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
        return {"ok": False, "error": "本轮禁止改图"}
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
            }
        },
        "required": ["operations"],
    },
    group="build",
)
def apply_flowgraph_patch(ctx: ToolContext, operations: List[Dict[str, Any]]):
    from . import registry

    if ctx.extra.get("mutation_forbidden"):
        return {"ok": False, "error": "本轮禁止改图"}
    if ctx.flow_graph is None:
        return {"ok": False, "error": "当前 session 没有已加载的流图"}
    if not isinstance(operations, list) or not operations or len(operations) > 100:
        return {"ok": False, "error": "operations 必须是 1~100 项的列表"}
    allowed = {"add", "remove", "set", "connect", "disconnect"}
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or operation.get("op") not in allowed:
            return {"ok": False, "error": f"operations[{index}] 的 op 非法"}

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
                "error": f"patch 第 {index + 1} 项失败: {result.get('error')}",
                "rolled_back": True,
            }
        applied.append({"op": op, "result": result})

    validation = registry.call("validate_flowgraph", {}, ctx)
    if not validation.get("ok") or not validation.get("valid"):
        restore_graph()
        return {
            "ok": False,
            "error": "patch 后结构校验失败",
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
        return {"ok": False, "error": f"patch 存盘失败: {exc}", "rolled_back": True}

    state.project.grc_path = target
    state.project.flowgraph_version += 1
    state.project.config["canvas_dirty"] = False
    ClaimStore(state).invalidate_by_version(state.project.flowgraph_version)
    return {
        "ok": True,
        "outcome": "passed",
        "path": target,
        "flowgraph_version": state.project.flowgraph_version,
        "applied": applied,
    }


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
        return {"ok": False, "error": sim.get("error") or "重仿真失败"}
    ctx.extra.setdefault("artifacts", {})
    if sim.get("out_dir"):
        ctx.extra["artifacts"]["out_dir"] = sim["out_dir"]
    recipe_name = str(state.project.config.get("recipe") or "")
    selected = recipes.get_recipe(recipe_name)
    want_evm = selected is None or "evm" in (selected.metrics or [])
    modulation = str(state.project.config.get("modulation") or "bpsk")
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
        "claims": bound.get("updated", []),
    }
    if notes:
        out["note"] = "; ".join(notes)
    return out


def make_task_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
