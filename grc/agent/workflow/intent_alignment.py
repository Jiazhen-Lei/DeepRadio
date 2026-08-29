"""Alignment Gate before Workflow instantiation.

This module deliberately does not execute stages.  It turns an underspecified
request into a versioned SharedIntent, requests one answer at a time, and only
hands a confirmed WorkflowIntent to the existing WorkflowEngine.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..knowledge.spec_requirements import missing_required_fields, question_for
from ..state import SharedIntent
from .planning import project_intent_ir
from .schema import WorkflowIntent
from .revision import analyze_intent_patch


_APPROVE = frozenset({"确认", "确认意图", "同意", "正确", "继续", "approve", "confirmed"})
_REVISE = frozenset({"修改", "不对", "返回修改", "重新填写", "revise", "reject"})


@dataclass
class AlignmentOutcome:
    pending: bool
    intent: Optional[WorkflowIntent] = None
    message: str = ""


class IntentAlignmentCoordinator:
    """Host-owned writer for SharedIntent and structured interactions."""

    def __init__(
        self,
        engine: Any,
        state: Any,
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.engine = engine
        self.state = state
        self.event_sink = event_sink or (lambda _event, _payload: None)

    def needs_alignment(self) -> bool:
        item = self.state.intent
        return item.status in {"draft", "awaiting_input", "awaiting_confirmation"}

    def consume_text(self, text: str) -> AlignmentOutcome:
        shared = self.state.intent
        if self.needs_alignment():
            if shared.status == "awaiting_confirmation":
                normalized = str(text or "").strip().lower()
                if normalized in _APPROVE:
                    return self._confirm()
                if normalized in _REVISE:
                    return self._request_revision()
                return self._merge_free_text(text)
            return self._answer_field(text, source="user_text")
        return self._start(text)

    def consume_response(self, response: Dict[str, Any]) -> AlignmentOutcome:
        shared = self.state.intent
        interaction = dict(shared.interaction or {})
        if not interaction or str(response.get("interaction_id") or "") != str(
            interaction.get("interaction_id") or ""
        ):
            return AlignmentOutcome(True, message="意图交互已变化，请刷新后重试。")
        try:
            base_revision = int(response.get("base_intent_revision") or 0)
        except (TypeError, ValueError):
            base_revision = 0
        if base_revision != shared.revision:
            return AlignmentOutcome(True, message="意图版本已变化，请按最新问题回答。")
        if interaction.get("kind") == "intent_confirmation":
            decision = str(response.get("decision") or "")
            if decision == "approved":
                return self._confirm()
            return self._request_revision()
        value = response.get("custom_value")
        if value in (None, ""):
            value = response.get("value")
        return self._apply_field(
            str(interaction.get("field") or ""), value, source="user_choice"
        )

    def consume_updates(self, response: Dict[str, Any]) -> AlignmentOutcome:
        """Apply one Radio Specification table submission atomically."""
        shared = self.state.intent
        interaction = dict(shared.interaction or {})
        interaction_id = str(response.get("interaction_id") or "")
        if interaction and interaction_id != str(interaction.get("interaction_id") or ""):
            return AlignmentOutcome(True, message="意图交互已变化，请刷新后重试。")
        if str(response.get("intent_id") or shared.intent_id) != shared.intent_id:
            return AlignmentOutcome(True, message="意图已变化，请按最新规格修改。")
        try:
            base_revision = int(response.get("base_intent_revision") or 0)
        except (TypeError, ValueError):
            base_revision = 0
        if base_revision != shared.revision:
            return AlignmentOutcome(True, message="意图版本已变化，请按最新规格修改。")
        updates = dict(response.get("updates") or {})
        if not updates:
            if interaction.get("kind") == "intent_confirmation":
                return self._confirm()
            return self._evaluate(
                self._to_workflow_intent(shared), had_user_questions=True
            )
        changed = []
        before = dict(shared.parameters or {})
        for field, raw_value in updates.items():
            field = str(field or "")
            if not field:
                continue
            value = self._coerce(field, raw_value)
            if value in (None, "", []):
                continue
            if field == "success_conditions" and not isinstance(value, list):
                value = [str(value)]
            if field == "advertising_channels":
                if not isinstance(value, list):
                    match = re.search(r"\d+", str(value))
                    value = [int(match.group(0))] if match else []
                if value:
                    channel = int(value[0])
                    centers = {37: 2_402_000_000.0, 38: 2_426_000_000.0, 39: 2_480_000_000.0}
                    if channel in centers:
                        shared.parameters["carrier_frequency"] = centers[channel]
                        shared.parameter_sources["carrier_frequency"] = "derived"
            if shared.parameters.get(field) != value or field in shared.missing_fields:
                shared.parameters[field] = value
                shared.parameter_sources[field] = "user_choice"
                changed.append(field)
            if field == "duration_seconds":
                shared.parameters["max_duration_seconds"] = value
                shared.parameter_sources["max_duration_seconds"] = "user_choice"
        if not changed:
            return self._evaluate(
                self._to_workflow_intent(shared), had_user_questions=True
            )
        shared.revision += 1
        shared.record_patch(
            changed_fields=changed,
            scope="intent_only",
            source="radio_specification_table",
            note="{} -> {}".format(before, shared.parameters),
        )
        self.event_sink(
            "specification_table_updated",
            {**self._event_payload(), "changed_fields": changed},
        )
        return self._evaluate(
            self._to_workflow_intent(shared), had_user_questions=True
        )

    def project_confirmed(self, intent: WorkflowIntent, *, source: str = "workflow") -> None:
        """Synchronize an active Workflow without granting it mutation authority."""
        shared = self.state.intent
        before = dict(shared.parameters or {})
        if shared.status == "idle":
            shared = SharedIntent.new(intent.raw_text)
            self.state.intent = shared
        self._project(shared, intent)
        shared.status = "confirmed"
        shared.interaction = {}
        if not shared.confirmed_at:
            shared.confirmed_at = time.time()
        impact = analyze_intent_patch(
            before,
            shared.parameters,
            runtime_active=str(getattr(self.state.runtime, "status", "")) == "running",
        )
        changed = list(impact["changed_fields"])
        if changed and before:
            shared.revision += 1
            shared.record_patch(
                changed_fields=changed,
                scope=str(impact["scope"]),
                source=source,
                note="Workflow intent synchronized after a user adjustment",
            )
            self.event_sink("intent_patch_applied", {**self._event_payload(), **impact})
        shared.refresh_hash()

    def request_patch_confirmation(
        self, intent: WorkflowIntent, impact: Dict[str, Any]
    ) -> AlignmentOutcome:
        """Pause execution when a mid-workflow change invalidates prior work."""
        shared = self.state.intent
        if shared.status == "idle":
            shared = SharedIntent.new(intent.raw_text)
            self.state.intent = shared
        self._project(shared, intent)
        shared.revision += 1
        shared.record_patch(
            changed_fields=list(impact.get("changed_fields") or []),
            scope=str(impact.get("scope") or "downstream"),
            source="user_mid_workflow",
            note="Execution paused before applying an intent revision",
        )
        self.event_sink("intent_patch_proposed", {**self._event_payload(), **impact})
        return self._ask_confirmation()

    def _start(self, text: str) -> AlignmentOutcome:
        intent = self.engine.classify(text, self.state)
        intent = self.engine._reconcile_intent(intent, text, self.state)
        shared = SharedIntent.new(text)
        self.state.intent = shared
        self._project(shared, intent)
        self.event_sink("intent_draft_created", self._event_payload())
        return self._evaluate(intent, had_user_questions=False)

    def _answer_field(self, text: str, *, source: str) -> AlignmentOutcome:
        interaction = dict(self.state.intent.interaction or {})
        field = str(interaction.get("field") or "")
        if not field:
            return self._merge_free_text(text)
        parsed = self.engine.classify(text, self.state).slots.get(field)
        value = parsed if parsed not in (None, "", []) else self._coerce(field, text)
        return self._apply_field(field, value, source=source)

    def _apply_field(self, field: str, value: Any, *, source: str) -> AlignmentOutcome:
        shared = self.state.intent
        if not field or value in (None, "", []):
            return AlignmentOutcome(True, message=f"{field or '该字段'} 不能为空。")
        before = shared.parameters.get(field)
        shared.parameters[field] = value
        shared.parameter_sources[field] = source
        if field == "duration_seconds":
            shared.parameters["max_duration_seconds"] = value
            shared.parameter_sources["max_duration_seconds"] = source
        if field == "success_conditions" and not isinstance(value, list):
            shared.parameters[field] = [str(value)]
        if field == "carrier_frequency" and str(shared.parameters.get("protocol") or "").lower() == "ble":
            channel_map = {2402000000.0: 37, 2426000000.0: 38, 2480000000.0: 39}
            try:
                frequency = float(value)
            except (TypeError, ValueError):
                frequency = 0.0
            if frequency in channel_map:
                shared.parameters["advertising_channels"] = [channel_map[frequency]]
                shared.parameter_sources["advertising_channels"] = "derived"
        shared.revision += 1
        shared.record_patch(
            changed_fields=[field], scope="intent_only", source=source,
            note=f"{before!r} -> {value!r}",
        )
        self.event_sink(
            "interaction_answered",
            {**self._event_payload(), "field": field, "source": source},
        )
        intent = self._to_workflow_intent(shared)
        return self._evaluate(intent, had_user_questions=True)

    def _merge_free_text(self, text: str) -> AlignmentOutcome:
        shared = self.state.intent
        update = self.engine.classify(text, self.state)
        changed = []
        for key, value in update.slots.items():
            if value in (None, "", []) or shared.parameters.get(key) == value:
                continue
            shared.parameters[key] = value
            shared.parameter_sources[key] = "user_revision"
            changed.append(key)
        if not changed:
            return self._request_revision(
                "请直接说明要改的参数，例如“设备改为 PlutoSDR”或“名称改为 demo”。"
            )
        shared.raw_text = (shared.raw_text + "\n" + str(text or "")).strip()
        shared.revision += 1
        shared.record_patch(
            changed_fields=changed, scope="intent_only", source="user_revision"
        )
        return self._evaluate(self._to_workflow_intent(shared), had_user_questions=True)

    def _evaluate(
        self, intent: WorkflowIntent, *, had_user_questions: bool
    ) -> AlignmentOutcome:
        intent.missing_slots = list(dict.fromkeys(
            list(self.engine._missing_slots(
                intent.task_type, intent.slots, self.state, intent.capabilities
            ))
            + missing_required_fields(
                capabilities=intent.capabilities, slots=intent.slots,
                slot_sources=intent.slot_sources,
            )
        ))
        intent.validation_errors = self.engine._validate_slots(intent.slots)
        project_intent_ir(intent)
        shared = self.state.intent
        self._project(shared, intent)
        if intent.validation_errors:
            field = str(intent.validation_errors[0]).removesuffix("_invalid")
            if field == "carrier_frequency_out_of_device_range":
                field = "carrier_frequency"
            if field == "modulation_incompatible_with_ble":
                field = "modulation"
            return self._ask(field, validation_error=intent.validation_errors[0])
        if intent.missing_slots:
            return self._ask(intent.missing_slots[0])
        if had_user_questions or bool(shared.patch_history):
            return self._ask_confirmation()
        shared.status = "confirmed"
        shared.confirmed_at = time.time()
        shared.interaction = {}
        shared.refresh_hash()
        self.event_sink("intent_confirmed", self._event_payload())
        return AlignmentOutcome(False, intent=self._to_workflow_intent(shared))

    def _ask(self, field: str, *, validation_error: str = "") -> AlignmentOutcome:
        shared = self.state.intent
        question = question_for(field)
        interaction = {
            "action": "intent_alignment",
            "kind": "ask_user_question",
            "purpose": "intent_alignment",
            "interaction_id": f"interaction-{uuid.uuid4().hex[:10]}",
            "base_intent_revision": shared.revision,
            "field": field,
            "prompt": question["prompt"],
            "reason": question["prompt"],
            "choices": list(question.get("choices") or []),
            "allow_custom": bool(question.get("allow_custom", True)),
            "validation_error": validation_error,
            "can_confirm": True,
            "approved": False,
        }
        shared.status = "awaiting_input"
        shared.interaction = interaction
        shared.refresh_hash()
        self.event_sink("interaction_requested", {**self._event_payload(), **interaction})
        return AlignmentOutcome(True, message=question["prompt"])

    def _ask_confirmation(self) -> AlignmentOutcome:
        shared = self.state.intent
        summary = self._summary(shared)
        interaction = {
            "action": "intent_alignment",
            "kind": "intent_confirmation",
            "purpose": "intent_confirmation",
            "interaction_id": f"interaction-{uuid.uuid4().hex[:10]}",
            "base_intent_revision": shared.revision,
            "prompt": "请确认对齐后的意图；确认后才建立 Workflow。",
            "reason": "请确认对齐后的意图；确认后才建立 Workflow。",
            "summary": summary,
            "choices": [
                {"id": "approved", "label": "确认并建立 Workflow", "value": "approved"},
                {"id": "revise", "label": "继续修改", "value": "revise"}
            ],
            "allow_custom": False,
            "can_confirm": True,
            "approved": False,
        }
        shared.status = "awaiting_confirmation"
        shared.interaction = interaction
        shared.refresh_hash()
        self.event_sink("interaction_requested", {**self._event_payload(), **interaction})
        return AlignmentOutcome(True, message=interaction["prompt"] + "\n" + summary)

    def _request_revision(self, message: str = "请说明需要修改的字段和值。") -> AlignmentOutcome:
        shared = self.state.intent
        shared.status = "awaiting_confirmation"
        shared.interaction = {
            **dict(shared.interaction or {}),
            "reason": message,
            "prompt": message,
        }
        return AlignmentOutcome(True, message=message)

    def _confirm(self) -> AlignmentOutcome:
        shared = self.state.intent
        shared.status = "confirmed"
        shared.confirmed_at = time.time()
        shared.interaction = {}
        shared.refresh_hash()
        self.event_sink("intent_confirmed", self._event_payload())
        return AlignmentOutcome(False, intent=self._to_workflow_intent(shared))

    @staticmethod
    def _project(shared: SharedIntent, intent: WorkflowIntent) -> None:
        shared.raw_text = intent.raw_text or shared.raw_text
        shared.task_type = intent.task_type
        shared.capabilities = list(intent.capabilities)
        shared.parameters = dict(intent.slots)
        shared.parameter_sources = dict(intent.slot_sources)
        shared.assumptions = [
            {"field": key, "value": intent.slots.get(key), "source": source}
            for key, source in intent.slot_sources.items()
            if source not in {"user", "user_choice", "user_text", "user_revision"}
            and intent.slots.get(key) not in (None, "", [])
        ]
        shared.goals = list(intent.goals)
        shared.constraints = dict(intent.constraints)
        shared.success_criteria = list(
            intent.slots.get("success_conditions") or intent.evidence_requirements or []
        )
        shared.missing_fields = list(intent.missing_slots)
        shared.validation_errors = list(intent.validation_errors)
        shared.intent_ir = {
            "goals": list(intent.goals),
            "requested_operations": list(intent.requested_operations),
            "desired_artifacts": list(intent.desired_artifacts),
            "evidence_requirements": list(intent.evidence_requirements),
            "forbidden_effects": list(intent.forbidden_effects),
            "decision_boundaries": list(intent.decision_boundaries),
            "stop_conditions": list(intent.stop_conditions),
            "entities": dict(intent.entities),
            "context": dict(intent.context),
        }
        shared.refresh_hash()

    @staticmethod
    def _to_workflow_intent(shared: SharedIntent) -> WorkflowIntent:
        ir = dict(shared.intent_ir or {})
        intent = WorkflowIntent(
            raw_text=shared.raw_text,
            turn_relation="new_task",
            task_type=shared.task_type or "END_TO_END_SIM",
            confidence=0.95,
            slots=dict(shared.parameters),
            missing_slots=list(shared.missing_fields),
            capabilities=list(shared.capabilities),
            slot_sources=dict(shared.parameter_sources),
            context={
                **dict(ir.get("context") or {}),
                "shared_intent": {
                    "intent_id": shared.intent_id,
                    "revision": shared.revision,
                    "semantic_hash": shared.semantic_hash,
                },
            },
            validation_errors=list(shared.validation_errors),
            goals=list(shared.goals),
            constraints=dict(shared.constraints),
            requested_operations=list(ir.get("requested_operations") or []),
            desired_artifacts=list(ir.get("desired_artifacts") or []),
            evidence_requirements=list(ir.get("evidence_requirements") or []),
            forbidden_effects=list(ir.get("forbidden_effects") or []),
            decision_boundaries=list(ir.get("decision_boundaries") or []),
            stop_conditions=list(ir.get("stop_conditions") or []),
            entities=dict(ir.get("entities") or {}),
        )
        project_intent_ir(intent)
        return intent

    def _event_payload(self) -> Dict[str, Any]:
        shared = self.state.intent
        return {
            "intent_id": shared.intent_id,
            "intent_revision": shared.revision,
            "intent_status": shared.status,
            "intent_hash": shared.semantic_hash,
            "task_type": shared.task_type,
        }

    @staticmethod
    def _summary(shared: SharedIntent) -> str:
        visible = [
            f"{key}={value}"
            for key, value in shared.parameters.items()
            if value not in (None, "", [])
            and key in {
                "protocol", "modulation", "direction", "hardware", "local_name",
                "carrier_frequency", "sample_rate", "bandwidth", "ebn0_db",
                "duration_seconds", "operation", "signal_source_scope",
            }
        ]
        return "意图: {}；{}".format(shared.task_type or "未分类", "，".join(visible))

    @staticmethod
    def _coerce(field: str, value: Any) -> Any:
        text = str(value or "").strip()
        if field in {"carrier_frequency", "sample_rate"}:
            match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([gmk]?)", text, re.I)
            if not match:
                return text
            scale = {"g": 1e9, "m": 1e6, "k": 1e3, "": 1.0}
            return float(match.group(1)) * scale[match.group(2).lower()]
        if field == "ebn0_db":
            match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
            return float(match.group(0)) if match else text
        if field == "duration_seconds":
            match = re.search(r"\d+(?:\.\d+)?", text)
            return float(match.group(0)) if match else text
        if field == "success_conditions":
            return [text] if text else []
        if field == "advertising_channels":
            match = re.search(r"\d+", text)
            return [int(match.group(0))] if match else []
        if field == "current_project":
            return "current_canvas"
        return text
