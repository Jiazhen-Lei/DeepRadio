"""Persistent shared facts exchanged by DeepRadio agents."""

from __future__ import annotations

import json
import os
import shutil
import time
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Decision:
    key: str
    value: Any
    source: str
    rationale: str = ""


@dataclass
class RadioSpec:
    goals: List[str] = field(default_factory=list)
    success_conditions: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    decisions: List[Decision] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)


@dataclass
class ProjectState:
    grc_path: str = ""
    flowgraph_version: int = 0
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Evidence:
    test: str
    observation: Dict[str, Any]
    project_version: int
    artifact: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class Claim:
    id: str
    statement: str
    layer: str
    status: str = "NotTested"
    evidence: List[Evidence] = field(default_factory=list)
    project_version: int = 0


@dataclass
class TaskCard:
    task_id: str
    loop_mode: str
    target_agent: str
    instruction: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    expected_claims: List[str] = field(default_factory=list)
    expected_results: List[str] = field(default_factory=list)
    workflow_id: str = ""
    stage_id: str = ""
    workflow_revision: int = 0
    base_project_version: int = 0

    def validate(self) -> None:
        if not all((self.task_id, self.workflow_id, self.stage_id, self.target_agent)):
            raise ValueError("TaskCard 缺少 task/workflow/stage/agent 标识")
        if self.workflow_revision < 1 or self.base_project_version < 0:
            raise ValueError("TaskCard Workflow/Project 版本非法")


@dataclass
class ResultEnvelope:
    task_id: str
    ok: bool
    produced_claims: List[str] = field(default_factory=list)
    proposed_changes: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    outcome: str = ""
    workflow_id: str = ""
    stage_id: str = ""
    workflow_revision: int = 0
    base_project_version: int = 0
    completion: Dict[str, bool] = field(default_factory=dict)
    invocations: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not all((self.task_id, self.workflow_id, self.stage_id)):
            raise ValueError("ResultEnvelope 缺少 task/workflow/stage 标识")
        if self.outcome not in ("passed", "failed", "inconclusive"):
            raise ValueError(f"ResultEnvelope outcome 非法: {self.outcome}")
        if self.workflow_revision < 1 or self.base_project_version < 0:
            raise ValueError("ResultEnvelope Workflow/Project 版本非法")
        if any(not isinstance(value, bool) for value in self.completion.values()):
            raise ValueError("ResultEnvelope completion 必须为布尔映射")


@dataclass
class Coordination:
    active_task: Optional[TaskCard] = None
    locked_constraints: List[str] = field(default_factory=list)
    pending_confirmations: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[str] = field(default_factory=list)


@dataclass
class SharedState:
    session_id: str = ""
    spec: RadioSpec = field(default_factory=RadioSpec)
    project: ProjectState = field(default_factory=ProjectState)
    claims: List[Claim] = field(default_factory=list)
    coordination: Coordination = field(default_factory=Coordination)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: str) -> str:
        if getattr(self, "_load_failed", False):
            raise OSError("拒绝覆盖无法读取的 SharedState；请先恢复损坏备份")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str, session_id: str = "") -> "SharedState":
        if not os.path.exists(path):
            return cls(session_id=session_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = _from_dict(json.load(handle))
            if session_id:
                state.session_id = session_id
            return state
        except (OSError, TypeError, ValueError, KeyError) as exc:
            backup = f"{path}.corrupt.{int(time.time())}"
            try:
                shutil.copy2(path, backup)
            except OSError:
                backup = ""
            warnings.warn(
                f"SharedState 无法读取，已保留损坏副本 {backup or path}: {exc}",
                RuntimeWarning,
            )
            state = cls(session_id=session_id)
            setattr(state, "_load_failed", True)
            setattr(state, "_corrupt_backup", backup)
            return state

    def spec_digest(self) -> Dict[str, Any]:
        decided = {item.key: item.value for item in self.spec.decisions}
        return {
            "goals": list(self.spec.goals),
            "success_conditions": list(self.spec.success_conditions),
            "constraints": dict(self.spec.constraints),
            "decisions": [asdict(item) for item in self.spec.decisions],
            "open_questions": list(self.spec.open_questions),
            "recipe": str(self.project.config.get("recipe") or ""),
            "modulation": str(
                self.project.config.get("modulation")
                or decided.get("modulation")
                or ""
            ),
            "channel": str(
                self.project.config.get("channel")
                or decided.get("channel")
                or ""
            ),
        }


def _from_dict(data: Dict[str, Any]) -> SharedState:
    spec_data = data.get("spec") or {}
    project_data = data.get("project") or {}
    coord_data = data.get("coordination") or {}
    active_data = coord_data.get("active_task")
    spec = RadioSpec(
        goals=list(spec_data.get("goals") or []),
        success_conditions=list(spec_data.get("success_conditions") or []),
        constraints=dict(spec_data.get("constraints") or {}),
        decisions=[Decision(**item) for item in spec_data.get("decisions") or []],
        open_questions=list(spec_data.get("open_questions") or []),
    )
    claims = []
    for item in data.get("claims") or []:
        evidence = [Evidence(**ev) for ev in item.get("evidence") or []]
        claims.append(
            Claim(
                id=item["id"],
                statement=item["statement"],
                layer=item["layer"],
                status=item.get("status", "NotTested"),
                evidence=evidence,
                project_version=int(item.get("project_version", 0)),
            )
        )
    coordination = Coordination(
        active_task=TaskCard(**active_data) if active_data else None,
        locked_constraints=list(coord_data.get("locked_constraints") or []),
        pending_confirmations=list(
            coord_data.get("pending_confirmations") or []
        ),
        snapshots=list(coord_data.get("snapshots") or []),
    )
    return SharedState(
        session_id=str(data.get("session_id") or ""),
        spec=spec,
        project=ProjectState(
            grc_path=str(project_data.get("grc_path") or ""),
            flowgraph_version=int(project_data.get("flowgraph_version", 0)),
            config=dict(project_data.get("config") or {}),
        ),
        claims=claims,
        coordination=coordination,
    )
