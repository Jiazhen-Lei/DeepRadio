"""Deterministic checks for Task Catalog Stage completion contracts."""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Iterable


MUTATING_TOOLS = frozenset(
    {
        "design_link",
        "design_flowgraph",
        "render_grc",
        "apply_grc_diff",
        "apply_flowgraph_patch",
        "build_ble_uhd_tx_flowgraph",
        "build_ble_pluto_tx_flowgraph",
        "arm_hardware_flowgraph",
        "build_usrp_rx_spectrum_flowgraph",
        "build_sdr_rx_spectrum_flowgraph",
        "build_sdr_tx_flowgraph",
    }
)
_BUILD_SAVE_STAGES = frozenset(
    {
        "apply_and_verify",
        "build_and_verify",
        "tx_build_and_validate",
        "rx_build_and_verify",
    }
)


def tool_payload_succeeded(payload: Any) -> bool:
    """Treat DENY / explicit errors as failure even when ``ok`` is omitted."""
    if not isinstance(payload, dict):
        return bool(payload)
    if payload.get("ok") is False:
        return False
    if str(payload.get("policy") or "").upper() == "DENY":
        return False
    if payload.get("error") and payload.get("ok") is not True:
        return False
    return True


def invocation_succeeded(item: Any) -> bool:
    result = getattr(item, "result", None)
    explicit = getattr(item, "ok", None)
    if result is None and isinstance(item, dict):
        result = item.get("result")
        explicit = item.get("ok")
    if isinstance(result, dict) and not tool_payload_succeeded(result):
        return False
    if explicit is False:
        return False
    if explicit is True:
        return True
    return tool_payload_succeeded(result) if isinstance(result, dict) else bool(result)


def _mutated_flowgraph(invocations: Iterable[Any]) -> bool:
    for item in invocations:
        name = getattr(item, "name", None)
        if name is None and isinstance(item, dict):
            name = item.get("name")
        if name in MUTATING_TOOLS and invocation_succeeded(item):
            return True
    return False


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
        "flowgraph_armed",
        "hardware_check_completed",
        "ble_packet_created",
        "ble_waveform_generated",
        "ble_packet_valid",
        "device_discovered",
        "device_probed",
        "device_identity_matched",
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


#: Completion predicates that describe external preconditions (device presence,
#: endpoint armed, parameters echoed by hardware) rather than execution
#: quality.  When only these are unmet, the stage is parked as ``waiting``
#: instead of ``failed`` so the user can fix the condition and retry.
EXTERNAL_PRECONDITION_COMPLETIONS = frozenset({
    "hardware_endpoint_present",
    "radio_parameters_match",
    "realtime_sink_present",
    "device_discovered",
    "device_probed",
    "device_identity_matched",
    "flowgraph_armed",
    "hardware_check_completed",
    "hardware_precheck_completed",
    "configuration_recorded",
})

#: User-facing explanations for the predicates above.
EXTERNAL_PRECONDITION_NOTES = {
    "hardware_endpoint_present": (
        "the flowgraph has no active SDR hardware output yet"
    ),
    "radio_parameters_match": (
        "the hardware parameters do not match the requested "
        "frequency or sample rate"
    ),
    "realtime_sink_present": "no live spectrum display is present yet",
    "device_discovered": "no SDR device was discovered",
    "device_probed": "the SDR device could not be probed",
    "device_identity_matched": (
        "the connected device does not match the requested SDR model"
    ),
    "flowgraph_armed": "the hardware output has not been armed yet",
    "hardware_check_completed": "the hardware check has not completed yet",
    "hardware_precheck_completed": "the hardware pre-check has not completed yet",
    "configuration_recorded": "the device configuration has not been recorded yet",
}


