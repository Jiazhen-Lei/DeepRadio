"""Capability-based requirement references for intent alignment."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, Iterable, List


_REFERENCE_PATH = os.path.join(
    os.path.dirname(__file__), "specs", "requirements.json"
)


@lru_cache(maxsize=1)
def load_requirements() -> Dict[str, Any]:
    with open(_REFERENCE_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("fields"), dict) or not isinstance(data.get("rules"), list):
        raise ValueError("requirements.json 缺少 fields/rules")
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
    *, capabilities: Iterable[str], slots: Dict[str, Any]
) -> List[str]:
    return [
        name for name in required_fields(capabilities=capabilities, slots=slots)
        if slots.get(name) in (None, "", [])
    ]


def question_for(field: str) -> Dict[str, Any]:
    item = dict((load_requirements().get("fields") or {}).get(field) or {})
    item.setdefault("prompt", f"请补充 {field}。")
    item.setdefault("choices", [])
    item.setdefault("allow_custom", True)
    item["field"] = field
    return item
