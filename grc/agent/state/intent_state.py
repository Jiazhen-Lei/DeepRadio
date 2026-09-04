"""Versioned, user-confirmable intent shared across the whole agent run."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


INTENT_STATUSES = frozenset(
    {"idle", "draft", "awaiting_input", "awaiting_confirmation", "confirmed", "superseded"}
)
SPEC_FIELD_GROUPS = frozenset({"required", "added"})
SPEC_FIELD_STATUSES = frozenset({"aligned", "needs_confirmation", "missing"})
_USER_SOURCES = frozenset({"user", "extracted"})
_SOURCE_ALIASES = {
    "user_choice": "user",
    "user_revision": "user",
    "user_text": "extracted",
    "llm": "extracted",
    "default": "protocol_default",
    "safe_preview_default": "safety_default",
    "rules": "derived",
}


def semantic_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class SpecificationField:
    """One row in the user-visible Radio Specification."""

    key: str
    value: Any = None
    label: str = ""
    group: str = "added"
    source: str = "unresolved"
    status: str = "missing"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpecificationField":
        key = str(data.get("key") or "")
        value = data.get("value")
        source = _SOURCE_ALIASES.get(
            str(data.get("source") or "unresolved"),
            str(data.get("source") or "unresolved"),
        )
        group = str(data.get("group") or "").lower()
        if group not in SPEC_FIELD_GROUPS:
            group = (
                "required"
                if (
                    data.get("requirement") == "required"
                    or key == "goal"
                    or source not in _USER_SOURCES
                )
                else "added"
            )
        status = str(data.get("status") or "").lower()
        if status not in SPEC_FIELD_STATUSES:
            if value in (None, "", []):
                status = "missing"
            elif bool(data.get("confirmed")) or group == "added":
                status = "aligned"
            else:
                status = "needs_confirmation"
        return cls(
            key=key,
            value=value,
            label=str(data.get("label") or key.replace("_", " ").title()),
            group=group,
            source=source,
            status=status,
        )


@dataclass
class RadioSpecification:
    """Versioned specification embedded in SharedIntent.

    It is the canonical communication-parameter view.  The GUI and exported
    ``radio_specification.json`` are projections and never independent writers.
    """

    revision: int = 0
    fields: List[SpecificationField] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    assumptions: List[Dict[str, Any]] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RadioSpecification":
        return cls(
            revision=int(data.get("revision", 0) or 0),
            fields=[
                SpecificationField.from_dict(item)
                for item in data.get("fields") or []
                if isinstance(item, dict) and item.get("key")
            ],
            constraints=dict(data.get("constraints") or {}),
            assumptions=list(data.get("assumptions") or []),
            validation_errors=list(data.get("validation_errors") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def field(self, key: str) -> SpecificationField | None:
        return next((item for item in self.fields if item.key == key), None)

    def unresolved_fields(self) -> List[Dict[str, str]]:
        unresolved = [
            {
                "key": item.key,
                "label": item.label or item.key,
                "status": item.status,
            }
            for item in self.fields
            if item.group == "required" and item.status != "aligned"
        ]
        if self.field("goal") is None:
            unresolved.insert(0, {"key": "goal", "label": "Goal", "status": "missing"})
        return unresolved

    def validate(self) -> List[str]:
        errors: List[str] = []
        seen = set()
        for item in self.fields:
            if not item.key:
                errors.append("Specification field key cannot be empty")
                continue
            if item.key in seen:
                errors.append(f"Duplicate Specification field: {item.key}")
            seen.add(item.key)
            if item.group not in SPEC_FIELD_GROUPS:
                errors.append(f"Invalid group for {item.key}: {item.group}")
            if item.status not in SPEC_FIELD_STATUSES:
                errors.append(f"Invalid status for {item.key}: {item.status}")
            if item.status == "aligned" and item.value in (None, "", []):
                errors.append(f"Aligned field has no value: {item.key}")
            if item.status == "missing" and item.value not in (None, "", []):
                errors.append(f"Missing field already has a value: {item.key}")
            if item.group == "added" and item.source not in _USER_SOURCES:
                errors.append(f"Added field must come from the user: {item.key}")
            if item.group == "added" and item.status != "aligned":
                errors.append(f"Added field must already be aligned: {item.key}")
        if self.field("goal") and self.field("goal").group != "required":
            errors.append("Goal must belong to Required")
        if not isinstance(self.constraints, dict):
            errors.append("Specification constraints must be an object")
        if not isinstance(self.assumptions, list) or any(
            not isinstance(item, dict) for item in self.assumptions
        ):
            errors.append("Specification assumptions must be a list of objects")
        return errors


@dataclass
class SharedIntent:
    """Canonical intent contract.

    MainAgent owns Workflow intent. SpecAgent owns ``specification``; the host
    persists it and exposes a read-only projection to the GUI.
    """

    intent_id: str = ""
    workflow_id: str = ""
    revision: int = 0
    status: str = "idle"
    raw_text: str = ""
    task_type: str = ""
    capabilities: List[str] = field(default_factory=list)
    specification: RadioSpecification = field(default_factory=RadioSpecification)
    semantic_hash: str = ""
    confirmed_at: float = 0.0
    patch_history: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if self.status not in INTENT_STATUSES:
            raise ValueError(f"非法 SharedIntent status: {self.status}")
        if self.status != "idle" and (not self.intent_id or self.revision < 1):
            raise ValueError("非空 SharedIntent 必须具有 intent_id/revision")

    def contract_payload(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "task_type": self.task_type,
            "capabilities": list(self.capabilities),
            "specification": self.specification.to_dict(),
        }

    def refresh_hash(self) -> str:
        self.semantic_hash = semantic_hash(self.contract_payload())
        return self.semantic_hash

    def snapshot(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SharedIntent":
        specification_data = dict(data.get("specification") or {})
        specification_data.setdefault("constraints", dict(data.get("constraints") or {}))
        specification_data.setdefault("assumptions", list(data.get("assumptions") or []))
        specification = RadioSpecification.from_dict(specification_data)
        if not specification.fields:
            goals = list(data.get("goals") or [])
            if goals:
                specification.fields.append(SpecificationField(
                    key="goal", label="Goal", value=str(goals[0]),
                    group="required", source="user", status="aligned",
                ))
            sources = dict(data.get("parameter_sources") or {})
            for key, value in dict(data.get("parameters") or {}).items():
                if value in (None, "", []) or key == "success_conditions":
                    continue
                specification.fields.append(SpecificationField.from_dict({
                    "key": key,
                    "value": value,
                    "source": sources.get(key) or "extracted",
                    "status": "aligned",
                }))
            criteria = data.get("success_criteria") or (
                data.get("parameters") or {}
            ).get("success_conditions") or []
            if criteria:
                specification.fields.append(SpecificationField(
                    key="success_conditions", label="Success condition",
                    value=criteria, group="required", source="extracted",
                    status="aligned",
                ))
        item = cls(
            intent_id=str(data.get("intent_id") or ""),
            workflow_id=str(data.get("workflow_id") or ""),
            revision=int(data.get("revision", 0) or 0),
            status=str(data.get("status") or "idle"),
            raw_text=str(data.get("raw_text") or ""),
            task_type=str(data.get("task_type") or ""),
            capabilities=list(data.get("capabilities") or []),
            specification=specification,
            semantic_hash=str(data.get("semantic_hash") or ""),
            confirmed_at=float(data.get("confirmed_at", 0.0) or 0.0),
            patch_history=list(data.get("patch_history") or []),
        )
        item.validate()
        return item

    @classmethod
    def new(cls, raw_text: str = "", workflow_id: str = "") -> "SharedIntent":
        return cls(
            intent_id=f"intent-{uuid.uuid4().hex[:10]}",
            workflow_id=str(workflow_id or ""),
            revision=1,
            status="draft",
            raw_text=str(raw_text or ""),
        )

    def record_patch(
        self, *, changed_fields: List[str], scope: str, source: str, note: str = ""
    ) -> None:
        self.patch_history.append(
            {
                "revision": self.revision,
                "changed_fields": list(changed_fields),
                "scope": str(scope or "none"),
                "source": str(source or "unknown"),
                "note": str(note or ""),
                "ts": time.time(),
            }
        )
