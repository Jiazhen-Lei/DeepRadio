"""MainAgent-only tools for maintaining the dynamic Workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..tools.registry import ToolContext
from ..workflow.dynamic import DynamicWorkflowStore
from . import session_store as store


def _validate_stage_plan(stages: List[Dict[str, Any]]) -> List[str]:
    """Validate fixed Stage-to-Task mappings from the maintained library."""
    try:
        from grc.core.io import yaml

        path = (
            Path(__file__).resolve().parents[1]
            / "skills/grc-orchestration/references/stage_library.yaml"
        )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        catalog = dict(data.get("stages") or {})
    except Exception as exc:  # noqa: BLE001 - surface a controlled tool error
        raise ValueError(f"Stage library could not be loaded: {exc}") from exc
    if not catalog:
        raise ValueError("Stage library is empty")
    if not stages:
        raise ValueError("Workflow requires at least one Stage")

    capabilities: List[str] = []
    for stage in stages or []:
        if not isinstance(stage, dict):
            raise ValueError("Each Stage must be an object")
        stage_id = str(stage.get("id") or "")
        definition = catalog.get(stage_id)
        if not isinstance(definition, dict):
            raise ValueError(f"Unknown Stage: {stage_id or '(empty)'}")
        tasks = [item for item in stage.get("tasks") or [] if isinstance(item, dict)]
        if len(tasks) != 1:
            raise ValueError(f"Stage {stage_id} requires exactly one Task")
        task = tasks[0]
        expected_task = dict(definition.get("task") or {})
        if str(task.get("id") or "") != str(expected_task.get("id") or ""):
            raise ValueError(f"Stage {stage_id} has an invalid Task id")
        if str(task.get("target_agent") or "") != str(definition.get("target_agent") or ""):
            raise ValueError(f"Stage {stage_id} has an invalid target_agent")
        required_evidence = {
            str(item) for item in expected_task.get("expected_evidence") or []
        }
        declared_evidence = {
            str(item) for item in task.get("expected_evidence") or []
        }
        if not required_evidence.issubset(declared_evidence):
            raise ValueError(f"Stage {stage_id} is missing required Evidence")
        for capability in definition.get("capabilities") or []:
            if capability not in capabilities:
                capabilities.append(str(capability))
    return capabilities


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
        Stage fields: id, objective, status, result_refs, tasks. Each Stage has
        exactly one Task. The Task has
        id, objective, target_agent, inputs, expected_evidence, status and
        result_refs. Use the revision from CURRENT_WORKFLOW.
        """
        try:
            capabilities = _validate_stage_plan(stages)
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
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
        if newly_completed and str(current_stage or "") != newly_completed[0]:
            return json.dumps({
                "ok": False,
                "error": "Keep current_stage on the Stage completed this turn",
            }, ensure_ascii=False)
        if newly_completed and any(
            str(stage.get("status") or "") == "running" for stage in stages
        ):
            return json.dumps({
                "ok": False,
                "error": "Do not start another Stage in the completion update",
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
                state.intent = SharedIntent.new(
                    result.intent.raw_text, workflow_id=result.workflow_id
                )
            elif (
                state.intent.workflow_id
                and state.intent.workflow_id != result.workflow_id
            ):
                state.intent = SharedIntent.new(
                    result.intent.raw_text, workflow_id=result.workflow_id
                )
            elif not state.intent.workflow_id:
                state.intent.workflow_id = result.workflow_id
            state.intent.raw_text = result.intent.raw_text
            state.intent.task_type = result.task_type
            state.intent.capabilities = capabilities
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
