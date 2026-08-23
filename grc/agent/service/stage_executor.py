"""Small contract boundary shared by deepagents and deterministic Stage paths."""

from __future__ import annotations

import uuid
from typing import Any

from ..state import ResultEnvelope, TaskCard
from ..workflow.completion import complete, evaluate


def synthesize_deterministic_invocations(
    parent: TaskCard,
    stage: Any,
    reply: Any,
) -> list[dict[str, Any]]:
    """One TaskCard/ResultEnvelope pair per recommended Subagent."""
    agents = list(stage.recommended_agents or [parent.target_agent])
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
    invocations: list[dict[str, Any]] = []
    for agent in agents:
        card = make_invocation_card(parent, agent)
        envelope = ResultEnvelope(
            task_id=card.task_id,
            ok=reply_ok,
            outcome="passed" if reply_ok else "failed",
            artifacts=dict(getattr(reply, "artifacts", None) or {}),
            note=str(getattr(reply, "text", "") or ""),
            workflow_id=parent.workflow_id,
            stage_id=parent.stage_id,
            workflow_revision=parent.workflow_revision,
            base_project_version=parent.base_project_version,
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
        }
        record["tools"] = tools
        invocations.append(record)
    return invocations


def make_task_card(workflow: Any, stage: Any, state: Any, user_text: str) -> TaskCard:
    prior = [
        {"stage_id": item.id, "outcome": item.outcome, "result": item.result}
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
        },
        expected_results=list(stage.completion),
        workflow_id=workflow.workflow_id,
        stage_id=stage.id,
        workflow_revision=workflow.revision,
        base_project_version=workflow.base_project_version,
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
    )
    card.validate()
    return card


def invocation_cards_for_stage(parent: TaskCard, stage: Any) -> list[TaskCard]:
    agents = list(stage.recommended_agents or [parent.target_agent])
    return [make_invocation_card(parent, agent) for agent in agents]


def bind_invocation_result(
    invocation: dict[str, Any],
    parsed: dict[str, Any] | None,
    parent: TaskCard | None = None,
) -> dict[str, Any]:
    """Mark one Subagent ResultEnvelope as protocol-valid or not."""
    protocol_valid = bool(
        parsed
        and isinstance(parsed.get("ok"), bool)
        and parsed.get("task_id")
        in {
            invocation.get("task_id"),
            getattr(parent, "task_id", None),
        }
        and parsed.get("workflow_id") == invocation.get("workflow_id")
        and parsed.get("stage_id") == invocation.get("stage_id")
        and parsed.get("workflow_revision") == invocation.get("workflow_revision")
        and parsed.get("base_project_version") == invocation.get("base_project_version")
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
    required_agents = [
        name for name in (getattr(stage, "recommended_agents", None) or []) if name
    ]
    seen_agents = {
        str(item.get("target_agent") or "")
        for item in invocations
    }
    coverage_ok = not required_agents or all(
        name in seen_agents for name in required_agents
    )
    protocol_ok = (
        bool(invocations)
        and coverage_ok
        and all(item.get("protocol_valid") is True for item in invocations)
    )
    reply_ok = getattr(reply, "stage", "") not in (
        "ERROR",
        "CRITIC",
        "DENY",
        "CONFIRM",
        "CANCELLED",
    )
    succeeded = reply_ok and protocol_ok and complete(completion)
    errored = getattr(reply, "stage", "") == "ERROR"
    envelope = ResultEnvelope(
        task_id=task_card.task_id,
        workflow_id=workflow.workflow_id,
        stage_id=stage.id,
        workflow_revision=workflow.revision,
        base_project_version=workflow.base_project_version,
        ok=succeeded,
        outcome="inconclusive" if errored else ("passed" if succeeded else "failed"),
        artifacts=dict(getattr(reply, "artifacts", None) or {}),
        produced_claims=[
            claim.get("id")
            for claim in (getattr(reply, "claims", None) or [])
            if claim.get("id")
        ],
        proposed_changes=list(
            (getattr(reply, "pending", None) or {}).get("proposed_changes") or []
        ),
        note=str(getattr(reply, "text", "") or ""),
        completion=completion,
        invocations=list(invocations or []),
    )
    envelope.validate()
    return envelope
