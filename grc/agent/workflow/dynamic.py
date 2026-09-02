"""MainAgent-owned dynamic Workflow state.

The store persists and validates state, but never chooses a Stage or a
transition.  Planning authority belongs to MainAgent.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


WORKFLOW_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "errored", "cancelled"}
)
STAGE_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "failed"}
)
PERMISSIONS = frozenset(
    {
        "project.read",
        "project.write",
        "device.read",
        "device.configure",
        "rf.start",
        "rf.stop",
    }
)


@dataclass
class DynamicIntent:
    raw_text: str = ""
    summary: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicIntent":
        return cls(
            raw_text=str(data.get("raw_text") or ""),
            summary=str(data.get("summary") or ""),
            slots=dict(data.get("slots") or {}),
        )


@dataclass
class DynamicTask:
    id: str
    objective: str
    target_agent: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    expected_evidence: List[str] = field(default_factory=list)
    status: str = "pending"
    result_refs: List[str] = field(default_factory=list)

    def validate(self, known_agents: Iterable[str]) -> None:
        if not self.id or not self.objective or not self.target_agent:
            raise ValueError("Task requires id, objective and target_agent")
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"Invalid Task status: {self.status}")
        if self.target_agent not in set(known_agents):
            raise ValueError(f"Unknown SubAgent: {self.target_agent}")
        if self.status == "completed" and not self.expected_evidence:
            raise ValueError("A completed Task must declare expected_evidence")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicTask":
        return cls(
            id=str(data.get("id") or ""),
            objective=str(data.get("objective") or ""),
            target_agent=str(data.get("target_agent") or ""),
            inputs=dict(data.get("inputs") or {}),
            expected_evidence=[
                str(item) for item in data.get("expected_evidence") or [] if item
            ],
            status=str(data.get("status") or "pending"),
            result_refs=[str(item) for item in data.get("result_refs") or [] if item],
        )


@dataclass
class DynamicStage:
    """A user-visible phase delegated through exactly one Task."""

    id: str
    objective: str
    tasks: List[DynamicTask] = field(default_factory=list)
    status: str = "pending"
    result_refs: List[str] = field(default_factory=list)

    @property
    def expected_evidence(self) -> List[str]:
        return list(dict.fromkeys(
            evidence
            for task in self.tasks
            for evidence in task.expected_evidence
        ))

    def validate(self, known_agents: Iterable[str]) -> None:
        if not self.id or not self.objective or not self.tasks:
            raise ValueError("Stage requires id, objective and tasks")
        if len(self.tasks) != 1:
            raise ValueError("Stage requires exactly one Task")
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"Invalid Stage status: {self.status}")
        for task in self.tasks:
            task.validate(known_agents)
        if self.status == "completed" and not self.expected_evidence:
            raise ValueError("A completed Stage must declare expected evidence")
        if self.status == "completed" and any(
            task.status != "completed" for task in self.tasks
        ):
            raise ValueError("A completed Stage requires completed Tasks")

    def signature(self) -> tuple:
        """Fields whose change makes this Stage and later results outdated."""
        return (
            self.objective,
            tuple(
                (
                    task.id,
                    task.objective,
                    task.target_agent,
                    json.dumps(task.inputs, ensure_ascii=False, sort_keys=True),
                    tuple(task.expected_evidence),
                )
                for task in self.tasks
            ),
        )

    def reset(self) -> None:
        self.status = "pending"
        self.result_refs = []
        for task in self.tasks:
            task.status = "pending"
            task.result_refs = []

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicStage":
        tasks = [
            DynamicTask.from_dict(item)
            for item in data.get("tasks") or []
            if isinstance(item, dict)
        ]
        # Load schema-v2 workflows as a one-task user Stage.
        if not tasks and data.get("target_agent"):
            tasks = [DynamicTask.from_dict({
                "id": f"{data.get('id') or 'stage'}_task",
                "objective": data.get("objective"),
                "target_agent": data.get("target_agent"),
                "inputs": data.get("inputs") or {},
                "expected_evidence": data.get("expected_evidence") or [],
                "status": data.get("status") or "pending",
                "result_refs": data.get("result_refs") or [],
            })]
        return cls(
            id=str(data.get("id") or ""),
            objective=str(data.get("objective") or ""),
            tasks=tasks,
            status=str(data.get("status") or "pending"),
            result_refs=[str(item) for item in data.get("result_refs") or [] if item],
        )


@dataclass
class DynamicWorkflow:
    workflow_id: str
    revision: int
    intent: DynamicIntent
    stages: List[DynamicStage]
    current_stage: str = ""
    execution_status: str = "pending"
    base_project_version: int = 0
    task_type: str = "DYNAMIC"
    checkpoint: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def validate(self, known_agents: Iterable[str]) -> None:
        if not self.workflow_id or self.revision < 1:
            raise ValueError("Workflow requires a valid id and revision")
        if self.execution_status not in WORKFLOW_STATUSES:
            raise ValueError(f"Invalid Workflow status: {self.execution_status}")
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("Workflow Stage ids must be unique")
        if self.current_stage and self.current_stage not in ids:
            raise ValueError("current_stage is not present in stages")
        for stage in self.stages:
            stage.validate(known_agents)

    def stage(self, stage_id: str = "") -> Optional[DynamicStage]:
        wanted = stage_id or self.current_stage
        return next((item for item in self.stages if item.id == wanted), None)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicWorkflow":
        return cls(
            workflow_id=str(data.get("workflow_id") or ""),
            revision=int(data.get("revision") or 1),
            intent=DynamicIntent.from_dict(dict(data.get("intent") or {})),
            stages=[
                DynamicStage.from_dict(item)
                for item in data.get("stages") or []
                if isinstance(item, dict)
            ],
            current_stage=str(data.get("current_stage") or ""),
            execution_status=str(data.get("execution_status") or "pending"),
            base_project_version=int(data.get("base_project_version") or 0),
            task_type=str(data.get("task_type") or "DYNAMIC"),
            checkpoint=dict(data.get("checkpoint") or {}),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class DynamicWorkflowStore:
    """Persist MainAgent decisions and expose the legacy GUI digest shape."""

    schema_version = 3

    def __init__(self, path: str, known_agents: Iterable[str]) -> None:
        self.path = path
        self.known_agents = tuple(known_agents)
        self.load_error = ""
        self.reopened_from = ""
        self.workflow = self._load()

    def begin_turn(self, user_text: str, project_version: int) -> DynamicWorkflow:
        if self.load_error:
            raise RuntimeError(self.load_error)
        text = str(user_text or "").strip()
        if self.workflow is None:
            self.workflow = DynamicWorkflow(
                workflow_id=f"wf-{uuid.uuid4().hex[:10]}",
                revision=1,
                intent=DynamicIntent(raw_text=text, summary=text),
                stages=[],
                execution_status="pending",
                base_project_version=int(project_version),
            )
        elif (
            text
            and self.workflow.execution_status == "waiting"
            and self.workflow.checkpoint.get("status") == "pending"
            and self.workflow.checkpoint.get("kind") == "input"
        ):
            self.workflow.checkpoint["status"] = "answered"
            self.workflow.checkpoint["answer"] = text
            stage = self.workflow.stage(
                str(self.workflow.checkpoint.get("stage_id") or "")
            )
            if stage:
                stage.status = "pending"
            self.workflow.execution_status = "running"
            self.workflow.revision += 1
            self.workflow.updated_at = time.time()
        self.save()
        return self.workflow

    def update(
        self,
        *,
        intent_summary: str,
        intent_slots: Dict[str, Any],
        stages: List[Dict[str, Any]],
        current_stage: str,
        execution_status: str,
        task_type: str,
        expected_revision: int,
        events: Iterable[Dict[str, Any]],
        artifacts: Dict[str, Any],
        metrics: Dict[str, Any],
        project_version: int,
    ) -> DynamicWorkflow:
        if self.workflow is None:
            self.begin_turn(intent_summary, project_version)
        assert self.workflow is not None
        if int(expected_revision) != self.workflow.revision:
            raise ValueError(
                f"Stale Workflow revision: expected {expected_revision}, "
                f"current {self.workflow.revision}"
            )
        if int(project_version) != self.workflow.base_project_version:
            self.workflow.base_project_version = int(project_version)
        proposed = [DynamicStage.from_dict(item) for item in stages]
        candidate = DynamicWorkflow(
            workflow_id=self.workflow.workflow_id,
            revision=self.workflow.revision + 1,
            intent=DynamicIntent(
                raw_text=self.workflow.intent.raw_text,
                summary=str(intent_summary or self.workflow.intent.summary),
                slots=dict(intent_slots or {}),
            ),
            stages=proposed,
            current_stage=str(current_stage or ""),
            execution_status=str(execution_status or "running"),
            base_project_version=int(project_version),
            task_type=str(task_type or "DYNAMIC"),
            checkpoint=dict(self.workflow.checkpoint),
            created_at=self.workflow.created_at,
            updated_at=time.time(),
        )
        candidate.validate(self.known_agents)
        previous = {stage.id: stage for stage in self.workflow.stages}
        previous_status = {stage.id: stage.status for stage in self.workflow.stages}
        previous_index = {
            stage.id: index for index, stage in enumerate(self.workflow.stages)
        }
        changed_indexes = [
            index
            for index, stage in enumerate(candidate.stages)
            if stage.id in previous
            and previous[stage.id].status == "completed"
            and stage.signature() != previous[stage.id].signature()
        ]
        prior_current_index = previous_index.get(self.workflow.current_stage)
        candidate_current_index = next(
            (
                index for index, stage in enumerate(candidate.stages)
                if stage.id == candidate.current_stage
            ),
            None,
        )
        if (
            prior_current_index is not None
            and candidate_current_index is not None
            and candidate_current_index < prior_current_index
        ):
            changed_indexes.append(candidate_current_index)
        self.reopened_from = ""
        if changed_indexes:
            reopened_index = min(changed_indexes)
            self.reopened_from = candidate.stages[reopened_index].id
            candidate.current_stage = self.reopened_from
            candidate.execution_status = "running"
            candidate.checkpoint = {}
            for stage in candidate.stages[reopened_index:]:
                stage.reset()
        current_index = next(
            (
                index for index, stage in enumerate(candidate.stages)
                if stage.id == candidate.current_stage
            ),
            None,
        )
        if current_index is not None:
            incomplete = next(
                (
                    stage.id for stage in candidate.stages[:current_index]
                    if stage.status != "completed"
                ),
                "",
            )
            if incomplete:
                raise ValueError(
                    f"Current Stage cannot advance past incomplete Stage {incomplete}"
                )
        for stage in candidate.stages:
            if (
                stage.status == "completed"
                and previous_status.get(stage.id) != "completed"
            ):
                missing = missing_evidence(
                    stage.expected_evidence, events, artifacts, metrics
                )
                if missing:
                    raise ValueError(
                        f"Stage {stage.id} lacks verified evidence: {', '.join(missing)}"
                    )
        self.workflow = candidate
        self.save()
        return candidate

    def request_decision(
        self, *, stage_id: str, question: str, purpose: str, permission: str,
        kind: str = "approval",
    ) -> Dict[str, Any]:
        if self.workflow is None:
            raise ValueError("There is no active Workflow")
        if permission and permission not in PERMISSIONS:
            raise ValueError(f"Unknown permission: {permission}")
        if kind not in {"input", "approval"}:
            raise ValueError("Interaction kind must be input or approval")
        if permission and kind != "approval":
            raise ValueError("A permission request must use approval interaction")
        if stage_id and self.workflow.stage(stage_id) is None:
            raise ValueError(f"Unknown Stage: {stage_id}")
        checkpoint = {
            "id": f"checkpoint-{uuid.uuid4().hex[:10]}",
            "stage_id": stage_id,
            "question": str(question or "Please confirm before continuing."),
            "purpose": str(purpose or "user_decision"),
            "permission": str(permission or ""),
            "kind": kind,
            "status": "pending",
        }
        self.workflow.checkpoint = checkpoint
        self.workflow.current_stage = stage_id or self.workflow.current_stage
        self.workflow.execution_status = "waiting"
        stage = self.workflow.stage(stage_id)
        if stage:
            stage.status = "waiting"
        self.workflow.revision += 1
        self.workflow.updated_at = time.time()
        self.save()
        return checkpoint

    def resolve_decision(self, checkpoint_id: str, decision: str) -> Dict[str, Any]:
        if self.workflow is None or not self.workflow.checkpoint:
            raise ValueError("There is no pending decision")
        checkpoint = self.workflow.checkpoint
        if checkpoint.get("id") != checkpoint_id:
            raise ValueError("The pending decision has changed")
        if decision not in {"approved", "rejected"}:
            raise ValueError("Decision must be approved or rejected")
        checkpoint["status"] = decision
        stage = self.workflow.stage(str(checkpoint.get("stage_id") or ""))
        if stage:
            stage.status = "pending" if decision == "approved" else "failed"
        self.workflow.execution_status = "running" if decision == "approved" else "cancelled"
        self.workflow.revision += 1
        self.workflow.updated_at = time.time()
        self.save()
        return dict(checkpoint)

    def invalidate(self, project_version: int) -> None:
        if self.workflow is None:
            return
        self.workflow.base_project_version = int(project_version)
        for stage in self.workflow.stages:
            if stage.status == "completed":
                stage.reset()
        self.workflow.execution_status = "pending"
        self.workflow.revision += 1
        self.workflow.updated_at = time.time()
        self.save()

    def cancel(self) -> None:
        if self.workflow is None:
            return
        self.workflow.execution_status = "cancelled"
        self.workflow.checkpoint = {}
        self.workflow.revision += 1
        self.workflow.updated_at = time.time()
        self.save()

    def current_stage(self) -> Optional[DynamicStage]:
        return self.workflow.stage() if self.workflow else None

    def digest(self) -> Dict[str, Any]:
        if self.workflow is None:
            return {}
        workflow = self.workflow
        stage = workflow.stage()
        index = next(
            (i for i, item in enumerate(workflow.stages, 1) if item.id == workflow.current_stage),
            0,
        )
        checkpoint = dict(workflow.checkpoint or {})
        waiting = workflow.execution_status == "waiting" and checkpoint.get("status") == "pending"
        wait_kind = str(checkpoint.get("kind") or "approval") if waiting else ""
        return {
            "workflow_id": workflow.workflow_id,
            "task_type": workflow.task_type,
            "task_label": workflow.intent.summary or workflow.task_type,
            "execution_status": workflow.execution_status,
            "outcome": "passed" if workflow.execution_status == "completed" else "",
            "quality": "clean",
            "current_stage": workflow.current_stage,
            "stage_label": stage.objective if stage else "MainAgent planning",
            "stage_index": index,
            "stage_total": len(workflow.stages),
            "all_stage_total": len(workflow.stages),
            "revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "wait_kind": wait_kind,
            "waiting_reason": checkpoint.get("question") or "",
            "needs_confirmation": waiting and wait_kind == "approval",
            "can_confirm": waiting and wait_kind == "approval",
            "checkpoint_id": checkpoint.get("id") if waiting else "",
            "checkpoint_purpose": checkpoint.get("purpose") if waiting else "",
            "requested_permission": checkpoint.get("permission") if waiting else "",
            # Compatibility projection for the unchanged GTK presenter.
            "requested_effect": checkpoint.get("permission") if waiting else "",
            "interaction_request": (
                {
                    "id": checkpoint.get("id"),
                    "kind": "approval",
                    "status": "pending",
                    "purpose": checkpoint.get("purpose"),
                    "reason": checkpoint.get("question"),
                    "checkpoint_id": checkpoint.get("id"),
                    "allowed_actions": ["checkpoint_decision", "cancel_workflow"],
                }
                if waiting and wait_kind == "approval" else {}
            ),
            "intent_ir": {
                "raw_text": workflow.intent.raw_text,
                "summary": workflow.intent.summary,
                "slots": dict(workflow.intent.slots),
            },
            "stages": [
                {
                    "id": item.id,
                    "label": item.objective,
                    "objective": item.objective,
                    "target_agent": (
                        item.tasks[0].target_agent if len(item.tasks) == 1 else ""
                    ),
                    "tasks": [asdict(task) for task in item.tasks],
                    "status": item.status,
                    "execution_status": item.status,
                    "completion": list(item.expected_evidence),
                    "result_refs": list(item.result_refs),
                }
                for item in workflow.stages
            ],
        }

    def save(self) -> str:
        if self.workflow is None:
            return self.path
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            **self.workflow.to_dict(),
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return self.path

    def _load(self) -> Optional[DynamicWorkflow]:
        if not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if int(data.get("schema_version") or 0) not in {2, self.schema_version}:
                self.load_error = "Unsupported Workflow state version"
                return None
            workflow = DynamicWorkflow.from_dict(data)
            workflow.validate(self.known_agents)
            if workflow.execution_status == "running":
                workflow.execution_status = "pending"
                for stage in workflow.stages:
                    if stage.status == "running":
                        stage.reset()
            return workflow
        except (OSError, TypeError, ValueError) as exc:
            self.load_error = f"Workflow state could not be loaded: {exc}"
            return None


def missing_evidence(
    expected: Iterable[str],
    events: Iterable[Dict[str, Any]],
    artifacts: Dict[str, Any],
    metrics: Dict[str, Any],
) -> List[str]:
    """Return evidence requirements not satisfied by host-observed results."""
    successful = set()
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("ok") is False or str(payload.get("policy") or "") == "DENY":
            continue
        kind = str(event.get("kind") or "")
        if kind:
            successful.add(kind)
    aliases = {
        "validate_flowgraph": "validate",
        "run_simulation": "simulate",
    }
    missing = []
    for requirement in expected:
        name = str(requirement or "")
        if not name:
            continue
        if name.startswith("artifact:"):
            ok = bool(artifacts.get(name.split(":", 1)[1]))
        elif name.startswith("metric:"):
            ok = metrics.get(name.split(":", 1)[1]) is not None
        else:
            ok = name in successful or aliases.get(name) in successful
        if not ok:
            missing.append(name)
    return missing
