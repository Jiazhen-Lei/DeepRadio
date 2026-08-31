"""Small, serializable schemas for the Dynamic Workflow control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


EXECUTION_STATUSES = frozenset(
    {"pending", "running", "waiting", "completed", "errored", "invalidated"}
)
OUTCOMES = frozenset({"passed", "failed", "inconclusive", "cancelled", ""})
DECISIONS = frozenset({"pending", "approved", "rejected"})
TURN_RELATIONS = frozenset(
    {
        "new_task", "answer", "adjustment", "feedback",
        "approval", "rejection", "cancel", "question",
    }
)
EFFECT_LEVELS = frozenset(
    {"READ", "ARTIFACT_WRITE", "DEVICE_READ", "DEVICE_CONFIG", "RF_RUN"}
)
QUALITY_LEVELS = frozenset({"clean", "warning", "failed"})


@dataclass
class WorkflowIntent:
    raw_text: str = ""
    turn_relation: str = "new_task"
    task_type: str = "END_TO_END_SIM"
    confidence: float = 0.0
    slots: Dict[str, Any] = field(default_factory=dict)
    missing_slots: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    slot_sources: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    validation_errors: List[str] = field(default_factory=list)
    goals: List[str] = field(default_factory=list)
    requested_operations: List[str] = field(default_factory=list)
    desired_artifacts: List[str] = field(default_factory=list)
    evidence_requirements: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    forbidden_effects: List[str] = field(default_factory=list)
    decision_boundaries: List[str] = field(default_factory=list)
    stop_conditions: List[str] = field(default_factory=list)
    entities: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.turn_relation not in TURN_RELATIONS:
            raise ValueError(f"非法 turn_relation: {self.turn_relation}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Intent confidence 必须位于 0~1")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowIntent":
        item = cls(
            raw_text=str(data.get("raw_text") or ""),
            turn_relation=str(data.get("turn_relation") or "new_task"),
            task_type=str(data.get("task_type") or "END_TO_END_SIM"),
            confidence=float(data.get("confidence", 0.0)),
            slots=dict(data.get("slots") or {}),
            missing_slots=list(data.get("missing_slots") or []),
            capabilities=list(data.get("capabilities") or []),
            slot_sources=dict(data.get("slot_sources") or {}),
            context=dict(data.get("context") or {}),
            validation_errors=list(data.get("validation_errors") or []),
            goals=list(data.get("goals") or []),
            requested_operations=list(data.get("requested_operations") or []),
            desired_artifacts=list(data.get("desired_artifacts") or []),
            evidence_requirements=list(data.get("evidence_requirements") or []),
            constraints=dict(data.get("constraints") or {}),
            forbidden_effects=list(data.get("forbidden_effects") or []),
            decision_boundaries=list(data.get("decision_boundaries") or []),
            stop_conditions=list(data.get("stop_conditions") or []),
            entities=dict(data.get("entities") or {}),
        )
        item.validate()
        return item


@dataclass
class Checkpoint:
    id: str
    purpose: str = "generic_approval"
    decision_status: str = "pending"
    reason: str = ""
    action: str = ""
    payload_ref: str = ""
    resume_stage: bool = False
    requested_effect: str = "READ"
    blocker: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.decision_status not in DECISIONS:
            raise ValueError(f"非法 Checkpoint decision_status: {self.decision_status}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Checkpoint":
        item = cls(
            id=str(data.get("id") or ""),
            purpose=str(data.get("purpose") or "generic_approval"),
            decision_status=str(data.get("decision_status") or "pending"),
            reason=str(data.get("reason") or ""),
            action=str(data.get("action") or ""),
            payload_ref=str(data.get("payload_ref") or ""),
            resume_stage=bool(data.get("resume_stage", False)),
            requested_effect=str(data.get("requested_effect") or "READ"),
            blocker=dict(data.get("blocker") or {}),
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
    result_history: List[Dict[str, Any]] = field(default_factory=list)
    when: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    resume_pending: bool = False
    resume_from: str = ""
    effect_level: str = "READ"
    idempotent: bool = True
    safety_finalizer: bool = False
    objective: str = ""
    requires: List[str] = field(default_factory=list)
    produces: List[str] = field(default_factory=list)
    success_predicates: List[str] = field(default_factory=list)
    unbound_predicates: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id:
            raise ValueError("Stage id 不能为空")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"非法 Stage execution_status: {self.execution_status}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"非法 Stage outcome: {self.outcome}")
        if self.attempt < 0 or self.max_attempts < 1:
            raise ValueError("Stage attempt/max_attempts 非法")
        if self.effect_level not in EFFECT_LEVELS:
            raise ValueError(f"非法 Stage effect_level: {self.effect_level}")
        if self.checkpoint:
            self.checkpoint.validate()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Stage":
        checkpoint = data.get("checkpoint")
        when = data.get("when") or {}
        if isinstance(when, list):
            when = {"any": when}
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
            result_history=list(data.get("result_history") or []),
            when=dict(when),
            depends_on=list(data.get("depends_on") or []),
            resume_pending=bool(data.get("resume_pending", False)),
            resume_from=str(data.get("resume_from") or ""),
            effect_level=str(data.get("effect_level") or data.get("effect") or "READ"),
            idempotent=bool(data.get("idempotent", True)),
            safety_finalizer=bool(data.get("safety_finalizer", False)),
            objective=str(data.get("objective") or ""),
            requires=list(data.get("requires") or []),
            produces=list(data.get("produces") or []),
            success_predicates=list(data.get("success_predicates") or []),
            unbound_predicates=list(data.get("unbound_predicates") or []),
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
    quality: str = "clean"
    revision: int = 1
    base_project_version: int = 0
    current_stage: str = ""
    schema_version: int = 1
    catalog_version: int = 1
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    deferred_plan: List[Dict[str, Any]] = field(default_factory=list)
    compiled_plan: List[Dict[str, Any]] = field(default_factory=list)
    #: Summaries of superseded workflows, kept visible as Previous Attempts.
    previous_attempts: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.workflow_id or not self.task_type:
            raise ValueError("Workflow 标识和 Task Type 不能为空")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"非法 Workflow execution_status: {self.execution_status}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"非法 Workflow outcome: {self.outcome}")
        if self.quality not in QUALITY_LEVELS:
            raise ValueError(f"非法 Workflow quality: {self.quality}")
        if self.revision < 1 or self.base_project_version < 0:
            raise ValueError("Workflow revision/base_project_version 非法")
        self.intent.validate()
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
            quality=str(data.get("quality") or "clean"),
            revision=int(data.get("revision", 1)),
            base_project_version=int(data.get("base_project_version", 0)),
            current_stage=str(data.get("current_stage") or ""),
            schema_version=int(data.get("schema_version", 1)),
            catalog_version=int(data.get("catalog_version", 1)),
            decisions=list(data.get("decisions") or []),
            deferred_plan=list(data.get("deferred_plan") or []),
            compiled_plan=list(data.get("compiled_plan") or []),
            previous_attempts=list(data.get("previous_attempts") or []),
        )
        item.validate()
        return item
