"""Unified, evidence-graded diagnosis across host, device, runtime and RF path."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from .hardware_profiles import normalize_hardware, resolve_hardware_profile
from .hardware_tools import discover_devices, probe_device
from .registry import ToolContext, tool


def _finding(
    check_id: str,
    dimension: str,
    status: str,
    observation: Any,
    *,
    remediation: str = "",
    evidence_grade: str = "system_verified",
    requires_human: bool = False,
) -> Dict[str, Any]:
    return {
        "check_id": check_id,
        "dimension": dimension,
        "status": status,
        "observation": observation,
        "remediation": remediation,
        "evidence_grade": evidence_grade,
        "requires_human": requires_human,
    }


@tool(
    name="run_diagnosis_checks",
    description=(
        "Create one read-only, evidence-graded diagnosis report covering requested "
        "device identity, driver availability, discovery/probe, parameter range, "
        "runtime status, and physical RF-path unknowns."
    ),
    parameters={
        "type": "object",
        "properties": {
            "device_type": {"type": "string"},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "live_probe": {"type": "boolean"}
        }
    },
    group="diagnosis",
    origin="deepradio_control_plane",
    runtime="host_and_vendor_cli",
    effect_level="DEVICE_READ",
)
def run_diagnosis_checks(
    ctx: ToolContext,
    device_type: str = "",
    dimensions: List[str] | None = None,
    live_probe: bool = True,
) -> Dict[str, Any]:
    state = ctx.extra.get("state")
    shared_intent = dict(ctx.extra.get("shared_intent") or {})
    parameters = dict(shared_intent.get("parameters") or {})
    requested = str(device_type or parameters.get("hardware") or "")
    profile = resolve_hardware_profile(requested)
    selected = set(dimensions or [])
    findings: List[Dict[str, Any]] = []

    def include(name: str) -> bool:
        return not selected or name in selected

    if include("intent"):
        findings.append(_finding(
            "intent.requested_device_supported",
            "intent",
            "pass" if profile else "fail",
            {"requested": requested, "normalized": normalize_hardware(requested)},
            remediation="Select a supported HardwareProfile or add a declarative profile." if not profile else "",
        ))

    if include("environment"):
        executable = ""
        if profile:
            command = profile.command(probe=False)
            executable = shutil.which(command[0]) or "" if command else ""
        findings.append(_finding(
            "environment.vendor_cli_available",
            "environment",
            "pass" if executable else "fail",
            {"driver_family": profile.driver_family if profile else "", "executable": executable},
            remediation="Install the corresponding UHD, IIO, or vendor driver in the GNU Radio environment and ensure its command is on PATH." if not executable else "",
        ))

    discovery: Dict[str, Any] = {}
    probe: Dict[str, Any] = {}
    if include("device") and profile and live_probe:
        discovery = discover_devices(ctx, device_type=profile.key)
        found = bool(discovery.get("device_found"))
        findings.append(_finding(
            "device.discovery",
            "device",
            "pass" if found else "fail",
            {
                "requested_type": profile.key,
                "observed_type": discovery.get("device_type"),
                "identity": discovery.get("device_identity"),
                "health": discovery.get("health") or {},
                "report_path": discovery.get("report_path") or "",
            },
            remediation=str(discovery.get("error") or "Check USB/network connectivity, power, permissions, and vendor drivers.") if not found else "",
        ))
        identity = str(discovery.get("device_identity") or "")
        identity_match = bool(
            found and normalize_hardware(discovery.get("device_type") or "") == profile.key
        )
        findings.append(_finding(
            "device.requested_observed_identity_match",
            "device",
            "pass" if identity_match else "fail",
            {"requested_type": profile.key, "observed_type": discovery.get("device_type")},
            remediation="Do not treat another device in the same driver family as the requested model; select again or rerun discovery." if not identity_match else "",
        ))
        if found:
            probe = probe_device(ctx, device_type=profile.key, device_args=identity)
        probed = bool(probe.get("device_probed"))
        findings.append(_finding(
            "device.exact_probe",
            "device",
            "pass" if probed else "fail",
            {
                "identity": probe.get("device_identity") or identity,
                "health": probe.get("health") or {},
                "report_path": probe.get("report_path") or "",
            },
            remediation=str(probe.get("error") or "Retry probing with the exact identity returned by discovery.") if not probed else "",
        ))
    elif include("device"):
        findings.append(_finding(
            "device.discovery",
            "device",
            "unknown",
            {"requested_type": requested, "live_probe": live_probe},
            remediation="Enable read-only discovery and probing before determining whether the device is connected.",
            evidence_grade="not_observed",
        ))

    if include("parameters") and profile:
        frequency = parameters.get("carrier_frequency")
        in_range = None
        if frequency is not None:
            try:
                in_range = profile.frequency_range[0] <= float(frequency) <= profile.frequency_range[1]
            except (TypeError, ValueError):
                in_range = False
        findings.append(_finding(
            "parameters.frequency_in_device_range",
            "parameters",
            "unknown" if in_range is None else "pass" if in_range else "fail",
            {"frequency": frequency, "supported_range": list(profile.frequency_range)},
            remediation="Provide a valid center frequency within the device's supported range." if in_range is not True else "",
            evidence_grade="intent_validated" if frequency is not None else "not_observed",
        ))

    if include("project"):
        project = getattr(state, "project", None)
        path = str(getattr(project, "grc_path", "") or "")
        findings.append(_finding(
            "project.flowgraph_present",
            "project",
            "pass" if path and os.path.isfile(path) else "unknown",
            {"grc_path": path},
            remediation="If the issue involves a flowgraph, open or generate a .grc project first; hardware-connectivity diagnosis alone does not require one." if not path else "",
            evidence_grade="filesystem_verified" if path else "not_observed",
        ))

    if include("runtime"):
        project = getattr(state, "project", None)
        runtime = dict(getattr(project, "config", {}).get("runtime") or {}) if project else {}
        runtime_status = str(runtime.get("status") or "not_started")
        findings.append(_finding(
            "runtime.process_status",
            "runtime",
            "fail" if runtime_status in {"crashed", "failed"} else "pass" if runtime_status == "running" else "unknown",
            {
                "status": runtime_status,
                "return_code": runtime.get("return_code"),
                "log_path": runtime.get("log_path") or "",
            },
            remediation="Inspect runtime.log and return_code; do not substitute 'flowgraph generated' for 'runtime succeeded'." if runtime_status in {"crashed", "failed"} else "",
            evidence_grade="runtime_observed" if runtime else "not_observed",
        ))

    if include("rf_path"):
        findings.append(_finding(
            "rf_path.physical_connection",
            "rf_path",
            "unknown",
            {
                "reason": "Host discovery and probing cannot prove that antennas, attenuators, RF ports, or cables are connected correctly"
            },
            remediation="Inspect ports, antennas, and attenuators manually, or add evidence from a wired loopback, power meter, spectrum analyzer, or independent sniffer.",
            evidence_grade="requires_external_evidence",
            requires_human=True,
        ))

    failures = [item for item in findings if item["status"] == "fail"]
    unknowns = [item for item in findings if item["status"] == "unknown"]
    report = {
        "schema_version": 1,
        "created_at": time.time(),
        "requested_device": requested,
        "summary": {
            "pass": sum(item["status"] == "pass" for item in findings),
            "fail": len(failures),
            "unknown": len(unknowns),
            "requires_human": sum(bool(item["requires_human"]) for item in findings),
        },
        "findings": findings,
        "project_unchanged": True,
    }
    directory = Path(ctx.out_dir or os.getcwd()) / "diagnosis"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "diagnosis_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.extra.setdefault("artifacts", {})["diagnosis_report"] = str(path)
    return {
        "ok": True,
        "diagnosis_complete": True,
        "diagnosis_passed": not failures,
        "report_path": str(path),
        "summary": report["summary"],
        "findings": findings,
        "project_unchanged": True,
    }
