"""Small, serializable schemas for the Dynamic Workflow control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


EXECUTION_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "errored", "invalidated"}
)
OUTCOMES = frozenset({"passed", "failed", "inconclusive", "cancelled", ""})
DECISIONS = frozenset({"pending", "approved", "rejected"})


@dataclass
class WorkflowIntent:
    raw_text: str = ""
    turn_relation: str = "new_task"
    task_type: str = "END_TO_END_SIM"
    confidence: float = 0.0
    slots: Dict[str, Any] = field(default_factory=dict)
    missing_slots: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowIntent":
        return cls(
            raw_text=str(data.get("raw_text") or ""),
            turn_relation=str(data.get("turn_relation") or "new_task"),
            task_type=str(data.get("task_type") or "END_TO_END_SIM"),
            confidence=float(data.get("confidence", 0.0)),
            slots=dict(data.get("slots") or {}),
            missing_slots=list(data.get("missing_slots") or []),
        )


@dataclass
class Checkpoint:
    id: str
    decision_status: str = "pending"
    reason: str = ""

    def validate(self) -> None:
        if self.decision_status not in DECISIONS:
            raise ValueError(f"非法 Checkpoint decision_status: {self.decision_status}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Checkpoint":
        item = cls(
            id=str(data.get("id") or ""),
            decision_status=str(data.get("decision_status") or "pending"),
            reason=str(data.get("reason") or ""),
        )
        item.validate()
        return item


@dataclass
class Stage:
    id: str
    interaction: str = "autonomous"
    execution_status: str = "pending"
    attempt: int = 0
    max_attempts: int = 1
    outcome: str = ""
    recommended_agents: List[str] = field(default_factory=list)
    completion: List[str] = field(default_factory=list)
    transitions: Dict[str, str] = field(default_factory=dict)
    checkpoint: Optional[Checkpoint] = None
    result: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id:
            raise ValueError("Stage id 不能为空")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"非法 Stage execution_status: {self.execution_status}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"非法 Stage outcome: {self.outcome}")
        if self.attempt < 0 or self.max_attempts < 1:
            raise ValueError("Stage attempt/max_attempts 非法")
        if self.checkpoint:
            self.checkpoint.validate()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Stage":
        checkpoint = data.get("checkpoint")
        item = cls(
            id=str(data.get("id") or ""),
            interaction=str(data.get("interaction") or "autonomous"),
            execution_status=str(data.get("execution_status") or "pending"),
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 1)),
            outcome=str(data.get("outcome") or ""),
            recommended_agents=list(data.get("recommended_agents") or []),
            completion=list(data.get("completion") or []),
            transitions=dict(data.get("transitions") or data.get("on") or {}),
            checkpoint=Checkpoint.from_dict(checkpoint) if checkpoint else None,
            result=dict(data.get("result") or {}),
        )
        item.validate()
        return item


@dataclass
class Workflow:
    workflow_id: str
    task_type: str
    intent: WorkflowIntent
    stages: List[Stage]
    execution_status: str = "pending"
    outcome: str = ""
    revision: int = 1
    base_project_version: int = 0
    current_stage: str = ""
    schema_version: int = 1
    catalog_version: int = 1

    def validate(self) -> None:
        if not self.workflow_id or not self.task_type:
            raise ValueError("Workflow 标识和 Task Type 不能为空")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"非法 Workflow execution_status: {self.execution_status}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"非法 Workflow outcome: {self.outcome}")
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("Workflow Stage id 重复")
        if self.current_stage and self.current_stage not in ids:
            raise ValueError(f"current_stage 不存在: {self.current_stage}")
        for stage in self.stages:
            stage.validate()

    def stage(self, stage_id: str = "") -> Optional[Stage]:
        wanted = stage_id or self.current_stage
        return next((item for item in self.stages if item.id == wanted), None)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        item = cls(
            workflow_id=str(data.get("workflow_id") or ""),
            task_type=str(data.get("task_type") or ""),
            intent=WorkflowIntent.from_dict(data.get("intent") or {}),
            stages=[Stage.from_dict(stage) for stage in data.get("stages") or []],
            execution_status=str(data.get("execution_status") or "pending"),
            outcome=str(data.get("outcome") or ""),
            revision=int(data.get("revision", 1)),
            base_project_version=int(data.get("base_project_version", 0)),
            current_stage=str(data.get("current_stage") or ""),
            schema_version=int(data.get("schema_version", 1)),
            catalog_version=int(data.get("catalog_version", 1)),
        )
        item.validate()
        return item
