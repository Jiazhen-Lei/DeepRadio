"""Project Workflow and ArtifactIndex facts into SharedState.

This module is intentionally one-way: execution owns Workflow state, while
SharedState exposes a compact, stable view to agents and the GUI.
"""

from __future__ import annotations

import json
from typing import Any

from ..state import ArtifactRecord


def project_artifact_index(
    state: Any, manifest_path: str, *, workflow: Any = None
) -> None:
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError):
        return

    revision = int(getattr(workflow, "revision", 0) or 0)
    version = int(state.project.flowgraph_version)
    previous = {
        item.artifact_id: item for item in state.artifacts if item.artifact_id
    }
    records = []
    for item in payload.get("artifacts") or []:
        if not isinstance(item, dict) or not item.get("artifact_id"):
            continue
        artifact_id = str(item["artifact_id"])
        prior = previous.get(artifact_id)
        records.append(ArtifactRecord(
            artifact_id=artifact_id,
            role=str(item.get("role") or "artifact"),
            path=str(item.get("path") or ""),
            sha256=str(item.get("sha256") or ""),
            size=int(item.get("size") or 0),
            producer=prior.producer if prior else str(
                state.runtime.current_node or ""
            ),
            workflow_revision=prior.workflow_revision if prior else revision,
            project_version=prior.project_version if prior else version,
        ))
    state.artifacts = records


