"""Read-only UHD discovery and explicitly gated RF runtime tools."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

from ..service.hardware_runtime import RUNTIME
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


def _rf_approved(ctx: ToolContext) -> bool:
    workflow = ctx.extra.get("workflow") or {}
    return any(
        stage.get("id") == "rf_plan_confirmation"
        and (stage.get("checkpoint") or {}).get("decision_status") == "approved"
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict)
    )


def _device_command(device_type: str, *, probe: bool) -> list[str]:
    kind = (device_type or "").lower()
    if kind in ("b210", "usrp", "b200"):
        return [
            "uhd_usrp_probe" if probe else "uhd_find_devices",
            "--args", "type=b200",
        ]
    if kind == "hackrf":
        return ["hackrf_info"]
    if kind == "pluto":
        return ["iio_info", "-s"]
    if kind == "limesdr":
        return ["LimeUtil", "--info" if probe else "--find"]
    return []


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
    command = _device_command(device_type, probe=False)
    if device_args and (device_type or "").lower() in ("b210", "usrp", "b200"):
        command = ["uhd_find_devices", "--args", device_args]
    if not command:
        return {"ok": False, "read_only": True, "device_found": False,
                "error": f"不支持的硬件发现类型: {device_type or '(empty)'}"}
    result = _run(command)
    result["read_only"] = True
    result["device_type"] = device_type
    result["device_found"] = bool(
        result.get("ok") and str(result.get("output") or "").strip()
    )
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
    command = _device_command(device_type, probe=True)
    if device_args and (device_type or "").lower() in ("b210", "usrp", "b200"):
        command = ["uhd_usrp_probe", "--args", device_args]
    if not command:
        return {"ok": False, "read_only": True, "device_probed": False,
                "error": f"不支持的硬件探测类型: {device_type or '(empty)'}"}
    result = _run(command, timeout=20.0)
    result["read_only"] = True
    result["device_type"] = device_type
    result["device_probed"] = bool(result.get("ok"))
    return result


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
    if os.environ.get("GRC_AGENT_ENABLE_RF") != "1":
        return {
            "ok": False,
            "enabled": False,
            "requires_confirmation": True,
            "error": "真实 RF 默认关闭；仅在完成安全检查后显式设置 GRC_AGENT_ENABLE_RF=1",
        }
    if not _rf_approved(ctx):
        return {"ok": False, "requires_confirmation": True, "error": "缺少 rf_plan_confirmation"}
    source = Path(grc_path).resolve()
    out_dir = Path(ctx.out_dir or "").resolve()
    if not source.is_file() or out_dir not in source.parents:
        return {"ok": False, "error": "只允许执行当前 session 输出目录中的 .grc"}
    if source.suffix != ".grc":
        return {"ok": False, "error": "硬件运行目标必须是 .grc"}
    build_dir = out_dir / "hardware_runtime"
    build_dir.mkdir(parents=True, exist_ok=True)
    compiled = _run(["grcc", "-d", str(build_dir), str(source)], timeout=30.0)
    if not compiled.get("ok"):
        return {"ok": False, "error": "grcc 生成失败", "detail": compiled}
    candidates = sorted(build_dir.glob("*.py"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return {"ok": False, "error": "grcc 未生成 Python 程序"}
    candidates[0].chmod(candidates[0].stat().st_mode | 0o100)
    return RUNTIME.start(_session_id(ctx), str(candidates[0]), duration_seconds)


@tool(
    name="query_runtime_status",
    description="Read current bounded hardware runtime status.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def query_runtime_status(ctx: ToolContext) -> Dict[str, Any]:
    return RUNTIME.status(_session_id(ctx))


@tool(
    name="stop_flowgraph",
    description="Stop the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def stop_flowgraph(ctx: ToolContext) -> Dict[str, Any]:
    return RUNTIME.stop(_session_id(ctx))


@tool(
    name="emergency_stop",
    description="Immediately kill the current hardware flowgraph. Always allowed.",
    parameters={"type": "object", "properties": {}},
    group="hardware",
)
def emergency_stop(ctx: ToolContext) -> Dict[str, Any]:
    return RUNTIME.stop(_session_id(ctx), emergency=True)


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
