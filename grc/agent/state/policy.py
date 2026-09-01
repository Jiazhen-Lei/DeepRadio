"""Protect explicitly locked user constraints during project mutation."""

from __future__ import annotations

from typing import Any, Dict

from .shared_state import Coordination

ALLOW = "ALLOW"
PROPOSE = "PROPOSE"
DENY = "DENY"
CONFIRM = "CONFIRM"

def gate(action: Dict[str, Any], coordination: Coordination) -> str:
    target = str(action.get("target") or "")
    if target in coordination.locked_constraints:
        return DENY
    return ALLOW
