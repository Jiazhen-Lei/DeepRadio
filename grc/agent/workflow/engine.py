"""Deterministic single-workflow, serial-stage state machine."""

from __future__ import annotations

import hashlib
import logging
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .completion import KNOWN_COMPLETIONS
from .schema import Checkpoint, Stage, Workflow, WorkflowIntent
from ..tools.hardware_profiles import resolve_hardware_profile
from ..tools.state_tools import is_read_only_request

logger = logging.getLogger(__name__)


_TERMINALS = {"completed", "errored"}
_APPROVE = frozenset({"确认", "同意", "继续", "确认执行", "确认修改", "approve"})
_REJECT_HINTS = ("取消", "拒绝", "不同意", "不要执行", "cancel")
_KNOWN_AGENTS = frozenset(
    {
        "spec_agent",
        "radio_design_agent",
        "flowgraph_agent",
        "verification_agent",
        "diagnosis_agent",
        "hardware_agent",
        "protocol_agent",
    }
)
_TERMINAL_TARGETS = frozenset({"completed", "cancelled", "stop", "waiting_user"})
_TASK_TYPES = frozenset(
    {
        "END_TO_END_SIM",
        "TX_BUILD",
        "RX_BUILD",
        "DIAGNOSE",
        "MODIFY_PROJECT",
        "OBSERVE",
        "HARDWARE_CONFIGURE",
    }
)
_TASK_LABELS = {
    "END_TO_END_SIM": "端到端仿真",
    "TX_BUILD": "构建发射链路",
    "RX_BUILD": "构建接收链路",
    "DIAGNOSE": "诊断工程",
    "MODIFY_PROJECT": "修改已有工程",
    "OBSERVE": "观测工程",
    "HARDWARE_CONFIGURE": "配置 SDR",
}
_STAGE_LABELS = {
    "spec_alignment": "规格对齐",
    "rx_spec_alignment": "接收规格对齐",
    "build_and_verify": "构建与验证",
    "tx_build_and_validate": "发射机构建与校验",
    "rx_build_and_verify": "接收机构建与验证",
    "inspect_and_diagnose": "检查与诊断",
    "repair_confirmation": "修复确认",
    "repair_and_verify": "修复与重验",
    "inspect_and_plan": "检查与规划",
    "change_confirmation": "变更确认",
    "apply_and_verify": "应用与重验",
    "inspect_and_measure": "检查与测量",
    "hardware_precheck": "硬件预检",
    "hardware_confirmation": "硬件确认",
    "configure_and_check": "配置与检查",
    "protocol_spec_alignment": "BLE 规格对齐",
    "build_ble_advertiser": "构建 BLE 广播",
    "offline_protocol_verify": "BLE 离线协议校验",
    "discover_and_probe_device": "发现并探测所选 SDR",
    "rf_plan_confirmation": "RF 计划确认",
    "configure_device": "配置设备参数",
    "transmit_bounded": "有限时长发射",
    "over_air_verification": "LightBlue 空口验收",
    "stop_and_finalize": "停止并完成审计",
    "discover_and_probe_hardware": "发现并探测硬件",
    "run_bounded": "有限时长运行",
    "runtime_observation": "运行结果确认",
    "stop_runtime": "停止硬件运行",
}

_ALIGNMENT_STAGES = frozenset(
    {"spec_alignment", "rx_spec_alignment", "protocol_spec_alignment"}
)
_BUILD_HINTS = (
    "构建", "生成", "创建", "做一个", "搭一个", "搭建", "build", "create",
)
_MODIFY_HINTS = ("修改", "改成", "改为", "换成", "调参", "设为", "modify", "change")
_OBSERVE_HINTS = ("观察", "查看", "频谱", "星座图", "眼图", "measure", "spectrum")
_DEVICE_HINTS = (
    "usrp", "b210", "b200", "hackrf", "pluto", "limesdr", "adalm", "ettus",
)
_HW_CAPABILITIES = ("hardware_configure", "hardware_runtime", "deploy")
_TX_CONFIRM_ONLY = re.compile(
    r"(?:停|等待|止步|pause|stop)\s*(?:在|at)?\s*(?:发射|rf|tx).{0,8}(?:确认|checkpoint)|"
    r"(?:发射|rf|tx).{0,8}(?:确认|checkpoint).{0,8}(?:停|等待|pause|stop)",
    re.I,
)
_TX_FORBIDDEN = re.compile(
    r"(?:先|暂时)?\s*(?:不要|不|禁止|勿|别)\s*(?:启动|运行|发射|tx|transmit)|"
    r"(?:do\s+not|don't|without)\s+(?:start|run|transmit)",
    re.I,
)
_DEVICE_ACCESS_FORBIDDEN = re.compile(
    r"(?:不要|不|禁止|勿|别)\s*(?:访问|探测|连接|打开)\s*(?:设备|硬件|sdr|radio)|"
    r"without\s+(?:accessing|probing|connecting)\s+(?:the\s+)?(?:device|hardware|sdr)",
    re.I,
)
_NEGATION = re.compile(
    r"(?:不(?:要|用|接|做|上|含)?|别|禁止|勿|without|no(?![a-z])|not\s+)",
    re.I,
)
_SIM_ONLY = re.compile(
    r"(?<!不)(?:只|仅).{0,16}(?:仿真|模拟|simulat)|simulation[\s-]*only",
    re.I,
)
_CAUSE_DEPENDENCIES = {
    "flowgraph_changed": {"project.flowgraph"},
    "snapshot_restored": {"project.flowgraph"},
    "project_version_mismatch": {"project.flowgraph"},
    "spec_changed": {"spec.architecture"},
    "architecture_changed": {"spec.architecture"},
    "recipe_changed": {"spec.architecture", "project.flowgraph"},
    "success_conditions_changed": {"success_conditions"},
}


def _negated_span(text: str, start: int, width: int = 18) -> bool:
    window = (text or "")[max(0, start - width): start + width]
    return bool(_NEGATION.search(window))


def _hardware_affirmed(text: str) -> bool:
    low = (text or "").lower()
    for hint in _DEVICE_HINTS + ("sdr",):
        index = low.find(hint)
        while index >= 0:
            if not _negated_span(low, index):
                return True
            index = low.find(hint, index + 1)
    for match in re.finditer(
        r"(?:配置|接入|接上|连接).{0,16}(?:硬件|电台|sdr)",
        text or "",
        flags=re.I,
    ):
        if not _negated_span(text or "", match.start()):
            return True
    return False


_HW_OBJECT = re.compile(r"硬件|射频|sdr|usrp|电台|板子|空口", re.I)


def _offline_forbidden(text: str) -> set[str]:
    forbidden: set[str] = set()
    if _DEVICE_ACCESS_FORBIDDEN.search(text or ""):
        forbidden.update({"hardware_runtime", "deploy"})
    if _SIM_ONLY.search(text or ""):
        forbidden.update(_HW_CAPABILITIES)
    for match in _HW_OBJECT.finditer(text or ""):
        if _negated_span(text or "", match.start()):
            forbidden.update(_HW_CAPABILITIES)
            break
    return forbidden


def _task_type_from_capabilities(
    capabilities: list[str],
    current: str,
    *,
    slots: Dict[str, Any] | None = None,
    forbidden: list[str] | None = None,
) -> str:
    blocked = set(forbidden or [])
    caps = set(capabilities or [])
    slots = dict(slots or {})
    if "diagnose" in caps:
        return "DIAGNOSE"
    if "modify_project" in caps:
        return "MODIFY_PROJECT"
    if (
        "protocol" in caps
        and slots.get("operation") == "deploy"
        and "hardware_configure" not in blocked
    ):
        return "HARDWARE_CONFIGURE"
    if (
        "hardware_configure" in caps
        and slots.get("terminal_checkpoint") == "rf_plan_confirmation"
    ):
        return "HARDWARE_CONFIGURE"
    hardware_primary = (
        "hardware_configure" in caps
        and "deploy" not in caps
        and "hardware_runtime" not in caps
        and not slots.get("requires_build")
    )
    if hardware_primary:
        return "HARDWARE_CONFIGURE"
    if "build_rx" in caps:
        return "RX_BUILD"
    if "build_tx" in caps:
        return "TX_BUILD"
    if "hardware_configure" in caps:
        return "HARDWARE_CONFIGURE"
    if "build_signal" in caps:
        return "END_TO_END_SIM"
    if "observe" in caps:
        return "OBSERVE"
    if current == "HARDWARE_CONFIGURE":
        return "END_TO_END_SIM"
    if current in _TASK_TYPES:
        return current
    return "END_TO_END_SIM"


