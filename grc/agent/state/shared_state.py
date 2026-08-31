"""Persistent shared facts exchanged by DeepRadio agents."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .intent_state import SharedIntent


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
class WorkflowDecision:
    decision_id: str
    key: str
    value: Any
    source: str
    effect_level: str = "READ"
    workflow_id: str = ""
    stage_id: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class RuntimeState:
    current_node: str = ""
    status: str = "planned"
    requested_effect: str = "READ"
    granted_effects: List[str] = field(default_factory=list)
    blocker: Dict[str, Any] = field(default_factory=dict)
    operations: List[Dict[str, Any]] = field(default_factory=list)
    quality: str = "clean"
    warnings: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ArtifactRecord:
    artifact_id: str
    role: str
    path: str
    sha256: str
    size: int = 0
    producer: str = ""
    workflow_revision: int = 0
    project_version: int = 0


@dataclass
class Evidence:
    test: str
    observation: Dict[str, Any]
    project_version: int
    artifact: str = ""
    measurement_id: str = ""
    evidence_grade: str = "system_verified"
    ts: float = field(default_factory=time.time)


@dataclass
class Claim:
    id: str
    statement: str
    layer: str
    status: str = "NotTested"
    evidence: List[Evidence] = field(default_factory=list)
    project_version: int = 0
    producer: str = ""
    measurement_id: str = ""
    stale_reason: str = ""
    intent_id: str = ""
    intent_revision: int = 0


@dataclass
class MeasurementRun:
    measurement_id: str
    metric: str
    project_version: int
    run_id: str = ""
    probe_ids: List[str] = field(default_factory=list)
    sample_range: Dict[str, Any] = field(default_factory=dict)
    algorithm: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    artifact_ids: List[str] = field(default_factory=list)


@dataclass
class DiagnosisSnapshot:
    """Latest scoped diagnosis, bound to one intent revision."""

    intent_id: str = ""
    intent_revision: int = 0
    requested_dimensions: List[str] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    report_path: str = ""
    created_at: float = 0.0


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
    intent_id: str = ""
    intent_revision: int = 0
    intent_hash: str = ""

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
    quality: str = "clean"
    evidence_grade: str = ""
    workflow_id: str = ""
    stage_id: str = ""
    workflow_revision: int = 0
    base_project_version: int = 0
    completion: Dict[str, bool] = field(default_factory=dict)
    invocations: List[Dict[str, Any]] = field(default_factory=list)
    acceptance: Dict[str, Any] = field(default_factory=dict)
    intent_id: str = ""
    intent_revision: int = 0
    intent_hash: str = ""

    def validate(self) -> None:
        if not all((self.task_id, self.workflow_id, self.stage_id)):
            raise ValueError("ResultEnvelope 缺少 task/workflow/stage 标识")
        if self.outcome not in ("passed", "failed", "inconclusive"):
            raise ValueError(f"ResultEnvelope outcome 非法: {self.outcome}")
        if self.quality not in ("clean", "warning", "failed"):
            raise ValueError(f"ResultEnvelope quality 非法: {self.quality}")
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
    intent: SharedIntent = field(default_factory=SharedIntent)
    spec: RadioSpec = field(default_factory=RadioSpec)
    project: ProjectState = field(default_factory=ProjectState)
    claims: List[Claim] = field(default_factory=list)
    coordination: Coordination = field(default_factory=Coordination)
    decisions: List[WorkflowDecision] = field(default_factory=list)
    runtime: RuntimeState = field(default_factory=RuntimeState)
    artifacts: List[ArtifactRecord] = field(default_factory=list)
    measurements: List[MeasurementRun] = field(default_factory=list)
    diagnosis: DiagnosisSnapshot = field(default_factory=DiagnosisSnapshot)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: str) -> str:
        if getattr(self, "_load_failed", False):
            raise OSError("拒绝覆盖无法读取的 SharedState；请先恢复损坏备份")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        payload = relativize_tree_paths(parent, self.to_dict())
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        if self.intent.intent_id:
            specification_path = os.path.join(parent, "radio_specification.json")
            specification_payload = {
                "schema_version": 1,
                "intent_id": self.intent.intent_id,
                "revision": self.intent.revision,
                "status": self.intent.status,
                "semantic_hash": self.intent.semantic_hash,
                "specification": self.intent.specification.to_dict(),
            }
            specification_tmp = f"{specification_path}.tmp"
            with open(specification_tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    specification_payload, handle, ensure_ascii=False, indent=2
                )
            os.replace(specification_tmp, specification_path)
        return path

    @classmethod
    def load(cls, path: str, session_id: str = "") -> "SharedState":
        if not os.path.exists(path):
            return cls(session_id=session_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            payload = resolve_tree_paths(os.path.dirname(os.path.abspath(path)), payload)
            state = _from_dict(payload)
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
        decision_sources = {item.key: item.source for item in self.spec.decisions}
        config = self.project.config
        shared = self.intent
        active_intent = bool(
            shared.task_type or shared.parameters or shared.status != "idle"
        )
        parameters = dict(shared.parameters or {}) if active_intent else {}
        sources = dict(shared.parameter_sources or {}) if active_intent else {}

        def value(key: str, *fallback_keys: str) -> Any:
            if key in parameters and parameters.get(key) not in (None, "", []):
                return parameters.get(key)
            for fallback in fallback_keys:
                if fallback in parameters and parameters.get(fallback) not in (None, "", []):
                    return parameters.get(fallback)
            if active_intent:
                return None
            for candidate in (key,) + fallback_keys:
                if config.get(candidate) not in (None, "", []):
                    return config.get(candidate)
                if decided.get(candidate) not in (None, "", []):
                    return decided.get(candidate)
            return None

        def source(key: str, *fallback_keys: str) -> str:
            for candidate in (key,) + fallback_keys:
                if sources.get(candidate):
                    return str(sources[candidate])
                if not active_intent and decision_sources.get(candidate):
                    return str(decision_sources[candidate])
            return ""

        protocol = str(
            value("protocol") or ""
        )
        hardware = str(
            value("hardware") or ""
        )
        local_name = str(
            value("local_name") or ""
        )
        ble_channel = value("ble_channel")
        if ble_channel is None:
            channels = value("advertising_channels") or []
            ble_channel = channels[0] if channels else ""
        carrier = value("carrier_frequency")
        duration = (
            value("max_duration_seconds", "duration_seconds")
            or ""
        )
        goals = list(shared.goals or self.spec.goals) if active_intent else list(self.spec.goals)
        success_conditions = list(
            shared.success_criteria or parameters.get("success_conditions") or []
        ) if active_intent else list(self.spec.success_conditions)
        digest = {
            "goals": goals,
            "success_conditions": success_conditions,
            "constraints": dict(shared.constraints or self.spec.constraints)
            if active_intent else dict(self.spec.constraints),
            "decisions": [asdict(item) for item in self.spec.decisions],
            "open_questions": list(shared.missing_fields or self.spec.open_questions)
            if active_intent else list(self.spec.open_questions),
            "recipe": str(value("recipe") or ""),
            "modulation": str(
                value("modulation") or ""
            ),
            "channel": str(
                value("channel") or ""
            ),
            "protocol": protocol,
            "hardware": hardware,
            "local_name": local_name,
            "ble_channel": ble_channel,
            "carrier_frequency": carrier,
            "sample_rate": value("sample_rate"),
            "direction": str(value("direction") or ""),
            "signal_source_scope": str(
                value("signal_source_scope") or ""
            ),
            "rf_armed": bool(config.get("rf_armed")),
            "max_duration_seconds": duration,
            "spec_kind": "ble" if protocol.lower() == "ble" else "link",
            "intent_id": shared.intent_id if active_intent else "",
            "intent_revision": shared.revision if active_intent else 0,
            "intent_status": shared.status if active_intent else "idle",
            "parameter_sources": sources,
        }
        specification = shared.specification
        if active_intent and not specification.fields:
            from ..knowledge.spec_requirements import resolve_specification

            specification = resolve_specification(
                task_type=shared.task_type,
                capabilities=shared.capabilities,
                slots=parameters,
                slot_sources=sources,
                missing_fields=shared.missing_fields,
                validation_errors=shared.validation_errors,
                goals=shared.goals,
                raw_text=shared.raw_text,
            )
            shared.specification = specification
        rows = []
        for item in specification.fields:
            from ..knowledge.spec_requirements import question_for

            unresolved = item.value in (None, "", []) or item.source == "unresolved"
            question = question_for(item.key)
            rows.append({
                "key": item.key,
                "label": item.label or item.key,
                "value": item.value,
                "display_value": "" if unresolved else _display_spec_value(
                    item.key, item.value
                ),
                "source": item.source,
                "unresolved": unresolved,
                "needs_confirmation": item.requirement == "required" and not item.confirmed,
                "editable": False,
                "locked": True,
                "confirmed": bool(item.confirmed),
                "requirement": item.requirement,
                "reason": item.reason,
                "depends_on": list(item.depends_on),
                "choices": list(question.get("choices") or []),
                "allow_custom": bool(question.get("allow_custom", True)),
            })
        digest["radio_specification"] = rows
        digest["specification_profiles"] = list(specification.profile_refs)
        digest["blocking_questions"] = list(specification.blocking_questions)
        digest["optional_prompts"] = list(specification.optional_prompts)
        digest["summary"] = _spec_summary_line(digest)
        if protocol.lower() == "ble" and duration not in ("", None):
            digest["duration_note"] = (
                f"Maximum duration: {duration:g} seconds; OTA confirmation or cancellation stops it early."
                if isinstance(duration, (int, float))
                else f"Maximum duration: {duration} seconds; OTA confirmation or cancellation stops it early."
            )
        return digest


def _display_spec_value(key: str, value: Any) -> str:
    if key == "hardware":
        return _hardware_label(str(value or ""))
    if key in {"carrier_frequency", "bandwidth"}:
        return _format_hz(value)
    if key == "sample_rate":
        return _format_rate(value)
    if key in {"duration_seconds", "max_duration_seconds", "capture_duration"}:
        return f"{value:g} s" if isinstance(value, (int, float)) else str(value)
    if key == "advertising_channels":
        values = value if isinstance(value, list) else [value]
        return ", ".join(f"CH{item}" for item in values)
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    if key in {"protocol", "modulation", "channel"}:
        return str(value or "").upper()
    return str(value)


def _format_hz(value: Any) -> str:
    try:
        hz = float(value)
    except (TypeError, ValueError):
        return ""
    if hz >= 1e9:
        text = f"{hz / 1e9:.3f}".rstrip("0").rstrip(".")
        return f"{text} GHz"
    if hz >= 1e6:
        text = f"{hz / 1e6:.3f}".rstrip("0").rstrip(".")
        return f"{text} MHz"
    if hz >= 1e3:
        text = f"{hz / 1e3:.3f}".rstrip("0").rstrip(".")
        return f"{text} kHz"
    return f"{hz:g} Hz"


def _hardware_label(hardware: str) -> str:
    key = str(hardware or "").strip().lower()
    labels = {
        "pluto": "PlutoSDR",
        "plutosdr": "PlutoSDR",
        "b210": "B210",
        "b200": "B210",
        "usrp": "USRP",
        "usrp_b210": "B210",
    }
    return labels.get(key, hardware or "")


def _format_rate(value: Any) -> str:
    text = _format_hz(value)
    return text.replace("Hz", "sps") if text else ""


def _spec_summary_line(digest: Dict[str, Any]) -> str:
    if str(digest.get("protocol") or "").lower() == "ble":
        channel = digest.get("ble_channel")
        channel_text = f"CH{channel}" if channel not in ("", None) else "CH?"
        parts = [
            "BLE 1M",
            channel_text,
            _format_hz(digest.get("carrier_frequency")),
            _hardware_label(str(digest.get("hardware") or "")),
        ]
        local_name = str(digest.get("local_name") or "")
        if local_name:
            parts.append(f"Local Name={local_name}")
        return " · ".join(part for part in parts if part)
    hardware = str(digest.get("hardware") or "")
    recipe = str(digest.get("recipe") or "")
    source_scope = str(digest.get("signal_source_scope") or "")
    if source_scope == "live_device":
        parts = [_hardware_label(hardware) or "SDR", "RX", "live device"]
        for value in (
            _format_hz(digest.get("carrier_frequency")),
            _format_rate(digest.get("sample_rate")),
        ):
            if value:
                parts.append(value)
        return " · ".join(parts)
    if source_scope == "current_project_offline":
        return " · ".join(
            item for item in ("Observe", "current project offline", recipe)
            if item
        )
    if source_scope == "generated_fixture":
        modulation = str(digest.get("modulation") or "").upper()
        return " · ".join(
            item for item in (modulation, "generated test fixture", recipe)
            if item
        )
    if hardware and not recipe:
        parts = [_hardware_label(hardware)]
        direction = str(digest.get("direction") or "").upper()
        if direction:
            parts.append(direction)
        hz = _format_hz(digest.get("carrier_frequency"))
        if hz:
            parts.append(hz)
        rate = _format_rate(digest.get("sample_rate"))
        if rate:
            parts.append(rate)
        parts.append("RF armed" if digest.get("rf_armed") else "sink unarmed")
        return " · ".join(part for part in parts if part)
    parts = [
        str(digest.get("modulation") or "").upper(),
        str(digest.get("channel") or "").upper(),
        recipe,
    ]
    return " → ".join(item for item in parts if item) or "Not extracted"


def _from_dict(data: Dict[str, Any]) -> SharedState:
    intent_data = data.get("intent") or {}
    spec_data = data.get("spec") or {}
    project_data = data.get("project") or {}
    coord_data = data.get("coordination") or {}
    runtime_data = data.get("runtime") or {}
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
        evidence = [
            Evidence(
                test=str(ev.get("test") or ""),
                observation=dict(ev.get("observation") or {}),
                project_version=int(ev.get("project_version", 0) or 0),
                artifact=str(ev.get("artifact") or ""),
                measurement_id=str(ev.get("measurement_id") or ""),
                evidence_grade=str(
                    ev.get("evidence_grade") or "system_verified"
                ),
                ts=float(ev.get("ts") or time.time()),
            )
            for ev in item.get("evidence") or []
            if isinstance(ev, dict)
        ]
        claims.append(
            Claim(
                id=item["id"],
                statement=item["statement"],
                layer=item["layer"],
                status=item.get("status", "NotTested"),
                evidence=evidence,
                project_version=int(item.get("project_version", 0)),
                producer=str(item.get("producer") or ""),
                measurement_id=str(item.get("measurement_id") or ""),
                stale_reason=str(item.get("stale_reason") or ""),
                intent_id=str(item.get("intent_id") or ""),
                intent_revision=int(item.get("intent_revision", 0) or 0),
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
        intent=SharedIntent.from_dict(intent_data),
        spec=spec,
        project=ProjectState(
            grc_path=str(project_data.get("grc_path") or ""),
            flowgraph_version=int(project_data.get("flowgraph_version", 0)),
            config=dict(project_data.get("config") or {}),
        ),
        claims=claims,
        coordination=coordination,
        decisions=[
            WorkflowDecision(**item)
            for item in data.get("decisions") or []
            if isinstance(item, dict) and item.get("decision_id")
        ],
        runtime=RuntimeState(
            current_node=str(runtime_data.get("current_node") or ""),
            status=str(runtime_data.get("status") or "planned"),
            requested_effect=str(
                runtime_data.get("requested_effect") or "READ"
            ),
            granted_effects=list(runtime_data.get("granted_effects") or []),
            blocker=dict(runtime_data.get("blocker") or {}),
            operations=list(runtime_data.get("operations") or []),
            quality=str(runtime_data.get("quality") or "clean"),
            warnings=list(runtime_data.get("warnings") or []),
        ),
        artifacts=[
            ArtifactRecord(**item)
            for item in data.get("artifacts") or []
            if isinstance(item, dict) and item.get("artifact_id")
        ],
        measurements=[
            MeasurementRun(
                measurement_id=str(item.get("measurement_id") or ""),
                metric=str(item.get("metric") or ""),
                project_version=int(item.get("project_version", 0) or 0),
                run_id=str(item.get("run_id") or ""),
                probe_ids=list(item.get("probe_ids") or []),
                sample_range=dict(item.get("sample_range") or {}),
                algorithm=dict(item.get("algorithm") or {}),
                result=dict(item.get("result") or {}),
                artifact_ids=list(item.get("artifact_ids") or []),
            )
            for item in data.get("measurements") or []
            if isinstance(item, dict) and item.get("measurement_id")
        ],
        diagnosis=DiagnosisSnapshot(
            intent_id=str((data.get("diagnosis") or {}).get("intent_id") or ""),
            intent_revision=int(
                (data.get("diagnosis") or {}).get("intent_revision", 0) or 0
            ),
            requested_dimensions=list(
                (data.get("diagnosis") or {}).get("requested_dimensions") or []
            ),
            findings=list((data.get("diagnosis") or {}).get("findings") or []),
            summary=dict((data.get("diagnosis") or {}).get("summary") or {}),
            report_path=str(
                (data.get("diagnosis") or {}).get("report_path") or ""
            ),
            created_at=float(
                (data.get("diagnosis") or {}).get("created_at", 0.0) or 0.0
            ),
        ),
    )


_PATH_KEYS = frozenset(
    {
        "path",
        "grc_path",
        "program",
        "log_path",
        "status_path",
        "artifact",
        "rf_armed_path",
        "waveform_path",
        "runtime_program",
        "runtime_log",
        "runtime_status",
    }
)
_RELATIVE_PREFIXES = ("final/", "work/", "snapshots/", "final\\", "work\\", "snapshots\\")


def is_path_key(key: str) -> bool:
    return key in _PATH_KEYS or key.endswith("_path") or key.endswith("_paths")


def looks_like_session_path(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if os.path.isabs(value):
        return True
    return value.startswith(_RELATIVE_PREFIXES)


def to_relpath(root: str, path: str) -> str:
    if not path:
        return path
    abs_root = os.path.abspath(root)
    abs_path = os.path.abspath(path) if os.path.isabs(path) else os.path.normpath(
        os.path.join(abs_root, path)
    )
    prefix = abs_root + os.sep
    if abs_path == abs_root:
        return "."
    if abs_path.startswith(prefix):
        return os.path.relpath(abs_path, abs_root).replace(os.sep, "/")
    return path


def to_abspath(root: str, path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.normpath(os.path.join(os.path.abspath(root), path))


def relativize_tree_paths(root: str, payload: Any) -> Any:
    return _convert_tree(root, payload, to_relative=True)


def resolve_tree_paths(root: str, payload: Any) -> Any:
    return _convert_tree(root, payload, to_relative=False)


def rewrite_root_prefix(payload: Any, old_root: str, new_root: str) -> Any:
    old = os.path.abspath(old_root)
    new = os.path.abspath(new_root)
    old_prefix = old + os.sep

    def convert(value: Any) -> Any:
        if isinstance(value, str):
            abs_value = os.path.abspath(value) if os.path.isabs(value) else ""
            if abs_value == old:
                return new
            if abs_value.startswith(old_prefix):
                return os.path.join(new, os.path.relpath(abs_value, old))
            if value.startswith(old_prefix):
                return new + value[len(old):]
            return value
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(payload)


def _convert_tree(root: str, payload: Any, *, to_relative: bool) -> Any:
    if isinstance(payload, dict):
        converted = {}
        for key, value in payload.items():
            if key == "artifacts" and isinstance(value, dict):
                converted[key] = {
                    inner_key: _convert_path(root, inner_value, to_relative)
                    if isinstance(inner_value, str) and looks_like_session_path(inner_value)
                    else _convert_tree(root, inner_value, to_relative=to_relative)
                    for inner_key, inner_value in value.items()
                }
                continue
            if is_path_key(key) and isinstance(value, str):
                converted[key] = _convert_path(root, value, to_relative)
                continue
            if is_path_key(key) and isinstance(value, list):
                converted[key] = [
                    _convert_path(root, item, to_relative)
                    if isinstance(item, str)
                    else _convert_tree(root, item, to_relative=to_relative)
                    for item in value
                ]
                continue
            converted[key] = _convert_tree(root, value, to_relative=to_relative)
        return converted
    if isinstance(payload, list):
        return [
            _convert_tree(root, item, to_relative=to_relative) for item in payload
        ]
    return payload


def _convert_path(root: str, path: str, to_relative: bool) -> str:
    if not path:
        return path
    if to_relative:
        return to_relpath(root, path)
    if os.path.isabs(path) or looks_like_session_path(path):
        return to_abspath(root, path)
    return path


def current_measurement_id(ctx: Any) -> str:
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        return f"meas-{uuid.uuid4().hex[:10]}"
    mid = str(extra.get("measurement_id") or "")
    if not mid:
        mid = f"meas-{uuid.uuid4().hex[:10]}"
        extra["measurement_id"] = mid
    return mid


def attach_measurement(
    ctx: Any,
    *,
    metric: str,
    result: Optional[Dict[str, Any]] = None,
    probe_ids: Optional[List[str]] = None,
    algorithm: Optional[Dict[str, Any]] = None,
    artifact: str = "",
) -> str:
    """Bind a metric/plot/claim to one MeasurementRun identity for this round."""
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        extra = {}
    mid = current_measurement_id(ctx)
    state = extra.get("state")
    payload = dict(result or {})
    probes = [item for item in list(probe_ids or []) if item]
    record = None
    if state is not None:
        records = getattr(state, "measurements", None)
        if records is None:
            state.measurements = []
            records = state.measurements
        record = next(
            (item for item in records if item.measurement_id == mid),
            None,
        )
        if record is None:
            record = MeasurementRun(
                measurement_id=mid,
                metric=str(metric or ""),
                project_version=int(
                    getattr(getattr(state, "project", None), "flowgraph_version", 0) or 0
                ),
                run_id=str(extra.get("run_id") or payload.get("run_id") or ""),
            )
            records.append(record)
        if metric and not record.metric:
            record.metric = str(metric)
        if payload:
            record.result.update(payload)
        for probe in probes:
            if probe not in record.probe_ids:
                record.probe_ids.append(probe)
        if algorithm:
            record.algorithm.update(dict(algorithm))
        if artifact and artifact not in record.artifact_ids:
            record.artifact_ids.append(artifact)
    extra.setdefault("measurement_ids", [])
    if mid not in extra["measurement_ids"]:
        extra["measurement_ids"].append(mid)
    extra["measurement_id"] = mid
    return mid
