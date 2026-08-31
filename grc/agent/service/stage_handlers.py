"""Deterministic Stage handlers. Stage ids stay catalog-owned."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from ..schema import AgentReply
from ..tools.hardware_profiles import (
    device_args_for,
    normalize_hardware,
    resolve_hardware_profile,
)
from ..tools.registry import ToolContext
from . import session_store as _store

_Handler = Callable[..., AgentReply]
_HARDWARE_BUILD_STAGES = frozenset({
    "build_and_verify", "tx_build_and_validate",
    "rx_build_and_verify", "apply_and_verify",
})


def run_deterministic_stage(
    self,
    ctx: ToolContext,
    user_text: str,
    recipe: str,
    simulate: bool,
    stage_id: str,
) -> AgentReply:
    """Minimal deterministic handlers sharing the same Stage semantics as LLM."""
    _emit_route_events(self, stage_id)
    capabilities = set(
        self._workflow.workflow.intent.capabilities
        if self._workflow.workflow else []
    )
    if "hardware_configure" in capabilities and stage_id in _HARDWARE_BUILD_STAGES:
        if self._hardware_rx_spectrum_ready():
            return self._run_hardware_rx_spectrum(ctx)
        return self._run_hardware_endpoint_flowgraph(ctx)
    handler = _HANDLERS.get(stage_id)
    if handler is None:
        return self._fold(
            ctx, f"Stage {stage_id} has no deterministic change that can be executed safely.",
            source="deterministic-stage", ok=False,
        )
    return handler(self, ctx, user_text, recipe, simulate)


def _emit_route_events(self, stage_id: str) -> None:
    active = self._state.coordination.active_task
    payload = self._workflow_event_payload({
        "target_agent": active.target_agent if active else "stage_handler",
        "stage_id": stage_id,
        "mode": "deterministic",
        "executor": "deterministic_stage_handler",
    })
    _store.append_session_event(self.session_id, "stage_routed", payload)
    _store.append_session_event(
        self.session_id, "deterministic_handler_started", payload
    )


def _slots(self) -> dict:
    return (
        self._workflow.workflow.intent.slots
        if self._workflow.workflow else {}
    )


def _recipe_graph_patch(from_recipe: str, to_recipe: str) -> dict:
    """Compile a semantic recipe delta without replacing the whole canvas."""
    from ..knowledge import recipes

    before = recipes.get_recipe(from_recipe)
    after = recipes.get_recipe(to_recipe)
    if before is None or after is None:
        return {}
    old_blocks = {block_id: (key, dict(params)) for key, block_id, params in before.blocks}
    new_blocks = {block_id: (key, dict(params)) for key, block_id, params in after.blocks}
    operations = []
    def connection_key(item: Any) -> tuple[str, int, str, int]:
        values = tuple(item or ())
        if len(values) < 2:
            raise ValueError("A recipe connection requires at least src_id and dst_id")
        if len(values) >= 4:
            return (
                str(values[0]), int(values[2]),
                str(values[1]), int(values[3]),
            )
        return str(values[0]), 0, str(values[1]), 0

    old_connections = {connection_key(item) for item in before.connections}
    new_connections = {connection_key(item) for item in after.connections}
    for src, src_port, dst, dst_port in sorted(old_connections - new_connections):
        operations.append({
            "op": "disconnect", "src_id": src, "src_port": src_port,
            "dst_id": dst, "dst_port": dst_port,
        })
    for block_id in sorted(old_blocks.keys() - new_blocks.keys()):
        operations.append({"op": "remove", "id": block_id})
    for block_id in sorted(old_blocks.keys() & new_blocks.keys()):
        old_key, old_params = old_blocks[block_id]
        new_key, new_params = new_blocks[block_id]
        if old_key != new_key:
            operations.extend([
                {"op": "remove", "id": block_id},
                {"op": "add", "id": block_id, "key": new_key,
                 "params": new_params},
            ])
            continue
        for name, value in new_params.items():
            if str(old_params.get(name)) != str(value):
                operations.append({
                    "op": "set", "id": block_id, "name": name,
                    "value": value,
                })
    for block_id in sorted(new_blocks.keys() - old_blocks.keys()):
        key, params = new_blocks[block_id]
        operations.append({
            "op": "add", "id": block_id, "key": key, "params": params,
        })
    for src, src_port, dst, dst_port in sorted(new_connections - old_connections):
        operations.append({
            "op": "connect", "src_id": src, "src_port": src_port,
            "dst_id": dst, "dst_port": dst_port,
        })
    return {
        "operations": operations,
        "preconditions": sorted(old_blocks),
        "from_recipe": before.name,
        "to_recipe": after.name,
        "preserved_block_ids": sorted(
            set(old_blocks).intersection(new_blocks)
        ),
    }


def _bind_preview_identity(self, ctx: ToolContext, identity: str) -> None:
    from ..tools.hardware_tools import bind_endpoint_identity

    path = str(getattr(self._state.project, "grc_path", "") or "")
    flow_graph = getattr(ctx, "flow_graph", None)
    if not identity or flow_graph is None or not path:
        return
    if not bind_endpoint_identity(flow_graph, identity):
        return
    try:
        flow_graph.rewrite()
        ctx.platform.save_flow_graph(path, flow_graph)
    except Exception:  # noqa: BLE001
        return
    ctx.extra.setdefault("artifacts", {})["grc_path"] = path


def _handle_build(self, ctx, user_text, recipe, simulate) -> AgentReply:
    scope = str(_slots(self).get("signal_source_scope") or "")
    if scope:
        ctx.extra.setdefault("metrics", {})["signal_source_scope"] = scope
    return self._run_deterministic(ctx, user_text, recipe, simulate)


def _handle_apply(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    slots = _slots(self)
    target_recipe = str(slots.get("target_recipe") or "")
    graph_patch = slots.get("graph_patch")
    if graph_patch:
        payload = (
            graph_patch if isinstance(graph_patch, dict)
            else {"operations": graph_patch}
        )
        result = registry.call("apply_flowgraph_patch", {
            "operations": payload.get("operations") or [],
            "preconditions": payload.get("preconditions") or [],
            "resimulate": bool(simulate),
        }, ctx)
        self._record_tool_result(ctx, "apply_flowgraph_patch", result)
        validation = self._validate_loaded(ctx)
        self._record_tool_result(ctx, "validate_flowgraph", validation)
        if result.get("path"):
            ctx.extra.setdefault("artifacts", {})["grc_path"] = result["path"]
        if result.get("ok") and target_recipe:
            from ..knowledge import recipes

            self._state.project.config["recipe"] = target_recipe
            modulation = recipes.guess_modulation(target_recipe)
            if modulation:
                self._state.project.config["modulation"] = modulation
        return self._fold(
            ctx,
            result.get("error") or "Applied the GraphPatch and completed revalidation.",
            source="deterministic-stage",
            ok=bool(result.get("ok")) and bool(validation.get("valid")),
        )
    if target_recipe:
        return self._run_deterministic(ctx, user_text, target_recipe, simulate)
    if recipe:
        return self._run_deterministic(ctx, user_text, recipe, simulate)
    request_text = (
        self._workflow.workflow.intent.raw_text
        if self._workflow.workflow else user_text
    )
    change = re.search(
        r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(?:改为|设为|改成|=)\s*([^，。\s]+)",
        request_text,
    )
    if not change:
        return self._fold(
            ctx, "Unable to determine the block.parameter target and new value from the change request.",
            source="deterministic-stage", ok=False,
        )
    result = registry.call("apply_grc_diff", {
        "block_id": change.group(1),
        "parameter": change.group(2),
        "value": change.group(3),
        "resimulate": simulate,
    }, ctx)
    self._record_tool_result(ctx, "apply_grc_diff", result)
    validation = self._validate_loaded(ctx)
    self._record_tool_result(ctx, "validate_flowgraph", validation)
    if result.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = result["path"]
    return self._fold(
        ctx,
        result.get("error") or (
            f"Changed {change.group(1)}.{change.group(2)} and completed revalidation."
        ),
        source="deterministic-stage",
        ok=bool(result.get("ok")) and bool(validation.get("valid")),
    )


def _handle_inspect_plan(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    result = registry.call("inspect_flowgraph", {}, ctx)
    self._record_tool_result(ctx, "inspect_flowgraph", result)
    slots = _slots(self)
    target = str(slots.get("target_recipe") or "")
    current = str(self._state.project.config.get("recipe") or "")
    if result.get("ok") and target:
        patch = _recipe_graph_patch(current, target)
        if patch.get("operations"):
            slots["graph_patch"] = patch
            slots["change_type"] = "multi_block_change"
            plan_path = os.path.join(ctx.out_dir, "change_plan.json")
            with open(plan_path, "w", encoding="utf-8") as handle:
                json.dump(patch, handle, ensure_ascii=False, indent=2, sort_keys=True)
            ctx.extra.setdefault("artifacts", {})["change_plan"] = plan_path
            note = (
                f"Inspected the current project ({current or 'Untitled'}) and generated a GraphPatch with "
                f"{len(patch['operations'])} in-place change(s); the canvas remains unchanged until confirmation."
            )
        else:
            slots["rebuild_required"] = True
            slots["rebuild_reason"] = "A constrained semantic diff cannot be generated between the current and target structures"
            note = (
                "The current project was inspected, but a verifiable in-place GraphPatch could not be formed. "
                "Continuing will explicitly use the rebuild path."
            )
    elif result.get("ok"):
        # No target recipe and no derivable patch: report honestly instead of
        # claiming a change plan exists.
        note = (
            "I inspected the current project ({}), but I could not derive a "
            "concrete change plan from this request, so nothing was modified. "
            "Tell me which parameter to change (for example, "
            "'local name = cindysha' or 'sample rate = 2 MHz'), or restate "
            "what the changed project should do."
        ).format(current or "Untitled")
    else:
        note = result.get("error", "Project inspection failed")
    return self._fold(
        ctx, note, source="deterministic-stage", ok=bool(result.get("ok"))
    )


def _handle_inspect_measure(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return inspect_measure_stage(self, ctx, diagnose=False)


def _handle_inspect_diagnose(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return inspect_measure_stage(self, ctx, diagnose=True)


def _handle_hardware_precheck(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    hardware = str(_slots(self).get("hardware") or "")
    result = registry.call("hardware_preflight", {"device_type": hardware}, ctx)
    self._record_tool_result(ctx, "hardware_preflight", result)
    missing = list(result.get("missing") or [])
    note = result.get("note") or "Hardware precheck completed."
    if missing:
        note = "Hardware precheck is still missing: {}. {}".format(", ".join(missing), note)
    return self._fold(
        ctx, note, source="deterministic-stage", ok=bool(result.get("ok")),
    )


def _handle_configure_and_check(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    slots = _slots(self)
    result = registry.call("configure_sdr", {
        "device_type": slots.get("hardware") or "sdr",
        "center_freq": slots.get("carrier_frequency"),
        "sample_rate": slots.get("sample_rate"),
    }, ctx)
    self._record_tool_result(ctx, "configure_sdr", result)
    preflight = registry.call(
        "hardware_preflight",
        {"device_type": slots.get("hardware") or "sdr"},
        ctx,
    )
    self._record_tool_result(ctx, "hardware_preflight", preflight)
    return self._fold(
        ctx,
        result.get("error") or "SDR parameters were recorded; physical hardware operations remain disabled.",
        source="deterministic-stage",
        ok=bool(result.get("ok")) and bool(preflight.get("ok")),
    )


def _handle_build_ble(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    slots = self._workflow.workflow.intent.slots
    local_name = str(slots.get("local_name") or "")
    channel = int((slots.get("advertising_channels") or [37])[0])
    hardware = normalize_hardware(str(slots.get("hardware") or "b210"))
    profile = resolve_hardware_profile(hardware)
    pdu_args = {"local_name": local_name, "channel": channel}
    pdu = registry.call("build_ble_advertising_pdu", pdu_args, ctx)
    self._record_tool_result(ctx, "build_ble_advertising_pdu", pdu, pdu_args)
    waveform_args = {
        "local_name": local_name,
        "channel": channel,
        "sample_rate": slots.get("sample_rate") or 2e6,
        "interval_ms": slots.get("advertising_interval_ms") or 100.0,
        "bt": slots.get("bt") or 0.5,
        "modulation_index": slots.get("modulation_index") or 0.5,
        "digital_amplitude": slots.get("digital_amplitude") or 0.5,
    }
    waveform = registry.call("generate_ble_1m_waveform", waveform_args, ctx)
    self._record_tool_result(
        ctx, "generate_ble_1m_waveform", waveform, waveform_args
    )
    if profile is None or not profile.ble_tx_builder:
        return self._fold(
            ctx,
            f"No BLE TX builder is available for the selected hardware ({hardware or '(empty)'}). "
            "Execution stopped without substituting another SDR.",
            source="deterministic-stage",
            ok=False,
        )
    if profile.ble_tx_builder == "build_ble_pluto_tx_flowgraph":
        build_args = {
            "waveform_path": waveform.get("path") or "",
            "channel": channel,
            "sample_rate": slots.get("sample_rate") or 2e6,
            "attenuation": slots.get("tx_attenuation", 30.0),
            "uri": slots.get("device_uri") or "",
            "duration_seconds": slots.get("duration_seconds") or 30.0,
        }
        built = registry.call("build_ble_pluto_tx_flowgraph", build_args, ctx)
        builder = "build_ble_pluto_tx_flowgraph"
        sink_note = "the PlutoSDR TX flowgraph was generated; RF has not started."
    elif profile.ble_tx_builder == "build_ble_uhd_tx_flowgraph":
        build_args = {
            "waveform_path": waveform.get("path") or "",
            "channel": channel,
            "sample_rate": slots.get("sample_rate") or 2e6,
            "gain": slots.get("tx_gain", 0.0),
            "device_args": device_args_for(
                hardware, str(slots.get("device_args") or "")
            ),
            "duration_seconds": slots.get("duration_seconds") or 30.0,
        }
        built = registry.call("build_ble_uhd_tx_flowgraph", build_args, ctx)
        builder = "build_ble_uhd_tx_flowgraph"
        sink_note = "the B210 TX flowgraph was generated; RF has not started."
    else:
        return self._fold(
            ctx,
            f"The BLE builder for HardwareProfile {profile.key} is not implemented.",
            source="deterministic-stage",
            ok=False,
        )
    self._record_tool_result(ctx, builder, built, build_args)
    if built.get("grc_path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = built["grc_path"]
        self._state.project.grc_path = built["grc_path"]
        self._state.project.flowgraph_version += 1
        self._state.project.config.update({
            "protocol": "ble",
            "local_name": local_name,
            "ble_channel": channel,
            "ble_capability": "ble_advertising_single_channel",
            "hardware": hardware,
            "rf_armed": False,
            "desired_device": {
                "type": hardware,
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
            },
        })
    return self._fold(
        ctx,
        built.get("error") or f"The BLE advertising PDU and offline waveform were generated, and {sink_note}",
        source="deterministic-stage",
        ok=bool(pdu.get("ok") and waveform.get("ok") and built.get("ok")),
    )


def _handle_offline_protocol(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    slots = self._workflow.workflow.intent.slots
    channel = int((slots.get("advertising_channels") or [37])[0])
    verify_args = {
        "local_name": slots.get("local_name") or "", "channel": channel,
    }
    verified = registry.call("verify_ble_packet_bits", verify_args, ctx)
    self._record_tool_result(
        ctx, "verify_ble_packet_bits", verified, verify_args
    )
    validation = self._validate_loaded(ctx)
    self._record_tool_result(ctx, "validate_flowgraph", validation)
    hardware = normalize_hardware(str(slots.get("hardware") or "b210"))
    profile = resolve_hardware_profile(hardware)
    sink = profile.label if profile else hardware or "SDR"
    return self._fold(
        ctx, f"Offline BLE PDU/CRC/whitening and {sink} TX flowgraph verification completed.",
        source="deterministic-stage",
        ok=bool(verified.get("valid") and validation.get("valid")),
    )


def _discover_and_probe(
    self,
    ctx: ToolContext,
    *,
    default_hardware: str,
    with_timestamp: bool,
    success_note: str,
) -> AgentReply:
    from ..tools import registry

    slots = self._workflow.workflow.intent.slots
    hardware = normalize_hardware(str(slots.get("hardware") or default_hardware))
    profile = resolve_hardware_profile(hardware)
    if profile is None:
        return self._fold(
            ctx, f"Unsupported SDR type: {hardware or '(empty)' }.",
            source="deterministic-stage", ok=False,
        )
    args = {"device_type": hardware}
    discovered = registry.call("discover_devices", args, ctx)
    self._record_tool_result(ctx, "discover_devices", discovered, args)
    if discovered.get("device_identity"):
        args["device_args"] = discovered["device_identity"]
    probed = registry.call("probe_device", args, ctx)
    self._record_tool_result(ctx, "probe_device", probed, args)
    if discovered.get("device_found") and probed.get("device_probed"):
        observed = {
            "type": profile.key,
            "identity": probed.get("device_identity")
            or discovered.get("device_identity"),
            "driver_family": profile.driver_family,
        }
        if with_timestamp:
            observed["observed_at"] = (
                probed.get("observed_at")
                or discovered.get("observed_at")
                or time.time()
            )
        self._state.project.config["observed_device"] = observed
        _bind_preview_identity(self, ctx, str(observed.get("identity") or ""))
    label = profile.label
    if not discovered.get("device_found"):
        note = discovered.get("error") or f"No available {label} was discovered."
    elif not discovered.get("device_identity"):
        note = f"{label} was discovered, but no device identity suitable for precise probing could be extracted."
    elif not probed.get("device_probed"):
        note = (
            probed.get("error")
            or f"{label} {discovered.get('device_identity')} was discovered, "
            "but precise probing did not pass acceptance."
        )
    else:
        note = success_note.format(label=label)
    return self._fold(
        ctx, note, source="deterministic-stage",
        ok=bool(discovered.get("device_found") and probed.get("device_probed")),
    )


def _handle_probe_device(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _discover_and_probe(
        self, ctx, default_hardware="b210", with_timestamp=False,
        success_note="Read-only discovery and probing completed for {label}; the TX stream has not been opened.",
    )


def _handle_probe_hardware(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _discover_and_probe(
        self, ctx, default_hardware="", with_timestamp=True,
        success_note="Read-only discovery and probing completed for {label}; the flowgraph has not started.",
    )


def _handle_configure_device(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    slots = self._workflow.workflow.intent.slots
    current = self._workflow.current_stage()
    resume_from = str(getattr(current, "resume_from", "") or "")
    configure_args = {
        "device_type": slots.get("hardware") or "b210",
        "center_freq": slots.get("carrier_frequency"),
        "sample_rate": slots.get("sample_rate"),
    }
    if resume_from == "arm_flowgraph":
        result = {"ok": True, "resumed": True}
    else:
        result = registry.call("configure_sdr", configure_args, ctx)
        self._record_tool_result(ctx, "configure_sdr", result, configure_args)
    hardware = normalize_hardware(str(slots.get("hardware") or "b210"))
    profile = resolve_hardware_profile(hardware)
    label = profile.label if profile else hardware or "SDR"
    armed = {"ok": True, "armed": False}
    tx_runtime = (
        str(slots.get("protocol") or "").lower() == "ble"
        or str(slots.get("direction") or "").lower() == "tx"
    )
    if tx_runtime and result.get("ok"):
        arm_args = {
            "grc_path": self._state.project.grc_path,
            "device_identity": str(
                (self._state.project.config.get("observed_device") or {}).get(
                    "identity"
                )
                or ""
            ),
        }
        armed = registry.call("arm_hardware_flowgraph", arm_args, ctx)
        self._record_tool_result(
            ctx, "arm_hardware_flowgraph", armed, arm_args
        )
    ok = bool(result.get("ok") and armed.get("ok"))
    if current is not None:
        current.resume_from = (
            "arm_flowgraph"
            if (result.get("ok") and tx_runtime and not armed.get("ok"))
            else ""
        )
    return self._fold(
        ctx,
        result.get("error") or armed.get("error")
        or (
            f"The {label} transmit configuration was recorded and safely armed; waiting to start."
            if tx_runtime
            else f"The {label} receive configuration was recorded; waiting for a bounded runtime."
        ),
        source="deterministic-stage",
        ok=ok,
    )


def _start_bounded(self, ctx: ToolContext, *, tx: bool) -> AgentReply:
    from ..tools import registry

    slots = self._workflow.workflow.intent.slots
    duration = (
        slots.get("max_duration_seconds")
        or slots.get("duration_seconds")
        or 30.0
    )
    start_args = {
        "grc_path": self._state.project.grc_path,
        "duration_seconds": duration,
    }
    result = registry.call("start_flowgraph", start_args, ctx)
    self._record_tool_result(ctx, "start_flowgraph", result, start_args)
    if tx:
        target = (
            f"Check for advertising name {slots.get('local_name')} before the deadline."
            if slots.get("local_name")
            else "Check the target signal with an independent receiver before the deadline."
        )
        note = (
            f"Bounded transmission started (maximum duration: {duration:g} seconds; "
            "OTA confirmation or cancellation stops it early). "
            + target
            + "The process is managed by the workflow; you do not need to click Run in GRC."
        )
    else:
        note = (
            f"Bounded runtime started (maximum duration: {duration:g} seconds). "
            "You do not need to click Run in GRC."
        )
    return self._fold(
        ctx, result.get("error") or note, source="deterministic-stage",
        ok=bool(result.get("running") and result.get("ready")),
    )


def _handle_transmit(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _start_bounded(self, ctx, tx=True)


def _handle_run_bounded(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _start_bounded(self, ctx, tx=False)


def _stop_runtime(self, ctx: ToolContext, *, require_ota: bool) -> AgentReply:
    from ..tools import registry

    stopped = registry.call("stop_flowgraph", {}, ctx)
    self._record_tool_result(ctx, "stop_flowgraph", stopped)
    if require_ota:
        observed = bool(
            self._workflow.workflow.intent.slots.get("over_air_observed")
        )
        note = (
            "✅ Transmission stopped. Independent receiver observation was recorded."
            if observed else
            "⏹ Transmission stopped. The target signal was not observed."
        )
        ok = bool(
            stopped.get("ok")
            and not stopped.get("running")
            and not stopped.get("crashed")
        )
    else:
        note = "The hardware flowgraph was stopped; runtime status and user observations were recorded."
        ok = bool(
            stopped.get("ok")
            and not stopped.get("running")
            and not stopped.get("crashed")
            and stopped.get("run_id")
        )
    return self._fold(ctx, note, source="deterministic-stage", ok=ok)


def _handle_stop_finalize(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _stop_runtime(self, ctx, require_ota=True)


def _handle_stop_runtime(self, ctx, user_text, recipe, simulate) -> AgentReply:
    return _stop_runtime(self, ctx, require_ota=False)


def _handle_repair(self, ctx, user_text, recipe, simulate) -> AgentReply:
    from ..tools import registry

    diagnosed = self._workflow.workflow.stage("inspect_and_diagnose")
    changes = list(
        (diagnosed.result if diagnosed else {}).get("proposed_changes") or []
    )
    if not changes:
        return self._fold(
            ctx, "No deterministic repair parameters are available. Provide the repair target first.",
            source="deterministic-stage", ok=False,
        )
    change = changes[0]
    result = registry.call("apply_grc_diff", {
        "block_id": change.get("block_id"),
        "parameter": change.get("parameter"),
        "value": change.get("value"),
        "resimulate": simulate,
    }, ctx)
    self._record_tool_result(ctx, "apply_grc_diff", result)
    validation = self._validate_loaded(ctx)
    self._record_tool_result(ctx, "validate_flowgraph", validation)
    if result.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = result["path"]
    return self._fold(
        ctx, result.get("error") or "The minimum repair was applied and revalidated.",
        source="deterministic-stage",
        ok=bool(result.get("ok")) and bool(validation.get("valid")),
    )


def _diagnosis_dimensions(raw_text: str, slots: dict) -> list[str]:
    """Infer a diagnosis scope without coupling it to catalog task names.

    An LLM/planner may provide ``diagnosis_dimensions`` directly.  The lexical
    fallback only maps domain nouns to reusable check dimensions; it never
    selects a canned task result.
    """
    explicit = slots.get("diagnosis_dimensions") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    if explicit:
        return list(dict.fromkeys(str(item) for item in explicit if item))
    text = str(raw_text or "").lower()
    groups = (
        ("environment", ("驱动", "环境", "依赖", "driver", "uhd", "libiio")),
        ("device", ("设备", "型号", "发现", "探测", "probe", "identity", "sdr")),
        ("parameters", ("参数", "频率", "采样率", "带宽", "增益", "衰减")),
        ("project", ("流图", "工程", ".grc", "block", "连接边")),
        ("runtime", ("运行", "进程", "日志", "报错", "崩溃", "runtime")),
        ("rf_path", ("接线", "线缆", "端口", "天线", "连接", "射频链路")),
        ("signal", ("evm", "ber", "星座", "频谱", "波形", "信号质量")),
    )
    selected = [name for name, hints in groups if any(hint in text for hint in hints)]
    if not selected:
        selected = ["intent", "environment", "device", "parameters", "runtime", "rf_path"]
    elif "device" in selected and "intent" not in selected:
        selected.insert(0, "intent")
    return list(dict.fromkeys(selected))


def inspect_measure_stage(
    self, ctx: ToolContext, *, diagnose: bool = False
) -> AgentReply:
    from ..tools import registry

    workflow = self._workflow.workflow
    slots = workflow.intent.slots if workflow else {}
    hardware_report = {}
    if diagnose and slots.get("hardware"):
        dimensions = _diagnosis_dimensions(workflow.intent.raw_text, slots)
        hardware_report = registry.call(
            "run_diagnosis_checks",
            {
                "device_type": slots.get("hardware"),
                "dimensions": [
                    item for item in dimensions
                    if item in {
                        "intent", "environment", "device", "parameters",
                        "project", "runtime", "rf_path",
                    }
                ],
                "live_probe": True,
            },
            ctx,
        )
        self._record_tool_result(ctx, "run_diagnosis_checks", hardware_report)
        if hardware_report.get("report_path"):
            ctx.extra.setdefault("artifacts", {})["hardware_diagnosis_report"] = (
                hardware_report["report_path"]
            )
        snapshot = self._state.diagnosis
        snapshot.intent_id = self._state.intent.intent_id
        snapshot.intent_revision = self._state.intent.revision
        snapshot.requested_dimensions = list(dimensions)
        snapshot.findings = list(hardware_report.get("findings") or [])
        snapshot.summary = dict(hardware_report.get("summary") or {})
        snapshot.report_path = str(hardware_report.get("report_path") or "")
        snapshot.created_at = time.time()
        hardware_only = not set(dimensions).intersection({
            "project", "signal", "waveform", "metrics",
        })
        if hardware_only or not self._state.project.grc_path:
            summary = dict(hardware_report.get("summary") or {})
            reply = self._fold(
                ctx,
                "Hardware diagnosis completed: pass={pass_count}, fail={fail_count}, "
                "unknown={unknown_count}. Unknown findings require external or human evidence.".format(
                    pass_count=summary.get("pass", 0),
                    fail_count=summary.get("fail", 0),
                    unknown_count=summary.get("unknown", 0),
                ),
                source="deterministic-stage",
                ok=bool(hardware_report.get("diagnosis_complete"))
                and not bool(summary.get("fail")),
            )
            if summary.get("unknown"):
                reply.quality = "warning"
                self._state.runtime.quality = "warning"
                self._state.runtime.warnings.append({
                    "code": "diagnosis_unknown_requires_evidence",
                    "count": int(summary.get("unknown") or 0),
                })
            return reply

    inspected = registry.call("inspect_flowgraph", {}, ctx)
    self._record_tool_result(ctx, "inspect_flowgraph", inspected)
    validation = self._validate_loaded(ctx)
    self._record_tool_result(ctx, "validate", validation)
    if not inspected.get("ok") or not validation.get("ok"):
        return self._fold(
            ctx, inspected.get("error") or validation.get("error") or "Project inspection failed",
            source="deterministic-stage", ok=False,
        )
    if not validation.get("valid"):
        explained = registry.call(
            "explain_error", {"errors": validation.get("errors") or []}, ctx
        )
        self._record_tool_result(ctx, "explain_error", explained)
        return self._fold(
            ctx, "Structural validation did not pass; specific errors and repair suggestions were provided.",
            source="deterministic-stage", ok=False,
        )
    if workflow:
        inferred_modulation = ""
        inferred_channel = ""
        for block in inspected.get("blocks") or []:
            key = str(block.get("key") or "").lower()
            params = dict(block.get("params") or {})
            if "constellation" in key:
                token = str(
                    params.get("type") or params.get("constellation") or ""
                ).lower()
                inferred_modulation = next(
                    (name for name in ("qpsk", "bpsk", "ofdm", "gfsk")
                     if name in token),
                    inferred_modulation,
                )
            if key == "channels_channel_model":
                inferred_channel = "awgn"
        for name, value in (("modulation", inferred_modulation),
                            ("channel", inferred_channel)):
            if value and not workflow.intent.slots.get(name):
                workflow.intent.slots[name] = value
                workflow.intent.slot_sources[name] = "canvas"
                self._state.project.config[name] = value
        workflow.intent.missing_slots = self._workflow._missing_slots(
            workflow.task_type, workflow.intent.slots, self._state,
            workflow.intent.capabilities,
        )
        self._sync_workflow_intent_to_state()
    simulated = registry.call("run_simulation", {}, ctx)
    self._record_tool_result(ctx, "simulate", simulated)
    if not simulated.get("ok"):
        return self._fold(
            ctx, simulated.get("error") or "Simulation failed",
            source="deterministic-stage", ok=False,
        )
    slots = workflow.intent.slots if workflow else {}
    source_scope = str(
        slots.get("signal_source_scope") or "current_project_offline"
    )
    if source_scope == "live_device":
        return self._fold(
            ctx,
            "Live device observation requires device identity probing and a controlled RX runtime; "
            "offline simulation of the current project will not be substituted.",
            source="deterministic-stage",
            ok=False,
        )
    requested = list(slots.get("requested_metrics") or [])
    if diagnose and not requested:
        requested = ["evm"]
    if not requested:
        requested = ["spectrum"]
    modulation = str(
        self._state.project.config.get("modulation") or slots.get("modulation") or "bpsk"
    )
    sps = 4
    samp_rate = self._flowgraph_sample_rate(ctx)
    metrics = ctx.extra.setdefault("metrics", {})
    metrics["signal_source_scope"] = source_scope
    plot_for = {
        "evm": ("constellation",),
        "ber": (),
        "spectrum": ("spectrum",),
        "constellation": ("constellation",),
        "eye": ("eye",),
    }
    plots: list[str] = []
    for kind in requested:
        if kind in ("evm", "ber", "spectrum"):
            args = {
                "kind": kind,
                "modulation": modulation,
                "sps": 1 if kind == "ber" else sps,
            }
            if kind == "spectrum":
                args["samp_rate"] = samp_rate
            if kind == "ber":
                args.update({"probe_id": "sink", "tx_bits_probe": "tx_sink"})
            measured = registry.call("read_metric", args, ctx)
            self._record_tool_result(ctx, "read_metric", measured)
            if measured.get("measurement_id"):
                metrics["measurement_id"] = measured["measurement_id"]
            if measured.get("ok") and measured.get("value") is not None:
                metrics["evm_pct" if kind == "evm" else (
                    "spectrum_peak" if kind == "spectrum" else "ber"
                )] = measured["value"]
                if kind == "ber":
                    metrics["ber_report"] = {
                        key: value for key, value in measured.items()
                        if key not in {"ok", "kind"}
                    }
                elif kind == "spectrum":
                    metrics["spectrum_peak_report"] = {
                        key: value for key, value in measured.items()
                        if key not in {"ok", "kind"}
                    }
                if measured.get("peak_bin") is not None:
                    metrics["spectrum_peak_bin"] = measured["peak_bin"]
        for plot_kind in plot_for.get(kind, ()):
            if plot_kind not in plots:
                plots.append(plot_kind)
    for kind in plots:
        plot_name = {
            "spectrum": "plot_spectrum",
            "constellation": "plot_constellation",
            "eye": "plot_eye",
        }.get(kind)
        if not plot_name:
            continue
        if plot_name == "plot_spectrum":
            plot_args = {"samp_rate": samp_rate}
        elif plot_name == "plot_constellation":
            plot_args = {"sps": sps, "modulation": modulation}
        else:
            plot_args = {"sps": sps}
        plotted = registry.call(plot_name, plot_args, ctx)
        self._record_tool_result(ctx, plot_name, plotted)
        if plotted.get("path"):
            key = {
                "plot_spectrum": "spectrum_png",
                "plot_constellation": "constellation_png",
                "plot_eye": "eye_png",
            }[plot_name]
            ctx.extra.setdefault("artifacts", {})[key] = plotted["path"]
    if diagnose:
        experiment = registry.call("run_diagnosis_experiment", {
            "metric": "evm" if "evm" in requested else "spectrum",
            "modulation": modulation,
            "sps": sps,
            "samp_rate": samp_rate,
        }, ctx)
        self._record_tool_result(ctx, "run_diagnosis_experiment", experiment)
        if experiment.get("report_path"):
            ctx.extra.setdefault("artifacts", {})["signal_diagnosis_report"] = (
                experiment["report_path"]
            )
        diagnosis = registry.call("debug_by_metric", {
            "metric": "evm" if "evm" in requested else "spectrum",
            "modulation": modulation,
            "sps": sps,
        }, ctx)
        self._record_tool_result(ctx, "debug_by_metric", diagnosis)
        verdict = str(diagnosis.get("verdict") or "").lower()
        issue = (
            any(marker in verdict for marker in ("偏高", "high", "elevated"))
            and not diagnosis.get("meets_claim")
        )
        forbidden = set(
            (workflow.intent.context if workflow else {}).get("forbidden_capabilities")
            or []
        )
        readonly = bool(
            ctx.extra.get("mutation_forbidden") or "modify_project" in forbidden
        )
        reply = self._fold(
            ctx,
            diagnosis.get("narrative") or diagnosis.get("error") or "Diagnosis completed.",
            source="deterministic-stage",
            ok=bool(diagnosis.get("ok")) if readonly else (
                not issue and bool(diagnosis.get("ok"))
            ),
        )
        if issue and not readonly:
            proposed = None
            ranked = list(experiment.get("ranked") or []) if experiment.get("ok") else []
            if ranked:
                top = ranked[0]
                proposed = {
                    "block_id": top.get("block"),
                    "parameter": top.get("param"),
                    "value": top.get("trial_value"),
                }
            if proposed:
                reply.pending = {
                    "action": "workflow_checkpoint",
                    "reason": "The controlled experiment identified the primary factor; the original project will be modified only after confirmation",
                    "approved": False,
                    "proposed_changes": [proposed],
                }
        return reply
    summary = "Project inspection and measurement completed."
    peak = metrics.get("spectrum_peak_report")
    if isinstance(peak, dict) and peak.get("valid"):
        summary += " Peak at {:.3f} Hz with amplitude {:.2f} dBFS (FFT {}, {} window).".format(
            float(peak.get("frequency_hz") or 0.0),
            float(peak.get("magnitude_dbfs") or 0.0),
            int(peak.get("fft_size") or 0),
            peak.get("window") or "unknown",
        )
    return self._fold(
        ctx, summary, source="deterministic-stage", ok=True
    )


_HANDLERS: dict[str, _Handler] = {
    "build_and_verify": _handle_build,
    "tx_build_and_validate": _handle_build,
    "rx_build_and_verify": _handle_build,
    "apply_and_verify": _handle_apply,
    "inspect_and_plan": _handle_inspect_plan,
    "inspect_and_measure": _handle_inspect_measure,
    "inspect_and_diagnose": _handle_inspect_diagnose,
    "hardware_precheck": _handle_hardware_precheck,
    "configure_and_check": _handle_configure_and_check,
    "build_ble_advertiser": _handle_build_ble,
    "offline_protocol_verify": _handle_offline_protocol,
    "discover_and_probe_device": _handle_probe_device,
    "discover_and_probe_hardware": _handle_probe_hardware,
    "configure_device": _handle_configure_device,
    "transmit_bounded": _handle_transmit,
    "run_bounded": _handle_run_bounded,
    "stop_and_finalize": _handle_stop_finalize,
    "stop_runtime": _handle_stop_runtime,
    "repair_and_verify": _handle_repair,
}