def project_tool_results(
    state: Any,
    reply: Any,
    *,
    record_claim: Any,
    semantic_hash: Any,
) -> None:
    """Project host-observed tool facts identically for every executor."""
    import time

    results: dict[str, list[dict]] = {}
    for invocation in reply.tool_invocations or []:
        if isinstance(invocation.result, dict):
            results.setdefault(invocation.name, []).append(invocation.result)

    def latest(name: str, predicate) -> dict | None:
        return next(
            (item for item in reversed(results.get(name, [])) if predicate(item)),
            None,
        )

    discovery = (
        results.get("discover_devices", [])[-1]
        if results.get("discover_devices") else None
    )
    probe = (
        results.get("probe_device", [])[-1]
        if results.get("probe_device") else None
    )
    detection = None
    if discovery is not None:
        state.project.config.pop("observed_device", None)
        found = bool(discovery.get("device_found"))
        detection = {
            "state": "detected" if found else (
                "not_found" if discovery.get("ok") else "failed"
            ),
            "device_type": discovery.get("device_type"),
            "device_label": discovery.get("device_label"),
            "identity": discovery.get("device_identity"),
            "driver_family": discovery.get("driver_family"),
            "observed_at": discovery.get("observed_at") or time.time(),
            "error": "" if found else str(
                discovery.get("error")
                or discovery.get("mismatch_hint")
                or "No matching SDR was found."
            ),
            "workflow_id": state.intent.workflow_id,
        }
    if probe is not None:
        probed = bool(probe.get("device_probed"))
        detection = {
            "state": "detected" if probed else "failed",
            "device_type": probe.get("device_type"),
            "device_label": probe.get("device_label"),
            "identity": probe.get("device_identity"),
            "driver_family": probe.get("driver_family"),
            "observed_at": probe.get("observed_at") or time.time(),
            "error": "" if probed else str(
                probe.get("error") or "Hardware probe failed."
            ),
            "workflow_id": state.intent.workflow_id,
        }
        if not probed:
            state.project.config.pop("observed_device", None)
    if detection is not None:
        state.project.config["hardware_detection"] = detection
    if probe is not None:
        observed_device = {
            "type": probe.get("device_type"),
            "identity": probe.get("device_identity"),
            "driver_family": probe.get("driver_family"),
            "observed_at": probe.get("observed_at") or time.time(),
        }
        if probe.get("device_probed"):
            state.project.config["observed_device"] = observed_device
        record_claim(
            "hardware_device_probed",
            "Selected SDR is available and satisfies the required device capabilities.",
            "hardware",
            "discover_and_probe",
            observed_device if probe.get("device_probed") else detection,
            True if probe.get("device_probed") else (
                False if probe.get("ok") else None
            ),
            artifact=str(probe.get("report_path") or ""),
        )
        probe_warnings = list(
            dict(probe.get("health") or {}).get("warnings") or []
        )
        state.runtime.warnings = [
            item for item in state.runtime.warnings
            if item.get("code") != "device_probe_warning"
        ]
        if probe_warnings:
            if state.runtime.quality != "failed":
                state.runtime.quality = "warning"
            state.runtime.warnings.append({
                "code": "device_probe_warning",
                "message": (
                    "The device identity and core probe passed, but optional "
                    "driver attributes reported warnings."
                ),
                "details": probe_warnings,
            })
    validation_results = (
        results.get("validate_flowgraph") or results.get("validate") or []
    )
    validation = validation_results[-1] if validation_results else None
    if validation is not None:
        record_claim(
            "final_flowgraph_valid",
            "Current flowgraph has valid blocks, ports, parameters, and connections.",
            "flowgraph",
            "validate_flowgraph",
            {
                "valid": validation.get("valid"),
                "errors": list(validation.get("errors") or []),
            },
            bool(validation.get("valid")) if "valid" in validation else None,
            artifact=str(validation.get("report_path") or ""),
        )
    verified_results = results.get("verify_ble_packet_bits") or []
    verified = verified_results[-1] if verified_results else None
    if verified is not None:
        record_claim(
            "ble_offline_protocol_valid",
            "Generated BLE advertising data conforms to the requested packet fields and checksum.",
            "protocol",
            "verify_ble_packet_bits",
            {"checks": dict(verified.get("checks") or {})},
            bool(verified.get("valid")) if "valid" in verified else None,
        )
    armed = latest(
        "arm_hardware_flowgraph",
        lambda item: item.get("ok") and item.get("armed"),
    )
    if armed:
        state.project.config["rf_armed"] = True
        state.project.config["rf_armed_path"] = armed.get("grc_path")
        hashed = semantic_hash(str(armed.get("grc_path") or ""))
        if hashed:
            state.project.config["flowgraph_semantic_hash"] = hashed
        record_claim(
            "final_flowgraph_valid",
            "Current flowgraph has valid blocks, ports, parameters, and connections.",
            "flowgraph",
            "arm_hardware_flowgraph",
            {
                "grc_path": armed.get("grc_path"),
                "compiled": bool(dict(armed.get("compile") or {}).get("compiled")),
            },
            True,
            artifact=str(armed.get("report_path") or ""),
        )
    started = latest(
        "start_flowgraph",
        lambda item: (
            item.get("ok") and item.get("running") and item.get("ready")
            and item.get("startup_health_passed") and item.get("run_id")
        ),
    )
    if started:
        state.project.config["rf_started"] = True  # compatibility key
        state.project.config["rf_ever_started"] = True
        state.project.config["rf_active"] = True
        record_claim(
            "bounded_runtime_healthy",
            "Bounded radio execution completes without a runtime error.",
            "runtime",
            "start_flowgraph",
            {
                "pid": started.get("pid"),
                "run_id": started.get("run_id"),
                "duration_seconds": started.get("duration_seconds"),
                "program": started.get("program"),
            },
            None,
        )
    elif results.get("start_flowgraph"):
        attempt = results["start_flowgraph"][-1]
        record_claim(
            "bounded_runtime_healthy",
            "Bounded radio execution completes without a runtime error.",
            "runtime",
            "start_flowgraph",
            {
                "error": attempt.get("error"),
                "policy": attempt.get("policy"),
                "run_id": attempt.get("run_id"),
            },
            None,
        )
    terminal = next(
        (
            item
            for name in (
                "stop_flowgraph", "emergency_stop", "query_runtime_status"
            )
            for item in reversed(results.get(name, []))
            if item.get("run_id") and not item.get("running")
        ),
        None,
    )
    if terminal:
        state.project.config["rf_active"] = False
        clean = bool(
            terminal.get("ok")
            and not terminal.get("crashed")
            and terminal.get("reason")
            in {"stopped", "emergency_stop", "exited"}
            and terminal.get("return_code") in (0, -15, -9)
        )
        record_claim(
            "bounded_runtime_healthy",
            "Bounded radio execution completes without a runtime error.",
            "runtime",
            "runtime_terminal_status",
            {
                "run_id": terminal.get("run_id"),
                "reason": terminal.get("reason"),
                "return_code": terminal.get("return_code"),
                "crashed": bool(terminal.get("crashed")),
            },
            clean,
            artifact=str(terminal.get("log_path") or ""),
        )
        if not clean:
            state.runtime.quality = "failed"
    quality_samples = [
        item
        for name in (
            "start_flowgraph", "query_runtime_status",
            "stop_flowgraph", "emergency_stop",
        )
        for item in results.get(name, [])
        if "underrun_count" in item or "overrun_count" in item
    ]
    quality = quality_samples[-1] if quality_samples else {}
    underruns = int(quality.get("underrun_count") or 0)
    overruns = int(quality.get("overrun_count") or 0)
    if underruns or overruns:
        state.runtime.quality = "warning" \
            if state.runtime.quality != "failed" else "failed"
        warning = {
            "code": "rf_stream_quality",
            "underrun_count": underruns,
            "overrun_count": overruns,
            "run_id": (terminal or started or quality).get("run_id"),
        }
        state.runtime.warnings = [
            item for item in state.runtime.warnings
            if item.get("code") != "rf_stream_quality"
        ] + [warning]
