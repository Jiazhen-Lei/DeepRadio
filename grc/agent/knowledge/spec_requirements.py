"""Capability-based requirement references for intent alignment."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, Iterable, List


_REFERENCE_PATH = os.path.join(
    os.path.dirname(__file__), "specs", "requirements.json"
)
_PROFILES_PATH = os.path.join(
    os.path.dirname(__file__), "specs", "profiles.json"
)

_EXPLICIT_SOURCES = frozenset({
    "user", "user_choice", "user_text", "user_revision", "llm",
})
_DEFAULT_SOURCES = frozenset({
    "default", "protocol_default", "safety_default",
    "safe_preview_default", "derived", "rules", "",
})

# Slot-key synonyms merged onto canonical keys before rendering so the card
# never shows two rows for the same fact (e.g. `device` + `hardware`).  Keep
# in sync with workflow/engine.py::_SLOT_ALIASES.
_SLOT_ALIAS_CANON = {
    "device": "hardware",
    "sdr": "hardware",
    "radio": "hardware",
}


@lru_cache(maxsize=1)
def load_requirements() -> Dict[str, Any]:
    with open(_REFERENCE_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("fields"), dict) or not isinstance(data.get("rules"), list):
        raise ValueError("requirements.json is missing fields/rules")
    return data


@lru_cache(maxsize=1)
def load_profiles() -> Dict[str, Any]:
    with open(_PROFILES_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("profiles"), list):
        raise ValueError("profiles.json is missing profiles")
    return data


def required_fields(
    *, capabilities: Iterable[str], slots: Dict[str, Any]
) -> List[str]:
    capabilities = set(capabilities or [])
    required: List[str] = []
    for rule in load_requirements().get("rules") or []:
        any_caps = set(rule.get("when_any_capability") or [])
        slot_conditions = dict(rule.get("when_slots") or {})
        if any_caps and not capabilities.intersection(any_caps):
            continue
        if slot_conditions and any(slots.get(key) != value for key, value in slot_conditions.items()):
            continue
        for field in rule.get("required") or []:
            if field not in required:
                required.append(str(field))
    return required


def missing_required_fields(
    *, capabilities: Iterable[str], slots: Dict[str, Any],
    slot_sources: Dict[str, str] | None = None,
) -> List[str]:
    capabilities = set(capabilities or [])
    sources = dict(slot_sources or {})
    missing: List[str] = []
    for rule in load_requirements().get("rules") or []:
        any_caps = set(rule.get("when_any_capability") or [])
        slot_conditions = dict(rule.get("when_slots") or {})
        if any_caps and not capabilities.intersection(any_caps):
            continue
        if slot_conditions and any(
            slots.get(key) != value for key, value in slot_conditions.items()
        ):
            continue
        explicit = set(rule.get("require_explicit_sources") or [])
        for name in rule.get("required") or []:
            absent = slots.get(name) in (None, "", [])
            default_source = sources.get(name, "") in _DEFAULT_SOURCES
            if (absent or (name in explicit and default_source)) and name not in missing:
                missing.append(str(name))
    return missing


def question_for(field: str) -> Dict[str, Any]:
    item = dict((load_requirements().get("fields") or {}).get(field) or {})
    item.setdefault("prompt", f"Please provide {field}.")
    item.setdefault("choices", [])
    item.setdefault("allow_custom", True)
    item["field"] = field
    return item


def resolve_specification(
    *,
    task_type: str,
    capabilities: Iterable[str],
    slots: Dict[str, Any],
    slot_sources: Dict[str, str],
    missing_fields: Iterable[str],
    validation_errors: Iterable[str],
    goals: Iterable[str] = (),
    raw_text: str = "",
):
    """Compose a RadioSpecification from generic/profile overlays.

    The LLM may extract values, but requirement/visibility/provenance remain a
    deterministic host decision.  This keeps examples out of the coordinator.
    """
    from ..state.intent_state import RadioSpecification, SpecificationField

    caps = set(capabilities or [])
    values = dict(slots or {})
    sources = dict(slot_sources or {})
    # Merge slot synonyms onto canonical keys before any required/mentioned
    # computation: one fact must render as one field with one source.
    for alias, canonical in _SLOT_ALIAS_CANON.items():
        if alias not in values:
            continue
        alias_value = values.pop(alias)
        alias_source = sources.pop(alias, "")
        if values.get(canonical) in (None, "", []) and alias_value not in (
            None, "", [],
        ):
            values[canonical] = alias_value
            if canonical not in sources:
                sources[canonical] = alias_source
    missing = list(dict.fromkeys(str(item) for item in missing_fields or [] if item))
    active_profiles = []
    profile_required: List[str] = []
    optional: List[str] = []
    derived_display: List[str] = []
    for profile in load_profiles().get("profiles") or []:
        if not _profile_matches(profile, caps, values, task_type):
            continue
        active_profiles.append(str(profile.get("id") or "profile"))
        _extend_unique(profile_required, profile.get("required") or [])
        _extend_unique(optional, profile.get("optional") or [])
        _extend_unique(derived_display, profile.get("derived_display") or [])

    required = required_fields(capabilities=caps, slots=values)
    _extend_unique(required, profile_required)
    _extend_unique(required, missing)
    # Only values explicitly expressed by the user count as mentioned.  A
    # protocol default is not evidence that the user requested that field.
    mentioned = [
        key for key, value in values.items()
        if value not in (None, "", []) and sources.get(key, "") in _EXPLICIT_SOURCES
    ]
    visible = []
    if list(goals or []) or raw_text:
        visible.append("goal")
    _extend_unique(visible, required)
    _extend_unique(visible, mentioned)
    _extend_unique(
        visible,
        [key for key in derived_display if values.get(key) not in (None, "", [])],
    )

    definitions = load_requirements().get("fields") or {}
    fields = []
    goal_text = "; ".join(str(item) for item in goals or [] if item)
    if not goal_text:
        goal_text = str(raw_text or "").strip()
    for key in visible:
        meta = dict(definitions.get(key) or {})
        value = goal_text if key == "goal" else values.get(key)
        source = "user" if key == "goal" and value else str(sources.get(key) or "")
        requirement = (
            "required" if key in required
            else "optional_added"
            if key in optional and source in {"user_revision", "user_choice"}
            else "mentioned" if key in mentioned or key == "goal"
            else "derived"
        )
        unresolved = value in (None, "", [])
        if unresolved:
            source = "unresolved"
        confirmed = key not in missing and not unresolved and (
            source in _EXPLICIT_SOURCES
            or (key not in missing and source not in {"safety_default", "safe_preview_default"})
        )
        # Per-row explanatory sentences were removed from the user-facing card;
        # the requirement/source tags carry the same information compactly.
        reason = ""
        fields.append(SpecificationField(
            key=key,
            value=value,
            label=str(meta.get("label") or key.replace("_", " ").title()),
            requirement=requirement,
            source=source,
            locked=True,
            confirmed=confirmed,
            reason=reason,
            depends_on=_field_dependencies(key),
        ))

    blocking_questions = [
        _question_payload(key) for key in missing
    ]
    optional_prompts = []
    for key in optional:
        if key in visible:
            continue
        meta = dict(definitions.get(key) or {})
        optional_prompts.append({
            "field": key,
            "label": str(meta.get("label") or key.replace("_", " ").title()),
            "teaching": str(meta.get("teaching") or meta.get("prompt") or ""),
        })
    return RadioSpecification(
        profile_refs=active_profiles,
        fields=fields,
        blocking_questions=blocking_questions,
        optional_prompts=optional_prompts,
        validation_errors=list(validation_errors or []),
    )


def missing_profile_fields(
    *, task_type: str, capabilities: Iterable[str], slots: Dict[str, Any],
    slot_sources: Dict[str, str] | None = None,
) -> List[str]:
    """Return blocking fields contributed by composed profiles."""
    caps = set(capabilities or [])
    values = dict(slots or {})
    sources = dict(slot_sources or {})
    missing: List[str] = []
    for profile in load_profiles().get("profiles") or []:
        if not _profile_matches(profile, caps, values, task_type):
            continue
        explicit = set(profile.get("require_explicit_sources") or [])
        for field in profile.get("required") or []:
            absent = values.get(field) in (None, "", [])
            unconfirmed_default = field in explicit and sources.get(field, "") in _DEFAULT_SOURCES
            if (absent or unconfirmed_default) and field not in missing:
                missing.append(str(field))
    return missing


def combined_question(fields: Iterable[str]) -> str:
    """Ask all currently blocking fields in one natural-language turn."""
    questions = [_question_payload(str(field)) for field in fields if field]
    if not questions:
        return ""
    noun = "detail" if len(questions) == 1 else "details"
    lines = [f"🧭 I need {len(questions)} more {noun} before I create the workflow:"]
    for index, item in enumerate(questions, 1):
        label = str(question_for(item["field"]).get("label") or item["field"])
        line = f"{index}. {label}: {item['prompt']}"
        if item["suggestions"]:
            line += " Suggested: " + ", ".join(item["suggestions"][:4])
        lines.append(line)
    lines.append("Reply in one natural-language message; I will keep only unanswered items open.")
    return "\n".join(lines)


def _question_payload(field: str) -> Dict[str, Any]:
    item = question_for(field)
    return {
        "field": field,
        "prompt": str(item.get("prompt") or f"Please provide {field}."),
        "suggestions": [
            str(choice.get("label") or choice.get("value") or "")
            for choice in item.get("choices") or []
            if isinstance(choice, dict)
        ],
    }


def _profile_matches(
    profile: Dict[str, Any], capabilities: set[str], slots: Dict[str, Any], task_type: str
) -> bool:
    if profile.get("always"):
        return True
    any_caps = set(profile.get("when_any_capability") or [])
    if any_caps and not capabilities.intersection(any_caps):
        return False
    task_types = set(profile.get("when_task_type") or [])
    if task_types and task_type not in task_types:
        return False
    for key, expected in dict(profile.get("when_slots") or {}).items():
        if slots.get(key) != expected:
            return False
    return bool(any_caps or task_types or profile.get("when_slots"))


def _extend_unique(target: List[str], values: Iterable[str]) -> None:
    for value in values or []:
        name = str(value)
        if name and name not in target:
            target.append(name)


def _field_dependencies(key: str) -> List[str]:
    return {
        "carrier_frequency": ["protocol", "advertising_channels", "wifi_channel"],
        "modulation": ["protocol", "wifi_standard"],
        "sample_rate": ["protocol", "bandwidth"],
        "local_name": ["protocol", "ble_mode"],
        "ssid": ["protocol", "wifi_role"],
    }.get(key, [])
