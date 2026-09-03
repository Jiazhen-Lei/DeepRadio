"""MainAgent-only tools for maintaining the dynamic Workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..tools.registry import ToolContext
from ..workflow.dynamic import DynamicWorkflowStore
from . import session_store as store


def _normalize_intent_slots(
    value: Dict[str, Any] | str | None,
) -> Dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ValueError("intent_slots must be a dictionary or an empty string")


def _load_stage_catalog() -> Dict[str, Dict[str, Any]]:
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
    return catalog


def _materialize_stage_plan(
    stages: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Build fixed Task fields from the Stage library."""
    if not stages:
        raise ValueError("Workflow requires at least one Stage")

    catalog = _load_stage_catalog()
    materialized: List[Dict[str, Any]] = []
    capabilities: List[str] = []
    for stage in stages or []:
        if not isinstance(stage, dict):
            raise ValueError("Each Stage must be an object")
        stage_id = str(stage.get("id") or "")
        definition = catalog.get(stage_id)
        if not isinstance(definition, dict):
            raise ValueError(f"Unknown Stage: {stage_id or '(empty)'}")
        legacy_tasks = [
            item for item in stage.get("tasks") or [] if isinstance(item, dict)
        ]
        legacy_task = legacy_tasks[0] if legacy_tasks else {}
        expected_task = dict(definition.get("task") or {})
        status = str(stage.get("status") or "pending")
        result_refs = list(
            stage.get("result_refs") or legacy_task.get("result_refs") or []
        )
        inputs = stage.get("inputs")
        if inputs is None:
            inputs = legacy_task.get("inputs") or {}
        materialized.append({
            "id": stage_id,
            "objective": str(
                stage.get("objective") or definition.get("objective") or ""
            ),
            "status": status,
            "result_refs": result_refs,
            "tasks": [{
                "id": str(expected_task.get("id") or ""),
                "objective": str(expected_task.get("objective") or ""),
                "target_agent": str(definition.get("target_agent") or ""),
                "inputs": dict(inputs or {}),
                "expected_evidence": list(
                    expected_task.get("expected_evidence") or []
                ),
                "status": status,
                "result_refs": list(result_refs),
            }],
        })
        for capability in definition.get("capabilities") or []:
            if capability not in capabilities:
                capabilities.append(str(capability))
    return materialized, capabilities


