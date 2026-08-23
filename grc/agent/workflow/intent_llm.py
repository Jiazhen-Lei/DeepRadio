"""Optional LLM completion for low-confidence WorkflowIntent classification.

Rules remain the source of truth for explicit user slots. The model may only
fill ambiguous task_type / capabilities / missing slots. Any failure falls
back to the rules Intent unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .schema import WorkflowIntent

logger = logging.getLogger(__name__)

_TASK_TYPES = frozenset(
    {
        "END_TO_END_SIM",
        "TX_BUILD",
        "RX_BUILD",
        "DIAGNOSE",
        "MODIFY_PROJECT",
        "OBSERVE",
        "HARDWARE_CONFIGURE",
    }
)
_CAPABILITIES = frozenset(
    {
        "diagnose",
        "modify_project",
        "build_rx",
        "build_tx",
        "build_signal",
        "hardware_configure",
        "observe",
        "realtime_observe",
        "signal_agnostic_observe",
        "protocol",
        "deploy",
        "hardware_runtime",
    }
)
_PROMPT = """你是 DeepRadio 的 Intent 结构化补全器。只输出一个 JSON 对象，不要 Markdown。
字段:
- task_type: 七类之一 END_TO_END_SIM / TX_BUILD / RX_BUILD / DIAGNOSE / MODIFY_PROJECT / OBSERVE / HARDWARE_CONFIGURE
- capabilities: 字符串数组，只能使用给定集合
- slots: 只填写用户文本里明确出现或可安全默认的参数
- confidence: 0~1
规则:
- 不得把硬件实时观察改写成离线仿真
- 不得因为 2.4GHz 就判定 BLE，除非用户说了 ble/蓝牙/发射广播
- 用户已给出的槽位不得覆盖
- 不要发明 local_name 或 operation=deploy
"""


def complete_intent(
    rules_intent: WorkflowIntent, text: str, shared_state: Any
) -> WorkflowIntent:
    """Merge a low-confidence rules Intent with an optional LLM JSON patch."""
    try:
        from ..llm import chat, is_configured
    except Exception:  # noqa: BLE001
        return rules_intent
    if not is_configured():
        return rules_intent
    payload = {
        "text": text,
        "rules_intent": {
            "task_type": rules_intent.task_type,
            "confidence": rules_intent.confidence,
            "slots": rules_intent.slots,
            "missing_slots": rules_intent.missing_slots,
            "capabilities": rules_intent.capabilities,
            "slot_sources": rules_intent.slot_sources,
        },
        "allowed_task_types": sorted(_TASK_TYPES),
        "allowed_capabilities": sorted(_CAPABILITIES),
        "has_project": bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
        ),
    }
    try:
        content = chat(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )
        parsed = _parse_json_object(content)
    except Exception as exc:  # noqa: BLE001
        logger.info("Intent LLM 补全失败，沿用规则分类: %s", exc)
        return rules_intent
    return _merge(rules_intent, parsed)


def _merge(rules: WorkflowIntent, parsed: dict[str, Any]) -> WorkflowIntent:
    task_type = str(parsed.get("task_type") or rules.task_type)
    if task_type not in _TASK_TYPES:
        task_type = rules.task_type
    capabilities = list(rules.capabilities)
    for name in parsed.get("capabilities") or []:
        if name in _CAPABILITIES and name not in capabilities:
            capabilities.append(str(name))
    slots = dict(rules.slots)
    sources = dict(rules.slot_sources)
    for key, value in dict(parsed.get("slots") or {}).items():
        if value in (None, "", []):
            continue
        if sources.get(key) == "user":
            continue
        slots[key] = value
        sources[key] = "llm"
    try:
        confidence = float(parsed.get("confidence", rules.confidence))
    except (TypeError, ValueError):
        confidence = rules.confidence
    confidence = min(1.0, max(rules.confidence, confidence, 0.0))
    return WorkflowIntent(
        raw_text=rules.raw_text,
        turn_relation=rules.turn_relation,
        task_type=task_type,
        confidence=confidence,
        slots=slots,
        missing_slots=list(rules.missing_slots),
        capabilities=capabilities,
        slot_sources=sources,
        context=dict(rules.context),
        validation_errors=list(rules.validation_errors),
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Intent LLM 返回值不是对象")
    return data
