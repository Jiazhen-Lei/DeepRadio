"""Deterministic BLE advertising packet, waveform, and GRC builders."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np

from .registry import ToolContext, call, tool


ADV_ACCESS_ADDRESS = bytes.fromhex("d6be898e")
ADV_CRC_INIT = 0x555555
ADV_FREQUENCIES = {37: 2_402_000_000.0, 38: 2_426_000_000.0, 39: 2_480_000_000.0}


def _bits_lsb(data: bytes) -> list[int]:
    return [(byte >> bit) & 1 for byte in data for bit in range(8)]


def _bytes_lsb(bits: Iterable[int]) -> bytes:
    values = list(bits)
    return bytes(
        sum((values[offset + bit] & 1) << bit for bit in range(8))
        for offset in range(0, len(values), 8)
    )


def _crc24(data: bytes, init: int = ADV_CRC_INIT) -> bytes:
    state = init & 0xFFFFFF
    for bit in _bits_lsb(data):
        feedback = (state & 1) ^ bit
        state >>= 1
        if feedback:
            state ^= 0x00065B
    return state.to_bytes(3, "little")


def _whiten(data: bytes, channel: int) -> bytes:
    state = (int(channel) & 0x3F) | 0x40
    out = []
    for bit in _bits_lsb(data):
        whiten = state & 1
        out.append(bit ^ whiten)
        state >>= 1
        if whiten:
            state ^= 0x44  # x^7 + x^4 + 1, LSB-first form
    return _bytes_lsb(out)


def _advertiser_address(local_name: str) -> bytes:
    address = bytearray(hashlib.sha256(local_name.encode("utf-8")).digest()[:6])
    address[-1] = (address[-1] & 0x3F) | 0xC0  # static random address
    return bytes(address)


def _packet(local_name: str, channel: int) -> Dict[str, Any]:
    encoded = local_name.encode("utf-8")
    if not encoded or len(encoded) > 26:
        raise ValueError("BLE Complete Local Name 必须为 1~26 个 UTF-8 字节")
    if channel not in ADV_FREQUENCIES:
        raise ValueError("BLE Advertising channel 必须是 37、38 或 39")
    adv_address = _advertiser_address(local_name)
    adv_data = bytes((2, 0x01, 0x06, len(encoded) + 1, 0x09)) + encoded
    payload = adv_address + adv_data
    header = bytes((0x42, len(payload)))  # ADV_NONCONN_IND + TxAdd=random
    pdu = header + payload
    crc = _crc24(pdu)
    whitened = _whiten(pdu + crc, channel)
    air = bytes((0xAA,)) + ADV_ACCESS_ADDRESS + whitened
    return {
        "pdu": pdu,
        "crc": crc,
        "air_packet": air,
        "advertiser_address": ":".join(f"{value:02X}" for value in adv_address[::-1]),
        "channel": channel,
        "center_freq": ADV_FREQUENCIES[channel],
        "local_name": local_name,
    }


def _out_dir(ctx: ToolContext) -> Path:
    path = Path(ctx.out_dir or os.getcwd()) / "ble"
    path.mkdir(parents=True, exist_ok=True)
    return path


@tool(
    name="build_ble_advertising_pdu",
    description="Build a deterministic BLE ADV_NONCONN_IND packet containing Complete Local Name.",
    parameters={
        "type": "object",
        "properties": {
            "local_name": {"type": "string"},
            "channel": {"type": "integer"},
        },
        "required": ["local_name"],
    },
    group="ble",
)
def build_ble_advertising_pdu(
    ctx: ToolContext, local_name: str, channel: int = 37
) -> Dict[str, Any]:
    packet = _packet(local_name, int(channel))
    out_dir = _out_dir(ctx)
    packet_path = out_dir / f"ble_adv_ch{channel}.bin"
    metadata_path = out_dir / f"ble_adv_ch{channel}.json"
    packet_path.write_bytes(packet["air_packet"])
    metadata = {
        key: value
        for key, value in packet.items()
        if key not in ("pdu", "crc", "air_packet")
    }
    metadata.update(
        {
            "pdu_hex": packet["pdu"].hex(),
            "crc_hex": packet["crc"].hex(),
            "air_packet_hex": packet["air_packet"].hex(),
        }
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    ctx.extra["ble_packet"] = packet
    ctx.extra.setdefault("artifacts", {}).update(
        {"ble_packet": str(packet_path), "ble_metadata": str(metadata_path)}
    )
    return {
        "ok": True,
        **metadata,
        "packet_path": str(packet_path),
        "metadata_path": str(metadata_path),
    }


def _gaussian_taps(samples_per_symbol: int, bt: float = 0.5, span: int = 4) -> np.ndarray:
    t = np.arange(-span * samples_per_symbol, span * samples_per_symbol + 1)
    alpha = math.sqrt(math.log(2.0)) / (bt * samples_per_symbol)
    taps = np.exp(-0.5 * (alpha * t) ** 2)
    return taps / np.sum(taps)


@tool(
    name="generate_ble_1m_waveform",
    description="Generate an offline complex64 BLE 1M GFSK advertising waveform; never opens hardware.",
    parameters={
        "type": "object",
        "properties": {
            "local_name": {"type": "string"},
            "channel": {"type": "integer"},
            "sample_rate": {"type": "number"},
            "interval_ms": {"type": "number"},
        },
        "required": ["local_name"],
    },
    group="ble",
)
def generate_ble_1m_waveform(
    ctx: ToolContext,
    local_name: str,
    channel: int = 37,
    sample_rate: float = 2_000_000.0,
    interval_ms: float = 100.0,
) -> Dict[str, Any]:
    sample_rate = float(sample_rate)
    samples_per_symbol = int(round(sample_rate / 1_000_000.0))
    if samples_per_symbol < 2 or abs(samples_per_symbol * 1_000_000.0 - sample_rate) > 1:
        return {"ok": False, "error": "BLE 1M sample_rate 必须是 1 MHz 的整数倍且至少 2 MHz"}
    if not 20.0 <= float(interval_ms) <= 10_240.0:
        return {"ok": False, "error": "advertising interval 必须位于 20~10240 ms"}
    packet = _packet(local_name, int(channel))
    symbols = 2.0 * np.asarray(_bits_lsb(packet["air_packet"]), dtype=np.float64) - 1.0
    nrz = np.repeat(symbols, samples_per_symbol)
    shaped = np.convolve(nrz, _gaussian_taps(samples_per_symbol), mode="same")
    phase = np.cumsum((math.pi * 0.5 / samples_per_symbol) * shaped)
    burst = np.exp(1j * phase).astype(np.complex64)
    interval_samples = int(round(sample_rate * float(interval_ms) / 1000.0))
    frame = np.zeros(max(interval_samples, len(burst)), dtype=np.complex64)
    frame[: len(burst)] = burst
    path = _out_dir(ctx) / f"ble_adv_ch{channel}_{int(sample_rate)}sps.cfile"
    frame.tofile(path)
    ctx.extra["ble_packet"] = packet
    ctx.extra["ble_waveform"] = {
        "path": str(path),
        "sample_rate": sample_rate,
        "samples_per_symbol": samples_per_symbol,
        "interval_ms": float(interval_ms),
        "sample_count": len(frame),
    }
    ctx.extra.setdefault("artifacts", {})["ble_waveform"] = str(path)
    return {"ok": True, **ctx.extra["ble_waveform"], "channel": channel}


@tool(
    name="verify_ble_packet_bits",
    description="Verify the current offline BLE advertising packet, CRC, whitening round-trip, and local name.",
    parameters={
        "type": "object",
        "properties": {"local_name": {"type": "string"}, "channel": {"type": "integer"}},
        "required": ["local_name"],
    },
    group="ble",
)
def verify_ble_packet_bits(
    ctx: ToolContext, local_name: str, channel: int = 37
) -> Dict[str, Any]:
    expected = _packet(local_name, int(channel))
    current = ctx.extra.get("ble_packet") or expected
    verified = (
        current.get("pdu") == expected["pdu"]
        and current.get("crc") == _crc24(current["pdu"])
        and _whiten(_whiten(current["pdu"] + current["crc"], int(channel)), int(channel))
        == current["pdu"] + current["crc"]
    )
    result = {
        "ok": bool(verified),
        "valid": bool(verified),
        "local_name": local_name,
        "channel": int(channel),
        "crc_hex": current.get("crc", b"").hex(),
    }
    ctx.extra["ble_verification"] = result
    return result


@tool(
    name="build_ble_uhd_tx_flowgraph",
    description="Build and validate a BLE waveform-to-UHD Sink flowgraph without starting it.",
    parameters={
        "type": "object",
        "properties": {
            "waveform_path": {"type": "string"},
            "channel": {"type": "integer"},
            "sample_rate": {"type": "number"},
            "gain": {"type": "number"},
            "device_args": {"type": "string"},
        },
        "required": ["waveform_path"],
    },
    group="ble",
)
def build_ble_uhd_tx_flowgraph(
    ctx: ToolContext,
    waveform_path: str,
    channel: int = 37,
    sample_rate: float = 2_000_000.0,
    gain: float = 0.0,
    device_args: str = "type=b200",
) -> Dict[str, Any]:
    path = Path(waveform_path).resolve()
    if not path.is_file():
        return {"ok": False, "error": "BLE waveform 文件不存在"}
    if int(channel) not in ADV_FREQUENCIES:
        return {"ok": False, "error": "仅支持 BLE Advertising channel 37/38/39"}
    if not 0.0 <= float(gain) <= 10.0:
        return {"ok": False, "error": "首期安全策略限制 TX gain 为 0~10 dB"}
    steps = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        return result

    invoke("init_flow_graph", {"flowgraph_id": "ble_b210_advertiser", "generate_options": "no_gui"})
    invoke(
        "add_block",
        {
            "key": "blocks_file_source",
            "id": "ble_waveform",
            "params": {"file": repr(str(path)), "type": "complex", "repeat": "True"},
        },
    )
    invoke(
        "add_block",
        {
            "key": "blocks_head",
            "id": "hard_stop_60s",
            "params": {
                "type": "complex",
                "num_items": str(int(float(sample_rate) * 60.0)),
            },
        },
    )
    invoke(
        "add_block",
        {
            "key": "uhd_usrp_sink",
            "id": "b210_sink",
            "params": {
                "type": "fc32",
                "dev_addr": repr(device_args),
                "samp_rate": str(float(sample_rate)),
                "center_freq0": str(ADV_FREQUENCIES[int(channel)]),
                "gain0": str(float(gain)),
                "ant0": repr("TX/RX"),
            },
        },
    )
    invoke("connect", {"src_id": "ble_waveform", "dst_id": "hard_stop_60s"})
    # UHD Sink exposes the message command port before stream channel 0.
    invoke("connect", {"src_id": "hard_stop_60s", "dst_id": "b210_sink", "dst_port": 1})
    validation = invoke("validate_flowgraph", {})
    rendered = invoke("render_grc", {}) if validation.get("valid") else {"ok": False}
    if rendered.get("path"):
        ctx.extra.setdefault("artifacts", {})["grc_path"] = rendered["path"]
    return {
        "ok": bool(validation.get("valid") and rendered.get("ok")),
        "valid": bool(validation.get("valid")),
        "grc_path": rendered.get("path"),
        "steps": steps,
        "errors": validation.get("errors", []),
        "center_freq": ADV_FREQUENCIES[int(channel)],
        "gain": float(gain),
        "not_started": True,
    }