def build_workflow_tools(ctx: ToolContext, workflow: DynamicWorkflowStore) -> list[Any]:
    from langchain_core.tools import tool

    @tool
    def update_workflow(
        intent_summary: str,
        stages: List[Dict[str, Any]],
        current_stage: str = "",
        execution_status: str = "running",
        task_type: str = "DYNAMIC",
        intent_slots: Dict[str, Any] | str | None = None,
        expected_revision: int = 1,
    ) -> str:
        """Create or update the complete ordered Workflow.

        Use this for initial planning or user-requested structural replanning.
        Stage fields: id, objective, inputs, status and result_refs. Fixed Task,
        target_agent and expected_evidence fields come from the Stage library.
        Use the revision from CURRENT_WORKFLOW.
        intent_slots must be a JSON object; an empty string is treated as {}.
        """
        try:
            normalized_slots = _normalize_intent_slots(intent_slots)
            stage_plan, capabilities = _materialize_stage_plan(stages)
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        state = ctx.extra.get("state")
        previous_status = {
            stage.id: stage.status
            for stage in (workflow.workflow.stages if workflow.workflow else [])
        }
        newly_completed = [
            str(stage.get("id") or "")
            for stage in stage_plan
            if stage.get("status") == "completed"
            and previous_status.get(str(stage.get("id") or "")) != "completed"
        ]
        newly_failed = [
            str(stage.get("id") or "")
            for stage in stage_plan
            if stage.get("status") == "failed"
            and previous_status.get(str(stage.get("id") or "")) != "failed"
        ]
        newly_finished = newly_completed + newly_failed
        if len(newly_finished) > 1:
            return json.dumps({
                "ok": False,
                "error": "Only one user-visible Stage may finish in one turn",
            }, ensure_ascii=False)
        if newly_finished and str(current_stage or "") != newly_finished[0]:
            return json.dumps({
                "ok": False,
                "error": "Keep current_stage on the Stage finished this turn",
            }, ensure_ascii=False)
        if newly_finished and any(
            str(stage.get("status") or "") == "running" for stage in stage_plan
        ):
            return json.dumps({
                "ok": False,
                "error": "Do not start another Stage in the current Stage result update",
            }, ensure_ascii=False)
        finished_this_turn = str(ctx.extra.get("finished_stage_this_turn") or "")
        if newly_finished and finished_this_turn not in {"", newly_finished[0]}:
            return json.dumps({
                "ok": False,
                "error": "This turn already finished its current Stage",
            }, ensure_ascii=False)
        project_version = int(
            getattr(getattr(state, "project", None), "flowgraph_version", 0)
        )
        try:
            result = workflow.update(
                intent_summary=intent_summary,
                intent_slots=normalized_slots,
                stages=stage_plan,
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
                on_reopened = ctx.extra.get("on_workflow_reopened")
                if callable(on_reopened):
                    on_reopened(result.revision)
            state_path = str(ctx.extra.get("state_path") or "")
            if state_path:
                state.save(state_path)
        ctx.extra["workflow"] = result.to_dict()
        ctx.extra["stage_id"] = result.current_stage
        if newly_finished:
            ctx.extra["finished_stage_this_turn"] = newly_finished[0]
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
        on_updated = ctx.extra.get("on_workflow_updated")
        if callable(on_updated):
            on_updated("workflow_updated")
        if newly_finished:
            payload["turn_complete"] = True
            payload["instruction"] = (
                "Reply with this Stage result and stop. Do not execute the next Stage in this turn."
            )
        return json.dumps(payload, ensure_ascii=False)

    @tool
    def update_current_stage(
        stage_id: str,
        status: str,
        expected_revision: int,
        inputs: Dict[str, Any] | None = None,
        result_refs: List[str] | None = None,
    ) -> str:
        """Update one Stage or start the immediate next Stage.

        Use this for routine execution updates. It preserves the Workflow plan
        and all unrelated Stages. Use update_workflow for structural replanning
        or reopening a completed Stage.
        """
        finished = status in {"completed", "failed"}
        finished_this_turn = str(ctx.extra.get("finished_stage_this_turn") or "")
        if finished and finished_this_turn not in {"", stage_id}:
            return json.dumps({
                "ok": False,
                "error": "This turn already finished its current Stage",
            }, ensure_ascii=False)
        state = ctx.extra.get("state")
        project_version = int(
            getattr(getattr(state, "project", None), "flowgraph_version", 0)
        )
        try:
            result = workflow.update_stage(
                stage_id=stage_id,
                status=status,
                inputs=inputs,
                result_refs=result_refs,
                expected_revision=expected_revision,
                events=list(ctx.extra.get("events") or []),
                artifacts=dict(ctx.extra.get("artifacts") or {}),
                metrics=dict(ctx.extra.get("metrics") or {}),
                project_version=project_version,
            )
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

        if state is not None:
            state.intent.revision = result.revision
            state.intent.refresh_hash()
            state_path = str(ctx.extra.get("state_path") or "")
            if state_path:
                state.save(state_path)
        ctx.extra["workflow"] = result.to_dict()
        ctx.extra["stage_id"] = result.current_stage
        if finished:
            ctx.extra["finished_stage_this_turn"] = stage_id
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
        on_updated = ctx.extra.get("on_workflow_updated")
        if callable(on_updated):
            on_updated("workflow_updated")
        payload = {"ok": True, "workflow": workflow.digest()}
        if finished:
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
        on_updated = ctx.extra.get("on_workflow_updated")
        if callable(on_updated):
            on_updated("workflow_waiting")
        return json.dumps({"ok": True, "checkpoint": checkpoint}, ensure_ascii=False)

    return [update_workflow, update_current_stage, request_user_decision]
