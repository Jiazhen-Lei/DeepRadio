"""Persistent, non-secret DeepRadio runtime preferences."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    override = str(os.environ.get("GRC_AGENT_SETTINGS_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "deepradio" / "settings.json"


def _read_settings() -> dict[str, Any]:
    try:
        payload = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def rf_runtime_enabled() -> bool:
    """Return the explicit process override or persisted user preference."""
    explicit = str(os.environ.get("GRC_AGENT_ENABLE_RF") or "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return _read_settings().get("rf_runtime_enabled") is True


def set_rf_runtime_enabled(enabled: bool) -> Path:
    """Persist an explicit user choice and apply it to the current GUI."""
    path = settings_path()
    payload = _read_settings()
    payload["rf_runtime_enabled"] = bool(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    os.environ["GRC_AGENT_ENABLE_RF"] = "1" if enabled else "0"
    return path
