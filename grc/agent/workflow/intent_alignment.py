"""Alignment Gate before Workflow instantiation.

This module deliberately does not execute stages.  It turns an underspecified
request into a versioned SharedIntent, asks for all currently blocking fields,
and only hands a confirmed WorkflowIntent to the existing WorkflowEngine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..knowledge.spec_requirements import (
    combined_question,
    missing_profile_fields,
    missing_required_fields,
    question_for,
    resolve_specification,
)
from ..state import SharedIntent
from .planning import project_intent_ir
from .schema import WorkflowIntent
from .revision import analyze_intent_patch


_APPROVE = frozenset({
    "确认", "确认意图", "同意", "正确", "继续",
    "approve", "confirm", "confirmed", "yes", "ok",
})
_REVISE = frozenset({"修改", "不对", "返回修改", "重新填写", "revise", "reject"})


@dataclass
class AlignmentOutcome:
    pending: bool
    intent: Optional[WorkflowIntent] = None
    message: str = ""
    kind: str = ""


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
                semantic = self._alignment_turn_from_llm(text, [])
                if semantic is None:
                    # The fixed vocabulary is retained strictly for isolated
                    # deterministic unit tests. Production never falls back
                    # to it when semantic understanding is unavailable.
                    return self._consume_confirmation_test_bypass(text)
                action = str(semantic.get("intent_action") or "param_update")
                if action == "confirm":
                    return self._confirm()
                if action == "revise":
                    return self._request_revision()
                if action == "optional_fields":
                    return self._describe_optional_fields()
                if action == "teach":
                    return self._teach_specification()
                if action == "question":
                    return self._answer_question(text)
                return self._merge_free_text(
                    text,
                    semantic_updates=dict(semantic.get("updates") or {}),
                    semantic_ready=True,
                )
            from .engine import _looks_like_question

            semantic = self._alignment_turn_from_llm(
                text, list(shared.missing_fields or [])
            )
            if (
                semantic is not None
                and str(semantic.get("intent_action") or "") == "question"
            ) or (semantic is None and _looks_like_question(text)):
                return self._answer_question(text)
            if (
                semantic is not None
                and str(semantic.get("intent_action") or "") == "confirm"
                and not dict(semantic.get("updates") or {})
            ):
                # The user replied with a confirmation while a field was
                # pending.  Dropping that turn re-asked the same question
                # forever (observed: three consecutive alignment turns with
                # intent_action=confirm, updated_fields=[], no progress).
                return self._confirm_requested_fields()
            return self._answer_fields(text, source="user_text", semantic=semantic)
        return self._start(text)

    def _consume_confirmation_test_bypass(self, text: str) -> AlignmentOutcome:
        """Legacy deterministic router, reachable only with the test bypass."""
        normalized = str(text or "").strip().lower().rstrip(".,!?")
        if normalized in _APPROVE:
            return self._confirm()
        if normalized in _REVISE:
            return self._request_revision()
        if normalized in {
            "可选字段", "有哪些可选字段", "还能添加什么字段",
            "optional fields", "what optional fields are available",
        }:
            return self._describe_optional_fields()
        if any(word in normalized for word in (
            "介绍这些参数", "解释这些参数", "参数教学",
            "explain these parameters", "teach me these parameters",
        )):
            return self._teach_specification()
        from .engine import _looks_like_question

        if _looks_like_question(text):
            return self._answer_question(text)
        return self._merge_free_text(text)

    def consume_response(self, response: Dict[str, Any]) -> AlignmentOutcome:
        shared = self.state.intent
        interaction = dict(shared.interaction or {})
        if not interaction or str(response.get("interaction_id") or "") != str(
            interaction.get("interaction_id") or ""
        ):
            return AlignmentOutcome(True, message="The intent interaction has changed. Refresh and try again.")
        try:
            base_revision = int(response.get("base_intent_revision") or 0)
        except (TypeError, ValueError):
            base_revision = 0
        if base_revision != shared.revision:
            return AlignmentOutcome(True, message="The intent revision has changed. Answer the latest question.")
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
        """Apply a legacy structured client update through the canonical writer.

        The GTK main path uses natural language.  This remains for resumed
        sessions and non-GUI API clients; it never writes exported JSON.
        """
        shared = self.state.intent
        interaction = dict(shared.interaction or {})
        interaction_id = str(response.get("interaction_id") or "")
        if interaction and interaction_id != str(interaction.get("interaction_id") or ""):
            return AlignmentOutcome(True, message="The intent interaction has changed. Refresh and try again.")
        if str(response.get("intent_id") or shared.intent_id) != shared.intent_id:
            return AlignmentOutcome(True, message="The intent has changed. Revise the latest specification.")
        try:
            base_revision = int(response.get("base_intent_revision") or 0)
        except (TypeError, ValueError):
            base_revision = 0
        if base_revision != shared.revision:
            return AlignmentOutcome(True, message="The intent revision has changed. Revise the latest specification.")
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
            source="structured_specification_update",
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
        semantics = dict((intent.context or {}).get("turn_semantics") or {})
        decision = str(semantics.get("confirmation_decision") or "none")
        if decision in {"approved", "rejected"}:
            # This decision came from the LLM, not a vocabulary match.  With no
            # active checkpoint there is nothing safe to approve/reject, so do
            # not reinterpret the short control turn as a new underspecified
            # radio task and overwrite the existing SharedIntent.
            self.event_sink("orphan_confirmation_rejected", {
                "confirmation_decision": decision,
                "state_preserved": True,
            })
            return AlignmentOutcome(
                True,
                message=(
                    "There is no pending confirmation in the current workflow. "
                    "State the new radio goal if you want to begin another task."
                ),
            )
        shared = SharedIntent.new(text)
        self.state.intent = shared
        self._project(shared, intent)
        self.event_sink("intent_draft_created", self._event_payload())
        return self._evaluate(intent, had_user_questions=False)

    def _answer_fields(
        self,
        text: str,
        *,
        source: str,
        semantic: Optional[Dict[str, Any]] = None,
    ) -> AlignmentOutcome:
        """Merge every reliably extracted answer from one natural-language turn."""
        interaction = dict(self.state.intent.interaction or {})
        fields = list(interaction.get("fields") or [])
        field = str(interaction.get("field") or "")
        if field and field not in fields:
            fields.insert(0, field)
        if not fields:
            return self._merge_free_text(text)
        semantic_updates = (
            dict(semantic.get("updates") or {})
            if semantic is not None
            else self._alignment_updates_from_llm(text, fields)
        )
        if semantic_updates is None:
            # Deterministic unit-test bypass only. Production never reaches
            # this branch, so a model outage cannot become a regex guess.
            update = self.engine.classify(text, self.state)
            update = self.engine._reconcile_intent(update, text, self.state)
            update_slots = dict(update.slots or {})
            update_sources = dict(update.slot_sources or {})
        else:
            update_slots = semantic_updates
            update_sources = {key: "llm" for key in semantic_updates}
        shared = self.state.intent
        changed = []
        before = dict(shared.parameters or {})
        explicit_sources = {"user", "llm", "user_text", "user_choice", "user_revision"}
        # A reply to a concrete missing-field question must not accidentally
        # reinterpret the already-established task.  For example, "an
        # independent receiver observes the signal" is a success condition,
        # not a request to turn a TX workflow into RX.  These identity fields
        # may still be supplied when they are themselves being requested.
        stable_identity_fields = {"protocol", "direction", "operation", "hardware"}
        for key, value in update_slots.items():
            if value in (None, "", []):
                continue
            if (
                key in stable_identity_fields
                and key not in fields
                and shared.parameters.get(key) not in (None, "", [])
            ):
                continue
            value_source = str(update_sources.get(key) or "")
            # Protocol defaults may accompany an explicitly named protocol;
            # retain them as defaults, but never claim the user supplied them.
            accepted_default = bool(
                update_slots.get("protocol")
                and update_sources.get("protocol") in explicit_sources
                and value_source in {"protocol_default", "derived"}
            )
            if value_source not in explicit_sources and not accepted_default:
                continue
            if shared.parameters.get(key) == value:
                # Confirming a proposed/default value is still a semantic
                # change: the value stays equal, but its authority becomes
                # the user's explicit decision.
                if (
                    value_source in explicit_sources
                    and shared.parameter_sources.get(key) not in explicit_sources
                ):
                    shared.parameter_sources[key] = source
                    changed.append(key)
                continue
            shared.parameters[key] = value
            shared.parameter_sources[key] = (
                source if value_source in explicit_sources else value_source
            )
            changed.append(key)
        if semantic_updates is None and not changed and len(fields) == 1:
            value = self._coerce(fields[0], text)
            if value not in (None, "", []):
                shared.parameters[fields[0]] = value
                shared.parameter_sources[fields[0]] = source
                changed.append(fields[0])
        if semantic_updates is None:
            numbered = [
                item.strip(" .")
                for item in re.findall(
                    r"(?:^|\n|;|\s)\d+\.\s+(.+?)(?=(?:\s+\d+\.\s+)|$)",
                    str(text or "").strip(),
                )
                if item.strip()
            ]
            for index, remaining in enumerate(fields):
                if remaining in changed or index >= len(numbered):
                    continue
                value = self._coerce(remaining, numbered[index])
                if value in (None, "", []):
                    continue
                shared.parameters[remaining] = value
                shared.parameter_sources[remaining] = source
                changed.append(remaining)
        if "duration_seconds" in changed:
            shared.parameters["max_duration_seconds"] = shared.parameters["duration_seconds"]
            shared.parameter_sources["max_duration_seconds"] = source
        if not changed:
            return AlignmentOutcome(
                True,
                message=(combined_question(fields) or "No usable parameter was recognized. Please rephrase your answer."),
            )
        shared.raw_text = (shared.raw_text + "\n" + str(text or "")).strip()
        shared.revision += 1
        shared.record_patch(
            changed_fields=changed,
            scope="intent_only",
            source=source,
            note="{} -> {}".format(before, shared.parameters),
        )
        self.event_sink(
            "interaction_answered",
            {**self._event_payload(), "fields": changed, "source": source},
        )
        return self._evaluate(
            self._to_workflow_intent(shared), had_user_questions=True
        )

    def _confirm_requested_fields(self) -> AlignmentOutcome:
        """Resolve an explicit confirmation received while fields are pending.

        A pending field whose specification offers exactly one non-custom
        choice (for example ``current_project``) is a default the user can
        only accept or abandon; an explicit confirmation therefore accepts
        it.  Fields without such a sole choice stay open and the coordinator
        keeps waiting for a real answer instead of silently inventing one.
        """
        from ..knowledge.spec_requirements import load_requirements

        shared = self.state.intent
        requirements = load_requirements().get("fields") or {}
        changed = []
        for key in list(shared.missing_fields or []):
            spec = requirements.get(key) or {}
            choices = list(spec.get("choices") or [])
            if len(choices) != 1 or spec.get("allow_custom"):
                continue
            value = choices[0].get("value")
            if value in (None, "", []):
                continue
            shared.parameters[key] = value
            shared.parameter_sources[key] = "user_choice"
            changed.append(key)
        if not changed:
            return self._evaluate(
                self._to_workflow_intent(shared), had_user_questions=True
            )
        shared.revision += 1
        shared.record_patch(
            changed_fields=changed,
            scope="intent_only",
            source="user_confirmation",
            note="accepted sole-choice defaults: {}".format(changed),
        )
        self.event_sink(
            "interaction_answered",
            {
                **self._event_payload(),
                "fields": changed,
                "source": "user_confirmation",
            },
        )
        return self._evaluate(
            self._to_workflow_intent(shared), had_user_questions=True
        )

    def _alignment_turn_from_llm(
        self, text: str, fields: list[str]
    ) -> Optional[Dict[str, Any]]:
        """Classify a follow-up action and extract parameter updates once.

        ``None`` means the deterministic test-only bypass is active. Any
        production configuration/request failure raises instead of returning
        a rule-derived approximation.
        """
        from ..llm import (
            SemanticUnderstandingError,
            chat,
            get_config,
            intent_test_bypass_enabled,
            is_configured,
            parse_json_object,
        )
        if not is_configured():
            if intent_test_bypass_enabled():
                return None
            self.event_sink("alignment_llm_failed", {
                **self._event_payload(), "reason": "not_configured",
            })
            raise SemanticUnderstandingError(
                "The language model is not configured. Your answer was not interpreted."
            )

        from ..knowledge.spec_requirements import load_requirements

        allowed = set((load_requirements().get("fields") or {}).keys())
        allowed.update({"max_duration_seconds", "deploy_permission", "hardware_access"})
        shared = self.state.intent
        payload = {
            "user_reply": str(text or ""),
            "requested_fields": list(fields),
            "established_intent": {
                "task_type": shared.task_type,
                "capabilities": list(shared.capabilities),
                "parameters": dict(shared.parameters),
                "parameter_sources": dict(shared.parameter_sources),
                "missing_fields": list(shared.missing_fields),
            },
            "allowed_update_fields": sorted(allowed),
        }
        request_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cfg = dict(get_config())
        try:
            semantic_timeout = float(
                os.environ.get("GRC_AGENT_SEMANTIC_TIMEOUT", "45")
            )
        except ValueError:
            semantic_timeout = 45.0
        cfg["timeout"] = max(5.0, min(float(cfg.get("timeout") or 45.0), semantic_timeout))
        try:
            retries = max(0, int(os.environ.get("GRC_AGENT_SEMANTIC_RETRIES", "1")))
        except ValueError:
            retries = 1
        prompt = (
            "You interpret a follow-up to an existing GNU Radio Radio Specification. "
            "Return one JSON object with 'intent_action' and 'updates'. intent_action "
            "must be exactly one of confirm, revise, optional_fields, teach, "
            "question, or param_update. Use confirm only when the user accepts the "
            "specification; revise when they reject it without yet supplying a "
            "concrete replacement; optional_fields when they ask what else can be "
            "added; teach when they ask for an explanation of the listed parameters; "
            "question when they ask a factual or how/why question that should not "
            "change the specification; otherwise use param_update. The established "
            "task identity is context, not something to reclassify. Extract every "
            "field explicitly answered or newly mentioned in this reply, including "
            "optional fields such as local_name. Interpret durations with units and "
            "put observable acceptance evidence in success_conditions as a string list. "
            "Do not invent values, defaults, capabilities, or task types. If a requested "
            "field is unanswered, omit it. Use only allowed_update_fields."
        )
        self.event_sink("alignment_llm_started", {
            **self._event_payload(), "request_hash": request_hash,
            "model": str(cfg.get("model") or ""), "requested_fields": list(fields),
        })
        started_at = time.perf_counter()
        last_error: Optional[Exception] = None
        parsed: Dict[str, Any] = {}
        for attempt in range(retries + 1):
            try:
                content = chat([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ], config=cfg)
                parsed = parse_json_object(content)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.event_sink("alignment_llm_retry", {
                    **self._event_payload(), "request_hash": request_hash,
                    "attempt": attempt + 1, "will_retry": attempt < retries,
                    "error_type": type(exc).__name__,
                })
        if last_error is not None:
            self.event_sink("alignment_llm_failed", {
                **self._event_payload(), "reason": "request_failed",
                "request_hash": request_hash,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "error_type": type(last_error).__name__,
            })
            raise SemanticUnderstandingError(
                "The language model did not understand this reply after retrying. "
                "The current Radio Specification is unchanged."
            ) from last_error

        raw_updates = parsed.get("updates")
        if not isinstance(raw_updates, dict):
            raise SemanticUnderstandingError(
                "The language model returned an invalid specification update. "
                "The current Radio Specification is unchanged."
            )
        updates: Dict[str, Any] = {}
        for key, raw in raw_updates.items():
            key = str(key or "")
            if key not in allowed or raw in (None, "", []):
                continue
            if key == "success_conditions":
                value = [str(item).strip() for item in raw] if isinstance(raw, list) else [str(raw).strip()]
                value = [item for item in value if item]
            elif key == "advertising_channels" and isinstance(raw, list):
                value = raw
            else:
                value = self._coerce(key, raw)
            if value not in (None, "", []):
                updates[key] = value
        action = str(parsed.get("intent_action") or "param_update").strip().lower()
        if action not in {
            "confirm", "revise", "optional_fields", "teach", "question",
            "param_update",
        }:
            raise SemanticUnderstandingError(
                "The language model returned an invalid alignment action. "
                "The current Radio Specification is unchanged."
            )
        self.event_sink("alignment_llm_succeeded", {
            **self._event_payload(), "request_hash": request_hash,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "intent_action": action, "updated_fields": sorted(updates),
        })
        return {"intent_action": action, "updates": updates}

    def _alignment_updates_from_llm(
        self, text: str, fields: list[str]
    ) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper for callers that only need field updates."""
        semantic = self._alignment_turn_from_llm(text, fields)
        if semantic is None:
            return None
        return dict(semantic.get("updates") or {})

    def _apply_field(self, field: str, value: Any, *, source: str) -> AlignmentOutcome:
        shared = self.state.intent
        if not field or value in (None, "", []):
            return AlignmentOutcome(True, message=f"{field or 'This field'} cannot be empty.")
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

    def _merge_free_text(
        self,
        text: str,
        *,
        semantic_updates: Optional[Dict[str, Any]] = None,
        semantic_ready: bool = False,
    ) -> AlignmentOutcome:
        shared = self.state.intent
        if not semantic_ready:
            semantic_updates = self._alignment_updates_from_llm(text, [])
        update_slots = (
            self.engine.classify(text, self.state).slots
            if semantic_updates is None else semantic_updates
        )
        changed = []
        for key, value in update_slots.items():
            if value in (None, "", []) or shared.parameters.get(key) == value:
                continue
            shared.parameters[key] = value
            shared.parameter_sources[key] = "user_revision"
            changed.append(key)
        if not changed:
            return self._request_revision(
                "State the parameter and its new value directly, for example, "
                "'change the device to PlutoSDR' or 'change the name to demo'."
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
            + missing_profile_fields(
                task_type=intent.task_type,
                capabilities=intent.capabilities,
                slots=intent.slots,
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
            return self._ask_missing(intent.missing_slots)
        # Hardware/deploy stakes must never be silently confirmed: the
        # specification card locks device identity and physical RF facts the
        # user has not seen yet.  (V6: a PlutoSDR TX plan reached
        # "confirmed" 0.7 ms after the draft with every spec field marked
        # confirmed and no user turn in between.)
        hardware_stakes = bool(
            set(intent.capabilities or [])
            & {"hardware_configure", "deploy", "hardware_runtime"}
        ) or str(
            (intent.slots or {}).get("operation") or ""
        ).lower() == "deploy"
        if (
            had_user_questions
            or bool(shared.patch_history)
            or hardware_stakes
        ):
            return self._ask_confirmation()
        shared.status = "confirmed"
        shared.confirmed_at = time.time()
        shared.interaction = {}
        shared.refresh_hash()
        self.event_sink("intent_confirmed", self._event_payload())
        return AlignmentOutcome(False, intent=self._to_workflow_intent(shared))

    def _ask(self, field: str, *, validation_error: str = "") -> AlignmentOutcome:
        return self._ask_missing([field], validation_error=validation_error)

    def _ask_missing(
        self, fields: list[str], *, validation_error: str = ""
    ) -> AlignmentOutcome:
        shared = self.state.intent
        fields = list(dict.fromkeys(str(item) for item in fields if item))
        question = question_for(fields[0]) if fields else question_for("goal")
        prompt = combined_question(fields) or str(question["prompt"])
        interaction = {
            "action": "intent_alignment",
            "kind": "ask_user_question",
            "purpose": "intent_alignment",
            "interaction_id": f"interaction-{uuid.uuid4().hex[:10]}",
            "base_intent_revision": shared.revision,
            "field": fields[0] if fields else "",
            "fields": fields,
            "questions": [question_for(field) for field in fields],
            "prompt": prompt,
            "reason": prompt,
            # Choices are retained only as suggestions for API compatibility;
            # the GUI main path answers through natural language.
            "choices": [],
            "allow_custom": True,
            "validation_error": validation_error,
            "can_confirm": True,
            "approved": False,
        }
        shared.status = "awaiting_input"
        shared.interaction = interaction
        shared.specification.blocking_questions = [
            {
                "field": field,
                "prompt": str(question_for(field).get("prompt") or ""),
                "suggestions": [
                    str(item.get("label") or item.get("value") or "")
                    for item in question_for(field).get("choices") or []
                    if isinstance(item, dict)
                ],
            }
            for field in fields
        ]
        shared.refresh_hash()
        self.event_sink("interaction_requested", {**self._event_payload(), **interaction})
        return AlignmentOutcome(True, message=prompt)

    def _ask_confirmation(self) -> AlignmentOutcome:
        shared = self.state.intent
        summary = self._summary(shared)
        interaction = {
            "action": "intent_alignment",
            "kind": "intent_confirmation",
            "purpose": "intent_confirmation",
            "interaction_id": f"interaction-{uuid.uuid4().hex[:10]}",
            "base_intent_revision": shared.revision,
            "prompt": (
                "✅ The Radio Specification is complete.\n\n"
                "- Confirm it to create the workflow\n"
                "- Or name a field to change\n"
                "- Or ask a question about these parameters"
            ),
            "reason": "Confirm, continue revising, add optional fields, or request parameter guidance.",
            "summary": summary,
            "choices": [
                {"id": "approved", "label": "Confirm and Create Workflow", "value": "approved"},
                {"id": "revise", "label": "Continue Revising", "value": "revise"}
            ],
            "allow_custom": False,
            "can_confirm": True,
            "approved": False,
        }
        shared.status = "awaiting_confirmation"
        shared.interaction = interaction
        shared.refresh_hash()
        self.event_sink("interaction_requested", {**self._event_payload(), **interaction})
        # The structured Specification card is the single public rendering of
        # resolved fields.  The chat bubble should contain only the next
        # action, not a second copy of the same parameter list.
        return AlignmentOutcome(True, message=interaction["prompt"])

    def _describe_optional_fields(self) -> AlignmentOutcome:
        optional = list(self.state.intent.specification.optional_prompts or [])
        if not optional:
            return AlignmentOutcome(True, message="The current profile has no additional recommended fields. You can confirm it now.")
        labels = ", ".join(
            str(item.get("label") or item.get("field") or "") for item in optional
        )
        return AlignmentOutcome(
            True,
            message="Optional fields include: {}. State the field and value to add, or reply 'confirm'.".format(labels),
        )

    def _teach_specification(self) -> AlignmentOutcome:
        fields = list(self.state.intent.specification.fields or [])
        lines = ["Current Radio Specification parameter guide:"]
        for item in fields:
            meta = question_for(item.key)
            teaching = str(meta.get("teaching") or meta.get("prompt") or item.reason)
            lines.append("- {}: {}".format(item.label or item.key, teaching))
        lines.append("This explanation does not confirm any parameters. Continue revising or reply 'confirm'.")
        return AlignmentOutcome(True, message="\n".join(lines))

    def _answer_question(self, text: str) -> AlignmentOutcome:
        from .narration import answer_question

        shared = self.state.intent
        context = {
            "specification": [
                {
                    "key": item.key,
                    "label": item.label,
                    "value": item.value,
                    "requirement": item.requirement,
                }
                for item in (shared.specification.fields or [])
            ],
            "missing_fields": list(shared.missing_fields or []),
            "status": shared.status,
        }
        message = answer_question(user_text=text, context=context)
        return AlignmentOutcome(True, message=message, kind="question")

    def _request_revision(self, message: str = "State the field and value you want to revise.") -> AlignmentOutcome:
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
        shared.specification = resolve_specification(
            task_type=intent.task_type,
            capabilities=intent.capabilities,
            slots=intent.slots,
            slot_sources=intent.slot_sources,
            missing_fields=intent.missing_slots,
            validation_errors=intent.validation_errors,
            goals=intent.goals,
            raw_text=shared.raw_text,
        )
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
        labels = {
            "protocol": "Protocol",
            "modulation": "Modulation",
            "direction": "Direction",
            "hardware": "Hardware",
            "local_name": "Local name",
            "carrier_frequency": "Carrier frequency",
            "sample_rate": "Sample rate",
            "bandwidth": "Bandwidth",
            "ebn0_db": "Eb/No",
            "duration_seconds": "Maximum duration",
            "operation": "Requested operation",
            "signal_source_scope": "Signal source",
        }
        visible = [
            f"{labels[key]}: {value}"
            for key, value in shared.parameters.items()
            if value not in (None, "", [])
            and key in labels
        ]
        return "Proposed radio specification: " + "; ".join(visible)

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
            labeled = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:seconds?|秒)\b", text, re.I
            )
            if labeled:
                return float(labeled.group(1))
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
