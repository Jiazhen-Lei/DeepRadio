"""MainAgent-only tools for maintaining the dynamic Workflow."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..tools.registry import ToolContext
from ..workflow.dynamic import DynamicWorkflowStore
from . import session_store as store


def build_workflow_tools(ctx: ToolContext, workflow: DynamicWorkflowStore) -> list[Any]:
    from langchain_core.tools import tool

    @tool
    def update_workflow(
        intent_summary: str,
        stages: List[Dict[str, Any]],
        current_stage: str = "",
        execution_status: str = "running",
        task_type: str = "DYNAMIC",
        intent_slots: Dict[str, Any] | None = None,
        expected_revision: int = 1,
    ) -> str:
        """Create or update the complete ordered Workflow.

        MainAgent must call this before delegation and after verified results.
        Stage fields: id, objective, status, result_refs, tasks. Each Task has
        id, objective, target_agent, inputs, expected_evidence, status and
        result_refs. A Stage is user-visible; its Tasks are internal delegated
        work. Use the revision from CURRENT_WORKFLOW.
        """
        state = ctx.extra.get("state")
        previous_status = {
            stage.id: stage.status
            for stage in (workflow.workflow.stages if workflow.workflow else [])
        }
        newly_completed = [
            str(stage.get("id") or "")
            for stage in stages or []
            if stage.get("status") == "completed"
            and previous_status.get(str(stage.get("id") or "")) != "completed"
        ]
        if len(newly_completed) > 1:
            return json.dumps({
                "ok": False,
                "error": "Only one user-visible Stage may complete in one turn",
            }, ensure_ascii=False)
        completed_this_turn = str(ctx.extra.get("completed_stage_this_turn") or "")
        if newly_completed and completed_this_turn not in {"", newly_completed[0]}:
            return json.dumps({
                "ok": False,
                "error": "This turn already completed its current Stage",
            }, ensure_ascii=False)
        project_version = int(
            getattr(getattr(state, "project", None), "flowgraph_version", 0)
        )
        try:
            result = workflow.update(
                intent_summary=intent_summary,
                intent_slots=dict(intent_slots or {}),
                stages=list(stages or []),
                current_stage=current_stage,
                execution_status=execution_status,
                task_type=task_type,
                expected_revision=expected_revision,
                events=list(ctx.extra.get("events") or []),
                artifacts=dict(ctx.extra.get("artifacts") or {}),
                metrics=dict(ctx.extra.get("metrics") or {}),
                project_version=project_version,
            )
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        if newly_completed and result.execution_status != "completed":
            result.execution_status = "pending"
            workflow.save()
        if state is not None:
            from ..state import ClaimStore, SharedIntent

            if not state.intent.intent_id:
                state.intent = SharedIntent.new(result.intent.raw_text)
            state.intent.status = "confirmed"
            state.intent.raw_text = result.intent.raw_text
            state.intent.task_type = result.task_type
            state.intent.parameters = dict(result.intent.slots)
            state.intent.parameter_sources = {
                key: "mainagent" for key in result.intent.slots
            }
            state.intent.goals = [result.intent.summary] if result.intent.summary else []
            state.intent.revision = result.revision
            state.intent.refresh_hash()
            if workflow.reopened_from:
                ClaimStore(state).invalidate_by_intent_revision(result.revision)
                state.project.config["rf_armed"] = False
                state.project.config.pop("rf_armed_path", None)
                state.project.config.pop("rf_permission_grant", None)
                state.runtime.granted_permissions = [
                    permission
                    for permission in state.runtime.granted_permissions
                    if permission not in {"rf.start", "RF_RUN"}
                ]
                runtime = dict(state.project.config.get("runtime") or {})
                if runtime.get("running"):
                    from ..tools import registry

                    registry.call("stop_flowgraph", {}, ctx)
            state_path = str(ctx.extra.get("state_path") or "")
            if state_path:
                state.save(state_path)
        ctx.extra["workflow"] = result.to_dict()
        ctx.extra["stage_id"] = result.current_stage
        if newly_completed:
            ctx.extra["completed_stage_this_turn"] = newly_completed[0]
        store.append_session_event(
            str(ctx.extra.get("session_id") or ""),
            "workflow_updated_by_mainagent",
            {
                "workflow_id": result.workflow_id,
                "revision": result.revision,
                "current_stage": result.current_stage,
                "execution_status": result.execution_status,
            },
        )
        payload = {"ok": True, "workflow": workflow.digest()}
        if newly_completed:
            payload["turn_complete"] = True
            payload["instruction"] = (
                "Reply with this Stage result and stop. Do not execute the next Stage in this turn."
            )
        return json.dumps(payload, ensure_ascii=False)

    @tool
    def request_user_decision(
        stage_id: str,
        question: str,
        purpose: str = "user_decision",
        permission: str = "",
        kind: str = "approval",
    ) -> str:
        """Pause the Workflow and request one structured user decision.

        Use kind='input' for missing information. Use kind='approval' and
        permission='rf.start' before physical RF transmission. This tool records
        a request only; it never grants a permission.
        """
        try:
            checkpoint = workflow.request_decision(
                stage_id=stage_id,
                question=question,
                purpose=purpose,
                permission=permission,
                kind=kind,
            )
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        ctx.extra["pending_decision"] = dict(checkpoint)
        store.append_session_event(
            str(ctx.extra.get("session_id") or ""),
            "checkpoint_opened",
            dict(checkpoint),
        )
        return json.dumps({"ok": True, "checkpoint": checkpoint}, ensure_ascii=False)

    return [update_workflow, request_user_decision]
