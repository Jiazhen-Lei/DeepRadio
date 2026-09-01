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


def semantic_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class SpecificationField:
    """One user-visible radio parameter with requirement and provenance."""

    key: str
    value: Any = None
    label: str = ""
    requirement: str = "mentioned"
    source: str = "unresolved"
    locked: bool = True
    confirmed: bool = False
    reason: str = ""
    depends_on: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpecificationField":
        return cls(
            key=str(data.get("key") or ""),
            value=data.get("value"),
            label=str(data.get("label") or data.get("key") or ""),
            requirement=str(data.get("requirement") or "mentioned"),
            source=str(data.get("source") or "unresolved"),
            locked=bool(data.get("locked", True)),
            confirmed=bool(data.get("confirmed")),
            reason=str(data.get("reason") or ""),
            depends_on=list(data.get("depends_on") or []),
        )


@dataclass
class RadioSpecification:
    """Versioned specification embedded in SharedIntent.

    It is the canonical communication-parameter view.  The GUI and exported
    ``radio_specification.json`` are projections and never independent writers.
    """

    profile_refs: List[str] = field(default_factory=list)
    fields: List[SpecificationField] = field(default_factory=list)
    blocking_questions: List[Dict[str, Any]] = field(default_factory=list)
    optional_prompts: List[Dict[str, Any]] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RadioSpecification":
        return cls(
            profile_refs=list(data.get("profile_refs") or []),
            fields=[
                SpecificationField.from_dict(item)
                for item in data.get("fields") or []
                if isinstance(item, dict) and item.get("key")
            ],
            blocking_questions=list(data.get("blocking_questions") or []),
            optional_prompts=list(data.get("optional_prompts") or []),
            validation_errors=list(data.get("validation_errors") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SharedIntent:
    """Canonical intent contract.

    MainAgent is the semantic owner. The host persists its structured update
    and exposes a read-only snapshot to SubAgents and the GUI.
    """

    intent_id: str = ""
    revision: int = 0
    status: str = "idle"
    raw_text: str = ""
    task_type: str = ""
    capabilities: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    parameter_sources: Dict[str, str] = field(default_factory=dict)
    goals: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    success_criteria: List[str] = field(default_factory=list)
    assumptions: List[Dict[str, Any]] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    interaction: Dict[str, Any] = field(default_factory=dict)
    intent_ir: Dict[str, Any] = field(default_factory=dict)
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
            "task_type": self.task_type,
            "capabilities": list(self.capabilities),
            "parameters": dict(self.parameters),
            "goals": list(self.goals),
            "constraints": dict(self.constraints),
            "success_criteria": list(self.success_criteria),
            "intent_ir": dict(self.intent_ir),
            "specification": self.specification.to_dict(),
        }

    def refresh_hash(self) -> str:
        self.semantic_hash = semantic_hash(self.contract_payload())
        return self.semantic_hash

    def snapshot(self) -> Dict[str, Any]:
        data = asdict(self)
        # Interactions are UI transport, not part of the immutable execution
        # contract handed to subagents.
        data.pop("interaction", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SharedIntent":
        item = cls(
            intent_id=str(data.get("intent_id") or ""),
            revision=int(data.get("revision", 0) or 0),
            status=str(data.get("status") or "idle"),
            raw_text=str(data.get("raw_text") or ""),
            task_type=str(data.get("task_type") or ""),
            capabilities=list(data.get("capabilities") or []),
            parameters=dict(data.get("parameters") or {}),
            parameter_sources=dict(data.get("parameter_sources") or {}),
            goals=list(data.get("goals") or []),
            constraints=dict(data.get("constraints") or {}),
            success_criteria=list(data.get("success_criteria") or []),
            assumptions=list(data.get("assumptions") or []),
            missing_fields=list(data.get("missing_fields") or []),
            validation_errors=list(data.get("validation_errors") or []),
            interaction=dict(data.get("interaction") or {}),
            intent_ir=dict(data.get("intent_ir") or {}),
            specification=RadioSpecification.from_dict(
                dict(data.get("specification") or {})
            ),
            semantic_hash=str(data.get("semantic_hash") or ""),
            confirmed_at=float(data.get("confirmed_at", 0.0) or 0.0),
            patch_history=list(data.get("patch_history") or []),
        )
        item.validate()
        return item

    @classmethod
    def new(cls, raw_text: str = "") -> "SharedIntent":
        return cls(
            intent_id=f"intent-{uuid.uuid4().hex[:10]}",
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
