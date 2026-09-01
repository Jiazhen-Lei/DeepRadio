"""Deterministic single-workflow, serial-stage state machine."""

from __future__ import annotations

import hashlib
import logging
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional

from .completion import (
    EXTERNAL_PRECONDITION_COMPLETIONS,
    KNOWN_COMPLETIONS,
    external_waiting_note,
)
from .plan_compiler import (
    PlanCoverageError,
    _PROTECTED_TAIL_IDS,
    compact_invocations,
    compact_workflow_payload,
    compile_stages,
    compiled_plan_summary,
    plan_needs_proposal,
    replan_tail,
    tail_needs_replan_proposal,
)
from .planning import (
    EffectLevel,
    highest_effect,
    is_rf_grant_effect,
    normalize_effect,
    project_intent_ir,
    split_at_decision_boundary,
    stage_display_label,
    stage_plan_item,
    stops_at_boundary,
    system_capability_blocker,
)
from .schema import (
    STAGE_EXECUTION_MODES,
    Checkpoint,
    Stage,
    Workflow,
    WorkflowIntent,
)
from ..knowledge.spec_requirements import normalize_direction
from ..tools.hardware_profiles import resolve_hardware_profile

logger = logging.getLogger(__name__)


def _atomic_replace(src: str, dst: str, *, retries: int = 5) -> None:
    """Replace a state file, tolerating short Windows file-lock races."""
    delay = 0.02
    for attempt in range(max(1, int(retries))):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == max(1, int(retries)) - 1:
                raise
            time.sleep(delay)
            delay *= 1.6


_TERMINALS = {"completed", "errored"}
_NON_STAGE_TARGETS = _TERMINALS | {"cancelled", "stop", "waiting_user"}
_TEST_APPROVE = frozenset({
    "确认", "同意", "继续", "确认执行", "确认修改",
    "approve", "confirm", "confirmed", "yes", "ok",
})
_TEST_REJECT_HINTS = ("取消", "拒绝", "不同意", "不要执行", "cancel")
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
    "END_TO_END_SIM": "End-to-End Simulation",
    "TX_BUILD": "Build Transmit Chain",
    "RX_BUILD": "Build Receive Chain",
    "DIAGNOSE": "Diagnose Project",
    "MODIFY_PROJECT": "Modify Existing Project",
    "OBSERVE": "Observe Project",
    "HARDWARE_CONFIGURE": "Configure SDR",
}
_STAGE_LABELS = {
    "spec_alignment": "Specification Alignment",
    "rx_spec_alignment": "Receiver Specification Alignment",
    "build_and_verify": "Build and Verify",
    "tx_build_and_validate": "Build and Validate Transmitter",
    "rx_build_and_verify": "Build and Verify Receiver",
    "inspect_and_diagnose": "Inspect and Diagnose",
    "repair_confirmation": "Repair Confirmation",
    "repair_and_verify": "Repair and Reverify",
    "inspect_and_plan": "Inspect and Plan",
    "change_confirmation": "Change Confirmation",
    "apply_and_verify": "Apply and Reverify",
    "inspect_and_measure": "Inspect and Measure",
    "hardware_precheck": "Hardware Precheck",
    "hardware_confirmation": "Hardware Confirmation",
    "configure_and_check": "Configure and Check",
    "protocol_spec_alignment": "BLE Specification Alignment",
    "build_ble_advertiser": "Build BLE Advertiser",
    "offline_protocol_verify": "Offline BLE Protocol Verification",
    "flowgraph_confirmation": "Flowgraph Review",
    "discover_and_probe_device": "Discover and Probe Selected SDR",
    "rf_plan_confirmation": "RF Plan Confirmation",
    "configure_device": "Configure Device Parameters",
    "transmit_bounded": "Bounded Transmission",
    "over_air_verification": "LightBlue Over-the-Air Verification",
    "stop_and_finalize": "Stop and Finalize Audit",
    "discover_and_probe_hardware": "Discover and Probe Hardware",
    "run_bounded": "Bounded Runtime",
    "runtime_observation": "Runtime Result Confirmation",
    "stop_runtime": "Stop Hardware Runtime",
}

_ALIGNMENT_STAGES = frozenset(
    {"spec_alignment", "rx_spec_alignment", "protocol_spec_alignment"}
)
_FLOWGRAPH_REVIEW_STAGES = frozenset({"flowgraph_confirmation"})
_HARDWARE_ENTRY_IDS = frozenset({
    "hardware_precheck",
    "discover_and_probe_hardware",
    "discover_and_probe_device",
})
_ARTIFACT_REPLAY_IDS = frozenset({
    "build_and_verify",
    "tx_build_and_validate",
    "rx_build_and_verify",
    "apply_and_verify",
    "build_ble_advertiser",
    "offline_protocol_verify",
})
_QUESTION_STARTERS = (
    "which", "what", "how many", "how much", "why", "where", "explain",
)


def _looks_like_question(text: str) -> bool:
    """Test-bypass heuristic; production classifies questions with the LLM."""
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith("?"):
        return True
    low = raw.lower()
    return any(low.startswith(prefix) for prefix in _QUESTION_STARTERS)
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
_TX_AUTHORIZE = re.compile(
    r"(?:现在|开始|批准|授权|立刻|立即)\s*(?:发射|运行|开机)|"
    r"(?:发射|运行|transmit)\s*(?:现在|now)|"
    r"transmit\s+now|start\s+(?:tx|transmit|rf)|"
    r"authorize\s+(?:tx|rf|transmit)|"
    r"(?:运行|发射|持续)\s*[:=为]?\s*\d+(?:\.\d+)?\s*(?:秒|s\b)",
    re.I,
)
_DEVICE_ACCESS_FORBIDDEN = re.compile(
    r"(?:不要|不|禁止|勿|别)\s*(?:访问|探测|连接|打开)\s*(?:设备|硬件|sdr|radio)|"
    r"without\s+(?:accessing|probing|connecting)\s+(?:the\s+)?(?:device|hardware|sdr)",
    re.I,
)
_EBN0_LABELED = re.compile(
    r"(?:eb\s*/?\s*n0|ebn0)\s*[:=为]?\s*"
    r"([-+]?\d+(?:\.\d+)?)\s*(?:db|分贝)?",
    re.I,
)
_EBN0_ANSWER = re.compile(
    r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:db|分贝)?\s*$",
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


def _parse_ebn0_db(text: str, *, allow_bare_answer: bool = False) -> float | None:
    """Parse Eb/N0 from a labeled phrase, or a short follow-up like ``8dB``."""
    raw = (text or "").strip()
    if not raw:
        return None
    labeled = _EBN0_LABELED.search(raw)
    if labeled:
        return float(labeled.group(1))
    if allow_bare_answer:
        bare = _EBN0_ANSWER.fullmatch(raw)
        if bare:
            return float(bare.group(1))
    return None


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


