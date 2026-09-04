"""Load the canonical Stage contracts used by Workflow and tool policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills/grc-orchestration/references/stage_library.yaml"
)


def load_stage_catalog() -> Dict[str, Dict[str, Any]]:
    try:
        from grc.core.io import yaml

        data = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
        catalog = dict(data.get("stages") or {})
    except Exception as exc:  # noqa: BLE001 - return a controlled Workflow error
        raise ValueError(f"Stage library could not be loaded: {exc}") from exc
    if not catalog:
        raise ValueError("Stage library is empty")
    return catalog


def stage_contract(stage_id: str) -> Dict[str, Any]:
    definition = load_stage_catalog().get(str(stage_id or ""))
    if not isinstance(definition, dict):
        raise ValueError(f"Unknown Stage: {stage_id or '(empty)'}")
    return definition


def allowed_tools_for_stage(stage_id: str) -> set[str]:
    return {
        str(name)
        for name in stage_contract(stage_id).get("allowed_tools") or []
        if name
    }
