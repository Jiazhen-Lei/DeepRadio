"""Deterministic impact analysis for user-authored IntentPatch objects."""

from __future__ import annotations

from typing import Any, Dict


PRESENTATION_FIELDS = frozenset({"user_profile", "explanation_level", "language_style"})
SEMANTIC_FIELDS = frozenset(
    {"protocol", "modulation", "direction", "hardware", "operation", "signal_source_scope"}
)
ARTIFACT_FIELDS = frozenset(
    {
        "local_name", "payload", "carrier_frequency", "sample_rate", "bandwidth",
        "symbol_rate", "advertising_channels", "recipe", "ebn0_db",
    }
)
SAFETY_FIELDS = frozenset(
    {"tx_gain", "tx_attenuation", "duration_seconds", "max_duration_seconds"}
)


def analyze_intent_patch(
    before: Dict[str, Any],
    after: Dict[str, Any],
    *,
    runtime_active: bool = False,
) -> Dict[str, Any]:
    """Return scope without asking an LLM to guess state invalidation.

    The result is capability/field based and deliberately independent of the
    seven catalog labels, so adding a new task does not require new branches.
    """
    changed = sorted(
        key for key in set(before or {}) | set(after or {})
        if (before or {}).get(key) != (after or {}).get(key)
    )
    fields = set(changed)
    if not changed:
        scope = "none"
    elif fields <= PRESENTATION_FIELDS:
        scope = "presentation_only"
    elif fields & SEMANTIC_FIELDS:
        scope = "supersede"
    elif fields & (ARTIFACT_FIELDS | SAFETY_FIELDS):
        scope = "downstream"
    else:
        scope = "future_only"
    return {
        "changed_fields": changed,
        "scope": scope,
        "requires_stop": bool(runtime_active and fields - PRESENTATION_FIELDS),
        "invalidate_artifacts": bool(fields & (SEMANTIC_FIELDS | ARTIFACT_FIELDS)),
        "requires_reconfirmation": bool(fields & (SEMANTIC_FIELDS | ARTIFACT_FIELDS | SAFETY_FIELDS)),
    }
