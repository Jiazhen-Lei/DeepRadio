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
    acceptance: Dict[str, Any] = field(default_factory=dict)

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
        payload = relativize_tree_paths(parent, self.to_dict())
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
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
        config = self.project.config
        protocol = str(
            config.get("protocol") or decided.get("protocol") or ""
        )
        hardware = str(
            config.get("hardware") or decided.get("hardware") or ""
        )
        local_name = str(
            config.get("local_name") or decided.get("local_name") or ""
        )
        ble_channel = config.get("ble_channel")
        if ble_channel is None:
            channels = config.get("advertising_channels") or []
            ble_channel = channels[0] if channels else ""
        carrier = config.get("carrier_frequency")
        duration = (
            config.get("max_duration_seconds")
            or config.get("duration_seconds")
            or ""
        )
        digest = {
            "goals": list(self.spec.goals),
            "success_conditions": list(self.spec.success_conditions),
            "constraints": dict(self.spec.constraints),
            "decisions": [asdict(item) for item in self.spec.decisions],
            "open_questions": list(self.spec.open_questions),
            "recipe": str(config.get("recipe") or ""),
            "modulation": str(
                config.get("modulation") or decided.get("modulation") or ""
            ),
            "channel": str(
                config.get("channel") or decided.get("channel") or ""
            ),
            "protocol": protocol,
            "hardware": hardware,
            "local_name": local_name,
            "ble_channel": ble_channel,
            "carrier_frequency": carrier,
            "sample_rate": config.get("sample_rate"),
            "direction": str(config.get("direction") or ""),
            "rf_armed": bool(config.get("rf_armed")),
            "max_duration_seconds": duration,
            "spec_kind": "ble" if protocol.lower() == "ble" else "link",
        }
        digest["summary"] = _spec_summary_line(digest)
        if protocol.lower() == "ble" and duration not in ("", None):
            digest["duration_note"] = (
                f"最大时长 {duration:g} 秒；OTA 确认或取消后会提前停止"
                if isinstance(duration, (int, float))
                else f"最大时长 {duration} 秒；OTA 确认或取消后会提前停止"
            )
        return digest


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
        parts.append("RF armed" if digest.get("rf_armed") else "sink 未 arm")
        return " · ".join(part for part in parts if part)
    modulation = str(digest.get("modulation") or "").upper() or "?"
    channel = str(digest.get("channel") or "").upper() or "?"
    recipe_name = recipe or "?"
    return f"{modulation} → {channel} → {recipe_name}"


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
        return os.path.relpath(abs_path, abs_root)
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