def _offline_read_only(text: str) -> bool:
    """Test-bypass approximation; never used by the GUI/API semantic path."""
    low = str(text or "").lower()
    return any(marker in low for marker in (
        "不修改", "保持工程不变", "先保持", "只观察", "仅观察",
        "只诊断", "仅诊断", "read only", "read-only", "do not modify",
    ))


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
    # V2 §5.5: a compound request keeps the task candidate with the largest
    # end-state scope.  "Build BPSK through AWGN with EVM below 10%" carries
    # a diagnose capability only as the post-build verification step, so a
    # build capability must outrank it.  A genuine diagnosis request has no
    # build capability and still maps to DIAGNOSE below (previously the
    # first rule here swallowed END_TO_END_SIM/RX_BUILD requests).
    build_caps = caps & {"build_signal", "build_rx", "build_tx"}
    # LLM 是语义权威:它明确判定 OBSERVE 且本轮带观测能力时,不能被 diagnose
    # 规则吞掉。"查看频谱和星座图并报告主峰,只观察不修改"(手册 Task 6)会同时
    # 带出 diagnose 能力(读指标),但终态产物是测量结果而非诊断结论。
    observe_caps = caps & {"observe", "realtime_observe"}
    if current == "OBSERVE" and observe_caps and not build_caps:
        return "OBSERVE"
    if "diagnose" in caps and not build_caps:
        return "DIAGNOSE"
    # V2 §5.5: Task 类型按最终产物、验收方式和**安全边界**划分。
    # operation=prepare 是 LLM 明确判定的执行效果边界("停在发射确认"),语义
    # 强度高于 modify_project 这个附带的"保存配置"副作用能力;若让
    # modify_project 先命中,手册 Task 7 就会丢掉硬件 Stage 与 RF 确认点。
    if (
        "hardware_configure" in caps
        and slots.get("operation") == "prepare"
        and "hardware_configure" not in blocked
    ):
        return "HARDWARE_CONFIGURE"
    if "modify_project" in caps:
        return "MODIFY_PROJECT"
    if (
        "protocol" in caps
        and slots.get("operation") == "deploy"
        and "hardware_configure" not in blocked
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
    # V2 §5.5: 复合请求取终态范围最大的候选。端到端的判据是**发与收同时存在**
    # (build_tx + build_rx);``build_signal`` 只是"要有激励信号"的辅助能力,
    # 例如 "self-contained BPSK receiver"(手册 Task 3)会带 build_signal 生成
    # 内部测试激励,但终态产物仍是接收机,必须落 RX_BUILD。
    if {"build_tx", "build_rx"} <= caps:
        return "END_TO_END_SIM"
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


#: Task Type -> 该候选的 Stage 能真正承载的核心能力。
#: 只列"必须由这个 Task 的 Stage 才能完成"的能力;像 observe / diagnose 这类
#: 每个候选的验证 Stage 都能顺带完成的能力不在此列,否则会把普通仿真请求
#: 误判成"无法承载"。
_TASK_CORE_CAPABILITIES = {
    "END_TO_END_SIM": {"build_signal", "build_tx", "build_rx"},
    "TX_BUILD": {"build_tx", "build_signal"},
    "RX_BUILD": {"build_rx", "build_signal"},
    "DIAGNOSE": {"diagnose"},
    "MODIFY_PROJECT": {"modify_project"},
    "OBSERVE": {"observe", "realtime_observe", "signal_agnostic_observe"},
    "HARDWARE_CONFIGURE": {
        "hardware_configure", "hardware_runtime", "deploy", "protocol",
    },
}
#: 需要独立 Stage 编排与安全边界的能力:LLM 选的候选若覆盖不了它们,
#: 说明这个 task_type 执行不下去(会丢 Stage 或丢确认点),必须归一化。
_STAGE_CRITICAL_CAPABILITIES = {
    "build_tx", "build_rx", "build_signal", "modify_project",
    "hardware_configure", "hardware_runtime", "deploy",
}


def _reconcile_task_type(
    llm_task_type: str,
    capabilities: list[str],
    *,
    slots: Dict[str, Any] | None = None,
    forbidden: list[str] | None = None,
) -> str:
    """以 LLM 判定为准,仅在它无法承载自身 capabilities 时才归一化。

    V2 §5.1 把 task_type 定义为"兼容标签",真正决定执行的是 capabilities 与
    Stage 编排。因此这里的契约是:

    1. LLM 给出的 task_type 只要能覆盖它自己列出的 stage-critical 能力,就
       原样采纳 —— 语义权威属于 LLM,规则不得改写(这正是手册 Task 6 里
       ``OBSERVE`` 曾被 ``diagnose`` 规则改成 ``DIAGNOSE`` 的问题)。
    2. 只有当 LLM 的候选装不下它要求的能力时(例如判 ``END_TO_END_SIM`` 却
       要求 ``hardware_configure`` + ``hardware_runtime``,而该候选没有硬件
       Stage 与 RF 确认点),才交给确定性投影重选,避免执行阶段丢 Stage。

    这样不额外增加一轮 LLM 调用,又消除了"规则无条件覆盖 LLM"。
    """
    caps = [name for name in (capabilities or []) if name in _CAPABILITIES]
    slot_values = dict(slots or {})
    if llm_task_type not in _TASK_TYPES:
        return _task_type_from_capabilities(
            caps, llm_task_type, slots=slot_values, forbidden=forbidden
        )
    # 单向链路有独立的验收契约:RX_BUILD 要 receive_quality_evaluated(BER),
    # TX_BUILD 只做结构校验。LLM 把明确的单向请求判成 END_TO_END_SIM 时会丢掉
    # 这个契约,而 direction 是它自己给出的槽位,足以判定,无需再问一轮。
    direction = str(slot_values.get("direction") or "").lower()
    if (
        llm_task_type == "END_TO_END_SIM"
        and direction in ("rx", "tx")
        and not {"build_tx", "build_rx"} <= set(caps)
    ):
        return "RX_BUILD" if direction == "rx" else "TX_BUILD"
    required = set(caps) & _STAGE_CRITICAL_CAPABILITIES
    covered = _TASK_CORE_CAPABILITIES.get(llm_task_type, set())
    if required <= covered:
        return llm_task_type
    return _task_type_from_capabilities(
        caps, llm_task_type, slots=slot_values, forbidden=forbidden
    )


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
        self._turn_intent_patch: Dict[str, Any] = {}
        if self.workflow:
            self._recover_interrupted()
            self.refresh_system_capabilities()

    def _load_catalog(self) -> Dict[str, Any]:
        with open(self.catalog_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        candidates = data.get("task_candidates")
        profiles = data.get("stage_profiles")
        if data.get("schema_version") != 1 or not isinstance(candidates, dict):
            raise ValueError("Unsupported Task Catalog")
        if not isinstance(profiles, dict):
            raise ValueError("Task Catalog is missing stage_profiles")
        for stage_id, profile in profiles.items():
            if not stage_id or not isinstance(profile, dict):
                raise ValueError("Task Catalog contains an invalid stage profile")
            mode = str(profile.get("execution_mode") or "")
            if mode not in STAGE_EXECUTION_MODES:
                raise ValueError(
                    f"Stage profile {stage_id} has invalid execution_mode: {mode}"
                )
            if not isinstance(profile.get("allowed_tools"), list):
                raise ValueError(
                    f"Stage profile {stage_id} must declare allowed_tools"
                )
        for task_type, candidate in candidates.items():
            stage_sets = [list(candidate.get("stages") or [])]
            if candidate.get("deploy_stages"):
                stage_sets.append(list(candidate.get("deploy_stages") or []))
            if candidate.get("runtime_stages"):
                stage_sets.append(list(candidate.get("runtime_stages") or []))
            for stages in stage_sets:
                for stage in stages:
                    stage_id = str(stage.get("id") or "")
                    profile = profiles.get(stage_id)
                    if profile is None:
                        raise ValueError(
                            f"Task {task_type} stage {stage_id} has no stage profile"
                        )
                    for key, value in profile.items():
                        stage.setdefault(key, list(value) if isinstance(value, list) else value)
                self._validate_catalog_stages(task_type, stages)
        from ..tools import registry

        registry.load_all()
        known_tools = set(registry.names()) | {"design_flowgraph"}
        for stage_id, profile in profiles.items():
            unknown = set(profile.get("allowed_tools") or []) - known_tools
            if unknown:
                raise ValueError(
                    f"Stage profile {stage_id} uses unknown tools: {sorted(unknown)}"
                )
        return data

    @staticmethod
    def _validate_catalog_stages(task_type: str, stages: list[Dict[str, Any]]) -> None:
        ids = [str(stage.get("id") or "") for stage in stages]
        if not task_type or not stages or any(not stage_id for stage_id in ids):
            raise ValueError(f"Task {task_type!r} has no valid stage")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Task {task_type} contains duplicate stage IDs")
        for stage in stages:
            unknown_agents = set(stage.get("recommended_agents") or []) - _KNOWN_AGENTS
            unknown_completion = set(stage.get("completion") or []) - KNOWN_COMPLETIONS
            if unknown_agents:
                raise ValueError(f"Task {task_type} uses unknown subagents: {sorted(unknown_agents)}")
            if unknown_completion:
                raise ValueError(
                    f"Task {task_type} uses unknown completion conditions: {sorted(unknown_completion)}"
                )
            if stage.get("execution_mode") not in STAGE_EXECUTION_MODES:
                raise ValueError(
                    f"Task {task_type} stage {stage.get('id')} has invalid execution_mode"
                )
            for target in (stage.get("on") or {}).values():
                if target not in ids and target not in _TERMINAL_TARGETS:
                    raise ValueError(
                        f"Task {task_type} stage {stage.get('id')} has an unknown transition target: {target}"
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
        # A persisted workflow cannot truthfully be ``completed/passed`` while
        # a deferred decision horizon still exists.  Older plans could inherit
        # a fragment's ``passed -> completed`` edge and strand the remaining
        # hardware/RF stages.  Recover the first unfinished active stage (or
        # materialize the next horizon) instead of treating the next user turn
        # as a brand-new task.
        if (
            self.workflow.execution_status == "completed"
            and self.workflow.outcome == "passed"
            and self.workflow.deferred_plan
        ):
            next_stage = self._first_unfinished_active_stage()
            if next_stage is None:
                added = self._materialize_next_horizon()
                next_stage = added[0] if added else None
            if next_stage is not None:
                self.workflow.current_stage = next_stage.id
                self.workflow.execution_status = (
                    "waiting"
                    if next_stage.execution_status == "waiting"
                    else "pending"
                )
                self.workflow.outcome = ""
                changed = True
                self._event("premature_completion_recovered", {
                    "next_stage": next_stage.id,
                    "deferred_stage_count": len(self.workflow.deferred_plan or []),
                })
        if changed:
            self._event("workflow_recovered", self.digest())
            self.save()

    def _first_unfinished_active_stage(self) -> Optional[Stage]:
        if not self.workflow:
            return None
        unfinished = {"pending", "invalidated", "waiting", "running"}
        return next(
            (
                stage for stage in self.workflow.stages
                if stage.execution_status in unfinished
            ),
            None,
        )

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
            if relation == "question":
                self.save()
                return self.workflow
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
                    if current.id in _FLOWGRAPH_REVIEW_STAGES:
                        self._replay_artifact_stages()
                        return self.workflow
                    current.checkpoint.reason = self._checkpoint_reason(
                        current,
                        requested_effect=current.checkpoint.requested_effect,
                    )
                    self.workflow.revision += 1
                    self.save()
                return self.workflow
            if relation in ("answer", "feedback", "adjustment"):
                # A control turn such as retry/continue carries no radio
                # parameters.  Re-running full intent extraction for feedback
                # used to turn "Confirm to transmit" into a second,
                # underspecified task and overwrite SharedIntent.  Only an
                # answer or an explicit adjustment may patch intent slots.
                if relation in ("answer", "adjustment"):
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
        elif self.workflow:
            relation = self._turn_relation(text, shared_state)
            self.workflow.intent.turn_relation = relation
            if relation == "question":
                self.save()
                return self.workflow

        intent = self.classify(text, shared_state)
        return self.instantiate(
            self._reconcile_intent(intent, text, shared_state), shared_state
        )

    def _turn_relation(self, text: str, shared_state: Any) -> str:
        # Understanding user input is the LLM's job exclusively.  Production
        # never re-interprets a turn with keyword rules; the deterministic
        # branch below only serves the unit-test bypass.
        self._turn_intent_patch = {}
        low = (text or "").lower().strip()
        current = self.current_stage()
        relation = self._turn_relation_llm(text)
        if relation is not None:
            return relation
        # Deterministic fallback: only reached by the unit-test bypass.
        if any(hint in low for hint in ("终止任务", "取消任务", "cancel task")):
            return "cancel"
        if current and current.execution_status == "waiting" and current.checkpoint:
            decision = self._decision(text)
            if decision:
                return "approval" if decision == "approved" else "rejection"
            if _looks_like_question(text):
                return "question"
        if _looks_like_question(text):
            return "question"
        if self._is_explicit_new_task(low):
            return "new_task"
        if current and current.execution_status == "waiting" and current.checkpoint:
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

    _RELATION_PROMPT = """你是 DeepRadio 的会话关系判定器；你也是会话语义解析器。当前有一个活动工作流,用户发来了新一轮输入。
只输出一个 JSON 对象:
{"relation":"...","reason":"...","turn_semantics":{"read_only":false,"confirmation_decision":"none","recipe_switch_target":""},"intent_patch":{"slots":{},"capabilities":[]}}
relation 只能取:
- new_task: 一个明确的新目标(与当前工作流不同的能力/方向/工件)
- adjustment: 对当前任务参数的补充或修改(例如指定 local name、频率、时长、设备型号、信道)
- answer: 回答当前工作流正在等待的问题
- feedback: 对等待/失败的反馈(重试、继续、启动、请求诊断)
- approval: 批准当前确认点(如"确认"、"同意"、"可以发射")
- rejection: 拒绝当前确认点(如"不行"、"拒绝"、"不要发射")
- cancel: 取消当前任务
- question: 对当前规格、流图、协议、硬件或阶段的提问;不修改参数、不批准确认点
规则:
- 补充参数和修改参数都是 adjustment,绝不是 new_task。例如 "the local name must be X" 是对当前发射任务的参数补充。
- 询问事实、含义、为什么、有多少、哪一个信道等是 question,即使当前有确认点。question 不得推进工作流。
- question 仅限"只用已有信息就能回答"的提问。要求系统**去做诊断/测量/观测**并产出结论、证据或建议(例如 "diagnose the EVM ... explain the cause and give a suggestion"、"查看当前频谱并报告主峰")的,是新目标 new_task,即使句中含 explain/why;这类请求需要新建 DIAGNOSE / OBSERVE 工作流,不能当成 question 只答不做。
- 当前工作流已 completed 时,新一轮实质请求默认是 new_task,不要再判 adjustment/feedback。
- 只有用户明确提出与当前工作流不同的新目标才是 new_task。
- current_workflow 只是背景;不要因为文本里提到协议名或设备名就判定 new_task。
- 当前没有确认点(stage_status 不是 waiting 或没有 checkpoint)时,不要输出 approval/rejection。
- has_checkpoint=true 且 stage_status=waiting 时，"confirm to transmit" / "proceed" 等短控制轮是 approval，不是新的发射任务，不得重新提取规格字段。
- read_only 仅在用户明确要求只读、只诊断、不要修改时为 true。
- confirmation_decision 只能为 approved/rejected/none，并与 relation 保持一致。
- recipe_switch_target 只在用户明确要求切换现有工程配方时填写给定的 canonical recipe id；否则为空。
- intent_patch.slots 只包含本轮文本明确新增或修改的参数，不复制 current_workflow.current_slots。
- intent_patch.capabilities 只包含本轮明确新增的能力；普通确认、反馈和参数回答应为空。
- 参数键必须使用 canonical 名称；不得从 current_workflow 推测用户本轮没有表达的参数。
- 拿不准时优先 adjustment。
"""

    def _turn_relation_llm(self, text: str) -> Optional[str]:
        """Classify the turn relation with the LLM as the only authority.

        Returns ``None`` only when the deterministic unit-test bypass is
        active.  Any production configuration or request failure raises
        ``SemanticUnderstandingError`` — the turn is never silently
        re-interpreted by keyword rules.
        """
        from ..llm import (
            SemanticUnderstandingError,
            chat,
            get_config,
            intent_test_bypass_enabled,
            is_configured,
        )

        if not is_configured():
            if intent_test_bypass_enabled():
                return None
            self._event("turn_relation_llm_failed", {"reason": "not_configured"})
            raise SemanticUnderstandingError(
                "The language model is not configured, so your message was not "
                "interpreted. Check the model connection and send it again."
            )
        stage = self.current_stage()
        workflow = self.workflow
        payload = {
            "text": str(text or ""),
            "allowed_recipe_ids": _known_recipe_ids(),
            "current_workflow": {
                "task_type": getattr(workflow, "task_type", "") if workflow else "",
                "task_label": _TASK_LABELS.get(
                    getattr(workflow, "task_type", ""), ""
                ) if workflow else "",
                "capabilities": list(
                    getattr(getattr(workflow, "intent", None), "capabilities", None)
                    or []
                ) if workflow else [],
                "current_stage": getattr(stage, "id", "") if stage else "",
                "stage_status": getattr(stage, "execution_status", "") if stage else "",
                "has_checkpoint": bool(
                    stage is not None and getattr(stage, "checkpoint", None)
                ),
                "workflow_status": (
                    getattr(workflow, "execution_status", "") if workflow else ""
                ),
                "missing_slots": list(
                    getattr(getattr(workflow, "intent", None), "missing_slots", None)
                    or []
                ) if workflow else [],
                "current_slots": dict(
                    getattr(getattr(workflow, "intent", None), "slots", None)
                    or {}
                ) if workflow else {},
            },
        }
        emit_payload = {
            "text_hash": hashlib.sha256(
                str(text or "").encode("utf-8")
            ).hexdigest(),
        }
        self._event("turn_relation_llm_started", emit_payload)
        started_at = time.perf_counter()
        try:
            content = chat(
                [
                    {"role": "system", "content": self._RELATION_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            from ..llm import parse_json_object

            parsed = parse_json_object(content)
            relation = str(parsed.get("relation") or "")
            allowed = {
                "new_task", "adjustment", "answer", "feedback",
                "approval", "rejection", "cancel", "question",
            }
            if relation not in allowed:
                raise ValueError(f"invalid relation: {relation!r}")
            semantics = _normalize_turn_semantics(parsed)
            semantics["relation"] = relation
            if relation == "approval":
                semantics["confirmation_decision"] = "approved"
            elif relation in {"rejection", "cancel"}:
                semantics["confirmation_decision"] = "rejected"
            raw_patch = parsed.get("intent_patch")
            if not isinstance(raw_patch, Mapping):
                raw_patch = {}
            patch_slots = raw_patch.get("slots")
            patch_capabilities = raw_patch.get("capabilities")
            self._turn_intent_patch = {
                "slots": dict(patch_slots) if isinstance(patch_slots, Mapping) else {},
                "capabilities": [
                    str(item) for item in patch_capabilities
                    if isinstance(item, str)
                ] if isinstance(patch_capabilities, list) else [],
            }
        except Exception as exc:  # noqa: BLE001
            self._event("turn_relation_llm_failed", {
                **emit_payload,
                "reason": "request_failed",
                "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            })
            raise SemanticUnderstandingError(
                "The language model could not classify your message, so it was "
                "not interpreted. Nothing was changed; check the model "
                "connection and send it again."
            ) from exc
        self._event("turn_relation_llm_succeeded", {
            **emit_payload,
            "relation": relation,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
        })
        if self.workflow is not None:
            self.workflow.intent.context["turn_semantics"] = semantics
        return relation

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
        patch = dict(self._turn_intent_patch or {})
        updates = {
            str(key): value
            for key, value in dict(patch.get("slots") or {}).items()
            if value not in (None, "", [])
        }
        if not patch:
            # Deterministic parsing is retained only for isolated unit tests.
            # Production always receives an LLM-produced intent_patch above.
            update = self._reconcile_intent(
                self.classify(text, shared_state), text, shared_state
            )
            updates = {
                key: value
                for key, value in update.slots.items()
                if value not in (None, "", [])
            }
        missing_before = list(self.workflow.intent.missing_slots or [])
        if (
            "ebn0_db" in missing_before
            and updates.get("ebn0_db") is None
        ):
            from ..llm import intent_test_bypass_enabled

            if intent_test_bypass_enabled():
                parsed = _parse_ebn0_db(text, allow_bare_answer=True)
                if parsed is not None:
                    updates["ebn0_db"] = parsed
        self.workflow.intent.slots.update(updates)
        self.workflow.intent.slot_sources.update(
            {key: "llm" for key in updates}
        )
        self._turn_intent_patch = {}
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

    @staticmethod
    def _tx_authorize_requested(text: str) -> bool:
        raw = text or ""
        if _TX_CONFIRM_ONLY.search(raw) or _TX_FORBIDDEN.search(raw):
            return False
        return bool(_TX_AUTHORIZE.search(raw))

    @staticmethod
    def _inherit_tx_preview_slots(
        text: str,
        slots: Dict[str, Any],
        slot_sources: Dict[str, str],
        shared_state: Any,
    ) -> None:
        """Reuse a saved unarmed TX preview when the user authorizes RF."""
        if not WorkflowEngine._tx_authorize_requested(text):
            return
        if slots.get("operation") in ("prepare", "configure"):
            return
        if slots.get("deploy_permission") == "forbidden":
            return
        project = getattr(shared_state, "project", None)
        config = dict(getattr(project, "config", None) or {})
        path = str(getattr(project, "grc_path", "") or "")
        if (
            path
            and str(config.get("direction") or "") == "tx"
            and config.get("hardware")
            and not bool(config.get("rf_armed"))
        ):
            inherited = {
                "hardware": config.get("hardware"),
                "direction": "tx",
                "carrier_frequency": config.get("carrier_frequency"),
                "sample_rate": config.get("sample_rate"),
                "bandwidth": config.get("rf_bandwidth") or config.get("sample_rate"),
                "tx_gain": config.get("tx_gain"),
                "tx_attenuation": config.get("tx_attenuation"),
            }
            for key, value in inherited.items():
                if value in (None, "", []) or slots.get(key) not in (None, "", []):
                    continue
                slots[key] = value
                slot_sources[key] = "current_project"
        if slots.get("hardware"):
            slots["operation"] = "deploy"
            slots.setdefault("deploy_permission", "requested")
            slot_sources.setdefault("operation", "rules")
            slot_sources.setdefault("deploy_permission", "rules")

    def classify(self, text: str, shared_state: Any) -> WorkflowIntent:
        from ..llm import intent_test_bypass_enabled

        if not intent_test_bypass_enabled():
            return self._production_intent_seed(text, shared_state)
        low = (text or "").lower()
        has_project = bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
            or getattr(getattr(shared_state, "project", None), "config", {}).get("recipe")
        )
        slots = self._parse_slots(text)
        # Regex parsing only generates candidates.  It must not masquerade as
        # semantic authority or prevent the LLM from correcting ambiguity.
        slot_sources = {
            key: "rules" for key, value in slots.items() if value not in (None, "", [])
        }
        self._inherit_tx_preview_slots(text, slots, slot_sources, shared_state)
        capabilities = self._detect_capabilities(text, slots, has_project)
        task_type = _task_type_from_capabilities(
            capabilities, "END_TO_END_SIM", slots=slots
        )
        if (
            slots.get("hardware")
            and slots.get("observation_mode") == "realtime"
            and slots.get("direction") == "rx"
        ):
            slots["signal_source_scope"] = "live_device"
            slot_sources["signal_source_scope"] = "derived"
        elif (
            task_type == "RX_BUILD"
            and "ber" in (slots.get("requested_metrics") or [])
            and not slots.get("hardware")
        ):
            slots["signal_source_scope"] = "generated_fixture"
            slot_sources["signal_source_scope"] = "derived"
        elif task_type == "OBSERVE" and has_project:
            slots["signal_source_scope"] = "current_project_offline"
            slot_sources["signal_source_scope"] = "current_project"
        project_config = getattr(getattr(shared_state, "project", None), "config", {})
        for key in ("operation", "deploy_permission", "hardware_access"):
            if key in slots and slot_sources.get(key) == "user":
                slot_sources[key] = "rules"
        if slots.get("deploy_permission") == "forbidden":
            slot_sources["deploy_permission"] = "user"
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
                        r"(?:运行|持续|时长|发射|duration|最多|最长|不超过|up to|at most|no more than)\s*"
                        r"(?:改为|改成|换成|设为|设置为|[:=为])?\s*"
                        r"\d+(?:\.\d+)?\s*(?:秒|s|sec(?:onds?)?)"
                        r"|\d+(?:\.\d+)?\s*(?:seconds?|秒)\b",
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
        elif slots.get("protocol") == "wifi":
            for key in ("wifi_role", "modulation"):
                if key in slots:
                    slot_sources[key] = "derived"
            if re.search(r"(?:ssid|网络名称|热点名称)", low) and slots.get("ssid"):
                slot_sources["ssid"] = "user"
        context = {
            "current_project": {
                "grc_path": str(getattr(getattr(shared_state, "project", None), "grc_path", "") or ""),
                "recipe": str(project_config.get("recipe") or ""),
                "modulation": str(project_config.get("modulation") or ""),
                "channel": str(project_config.get("channel") or ""),
                "hardware": str(project_config.get("hardware") or ""),
                "direction": str(project_config.get("direction") or ""),
                "carrier_frequency": project_config.get("carrier_frequency"),
                "sample_rate": project_config.get("sample_rate"),
                "preview_mode": str(project_config.get("preview_mode") or ""),
                "rf_armed": bool(project_config.get("rf_armed")),
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
        if slots.get("operation") == "deploy" or "deploy" in capabilities:
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
        project_intent_ir(intent)
        return intent

    @staticmethod
    def _production_intent_seed(text: str, shared_state: Any) -> WorkflowIntent:
        """Build only host-owned context before production LLM extraction.

        The former implementation ran regex slot/capability classification
        before every model call.  Even though merge precedence was improved,
        those guesses still influenced missing-field checks and audit output.
        Production now starts with no semantic claims at all: the only seed is
        the current project's factual configuration.  The larger deterministic
        parser remains reachable solely under the unittest bypass.
        """
        project = getattr(shared_state, "project", None)
        config = dict(getattr(project, "config", None) or {})
        path = str(getattr(project, "grc_path", "") or "")
        current_project = {
            "grc_path": path,
            "recipe": str(config.get("recipe") or ""),
            "modulation": str(config.get("modulation") or ""),
            "channel": str(config.get("channel") or ""),
            "hardware": str(config.get("hardware") or ""),
            "direction": str(config.get("direction") or ""),
            "carrier_frequency": config.get("carrier_frequency"),
            "sample_rate": config.get("sample_rate"),
            "bandwidth": config.get("rf_bandwidth") or config.get("bandwidth"),
            "tx_gain": config.get("tx_gain"),
            "tx_attenuation": config.get("tx_attenuation"),
            "preview_mode": str(config.get("preview_mode") or ""),
            "rf_armed": bool(config.get("rf_armed")),
        }
        context = {"current_project": current_project} if path or any(
            value not in (None, "", False) for key, value in current_project.items()
            if key != "grc_path"
        ) else {}
        return WorkflowIntent(
            raw_text=str(text or ""),
            turn_relation="new_task",
            task_type="END_TO_END_SIM",
            confidence=0.0,
            slots={},
            missing_slots=[],
            capabilities=[],
            slot_sources={},
            context=context,
            validation_errors=[],
        )

    def _reconcile_intent(
        self, intent: WorkflowIntent, text: str, shared_state: Any
    ) -> WorkflowIntent:
        """Use the LLM result as the sole production semantic authority."""
        from ..llm import intent_test_bypass_enabled

        # Offline keyword semantics are available only to deterministic unit
        # tests.  The GUI/API path never derives forbidden capabilities,
        # confirmation, read-only intent, or task identity from vocabulary.
        if intent_test_bypass_enabled():
            forbidden = set(_offline_forbidden(text))
            if _offline_read_only(text):
                forbidden.add("modify_project")
                intent.context.setdefault("turn_semantics", {})["read_only"] = True
            intent.context.setdefault("forbidden_capabilities", sorted(forbidden))
        self._event("intent_context_seeded", {
            "task_type": intent.task_type,
            "capabilities": list(intent.capabilities),
            "slots": dict(intent.slots),
            "slot_sources": dict(intent.slot_sources),
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
        intent = complete_intent(
            intent,
            text,
            shared_state,
            event_sink=self._event,
        )
        semantics = dict((intent.context or {}).get("turn_semantics") or {})
        semantic_read_only = semantics.get("read_only") is True
        if "diagnose" in intent.capabilities and semantic_read_only:
            forbidden = set(intent.context.get("forbidden_capabilities") or [])
            forbidden.add("modify_project")
            intent.context["forbidden_capabilities"] = sorted(forbidden)
        intent.capabilities = [
            name for name in intent.capabilities
            if name not in set(intent.context.get("forbidden_capabilities") or ())
        ]
        llm_task_type = intent.task_type
        intent.task_type = _reconcile_task_type(
            llm_task_type,
            intent.capabilities,
            slots=intent.slots,
            forbidden=list(intent.context.get("forbidden_capabilities") or []),
        )
        if intent.task_type != llm_task_type:
            self._event("intent_task_normalized", {
                "llm_task_type": llm_task_type,
                "normalized_task_type": intent.task_type,
                "capabilities": list(intent.capabilities),
                "reason": "task_type_cannot_cover_capabilities",
            })
        intent.missing_slots = self._missing_slots(
            intent.task_type, intent.slots, shared_state, intent.capabilities
        )
        intent.validation_errors = self._validate_slots(intent.slots)
        project_intent_ir(intent)
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
        modify = (
            any(word in low for word in _MODIFY_HINTS)
            and not _offline_read_only(text)
        )
        build = any(word in low for word in _BUILD_HINTS)
        rx = slots.get("direction") == "rx"
        tx = slots.get("direction") == "tx"
        hardware = bool(slots.get("hardware")) or _hardware_affirmed(text)
        observe = bool(slots.get("requested_metrics")) or any(word in low for word in _OBSERVE_HINTS)
        realtime = any(word in low for word in ("实时", "live", "real-time", "realtime"))

        add("diagnose", diagnose)
        add("modify_project", modify and (has_project or bool(slots.get("target_project"))))
        add("build_rx", (build and rx) or (hardware and observe and realtime and rx))
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
            or slots.get("operation") in {"deploy", "prepare"}
            or any(word in low for word in (
                "启动", "运行", "发射", "transmit", "run", "start",
            ))
        ) and slots.get("hardware_access") != "forbidden"
          and slots.get("deploy_permission") != "forbidden")
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
        ebn0 = _parse_ebn0_db(text or "", allow_bare_answer=False)
        if ebn0 is None and (
            "ber" in metrics or "ber" in low or "误码率" in (text or "")
        ):
            unlabeled = re.search(
                r"([-+]?\d+(?:\.\d+)?)\s*(?:db|分贝)",
                low,
            )
            if unlabeled:
                ebn0 = float(unlabeled.group(1))
        if ebn0 is not None:
            slots["ebn0_db"] = ebn0
        if any(word in low for word in ("直接部署", "部署", "deploy")):
            slots["operation"] = "deploy"
            slots["deploy_permission"] = "requested"
        if _TX_CONFIRM_ONLY.search(text or ""):
            slots["operation"] = "prepare"
            slots["deploy_permission"] = "pending"
            slots["hardware_access"] = "read_only_probe"
        elif _TX_FORBIDDEN.search(text or ""):
            slots["operation"] = "configure"
            slots["deploy_permission"] = "forbidden"
            slots["hardware_access"] = "configuration_only"
        if _DEVICE_ACCESS_FORBIDDEN.search(text or ""):
            slots["hardware_access"] = "forbidden"
        duration = re.search(
            r"(?:运行|持续|时长|发射|duration|最多|最长|不超过|up to|at most|no more than)\s*"
            r"(?:改为|改成|换成|设为|设置为|[:=为])?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec(?:onds?)?)",
            low,
        )
        if not duration:
            duration = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:seconds?|秒)\b",
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
                    "direction": "tx" if any(
                        word in low for word in ("发射", "部署", "transmit", "deploy")
                    ) else slots.get("direction", ""),
                    "modulation": "gfsk",
                    "advertising_channels": [37],
                    "carrier_frequency": 2_402_000_000.0,
                    "sample_rate": 2_000_000.0,
                    "duration_seconds": slots.get("duration_seconds", 30.0),
                    "max_duration_seconds": slots.get("duration_seconds", 30.0),
                    "tx_gain": 0.0,
                }
            )
        elif any(marker in low for marker in ("wi-fi", "wifi", "802.11")):
            slots["protocol"] = "wifi"
            if any(word in low for word in ("发射", "发送", "transmit", " tx")):
                slots["direction"] = "tx"
                slots["operation"] = (
                    "deploy"
                    if any(word in low for word in ("部署", "运行", "发射", "transmit"))
                    else "configure"
                )
            elif any(word in low for word in ("接收", "receive", " rx")):
                slots["direction"] = "rx"
            if any(word in low for word in ("beacon", "ssid", "热点", "ap帧", "ap 帧")):
                slots["wifi_role"] = "beacon"
            elif slots.get("direction") == "tx":
                slots["wifi_role"] = "frame_tx"
            elif slots.get("direction") == "rx":
                slots["wifi_role"] = "frame_rx"
            slots.setdefault("modulation", "ofdm")
            ssid = re.search(
                r"(?:ssid|网络名称|热点名称)\s*"
                r"(?:改为|改成|换成|设为|设置为|为|=|:|：)?\s*"
                r"([A-Za-z0-9_-]{1,32})",
                text,
                flags=re.IGNORECASE,
            )
            if ssid:
                slots["ssid"] = ssid.group(1)
        name = re.search(
            r"(?:local\s*name|localname|本地名称|设备名称)"
            r".{0,48}?"
            r"(?:to be|改为|改成|换成|设为|设置为|为|=|:|：)\s*"
            r"['\"]?([A-Za-z0-9_-]{1,26})",
            text,
            flags=re.IGNORECASE,
        )
        if name:
            slots["local_name"] = name.group(1)
        observation_receiver = bool(re.search(
            r"(?:observ(?:e|ed|ation)|success condition|verify).{0,48}\breceivers?\b"
            r"|\breceivers?\b.{0,48}(?:observ|verif)",
            low,
        ))
        if (
            any(word in low for word in ("接收机", "解调", " rx"))
            or re.search(r"(?:天线口.*接收|接收.*(?:频谱|信号|波形))", low)
            or (re.search(r"\breceivers?\b", low) and not observation_receiver)
        ):
            slots["direction"] = "rx"
        elif any(
            word in low
            for word in ("发射机", "transmitter", "发射链", "发射流图", " tx")
        ):
            slots["direction"] = "tx"
        elif modulation:
            slots["direction"] = "sim"
        target_project = re.search(
            r"(?:直接)?(?:修改|打开|基于|modify)\s*([A-Za-z_]\w*)",
            text,
            flags=re.IGNORECASE,
        )
        if target_project:
            slots["target_project"] = target_project.group(1)
        units = {"g": 1e9, "m": 1e6, "k": 1e3, "": 1.0}
        patterns = {
            "carrier_frequency": r"(?:中心频率|载频|carrier(?: frequency)?)\s*(?:改为|改成|换成|设为|设置为|[:=为])?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "sample_rate": r"(?:采样率|sample rate)\s*(?:改为|改成|换成|设为|设置为|[:=为])?\s*(\d+(?:\.\d+)?)\s*([gmk]?)s?(?:ps|hz)",
            "bandwidth": r"(?:带宽|bandwidth)\s*(?:改为|改成|换成|设为|设置为|[:=为])?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "symbol_rate": r"(?:符号率|symbol rate)\s*(?:改为|改成|换成|设为|设置为|[:=为])?\s*(\d+(?:\.\d+)?)\s*([gmk]?)(?:baud|sym/s)",
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
        explicit_hardware_tx = bool(
            device
            and any(
                word in low
                for word in ("发射", "开始发送", "transmit", "start tx")
            )
            and slots.get("direction") != "rx"
        )
        if explicit_hardware_tx:
            slots["direction"] = "tx"
        if slots.get("direction") not in (None, "", []):
            slots["direction"] = normalize_direction(slots.get("direction"))
        if (
            explicit_hardware_tx
            and slots.get("deploy_permission") not in {"pending", "forbidden"}
        ):
            slots["operation"] = "deploy"
            slots["deploy_permission"] = "requested"
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
        semantic_success = re.search(
            r"(?:成功条件|验收条件|判定成功|success conditions?|acceptance)\s*"
            r"(?:为|是|[:：=])?\s*"
            r"([^，。；\n]+)",
            str(text or ""),
            re.I,
        )
        if semantic_success:
            success.append(semantic_success.group(1).strip())
        observed = re.search(
            r"(?:observed by|observe(?:d)? (?:it )?(?:in|on|with)|"
            r"verify (?:it )?(?:in|on|with))\s+([^.\n]+)",
            str(text or ""),
            re.I,
        )
        if observed:
            success.append(observed.group(0).strip())
        if success:
            slots["success_conditions"] = list(dict.fromkeys(success))
        channel_hits = [
            int(item) for item in re.findall(r"channel\s*(?:to\s*)?(\d+)", low)
        ]
        switch_channel = re.search(
            r"(?:switch|change|move|set).{0,40}channel\s*(?:to\s*)?(\d+)",
            low,
        )
        if switch_channel:
            channel_hits.append(int(switch_channel.group(1)))
        if channel_hits:
            channel = channel_hits[-1]
            if channel in (37, 38, 39):
                slots["advertising_channels"] = [channel]
                slots["carrier_frequency"] = {
                    37: 2_402_000_000.0,
                    38: 2_426_000_000.0,
                    39: 2_480_000_000.0,
                }[channel]
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
        hardware_diagnosis = task_type == "DIAGNOSE" and bool(
            slots.get("hardware")
            or set(capabilities).intersection({"hardware_configure", "hardware_runtime"})
        )
        if task_type in {"DIAGNOSE", "MODIFY_PROJECT", "OBSERVE"} and not hardware_diagnosis:
            project = getattr(shared_state, "project", None)
            project_ready = bool(
                getattr(project, "grc_path", "")
                or getattr(project, "config", {}).get("recipe")
                # The user explicitly confirmed "the canvas project is the
                # current project" through the sole-choice interaction, so
                # the field is answered even if the host state has not yet
                # registered the canvas file.
                or slots.get("current_project") == "current_canvas"
            )
            if not project_ready:
                missing.append("current_project")
        if task_type == "HARDWARE_CONFIGURE" or "hardware_configure" in capabilities:
            for key in ("hardware", "carrier_frequency", "sample_rate"):
                if not slots.get(key):
                    missing.append(key)
        return missing

    @staticmethod
    def _validate_slots(slots: Dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if (
            str(slots.get("protocol") or "").lower() == "ble"
            and str(slots.get("modulation") or "gfsk").lower() != "gfsk"
        ):
            errors.append("modulation_incompatible_with_ble")
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

    @staticmethod
    def _blocking_unbound_actions(unbound_actions: list[str]) -> set[str]:
        """Unbound device/RF-grade planner actions must block execution."""
        blocking: set[str] = set()
        for action in unbound_actions:
            name = str(action or "")
            if name in _PROTECTED_TAIL_IDS:
                blocking.add(name)
                continue
            try:
                from ..tools import registry

                spec = registry.get(name)
            except Exception:  # noqa: BLE001
                spec = None
            if spec is None:
                continue
            effect = str(getattr(spec, "effect_level", "") or "")
            if effect in {"DEVICE_CONFIG", "RF_RUN"}:
                blocking.add(name)
        return blocking

    def _archive_superseded_workflow(self) -> list[dict[str, Any]]:
        """Keep a superseded workflow visible as a Previous Attempt.

        Called whenever a new workflow replaces an active (non-terminal) one.
        The archive is a compact summary — the monitor shows what was running
        and how far it got, without resurrecting its execution authority.
        """
        existing = self.workflow
        if existing is None or existing.execution_status in _TERMINALS:
            # Terminal workflows are history already; only a superseded
            # active workflow becomes a Previous Attempt.
            return list(getattr(existing, "previous_attempts", None) or []) if existing else []
        summary = {
            "workflow_id": existing.workflow_id,
            "task_type": existing.task_type,
            "task_label": _TASK_LABELS.get(existing.task_type, existing.task_type),
            "execution_status": existing.execution_status,
            "outcome": existing.outcome,
            "current_stage": existing.current_stage,
            "stage_label": _STAGE_LABELS.get(
                existing.current_stage, existing.current_stage
            ),
            "stages": [
                {
                    "id": stage.id,
                    "label": _STAGE_LABELS.get(stage.id, stage.id),
                    "status": stage.execution_status,
                    "outcome": stage.outcome,
                }
                for stage in existing.stages
            ],
            "superseded_at": time.time(),
        }
        chain = list(getattr(existing, "previous_attempts", None) or [])
        chain.append(summary)
        self._event("workflow_archived", {
            "workflow_id": existing.workflow_id,
            "task_type": existing.task_type,
            "stage_count": len(existing.stages),
        })
        return chain[-5:]

    def instantiate(self, intent: WorkflowIntent, shared_state: Any) -> Workflow:
        candidate = self.catalog["task_candidates"].get(intent.task_type)
        if not candidate:
            raise ValueError(f"Unknown task type: {intent.task_type}")
        stages = self._compose_stages(intent, candidate)
        if not stages:
            raise ValueError(f"Task {intent.task_type} has no executable stage")
        from .llm_planner import propose_plan

        proposal = (
            propose_plan(
                intent,
                shared_state,
                catalog=self.catalog,
                event_sink=self._event,
            )
            if plan_needs_proposal(intent, stages)
            else None
        )
        stages, _nodes, rejected, unbound_actions = compile_stages(
            intent, stages, catalog=self.catalog, proposal=proposal
        )
        if unbound_actions:
            # Plan Coverage Validator: a planner action that no stage can
            # serve must never be silently dropped.  Device/RF-grade actions
            # block execution; informational ones are recorded for audit.
            blocking = self._blocking_unbound_actions(unbound_actions)
            self._event("plan_actions_unbound", {
                "workflow_id": self.workflow.workflow_id if self.workflow else "",
                "unbound_actions": list(unbound_actions),
                "blocking": sorted(blocking),
            })
            if blocking:
                raise PlanCoverageError(
                    "The plan was rejected because required actions could not "
                    "be compiled into any stage: {}. Restate the goal or "
                    "remove these actions.".format(", ".join(sorted(blocking)))
                )
        horizon, deferred = split_at_decision_boundary(stages)
        version = int(getattr(getattr(shared_state, "project", None), "flowgraph_version", 0))
        runtime = getattr(shared_state, "runtime", None)
        if runtime is not None:
            # Runtime quality belongs to one execution, not the whole GUI
            # session.  Historical Failed claims remain available separately.
            runtime.quality = "clean"
            runtime.warnings = []
        deferred_items = [stage_plan_item(item) for item in deferred]
        shared_intent_ref = dict((intent.context or {}).get("shared_intent") or {})
        shared_intent_id = str(shared_intent_ref.get("intent_id") or "")
        previous_attempts = self._archive_superseded_workflow()
        self.workflow = Workflow(
            workflow_id=(
                "wf-" + shared_intent_id.removeprefix("intent-")
                if shared_intent_id else f"wf-{uuid.uuid4().hex[:8]}"
            ),
            task_type=intent.task_type,
            intent=intent,
            stages=list(horizon),
            deferred_plan=deferred_items,
            compiled_plan=compiled_plan_summary(horizon, deferred_items),
            base_project_version=version,
            current_stage=horizon[0].id,
            catalog_version=int(self.catalog.get("schema_version", 1)),
            previous_attempts=previous_attempts,
        )
        if rejected:
            self._event("plan_actions_rejected", {"actions": rejected})
        self._event("plan_compiled", {
            "stage_ids": [stage.id for stage in horizon],
            "deferred_stage_ids": [
                str(item.get("id") or "") for item in deferred_items
            ],
            "rejected_actions": list(rejected),
        })
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
        """Compose capability groups. task_type is only a compatibility label.

        Fragments come from capability membership.  The Plan Compiler then
        attaches PlanNodes and enforces generic effect/truncation policy.
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
        groups = self._insert_flowgraph_review(groups)
        for index, group in enumerate(groups[:-1]):
            next_stage = groups[index + 1][0].id
            for stage in group:
                for outcome, target in list(stage.transitions.items()):
                    if target == "completed":
                        stage.transitions[outcome] = next_stage

        stages = [stage for group in groups for stage in group]
        if len({stage.id for stage in stages}) != len(stages):
            raise ValueError("The capability composition produced duplicate stage IDs")

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
        # Physical device presence is deliberately NOT a build-stage
        # completion: until RF is authorized the builder can only emit a
        # null-sink preview, so requiring an active hardware endpoint here
        # made every safe-preview build structurally "waiting".  Device
        # presence is verified by the discover_and_probe_* stage instead,
        # and structural endpoint evidence by the builder's claims.
        capabilities = set(intent.capabilities)
        if "hardware_configure" in capabilities:
            for stage in stages:
                if stage.id in {"build_and_verify", "tx_build_and_validate", "rx_build_and_verify", "apply_and_verify"}:
                    if "signal_agnostic_observe" in capabilities:
                        stage.completion = [
                            name for name in stage.completion
                            if name not in {"receive_quality_evaluated", "measurement_completed"}
                        ]
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

    def _insert_flowgraph_review(self, groups: list[list[Stage]]) -> list[list[Stage]]:
        """Pause after an artifact build before any hardware stage.

        Confirming the flowgraph is a generic decision boundary, not a
        protocol-specific script.  Catalog fragments that already include
        ``flowgraph_confirmation`` are left unchanged.
        """
        if any(
            stage.id in _FLOWGRAPH_REVIEW_STAGES
            for group in groups for stage in group
        ):
            return groups
        inserted: list[list[Stage]] = []
        for index, group in enumerate(groups):
            inserted.append(group)
            if index + 1 >= len(groups) or not group:
                continue
            nxt = groups[index + 1]
            if not nxt:
                continue
            produces_flowgraph = any(
                "flowgraph_saved" in (stage.completion or []) for stage in group
            )
            if produces_flowgraph and nxt[0].id in _HARDWARE_ENTRY_IDS:
                inserted.append([
                    Stage.from_dict({
                        "id": "flowgraph_confirmation",
                        "interaction": "checkpoint",
                        "execution_mode": "checkpoint",
                        "allowed_tools": [],
                        "recommended_agents": ["flowgraph_agent"],
                        "completion": ["flowgraph_decision_recorded"],
                        "depends_on": ["project.flowgraph"],
                        "on": {
                            "approved": nxt[0].id,
                            "rejected": "cancelled",
                        },
                    })
                ])
        return inserted

    def _replay_artifact_stages(self) -> None:
        """Rebuild flowgraph artifacts after a parameter adjustment at review."""
        if not self.workflow:
            return
        current = self.current_stage()
        replay: list[Stage] = []
        for stage in self.workflow.stages:
            if current is not None and stage.id == current.id:
                break
            if (
                stage.id in _ARTIFACT_REPLAY_IDS
                or "flowgraph_saved" in (stage.completion or [])
            ):
                replay.append(stage)
        targets = list(replay)
        if current is not None:
            targets.append(current)
        if not replay:
            if current is not None:
                current.checkpoint = None
                current.execution_status = "pending"
                current.outcome = ""
            self.workflow.execution_status = "pending"
            self.save()
            return
        for stage in targets:
            stage.execution_status = "pending"
            stage.outcome = ""
            stage.attempt = 0
            stage.result = {}
            stage.checkpoint = None
            stage.resume_pending = False
        self.workflow.current_stage = replay[0].id
        self.workflow.execution_status = "pending"
        self.workflow.revision += 1
        self._event("artifact_stages_replayed", {
            "stages": [stage.id for stage in replay],
            "resume_checkpoint": current.id if current else "",
        })
        self.save()

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
            raise ValueError("There is no active workflow")
        data = result if isinstance(result, dict) else vars(result)
        stage = self.current_stage()
        if not stage:
            raise ValueError("There is no current stage")
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
        quality = str(data.get("quality") or "clean")
        if quality not in ("clean", "warning", "failed"):
            quality = "failed"
        if missing_completion and all(
            name in EXTERNAL_PRECONDITION_COMPLETIONS
            for name in missing_completion
        ):
            # Missing external preconditions (no SDR connected, endpoint not
            # armed yet, device mismatch) are not execution failures.  Park
            # the stage as waiting so the user can fix the condition and
            # retry, instead of being shown a hard "failed" verdict.
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
                    "quality",
                    "evidence_grade",
                )
                if data.get(key)
            }
            stage.result["quality"] = quality
            stage.result["missing_completion"] = missing_completion
            stage.result["note"] = str(
                data.get("note")
                or external_waiting_note(missing_completion)
            )
            stage.execution_status = "waiting"
            stage.outcome = "inconclusive"
            self.workflow.execution_status = "waiting"
            self._event("stage_waiting_external", {
                "stage_id": stage.id,
                "attempt": stage.attempt,
                "missing_completion": missing_completion,
                "note": stage.result["note"],
            })
            self.save()
            return True
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
                "quality",
                "evidence_grade",
            )
            if data.get(key)
        }
        if data.get("invocations"):
            stage.result["invocations"] = compact_invocations(
                list(data.get("invocations") or [])
            )
        stage.result["quality"] = quality
        quality_rank = {"clean": 0, "warning": 1, "failed": 2}
        if quality_rank[quality] > quality_rank.get(self.workflow.quality, 0):
            self.workflow.quality = quality
        fingerprint = _result_fingerprint(stage.result, ok, outcome)
        stage.result["fingerprint"] = fingerprint
        if missing_completion:
            stage.result["missing_completion"] = missing_completion
        if data.get("errored"):
            if data.get("resume_from"):
                stage.resume_from = str(data.get("resume_from") or "")
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
        if not ok or stage.outcome == "failed":
            if data.get("resume_from"):
                stage.resume_from = str(data.get("resume_from") or "")
        else:
            stage.resume_from = ""
        target = stage.transitions.get(transition_key)
        if target is None and transition_key == "passed":
            target = stage.transitions.get("completed")
        if target is None and stage.outcome == "failed":
            target = stage.transitions.get("failed")
        resolved_target = target or ("completed" if ok else "waiting_user")
        if (
            resolved_target == "waiting_user"
            and stage.outcome != "passed"
            and not stage.checkpoint
        ):
            stage.execution_status = "waiting"
        self._transition(resolved_target)
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
            raise ValueError("There is no current stage")
        checkpoint = Checkpoint(
            id=f"cp-{uuid.uuid4().hex[:8]}",
            purpose=self._checkpoint_purpose(stage),
            reason=reason or stage_display_label(
                stage.id,
                _STAGE_LABELS.get(stage.id, stage.id),
                stage.checkpoint.requested_effect if stage.checkpoint else "",
            ),
            action=action,
            payload_ref=payload_ref,
            resume_stage=True,
        )
        stage.execution_status = "waiting"
        stage.checkpoint = checkpoint
        self.workflow.execution_status = "waiting"
        self._event(
            "checkpoint_opened",
            {"stage_id": stage.id, "reason": checkpoint.reason,
             "checkpoint_id": checkpoint.id, "purpose": checkpoint.purpose},
        )
        self.save()
        return checkpoint

    def resolve_checkpoint(self, decision: str) -> None:
        stage = self.current_stage()
        if not stage or not stage.checkpoint or stage.execution_status != "waiting":
            raise ValueError("There is no pending checkpoint")
        if stage.checkpoint.blocker:
            raise ValueError(
                stage.checkpoint.blocker.get("message")
                or "The required system capability is unavailable, so this confirmation cannot be accepted"
            )
        normalized = "approved" if decision == "approved" else "rejected"
        stage.checkpoint.decision_status = normalized
        self.workflow.decisions.append({
            "checkpoint_id": stage.checkpoint.id,
            "stage_id": stage.id,
            "decision": normalized,
            "requested_effect": stage.checkpoint.requested_effect,
            "purpose": stage.checkpoint.purpose,
            "decided_at": time.time(),
        })
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
        if normalized == "approved" and stops_at_boundary(self.workflow.intent, stage.id):
            self.workflow.deferred_plan = []
            self._transition("completed")
            self.save()
            return
        target = stage.transitions.get(normalized, "completed")
        if normalized == "approved":
            # Expand the already-compiled grant first.  LLM replan may only
            # refine what remains deferred after that horizon.
            if target not in _NON_STAGE_TARGETS:
                self._materialize_until(target)
            self._replan_remaining_tail()
        self._transition(target)
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
            elif name in (
                "hardware_decision_recorded",
                "change_decision_recorded",
                "flowgraph_decision_recorded",
            ):
                completion[name] = True
            else:
                completion[name] = approved
        return {
            "completion": completion,
            "acceptance": {
                "decision": decision,
                "purpose": checkpoint.purpose if checkpoint else "generic_approval",
                "checkpoint_id": checkpoint.id if checkpoint else "",
                "decided_at": time.time(),
                "run_id": observation.get("run_id") or "",
                "evidence_id": observation.get("artifact") or "",
                "evidence_sha256": observation.get("sha256")
                or observation.get("artifact_sha256")
                or "",
                "evidence_complete": bool(observation.get("artifact")),
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

        payload = compact_workflow_payload(
            relativize_tree_paths(parent, self.workflow.to_dict())
        )
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _atomic_replace(tmp, self.path)
        return self.path

    def _waiting_digest(self, stage: Optional[Stage]) -> tuple[str, str, Dict[str, Any]]:
        """Project the current waiting state once for API and GUI consumers."""
        waiting = self.workflow.execution_status == "waiting"
        checkpoint = stage.checkpoint if stage else None
        pending_checkpoint = bool(
            waiting
            and checkpoint is not None
            and checkpoint.decision_status == "pending"
        )
        if waiting and (
            self.workflow.intent.missing_slots
            or self.workflow.intent.validation_errors
        ):
            kind = "input"
        elif pending_checkpoint and checkpoint.blocker:
            kind = "capability"
        elif pending_checkpoint:
            kind = "approval"
        elif waiting and _is_mutation_denied(stage):
            kind = "denied"
        elif waiting:
            kind = "recovery"
        else:
            kind = ""
        if kind == "capability":
            reason = str(
                checkpoint.blocker.get("message")
                or "The required system capability is unavailable."
            )
        elif kind == "approval":
            reason = str(checkpoint.reason or "")
        elif kind == "input":
            reason = "Provide or correct: {}".format(
                ", ".join(
                    list(self.workflow.intent.missing_slots)
                    + list(self.workflow.intent.validation_errors)
                )
            )
        elif kind == "denied":
            reason = str(
                ((stage.result if stage else {}) or {}).get("note")
                or "The flowgraph change was rejected; the project remains unchanged."
            )
        elif kind == "recovery":
            reason = str(
                ((stage.result if stage else {}) or {}).get("note")
                or "The current stage did not meet its completion conditions. Retry, revise the plan, or cancel."
            )
        else:
            reason = ""
        blocker = dict(checkpoint.blocker) if (
            checkpoint and checkpoint.blocker
        ) else {}
        if (
            kind == "recovery"
            and stage is not None
            and stage.attempt >= stage.max_attempts
        ):
            reason = (
                f"{stage_display_label(stage.id, _STAGE_LABELS.get(stage.id, stage.id), '')} "
                f"reached its attempt limit ({stage.max_attempts}). "
                "Revise the plan or cancel this workflow."
            )
            blocker = {
                "code": "ATTEMPT_LIMIT_REACHED",
                "message": reason,
                "stage_id": stage.id,
                "attempt": stage.attempt,
                "max_attempts": stage.max_attempts,
            }
        return kind, reason, blocker

    @staticmethod
    def _intent_ir_digest(intent: WorkflowIntent) -> Dict[str, Any]:
        return {
            "goals": list(intent.goals),
            "requested_operations": list(intent.requested_operations),
            "desired_artifacts": list(intent.desired_artifacts),
            "evidence_requirements": list(intent.evidence_requirements),
            "constraints": dict(intent.constraints),
            "forbidden_effects": list(intent.forbidden_effects),
            "decision_boundaries": list(intent.decision_boundaries),
            "stop_conditions": list(intent.stop_conditions),
            "entities": dict(intent.entities),
        }

    @staticmethod
    def _stage_digest(stage: Stage) -> Dict[str, Any]:
        return {
            "id": stage.id,
            "label": stage_display_label(
                stage.id,
                _STAGE_LABELS.get(stage.id, stage.id),
                stage.checkpoint.requested_effect if stage.checkpoint else "",
            ),
            "execution_status": stage.execution_status,
            "outcome": stage.outcome,
            "attempt": stage.attempt,
            "max_attempts": stage.max_attempts,
            "completion": list(stage.completion),
            "completion_result": dict(stage.result.get("completion") or {}),
            "quality": str(stage.result.get("quality") or "clean"),
            "unbound_predicates": list(stage.unbound_predicates),
            "execution_mode": stage.execution_mode,
        }

    @staticmethod
    def _deferred_stage_digest(item: Mapping[str, Any]) -> Dict[str, Any]:
        stage_id = str(item.get("id") or "")
        return {
            "id": stage_id,
            "label": stage_display_label(
                stage_id, str(item.get("label") or stage_id), ""
            ),
            "execution_status": "deferred",
            "outcome": "",
            "attempt": 0,
            "max_attempts": int(item.get("max_attempts") or 0),
            "completion": list(item.get("completion") or []),
            "completion_result": {},
            "quality": "clean",
            "unbound_predicates": list(item.get("unbound_predicates") or []),
            "execution_mode": str(item.get("execution_mode") or "hybrid"),
        }

    def digest(self) -> Dict[str, Any]:
        if not self.workflow:
            return {}
        stage = self.current_stage()
        index = next((i for i, item in enumerate(self.workflow.stages, 1) if item.id == self.workflow.current_stage), 0)
        wait_kind, waiting_reason, blocker = self._waiting_digest(stage)
        # The monitor always shows the whole plan: completed, current, and
        # upcoming (including deferred) stages.  Hiding the horizon made the
        # workflow look like it "lost" stages after each decision boundary.
        visible_stages = list(self.workflow.stages)
        deferred_stages = [
            self._deferred_stage_digest(item)
            for item in (self.workflow.deferred_plan or [])
            if isinstance(item, Mapping)
        ]
        total_stages = len(visible_stages) + len(deferred_stages)
        return {
            "workflow_id": self.workflow.workflow_id,
            "task_type": self.workflow.task_type,
            "task_label": _TASK_LABELS.get(self.workflow.task_type, self.workflow.task_type),
            "execution_status": self.workflow.execution_status,
            "outcome": self.workflow.outcome,
            "quality": self.workflow.quality,
            "current_stage": self.workflow.current_stage,
            "stage_label": stage_display_label(
                self.workflow.current_stage,
                _STAGE_LABELS.get(
                    self.workflow.current_stage, self.workflow.current_stage
                ),
                (
                    stage.checkpoint.requested_effect
                    if stage and stage.checkpoint else ""
                ),
            ),
            "stage_index": index,
            "stage_total": total_stages,
            "all_stage_total": total_stages,
            "deferred_stage_count": len(self.workflow.deferred_plan or []),
            "waiting_reason": waiting_reason,
            "wait_kind": wait_kind,
            "can_confirm": wait_kind == "approval",
            "needs_confirmation": wait_kind == "approval",
            "blocker": blocker,
            "interaction_request": (
                {
                    "id": (
                        stage.checkpoint.id
                        if stage and stage.checkpoint
                        else "{}:{}:{}:{}".format(
                            self.workflow.workflow_id,
                            self.workflow.current_stage,
                            self.workflow.revision,
                            wait_kind,
                        )
                    ),
                    "kind": wait_kind,
                    "status": "pending",
                    "purpose": (
                        stage.checkpoint.purpose
                        if stage and stage.checkpoint else ""
                    ),
                    "reason": waiting_reason,
                    "checkpoint_id": (
                        stage.checkpoint.id
                        if wait_kind == "approval" and stage and stage.checkpoint
                        else ""
                    ),
                    "allowed_actions": (
                        ["checkpoint_decision", "cancel_workflow"]
                        if wait_kind == "approval"
                        else ["retry_stage", "cancel_workflow"]
                        if wait_kind in {"recovery", "capability"}
                        else ["interaction_response", "cancel_workflow"]
                        if wait_kind in {"input", "intent"}
                        else ["cancel_workflow"]
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
            "checkpoint_purpose": (
                stage.checkpoint.purpose
                if wait_kind == "approval" and stage and stage.checkpoint
                else ""
            ),
            "requested_effect": (
                stage.checkpoint.requested_effect
                if wait_kind == "approval" and stage and stage.checkpoint
                else ""
            ),
            "intent_ir": self._intent_ir_digest(self.workflow.intent),
            "compiled_plan": list(self.workflow.compiled_plan or []),
            "previous_attempts": list(self.workflow.previous_attempts or []),
            "revision": self.workflow.revision,
            "base_project_version": self.workflow.base_project_version,
            "capabilities": list(self.workflow.intent.capabilities),
            "missing_slots": list(self.workflow.intent.missing_slots),
            "validation_errors": list(self.workflow.intent.validation_errors),
            "max_duration_seconds": (
                (
                    self.workflow.intent.slots.get("max_duration_seconds")
                    or self.workflow.intent.slots.get("duration_seconds")
                )
                if (
                    self.workflow.intent.slots.get("operation") == "deploy"
                    or is_rf_grant_effect(
                        stage.checkpoint.requested_effect
                        if stage and stage.checkpoint else ""
                    )
                )
                else None
            ),
            "stages": [
                self._stage_digest(item) for item in visible_stages
            ] + deferred_stages,
        }

    @staticmethod
    def _decision(text: str) -> str:
        normalized = (text or "").strip().lower().rstrip(".,!?")
        if normalized in _TEST_APPROVE or any(word in normalized for word in ("确认执行", "同意修改", "继续执行")):
            return "approved"
        if any(word in normalized for word in _TEST_REJECT_HINTS):
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
            requested_effect = self._checkpoint_requested_effect(stage)
            reason = self._checkpoint_reason(stage, requested_effect=requested_effect)
            capability_blocker = system_capability_blocker(requested_effect)
            stage.execution_status = "waiting"
            stage.attempt = max(int(stage.attempt or 0), 1)
            stage.checkpoint = Checkpoint(
                id=f"cp-{uuid.uuid4().hex[:8]}",
                purpose=self._checkpoint_purpose(stage),
                reason=reason,
                action=stage.id,
                requested_effect=requested_effect,
                blocker=(
                    capability_blocker.to_dict() if capability_blocker else {}
                ),
            )
            self.workflow.execution_status = "waiting"
            self._event(
                "checkpoint_opened",
                {"stage_id": stage.id, "reason": reason,
                 "checkpoint_id": stage.checkpoint.id,
                 "purpose": stage.checkpoint.purpose},
            )

    def _checkpoint_purpose(self, stage: Stage) -> str:
        if stage.id == "rf_plan_confirmation":
            return (
                "config_handoff"
                if self.workflow and stops_at_boundary(self.workflow.intent, stage.id)
                else "rf_authorization"
            )
        if stage.id == "flowgraph_confirmation":
            return "flowgraph_review"
        if stage.id == "over_air_verification":
            return "ota_observation"
        if stage.id == "runtime_observation":
            return "runtime_observation"
        if stage.id in {"change_confirmation", "repair_confirmation"}:
            return "project_mutation"
        if stage.id == "hardware_confirmation":
            return "device_configuration"
        if stage.id in _ALIGNMENT_STAGES:
            return "specification_alignment"
        return "generic_approval"

    def _checkpoint_requested_effect(self, stage: Stage) -> str:
        """Return the highest effect before the next decision boundary.

        A grant covers the bounded segment the user is actually approving, not
        merely the first implementation operation in that segment.  This keeps
        ``configure -> run`` from being mislabeled as a DEVICE_CONFIG-only
        decision and works for catalog and future generated plans alike.
        """
        if not self.workflow:
            return "READ"
        if stage.id in _FLOWGRAPH_REVIEW_STAGES:
            return "ARTIFACT_WRITE"
        if stops_at_boundary(self.workflow.intent, stage.id):
            return max(
                normalize_effect(stage.effect_level),
                EffectLevel.DEVICE_READ,
            ).name
        deferred = list(self.workflow.deferred_plan or [])
        if deferred:
            return highest_effect(deferred).name
        target = stage.transitions.get("approved") or stage.transitions.get("passed")
        highest = EffectLevel.READ
        visited: set[str] = set()
        while target and str(target) not in _TERMINALS and str(target) not in visited:
            stage_id = str(target)
            visited.add(stage_id)
            candidate = self.workflow.stage(stage_id)
            if candidate is None or candidate.safety_finalizer:
                break
            if "checkpoint" in str(candidate.interaction or ""):
                break
            highest = max(highest, normalize_effect(candidate.effect_level))
            # Follow only the ordinary success path. Branches and failures are
            # replanning boundaries and must not broaden an authorization.
            target = (
                candidate.transitions.get("passed")
                or candidate.transitions.get("completed")
                or candidate.transitions.get("approved")
            )
        return highest.name

    def refresh_system_capabilities(self) -> None:
        """Re-evaluate launch-time blockers when a session is restored."""
        if not self.workflow:
            return
        stage = self.current_stage()
        if not stage or not stage.checkpoint:
            return
        requested = stage.checkpoint.requested_effect or self._checkpoint_requested_effect(stage)
        blocker = system_capability_blocker(requested)
        updated = blocker.to_dict() if blocker else {}
        if updated == stage.checkpoint.blocker:
            return
        stage.checkpoint.requested_effect = requested
        stage.checkpoint.blocker = updated
        self._event("system_capability_refreshed", {
            "stage_id": stage.id,
            "requested_effect": requested,
            "available": not bool(updated),
            "blocker": updated,
        })
        self.save()

    def _checkpoint_required(self, stage: Stage) -> bool:
        if stage.id in _ALIGNMENT_STAGES:
            return bool(
                self.workflow.intent.missing_slots
                or self.workflow.intent.validation_errors
            )
        if stage.id == "change_confirmation":
            return self.workflow.intent.slots.get("change_type") != "single_parameter"
        return True

    def _checkpoint_reason(self, stage: Stage, requested_effect: str = "") -> str:
        return ", ".join(
            self.workflow.intent.missing_slots
            + self.workflow.intent.validation_errors
        ) or str(
            self.workflow.intent.slots.get("change_type")
            or stage_display_label(
                stage.id,
                _STAGE_LABELS.get(stage.id, stage.id),
                requested_effect or (
                    stage.checkpoint.requested_effect if stage.checkpoint else ""
                ),
            )
        )

    def ensure_stage(self, stage_id: str) -> Optional[Stage]:
        """Instantiate deferred plan items until ``stage_id`` exists."""
        if not self.workflow or not stage_id:
            return None
        found = self.workflow.stage(stage_id)
        if found:
            return found
        self._materialize_until(stage_id)
        return self.workflow.stage(stage_id)

    def _restore_stage_from_catalog(self, stage_id: str) -> Optional[Stage]:
        """Re-bind a missing transition target from the catalog fragment index."""
        from .plan_compiler import catalog_stage_index

        if not self.workflow or not stage_id or stage_id in _NON_STAGE_TARGETS:
            return None
        fragment = catalog_stage_index(self.catalog).get(stage_id)
        if not fragment:
            return None
        stage = Stage.from_dict(stage_plan_item(fragment))
        self.workflow.stages.append(stage)
        self.workflow.revision += 1
        self.workflow.compiled_plan = compiled_plan_summary(
            self.workflow.stages, self.workflow.deferred_plan
        )
        return stage

    def _materialize_until(self, stage_id: str) -> None:
        if not stage_id or stage_id in _NON_STAGE_TARGETS:
            return
        while self.workflow and self.workflow.deferred_plan:
            if self.workflow.stage(stage_id):
                return
            added = self._materialize_next_horizon()
            if not added:
                return

    def _materialize_next_horizon(self) -> list[Stage]:
        if not self.workflow:
            return []
        pending = list(self.workflow.deferred_plan or [])
        if not pending:
            return []
        horizon, rest = split_at_decision_boundary(pending)
        existing = {item.id for item in self.workflow.stages}
        added: list[Stage] = []
        for item in horizon:
            payload = stage_plan_item(item)
            stage_id = str(payload.get("id") or "")
            if not stage_id or stage_id in existing:
                continue
            stage = Stage.from_dict(payload)
            self.workflow.stages.append(stage)
            existing.add(stage_id)
            added.append(stage)
        self.workflow.deferred_plan = [stage_plan_item(item) for item in rest]
        self.workflow.compiled_plan = compiled_plan_summary(
            self.workflow.stages, self.workflow.deferred_plan
        )
        if added:
            self.workflow.revision += 1
        return added

    def _replan_remaining_tail(self) -> None:
        """Rebuild only the still-deferred tail; do not expand another horizon."""
        from .llm_planner import propose_plan

        if not self.workflow:
            return
        before = list(self.workflow.deferred_plan or [])
        proposal = None
        if tail_needs_replan_proposal(before):
            proposal = propose_plan(
                self.workflow.intent,
                None,
                catalog=self.catalog,
                event_sink=self._event,
            )
        self.workflow.deferred_plan = replan_tail(
            before, proposal=proposal, catalog=self.catalog
        )
        if proposal:
            self._event("plan_replanned", {
                "before": [
                    str(item.get("id") or "")
                    for item in before if isinstance(item, dict)
                ],
                "after": [
                    str(item.get("id") or "")
                    for item in self.workflow.deferred_plan
                    if isinstance(item, dict)
                ],
            })
        self.workflow.compiled_plan = compiled_plan_summary(
            self.workflow.stages, self.workflow.deferred_plan
        )

    def _replan_and_materialize(self) -> None:
        """Rebuild the unexecuted tail, then expose the next decision horizon."""
        self._replan_remaining_tail()
        self._materialize_next_horizon()

    def _transition(self, target: str) -> None:
        if target == "completed":
            # A terminal edge copied from a catalog fragment must never skip a
            # still-deferred decision horizon.  The compiler normally rewires
            # such edges; this is the runtime safety net for restored sessions
            # and malformed LLM proposals.
            if self.workflow.deferred_plan:
                next_stage = self._first_unfinished_active_stage()
                if next_stage is None:
                    added = self._materialize_next_horizon()
                    next_stage = added[0] if added else None
                if next_stage is not None:
                    self.workflow.current_stage = next_stage.id
                    self.workflow.execution_status = (
                        "waiting"
                        if next_stage.execution_status == "waiting"
                        else "pending"
                    )
                    self.workflow.outcome = ""
                    self._event("premature_completion_prevented", {
                        "next_stage": next_stage.id,
                        "deferred_stage_count": len(
                            self.workflow.deferred_plan or []
                        ),
                    })
                    self._activate_current()
                    return
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
            self._materialize_until(target)
        if not self.workflow.stage(target):
            self._restore_stage_from_catalog(target)
        if not self.workflow.stage(target):
            raise ValueError(f"Stage transition target does not exist: {target}")
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
#: 槽位同义键 -> 规范键。``knowledge.spec_requirements`` 直接复用这一份,
#: 避免两处手动同步走样。
_SLOT_ALIASES = {
    "device": "hardware",
    "sdr": "hardware",
    "radio": "hardware",
}
#: 只有这三类来源是确定性事实(安全边界与当前工程),LLM 不得改写;
#: 其余规则候选一律让位于 LLM 的原文解读(见 ``_merge``)。
_DETERMINISTIC_SLOT_SOURCES = frozenset({
    "safety_default", "safe_preview_default", "current_project",
})
_EXECUTION_MODES = frozenset({
    "design", "prepare", "configure", "deploy", "observe", "diagnose",
})

_PROMPT = """你是 DeepRadio 的 Intent 语义解析器。只输出一个 JSON 对象。
字段:
- goals / requested_operations / desired_artifacts / evidence_requirements
- constraints / decision_boundaries / stop_conditions
- execution_mode: 必填，只能是 design / prepare / configure / deploy / observe / diagnose
- task_type: 仅作兼容标签，不得用它改写 goals
- capabilities: 只能用给定集合
- forbidden_capabilities: 用户明确不要的能力
- slots: 文本里明确出现的参数
- turn_semantics: {read_only, confirmation_decision, recipe_switch_target}
- confidence: 0~1
规则:
- 独立根据用户原文识别目标，不要套固定任务模板。
- deterministic_context 只含协议/安全默认值和当前工程事实，不是意图分类结果。
- 否定、只仿真、不要硬件/射频 优先于关键词。
- 目标是现在发射/运行/部署: operation=deploy，不要加 stop_at_decision_boundary。
- execution_mode 是用户请求的最高执行效果，不是流程图方向。要求真实发射、启动、运行或部署硬件时必须为 deploy；只要求构建发射链/生成文件时为 design；只改硬件参数但不启动时为 configure；停在执行批准边界时为 prepare。
- “使用/通过某个真实 SDR 发射或发送一个信号”描述的是实际 RF 行为，必须为 deploy；只有明确要求 build/create/configure/save a transmit chain/flowgraph 而没有要求信号实际发射时才是 design/configure。不得把 transmit a signal 改写成 build a transmit chain。
- 已有未 arm 的硬件预览图时，「现在发射/运行 N 秒」是新一轮 deploy，复用当前工程参数，不要重建，也不要当成配置确认。
- 目标是保存配置或停在下一决策、尚未授权射频: operation=prepare，deploy_permission=pending，stop_at_decision_boundary；仍做只读预检。
- "configure ... for the <设备>, save the configuration, and stop at the transmission confirmation" 这类"给指定 SDR 配置并停在发射确认"的请求必须是 execution_mode=prepare、operation=prepare、deploy_permission=pending，并且要给出 hardware 槽位；不要判成 design 或纯粹的工程修改(modify_project)。
- 「不要发射」才是 deploy_permission=forbidden；与「先停在确认」不是同一件事。
- 配置/保存发射流图不是 TX_BUILD，也不是已经授权 RF_RUN。
- 不得把实时硬件观察改写成离线仿真。
- 不得因为载频像 2.4 GHz 就判定 BLE，除非用户说了 ble/蓝牙/广播。
- "transmit chain" / "transmit-only" means direction=tx; "receive chain" means direction=rx; a baseband / end-to-end / loopback simulation link with no separate tx-only or rx-only target means direction=sim. direction 必须在 tx / rx / sim 三者中取值,不要省略。
- slots must include every parameter explicitly stated in the current user text.
- slots 只能用规范键名: hardware(设备型号, 如 plutosdr/b210/hackrf), protocol, direction, modulation, carrier_frequency, sample_rate, bandwidth, duration_seconds, local_name, ble_mode, advertising_channels, payload, success_conditions, diagnosis_dimensions。设备型号必须写在 hardware 键下, 禁止用 device/sdr/radio 等同义键。
- 出现 diagnose 能力时必须给出 diagnosis_dimensions: 从 intent / environment / device / parameters / project / runtime / rf_path / signal 中选出本轮真正要排查的维度(数组)。判断依据是用户描述的现象,不要一次全选。
- deterministic_context.preserved_slots 里 slot_sources 为 safety_default/safe_preview_default/current_project 的是确定性安全与工程事实，不得改写；其余 preserved 值若与用户原文冲突，以你从原文的提取为准。
- 不得把 safety_default 时长改写成用户约束；用户没写时长就不要填 duration。prepare/配置确认不要写 30 秒。
- 不要发明 local_name。
- read_only 仅在用户明确要求只读、只诊断或不要修改时为 true；false 不能授予写权限。
- confirmation_decision 只能是 approved/rejected/none。
- recipe_switch_target 仅在用户明确要求切换现有工程配方时填写 allowed_recipe_ids 中的 canonical id。
"""


def complete_intent(
    rules_intent: WorkflowIntent,
    text: str,
    shared_state: Any,
    *,
    event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> WorkflowIntent:
    """Use the LLM as semantic authority and merge its structured result.

    Rules provide candidates and deterministic safety constraints only.  A
    production request never silently falls back to them when the LLM fails.
    """
    emit = event_sink or (lambda _event, _payload: None)
    try:
        from ..llm import (
            SemanticUnderstandingError,
            chat,
            get_config,
            intent_test_bypass_enabled,
            is_configured,
        )
    except Exception as exc:  # noqa: BLE001
        emit("intent_llm_failed", {
            "reason": "llm_import_unavailable",
            "error_type": type(exc).__name__,
        })
        raise RuntimeError("The semantic understanding service could not be loaded.") from exc
    if not is_configured():
        if intent_test_bypass_enabled():
            emit("intent_llm_test_bypass", {"reason": "test_runner"})
            for key, source in list(rules_intent.slot_sources.items()):
                if source == "rules":
                    rules_intent.slot_sources[key] = "user"
            return rules_intent
        emit("intent_llm_failed", {"reason": "not_configured"})
        raise SemanticUnderstandingError(
            "The language model is not configured. Your request was not interpreted."
        )
    preserved_slots = {
        key: value for key, value in rules_intent.slots.items()
        if rules_intent.slot_sources.get(key) in _DETERMINISTIC_SLOT_SOURCES
    }
    preserved_sources = {
        key: value for key, value in rules_intent.slot_sources.items()
        if value in _DETERMINISTIC_SLOT_SOURCES
    }
    payload = {
        "text": text,
        "deterministic_context": {
            "preserved_slots": preserved_slots,
            "slot_sources": preserved_sources,
            "forbidden_capabilities": list(
                (rules_intent.context or {}).get("forbidden_capabilities") or []
            ),
            "current_project": dict((rules_intent.context or {}).get("current_project") or {}),
        },
        "allowed_task_types": sorted(_TASK_TYPES),
        "allowed_capabilities": sorted(_CAPABILITIES),
        "allowed_recipe_ids": _known_recipe_ids(),
        "has_project": bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
        ),
    }
    request_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    model = ""
    try:
        model = str(get_config().get("model") or "")
    except Exception:  # noqa: BLE001
        pass
    emit("intent_llm_started", {
        "model": model,
        "request_hash": request_hash,
    })
    started_at = time.perf_counter()
    try:
        content = chat(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )
        from ..llm import parse_json_object

        parsed = parse_json_object(content)
        if not isinstance(parsed.get("slots"), dict):
            raise ValueError("Intent LLM response is missing the slots object")
        if not isinstance(parsed.get("capabilities"), list):
            raise ValueError("Intent LLM response is missing the capabilities list")
        if str(parsed.get("task_type") or "") not in _TASK_TYPES:
            raise ValueError("Intent LLM response has an invalid task_type")
        if str(parsed.get("execution_mode") or "").lower() not in _EXECUTION_MODES:
            if intent_test_bypass_enabled():
                # Legacy unit-test fixtures predate the production semantic
                # contract.  This bypass is unreachable in configured GUI
                # sessions and must never interpret real user text.
                parsed["execution_mode"] = "design"
            else:
                raise ValueError(
                    "Intent LLM response is missing a valid execution_mode"
                )
        parsed_slots = dict(parsed.get("slots") or {})
        structured_hardware = next(
            (
                parsed_slots.get(key)
                for key in ("hardware", "device", "sdr", "radio")
                if parsed_slots.get(key) not in (None, "", [])
            ),
            None,
        )
        needs_effect_adjudication = (
            not intent_test_bypass_enabled()
            and str(parsed.get("execution_mode") or "").lower() == "design"
            and structured_hardware is not None
            and str(parsed_slots.get("direction") or "").lower() == "tx"
        )
        if needs_effect_adjudication:
            adjudication_payload = {
                "user_text": text,
                "initial_execution_mode": parsed.get("execution_mode"),
                "structured_intent": {
                    "goals": parsed.get("goals") or [],
                    "requested_operations": parsed.get("requested_operations") or [],
                    "capabilities": parsed.get("capabilities") or [],
                    "slots": parsed_slots,
                },
            }
            adjudication_started = time.perf_counter()
            emit("intent_effect_adjudication_started", {
                "initial_execution_mode": parsed.get("execution_mode"),
            })
            adjudication_content = chat([
                {
                    "role": "system",
                    "content": (
                        "You adjudicate the requested execution effect for a radio "
                        "assistant. Return one JSON object with execution_mode and "
                        "reason. execution_mode must be design, prepare, configure, "
                        "deploy, observe, or diagnose. Decide from the original user "
                        "text: actual transmission/running on named hardware is "
                        "deploy; merely creating or saving a transmit chain or "
                        "flowgraph is design/configure. Do not reinterpret "
                        "'transmit a signal using hardware' as 'build a chain'."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        adjudication_payload, ensure_ascii=False
                    ),
                },
            ])
            adjudication = parse_json_object(adjudication_content)
            adjudicated_mode = str(
                adjudication.get("execution_mode") or ""
            ).lower()
            if adjudicated_mode not in _EXECUTION_MODES:
                raise ValueError(
                    "Effect adjudication returned an invalid execution_mode"
                )
            parsed["execution_mode"] = adjudicated_mode
            emit("intent_effect_adjudication_succeeded", {
                "initial_execution_mode": "design",
                "execution_mode": adjudicated_mode,
                "latency_ms": round(
                    (time.perf_counter() - adjudication_started) * 1000, 3
                ),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intent LLM failed; refusing rule-only interpretation: %s", exc)
        emit("intent_llm_failed", {
            "reason": "request_failed",
            "model": model,
            "request_hash": request_hash,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "error_type": type(exc).__name__,
        })
        raise SemanticUnderstandingError(
            "The language model did not complete semantic understanding. "
            "Your request was not replaced by a rule-based guess."
        ) from exc
    merged = _merge(rules_intent, parsed)
    project_intent_ir(merged)
    llm_task_type = str(parsed.get("task_type") or "")
    emit("intent_llm_succeeded", {
        "model": model,
        "request_hash": request_hash,
        "response_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
        "task_type": merged.task_type,
        # Audit the merge decision: the LLM tag is advisory only, so a
        # divergence (e.g. LLM says TX_BUILD, rules keep HARDWARE_CONFIGURE)
        # must be visible in the event stream instead of silently dropped.
        "llm_task_type": llm_task_type,
        "task_type_overridden": bool(
            llm_task_type and llm_task_type != merged.task_type
        ),
        "capabilities": list(merged.capabilities),
    })
    return merged


def _merge(rules: WorkflowIntent, parsed: dict[str, Any]) -> WorkflowIntent:
    task_type = str(parsed.get("task_type") or "")
    if task_type not in _TASK_TYPES:
        task_type = "END_TO_END_SIM"
    forbidden = {
        name for name in list(parsed.get("forbidden_capabilities") or [])
        + list((rules.context or {}).get("forbidden_capabilities") or [])
        if name in _CAPABILITIES
    }
    raw_caps = parsed.get("capabilities")
    capabilities = [name for name in raw_caps if name in _CAPABILITIES]
    capabilities = [name for name in capabilities if name not in forbidden]
    # Production never promotes regex/keyword candidates to user facts.  Only
    # host-owned safety defaults and current-project facts survive before the
    # LLM extraction is applied: values the regex layer labelled
    # "user"/"derived"/"protocol_default" are still guesses about the user's
    # text, and the LLM extraction of that same text is the authoritative
    # reading that must win on conflict.
    slots = {
        key: value for key, value in rules.slots.items()
        if rules.slot_sources.get(key) in _DETERMINISTIC_SLOT_SOURCES
    }
    sources = {
        key: value for key, value in rules.slot_sources.items()
        if value in _DETERMINISTIC_SLOT_SOURCES
    }
    llm_slots = dict(parsed.get("slots") or {})
    # Normalize common synonyms onto canonical keys so a `device` answer
    # lands in `hardware` instead of forking into duplicate spec rows.
    for _alias, _canonical in _SLOT_ALIASES.items():
        if _alias not in llm_slots:
            continue
        _alias_value = llm_slots.pop(_alias)
        if _alias_value in (None, "", []):
            continue
        if llm_slots.get(_canonical) in (None, "", []):
            llm_slots[_canonical] = _alias_value
    for key, value in llm_slots.items():
        if value in (None, "", []):
            continue
        if sources.get(key) in _DETERMINISTIC_SLOT_SOURCES:
            continue
        slots[key] = value
        sources[key] = "llm"
    requested_operations = _normalize_requested_operations(
        parsed.get("requested_operations")
    )
    execution_mode = str(parsed.get("execution_mode") or "").lower()
    _apply_semantic_defaults(
        slots,
        sources,
        capabilities,
        context=dict(rules.context or {}),
        requested_operations=requested_operations,
        execution_mode=execution_mode,
    )
    checkpoint_prepare = (
        slots.get("operation") == "prepare"
        and slots.get("deploy_permission") == "pending"
    )
    if checkpoint_prepare:
        forbidden.discard("hardware_runtime")
        for name in ("hardware_configure", "hardware_runtime"):
            if name not in capabilities:
                capabilities.append(name)
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(confidence, 0.0))
    context = dict(rules.context)
    context["execution_mode"] = execution_mode
    if forbidden:
        context["forbidden_capabilities"] = sorted(forbidden)
    context["turn_semantics"] = _normalize_turn_semantics(parsed)
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
        goals=_string_list(parsed.get("goals"), []),
        requested_operations=requested_operations,
        desired_artifacts=_string_list(
            parsed.get("desired_artifacts"), []
        ),
        evidence_requirements=_string_list(
            parsed.get("evidence_requirements"), []
        ),
        constraints=(
            dict(parsed.get("constraints") or {})
            if isinstance(parsed.get("constraints"), dict)
            else {}
        ),
        forbidden_effects=_string_list(
            parsed.get("forbidden_effects"), []
        ),
        decision_boundaries=_string_list(
            parsed.get("decision_boundaries"), []
        ),
        stop_conditions=_string_list(
            parsed.get("stop_conditions"), []
        ),
        # Entities are re-derived from the merged slots by project_intent_ir.
        # Falling back to rules.entities here leaked stale regex guesses (for
        # example local_name="of") next to the LLM's corrected parameter.
        entities=(
            dict(parsed.get("entities") or {})
            if isinstance(parsed.get("entities"), dict)
            else {}
        ),
    )


def _known_recipe_ids() -> list[str]:
    try:
        from ..knowledge import recipes

        return sorted(str(name) for name in recipes.RECIPES)
    except Exception:  # noqa: BLE001
        return []


def _normalize_turn_semantics(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw = payload.get("turn_semantics")
    data = dict(raw) if isinstance(raw, Mapping) else {}
    decision = str(
        data.get("confirmation_decision")
        or payload.get("confirmation_decision")
        or "none"
    ).lower()
    if decision not in {"approved", "rejected", "none"}:
        decision = "none"
    target = str(
        data.get("recipe_switch_target")
        or payload.get("recipe_switch_target")
        or ""
    ).strip()
    if target not in _known_recipe_ids():
        target = ""
    read_only = data.get("read_only", payload.get("read_only"))
    return {
        "read_only": read_only is True,
        "confirmation_decision": decision,
        "recipe_switch_target": target,
    }


def _apply_semantic_defaults(
    slots: Dict[str, Any],
    sources: Dict[str, str],
    capabilities: list[str],
    *,
    context: Dict[str, Any],
    requested_operations: list[str],
    execution_mode: str = "",
) -> None:
    """Derive protocol/safety facts only after the LLM established semantics."""
    operations = {str(item).lower() for item in requested_operations or []}
    if execution_mode == "deploy":
        slots["operation"] = "deploy"
        sources["operation"] = "llm"
    elif execution_mode == "prepare":
        slots["operation"] = "prepare"
        sources["operation"] = "llm"
    elif execution_mode == "configure":
        slots["operation"] = "configure"
        sources["operation"] = "llm"
    elif "deploy" in capabilities or "deploy" in operations:
        slots.setdefault("operation", "deploy")
        sources.setdefault("operation", "derived")
    operation = str(slots.get("operation") or "").lower()
    if slots.get("direction") not in (None, "", []):
        slots["direction"] = normalize_direction(slots.get("direction"))
    elif "build_signal" in capabilities and not slots.get("hardware"):
        # V2 §6.1 把 direction 列为结构槽位,下游 recipe 选型和 spec 卡片都要读它。
        # 这里只从 **LLM 自己给出的 capabilities** 派生,不复活正则猜测:
        # 有 build_signal 又没有硬件,就是基带端到端仿真链路 => sim。
        slots["direction"] = "sim"
        sources["direction"] = "derived"
    current = dict(context.get("current_project") or {})
    if operation in {"deploy", "prepare"}:
        for key in (
            "hardware", "direction", "carrier_frequency", "sample_rate",
            "bandwidth", "tx_gain", "tx_attenuation",
        ):
            value = current.get(key)
            if slots.get(key) in (None, "", []) and value not in (None, "", []):
                slots[key] = value
                sources[key] = "current_project"
    protocol = str(slots.get("protocol") or "").lower()
    if protocol == "ble":
        defaults = {
            "ble_mode": "advertising",
            "modulation": "gfsk",
            "advertising_channels": [37],
            "carrier_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
        }
        for key, value in defaults.items():
            if slots.get(key) in (None, "", []):
                slots[key] = value
                sources[key] = "protocol_default"
    hardware = str(slots.get("hardware") or "").lower()
    if hardware and operation in {"deploy", "prepare"}:
        for capability in ("hardware_configure", "hardware_runtime"):
            if capability not in capabilities:
                capabilities.append(capability)
    if protocol and "protocol" not in capabilities:
        capabilities.append("protocol")
    if operation == "deploy" and "deploy" not in capabilities:
        capabilities.append("deploy")
    if operation == "prepare":
        slots.setdefault("deploy_permission", "pending")
        sources.setdefault("deploy_permission", "derived")
    if operation == "deploy":
        slots.setdefault("deploy_permission", "requested")
        sources.setdefault("deploy_permission", "derived")
        slots.setdefault("duration_seconds", 30.0)
        slots.setdefault("max_duration_seconds", slots["duration_seconds"])
        sources.setdefault("duration_seconds", "safety_default")
        sources.setdefault("max_duration_seconds", "safety_default")
    if slots.get("sample_rate") not in (None, "", []):
        slots.setdefault("bandwidth", slots["sample_rate"])
        sources.setdefault("bandwidth", "derived")
    if hardware in {"pluto", "plutosdr", "adalm-pluto"}:
        slots.setdefault("tx_attenuation", 30.0)
        sources.setdefault("tx_attenuation", "safety_default")


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback or [])
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or list(fallback or [])


def _normalize_requested_operations(value: Any) -> list[str]:
    """Flatten LLM operation objects into comparable short strings.

    The intent LLM sometimes returns ``requested_operations`` as objects
    (``{"operation": "prepare", "capability": "build_tx", ...}``);
    stringifying them wholesale produced python-repr blobs that never
    matched downstream ``"deploy" in operations`` checks and were persisted
    into the IntentIR as opaque strings.
    """
    items = value if isinstance(value, list) else []
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = str(
                item.get("operation")
                or item.get("capability")
                or item.get("tool")
                or item.get("action")
                or ""
            ).strip()
        else:
            text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out
