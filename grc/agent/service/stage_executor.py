"""Small contract boundary shared by deepagents and deterministic Stage paths."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..state import ResultEnvelope, TaskCard
from ..workflow.completion import complete, evaluate


def synthesize_deterministic_invocations(
    parent: TaskCard,
    stage: Any,
    reply: Any,
    executor_id: str = "deterministic_stage_handler",
) -> list[dict[str, Any]]:
    """Record the one executor that actually ran the deterministic handler.

    ``recommended_agents`` is routing advice for an LLM orchestrator.  Creating
    one synthetic record per recommendation made the audit trail claim work
    that never happened and hid differences between deterministic and
    deep-agent execution.
    """
    tools = [
        {
            "name": getattr(item, "name", ""),
            "ok": bool(getattr(item, "ok", True)),
            "result": getattr(item, "result", None),
        }
        for item in (getattr(reply, "tool_invocations", None) or [])
    ]
    reply_ok = getattr(reply, "stage", "") not in (
        "ERROR",
        "CRITIC",
        "DENY",
        "CONFIRM",
        "CANCELLED",
    )
    card = make_invocation_card(parent, executor_id)
    envelope = ResultEnvelope(
        task_id=card.task_id,
        ok=reply_ok,
        outcome="passed" if reply_ok else "failed",
        artifacts=dict(getattr(reply, "artifacts", None) or {}),
        note=str(getattr(reply, "text", "") or ""),
        quality="clean",
        workflow_id=parent.workflow_id,
        stage_id=parent.stage_id,
        workflow_revision=parent.workflow_revision,
        base_project_version=parent.base_project_version,
        intent_id=parent.intent_id,
        intent_revision=parent.intent_revision,
        intent_hash=parent.intent_hash,
    )
    envelope.validate()
    record = vars(card)
    record["protocol_valid"] = True
    record["result"] = {
        "task_id": envelope.task_id,
        "ok": envelope.ok,
        "outcome": envelope.outcome,
        "workflow_id": envelope.workflow_id,
        "stage_id": envelope.stage_id,
        "workflow_revision": envelope.workflow_revision,
        "base_project_version": envelope.base_project_version,
        "intent_id": envelope.intent_id,
        "intent_revision": envelope.intent_revision,
        "intent_hash": envelope.intent_hash,
    }
    record["tools"] = tools
    return [record]


def make_task_card(workflow: Any, stage: Any, state: Any, user_text: str) -> TaskCard:
    shared_intent = getattr(state, "intent", None)
    prior = [
        {
            "stage_id": item.id,
            "outcome": item.outcome,
            "artifact_refs": {
                key: value
                for key, value in dict(item.result.get("artifacts") or {}).items()
                if isinstance(value, str)
            },
            "produced_claims": list(item.result.get("produced_claims") or []),
            "failure_codes": list(
                (item.result.get("acceptance") or {}).get("failure_codes") or []
            ),
            "result_digest": str(item.result.get("fingerprint") or ""),
        }
        for item in workflow.stages
        if item.result
    ]
    card = TaskCard(
        task_id=f"task-{uuid.uuid4().hex[:8]}",
        loop_mode=workflow.task_type,
        target_agent=(stage.recommended_agents or ["main_agent"])[0],
        instruction=workflow.intent.raw_text or user_text,
        inputs={
            "intent": dict(workflow.intent.slots),
            "capabilities": list(workflow.intent.capabilities),
            "slot_sources": dict(workflow.intent.slot_sources),
            "context": dict(workflow.intent.context),
            "current_grc": str(getattr(state.project, "grc_path", "") or ""),
            "prior_results": prior,
            "claim_snapshot": {
                str(item.id): _claim_fingerprint(item)
                for item in list(getattr(state, "claims", None) or [])
            },
            "shared_intent": (
                shared_intent.snapshot() if shared_intent is not None else {}
            ),
        },
        expected_results=list(stage.completion),
        workflow_id=workflow.workflow_id,
        stage_id=stage.id,
        workflow_revision=workflow.revision,
        base_project_version=workflow.base_project_version,
        intent_id=str(getattr(shared_intent, "intent_id", "") or ""),
        intent_revision=int(getattr(shared_intent, "revision", 0) or 0),
        intent_hash=str(getattr(shared_intent, "semantic_hash", "") or ""),
    )
    card.validate()
    return card


def make_invocation_card(parent: TaskCard, target_agent: str, instruction: str = "") -> TaskCard:
    card = TaskCard(
        task_id=f"task-{uuid.uuid4().hex[:8]}",
        loop_mode=parent.loop_mode,
        target_agent=target_agent,
        instruction=instruction or parent.instruction,
        inputs=dict(parent.inputs),
        expected_claims=list(parent.expected_claims),
        expected_results=list(parent.expected_results),
        workflow_id=parent.workflow_id,
        stage_id=parent.stage_id,
        workflow_revision=parent.workflow_revision,
        base_project_version=parent.base_project_version,
        intent_id=parent.intent_id,
        intent_revision=parent.intent_revision,
        intent_hash=parent.intent_hash,
    )
    card.validate()
    return card


def bind_invocation_result(
    invocation: dict[str, Any],
    parsed: dict[str, Any] | None,
    parent: TaskCard | None = None,
) -> dict[str, Any]:
    """Mark one Subagent ResultEnvelope as protocol-valid or not.

    Models only have to return ``outcome`` and a boolean ``completion`` map.
    Identity mismatches and missing optional lists are warnings, not a veto
    of host-proved work.
    """
    completion = parsed.get("completion") if isinstance(parsed, dict) else None
    shape_valid = bool(
        parsed
        and parsed.get("outcome") in {"passed", "failed", "inconclusive"}
        and isinstance(completion, dict)
        and all(isinstance(value, bool) for value in completion.values())
    )
    workflow_id = parsed.get("workflow_id") if parsed else None
    stage_id = parsed.get("stage_id") if parsed else None
    identity_ok = (
        (not workflow_id or workflow_id == invocation.get("workflow_id"))
        and (not stage_id or stage_id == invocation.get("stage_id"))
    )
    protocol_valid = bool(parsed and shape_valid and identity_ok)
    if isinstance(parsed, dict):
        if "ok" not in parsed:
            parsed["ok"] = parsed.get("outcome") == "passed"
        if not isinstance(parsed.get("artifacts"), dict):
            parsed["artifacts"] = {}
        if not isinstance(parsed.get("produced_claims"), list):
            parsed["produced_claims"] = []
        if not isinstance(parsed.get("proposed_changes"), list):
            parsed["proposed_changes"] = []
        if not parsed.get("task_id"):
            parsed["task_id"] = invocation.get("task_id") or getattr(
                parent, "task_id", ""
            )
    invocation["protocol_valid"] = protocol_valid
    invocation["result"] = parsed
    return invocation


def make_result_envelope(
    workflow: Any,
    stage: Any,
    state: Any,
    task_card: TaskCard,
    reply: Any,
    invocations: list[dict[str, Any]],
) -> ResultEnvelope:
    completion = evaluate(stage, workflow, state, reply)
    # Host-proved completion is the Stage gate. Envelope shape / task_id
    # mismatches are warnings when the tools already produced the work.
    protocol_strict = bool(invocations) and all(
        item.get("protocol_valid") is True for item in invocations
    )
    completion_ok = complete(completion)
    protocol_ok = bool(invocations) and (protocol_strict or completion_ok)
    stage_name = getattr(reply, "stage", "")
    # Host-proved completion is the Stage gate. CRITIC is advisory when the
    # tools already satisfied completion.
    reply_ok = stage_name not in (
        "ERROR",
        "DENY",
        "CONFIRM",
        "CANCELLED",
    ) and (stage_name != "CRITIC" or completion_ok)
    succeeded = reply_ok and bool(invocations) and completion_ok
    errored = getattr(reply, "stage", "") == "ERROR"
    failure_codes = []
    protocol_warnings = []
    if not reply_ok:
        failure_codes.append("REPLY_STATUS_REJECTED")
    if not invocations:
        failure_codes.append("MISSING_EXECUTION_INVOCATION")
    elif not protocol_strict:
        if completion_ok:
            protocol_warnings.append("INVALID_EXECUTION_INVOCATION")
        else:
            failure_codes.append("INVALID_EXECUTION_INVOCATION")
    failure_codes.extend(
        f"MISSING_COMPLETION:{name}"
        for name, passed in completion.items()
        if not passed
    )
    before = dict((task_card.inputs or {}).get("claim_snapshot") or {})
    produced_claims = []
    for claim in list(getattr(state, "claims", None) or []):
        claim_id = str(getattr(claim, "id", "") or "")
        if claim_id and before.get(claim_id) != _claim_fingerprint(claim):
            produced_claims.append(claim_id)
    envelope = ResultEnvelope(
        task_id=task_card.task_id,
        workflow_id=workflow.workflow_id,
        stage_id=stage.id,
        workflow_revision=workflow.revision,
        base_project_version=workflow.base_project_version,
        intent_id=task_card.intent_id,
        intent_revision=task_card.intent_revision,
        intent_hash=task_card.intent_hash,
        ok=succeeded,
        outcome="inconclusive" if errored else ("passed" if succeeded else "failed"),
        quality=str(getattr(getattr(state, "runtime", None), "quality", "clean")),
        artifacts=dict(getattr(reply, "artifacts", None) or {}),
        produced_claims=produced_claims,
        proposed_changes=list(
            (getattr(reply, "pending", None) or {}).get("proposed_changes") or []
        ),
        note=str(getattr(reply, "text", "") or ""),
        completion=completion,
        invocations=list(invocations or []),
        acceptance={
            "reply_ok": reply_ok,
            "execution_protocol_ok": protocol_ok,
            "completion_ok": completion_ok,
            "failure_codes": failure_codes,
            "protocol_warnings": protocol_warnings,
        },
    )
    envelope.validate()
    return envelope


def _claim_fingerprint(claim: Any) -> str:
    payload = {
        "id": getattr(claim, "id", ""),
        "statement": getattr(claim, "statement", ""),
        "layer": getattr(claim, "layer", ""),
        "status": getattr(claim, "status", ""),
        "project_version": getattr(claim, "project_version", 0),
        "evidence": [
            vars(item) if hasattr(item, "__dict__") else item
            for item in list(getattr(claim, "evidence", None) or [])
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
