"""Optional LLM short-horizon planner.

When no model is configured this module is a no-op.  Proposals are always
passed through the Plan Compiler before execution.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .plan_compiler import PlanNode, known_action_ids

logger = logging.getLogger(__name__)

_PROMPT = """你是 DeepRadio 的短期计划器。只输出一个 JSON 对象:
{"nodes":[{"id":"...","objective":"...","requires":[],"produces":[],"effect_level":"READ","success_predicates":[],"needs_user_decision":false,"tools":[]}]}

规则:
- 只规划到下一个用户决策边界
- id 必须来自 allowed_actions
- tools 必须来自 allowed_actions；禁止编造工具
- 不要用七类 Task 名称当执行路由
- 不要发明 RF 运行，除非用户明确要求发射/运行且 allowed 中有对应 action
"""


def propose_plan(
    intent: Any,
    shared_state: Any = None,
    *,
    catalog: dict[str, Any] | None = None,
) -> list[PlanNode] | None:
    """Return a compiler-bound proposal, or None to keep the capability plan."""
    try:
        from ..llm import chat, is_configured
    except Exception:  # noqa: BLE001
        return None
    if not is_configured():
        return None
    allowed = sorted(known_action_ids(catalog))
    payload = {
        "text": getattr(intent, "raw_text", "") or "",
        "goals": list(getattr(intent, "goals", None) or []),
        "requested_operations": list(getattr(intent, "requested_operations", None) or []),
        "capabilities": list(getattr(intent, "capabilities", None) or []),
        "stop_conditions": list(getattr(intent, "stop_conditions", None) or []),
        "desired_artifacts": list(getattr(intent, "desired_artifacts", None) or []),
        "has_project": bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
        ),
        "allowed_actions": allowed,
    }
    try:
        content = chat(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )
        from ..llm import parse_json_object

        parsed = parse_json_object(content)
    except Exception as exc:  # noqa: BLE001
        logger.info("LLM 短期计划失败，沿用能力计划: %s", exc)
        return None
    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return None
    return [PlanNode.from_dict(item) for item in nodes if isinstance(item, dict)]
