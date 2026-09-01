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
        Stage fields: id, objective, target_agent, inputs, expected_evidence,
        status, result_refs. Use the revision from CURRENT_WORKFLOW.
        """
        state = ctx.extra.get("state")
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
        if state is not None:
            from ..state import SharedIntent

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
            state_path = str(ctx.extra.get("state_path") or "")
            if state_path:
                state.save(state_path)
        ctx.extra["workflow"] = result.to_dict()
        ctx.extra["stage_id"] = result.current_stage
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
        return json.dumps(
            {"ok": True, "workflow": workflow.digest()}, ensure_ascii=False
        )

    @tool
    def request_user_decision(
        stage_id: str,
        question: str,
        purpose: str = "user_decision",
        permission: str = "",
    ) -> str:
        """Pause the Workflow and request one structured user decision.

        Use permission='rf.start' before physical RF transmission. This tool
        records a request only; it never grants a permission.
        """
        try:
            checkpoint = workflow.request_decision(
                stage_id=stage_id,
                question=question,
                purpose=purpose,
                permission=permission,
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
