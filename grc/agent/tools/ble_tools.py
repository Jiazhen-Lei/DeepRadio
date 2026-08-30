"""Deterministic BLE advertising packet, waveform, and GRC builders.

These are DeepRadio protocol tools. GNU Radio has no BLE Complete Local Name
generator; this module implements PDU/CRC/whitening/GFSK IQ, then *composes*
stock GNU Radio blocks (file_source, uhd_usrp_sink, iio_pluto_sink) into a
flowgraph. Agents must call these registry tools instead of inlining the
algorithms or asking an LLM to invent Access Address/CRC.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np

from .hardware_profiles import device_args_for
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
    """Return the BLE Link Layer CRC in over-the-air byte representation.

    BLE consumes each input octet least-significant bit first, while the
    24-bit LFSR is described with its x^24 term at the most-significant end.
    Keeping those two bit orders explicit avoids the common (and previously
    present) mistake of combining a right-shifting register with 0x00065B.
    """
    state = init & 0xFFFFFF
    for bit in _bits_lsb(data):
        feedback = ((state >> 23) & 1) ^ bit
        state = (state << 1) & 0xFFFFFF
        if feedback:
            state ^= 0x00065B
    return bytes(_reverse_octet(value) for value in state.to_bytes(3, "big"))


def _reverse_octet(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def _reference_crc24(data: bytes, init: int = ADV_CRC_INIT) -> bytes:
    """Independent bit-vector reference used by the packet validator.

    This intentionally does not call :func:`_crc24` and does not share its
    integer shift implementation.  It is slower, but validation is offline.
    """
    register = [(init >> bit) & 1 for bit in range(23, -1, -1)]
    polynomial = [(0x00065B >> bit) & 1 for bit in range(23, -1, -1)]
    for input_bit in _bits_lsb(data):
        feedback = register[0] ^ input_bit
        register = register[1:] + [0]
        if feedback:
            register = [left ^ right for left, right in zip(register, polynomial)]
    state = sum(bit << (23 - index) for index, bit in enumerate(register))
    return bytes(_reverse_octet(value) for value in state.to_bytes(3, "big"))


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


def _reference_dewhiten(data: bytes, channel: int) -> bytes:
    """Independent seven-bit LFSR representation for offline validation."""
    initial = (int(channel) & 0x3F) | 0x40
    register = [(initial >> bit) & 1 for bit in range(7)]
    output = []
    for input_bit in _bits_lsb(data):
        whitening_bit = register[0]
        output.append(input_bit ^ whitening_bit)
        old = register
        register = [
            old[1],
            old[2],
            old[3] ^ old[0],
            old[4],
            old[5],
            old[6],
            old[0],
        ]
    return _bytes_lsb(output)


def _advertiser_address(local_name: str) -> bytes:
    # A local name is optional in a valid advertising payload.  It is only a
    # deterministic seed here so repeated offline builds remain reproducible.
    seed = local_name or "deepradio-unnamed-advertiser"
    address = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:6])
    address[-1] = (address[-1] & 0x3F) | 0xC0  # static random address
    return bytes(address)


def _packet(local_name: str, channel: int) -> Dict[str, Any]:
    encoded = local_name.encode("utf-8")
    if len(encoded) > 26:
        raise ValueError("BLE Complete Local Name cannot exceed 26 UTF-8 bytes")
    if channel not in ADV_FREQUENCIES:
        raise ValueError("BLE advertising channel must be 37, 38, or 39")
    adv_address = _advertiser_address(local_name)
    adv_data = bytes((2, 0x01, 0x06))
    if encoded:
        adv_data += bytes((len(encoded) + 1, 0x09)) + encoded
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


def _set_runtime_mode(ctx: ToolContext) -> None:
    """Make generated no-GUI programs suitable for a managed subprocess."""
    options = getattr(ctx.flow_graph, "options_block", None)
    if options is not None and "run_options" in options.params:
        options.params["run_options"].set_value("run")
        options.params["run_options"].rewrite()


def _leave_hardware_sink_unarmed(ctx: ToolContext, block_id: str) -> None:
    block = ctx.blocks.get(block_id)
    if block is not None:
        block.state = "disabled"


@tool(
    name="build_ble_advertising_pdu",
    description=(
        "DeepRadio protocol tool: build a BLE ADV_NONCONN_IND PDU. Complete "
        "Local Name is optional. Not a GNU Radio block; call this tool instead "
        "of synthesizing bits."
    ),
    parameters={
        "type": "object",
        "properties": {
            "local_name": {"type": "string"},
            "channel": {"type": "integer"},
        },
        "required": [],
    },
    group="ble",
    origin="deepradio_protocol",
    runtime="deepradio",
    effect_level="ARTIFACT_WRITE",
)
def build_ble_advertising_pdu(
    ctx: ToolContext, local_name: str = "", channel: int = 37
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
        "capability": "ble_advertising_single_channel",
        "unsupported_capabilities": [
            "ble_advertising_three_channel",
            "ble_independent_sniffer",
        ],
    }


def _gaussian_taps(samples_per_symbol: int, bt: float = 0.5, span: int = 4) -> np.ndarray:
    if samples_per_symbol < 2 or bt <= 0:
        raise ValueError("samples_per_symbol and BT must be positive")
    # Gaussian impulse response, with time expressed in symbol periods.
    # A larger BT has a wider frequency response and therefore a narrower
    # time-domain impulse response.
    t = np.arange(-span * samples_per_symbol, span * samples_per_symbol + 1)
    t_symbols = t / float(samples_per_symbol)
    taps = np.exp(-2.0 * (math.pi * bt * t_symbols) ** 2 / math.log(2.0))
    return taps / np.sum(taps)


@tool(
    name="generate_ble_1m_waveform",
    description=(
        "DeepRadio protocol tool: generate an offline complex64 BLE 1M GFSK "
        "advertising waveform. Not a GNU Radio block; never opens hardware."
    ),
    parameters={
        "type": "object",
        "properties": {
            "local_name": {"type": "string"},
            "channel": {"type": "integer"},
            "sample_rate": {"type": "number"},
            "interval_ms": {"type": "number"},
            "bt": {"type": "number"},
            "modulation_index": {"type": "number"},
            "digital_amplitude": {"type": "number"},
        },
        "required": [],
    },
    group="ble",
    origin="deepradio_protocol",
    runtime="deepradio",
    effect_level="ARTIFACT_WRITE",
)
def generate_ble_1m_waveform(
    ctx: ToolContext,
    local_name: str = "",
    channel: int = 37,
    sample_rate: float = 2_000_000.0,
    interval_ms: float = 100.0,
    bt: float = 0.5,
    modulation_index: float = 0.5,
    digital_amplitude: float = 0.5,
) -> Dict[str, Any]:
    sample_rate = float(sample_rate)
    samples_per_symbol = int(round(sample_rate / 1_000_000.0))
    if samples_per_symbol < 2 or abs(samples_per_symbol * 1_000_000.0 - sample_rate) > 1:
        return {"ok": False, "error": "BLE 1M sample_rate must be an integer multiple of 1 MHz and at least 2 MHz"}
    if not 20.0 <= float(interval_ms) <= 10_240.0:
        return {"ok": False, "error": "Advertising interval must be between 20 and 10240 ms"}
    if not 0.3 <= float(bt) <= 0.7:
        return {"ok": False, "error": "BLE GFSK BT must be between 0.3 and 0.7"}
    if not 0.45 <= float(modulation_index) <= 0.55:
        return {"ok": False, "error": "BLE 1M modulation_index must be between 0.45 and 0.55"}
    if not 0.0 < float(digital_amplitude) <= 0.8:
        return {"ok": False, "error": "digital_amplitude must be in (0, 0.8]"}
    packet = _packet(local_name, int(channel))
    symbols = 2.0 * np.asarray(_bits_lsb(packet["air_packet"]), dtype=np.float64) - 1.0
    nrz = np.repeat(symbols, samples_per_symbol)
    shaped = np.convolve(nrz, _gaussian_taps(samples_per_symbol, float(bt)), mode="same")
    phase = np.cumsum(
        (math.pi * float(modulation_index) / samples_per_symbol) * shaped
    )
    burst = (
        float(digital_amplitude) * np.exp(1j * phase)
    ).astype(np.complex64)
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
        "bt": float(bt),
        "modulation_index": float(modulation_index),
        "digital_amplitude": float(digital_amplitude),
    }
    ctx.extra.setdefault("artifacts", {})["ble_waveform"] = str(path)
    return {"ok": True, **ctx.extra["ble_waveform"], "channel": channel,
            "capability": "ble_advertising_single_channel",
            "unsupported_capabilities": [
                "ble_advertising_three_channel",
                "ble_independent_sniffer",
            ]}


@tool(
    name="verify_ble_packet_bits",
    description=(
        "DeepRadio protocol tool: verify the offline BLE advertising packet, "
        "CRC and whitening round-trip. When requested, also verify the local "
        "name. Not a GNU Radio block."
    ),
    parameters={
        "type": "object",
        "properties": {"local_name": {"type": "string"}, "channel": {"type": "integer"}},
        "required": [],
    },
    group="ble",
    origin="deepradio_protocol",
    runtime="deepradio",
)
def verify_ble_packet_bits(
    ctx: ToolContext, local_name: str = "", channel: int = 37
) -> Dict[str, Any]:
    channel = int(channel)
    current = ctx.extra.get("ble_packet") or {}
    air_packet = current.get("air_packet")
    if not isinstance(air_packet, bytes):
        packet_path = str((ctx.extra.get("artifacts") or {}).get("ble_packet") or "")
        try:
            air_packet = Path(packet_path).read_bytes()
        except (OSError, TypeError):
            air_packet = b""
    parsed = _parse_advertising_air_packet(air_packet, channel)
    waveform_check = _verify_waveform_loopback(ctx, air_packet)
    checks = dict(parsed.get("checks") or {})
    if local_name:
        expected_name = local_name.encode("utf-8")
        checks["local_name_matches_request"] = (
            parsed.get("local_name_bytes") == expected_name
        )
    checks.update(waveform_check.get("checks") or {})
    verified = bool(checks and all(checks.values()))
    result = {
        "ok": bool(verified),
        "valid": bool(verified),
        "local_name": local_name,
        "channel": channel,
        "crc_hex": str(parsed.get("crc_hex") or ""),
        "checks": checks,
        "failure_codes": [
            name.upper() for name, passed in checks.items() if not passed
        ],
        "waveform_checked": bool(waveform_check.get("checked")),
    }
    ctx.extra["ble_verification"] = result
    return result


def _parse_advertising_air_packet(air_packet: bytes, channel: int) -> Dict[str, Any]:
    """Parse and validate one advertising-channel packet without the builder."""
    checks = {
        "valid_channel": channel in ADV_FREQUENCIES,
        "preamble_valid": len(air_packet) >= 5 and air_packet[0] == 0xAA,
        "access_address_valid": len(air_packet) >= 5
        and air_packet[1:5] == ADV_ACCESS_ADDRESS,
        "packet_length_valid": False,
        "pdu_type_valid": False,
        "crc_valid": False,
        "advertising_data_valid": False,
    }
    if not all((checks["valid_channel"], checks["preamble_valid"], checks["access_address_valid"])):
        return {"checks": checks}
    decoded = _reference_dewhiten(air_packet[5:], channel)
    if len(decoded) < 5:
        return {"checks": checks}
    payload_length = decoded[1] & 0x3F
    expected_length = 2 + payload_length + 3
    checks["packet_length_valid"] = len(decoded) == expected_length
    if not checks["packet_length_valid"] or payload_length < 6:
        return {"checks": checks}
    pdu = decoded[: 2 + payload_length]
    received_crc = decoded[2 + payload_length : expected_length]
    checks["pdu_type_valid"] = (pdu[0] & 0x0F) == 0x02
    checks["crc_valid"] = received_crc == _reference_crc24(pdu)
    ad_bytes = pdu[8:]
    local_name_bytes = None
    cursor = 0
    ad_valid = True
    while cursor < len(ad_bytes):
        field_length = ad_bytes[cursor]
        if field_length == 0:
            cursor += 1
            continue
        end = cursor + 1 + field_length
        if field_length < 1 or end > len(ad_bytes):
            ad_valid = False
            break
        ad_type = ad_bytes[cursor + 1]
        value = ad_bytes[cursor + 2 : end]
        if ad_type == 0x09:
            local_name_bytes = value
        cursor = end
    checks["advertising_data_valid"] = ad_valid and cursor == len(ad_bytes)
    return {
        "checks": checks,
        "pdu": pdu,
        "crc_hex": received_crc.hex(),
        "local_name_bytes": local_name_bytes,
    }


def _verify_waveform_loopback(ctx: ToolContext, air_packet: bytes) -> Dict[str, Any]:
    """Recover packet bits from the generated IQ file using phase differences."""
    metadata = dict(ctx.extra.get("ble_waveform") or {})
    path = Path(str(metadata.get("path") or ""))
    if not metadata or not path.is_file() or not air_packet:
        return {"checked": False, "checks": {"waveform_available": False}}
    samples_per_symbol = int(metadata.get("samples_per_symbol") or 0)
    modulation_index = float(metadata.get("modulation_index") or 0.5)
    required_samples = len(air_packet) * 8 * samples_per_symbol
    samples = np.fromfile(path, dtype=np.complex64, count=required_samples)
    finite = bool(np.all(np.isfinite(samples)))
    amplitude = np.abs(samples)
    amplitude_ok = bool(
        len(samples) == required_samples
        and finite
        and float(amplitude.max(initial=0.0)) <= 0.800001
        and float(amplitude.max(initial=0.0)) > 0.0
    )
    if not amplitude_ok or samples_per_symbol < 2 or modulation_index <= 0:
        return {
            "checked": True,
            "checks": {
                "waveform_available": True,
                "waveform_finite_and_bounded": amplitude_ok,
                "waveform_bits_recovered": False,
            },
        }
    previous = np.concatenate((np.ones(1, dtype=np.complex64), samples[:-1]))
    phase_step = np.angle(samples * np.conj(previous))
    symbol_metric = phase_step.reshape(-1, samples_per_symbol).mean(axis=1)
    recovered = _bytes_lsb((symbol_metric >= 0).astype(np.uint8).tolist())
    sample_rate = float(metadata.get("sample_rate") or 0.0)
    peak_deviation_hz = float(np.max(np.abs(phase_step))) * sample_rate / (2.0 * math.pi)
    expected_deviation_hz = modulation_index * 1_000_000.0 / 2.0
    deviation_ok = bool(
        expected_deviation_hz > 0
        and abs(peak_deviation_hz - expected_deviation_hz)
        <= expected_deviation_hz * 0.15
    )
    return {
        "checked": True,
        "peak_deviation_hz": peak_deviation_hz,
        "checks": {
            "waveform_available": True,
            "waveform_finite_and_bounded": amplitude_ok,
            "waveform_bits_recovered": recovered == air_packet,
            "waveform_frequency_deviation_valid": deviation_ok,
        },
    }


@tool(
    name="build_ble_uhd_tx_flowgraph",
    description=(
        "DeepRadio compose tool: assemble GNU Radio blocks "
        "(file_source → uhd_usrp_sink) into a BLE TX flowgraph. Does not start RF."
    ),
    parameters={
        "type": "object",
        "properties": {
            "waveform_path": {"type": "string"},
            "channel": {"type": "integer"},
            "sample_rate": {"type": "number"},
            "gain": {"type": "number"},
            "device_args": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
        "required": ["waveform_path"],
    },
    group="ble",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    effect_level="ARTIFACT_WRITE",
)
def build_ble_uhd_tx_flowgraph(
    ctx: ToolContext,
    waveform_path: str,
    channel: int = 37,
    sample_rate: float = 2_000_000.0,
    gain: float = 0.0,
    device_args: str = "",
    duration_seconds: float = 30.0,
) -> Dict[str, Any]:
    device_args = device_args_for("b210", device_args)
    path = Path(waveform_path).resolve()
    if not path.is_file():
        return {"ok": False, "error": "BLE waveform file does not exist"}
    if int(channel) not in ADV_FREQUENCIES:
        return {"ok": False, "error": "Only BLE advertising channels 37, 38, and 39 are supported"}
    if not 0.0 <= float(gain) <= 10.0:
        return {"ok": False, "error": "The initial safety policy limits TX gain to 0–10 dB"}
    if not 1.0 <= float(duration_seconds) <= 60.0:
        return {"ok": False, "error": "TX duration_seconds must be between 1 and 60 seconds"}
    steps = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        return result

    invoke("init_flow_graph", {"flowgraph_id": "ble_b210_advertiser", "generate_options": "no_gui"})
    _set_runtime_mode(ctx)
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
            "id": "bounded_tx",
            "params": {
                "type": "complex",
                "num_items": str(int(float(sample_rate) * float(duration_seconds))),
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
    invoke("connect", {"src_id": "ble_waveform", "dst_id": "bounded_tx"})
    # UHD Sink exposes the message command port before stream channel 0.
    invoke("connect", {"src_id": "bounded_tx", "dst_id": "b210_sink", "dst_port": 1})
    validation = invoke("validate_flowgraph", {})
    if validation.get("valid"):
        _leave_hardware_sink_unarmed(ctx, "b210_sink")
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
        "hardware": "b210",
        "duration_seconds": float(duration_seconds),
        "armed": False,
        "not_started": True,
    }


@tool(
    name="build_ble_pluto_tx_flowgraph",
    description=(
        "DeepRadio compose tool: assemble GNU Radio blocks "
        "(file_source → iio_pluto_sink) into a BLE TX flowgraph. Does not start RF."
    ),
    parameters={
        "type": "object",
        "properties": {
            "waveform_path": {"type": "string"},
            "channel": {"type": "integer"},
            "sample_rate": {"type": "number"},
            "attenuation": {"type": "number"},
            "uri": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
        "required": ["waveform_path"],
    },
    group="ble",
    origin="deepradio_compose",
    runtime="gnuradio_blocks",
    effect_level="ARTIFACT_WRITE",
)
def build_ble_pluto_tx_flowgraph(
    ctx: ToolContext,
    waveform_path: str,
    channel: int = 37,
    sample_rate: float = 2_000_000.0,
    attenuation: float = 30.0,
    uri: str = "",
    duration_seconds: float = 30.0,
) -> Dict[str, Any]:
    path = Path(waveform_path).resolve()
    if not path.is_file():
        return {"ok": False, "error": "BLE waveform file does not exist"}
    if int(channel) not in ADV_FREQUENCIES:
        return {"ok": False, "error": "Only BLE advertising channels 37, 38, and 39 are supported"}
    if not 10.0 <= float(attenuation) <= 80.0:
        return {"ok": False, "error": "The initial safety policy limits Pluto TX attenuation to 10–80 dB"}
    if not 1.0 <= float(duration_seconds) <= 60.0:
        return {"ok": False, "error": "TX duration_seconds must be between 1 and 60 seconds"}
    steps = []

    def invoke(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = call(name, args, ctx)
        steps.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        return result

    invoke("init_flow_graph", {"flowgraph_id": "ble_pluto_advertiser", "generate_options": "no_gui"})
    _set_runtime_mode(ctx)
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
            "id": "bounded_tx",
            "params": {
                "type": "complex",
                "num_items": str(int(float(sample_rate) * float(duration_seconds))),
            },
        },
    )
    added = invoke(
        "add_block",
        {
            "key": "iio_pluto_sink",
            "id": "pluto_sink",
            "params": {
                "type": "fc32",
                "uri": repr(uri),
                "frequency": str(int(ADV_FREQUENCIES[int(channel)])),
                "samplerate": str(int(float(sample_rate))),
                "bandwidth": str(int(float(sample_rate))),
                "buffer_size": "32768",
                "cyclic": "False",
                "attenuation1": str(float(attenuation)),
                "filter_source": "'Auto'",
            },
        },
    )
    if not added.get("ok"):
        return {
            "ok": False,
            "valid": False,
            "error": added.get("error") or "iio_pluto_sink is unavailable in the current environment",
            "steps": steps,
            "not_started": True,
        }
    invoke("connect", {"src_id": "ble_waveform", "dst_id": "bounded_tx"})
    invoke("connect", {"src_id": "bounded_tx", "dst_id": "pluto_sink"})
    validation = invoke("validate_flowgraph", {})
    if validation.get("valid"):
        _leave_hardware_sink_unarmed(ctx, "pluto_sink")
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
        "attenuation": float(attenuation),
        "hardware": "pluto",
        "duration_seconds": float(duration_seconds),
        "armed": False,
        "not_started": True,
    }
