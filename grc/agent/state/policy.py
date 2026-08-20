"""Policy gateway for state and flowgraph mutations."""

from __future__ import annotations

from typing import Any, Dict

from .shared_state import Coordination

ALLOW = "ALLOW"
PROPOSE = "PROPOSE"
DENY = "DENY"
CONFIRM = "CONFIRM"


def gate(action: Dict[str, Any], coordination: Coordination) -> str:
    if action.get("target") in coordination.locked_constraints:
        return DENY
    if action.get("domain") == "hardware":
        return CONFIRM
    if action.get("scope") == "multi_block_change":
        return PROPOSE
    return ALLOW
