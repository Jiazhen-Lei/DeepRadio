"""Policy gateway for state and flowgraph mutations."""

from __future__ import annotations

from typing import Any, Dict

from .shared_state import Coordination

ALLOW = "ALLOW"
PROPOSE = "PROPOSE"
DENY = "DENY"
CONFIRM = "CONFIRM"

#: 改这些参数等于换调制,必须走 recipe 级确认。
_MODULATION_PARAMS = frozenset({
    "modulation",
    "constellation",
    "const_points",
    "sym_map",
    "rot_sym",
})

#: 星座块上再拦一层,避免用 type/dims 把 BPSK 改成 QPSK。
_CONSTELLATION_BLOCK_PARAMS = frozenset({
    "type",
    "dims",
    "normalization",
    "const_points",
    "sym_map",
    "rot_sym",
    "precision",
})


def _is_modulation_change(action: Dict[str, Any]) -> bool:
    if action.get("changes_modulation"):
        return True
    target = str(action.get("target") or "")
    if target in _MODULATION_PARAMS:
        return True
    block_id = str(action.get("block_id") or "").lower()
    if "const" in block_id and target in _CONSTELLATION_BLOCK_PARAMS:
        return True
    return False


def gate(action: Dict[str, Any], coordination: Coordination) -> str:
    target = str(action.get("target") or "")
    if target in coordination.locked_constraints:
        return DENY
    if action.get("domain") == "hardware":
        return CONFIRM
    if action.get("scope") == "multi_block_change":
        return PROPOSE
    if _is_modulation_change(action):
        return PROPOSE
    return ALLOW