def external_waiting_note(missing: list) -> str:
    """Friendly explanation shown while waiting on external preconditions."""
    reasons = [
        EXTERNAL_PRECONDITION_NOTES.get(
            name, str(name).replace("_", " ")
        )
        for name in missing
    ]
    return (
        "Waiting on hardware conditions: {}. "
        "Connect or re-check the SDR, then press Retry; "
        "I will re-check the device first.".format("; ".join(dict.fromkeys(reasons)))
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
    runtime_state = dict(getattr(project, "config", {}).get("runtime") or {})
    raw_open_questions = getattr(
        getattr(state, "spec", None), "open_questions", None
    )
    state_open_questions = list(raw_open_questions) if isinstance(
        raw_open_questions, (list, tuple)
    ) else []
    raw_missing = getattr(workflow.intent, "missing_slots", [])
    raw_validation = getattr(workflow.intent, "validation_errors", [])
    intent_missing = list(raw_missing) if isinstance(raw_missing, (list, tuple)) else []
    intent_validation = list(raw_validation) if isinstance(
        raw_validation, (list, tuple)
    ) else []
    intent_incomplete = bool(
        intent_missing or intent_validation or state_open_questions
    )

    def runtime_start_accepted(item: Dict[str, Any]) -> bool:
        return bool(
            item.get("ok")
            and item.get("running")
            and item.get("ready")
            and item.get("startup_health_passed")
            and item.get("run_id")
        )

    def runtime_stop_accepted(item: Dict[str, Any]) -> bool:
        return bool(
            item.get("ok")
            and not item.get("running")
            and not item.get("crashed")
            and item.get("run_id")
            and item.get("reason") in {"stopped", "emergency_stop", "exited"}
            and item.get("return_code") in (0, -15, -9)
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
        return any(
            tool_payload_succeeded(result) for result in tool_results.get(name, [])
        )

    def device_identity_matched() -> bool:
        config = dict(getattr(project, "config", {}) or {})
        observed = dict(config.get("observed_device") or {})
        desired = dict(config.get("desired_device") or {})
        requested = str(slots.get("hardware") or desired.get("type") or "").lower()
        actual = str(observed.get("type") or "").lower()
        aliases = {
            "plutosdr": "pluto", "adalm-pluto": "pluto",
            "b200": "b210", "usrp_b210": "b210",
        }
        requested = aliases.get(requested, requested)
        actual = aliases.get(actual, actual)
        return bool(
            requested
            and actual == requested
            and observed.get("identity")
            and checked("discover_devices")
            and checked("probe_device")
        )

    def stage_passed(stage_id: str) -> bool:
        return any(
            getattr(item, "id", "") == stage_id
            and getattr(item, "execution_status", "") == "completed"
            and getattr(item, "outcome", "") == "passed"
            for item in (getattr(workflow, "stages", None) or [])
        )

    def structural_validation() -> bool:
        results = tool_results.get("validate", []) + tool_results.get("validate_flowgraph", [])
        if any(
            tool_payload_succeeded(item) and bool(item.get("valid")) for item in results
        ):
            return True
        if any(
            tool_payload_succeeded(item) and bool(item.get("valid"))
            for item in tool_results.get("design_link", [])
        ):
            return True
        return any(
            tool_payload_succeeded(item) and bool(item.get("valid"))
            for name in (
                "build_usrp_rx_spectrum_flowgraph",
                "build_sdr_tx_flowgraph",
                "build_ble_uhd_tx_flowgraph",
                "build_ble_pluto_tx_flowgraph",
            )
            for item in tool_results.get(name, [])
        )

    def claims_current() -> bool:
        affected = [claim for claim in claims if getattr(claim, "layer", "") in ("sim", "structure")]
        return all(
            int(getattr(claim, "project_version", -1)) == project_version
            and getattr(claim, "status", "NotTested") not in ("NotTested", "Stale")
            for claim in affected
        )

    def current_passed_claim(claim_id: str) -> bool:
        return any(
            getattr(claim, "id", "") == claim_id
            and int(getattr(claim, "project_version", -1)) == project_version
            and getattr(claim, "status", "NotTested") == "Passed"
            and bool(getattr(claim, "evidence", None))
            for claim in claims
        )

    def requested_measurements() -> bool:
        requested = list(slots.get("requested_metrics") or [])
        if not requested:
            return structural_validation() and bool(
                metrics or checked("simulate") or checked("design_link")
            )
        spectrum_report = metrics.get("spectrum_peak_report")
        spectrum_valid = bool(
            isinstance(spectrum_report, dict)
            and spectrum_report.get("valid")
            and spectrum_report.get("frequency_hz") is not None
            and spectrum_report.get("magnitude_dbfs") is not None
            and spectrum_report.get("fft_size")
            and spectrum_report.get("sample_rate")
        )
        available = {
            "evm": metrics.get("evm_pct") is not None,
            "ber": metrics.get("ber") is not None,
            "spectrum": spectrum_valid and current_passed_claim(
                "spectrum_peak_measured"
            ),
            "constellation": bool(artifacts.get("constellation_png")),
            "eye": bool(artifacts.get("eye_png")),
        }
        return all(available.get(name, False) for name in requested)

    checks = {
        "required_slots_complete": not intent_incomplete,
        "flowgraph_saved": (
            bool(grc_path and os.path.isfile(grc_path))
            and (
                getattr(stage, "id", "") not in _BUILD_SAVE_STAGES
                or _mutated_flowgraph(invocations)
            )
        ),
        "structural_validation_completed": structural_validation(),
        "affected_claims_evaluated": claims_current() or any(
            item.get("ok") and item.get("affected_claims_evaluated") is True
            for item in tool_results.get("apply_flowgraph_patch", [])
        ),
        "receive_quality_evaluated": _receive_quality_evaluated(
            metrics, artifacts, slots, tool_results, claims, project_version
        ),
        "diagnosis_created": bool(
            any(
                item.get("ok") and item.get("diagnosis_complete")
                and item.get("report_path")
                and item.get("project_unchanged") is True
                for item in tool_results.get("run_diagnosis_checks", [])
            )
            or
            any(
                item.get("ok") and item.get("report_path")
                and item.get("project_unchanged") is True
                for item in tool_results.get("run_diagnosis_experiment", [])
            )
            or tool_names.intersection({"diagnose_by_metric", "explain_error"})
        ),
        "repair_decision_recorded": True,
        "repair_applied": _mutated_flowgraph(invocations),
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
        "flowgraph_armed": bool(
            getattr(project, "config", {}).get("rf_armed")
            and any(
                bool(item.get("ok")) and bool(item.get("armed"))
                for item in tool_results.get("arm_hardware_flowgraph", [])
            )
        ),
        "hardware_check_completed": _hardware_check_completed(
            project, tool_results, slots
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
        "device_identity_matched": device_identity_matched(),
        "rf_plan_approved": stage_passed("rf_plan_confirmation"),
        "transmit_started": any(
            runtime_start_accepted(item)
            for item in tool_results.get("start_flowgraph", [])
        ),
        "over_air_observed": bool(
            slots.get("over_air_observed")
            and (slots.get("ota_observation") or {}).get("run_id")
            == runtime_state.get("run_id")
        ),
        "transmit_stopped": any(
            runtime_stop_accepted(item)
            for name in ("stop_flowgraph", "emergency_stop", "query_runtime_status")
            for item in tool_results.get(name, [])
        ),
        "hardware_endpoint_present": hardware_endpoint(),
        "radio_parameters_match": radio_parameters(),
        "realtime_sink_present": bool(block_keys().intersection({
            "qtgui_freq_sink_x", "qtgui_waterfall_sink_x",
        })),
        "runtime_started": any(
            runtime_start_accepted(item)
            for item in tool_results.get("start_flowgraph", [])
        ),
        "runtime_observation_recorded": True,
        "runtime_stopped": any(
            runtime_stop_accepted(item)
            for name in ("stop_flowgraph", "emergency_stop", "query_runtime_status")
            for item in tool_results.get(name, [])
        ),
    }
    result = {name: bool(checks.get(name, False)) for name in stage.completion}
    # A stale/open specification invalidates every autonomous completion.  This
    # prevents OBSERVE from completing while state.json still asks a question.
    if intent_incomplete:
        result = {name: False for name in result}
    return result


def _hardware_check_completed(
    project: Any,
    tool_results: Dict[str, list[Dict[str, Any]]],
    slots: Dict[str, Any],
) -> bool:
    device = dict(getattr(project, "config", {}).get("device") or {})
    if not device:
        return False
    recorded = (
        device.get("configuration_mode") == "recorded"
        or device.get("mode") in {"configuration_recorded", "flowgraph_config_only"}
    )
    if not recorded:
        return False
    preflight_ok = any(
        bool(item.get("ok"))
        and not list(item.get("missing") or [])
        for item in tool_results.get("hardware_preflight", [])
    )
    if not preflight_ok:
        return False

    def same_number(left: Any, right: Any) -> bool:
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return abs(a - b) <= max(abs(b) * 1e-9, 1e-6)

    expected_type = str(slots.get("hardware") or "").lower()
    actual_type = str(device.get("type") or "").lower()
    aliases = {
        "plutosdr": "pluto", "adalm-pluto": "pluto",
        "b200": "b210", "usrp_b210": "b210",
    }
    type_matches = (
        not expected_type
        or aliases.get(actual_type, actual_type)
        == aliases.get(expected_type, expected_type)
    )
    return bool(
        type_matches
        and same_number(device.get("center_freq"), slots.get("carrier_frequency"))
        and same_number(device.get("sample_rate"), slots.get("sample_rate"))
    )


def complete(results: Dict[str, bool]) -> bool:
    return all(results.values())


_RANDOM_BER_CEILING = 0.45


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _receive_quality_evaluated(
    metrics: Dict[str, Any],
    artifacts: Dict[str, Any],
    slots: Dict[str, Any],
    tool_results: Dict[str, list[Dict[str, Any]]],
    claims: Iterable[Any],
    project_version: int,
) -> bool:
    """BER must be finite and clearly below random; otherwise EVM may prove RX."""
    requested = list(slots.get("requested_metrics") or [])
    ber = _finite_number(metrics.get("ber"))
    report = metrics.get("ber_report")
    evm = _finite_number(metrics.get("evm_pct"))
    if "ber" in requested or ber is not None:
        if ber is None or ber >= _RANDOM_BER_CEILING:
            return False
        if not _valid_ber_report(report):
            return False
        return (
            _ber_probes_present(artifacts, tool_results)
            and _claim_passed(claims, "ber_measured", project_version)
        )
    return evm is not None


def _valid_ber_report(report: Any) -> bool:
    if not isinstance(report, dict) or not report.get("valid"):
        return False
    try:
        compared = int(report.get("compared_bits") or 0)
        errors = int(report.get("bit_errors") or 0)
        value = float(report.get("value"))
    except (TypeError, ValueError):
        return False
    if compared < 256 or errors < 0 or errors > compared:
        return False
    if abs(value - (errors / compared)) > max(1e-12, 1.0 / compared):
        return False
    return bool(
        report.get("alignment_method")
        and report.get("tx_probe")
        and report.get("rx_probe")
    )


def _claim_passed(claims: Iterable[Any], claim_id: str, version: int) -> bool:
    return any(
        getattr(claim, "id", "") == claim_id
        and int(getattr(claim, "project_version", -1)) == int(version)
        and getattr(claim, "status", "NotTested") == "Passed"
        and bool(getattr(claim, "evidence", None))
        for claim in claims
    )


def _ber_probes_present(
    artifacts: Dict[str, Any],
    tool_results: Dict[str, list[Dict[str, Any]]],
) -> bool:
    """Fail only when simulation evidence is present but TX/RX probes are not."""
    names: set[str] = set()
    extra = artifacts.get("probes") or artifacts.get("probe_sizes")
    if isinstance(extra, dict):
        names.update(str(name) for name in extra)
    nested = artifacts.get("metrics")
    if isinstance(nested, dict):
        report = nested.get("ber_report")
        if isinstance(report, dict):
            for key in ("tx_probe", "rx_probe"):
                if report.get(key):
                    names.add(str(report[key]))
    for result in (
        tool_results.get("run_simulation", [])
        + tool_results.get("simulate", [])
        + tool_results.get("design_link", [])
    ):
        for key in ("probes", "probe_sizes"):
            value = result.get(key)
            if isinstance(value, dict):
                names.update(str(name) for name in value)
    if not names:
        return False
    lowered = {name.lower() for name in names}
    has_tx = any("tx" in name for name in lowered)
    has_rx = any(
        name in {"sink", "rx"} or "rx" in name
        for name in lowered
    )
    return has_tx and has_rx


def _tool_results(invocations: Iterable[Any]) -> Dict[str, list[Dict[str, Any]]]:
    out: Dict[str, list[Dict[str, Any]]] = {}
    for invocation in invocations:
        name = str(getattr(invocation, "name", "") or "")
        result = getattr(invocation, "result", None)
        if name:
            out.setdefault(name, []).append(result if isinstance(result, dict) else {})
    return out
