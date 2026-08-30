"""Driver-specific SDR discovery and explicitly gated RF runtime tools.

``discover_devices`` / ``probe_device`` wrap vendor CLIs (uhd_find_devices,
iio_info, …). ``arm_hardware_flowgraph`` / ``start_flowgraph`` are DeepRadio
runtime gates around GNU Radio ``grcc`` + a bounded subprocess. Agents must
call these registry tools; GNU Radio has no BLE ADV or device-discovery API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from ..service.hardware_runtime import RUNTIME
from .hardware_profiles import (
    device_args_for,
    iter_profiles,
    normalize_hardware,
    output_indicates_device,
    output_indicates_successful_probe,
    parse_device_identity,
    resolve_hardware_profile,
)
from .registry import ToolContext, call, tool


def _grc_number(value: float) -> str:
    """Render a numeric literal without introducing an invalid float suffix.

    GRC propagates expression types between connected block parameters.  A
    mathematically integral sample rate therefore needs an integer literal
    (``2000000``), while genuinely fractional values keep their precision.
    """
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return format(number, ".15g")


def _run(command: list[str], timeout: float = 15.0) -> Dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {"ok": False, "error": f"Command unavailable: {command[0]}"}
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "Hardware command timed out", "output": str(exc.stdout or "")}
    return {
        "ok": completed.returncode == 0,
        "return_code": completed.returncode,
        "output": completed.stdout[-12000:],
        "command": command,
    }


def _probe_health(output: str, *, identity_ok: bool) -> Dict[str, Any]:
    """Classify vendor output without treating every optional attr as fatal."""
    text = str(output or "")
    low = text.lower()
    warning_markers = (
        "socket operation on non-socket",
        "out of sync",
        "attribute read error",
        "not supported",
    )
    fatal_markers = (
        "no devices found",
        "unable to create context",
        "permission denied",
        "device or resource busy",
        "connection refused",
    )
    warnings = [marker for marker in warning_markers if marker in low]
    fatal_errors = [marker for marker in fatal_markers if marker in low]
    return {
        "identity_ok": bool(identity_ok),
        "core_ready": bool(identity_ok and not fatal_errors),
        "warnings": warnings,
        "fatal_errors": fatal_errors,
    }


def _compact_vendor_output(output: str, limit: int = 2000) -> str:
    text = str(output or "")[-max(200, int(limit)):]
    return re.sub(
        r"(?im)(serial\s*[:=]\s*)([^,\s]+)", r"\1<redacted>", text
    )


def _resolve_work_path(ctx: ToolContext, path: str) -> Path:
    candidate = Path(path or "")
    if candidate.is_absolute():
        return candidate.resolve()
    from ..service.session_store import session_root

    return (Path(session_root(_session_id(ctx))) / path).resolve()


def _session_id(ctx: ToolContext) -> str:
    return str(getattr(ctx.extra.get("state"), "session_id", "") or "default")


def _resolve_gnuradio_python(device_type: str = "") -> Dict[str, Any]:
    """Select an explicit interpreter that can import the required runtime."""
    required = ["gr", "blocks"]
    profile = resolve_hardware_profile(device_type)
    if profile and profile.driver_family == "iio":
        required.append("iio")
    elif profile and profile.driver_family == "uhd":
        required.append("uhd")
    statement = "from gnuradio import {}".format(", ".join(required))
    configured = str(os.environ.get("GRC_AGENT_PYTHON") or "").strip()
    grcc = shutil.which("grcc") or ""
    candidates = [configured, sys.executable]
    if grcc:
        candidates.extend([
            str(Path(grcc).with_name("python")),
            str(Path(grcc).with_name("python3")),
        ])
    candidates.extend([shutil.which("python") or "", shutil.which("python3") or ""])
    checked = []
    for candidate in dict.fromkeys(item for item in candidates if item):
        executable = shutil.which(candidate)
        if not executable:
            checked.append({"interpreter": candidate, "ok": False, "error": "not executable"})
            continue
        result = _run([executable, "-c", statement], timeout=10.0)
        checked.append({
            "interpreter": executable,
            "ok": bool(result.get("ok")),
            "output": str(result.get("output") or "")[-1000:],
        })
        if result.get("ok"):
            return {"ok": True, "interpreter": executable, "required": required}
    return {
        "ok": False,
        "error": "No Python interpreter capable of importing GNU Radio hardware modules was found",
        "required": required,
        "checked": checked,
    }


def _project_runtime_state(ctx: ToolContext, result: Dict[str, Any]) -> None:
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    if project is None:
        return
    status = (
        "running" if result.get("running")
        else "crashed" if result.get("crashed")
        else "stopped" if result.get("reason") in {"stopped", "emergency_stop"}
        else "exited" if result.get("reason") == "exited"
        else "failed" if result.get("ok") is False
        else "idle"
    )
    runtime = dict(project.config.get("runtime") or {})
    for key in (
        "run_id", "pid", "program", "interpreter", "started_at", "deadline",
        "stopped_at", "return_code", "reason", "crashed", "ready",
        "duration_seconds", "log_path",
    ):
        if key in result:
            runtime[key] = result.get(key)
    if "duration_seconds" in result:
        runtime["max_duration_seconds"] = result.get("duration_seconds")
    runtime["status"] = status
    runtime["running"] = bool(result.get("running"))
    project.config["runtime"] = runtime
    if not result.get("running") and status in {"crashed", "stopped", "exited", "failed"}:
        project.config["rf_armed"] = False
        project.config.pop("rf_armed_path", None)


def _rf_approved(ctx: ToolContext) -> bool:
    """Check a typed effect grant; checkpoint names are not policy facts."""
    ranks = {
        "READ": 0,
        "ARTIFACT_WRITE": 1,
        "DEVICE_READ": 2,
        "DEVICE_CONFIG": 3,
        "RF_RUN": 4,
    }
    state = ctx.extra.get("state")
    runtime = getattr(state, "runtime", None)
    grants = list(getattr(runtime, "granted_effects", None) or [])
    if any(ranks.get(str(effect).upper(), 0) >= ranks["RF_RUN"] for effect in grants):
        return True
    workflow = ctx.extra.get("workflow") or {}
    return any(
        (stage.get("checkpoint") or {}).get("decision_status") == "approved"
        and ranks.get(
            str((stage.get("checkpoint") or {}).get("requested_effect") or "").upper(),
            -1,
        ) >= ranks["RF_RUN"]
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict)
    )


def _completion_satisfied(ctx: ToolContext, predicate: str) -> bool:
    """Resolve a stable completion fact without depending on Stage names."""
    state = ctx.extra.get("state")
    for claim in list(getattr(state, "claims", None) or []):
        if (
            str(getattr(claim, "id", "") or "") == predicate
            and str(getattr(claim, "status", "") or "").lower() in {"pass", "passed"}
        ):
            return True
    workflow = ctx.extra.get("workflow") or {}
    return any(
        stage.get("execution_status") == "completed"
        and stage.get("outcome") == "passed"
        and bool(
            ((stage.get("result") or {}).get("completion") or {}).get(predicate)
        )
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict)
    )


def _is_ble_deploy(ctx: ToolContext) -> bool:
    intent = (ctx.extra.get("workflow") or {}).get("intent") or {}
    slots = intent.get("slots") or {}
    return str(slots.get("protocol") or "").lower() == "ble"


def _rf_armed(ctx: ToolContext, grc_path: str) -> bool:
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    config = dict(getattr(project, "config", {}) or {})
    return bool(
        config.get("rf_armed")
        and _resolve_work_path(ctx, str(getattr(project, "grc_path", "") or ""))
        == _resolve_work_path(ctx, grc_path)
    )


def _tx_requires_arming(ctx: ToolContext) -> bool:
    intent = (ctx.extra.get("workflow") or {}).get("intent") or {}
    slots = intent.get("slots") or {}
    return bool(
        _is_ble_deploy(ctx)
        or str(slots.get("direction") or "").lower() == "tx"
    )


def _persist_hardware_report(
    ctx: ToolContext, name: str, payload: Dict[str, Any]
) -> str:
    """Persist a host-observed hardware fact and register it as an artifact."""
    directory = Path(ctx.out_dir or os.getcwd()) / "hardware_reports"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        return ""
    ctx.extra.setdefault("artifacts", {})[path.stem] = str(path)
    return str(path)


def _compile_grc(path: str) -> Dict[str, Any]:
    """Compile a saved GRC in a temporary directory without running it."""
    if not shutil.which("grcc"):
        return {"ok": False, "error": "Command unavailable: grcc", "compiled": False}
    try:
        with tempfile.TemporaryDirectory(prefix="deepradio-grcc-") as build_dir:
            result = _run(["grcc", "-o", build_dir, path], timeout=30.0)
    except OSError as exc:
        return {"ok": False, "error": f"Unable to create a temporary compilation directory: {exc}", "compiled": False}
    result["compiled"] = bool(result.get("ok"))
    return result


def _device_command(device_type: str, *, probe: bool) -> list[str]:
    profile = resolve_hardware_profile(device_type)
    return profile.command(probe=probe) if profile else []


def _scan_other_families(skip_family: str) -> list[Dict[str, Any]]:
    """Read-only cross-family scan for when the expected SDR is absent.

    Runs each other driver family's discovery command (``iio_info -S usb``,
    ``hackrf_info``, ...) so a PlutoSDR is still visible when the caller
    expected a UHD device, and vice versa.  Never opens an RF stream.
    """
    found: list[Dict[str, Any]] = []
    seen_families: set[str] = set()
    if skip_family:
        seen_families.add(skip_family)
    for profile in iter_profiles():
        if profile.driver_family in seen_families:
            continue
        seen_families.add(profile.driver_family)
        command = profile.command(probe=False)
        if not command:
            continue
        try:
            result = _run(command, timeout=10.0)
        except Exception:  # noqa: BLE001 - diagnostic scan must not raise
            continue
        output = str(result.get("output") or "")
        if not (result.get("ok") and output_indicates_device(profile, output)):
            continue
        found.append({
            "device_type": profile.key,
            "device_label": profile.label,
            "driver_family": profile.driver_family,
            "device_identity": parse_device_identity(profile, output),
        })
    return found


@tool(
    name="discover_devices",
    description=(
        "Vendor CLI wrapper: read-only SDR discovery via uhd_find_devices / "
        "iio_info. Never opens an RF stream. Not a GNU Radio block."
    ),
    parameters={"type": "object", "properties": {
        "device_args": {"type": "string"},
        "device_type": {"type": "string"},
    }},
    group="hardware",
    origin="vendor_cli",
    runtime="uhd_iio",
    effect_level="DEVICE_READ",
)
def discover_devices(
    ctx: ToolContext, device_args: str = "", device_type: str = "b210"
) -> Dict[str, Any]:
    profile = resolve_hardware_profile(device_type)
    command = profile.command(probe=False, identity=device_args) if profile else []
    if not command:
        result = {
            "ok": False, "return_code": 1, "output": "",
            "error": f"Unsupported hardware discovery type: {device_type or '(empty)'}",
        }
    else:
        result = _run(command)
    result["read_only"] = True
    result["device_type"] = device_type
    output = str(result.get("output") or "")
    result["device_found"] = bool(
        profile and result.get("ok") and output_indicates_device(profile, output)
    )
    if profile:
        result["device_type"] = profile.key
        result["device_label"] = profile.label
        result["driver_family"] = profile.driver_family
        result["device_identity"] = parse_device_identity(profile, output)
    result["observed_at"] = time.time()
    # Cross-family fallback: when the expected radio is missing, scan the
    # other driver families read-only so a physically present PlutoSDR is
    # still reported even if the caller probed for a UHD device (or vice
    # versa), instead of blindly re-running the same failing command.
    if not result["device_found"]:
        alternates = _scan_other_families(
            profile.driver_family if profile else ""
        )
        if alternates:
            result["devices"] = alternates
            expected = (
                profile.label if profile else (device_type or "an SDR")
            )
            found_desc = ", ".join(
                "{}{}".format(
                    item.get("device_label") or item.get("device_type"),
                    " ({})".format(item["device_identity"])
                    if item.get("device_identity") else "",
                )
                for item in alternates
            )
            result["mismatch_hint"] = (
                f"Expected {expected} but discovered: {found_desc}. "
                "Confirm the device selection or the physical connection."
            )
    result["health"] = _probe_health(
        output, identity_ok=bool(result.get("device_found"))
    )
    result["report_path"] = _persist_hardware_report(
        ctx, "device_discovery.json", result
    )
    result["output"] = _compact_vendor_output(output)
    result["raw_output_artifact"] = result["report_path"]
    result["output_truncated"] = len(output) > len(result["output"])
    return result


@tool(
    name="probe_device",
    description=(
        "Vendor CLI wrapper: read-only probe for an explicitly selected SDR "
        "(uhd_usrp_probe / iio_info). Not a GNU Radio block."
    ),
    parameters={"type": "object", "properties": {
        "device_args": {"type": "string"},
        "device_type": {"type": "string"},
    }},
    group="hardware",
    origin="vendor_cli",
    runtime="uhd_iio",
    effect_level="DEVICE_READ",
)
def probe_device(
    ctx: ToolContext, device_args: str = "", device_type: str = "b210"
) -> Dict[str, Any]:
    profile = resolve_hardware_profile(device_type)
    if profile and profile.driver_family == "iio" and not device_args:
        return {
            "ok": False,
            "read_only": True,
            "device_probed": False,
            "error": "IIO probing requires the exact device_identity returned by discovery",
        }
    command = profile.command(probe=True, identity=device_args) if profile else []
    if not command:
        return {"ok": False, "read_only": True, "device_probed": False,
                "error": f"Unsupported hardware probe type: {device_type or '(empty)'}"}
    result = _run(command, timeout=20.0)
    result["read_only"] = True
    result["device_type"] = device_type
    output = str(result.get("output") or "")
    result["device_probed"] = bool(
        profile
        and result.get("ok")
        and output_indicates_successful_probe(profile, output)
    )
    if profile:
        result["device_type"] = profile.key
        result["device_label"] = profile.label
        result["driver_family"] = profile.driver_family
        result["device_identity"] = device_args or parse_device_identity(profile, output)
    result["observed_at"] = time.time()
    result["health"] = _probe_health(
        output, identity_ok=bool(result.get("device_probed"))
    )
    if result["health"]["fatal_errors"]:
        result["device_probed"] = False
        result["ok"] = False
    result["report_path"] = _persist_hardware_report(
        ctx, "device_probe.json", result
    )
    result["output"] = _compact_vendor_output(output)
    result["raw_output_artifact"] = result["report_path"]
    result["output_truncated"] = len(output) > len(result["output"])
    return result


@tool(
    name="arm_hardware_flowgraph",
    description=(
        "Enable a disabled hardware sink only after offline verification, "
        "device probing, and the RF checkpoint have passed. Never starts RF."
    ),
    parameters={
        "type": "object",
        "properties": {
            "grc_path": {"type": "string"},
            "device_identity": {"type": "string"},
        },
        "required": ["grc_path"],
    },
    group="hardware",
    origin="deepradio_runtime",
    runtime="grc_rewrite",
    effect_level="DEVICE_CONFIG",
    requires=["rf_runtime", "device_probed", "user_effect_grant"],
)
def arm_hardware_flowgraph(
    ctx: ToolContext, grc_path: str, device_identity: str = ""
) -> Dict[str, Any]:
    if os.environ.get("GRC_AGENT_ENABLE_RF") != "1":
        return {
            "ok": False,
            "armed": False,
            "requires_system_enable": True,
            "error": "RF runtime is disabled; refusing to generate an armed flowgraph",
        }
    if _is_ble_deploy(ctx) and not _completion_satisfied(ctx, "ble_packet_valid"):
        return {"ok": False, "armed": False, "error": "Offline protocol verification has not passed"}
    if not _completion_satisfied(ctx, "device_probed"):
        return {"ok": False, "armed": False, "error": "Hardware discovery and probing have not passed"}
    if not _rf_approved(ctx):
        return {"ok": False, "armed": False, "error": "RF_RUN user authorization is missing"}
    source = _resolve_work_path(ctx, grc_path)
    out_dir = Path(ctx.out_dir or "").resolve()
    if not source.is_file() or out_dir not in source.parents:
        return {"ok": False, "armed": False, "error": "Only the current session's flowgraph may be armed"}
    if ctx.flow_graph is None:
        return {"ok": False, "armed": False, "error": "The current session has no loaded flowgraph"}
    sink_keys = {
        "uhd_usrp_sink", "iio_pluto_sink", "iio_fmcomms2_sink_fc32",
        "osmosdr_sink", "limesdr_sink",
    }
    sinks = [
        block for block in ctx.flow_graph.blocks
        if str(getattr(block, "key", "")) in sink_keys
    ]
    if not sinks:
        return {"ok": False, "armed": False, "error": "The flowgraph has no supported hardware TX sink"}
    prior_states = [block.state for block in sinks]
    preview_blocks = [
        block for block in ctx.flow_graph.blocks
        if str(getattr(block, "name", "")) in {"preview_throttle", "preview_sink"}
    ]
    preview_prior_states = [block.state for block in preview_blocks]
    bind_endpoint_identity(ctx.flow_graph, device_identity)
    for block in sinks:
        block.state = "enabled"
    for block in preview_blocks:
        block.state = "disabled"
    ctx.flow_graph.rewrite()
    ctx.flow_graph.validate()
    if not ctx.flow_graph.is_valid():
        for block, state in zip(sinks, prior_states):
            block.state = state
        for block, state in zip(preview_blocks, preview_prior_states):
            block.state = state
        ctx.flow_graph.rewrite()
        return {"ok": False, "armed": False, "error": "Flowgraph validation failed after enabling the TX sink"}
    armed_path = source.with_name(f"{source.stem}.armed.grc")
    try:
        ctx.platform.save_flow_graph(str(armed_path), ctx.flow_graph)
    except Exception as exc:  # noqa: BLE001
        for block, state in zip(sinks, prior_states):
            block.state = state
        for block, state in zip(preview_blocks, preview_prior_states):
            block.state = state
        ctx.flow_graph.rewrite()
        return {"ok": False, "armed": False, "error": f"Failed to save the armed flowgraph: {exc}"}
    compiled = _compile_grc(str(armed_path))
    if not compiled.get("compiled"):
        for block, state in zip(sinks, prior_states):
            block.state = state
        for block, state in zip(preview_blocks, preview_prior_states):
            block.state = state
        ctx.flow_graph.rewrite()
        try:
            armed_path.unlink()
        except OSError:
            pass
        return {
            "ok": False,
            "armed": False,
            "error": "The armed flowgraph did not compile with grcc",
            "compile": compiled,
        }
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    if project is not None:
        project.grc_path = str(armed_path)
        project.config["rf_armed"] = True
        project.config["rf_armed_path"] = str(armed_path)
    ctx.extra.setdefault("artifacts", {})["grc_path"] = str(armed_path)
    result = {
        "ok": True,
        "armed": True,
        "grc_path": str(armed_path),
        "device_identity": device_identity,
        "not_started": True,
        "compile": compiled,
    }
    result["report_path"] = _persist_hardware_report(
        ctx, "flowgraph_arm.json", result
    )
    return result


@tool(
    name="start_flowgraph",
    description=(
        "DeepRadio runtime: start an approved hardware flowgraph via grcc for "
        "at most duration_seconds (max 60). Stops early on OTA confirm or cancel."
    ),
    parameters={
        "type": "object",
        "properties": {
            "grc_path": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
        "required": ["grc_path"],
    },
    group="hardware",
    origin="deepradio_runtime",
    runtime="grcc",
    effect_level="RF_RUN",
    idempotent=False,
    requires=["rf_runtime", "flowgraph_armed", "user_effect_grant"],
)
def start_flowgraph(
    ctx: ToolContext, grc_path: str, duration_seconds: float = 30.0
) -> Dict[str, Any]:
    workflow = ctx.extra.get("workflow") or {}
    stage_id = str(ctx.extra.get("stage_id") or "")
    source_key = _resolve_work_path(ctx, grc_path)
    action_key = "{}:{}:{}:{}".format(
        workflow.get("workflow_id") or "",
        stage_id,
        workflow.get("revision") or 0,
        source_key,
    )
    action_results = ctx.extra.setdefault("hardware_action_results", {})
    if ctx.extra.get("force_hardware_start"):
        RUNTIME.stop(_session_id(ctx))
        action_results.pop(action_key, None)
    if action_key in action_results:
        replay = dict(action_results[action_key])
        replay["idempotent_replay"] = True
        return replay
    if os.environ.get("GRC_AGENT_ENABLE_RF") != "1":
        return {
            "ok": False,
            "enabled": False,
            "requires_confirmation": True,
            "error": "Physical RF is disabled by default; explicitly set GRC_AGENT_ENABLE_RF=1 only after completing safety checks",
        }
    if not _rf_approved(ctx):
        return {"ok": False, "requires_confirmation": True, "error": "RF_RUN user authorization is missing"}
    if _is_ble_deploy(ctx) and not _completion_satisfied(ctx, "ble_packet_valid"):
        return {"ok": False, "error": "Offline protocol verification has not passed; refusing to start RF"}
    if not _completion_satisfied(ctx, "device_probed"):
        return {"ok": False, "error": "Hardware discovery and probing have not passed; refusing to start RF"}
    if _tx_requires_arming(ctx) and not _rf_armed(ctx, grc_path):
        return {"ok": False, "error": "The flowgraph has not been armed by the controlled workflow; refusing to start RF"}
    source = _resolve_work_path(ctx, grc_path)
    out_dir = Path(ctx.out_dir or "").resolve()
    if not source.is_file() or out_dir not in source.parents:
        return {"ok": False, "error": "Only .grc files in the current session output directory may be executed"}
    if source.suffix != ".grc":
        return {"ok": False, "error": "The hardware runtime target must be a .grc file"}
    build_dir = out_dir / "hardware_runtime"
    build_dir.mkdir(parents=True, exist_ok=True)
    compiled = _run(["grcc", "-o", str(build_dir), str(source)], timeout=30.0)
    if not compiled.get("ok"):
        return {"ok": False, "error": "grcc generation failed", "detail": compiled}
    candidates = sorted(build_dir.glob("*.py"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return {"ok": False, "error": "grcc did not generate a Python program"}
    candidates[0].chmod(candidates[0].stat().st_mode | 0o100)
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    device_type = str(
        ((getattr(project, "config", {}) or {}).get("observed_device") or {}).get("type")
        or ((getattr(project, "config", {}) or {}).get("desired_device") or {}).get("type")
        or ""
    )
    python_runtime = _resolve_gnuradio_python(device_type)
    if not python_runtime.get("ok"):
        result = dict(python_runtime)
        result.update({"running": False, "ready": False})
        _project_runtime_state(ctx, result)
        action_results[action_key] = dict(result)
        return result
    ctx.extra.setdefault("artifacts", {})["runtime_program"] = str(candidates[0])
    result = RUNTIME.start(
        _session_id(ctx),
        str(candidates[0]),
        duration_seconds,
        interpreter=str(python_runtime["interpreter"]),
        startup_grace=0.75,
    )
    result["runtime_imports"] = list(python_runtime.get("required") or [])
    _project_runtime_state(ctx, result)
    action_results[action_key] = dict(result)
    return result


@tool(
    name="query_runtime_status",
    description="Read current bounded hardware runtime status.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
    origin="deepradio_runtime",
    runtime="hardware_runtime",
    effect_level="DEVICE_READ",
)
def query_runtime_status(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(ctx, RUNTIME.status(_session_id(ctx)))


@tool(
    name="stop_flowgraph",
    description="Stop the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
    origin="deepradio_runtime",
    runtime="hardware_runtime",
    effect_level="RF_RUN",
)
def stop_flowgraph(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(ctx, RUNTIME.stop(_session_id(ctx)))


@tool(
    name="emergency_stop",
    description="Immediately kill the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
    origin="deepradio_runtime",
    runtime="hardware_runtime",
    effect_level="RF_RUN",
)
def emergency_stop(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(
        ctx, RUNTIME.stop(_session_id(ctx), emergency=True)
    )


def _persist_runtime_result(ctx: ToolContext, result: Dict[str, Any]) -> Dict[str, Any]:
    output = str(result.get("output") or "")
    if not (
        result.get("run_id") or result.get("running") or output
    ):
        _project_runtime_state(ctx, result)
        return dict(result)
    enriched = dict(result)
    if output:
        directory = Path(ctx.out_dir or os.getcwd()) / "hardware_runtime"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "runtime.log"
        try:
            path.write_text(output, encoding="utf-8")
        except OSError:
            path = None
        if path is not None:
            enriched["log_path"] = str(path)
            ctx.extra.setdefault("artifacts", {})["runtime_log"] = str(path)
    status_dir = Path(ctx.out_dir or os.getcwd()) / "hardware_runtime"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / "runtime_status.json"
    try:
        status_path.write_text(
            json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        enriched["status_path"] = str(status_path)
        ctx.extra.setdefault("artifacts", {})["runtime_status"] = str(status_path)
    except OSError:
        pass
    _project_runtime_state(ctx, enriched)
    return enriched


_SINK_KEYS = {
    "uhd_usrp_sink", "iio_pluto_sink", "iio_fmcomms2_sink_fc32",
    "osmosdr_sink", "limesdr_sink",
}
_ENDPOINT_KEYS = _SINK_KEYS | {
    "uhd_usrp_source", "iio_pluto_source", "osmosdr_source",
}


def bind_endpoint_identity(flow_graph: Any, identity: str) -> int:
    """Write a probed identity into hardware endpoints without enabling them."""
    if flow_graph is None or not identity:
        return 0
    changed = 0
    address = identity if "=" in identity else f"serial={identity}"
    for block in list(getattr(flow_graph, "blocks", None) or []):
        key = str(getattr(block, "key", "") or "")
        if key not in _ENDPOINT_KEYS:
            continue
        params = getattr(block, "params", None) or {}
        if "uri" in params:
            params["uri"].set_value(repr(identity))
            changed += 1
        elif "dev_addr" in params:
            params["dev_addr"].set_value(repr(address))
            changed += 1
    return changed


def _disable_block(ctx: ToolContext, block_id: str) -> None:
    block = ctx.blocks.get(block_id)
    if block is not None:
        block.state = "disabled"


def _sdr_tx_sink_candidates(
    hardware: str, center_freq: float, sample_rate: float
) -> list[tuple[str, str, Dict[str, str], int]]:
    """Return (key, id, params, dst_port) candidates for an unarmed TX sink."""
    profile = resolve_hardware_profile(hardware)
    key = profile.key if profile else (hardware or "").strip().lower()
    freq = str(float(center_freq))
    rate = str(float(sample_rate))
    if key in {"b210", "usrp"} or (profile and profile.driver_family == "uhd"):
        return [(
            "uhd_usrp_sink",
            "sdr_sink",
            {
                "type": "fc32",
                "dev_addr": repr(device_args_for(key or "usrp")),
                "samp_rate": rate,
                "center_freq0": freq,
                "gain0": "0",
                "ant0": repr("TX/RX"),
            },
            1,
        )]
    if key == "pluto" or (profile and profile.driver_family == "iio"):
        pluto = {
            "type": "fc32",
            "uri": repr(""),
            "frequency": str(int(float(center_freq))),
            "samplerate": str(int(float(sample_rate))),
            "bandwidth": str(int(float(sample_rate))),
            "buffer_size": "32768",
            "cyclic": "False",
            "attenuation1": "30.0",
            "filter_source": "'Auto'",
        }
        return [
            ("iio_pluto_sink", "sdr_sink", dict(pluto), 0),
            ("iio_fmcomms2_sink_fc32", "sdr_sink", dict(pluto), 0),
        ]
    if key == "hackrf":
        return [(
            "osmosdr_sink",
            "sdr_sink",
            {
                "args": repr("hackrf=0"),
                "samp_rate": rate,
                "freq": freq,
                "gain": "0",
            },
            0,
        )]
    if key == "limesdr":
        return [(
            "limesdr_sink",
            "sdr_sink",
            {"freq": freq, "samp_rate": rate, "gain": "0"},
            0,
        )]
    return []


@tool(
    name="build_sdr_tx_flowgraph",
    description=(
        "DeepRadio compose tool: analog source → SDR TX sink, left unarmed. "
        "Saves .grc without starting RF. Device family selects the sink block."
    ),
    parameters={
        "type": "object",
        "properties": {
            "device_type": {"type": "string"},
            "center_freq": {"type": "number"},
            "sample_rate": {"type": "number"},
        },
        "required": ["device_type", "center_freq", "sample_rate"],
    },
    group="hardware",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    effect_level="ARTIFACT_WRITE",
)
def build_sdr_tx_flowgraph(
    ctx: ToolContext,
    device_type: str,
    center_freq: float,
    sample_rate: float,
) -> Dict[str, Any]:
    freq = float(center_freq)
    rate = float(sample_rate)
    if freq <= 0 or rate <= 0:
        return {"ok": False, "error": "Center frequency and sample rate must be positive"}
    candidates = _sdr_tx_sink_candidates(device_type, freq, rate)
    if not candidates:
        return {
            "ok": False,
            "error": f"No deterministic structure covers the {device_type or 'SDR'} transmit endpoint",
        }
    steps: list[Dict[str, Any]] = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({
            "tool": name, "ok": bool(result.get("ok")), "error": result.get("error"),
        })
        return result

    hardware = normalize_hardware(device_type)
    invoke(
        "init_flow_graph",
        {"flowgraph_id": f"{hardware or 'sdr'}_tx", "generate_options": "no_gui"},
    )
    invoke(
        "add_block",
        {
            "key": "variable",
            "id": "samp_rate",
            "params": {"value": _grc_number(rate)},
        },
    )
    invoke(
        "add_block",
        {
            "key": "analog_sig_source_x",
            "id": "src",
            "params": {
                "type": "complex",
                "samp_rate": "samp_rate",
                "waveform": "analog.GR_COS_WAVE",
                "freq": "1000",
                "amp": "0.3",
                "comment": "No modulation specified: using a 1 kHz test tone for preview",
            },
        },
    )
    throttle_key = (
        "blocks_throttle2"
        if "blocks_throttle2" in getattr(ctx.platform, "blocks", {})
        else "blocks_throttle"
    )
    invoke(
        "add_block",
        {
            "key": throttle_key,
            "id": "preview_throttle",
            "params": {
                "type": "complex",
                "samples_per_second": "samp_rate",
                "comment": "Rate limiting only, to prevent an unbounded preview loop; this is not an error",
            },
        },
    )
    invoke(
        "add_block",
        {
            "key": "blocks_null_sink",
            "id": "preview_sink",
            "params": {
                "type": "complex",
                "comment": "Safe preview: samples are discarded and no antenna is connected. The grey hardware endpoint is unarmed and will not transmit.",
            },
        },
    )
    added = {"ok": False}
    sink_key = ""
    dst_port = 0
    for sink_key, sink_id, params, dst_port in candidates:
        params = dict(params)
        params["comment"] = "Unauthorized RF; remains disabled. Grey means the hardware endpoint is unarmed and will not transmit."
        added = invoke("add_block", {"key": sink_key, "id": sink_id, "params": params})
        if added.get("ok"):
            break
    if not added.get("ok"):
        return {
            "ok": False,
            "valid": False,
            "error": added.get("error") or f"No {device_type} transmit sink is available in the current environment",
            "steps": steps,
            "not_started": True,
            "armed": False,
        }
    connect_args: Dict[str, Any] = {"src_id": "src", "dst_id": "sdr_sink"}
    if dst_port:
        connect_args["dst_port"] = dst_port
    connected = invoke("connect", connect_args)
    if not connected.get("ok"):
        return {
            "ok": False,
            "valid": False,
            "error": connected.get("error") or "Unable to connect the baseband source to the SDR sink",
            "steps": steps,
            "hardware": hardware,
            "sink_key": sink_key,
            "not_started": True,
            "armed": False,
        }
    preview_src = invoke(
        "connect", {"src_id": "src", "dst_id": "preview_throttle"}
    )
    preview_sink = invoke(
        "connect", {"src_id": "preview_throttle", "dst_id": "preview_sink"}
    )
    intended_validation = invoke("validate_flowgraph", {})
    if intended_validation.get("valid"):
        _disable_block(ctx, "sdr_sink")
    # The artifact itself must be valid.  With the hardware endpoint disabled,
    # the throttle/null branch is the runnable, RF-safe preview topology.
    preview_validation = (
        invoke("validate_flowgraph", {})
        if intended_validation.get("valid")
        and preview_src.get("ok")
        and preview_sink.get("ok")
        else {"ok": False, "valid": False, "errors": ["Failed to create the safe-preview branch"]}
    )
    rendered = (
        invoke("render_grc", {})
        if preview_validation.get("valid")
        else {"ok": False}
    )
    compiled = (
        _compile_grc(str(rendered.get("path") or ""))
        if rendered.get("ok") and rendered.get("path")
        else {"ok": False, "compiled": False, "error": "The flowgraph has not been saved"}
    )
    final_valid = bool(
        preview_validation.get("valid") and compiled.get("compiled")
    )
    if rendered.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = rendered["path"]
        state = ctx.extra.get("state")
        if state is not None:
            state.project.grc_path = rendered["path"]
            state.project.config.update({
                "hardware": hardware,
                "direction": "tx",
                "carrier_frequency": freq,
                "sample_rate": rate,
                "rf_bandwidth": rate,
                "tx_signal": {
                    "kind": "diagnostic_tone",
                    "frequency_hz": 1000.0,
                    "amplitude": 0.3,
                    "source": "safe_preview_default",
                },
                "tx_attenuation": 30.0 if hardware == "pluto" else None,
                "tx_gain": 0.0 if hardware != "pluto" else None,
                "rf_armed": False,
                "rf_started": False,
                "preview_mode": "throttled_null_sink",
            })
    result = {
        "ok": bool(final_valid),
        "valid": bool(final_valid),
        "grc_path": rendered.get("path"),
        "steps": steps,
        "errors": preview_validation.get("errors", [])
        or ([] if compiled.get("compiled") else [compiled.get("error") or "grcc compilation failed"]),
        "intended_topology_valid": bool(intended_validation.get("valid")),
        "preview_topology_valid": bool(preview_validation.get("valid")),
        "compiled": bool(compiled.get("compiled")),
        "compile": compiled,
        "center_freq": freq,
        "sample_rate": rate,
        "bandwidth": rate,
        "hardware": hardware,
        "sink_key": sink_key,
        "baseband": {
            "kind": "diagnostic_tone",
            "frequency_hz": 1000.0,
            "amplitude": 0.3,
            "source": "safe_preview_default",
        },
        "preview_mode": "throttled_null_sink",
        "armed": False,
        "not_started": True,
    }
    result["report_path"] = _persist_hardware_report(
        ctx, "flowgraph_validation.json", result
    )
    return result


@tool(
    name="build_usrp_rx_spectrum_flowgraph",
    description=(
        "DeepRadio compose tool: assemble GNU Radio blocks "
        "(uhd_usrp_source → QT frequency sink). Validates and saves .grc "
        "without starting RF."
    ),
    parameters={
        "type": "object",
        "properties": {
            "center_freq": {"type": "number"},
            "sample_rate": {"type": "number"},
            "gain": {"type": "number"},
            "device_args": {"type": "string"},
            "antenna": {"type": "string"},
        },
        "required": ["center_freq", "sample_rate"],
    },
    group="hardware",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    effect_level="ARTIFACT_WRITE",
)
def build_usrp_rx_spectrum_flowgraph(
    ctx: ToolContext,
    center_freq: float,
    sample_rate: float,
    gain: float = 20.0,
    device_args: str = "",
    antenna: str = "RX2",
) -> Dict[str, Any]:
    device_args = device_args_for("b210", device_args)
    freq = float(center_freq)
    rate = float(sample_rate)
    rx_gain = float(gain)
    if freq <= 0 or rate <= 0:
        return {"ok": False, "error": "Center frequency and sample rate must be positive"}
    if not 70e6 <= freq <= 6e9:
        return {"ok": False, "error": "Center frequency is outside the B210 capability range"}
    if not 0.0 <= rx_gain <= 76.0:
        return {"ok": False, "error": "RX gain must be between 0 and 76 dB"}
    steps = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        return result

    invoke(
        "init_flow_graph",
        {"flowgraph_id": "b210_rx_spectrum", "generate_options": "qt_gui"},
    )
    invoke(
        "add_block",
        {
            "key": "variable",
            "id": "samp_rate",
            "params": {"value": _grc_number(rate)},
        },
    )
    source = invoke(
        "add_block",
        {
            "key": "uhd_usrp_source",
            "id": "b210_src",
            "params": {
                "type": "fc32",
                "dev_addr": repr(device_args),
                "samp_rate": "samp_rate",
                "center_freq0": str(freq),
                "gain0": str(rx_gain),
                "ant0": repr(antenna),
            },
        },
    )
    sink = invoke(
        "add_block",
        {
            "key": "qtgui_freq_sink_x",
            "id": "rx_spectrum",
            "params": {
                "type": "complex",
                "name": repr("B210 RX Spectrum"),
                "fftsize": "1024",
                "fc": str(freq),
                "bw": "samp_rate",
                "wintype": "window.WIN_BLACKMAN_hARRIS",
            },
        },
    )
    connected = {"ok": False}
    if source.get("ok") and sink.get("ok"):
        for src_port, dst_port in ((0, 0), (1, 0), (0, 1)):
            connected = invoke(
                "connect",
                {
                    "src_id": "b210_src",
                    "dst_id": "rx_spectrum",
                    "src_port": src_port,
                    "dst_port": dst_port,
                },
            )
            if connected.get("ok"):
                break
    validation = invoke("validate_flowgraph", {})
    rendered = invoke("render_grc", {}) if validation.get("valid") else {"ok": False}
    if rendered.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = rendered["path"]
        state = ctx.extra.get("state")
        if state is not None:
            state.project.grc_path = rendered["path"]
            state.project.flowgraph_version = int(
                getattr(state.project, "flowgraph_version", 0) or 0
            ) + 1
            state.project.config.update(
                {
                    "recipe": "b210_rx_spectrum",
                    "hardware": "b210",
                    "direction": "rx",
                    "carrier_frequency": freq,
                    "sample_rate": rate,
                }
            )
    return {
        "ok": bool(validation.get("valid") and rendered.get("ok") and connected.get("ok")),
        "valid": bool(validation.get("valid")),
        "grc_path": rendered.get("path"),
        "steps": steps,
        "errors": validation.get("errors", []),
        "center_freq": freq,
        "sample_rate": rate,
        "gain": rx_gain,
        "not_started": True,
        "realtime_ui": "qtgui_freq_sink_x",
    }


@tool(
    name="build_sdr_rx_spectrum_flowgraph",
    description=(
        "Build a receive-only SDR to QT frequency-sink flowgraph for the "
        "selected HardwareProfile. Saves and validates it without starting."
    ),
    parameters={
        "type": "object",
        "properties": {
            "device_type": {"type": "string"},
            "center_freq": {"type": "number"},
            "sample_rate": {"type": "number"},
            "gain": {"type": "number"},
            "device_args": {"type": "string"},
        },
        "required": ["device_type", "center_freq", "sample_rate"],
    },
    group="hardware",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    effect_level="ARTIFACT_WRITE",
)
def build_sdr_rx_spectrum_flowgraph(
    ctx: ToolContext,
    device_type: str,
    center_freq: float,
    sample_rate: float,
    gain: float = 20.0,
    device_args: str = "",
) -> Dict[str, Any]:
    hardware = normalize_hardware(device_type)
    profile = resolve_hardware_profile(hardware)
    if profile is None:
        return {"ok": False, "error": f"Unknown SDR type: {device_type}"}
    freq = float(center_freq)
    rate = float(sample_rate)
    if freq <= 0 or rate <= 0:
        return {"ok": False, "error": "Center frequency and sample rate must be positive"}
    low, high = profile.frequency_range
    if not low <= freq <= high:
        return {
            "ok": False,
            "error": f"Center frequency is outside the declared capability range of {profile.label}",
        }
    if profile.driver_family == "uhd":
        result = build_usrp_rx_spectrum_flowgraph(
            ctx,
            center_freq=freq,
            sample_rate=rate,
            gain=gain,
            device_args=device_args_for(hardware, device_args),
        )
        result.update({
            "hardware": hardware,
            "signal_source_scope": "live_device",
        })
        state = ctx.extra.get("state")
        if state is not None and result.get("ok"):
            state.project.config["signal_source_scope"] = "live_device"
        return result
    if profile.driver_family != "iio":
        return {
            "ok": False,
            "error": f"No receive-spectrum builder is available for {profile.label}",
        }

    steps: list[Dict[str, Any]] = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({
            "tool": name,
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
        })
        return result

    invoke(
        "init_flow_graph",
        {"flowgraph_id": f"{hardware}_rx_spectrum", "generate_options": "qt_gui"},
    )
    invoke("add_block", {
        "key": "variable",
        "id": "samp_rate",
        "params": {"value": _grc_number(rate)},
    })
    source = invoke("add_block", {
        "key": "iio_pluto_source",
        "id": "sdr_source",
        "params": {
            "type": "fc32",
            "uri": repr(device_args),
            "frequency": str(int(freq)),
            "samplerate": "samp_rate",
            "bandwidth": str(int(rate)),
            "buffer_size": "32768",
            "gain1": repr("slow_attack"),
            "filter_source": repr("Auto"),
        },
    })
    sink = invoke("add_block", {
        "key": "qtgui_freq_sink_x",
        "id": "rx_spectrum",
        "params": {
            "type": "complex",
            "name": repr(f"{profile.label} RX Spectrum"),
            "fftsize": "1024",
            "fc": str(freq),
            "bw": "samp_rate",
            "wintype": "window.WIN_BLACKMAN_hARRIS",
        },
    })
    connected = invoke("connect", {
        "src_id": "sdr_source", "dst_id": "rx_spectrum",
    }) if source.get("ok") and sink.get("ok") else {"ok": False}
    validation = invoke("validate_flowgraph", {})
    rendered = invoke("render_grc", {}) if validation.get("valid") else {"ok": False}
    if rendered.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = rendered["path"]
        state = ctx.extra.get("state")
        if state is not None:
            state.project.grc_path = rendered["path"]
            state.project.flowgraph_version += 1
            state.project.config.update({
                "recipe": f"{hardware}_rx_spectrum",
                "hardware": hardware,
                "direction": "rx",
                "carrier_frequency": freq,
                "sample_rate": rate,
                "signal_source_scope": "live_device",
                "rf_active": False,
            })
    return {
        "ok": bool(validation.get("valid") and rendered.get("ok") and connected.get("ok")),
        "valid": bool(validation.get("valid")),
        "grc_path": rendered.get("path"),
        "steps": steps,
        "errors": validation.get("errors", []),
        "center_freq": freq,
        "sample_rate": rate,
        "gain_mode": "slow_attack",
        "hardware": hardware,
        "source_key": "iio_pluto_source",
        "not_started": True,
        "realtime_ui": "qtgui_freq_sink_x",
        "signal_source_scope": "live_device",
    }
