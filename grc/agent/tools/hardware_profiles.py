"""Declarative SDR capability profiles used by planning and runtime tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HardwareProfile:
    key: str
    label: str
    aliases: tuple[str, ...]
    driver_family: str
    frequency_range: tuple[float, float]
    ble_tx_builder: str = ""
    default_device_args: str = ""

    def command(self, *, probe: bool, identity: str = "") -> list[str]:
        if self.driver_family == "uhd":
            command = ["uhd_usrp_probe" if probe else "uhd_find_devices"]
            address = identity or self.default_device_args
            if probe and address and "=" not in address:
                address = f"serial={address}"
            return [*command, "--args", address] if address else command
        if self.driver_family == "iio":
            if probe:
                return ["iio_info", "-u", identity] if identity else []
            # Limit discovery to USB by default.  ``iio_info`` uses an
            # uppercase -S for scan; lowercase -s is not a scan option.
            return ["iio_info", "-S", "usb"]
        if self.driver_family == "hackrf":
            return ["hackrf_info"]
        if self.driver_family == "lime":
            return ["LimeUtil", "--info" if probe else "--find"]
        return []


_PROFILES = (
    HardwareProfile(
        key="b210",
        label="USRP B210",
        aliases=("b210", "b200", "usrpb210"),
        driver_family="uhd",
        frequency_range=(70e6, 6e9),
        ble_tx_builder="build_ble_uhd_tx_flowgraph",
        default_device_args="type=b200",
    ),
    HardwareProfile(
        key="usrp",
        label="USRP（型号未指定）",
        aliases=("usrp", "ettus usrp"),
        driver_family="uhd",
        frequency_range=(0.0, float("inf")),
    ),
    HardwareProfile(
        key="pluto",
        label="PlutoSDR",
        aliases=("pluto", "plutosdr", "adalm-pluto"),
        driver_family="iio",
        frequency_range=(47e6, 6e9),
        ble_tx_builder="build_ble_pluto_tx_flowgraph",
    ),
    HardwareProfile(
        key="hackrf",
        label="HackRF",
        aliases=("hackrf", "hackrf one"),
        driver_family="hackrf",
        frequency_range=(1e6, 6e9),
    ),
    HardwareProfile(
        key="limesdr",
        label="LimeSDR",
        aliases=("limesdr", "lime sdr", "lime"),
        driver_family="lime",
        frequency_range=(100e3, 3.8e9),
    ),
)


def iter_profiles():
    """Read-only access to all hardware profiles (cross-family discovery)."""
    return _PROFILES


def resolve_hardware_profile(value: str) -> Optional[HardwareProfile]:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    for profile in _PROFILES:
        if normalized == profile.key or any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                normalized,
            )
            for alias in profile.aliases
        ):
            return profile
    return None


def normalize_hardware(value: str) -> str:
    profile = resolve_hardware_profile(value)
    return profile.key if profile else (value or "").strip().lower()


def device_args_for(device_type: str, override: str = "") -> str:
    """Return UHD/IIO device args from the profile unless the caller overrode them."""
    if override:
        return override
    profile = resolve_hardware_profile(device_type)
    return str(profile.default_device_args or "") if profile else ""


def parse_device_identity(profile: HardwareProfile, output: str) -> str:
    """Extract a stable address when the backend exposes one."""
    text = output or ""
    if profile.driver_family == "iio":
        match = re.search(r"\[((?:usb|ip|local):[^\]]+)\]", text, re.I)
        if match:
            return match.group(1)
    if profile.driver_family == "uhd":
        match = re.search(r"(?:serial|addr)\s*[:=]\s*([^,\s]+)", text, re.I)
        if match:
            return match.group(1)
    return ""


def output_indicates_device(profile: HardwareProfile, output: str) -> bool:
    """Return whether discovery output identifies the requested SDR family."""
    text = (output or "").strip().lower()
    if not text:
        return False
    if profile.key == "pluto":
        return "pluto" in text or "adalm" in text
    if profile.key == "b210":
        return "b210" in text or "b200" in text or "usrp" in text
    if profile.key == "usrp":
        return "usrp" in text
    if profile.key == "hackrf":
        return "hackrf" in text
    if profile.key == "limesdr":
        return "lime" in text
    return False


def output_indicates_successful_probe(
    profile: HardwareProfile, output: str
) -> bool:
    """Validate the structure of output from an identity-bound probe.

    Discovery output usually contains a product name, while an exact IIO
    context dump commonly contains only device nodes and attributes.  Probe
    acceptance therefore must not reuse product-name matching.
    """
    text = (output or "").strip()
    if not text:
        return False
    if profile.driver_family == "iio":
        return bool(re.search(r"(?m)^\s*iio:device\d+\s*:", text))
    return output_indicates_device(profile, text)
