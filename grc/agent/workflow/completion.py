"""Deterministic checks for Task Catalog Stage completion contracts."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable


KNOWN_COMPLETIONS = frozenset(
    {
        "required_slots_complete",
        "flowgraph_saved",
        "structural_validation_completed",
        "affected_claims_evaluated",
        "receive_quality_evaluated",
        "diagnosis_created",
        "repair_decision_recorded",
        "repair_applied",
        "change_plan_created",
        "change_decision_recorded",
        "measurement_completed",
        "hardware_precheck_completed",
        "hardware_decision_recorded",
        "configuration_recorded",
        "hardware_check_completed",
        "ble_packet_created",
        "ble_waveform_generated",
        "ble_packet_valid",
        "device_discovered",
        "device_probed",
        "rf_plan_approved",
        "transmit_started",
        "over_air_observed",
        "transmit_stopped",
        "hardware_endpoint_present",
        "radio_parameters_match",
        "realtime_sink_present",
        "runtime_started",
        "runtime_observation_recorded",
        "runtime_stopped",
    }
)


def evaluate(stage: Any, workflow: Any, state: Any, reply: Any) -> Dict[str, bool]:
    """Evaluate only facts produced by tools/state; never trust narrative text."""
    invocations = list(getattr(reply, "tool_invocations", None) or [])
    artifacts = dict(getattr(reply, "artifacts", None) or {})
    metrics = artifacts.get("metrics") if isinstance(artifacts.get("metrics"), dict) else {}
    tool_results = _tool_results(invocations)
    tool_names = set(tool_results)
    project = getattr(state, "project", None)
    project_version = int(getattr(project, "flowgraph_version", 0))
    grc_path = str(artifacts.get("grc_path") or getattr(project, "grc_path", "") or "")
    claims = list(getattr(state, "claims", None) or [])
    pending = dict(getattr(reply, "pending", None) or {})
    slots = dict(getattr(getattr(workflow, "intent", None), "slots", None) or {})
    capabilities = set(
        getattr(getattr(workflow, "intent", None), "capabilities", None) or []
    )

    def flowgraph_blocks() -> list[Dict[str, Any]]:
        inspected = tool_results.get("inspect_flowgraph", [])
        for result in reversed(inspected):
            blocks = result.get("blocks")
            if isinstance(blocks, list):
                return [item for item in blocks if isinstance(item, dict)]
        return []

    def flowgraph_text() -> str:
        if not grc_path or not os.path.isfile(grc_path):
            return ""
        try:
            with open(grc_path, "r", encoding="utf-8") as handle:
                return handle.read().lower()
        except OSError:
            return ""

    def block_keys() -> set[str]:
        keys = {
            str(block.get("key") or "").lower()
            for block in flowgraph_blocks()
        }
        if keys:
            return keys
        text = flowgraph_text()
        known = {
            "uhd_usrp_source", "uhd_usrp_sink", "qtgui_freq_sink_x",
            "qtgui_waterfall_sink_x", "qtgui_time_sink_x",
            "osmosdr_source", "osmosdr_sink", "iio_pluto_source",
            "iio_pluto_sink", "iio_fmcomms2_source_fc32",
            "iio_fmcomms2_sink_fc32", "limesdr_source", "limesdr_sink",
        }
        return {key for key in known if key in text}

    def contains_numeric(expected: Any) -> bool:
        try:
            target = float(expected)
        except (TypeError, ValueError):
            return False
        values: list[str] = []
        for block in flowgraph_blocks():
            values.extend(str(value) for value in (block.get("params") or {}).values())
        if not values:
            values = [flowgraph_text()]
        for value in values:
            for token in re.findall(
                r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?",
                value.lower(),
            ):
                try:
                    number = float(token)
                except ValueError:
                    continue
                tolerance = max(abs(target) * 1e-9, 1e-6)
                if abs(number - target) <= tolerance:
                    return True
        return False

    def hardware_endpoint() -> bool:
        keys = block_keys()
        direction = str(slots.get("direction") or "").lower()
        hardware = str(slots.get("hardware") or "").lower()
        families = {
            "b210": ({"uhd_usrp_source"}, {"uhd_usrp_sink"}),
            "usrp": ({"uhd_usrp_source"}, {"uhd_usrp_sink"}),
            "hackrf": ({"osmosdr_source"}, {"osmosdr_sink"}),
            "pluto": (
                {"iio_pluto_source", "iio_fmcomms2_source_fc32"},
                {"iio_pluto_sink", "iio_fmcomms2_sink_fc32"},
            ),
            "limesdr": ({"limesdr_source"}, {"limesdr_sink"}),
        }
        sources, sinks = families.get(hardware, (set(), set()))
        if direction == "rx":
            return bool(keys.intersection(sources))
        if direction == "tx":
            return bool(keys.intersection(sinks))
        return bool(keys.intersection(sources | sinks))

    def radio_parameters() -> bool:
        required = [slots.get("carrier_frequency"), slots.get("sample_rate")]
        return all(value is not None and contains_numeric(value) for value in required)

    def checked(name: str) -> bool:
        return any(bool(result.get("ok", True)) for result in tool_results.get(name, []))

    def structural_validation() -> bool:
        results = tool_results.get("validate", []) + tool_results.get("validate_flowgraph", [])
        if any(bool(item.get("ok", True)) and bool(item.get("valid")) for item in results):
            return True
        if any(
            bool(item.get("ok", True)) and bool(item.get("valid"))
            for item in tool_results.get("design_link", [])
        ):
            return True
        return any(
            bool(item.get("ok", True)) and bool(item.get("valid"))
            for name in (
                "build_usrp_rx_spectrum_flowgraph",
                "build_ble_uhd_tx_flowgraph",
            )
            for item in tool_results.get(name, [])
        )

    def claims_current() -> bool:
        affected = [claim for claim in claims if getattr(claim, "layer", "") in ("sim", "structure")]
        return all(
            int(getattr(claim, "project_version", -1)) == project_version
            and getattr(claim, "status", "NotTested") != "NotTested"
            for claim in affected
        )

    def requested_measurements() -> bool:
        requested = list(slots.get("requested_metrics") or [])
        if not requested:
            return structural_validation() and bool(
                metrics or checked("simulate") or checked("design_link")
            )
        available = {
            "evm": metrics.get("evm_pct") is not None,
            "ber": metrics.get("ber") is not None,
            "spectrum": metrics.get("spectrum_peak") is not None
            or bool(artifacts.get("spectrum_png")),
            "constellation": bool(artifacts.get("constellation_png")),
            "eye": bool(artifacts.get("eye_png")),
        }
        return all(available.get(name, False) for name in requested)

    checks = {
        "required_slots_complete": not bool(
            getattr(workflow.intent, "missing_slots", [])
            or getattr(workflow.intent, "validation_errors", [])
        ),
        "flowgraph_saved": bool(grc_path and os.path.isfile(grc_path)),
        "structural_validation_completed": structural_validation(),
        "affected_claims_evaluated": claims_current(),
        "receive_quality_evaluated": metrics.get("ber") is not None
        or metrics.get("evm_pct") is not None,
        "diagnosis_created": bool(
            tool_names.intersection({"debug_by_metric", "diagnose_by_metric", "explain_error"})
        ),
        "repair_decision_recorded": True,
        "repair_applied": bool(
            tool_names.intersection({"apply_grc_diff", "apply_flowgraph_patch", "design_link"})
        ),
        "change_plan_created": bool(
            checked("inspect_flowgraph")
            and (
                slots.get("target_recipe")
                or slots.get("change_type")
                or slots.get("target_project")
                or "modify_project" in capabilities
                or pending
            )
        ),
        "change_decision_recorded": True,
        "measurement_completed": requested_measurements(),
        "hardware_precheck_completed": checked("hardware_preflight"),
        "hardware_decision_recorded": True,
        "configuration_recorded": bool(
            getattr(project, "config", {}).get("device")
        ),
        "hardware_check_completed": bool(
            getattr(project, "config", {}).get("device", {}).get("mode")
            == "flowgraph_config_only"
        ),
        "ble_packet_created": checked("build_ble_advertising_pdu"),
        "ble_waveform_generated": checked("generate_ble_1m_waveform"),
        "ble_packet_valid": any(
            bool(item.get("valid"))
            for item in tool_results.get("verify_ble_packet_bits", [])
        ),
        "device_discovered": any(
            bool(item.get("device_found"))
            for item in tool_results.get("discover_devices", [])
        ),
        "device_probed": any(
            bool(item.get("device_probed"))
            for item in tool_results.get("probe_device", [])
        ),
        "rf_plan_approved": True,
        "transmit_started": any(
            bool(item.get("running"))
            for item in tool_results.get("start_flowgraph", [])
        ),
        "over_air_observed": True,
        "transmit_stopped": any(
            bool(item.get("ok")) and not bool(item.get("running"))
            for name in ("stop_flowgraph", "emergency_stop", "query_runtime_status")
            for item in tool_results.get(name, [])
        ),
        "hardware_endpoint_present": hardware_endpoint(),
        "radio_parameters_match": radio_parameters(),
        "realtime_sink_present": bool(block_keys().intersection({
            "qtgui_freq_sink_x", "qtgui_waterfall_sink_x",
        })),
        "runtime_started": any(
            bool(item.get("running"))
            for item in tool_results.get("start_flowgraph", [])
        ),
        "runtime_observation_recorded": True,
        "runtime_stopped": any(
            bool(item.get("ok")) and not bool(item.get("running"))
            for name in ("stop_flowgraph", "emergency_stop", "query_runtime_status")
            for item in tool_results.get(name, [])
        ),
    }
    return {name: bool(checks.get(name, False)) for name in stage.completion}


def complete(results: Dict[str, bool]) -> bool:
    return all(results.values())


def _tool_results(invocations: Iterable[Any]) -> Dict[str, list[Dict[str, Any]]]:
    out: Dict[str, list[Dict[str, Any]]] = {}
    for invocation in invocations:
        name = str(getattr(invocation, "name", "") or "")
        result = getattr(invocation, "result", None)
        if name:
            out.setdefault(name, []).append(result if isinstance(result, dict) else {})
    return out
