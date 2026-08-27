"""Generic planning and side-effect policy primitives.

The evaluation taxonomy is deliberately absent from this module.  Plans are
checked by the effects they request, so an unseen or compound user request is
subject to the same safety rules as a catalog-backed compatibility workflow.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Iterable, Mapping


class EffectLevel(IntEnum):
    READ = 0
    ARTIFACT_WRITE = 1
    DEVICE_READ = 2
    DEVICE_CONFIG = 3
    RF_RUN = 4


_EFFECT_NAMES = {item.name: item for item in EffectLevel}


def normalize_effect(value: Any) -> EffectLevel:
    if isinstance(value, EffectLevel):
        return value
    return _EFFECT_NAMES.get(str(value or "READ").strip().upper(), EffectLevel.READ)


def is_rf_grant_effect(value: Any) -> bool:
    """True when the grant covers device mutation or bounded RF."""
    return normalize_effect(value) >= EffectLevel.DEVICE_CONFIG


def stage_display_label(
    stage_id: str,
    default_label: str = "",
    requested_effect: Any = "",
) -> str:
    """Human stage name. Config confirm is not an RF authorization."""
    if str(stage_id or "") == "rf_plan_confirmation":
        return "RF 计划确认" if is_rf_grant_effect(requested_effect) else "配置确认"
    return default_label or str(stage_id or "")


@dataclass(frozen=True)
class CapabilityBlocker:
    code: str
    capability: str
    requested_effect: str
    message: str
    remediation: str = ""
    retryable: bool = False
    requires_restart: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "capability": self.capability,
            "requested_effect": self.requested_effect,
            "message": self.message,
            "remediation": self.remediation,
            "retryable": self.retryable,
            "requires_restart": self.requires_restart,
            "details": dict(self.details),
        }


def system_capability_blocker(effect: Any) -> CapabilityBlocker | None:
    """Return the launch-time blocker for an upcoming side effect.

    Offline design, artifact generation, and read-only probing stay available
    when RF runtime is disabled.  Device mutation and RF execution require the
    explicit process capability before a user is asked to authorize them.
    """
    requested = normalize_effect(effect)
    if requested < EffectLevel.DEVICE_CONFIG:
        return None
    if os.environ.get("GRC_AGENT_ENABLE_RF") == "1":
        return None
    return CapabilityBlocker(
        code="SYSTEM_CAPABILITY_MISSING",
        capability="rf_runtime",
        requested_effect=requested.name,
        message="当前 GUI 进程未启用 RF 运行能力，不能接受本次执行授权。",
        remediation=(
            "关闭 GRC，设置 GRC_AGENT_ENABLE_RF=1 后重新启动；"
            "会话恢复后再确认。"
        ),
        retryable=False,
        requires_restart=True,
    )


def _field(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def split_at_decision_boundary(items: Iterable[Any]) -> tuple[list[Any], list[Any]]:
    """Keep work through the first user checkpoint; defer the rest."""
    sequence = list(items)
    horizon: list[Any] = []
    for index, item in enumerate(sequence):
        horizon.append(item)
        if "checkpoint" in str(_field(item, "interaction") or ""):
            return horizon, sequence[index + 1:]
    return horizon, []


def stops_at_boundary(intent: Any, stage_id: str) -> bool:
    """True when the current decision is a requested handoff, not RF grant.

    Slot-filling alignment is never the handoff, even when the user asked to
    stop at the next real decision boundary.
    """
    stage_id = str(stage_id or "")
    conditions = [str(item) for item in (getattr(intent, "stop_conditions", None) or [])]
    if f"stop_at:{stage_id}" in conditions:
        return True
    if "stop_at_decision_boundary" not in conditions:
        return False
    return "alignment" not in stage_id


def stage_plan_item(stage: Any) -> dict:
    """Serialize a Stage (or plan dict) as a pending deferred-plan item."""
    if isinstance(stage, Mapping):
        data = dict(stage)
    else:
        data = asdict(stage)
    data.pop("checkpoint", None)
    data["execution_status"] = "pending"
    data["outcome"] = ""
    data["result"] = {}
    data["result_history"] = []
    data["attempt"] = 0
    data["resume_pending"] = False
    data["resume_from"] = ""
    return data


def highest_effect(items: Iterable[Any]) -> EffectLevel:
    """Highest effect before the next decision or safety finalizer."""
    highest = EffectLevel.READ
    for item in items:
        if _field(item, "safety_finalizer"):
            break
        if "checkpoint" in str(_field(item, "interaction") or ""):
            break
        highest = max(highest, normalize_effect(_field(item, "effect_level")))
    return highest


def visible_plan_horizon(stages: Iterable[Any], current_stage: str) -> list[Any]:
    """Expose completed/current work only through the next decision boundary."""
    items = list(stages)
    if not items:
        return []
    current_index = next(
        (index for index, item in enumerate(items)
         if _field(item, "id") == current_stage), 0
    )
    end = len(items)
    for index in range(current_index, len(items)):
        if "checkpoint" in str(_field(items[index], "interaction") or ""):
            end = index + 1
            break
    return items[:end]


def project_intent_ir(intent: Any) -> None:
    """Populate open IntentIR fields without changing authoritative user facts."""
    raw_text = str(getattr(intent, "raw_text", "") or "")
    capabilities = list(getattr(intent, "capabilities", None) or [])
    slots = dict(getattr(intent, "slots", None) or {})
    context = dict(getattr(intent, "context", None) or {})
    if not getattr(intent, "goals", None) and raw_text:
        intent.goals = [raw_text]
    if not getattr(intent, "requested_operations", None):
        intent.requested_operations = capabilities
    if not getattr(intent, "constraints", None):
        intent.constraints = {
            key: value
            for key, value in slots.items()
            if key in {
                "duration_seconds", "max_duration_seconds", "deploy_permission",
                "hardware_access",
            }
        }
    forbidden = list(context.get("forbidden_capabilities") or [])
    if forbidden and not getattr(intent, "forbidden_effects", None):
        intent.forbidden_effects = forbidden
    if str(slots.get("operation") or "") == "prepare":
        if "stop_at_decision_boundary" not in getattr(intent, "stop_conditions", []):
            intent.stop_conditions.append("stop_at_decision_boundary")
    elif str(slots.get("operation") or "") == "deploy":
        intent.stop_conditions = [
            item for item in list(getattr(intent, "stop_conditions", None) or [])
            if item != "stop_at_decision_boundary"
        ]
    if not getattr(intent, "entities", None):
        intent.entities = {
            key: value
            for key, value in slots.items()
            if key in {
                "hardware", "protocol", "modulation", "local_name",
                "carrier_frequency", "sample_rate",
            } and value not in (None, "", [])
        }
    # Legacy sessions stored a named terminal checkpoint.
    terminal = str(slots.get("terminal_checkpoint") or "")
    if terminal:
        marker = f"stop_at:{terminal}"
        if marker not in getattr(intent, "stop_conditions", []):
            intent.stop_conditions.append(marker)

