"""Driver-specific SDR discovery and explicitly gated RF runtime tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from ..service.hardware_runtime import RUNTIME
from .hardware_profiles import (
    normalize_hardware,
    output_indicates_device,
    output_indicates_successful_probe,
    parse_device_identity,
    resolve_hardware_profile,
)
from .registry import ToolContext, call, tool


def _run(command: list[str], timeout: float = 15.0) -> Dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {"ok": False, "error": f"命令不可用: {command[0]}"}
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
        return {"ok": False, "error": "硬件命令超时", "output": str(exc.stdout or "")}
    return {
        "ok": completed.returncode == 0,
        "return_code": completed.returncode,
        "output": completed.stdout[-12000:],
        "command": command,
    }


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
        "error": "没有找到可导入 GNU Radio 硬件模块的 Python 解释器",
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
    runtime["status"] = status
    runtime["running"] = bool(result.get("running"))
    project.config["runtime"] = runtime
    if not result.get("running") and status in {"crashed", "stopped", "exited", "failed"}:
        project.config["rf_armed"] = False
        project.config.pop("rf_armed_path", None)


def _rf_approved(ctx: ToolContext) -> bool:
    workflow = ctx.extra.get("workflow") or {}
    return any(
        stage.get("id") == "rf_plan_confirmation"
        and (stage.get("checkpoint") or {}).get("decision_status") == "approved"
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict)
    )


def _stage_passed(ctx: ToolContext, stage_id: str) -> bool:
    workflow = ctx.extra.get("workflow") or {}
    return any(
        stage.get("id") == stage_id
        and stage.get("execution_status") == "completed"
        and stage.get("outcome") == "passed"
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
        and Path(str(getattr(project, "grc_path", "") or "")).resolve()
        == Path(grc_path).resolve()
    )


def normalize_sdr_hardware(device_type: str) -> str:
    """Compatibility alias for the declarative HardwareProfile registry."""
    return normalize_hardware(device_type)


def _device_command(device_type: str, *, probe: bool) -> list[str]:
    profile = resolve_hardware_profile(device_type)
    return profile.command(probe=probe) if profile else []


@tool(
    name="discover_devices",
    description="Read-only SDR device discovery. Never opens an RF stream.",
    parameters={"type": "object", "properties": {
        "device_args": {"type": "string"},
        "device_type": {"type": "string"},
    }},
    group="hardware",
)
def discover_devices(
    ctx: ToolContext, device_args: str = "", device_type: str = "b210"
) -> Dict[str, Any]:
    profile = resolve_hardware_profile(device_type)
    command = profile.command(probe=False, identity=device_args) if profile else []
    if not command:
        return {"ok": False, "read_only": True, "device_found": False,
                "error": f"不支持的硬件发现类型: {device_type or '(empty)'}"}
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
    return result


@tool(
    name="probe_device",
    description="Read-only probe for an explicitly selected SDR device.",
    parameters={"type": "object", "properties": {
        "device_args": {"type": "string"},
        "device_type": {"type": "string"},
    }},
    group="hardware",
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
            "error": "IIO probe 需要 discover 返回的明确 device_identity",
        }
    command = profile.command(probe=True, identity=device_args) if profile else []
    if not command:
        return {"ok": False, "read_only": True, "device_probed": False,
                "error": f"不支持的硬件探测类型: {device_type or '(empty)'}"}
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
)
def arm_hardware_flowgraph(
    ctx: ToolContext, grc_path: str, device_identity: str = ""
) -> Dict[str, Any]:
    if os.environ.get("GRC_AGENT_ENABLE_RF") != "1":
        return {
            "ok": False,
            "armed": False,
            "requires_system_enable": True,
            "error": "RF 运行功能未启用，拒绝生成 armed 流图",
        }
    if not _stage_passed(ctx, "offline_protocol_verify"):
        return {"ok": False, "armed": False, "error": "离线协议校验尚未通过"}
    if not _stage_passed(ctx, "discover_and_probe_device"):
        return {"ok": False, "armed": False, "error": "硬件 discover/probe 尚未通过"}
    if not _rf_approved(ctx):
        return {"ok": False, "armed": False, "error": "缺少 rf_plan_confirmation"}
    source = Path(grc_path).resolve()
    out_dir = Path(ctx.out_dir or "").resolve()
    if not source.is_file() or out_dir not in source.parents:
        return {"ok": False, "armed": False, "error": "只允许武装当前 session 的流图"}
    if ctx.flow_graph is None:
        return {"ok": False, "armed": False, "error": "当前 session 没有已加载流图"}
    sink_keys = {
        "uhd_usrp_sink", "iio_pluto_sink", "iio_fmcomms2_sink_fc32",
        "osmosdr_sink", "limesdr_sink",
    }
    sinks = [
        block for block in ctx.flow_graph.blocks
        if str(getattr(block, "key", "")) in sink_keys
    ]
    if not sinks:
        return {"ok": False, "armed": False, "error": "流图中没有受支持的硬件 TX Sink"}
    prior_states = [block.state for block in sinks]
    for block in sinks:
        key = str(getattr(block, "key", ""))
        if device_identity and key in {"iio_pluto_sink", "iio_fmcomms2_sink_fc32"}:
            if "uri" in block.params:
                block.params["uri"].set_value(repr(device_identity))
        elif device_identity and key == "uhd_usrp_sink" and "dev_addr" in block.params:
            address = device_identity if "=" in device_identity else f"serial={device_identity}"
            block.params["dev_addr"].set_value(repr(address))
        block.state = "enabled"
    ctx.flow_graph.rewrite()
    ctx.flow_graph.validate()
    if not ctx.flow_graph.is_valid():
        for block, state in zip(sinks, prior_states):
            block.state = state
        return {"ok": False, "armed": False, "error": "启用 TX Sink 后流图校验失败"}
    armed_path = source.with_name(f"{source.stem}.armed.grc")
    try:
        ctx.platform.save_flow_graph(str(armed_path), ctx.flow_graph)
    except Exception as exc:  # noqa: BLE001
        for block, state in zip(sinks, prior_states):
            block.state = state
        return {"ok": False, "armed": False, "error": f"武装流图保存失败: {exc}"}
    state = ctx.extra.get("state")
    project = getattr(state, "project", None)
    if project is not None:
        project.grc_path = str(armed_path)
        project.config["rf_armed"] = True
        project.config["rf_armed_path"] = str(armed_path)
    ctx.extra.setdefault("artifacts", {})["grc_path"] = str(armed_path)
    return {
        "ok": True,
        "armed": True,
        "grc_path": str(armed_path),
        "device_identity": device_identity,
        "not_started": True,
    }


@tool(
    name="start_flowgraph",
    description="Start an approved, generated hardware flowgraph for at most 60 seconds. Disabled by default.",
    parameters={
        "type": "object",
        "properties": {
            "grc_path": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
        "required": ["grc_path"],
    },
    group="hardware",
)
def start_flowgraph(
    ctx: ToolContext, grc_path: str, duration_seconds: float = 30.0
) -> Dict[str, Any]:
    workflow = ctx.extra.get("workflow") or {}
    stage_id = str(ctx.extra.get("stage_id") or "")
    action_key = "{}:{}:{}:{}".format(
        workflow.get("workflow_id") or "",
        stage_id,
        workflow.get("revision") or 0,
        Path(grc_path).resolve(),
    )
    action_results = ctx.extra.setdefault("hardware_action_results", {})
    if action_key in action_results:
        replay = dict(action_results[action_key])
        replay["idempotent_replay"] = True
        return replay
    if os.environ.get("GRC_AGENT_ENABLE_RF") != "1":
        return {
            "ok": False,
            "enabled": False,
            "requires_confirmation": True,
            "error": "真实 RF 默认关闭；仅在完成安全检查后显式设置 GRC_AGENT_ENABLE_RF=1",
        }
    if not _rf_approved(ctx):
        return {"ok": False, "requires_confirmation": True, "error": "缺少 rf_plan_confirmation"}
    if _is_ble_deploy(ctx) and not _stage_passed(ctx, "offline_protocol_verify"):
        return {"ok": False, "error": "离线协议校验尚未通过，拒绝启动 RF"}
    if _is_ble_deploy(ctx) and not _stage_passed(ctx, "discover_and_probe_device"):
        return {"ok": False, "error": "硬件 discover/probe 尚未通过，拒绝启动 RF"}
    if _is_ble_deploy(ctx) and not _rf_armed(ctx, grc_path):
        return {"ok": False, "error": "流图尚未由受控流程武装，拒绝启动 RF"}
    source = Path(grc_path).resolve()
    out_dir = Path(ctx.out_dir or "").resolve()
    if not source.is_file() or out_dir not in source.parents:
        return {"ok": False, "error": "只允许执行当前 session 输出目录中的 .grc"}
    if source.suffix != ".grc":
        return {"ok": False, "error": "硬件运行目标必须是 .grc"}
    build_dir = out_dir / "hardware_runtime"
    build_dir.mkdir(parents=True, exist_ok=True)
    compiled = _run(["grcc", "-o", str(build_dir), str(source)], timeout=30.0)
    if not compiled.get("ok"):
        return {"ok": False, "error": "grcc 生成失败", "detail": compiled}
    candidates = sorted(build_dir.glob("*.py"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return {"ok": False, "error": "grcc 未生成 Python 程序"}
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
)
def query_runtime_status(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(ctx, RUNTIME.status(_session_id(ctx)))


@tool(
    name="stop_flowgraph",
    description="Stop the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def stop_flowgraph(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(ctx, RUNTIME.stop(_session_id(ctx)))


@tool(
    name="emergency_stop",
    description="Immediately kill the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def emergency_stop(ctx: ToolContext) -> Dict[str, Any]:
    return _persist_runtime_result(
        ctx, RUNTIME.stop(_session_id(ctx), emergency=True)
    )


def _persist_runtime_result(ctx: ToolContext, result: Dict[str, Any]) -> Dict[str, Any]:
    output = str(result.get("output") or "")
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


@tool(
    name="build_usrp_rx_spectrum_flowgraph",
    description=(
        "Build a USRP B210 receive flowgraph with QT GUI frequency sink. "
        "Validates and saves .grc without starting RF."
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
)
def build_usrp_rx_spectrum_flowgraph(
    ctx: ToolContext,
    center_freq: float,
    sample_rate: float,
    gain: float = 20.0,
    device_args: str = "type=b200",
    antenna: str = "RX2",
) -> Dict[str, Any]:
    freq = float(center_freq)
    rate = float(sample_rate)
    rx_gain = float(gain)
    if freq <= 0 or rate <= 0:
        return {"ok": False, "error": "中心频率和采样率必须为正数"}
    if not 70e6 <= freq <= 6e9:
        return {"ok": False, "error": "中心频率超出 B210 能力范围"}
    if not 0.0 <= rx_gain <= 76.0:
        return {"ok": False, "error": "RX gain 必须在 0~76 dB"}
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
            "params": {"value": str(rate)},
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