class WorkflowEngine:
    def __init__(
        self,
        path: str,
        *,
        catalog_path: str = "",
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.path = path
        self.catalog_path = catalog_path or os.path.join(
            os.path.dirname(__file__), "task_catalog.yaml"
        )
        self._event_sink = event_sink
        self.catalog = self._load_catalog()
        self.workflow = self._load()
        if self.workflow:
            self._recover_interrupted()

    def _load_catalog(self) -> Dict[str, Any]:
        with open(self.catalog_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        candidates = data.get("task_candidates")
        if data.get("schema_version") != 1 or not isinstance(candidates, dict):
            raise ValueError("不支持的 Task Catalog")
        for task_type, candidate in candidates.items():
            stage_sets = [list(candidate.get("stages") or [])]
            if candidate.get("deploy_stages"):
                stage_sets.append(list(candidate.get("deploy_stages") or []))
            if candidate.get("runtime_stages"):
                stage_sets.append(list(candidate.get("runtime_stages") or []))
            for stages in stage_sets:
                self._validate_catalog_stages(task_type, stages)
        return data

    @staticmethod
    def _validate_catalog_stages(task_type: str, stages: list[Dict[str, Any]]) -> None:
        ids = [str(stage.get("id") or "") for stage in stages]
        if not task_type or not stages or any(not stage_id for stage_id in ids):
            raise ValueError(f"Task {task_type!r} 缺少有效 Stage")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Task {task_type} Stage id 重复")
        for stage in stages:
            unknown_agents = set(stage.get("recommended_agents") or []) - _KNOWN_AGENTS
            unknown_completion = set(stage.get("completion") or []) - KNOWN_COMPLETIONS
            if unknown_agents:
                raise ValueError(f"Task {task_type} 使用未知 Subagent: {sorted(unknown_agents)}")
            if unknown_completion:
                raise ValueError(
                    f"Task {task_type} 使用未知 completion: {sorted(unknown_completion)}"
                )
            for target in (stage.get("on") or {}).values():
                if target not in ids and target not in _TERMINAL_TARGETS:
                    raise ValueError(
                        f"Task {task_type} Stage {stage.get('id')} 转移目标不存在: {target}"
                    )

    def _load(self) -> Optional[Workflow]:
        if not os.path.isfile(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        from ..state.shared_state import resolve_tree_paths

        data = resolve_tree_paths(os.path.dirname(os.path.abspath(self.path)), data)
        return Workflow.from_dict(data)

    def _recover_interrupted(self) -> None:
        changed = False
        for stage in self.workflow.stages:
            if stage.execution_status == "running":
                stage.execution_status = "pending"
                changed = True
        if self.workflow.execution_status == "running":
            self.workflow.execution_status = "pending"
            changed = True
        if changed:
            self._event("workflow_recovered", self.digest())
            self.save()

    def reconcile_project_version(self, project_version: int) -> None:
        """Invalidate stale verification facts after loading state/workflow."""
        if not self.workflow:
            return
        current = int(project_version)
        if current != self.workflow.base_project_version:
            self.invalidate("project_version_mismatch", current)

    def consume_turn(self, user_text: str, shared_state: Any) -> Workflow:
        text = (user_text or "").strip()
        current = self.current_stage()
        if self.workflow and self.workflow.execution_status not in _TERMINALS:
            relation = self._turn_relation(text, shared_state)
            self.workflow.intent.turn_relation = relation
            self._event("turn_relation_classified", {"relation": relation, "text": text})
            if relation == "cancel":
                self.workflow.execution_status = "completed"
                self.workflow.outcome = "cancelled"
                if current:
                    current.execution_status = "completed"
                    current.outcome = "cancelled"
                self._event("workflow_cancelled", {"workflow_id": self.workflow.workflow_id})
                self.save()
                return self.workflow
            if relation in ("approval", "rejection"):
                if current and current.id in (
                    "over_air_verification", "runtime_observation"
                ):
                    key = (
                        "over_air_observed"
                        if current.id == "over_air_verification"
                        else "runtime_observed"
                    )
                    self.workflow.intent.slots[key] = relation == "approval"
                self.resolve_checkpoint(
                    "approved" if relation == "approval" else "rejected"
                )
                return self.workflow
            if (
                current
                and current.execution_status == "waiting"
                and current.checkpoint
                and relation != "new_task"
            ):
                if current.id in _ALIGNMENT_STAGES:
                    self._merge_turn_slots(text, shared_state)
                    if not self.workflow.intent.missing_slots and not self.workflow.intent.validation_errors:
                        self.resolve_checkpoint("approved")
                    else:
                        self.save()
                    return self.workflow
                if relation == "adjustment":
                    self._merge_turn_slots(text, shared_state)
                    current.checkpoint.reason = self._checkpoint_reason(current)
                    self.workflow.revision += 1
                    self.save()
                return self.workflow
            if relation in ("answer", "feedback", "adjustment"):
                self._merge_turn_slots(text, shared_state)
                if current and self.workflow.execution_status == "waiting":
                    self._remember_result(current, "user_feedback")
                    resume_stage = current
                    failure_codes = list(
                        (current.result.get("acceptance") or {}).get(
                            "failure_codes"
                        )
                        or []
                    )
                    project_config = getattr(
                        getattr(shared_state, "project", None), "config", {}
                    ) or {}
                    if (
                        current.id == "transmit_bounded"
                        and not project_config.get("rf_armed")
                        and any(
                            code == "MISSING_COMPLETION:transmit_started"
                            for code in failure_codes
                        )
                    ):
                        configured = self.workflow.stage("configure_device")
                        if configured is not None:
                            resume_stage = configured
                            self.workflow.current_stage = configured.id
                    resume_stage.execution_status = "pending"
                    resume_stage.outcome = ""
                    resume_stage.result = {}
                    # A user-authorized retry resumes the selected Stage even
                    # when its ordinary autonomous attempt budget was spent.
                    resume_stage.resume_pending = True
                self.workflow.execution_status = "pending"
                self.workflow.revision += 1
                self.save()
                return self.workflow
            if relation != "new_task":
                self.save()
                return self.workflow
            self._event(
                "workflow_superseded",
                {"workflow_id": self.workflow.workflow_id, "new_text": text},
            )

        intent = self.classify(text, shared_state)
        return self.instantiate(
            self._reconcile_intent(intent, text, shared_state), shared_state
        )

    def _turn_relation(self, text: str, shared_state: Any) -> str:
        low = (text or "").lower().strip()
        current = self.current_stage()
        if any(hint in low for hint in ("终止任务", "取消任务", "cancel task")):
            return "cancel"
        if current and current.execution_status == "waiting" and current.checkpoint:
            decision = self._decision(text)
            if decision:
                return "approval" if decision == "approved" else "rejection"
        if self._is_explicit_new_task(low):
            return "new_task"
        if current and current.execution_status == "waiting" and current.checkpoint:
            # A checkpoint pauses the current workflow; it does not capture all
            # subsequent user turns.  A clearly classified request for another
            # capability must be allowed to supersede it (for example, moving
            # from diagnosis to a read-only observation).
            classified = self.classify(text, shared_state)
            if self._is_strong_task_switch(classified):
                return "new_task"
            if current.id in _ALIGNMENT_STAGES:
                return "answer"
            return "adjustment"
        if self.workflow and self.workflow.execution_status == "waiting" and (
            bool(self._decision(text))
            or low in {"启动", "继续", "重试", "再试", "resume", "retry"}
        ):
            return "feedback"
        classified = self.classify(text, shared_state)
        if self._is_strong_task_switch(classified):
            return "new_task"
        if self.workflow and self.workflow.execution_status == "waiting":
            return "feedback"
        if current and current.execution_status in ("pending", "invalidated"):
            if any(hint in low for hint in ("调整", "改一下方案", "补充", "其余条件")):
                return "adjustment"
        if self.workflow.execution_status == "waiting":
            return "feedback"
        return "adjustment"

    @staticmethod
    def _is_explicit_new_task(low: str) -> bool:
        return any(
            hint in low
            for hint in ("新任务", "另外一个任务", "开始新的", "new task")
        )

    def _is_strong_task_switch(self, intent: WorkflowIntent) -> bool:
        if not self.workflow or intent.task_type == self.workflow.task_type:
            return False
        strong = {
            "diagnose",
            "modify_project",
            "build_rx",
            "build_tx",
            "hardware_configure",
            "observe",
            "protocol",
            "deploy",
        }
        if not set(intent.capabilities or ()) & strong:
            return False
        return intent.confidence >= 0.9 or bool(
            intent.slots.get("modulation") or intent.slots.get("hardware")
        )

    def _merge_turn_slots(self, text: str, shared_state: Any) -> None:
        update = self.classify(text, shared_state)
        updates = {
            key: value
            for key, value in update.slots.items()
            if value not in (None, "", [])
        }
        self.workflow.intent.slots.update(updates)
        self.workflow.intent.slot_sources.update(
            {key: "user" for key in updates}
        )
        # Capabilities describe the instantiated plan and stay stable during
        # ordinary feedback.  A material capability change is a replan/new
        # task decision, not an append-only merge from a short control turn.
        self.workflow.intent.missing_slots = self._missing_slots(
            self.workflow.task_type,
            self.workflow.intent.slots,
            shared_state,
            self.workflow.intent.capabilities,
        )
        self.workflow.intent.validation_errors = self._validate_slots(
            self.workflow.intent.slots
        )

    def classify(self, text: str, shared_state: Any) -> WorkflowIntent:
        low = (text or "").lower()
        has_project = bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
            or getattr(getattr(shared_state, "project", None), "config", {}).get("recipe")
        )
        slots = self._parse_slots(text)
        capabilities = self._detect_capabilities(text, slots, has_project)
        task_type = _task_type_from_capabilities(
            capabilities, "END_TO_END_SIM", slots=slots
        )
        project_config = getattr(getattr(shared_state, "project", None), "config", {})
        slot_sources = {key: "user" for key, value in slots.items() if value not in (None, "", [])}
        if slots.get("protocol") == "ble":
            # Protocol-derived defaults are not fresh user decisions.  Keep
            # provenance precise so replanning may change defaults without
            # silently overriding an explicit constraint.
            for key in (
                "ble_mode", "modulation", "advertising_channels",
                "carrier_frequency", "sample_rate",
            ):
                if key in slots:
                    slot_sources[key] = "protocol_default"
            if "duration_seconds" in slots:
                slot_sources["duration_seconds"] = (
                    "user" if re.search(
                        r"(?:运行|持续|时长|duration)\s*[:=为]?\s*"
                        r"\d+(?:\.\d+)?\s*(?:秒|s|sec(?:onds?)?)",
                        low,
                    ) else "safety_default"
                )
                slot_sources["max_duration_seconds"] = slot_sources["duration_seconds"]
                slots["max_duration_seconds"] = slots["duration_seconds"]
            if "tx_gain" in slots:
                slot_sources["tx_gain"] = "safety_default"
            if "tx_attenuation" in slots:
                slot_sources["tx_attenuation"] = "safety_default"
            explicit_frequency = bool(re.search(
                r"(?<![0-9.])\d+(?:\.\d+)?\s*[gmk]?hz(?![A-Za-z0-9_])",
                low,
            ))
            if explicit_frequency:
                slot_sources["carrier_frequency"] = "user"
                slot_sources["advertising_channels"] = "derived"
            if re.search(r"(?:采样率|sample rate)", low):
                slot_sources["sample_rate"] = "user"
        context = {
            "current_project": {
                "grc_path": str(getattr(getattr(shared_state, "project", None), "grc_path", "") or ""),
                "recipe": str(project_config.get("recipe") or ""),
                "modulation": str(project_config.get("modulation") or ""),
                "channel": str(project_config.get("channel") or ""),
            }
        } if has_project else {}
        # Only inherit parameters that are requirements of a signal-generation
        # task.  Existing project facts remain context for modify/observe/hardware
        # workflows and are never represented as a fresh user decision.
        if task_type in {"END_TO_END_SIM", "TX_BUILD", "RX_BUILD"} and "signal_agnostic_observe" not in capabilities:
            for key in ("modulation", "channel"):
                if not slots.get(key) and project_config.get(key):
                    slots[key] = project_config[key]
                    slot_sources[key] = "current_project"
        if (
            "signal_agnostic_observe" in capabilities
            and slots.get("hardware")
            and slots.get("carrier_frequency")
            and not slots.get("sample_rate")
        ):
            slots["sample_rate"] = 2_000_000.0
            slot_sources["sample_rate"] = "default"
        if (
            slots.get("hardware")
            and slots.get("direction") == "tx"
            and slots.get("sample_rate")
        ):
            slots.setdefault("bandwidth", slots["sample_rate"])
            slot_sources.setdefault("bandwidth", "derived")
            slots.setdefault("baseband_kind", "diagnostic_tone")
            slots.setdefault("tone_frequency_hz", 1000.0)
            slots.setdefault("tone_amplitude", 0.3)
            for key in ("baseband_kind", "tone_frequency_hz", "tone_amplitude"):
                slot_sources.setdefault(key, "safe_preview_default")
            if slots.get("hardware") == "pluto":
                slots.setdefault("tx_attenuation", 30.0)
                slot_sources.setdefault("tx_attenuation", "safety_default")
            else:
                slots.setdefault("tx_gain", 0.0)
                slot_sources.setdefault("tx_gain", "safety_default")
        if "hardware_runtime" in capabilities:
            slots.setdefault("duration_seconds", 30.0)
            slots.setdefault("max_duration_seconds", slots["duration_seconds"])
            slot_sources.setdefault("duration_seconds", "safety_default")
            slot_sources.setdefault("max_duration_seconds", "safety_default")
        missing = self._missing_slots(task_type, slots, shared_state, capabilities)
        validation_errors = self._validate_slots(slots)
        intent = WorkflowIntent(
            raw_text=text,
            turn_relation="new_task",
            task_type=task_type,
            confidence=0.95 if any(slots.values()) or task_type != "END_TO_END_SIM" else 0.65,
            slots=slots,
            missing_slots=missing,
            capabilities=capabilities,
            slot_sources=slot_sources,
            context=context,
            validation_errors=validation_errors,
        )
        return intent

    def _reconcile_intent(
        self, intent: WorkflowIntent, text: str, shared_state: Any
    ) -> WorkflowIntent:
        """Let LLM (or an offline fallback) drop capabilities that contradict constraints."""
        intent.context.setdefault("forbidden_capabilities", sorted(_offline_forbidden(text)))
        try:
            from ..llm import is_configured
            configured = is_configured()
        except Exception:  # noqa: BLE001
            configured = False
        if configured:
            intent = complete_intent(intent, text, shared_state)
        if "diagnose" in intent.capabilities and is_read_only_request(text):
            forbidden = set(intent.context.get("forbidden_capabilities") or [])
            forbidden.add("modify_project")
            intent.context["forbidden_capabilities"] = sorted(forbidden)
        intent.capabilities = [
            name for name in intent.capabilities
            if name not in set(intent.context.get("forbidden_capabilities") or ())
        ]
        intent.task_type = _task_type_from_capabilities(
            intent.capabilities,
            intent.task_type,
            slots=intent.slots,
            forbidden=list(intent.context.get("forbidden_capabilities") or []),
        )
        intent.missing_slots = self._missing_slots(
            intent.task_type, intent.slots, shared_state, intent.capabilities
        )
        intent.validation_errors = self._validate_slots(intent.slots)
        return intent

    @staticmethod
    def _detect_capabilities(
        text: str, slots: Dict[str, Any], has_project: bool
    ) -> list[str]:
        low = (text or "").lower()
        capabilities: list[str] = []

        def add(name: str, condition: bool) -> None:
            if condition and name not in capabilities:
                capabilities.append(name)

        diagnose = any(word in low for word in ("诊断", "排查", "故障", "为什么", "diagnos"))
        modify = any(word in low for word in _MODIFY_HINTS)
        build = any(word in low for word in _BUILD_HINTS)
        rx = slots.get("direction") == "rx"
        tx = slots.get("direction") == "tx"
        hardware = bool(slots.get("hardware")) or _hardware_affirmed(text)
        observe = bool(slots.get("requested_metrics")) or any(word in low for word in _OBSERVE_HINTS)
        realtime = any(word in low for word in ("实时", "live", "real-time", "realtime"))

        add("diagnose", diagnose)
        add("modify_project", modify and (has_project or bool(slots.get("target_project"))))
        add("build_rx", build and rx)
        add("build_tx", build and tx)
        add(
            "build_tx",
            hardware
            and tx
            and any(word in low for word in ("流图", "flowgraph", "flow graph")),
        )
        add("build_signal", build and not rx and not tx)
        add("hardware_configure", hardware)
        add("observe", observe)
        add("realtime_observe", observe and realtime)
        add("signal_agnostic_observe", hardware and observe and not any(
            word in low for word in ("解调", "decode", "demod", "判决")
        ))
        add("protocol", bool(slots.get("protocol")))
        add("deploy", slots.get("operation") == "deploy")
        add("hardware_runtime", hardware and (
            realtime
            or slots.get("operation") == "deploy"
            or slots.get("terminal_checkpoint") == "rf_plan_confirmation"
            or any(word in low for word in ("启动", "运行", "run", "start"))
        ) and slots.get("hardware_access") != "forbidden")
        forbidden = _offline_forbidden(text)
        return [name for name in capabilities if name not in forbidden]

    @staticmethod
    def _parse_slots(text: str) -> Dict[str, Any]:
        low = (text or "").lower()
        modulation = next((
            name for name in ("ofdm", "qpsk", "bpsk", "gfsk")
            if re.search(
                rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", low
            )
        ), "")
        channel = "awgn" if (
            re.search(r"(?<![A-Za-z0-9_])awgn(?![A-Za-z0-9_])", low)
            or "高斯" in text or "噪声" in text
        ) else ""
        metrics = [
            name for name, hints in (
                ("evm", ("evm",)), ("ber", ("ber", "误码率")),
                ("spectrum", ("频谱", "spectrum")),
                ("constellation", ("星座", "constellation")),
                ("eye", ("眼图", "eye diagram")),
            ) if any(hint in low for hint in hints)
        ]
        slots: Dict[str, Any] = {
            "modulation": modulation,
            "channel": channel,
            "requested_metrics": metrics,
        }
        ebn0 = re.search(
            r"(?:eb\s*/?\s*n0|ebn0)\s*[:=为]?\s*"
            r"([-+]?\d+(?:\.\d+)?)\s*db",
            low,
        )
        if ebn0:
            slots["ebn0_db"] = float(ebn0.group(1))
        if any(word in low for word in ("直接部署", "部署", "deploy")):
            slots["operation"] = "deploy"
            slots["deploy_permission"] = "requested"
        if _TX_CONFIRM_ONLY.search(text or ""):
            slots["operation"] = "prepare"
            slots["deploy_permission"] = "pending"
            slots["terminal_checkpoint"] = "rf_plan_confirmation"
            slots["hardware_access"] = "read_only_probe"
        elif _TX_FORBIDDEN.search(text or ""):
            slots["operation"] = "configure"
            slots["deploy_permission"] = "forbidden"
            slots["hardware_access"] = "configuration_only"
        if _DEVICE_ACCESS_FORBIDDEN.search(text or ""):
            slots["hardware_access"] = "forbidden"
        duration = re.search(
            r"(?:运行|持续|时长|duration)\s*[:=为]?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec(?:onds?)?)",
            low,
        )
        if duration:
            slots["duration_seconds"] = float(duration.group(1))
            slots["max_duration_seconds"] = float(duration.group(1))
        if any(word in low for word in ("实时", "live", "real-time", "realtime")):
            slots["observation_mode"] = "realtime"
        if "ble" in low or "bluetooth low energy" in low or "低功耗蓝牙" in text:
            slots.update(
                {
                    "protocol": "ble",
                    "ble_mode": "advertising",
                    "operation": "deploy" if any(
                        word in low for word in ("发射", "部署", "transmit", "deploy")
                    ) else "configure",
                    "modulation": "gfsk",
                    "advertising_channels": [37, 38, 39],
                    "carrier_frequency": 2_402_000_000.0,
                    "sample_rate": 2_000_000.0,
                    "duration_seconds": slots.get("duration_seconds", 30.0),
                    "max_duration_seconds": slots.get("duration_seconds", 30.0),
                    "tx_gain": 0.0,
                }
            )
        name = re.search(
            r"(?:local\s*name|localname|本地名称|设备名称)\s*(?:为|=|:)?\s*"
            r"([A-Za-z0-9_-]{1,26})",
            text,
            flags=re.IGNORECASE,
        )
        if name:
            slots["local_name"] = name.group(1)
        if any(word in low for word in ("接收机", "receiver", "解调", " rx")):
            slots["direction"] = "rx"
        elif any(
            word in low
            for word in ("发射机", "transmitter", "发射链", "发射流图", " tx")
        ):
            slots["direction"] = "tx"
        elif modulation:
            slots["direction"] = "transceiver"
        target_project = re.search(
            r"(?:直接)?(?:修改|打开|基于|modify)\s*([A-Za-z_]\w*)",
            text,
            flags=re.IGNORECASE,
        )
        if target_project:
            slots["target_project"] = target_project.group(1)
        units = {"g": 1e9, "m": 1e6, "k": 1e3, "": 1.0}
        patterns = {
            "carrier_frequency": r"(?:中心频率|载频|carrier(?: frequency)?)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "sample_rate": r"(?:采样率|sample rate)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)s?(?:ps|hz)",
            "bandwidth": r"(?:带宽|bandwidth)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "symbol_rate": r"(?:符号率|symbol rate)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)(?:baud|sym/s)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, low, flags=re.IGNORECASE)
            if match:
                slots[key] = float(match.group(1)) * units[match.group(2).lower()]
        if not slots.get("sample_rate"):
            msps = re.search(
                r"(\d+(?:\.\d+)?)\s*m(?:sps|(?:ega)?(?:samples?)\s*/\s*s)",
                low,
            )
            if msps:
                slots["sample_rate"] = float(msps.group(1)) * 1e6
        if not slots.get("carrier_frequency") and any(
            marker in low for marker in ("硬件", "usrp", "sdr", "信号", "carrier")
        ):
            match = re.search(
                r"(?<![0-9.])\d+(?:\.\d+)?\s*[gmk]?hz(?![A-Za-z0-9_])", low
            )
            if match:
                parsed = re.fullmatch(
                    r"(\d+(?:\.\d+)?)\s*([gmk]?)hz", match.group(0).strip()
                )
                if parsed:
                    slots["carrier_frequency"] = (
                        float(parsed.group(1)) * units[parsed.group(2).lower()]
                    )
        device = next(
            (
                name
                for name, pattern in (
                    ("b210", r"(?<![a-z0-9])(?:usrp\s*)?b2(?:00|10)(?![a-z0-9])"),
                    ("hackrf", r"(?<![a-z0-9])hackrf(?:\s+one)?(?![a-z0-9])"),
                    ("pluto", r"(?<![a-z0-9])(?:adalm[-\s]*)?pluto(?:sdr)?(?![a-z0-9])"),
                    ("limesdr", r"(?<![a-z0-9])lime\s*sdr(?![a-z0-9])"),
                    ("usrp", r"(?<![a-z0-9])usrp(?![a-z0-9])"),
                )
                if re.search(pattern, low)
            ),
            "",
        )
        if device:
            slots["hardware"] = device
        switch = re.search(
            r"(?:改成|换成|改为|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s*"
            r"([a-z0-9_]+)",
            low,
        )
        if switch:
            target = switch.group(1)
            mapping = {"bpsk": "bpsk_awgn", "qpsk": "qpsk_awgn", "ofdm": "ofdm_awgn"}
            slots["target_recipe"] = mapping.get(target, target)
            slots["change_type"] = "modulation_change" if target in mapping else "recipe_change"
        elif re.search(r"[A-Za-z_]\w*\.[A-Za-z_]\w*\s*(?:改为|设为|改成|=)", text):
            slots["change_type"] = "single_parameter"
        if device and any(word in low for word in _BUILD_HINTS):
            slots["requires_build"] = True
        success = re.findall(r"(?:evm|ber)\s*(?:小于|低于|<|≤)\s*\d+(?:\.\d+)?\s*%?", low)
        if success:
            slots["success_conditions"] = success
        if slots.get("protocol") == "ble":
            ghz = re.search(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*ghz", low)
            if ghz:
                slots["carrier_frequency"] = float(ghz.group(1)) * 1e9
            freq = slots.get("carrier_frequency")
            if freq is not None:
                for channel, center in (
                    (37, 2_402_000_000.0),
                    (38, 2_426_000_000.0),
                    (39, 2_480_000_000.0),
                ):
                    if abs(float(freq) - center) < 5e5:
                        slots["advertising_channels"] = [channel]
                        slots["carrier_frequency"] = center
                        break
            if str(slots.get("hardware") or "") == "pluto":
                slots.setdefault("tx_attenuation", 30.0)
        return slots

    @staticmethod
    def _missing_slots(
        task_type: str,
        slots: Dict[str, Any],
        shared_state: Any,
        capabilities: Optional[list[str]] = None,
    ) -> list[str]:
        missing = []
        capabilities = list(capabilities or [])
        signal_agnostic = "signal_agnostic_observe" in capabilities
        if task_type in {"END_TO_END_SIM", "TX_BUILD", "RX_BUILD"} and not signal_agnostic and not slots.get("modulation"):
            missing.append("modulation")
        if (
            task_type == "RX_BUILD"
            and "ber" in (slots.get("requested_metrics") or [])
            and slots.get("ebn0_db") is None
        ):
            missing.append("ebn0_db")
        if task_type in {"DIAGNOSE", "MODIFY_PROJECT", "OBSERVE"}:
            project = getattr(shared_state, "project", None)
            if not (getattr(project, "grc_path", "") or getattr(project, "config", {}).get("recipe")):
                missing.append("current_project")
        if task_type == "HARDWARE_CONFIGURE" or "hardware_configure" in capabilities:
            for key in ("hardware", "carrier_frequency", "sample_rate"):
                if not slots.get(key):
                    missing.append(key)
            if slots.get("operation") == "deploy" and slots.get("protocol") == "ble":
                if not slots.get("local_name"):
                    missing.append("local_name")
        return missing

    @staticmethod
    def _validate_slots(slots: Dict[str, Any]) -> list[str]:
        errors: list[str] = []
        frequency = slots.get("carrier_frequency")
        if frequency is not None:
            try:
                value = float(frequency)
            except (TypeError, ValueError):
                errors.append("carrier_frequency_invalid")
            else:
                if value <= 0:
                    errors.append("carrier_frequency_invalid")
                # This is a capability guard, not a task classifier. Known
                # radios can add ranges without changing workflow semantics.
                profile = resolve_hardware_profile(
                    str(slots.get("hardware") or "")
                )
                if profile:
                    low, high = profile.frequency_range
                    if not low <= value <= high:
                        errors.append("carrier_frequency_out_of_device_range")
        for key in ("sample_rate", "bandwidth", "symbol_rate"):
            if slots.get(key) is not None:
                try:
                    if float(slots[key]) <= 0:
                        errors.append(f"{key}_invalid")
                except (TypeError, ValueError):
                    errors.append(f"{key}_invalid")
        return errors

    def instantiate(self, intent: WorkflowIntent, shared_state: Any) -> Workflow:
        candidate = self.catalog["task_candidates"].get(intent.task_type)
        if not candidate:
            raise ValueError(f"未知 Task Type: {intent.task_type}")
        stages = self._compose_stages(intent, candidate)
        if not stages:
            raise ValueError(f"Task {intent.task_type} 没有可执行 Stage")
        version = int(getattr(getattr(shared_state, "project", None), "flowgraph_version", 0))
        self.workflow = Workflow(
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            task_type=intent.task_type,
            intent=intent,
            stages=stages,
            base_project_version=version,
            current_stage=stages[0].id,
            catalog_version=int(self.catalog.get("schema_version", 1)),
        )
        self._activate_current()
        self._event("intent_classified", {
            "task_type": intent.task_type,
            "capabilities": intent.capabilities,
            "slots": intent.slots,
            "slot_sources": intent.slot_sources,
            "missing_slots": intent.missing_slots,
            "validation_errors": intent.validation_errors,
        })
        self._event("workflow_created", self.digest())
        self.save()
        return self.workflow

    def _compose_stages(
        self, intent: WorkflowIntent, candidate: Dict[str, Any]
    ) -> list[Stage]:
        """Compose capability groups while preserving each group's failure graph.

        Task candidates remain stable policy/display entry points.  Independent
        capabilities may extend their success path; no capability may replace
        the user's raw text or silently terminate a later group.
        """
        deploying_protocol = (
            intent.slots.get("operation") == "deploy"
            and "protocol" in intent.capabilities
        )
        if deploying_protocol:
            selected = list(candidate.get("deploy_stages") or [])
            if not selected:
                hardware = self.catalog["task_candidates"].get("HARDWARE_CONFIGURE") or {}
                selected = list(hardware.get("deploy_stages") or [])
            if not selected:
                selected = list(candidate.get("stages") or [])
        elif (
            intent.task_type == "HARDWARE_CONFIGURE"
            and "hardware_runtime" in intent.capabilities
        ):
            selected = candidate.get("runtime_stages") or candidate.get("stages") or []
        else:
            selected = candidate.get("stages") or []
        groups: list[list[Stage]] = []

        def make_group(raw_stages: list[Dict[str, Any]]) -> list[Stage]:
            return [
                Stage.from_dict(raw)
                for raw in raw_stages
                if str(raw.get("id") or "") not in _ALIGNMENT_STAGES
                or bool(intent.missing_slots or intent.validation_errors)
            ]

        base_group = make_group(list(selected))
        if base_group:
            groups.append(base_group)

        if not deploying_protocol:
            capabilities = set(intent.capabilities)
            # A hardware Task may also carry a signal-build capability. Choose
            # the build family from direction, never from a device keyword.
            if intent.task_type == "HARDWARE_CONFIGURE" and capabilities.intersection(
                {"build_rx", "build_tx", "build_signal"}
            ):
                build_type = (
                    "RX_BUILD" if "build_rx" in capabilities
                    else "TX_BUILD" if "build_tx" in capabilities
                    else "END_TO_END_SIM"
                )
                build_group = make_group(list(
                    self.catalog["task_candidates"][build_type].get("stages") or []
                ))
                groups.insert(0, build_group)

            composable = intent.task_type in {
                "END_TO_END_SIM", "TX_BUILD", "RX_BUILD", "MODIFY_PROJECT",
                "HARDWARE_CONFIGURE",
            }
            if (
                "hardware_configure" in capabilities
                and intent.task_type != "HARDWARE_CONFIGURE"
                and composable
            ):
                hardware_candidate = self.catalog["task_candidates"]["HARDWARE_CONFIGURE"]
                hardware_stages = (
                    hardware_candidate.get("runtime_stages")
                    if "hardware_runtime" in capabilities
                    else hardware_candidate.get("stages")
                ) or []
                groups.append(make_group(list(
                    hardware_stages
                )))

        # Missing or invalid execution parameters are a global gate.  Task
        # templates that do not define alignment receive the generic alignment
        # stage without changing their business classification.
        flattened_ids = {stage.id for group in groups for stage in group}
        if (
            (intent.missing_slots or intent.validation_errors)
            and not flattened_ids.intersection(_ALIGNMENT_STAGES)
        ):
            alignment = Stage.from_dict(
                self.catalog["task_candidates"]["END_TO_END_SIM"]["stages"][0]
            )
            alignment.transitions["approved"] = "completed"
            groups.insert(0, [alignment])

        groups = [group for group in groups if group]
        for index, group in enumerate(groups[:-1]):
            next_stage = groups[index + 1][0].id
            for stage in group:
                for outcome, target in list(stage.transitions.items()):
                    if target == "completed":
                        stage.transitions[outcome] = next_stage

        stages = [stage for group in groups for stage in group]
        if len({stage.id for stage in stages}) != len(stages):
            raise ValueError("能力组合产生重复 Stage id")

        forbidden = set(intent.context.get("forbidden_capabilities") or [])
        if "modify_project" in forbidden:
            for stage in stages:
                if stage.id == "inspect_and_diagnose":
                    stage.transitions["failed"] = "completed"
                    stage.transitions["failed_without_improvement"] = "completed"
            stages = [
                stage
                for stage in stages
                if stage.id not in {"repair_confirmation", "repair_and_verify"}
            ]

        # Hardware observation needs structural evidence from the build/change
        # stage.  This is capability-driven and applies to any compatible Task.
        capabilities = set(intent.capabilities)
        if "hardware_configure" in capabilities:
            for stage in stages:
                if stage.id in {"build_and_verify", "tx_build_and_validate", "rx_build_and_verify", "apply_and_verify"}:
                    if "signal_agnostic_observe" in capabilities:
                        stage.completion = [
                            name for name in stage.completion
                            if name not in {"receive_quality_evaluated", "measurement_completed"}
                        ]
                    self._add_completion(stage, "hardware_endpoint_present")
                    self._add_completion(stage, "radio_parameters_match")
                    if "realtime_observe" in capabilities:
                        self._add_completion(stage, "realtime_sink_present")
                if stage.id in {
                    "discover_and_probe_device", "discover_and_probe_hardware",
                }:
                    self._add_completion(stage, "device_identity_matched")
                if (
                    stage.id == "configure_device"
                    and str(intent.slots.get("direction") or "").lower() == "tx"
                ):
                    self._add_completion(stage, "flowgraph_armed")
        return stages

    @staticmethod
    def _add_completion(stage: Stage, name: str) -> None:
        if name not in stage.completion:
            stage.completion.append(name)

    def current_stage(self) -> Optional[Stage]:
        return self.workflow.stage() if self.workflow else None

    def start_stage(self) -> Optional[Stage]:
        stage = self.current_stage()
        if not stage or stage.execution_status == "waiting":
            return stage
        if stage.execution_status not in ("pending", "invalidated"):
            return stage
        if not stage.resume_pending and stage.attempt >= stage.max_attempts:
            stage.execution_status = "waiting"
            self.workflow.execution_status = "waiting"
            self._event("attempt_limit_reached", {
                "stage_id": stage.id,
                "attempt": stage.attempt,
                "max_attempts": stage.max_attempts,
            })
            self.save()
            return stage
        stage.execution_status = "running"
        if stage.resume_pending:
            stage.resume_pending = False
        else:
            stage.attempt += 1
        self.workflow.execution_status = "running"
        self._event("stage_started", {"workflow_id": self.workflow.workflow_id, "stage_id": stage.id, "attempt": stage.attempt})
        self.save()
        return stage

    def accept_result(self, result: Any) -> bool:
        if not self.workflow:
            raise ValueError("没有活动 Workflow")
        data = result if isinstance(result, dict) else vars(result)
        stage = self.current_stage()
        if not stage:
            raise ValueError("没有 current_stage")
        stale = (
            str(data.get("workflow_id") or self.workflow.workflow_id) != self.workflow.workflow_id
            or str(data.get("stage_id") or stage.id) != stage.id
            or int(data.get("workflow_revision", self.workflow.revision)) != self.workflow.revision
            or int(data.get("base_project_version", self.workflow.base_project_version)) != self.workflow.base_project_version
        )
        if stale:
            self._event("stale_result_discarded", {"stage_id": data.get("stage_id"), "workflow_revision": data.get("workflow_revision")})
            if stage.execution_status == "running":
                stage.execution_status = "pending"
                self.workflow.execution_status = "pending"
                self.save()
            return False
        completion = dict(data.get("completion") or {})
        missing_completion = [
            name for name in stage.completion if completion.get(name) is not True
        ]
        ok = bool(data.get("ok")) and not missing_completion
        outcome = str(data.get("outcome") or ("passed" if ok else "failed"))
        if missing_completion:
            outcome = "failed"
        stage.result = {
            key: data.get(key)
            for key in (
                "note",
                "artifacts",
                "produced_claims",
                "proposed_changes",
                "completion",
                "invocations",
                "acceptance",
            )
            if data.get(key)
        }
        fingerprint = _result_fingerprint(stage.result, ok, outcome)
        stage.result["fingerprint"] = fingerprint
        if missing_completion:
            stage.result["missing_completion"] = missing_completion
        if data.get("errored"):
            self._event("stage_errored", {"stage_id": stage.id, "attempt": stage.attempt})
            self._remember_result(stage, "errored")
            stage.execution_status = "waiting"
            stage.outcome = "inconclusive"
            self.workflow.execution_status = "waiting"
            self.save()
            return True
        else:
            stage.execution_status = "completed"
            stage.outcome = outcome if outcome in ("passed", "failed", "inconclusive") else ("passed" if ok else "failed")
            improvement = bool(data.get("improvement_available"))
            prior_prints = [
                (item.get("result") or {}).get("fingerprint")
                for item in stage.result_history
            ]
            if fingerprint and fingerprint in prior_prints:
                improvement = False
            if stage.outcome == "passed":
                transition_key = "passed"
            elif improvement and stage.attempt < stage.max_attempts:
                transition_key = "failed_with_improvement"
            elif stage.outcome == "failed":
                transition_key = "failed_without_improvement"
            else:
                transition_key = "errored"
            self._event("stage_completed", {
                "stage_id": stage.id,
                "outcome": stage.outcome,
                "attempt": stage.attempt,
                "acceptance": dict(data.get("acceptance") or {}),
                "missing_completion": missing_completion,
            })
        target = stage.transitions.get(transition_key)
        if target is None and transition_key == "passed":
            target = stage.transitions.get("completed")
        if target is None and stage.outcome == "failed":
            target = stage.transitions.get("failed")
        self._transition(target or ("completed" if ok else "waiting_user"))
        self.save()
        return True

    def wait_for_checkpoint(
        self,
        reason: str,
        *,
        action: str = "",
        payload_ref: str = "",
    ) -> Checkpoint:
        """Pause the current autonomous Stage for a Policy/user decision."""
        stage = self.current_stage()
        if not stage:
            raise ValueError("没有 current_stage")
        checkpoint = Checkpoint(
            id=f"cp-{uuid.uuid4().hex[:8]}",
            reason=reason or _STAGE_LABELS.get(stage.id, stage.id),
            action=action,
            payload_ref=payload_ref,
            resume_stage=True,
        )
        stage.execution_status = "waiting"
        stage.checkpoint = checkpoint
        self.workflow.execution_status = "waiting"
        self._event(
            "checkpoint_opened",
            {"stage_id": stage.id, "reason": checkpoint.reason, "checkpoint_id": checkpoint.id},
        )
        self.save()
        return checkpoint

    def resolve_checkpoint(self, decision: str) -> None:
        stage = self.current_stage()
        if not stage or not stage.checkpoint or stage.execution_status != "waiting":
            raise ValueError("当前没有待决 Checkpoint")
        normalized = "approved" if decision == "approved" else "rejected"
        stage.checkpoint.decision_status = normalized
        if stage.checkpoint.resume_stage and normalized == "approved":
            checkpoint_id = stage.checkpoint.id
            stage.execution_status = "pending"
            stage.outcome = ""
            stage.checkpoint = None
            stage.resume_pending = True
            self.workflow.execution_status = "pending"
            self._event(
                "checkpoint_resolved",
                {"stage_id": stage.id, "decision": normalized, "checkpoint_id": checkpoint_id},
            )
            self.save()
            return
        if stage.checkpoint.resume_stage and normalized == "rejected":
            checkpoint_id = stage.checkpoint.id
            stage.result = self._checkpoint_result(stage, normalized)
            stage.execution_status = "completed"
            stage.outcome = "cancelled"
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "cancelled"
            self._event(
                "checkpoint_resolved",
                {"stage_id": stage.id, "decision": normalized, "checkpoint_id": checkpoint_id},
            )
            self.save()
            return
        stage.result = self._checkpoint_result(stage, normalized)
        stage.execution_status = "completed"
        stage.outcome = "passed" if normalized == "approved" else "cancelled"
        self._event("checkpoint_resolved", {"stage_id": stage.id, "decision": normalized})
        self._transition(stage.transitions.get(normalized, "completed"))
        self.save()

    def _checkpoint_result(self, stage: Stage, decision: str) -> Dict[str, Any]:
        approved = decision == "approved"
        checkpoint = stage.checkpoint
        slots = self.workflow.intent.slots if self.workflow else {}
        observation = dict(slots.get("ota_observation") or {})
        completion = {}
        for name in stage.completion:
            if name == "rf_plan_approved":
                completion[name] = approved
            elif name == "over_air_observed":
                completion[name] = bool(approved and slots.get("over_air_observed"))
            elif name == "runtime_observation_recorded":
                completion[name] = bool(approved and slots.get("runtime_observed"))
            elif name == "required_slots_complete":
                completion[name] = not list(self.workflow.intent.missing_slots or [])
            elif name in ("hardware_decision_recorded", "change_decision_recorded"):
                completion[name] = True
            else:
                completion[name] = approved
        return {
            "completion": completion,
            "acceptance": {
                "decision": decision,
                "checkpoint_id": checkpoint.id if checkpoint else "",
                "decided_at": time.time(),
                "run_id": observation.get("run_id") or "",
                "evidence_id": observation.get("artifact") or "",
                "evidence_sha256": observation.get("sha256")
                or observation.get("artifact_sha256")
                or "",
            },
            "note": (checkpoint.reason if checkpoint else "") or stage.id,
        }

    def invalidate(self, cause: str, project_version: int) -> None:
        if not self.workflow:
            return
        wanted_deps = _CAUSE_DEPENDENCIES.get(cause, {"project.flowgraph"})
        fallback_ids = {
            "build_and_verify",
            "tx_build_and_validate",
            "rx_build_and_verify",
            "inspect_and_diagnose",
            "repair_and_verify",
            "apply_and_verify",
            "inspect_and_measure",
            "hardware_precheck",
            "configure_and_check",
        }
        if cause in ("spec_changed", "architecture_changed", "recipe_changed"):
            fallback_ids = {
                stage.id for stage in self.workflow.stages if "alignment" not in stage.id
            }
        elif cause == "success_conditions_changed":
            fallback_ids = fallback_ids - {"hardware_precheck", "configure_and_check"}
        affected = [
            stage
            for stage in self.workflow.stages
            if stage.execution_status == "completed"
            and (
                (set(stage.depends_on) & wanted_deps)
                if stage.depends_on
                else stage.id in fallback_ids
            )
        ]
        if not affected:
            # Even when no completed Stage needs rerunning, future envelopes must
            # be compared with the current project version.
            if self.workflow.base_project_version != int(project_version):
                self.workflow.revision += 1
                self.workflow.base_project_version = int(project_version)
                self._event(
                    "workflow_rebased",
                    {"cause": cause, "project_version": project_version},
                )
                self.save()
            return
        for stage in affected:
            self._remember_result(stage, cause)
            stage.execution_status = "invalidated"
            stage.outcome = ""
            stage.attempt = 0
            stage.resume_pending = False
            stage.result = {}
        self.workflow.revision += 1
        self.workflow.base_project_version = int(project_version)
        self.workflow.current_stage = affected[0].id
        self.workflow.execution_status = "pending"
        self.workflow.outcome = ""
        self._event("stage_invalidated", {"cause": cause, "stages": [stage.id for stage in affected], "project_version": project_version})
        self.save()

    def _remember_result(self, stage: Stage, cause: str) -> None:
        if not stage.result and not stage.outcome:
            return
        stage.result_history.append(
            {
                "revision": self.workflow.revision,
                "outcome": stage.outcome,
                "cause": cause,
                "result": dict(stage.result),
            }
        )

    def finish(self, outcome: str = "passed") -> None:
        """Finish an idempotent/no-op workflow without forcing another stage."""
        if not self.workflow:
            return
        stage = self.current_stage()
        if stage and stage.execution_status not in ("completed", "errored"):
            stage.execution_status = "completed"
            stage.outcome = "cancelled" if outcome == "cancelled" else "passed"
        self.workflow.execution_status = "completed"
        self.workflow.outcome = outcome
        self._event(
            "workflow_cancelled" if outcome == "cancelled" else "workflow_completed",
            {"workflow_id": self.workflow.workflow_id, "outcome": outcome},
        )
        self.save()

    def save(self) -> str:
        if not self.workflow:
            return self.path
        self.workflow.validate()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        from ..state.shared_state import relativize_tree_paths

        payload = relativize_tree_paths(parent, self.workflow.to_dict())
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return self.path

    def digest(self) -> Dict[str, Any]:
        if not self.workflow:
            return {}
        stage = self.current_stage()
        index = next((i for i, item in enumerate(self.workflow.stages, 1) if item.id == self.workflow.current_stage), 0)
        wait_kind = (
            "input"
            if self.workflow.intent.missing_slots
            or self.workflow.intent.validation_errors
            else "approval"
            if stage and stage.checkpoint
            else "denied"
            if self.workflow.execution_status == "waiting"
            and _is_mutation_denied(stage)
            else "recovery"
            if self.workflow.execution_status == "waiting"
            else ""
        )
        waiting_reason = ""
        if wait_kind == "approval" and stage and stage.checkpoint:
            waiting_reason = stage.checkpoint.reason
        elif wait_kind == "input":
            waiting_reason = "待补充或修正：{}".format(
                ", ".join(
                    list(self.workflow.intent.missing_slots)
                    + list(self.workflow.intent.validation_errors)
                )
            )
        elif wait_kind == "denied":
            waiting_reason = str(
                ((stage.result if stage else {}) or {}).get("note")
                or "改图被拒绝，工程保持不变。"
            )
        elif wait_kind == "recovery":
            waiting_reason = str(
                ((stage.result if stage else {}) or {}).get("note")
                or "当前 Stage 未满足完成条件，请重试、调整方案或取消。"
            )
        return {
            "workflow_id": self.workflow.workflow_id,
            "task_type": self.workflow.task_type,
            "task_label": _TASK_LABELS.get(self.workflow.task_type, self.workflow.task_type),
            "execution_status": self.workflow.execution_status,
            "outcome": self.workflow.outcome,
            "current_stage": self.workflow.current_stage,
            "stage_label": _STAGE_LABELS.get(
                self.workflow.current_stage, self.workflow.current_stage
            ),
            "stage_index": index,
            "stage_total": len(self.workflow.stages),
            "waiting_reason": waiting_reason,
            "wait_kind": wait_kind,
            "interaction_request": (
                {
                    "kind": wait_kind,
                    "reason": waiting_reason,
                    "checkpoint_id": (
                        stage.checkpoint.id
                        if wait_kind == "approval" and stage and stage.checkpoint
                        else ""
                    ),
                }
                if wait_kind
                else {}
            ),
            "checkpoint_id": (
                stage.checkpoint.id
                if wait_kind == "approval" and stage and stage.checkpoint
                else ""
            ),
            "revision": self.workflow.revision,
            "base_project_version": self.workflow.base_project_version,
            "capabilities": list(self.workflow.intent.capabilities),
            "missing_slots": list(self.workflow.intent.missing_slots),
            "validation_errors": list(self.workflow.intent.validation_errors),
            "max_duration_seconds": (
                self.workflow.intent.slots.get("max_duration_seconds")
                or self.workflow.intent.slots.get("duration_seconds")
            ),
            "stages": [
                {
                    "id": item.id,
                    "label": _STAGE_LABELS.get(item.id, item.id),
                    "execution_status": item.execution_status,
                    "outcome": item.outcome,
                    "attempt": item.attempt,
                    "max_attempts": item.max_attempts,
                    "completion": list(item.completion),
                    "completion_result": dict(item.result.get("completion") or {}),
                }
                for item in self.workflow.stages
            ],
        }

    @staticmethod
    def _decision(text: str) -> str:
        normalized = (text or "").strip().lower()
        if normalized in _APPROVE or any(word in normalized for word in ("确认执行", "同意修改", "继续执行")):
            return "approved"
        if any(word in normalized for word in _REJECT_HINTS):
            return "rejected"
        return ""

    def _activate_current(self) -> None:
        stage = self.current_stage()
        if not stage:
            return
        if "checkpoint" in stage.interaction:
            if stage.interaction == "conditional_checkpoint" and not self._checkpoint_required(stage):
                stage.execution_status = "completed"
                stage.outcome = "passed"
                stage.result = {
                    "completion": {name: True for name in stage.completion},
                    "acceptance": {"decision": "not_required", "decided_at": time.time()},
                    "note": "not_required",
                }
                self._event("stage_skipped", {"stage_id": stage.id, "reason": "not_required"})
                self._transition(stage.transitions.get("not_required", "completed"))
                return
            reason = self._checkpoint_reason(stage)
            stage.execution_status = "waiting"
            stage.attempt = max(int(stage.attempt or 0), 1)
            stage.checkpoint = Checkpoint(
                id=f"cp-{uuid.uuid4().hex[:8]}",
                reason=reason,
                action=stage.id,
            )
            self.workflow.execution_status = "waiting"
            self._event(
                "checkpoint_opened",
                {"stage_id": stage.id, "reason": reason, "checkpoint_id": stage.checkpoint.id},
            )

    def _checkpoint_required(self, stage: Stage) -> bool:
        if stage.id in _ALIGNMENT_STAGES:
            return bool(
                self.workflow.intent.missing_slots
                or self.workflow.intent.validation_errors
            )
        if stage.id == "change_confirmation":
            return self.workflow.intent.slots.get("change_type") != "single_parameter"
        return True

    def _checkpoint_reason(self, stage: Stage) -> str:
        return ", ".join(
            self.workflow.intent.missing_slots
            + self.workflow.intent.validation_errors
        ) or str(
            self.workflow.intent.slots.get("change_type")
            or _STAGE_LABELS.get(stage.id, stage.id)
        )

    def _transition(self, target: str) -> None:
        if target == "completed":
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "passed"
            self._event("workflow_completed", {"workflow_id": self.workflow.workflow_id})
            return
        if target == "cancelled":
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "cancelled"
            self._event("workflow_cancelled", {"workflow_id": self.workflow.workflow_id})
            return
        if target == "stop":
            self.workflow.execution_status = "errored"
            self.workflow.outcome = "inconclusive"
            return
        if target == "waiting_user":
            self.workflow.execution_status = "waiting"
            return
        if not self.workflow.stage(target):
            raise ValueError(f"Stage 转移目标不存在: {target}")
        self.workflow.current_stage = target
        next_stage = self.current_stage()
        if next_stage.execution_status in ("completed", "errored"):
            self._remember_result(next_stage, "retry")
            next_stage.execution_status = "pending"
            next_stage.outcome = ""
            next_stage.result = {}
        self.workflow.execution_status = "pending"
        self._activate_current()

    def _event(self, event: str, payload: Dict[str, Any]) -> None:
        if self._event_sink:
            data = dict(payload or {})
            if self.workflow:
                stage = self.current_stage()
                data.setdefault("workflow_id", self.workflow.workflow_id)
                data.setdefault("workflow_revision", self.workflow.revision)
                data.setdefault("task_type", self.workflow.task_type)
                data.setdefault("stage_id", self.workflow.current_stage)
                data.setdefault("attempt", stage.attempt if stage else 0)
            self._event_sink(event, data)


def _is_mutation_denied(stage: Any) -> bool:
    """True when waiting because a mutating tool was refused this turn."""
    result = (stage.result if stage else {}) or {}
    note = str(result.get("note") or "")
    if any(marker in note for marker in ("禁止改图", "本轮禁止")):
        return True
    if "DENY" in note:
        return True
    codes = (result.get("acceptance") or {}).get("failure_codes") or []
    return "REPLY_STATUS_REJECTED" in codes and "禁止改图" in note


def _result_fingerprint(result: Dict[str, Any], ok: bool, outcome: str) -> str:
    payload = {
        "ok": ok,
        "outcome": outcome,
        "completion": result.get("completion") or {},
        "produced_claims": result.get("produced_claims") or [],
        "proposed_changes": result.get("proposed_changes") or [],
        "note": result.get("note") or "",
        "artifacts": result.get("artifacts") or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


_CAPABILITIES = frozenset(
    {
        "diagnose",
        "modify_project",
        "build_rx",
        "build_tx",
        "build_signal",
        "hardware_configure",
        "observe",
        "realtime_observe",
        "signal_agnostic_observe",
        "protocol",
        "deploy",
        "hardware_runtime",
    }
)
_PROMPT = """你是 DeepRadio 的 Intent 校正器。只输出一个 JSON 对象。
字段:
- task_type: END_TO_END_SIM / TX_BUILD / RX_BUILD / DIAGNOSE / MODIFY_PROJECT / OBSERVE / HARDWARE_CONFIGURE
- capabilities: 用户最终要做的事，只能用给定集合
- forbidden_capabilities: 用户明确不要的能力
- slots: 文本里明确出现的参数
- confidence: 0~1
规则:
- 否定、只仿真、不要硬件/射频 优先于关键词；不要因为出现「硬件」就打开 hardware_configure
- 「停在发射确认」表示 deploy_permission=pending、terminal_checkpoint=rf_plan_confirmation，
  不是禁止硬件只读预检；task_type 保持 HARDWARE_CONFIGURE，build_tx 只参与 Stage 组合
- 「不要发射」才表示 deploy_permission=forbidden；不得把二者合并
- 不要因为「发射流图」就把配置任务改成 TX_BUILD
- 不得把实时硬件观察改写成离线仿真
- 不得因为 2.4GHz 就判定 BLE，除非用户说了 ble/蓝牙/发射广播
- 用户已给出的槽位不得覆盖
- 不要发明 local_name 或 operation=deploy
"""


def complete_intent(
    rules_intent: WorkflowIntent, text: str, shared_state: Any
) -> WorkflowIntent:
    """Merge rules Intent with an optional LLM patch (may drop forbidden capabilities)."""
    try:
        from ..llm import chat, is_configured
    except Exception:  # noqa: BLE001
        return rules_intent
    if not is_configured():
        return rules_intent
    payload = {
        "text": text,
        "rules_intent": {
            "task_type": rules_intent.task_type,
            "confidence": rules_intent.confidence,
            "slots": rules_intent.slots,
            "missing_slots": rules_intent.missing_slots,
            "capabilities": rules_intent.capabilities,
            "forbidden_capabilities": list(
                (rules_intent.context or {}).get("forbidden_capabilities") or []
            ),
            "slot_sources": rules_intent.slot_sources,
        },
        "allowed_task_types": sorted(_TASK_TYPES),
        "allowed_capabilities": sorted(_CAPABILITIES),
        "has_project": bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
        ),
    }
    try:
        content = chat(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )
        parsed = _parse_json_object(content)
    except Exception as exc:  # noqa: BLE001
        logger.info("Intent LLM 补全失败，沿用规则分类: %s", exc)
        return rules_intent
    return _merge(rules_intent, parsed)


def _merge(rules: WorkflowIntent, parsed: dict[str, Any]) -> WorkflowIntent:
    task_type = str(parsed.get("task_type") or rules.task_type)
    if task_type not in _TASK_TYPES:
        task_type = rules.task_type
    forbidden = {
        name for name in list(parsed.get("forbidden_capabilities") or [])
        + list((rules.context or {}).get("forbidden_capabilities") or [])
        if name in _CAPABILITIES
    }
    checkpoint_prepare = (
        rules.slots.get("terminal_checkpoint") == "rf_plan_confirmation"
        and rules.slots.get("deploy_permission") == "pending"
    )
    if checkpoint_prepare:
        # Reaching a TX checkpoint requires read-only preflight/probe.  It is
        # deferred runtime authority, not an instruction to skip the gates.
        forbidden.discard("hardware_runtime")
    raw_caps = parsed.get("capabilities")
    if isinstance(raw_caps, list) and raw_caps:
        capabilities = [name for name in raw_caps if name in _CAPABILITIES]
    else:
        capabilities = list(rules.capabilities)
    capabilities = [name for name in capabilities if name not in forbidden]
    if checkpoint_prepare:
        for name in ("hardware_configure", "hardware_runtime"):
            if name in rules.capabilities and name not in capabilities:
                capabilities.append(name)
    slots = dict(rules.slots)
    sources = dict(rules.slot_sources)
    for key, value in dict(parsed.get("slots") or {}).items():
        if value in (None, "", []):
            continue
        if sources.get(key) == "user":
            continue
        slots[key] = value
        sources[key] = "llm"
    try:
        confidence = float(parsed.get("confidence", rules.confidence))
    except (TypeError, ValueError):
        confidence = rules.confidence
    confidence = min(1.0, max(rules.confidence, confidence, 0.0))
    context = dict(rules.context)
    if forbidden:
        context["forbidden_capabilities"] = sorted(forbidden)
    return WorkflowIntent(
        raw_text=rules.raw_text,
        turn_relation=rules.turn_relation,
        task_type=task_type,
        confidence=confidence,
        slots=slots,
        missing_slots=list(rules.missing_slots),
        capabilities=capabilities,
        slot_sources=sources,
        context=context,
        validation_errors=list(rules.validation_errors),
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Intent LLM 返回值不是对象")
    return data
