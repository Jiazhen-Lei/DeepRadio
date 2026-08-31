"""Project Workflow and ArtifactIndex facts into SharedState.

This module is intentionally one-way: execution owns Workflow state, while
SharedState exposes a compact, stable view to agents and the GUI.
"""

from __future__ import annotations

import json
from typing import Any

from ..state import ArtifactRecord


_RUNTIME_STATUS = {
    "pending": "planned",
    "running": "active",
    "waiting": "waiting",
    "completed": "succeeded",
    "errored": "failed",
    "invalidated": "planned",
}


def project_control(workflow_engine: Any, state: Any) -> None:
    digest = workflow_engine.digest()
    if not digest:
        return
    stage = workflow_engine.current_stage()
    checkpoint = stage.checkpoint if stage else None
    state.runtime.current_node = str(digest.get("current_stage") or "")
    state.runtime.status = _RUNTIME_STATUS.get(
        str(digest.get("execution_status") or "pending"), "planned"
    )
    if (
        str(digest.get("execution_status") or "") == "completed"
        and not any(
            str(getattr(item, "effect_level", "") or "") == "RF_RUN"
            and not bool(getattr(item, "safety_finalizer", False))
            for item in (getattr(workflow_engine.workflow, "stages", None) or [])
        )
    ):
        # Completing design/configuration work is not runtime success.
        state.runtime.status = "not_started"
    state.runtime.requested_effect = str(
        getattr(checkpoint, "requested_effect", "")
        or getattr(stage, "effect_level", "READ")
        or "READ"
    )
    state.runtime.blocker = dict(digest.get("blocker") or {})


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

    from ..state import ClaimStore

    results: dict[str, list[dict]] = {}
    for invocation in reply.tool_invocations or []:
        if isinstance(invocation.result, dict):
            results.setdefault(invocation.name, []).append(invocation.result)

    def latest(name: str, predicate) -> dict | None:
        return next(
            (item for item in reversed(results.get(name, [])) if predicate(item)),
            None,
        )

    discovery_attempted = bool(results.get("discover_devices"))
    probe_attempted = bool(results.get("probe_device"))
    discovered = latest("discover_devices", lambda item: item.get("device_found"))
    probed = latest("probe_device", lambda item: item.get("device_probed"))
    if (discovery_attempted or probe_attempted) and not (discovered and probed):
        # Never carry a physical-device success fact across a failed fresh
        # observation. Structural endpoints remain separate structure claims.
        state.project.config.pop("observed_device", None)
    if discovered and probed:
        state.project.config["observed_device"] = {
            "type": probed.get("device_type") or discovered.get("device_type"),
            "identity": probed.get("device_identity")
            or discovered.get("device_identity"),
            "driver_family": probed.get("driver_family")
            or discovered.get("driver_family"),
            "observed_at": probed.get("observed_at")
            or discovered.get("observed_at")
            or time.time(),
        }
        record_claim(
            "hardware_device_probed",
            "Selected SDR was discovered and probed by its explicit identity",
            "hardware",
            "discover_and_probe",
            state.project.config["observed_device"],
            True,
            artifact=str(
                probed.get("report_path") or discovered.get("report_path") or ""
            ),
        )
        probe_warnings = list(
            dict(probed.get("health") or {}).get("warnings") or []
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
    built_tx = latest(
        "build_sdr_tx_flowgraph",
        lambda item: item.get("ok") and item.get("valid") and item.get("compiled"),
    )
    if built_tx:
        report = str(built_tx.get("report_path") or "")
        record_claim(
            "final_flowgraph_valid",
            "Saved flowgraph passed structural validation and grcc compilation",
            "structure",
            "build_sdr_tx_flowgraph",
            {
                "grc_path": built_tx.get("grc_path"),
                "preview_topology_valid": built_tx.get("preview_topology_valid"),
                "compiled": built_tx.get("compiled"),
            },
            True,
            artifact=report,
        )
        record_claim(
            "hardware_endpoint_configured",
            "Requested SDR TX endpoint is present with the requested radio parameters",
            "structure",
            "build_sdr_tx_flowgraph",
            {
                "hardware": built_tx.get("hardware"),
                "sink_key": built_tx.get("sink_key"),
                "center_freq": built_tx.get("center_freq"),
                "sample_rate": built_tx.get("sample_rate"),
            },
            True,
            artifact=report,
        )
        record_claim(
            "rf_not_started",
            "Flowgraph artifact is in RF-safe preview mode and has not started RF",
            "hardware",
            "build_sdr_tx_flowgraph",
            {
                "armed": bool(built_tx.get("armed")),
                "not_started": bool(built_tx.get("not_started")),
                "preview_mode": built_tx.get("preview_mode"),
            },
            bool(built_tx.get("not_started") and not built_tx.get("armed")),
            artifact=report,
        )
    verified = latest("verify_ble_packet_bits", lambda item: item.get("valid"))
    if verified:
        record_claim(
            "ble_offline_protocol_valid",
            "BLE packet and IQ waveform passed independent offline validation",
            "structure",
            "verify_ble_packet_bits",
            {"checks": dict(verified.get("checks") or {})},
            True,
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
            "rf_not_started",
            "Flowgraph artifact is in RF-safe preview mode and has not started RF",
            "hardware",
            "start_flowgraph",
            {"run_id": started.get("run_id"), "running": True},
            False,
        )
        record_claim(
            "rf_runtime_started",
            "Bounded RF runtime was started by the controlled service",
            "hardware",
            "start_flowgraph",
            {
                "pid": started.get("pid"),
                "run_id": started.get("run_id"),
                "duration_seconds": started.get("duration_seconds"),
                "program": started.get("program"),
            },
            True,
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
            "rf_runtime_reached_terminal_state",
            "Controlled RF process reached a verified terminal state",
            "hardware",
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
        if not clean and ClaimStore(state).get("rf_runtime_started"):
            state.runtime.quality = "failed"
            record_claim(
                "rf_runtime_started",
                "Bounded RF runtime was started by the controlled service",
                "hardware",
                "runtime_failure",
                {
                    "run_id": terminal.get("run_id"),
                    "return_code": terminal.get("return_code"),
                    "reason": terminal.get("reason"),
                },
                False,
                artifact=str(terminal.get("log_path") or ""),
            )
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
        record_claim(
            "rf_runtime_underflow",
            "Hardware runtime reported scheduler underflow or overrun markers",
            "hardware",
            "runtime_stream_quality",
            warning,
            False,
            artifact=str((terminal or quality).get("log_path") or ""),
        )
